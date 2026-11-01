"""P1 evidence files and small contact sheets, not a bulk data store."""

import hashlib
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw


def write_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def contact_sheet(entries: list[tuple[Path, str]], output: Path, columns=5):
    if not entries:
        return
    cell_width, cell_height = 264, 226
    result = Image.new("RGB", (columns * cell_width, math.ceil(len(entries) / columns) * cell_height), "#222222")
    draw = ImageDraw.Draw(result)
    for index, (path, label) in enumerate(entries):
        x, y = index % columns * cell_width, index // columns * cell_height
        with Image.open(path) as source:
            thumbnail = source.convert("RGB")
            thumbnail.thumbnail((256, 192))
            result.paste(thumbnail, (x + 4, y + 4))
        draw.text((x + 4, y + 198), label, fill="white")
    result.save(output)
