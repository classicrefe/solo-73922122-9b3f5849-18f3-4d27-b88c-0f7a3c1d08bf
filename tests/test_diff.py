"""差异导出核心测试：变更识别 / 变更组 / 退回 / 确定性 / 幂等登记。

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
    KIND_DUP_POS, KIND_MISSING, KIND_ORPHAN,
    DiffRegistry, diff_packages, restore_bundle, split_jsonl,
    validate_batch_no,
)


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def make_zip(zip_path, tables):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, rows in tables.items():
            zf.writestr(name, "".join(
                json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="t-diff-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def package(self, name, records):
        src = self.tmp / f"{name}.jsonl"
        write_jsonl(src, records)
        res = split_jsonl(src, self.tmp / f"{name}-split")
        self.assertTrue(res.ok, res.errors.as_dict())
        return res.zip_path

    def diff(self, base_records, cur_records, name="d"):
        b = self.package(name + "-base", base_records)
        c = self.package(name + "-cur", cur_records)
        return diff_packages(b, c, self.tmp / (name + "-work"))


BASE = [
    {"inspect_no": "S-1", "bridge": "甲桥",
     "points": [{"pos": "p0", "v": 1}, {"pos": "p1", "v": 2},
                {"pos": "p2", "v": 3}],
     "photos": [{"file": "a.jpg"}]},
    {"inspect_no": "S-2", "bridge": "乙桥", "points": []},
    {"inspect_no": "S-3", "bridge": "丙桥", "photos": None},
    {"inspect_no": "S-4", "bridge": "丁桥"},
]


class ChangeDetectionTests(Base):
    def test_added_modified_deleted_in_one_group(self):
        cur = [
            # 修改：主记录 + 测点修改/删除 + 照片修改/新增
            {"inspect_no": "S-1", "bridge": "甲桥（复测）",
             "points": [{"pos": "p0", "v": 9}, {"pos": "p3", "v": 4}],
             "photos": [{"file": "a2.jpg"}, {"file": "b.jpg"}]},
            {"inspect_no": "S-2", "bridge": "乙桥", "points": []},  # 不变
            # S-3 被删除
            {"inspect_no": "S-4", "bridge": "丁桥"},               # 不变
            # 新增
            {"inspect_no": "S-5", "bridge": "戊桥",
             "points": [{"pos": "n0"}, {"pos": "n1"}], "photos": []},
        ]
        res = self.diff(BASE, cur)
        self.assertTrue(res.ok, res.errors.as_dict())
        groups = read_jsonl(res.out_path)
        self.assertEqual([g["inspect_no"] for g in groups], ["S-1", "S-3", "S-5"])
        by_no = {g["inspect_no"]: g for g in groups}

        g1 = by_no["S-1"]
        self.assertEqual(g1["change"], "modified")
        self.assertEqual(g1["before"]["fields"]["bridge"], "甲桥")
        self.assertEqual(g1["after"]["fields"]["bridge"], "甲桥（复测）")
        # 主记录与两类明细同在一组
        self.assertEqual(
            [(c["seq"], c["change"]) for c in g1["points"]],
            [(0, "modified"), (1, "modified"), (2, "deleted")])
        self.assertEqual(g1["points"][0]["before"], {"pos": "p0", "v": 1})
        self.assertEqual(g1["points"][0]["after"], {"pos": "p0", "v": 9})
        self.assertEqual(g1["points"][1]["before"], {"pos": "p1", "v": 2})
        self.assertEqual(g1["points"][1]["after"], {"pos": "p3", "v": 4})
        self.assertEqual(g1["points"][2]["before"], {"pos": "p2", "v": 3})
        self.assertNotIn("after", g1["points"][2])
        self.assertEqual(
            [(c["seq"], c["change"]) for c in g1["photos"]],
            [(0, "modified"), (1, "added")])
        self.assertEqual(g1["photos"][1]["after"], {"file": "b.jpg"})

        g3 = by_no["S-3"]
        self.assertEqual(g3["change"], "deleted")
        self.assertIsNone(g3["after"])
        self.assertEqual(g3["before"]["fields"]["bridge"], "丙桥")

        g5 = by_no["S-5"]
        self.assertEqual(g5["change"], "added")
        self.assertIsNone(g5["before"])
        self.assertEqual([c["seq"] for c in g5["points"]], [0, 1])
        self.assertTrue(all("after" in c and "before" not in c
                            for c in g5["points"]))

    def test_deleted_record_keeps_detail_values_and_seq(self):
        res = self.diff(BASE, [
            {"inspect_no": "S-2", "bridge": "乙桥", "points": []},
            {"inspect_no": "S-3", "bridge": "丙桥", "photos": None},
            {"inspect_no": "S-4", "bridge": "丁桥"},
        ])
        self.assertTrue(res.ok, res.errors.as_dict())
        groups = read_jsonl(res.out_path)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual((g["inspect_no"], g["change"]), ("S-1", "deleted"))
        self.assertEqual([(c["seq"], c["change"], c["before"])
                          for c in g["points"]],
                         [(0, "deleted", {"pos": "p0", "v": 1}),
                          (1, "deleted", {"pos": "p1", "v": 2}),
                          (2, "deleted", {"pos": "p2", "v": 3})])
        self.assertEqual(g["photos"][0]["seq"], 0)
        self.assertEqual(g["photos"][0]["before"], {"file": "a.jpg"})

    def test_identical_packages_produce_empty_stream(self):
        res = self.diff(BASE, BASE)
        self.assertTrue(res.ok, res.errors.as_dict())
        self.assertEqual(read_jsonl(res.out_path), [])
        self.assertEqual(res.stats["groups"], 0)
        self.assertEqual(res.stats["unchanged_records"], len(BASE))

    def test_deterministic_order_independent_of_table_row_order(self):
        cur = BASE + [{"inspect_no": "S-0", "bridge": "零桥", "points": []}]
        b = self.package("base", BASE)
        c = self.package("cur", cur)
        r1 = diff_packages(b, c, self.tmp / "d1")
        self.assertTrue(r1.ok, r1.errors.as_dict())
        # 把两个包内各表行序彻底打乱后重跑
        def shuffle_pkg(src, dst):
            with zipfile.ZipFile(src) as zf:
                tables = {}
                for name in ("main.jsonl", "points.jsonl", "photos.jsonl"):
                    rows = [json.loads(line) for line in
                            zf.read(name).decode().splitlines() if line.strip()]
                    random.Random(hash(name) & 0xFFFF).shuffle(rows)
                    tables[name] = rows
            make_zip(dst, tables)

        b2 = self.tmp / "base-shuffled.zip"
        c2 = self.tmp / "cur-shuffled.zip"
        shuffle_pkg(b, b2)
        shuffle_pkg(c, c2)
        r2 = diff_packages(b2, c2, self.tmp / "d2")
        self.assertTrue(r2.ok, r2.errors.as_dict())

        raw1 = Path(r1.out_path).read_bytes()
        raw2 = Path(r2.out_path).read_bytes()
        self.assertEqual(raw1, raw2)
        nos = [g["inspect_no"] for g in read_jsonl(r1.out_path)]
        self.assertEqual(nos, sorted(nos))

    def test_same_content_repacked_has_same_digest_and_diff(self):
        # 同一批数据换一种打包（表内行序打乱 / 重新压缩）应视为相同两包
        b = self.package("base-dig", BASE)
        r1 = restore_bundle(b, self.tmp / "b1", emit_jsonl=False)
        reshuffled = self.tmp / "repacked.zip"
        with zipfile.ZipFile(b) as zf:
            tables = {}
            for name in ("main.jsonl", "points.jsonl", "photos.jsonl"):
                rows = [json.loads(line) for line in
                        zf.read(name).decode().splitlines() if line.strip()]
                rows.reverse()
                tables[name] = rows
        make_zip(reshuffled, tables)
        r2 = restore_bundle(reshuffled, self.tmp / "b2", emit_jsonl=False)
        self.assertTrue(r1.ok and r2.ok)
        self.assertEqual(r1.digest, r2.digest)
        self.assertNotEqual(Path(b).read_bytes(), reshuffled.read_bytes())

    def test_type_only_change_is_modified(self):
        # Python 里 True == 1、1 == 1.0；差异判定必须能识别类型变化
        res = self.diff(
            [{"inspect_no": "T-1", "points": [{"v": 1}, {"ok": True}]}],
            [{"inspect_no": "T-1", "points": [{"v": 1.0}, {"ok": 1}]}],
        )
        self.assertTrue(res.ok, res.errors.as_dict())
        groups = read_jsonl(res.out_path)
        self.assertEqual(len(groups), 1)
        changes = {c["seq"]: c for c in groups[0]["points"]}
        self.assertEqual(changes[0]["change"], "modified")
        self.assertEqual(changes[0]["before"], {"v": 1})
        self.assertEqual(changes[0]["after"], {"v": 1.0})
        self.assertEqual(changes[1]["change"], "modified")

    def test_meta_state_change_counts_as_modification(self):
        res = self.diff(
            [{"inspect_no": "M-1", "points": []}],
            [{"inspect_no": "M-1"}],  # points 由 [] 变为缺省
        )
        self.assertTrue(res.ok, res.errors.as_dict())
        groups = read_jsonl(res.out_path)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["change"], "modified")
        self.assertEqual(groups[0]["before"]["lists"]["points"], "list")
        self.assertEqual(groups[0]["after"]["lists"]["points"], "absent")


class RejectionTests(Base):
    def test_bad_baseline_rejects_whole_export(self):
        good = self.package("good", BASE)
        bad = self.tmp / "bad.zip"
        make_zip(bad, {
            "main.jsonl": [
                {"inspect_no": "S-1", "_meta": {"points": "list", "points_len": 2,
                                                "photos": "absent", "photos_len": 0}}],
            "points.jsonl": [
                {"_no": "S-1", "_seq": 0},
                {"_no": "S-1", "_seq": 2}],   # 缺 1 + 多 2
            "photos.jsonl": [],
        })
        res = diff_packages(bad, good, self.tmp / "rj")
        self.assertFalse(res.ok)
        self.assertIsNone(res.out_path)
        kinds = set(res.errors.as_dict()["by_kind"])
        self.assertIn(KIND_MISSING, kinds)

    def test_orphan_in_current_rejects(self):
        good = self.package("good2", BASE)
        bad = self.tmp / "bad2.zip"
        make_zip(bad, {
            "main.jsonl": [
                {"inspect_no": "S-1", "_meta": {"points": "list", "points_len": 1,
                                                "photos": "absent", "photos_len": 0}}],
            "points.jsonl": [
                {"_no": "S-1", "_seq": 0},
                {"_no": "GHOST", "_seq": 0}],
            "photos.jsonl": [],
        })
        res = diff_packages(good, bad, self.tmp / "rj2")
        self.assertFalse(res.ok)
        kinds = set(res.errors.as_dict()["by_kind"])
        self.assertIn(KIND_ORPHAN, kinds)
        # 错误信息标明来源包
        tables = {i["table"] for i in res.errors.as_dict()["items"]}
        self.assertTrue(any(t.startswith("current/") for t in tables), tables)

    def test_ambiguous_duplicate_seq_rejects(self):
        good = self.package("good3", BASE)
        bad = self.tmp / "bad3.zip"
        make_zip(bad, {
            "main.jsonl": [
                {"inspect_no": "S-1", "_meta": {"points": "list", "points_len": 2,
                                                "photos": "absent", "photos_len": 0}}],
            "points.jsonl": [
                {"_no": "S-1", "_seq": 0, "pos": "a"},
                {"_no": "S-1", "_seq": 0, "pos": "b"}],
            "photos.jsonl": [],
        })
        res = diff_packages(good, bad, self.tmp / "rj3")
        self.assertFalse(res.ok)
        self.assertIn(KIND_DUP_POS, set(res.errors.as_dict()["by_kind"]))

    def test_both_packages_errors_merged(self):
        bad1 = self.tmp / "b1.zip"
        bad2 = self.tmp / "b2.zip"
        bad1.write_bytes(b"nope")
        bad2.write_bytes(b"also nope")
        res = diff_packages(bad1, bad2, self.tmp / "rj4")
        self.assertFalse(res.ok)
        self.assertGreaterEqual(res.errors.by_kind["包错误"], 2)


class BatchNoTests(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(validate_batch_no("  EXP-1  "), ("EXP-1", None))
        self.assertEqual(validate_batch_no("导出-2026"), ("导出-2026", None))
        for bad in ("", "   ", None, 123, "a" * 65, "bad\nno", "x\x00y"):
            got, err = validate_batch_no(bad)
            self.assertIsNone(got)
            self.assertTrue(err)


class RegistryTests(Base):
    def test_replay_same_packages_and_conflict_on_new_packages(self):
        reg = DiffRegistry(self.tmp / "registry.sqlite3")
        self.assertIsNone(reg.get("EXP-1"))
        record, created = reg.add("EXP-1", "a" * 64, "b" * 64, "t" * 32,
                                  {"groups": 3}, "r" * 64)
        self.assertTrue(created)
        self.assertEqual(record["token"], "t" * 32)
        # 重复登记同一单号：返回已有记录，不覆盖
        record2, created2 = reg.add("EXP-1", "a" * 64, "b" * 64, "u" * 32,
                                    {"groups": 9}, "s" * 64)
        self.assertFalse(created2)
        self.assertEqual(record2["token"], "t" * 32)
        self.assertEqual(record2["stats"], {"groups": 3})
        # forget 后单号可重新登记
        reg.forget("EXP-1", "t" * 32)
        self.assertIsNone(reg.get("EXP-1"))


class MemorySmokeTests(Base):
    def test_diff_large_packages_bounded_memory(self):
        n = 4000
        base = [{"inspect_no": f"L-{i:06d}", "bridge": "桥",
                 "points": [{"pos": "a"}, {"pos": "b"}],
                 "photos": [{"file": "x.jpg"}]} for i in range(n)]
        cur = [r for i, r in enumerate(base) if i % 10]
        cur += [{"inspect_no": f"L-{i:06d}", "bridge": "桥",
                 "points": [{"pos": "a"}], "photos": []}
                for i in range(n, n + 200)]
        b = self.package("m-base", base)
        c = self.package("m-cur", cur)

        tracemalloc.start()
        res = diff_packages(b, c, self.tmp / "md")
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        self.assertTrue(res.ok, res.errors.as_dict())
        self.assertEqual(res.stats["groups"], n // 10 + 200)  # 删除 + 新增，其余不变
        limit = 40 * 1024 * 1024
        self.assertLess(peak, limit, f"diff peak {peak/1e6:.1f} MB")
        print(f"\n[memory-smoke-diff] n={n} peak={peak/1048576:.1f}MiB "
              f"groups={res.stats['groups']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
