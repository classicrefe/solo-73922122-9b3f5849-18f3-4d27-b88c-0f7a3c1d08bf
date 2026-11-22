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


if __name__ == "__main__":
    unittest.main(verbosity=2)
