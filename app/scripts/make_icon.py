#!/usr/bin/env python3
"""Generate the Recast macOS app icon set.

Pillow only. Geometry is authored on a 1024-unit canvas and rendered at
SS-times supersampling, then downsampled with LANCZOS.

Outputs go to the AppIcon.appiconset folder plus a 1024 preview in scripts/.
Contents.json is updated so each entry carries its matching filename.
"""
import json
import math
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
ICONSET = os.path.join(ROOT, "Recast/Assets.xcassets/AppIcon.appiconset")
SCRIPTS = os.path.join(ROOT, "scripts")

SS = 4            # supersampling factor
CANVAS = 1024     # design units

ORANGE = (242, 107, 58)    # #F26B3A, top-left of gradient
CORAL = (217, 54, 79)      # #D9364F, bottom-right of gradient
WHITE = (255, 255, 255)
FOLD = (246, 168, 143)     # flat tint for the folded corner flap

# Squircle-style rounded rect: 824 units, centered, 22.5% corner radius.
SHAPE_MIN = 100
SHAPE_MAX = 924
SHAPE_RADIUS = 0.225 * 824

# Glyph geometry (1024 units).
LOOP_R = 290       # radius of the circular convert loop (arc centerline)
ARC_W = 46         # arc stroke width
HEAD_HW = 56       # arrowhead half-width (radial)
HEAD_DEG = 19      # arrowhead length in degrees of arc
ARROW_A = (-78, 72)            # first arrow, clockwise
ARROW_B = (102, 252)           # second arrow, 180 degrees opposite

DOC_W, DOC_H = 240, 300
DOC_FOLD = 74
DOC_RADIUS = 24


def gradient_mask(size):
    """Diagonal 0..255 ramp (top-left = 0, bottom-right = 255)."""
    ramp = Image.linear_gradient("L")               # 256x256, top 0 -> bottom 255
    vert = ramp.resize((size, size), Image.BILINEAR)
    horiz = ramp.rotate(90, expand=True).resize((size, size), Image.BILINEAR)
    # horizontal ramp check: left edge should be dark, right edge bright
    assert horiz.getpixel((0, size // 2)) < horiz.getpixel((size - 1, size // 2))
    return ImageChops.add(horiz, vert, scale=2.0)   # (h + v) / 2


def arc_arrow(d, cx, cy, r, w, a0, a1, head_deg, head_hw, color):
    """Clockwise arc stroke from angle a0 to a1 (degrees, screen space) with a head at a1."""
    steps = 240

    def pt(rad, ang):
        t = math.radians(ang)
        return (cx + rad * math.cos(t), cy + rad * math.sin(t))

    angs = [a0 + (a1 - a0) * i / steps for i in range(steps + 1)]
    outer = [pt(r + w / 2, a) for a in angs]
    inner = [pt(r - w / 2, a) for a in reversed(angs)]
    head = [pt(r + head_hw, a1), pt(r, a1 + head_deg), pt(r - head_hw, a1)]
    d.polygon(outer + head + inner, fill=color)

    # rounded tail
    tx, ty = pt(r, a0)
    rr = w / 2
    d.ellipse([tx - rr, ty - rr, tx + rr, ty + rr], fill=color)


def doc_polygon(cx, cy, w, h, fold, rr):
    x0, y0 = cx - w / 2, cy - h / 2
    x1, y1 = cx + w / 2, cy + h / 2
    pts = []

    def corner(ccx, ccy, a0, a1):
        for i in range(17):
            a = math.radians(a0 + (a1 - a0) * i / 16)
            pts.append((ccx + rr * math.cos(a), ccy + rr * math.sin(a)))

    corner(x0 + rr, y0 + rr, 180, 270)       # top-left
    pts.append((x1 - fold, y0))              # top edge up to fold
    pts.append((x1, y0 + fold))              # diagonal fold cut
    corner(x1 - rr, y1 - rr, 0, 90)          # bottom-right
    corner(x0 + rr, y1 - rr, 90, 180)        # bottom-left
    flap = [(x1 - fold, y0), (x1 - fold, y0 + fold), (x1, y0 + fold)]
    return pts, flap


def render(px, stroke_scale=1.0):
    """Render the icon at px x px, supersampled SS times then downsampled."""
    W = px * SS
    k = W / CANVAS

    def S(v):
        return v * k

    x0, x1 = S(SHAPE_MIN), S(SHAPE_MAX)
    radius = S(SHAPE_RADIUS)

    canvas = Image.new("RGBA", (W, W), (0, 0, 0, 0))

    # soft drop shadow under the shape
    shadow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [x0, x0 + S(14), x1, x1 + S(14)], radius=radius, fill=(70, 18, 30, 120)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(S(16)))
    canvas = Image.alpha_composite(canvas, shadow)

    # gradient body
    grad = gradient_mask(W)
    body_rgb = Image.composite(Image.new("RGB", (W, W), CORAL),
                               Image.new("RGB", (W, W), ORANGE), grad)
    shape_mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(shape_mask).rounded_rectangle(
        [x0, x0, x1, x1], radius=radius, fill=255
    )
    body = body_rgb.convert("RGBA")
    body.putalpha(shape_mask)
    canvas = Image.alpha_composite(canvas, body)

    # white glyph (flat fills, no gradients)
    glyph = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glyph)
    cx = cy = S(512)
    w = ARC_W * stroke_scale
    hw = HEAD_HW * stroke_scale
    for a0, a1 in (ARROW_A, ARROW_B):
        arc_arrow(gd, cx, cy, S(LOOP_R), S(w), a0, a1, HEAD_DEG, S(hw), WHITE + (255,))

    doc_pts, flap = doc_polygon(cx, cy, S(DOC_W), S(DOC_H), S(DOC_FOLD), S(DOC_RADIUS))
    gd.polygon(doc_pts, fill=WHITE + (255,))
    gd.polygon(flap, fill=FOLD + (255,))
    canvas = Image.alpha_composite(canvas, glyph)

    if W != px:
        canvas = canvas.resize((px, px), Image.LANCZOS)
    return canvas


def main():
    os.makedirs(SCRIPTS, exist_ok=True)
    master = render(1024)
    master.save(os.path.join(SCRIPTS, "icon_preview.png"), optimize=True)

    # Small sizes get heavier strokes so the loop still reads at 16 and 32 px.
    small = {16: render(16, stroke_scale=1.9), 32: render(32, stroke_scale=1.5)}

    def image_for_px(px):
        if px in small:
            return small[px]
        return master if px == 1024 else master.resize((px, px), Image.LANCZOS)

    # Map each Contents.json entry to its pixel size and filename.
    contents_path = os.path.join(ICONSET, "Contents.json")
    with open(contents_path) as f:
        contents = json.load(f)

    written = []
    for entry in contents["images"]:
        base = int(entry["size"].split("x")[0])
        scale = int(entry["scale"].rstrip("x"))
        px = base * scale
        suffix = "" if scale == 1 else "@2x"
        filename = f"icon_{entry['size']}{suffix}.png"
        entry["filename"] = filename
        out = os.path.join(ICONSET, filename)
        image_for_px(px).save(out, optimize=True)
        written.append((filename, px))

    with open(contents_path, "w") as f:
        json.dump(contents, f, indent=2)
        f.write("\n")

    for name, px in written:
        print(f"{px:5d}px  {name}")


if __name__ == "__main__":
    main()
