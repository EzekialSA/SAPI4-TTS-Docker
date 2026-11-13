#!/usr/bin/env python3
"""
sapi4-tts — SAPI 4.0 / Lernout & Hauspie TruVoice TTS service.

Surfaces:
  GET  /                        web UI (tetyys-style)
  GET  /api/voices              list voices + pitch/speed limits
  GET  /api/tts                 plain REST -> audio/wav
  POST /v1/audio/speech         OpenAI-compatible TTS
  GET  /healthz                 health probe
  Wyoming protocol runs on TCP 10200 (wyoming_server.py, same container).
  Optional: Qwen3-TTS voices, proxied from QWEN_TTS_URL (see README). Off unless configured.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

WINEPREFIX = os.environ.get("WINEPREFIX", "/opt/sapi4/wine")
APPDIR = Path(os.environ.get("SAPI4_APPDIR", "/opt/sapi4/bin"))
BONZI_VOICE = "Adult Male #2, American English (TruVoice)"
BONZI_PITCH = 140
BONZI_SPEED = 157

# Per-voice default pitch/speed, applied whenever the caller does not specify
# them. This is what makes "Adult Male #2" actually sound like BonziBUDDY
# rather than a flat TruVoice male — the engine's own defaults are 50/150.
#
# Applied in synth(), so it holds for EVERY surface: REST, OpenAI, Wyoming and
# the web UI. Wyoming in particular can only transmit a voice NAME (the
# protocol has no pitch/speed fields), so without this a Home Assistant
# tts.speak naming the voice explicitly silently loses the preset.
VOICE_PRESETS: Dict[str, tuple] = {
    "Adult Male #2, American English (TruVoice)": (140, 157),  # BonziBUDDY
}

# OpenAI voice-name aliases -> (voice, pitch, speed). None = voice default.
OPENAI_ALIASES: Dict[str, tuple] = {
    "bonzi": (BONZI_VOICE, None, None),
    "alloy": (BONZI_VOICE, None, None),
    "sam": ("Sam", None, None),
    "mary": ("Mary", None, None),
    "mike": ("Mike", None, None),
    "whisper": ("Female Whisper", None, None),
    "robot": ("RoboSoft Three", None, None),
}

VOICES: Dict[str, dict] = {}
_sem = asyncio.Semaphore(int(os.environ.get("SAPI4_CONCURRENCY", "2")))

# --- optional Qwen3-TTS voices (proxied) -------------------------------------
# Any OpenAI-compatible TTS server works. Disabled unless BOTH variables are set.
#   QWEN_TTS_URL     base URL of the server, e.g. http://tts-host.example:8000
#   QWEN_TTS_VOICES  comma-separated voice names that server knows
# Pitch and speed are SAPI4 concepts; the UI greys them out for these voices.
QWEN_TTS_URL = os.environ.get("QWEN_TTS_URL", "").strip().rstrip("/")
QWEN_TTS_VOICES = [v.strip() for v in os.environ.get("QWEN_TTS_VOICES", "").split(",") if v.strip()]
QWEN_TTS_MODEL = os.environ.get("QWEN_TTS_MODEL", "qwen3-tts").strip() or "qwen3-tts"
QWEN_TTS_TIMEOUT = int(os.environ.get("QWEN_TTS_TIMEOUT", "180"))
QWEN_ENABLED = bool(QWEN_TTS_URL and QWEN_TTS_VOICES)
if QWEN_TTS_URL and not QWEN_TTS_VOICES:
    print("[qwen] QWEN_TTS_URL is set but QWEN_TTS_VOICES is empty; Qwen voices disabled", flush=True)
QWEN_MIME = {"wav": "audio/wav", "mp3": "audio/mpeg", "opus": "audio/ogg",
             "flac": "audio/flac", "aac": "audio/aac"}
# A GPU-backed server: one request at a time unless told otherwise.
_qsem = asyncio.Semaphore(int(os.environ.get("QWEN_TTS_CONCURRENCY", "1")))

app = FastAPI(title="sapi4-tts", version="1.0.0")


def _wine_env() -> dict:
    env = dict(os.environ)
    env.update(
        WINEPREFIX=WINEPREFIX,
        WINEARCH="win32",
        WINEDEBUG="-all",
        DISPLAY=os.environ.get("DISPLAY", ":99"),
    )
    return env


NOISE = re.compile(r"^(err|warn|fixme|wine):.*$", re.IGNORECASE | re.MULTILINE)


def _clean(text: str) -> str:
    """Strip Wine's stderr chatter that leaks into stdout."""
    return NOISE.sub("", text).replace("\r\n", "\n").strip()


def _run(args, timeout=30) -> str:
    proc = subprocess.run(
        ["wine"] + args,
        cwd=str(APPDIR),
        env=_wine_env(),
        capture_output=True,
        timeout=timeout,
    )
    out = _clean(proc.stdout.decode("utf-8", "replace"))
    if proc.returncode != 0 and not out:
        err = _clean(proc.stderr.decode("utf-8", "replace"))
        raise RuntimeError(f"{args[0]} exit {proc.returncode}: {err[:400]}")
    return out


def load_voices() -> Dict[str, dict]:
    """Enumerate voices via sapi4limits.exe and capture each one's limits."""
    voices: Dict[str, dict] = {}
    for name in [v.strip() for v in _run(["sapi4limits.exe"]).split("\n") if v.strip()]:
        try:
            lines = [l for l in _run(["sapi4limits.exe", name]).split("\n") if l.strip()]
            if len(lines) < 3:
                continue
            pitch, speed = lines[1].split(), lines[2].split()
            voices[name] = {
                "name": name,
                "defPitch": int(pitch[0]), "minPitch": int(pitch[1]),
                "maxPitch": int(pitch[2]),
                "defSpeed": int(speed[0]), "minSpeed": int(speed[1]),
                "maxSpeed": int(speed[2]),
            }
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] limits failed for {name!r}: {exc}", flush=True)
    return voices


def synth(text: str, voice: str, pitch: Optional[int], speed: Optional[int]) -> bytes:
    if voice not in VOICES:
        raise HTTPException(400, f"Unknown voice {voice!r}")
    v = VOICES[voice]
    # A voice with a preset uses it when the caller omitted pitch/speed.
    # Explicit values from the caller always win.
    pre_pitch, pre_speed = VOICE_PRESETS.get(voice, (None, None))
    if pitch is None:
        pitch = pre_pitch if pre_pitch is not None else v["defPitch"]
    if speed is None:
        speed = pre_speed if pre_speed is not None else v["defSpeed"]
    pitch, speed = int(pitch), int(speed)
    if not (v["minPitch"] <= pitch <= v["maxPitch"]):
        raise HTTPException(400, f"pitch must be {v['minPitch']}-{v['maxPitch']}")
    if not (v["minSpeed"] <= speed <= v["maxSpeed"]):
        raise HTTPException(400, f"speed must be {v['minSpeed']}-{v['maxSpeed']}")
    if not text.strip():
        raise HTTPException(400, "text is required")
    if len(text) > 4095:
        raise HTTPException(400, "text too long (max 4095)")

    out = _run(["sapi4out.exe", voice, str(pitch), str(speed), text], timeout=60)
    winpath = out.strip().split("\n")[-1].strip()
    if not winpath:
        raise HTTPException(500, "synthesis produced no output")
    # sapi4out.exe prints a BARE 16-char filename (no path, no extension) and
    # writes it into its own working directory — not an absolute Windows path.
    # Only convert when a drive letter is actually present.
    unix = winpath.replace("\\", "/")
    if len(unix) > 1 and unix[1] == ":":
        unix = f"{WINEPREFIX}/dosdevices/{unix[0].lower()}:{unix[2:]}"
        p = Path(unix)
    else:
        p = APPDIR / unix
    if not p.exists():
        raise HTTPException(500, f"output wav missing: {winpath} (looked in {p})")
    try:
        return p.read_bytes()
    finally:
        p.unlink(missing_ok=True)


async def synth_async(text, voice, pitch, speed) -> bytes:
    async with _sem:
        return await asyncio.to_thread(synth, text, voice, pitch, speed)


def qwen_voice_names() -> list:
    """Configured Qwen voices, minus any name a SAPI voice already owns (SAPI wins)."""
    return [v for v in QWEN_TTS_VOICES if QWEN_ENABLED and v not in VOICES]


def is_qwen(voice: str) -> bool:
    return QWEN_ENABLED and voice in QWEN_TTS_VOICES and voice not in VOICES


def qwen_synth(text: str, voice: str, fmt: str, speed: float = 1.0) -> tuple:
    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    if len(text) > 4095:
        raise HTTPException(400, "text too long (max 4095)")
    fmt = (fmt or "wav").lower()
    if fmt not in QWEN_MIME:
        fmt = "wav"
    payload = {"model": QWEN_TTS_MODEL, "input": text, "voice": voice, "response_format": fmt}
    if speed and abs(speed - 1.0) > 0.01:
        payload["speed"] = speed
    req = urllib.request.Request(
        QWEN_TTS_URL + "/v1/audio/speech", data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=QWEN_TTS_TIMEOUT) as r:
            ctype = r.headers.get_content_type()
            return r.read(), (ctype if ctype.startswith("audio/") else QWEN_MIME[fmt])
    except urllib.error.HTTPError as e:
        detail = e.read(300).decode("utf-8", "replace")
        raise HTTPException(502, f"Qwen TTS returned {e.code}: {detail}") from e
    except (urllib.error.URLError, OSError) as e:
        raise HTTPException(502, f"Qwen TTS unreachable ({type(e).__name__})") from e


async def qwen_synth_async(text, voice, fmt, speed=1.0):
    async with _qsem:
        return await asyncio.to_thread(qwen_synth, text, voice, fmt, speed)


def to_format(wav: bytes, fmt: str) -> tuple:
    """Convert WAV via ffmpeg. Falls back to wav when unavailable."""
    fmt = (fmt or "wav").lower()
    if fmt in ("wav", "pcm"):
        return wav, "audio/wav"
    codecs = {"mp3": ("mp3", "audio/mpeg"), "opus": ("opus", "audio/ogg"),
              "flac": ("flac", "audio/flac"), "aac": ("adts", "audio/aac")}
    if fmt not in codecs or not shutil.which("ffmpeg"):
        return wav, "audio/wav"
    container, mime = codecs[fmt]
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", container, "pipe:1"], input=wav, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return wav, "audio/wav"
    return proc.stdout, mime


@app.on_event("startup")
def startup():
    global VOICES
    VOICES = load_voices()
    print(f"[sapi4] {len(VOICES)} voices loaded: {list(VOICES)}", flush=True)
    if QWEN_ENABLED:
        print(f"[qwen] voices enabled: {qwen_voice_names()}", flush=True)


@app.get("/healthz")
def healthz():
    ok = bool(VOICES)
    return JSONResponse({"status": "ok" if ok else "degraded", "voices": len(VOICES)},
                        status_code=200 if ok else 503)


@app.get("/api/voices")
def api_voices():
    out = []
    for v in VOICES.values():
        item = dict(v)
        pre = VOICE_PRESETS.get(v["name"])
        if pre:
            item["presetPitch"], item["presetSpeed"] = pre
        out.append(item)
    return {"voices": out,
            "bonzi": {"voice": BONZI_VOICE, "pitch": BONZI_PITCH, "speed": BONZI_SPEED},
            "qwen": [{"name": n} for n in qwen_voice_names()]}


@app.get("/api/tts")
async def api_tts(text: str = Query(..., min_length=1),
                  voice: str = Query(BONZI_VOICE),
                  pitch: Optional[int] = None, speed: Optional[int] = None,
                  format: str = "wav"):
    if is_qwen(voice):  # pitch/speed do not apply to Qwen voices
        data, mime = await qwen_synth_async(text, voice, format)
        return Response(content=data, media_type=mime)
    wav = await synth_async(text, voice, pitch, speed)
    data, mime = to_format(wav, format)
    return Response(content=data, media_type=mime)


# --- tetyys-compatible aliases, so old links/scripts keep working -------------
@app.get("/SAPI4/SAPI4")
async def legacy_tts(text: str, voice: str = BONZI_VOICE,
                     pitch: Optional[int] = None, speed: Optional[int] = None):
    wav = await synth_async(text, voice, pitch, speed)
    return Response(content=wav, media_type="audio/wav")


@app.get("/SAPI4/VoiceLimitations")
def legacy_limits(voice: str):
    if voice not in VOICES:
        raise HTTPException(400, "Invalid voice")
    return VOICES[voice]


class SpeechRequest(BaseModel):
    model: str = "sapi4"
    input: str
    voice: str = "bonzi"
    response_format: str = "wav"
    speed: float = 1.0


@app.post("/v1/audio/speech")
async def openai_speech(req: SpeechRequest):
    """OpenAI-compatible. HA's OpenAI TTS integration talks to this."""
    key = req.voice.strip()
    if is_qwen(key):
        data, mime = await qwen_synth_async(req.input, key, req.response_format, req.speed)
        return Response(content=data, media_type=mime)
    if key in VOICES:
        voice, pitch, speed = key, None, None
    else:
        voice, pitch, speed = OPENAI_ALIASES.get(
            key.lower(), (BONZI_VOICE, BONZI_PITCH, BONZI_SPEED))
    if voice not in VOICES:
        voice, pitch, speed = next(iter(VOICES), ""), None, None
    # OpenAI 'speed' is a multiplier; SAPI4 speed is words-per-minute.
    if req.speed and req.speed != 1.0 and voice in VOICES:
        v = VOICES[voice]
        base = speed if speed is not None else v["defSpeed"]
        speed = max(v["minSpeed"], min(v["maxSpeed"], int(base * req.speed)))
    wav = await synth_async(req.input, voice, pitch, speed)
    data, mime = to_format(wav, req.response_format)
    return Response(content=data, media_type=mime)


@app.get("/v1/models")
def models():
    return {"object": "list",
            "data": [{"id": "sapi4", "object": "model", "owned_by": "sapi4-tts"}]}


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>SAPI4 TTS</title>
<style>
 body{background:#c0c0c0;font-family:"MS Sans Serif",Tahoma,sans-serif;font-size:13px;
      display:flex;justify-content:center;padding:28px}
 .win{background:#c0c0c0;border:2px solid;border-color:#fff #404040 #404040 #fff;
      padding:14px 16px;width:640px;box-shadow:2px 2px 0 #808080}
 h1{font-size:15px;margin:0 0 12px}
 .row{display:grid;grid-template-columns:150px 1fr;gap:8px;align-items:center;margin-bottom:9px}
 label{text-align:right}
 select,input,textarea{font-family:inherit;font-size:13px;padding:2px 4px;
      border:2px solid;border-color:#404040 #fff #fff #404040;background:#fff}
 textarea{width:100%;height:76px;resize:vertical}
 button{font-family:inherit;font-size:13px;padding:4px 18px;background:#c0c0c0;
      border:2px solid;border-color:#fff #404040 #404040 #fff;cursor:pointer}
 button:active{border-color:#404040 #fff #fff #404040}
 .bar{display:flex;gap:8px;margin-top:10px;align-items:center}
 .lim{color:#404040;font-size:11px}
 audio{width:100%;margin-top:12px}
 #status{margin-left:6px;font-size:12px}
 input:disabled{background:#c0c0c0;color:#808080}
</style></head><body><div class="win">
<h1>Microsoft Sam &amp; friends &mdash; SAPI 4.0</h1>
<div class="row"><label>Select voice:</label><select id="voice"></select></div>
<div class="row"><label>Pitch:</label>
  <span><input id="pitch" type="number" style="width:90px"> <span class="lim" id="plim"></span></span></div>
<div class="row"><label>Speed:</label>
  <span><input id="speed" type="number" style="width:90px"> <span class="lim" id="slim"></span></span></div>
<div class="row"><label>Text:</label><textarea id="text">Hello, I am Bonzi Buddy!</textarea></div>
<div class="bar">
  <button onclick="say()">Say it</button>
  <button onclick="bonzi()">Bonzi preset</button>
  <button onclick="dl()">Download .wav</button>
  <span id="status"></span>
</div>
<audio id="player" controls></audio>
</div>
<script>
let V={},Q={},B=null;
const $=id=>document.getElementById(id);
function isQ(n){return !!Q[n];}
async function init(){
  const j=await (await fetch('api/voices')).json();
  B=j.bonzi; const sel=$('voice');
  const g1=document.createElement('optgroup');g1.label='SAPI 4.0';
  j.voices.forEach(v=>{V[v.name]=v;const o=document.createElement('option');
    o.value=v.name;o.textContent=v.name;g1.appendChild(o);});
  sel.appendChild(g1);
  const qs=j.qwen||[];
  if(qs.length){const g2=document.createElement('optgroup');g2.label='Qwen3-TTS';
    qs.forEach(v=>{Q[v.name]=v;const o=document.createElement('option');
      o.value=v.name;o.textContent=v.name;g2.appendChild(o);});
    sel.appendChild(g2);}
  sel.onchange=()=>fill(sel.value);
  if(V[B.voice]){sel.value=B.voice;fill(B.voice);
    $('pitch').value=B.pitch;$('speed').value=B.speed;}
  else if(j.voices.length){sel.value=j.voices[0].name;fill(j.voices[0].name);}
}
function setQ(on){
  ['pitch','speed'].forEach(k=>{$(k).disabled=on;});
  if(on){$('pitch').value='';$('speed').value='';
    $('plim').textContent='(not used by Qwen)';$('slim').textContent='(not used by Qwen)';}
}
function fill(n){
  if(isQ(n)){setQ(true);return;}
  setQ(false);
  const v=V[n];if(!v)return;
  $('pitch').value=(v.presetPitch!==undefined)?v.presetPitch:v.defPitch;
  $('speed').value=(v.presetSpeed!==undefined)?v.presetSpeed:v.defSpeed;
  $('plim').textContent='('+v.minPitch+'-'+v.maxPitch+')';
  $('slim').textContent='('+v.minSpeed+'-'+v.maxSpeed+')';}
function bonzi(){$('voice').value=B.voice;fill(B.voice);
  $('pitch').value=B.pitch;$('speed').value=B.speed;}
function url(){const voice=$('voice').value;
  const p=new URLSearchParams({text:$('text').value,voice:voice});
  if(!isQ(voice)){p.set('pitch',$('pitch').value);p.set('speed',$('speed').value);}
  return 'api/tts?'+p;}
async function say(){const s=$('status');s.textContent='generating...';
  try{const r=await fetch(url());
    if(!r.ok){s.textContent='error: '+await r.text();return;}
    const b=await r.blob();const pl=$('player');
    pl.src=URL.createObjectURL(b);pl.play();s.textContent='';}
  catch(e){s.textContent='error: '+e;}}
function dl(){const a=document.createElement('a');a.href=url();
  a.download=(isQ($('voice').value)?'qwen':'sapi4')+'.wav';a.click();}
init();
</script></body></html>
"""
