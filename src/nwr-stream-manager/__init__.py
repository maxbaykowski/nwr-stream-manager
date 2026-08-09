"""NOAA Weather Radio streaming from RTL-SDR devices to Icecast."""

from importlib.metadata import PackageNotFoundError, version


try:
    __version__ = version("nwr-stream-manager")
except PackageNotFoundError:
    __version__ = "0.0.0"
