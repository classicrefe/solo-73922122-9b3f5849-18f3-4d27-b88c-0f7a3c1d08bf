"""桥梁巡检单拆表 / 还原 / 差异导出 Web 服务。

页面与接口：
  GET  /                  三表单页面（拆表、还原、差异导出）
  POST /split             上传巡检 JSONL -> 校验并生成表包 ZIP，给出下载链接
  POST /restore           上传表包 ZIP -> 校验整包，全部通过才生成还原 JSONL 下载链接；
                          任一校验失败则整包退回并列出问题
  POST /diff              上传基线包、当前包与导出单号 -> 两包均校验通过才生成增量
                          JSONL 下载链接；同一单号重放返回原结果，换包则拒绝
  GET  /download/<token>  下载已生成的结果文件（ZIP / JSONL / 增量 JSONL）
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

from .core import (KIND_BATCH_CONFLICT, KIND_BATCH_INVALID, MAX_LINE_BYTES,
                   DiffRegistry, ErrorCollector, diff_bundles, restore_bundle,
                   restore_zip, split_jsonl, validate_batch_no)

TOKEN_RE = re.compile(r"[0-9a-f]{32}")

# 页面演示用的只读样例（白名单，避免任意文件读取）
EXAMPLE_FILES = {
    "inspections.jsonl": "巡检 JSONL（基线）",
    "inspections-current.jsonl": "巡检 JSONL（当前）",
}

# token 后缀 -> (MIME, 下载文件名)
RESULT_KINDS = {
    ".zip": ("application/zip", "inspection-tables.zip"),
    ".jsonl": ("application/x-ndjson", "inspections-restored.jsonl"),
    ".diff.jsonl": ("application/x-ndjson", "inspection-diff.jsonl"),
}


def create_app() -> Flask:
    app = Flask(__name__)
    work_root = Path(os.environ.get("APP_WORK_DIR", tempfile.gettempdir())) / "bridge-inspection"
    results_dir = work_root / "results"
    jobs_dir = work_root / "jobs"
    results_dir.mkdir(parents=True, exist_ok=True)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    registry = DiffRegistry(work_root / "diff-registry.sqlite3")
    examples_dir = Path(app.root_path).parent / "examples"
    # 上传总量安全阀（默认 1 GiB，可用环境变量调整）；单行 64 KiB 限制在核心层执行
    app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_BYTES", 1 << 30))

    # ------------------------------------------------------------------
    @app.get("/")
    def index():
        return render_template(
            "index.html", max_line_kib=MAX_LINE_BYTES // 1024,
            examples=[(name, label) for name, label in EXAMPLE_FILES.items()
                      if (examples_dir / name).is_file()])

    @app.get("/examples/<name>")
    def example(name):
        if name not in EXAMPLE_FILES:
            abort(404)
        path = examples_dir / name
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype="application/x-ndjson",
                         as_attachment=True, download_name=name)

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

    @app.post("/diff")
    def diff_view():
        baseline = request.files.get("baseline")
        current = request.files.get("current")
        if baseline is None or baseline.filename == "":
            return _respond("diff", None, None, "请选择基线表包 ZIP", 400)
        if current is None or current.filename == "":
            return _respond("diff", None, None, "请选择当前表包 ZIP", 400)
        batch_no, err = validate_batch_no(request.form.get("batch_no"))
        if err:
            errors = ErrorCollector()
            errors.add(KIND_BATCH_INVALID, "form", "-", err)
            return _respond("diff", errors, None, "导出单号不合法，未生成增量文件", 400)

        job = jobs_dir / uuid.uuid4().hex
        job.mkdir(parents=True)
        try:
            base_zip = job / "baseline.zip"
            cur_zip = job / "current.zip"
            _save_upload(baseline.stream, base_zip)
            _save_upload(current.stream, cur_zip)
            # 两包各自整包校验（并还原到磁盘 SQLite）；任一失败即整单退回
            base_bundle = restore_bundle(base_zip, job / "baseline", emit_jsonl=False)
            cur_bundle = restore_bundle(cur_zip, job / "current", emit_jsonl=False)
            if not (base_bundle.ok and cur_bundle.ok):
                res = diff_bundles(base_bundle, cur_bundle, job / "diff")
                return _respond("diff", res.errors, res.stats,
                                "表包校验未通过，整单已退回", 422,
                                batch_no=batch_no,
                                shas=(base_bundle.digest, cur_bundle.digest))
            base_sha = base_bundle.digest   # 表内容规范化指纹（与行序 / ZIP 字节无关）
            cur_sha = cur_bundle.digest

            existing = registry.get(batch_no)
            if existing is not None:
                if (existing["baseline_sha256"] == base_sha
                        and existing["current_sha256"] == cur_sha):
                    if (results_dir / f"{existing['token']}.diff.jsonl").exists():
                        # 同单号 + 相同两包：返回原结果（不重新计算）
                        return _respond(
                            "diff", None, existing["stats"], None, 200,
                            download_url=url_for("download", token=existing["token"]),
                            batch_no=batch_no, replayed=True,
                            shas=(base_sha, cur_sha),
                            result_sha256=existing["result_sha256"],
                            notice=f"导出单号 {batch_no} 已存在，返回首次导出结果（未重复计算）",
                        )
                    # 结果文件已被清理：释放陈旧登记后按首次导出重新计算
                    registry.forget(batch_no, existing["token"])
                else:
                    errors = ErrorCollector()
                    errors.add(
                        KIND_BATCH_CONFLICT, "form", "-",
                        f"导出单号 {batch_no} 已用于另一对表包"
                        f"（基线 {existing['baseline_sha256'][:12]}… / "
                        f"当前 {existing['current_sha256'][:12]}…），"
                        "不允许换包重复使用",
                    )
                    return _respond("diff", errors, None,
                                    "导出单号已使用且表包不同，换包已拒绝", 409,
                                    batch_no=batch_no, shas=(base_sha, cur_sha))

            res = diff_bundles(base_bundle, cur_bundle, job / "diff")
            if not res.ok:
                # 任一表包校验失败 / 关联缺失 / 序号含糊：整单退回，不登记单号
                return _respond("diff", res.errors, res.stats,
                                "表包校验未通过，整单已退回", 422,
                                batch_no=batch_no, shas=(base_sha, cur_sha))

            token = uuid.uuid4().hex
            shutil.move(res.out_path, results_dir / f"{token}.diff.jsonl")
            record, created = registry.add(
                batch_no, base_sha, cur_sha, token, res.stats, res.result_digest)
            if not created:
                # 并发下另一进程已登记同一单号
                same = (record is not None
                        and record["baseline_sha256"] == base_sha
                        and record["current_sha256"] == cur_sha)
                (results_dir / f"{token}.diff.jsonl").unlink(missing_ok=True)
                if not same:
                    errors = ErrorCollector()
                    errors.add(KIND_BATCH_CONFLICT, "form", "-",
                               f"导出单号 {batch_no} 已被并发请求用于另一对表包")
                    return _respond("diff", errors, None,
                                    "导出单号已使用且表包不同，换包已拒绝", 409,
                                    batch_no=batch_no, shas=(base_sha, cur_sha))
                return _respond(
                    "diff", None, record["stats"], None, 200,
                    download_url=url_for("download", token=record["token"]),
                    batch_no=batch_no, replayed=True, shas=(base_sha, cur_sha),
                    result_sha256=record["result_sha256"],
                    notice=f"导出单号 {batch_no} 已存在，返回首次导出结果（未重复计算）",
                )
            return _respond("diff", None, record["stats"], None, 200,
                            download_url=url_for("download", token=token),
                            batch_no=batch_no, replayed=False,
                            shas=(base_sha, cur_sha),
                            result_sha256=res.result_digest)
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
    def _respond(mode, errors, stats, message, status, download_url=None,
                 batch_no=None, replayed=None, shas=None, result_sha256=None,
                 notice=None):
        ok = status < 400
        payload = {
            "ok": ok,
            "mode": mode,
            "message": message,
            "stats": stats or {},
            "errors": errors.as_dict() if errors else None,
            "download_url": download_url,
        }
        if mode == "diff":
            payload.update({
                "batch_no": batch_no,
                "replayed": bool(replayed),
                "baseline_sha256": shas[0] if shas else None,
                "current_sha256": shas[1] if shas else None,
                "result_sha256": result_sha256,
                "notice": notice,
            })
        if _wants_json():
            return jsonify(payload), status
        return (render_template("report.html", ok=ok, mode=mode, message=message,
                                stats=stats or {},
                                errors=errors.as_dict() if errors else None,
                                download_url=download_url,
                                batch_no=batch_no, replayed=bool(replayed),
                                shas=shas or (None, None),
                                result_sha256=result_sha256,
                                notice=notice), status)

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
