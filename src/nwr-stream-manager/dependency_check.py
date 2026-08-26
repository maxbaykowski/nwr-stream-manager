from __future__ import annotations

import ctypes
import ctypes.util
import importlib.util
import platform
import shutil
from dataclasses import dataclass
from typing import Callable, Iterable


class DependencyCheckError(RuntimeError):
    """Raised when startup dependencies are missing or unusable."""


@dataclass(frozen=True)
class DependencyCheckResult:
    name: str
    kind: str
    required: bool
    available: bool
    detail: str = ""


@dataclass(frozen=True)
class PythonDependency:
    name: str
    module: str
    required_machines: frozenset[str] | None = None


@dataclass(frozen=True)
class NativeDependency:
    name: str
    lookup_name: str
    sonames: tuple[str, ...]
    symbols: tuple[str, ...] = ()


PYTHON_DEPENDENCIES = (
    PythonDependency("json5", "json5"),
    PythonDependency("numpy", "numpy"),
    PythonDependency("sounddevice", "sounddevice"),
    PythonDependency("pyrtlsdr", "rtlsdr"),
    PythonDependency(
        "pyrtlsdrlib",
        "pyrtlsdrlib",
        frozenset({"x86_64", "amd64"}),
    ),
    PythonDependency("soxr", "soxr"),
    PythonDependency("lameenc", "lameenc"),
    PythonDependency("aiortc", "aiortc"),
    PythonDependency("easrecorder", "easrecorder"),
)

NATIVE_DEPENDENCIES = (
    NativeDependency(
        "librtlsdr",
        "rtlsdr",
        ("librtlsdr.so.2", "librtlsdr.so.0", "librtlsdr.so"),
        (
            "rtlsdr_get_device_count",
            "rtlsdr_get_device_name",
            "rtlsdr_get_device_usb_strings",
            "rtlsdr_open",
            "rtlsdr_close",
            "rtlsdr_set_center_freq",
            "rtlsdr_set_freq_correction",
            "rtlsdr_set_tuner_gain",
            "rtlsdr_get_tuner_gains",
            "rtlsdr_set_tuner_gain_mode",
            "rtlsdr_set_sample_rate",
            "rtlsdr_reset_buffer",
            "rtlsdr_read_async",
            "rtlsdr_cancel_async",
        ),
    ),
    NativeDependency(
        "libasound",
        "asound",
        ("libasound.so.2", "libasound.so"),
        (
            "snd_card_next",
            "snd_ctl_open",
            "snd_pcm_open",
            "snd_pcm_close",
            "snd_pcm_writei",
            "snd_pcm_recover",
            "snd_strerror",
        ),
    ),
    NativeDependency(
        "libopus",
        "opus",
        ("libopus.so.0", "libopus.so", "opus.dll", "libopus.dylib"),
        (
            "opus_encoder_create",
            "opus_encoder_ctl",
            "opus_encode",
            "opus_encoder_destroy",
            "opus_strerror",
        ),
    ),
    NativeDependency(
        "libogg",
        "ogg",
        ("libogg.so.0", "libogg.so"),
        (
            "ogg_stream_init",
            "ogg_stream_packetin",
            "ogg_stream_pageout",
            "ogg_stream_flush",
            "ogg_stream_clear",
        ),
    ),
    NativeDependency(
        "libvorbis",
        "vorbis",
        ("libvorbis.so.0", "libvorbis.so"),
        (
            "vorbis_info_init",
            "vorbis_comment_init",
            "vorbis_analysis_init",
            "vorbis_analysis_headerout",
            "vorbis_analysis_buffer",
            "vorbis_analysis_wrote",
            "vorbis_analysis_blockout",
        ),
    ),
    NativeDependency(
        "libvorbisenc",
        "vorbisenc",
        ("libvorbisenc.so.2", "libvorbisenc.so"),
        ("vorbis_encode_init",),
    ),
)

EXECUTABLE_DEPENDENCIES = ("multimon-ng",)


def check_startup_dependencies() -> list[DependencyCheckResult]:
    results = gather_dependency_results()
    missing = [result for result in results if result.required and not result.available]
    if missing:
        detail = "; ".join(
            f"{result.name} ({result.kind}): {result.detail or 'not available'}"
            for result in missing
        )
        raise DependencyCheckError(f"startup dependency check failed: {detail}")
    return results


def gather_dependency_results(
    *,
    machine: str | None = None,
    find_spec: Callable[[str], object | None] = importlib.util.find_spec,
    find_library: Callable[[str], str | None] = ctypes.util.find_library,
    load_library: Callable[[str], object] = ctypes.CDLL,
    which: Callable[[str], str | None] = shutil.which,
) -> list[DependencyCheckResult]:
    machine_key = (machine or platform.machine()).lower()
    results: list[DependencyCheckResult] = []
    for dependency in PYTHON_DEPENDENCIES:
        required = _python_dependency_required(dependency, machine_key)
        available = find_spec(dependency.module) is not None
        detail = f"Python module {dependency.module}"
        if not required:
            detail += f" is not required on {machine_key or 'this architecture'}"
        elif not available:
            detail += " was not found"
        results.append(
            DependencyCheckResult(
                name=dependency.name,
                kind="python",
                required=required,
                available=available or not required,
                detail=detail,
            )
        )

    for dependency in NATIVE_DEPENDENCIES:
        results.append(
            _check_native_dependency(
                dependency,
                find_library=find_library,
                load_library=load_library,
            )
        )

    for executable in EXECUTABLE_DEPENDENCIES:
        path = which(executable)
        results.append(
            DependencyCheckResult(
                name=executable,
                kind="executable",
                required=True,
                available=path is not None,
                detail=path or f"{executable} was not found in PATH",
            )
        )

    return results


def format_dependency_summary(results: Iterable[DependencyCheckResult]) -> str:
    grouped: dict[str, list[str]] = {}
    for result in results:
        if result.available:
            grouped.setdefault(result.kind, []).append(result.name)
    return "; ".join(
        f"{kind}: {', '.join(sorted(names))}"
        for kind, names in sorted(grouped.items())
    )


def _python_dependency_required(dependency: PythonDependency, machine_key: str) -> bool:
    if dependency.required_machines is None:
        return True
    return machine_key in dependency.required_machines


def _check_native_dependency(
    dependency: NativeDependency,
    *,
    find_library: Callable[[str], str | None],
    load_library: Callable[[str], object],
) -> DependencyCheckResult:
    candidates = [find_library(dependency.lookup_name), *dependency.sonames]
    errors: list[str] = []
    for candidate in dict.fromkeys(filter(None, candidates)):
        try:
            library = load_library(candidate)
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
            continue
        missing = _missing_symbols(library, dependency.symbols)
        if missing:
            errors.append(f"{candidate}: missing symbols {', '.join(missing)}")
            continue
        return DependencyCheckResult(
            name=dependency.name,
            kind="native",
            required=True,
            available=True,
            detail=f"loaded {candidate}",
        )
    detail = "; ".join(errors) if errors else f"ctypes could not locate {dependency.name}"
    return DependencyCheckResult(
        name=dependency.name,
        kind="native",
        required=True,
        available=False,
        detail=detail,
    )


def _missing_symbols(library: object, symbols: tuple[str, ...]) -> list[str]:
    return [symbol for symbol in symbols if not hasattr(library, symbol)]
