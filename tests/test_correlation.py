import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from badapple_earth.production.correlation.features import multiscale, representation
from badapple_earth.production.correlation.search import update_topk
from badapple_earth.production.correlation.rerank import choose
from badapple_earth.matching.photos import read_rgb_rows
from badapple_earth.production.search import resolve


def test_signed_correlations_preserve_structure_and_reject_polarity_inversion():
    image = np.zeros((24, 32), dtype=np.float32)
    image[3:20, 8:24] = 1
    z, g = representation(np.stack([image, .2+.6*image, 1-image, np.zeros_like(image)]))
    for vectors in (z, g):
        assert vectors[0] @ vectors[1] == pytest.approx(1, abs=2e-6)
        assert vectors[0] @ vectors[2] == pytest.approx(-1, abs=2e-6)
        assert np.all(vectors[3] == 0)
    multi = multiscale([image[None], image[None, ::2, ::2]])
    assert multi[0][0] @ multi[0][0] == pytest.approx(1, abs=2e-6)


def test_floating_topk_keeps_fractional_similarity_and_global_ties_across_batches():
    scores = np.full((1, 3), -np.inf, np.float32)
    positions = np.full((1, 3), -1, np.int32)
    ranks = np.full((1, 3), np.iinfo(np.int32).max, np.int32)
    values = np.array([[.32, .71, .70, -.4, .71]], dtype=np.float32)
    ties = np.array([5, 3, 0, 1, 2], dtype=np.int32)
    update_topk(values[:, :3], ties[:3], 0, scores, positions, ranks)
    update_topk(values[:, 3:], ties[3:], 3, scores, positions, ranks)
    assert positions.tolist() == [[4, 1, 2]]
    assert scores[0].tolist() == pytest.approx([.71, .71, .70])


def test_visibility_guard_and_near_structure_brightness_selection():
    config = {"contrast_min": .08, "photo_std_min": .02, "shape_window": .01}
    def row(position, shape, contrast, mse):
        return {"dense_row": position, "structure": shape, "contrast": contrast,
                "photo_std": .2, "zncc": .9, "full_mse": mse, "view_id": str(position)}
    rows = [row(100, 1, .01, .01), row(200, .99, .2, .4), row(300, .985, .2, .2)]
    assert choose(rows, "structure", config, 50) == (300, "visible_structure_then_brightness")
    assert choose(rows[:1], "structure", config, 50) == (50, "no_visible_positive_contrast_ms_ssim20")
    assert choose(rows, "structure", config, 50, flat=True) == (50, "flat_target_mse")


def test_retrieved_metadata_tracks_original_rows_after_exclusions_and_reordering(tmp_path):
    pq.write_table(pa.Table.from_pylist([
        {"tile_id": "a", "feature_row": 0, "window": [1, 2, 3, 4]},
        {"tile_id": "excluded", "feature_row": 1, "window": [5, 6, 7, 8]},
        {"tile_id": "c", "feature_row": 2, "window": [9, 10, 11, 12]},
    ]), tmp_path / "tiles.parquet")
    np.save(tmp_path / "source_rows.npy", np.array([2, 0], dtype=np.int32))
    manifest = {"blocks": [{"start": 0, "stop": 2, "path": "tiles.parquet", "member": "source"}]}
    rows = resolve(tmp_path, manifest, np.array([1, 0]))
    assert rows[0]["tile_id"] == "c"
    assert rows[0]["base_feature_row"] == 2
    assert rows[0]["view_source_window"] == [9., 10., 11., 12.]
    assert rows[1]["tile_id"] == "a"
    assert rows[1]["index_member"] == "source"


def test_bulk_rgb_row_reads_preserve_exact_source_pixels(tmp_path):
    images = np.arange(5*9*12*3, dtype=np.uint16).astype(np.uint8).reshape(5, 9, 12, 3)
    path = tmp_path / "rgb.npy"
    np.save(path, images)
    chosen = read_rgb_rows(path, [4, 1, 4])
    assert set(chosen) == {1, 4}
    assert np.array_equal(chosen[1], images[1])
    assert np.array_equal(chosen[4], images[4])
