"""Single source of truth for the project version.

Read from pyproject.toml without tomllib: the build has to work on Python
3.9, where tomllib does not exist, and setuptools has not been imported at
that point in setup.py. The file is one line of simple key = "value" and
the first ``version`` key under ``[project]`` is the one that counts, so a
line scan is enough and cannot pull in a dependency the sdist would need.

Both build systems use this: setup.py passes it to the extension as
FASTMEM_VERSION, build_native.py does the same for the C library. That is
what keeps ``fastmem.__version__``, the extension's ``__version__`` and
``fastmem_version()`` from drifting apart across releases.
"""

import os

FALLBACK = "0.0.0-unknown"


def read_version(root):
    """Version string from ``<root>/pyproject.toml``, or :data:`FALLBACK`."""
    path = os.path.join(root, "pyproject.toml")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            in_project = False
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    in_project = stripped == "[project]"
                    continue
                if in_project and stripped.startswith("version"):
                    key, sep, value = stripped.partition("=")
                    if not sep or key.strip() != "version":
                        continue
                    return value.strip().strip('"').strip("'") or FALLBACK
    except OSError:
        pass
    return FALLBACK
