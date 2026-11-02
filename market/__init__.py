"""Market research & decision-support package for JARVIS.

Isolated from the voice/UI codebase. Importing this package performs no I/O,
no network calls, and does not read config. Call `market.config.load_config()`
explicitly to obtain validated settings.
"""
__version__ = "0.1.0"
