"""核心逻辑测试：拆表 / 还原 / 校验 / 内存冒烟。

运行方式（仓库根目录）：
    .venv/bin/python -m unittest discover -s tests -v
"""
import json
import random
import shutil
import sys
import tempfile
import tracemalloc
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import (  # noqa: E402
    KIND_DUP_NO, KIND_DUP_POS, KIND_EXTRA, KIND_LINE_TOO_LONG, KIND_MISSING,
    KIND_ORPHAN, KIND_PACKAGE, KIND_STRUCT, MAX_LINE_BYTES,
    restore_zip, split_jsonl,
)


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in records:
            if isinstance(r, str):
                f.write(r + "\n")
            else:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def extract_zip(zip_path, dest):
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)


def make_zip(zip_path, tables: dict):
    """tables: {entry_name: [row_dict, ...]}"""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, rows in tables.items():
            data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
            zf.writestr(name, data)


def meta(points_state, points_len, photos_state, photos_len):
    return {"points": points_state, "points_len": points_len,
            "photos": photos_state, "photos_len": photos_len}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="t-core-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def split(self, records):
        src = self.tmp / "in.jsonl"
        write_jsonl(src, records)
        return split_jsonl(src, self.tmp / "split")

    def restore_tables(self, tables):
        pkg = self.tmp / "pkg.zip"
        make_zip(pkg, tables)
        return restore_zip(pkg, self.tmp / "restore")


class RoundTripTests(Base):
    SAMPLE = [
        {"inspect_no": "A-1", "bridge": "青云江大桥",
         "points": [{"position": "主梁", "value": 12.3},
                    {"position": "桥墩", "value": 4.2}],
         "photos": [{"position": "主梁", "file": "a.jpg"}]},
        {"inspect_no": "A-2", "points": [], "photos": []},
        {"inspect_no": "A-3", "points": None, "photos": [{"file": "b.jpg"}]},
        {"inspect_no": "A-4", "photos": None},
        {"inspect_no": "A-5", "remark": "含中文<>&\"'",
         "points": [{"position": "塔顶", "value": None,
                     "tags": ["风振", "复测"], "meta": {"ok": True}}]},
    ]

    def test_roundtrip_preserves_semantics_and_states(self):
        res = self.split(self.SAMPLE)
        self.assertTrue(res.ok, res.errors.as_dict())
        out = self.tmp / "pkg"
        extract_zip(res.zip_path, out)
        self.assertEqual(
            sorted(p.name for p in out.iterdir()),
            ["main.jsonl", "photos.jsonl", "points.jsonl"])

        rres = restore_zip(res.zip_path, self.tmp / "r1")
        self.assertTrue(rres.ok, rres.errors.as_dict())
        restored = read_jsonl(rres.out_path)
        self.assertEqual(len(restored), len(self.SAMPLE))
        by_no = {r["inspect_no"]: r for r in restored}
        for orig in self.SAMPLE:
            got = by_no[orig["inspect_no"]]
            self.assertEqual(got, orig)  # dict 比较与键序无关
            # 缺省 / null / 空列表必须原样保留
            for f in ("points", "photos"):
                if f not in orig:
                    self.assertNotIn(f, got)
                elif orig[f] is None:
                    self.assertIn(f, got)
                    self.assertIsNone(got[f])
                elif orig[f] == []:
                    self.assertEqual(got[f], [])

    def test_list_order_preserved_with_shuffled_tables(self):
        records = [
            {"inspect_no": f"B-{i}",
             "points": [{"position": f"p{j}", "idx": j} for j in range(7)],
             "photos": [{"file": f"f{j}.jpg"} for j in range(5)]}
            for i in range(10)
        ]
        res = self.split(records)
        self.assertTrue(res.ok, res.errors.as_dict())
        # 重新打包：各表行序完全打乱（表包内各表行序不限）
        out = self.tmp / "pkg2"
        extract_zip(res.zip_path, out)
        tables = {}
        for name in ("main.jsonl", "points.jsonl", "photos.jsonl"):
            rows = read_jsonl(out / name)
            random.Random(42 + len(name)).shuffle(rows)
            tables[name] = rows
        pkg = self.tmp / "shuffled.zip"
        make_zip(pkg, tables)

        rres = restore_zip(pkg, self.tmp / "r2")
        self.assertTrue(rres.ok, rres.errors.as_dict())
        restored = {r["inspect_no"]: r for r in read_jsonl(rres.out_path)}
        self.assertEqual(len(restored), len(records))
        for orig in records:
            got = restored[orig["inspect_no"]]
            self.assertEqual(got["points"], orig["points"])  # 列表顺序一致
            self.assertEqual(got["photos"], orig["photos"])

    def test_empty_upload_ok(self):
        res = self.split([])
        self.assertTrue(res.ok, res.errors.as_dict())
        rres = restore_zip(res.zip_path, self.tmp / "r3")
        self.assertTrue(rres.ok, rres.errors.as_dict())
        self.assertEqual(read_jsonl(rres.out_path), [])


class SplitErrorTests(Base):
    def kinds(self, errors):
        return set(errors.as_dict()["by_kind"])

    def test_duplicate_inspect_no(self):
        res = self.split([{"inspect_no": "X"}, {"inspect_no": "X"}])
        self.assertFalse(res.ok)
        self.assertIn(KIND_DUP_NO, self.kinds(res.errors))

    def test_line_too_long(self):
        big = {"inspect_no": "BIG", "pad": "x" * (MAX_LINE_BYTES)}
        res = self.split([big])
        self.assertFalse(res.ok)
        self.assertIn(KIND_LINE_TOO_LONG, self.kinds(res.errors))

    def test_exactly_64kib_line_ok(self):
        # 内容（不含换行）恰好 64 KiB 应通过
        rec = {"inspect_no": "K", "pad": "y" * 10}
        line = json.dumps(rec, ensure_ascii=False)
        need = MAX_LINE_BYTES - len(line)
        rec["pad"] = "y" * (10 + need)
        self.assertEqual(len(json.dumps(rec, ensure_ascii=False).encode()),
                         MAX_LINE_BYTES)
        res = self.split([rec])
        self.assertTrue(res.ok, res.errors.as_dict())

    def test_bad_json_and_bad_types(self):
        src = self.tmp / "bad.jsonl"
        with open(src, "w", encoding="utf-8") as f:
            f.write("{not json}\n")
            f.write('["array"]\n')
            f.write('{"inspect_no": 123}\n')
            f.write('{"inspect_no": "S", "points": "oops"}\n')
            f.write('{"inspect_no": "T", "points": [1, 2]}\n')
            f.write('{"inspect_no": "U", "photos": [{"_seq": 0}]}\n')
        res = split_jsonl(src, self.tmp / "s")
        self.assertFalse(res.ok)
        kinds = self.kinds(res.errors)
        self.assertIn(KIND_STRUCT, kinds)
        self.assertGreaterEqual(res.errors.total, 6)

    def test_reserved_meta_key_rejected(self):
        res = self.split([{"inspect_no": "M", "_meta": {}}])
        self.assertFalse(res.ok)
        self.assertIn(KIND_STRUCT, self.kinds(res.errors))


class RestoreErrorTests(Base):
    def setUp(self):
        super().setUp()
        # 基准好包：两单，各两条测点一条照片
        self.main = [
            {"inspect_no": "R-1", "bridge": "甲桥",
             "_meta": meta("list", 2, "list", 1)},
            {"inspect_no": "R-2", "bridge": "乙桥",
             "_meta": meta("absent", 0, "null", 0)},
        ]
        self.points = [
            {"_no": "R-1", "_seq": 0, "position": "p0"},
            {"_no": "R-1", "_seq": 1, "position": "p1"},
        ]
        self.photos = [{"_no": "R-1", "_seq": 0, "file": "a.jpg"}]

    def kinds(self, errors):
        return set(errors.as_dict()["by_kind"])

    def test_orphan_row(self):
        pts = self.points + [{"_no": "GHOST", "_seq": 0, "position": "x"}]
        res = self.restore_tables({"main.jsonl": self.main,
                                   "points.jsonl": pts,
                                   "photos.jsonl": self.photos})
        self.assertFalse(res.ok)
        self.assertIn(KIND_ORPHAN, self.kinds(res.errors))
        self.assertIsNone(res.out_path)

    def test_duplicate_position(self):
        pts = self.points + [dict(self.points[1])]  # 重复 (R-1, seq=1)
        res = self.restore_tables({"main.jsonl": self.main,
                                   "points.jsonl": pts,
                                   "photos.jsonl": self.photos})
        self.assertFalse(res.ok)
        self.assertIn(KIND_DUP_POS, self.kinds(res.errors))

    def test_missing_detail(self):
        pts = self.points[:1]  # 少一条 -> 缺序号 1
        res = self.restore_tables({"main.jsonl": self.main,
                                   "points.jsonl": pts,
                                   "photos.jsonl": self.photos})
        self.assertFalse(res.ok)
        self.assertIn(KIND_MISSING, self.kinds(res.errors))

    def test_gap_detail(self):
        pts = [self.points[0], {"_no": "R-1", "_seq": 5, "position": "p5"}]
        res = self.restore_tables({"main.jsonl": self.main,
                                   "points.jsonl": pts,
                                   "photos.jsonl": self.photos})
        self.assertFalse(res.ok)
        kinds = self.kinds(res.errors)
        self.assertIn(KIND_MISSING, kinds)  # 缺 1
        self.assertIn(KIND_EXTRA, kinds)    # 多 5

    def test_extra_detail_for_absent_list(self):
        pts = self.points + [{"_no": "R-2", "_seq": 0, "position": "x"}]
        res = self.restore_tables({"main.jsonl": self.main,
                                   "points.jsonl": pts,
                                   "photos.jsonl": self.photos})
        self.assertFalse(res.ok)
        self.assertIn(KIND_EXTRA, self.kinds(res.errors))

    def test_duplicate_main_no(self):
        res = self.restore_tables({"main.jsonl": self.main + [self.main[0]],
                                   "points.jsonl": self.points,
                                   "photos.jsonl": self.photos})
        self.assertFalse(res.ok)
        self.assertIn(KIND_DUP_NO, self.kinds(res.errors))

    def test_bad_meta_and_bad_seq(self):
        bad_main = [{"inspect_no": "R-1", "_meta": {"points": "??"}}]
        res = self.restore_tables({"main.jsonl": bad_main,
                                   "points.jsonl": [],
                                   "photos.jsonl": []})
        self.assertFalse(res.ok)
        self.assertIn(KIND_STRUCT, self.kinds(res.errors))

        res2 = self.restore_tables({"main.jsonl": self.main,
                                    "points.jsonl": [{"_no": "R-1", "_seq": -1}],
                                    "photos.jsonl": self.photos})
        self.assertFalse(res2.ok)
        self.assertIn(KIND_STRUCT, self.kinds(res2.errors))

    def test_package_errors(self):
        # 非 ZIP
        junk = self.tmp / "junk.zip"
        junk.write_bytes(b"not a zip")
        res = restore_zip(junk, self.tmp / "rj")
        self.assertFalse(res.ok)
        self.assertIn(KIND_PACKAGE, self.kinds(res.errors))
        # 缺表
        res2 = self.restore_tables({"main.jsonl": self.main,
                                    "points.jsonl": self.points})
        self.assertFalse(res2.ok)
        self.assertIn(KIND_PACKAGE, self.kinds(res2.errors))

    def test_multiple_problems_reported_together(self):
        # R-1 测点声明 2 条实到 1 条（缺位）+ GHOST 无主行 + R-1 照片声明 1 条实到 0 条（缺位）
        pts = self.points[:1] + [{"_no": "GHOST", "_seq": 0}]
        res = self.restore_tables({"main.jsonl": self.main,
                                   "points.jsonl": pts,
                                   "photos.jsonl": []})
        kinds = self.kinds(res.errors)
        self.assertIn(KIND_ORPHAN, kinds)
        self.assertIn(KIND_MISSING, kinds)
        self.assertGreaterEqual(res.errors.by_kind[KIND_MISSING], 2)


class MemorySmokeTests(Base):
    def test_split_restore_large_file_bounded_memory(self):
        n = 20000
        src = self.tmp / "big.jsonl"
        with open(src, "w", encoding="utf-8") as f:
            for i in range(n):
                rec = {"inspect_no": f"L-{i:06d}", "bridge": "桥",
                       "points": [{"position": "p", "value": i % 97}],
                       "photos": []}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        tracemalloc.start()
        res = split_jsonl(src, self.tmp / "ms")
        self.assertTrue(res.ok, res.errors.as_dict())
        _, peak_split = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        tracemalloc.start()
        rres = restore_zip(res.zip_path, self.tmp / "mr")
        self.assertTrue(rres.ok, rres.errors.as_dict())
        _, peak_restore = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        self.assertEqual(rres.stats["restored_records"], n)
        # Python 侧分配应基本恒定（SQLite 自身缓存放磁盘/小页缓存，不在此统计内）
        limit = 40 * 1024 * 1024
        self.assertLess(peak_split, limit, f"split peak {peak_split/1e6:.1f} MB")
        self.assertLess(peak_restore, limit, f"restore peak {peak_restore/1e6:.1f} MB")
        print(f"\n[memory-smoke] n={n} "
              f"split_peak={peak_split/1048576:.1f}MiB "
              f"restore_peak={peak_restore/1048576:.1f}MiB")


if __name__ == "__main__":
    unittest.main(verbosity=2)
