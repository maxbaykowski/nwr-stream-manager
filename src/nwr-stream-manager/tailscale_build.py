"""nwr-stream-manager --build-libtailscale: builds libtailscale, which connecting to Tailscale
from Settings needs, where NWR Stream Manager finds it and reinstalling doesn't touch it."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

BUILD_SCRIPT = Path(__file__).resolve().parent / "libtailscale" / "build.sh"
# What the build needs, and what to install for each, by its Debian and Fedora names.
BUILD_TOOLS = (
    (("git",), "git"),
    (("go",), "Go (golang-go on Debian and Raspberry Pi OS, golang on Fedora)"),
    (("cc", "gcc", "clang"), "a C compiler (gcc)"),
)


# Go downloads the newer Go that libtailscale asks for itself, from Go 1.21 on.
GO_DOWNLOADS_ITSELF_FROM = (1, 21)


def go_version() -> tuple[int, int] | None:
    try:
        output = subprocess.run(["go", "env", "GOVERSION"], capture_output=True, text=True, timeout=30, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    # Such as go1.26.10, or go1.26.10-X:nodwarf5 on Fedora.
    match = re.match(r"go(\d+)\.(\d+)", output.strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def build_libtailscale() -> int:
    missing = [name for commands, name in BUILD_TOOLS if not any(shutil.which(command) for command in commands)]
    if missing:
        needed = missing[0] if len(missing) == 1 else ", ".join(missing[:-1]) + " and " + missing[-1]
        print(f"Building libtailscale needs {needed}. Install {'it' if len(missing) == 1 else 'them'}, then run this again.")
        return 1
    version = go_version()
    if version is not None and version < GO_DOWNLOADS_ITSELF_FROM:
        print(
            f"This computer's Go, version {version[0]}.{version[1]}, is too old to build libtailscale. Install Go 1.21 or later, "
            "such as golang-go from bookworm-backports on Debian 12 and Raspberry Pi OS based on it, then run this again."
        )
        return 1
    if os.geteuid() == 0:
        print("Building libtailscale for every user on this computer, including the systemd service.", flush=True)
    else:
        print("Building libtailscale for this user. To build it for the systemd service, run this with sudo instead.", flush=True)
    print("This downloads libtailscale and the Go packages it uses, and can take a few minutes.", flush=True)
    try:
        result = subprocess.run(["sh", str(BUILD_SCRIPT)], check=False)
    except KeyboardInterrupt:
        return 130
    if result.returncode != 0:
        print("libtailscale couldn't be built; the messages above say why.")
        return result.returncode
    print("NWR Stream Manager can now connect to Tailscale from Settings. If it's running, there's no need to restart it.")
    return 0
