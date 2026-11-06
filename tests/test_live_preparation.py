import json

import pyarrow.parquet as pq
import pytest

from badapple_earth.collection.bundle import members, read_manifest
from badapple_earth.production.variants.preparation import capture, pending_shards, run
from badapple_earth.production.variants.bank import build


def source(root, shard_ids):
    root.mkdir(exist_ok=True)
    manifest = {'index_id': root.name, 'rows': 10 * len(shard_ids),
                'config': {'cache_rgb': True}, 'feature_version': 'test',
                'canonical_size': [256, 191], 'coarse_shape': [12, 16],
                'shards': [{'shard_id': shard, 'rows': 10} for shard in shard_ids]}
    (root / 'manifest.json').write_text(json.dumps(manifest))


def test_published_shards_are_frozen_once_without_scanning_photos_and_later_shards_remain_pending(tmp_path, monkeypatch):
    live = tmp_path / 'live'
    source(live, ['first', 'second'])
    output = tmp_path / 'prepared'
    monkeypatch.setattr(pq, 'read_table', lambda *a, **k: pytest.fail('No tile scan for shard capture'))
    available = pending_shards([str(live)], [])
    first = capture(available, output, 10)
    root = output / first['path'] / 'index'
    manifest = read_manifest(root)
    assert first['parents'] == manifest['rows'] == 10
    assert manifest['tile_deduplication'] == 'not_performed'
    source(live, ['first', 'second', 'third'])
    frozen = members(root, manifest)[0][1]
    assert [s['shard_id'] for s in frozen['shards']] == ['first']
    remaining = pending_shards([str(live)], [first])
    assert [s['shard_id'] for s in remaining[0][2]] == ['second', 'third']


def test_live_preparation_respects_user_pause_before_reserving_new_data(tmp_path):
    pause = tmp_path / 'pause.json'
    pause.write_text('{}')
    config = {'output': str(tmp_path / 'prepared'), 'pause_file': str(pause)}
    with pytest.raises(InterruptedError):
        run(config)
    state = read_manifest(tmp_path / 'prepared')
    assert state['status'] == 'paused' and state['chunks'] == []
    with pytest.raises(InterruptedError):
        build({'workers': 1, 'pause_file': str(pause)})
