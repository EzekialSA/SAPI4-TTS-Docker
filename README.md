# sapi4-tts

Microsoft SAPI 4.0 text-to-speech — Microsoft Sam, BonziBUDDY, and 27 other
1998-era voices — as a native Linux container with a REST API, an
OpenAI-compatible endpoint, Wyoming protocol for Home Assistant, and a web UI.

No Windows machine is needed to build or run it.

Based on [TETYYS/SAPI4](https://github.com/TETYYS/SAPI4) (the engine shims are
his C++; the web server, APIs, and container are a rewrite).

## Quick start

```bash
docker compose up -d --build
# web UI      http://<host>:5000/
# Wyoming     tcp://<host>:10200
```

## Voices (29)

| Group | Voices |
|---|---|
| Microsoft | Sam, Mary, Mike (+ Mary/Mike "for Telephone") |
| Effects | RoboSoft One–Six, Male Whisper, Female Whisper |
| Reverb | Mary/Mike in Hall, in Stadium, in Space |
| TruVoice (L&H) | Adult Male #1–8, Adult Female #1–2 |

**BonziBUDDY** = `Adult Male #2, American English (TruVoice)`, pitch **140**,
speed **157**. Exposed as the alias `bonzi` and as the web UI's preset button.

Microsoft voices render at 22 kHz; TruVoice at 11 kHz.

## API

### Plain REST
```bash
curl -o out.wav \
  "http://host:5000/api/tts?text=Hello&voice=Sam&pitch=100&speed=150"
```
`format=` accepts `wav` (default), `mp3`, `opus`, `flac`, `aac`.

`GET /api/voices` returns every voice with its real `min/max/def` pitch and
speed. Limits are per-voice — Sam is pitch 50–200, TruVoice Male #2 is 50–400 —
so read them from the API rather than assuming.

### OpenAI-compatible
```bash
curl -X POST http://host:5000/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"sapi4","input":"Hello","voice":"bonzi","response_format":"mp3"}' \
  -o out.mp3
```
Aliases: `bonzi`/`alloy` → BonziBUDDY preset, `sam`, `mary`, `mike`,
`whisper`, `robot`. Any exact voice name also works. The OpenAI `speed`
multiplier is mapped onto SAPI4's words-per-minute range and clamped.

### tetyys.com compatibility
`GET /SAPI4/SAPI4?text=&voice=&pitch=&speed=` and
`GET /SAPI4/VoiceLimitations?voice=` behave like the original site.

## Home Assistant

### 1. Add the integration (Wyoming)

Settings → Devices & Services → **Add Integration** → **Wyoming Protocol** →
host `<docker-host-ip>`, port `10200`.

Wyoming is **raw TCP**. Do not point it at a reverse proxy — Caddy/nginx
vhosts are HTTP(S) only. Use the container host's IP directly.

This creates a `tts.sapi4` entity exposing all 29 voices. It appears anywhere
HA accepts a TTS engine: `tts.speak`, Assist pipelines, automations, scripts.

### 2. Speak something

```yaml
action: tts.speak
target:
  entity_id: tts.sapi4
data:
  media_player_entity_id: media_player.living_room_speaker
  message: "Would you like to hear a joke?"
  options:
    voice: "Adult Male #2, American English (TruVoice)"
```

Omit `options.voice` entirely and you get the BonziBUDDY preset by default.

### 3. Pitch and speed

The Wyoming protocol has **no pitch/speed fields** — it transmits a voice name
and nothing else. Two ways to control them:

**Presets (automatic).** Voices listed in `VOICE_PRESETS` in `app/server.py`
carry their own pitch/speed. `Adult Male #2` is preset to 140/157, which is
what makes it BonziBUDDY rather than a flat TruVoice male. Naming that voice
in HA gets the preset with no extra syntax.

**Per-call override.** Append `|pitch,speed` to the voice name:

```yaml
options:
  voice: "Adult Male #2, American English (TruVoice)|300,100"   # squeaky, slow
  # voice: "Sam|180,250"                                        # high, fast
  # voice: "Sam|,250"                                           # speed only
```

The separator is `|`, **not** `#` — every TruVoice name already contains a `#`
(`Adult Male #2, ...`), which would split the name itself.

Out-of-range values are rejected per voice; read the real limits from
`GET /api/voices` (Sam is pitch 50–200, TruVoice Male #2 is 50–400).

### 4. Full example automation

```yaml
alias: Sunset announcement
triggers:
  - trigger: sun
    event: sunset
conditions: []
actions:
  - action: tts.speak
    target:
      entity_id: tts.sapi4
    data:
      cache: true
      media_player_entity_id: media_player.living_room_speaker
      message: The sun is down.
      options:
        voice: "Adult Male #2, American English (TruVoice)"
mode: single
```

⚠️ **`cache: true` caches by text + voice.** If you change a preset or fix a
voice bug, previously cached clips keep playing the old audio. Change the
message text or set `cache: false` while testing.

### Alternative: OpenAI TTS integration

Point the integration's base URL at `http://<host>:5000/v1`; any API key value
is accepted. Voice accepts an alias (`bonzi`, `sam`, `mary`, `mike`,
`whisper`, `robot`) or an exact voice name.

### Alternative: rest_command (full control, any voice/pitch/speed)

Requires `rest_command:` in `configuration.yaml`:

```yaml
rest_command:
  bonzi_say:
    url: >-
      http://<host>:5000/api/tts?text={{ text | urlencode }}&voice={{ 'Adult Male #2, American English (TruVoice)' | urlencode }}&pitch={{ pitch | default(140) }}&speed={{ speed | default(157) }}
    method: GET
```

This returns audio to the caller rather than playing it, so it is mainly
useful for generating files. For playback, prefer `tts.speak`.

### Troubleshooting

| Symptom | Cause |
|---|---|
| Voice sounds flat / wrong pitch | HA cached an older render — change the text or use `cache: false` |
| `Unknown voice` | Name must match `/api/voices` exactly, including `#` and the `(TruVoice)` suffix |
| Integration won't connect | Wyoming is raw TCP on 10200; not proxied, not HTTPS |
| Pitch override ignored | Separator is `\|`, not `#` |

## How it works

1. **Build stage** — `mingw-w64` cross-compiles TETYYS's C++ COM shims
   (`sapi4.dll`, `sapi4out.exe`, `sapi4limits.exe`) to 32-bit Windows PE.
   The upstream README requires MSVC on Windows; this does not.
2. **Runtime stage** — Debian + 32-bit Wine. The WINEPREFIX is built at *image
   build* time with SAPI 4.0 + TruVoice + the Microsoft TTS engine registered,
   so container start is non-interactive.
3. **Service** — FastAPI shells out to `sapi4out.exe` under a resident
   `wineserver`, and serves REST + OpenAI + the UI. Wyoming runs alongside.

The build **fails** if voice enumeration returns nothing, rather than shipping
an image that starts cleanly and produces silence.

## Three traps, for anyone rebuilding this

1. **`-municode` breaks the build.** SAPI 4 is ANSI-only; the Unicode flag
   turns every `szModeName` into `WCHAR*` and `strcmp`/`strcpy` fail to
   compile. Build with `-UUNICODE -U_UNICODE`.

2. **The installers hang forever under Wine.** `spchapi.exe`, `tv_enua.exe`,
   and `msttsl.exe` are self-extracting CABs that block on a GUI dialog.
   `cabextract` them and install the INFs directly. `mstts.inf` goes further —
   it has `BeginPrompt`/`EndPrompt` sections that hang even
   `InstallHinfSection`, so its registry keys are applied with `regedit /S`.

3. **Wine does not expand the INF `%49000%` destination macro.** Every COM
   path lands in the registry as the literal string `%49000%\Speech.dll`.
   Install "succeeds", `regsvr32` returns 0, and voice enumeration returns
   `(null)`. Rewrite the macro in `system.reg`, then `wineserver -k`.

Trap 3 is the one that makes this look impossible: every step reports success
and you get silence.

## Repo layout

```
Dockerfile           two-stage: mingw cross-compile -> wine runtime
build_prefix.sh      builds the WINEPREFIX at image-build time
entrypoint.sh        Xvfb -> resident wineserver -> Wyoming + FastAPI
docker-compose.yml   standalone deploy
app/server.py        FastAPI: web UI, REST, OpenAI-compatible
app/wyoming_server.py  Wyoming protocol (HA TTS provider)
src/                 TETYYS's C++ SAPI4 shims (MIT) — cross-compiled at build
engines/             vendored 1998 installers (proprietary abandonware)
  SAPI4SDK.exe         SDK; speech.h is extracted from it
  spchapi.exe          SAPI 4.0 runtime
  tv_enua.exe          L&H TruVoice voices
  msttsl.exe           Microsoft Sam/Mary/Mike + RoboSoft/Whisper
```

## Licence

`sapi4.cpp`, `sapi4.hpp`, `sapi4out.cpp`, `sapi4limits.cpp` are TETYYS's, MIT
(see LICENSE). The bundled Microsoft and Lernout & Hauspie binaries are
proprietary abandonware, redistributed here for personal archival use.
