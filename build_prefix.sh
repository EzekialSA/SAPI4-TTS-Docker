#!/bin/bash
# Build the WINEPREFIX with SAPI 4.0 + L&H TruVoice registered.
# Runs at IMAGE BUILD time so the container starts non-interactively.
#
# Three non-obvious things, each of which silently produces a working-looking
# install that generates no audio:
#
#  1. spchapi.exe / tv_enua.exe under Wine HANG on a GUI dialog forever.
#     They are self-extracting CABs — unpack them and drive the INFs directly.
#  2. Wine's InstallHinfSection does NOT expand the INF destination macro
#     %49000%. Every COM path lands in the registry as the literal string
#     "%49000%\Speech.dll", so CoCreateInstance fails and voice enumeration
#     returns (null) while every command still exits 0.
#  3. The registry edit only takes effect after wineserver -k.
set -euo pipefail

WINEPREFIX="${WINEPREFIX:-/opt/sapi4/wine}"
SRC=/opt/sapi4/install
W="$WINEPREFIX/drive_c/windows"
SPEECH_DIR='C:\windows\speech'
export DISPLAY="${DISPLAY:-:99}"

echo "[prefix] starting Xvfb"
Xvfb :99 -screen 0 1024x768x16 >/dev/null 2>&1 &
XVFB_PID=$!
sleep 2

# xvfb-run needs xauth; without it every wine call silently no-ops and the
# whole install "succeeds" while doing nothing. Use the already-running Xvfb.
command -v xauth >/dev/null || { echo "[prefix] FATAL: xauth missing" >&2; exit 1; }

echo "[prefix] wineboot"
wineboot -i >/dev/null 2>&1 || true
sleep 5

echo "[prefix] staging engine files"
mkdir -p "$W/speech" "$W/lhsp/tv" "$W/lhsp/help" "$W/Fonts"

# SAPI 4.0 runtime
cp -f "$SRC"/spch/*.DLL "$W/speech/" 2>/dev/null || true
cp -f "$SRC"/spch/*.EXE "$W/speech/" 2>/dev/null || true
cp -f "$SRC"/spch/*.TLB "$W/speech/" 2>/dev/null || true
cp -f "$SRC"/spch/*.HLP "$SRC"/spch/*.CNT "$W/speech/" 2>/dev/null || true
cp -f "$SRC"/spch/*.DLL "$W/system32/" 2>/dev/null || true
cp -f "$SRC"/spch/*.TLB "$W/system32/" 2>/dev/null || true

# TruVoice engine — the INF's own DestinationDirs put it under lhsp\tv
cp -f "$SRC"/truvoice/tv_enua.dll "$SRC"/truvoice/tvenuax.dll "$W/lhsp/tv/"
cp -f "$SRC"/truvoice/Msvcirt.dll "$SRC"/truvoice/Msvcp50.dll "$W/system32/" 2>/dev/null || true
cp -f "$SRC"/truvoice/andmoipa.ttf "$W/Fonts/" 2>/dev/null || true

cp -f "$SRC"/spch/SPCHAPI.INF "$W/speech/"
cp -f "$SRC"/truvoice/tv_enua.inf "$W/speech/"

echo "[prefix] installing INFs (bypasses the hanging GUI installers)"
cd "$W/speech"
# Both INFs can wedge: SPCHAPI/tv_enua block on RunPostSetupCommands once a
# real DISPLAY exists. The registry work is already done by the time they hang,
# so cap them and clean up the stuck rundll32 rather than waiting forever.
timeout 45 wine rundll32.exe setupapi.dll,InstallHinfSection DefaultInstall 128 \
    "${SPEECH_DIR}\\SPCHAPI.INF" >/dev/null 2>&1 || true
pkill -f rundll32 2>/dev/null || true
sleep 1
timeout 45 wine rundll32.exe setupapi.dll,InstallHinfSection DefaultInstall 128 \
    "${SPEECH_DIR}\\tv_enua.inf" >/dev/null 2>&1 || true
pkill -f rundll32 2>/dev/null || true
sleep 2

# --- Microsoft TTS engine (Sam / Mary / Mike / RoboSoft / Whisper) -----------
# mstts.inf carries BeginPrompt/EndPrompt dialog sections. Under Wine,
# InstallHinfSection BLOCKS FOREVER on those prompts even with the quiet flag
# (rundll32 stays resident, the install never completes). The INF's [TTSReg]
# section is pure AddReg, so apply it directly via regedit and skip setupapi.
if [ -d "$SRC/mstts" ]; then
    echo "[prefix] installing Microsoft TTS engine (registry-direct)"
    cp -f "$SRC"/mstts/*.dll "$SRC"/mstts/*.vce "$SRC"/mstts/*.cfg "$W/speech/" 2>/dev/null || true
    cat > /tmp/mstts.reg <<'REGEOF'
REGEDIT4

[HKEY_LOCAL_MACHINE\Software\Voice\TextToSpeech\Engine]
"MSTTSSyn"="{E0725551-286F-11d0-8E73-00A0C9083363}"

[HKEY_CLASSES_ROOT\CLSID\{E0725551-286F-11d0-8E73-00A0C9083363}]
@="Microsoft TTS Engine"

[HKEY_CLASSES_ROOT\CLSID\{E0725551-286F-11d0-8E73-00A0C9083363}\InprocServer32]
@="C:\\windows\\speech\\MSTTSSYN.dll"
"ThreadingModel"="Apartment"

[HKEY_LOCAL_MACHINE\Software\Voice\TextToSpeech\Engine\{E0725551-286F-11d0-8E73-00A0C9083363}]
"Dirty"=hex:01

[HKEY_LOCAL_MACHINE\Software\Microsoft\MSTTS]
"InstallDir"="C:\\windows\\speech"
REGEOF
    timeout 45 wine regedit /S /tmp/mstts.reg >/dev/null 2>&1 || true
    sleep 2
fi

echo "[prefix] registering COM servers"
timeout 45 wine regsvr32 /s 'C:\windows\lhsp\tv\tv_enua.dll'  >/dev/null 2>&1 || true
timeout 45 wine regsvr32 /s 'C:\windows\lhsp\tv\tvenuax.dll'  >/dev/null 2>&1 || true
timeout 45 wine regsvr32 /s 'C:\windows\speech\SPEECH.DLL'    >/dev/null 2>&1 || true
sleep 2

# THE load-bearing fix. Without this, enumeration returns (null).
echo "[prefix] expanding unresolved %49000% destination macro"
BEFORE=$(grep -c '%49000%' "$WINEPREFIX/system.reg" || true)
sed -i 's|%49000%|C:\\\\windows\\\\speech|g' "$WINEPREFIX/system.reg"
AFTER=$(grep -c '%49000%' "$WINEPREFIX/system.reg" || true)
echo "[prefix] rewrote $BEFORE macro refs (remaining: ${AFTER:-0})"

wineserver -k 2>/dev/null || true
sleep 3

echo "[prefix] verifying voice enumeration"
cd /opt/sapi4/bin
VOICES=$(timeout 60 wine sapi4limits.exe 2>/dev/null | tr -d '\r' | grep -v '^$' | grep -v 'X connection' || true)
COUNT=$(printf '%s\n' "$VOICES" | grep -c . || true)
echo "[prefix] voices found: $COUNT"
printf '%s\n' "$VOICES"

kill $XVFB_PID 2>/dev/null || true

if [ "${COUNT:-0}" -lt 1 ]; then
    echo "[prefix] FATAL: no TruVoice voices registered — refusing to ship a broken image" >&2
    exit 1
fi
echo "[prefix] OK"
