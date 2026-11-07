# Public repository preparation

## Scope

The user requested a publication-ready repository, especially a clean English README. Their final licensing decision was to leave the project-wide license unspecified. No LICENSE file or project license expression was added. Existing third-party permissions remain in force.

This work prepares local source and documentation. It does not push commits, change repository visibility, publish packages or upload media.

## Changes

- Replaced the accumulated README with an English introduction, current matching/framing workflow, requirements, small baseline example, outputs, imagery sources and documentation links.
- Added `docs/USAGE.md` in English, separating a fresh baseline run from full-video presets that depend on unbundled local indexes and completed runs. Historical Astra experiments are not presented as the current default.
- Extended ignore rules for machine-local cloud credentials, coding-agent state and videos accidentally placed at the repository root. Existing input/data/environment/cache exclusions remain intact.
- Updated package description and repository links. Added `THIRD_PARTY_NOTICES.md` to package notice files after discovering that the initial wheel omitted it. This carries the existing notice into distributions without assigning a project-wide license, using the [PyPA license-files field](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/#license-and-license-files).
- Fixed the existing `displayed_candidates` argument mismatch exposed by tracked regression tests. Only that function's optional score argument was separated from pre-existing working-tree changes. Manual review, excerpt and other unfinished local changes were preserved.

## Verification

A source snapshot was exported from Git and overlaid only with the publication edits. It contained no local video, dataset, production cache or unfinished feature modules. Python imports were explicitly pointed at the exported source rather than the working-tree editable installation.

- Offline suite: **198 passed, 3 skipped** in 29.98 seconds. The skipped encoding/layout cases require the local font cache absent from the export. The first attempt also exposed a missing temporary parent directory in the verification harness; the harness was corrected before this result.
- README/usage local links and heading anchors resolved. Documented CLI options were checked against command help.
- `uv lock --check` passed. Source distribution and wheel built successfully offline from the exported source.
- Both distribution archives contained the third-party notice and no input/data/local credential directories. Wheel metadata contained `License-File: THIRD_PARTY_NOTICES.md` and no project `License-Expression`.
- Ignore rules excluded input videos, NumPy data, rendered videos, local fonts, environment values, cloud credentials and coding-agent state while leaving example environment files and source/configuration paths eligible for tracking.
- A read-only pattern scan of **610 reachable Git blobs** found no dataset/media paths, blobs over 10 MiB or machine-specific user paths. The sole credential-shaped URL was a synthetic username/password fixture in the historical imagery test; both values were confirmed to be dummy literals. No matching real access keys, private keys, API tokens or signed query strings were found by the scan.

Local evidence and build artifacts are under `.local/public-*` and remain ignored. The original input, collected imagery and completed videos were not changed or re-downloaded.
