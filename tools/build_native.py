"""Build the standalone C library and the C++ example.

Invokes cl.exe with the include and library paths discovered by setup.py,
because this machine has no vcvars batch file. Used to verify the C build
locally; CI uses the same setup.py paths through cibuildwheel-style steps.

Usage:
    python tools/build_native.py [--static] [--example]
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "build", "native")


def load_paths():
    """Import msvc_paths from the tools directory next to this file."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import msvc_paths
    return msvc_paths


def tool_paths(msvc, arch="x64"):
    """cl.exe, link.exe and lib.exe for the given host architecture."""
    bin_dir = os.path.join(msvc, "bin", "Hostx64", arch)
    if not os.path.isdir(bin_dir):
        bin_dir = os.path.join(msvc, "bin", arch)
    return bin_dir


def msvc_include_dirs(msvc):
    """The toolset's own CRT headers.

    setup.py does not need these: the Python extension is built by
    distutils, which runs inside an environment that already has them. This
    script drives cl.exe directly because there is no vcvars batch file
    here, so it has to supply them itself - without them windows.h fails to
    find vcruntime.h.
    """
    dirs = []
    for sub in ("include",):
        path = os.path.join(msvc, *sub.split("/"))
        if os.path.isdir(path):
            dirs.append(path)
    return dirs


def main():
    args = sys.argv[1:]
    static = "--static" in args
    with_example = "--example" in args

    paths = load_paths()
    _install, msvc = paths.find_msvc()
    if not msvc:
        print("no MSVC toolset found", file=sys.stderr)
        return 1

    arch = paths.target_arch()
    bin_dir = tool_paths(msvc, arch)
    cl = os.path.join(bin_dir, "cl.exe")
    if not os.path.isfile(cl):
        print("cl.exe not found in {}".format(bin_dir), file=sys.stderr)
        return 1

    os.makedirs(OUT, exist_ok=True)
    includes = paths.include_dirs() + msvc_include_dirs(msvc)
    libdirs = paths.library_dirs()

    # MSVC's own CRT import library. The SDK paths cover kernel32 and the
    # ucrt forwarders but not the vcruntime the CRT links against.
    crt_lib = os.path.join(msvc, "lib", "x64") if arch == "x64" else None
    if crt_lib and os.path.isdir(crt_lib) and crt_lib not in libdirs:
        libdirs.append(crt_lib)

    common = [
        "/nologo",
        "/O2",                      # optimised
        "/W4",                      # high warnings
        "/WX",                      # warnings are errors
        "/wd4996",                  # "deprecated or CRT header" noise
        "/DWIN32_LEAN_AND_MEAN",
        "/D_CRT_SECURE_NO_WARNINGS",
        "/LD",                      # build a DLL
    ]
    for path in includes:
        common.append("/I{}".format(path))

    src = os.path.join(ROOT, "csrc", "fastmem.c")
    dll = os.path.join(OUT, "fastmem.dll")
    lib = os.path.join(OUT, "fastmem.lib")
    obj = os.path.join(OUT, "fastmem.obj")

    cmd = [cl] + common + ["/Fo{}".format(obj), src,
                           "/Fe{}".format(dll)]
    if not static:
        cmd.append("/link")
        cmd.append("/DEF:{}".format(os.path.join(ROOT, "csrc", "fastmem.def")))
        for d in libdirs:
            cmd.append("/LIBPATH:{}".format(d))

    print("building {} ({})".format(os.path.basename(dll), arch))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    if with_example:
        return build_example(cl, includes, libdirs)

    return 0


def build_example(cl, includes, libdirs):
    """Compile and link the C++ header-only example against the DLL."""
    src = os.path.join(ROOT, "cpp", "examples", "quickstart.cpp")
    exe = os.path.join(OUT, "quickstart.exe")

    cmd = [cl, "/nologo", "/std:c++17", "/EHsc", "/O2", "/W4", "/WX",
           "/DWIN32_LEAN_AND_MEAN", "/MD"]
    for path in includes:
        cmd.append("/I{}".format(path))
    cmd.append("/I{}".format(os.path.join(ROOT, "cpp", "include")))
    # The wrapper includes the C header by bare name, so a consumer needs
    # one include path rather than two.
    cmd.append("/I{}".format(os.path.join(ROOT, "csrc")))
    cmd.append("/Fe{}".format(exe))
    cmd.append(src)
    cmd.append("/link")
    # /LIBPATH belongs to the linker. Passed to cl it yields
    # "D9002: ignoring unknown option" once per path.
    for d in libdirs:
        cmd.append("/LIBPATH:{}".format(d))
    cmd.append(os.path.join(OUT, "fastmem.lib"))
    cmd.append("/OUT:{}".format(exe))

    print("building quickstart.exe")
    return subprocess.run(cmd, cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())