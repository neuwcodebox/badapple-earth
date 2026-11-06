"""Prepare newly published photo shards while their collectors keep running."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import time

from ...collection.bundle import read_manifest
from ...collection.storage import writer_lock
from ...collection.windows import identity
from ...evidence import write_json
from ..search import pack
from ..correlation.search import pack_fine
from ..silhouette.control import check_pause
from .bank import build, read_manifest_file

VERSION = "published-shard-preparation-v1"


def pending_shards(sources, chunks):
    """Read descriptors only; photo equality and duplicate IDs are not inspected."""
    reserved = {(member['source_index_id'], shard)
                for chunk in chunks for member in chunk['inputs'] for shard in member['shard_ids']}
    result = []
    for source in sources:
        root = Path(source)
        if not (root / 'manifest.json').exists():
            continue
        manifest = read_manifest_file(root / 'manifest.json')
        if not manifest.get('config', {}).get('cache_rgb'):
            raise ValueError('Live preparation requires collected local RGB')
        shards = [s for s in manifest['shards'] if s['rows'] and (manifest['index_id'], s['shard_id']) not in reserved]
        if shards:
            result.append((root, manifest, shards))
    return result


def capture(sources, output, max_parents):
    """Freeze a bounded set of complete shards without reading their tile tables."""
    selected, count = [], 0
    for root, manifest, shards in sources:
        picked = []
        for shard in shards:
            if count >= max_parents:
                break
            picked.append(shard)
            count += shard['rows']
        if picked:
            selected.append((root, manifest, picked))
        if count >= max_parents:
            break
    if not selected:
        raise ValueError('No published photo shards available')
    inputs = [{'source': str(root.resolve()), 'source_index_id': manifest['index_id'],
               'shard_ids': [s['shard_id'] for s in shards]} for root, manifest, shards in selected]
    chunk_id = identity([VERSION, inputs])
    folder = output / 'chunks' / chunk_id[:20]
    index = folder / 'index'
    records = []
    first = selected[0][1]
    for root, manifest, shards in selected:
        for key in ('feature_version', 'canonical_size', 'coarse_shape'):
            if manifest[key] != first[key]:
                raise ValueError(f'Prepared sources disagree on {key}')
        source_id = manifest['index_id']
        frozen = {**manifest, 'snapshot_source_index_id': source_id,
                  'index_id': identity(['immutable-published-shards-v1', source_id, [s['shard_id'] for s in shards]]),
                  'shards': shards, 'rows': sum(s['rows'] for s in shards), 'status': 'snapshot',
                  'areas': {}, 'terrain_groups': {}, 'scales': {}}
        records.append({'path': Path(os.path.relpath(root.resolve(), index.resolve())).as_posix(),
                        'index_id': frozen['index_id'], 'rows': frozen['rows'],
                        'shard_ids': [s['shard_id'] for s in shards], 'exclude_tile_ids': [],
                        'snapshot_manifest': frozen})
    manifest = {'schema': 'index-bundle-v1', 'index_id': identity([
                    {k: v for k, v in record.items() if k != 'path'} for record in records]),
                'rows': count, 'members': records, 'tile_deduplication': 'not_performed',
                'duplicate_tile_ids_excluded': 0, 'areas': {}, 'terrain_groups': {}, 'scales': {},
                **{key: first[key] for key in ('feature_version', 'canonical_size', 'coarse_shape')}}
    index.mkdir(parents=True, exist_ok=True)
    if (index / 'manifest.json').exists():
        if read_manifest(index)['index_id'] != manifest['index_id']:
            raise ValueError('Preparation chunk belongs to other input shards')
    else:
        write_json(index / 'manifest.json', manifest)
    return {'chunk_id': chunk_id, 'path': folder.relative_to(output).as_posix(),
            'parents': count, 'inputs': inputs, 'status': 'pending'}


def prepare_chunk(chunk, root, config, save):
    folder = root / chunk['path']
    def stage(name):
        check_pause(config.get('pause_file'))
        if shutil.disk_usage(root).free < config['min_free_disk_gib'] * 1024**3:
            raise InterruptedError('Preparation stopped at the free disk floor')
        chunk['stage'] = name
        save()
    stage('packing')
    pack(folder / 'index', folder / 'packed')
    stage('fine')
    pack_fine(folder / 'packed', folder / 'fine', (24, 32), config['workers'])
    stage('affine')
    affine = {'index': str(folder / 'index'), 'parent_packs': [str(folder / 'packed')],
              'extra_rgb_cache': str(folder / 'unused-rgb'), 'bank': str(folder / 'bank'),
              'angle_step': 30, 'crop_scale': .75, 'workers': config['workers'],
              'min_free_disk_gib': config['min_free_disk_gib'], 'pause_file': config.get('pause_file')}
    write_json(folder / 'views.json', affine)
    bank = build(affine)
    if bank['status'] != 'complete':
        raise InterruptedError('Affine preparation is incomplete')
    chunk.update(status='complete', stage='complete', quarter_views=4 * chunk['parents'],
                 additional_views=bank['completed_views'], completed_at=datetime.now(timezone.utc).isoformat())
    save()


def run(config):
    root = Path(config['output']).resolve()
    preparation_id = identity([VERSION, config])
    with writer_lock(root):
        path = root / 'manifest.json'
        state = read_manifest(root) if path.exists() else {
            'schema': VERSION, 'preparation_id': preparation_id, 'config': config, 'chunks': []}
        if state['preparation_id'] != preparation_id:
            raise ValueError('Preparation settings changed; choose another output directory')
        def save():
            state['prepared_parents'] = sum(c['parents'] for c in state['chunks'] if c['status'] == 'complete')
            state['prepared_views'] = state['prepared_parents'] * 24
            state['updated_at'] = datetime.now(timezone.utc).isoformat()
            write_json(path, state)
        try:
            while True:
                check_pause(config.get('pause_file'))
                incomplete = next((c for c in state['chunks'] if c['status'] != 'complete'), None)
                if incomplete:
                    state['status'] = 'preparing'
                    prepare_chunk(incomplete, root, config, save)
                    print(f"Prepared {state['prepared_parents']:,} photos / {state['prepared_views']:,} views", flush=True)
                    continue
                available = pending_shards(config['sources'], state['chunks'])
                pending = sum(s['rows'] for _, _, shards in available for s in shards)
                collection = read_manifest_file(Path(config['collection_state']))
                finished = collection['status'] in ('complete', 'partial', 'failed')
                if pending and (pending >= config['min_chunk_parents'] or finished):
                    state['chunks'].append(capture(available, root, config['max_chunk_parents']))
                    save()
                    continue
                if finished and not pending:
                    state['status'] = 'complete' if collection['status'] == 'complete' else 'partial'
                    save()
                    return state
                state.update(status='waiting_for_collection', published_pending_parents=pending)
                save()
                time.sleep(config['poll_seconds'])
        except (InterruptedError, KeyboardInterrupt):
            state['status'] = 'paused'
            save()
            raise
        except BaseException as error:
            state.update(status='failed', error_type=type(error).__name__)
            save()
            raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    run(json.loads(args.config.read_text(encoding='utf-8')))
