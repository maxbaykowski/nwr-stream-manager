"""Where NWR Stream Manager keeps its files, saving them safely, and checking the values read from them."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .constants import (
    ALIAS_FILTER_STRENGTH_DEFAULT,
    SDR_SOURCE_LOCAL,
    SDR_SOURCE_REMOTE,
    STATE_DIRECTORY_NAME,
    STATE_FILE_NAME,
)
from .iq_pipeline import validate_alias_filter_strength
from .models import RtlControlSettings
from .remote_sdr import REMOTE_IQ_BUFFER_SECONDS, REMOTE_IQ_MAX_SETTING_SECONDS, REMOTE_IQ_MIN_BUFFER_SECONDS
from .rtl import DEFAULT_RTL_SAMPLE_RATE, validate_ppm_correction


LOG = logging.getLogger(__name__)


def default_state_path() -> Path:
    systemd_state_directory = systemd_directory_path("STATE_DIRECTORY")
    if systemd_state_directory is not None:
        base = systemd_state_directory
    elif os.environ.get("XDG_STATE_HOME"):
        base = Path(os.environ["XDG_STATE_HOME"]).expanduser() / STATE_DIRECTORY_NAME
    else:
        base = Path.home() / ".local" / "state" / STATE_DIRECTORY_NAME
    return base / STATE_FILE_NAME


# Files and folders NWR Stream Manager creates can be read and changed by the account it
# runs as, and nobody else: they hold password hashes, notification secrets and recordings.
OWNER_ONLY_UMASK = 0o077


def make_owner_only(paths: list[Path], *, folder: Path | None = None) -> None:
    """Makes these files, and folders with everything in them, readable and writable by their owner only.

    For files saved before NWR Stream Manager made them that way. folder is made
    private on its own, without what's inside it. Links are left alone, and so is
    anything this account doesn't own.
    """
    if folder is not None and folder.is_dir() and not folder.is_symlink():
        _owner_only(folder, 0o700)
    for path in paths:
        if path.is_symlink() or not path.exists():
            continue
        if path.is_dir():
            for directory, folders, files in os.walk(path):
                _owner_only(Path(directory), 0o700)
                for name in files:
                    _owner_only(Path(directory) / name, 0o600)
                folders[:] = [name for name in folders if not (Path(directory) / name).is_symlink()]
        else:
            _owner_only(path, 0o600)


def _owner_only(path: Path, needed: int) -> None:
    try:
        status = path.lstat()
        if status.st_uid != os.geteuid() or path.is_symlink():
            return
        mode = status.st_mode & 0o7777
        private = (mode & 0o700) | needed
        if private != mode:
            os.chmod(path, private)
    except OSError as exc:
        LOG.warning("couldn't make %s private: %s", path, exc)


def systemd_directory_path(variable: str) -> Path | None:
    value = os.environ.get(variable, "").strip()
    if not value:
        return None
    return Path(value.split(":", 1)[0]).expanduser()


def validate_remote_buffer_seconds(value: Any) -> float:
    try:
        seconds = round(float(value), 2)
    except (TypeError, ValueError) as exc:
        raise ValueError("The network buffer must be a number of seconds.") from exc
    if not REMOTE_IQ_MIN_BUFFER_SECONDS <= seconds <= REMOTE_IQ_MAX_SETTING_SECONDS:
        raise ValueError(
            f"The network buffer must be between {REMOTE_IQ_MIN_BUFFER_SECONDS:g} and {REMOTE_IQ_MAX_SETTING_SECONDS:g} seconds."
        )
    return seconds


def validate_sdr_source_mode(value: Any) -> str:
    mode = str(value or SDR_SOURCE_LOCAL).strip().lower()
    if mode not in {SDR_SOURCE_LOCAL, SDR_SOURCE_REMOTE}:
        raise ValueError("SDR source must be local or remote")
    return mode


def load_settings(path: Path) -> RtlControlSettings:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return RtlControlSettings()
    except Exception as exc:
        LOG.warning("failed to load RTL control settings from %s: %s", path, exc)
        return RtlControlSettings()
    if not isinstance(raw, dict):
        return RtlControlSettings()
    try:
        return RtlControlSettings(
            serial=str(raw.get("serial", "")).strip(),
            sample_rate=DEFAULT_RTL_SAMPLE_RATE,
            gain=None if raw.get("gain") is None else float(raw["gain"]),
            ppm_correction=validate_ppm_correction(int(raw.get("ppm_correction", 0))),
            bias_tee=bool(raw.get("bias_tee", False)),
            alias_filter_strength=validate_alias_filter_strength(
                raw.get("alias_filter_strength", ALIAS_FILTER_STRENGTH_DEFAULT)
            ),
            notify_sdr_failures=bool(raw.get("notify_sdr_failures", False)),
            remote_access_enabled=bool(raw.get("remote_access_enabled", False)),
            source_mode=validate_sdr_source_mode(raw.get("source_mode", SDR_SOURCE_LOCAL)),
            remote_host_id=str(raw.get("remote_host_id", "")).strip(),
            remote_buffer_seconds=validate_remote_buffer_seconds(raw.get("remote_buffer_seconds", REMOTE_IQ_BUFFER_SECONDS)),
        )
    except Exception as exc:
        LOG.warning("RTL control settings in %s are invalid: %s", path, exc)
        return RtlControlSettings()


def save_settings(path: Path, settings: RtlControlSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def whole_seconds(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a whole number of seconds")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a whole number of seconds") from exc
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError(f"{label} must be a whole number of seconds")
    return int(number)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as fp:
        fp.write(data)
        temp_path = Path(fp.name)
    try:
        temp_path.replace(path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def load_soundcard_name_cache(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        LOG.warning("failed to load sound card device name cache from %s: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    names = raw.get("soundcards", raw)
    if not isinstance(names, dict):
        return {}
    loaded: dict[str, str] = {}
    for stable_id, label in names.items():
        stable_id_text = str(stable_id).strip()
        label_text = str(label).strip()
        if stable_id_text and label_text:
            loaded[stable_id_text] = label_text
    return loaded


def save_soundcard_name_cache(path: Path, names: dict[str, str]) -> None:
    clean = {
        str(stable_id).strip(): str(label).strip()
        for stable_id, label in names.items()
        if str(stable_id).strip() and str(label).strip()
    }
    atomic_write_json(path, {"soundcards": clean})


def http_header_filename(value: str) -> str:
    return re.sub(r'[^A-Za-z0-9._ -]+', "_", value).replace('"', "_")


def sanitize_path_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return sanitized or "stream"
