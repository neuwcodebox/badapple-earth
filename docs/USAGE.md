# Usage guide

This guide separates the small baseline workflow, which can start with an empty `data/` directory, from the saved-run workflows used for full-video production.

## Setup and input

Install uv and put FFmpeg and ffprobe on `PATH`, with an H.264 encoder available. Run commands from the repository root:

~~~sh
uv sync --locked
uv run badapple-earth --help
uv run badapple-earth probe --input input/badapple.mp4 --output data/demo/probe
~~~

Supply your own MP4. `config/targets.json` defaults to a 1444 × 1080 crop, starting at 38 seconds for ten seconds. Set a crop within your decoded frame dimensions and match the collection preset's `aspect_ratio` to your content.

Existing full-video runs use constant-frame-rate, square-pixel input without rotation metadata. Other timing and geometry have not been validated for those production paths. The original input is preserved.

## Small baseline run

### Collect photographs

~~~sh
uv run badapple-earth collect --config config/collect.json --output data/demo/index --limit 1000 --redistribute
~~~

This requests Sentinel-2 imagery through Earth Search across several regions and terrain types. It checks pixel validity and cloud masks and stores provenance and search features. A requested limit is a target; enough usable imagery may not be available.

Re-run the same command to resume. Ctrl+C preserves completed work; `--stop-after-blocks N` stops at a checkpoint. Increasing `--limit` with the same extraction configuration extends the index. A changed extraction or source configuration needs a different output directory.

Resource limits in `config/collect.json` include memory, storage and minimum free disk space. Reaching a limit or failing to fill quotas leaves a partial index and returns a nonzero exit status. Inspect its manifest before continuing.

### Extract, match and render

~~~sh
uv run badapple-earth targets --config config/targets.json --output data/demo/targets
uv run badapple-earth match --targets data/demo/targets --index data/demo/index --output data/demo/match --top-k 20
uv run badapple-earth materialize --targets data/demo/targets --index data/demo/index --run data/demo/match
uv run badapple-earth render --targets data/demo/targets --run data/demo/match
~~~

Target extraction and baseline matching use fresh output directories. The default matcher uses exact grayscale MSE and clockwise quarter-turn views. `--rotations 0` searches without rotation. Materialization reuses cached pixels and can read missing source windows remotely.

The silent preview samples six source frames per second and holds photographs at 24 fps:

| File | Contents |
| --- | --- |
| `color.mp4` | Matched photographs in color |
| `gray.mp4` | Matched photographs in grayscale |
| `comparison.mp4` | Original and matched frames side by side |
| `top5.jpg` | Candidate comparison sheet |
| `matches.parquet` | Ranked search results |
| `selected.parquet` | Materialized choices and provenance |

## Larger collections

`collect-parallel` runs bounded workers with independent resumable indexes. For example:

~~~sh
uv run badapple-earth collect-parallel --config config/collect-naip.json --output data/index/naip --limit 100000 --workers 2
uv run badapple-earth collect-parallel --config config/collect-linz.json --output data/index/linz --limit 100000 --workers 2
~~~

These are substantial jobs. Review quota, memory and disk settings before running them. NAIP uses Microsoft Planetary Computer's anonymous access path; LINZ uses public imagery assets. The presets do not enable AWS Requester Pays.

Combine finished parts by reference, keeping their member directories:

~~~sh
uv run badapple-earth combine --index data/index/naip/part-01 --index data/index/naip/part-02 --output data/index/naip-combined
~~~

`--freeze` pins published shards while collection continues. `--keep-duplicates` combines saved references and counts without a tile deduplication scan. Additional source presets are in `config/collect-expand-*.json`.

## Full-video production

Full-video presets name local indexes, feature caches and completed runs. These artifacts are not distributed. Check dependencies before running a later stage:

| Preset | Existing prerequisites | Result |
| --- | --- | --- |
| `production-aerial120-views30.json` | Referenced imagery indexes, packed/affine views and baseline run | All-frame input and baseline photo results |
| `silhouette-full.json` | All-frame run and its packed/affine views | Full shape search and color-continuous selection |
| `produce-expand.json` | Previous shape/monochrome runs, extra collections and prepared views | Expanded shape candidates and grayscale production |
| `registration-full.json` | Expanded shape run, source RGB and completed grayscale baseline | Position-tolerant search and continuous framing |
| `tracking-full.json` | Completed registration, original grayscale baseline and shared target pixels | Motion-aware tracking and final videos |

The JSON files record exact artifact paths and parameters. The [production record](history/2026-11-07-P6-continuous-framing.md) explains their reuse. A fresh clone must collect imagery and prepare the earlier runs first; `--resume` requires existing prerequisites.

Once those artifacts exist, run the final two stages:

~~~powershell
$env:OPENBLAS_NUM_THREADS = '8'
$env:OMP_NUM_THREADS = '8'
$env:NUMBA_NUM_THREADS = '8'
uv run python -m badapple_earth.production.registration.pipeline --config config/registration-full.json --resume
uv run python -m badapple_earth.production.registration.tracking --config config/tracking-full.json --resume
~~~

The final default directory is `data/runs/p6-tracked-1.5m-full/`. `end_seconds` selects the interval; the recorded run uses every source frame starting before 218.5 seconds. The photo-only video retains the input content dimensions. The recorded source AAC track is copied locally to the requested end without re-encoding.

Framing stays inside real image bounds. The presentation displays the selected framing and four other distinct evaluated places, ordered by actual scores. Display candidates are independent of the stricter final-selection rules. Fewer than five evaluated places is an error for this layout.

### Pause, resume and progress

Request a pause for the running stage:

~~~sh
uv run python -m badapple_earth.production.registration.tracking --config config/tracking-full.json --pause
~~~

Use `registration.pipeline` instead for alignment. A pause stops new assignments and saves in-flight results before exiting. Resume with the same stage, configuration and output using `--resume`. Completed retrieval, alignment, tracking, photo and encoding work is reused. One process should own each output directory.

| File | Meaning |
| --- | --- |
| `manifest.json` | Overall state and output information |
| `retrieval/progress.json` | Additional view search progress, when present |
| `progress.json` | Alignment/feature batch progress |
| `motion-progress.json` | Source-video motion preparation |
| `verification.json` | Output and source-timing checks |

A changed configuration may invalidate an output directory's identity. Use a new output for a changed experiment and preserve earlier runs referenced by later stages.

## Other experiments

CLI help lists shape, correlation and view comparisons. Each uses its own saved inputs and caches; read its `--help` and configuration. Historical visual-model review experiments are optional and are not required by the CPU alignment/tracking pipeline.

`compare-metrics` includes an FSIM adaptation with educational/research-only terms. See [third-party notices](../THIRD_PARTY_NOTICES.md) before using that implementation.

## Testing and access checks

Run offline tests:

~~~sh
uv run pytest -q --basetemp .local/pytest
~~~

Tests use fixtures and temporary files. They do not need the original MP4, production indexes or imagery credentials. Some encoding/render tests require FFmpeg or an available font and may skip without them.

For an explicit live Sentinel-2 access check:

~~~sh
uv run badapple-earth sample --config config/p1.json --output data/access-check --limit 20
~~~

This reads real remote imagery. Use a new output directory. `verify-sample` can re-extract samples to diagnose a discrepancy; completed downloads do not require a second verification download.

## Local files and rights

Keep input media in `input/`, generated data in `data/` and machine-specific state in `.local/`. These directories, environment files and local credential stores are ignored by Git. Keep tokens and signed imagery URLs out of tracked configuration and documentation.

No project-wide code license has been assigned. Third-party code, imagery, animation and music retain their separate terms. Generated `credits.json` files record imagery attribution. These commands do not publish or upload videos.
