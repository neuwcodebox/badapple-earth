"""A 16:9 presentation connecting source, distinct Top-5 places and selection."""

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG = "#0b111a"
MUTED = "#8292a6"
INK = "#eaf0f6"
ACCENT = "#49e1c2"
SOURCE = (20, 430, 250, 187)
HERO = (552, 20, 1344, 1005)
THUMBS = [(310, 28 + i * 203, 206, 154) for i in range(5)]


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
    branch = (origin[0]+THUMBS[0][0])//2
    for x, y, width, height in THUMBS[:count]:
        center = y + height // 2
        draw.line([origin, (branch, origin[1]), (branch, center), (x - 8, center)], fill="#293645", width=1, joint="curve")
    chosen_center = THUMBS[selected][1] + THUMBS[selected][3] // 2
    draw.line([origin, (branch, origin[1]), (branch, chosen_center), (THUMBS[0][0]-8, chosen_center)], fill=ACCENT, width=2, joint="curve")
    start, end = (THUMBS[0][0]+THUMBS[0][2]+8, chosen_center), (HERO[0]-8, HERO[1] + HERO[3] // 2)
    first_control = start[0]+(end[0]-start[0])*.4
    second_control = start[0]+(end[0]-start[0])*.6
    curve = []
    for i in range(65):
        t = i / 64
        u = 1 - t
        curve.append((round(u**3*start[0] + 3*u*u*t*first_control + 3*u*t*t*second_control + t**3*end[0]),
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
    metadata = plan["caption"]+"   ·   "+plan.get("credit", "Contains modified Copernicus Sentinel data")
    size = 17
    while size>8 and draw.textlength(metadata,font=font(regular,size))>width:
        size -= 1
    draw.text((x,1042),metadata,font=font(regular,size),fill=INK)
    draw.text((sx,sy+sh+25),timestamp(plan["time"])+" / "+timestamp(plan["duration"]),font=font(regular,18),fill=INK)
    draw.text((sx+sw,sy+sh+55),f"{plan['slot']+1:,} / {plan['total']:,}",anchor="ra",font=font(regular,14),fill=MUTED)
    draw.line((sx,sy+sh+90,sx+sw,sy+sh+90),fill="#29394b",width=2)
    draw.line((sx,sy+sh+90,sx+sw*(plan["slot"]+1)/plan["total"],sy+sh+90),fill=ACCENT,width=2)
    return canvas.convert("L").convert("RGB") if config.get("monochrome") else canvas


def location_caption(row):
    lat, lon = row["latitude"], row["longitude"]
    dates = list(dict.fromkeys(value[:10] for value in row["acquired_at"].split("/")))
    precision = row.get("acquisition_precision")
    if precision in ("nominal_tile_year", "product_year"):
        dates = [row["acquired_at"][:4] + (" (representative year)" if precision == "nominal_tile_year" else " (product year)")]
    return (f"{abs(lat):.4f}° {'N' if lat >= 0 else 'S'}, {abs(lon):.4f}° {'E' if lon >= 0 else 'W'}"
            f"   ·   {' – '.join(dates)}")
