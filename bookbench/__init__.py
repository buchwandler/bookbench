"""BookBench: end-to-end validation harness for the Buchwandler speech pipeline."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("bookbench")
except PackageNotFoundError:  # pragma: no cover - source checkout fallback
    __version__ = "0.1.dev0"
