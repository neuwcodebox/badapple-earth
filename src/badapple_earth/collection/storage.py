"""One writer, immutable shard publication, and recoverable SQLite checkpoints."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import uuid

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ..evidence import checksum, write_json
from ..features import EXTRACTION_VERSION, FEATURE_VERSION, image_size
from .windows import identity


@contextmanager
def writer_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "writer.lock").open("a+b") as handle:
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise ValueError("Another collector is writing this index") from error
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Index:
    def __init__(self, root: Path, config: dict):
        self.root = root
        for name in ["staging", "shards", "catalog", "cache/blocks"]:
            (root / name).mkdir(parents=True, exist_ok=True)
        self.config = config
        self.config_id = identity([config, EXTRACTION_VERSION, FEATURE_VERSION])
        path = root / "manifest.json"
        if path.exists():
            self.manifest = json.loads(path.read_text(encoding="utf-8"))
            runtime = {"max_memory_mb", "max_disk_mb", "min_free_disk_mb", "max_block_attempts"}
            saved = {key: value for key, value in self.manifest["config"].items() if key not in runtime}
            requested = {key: value for key, value in config.items() if key not in runtime}
            if (saved != requested or self.manifest["feature_version"] != FEATURE_VERSION
                    or self.manifest["extraction_version"] != EXTRACTION_VERSION):
                raise ValueError("Index configuration differs; use its original config or a new output path")
            # Resource budgets can change without changing the identity of saved pixels/features.
            self.config_id = self.manifest["config_id"]
            self.manifest["config"] = config
        else:
            self.manifest = {
                "schema": "feature-index-v1", "index_id": self.config_id,
                "config_id": self.config_id, "config": config,
                "feature_version": FEATURE_VERSION, "extraction_version": EXTRACTION_VERSION,
                "canonical_size": list(image_size(config["preview_width"], config["aspect_ratio"])),
                "coarse_shape": list(image_size(16, config["aspect_ratio"])[::-1]),
                "fine_shape": list(image_size(32, config["aspect_ratio"])[::-1]),
                "dtype": "uint8", "rows": 0, "shards": [],
            }
            write_json(path, self.manifest)
        self.db = sqlite3.connect(root / "jobs.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, plan TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
                error_type TEXT, next_retry TEXT);
            CREATE TABLE IF NOT EXISTS shards (shard_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS tiles (
                tile_id TEXT PRIMARY KEY, shard_id TEXT, feature_row INTEGER,
                aoi_id TEXT, terrain_group TEXT, scene_id TEXT, width INTEGER,
                left_px INTEGER, top_px INTEGER, height INTEGER, dhash TEXT, coarse BLOB,
                sample_key TEXT, sample_image TEXT, near_duplicate_of TEXT);
            CREATE INDEX IF NOT EXISTS tile_area_scale ON tiles(aoi_id, width);
            CREATE INDEX IF NOT EXISTS tile_neighbors ON tiles(scene_id, width, left_px, top_px);
            CREATE INDEX IF NOT EXISTS tile_sample ON tiles(sample_key);
            CREATE TABLE IF NOT EXISTS excluded (
                tile_id TEXT PRIMARY KEY, reason TEXT, metadata TEXT);
        """)
        self.recover()

    def close(self):
        self.db.close()

    def recover(self):
        # Only final directories are published. Interrupted staging is never searchable.
        known = {item["shard_id"] for item in self.manifest["shards"]}
        for directory in sorted((self.root / "shards").iterdir()):
            if not directory.is_dir() or directory.name in known:
                continue
            descriptor = json.loads((directory / "shard.json").read_text(encoding="utf-8"))
            if descriptor["config_id"] != self.config_id:
                raise ValueError("Orphan shard belongs to a different index")
            self.manifest["shards"].append(descriptor)
            known.add(descriptor["shard_id"])
        self.manifest["rows"] = sum(shard["rows"] for shard in self.manifest["shards"])
        write_json(self.root / "manifest.json", self.manifest)
        for descriptor in self.manifest["shards"]:
            if not self.db.execute("SELECT 1 FROM shards WHERE shard_id=?", (descriptor["shard_id"],)).fetchone():
                self._apply(descriptor)
        # The OS file lock proves the previous writer has exited; no timed lease guess.
        self.db.execute("UPDATE jobs SET state='PENDING' WHERE state='RUNNING'")
        self.db.commit()

    def register(self, plans: list[dict]):
        self.db.executemany("INSERT OR IGNORE INTO jobs(job_id,plan) VALUES(?,?)",
                            [(p["job_id"], json.dumps(p)) for p in plans])
        self.db.commit()

    def count(self, area=None, width=None, scene=None):
        clauses, params = [], []
        for column, value in [("aoi_id", area), ("width", width), ("scene_id", scene)]:
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return self.db.execute("SELECT count(*) FROM tiles" + where, params).fetchone()[0]

    def known(self, tile_id):
        return bool(self.db.execute("SELECT 1 FROM tiles WHERE tile_id=? UNION ALL SELECT 1 FROM excluded WHERE tile_id=? LIMIT 1",
                                    (tile_id, tile_id)).fetchone())

    def sample_cutoff(self):
        row = self.db.execute("SELECT sample_key FROM tiles ORDER BY sample_key LIMIT 1 OFFSET ?",
                              (self.config["report_samples"] - 1,)).fetchone()
        return row[0] if row else None

    def release_blocks(self, area_id):
        """Evict regenerable native blocks once an AOI pass has finished.

        The collector's sole writer calls this between AOIs. Accepted RGB and
        views remain in immutable shards; later quota increases can read new
        source windows again without retaining every unused native pixel.
        """
        if not self.config.get("cache_rgb", False):
            return
        released = 0
        for row in self.db.execute("SELECT job_id FROM jobs WHERE json_extract(plan, '$.aoi_id')=?", (area_id,)):
            path = self.root / "cache/blocks" / (row[0] + ".npz")
            if path.exists():
                released += path.stat().st_size
                path.unlink()
        if released:
            self.manifest["evicted_native_block_bytes"] = self.manifest.get("evicted_native_block_bytes", 0) + released
            # Account for the deletion at the next resource check.
            self.last_disk_scan = -float("inf")

    def near_duplicate(self, scene_id, window, dhash, coarse):
        left, top, width, height = window
        rows = self.db.execute(
            "SELECT tile_id,left_px,top_px,height,dhash,coarse FROM tiles "
            "WHERE scene_id=? AND width=? AND left_px BETWEEN ? AND ? AND top_px BETWEEN ? AND ? LIMIT 32",
            (scene_id, width, left - width // 2, left + width // 2, top - height // 2, top + height // 2))
        for row in rows:
            if (int(dhash, 16) ^ int(row["dhash"], 16)).bit_count() > 2:
                continue
            previous = np.frombuffer(row["coarse"], dtype=np.uint8).astype(np.float32)
            if float(np.mean((coarse.reshape(-1).astype(np.float32) - previous) ** 2)) <= 4:
                return row["tile_id"]
        return None

    def commit(self, plan, cursor, done, records, coarse, fine, rejected, images, rotations=None, photos=None):
        shard_id = identity([plan["job_id"], cursor, [r["tile_id"] for r in records], rejected])
        stage = self.root / "staging" / (shard_id + "-" + uuid.uuid4().hex)
        stage.mkdir()
        if records and photos is not None:
            width, height = self.manifest["canonical_size"]
            data = np.stack([np.asarray(photo) for photo in photos])
            if data.shape != (len(records), height, width, 3) or data.dtype != np.uint8:
                raise ValueError("RGB cache rows/shape/dtype do not match tile metadata")
            np.save(stage / "rgb.npy", data, allow_pickle=False)
            for position, row in enumerate(records):
                row.update(rgb_cache_ref=f"shards/{shard_id}/rgb.npy", rgb_cache_row=position)
        for index, row in enumerate(records):
            row.update(shard_id=shard_id, feature_row=index)
            if row["tile_id"] in images:
                filename = row["tile_id"] + ".png"
                images[row["tile_id"]].save(stage / filename)
                row["sample_image"] = f"shards/{shard_id}/{filename}"
        table = pa.Table.from_pylist(records) if records else pa.table({"tile_id": pa.array([], pa.string())})
        pq.write_table(table, stage / "tiles.parquet", compression="zstd")
        for name, values, shape in [("gray_coarse", coarse, self.manifest["coarse_shape"]),
                                    ("gray_fine", fine, self.manifest["fine_shape"])]:
            data = np.stack(values) if values else np.empty((0, *shape), dtype=np.uint8)
            if data.shape != (len(records), *shape) or data.dtype != np.uint8:
                raise ValueError("Feature rows/shape/dtype do not match tile metadata")
            np.save(stage / (name + ".npy"), data, allow_pickle=False)
        write_json(stage / "checkpoint.json", {"plan": plan, "cursor": cursor,
                   "state": "DONE" if done else "PENDING", "rejected": rejected})
        if rotations and records:
            from ..matching.views import write_view_shard
            write_view_shard(stage / "views", records, rotations["images"], rotations["scene"], self.manifest, shard_id)
        files = [p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file()]
        descriptor = {"shard_id": shard_id, "config_id": self.config_id, "rows": len(records),
                      "files": {name: checksum(stage / name) for name in files}}
        write_json(stage / "shard.json", descriptor)
        # Publication and SQLite are separate transactions. recover() bridges a crash.
        stage.rename(self.root / "shards" / shard_id)
        self.manifest["shards"].append(descriptor)
        self.manifest["rows"] += len(records)
        write_json(self.root / "manifest.json", self.manifest)
        self._apply(descriptor)

    def _apply(self, descriptor):
        directory = self.root / "shards" / descriptor["shard_id"]
        checkpoint = json.loads((directory / "checkpoint.json").read_text(encoding="utf-8"))
        records = pq.read_table(directory / "tiles.parquet").to_pylist()
        arrays = [np.load(directory / (name + ".npy"), mmap_mode="r", allow_pickle=False)
                  for name in ["gray_coarse", "gray_fine"]]
        for array, shape in zip(arrays, [self.manifest["coarse_shape"], self.manifest["fine_shape"]]):
            if array.shape != (descriptor["rows"], *shape) or array.dtype != np.uint8:
                raise ValueError("Shard feature shape or row mapping is damaged")
        if len(records) != descriptor["rows"]:
            raise ValueError("Shard metadata row count is damaged")
        plan = checkpoint["plan"]
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs(job_id,plan) VALUES(?,?)", (plan["job_id"], json.dumps(plan)))
            for index, row in enumerate(records):
                if row["feature_row"] != index or row["shard_id"] != descriptor["shard_id"]:
                    raise ValueError("Shard tile/feature row mapping is damaged")
                left, top, width, height = row["window"]
                self.db.execute("INSERT INTO tiles VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    row["tile_id"], row["shard_id"], index, row["aoi_id"], row["terrain_group"],
                    row["scene_id"], width, left, top, height, row["dhash"], arrays[0][index].tobytes(),
                    row["sample_key"], row["sample_image"], row["near_duplicate_of"]))
            for rejection in checkpoint["rejected"]:
                self.db.execute("INSERT OR IGNORE INTO excluded VALUES(?,?,?)", (
                    rejection["tile_id"], rejection["reason"], json.dumps(rejection)))
            self.db.execute("UPDATE jobs SET cursor=?,state=?,error_type=NULL WHERE job_id=?",
                            (checkpoint["cursor"], checkpoint["state"], plan["job_id"]))
            self.db.execute("INSERT INTO shards VALUES(?)", (descriptor["shard_id"],))

    def report(self):
        from ..evidence import contact_sheet
        rows = self.db.execute("SELECT * FROM tiles ORDER BY sample_key LIMIT ?", (self.config["report_samples"],)).fetchall()
        contact_sheet([(self.root / row["sample_image"], f"{row['aoi_id']} {row['width']}px") for row in rows],
                      self.root / "contact-sheet.jpg")
        for column, name in [("aoi_id", "areas"), ("terrain_group", "terrain_groups"), ("width", "scales"), ("scene_id", "scenes")]:
            counts = self.db.execute(f"SELECT {column},count(*) FROM tiles GROUP BY {column}").fetchall()
            self.manifest[name] = {str(row[0]): row[1] for row in counts}
        self.manifest["near_duplicate_count"] = self.db.execute("SELECT count(*) FROM tiles WHERE near_duplicate_of IS NOT NULL").fetchone()[0]
        self.manifest["exclusions"] = dict(self.db.execute("SELECT reason,count(*) FROM excluded GROUP BY reason").fetchall())
        self.manifest["report_tile_ids"] = [row["tile_id"] for row in rows]
        write_json(self.root / "manifest.json", self.manifest)
        return self.manifest
