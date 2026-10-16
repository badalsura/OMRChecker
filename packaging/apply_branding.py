"""
Turn branding/branding.json and branding/icon.png into the files the build
uses: branding/icon.ico (exe, installer, shortcuts) and branding/branding.iss
(installer name). Run after changing the name or the icon:

    python packaging/apply_branding.py            # from the repo folder
    python packaging/apply_branding.py --draw     # also redraw the default icon

The web app and the launcher window read branding.json and icon.png at start,
so a name or icon change shows there without a rebuild.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.branding import BRANDING_DIR, load_branding  # noqa: E402

ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def draw_default_icon(path, size=512):
    """An answer sheet with filled bubbles on a blue tile."""
    from PIL import Image, ImageDraw

    scale = 4
    s = size * scale
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    u = s / 32.0
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=7 * u, fill=(22, 78, 166, 255))
    d.rounded_rectangle([6 * u, 4 * u, 26 * u, 28 * u], radius=2 * u, fill=(255, 255, 255, 255))
    # Timing marks down the sheet's left edge
    for row in range(5):
        y = 8 * u + row * 4 * u
        d.rectangle([7.5 * u, y - 0.6 * u, 9.5 * u, y + 0.6 * u], fill=(22, 78, 166, 255))
    marked = {0: 1, 1: 3, 2: 0, 3: 2, 4: 1}
    for row in range(5):
        y = 8 * u + row * 4 * u
        for col in range(4):
            x = 12.5 * u + col * 3.6 * u
            box = [x - 1.3 * u, y - 1.3 * u, x + 1.3 * u, y + 1.3 * u]
            if marked[row] == col:
                d.ellipse(box, fill=(240, 120, 20, 255))
            else:
                d.ellipse(box, outline=(120, 130, 150, 255), width=int(0.35 * u))
    image.resize((size, size), Image.LANCZOS).save(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--draw", action="store_true", help="redraw branding/icon.png")
    args = parser.parse_args()
    from PIL import Image

    brand = load_branding()
    icon_png = BRANDING_DIR / brand["icon"]
    if args.draw or not icon_png.exists():
        draw_default_icon(icon_png)
    image = Image.open(icon_png).convert("RGBA")
    image.save(BRANDING_DIR / "icon.ico", sizes=ICO_SIZES)
    # Inno Setup reads this; quotes in a name are doubled for its strings
    quote = lambda value: '"' + str(value).replace('"', '""') + '"'  # noqa: E731
    (BRANDING_DIR / "branding.iss").write_text(
        "; Written by packaging/apply_branding.py from branding.json\n"
        f"#define AppName {quote(brand['name'])}\n"
        f"#define AppPublisher {quote(brand['publisher'])}\n",
        encoding="utf-8",
    )
    print(f"Branding: {brand['name']} ({icon_png.name} -> icon.ico, branding.iss)")


if __name__ == "__main__":
    main()
