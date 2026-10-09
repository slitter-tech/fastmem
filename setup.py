"""Build fastmem.

A separate setup.py alongside setuptools in pyproject.toml because the C
extension must be built, but building it is NOT required: without a
compiler the build fails softly and fastmem runs on pure Python (see
src/fastmem/backend.py).

Usage:
    python setup.py build_ext --inplace   # build the extension locally
    pip install -e .                      # editable install
"""

import os
import subprocess
import sys

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

HERE = os.path.dirname(os.path.abspath(__file__))

# setuptools requires source paths to be relative and slash-separated: an
# absolute path or os.sep fails packaging with
# "setup script specifies an absolute path".
SRC = "src/fastmem/_fastmem.c"

# Windows SDK versions seen in the wild. Listed explicitly because the SDK
# is not always in the default location (e.g. G:\Windows Kits\10) and
# vcvars does not know about it.
_SDK_LIB_VERSIONS = (
    "10.0.26100.0",
    "10.0.22621.0",
    "10.0.22000.0",
    "10.0.20348.0",
    "10.0.19041.0",
)

_SDK_ROOTS = (
    r"C:\Program Files (x86)\Windows Kits\10",
    r"C:\Program Files\Windows Kits\10",
    r"G:\Windows Kits\10",
    r"D:\Windows Kits\10",
)


def _sdk_roots():
    """Candidate Windows SDK roots, de-duplicated, order preserved."""
    roots = []
    env = os.environ.get("WindowsSdkDir")
    if env:
        roots.append(env.rstrip("\\/"))
    roots.extend(p for p in _SDK_ROOTS if os.path.isdir(p))

    seen = set()
    out = []
    for root in roots:
        key = root.lower()
        if key not in seen:
            seen.add(key)
            out.append(root)
    return out


def _find_msvc():
    """Locate MSVC through vswhere. Returns (install_path, tools_dir)."""
    program_files = os.environ.get("ProgramFiles(x86)",
                                   r"C:\Program Files (x86)")
    vswhere = os.path.join(
        program_files, "Microsoft Visual Studio", "Installer", "vswhere.exe"
    )
    install = None
    if os.path.isfile(vswhere):
        try:
            out = subprocess.check_output(
                [vswhere, "-latest", "-products", "*", "-requires",
                 "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                 "-property", "installationPath"],
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", "replace").strip()
            install = out or None
        except (OSError, subprocess.CalledProcessError):
            install = None

    candidates = []
    if install:
        candidates.append(os.path.join(install, "VC", "Tools", "MSVC"))
    candidates.append(
        os.path.join(program_files, "Microsoft Visual Studio", "2022",
                     "BuildTools", "VC", "Tools", "MSVC")
    )
    candidates.append(
        r"C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Tools\MSVC"
    )

    for tools_root in candidates:
        if not os.path.isdir(tools_root):
            continue
        versions = sorted(
            (d for d in os.listdir(tools_root)
             if os.path.isdir(os.path.join(tools_root, d))),
            reverse=True,
        )
        if versions:
            return install, os.path.join(tools_root, versions[0])
    return install, None


def _python_lib_dir(target_dir):
    """Directory holding pythonXY.lib, generating it from the DLL if absent.

    Some Python builds ship no python311.lib (built without
    Py_ENABLE_SHARED), and the build then fails with LNK1104. In that case
    the export table of python311.dll is parsed with dumpbin and turned into
    an import library with lib.exe.

    :return: directory containing the library, or None.
    """
    import sysconfig

    tag = "python%d%d" % sys.version_info[:2]

    libdir = sysconfig.get_config_var("LIBDIR")
    if libdir and os.path.isdir(libdir) and os.path.isfile(
        os.path.join(libdir, tag + ".lib")
    ):
        return libdir

    base = sysconfig.get_config_var("prefix") or sys.base_prefix
    for cand in (base, os.path.join(base, "libs")):
        if cand and os.path.isfile(os.path.join(cand, tag + ".lib")):
            return cand

    # No .lib: generate one from the DLL.
    if not base:
        return None
    dll = os.path.join(base, tag + ".dll")
    if not os.path.isfile(dll):
        return None

    _install, msvc = _find_msvc()
    if not msvc:
        return None

    host = "x64" if sys.maxsize > 2 ** 32 else "x86"
    bin_dir = os.path.join(msvc, "bin", "Hostx64", host)
    if not os.path.isdir(bin_dir):
        bin_dir = os.path.join(msvc, "bin", host)
    dumpbin = os.path.join(bin_dir, "dumpbin.exe")
    lib_exe = os.path.join(bin_dir, "lib.exe")
    if not (os.path.isfile(dumpbin) and os.path.isfile(lib_exe)):
        return None

    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError:
        return None

    out_lib = os.path.join(target_dir, tag + ".lib")
    if os.path.isfile(out_lib):
        return target_dir

    try:
        raw = subprocess.check_output(
            [dumpbin, "/nologo", "/exports", dll],
            stderr=subprocess.DEVNULL,
        ).decode("ascii", "replace")
    except (OSError, subprocess.CalledProcessError):
        return None

    # dumpbin format: "ordinal hint RVA name" - four leading fields.
    names = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0].isdigit():
            name = parts[3]
            if name.startswith("Py") or name.startswith("_Py"):
                names.append(name)
    if not names:
        return None

    names.sort()
    def_path = os.path.join(target_dir, tag + ".def")
    try:
        with open(def_path, "w", encoding="ascii") as fh:
            fh.write("LIBRARY {}\nEXPORTS\n".format(os.path.basename(dll)))
            fh.write("\n".join(names))
            fh.write("\n")
        machine = "X64" if host == "x64" else "X86"
        subprocess.check_call(
            [lib_exe, "/nologo", "/def:" + def_path, "/out:" + out_lib,
             "/machine:" + machine],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    return target_dir if os.path.isfile(out_lib) else None


def _library_dirs():
    """Directories the linker takes kernel32 and the import library from."""
    dirs = []

    _install, msvc = _find_msvc()
    if msvc:
        for sub in ("lib/x64", "lib/x86", "lib/amd64_x64"):
            path = os.path.join(msvc, *sub.split("/"))
            if os.path.isdir(path):
                dirs.append(path)

    for root in _sdk_roots():
        for ver in _SDK_LIB_VERSIONS:
            for sub in ("um", "ucrt"):
                path = os.path.join(root, "Lib", ver, sub, "x64")
                if os.path.isdir(path):
                    dirs.append(path)
                    break
            else:
                continue
            break
    return dirs


def _include_dirs():
    """Include directories: Python.h and the Windows SDK."""
    import sysconfig

    dirs = []
    for key in ("include", "platinclude"):
        path = sysconfig.get_paths().get(key)
        if path and os.path.isdir(path) and path not in dirs:
            dirs.append(path)

    for root in _sdk_roots():
        base = os.path.join(root, "Include")
        if not os.path.isdir(base):
            continue
        versions = sorted(
            (d for d in os.listdir(base)
             if os.path.isdir(os.path.join(base, d))),
            reverse=True,
        )
        for ver in versions:
            if not os.path.isdir(os.path.join(base, ver, "ucrt")):
                continue
            for sub in ("ucrt", "um", "shared", "winrt"):
                path = os.path.join(base, ver, sub)
                if os.path.isdir(path):
                    dirs.append(path)
            break
    return dirs


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

    lib_dir = _python_lib_dir(os.path.join(HERE, "build", "tmp"))
    libdirs = _library_dirs()
    if lib_dir:
        libdirs.insert(0, lib_dir)

    return [
        Extension(
            "fastmem._fastmem",
            sources=[SRC],
            include_dirs=_include_dirs(),
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
