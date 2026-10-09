"""Pre-release checklist.

Checks the things that are cheap to verify locally and expensive to
discover after the tag is pushed: version agreement, the extension actually
building, the native library building, and no build artefacts staged for
commit.

Run before tagging:
    python tools/release_check.py
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


def section(title):
    print("\n=== {} ===".format(title))


def ok(message):
    print("  [ok] {}".format(message))


def fail(message):
    print("  [!!] {}".format(message))
    return 1


def check_version():
    section("version")
    try:
        import tomllib
    except ImportError:                       # Python 3.9 and 3.10
        import tomli as tomllib

    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        data = tomllib.load(fh)
    version = data["project"]["version"]
    ok("pyproject version is {}".format(version))

    problems = 0

    # The changelog must have a section for this version, because the
    # release job extracts it for the GitHub Release body.
    changelog = open(os.path.join(ROOT, "CHANGELOG.md"),
                     encoding="utf-8").read()
    if "## [{}]".format(version) not in changelog:
        problems += fail("CHANGELOG.md has no section for {}".format(version))
    else:
        ok("CHANGELOG.md has a section for {}".format(version))

    if "License-Expression" not in data.get("project", {}).get("license", "") \
            and not isinstance(data["project"].get("license"), str):
        problems += fail("project.license is not a plain string")

    return problems, version


def check_extension():
    section("Python extension")
    result = subprocess.run(
        [sys.executable, "setup.py", "build_ext", "--inplace"],
        cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        return fail("build_ext failed:\n{}".format(result.stderr[-800:]))

    from fastmem import backend
    if not backend.HAVE_C:
        return fail("extension did not load")
    ok("extension builds and loads ({})".format(backend.backend_name()))
    return 0


def check_native():
    section("C and C++")
    result = subprocess.run(
        [sys.executable, os.path.join("tools", "build_native.py"), "--example"],
        cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        return fail("native build failed:\n{}".format(result.stderr[-800:]))

    for name in ("fastmem.dll", "fastmem.lib", "quickstart.exe"):
        path = os.path.join(ROOT, "build", "native", name)
        if not os.path.isfile(path):
            return fail("{} was not produced".format(name))
    ok("C library, import library and C++ example all built")

    exe = os.path.join(ROOT, "build", "native", "quickstart.exe")
    run = subprocess.run([exe], capture_output=True, text=True,
                         env=dict(os.environ,
                                  PATH=os.pathsep.join([
                                      os.path.join(ROOT, "build", "native"),
                                      os.environ.get("PATH", "")])))
    if run.returncode != 0:
        return fail("quickstart.exe exited {}:\n{}".format(
            run.returncode, run.stdout[-500:]))
    for needle in ("marker located", "grouping speedup"):
        if needle not in run.stdout:
            return fail("quickstart.exe output missing {!r}".format(needle))
    ok("quickstart.exe runs and locates its marker")
    return 0


def check_tests():
    section("functional tests")
    result = subprocess.run(
        [sys.executable, "-m", "tests.test_fastmem"],
        cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        return fail("tests failed:\n{}".format(result.stdout[-800:]))
    ok("all test groups passed")
    return 0


def check_git_clean():
    section("git")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                            capture_output=True, text=True).stdout
    staged = [line for line in status.splitlines()
              if line[:2] in (" M", "M ", "A ", " D", "D ", "R ", "??")
              and not line.startswith("?? build")]
    if staged:
        return fail("uncommitted changes:\n  " + "\n  ".join(staged))
    ok("working tree is clean")

    tags = subprocess.run(["git", "tag", "-l", "v*"], cwd=ROOT,
                          capture_output=True, text=True).stdout.split()
    return tags, 0


def main():
    problems, version = check_version()
    problems += check_tests()
    problems += check_extension()
    problems += check_native()
    problems += check_git_clean()

    section("result")
    if problems:
        print("  {} problem(s) - not ready to tag".format(problems))
        return 1
    print("  ready to tag v{}".format(version))
    print("  git tag v{}\n  git push origin v{}\n".format(version, version))
    return 0


if __name__ == "__main__":
    sys.exit(main())