"""Generate a minimal Tauri icon set from scratch (no external icon source).

Produces the PNG sizes + .icns that tauri.conf.json references.
Design: dark square with a circular "pin" dot — enough to be recognizable
as an app icon while we don't have proper branding.
"""

from __future__ import annotations

import pathlib
import subprocess
import tempfile

from PIL import Image, ImageDraw

HERE = pathlib.Path(__file__).resolve().parent


def make_base(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # rounded dark-blue square background
    radius = int(size * 0.22)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=(17, 24, 39, 255))
    # outer ring (pin shape)
    cx = size // 2
    cy = int(size * 0.47)
    r_out = int(size * 0.28)
    r_in = int(size * 0.19)
    d.ellipse([cx - r_out, cy - r_out, cx + r_out, cy + r_out], fill=(59, 130, 246, 255))
    d.ellipse([cx - r_in, cy - r_in, cx + r_in, cy + r_in], fill=(17, 24, 39, 255))
    # stem
    stem_w = int(size * 0.06)
    stem_top = cy + int(r_out * 0.6)
    stem_bot = int(size * 0.84)
    d.rounded_rectangle(
        [cx - stem_w, stem_top, cx + stem_w, stem_bot],
        radius=stem_w,
        fill=(59, 130, 246, 255),
    )
    return img


def main() -> None:
    sizes = {
        "32x32.png": 32,
        "128x128.png": 128,
        "128x128@2x.png": 256,
        "icon.png": 512,
    }
    pngs: dict[str, pathlib.Path] = {}
    for name, size in sizes.items():
        out = HERE / name
        img = make_base(size)
        img.save(out)
        pngs[name] = out
        print(f"wrote {out}")

    # Build .icns from the 1024 variant via iconutil (mac-only)
    with tempfile.TemporaryDirectory() as td:
        iconset = pathlib.Path(td) / "icon.iconset"
        iconset.mkdir()
        icns_sizes = {
            "icon_16x16.png": 16,
            "icon_16x16@2x.png": 32,
            "icon_32x32.png": 32,
            "icon_32x32@2x.png": 64,
            "icon_128x128.png": 128,
            "icon_128x128@2x.png": 256,
            "icon_256x256.png": 256,
            "icon_256x256@2x.png": 512,
            "icon_512x512.png": 512,
            "icon_512x512@2x.png": 1024,
        }
        for name, size in icns_sizes.items():
            make_base(size).save(iconset / name)
        out = HERE / "icon.icns"
        subprocess.run(
            ["iconutil", "-c", "icns", "-o", str(out), str(iconset)],
            check=True,
        )
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
