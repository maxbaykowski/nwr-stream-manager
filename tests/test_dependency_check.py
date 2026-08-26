from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO_ROOT / "src" / "nwr-stream-manager"


def load_dependency_check_module():
    package = types.ModuleType("nwr_stream_manager")
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    package.__version__ = "0.0.0"  # type: ignore[attr-defined]
    sys.modules.setdefault("nwr_stream_manager", package)
    return importlib.import_module("nwr_stream_manager.dependency_check")


class FakeLibrary:
    def __init__(self, symbols: set[str]) -> None:
        self._symbols = symbols

    def __getattr__(self, name: str):
        if name in self._symbols:
            return object()
        raise AttributeError(name)


class DependencyCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dependency_check = load_dependency_check_module()

    def all_python_modules_present(self, module: str) -> object:
        return object()

    def all_native_libraries_present(self, dependency_name: str) -> str:
        return f"lib{dependency_name}.so"

    def load_complete_library(self, candidate: str) -> FakeLibrary:
        symbols = set()
        for dependency in self.dependency_check.NATIVE_DEPENDENCIES:
            symbols.update(dependency.symbols)
        return FakeLibrary(symbols)

    def test_all_dependencies_available_passes(self) -> None:
        results = self.dependency_check.gather_dependency_results(
            machine="x86_64",
            find_spec=self.all_python_modules_present,
            find_library=self.all_native_libraries_present,
            load_library=self.load_complete_library,
            which=lambda name: f"/usr/bin/{name}",
        )

        self.assertTrue(all(result.available for result in results))
        self.assertIn("python:", self.dependency_check.format_dependency_summary(results))
        self.assertIn("native:", self.dependency_check.format_dependency_summary(results))

    def test_pyrtlsdrlib_is_not_required_on_arm(self) -> None:
        def find_spec(module: str) -> object | None:
            if module == "pyrtlsdrlib":
                return None
            return object()

        results = self.dependency_check.gather_dependency_results(
            machine="aarch64",
            find_spec=find_spec,
            find_library=self.all_native_libraries_present,
            load_library=self.load_complete_library,
            which=lambda name: f"/usr/bin/{name}",
        )
        pyrtlsdrlib = next(result for result in results if result.name == "pyrtlsdrlib")

        self.assertFalse(pyrtlsdrlib.required)
        self.assertTrue(pyrtlsdrlib.available)

    def test_missing_required_python_dependency_fails_startup_check(self) -> None:
        def find_spec(module: str) -> object | None:
            if module == "numpy":
                return None
            return object()

        with self.assertRaises(self.dependency_check.DependencyCheckError) as context:
            results = self.dependency_check.gather_dependency_results(
                machine="x86_64",
                find_spec=find_spec,
                find_library=self.all_native_libraries_present,
                load_library=self.load_complete_library,
                which=lambda name: f"/usr/bin/{name}",
            )
            missing = [result for result in results if result.required and not result.available]
            if missing:
                raise self.dependency_check.DependencyCheckError(
                    "; ".join(result.name for result in missing)
                )

        self.assertIn("numpy", str(context.exception))

    def test_missing_native_symbol_is_reported(self) -> None:
        required_symbols = {
            symbol
            for dependency in self.dependency_check.NATIVE_DEPENDENCIES
            for symbol in dependency.symbols
        }
        required_symbols.discard("opus_encode")

        results = self.dependency_check.gather_dependency_results(
            machine="x86_64",
            find_spec=self.all_python_modules_present,
            find_library=self.all_native_libraries_present,
            load_library=lambda _candidate: FakeLibrary(required_symbols),
            which=lambda name: f"/usr/bin/{name}",
        )
        libopus = next(result for result in results if result.name == "libopus")

        self.assertFalse(libopus.available)
        self.assertIn("opus_encode", libopus.detail)

    def test_missing_multimon_is_reported(self) -> None:
        results = self.dependency_check.gather_dependency_results(
            machine="x86_64",
            find_spec=self.all_python_modules_present,
            find_library=self.all_native_libraries_present,
            load_library=self.load_complete_library,
            which=lambda _name: None,
        )
        multimon = next(result for result in results if result.name == "multimon-ng")

        self.assertFalse(multimon.available)
        self.assertIn("PATH", multimon.detail)


if __name__ == "__main__":
    unittest.main()
