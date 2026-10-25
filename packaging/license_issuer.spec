# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the License Issuer (packaging/license_issuer.py), one exe.

    pyinstaller --noconfirm --clean packaging/license_issuer.spec

No private key is ever bundled: the exe asks for one at start (or picks up
a private_key.pem placed next to it), so the build is safe to publish.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 (SPECPATH is set by PyInstaller)

datas = [
    (str(path), str(Path("tools/license_admin/static") / path.relative_to(ROOT / "tools/license_admin/static").parent))
    for path in (ROOT / "tools/license_admin/static").rglob("*")
    if path.is_file()
]

hiddenimports = collect_submodules("uvicorn") + [
    "tools.license_admin.server",
    "tools.vendor_lib",
    "src.licensing.verify",
    "src.licensing.model",
    "src.licensing.fingerprint",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "license_issuer.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["cv2", "numpy", "onnxruntime", "PIL", "matplotlib", "pandas", "scipy", "tkinter", "webview"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
icon = ROOT / "branding" / "icon.ico"
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="OMRChecker-License-Issuer",
    console=True,
    upx=False,
    icon=str(icon) if icon.exists() else None,
)
