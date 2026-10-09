"""fastmem — fast memory reading for Windows processes.

Simple case::

    from fastmem import Process

    with Process(pid) as p:
        data = p.read(address, size)
"""

from . import backend
from .exceptions import (
    FastMemError,
    ProcessClosedError,
    ProcessOpenError,
    ProcessTerminatedError,
    ReadMemoryError,
)
from .process import SCRATCH_SIZE, Process, Region

# Read the version from package metadata so it cannot drift from
# pyproject.toml. The fallback covers running straight from a checkout
# without installing.
try:
    from importlib.metadata import PackageNotFoundError, version

    __version__ = version("fastmem")
except (ImportError, PackageNotFoundError):       # pragma: no cover
    __version__ = "0.1.0"

__all__ = [
    "Process",
    "Region",
    "backend",
    "FastMemError",
    "ProcessOpenError",
    "ReadMemoryError",
    "ProcessClosedError",
    "ProcessTerminatedError",
    "SCRATCH_SIZE",
    "__version__",
]
