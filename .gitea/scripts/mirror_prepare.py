#!/usr/bin/env python3
"""
Build a binary-free mirror of this repo for public GitHub.

Every vendored Windows binary is replaced by a `<name>.txt` placeholder whose
content explains what the file is and where to obtain it. Nothing proprietary
is published; the repo stays buildable by anyone who fetches the four
installers themselves.

Usage:
    mirror_prepare.py <source-dir> <dest-dir>

The destination is populated in place (its .git/ is preserved).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# Binary -> provenance note. Every entry MUST be reproduced as <name>.txt.
SOURCES: dict[str, dict] = {
    "engines/SAPI4SDK.exe": {
        "desc": "Microsoft Speech SDK 4.0 (1998). Self-extracting CAB.\n"
                "Needed at build time ONLY for speech.h, which lives two CABs\n"
                "deep: SAPI4SDK.exe -> spchsdk.exe -> ipeech.h (renamed).",
        "url": "https://github.com/TETYYS/SAPI4",
        "note": "Committed in the TETYYS/SAPI4 repository root.",
    },
    "engines/spchapi.exe": {
        "desc": "Microsoft Speech API 4.0 runtime (1998). Self-extracting CAB.\n"
                "Provides SPEECH.DLL and the TTS enumerator COM server.",
        "url": "https://github.com/TETYYS/SAPI4",
        "note": "Committed in the TETYYS/SAPI4 repository root.",
    },
    "engines/tv_enua.exe": {
        "desc": "Lernout & Hauspie TruVoice American English TTS Engine (1998).\n"
                "Provides the 10 TruVoice voices, including Adult Male #2 —\n"
                "the BonziBUDDY voice.",
        "url": "https://github.com/TETYYS/SAPI4",
        "note": "Committed in the TETYYS/SAPI4 repository root.",
    },
    "engines/msttsl.exe": {
        "desc": "Microsoft Text-to-Speech Engine 4.0 (English), 1998.\n"
                "Provides Microsoft Sam, Mary and Mike, the RoboSoft voices,\n"
                "Male/Female Whisper and the Hall/Stadium/Space variants.",
        "url": "https://web.archive.org/web/20010414021222if_/"
               "http://activex.microsoft.com:80/activex/controls/sapi/msttsl.exe",
        "note": "Originally distributed by Microsoft at activex.microsoft.com;\n"
                "that host is long dead, so use the Internet Archive copy.\n"
                "Expected size: 7,674,104 bytes.",
    },
}

PLACEHOLDER = """\
{name} — PLACEHOLDER, NOT THE REAL FILE
{rule}

{desc}

DOWNLOAD FROM:
  {url}

{note}

WHY THIS IS A PLACEHOLDER
  This public mirror carries no binaries. The original is proprietary
  Microsoft / Lernout & Hauspie abandonware and is not redistributed here.

TO BUILD
  Download the file above, save it to `{path}` (drop the .txt), and run
  `docker compose up -d --build`. The Dockerfile expects the real binary at
  that exact path.
"""

# Never mirror these.
EXCLUDE_DIRS = {".git", "samples", "__pycache__", ".pytest_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".wav", ".mp3", ".bundle", ".tgz", ".tar.gz"}


def placeholder_for(rel: str) -> str:
    meta = SOURCES[rel]
    name = Path(rel).name
    return PLACEHOLDER.format(
        name=name, rule="=" * (len(name) + 28), desc=meta["desc"],
        url=meta["url"], note=meta["note"], path=rel,
    )


def is_tracked_binary(rel: str) -> bool:
    return rel in SOURCES


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    src, dst = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    if not src.is_dir() or not dst.is_dir():
        print(f"error: both paths must exist: {src} {dst}", file=sys.stderr)
        return 2

    # Wipe the destination except .git, so deletions propagate.
    for item in dst.iterdir():
        if item.name == ".git":
            continue
        shutil.rmtree(item) if item.is_dir() else item.unlink()

    copied, placeheld, skipped = [], [], []
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src).as_posix()
        if any(part in EXCLUDE_DIRS for part in path.relative_to(src).parts):
            continue
        if path.is_dir():
            continue
        if path.suffix in EXCLUDE_SUFFIXES:
            skipped.append(rel)
            continue

        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)

        if is_tracked_binary(rel):
            (target.parent / (target.name + ".txt")).write_text(placeholder_for(rel))
            placeheld.append(rel + ".txt")
        else:
            shutil.copy2(path, target)
            copied.append(rel)

    # Fail loudly rather than publishing a binary we forgot to map.
    leftovers = [
        p.relative_to(dst).as_posix()
        for p in dst.rglob("*")
        if p.is_file() and p.suffix.lower() in {".exe", ".dll", ".bin", ".msi"}
    ]
    if leftovers:
        print(f"FATAL: binaries would be published: {leftovers}", file=sys.stderr)
        return 1

    print(f"copied {len(copied)} files")
    print(f"placeholders {len(placeheld)}: {placeheld}")
    if skipped:
        print(f"skipped {len(skipped)}: {skipped}")
    if len(placeheld) != len(SOURCES):
        print(f"FATAL: expected {len(SOURCES)} placeholders, wrote {len(placeheld)}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
