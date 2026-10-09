"""
Discovery of the MSVC and Windows SDK paths, shared by setup.py and
tools/build_native.py.

Kept separate from setup.py because build_native.py has to work on a runner
where setuptools may not be installed yet - importing setup.py to reach
these functions failed there with ModuleNotFoundError: No module named
'setuptools'. Nothing in here imports anything beyond the standard library.
"""

import os
import subprocess
import sys

# Windows SDK versions seen in the wild. Listed explicitly because the SDK
# is not always in the default location (e.g. G:\Windows Kits\10) and
# vcvars does not know about it.
SDK_LIB_VERSIONS = (
    "10.0.26100.0",
    "10.0.22621.0",
    "10.0.22000.0",
    "10.0.20348.0",
    "10.0.19041.0",
)

SDK_ROOTS = (
    r"C:\Program Files (x86)\Windows Kits\10",
    r"C:\Program Files\Windows Kits\10",
    r"G:\Windows Kits\10",
    r"D:\Windows Kits\10",
)

# Architecture names, as FASTMEM_BUILD_ARCH spells them.
ARCH_X64 = "x64"
ARCH_X86 = "x86"
ARCH_ARM64 = "arm64"

# Library subdirectory names differ per target inside the MSVC toolset.
MSVC_LIB_SUBDIRS = {
    ARCH_X64: ("lib/x64", "lib/amd64_x64"),
    ARCH_X86: ("lib",),
    ARCH_ARM64: ("lib/arm64",),
}

# ... and so do the Windows SDK umbrella library directories.
SDK_LIB_SUBDIRS = {
    ARCH_X64: "x64",
    ARCH_X86: "x86",
    ARCH_ARM64: "arm64",
}


def sdk_roots():
    """Candidate Windows SDK roots, de-duplicated, order preserved."""
    roots = []
    env = os.environ.get("WindowsSdkDir")
    if env:
        roots.append(env.rstrip("\\/"))
    roots.extend(p for p in SDK_ROOTS if os.path.isdir(p))

    seen = set()
    out = []
    for root in roots:
        key = root.lower()
        if key not in seen:
            seen.add(key)
            out.append(root)
    return out


def target_arch():
    """
    Architecture being compiled for.

    Defaults to the host, but x86 and ARM64 are cross-compiled from an x64
    runner, so the library paths have to follow the target rather than the
    machine doing the compiling. Set FASTMEM_BUILD_ARCH to x86 / x64 /
    arm64.
    """
    env = os.environ.get("FASTMEM_BUILD_ARCH", "").strip().lower()
    if env in (ARCH_X86, ARCH_X64, ARCH_ARM64):
        return env
    return ARCH_X64 if sys.maxsize > 2 ** 32 else ARCH_X86


def find_msvc():
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


def _search_dirs():
    """
    Directories that may hold the interpreter for the target architecture.

    For a cross-compile the interesting interpreter is not the one running:
    cibuildwheel downloads an x86 or ARM64 CPython and puts it somewhere,
    while sysconfig still describes the x64 host. Its location is not
    documented as an environment variable, so the common candidates are all
    searched.
    """
    import sysconfig

    dirs = []
    libdir = sysconfig.get_config_var("LIBDIR")
    if libdir:
        dirs.append(libdir)
    base = sysconfig.get_config_var("prefix") or sys.base_prefix
    if base:
        dirs.append(base)
        dirs.append(os.path.join(base, "libs"))
    for var in ("PYTHONPATH", "DLLDIR", "PATH"):
        for entry in os.environ.get(var, "").split(os.pathsep):
            if entry and entry not in dirs:
                dirs.append(entry)
    return dirs


def python_lib_dir(target_dir):
    """
    Directory holding pythonXY.lib, generating it from the DLL if absent.

    Two situations need handling. Some Python builds ship no python311.lib
    (built without Py_ENABLE_SHARED) and the build then fails with LNK1104;
    the import library is generated from the DLL in that case. And for a
    cross-compile the DLL that matters belongs to the target architecture,
    which is not the interpreter running this script.

    :return: directory containing the library, or None.
    """
    tag = "python%d%d" % sys.version_info[:2]
    lib_name = tag + ".lib"
    dll_name = tag + ".dll"

    for directory in _search_dirs():
        if os.path.isfile(os.path.join(directory, lib_name)):
            return directory

    # No .lib anywhere: generate one from the target DLL, preferring the
    # host's copy and falling back to whatever the cross build provides.
    dll = None
    for directory in _search_dirs():
        candidate = os.path.join(directory, dll_name)
        if os.path.isfile(candidate):
            dll = candidate
            break
    if not dll:
        return None

    _install, msvc = find_msvc()
    if not msvc:
        return None

    host = target_arch()
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

    out_lib = os.path.join(target_dir, lib_name)
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
        machine = {"x64": "X64", "x86": "X86", "arm64": "ARM64"}[host]
        subprocess.check_call(
            [lib_exe, "/nologo", "/def:" + def_path, "/out:" + out_lib,
             "/machine:" + machine],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    return target_dir if os.path.isfile(out_lib) else None


def library_dirs():
    """Directories the linker takes kernel32 and the import library from."""
    dirs = []
    arch = target_arch()

    _install, msvc = find_msvc()
    if msvc:
        for sub in MSVC_LIB_SUBDIRS.get(arch, MSVC_LIB_SUBDIRS[ARCH_X64]):
            path = os.path.join(msvc, *sub.split("/"))
            if os.path.isdir(path):
                dirs.append(path)

    sdk_arch = SDK_LIB_SUBDIRS.get(arch, "x64")
    for root in sdk_roots():
        for ver in SDK_LIB_VERSIONS:
            found = False
            # Both um (kernel32 and friends) and ucrt (the CRT import
            # library) are needed: a build that links only um fails with
            # "cannot open file libucrt.lib".
            for sub in ("um", "ucrt"):
                path = os.path.join(root, "Lib", ver, sub, sdk_arch)
                if os.path.isdir(path):
                    dirs.append(path)
                    found = True
            if found:
                break
    return dirs


def include_dirs():
    """Include directories: Python.h and the Windows SDK."""
    import sysconfig

    dirs = []
    for key in ("include", "platinclude"):
        path = sysconfig.get_paths().get(key)
        if path and os.path.isdir(path) and path not in dirs:
            dirs.append(path)

    for root in sdk_roots():
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