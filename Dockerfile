# sapi4-tts — Microsoft SAPI 4.0 + L&H TruVoice as a native Linux REST service
#
# Two-stage build:
#   1. mingw-w64 cross-compiles TETYYS's C++ shims to 32-bit Windows PE.
#      (No MSVC, no Windows machine — the original README's hardest requirement.)
#   2. Runtime image builds the WINEPREFIX at BUILD time, so container start is
#      non-interactive and repeatable.
#
# Requires in build context: SAPI4SDK.exe, spchapi.exe, tv_enua.exe, sapi4*.cpp, sapi4.hpp

# ---------- stage 1: cross-compile the SAPI4 shims ----------
FROM debian:bookworm AS builder

RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
      mingw-w64 cabextract ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
# C++ shims (TETYYS's, MIT) and the vendored engine installers.
COPY src/ ./src/
COPY engines/ /engines/
RUN cp src/*.cpp src/*.hpp ./

# speech.h lives two CABs deep and is renamed: SAPI4SDK.exe -> spchsdk.exe -> ipeech.h
RUN cabextract -d sdk1 /engines/SAPI4SDK.exe \
    && cabextract -d sdk2 sdk1/spchsdk.exe \
    && cp sdk2/ipeech.h ./speech.h \
    && test -s speech.h

# CRITICAL: SAPI 4 is an ANSI-only API. -municode makes every szModeName a
# WCHAR* and the build dies on strcmp/strcpy. Force the ANSI code paths.
ENV CXXFLAGS="-O2 -UUNICODE -U_UNICODE -DWINVER=0x0400"

RUN i686-w64-mingw32-g++ -c sapi4.cpp -I. -o sapi4.o ${CXXFLAGS} \
 && i686-w64-mingw32-g++ -shared -o sapi4.dll sapi4.o \
      -lole32 -luuid -loleaut32 -static-libgcc -static-libstdc++ \
      -Wl,--out-implib,libsapi4.a \
 && i686-w64-mingw32-g++ -o sapi4out.exe sapi4out.cpp -I. -L. -lsapi4 \
      -lole32 -luuid -static-libgcc -static-libstdc++ ${CXXFLAGS} \
 && i686-w64-mingw32-g++ -o sapi4limits.exe sapi4limits.cpp -I. -L. -lsapi4 \
      -lole32 -luuid -static-libgcc -static-libstdc++ ${CXXFLAGS} \
 && ls -la sapi4.dll sapi4out.exe sapi4limits.exe

# Unpack the engine installers here too — they are plain self-extracting CABs.
# Running the .exe installers under Wine HANGS on a GUI dialog forever.
RUN cabextract -d spch /engines/spchapi.exe && cabextract -d truvoice /engines/tv_enua.exe \
 && if [ -f /engines/msttsl.exe ]; then cabextract -d mstts /engines/msttsl.exe; else mkdir -p mstts; fi

# ---------- stage 2: runtime ----------
FROM debian:bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    WINEPREFIX=/opt/sapi4/wine \
    WINEARCH=win32 \
    WINEDEBUG=-all \
    DISPLAY=:99 \
    SAPI4_APPDIR=/opt/sapi4/bin \
    PYTHONUNBUFFERED=1

RUN dpkg --add-architecture i386 \
 && apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends \
      wine wine32 xvfb xauth winbind ffmpeg procps \
      python3 python3-pip python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /opt/sapi4
COPY --from=builder /build/sapi4.dll /build/sapi4out.exe /build/sapi4limits.exe /opt/sapi4/bin/
COPY --from=builder /build/spch/ /opt/sapi4/install/spch/
COPY --from=builder /build/truvoice/ /opt/sapi4/install/truvoice/
COPY --from=builder /build/mstts/ /opt/sapi4/install/mstts/
COPY build_prefix.sh /opt/sapi4/
RUN chmod +x /opt/sapi4/build_prefix.sh && /opt/sapi4/build_prefix.sh

COPY app/ /opt/sapi4/app/
COPY entrypoint.sh /opt/sapi4/
RUN chmod +x /opt/sapi4/entrypoint.sh

EXPOSE 5000 10200
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz',timeout=5).status==200 else 1)"

ENTRYPOINT ["/opt/sapi4/entrypoint.sh"]
