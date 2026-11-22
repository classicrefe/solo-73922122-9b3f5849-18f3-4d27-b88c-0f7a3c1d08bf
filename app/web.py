"""桥梁巡检单拆表 / 还原 Web 服务。

页面与接口：
  GET  /                  双表单页面（拆表、还原）
  POST /split             上传巡检 JSONL -> 校验并生成表包 ZIP，给出下载链接
  POST /restore           上传表包 ZIP -> 校验整包，全部通过才生成还原 JSONL 下载链接；
                          任一校验失败则整包退回并列出问题
  GET  /download/<token>  下载已生成的结果文件（ZIP 或 JSONL）
  GET  /healthz           健康检查

所有接口加 ``?format=json``（或 Accept: application/json）返回 JSON，便于脚本集成。
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from flask import (Flask, abort, jsonify, render_template, request, send_file,
                   url_for)

from .core import MAX_LINE_BYTES, restore_zip, split_jsonl

TOKEN_RE = re.compile(r"[0-9a-f]{32}")

# token 后缀 -> (MIME, 下载文件名)
RESULT_KINDS = {
    ".zip": ("application/zip", "inspection-tables.zip"),
    ".jsonl": ("application/x-ndjson", "inspections-restored.jsonl"),
}


def create_app() -> Flask:
    app = Flask(__name__)
    work_root = Path(os.environ.get("APP_WORK_DIR", tempfile.gettempdir())) / "bridge-inspection"
    results_dir = work_root / "results"
    jobs_dir = work_root / "jobs"
    results_dir.mkdir(parents=True, exist_ok=True)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    # 上传总量安全阀（默认 1 GiB，可用环境变量调整）；单行 64 KiB 限制在核心层执行
    app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_BYTES", 1 << 30))

    # ------------------------------------------------------------------
    @app.get("/")
    def index():
        return render_template("index.html", max_line_kib=MAX_LINE_BYTES // 1024)

    @app.get("/healthz")
    def healthz():
        return "ok\n"

    @app.post("/split")
    def split_view():
        f = request.files.get("file")
        if f is None or f.filename == "":
            return _respond("split", None, None, "请选择要上传的巡检 JSONL 文件", 400)
        job = jobs_dir / uuid.uuid4().hex
        job.mkdir(parents=True)
        try:
            src = job / "upload.jsonl"
            _save_upload(f.stream, src)
            res = split_jsonl(src, job)
            if not res.ok:
                return _respond("split", res.errors, res.stats,
                                "拆表失败，未生成表包", 422)
            token = uuid.uuid4().hex
            shutil.move(res.zip_path, results_dir / f"{token}.zip")
            return _respond("split", res.errors, res.stats, None, 200,
                            download_url=url_for("download", token=token))
        finally:
            shutil.rmtree(job, ignore_errors=True)

    @app.post("/restore")
    def restore_view():
        f = request.files.get("file")
        if f is None or f.filename == "":
            return _respond("restore", None, None, "请选择要上传的表包 ZIP 文件", 400)
        job = jobs_dir / uuid.uuid4().hex
        job.mkdir(parents=True)
        try:
            pkg = job / "package.zip"
            _save_upload(f.stream, pkg)
            res = restore_zip(pkg, job)
            if not res.ok:
                # 整包退回：不生成任何可下载结果
                return _respond("restore", res.errors, res.stats,
                                "校验未通过，整包已退回", 422)
            # 验完整包后才开放还原文件下载
            token = uuid.uuid4().hex
            shutil.move(res.out_path, results_dir / f"{token}.jsonl")
            return _respond("restore", res.errors, res.stats, None, 200,
                            download_url=url_for("download", token=token))
        finally:
            shutil.rmtree(job, ignore_errors=True)

    @app.get("/download/<token>")
    def download(token):
        if not TOKEN_RE.fullmatch(token):
            abort(404)
        for suffix, (mime, name) in RESULT_KINDS.items():
            p = results_dir / f"{token}{suffix}"
            if p.exists():
                return send_file(p, mimetype=mime, as_attachment=True,
                                 download_name=name)
        abort(404)

    # ------------------------------------------------------------------
    def _respond(mode, errors, stats, message, status, download_url=None):
        ok = status < 400
        payload = {
            "ok": ok,
            "mode": mode,
            "message": message,
            "stats": stats or {},
            "errors": errors.as_dict() if errors else None,
            "download_url": download_url,
        }
        if _wants_json():
            return jsonify(payload), status
        return (render_template("report.html", ok=ok, mode=mode, message=message,
                                stats=stats or {},
                                errors=errors.as_dict() if errors else None,
                                download_url=download_url), status)

    return app


def _save_upload(stream, dst) -> None:
    """分块落盘，内存占用与上传大小无关。"""
    with open(dst, "wb") as out:
        shutil.copyfileobj(stream, out, 256 * 1024)


def _wants_json() -> bool:
    if request.args.get("format") == "json":
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
