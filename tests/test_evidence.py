import json
from pathlib import Path

import pytest

from badapple_earth.evidence import write_json


def test_json_publication_preserves_old_value_during_transient_access_denial(tmp_path, monkeypatch):
    path = tmp_path / 'manifest.json'
    path.write_text('{"rows":1}')
    replace = Path.replace
    attempts = []
    def temporarily_locked(temporary, target):
        attempts.append(True)
        if len(attempts) < 3:
            assert json.loads(path.read_text()) == {'rows': 1}
            raise PermissionError('reader holds the destination')
        return replace(temporary, target)
    monkeypatch.setattr(Path, 'replace', temporarily_locked)
    monkeypatch.setattr('badapple_earth.evidence.time.sleep', lambda _: None)
    write_json(path, {'rows': 2})
    assert json.loads(path.read_text()) == {'rows': 2}
    assert len(attempts) == 3


def test_persistent_json_access_denial_is_bounded_and_keeps_old_publication(tmp_path, monkeypatch):
    path = tmp_path / 'manifest.json'
    path.write_text('{"rows":1}')
    attempts = []
    def locked(*args):
        attempts.append(True)
        raise PermissionError('persistent denial')
    monkeypatch.setattr(Path, 'replace', locked)
    monkeypatch.setattr('badapple_earth.evidence.time.sleep', lambda _: None)
    with pytest.raises(PermissionError):
        write_json(path, {'rows': 2})
    assert len(attempts) == 8
    assert json.loads(path.read_text()) == {'rows': 1}
    assert json.loads(path.with_suffix('.json.tmp').read_text()) == {'rows': 2}
