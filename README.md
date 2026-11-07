# Bad Apple Earth

**Bad Apple!!, reconstructed with real photographs of Earth.**

A local Python project that finds satellite and aerial photographs resembling video frames. Each result uses one real place, with its location, capture information and source preserved.

The full-video presentation shows the original frame, **five distinct candidate places**, and the selected photograph. A second output contains just the matched photographs and the original audio.

## How it works

1. Collect photographs from public imagery catalogs and cache pixels and metadata locally.
2. Search brightness and shape features for promising matches.
3. Adjust the crop, position, scale and rotation within a photograph's valid bounds.
4. Track useful matches across nearby frames and choose a sequence using shape, brightness and motion.
5. Render the videos with coordinates and imagery credits.

The current pipeline runs on CPU. Collection, search, alignment and rendering save checkpoints so longer runs can resume after an interruption.

## Requirements

- [uv](https://docs.astral.sh/uv/) for Python and dependency management.
- Python 3.12 is pinned in `.python-version`; package metadata permits Python 3.11 or newer.
- FFmpeg and ffprobe on `PATH`, including an H.264 encoder.
- A local input video and internet access for imagery collection.

Development and full-video production have been exercised on Windows 11. Linux and macOS are not currently verified. The documented CPU pipeline does not require a GPU or an LLM API key.

~~~sh
uv sync --locked
uv run badapple-earth --help
ffmpeg -version
ffprobe -version
~~~

## Quick start

Run commands from the repository root. Place your own video at `input/badapple.mp4` and inspect it:

~~~sh
uv run badapple-earth probe --input input/badapple.mp4 --output data/demo/probe
~~~

The supplied `config/targets.json` uses a **1444 × 1080** crop and the **38–48 second** interval. Adjust its input path, crop and interval to match your video. The collection preset uses the same aspect ratio.

Build a small Sentinel-2 index and render a ten-second preview:

~~~sh
uv run badapple-earth collect --config config/collect.json --output data/demo/index --limit 1000 --redistribute
uv run badapple-earth targets --config config/targets.json --output data/demo/targets
uv run badapple-earth match --targets data/demo/targets --index data/demo/index --output data/demo/match --top-k 20
uv run badapple-earth materialize --targets data/demo/targets --index data/demo/index --run data/demo/match
uv run badapple-earth render --targets data/demo/targets --run data/demo/match
~~~

This produces `color.mp4`, `gray.mp4` and `comparison.mp4` in `data/demo/match/`. The preview samples six source frames per second and holds each photograph at 24 fps; it is silent. This is a small baseline run. The full-video pipeline additionally uses continuous alignment and motion-aware selection.

Collection can resume with the same command. Input inspection, target extraction and baseline matching require new output directories. See the [usage guide](docs/USAGE.md) for configuration, pause/resume and full-video prerequisites.

## Full-video pipeline

The latest approach combines shape search, continuous photo alignment and motion-aware selection. It preserves every source frame in the requested interval. The five displayed candidates are evaluated places; final selection has its own visibility and continuity conditions.

Full production presets refer to local indexes and completed intermediate runs. **Those data and caches are not included in this repository.** The [full-video guide](docs/USAGE.md#full-video-production) lists the prerequisite runs and execution order. A fresh clone needs those earlier stages before a final-stage preset can run.

| Output | Contents |
| --- | --- |
| `final-16x9.mp4` | 1920 × 1080 presentation: original, five candidates, selected photograph and provenance |
| `satellite-full.mp4` | Photo-only video at the input content aspect ratio |
| `selected.parquet` | Frame-level selections, source timestamps, imagery metadata and transforms |
| `frame-plan.parquet` | Exact frame layout and displayed candidates |
| `credits.json` | Imagery attribution and source references |
| `manifest.json` | Configuration, progress and output information |

Input videos, collected imagery, indexes, generated media and local credentials stay outside version control. The tool reads the supplied MP4 locally; video downloading and publishing are outside its workflow.

## Imagery sources

| Imagery | Access |
| --- | --- |
| Copernicus Sentinel-2 | [Earth Search / Sentinel-2 COGs](https://registry.opendata.aws/sentinel-2-l2a-cogs/) |
| USDA NAIP | [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/dataset/naip) |
| New Zealand aerial imagery | [LINZ](https://registry.opendata.aws/nz-imagery/) |
| Netherlands aerial imagery | [Beeldmateriaal Nederland](https://www.beeldmateriaal.nl/dataroom) |
| Swiss aerial imagery | [swisstopo SWISSIMAGE](https://www.swisstopo.admin.ch/fr/orthophotos-swissimage-10-cm) |

Source-specific attribution is recorded with the imagery and rendered outputs. Capture information may be an exact date, a date range or a representative/product year, depending on the source.

## Development

~~~sh
uv run pytest -q --basetemp .local/pytest
~~~

Tests use offline fixtures. Live imagery access is a separate, explicit command; see [testing and access checks](docs/USAGE.md#testing-and-access-checks).

| Location | Purpose |
| --- | --- |
| `src/badapple_earth/` | Collection, matching, alignment and video production |
| `config/` | Collection settings and recorded production presets |
| `tests/` | Offline contract and regression tests |
| [Usage guide](docs/USAGE.md) | English command reference and operational notes |
| [Specification](docs/SPEC.md) | Project scope and requirements — Korean |
| [Technical notes](docs/TECH.md) | Architecture and design decisions — Korean |
| [Development plan](docs/PLAN.md) | Current status and next work — Korean |
| `docs/history/` | Experiment records and previous decisions — primarily Korean |

## Rights and third-party notices

No project-wide license is currently specified. Existing third-party code retains its own terms; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). In particular, the optional FSIM comparison implementation carries an educational/research-use restriction.

The original animation, music and imagery have separate rights and attribution requirements. They are not bundled with the source code. Imagery licenses do not license the original music, animation or an entire rendered video.
