# Third-party notices

The [MIT license](LICENSE) covers the original Cloud Storage project code and
documentation. Dependencies, bundled runtimes and external tools retain their
own copyright notices and license terms. MIT does not replace those terms.

This is an index of major components, not a complete SBOM or a certification
that every distribution satisfies all third-party license obligations.

| Component | License information | Upstream |
|---|---|---|
| Python runtime | PSF license and bundled third-party notices | [Python licenses](https://docs.python.org/3/license.html) |
| PySide6, Shiboken6, Qt | Qt for Python provides open-source and commercial licensing options; individual modules and bundled components have their own terms | [Qt for Python licensing](https://doc.qt.io/qtforpython-6/licenses.html) |
| cryptography | Apache-2.0 OR BSD-3-Clause; bundled OpenSSL/Rust dependencies have additional notices | [cryptography source licenses](https://github.com/pyca/cryptography) |
| argon2-cffi, FastAPI | MIT | [argon2-cffi](https://github.com/hynek/argon2-cffi), [FastAPI](https://github.com/fastapi/fastapi) |
| psutil, Uvicorn | BSD-3-Clause | [psutil](https://github.com/giampaolo/psutil), [Uvicorn](https://github.com/Kludex/uvicorn) |
| qrcode | BSD; see the installed package's license text | [qrcode](https://github.com/lincolnloop/python-qrcode) |
| pywin32 | Python Software Foundation license | [pywin32](https://github.com/mhammond/pywin32) |
| Go runtime, golang.org/x/sys | BSD-style license texts supplied upstream | [Go](https://go.dev/LICENSE), [x/sys](https://go.googlesource.com/sys/+/refs/heads/master/LICENSE) |
| go-winfsp | MIT | [go-winfsp](https://github.com/winfsp/go-winfsp) |
| WinFsp | GPLv3 with the WinFsp licensing exception described upstream | [WinFsp license](https://github.com/winfsp/winfsp/blob/master/License.txt) |

The Windows packaging notice is [packaging/THIRD-PARTY-NOTICES.txt](packaging/THIRD-PARTY-NOTICES.txt).
Inspect each package's installed `.dist-info` license files, Go module license
files and any bundled notices for the exact versions being redistributed.
Mobile dependencies are declared separately in `mobile/package.json` and
`mobile/pnpm-lock.yaml`; their licenses are not covered by this index.

For a release, preserve applicable notices and license texts in the actual
distribution. Adding this index does not retroactively change the contents of
already published installer or ZIP assets.
