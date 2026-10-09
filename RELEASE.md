# Publishing to PyPI

## One-time setup (do this by hand)

Publishing uses **Trusted Publishing** (OIDC), so no token has to be kept
in the repository secrets.

1. Register the project at <https://pypi.org/project/fastmem/>.
   If the name is taken, `name` in `pyproject.toml` has to change.

2. Open the project -> **Manage -> Publishing -> Add a new publisher**.

3. Fill in:

   | Field | Value |
   |---|---|
   | PyPI project name | `fastmem` |
   | Owner | `slitter-tech` |
   | Repository name | `fastmem` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

   The environment name must match the `environment` block in
   `.github/workflows/publish.yml`. When creating the environment on GitHub,
   tick **Prevent self-review** so a second person does the approval.

## Release

```bash
git tag v0.1.0
git push origin v0.1.0
```

The tag builds the wheels and the sdist, publishes them to PyPI and creates
the GitHub Release with the matching CHANGELOG section. The tag is checked
against the version in `pyproject.toml`, so a mismatch stops the publish
rather than uploading the wrong number.

## What gets built

| Artefact | Contents |
|---|---|
| `fastmem-0.1.0-cp3X-cp3X-win_amd64.whl` | x64, Python 3.9-3.13 |
| `fastmem-0.1.0-cp3X-cp3X-win32.whl` | x86, Python 3.9-3.13 |
| `fastmem-0.1.0-cp3X-cp3X-win_arm64.whl` | ARM64, Python 3.9-3.13 |
| `fastmem-0.1.0.tar.gz` | sources; the extension builds on install |

Eighteen wheels: three architectures times six interpreter minors. x86 and
ARM64 are cross-compiled from the x64 runner, which works because
`setup.py` reads `FASTMEM_BUILD_ARCH` and picks the matching MSVC and SDK
library directories.

The sdist is built in one matrix leg only, otherwise all three would
upload the same filename.

Wheels can also be built without publishing: **Actions -> CI -> Build
wheels -> Run workflow**. The native C job is
**Actions -> CI -> C and C++ build**.

## Verifying the package

```bash
pip install fastmem
python -c "from fastmem import backend; print(backend.backend_name())"
```

`c-extension` is expected everywhere a wheel exists. `python-ctypes` means
no wheel matched and pip fell back to the sdist: check the architecture and
Python version, and that a compiler was available for the fallback path.

No compiler is needed for a normal install. Wheels cover every supported
interpreter and architecture; the sdist is only reached when pip has to
build from source, and the library works on pure Python even then.

## Notes

- CI runs `test-windows`, `Build wheels`, `C and C++ build` and
  `Install from sdist` on every push, so a broken wheel configuration
  surfaces on the branch rather than at release time.
- A CI step fails the build if any wheel ships without the compiled
  extension. The extension is optional by design, so that regression is
  otherwise silent: the wheel installs everywhere and is just slow.
- Adding a platform means adding a row to the matrices in `ci.yml` and
  `publish.yml`, plus a `FASTMEM_BUILD_ARCH` value `setup.py` understands.
  `pythonXY.lib` is generated from the DLL when a Python build ships none.