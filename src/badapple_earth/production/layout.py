"""A 16:9 presentation connecting source, distinct Top-5 places and selection."""

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG = "#0b111a"
MUTED = "#8292a6"
INK = "#eaf0f6"
ACCENT = "#49e1c2"
SOURCE = (48, 448, 288, 216)
HERO = (768, 160, 1104, 826)
THUMBS = [(432, 160 + i * 172, 192, 144) for i in range(5)]


@lru_cache(maxsize=32)
def font(path, size):
    bold = Path(path).stem.lower().endswith("b") or "bold" in str(path).lower()
    choices = [str(path), "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
               "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]
    for choice in choices:
        try:
            return ImageFont.truetype(choice, size)
        except OSError:
            pass
    raise ValueError("Set font_regular/font_bold to available local TrueType fonts")


@lru_cache(maxsize=160)
def resized(path, width, height):
    with Image.open(path) as source:
        return source.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)


def timestamp(value):
    minutes, seconds = divmod(value, 60)
    return f"{int(minutes):02d}:{seconds:06.3f}"


@lru_cache(maxsize=4)
def template(regular, bold):
    canvas = Image.new("RGB", (1920, 1080), BG)
    draw = ImageDraw.Draw(canvas)
    draw.text((48, 34), "BAD APPLE / EARTH", font=font(bold, 38), fill=INK)
    draw.text((50, 84), "Every frame. A real place.", font=font(regular, 18), fill=MUTED)
    draw.line((48, 116, 1872, 116), fill="#253141", width=1)
    draw.text((1872, 50), "REAL SATELLITE IMAGERY", anchor="ra", font=font(bold, 15), fill=ACCENT)
    draw.text((48, 406), "SOURCE", font=font(bold, 17), fill=MUTED)
    draw.text((432, 126), "TOP 5 PLACES", font=font(bold, 17), fill=MUTED)
    draw.text((768, 126), "MATCHED PLACE", font=font(bold, 17), fill=MUTED)
    draw.text((1872, 126), "SELECTED #1", anchor="ra", font=font(bold, 16), fill=ACCENT)
    canvas.paste(connections(), (0, 0), connections())
    return canvas


@lru_cache(maxsize=1)
def connections():
    # Rasterize static wires once with antialiasing, rather than on every frame.
    canvas = Image.new("RGBA", (1920*2, 1080*2))
    high = ImageDraw.Draw(canvas)
    class Pen:
        def line(self, points, **kwargs):
            high.line([(round(x*2), round(y*2)) for x, y in points], **{**kwargs, "width": kwargs["width"]*2})
        def ellipse(self, bounds, **kwargs):
            high.ellipse(tuple(round(v*2) for v in bounds), **kwargs)
    draw = Pen()
    sx, sy, sw, sh = SOURCE
    origin = (sx + sw + 2, sy + sh // 2)
    for x, y, width, height in THUMBS:
        center = y + height // 2
        draw.line([origin, (374, origin[1]), (374, center), (x - 12, center)], fill="#2c3c4f", width=2, joint="curve")
    chosen_center = THUMBS[0][1] + THUMBS[0][3] // 2
    draw.line([origin, (374, origin[1]), (374, chosen_center), (420, chosen_center)], fill=ACCENT, width=3, joint="curve")
    start, end = (632, chosen_center), (756, HERO[1] + HERO[3] // 2)
    curve = []
    for i in range(65):
        t = i / 64
        u = 1 - t
        curve.append((round(u**3*start[0] + 3*u*u*t*720 + 3*u*t*t*680 + t**3*end[0]),
                      round(u**3*start[1] + 3*u*u*t*start[1] + 3*u*t*t*end[1] + t**3*end[1])))
    draw.line(curve + [(768, end[1])], fill="#153b39", width=8)
    draw.line(curve + [(768, end[1])], fill=ACCENT, width=3)
    for point in [origin, start, (758, end[1])]:
        x, y = point
        draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=ACCENT)
    return canvas.resize((1920, 1080), Image.Resampling.LANCZOS)


def frame(plan, config):
    regular, bold = config["font_regular"], config["font_bold"]
    canvas = template(regular, bold).copy()
    draw = ImageDraw.Draw(canvas)
    sx, sy, sw, sh = SOURCE
    canvas.paste(resized(plan["source_image"], sw, sh), (sx, sy))
    draw.rectangle((sx - 1, sy - 1, sx + sw, sy + sh), outline="#536074", width=1)
    for number, (candidate, box) in enumerate(zip(plan["candidates"], THUMBS)):
        x, y, width, height = box
        canvas.paste(resized(candidate["path"], width, height), (x, y))
        draw.rectangle((x - 3, y - 3, x + width + 2, y + height + 2),
                       outline=ACCENT if number == 0 else "#36475c", width=3 if number == 0 else 1)
        draw.text((400, y + 42), f"{number+1:02d}", font=font(bold, 17), fill=ACCENT if number == 0 else MUTED)
        caption = f"{'SELECTED' if number == 0 else 'RANK '+str(candidate['rank'])}  /  MSE {candidate['raw_mse']:.4f}"
        draw.text((x, y + height + 7), caption, font=font(regular, 12), fill=ACCENT if number == 0 else MUTED)
    x, y, width, height = HERO
    canvas.paste(resized(plan["selected_image"], width, height), (x, y))
    draw.rectangle((x - 1, y - 1, x + width, y + height), outline="#536074", width=1)
    draw.text((x, 1009), plan["caption"], font=font(regular, 19), fill=INK)
    draw.text((48, 687), timestamp(plan["time"]) + " / " + timestamp(plan["duration"]), font=font(bold, 20), fill=INK)
    draw.text((48, 720), f"FRAME {plan['slot']+1:04d} / {plan['total']:04d}", font=font(regular, 15), fill=MUTED)
    draw.line((48, 766, 336, 766), fill="#29394b", width=3)
    draw.line((48, 766, 48 + 288 * (plan["slot"] + 1) / plan["total"], 766), fill=ACCENT, width=3)
    draw.text((48, 1009), f"{plan['places']:,} PLACES  /  4 ROTATIONS", font=font(regular, 14), fill=MUTED)
    draw.text((1872, 1045), plan.get("credit", "Contains modified Copernicus Sentinel data"), anchor="ra", font=font(regular, 12), fill=MUTED)
    return canvas


def location_caption(row):
    lat, lon = row["latitude"], row["longitude"]
    return (f"{abs(lat):.4f}° {'N' if lat >= 0 else 'S'}, {abs(lon):.4f}° {'E' if lon >= 0 else 'W'}"
            f"   /   {row['acquired_at'][:10]}   /   SENTINEL-2   /   "
            f"{row['ground_width_m']/1000:.2f} × {row['ground_height_m']/1000:.2f} km")
