"""Builds NWR Stream Manager's native library during installation. Everything else about the
package is in pyproject.toml.

It's plain C, loaded through ctypes rather than imported, so it uses no Python headers;
setuptools' extension build is just the way to compile it on install.
"""

import importlib.util
from pathlib import Path

from setuptools import Extension, setup

# The same compiler and linker flags as a source checkout's build, from one place.
_spec = importlib.util.spec_from_file_location("_nwr_native_build", Path(__file__).parent / "src/nwr-stream-manager/native.py")
_native = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_native)

setup(
    ext_modules=[
        Extension(
            "nwr_stream_manager._nwr_native",
            sources=["src/nwr-stream-manager/native/nwr_native.c"],
            extra_compile_args=_native.COMPILE_FLAGS,
            extra_link_args=_native.LINK_FLAGS,
        )
    ],
)
