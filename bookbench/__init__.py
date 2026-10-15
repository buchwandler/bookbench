"""BookBench: end-to-end validation harness for the Buchwandler speech pipeline."""

try:
    from importlib.metadata import version

    __version__ = version("bookbench")
except Exception:  # pragma: no cover - source checkout fallback
    __version__ = "0.1.dev0"
