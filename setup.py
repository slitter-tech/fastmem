"""Build fastmem.

A separate setup.py alongside setuptools in pyproject.toml because the C
extension must be built, but building it is NOT required: without a
compiler the build fails softly and fastmem runs on pure Python (see
src/fastmem/backend.py).

Usage:
    python setup.py build_ext --inplace   # build the extension locally
    pip install -e .                      # editable install

Path discovery lives in tools/msvc_paths.py so that tools/build_native.py
can use it without importing setuptools, which is not installed yet on a
fresh runner.
"""

import os
import sys

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "tools"))

import msvc_paths  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# setuptools requires source paths to be relative and slash-separated: an
# absolute path or os.sep fails packaging with
# "setup script specifies an absolute path".
SRC = "src/fastmem/_fastmem.c"


class OptionalBuildExt(build_ext):
    """build_ext that does not break the install when it fails.

    The extension is an optimisation, not a requirement: a user without MSVC
    must still get a working fastmem, just on pure Python.
    """

    def run(self):
        try:
            build_ext.run(self)
        except Exception as exc:                       # noqa: BLE001
            self._warn(exc)

    def build_extension(self, ext):
        try:
            build_ext.build_extension(self, ext)
        except Exception as exc:                       # noqa: BLE001
            self._warn(exc)

    @staticmethod
    def _warn(exc):
        sys.stderr.write(
            "\n[fastmem] C extension was not built: {}\n"
            "[fastmem] The library will run on pure Python.\n"
            "[fastmem] Batch methods become 2.5-3x slower and "
            "threads>1 is unavailable.\n\n".format(exc)
        )


def _ext_modules():
    """Extension declaration. Empty list when the source is missing."""
    if not os.path.isfile(os.path.join(HERE, *SRC.split("/"))):
        return []

    lib_dir = msvc_paths.python_lib_dir(os.path.join(HERE, "build", "tmp"))
    libdirs = msvc_paths.library_dirs()
    if lib_dir:
        libdirs.insert(0, lib_dir)

    return [
        Extension(
            "fastmem._fastmem",
            sources=[SRC],
            include_dirs=msvc_paths.include_dirs(),
            library_dirs=libdirs,
            # kernel32 comes in implicitly through windows.h; an explicit
            # library list would only depend on the SDK layout.
            libraries=[],
            define_macros=[("WIN32_LEAN_AND_MEAN", None)],
            optional=True,
        )
    ]


if __name__ == "__main__":
    setup(
        cmdclass={"build_ext": OptionalBuildExt},
        ext_modules=_ext_modules(),
    )