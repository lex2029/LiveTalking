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

# server.py
from flask import Flask, render_template,send_from_directory,request, jsonify
from flask_sockets import Sockets
import base64
import json
#import gevent
#from gevent import pywsgi
#from geventwebsocket.handler import WebSocketHandler
import re
import numpy as np
from threading import Thread,Event
#import multiprocessing
import torch.multiprocessing as mp

from aiohttp import web
import aiohttp
import aiohttp_cors
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.rtcrtpsender import RTCRtpSender
from aiortc.rtcrtpparameters import RTCRtpEncodingParameters
from webrtc import HumanPlayer
from basereal import BaseReal
from llm import llm_response

import argparse
import random
import shutil
import asyncio
import torch
import os
import time
from typing import Dict, Optional
from logger import logger
from daily_bot import DailyBot, DailyBotConfig
import uuid


def _enable_h264_nvenc() -> bool:
    try:
        import fractions
        import av
        import aiortc.codecs as codecs
        import aiortc.codecs.h264 as h264
    except Exception as exc:
        logger.info("NVENC init skipped: %s", exc)
        return False

    try:
        av.CodecContext.create("h264_nvenc", "w")
    except av.AVError as exc:
        logger.info("NVENC not available: %s", exc)
        return False

    class H264EncoderNVENC(h264.H264Encoder):
        def _encode_frame(self, frame, force_keyframe: bool):
            if self.codec and (
                frame.width != self.codec.width
                or frame.height != self.codec.height
                or abs(self.target_bitrate - self.codec.bit_rate) / self.codec.bit_rate
                > 0.1
            ):
                self.buffer_data = b""
                self.buffer_pts = None
                self.codec = None

            if force_keyframe:
                frame.pict_type = av.video.frame.PictureType.I
            else:
                frame.pict_type = av.video.frame.PictureType.NONE

            if self.codec is None:
                try:
                    codec = av.CodecContext.create("h264_nvenc", "w")
                except av.AVError:
                    codec = av.CodecContext.create("libx264", "w")

                codec.width = frame.width
                codec.height = frame.height
                codec.bit_rate = self.target_bitrate
                codec.pix_fmt = "yuv420p"
                codec.framerate = fractions.Fraction(h264.MAX_FRAME_RATE, 1)
                codec.time_base = fractions.Fraction(1, h264.MAX_FRAME_RATE)

                if codec.name == "h264_nvenc":
                    try:
                        codec.options = {
                            "preset": "p3",
                            "rc": "cbr",
                            "bf": "0",
                            "g": "60",
                            "tune": "ll",
                        }
                    except Exception:
                        codec.options = {}
                    try:
                        codec.profile = "baseline"
                    except Exception:
                        pass
                else:
                    codec.options = {
                        "level": "31",
                        "tune": "zerolatency",
                    }
                    codec.profile = "Baseline"

                self.codec = codec

            data_to_send = b""
            for package in self.codec.encode(frame):
                data_to_send += bytes(package)

            if data_to_send:
                yield from self._split_bitstream(data_to_send)

    h264.H264Encoder = H264EncoderNVENC
    codecs.H264Encoder = H264EncoderNVENC
    logger.info("Using NVENC H264 encoder")
    return True


_H264_NVENC_ENABLED = _enable_h264_nvenc()


app = Flask(__name__)
#sockets = Sockets(app)
nerfreals:Dict[int, BaseReal] = {} #sessionid:BaseReal
opt = None
model = None
avatar = None
_default_config = {
    "openai_key": "",
    "openai_base": "",
    "openai_model": "",
    "openai_tts_model": "",
    "openai_tts_voice": "",
    "openai_tts_format": "",
    "openai_tts_speed": None,
    "openai_tts_sample_rate": None,
    "assemblyai_key": "",
    "eleven_key": "",
    "eleven_voice": "",
    "eleven_model": "",
    "eleven_latency": None,
    "eleven_output_format": "",
    "eleven_speed": None,
}

# WebRTC quality presets (bitrate in bps).
QUALITY_PROFILES = {
    "emergency": {
        "max_bitrate": 80_000,
        "max_fps": 8,
        "scale": 3.0,
        "audio_bitrate": 16_000,
    },
    "very_low": {
        "max_bitrate": 150_000,
        "max_fps": 10,
        "scale": 2.5,
        "audio_bitrate": 20_000,
    },
    "low": {
        "max_bitrate": 350_000,
        "max_fps": 15,
        "scale": 1.5,
        "audio_bitrate": 24_000,
    },
    "balanced": {
        "max_bitrate": 800_000,
        "max_fps": 20,
        "scale": 1.0,
        "audio_bitrate": 32_000,
    },
    "high": {
        "max_bitrate": 1_600_000,
        "max_fps": 25,
        "scale": 1.0,
        "audio_bitrate": 48_000,
    },
}


def _apply_video_quality(sender: RTCRtpSender, quality: str) -> None:
    profile = QUALITY_PROFILES.get((quality or "").lower())
    if not profile:
        return
    if not hasattr(sender, "getParameters") or not hasattr(sender, "setParameters"):
        # Older aiortc versions don't support sender parameters; skip.
        return
    params = sender.getParameters()
    if not params.encodings:
        params.encodings = [RTCRtpEncodingParameters()]
    enc = params.encodings[0]
    if profile.get("max_bitrate"):
        enc.maxBitrate = int(profile["max_bitrate"])
    if profile.get("max_fps"):
        enc.maxFramerate = int(profile["max_fps"])
    if profile.get("scale"):
        enc.scaleResolutionDownBy = float(profile["scale"])
    sender.setParameters(params)

def _apply_audio_quality(sender: RTCRtpSender, quality: str) -> None:
    profile = QUALITY_PROFILES.get((quality or "").lower())
    if not profile:
        return
    if not hasattr(sender, "getParameters") or not hasattr(sender, "setParameters"):
        return
    params = sender.getParameters()
    if not params.encodings:
        params.encodings = [RTCRtpEncodingParameters()]
    enc = params.encodings[0]
    if profile.get("audio_bitrate"):
        enc.maxBitrate = int(profile["audio_bitrate"])
    sender.setParameters(params)

def _parse_fmtp(params: str) -> dict:
    result = {}
    if not params:
        return result
    for item in params.split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            key, value = item.split("=", 1)
            result[key.strip()] = value.strip()
        else:
            result[item] = "1"
    return result

def _format_fmtp(params: dict) -> str:
    items = []
    for key in sorted(params.keys()):
        value = params[key]
        if value is None or value == "":
            items.append(key)
        else:
            items.append(f"{key}={value}")
    return ";".join(items)

def _tune_audio_sdp(sdp: str) -> str:
    lines = sdp.splitlines()
    opus_pts = []
    for line in lines:
        if line.startswith("a=rtpmap:") and " opus/" in line.lower():
            pt = line.split(":", 1)[1].split(" ", 1)[0]
            opus_pts.append(pt)

    if not opus_pts:
        return sdp

    tuned = []
    opus_pt_set = set(opus_pts)
    inserted = set()
    for idx, line in enumerate(lines):
        if line.startswith("a=fmtp:"):
            pt = line.split(":", 1)[1].split(" ", 1)[0]
            if pt in opus_pt_set:
                params = ""
                if " " in line:
                    params = line.split(" ", 1)[1]
                fmtp = _parse_fmtp(params)
                fmtp.update(
                    {
                        "useinbandfec": "1",
                        "cbr": "1",
                        "maxaveragebitrate": "64000",
                        "maxplaybackrate": "16000",
                        "minptime": "10",
                        "maxptime": "20",
                        "ptime": "20",
                        "stereo": "0",
                    }
                )
                line = f"a=fmtp:{pt} {_format_fmtp(fmtp)}"
                inserted.add(pt)
        tuned.append(line)
        if line.startswith("a=rtpmap:"):
            pt = line.split(":", 1)[1].split(" ", 1)[0]
            if pt in opus_pt_set and pt not in inserted:
                fmtp = _format_fmtp(
                    {
                        "useinbandfec": "1",
                        "cbr": "1",
                        "maxaveragebitrate": "64000",
                        "maxplaybackrate": "16000",
                        "minptime": "10",
                        "maxptime": "20",
                        "ptime": "20",
                        "stereo": "0",
                    }
                )
                tuned.append(f"a=fmtp:{pt} {fmtp}")
                inserted.add(pt)
    return "\r\n".join(tuned) + "\r\n"


def _apply_video_overrides(sender: RTCRtpSender, overrides: dict) -> None:
    if not hasattr(sender, "getParameters") or not hasattr(sender, "setParameters"):
        return
    params = sender.getParameters()
    if not params.encodings:
        params.encodings = [RTCRtpEncodingParameters()]
    enc = params.encodings[0]
    if overrides.get("max_bitrate") is not None:
        enc.maxBitrate = int(overrides["max_bitrate"])
    if overrides.get("max_fps") is not None:
        enc.maxFramerate = int(overrides["max_fps"])
    if overrides.get("scale") is not None:
        enc.scaleResolutionDownBy = float(overrides["scale"])
    sender.setParameters(params)

def _load_secrets(path: str):
    if not path:
        return
    try:
        secrets_path = os.path.expanduser(path)
        if not os.path.isfile(secrets_path):
            return
        with open(secrets_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.info(f"Failed to load secrets file: {e}")
        return

    def _pick(*keys):
        for k in keys:
            if k in data and data[k] not in (None, ""):
                return str(data[k]).strip()
        return ""

    openai_key = _pick("openai_key", "openai_api_key", "OPENAI_API_KEY")
    openai_base = _pick("openai_base", "openai_base_url", "OPENAI_BASE_URL")
    openai_model = _pick("openai_model", "OPENAI_MODEL")
    openai_tts_model = _pick("openai_tts_model", "OPENAI_TTS_MODEL")
    openai_tts_voice = _pick("openai_tts_voice", "OPENAI_TTS_VOICE")
    openai_tts_format = _pick("openai_tts_format", "OPENAI_TTS_FORMAT")
    openai_tts_speed = data.get("openai_tts_speed", data.get("OPENAI_TTS_SPEED"))
    openai_tts_sample_rate = data.get("openai_tts_sample_rate", data.get("OPENAI_TTS_SAMPLE_RATE"))
    assemblyai_key = _pick("assemblyai_key", "assemblyai_api_key", "ASSEMBLYAI_API_KEY")
    eleven_key = _pick("eleven_key", "eleven_api_key", "ELEVEN_API_KEY")
    eleven_voice = _pick("eleven_voice", "eleven_voice_id", "ELEVEN_VOICE_ID")
    eleven_model = _pick("eleven_model", "eleven_model_id", "ELEVEN_MODEL_ID")
    eleven_latency = data.get("eleven_latency", data.get("eleven_optimize_latency"))
    eleven_output_format = _pick("eleven_output_format", "ELEVEN_OUTPUT_FORMAT")
    eleven_speed = data.get("eleven_speed", data.get("eleven_voice_speed"))

    if openai_key:
        _default_config["openai_key"] = openai_key
    if openai_base:
        _default_config["openai_base"] = openai_base
    if openai_model:
        _default_config["openai_model"] = openai_model
    if openai_tts_model:
        _default_config["openai_tts_model"] = openai_tts_model
    if openai_tts_voice:
        _default_config["openai_tts_voice"] = openai_tts_voice
    if openai_tts_format:
        _default_config["openai_tts_format"] = openai_tts_format
    if openai_tts_speed is not None:
        try:
            _default_config["openai_tts_speed"] = float(openai_tts_speed)
        except Exception:
            pass
    if openai_tts_sample_rate is not None:
        try:
            _default_config["openai_tts_sample_rate"] = int(openai_tts_sample_rate)
        except Exception:
            pass
    if assemblyai_key:
        _default_config["assemblyai_key"] = assemblyai_key
    if eleven_key:
        _default_config["eleven_key"] = eleven_key
    if eleven_voice:
        _default_config["eleven_voice"] = eleven_voice
    if eleven_model:
        _default_config["eleven_model"] = eleven_model
    if eleven_output_format:
        _default_config["eleven_output_format"] = eleven_output_format
    if eleven_latency is not None:
        try:
            _default_config["eleven_latency"] = int(eleven_latency)
        except Exception:
            pass
    if eleven_speed is not None:
        try:
            _default_config["eleven_speed"] = float(eleven_speed)
        except Exception:
            pass
        

#####webrtc###############################
pcs = set()
pcs_by_session: Dict[int, RTCPeerConnection] = {}
video_senders_by_session: Dict[int, RTCRtpSender] = {}
audio_senders_by_session: Dict[int, RTCRtpSender] = {}
daily_sessions: Dict[int, dict] = {}

def randN(N)->int:
    '''生成长度为 N的随机数 '''
    min = pow(10, N - 1)
    max = pow(10, N)
    return random.randint(min, max - 1)

def build_nerfreal(sessionid:int)->BaseReal:
    opt.sessionid=sessionid
    if opt.model == 'wav2lip':
        from lipreal import LipReal
        nerfreal = LipReal(opt,model,avatar)
    elif opt.model == 'musetalk':
        from musereal import MuseReal
        nerfreal = MuseReal(opt,model,avatar)
    elif opt.model == 'ernerf':
        from nerfreal import NeRFReal
        nerfreal = NeRFReal(opt,model,avatar)
    elif opt.model == 'ultralight':
        from lightreal import LightReal
        nerfreal = LightReal(opt,model,avatar)
    # apply cached defaults (if any)
    if _default_config.get("openai_key"):
        nerfreal.openai_api_key = _default_config["openai_key"]
    if _default_config.get("openai_base"):
        nerfreal.openai_base_url = _default_config["openai_base"]
    if _default_config.get("openai_model"):
        nerfreal.openai_model = _default_config["openai_model"]
    if _default_config.get("openai_tts_model"):
        nerfreal.openai_tts_model = _default_config["openai_tts_model"]
    if _default_config.get("openai_tts_voice"):
        nerfreal.openai_tts_voice = _default_config["openai_tts_voice"]
    if _default_config.get("openai_tts_format"):
        nerfreal.openai_tts_format = _default_config["openai_tts_format"]
    if _default_config.get("openai_tts_speed") is not None:
        nerfreal.openai_tts_speed = _default_config["openai_tts_speed"]
    if _default_config.get("openai_tts_sample_rate") is not None:
        nerfreal.openai_tts_sample_rate = _default_config["openai_tts_sample_rate"]
    if _default_config.get("eleven_key"):
        nerfreal.eleven_api_key = _default_config["eleven_key"]
    if _default_config.get("eleven_voice"):
        nerfreal.eleven_voice_id = _default_config["eleven_voice"]
    if _default_config.get("eleven_model"):
        nerfreal.eleven_model_id = _default_config["eleven_model"]
    if _default_config.get("eleven_output_format"):
        nerfreal.eleven_output_format = _default_config["eleven_output_format"]
    if _default_config.get("eleven_latency") is not None:
        nerfreal.eleven_optimize_latency = _default_config["eleven_latency"]
    if _default_config.get("eleven_speed") is not None:
        nerfreal.eleven_speed = _default_config["eleven_speed"]
    return nerfreal

def _daily_domain() -> str:
    return os.getenv("DAILY_DOMAIN", "").strip()


def _daily_api_key() -> str:
    return os.getenv("DAILY_API_KEY", "").strip()


async def _daily_api_request(method: str, path: str, payload: dict | None = None) -> Optional[dict]:
    api_key = _daily_api_key()
    if not api_key:
        logger.info("Daily API key missing.")
        return None
    url = f"https://api.daily.co/v1/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, json=payload, headers=headers) as response:
                text = await response.text()
                if response.status not in (200, 201):
                    logger.info("Daily API error %s: %s", response.status, text[:200])
                    return None
                if not text:
                    return {}
                return json.loads(text)
    except Exception as exc:
        logger.info("Daily API request failed: %s", exc)
        return None


async def _daily_create_room(name: str) -> Optional[dict]:
    ttl = int(os.getenv("DAILY_ROOM_TTL", "7200"))
    payload = {
        "name": name,
        "privacy": "private",
        "properties": {
            "exp": int(time.time()) + ttl,
        },
    }
    data = await _daily_api_request("post", "rooms", payload)
    if data:
        return data
    # Fallback: try to fetch existing room
    return await _daily_api_request("get", f"rooms/{name}")


async def _daily_delete_room(name: str) -> None:
    api_key = _daily_api_key()
    if not api_key:
        return
    url = f"https://api.daily.co/v1/rooms/{name}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.delete(url, headers=headers):
                return
    except Exception:
        return


async def _daily_create_token(room_name: str, user_name: str, is_owner: bool) -> Optional[str]:
    ttl = int(os.getenv("DAILY_TOKEN_TTL", "7200"))
    payload = {
        "properties": {
            "room_name": room_name,
            "user_name": user_name,
            "is_owner": is_owner,
            "exp": int(time.time()) + ttl,
        }
    }
    data = await _daily_api_request("post", "meeting-tokens", payload)
    if not data:
        return None
    return data.get("token")

#@app.route('/offer', methods=['POST'])
async def offer(request):
    return web.Response(
        content_type="application/json",
        status=410,
        text=json.dumps({"code": -1, "msg": "WebRTC offer disabled (Daily only)"}),
    )


async def daily_start(request):
    params = await request.json()
    if len(nerfreals) >= opt.max_session:
        return web.Response(
            content_type="application/json",
            status=429,
            text=json.dumps({"code": -1, "msg": "reach max session"}),
        )

    domain = _daily_domain()
    if not domain or not _daily_api_key():
        return web.Response(
            content_type="application/json",
            status=500,
            text=json.dumps({"code": -1, "msg": "Daily not configured"}),
        )

    if opt.transport != "daily":
        opt.transport = "daily"

    sessionid = randN(6)
    logger.info("daily sessionid=%d", sessionid)
    nerfreals[sessionid] = None
    try:
        nerfreal = await asyncio.get_event_loop().run_in_executor(None, build_nerfreal, sessionid)
    except Exception:
        logger.exception("build_nerfreal failed")
        nerfreals.pop(sessionid, None)
        return web.Response(
            content_type="application/json",
            status=500,
            text=json.dumps({"code": -1, "msg": "Failed to build avatar"}),
        )
    if nerfreal is None:
        nerfreals.pop(sessionid, None)
        return web.Response(
            content_type="application/json",
            status=500,
            text=json.dumps({"code": -1, "msg": "Failed to build avatar"}),
        )
    nerfreals[sessionid] = nerfreal

    room_name = f"avatar-{sessionid}-{uuid.uuid4().hex[:6]}"
    room = await _daily_create_room(room_name)
    if not room:
        nerfreals.pop(sessionid, None)
        return web.Response(
            content_type="application/json",
            status=502,
            text=json.dumps({"code": -1, "msg": "Failed to create Daily room"}),
        )
    room_url = room.get("url") or f"https://{domain}/{room_name}"

    viewer_token = await _daily_create_token(room_name, "viewer", False)
    bot_token = await _daily_create_token(room_name, f"avatar-{sessionid}", True)
    if not viewer_token or not bot_token:
        nerfreals.pop(sessionid, None)
        await _daily_delete_room(room_name)
        return web.Response(
            content_type="application/json",
            status=502,
            text=json.dumps({"code": -1, "msg": "Failed to create Daily token"}),
        )

    audio_rate = int(os.getenv("DAILY_AUDIO_RATE", "16000"))
    audio_bitrate = int(os.getenv("DAILY_AUDIO_BITRATE", "64000"))
    video_quality = os.getenv("DAILY_VIDEO_QUALITY", "high")
    preferred_codec = os.getenv("DAILY_VIDEO_CODEC", "H264")
    video_width = int(os.getenv("DAILY_VIDEO_WIDTH", str(nerfreal.W)))
    video_height = int(os.getenv("DAILY_VIDEO_HEIGHT", str(nerfreal.H)))
    video_fps = int(os.getenv("DAILY_VIDEO_FPS", "25"))
    quality_auto = os.getenv("DAILY_QUALITY_AUTO", "1").strip() != "0"
    bot = DailyBot(
        DailyBotConfig(
            room_url=room_url,
            meeting_token=bot_token,
            width=video_width,
            height=video_height,
            fps=video_fps,
            sample_rate=audio_rate,
            user_name=f"avatar-{sessionid}",
            video_quality=video_quality,
            preferred_codec=preferred_codec,
            audio_bitrate=audio_bitrate,
            quality_auto=quality_auto,
        )
    )
    if not bot.wait_ready(15) or bot.error:
        err = bot.error or "Daily bot join timeout"
        bot.close()
        nerfreals.pop(sessionid, None)
        await _daily_delete_room(room_name)
        return web.Response(
            content_type="application/json",
            status=502,
            text=json.dumps({"code": -1, "msg": err}),
        )

    quit_event = Event()
    render_thread = Thread(
        target=nerfreal.render,
        args=(quit_event, None, None, None, bot),
        daemon=True,
    )
    render_thread.start()

    daily_sessions[sessionid] = {
        "bot": bot,
        "quit": quit_event,
        "thread": render_thread,
        "room_name": room_name,
        "room_url": room_url,
    }

    return web.Response(
        content_type="application/json",
        text=json.dumps({"code": 0, "sessionid": sessionid, "room_url": room_url, "token": viewer_token}),
    )

async def human(request):
    params = await request.json()

    sessionid = params.get('sessionid',0)
    if params.get('interrupt'):
        nerfreals[sessionid].flush_talk()

    if params['type']=='echo':
        nerfreals[sessionid].put_msg_txt(params['text'])
    elif params['type']=='chat':
        try:
            res=await asyncio.get_event_loop().run_in_executor(None, llm_response, params['text'],nerfreals[sessionid])
            #nerfreals[sessionid].put_msg_txt(res)
        except Exception as e:
            # Do not echo the user's message on LLM errors.
            logger.info(f'LLM error, no echo: {e}')
            return web.Response(
                status=500,
                content_type="application/json",
                text=json.dumps({"code": -1, "msg": "LLM error"}),
            )

    payload = {"code": 0, "data": "ok"}
    if params.get('type') == 'chat':
        payload["reply"] = res if isinstance(res, str) else ""
    return web.Response(
        content_type="application/json",
        text=json.dumps(payload),
    )

async def humanaudio(request):
    try:
        form= await request.post()
        sessionid = int(form.get('sessionid',0))
        fileobj = form["file"]
        filename=fileobj.filename
        filebytes=fileobj.file.read()
        nerfreals[sessionid].put_audio_file(filebytes)

        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {"code": 0, "msg":"ok"}
            ),
        )
    except Exception as e:
        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {"code": -1, "msg":"err","data": ""+e.args[0]+""}
            ),
        )

async def set_audiotype(request):
    params = await request.json()

    sessionid = params.get('sessionid',0)    
    nerfreals[sessionid].set_custom_state(params['audiotype'],params['reinit'])

    return web.Response(
        content_type="application/json",
        text=json.dumps(
            {"code": 0, "data":"ok"}
        ),
    )

async def config(request):
    params = await request.json()
    sessionid = params.get('sessionid', 0)
    nerfreal = nerfreals.get(sessionid)
    # allow storing defaults before session exists
    if nerfreal is None and sessionid:
        return web.Response(
            content_type="application/json",
            text=json.dumps({"code": -1, "msg": "invalid session"}),
        )

    # LLM settings
    if 'openai_key' in params:
        value = (params.get('openai_key') or "").strip()
        _default_config["openai_key"] = value
        if nerfreal:
            nerfreal.openai_api_key = value
    if 'openai_base' in params:
        value = (params.get('openai_base') or "").strip()
        _default_config["openai_base"] = value
        if nerfreal:
            nerfreal.openai_base_url = value
    if 'openai_model' in params:
        model = (params.get('openai_model') or "").strip()
        if model:
            _default_config["openai_model"] = model
            if nerfreal:
                nerfreal.openai_model = model
    if 'openai_tts_model' in params:
        model = (params.get('openai_tts_model') or "").strip()
        if model:
            _default_config["openai_tts_model"] = model
            if nerfreal:
                nerfreal.openai_tts_model = model
    if 'openai_tts_voice' in params:
        value = (params.get('openai_tts_voice') or "").strip()
        if value:
            _default_config["openai_tts_voice"] = value
            if nerfreal:
                nerfreal.openai_tts_voice = value
    if 'openai_tts_format' in params:
        value = (params.get('openai_tts_format') or "").strip()
        if value:
            _default_config["openai_tts_format"] = value
            if nerfreal:
                nerfreal.openai_tts_format = value
    if 'openai_tts_speed' in params:
        try:
            value = float(params.get('openai_tts_speed'))
            _default_config["openai_tts_speed"] = value
            if nerfreal:
                nerfreal.openai_tts_speed = value
        except Exception:
            pass
    if 'openai_tts_sample_rate' in params:
        try:
            value = int(params.get('openai_tts_sample_rate'))
            _default_config["openai_tts_sample_rate"] = value
            if nerfreal:
                nerfreal.openai_tts_sample_rate = value
        except Exception:
            pass
    if 'assemblyai_key' in params:
        value = (params.get('assemblyai_key') or "").strip()
        _default_config["assemblyai_key"] = value

    # ElevenLabs settings
    if 'eleven_key' in params:
        value = (params.get('eleven_key') or "").strip()
        _default_config["eleven_key"] = value
        if nerfreal:
            nerfreal.eleven_api_key = value
    if 'eleven_voice' in params:
        value = (params.get('eleven_voice') or "").strip()
        _default_config["eleven_voice"] = value
        if nerfreal:
            nerfreal.eleven_voice_id = value
    if 'eleven_model' in params:
        model_id = (params.get('eleven_model') or "").strip()
        if model_id:
            _default_config["eleven_model"] = model_id
            if nerfreal:
                nerfreal.eleven_model_id = model_id
    if 'eleven_latency' in params:
        try:
            value = int(params.get('eleven_latency'))
            _default_config["eleven_latency"] = value
            if nerfreal:
                nerfreal.eleven_optimize_latency = value
        except Exception:
            pass
    if 'eleven_speed' in params:
        try:
            value = float(params.get('eleven_speed'))
            _default_config["eleven_speed"] = value
            if nerfreal:
                nerfreal.eleven_speed = value
        except Exception:
            pass

    return web.Response(
        content_type="application/json",
        text=json.dumps({"code": 0, "data": "ok"}),
    )


async def assemblyai_token(request):
    key = (_default_config.get("assemblyai_key") or "").strip()
    if not key:
        return web.Response(
            status=400,
            content_type="application/json",
            text=json.dumps({"code": -1, "msg": "AssemblyAI key not configured"}),
        )

    try:
        params = await request.json()
    except Exception:
        params = {}

    expires = params.get("expires_in_seconds", 120)
    try:
        expires = int(expires)
    except Exception:
        expires = 120
    expires = max(60, min(expires, 3600))

    url = f"https://streaming.assemblyai.com/v3/token?expires_in_seconds={expires}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"Authorization": key}) as resp:
                raw = await resp.text()
                if resp.status != 200:
                    return web.Response(
                        status=resp.status,
                        content_type="application/json",
                        text=json.dumps({"code": -1, "msg": "AssemblyAI token request failed", "detail": raw}),
                    )
                data = json.loads(raw)
    except Exception as e:
        return web.Response(
            status=500,
            content_type="application/json",
            text=json.dumps({"code": -1, "msg": f"AssemblyAI token error: {e}"}),
        )

    token = data.get("token") or data.get("temporary_token")
    if not token:
        return web.Response(
            status=500,
            content_type="application/json",
            text=json.dumps({"code": -1, "msg": "AssemblyAI token missing"}),
        )

    return web.Response(
        content_type="application/json",
        text=json.dumps(
            {
                "code": 0,
                "token": token,
                "expires_in_seconds": data.get("expires_in_seconds", expires),
            }
        ),
    )


async def webrtc_quality(request):
    params = await request.json()
    sessionid = int(params.get('sessionid', 0))
    video_sender = video_senders_by_session.get(sessionid)
    audio_sender = audio_senders_by_session.get(sessionid)
    if not video_sender and not audio_sender:
        return web.Response(
            content_type="application/json",
            status=410,
            text=json.dumps({"code": -1, "msg": "session expired"}),
        )

    quality = params.get("quality")
    if quality:
        if video_sender:
            _apply_video_quality(video_sender, quality)
        if audio_sender:
            _apply_audio_quality(audio_sender, quality)
    else:
        overrides = {}
        if params.get("max_bitrate") is not None:
            try:
                overrides["max_bitrate"] = int(params.get("max_bitrate"))
            except Exception:
                pass
        if params.get("max_fps") is not None:
            try:
                overrides["max_fps"] = int(params.get("max_fps"))
            except Exception:
                pass
        if params.get("scale") is not None:
            try:
                overrides["scale"] = float(params.get("scale"))
            except Exception:
                pass
        if overrides:
            if video_sender:
                _apply_video_overrides(video_sender, overrides)

    return web.Response(
        content_type="application/json",
        text=json.dumps({"code": 0, "data": "ok"}),
    )

async def record(request):
    params = await request.json()

    sessionid = params.get('sessionid',0)
    if params['type']=='start_record':
        # nerfreals[sessionid].put_msg_txt(params['text'])
        nerfreals[sessionid].start_recording()
    elif params['type']=='end_record':
        nerfreals[sessionid].stop_recording()
    return web.Response(
        content_type="application/json",
        text=json.dumps(
            {"code": 0, "data":"ok"}
        ),
    )

async def is_speaking(request):
    params = await request.json()

    sessionid = params.get('sessionid',0)
    return web.Response(
        content_type="application/json",
        text=json.dumps(
            {"code": 0, "data": nerfreals[sessionid].is_speaking()}
        ),
    )


async def end_session(request):
    params = await request.json()
    sessionid = int(params.get('sessionid', 0))
    if sessionid:
        pc = pcs_by_session.pop(sessionid, None)
        if pc:
            try:
                await pc.close()
            except Exception:
                pass
            pcs.discard(pc)
        video_senders_by_session.pop(sessionid, None)
        audio_senders_by_session.pop(sessionid, None)
        daily = daily_sessions.pop(sessionid, None)
        if daily:
            try:
                daily.get("quit").set()
            except Exception:
                pass
            try:
                if daily.get("thread"):
                    daily.get("thread").join(timeout=2)
            except Exception:
                pass
            try:
                daily.get("bot").close()
            except Exception:
                pass
            try:
                room_name = daily.get("room_name")
                if room_name:
                    await _daily_delete_room(room_name)
            except Exception:
                pass
        nerfreal = nerfreals.get(sessionid)
        if nerfreal:
            try:
                nerfreal.flush_talk()
            except Exception:
                pass
            del nerfreals[sessionid]
    return web.Response(
        content_type="application/json",
        text=json.dumps({"code": 0, "msg": "ended"}),
    )

async def health(request):
    return web.Response(text="ok")


async def on_shutdown(app):
    # close peer connections
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()
    pcs_by_session.clear()
    video_senders_by_session.clear()
    audio_senders_by_session.clear()
    for sessionid, daily in list(daily_sessions.items()):
        try:
            daily.get("quit").set()
        except Exception:
            pass
        try:
            if daily.get("thread"):
                daily.get("thread").join(timeout=2)
        except Exception:
            pass
        try:
            daily.get("bot").close()
        except Exception:
            pass
        try:
            room_name = daily.get("room_name")
            if room_name:
                await _daily_delete_room(room_name)
        except Exception:
            pass
        daily_sessions.pop(sessionid, None)
    audio_senders_by_session.clear()

async def post(url,data):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url,data=data) as response:
                return await response.text()
    except aiohttp.ClientError as e:
        logger.info(f'Error: {e}')

async def run(push_url,sessionid):
    nerfreal = await asyncio.get_event_loop().run_in_executor(None, build_nerfreal,sessionid)
    nerfreals[sessionid] = nerfreal

    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        logger.info("Connection state is %s" % pc.connectionState)
        if pc.connectionState == "failed":
            await pc.close()
            pcs.discard(pc)

    player = HumanPlayer(nerfreals[sessionid])
    audio_sender = pc.addTrack(player.audio)
    video_sender = pc.addTrack(player.video)

    await pc.setLocalDescription(await pc.createOffer())
    answer = await post(push_url,pc.localDescription.sdp)
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer,type='answer'))
##########################################
# os.environ['MKL_SERVICE_FORCE_INTEL'] = '1'
# os.environ['MULTIPROCESSING_METHOD'] = 'forkserver'                                                    
if __name__ == '__main__':
    mp.set_start_method('spawn')
    parser = argparse.ArgumentParser()
    parser.add_argument('--pose', type=str, default="data/data_kf.json", help="transforms.json, pose source")
    parser.add_argument('--au', type=str, default="data/au.csv", help="eye blink area")
    parser.add_argument('--torso_imgs', type=str, default="", help="torso images path")

    parser.add_argument('-O', action='store_true', help="equals --fp16 --cuda_ray --exp_eye")

    parser.add_argument('--data_range', type=int, nargs='*', default=[0, -1], help="data range to use")
    parser.add_argument('--workspace', type=str, default='data/video')
    parser.add_argument('--seed', type=int, default=0)

    ### training options
    parser.add_argument('--ckpt', type=str, default='data/pretrained/ngp_kf.pth')
   
    parser.add_argument('--num_rays', type=int, default=4096 * 16, help="num rays sampled per image for each training step")
    parser.add_argument('--cuda_ray', action='store_true', help="use CUDA raymarching instead of pytorch")
    parser.add_argument('--max_steps', type=int, default=16, help="max num steps sampled per ray (only valid when using --cuda_ray)")
    parser.add_argument('--num_steps', type=int, default=16, help="num steps sampled per ray (only valid when NOT using --cuda_ray)")
    parser.add_argument('--upsample_steps', type=int, default=0, help="num steps up-sampled per ray (only valid when NOT using --cuda_ray)")
    parser.add_argument('--update_extra_interval', type=int, default=16, help="iter interval to update extra status (only valid when using --cuda_ray)")
    parser.add_argument('--max_ray_batch', type=int, default=4096, help="batch size of rays at inference to avoid OOM (only valid when NOT using --cuda_ray)")

    ### loss set
    parser.add_argument('--warmup_step', type=int, default=10000, help="warm up steps")
    parser.add_argument('--amb_aud_loss', type=int, default=1, help="use ambient aud loss")
    parser.add_argument('--amb_eye_loss', type=int, default=1, help="use ambient eye loss")
    parser.add_argument('--unc_loss', type=int, default=1, help="use uncertainty loss")
    parser.add_argument('--lambda_amb', type=float, default=1e-4, help="lambda for ambient loss")

    ### network backbone options
    parser.add_argument('--fp16', action='store_true', help="use amp mixed precision training")
    
    parser.add_argument('--bg_img', type=str, default='white', help="background image")
    parser.add_argument('--fbg', action='store_true', help="frame-wise bg")
    parser.add_argument('--exp_eye', action='store_true', help="explicitly control the eyes")
    parser.add_argument('--fix_eye', type=float, default=-1, help="fixed eye area, negative to disable, set to 0-0.3 for a reasonable eye")
    parser.add_argument('--smooth_eye', action='store_true', help="smooth the eye area sequence")

    parser.add_argument('--torso_shrink', type=float, default=0.8, help="shrink bg coords to allow more flexibility in deform")

    ### dataset options
    parser.add_argument('--color_space', type=str, default='srgb', help="Color space, supports (linear, srgb)")
    parser.add_argument('--preload', type=int, default=0, help="0 means load data from disk on-the-fly, 1 means preload to CPU, 2 means GPU.")
    # (the default value is for the fox dataset)
    parser.add_argument('--bound', type=float, default=1, help="assume the scene is bounded in box[-bound, bound]^3, if > 1, will invoke adaptive ray marching.")
    parser.add_argument('--scale', type=float, default=4, help="scale camera location into box[-bound, bound]^3")
    parser.add_argument('--offset', type=float, nargs='*', default=[0, 0, 0], help="offset of camera location")
    parser.add_argument('--dt_gamma', type=float, default=1/256, help="dt_gamma (>=0) for adaptive ray marching. set to 0 to disable, >0 to accelerate rendering (but usually with worse quality)")
    parser.add_argument('--min_near', type=float, default=0.05, help="minimum near distance for camera")
    parser.add_argument('--density_thresh', type=float, default=10, help="threshold for density grid to be occupied (sigma)")
    parser.add_argument('--density_thresh_torso', type=float, default=0.01, help="threshold for density grid to be occupied (alpha)")
    parser.add_argument('--patch_size', type=int, default=1, help="[experimental] render patches in training, so as to apply LPIPS loss. 1 means disabled, use [64, 32, 16] to enable")

    parser.add_argument('--init_lips', action='store_true', help="init lips region")
    parser.add_argument('--finetune_lips', action='store_true', help="use LPIPS and landmarks to fine tune lips region")
    parser.add_argument('--smooth_lips', action='store_true', help="smooth the enc_a in a exponential decay way...")
    parser.add_argument('--lip_smooth', type=float, default=0.35, help="lip smoothing factor (0 = off, 0.35 default)")
    parser.add_argument('--audio_amp', type=float, default=1.0, help="audio feature amplitude for stronger mouth motion")

    parser.add_argument('--torso', action='store_true', help="fix head and train torso")
    parser.add_argument('--head_ckpt', type=str, default='', help="head model")

    ### GUI options
    parser.add_argument('--gui', action='store_true', help="start a GUI")
    parser.add_argument('--W', type=int, default=450, help="GUI width")
    parser.add_argument('--H', type=int, default=450, help="GUI height")
    parser.add_argument('--radius', type=float, default=3.35, help="default GUI camera radius from center")
    parser.add_argument('--fovy', type=float, default=21.24, help="default GUI camera fovy")
    parser.add_argument('--max_spp', type=int, default=1, help="GUI rendering max sample per pixel")

    ### else
    parser.add_argument('--att', type=int, default=2, help="audio attention mode (0 = turn off, 1 = left-direction, 2 = bi-direction)")
    parser.add_argument('--aud', type=str, default='', help="audio source (empty will load the default, else should be a path to a npy file)")
    parser.add_argument('--emb', action='store_true', help="use audio class + embedding instead of logits")

    parser.add_argument('--ind_dim', type=int, default=4, help="individual code dim, 0 to turn off")
    parser.add_argument('--ind_num', type=int, default=10000, help="number of individual codes, should be larger than training dataset size")

    parser.add_argument('--ind_dim_torso', type=int, default=8, help="individual code dim, 0 to turn off")

    parser.add_argument('--amb_dim', type=int, default=2, help="ambient dimension")
    parser.add_argument('--part', action='store_true', help="use partial training data (1/10)")
    parser.add_argument('--part2', action='store_true', help="use partial training data (first 15s)")

    parser.add_argument('--train_camera', action='store_true', help="optimize camera pose")
    parser.add_argument('--smooth_path', action='store_true', help="brute-force smooth camera pose trajectory with a window size")
    parser.add_argument('--smooth_path_window', type=int, default=7, help="smoothing window size")

    # asr
    parser.add_argument('--asr', action='store_true', help="load asr for real-time app")
    parser.add_argument('--asr_wav', type=str, default='', help="load the wav and use as input")
    parser.add_argument('--asr_play', action='store_true', help="play out the audio")

    #parser.add_argument('--asr_model', type=str, default='deepspeech')
    parser.add_argument('--asr_model', type=str, default='cpierse/wav2vec2-large-xlsr-53-esperanto') #
    # parser.add_argument('--asr_model', type=str, default='facebook/wav2vec2-large-960h-lv60-self')
    # parser.add_argument('--asr_model', type=str, default='facebook/hubert-large-ls960-ft')
    parser.add_argument('--asr_dim', type=int, default=0, help='override ASR feature dim (0 = auto)')

    parser.add_argument('--asr_save_feats', action='store_true')
    # audio FPS
    parser.add_argument('--fps', type=int, default=50)
    # sliding window left-middle-right length (unit: 20ms)
    parser.add_argument('-l', type=int, default=10)
    parser.add_argument('-m', type=int, default=8)
    parser.add_argument('-r', type=int, default=10)

    parser.add_argument('--fullbody', action='store_true', help="fullbody human")
    parser.add_argument('--fullbody_img', type=str, default='data/fullbody/img')
    parser.add_argument('--fullbody_width', type=int, default=580)
    parser.add_argument('--fullbody_height', type=int, default=1080)
    parser.add_argument('--fullbody_offset_x', type=int, default=0)
    parser.add_argument('--fullbody_offset_y', type=int, default=0)

    #musetalk opt
    parser.add_argument('--avatar_id', type=str, default='avator_1')
    parser.add_argument('--bbox_shift', type=int, default=5)
    parser.add_argument('--batch_size', type=int, default=16)

    # parser.add_argument('--customvideo', action='store_true', help="custom video")
    # parser.add_argument('--customvideo_img', type=str, default='data/customvideo/img')
    # parser.add_argument('--customvideo_imgnum', type=int, default=1)

    parser.add_argument('--customvideo_config', type=str, default='')

    parser.add_argument('--tts', type=str, default='edgetts') #xtts gpt-sovits cosyvoice
    parser.add_argument('--REF_FILE', type=str, default=None)
    parser.add_argument('--REF_TEXT', type=str, default=None)
    parser.add_argument('--TTS_SERVER', type=str, default='http://127.0.0.1:9880') # http://localhost:9000
    parser.add_argument('--openai_tts_model', type=str, default='gpt-4o-mini-tts')
    parser.add_argument('--openai_tts_voice', type=str, default='ash')
    parser.add_argument('--openai_tts_format', type=str, default='pcm')
    parser.add_argument('--openai_tts_speed', type=float, default=None)
    parser.add_argument('--openai_tts_sample_rate', type=int, default=24000)
    parser.add_argument('--openai_base', type=str, default='')
    parser.add_argument('--openai_model', type=str, default='gpt-4o-mini')
    parser.add_argument('--eleven_voice', type=str, default='')
    parser.add_argument('--eleven_model', type=str, default='eleven_turbo_v2')
    parser.add_argument('--eleven_output_format', type=str, default='pcm_16000')
    parser.add_argument('--eleven_optimize_latency', type=int, default=1)
    parser.add_argument('--eleven_speed', type=float, default=None)
    parser.add_argument('--secrets', type=str, default='/workspace/LiveTalking/keys.json', help='path to JSON secrets file')
    # parser.add_argument('--CHARACTER', type=str, default='test')
    # parser.add_argument('--EMOTION', type=str, default='default')

    parser.add_argument('--model', type=str, default='ernerf') #musetalk wav2lip

    parser.add_argument('--transport', type=str, default='rtcpush') #rtmp webrtc rtcpush
    parser.add_argument('--push_url', type=str, default='http://localhost:1985/rtc/v1/whip/?app=live&stream=livestream') #rtmp://localhost/live/livestream

    parser.add_argument('--max_session', type=int, default=1)  #multi session count
    parser.add_argument('--listenport', type=int, default=8010)

    opt = parser.parse_args()
    _load_secrets(opt.secrets)
    #app.config.from_object(opt)
    #print(app.config)
    opt.customopt = []
    if opt.customvideo_config!='':
        with open(opt.customvideo_config,'r') as file:
            opt.customopt = json.load(file)

    if opt.model == 'ernerf':       
        from nerfreal import NeRFReal,load_model,load_avatar
        model = load_model(opt)
        avatar = load_avatar(opt) 
        
        # we still need test_loader to provide audio features for testing.
        # for k in range(opt.max_session):
        #     opt.sessionid=k
        #     nerfreal = NeRFReal(opt, trainer, test_loader,audio_processor,audio_model)
        #     nerfreals.append(nerfreal)
    elif opt.model == 'musetalk':
        from musereal import MuseReal,load_model,load_avatar,warm_up
        logger.info(opt)
        model = load_model()
        avatar = load_avatar(opt.avatar_id) 
        warm_up(opt.batch_size,model)      
        # for k in range(opt.max_session):
        #     opt.sessionid=k
        #     nerfreal = MuseReal(opt,audio_processor,vae, unet, pe,timesteps)
        #     nerfreals.append(nerfreal)
    elif opt.model == 'wav2lip':
        from lipreal import LipReal,load_model,load_avatar,warm_up
        logger.info(opt)
        model = load_model("./models/wav2lip.pth")
        avatar = load_avatar(opt.avatar_id)
        warm_up(opt.batch_size,model,256)
        # for k in range(opt.max_session):
        #     opt.sessionid=k
        #     nerfreal = LipReal(opt,model)
        #     nerfreals.append(nerfreal)
    elif opt.model == 'ultralight':
        from lightreal import LightReal,load_model,load_avatar,warm_up
        logger.info(opt)
        model = load_model(opt)
        avatar = load_avatar(opt.avatar_id)
        warm_up(opt.batch_size,avatar,160)

    if opt.transport=='rtmp':
        thread_quit = Event()
        nerfreals[0] = build_nerfreal(0)
        rendthrd = Thread(target=nerfreals[0].render,args=(thread_quit,))
        rendthrd.start()

    #############################################################################
    appasync = web.Application()
    appasync.on_shutdown.append(on_shutdown)
    appasync.router.add_post("/daily/start", daily_start)
    appasync.router.add_post("/human", human)
    appasync.router.add_post("/humanaudio", humanaudio)
    appasync.router.add_post("/set_audiotype", set_audiotype)
    appasync.router.add_post("/config", config)
    appasync.router.add_post("/assemblyai/token", assemblyai_token)
    appasync.router.add_post("/webrtc_quality", webrtc_quality)
    appasync.router.add_post("/record", record)
    appasync.router.add_post("/is_speaking", is_speaking)
    appasync.router.add_post("/end_session", end_session)
    appasync.router.add_get("/health", health)
    appasync.router.add_static('/',path='web')

    # Configure default CORS settings.
    cors = aiohttp_cors.setup(appasync, defaults={
            "*": aiohttp_cors.ResourceOptions(
                allow_credentials=True,
                expose_headers="*",
                allow_headers="*",
            )
        })
    # Configure CORS on all routes.
    for route in list(appasync.router.routes()):
        cors.add(route)

    pagename='webrtcapi.html'
    if opt.transport=='rtmp':
        pagename='echoapi.html'
    elif opt.transport=='rtcpush':
        pagename='rtcpushapi.html'
    logger.info('start http server; http://<serverip>:'+str(opt.listenport)+'/'+pagename)
    logger.info('如果使用webrtc，推荐访问webrtc集成前端: http://<serverip>:'+str(opt.listenport)+'/dashboard.html')
    def run_server(runner):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, '0.0.0.0', opt.listenport)
        loop.run_until_complete(site.start())
        if opt.transport=='rtcpush':
            for k in range(opt.max_session):
                push_url = opt.push_url
                if k!=0:
                    push_url = opt.push_url+str(k)
                loop.run_until_complete(run(push_url,k))
        loop.run_forever()    
    #Thread(target=run_server, args=(web.AppRunner(appasync),)).start()
    run_server(web.AppRunner(appasync))

    #app.on_shutdown.append(on_shutdown)
    #app.router.add_post("/offer", offer)

    # print('start websocket server')
    # server = pywsgi.WSGIServer(('0.0.0.0', 8000), app, handler_class=WebSocketHandler)
    # server.serve_forever()
    
    
