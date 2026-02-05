import os
import threading
import time
import queue
from dataclasses import dataclass
from typing import Optional

import numpy as np
import resampy
import cv2
from daily import Daily, CallClient

from logger import logger

_DAILY_INIT_LOCK = threading.Lock()
_DAILY_INIT_DONE = False


def _ensure_daily_init() -> None:
    global _DAILY_INIT_DONE
    if _DAILY_INIT_DONE:
        return
    with _DAILY_INIT_LOCK:
        if _DAILY_INIT_DONE:
            return
        worker_threads = int(os.getenv("DAILY_WORKER_THREADS", "4"))
        try:
            Daily.init(worker_threads=worker_threads)
        except Exception as exc:
            logger.info("Daily.init failed: %s", exc)
        _DAILY_INIT_DONE = True


@dataclass
class DailyBotConfig:
    room_url: str
    meeting_token: str
    width: int
    height: int
    fps: int = 25
    sample_rate: int = 16000
    user_name: str = "avatar-bot"
    video_quality: str = "high"
    preferred_codec: str = "H264"
    audio_bitrate: int = 64000
    quality_auto: bool = True


class DailyBot:
    def __init__(self, config: DailyBotConfig):
        _ensure_daily_init()
        self.config = config
        self.client = CallClient()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._error: Optional[str] = None
        self._input_sample_rate = 16000
        self._target_width = int(config.width)
        self._target_height = int(config.height)
        self._audio_queue = queue.Queue(maxsize=int(os.getenv("DAILY_AUDIO_QUEUE", "200")))
        self._audio_backlog = int(os.getenv("DAILY_AUDIO_MAX_BACKLOG", "5"))
        self._audio_thread = threading.Thread(target=self._audio_loop, daemon=True)
        self._quality_thread: Optional[threading.Thread] = None
        self._quality_controller: Optional["QualityController"] = None

        try:
            # Create virtual devices.
            self.camera = Daily.create_camera_device(
                f"nerf-camera-{int(time.time()*1000)}",
                config.width,
                config.height,
                color_format="RGB",
            )
            self.microphone = Daily.create_microphone_device(
                f"nerf-mic-{int(time.time()*1000)}",
                sample_rate=config.sample_rate,
                channels=1,
                non_blocking=True,
            )
        except Exception as exc:
            self._error = str(exc)
            logger.error("Daily device init failed: %s", exc)
            self._ready.set()
            return

        self._join_thread = threading.Thread(target=self._join, daemon=True)
        self._join_thread.start()
        self._audio_thread.start()

    def _join(self) -> None:
        def _on_joined(join_data, error):
            if error:
                self._error = str(error)
                logger.error("Daily join error: %s", error)
                return
            self._ready.set()
            logger.info("Daily bot joined room")
            if self.config.quality_auto:
                self._start_quality_controller()

        try:
            self.client.set_user_name(self.config.user_name)
            client_settings = {
                "inputs": {
                    "camera": {
                        "isEnabled": True,
                        "settings": {
                            "deviceId": self.camera.name,
                            "frameRate": self.config.fps,
                            "width": self.config.width,
                            "height": self.config.height,
                        },
                    },
                    "microphone": {
                        "isEnabled": True,
                        "settings": {
                            "deviceId": self.microphone.name,
                        },
                    },
                },
                "publishing": {
                    "camera": {
                        "isPublishing": True,
                        "sendSettings": {
                            "maxQuality": self.config.video_quality,
                            "preferredCodec": self.config.preferred_codec,
                        },
                    },
                    "microphone": {
                        "isPublishing": True,
                        "sendSettings": {
                            "bitrate": self.config.audio_bitrate,
                            "channelConfig": "mono",
                        },
                    },
                },
            }
            self.client.join(
                self.config.room_url,
                meeting_token=self.config.meeting_token,
                client_settings=client_settings,
                completion=_on_joined,
            )
        except Exception as exc:
            self._error = str(exc)
            logger.error("Daily join exception: %s", exc)

    def wait_ready(self, timeout: float = 15.0) -> bool:
        return self._ready.wait(timeout)

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def error(self) -> Optional[str]:
        return self._error

    def send_video(self, frame: np.ndarray) -> None:
        if not self.ready or self._closed.is_set():
            return
        if frame is None:
            return
        if frame.shape[1] != self._target_width or frame.shape[0] != self._target_height:
            # Letterbox resize to match target resolution without distortion.
            h, w = frame.shape[:2]
            scale = min(self._target_width / w, self._target_height / h)
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
            canvas = np.zeros((self._target_height, self._target_width, 3), dtype=resized.dtype)
            x0 = (self._target_width - new_w) // 2
            y0 = (self._target_height - new_h) // 2
            canvas[y0:y0 + new_h, x0:x0 + new_w] = resized
            frame = canvas
        if frame.dtype != np.uint8:
            frame = frame.astype(np.uint8)
        if not frame.flags["C_CONTIGUOUS"]:
            frame = np.ascontiguousarray(frame)
        with self._lock:
            try:
                self.camera.write_frame(frame.tobytes())
            except Exception as exc:
                logger.debug("Daily video write failed: %s", exc)

    def send_audio(self, pcm: np.ndarray) -> None:
        if not self.ready or self._closed.is_set():
            return
        if pcm is None:
            return
        if pcm.dtype != np.int16:
            pcm = pcm.astype(np.int16)
        if not pcm.flags["C_CONTIGUOUS"]:
            pcm = np.ascontiguousarray(pcm)
        try:
            self._audio_queue.put_nowait(pcm)
        except queue.Full:
            # Drop oldest frame to keep latency bounded.
            try:
                _ = self._audio_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._audio_queue.put_nowait(pcm)
            except queue.Full:
                pass

    def _audio_loop(self) -> None:
        next_time = time.perf_counter()
        while not self._closed.is_set():
            try:
                pcm = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                next_time = time.perf_counter()
                continue

            # Drop backlog if we've accumulated too much audio.
            try:
                while self._audio_queue.qsize() > self._audio_backlog:
                    _ = self._audio_queue.get_nowait()
            except queue.Empty:
                pass

            if self.config.sample_rate != self._input_sample_rate:
                audio = pcm.astype(np.float32) / 32767.0
                audio = resampy.resample(audio, self._input_sample_rate, self.config.sample_rate)
                pcm_out = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
            else:
                pcm_out = pcm

            if not pcm_out.flags["C_CONTIGUOUS"]:
                pcm_out = np.ascontiguousarray(pcm_out)

            duration = pcm_out.shape[0] / float(self.config.sample_rate)
            now = time.perf_counter()
            if now < next_time:
                time.sleep(next_time - now)
            else:
                next_time = now

            with self._lock:
                try:
                    self.microphone.write_frames(pcm_out.tobytes())
                except Exception as exc:
                    logger.debug("Daily audio write failed: %s", exc)
            next_time += duration

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self.client.leave()
        except Exception:
            pass
        try:
            self.client.release()
        except Exception:
            pass

    def _start_quality_controller(self) -> None:
        try:
            controller = QualityController(self.client, self._closed)
            self._quality_controller = controller
            self._quality_thread = threading.Thread(target=controller.run, daemon=True)
            self._quality_thread.start()
        except Exception as exc:
            logger.debug("Quality controller init failed: %s", exc)


class QualityController:
    def __init__(self, client: CallClient, stop_event: threading.Event):
        self.client = client
        self.stop_event = stop_event
        self.profile = None
        self.good_streak = 0
        self.warn_streak = 0
        self.bad_streak = 0
        self.interval = float(os.getenv("DAILY_QUALITY_INTERVAL", "2.0"))
        self.good_threshold = int(os.getenv("DAILY_QUALITY_GOOD_STREAK", "8"))
        self.warn_threshold = int(os.getenv("DAILY_QUALITY_WARN_STREAK", "1"))
        self.bad_threshold = int(os.getenv("DAILY_QUALITY_BAD_STREAK", "2"))

    def run(self) -> None:
        # Start balanced to avoid spikes on join.
        self.apply_profile("med", reason="boot")
        while not self.stop_event.is_set():
            try:
                stats = self.client.get_network_stats()
            except Exception:
                time.sleep(self.interval)
                continue
            state = self._extract_state(stats)
            if state in ("bad", "poor", "very-bad", "very-poor"):
                self.bad_streak += 1
                self.warn_streak = 0
                self.good_streak = 0
            elif state in ("warning", "warn", "fair"):
                self.warn_streak += 1
                self.bad_streak = 0
                self.good_streak = 0
            elif state in ("good", "excellent"):
                self.good_streak += 1
                self.warn_streak = 0
                self.bad_streak = 0
            else:
                self.good_streak = 0
                self.warn_streak = 0
                self.bad_streak = 0

            if self.bad_streak >= self.bad_threshold:
                self.apply_profile("low", reason="bad")
            elif self.warn_streak >= self.warn_threshold:
                self.apply_profile("med", reason="warn")
            elif self.good_streak >= self.good_threshold:
                self.apply_profile("high", reason="good")

            time.sleep(self.interval)

    def _extract_state(self, stats: Optional[dict]) -> str:
        if not stats:
            return "unknown"
        if isinstance(stats, dict):
            if "networkState" in stats:
                return str(stats.get("networkState") or "unknown").lower()
            if "quality" in stats:
                return str(stats.get("quality") or "unknown").lower()
            if "stats" in stats and isinstance(stats["stats"], dict):
                inner = stats["stats"]
                if "networkState" in inner:
                    return str(inner.get("networkState") or "unknown").lower()
        return "unknown"

    def apply_profile(self, profile: str, reason: str = "") -> None:
        if profile == self.profile:
            return
        publish = self._publish_settings(profile)
        try:
            self.client.update_publishing(publish)
        except Exception as exc:
            logger.debug("update_publishing failed: %s", exc)
        try:
            self.client.send_app_message({"type": "QUALITY_PROFILE", "profile": profile, "reason": reason})
        except Exception as exc:
            logger.debug("send_app_message failed: %s", exc)
        self.profile = profile
        logger.info("quality profile -> %s (%s)", profile, reason)

    def _publish_settings(self, profile: str) -> dict:
        if profile == "high":
            cam = {"sendSettings": {"maxQuality": "high"}}
            mic = {"sendSettings": {"bitrate": 96000}}
        elif profile == "med":
            cam = {"sendSettings": {"maxQuality": "medium"}}
            mic = {"sendSettings": {"bitrate": 64000}}
        else:
            cam = {"sendSettings": {"maxQuality": "low"}}
            mic = {"sendSettings": {"bitrate": 32000}}
        return {"camera": cam, "microphone": mic}
