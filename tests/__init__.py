"""Tests and benchmarks for fastmem.

Usage:
    python -m tests.test_fastmem     # functional tests
    python -m tests.benchmarks       # benchmarks (own process)
    python -m tests.benchmarks --foreign
"""

import sys


def _force_utf8_stdout():
    """Switch stdout/stderr to UTF-8 with replacement.

    Tests print non-ASCII text while the Windows console defaults to
    cp1252/cp866, which turns a run into UnicodeEncodeError before the
    first assertion - the code itself is fine. errors="replace" means even
    under cp1252 the run cannot die on an unsupported character.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # stream already closed, or does not support reconfiguration
            pass


_force_utf8_stdout()
