"""
Compile the license public key into the build: writes
src/licensing/embedded_key.py from src/licensing/public_key.pem (both
git-ignored). A packaged app trusts only this embedded key, so a key file
swapped in next to the exe does nothing. Run by packaging/omr.spec and
packaging/build_nuitka.py before they collect src/.

    python packaging/embed_public_key.py
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PEM = ROOT / "src" / "licensing" / "public_key.pem"
MODULE = ROOT / "src" / "licensing" / "embedded_key.py"


def embed(pem=PEM, module=MODULE):
    """Write the module; True when a key was embedded. Without a key a stale
    module from an earlier build is removed, so no old key ships by mistake."""
    if not Path(pem).is_file():
        Path(module).unlink(missing_ok=True)
        print("WARNING: src/licensing/public_key.pem is missing; this build "
              "will not accept any license. See docs/licensing.md.")
        return False
    data = Path(pem).read_bytes().strip()
    if b"BEGIN PUBLIC KEY" not in data:
        raise SystemExit(f"{pem} is not a PEM public key")
    Path(module).write_text(
        "# Written by packaging/embed_public_key.py at build time; not committed\n"
        f"PUBLIC_KEY_PEM = {data!r}\n",
        encoding="utf-8",
    )
    print(f"License public key embedded in {Path(module).name}")
    return True


if __name__ == "__main__":
    embed()
