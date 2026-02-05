###############################################################################
#  Copyright (C) 2024 LiveTalking@lipku https://github.com/lipku/LiveTalking
#  email: lipku@foxmail.com
# 
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  
#       http://www.apache.org/licenses/LICENSE-2.0
# 
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
###############################################################################

import asyncio
import json
import logging
import threading
import time
from typing import Tuple, Dict, Optional, Set, Union
from av.frame import Frame
from av.packet import Packet
from av import AudioFrame
import fractions
import numpy as np

SAMPLE_RATE = 16000
AUDIO_PTIME = 0.020  # 20ms audio packetization
VIDEO_CLOCK_RATE = 90000
VIDEO_PTIME = 0.040 #1 / 25  # 30fps
VIDEO_TIME_BASE = fractions.Fraction(1, VIDEO_CLOCK_RATE)
AUDIO_TIME_BASE = fractions.Fraction(1, SAMPLE_RATE)
MAX_VIDEO_QUEUE = 8
MAX_AUDIO_QUEUE = 120
MAX_VIDEO_LATE = 0.12  # seconds before we drop video frames to catch up

#from aiortc.contrib.media import MediaPlayer, MediaRelay
#from aiortc.rtcrtpsender import RTCRtpSender
from aiortc import (
    MediaStreamTrack,
)

logging.basicConfig()
logger = logging.getLogger(__name__)
from logger import logger as mylogger


class PlayerStreamTrack(MediaStreamTrack):
    """
    A video track that returns an animated flag.
    """

    def __init__(self, player, kind):
        super().__init__()  # don't forget this!
        self.kind = kind
        self._player = player
        self._queue = asyncio.Queue()
        self.timelist = [] #记录最近包的时间戳
        self.current_frame_count = 0
        self._start = None
        self._timestamp = 0
        self._frame_index = 0
        self._audio_samples = 0
        self._last_frame = None
        self._dropped = 0
        self._last_drop_log = 0.0
        if self.kind == 'video':
            self.framecount = 0
            self.lasttime = time.perf_counter()
            self.totaltime = 0
    
    _start: float
    _timestamp: int

    def _ensure_start(self) -> None:
        if self._start is not None:
            return
        shared_start = getattr(self._player, "_start_time", None)
        if shared_start is None:
            shared_start = time.perf_counter()
            self._player._start_time = shared_start
        self._start = shared_start
        self._timestamp = 0
        self._frame_index = 0
        self._audio_samples = 0
        self.timelist.append(self._start)
        if self.kind == 'video':
            mylogger.info('video start:%f', self._start)
        else:
            mylogger.info('audio start:%f', self._start)

    def _log_drops(self, dropped: int) -> None:
        if dropped <= 0:
            return
        now = time.perf_counter()
        if now - self._last_drop_log >= 5.0:
            mylogger.warning("webrtc %s drop=%d q=%d", self.kind, dropped, self._queue.qsize())
            self._last_drop_log = now

    async def _next_audio_timestamp(self, frame: AudioFrame) -> Tuple[int, fractions.Fraction]:
        self._ensure_start()
        target_time = self._start + (self._audio_samples / SAMPLE_RATE)
        wait = target_time - time.perf_counter()
        if wait > 0:
            await asyncio.sleep(wait)
        pts = self._audio_samples
        self._audio_samples += int(frame.samples)
        return pts, AUDIO_TIME_BASE

    async def _next_video_timestamp(self) -> Tuple[int, fractions.Fraction]:
        self._ensure_start()
        target_time = self._start + (self._frame_index * VIDEO_PTIME)
        wait = target_time - time.perf_counter()
        if wait > 0:
            await asyncio.sleep(wait)
        pts = int(self._frame_index * VIDEO_PTIME * VIDEO_CLOCK_RATE)
        self._frame_index += 1
        return pts, VIDEO_TIME_BASE

    def _drop_audio_backlog(self) -> None:
        if self._queue.qsize() <= MAX_AUDIO_QUEUE:
            return
        dropped = 0
        while self._queue.qsize() > MAX_AUDIO_QUEUE // 2:
            try:
                self._queue.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        self._log_drops(dropped)

    def _drop_video_backlog(self, now: float) -> None:
        if self._start is None:
            return
        dropped = 0
        if self._queue.qsize() > MAX_VIDEO_QUEUE:
            to_drop = max(0, self._queue.qsize() - 2)
            for _ in range(to_drop):
                try:
                    self._queue.get_nowait()
                    dropped += 1
                except asyncio.QueueEmpty:
                    break
        target_index = int((now - self._start) / VIDEO_PTIME)
        if target_index > self._frame_index + 1:
            late_frames = target_index - self._frame_index - 1
            if late_frames > 0 and self._queue.qsize() > 1:
                for _ in range(min(late_frames, self._queue.qsize() - 1)):
                    try:
                        self._queue.get_nowait()
                        dropped += 1
                    except asyncio.QueueEmpty:
                        break
                self._frame_index = max(self._frame_index, target_index)
        if dropped:
            self._dropped += dropped
            self._log_drops(dropped)

    async def recv(self) -> Union[Frame, Packet]:
        # frame = self.frames[self.counter % 30]            
        self._player._start(self)
        eventpoint = None
        if self.kind == 'audio':
            self._drop_audio_backlog()
            try:
                frame, eventpoint = await asyncio.wait_for(self._queue.get(), timeout=AUDIO_PTIME)
            except asyncio.TimeoutError:
                audio = np.zeros((1, int(SAMPLE_RATE * AUDIO_PTIME)), dtype=np.int16)
                frame = AudioFrame.from_ndarray(audio, layout='mono', format='s16')
                frame.sample_rate = SAMPLE_RATE
            pts, time_base = await self._next_audio_timestamp(frame)
        else:
            now = time.perf_counter()
            self._ensure_start()
            if now - self._start > MAX_VIDEO_LATE:
                self._drop_video_backlog(now)
            try:
                frame, _ = await asyncio.wait_for(self._queue.get(), timeout=VIDEO_PTIME)
                self._last_frame = frame
            except asyncio.TimeoutError:
                frame = self._last_frame
                if frame is None:
                    frame, _ = await self._queue.get()
                    self._last_frame = frame
            pts, time_base = await self._next_video_timestamp()
        frame.pts = pts
        frame.time_base = time_base
        if eventpoint:
            self._player.notify(eventpoint)
        if frame is None:
            self.stop()
            raise Exception
        if self.kind == 'video':
            self.totaltime += (time.perf_counter() - self.lasttime)
            self.framecount += 1
            self.lasttime = time.perf_counter()
            if self.framecount==100:
                mylogger.info(f"------actual avg final fps:{self.framecount/self.totaltime:.4f}")
                self.framecount = 0
                self.totaltime=0
        return frame
    
    def stop(self):
        super().stop()
        if self._player is not None:
            self._player._stop(self)
            self._player = None

def player_worker_thread(
    quit_event,
    loop,
    container,
    audio_track,
    video_track
):
    container.render(quit_event,loop,audio_track,video_track)

class HumanPlayer:

    def __init__(
        self, nerfreal, format=None, options=None, timeout=None, loop=False, decode=True
    ):
        self.__thread: Optional[threading.Thread] = None
        self.__thread_quit: Optional[threading.Event] = None

        # examine streams
        self.__started: Set[PlayerStreamTrack] = set()
        self.__audio: Optional[PlayerStreamTrack] = None
        self.__video: Optional[PlayerStreamTrack] = None

        self.__audio = PlayerStreamTrack(self, kind="audio")
        self.__video = PlayerStreamTrack(self, kind="video")

        self.__container = nerfreal
        self._start_time = None

    def notify(self,eventpoint):
        self.__container.notify(eventpoint)

    @property
    def audio(self) -> MediaStreamTrack:
        """
        A :class:`aiortc.MediaStreamTrack` instance if the file contains audio.
        """
        return self.__audio

    @property
    def video(self) -> MediaStreamTrack:
        """
        A :class:`aiortc.MediaStreamTrack` instance if the file contains video.
        """
        return self.__video

    def _start(self, track: PlayerStreamTrack) -> None:
        self.__started.add(track)
        if self.__thread is None:
            self.__log_debug("Starting worker thread")
            self.__thread_quit = threading.Event()
            self.__thread = threading.Thread(
                name="media-player",
                target=player_worker_thread,
                args=(
                    self.__thread_quit,
                    asyncio.get_event_loop(),
                    self.__container,
                    self.__audio,
                    self.__video                   
                ),
            )
            self.__thread.start()

    def _stop(self, track: PlayerStreamTrack) -> None:
        self.__started.discard(track)

        if not self.__started and self.__thread is not None:
            self.__log_debug("Stopping worker thread")
            self.__thread_quit.set()
            self.__thread.join()
            self.__thread = None
            self._start_time = None

        if not self.__started and self.__container is not None:
            #self.__container.close()
            self.__container = None

    def __log_debug(self, msg: str, *args) -> None:
        mylogger.debug(f"HumanPlayer {msg}", *args)
