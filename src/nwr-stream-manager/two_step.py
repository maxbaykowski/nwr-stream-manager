"""Two-step verification: a code from an authenticator app (TOTP, RFC 6238), asked for after the
password whenever NWR Stream Manager is reached from outside its local network (see local_network).

Recovery codes stand in for the app if the phone it's on is lost; each works once.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import hashlib
import hmac
import logging
import secrets
import socket
import struct
import threading
import time
import zlib
from urllib.parse import quote, urlencode

LOG = logging.getLogger(__name__)

ISSUER = "NWR Stream Manager"
STEP_SECONDS = 30
DIGITS = 6
# A code from the step before or after is accepted too, for a phone whose clock is a little off.
ALLOWED_STEPS_OFF = 1
RECOVERY_CODE_COUNT = 10
REMEMBER_DEVICE_SECONDS = 30 * 24 * 3600
SETUP_CODE_SECONDS = 24 * 3600


def new_secret() -> str:
    """A key for an authenticator app, as it's typed into one: base32, without padding."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    cleaned = secret.replace(" ", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_SECONDS)


def matching_step(secret: str, code: str, last_used_step: int = -1, now: float | None = None) -> int | None:
    """The step a code is for, if it's right and newer than the last one used; None otherwise.
    A code that's been used can't be used again, even within its 30 seconds."""
    code = "".join(character for character in str(code) if character.isdigit())
    if len(code) != DIGITS:
        return None
    step = current_step(now)
    for candidate in range(step - ALLOWED_STEPS_OFF, step + ALLOWED_STEPS_OFF + 1):
        if candidate > last_used_step and hmac.compare_digest(code_at(secret, candidate), code):
            return candidate
    return None


def grouped_secret(secret: str) -> str:
    """The key in groups of four, which is easier to type and to read out."""
    return " ".join(secret[index : index + 4] for index in range(0, len(secret), 4))


def authenticator_name(username: str, instance_name: str) -> str:
    """What the account is called in an authenticator app, under NWR Stream Manager: the username and
    which NWR Stream Manager it's on, as someone may have it on several computers, each with its own key."""
    place = instance_name.strip()
    if place.startswith(f"{ISSUER} on "):
        place = place[len(f"{ISSUER} on ") :].strip()
    if not place or place == ISSUER:
        place = socket.gethostname().split(".")[0].strip()
    return f"{username} on {place}" if place else username


def setup_uri(secret: str, username: str, instance_name: str) -> str:
    """The otpauth: link an authenticator app takes the key from, by QR code or opened on a phone."""
    label = quote(f"{ISSUER}:{authenticator_name(username, instance_name)}", safe="")
    return f"otpauth://totp/{label}?" + urlencode(
        {"secret": secret, "issuer": ISSUER, "algorithm": "SHA1", "digits": DIGITS, "period": STEP_SECONDS}
    )


# -- recovery codes ----------------------------------------------------------

RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # nothing that's easily mistaken for something else


def _new_code() -> str:
    letters = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(10))
    return f"{letters[:5]}-{letters[5:]}"


def new_recovery_codes() -> list[str]:
    return [_new_code() for _ in range(RECOVERY_CODE_COUNT)]


def new_setup_code() -> str:
    """A code the owner gives someone, to set up two-step verification from outside the local network.
    It looks like a recovery code, and is kept the same way, as a hash."""
    return _new_code()


def normalize_recovery_code(code: str) -> str:
    return "".join(character for character in str(code).lower() if character in RECOVERY_ALPHABET)


def recovery_code_hash(code: str) -> str:
    return hashlib.sha256(normalize_recovery_code(code).encode("ascii")).hexdigest()


def looks_like_recovery_code(code: str) -> bool:
    return len(normalize_recovery_code(code)) == 10


# -- QR codes ----------------------------------------------------------------
# From libqrencode, if it's installed (qrencode-libs on Fedora, libqrencode4 on Debian and
# Raspberry Pi OS); without it, the key is shown to be typed in, as it always is too.


class _QRcode(ctypes.Structure):
    _fields_ = [("version", ctypes.c_int), ("width", ctypes.c_int), ("data", ctypes.POINTER(ctypes.c_ubyte))]


_qrencode: ctypes.CDLL | None = None
_qrencode_looked = False
_qrencode_lock = threading.Lock()
QR_ECLEVEL_M = 1
QR_MODE_8 = 2


def _load_qrencode() -> ctypes.CDLL | None:
    global _qrencode, _qrencode_looked
    with _qrencode_lock:
        if _qrencode_looked:
            return _qrencode
        _qrencode_looked = True
        for name in ("libqrencode.so.4", ctypes.util.find_library("qrencode")):
            if not name:
                continue
            try:
                lib = ctypes.CDLL(name)
            except OSError:
                continue
            lib.QRcode_encodeString.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
            lib.QRcode_encodeString.restype = ctypes.POINTER(_QRcode)
            lib.QRcode_free.argtypes = [ctypes.POINTER(_QRcode)]
            lib.QRcode_free.restype = None
            _qrencode = lib
            break
        else:
            LOG.info("libqrencode isn't installed, so two-step verification is set up by typing in its key")
        return _qrencode


def qr_code_png(text: str, scale: int = 6, border: int = 4) -> bytes | None:
    """text as a QR code, a black and white PNG; None without libqrencode."""
    lib = _load_qrencode()
    if lib is None:
        return None
    code = lib.QRcode_encodeString(text.encode("utf-8"), 0, QR_ECLEVEL_M, QR_MODE_8, 1)
    if not code:
        return None
    try:
        width = code.contents.width
        # The lowest bit of each module says whether it's dark.
        modules = [code.contents.data[index] & 1 for index in range(width * width)]
    finally:
        lib.QRcode_free(code)
    size = (width + 2 * border) * scale
    rows = []
    for y in range(size):
        module_y = y // scale - border
        row = bytearray(b"\x00")  # no filter
        for x in range(size):
            module_x = x // scale - border
            dark = 0 <= module_x < width and 0 <= module_y < width and modules[module_y * width + module_x]
            row.append(0 if dark else 255)
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)  # 8-bit greyscale
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")
