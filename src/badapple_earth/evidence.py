"""P1 evidence files and small contact sheets, not a bulk data store."""

import hashlib
import json
import math
from pathlib import Path
import time

from PIL import Image, ImageDraw


def write_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Concurrent readers and antivirus scanners can briefly hold the old name
    # on Windows. Keep the complete temporary file and retry only replacement.
    for attempt in range(8):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(min(.05 * 2**attempt, .5))


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
