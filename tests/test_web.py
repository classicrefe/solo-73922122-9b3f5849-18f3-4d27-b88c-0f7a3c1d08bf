"""Web 层测试（Flask test client），覆盖 HTML 与 JSON 两种响应。

运行方式（仓库根目录）：
    .venv/bin/python -m unittest discover -s tests -v
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["APP_WORK_DIR"] = tempfile.mkdtemp(prefix="t-web-")

from app.web import create_app  # noqa: E402

RECORDS = [
    {"inspect_no": "W-1", "points": [{"position": "p0"}, {"position": "p1"}],
     "photos": []},
    {"inspect_no": "W-2", "photos": None},
]


def jsonl_bytes(records):
    return "".join(json.dumps(r, ensure_ascii=False) + "\n"
                   for r in records).encode("utf-8")


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(os.environ["APP_WORK_DIR"], ignore_errors=True)

    def _split(self, data, query=""):
        return self.client.post("/split" + query, data={
            "file": (io.BytesIO(data), "inspections.jsonl"),
        }, content_type="multipart/form-data")

    def test_index_page(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("拆表".encode(), r.data)
        self.assertIn("64 KiB".encode(), r.data)

    def test_split_html_and_download(self):
        r = self._split(jsonl_bytes(RECORDS))
        self.assertEqual(r.status_code, 200)
        self.assertIn("下载表包".encode(), r.data)
        # JSON 模式拿到下载链接，验证 ZIP 可下载且含三表
        rj = self._split(jsonl_bytes(RECORDS), "?format=json")
        payload = rj.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["stats"]["points_rows"], 2)
        dl = self.client.get(payload["download_url"])
        self.assertEqual(dl.status_code, 200)
        zf = zipfile.ZipFile(io.BytesIO(dl.data))
        self.assertEqual(sorted(zf.namelist()),
                         ["main.jsonl", "photos.jsonl", "points.jsonl"])
        # 还原该 ZIP，语义比对
        rr = self.client.post("/restore?format=json", data={
            "file": (io.BytesIO(dl.data), "tables.zip"),
        }, content_type="multipart/form-data")
        rp = rr.get_json()
        self.assertTrue(rp["ok"], rp)
        out = self.client.get(rp["download_url"])
        got = {r["inspect_no"]: r for r in
               (json.loads(l) for l in out.data.decode().splitlines())}
        for orig in RECORDS:
            self.assertEqual(got[orig["inspect_no"]], orig)

    def test_restore_rejects_junk_html(self):
        # 回归：HTML 错误报告模板曾把 errors.items 解析成 dict 方法
        r = self.client.post("/restore", data={
            "file": (io.BytesIO(b"not a zip"), "junk.zip"),
        }, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 422)
        self.assertIn("整包已退回".encode(), r.data)
        self.assertIn("不是有效的 ZIP 文件".encode(), r.data)

    def test_restore_rejects_orphan_json(self):
        main = [{"inspect_no": "W-1",
                 "_meta": {"points": "list", "points_len": 1,
                           "photos": "absent", "photos_len": 0}}]
        points = [{"_no": "W-1", "_seq": 0}, {"_no": "GHOST", "_seq": 0}]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("main.jsonl", jsonl_bytes(main).decode())
            zf.writestr("points.jsonl", jsonl_bytes(points).decode())
            zf.writestr("photos.jsonl", "")
        r = self.client.post("/restore?format=json", data={
            "file": (buf.getvalue() and io.BytesIO(buf.getvalue()), "bad.zip"),
        }, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 422)
        payload = r.get_json()
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload["download_url"])
        self.assertIn("无主行", payload["errors"]["by_kind"])

    def test_split_rejects_duplicate_no(self):
        r = self._split(jsonl_bytes([{"inspect_no": "D"}, {"inspect_no": "D"}]),
                        "?format=json")
        self.assertEqual(r.status_code, 422)
        self.assertIn("重复巡检号", r.get_json()["errors"]["by_kind"])

    def test_download_token_validation(self):
        self.assertEqual(self.client.get("/download/..%2Fetc").status_code, 404)
        self.assertEqual(self.client.get("/download/nothex").status_code, 404)
        self.assertEqual(self.client.get("/download/" + "0" * 32).status_code, 404)

    # ------------------------------------------------------------------
    # 差异导出
    # ------------------------------------------------------------------
    def _package(self, records):
        r = self._split(jsonl_bytes(records), "?format=json")
        self.assertEqual(r.status_code, 200, r.get_json())
        return self.client.get(r.get_json()["download_url"]).data

    def _diff(self, base_zip, cur_zip, batch_no="EXP-1", query="?format=json",
              with_batch=True):
        data = {
            "baseline": (io.BytesIO(base_zip), "baseline.zip"),
            "current": (io.BytesIO(cur_zip), "current.zip"),
        }
        if with_batch:
            data["batch_no"] = batch_no
        return self.client.post("/diff" + query, data=data,
                                content_type="multipart/form-data")

    def test_index_has_diff_entry(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("差异导出".encode(), r.data)
        self.assertIn(b'action="/diff"', r.data)
        self.assertIn(b'name="baseline"', r.data)
        self.assertIn(b'name="current"', r.data)
        self.assertIn(b'name="batch_no"', r.data)
        self.assertIn(b'href="/examples/inspections-current.jsonl"', r.data)

    def test_example_downloads(self):
        for name in ("inspections.jsonl", "inspections-current.jsonl"):
            r = self.client.get(f"/examples/{name}")
            self.assertEqual(r.status_code, 200)
            records = [json.loads(line) for line in r.data.decode().splitlines()
                       if line.strip()]
            self.assertTrue(records)
        self.assertEqual(self.client.get("/examples/..%2Fapp%2Fcore.py").status_code, 404)
        self.assertEqual(self.client.get("/examples/nope.jsonl").status_code, 404)

    def test_diff_success_download_and_replay(self):
        base = RECORDS
        cur = [
            {"inspect_no": "W-1", "points": [{"position": "p0"},
                                             {"position": "p1b"}],
             "photos": []},
            {"inspect_no": "W-3", "photos": None},
        ]
        bz = self._package(base)
        cz = self._package(cur)

        r = self._diff(bz, cz, "EXP-WEB-1")
        self.assertEqual(r.status_code, 200, r.get_json())
        payload = r.get_json()
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["replayed"])
        self.assertIsNotNone(payload["download_url"])
        self.assertEqual(payload["stats"]["groups"], 3)

        dl = self.client.get(payload["download_url"])
        self.assertEqual(dl.status_code, 200)
        groups = [json.loads(line) for line in dl.data.decode().splitlines()]
        by_no = {g["inspect_no"]: g for g in groups}
        self.assertEqual(sorted(by_no), ["W-1", "W-2", "W-3"])
        self.assertEqual(by_no["W-1"]["change"], "modified")
        self.assertEqual(by_no["W-1"]["points"],
                         [{"seq": 1, "change": "modified",
                           "before": {"position": "p1"},
                           "after": {"position": "p1b"}}])
        self.assertEqual(by_no["W-2"]["change"], "deleted")
        self.assertEqual(by_no["W-2"]["before"]["fields"]["inspect_no"], "W-2")
        self.assertIsNone(by_no["W-2"]["after"])
        self.assertEqual(by_no["W-3"]["change"], "added")
        self.assertIsNone(by_no["W-3"]["before"])

        # 同一单号 + 相同两包：返回原结果，内容逐字节一致
        r2 = self._diff(bz, cz, "EXP-WEB-1")
        self.assertEqual(r2.status_code, 200)
        p2 = r2.get_json()
        self.assertTrue(p2["replayed"])
        self.assertEqual(p2["download_url"], payload["download_url"])
        self.assertEqual(p2["result_sha256"], payload["result_sha256"])
        dl2 = self.client.get(p2["download_url"])
        self.assertEqual(dl2.data, dl.data)

    def test_diff_replay_after_restart_same_workdir(self):
        bz = self._package(RECORDS)
        cz = self._package(RECORDS)
        r1 = self._diff(bz, cz, "EXP-WEB-RESTART").get_json()
        # 模拟进程重启：同一 APP_WORK_DIR 新建应用实例
        app2 = create_app()
        client2 = app2.test_client()
        r2 = client2.post("/diff?format=json", data={
            "baseline": (io.BytesIO(bz), "b.zip"),
            "current": (io.BytesIO(cz), "c.zip"),
            "batch_no": "EXP-WEB-RESTART",
        }, content_type="multipart/form-data")
        self.assertEqual(r2.status_code, 200)
        p2 = r2.get_json()
        self.assertTrue(p2["replayed"])
        self.assertEqual(p2["download_url"], r1["download_url"])

    def test_diff_replay_when_packages_repacked_same_content(self):
        # 同一批数据另一种打包（表内行序打乱）应仍视为相同两包 -> 重放
        bz = self._package(RECORDS)
        cz = self._package(RECORDS)
        first = self._diff(bz, cz, "EXP-WEB-REPACK").get_json()
        self.assertFalse(first["replayed"])

        def repack(data):
            buf = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                tables = {}
                for name in ("main.jsonl", "points.jsonl", "photos.jsonl"):
                    rows = list(reversed([
                        line for line in zf.read(name).decode().splitlines()
                        if line.strip()]))
                    tables[name] = "\n".join(rows) + "\n" if rows else ""
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as out:
                for name, text in tables.items():
                    out.writestr(name, text)
            return buf.getvalue()

        r = self._diff(repack(bz), repack(cz), "EXP-WEB-REPACK")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["replayed"])
        self.assertEqual(r.get_json()["download_url"], first["download_url"])

    def test_diff_conflict_when_packages_change(self):
        bz = self._package(RECORDS)
        cz = self._package(RECORDS)
        self.assertEqual(self._diff(bz, cz, "EXP-WEB-2").status_code, 200)
        other = self._package([{"inspect_no": "W-9", "points": []}])
        r = self._diff(bz, other, "EXP-WEB-2")
        self.assertEqual(r.status_code, 409)
        payload = r.get_json()
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload["download_url"])
        self.assertIn("导出单号冲突", payload["errors"]["by_kind"])

    def test_diff_rejects_bad_package_without_download(self):
        bz = self._package(RECORDS)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("main.jsonl", jsonl_bytes([
                {"inspect_no": "W-1", "_meta": {"points": "list", "points_len": 2,
                                                "photos": "absent", "photos_len": 0}}]
            ).decode())
            zf.writestr("points.jsonl", jsonl_bytes([
                {"_no": "W-1", "_seq": 0}]).decode())
            zf.writestr("photos.jsonl", "")
        r = self._diff(bz, buf.getvalue(), "EXP-WEB-BAD")
        self.assertEqual(r.status_code, 422)
        payload = r.get_json()
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload["download_url"])
        self.assertIn("缺位明细", payload["errors"]["by_kind"])
        # 失败的导出不占用单号：修好包后同一单号可成功
        good = self._diff(bz, self._package(RECORDS), "EXP-WEB-BAD")
        self.assertEqual(good.status_code, 200)
        self.assertTrue(good.get_json()["ok"])

    def test_diff_rejects_missing_batch_no_and_bad_batch_no(self):
        bz = self._package(RECORDS)
        r = self._diff(bz, bz, with_batch=False)
        self.assertEqual(r.status_code, 400)
        self.assertIn("导出单号", r.get_json()["message"])
        r2 = self._diff(bz, bz, "bad\nno")
        self.assertEqual(r2.status_code, 400)
        self.assertIn("导出单号非法", r2.get_json()["errors"]["by_kind"])

    def test_diff_rejects_missing_file(self):
        bz = self._package(RECORDS)
        r = self.client.post("/diff?format=json", data={
            "baseline": (io.BytesIO(bz), "b.zip"),
            "batch_no": "EXP-WEB-3",
        }, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(r.get_json()["download_url"])

    def test_diff_html_report(self):
        bz = self._package(RECORDS)
        cz = self._package(RECORDS)
        ok = self._diff(bz, cz, "EXP-WEB-HTML", query="")
        self.assertEqual(ok.status_code, 200)
        self.assertIn("下载增量 JSONL".encode(), ok.data)
        self.assertIn("EXP-WEB-HTML".encode(), ok.data)
        bad = self._diff(bz, b"NOPE", "EXP-WEB-HTML-BAD", query="")
        self.assertEqual(bad.status_code, 422)
        self.assertIn("整单已退回".encode(), bad.data)
        self.assertIn("包错误".encode(), bad.data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
