"""A 16:9 presentation connecting source, distinct Top-5 places and selection."""

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG = "#0b111a"
MUTED = "#8292a6"
INK = "#eaf0f6"
ACCENT = "#49e1c2"
SOURCE = (40, 395, 288, 216)
HERO = (728, 72, 1152, 862)
THUMBS = [(416, 52 + i * 190, 192, 144) for i in range(5)]


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
    minutes, tenths = divmod(round(value * 10), 600)
    return f"{minutes:02d}:{tenths/10:04.1f}"


@lru_cache(maxsize=25)
def template(selected, count=5):
    canvas = Image.new("RGB", (1920, 1080), BG)
    canvas.paste(connections(selected,count), (0, 0), connections(selected,count))
    return canvas


@lru_cache(maxsize=25)
def connections(selected, count=5):
    if not 1 <= count <= len(THUMBS) or not 0 <= selected < count:
        raise ValueError("Show one to five actual candidates with the selected view present")
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
    for x, y, width, height in THUMBS[:count]:
        center = y + height // 2
        draw.line([origin, (370, origin[1]), (370, center), (x - 12, center)], fill="#293645", width=1, joint="curve")
    chosen_center = THUMBS[selected][1] + THUMBS[selected][3] // 2
    draw.line([origin, (370, origin[1]), (370, chosen_center), (404, chosen_center)], fill=ACCENT, width=2, joint="curve")
    start, end = (616, chosen_center), (716, HERO[1] + HERO[3] // 2)
    curve = []
    for i in range(65):
        t = i / 64
        u = 1 - t
        curve.append((round(u**3*start[0] + 3*u*u*t*682 + 3*u*t*t*656 + t**3*end[0]),
                      round(u**3*start[1] + 3*u*u*t*start[1] + 3*u*t*t*end[1] + t**3*end[1])))
    draw.line(curve + [(HERO[0], end[1])], fill="#153b39", width=6)
    draw.line(curve + [(HERO[0], end[1])], fill=ACCENT, width=2)
    for point in [origin, start, end]:
        x, y = point
        draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=ACCENT)
    return canvas.resize((1920, 1080), Image.Resampling.LANCZOS)


def frame(plan, config):
    regular, bold = config["font_regular"], config["font_bold"]
    selected = plan.get("selected_candidate", 0)
    canvas = template(selected,len(plan["candidates"])).copy()
    draw = ImageDraw.Draw(canvas)
    sx, sy, sw, sh = SOURCE
    canvas.paste(resized(plan["source_image"], sw, sh), (sx, sy))
    draw.rectangle((sx - 1, sy - 1, sx + sw, sy + sh), outline="#536074", width=1)
    for number, (candidate, box) in enumerate(zip(plan["candidates"], THUMBS)):
        x, y, width, height = box
        canvas.paste(resized(candidate["path"], width, height), (x, y))
        draw.rectangle((x - 3, y - 3, x + width + 2, y + height + 2),
                       outline=ACCENT if number == selected else "#36475c", width=2 if number == selected else 1)
        color = ACCENT if number == selected else MUTED
        draw.text((x, y + height + 9), f"{candidate['rank']:02d}", font=font(regular, 15), fill=color)
        value = candidate.get("selection_score", candidate["raw_mse"])
        draw.text((x + width, y + height + 7), f"{value:.3f}", anchor="ra", font=font(regular, 18), fill=color)
    x, y, width, height = HERO
    canvas.paste(resized(plan["selected_image"], width, height), (x, y))
    draw.rectangle((x - 1, y - 1, x + width, y + height), outline="#536074", width=1)
    draw.text((x, 960), plan["caption"], font=font(regular, 19), fill=INK)
    draw.text((416, 24), plan.get("score_label", "MSE"), font=font(regular, 12), fill=MUTED)
    draw.text((40, 640), timestamp(plan["time"]) + " / " + timestamp(plan["duration"]), font=font(regular, 18), fill=INK)
    draw.text((328, 670), f"{plan['slot']+1:,} / {plan['total']:,}", anchor="ra", font=font(regular, 14), fill=MUTED)
    draw.line((40, 708, 328, 708), fill="#29394b", width=2)
    draw.line((40, 708, 40 + 288 * (plan["slot"] + 1) / plan["total"], 708), fill=ACCENT, width=2)
    draw.text((1880, 1044), plan.get("credit", "Contains modified Copernicus Sentinel data"), anchor="ra", font=font(regular, 11), fill=MUTED)
    return canvas


def location_caption(row):
    lat, lon = row["latitude"], row["longitude"]
    dates = list(dict.fromkeys(value[:10] for value in row["acquired_at"].split("/")))
    return (f"{abs(lat):.4f}° {'N' if lat >= 0 else 'S'}, {abs(lon):.4f}° {'E' if lon >= 0 else 'W'}"
            f"   ·   {' – '.join(dates)}")
