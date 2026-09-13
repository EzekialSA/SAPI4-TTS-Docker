#!/usr/bin/env python3
"""
Wyoming protocol server for sapi4-tts.

Makes SAPI4/TruVoice appear as a native Home Assistant TTS provider:
Settings -> Devices & Services -> Add Integration -> Wyoming Protocol -> host:10200

Wyoming streams raw PCM chunks, so the WAV header is stripped and the frames
are sent as audio-start / audio-chunk* / audio-stop.
"""
import argparse
import asyncio
import io
import logging
import os
import wave

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.info import Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.tts import Synthesize

import server as sapi  # reuse the synth/voice layer

_LOG = logging.getLogger("wyoming_sapi4")
ATTRIB = Attribution(name="Microsoft SAPI 4 / L&H TruVoice", url="https://tetyys.com/SAPI4/")


def build_info() -> Info:
    voices = []
    for name, v in sapi.VOICES.items():
        voices.append(
            TtsVoice(
                name=name,
                description=f"{name} (pitch {v['minPitch']}-{v['maxPitch']}, "
                            f"speed {v['minSpeed']}-{v['maxSpeed']})",
                attribution=ATTRIB,
                installed=True,
                version=None,
                languages=["en-US"],
            )
        )
    return Info(
        tts=[
            TtsProgram(
                name="sapi4",
                description="Microsoft SAPI 4.0 TTS (TruVoice) under Wine",
                attribution=ATTRIB,
                installed=True,
                version="1.0.0",
                voices=voices,
            )
        ]
    )


class SAPI4EventHandler(AsyncEventHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def handle_event(self, event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(build_info().event())
            return True

        if not Synthesize.is_type(event.type):
            return True

        synthesize = Synthesize.from_event(event)
        text = " ".join(synthesize.text.strip().splitlines())

        voice_name = sapi.BONZI_VOICE
        pitch, speed = None, None
        if synthesize.voice and synthesize.voice.name:
            requested = synthesize.voice.name.strip()
            # The Wyoming protocol has no pitch/speed fields, so allow an
            # optional "Voice Name|pitch,speed" suffix as an escape hatch
            # (e.g. "Sam|180,90"). Without a suffix, server.VOICE_PRESETS
            # supplies the per-voice default — that is what keeps
            # "Adult Male #2" sounding like BonziBUDDY.
            #
            # The separator is '|', NOT '#': every TruVoice name already
            # contains a '#' ("Adult Male #2, American English (TruVoice)"),
            # so '#' would split the voice name itself.
            if "|" in requested:
                requested, _, spec = requested.rpartition("|")
                requested = requested.strip()
                parts = [p.strip() for p in spec.split(",")]
                try:
                    if parts and parts[0]:
                        pitch = int(parts[0])
                    if len(parts) > 1 and parts[1]:
                        speed = int(parts[1])
                except ValueError:
                    _LOG.warning("bad pitch/speed spec %r, ignoring", spec)
                    pitch = speed = None
            if requested in sapi.VOICES:
                voice_name = requested
            else:
                voice_name, a_pitch, a_speed = sapi.OPENAI_ALIASES.get(
                    requested.lower(),
                    (sapi.BONZI_VOICE, None, None),
                )
                if pitch is None:
                    pitch = a_pitch
                if speed is None:
                    speed = a_speed
        if voice_name not in sapi.VOICES:
            voice_name = next(iter(sapi.VOICES), "")
            pitch = speed = None

        _LOG.info("synthesize: voice=%r pitch=%s speed=%s len=%d",
                  voice_name, pitch, speed, len(text))

        wav_bytes = await asyncio.to_thread(sapi.synth, text, voice_name, pitch, speed)

        with io.BytesIO(wav_bytes) as buf, wave.open(buf, "rb") as wf:
            rate, width, channels = wf.getframerate(), wf.getsampwidth(), wf.getnchannels()
            frames = wf.readframes(wf.getnframes())

        await self.write_event(
            AudioStart(rate=rate, width=width, channels=channels).event()
        )
        chunk = 1024 * width * channels
        for i in range(0, len(frames), chunk):
            await self.write_event(
                AudioChunk(rate=rate, width=width, channels=channels,
                           audio=frames[i:i + chunk]).event()
            )
        await self.write_event(AudioStop().event())
        return True


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default=os.environ.get("WYOMING_URI", "tcp://0.0.0.0:10200"))
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    sapi.VOICES = await asyncio.to_thread(sapi.load_voices)
    _LOG.info("loaded %d voices", len(sapi.VOICES))
    if not sapi.VOICES:
        _LOG.error("no voices available — check the WINEPREFIX")

    server = AsyncServer.from_uri(args.uri)
    _LOG.info("wyoming listening on %s", args.uri)
    await server.run(SAPI4EventHandler)


if __name__ == "__main__":
    asyncio.run(main())
