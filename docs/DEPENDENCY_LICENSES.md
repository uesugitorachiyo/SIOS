# Resolved production dependency licenses

**Inherited AIOS-Public reference only.** This is not the license inventory
for the newly resolved SIOS Basic Memory 0.23.2 environment.

This inventory covers the `uv.lock` resolution for AIOS Core 0.1.0a1 with the `agent`, `backup`, and `verification` extras, across its supported resolution markers, as checked on 2026-10-13. It includes all 34 third-party packages in that lock; `pywin32` applies only on Windows. The default installation requires only `PyYAML` and `tzdata`. Optional extras add their dependency closures. Build tools, the Python standard library, and separately installed Basic Memory are outside this lock inventory.

The license column reports each exact version's installed distribution metadata (`License-Expression` where present, otherwise `License`) and its shipped license files. The original macOS build-environment review covered the prior lock; the updated `jsonschema`, `rfc3339-validator`, and `six` entries were checked in the isolated central-completion validation environment against their installed metadata and license files. For Windows-only `pywin32`, the 312 CPython 3.11 Windows x64 wheel from PyPI was inspected directly. The package links lead to the corresponding upstream release pages. These labels describe the dependencies, not the license of AIOS Core's own MIT-licensed source.

| Package | Locked version | License declaration / component detail |
|---|---:|---|
| [annotated-types](https://pypi.org/project/annotated-types/0.8.0/) | 0.8.0 | MIT |
| [anyio](https://pypi.org/project/anyio/4.15.1/) | 4.15.1 | MIT |
| [attrs](https://pypi.org/project/attrs/26.1.0/) | 26.1.0 | MIT |
| [certifi](https://pypi.org/project/certifi/2026.7.22/) | 2026.7.22 | MPL-2.0 |
| [cffi](https://pypi.org/project/cffi/2.1.1/) | 2.1.1 | MIT-0 |
| [click](https://pypi.org/project/click/8.5.0/) | 8.5.0 | BSD-3-Clause |
| [cryptography](https://pypi.org/project/cryptography/50.0.2/) | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| [h11](https://pypi.org/project/h11/0.16.0/) | 0.16.0 | MIT |
| [httpcore](https://pypi.org/project/httpcore/1.0.9/) | 1.0.9 | BSD-3-Clause |
| [httpx](https://pypi.org/project/httpx/0.28.1/) | 0.28.1 | BSD-3-Clause |
| [httpx-sse](https://pypi.org/project/httpx-sse/0.4.3/) | 0.4.3 | MIT |
| [idna](https://pypi.org/project/idna/3.20/) | 3.20 | BSD-3-Clause |
| [jsonschema](https://pypi.org/project/jsonschema/4.25.1/) | 4.25.1 | MIT |
| [jsonschema-specifications](https://pypi.org/project/jsonschema-specifications/2025.9.1/) | 2025.9.1 | MIT |
| [mcp](https://pypi.org/project/mcp/1.30.0/) | 1.30.0 | MIT |
| [pycparser](https://pypi.org/project/pycparser/3.0/) | 3.0 | BSD-3-Clause |
| [pydantic](https://pypi.org/project/pydantic/2.13.5/) | 2.13.5 | MIT |
| [pydantic-core](https://pypi.org/project/pydantic-core/2.46.5/) | 2.46.5 | MIT |
| [pydantic-settings](https://pypi.org/project/pydantic-settings/2.15.0/) | 2.15.0 | MIT |
| [PyJWT](https://pypi.org/project/PyJWT/2.15.1/) | 2.15.1 | MIT |
| [python-dotenv](https://pypi.org/project/python-dotenv/1.2.4/) | 1.2.4 | BSD-3-Clause |
| [python-multipart](https://pypi.org/project/python-multipart/0.0.32/) | 0.0.32 | Apache-2.0 |
| [pywin32](https://pypi.org/project/pywin32/312/) | 312 | Package metadata: PSF. The wheel also includes [component license files](https://github.com/mhammond/pywin32/tree/main): LGPL-2.1 text for `adodbapi`, BSD-style terms for Win32/COM/Pythonwin, MIT text for MAPIStubLibrary, Scintilla terms, and the Python license for an IDLE component. Preserve the wheel's actual files if redistributing it. Windows only. |
| [PyYAML](https://pypi.org/project/PyYAML/6.0.3/) | 6.0.3 | MIT |
| [referencing](https://pypi.org/project/referencing/0.37.0/) | 0.37.0 | MIT |
| [rfc3339-validator](https://pypi.org/project/rfc3339-validator/0.1.4/) | 0.1.4 | MIT license |
| [rpds-py](https://pypi.org/project/rpds-py/2026.9.1/) | 2026.9.1 | MIT |
| [six](https://pypi.org/project/six/1.17.0/) | 1.17.0 | MIT |
| [sse-starlette](https://pypi.org/project/sse-starlette/3.5.0/) | 3.5.0 | BSD-3-Clause |
| [starlette](https://pypi.org/project/starlette/1.7.0/) | 1.7.0 | BSD-3-Clause |
| [typing-extensions](https://pypi.org/project/typing-extensions/4.16.0/) | 4.16.0 | PSF-2.0 |
| [typing-inspection](https://pypi.org/project/typing-inspection/0.4.4/) | 0.4.4 | MIT |
| [tzdata](https://pypi.org/project/tzdata/2026.5/) | 2026.5 | Apache-2.0 package; its IANA time-zone data has separate public-domain provenance described upstream. |
| [uvicorn](https://pypi.org/project/uvicorn/0.54.0/) | 0.54.0 | BSD-3-Clause |

This inventory is specific to this lock revision. If dependencies change, regenerate and review it against the new lock and the exact distributions shipped or installed. License files inside each upstream distribution remain the authoritative text. The AIOS Core release artifacts contain only the project's own source and wheel contents, so dependency license files are delivered by the dependency installers rather than copied into AIOS Core's artifacts.
