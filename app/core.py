"""桥梁巡检单拆表 / 还原 / 校验核心逻辑。

数据约定
--------
巡检 JSONL：每行一张巡检单（JSON 对象），单行不超过 64 KiB。字段：
  - ``inspect_no``  巡检号，非空字符串，全文件唯一（必填）；
  - ``points``      测点清单，数组 / null / 缺省；
  - ``photos``      照片清单，数组 / null / 缺省；
  - 其余字段任意，全部进入主表。

表包 ZIP：固定三个成员 ``main.jsonl`` / ``points.jsonl`` / ``photos.jsonl``，
各表行序不限。
  - 主表行 = 巡检单去掉两类清单后的字段，外加 ``_meta``：
    ``{"points": "absent|null|list", "points_len": N, "photos": ..., "photos_len": N}``
  - 明细行 = ``{"_no": 巡检号, "_seq": 原序号(0 起), ...清单项原字段...}``

内存设计
--------
拆表与还原均不随全包行数增长：
  - 逐行流式读写（单行有上限，超限报错跳过）；
  - 需要按巡检号聚合 / 去重 / 关联的状态全部放在 SQLite（磁盘库，
    temp_store=FILE，页缓存约 2 MiB）；
  - 错误报告只保留前 ``MAX_ERRORS`` 条明细，总数与分类计数完整统计。

还原校验（任一失败即退回整包）
------------------------------
  - 无主行：明细行的巡检号在主表不存在；
  - 重复位置：同一巡检号在同一明细表内序号重复；
  - 缺位明细：实际序号集合覆盖不了 ``_meta`` 声明的 0..len-1；
  - 多余明细：出现 ``_meta`` 声明范围之外的明细（含 absent/null 却有明细行）；
  - 以及结构错误：重复巡检号、非法 JSON、非法 ``_meta`` / ``_seq`` 等。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

INSPECT_NO_FIELD = "inspect_no"
LIST_FIELDS = ("points", "photos")
META_KEY = "_meta"
DETAIL_NO_KEY = "_no"
DETAIL_SEQ_KEY = "_seq"

STATE_ABSENT = "absent"
STATE_NULL = "null"
STATE_LIST = "list"
STATES = (STATE_ABSENT, STATE_NULL, STATE_LIST)

MAX_LINE_BYTES = 64 * 1024          # 巡检 JSONL 单行上限（64 KiB）
MAX_PKG_LINE_BYTES = 1024 * 1024    # 表包单行上限（防御性限制，1 MiB）
MAX_ERRORS = 200                    # 报告中保留的最大错误条数（总数仍完整统计）
DETAIL_FETCH_LIMIT = 10000          # 计算缺位/多余差集时单巡检号最多读取的序号数
SEQ_SHOW_LIMIT = 8                  # 错误消息中最多展示的序号个数

TABLE_MAIN = "main"
TABLE_POINTS = "points"
TABLE_PHOTOS = "photos"
DETAIL_TABLES = (TABLE_POINTS, TABLE_PHOTOS)
# 清单字段 -> 明细表 的对应关系
FIELD_TO_TABLE = {LIST_FIELDS[0]: TABLE_POINTS, LIST_FIELDS[1]: TABLE_PHOTOS}
PKG_ENTRIES = {
    TABLE_MAIN: "main.jsonl",
    TABLE_POINTS: "points.jsonl",
    TABLE_PHOTOS: "photos.jsonl",
}

KIND_LINE_TOO_LONG = "行超长"
KIND_JSON = "JSON错误"
KIND_STRUCT = "结构错误"
KIND_DUP_NO = "重复巡检号"
KIND_ORPHAN = "无主行"
KIND_DUP_POS = "重复位置"
KIND_MISSING = "缺位明细"
KIND_EXTRA = "多余明细"
KIND_PACKAGE = "包错误"


class ErrorCollector:
    """收集校验错误；只保留前 limit 条明细，总数与分类计数完整统计（内存有界）。"""

    def __init__(self, limit: int = MAX_ERRORS):
        self.limit = limit
        self.items: list[dict] = []
        self.total = 0
        self.by_kind: Counter = Counter()

    def add(self, kind: str, table: str, line, message: str) -> None:
        self.total += 1
        self.by_kind[kind] += 1
        if len(self.items) < self.limit:
            self.items.append(
                {"kind": kind, "table": table, "line": line, "message": message}
            )

    def __bool__(self) -> bool:
        return self.total > 0

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "by_kind": dict(self.by_kind),
            "items": self.items,
            "truncated": self.total > len(self.items),
        }


@dataclass
class SplitResult:
    ok: bool
    zip_path: str | None
    errors: ErrorCollector
    stats: dict = field(default_factory=dict)


@dataclass
class RestoreResult:
    ok: bool
    out_path: str | None
    errors: ErrorCollector
    stats: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _tune_db(db: sqlite3.Connection) -> None:
    """限制 SQLite 内存占用：临时结构落盘，页缓存约 2 MiB。"""
    db.execute("PRAGMA journal_mode = OFF")
    db.execute("PRAGMA synchronous = OFF")
    db.execute("PRAGMA temp_store = FILE")
    db.execute("PRAGMA cache_size = -2000")


def _iter_lines(path, max_bytes: int, errors: ErrorCollector, label: str):
    """逐行产出 (lineno, content_bytes)。

    空行跳过；超过 max_bytes 的行记错误并跳过（吞掉该行剩余部分，
    不会把超长行读进内存）。
    """
    with open(path, "rb") as f:
        lineno = 0
        while True:
            raw = f.readline(max_bytes + 3)
            if not raw:
                return
            lineno += 1
            if len(raw) == max_bytes + 3 and not raw.endswith(b"\n"):
                # 行未结束：吞掉剩余部分，避免把超长行载入内存
                while True:
                    more = f.readline(max_bytes + 3)
                    if not more or more.endswith(b"\n"):
                        break
                errors.add(KIND_LINE_TOO_LONG, label, lineno,
                           f"行内容超过 {max_bytes} 字节上限")
                continue
            content = raw
            if content.endswith(b"\n"):
                content = content[:-1]
            if content.endswith(b"\r"):
                content = content[:-1]
            if len(content) > max_bytes:
                errors.add(KIND_LINE_TOO_LONG, label, lineno,
                           f"行内容超过 {max_bytes} 字节上限")
                continue
            if not content.strip():
                continue
            yield lineno, content


def _parse_obj(raw: bytes):
    """解析一行 JSON；返回 (obj, err_msg)。要求必须是 JSON 对象。"""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"JSON 解析失败：{e}"
    if not isinstance(obj, dict):
        return None, "每行必须是一个 JSON 对象"
    return obj, None


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _write_row(fh, obj) -> None:
    fh.write(_dump(obj))
    fh.write("\n")


def _check_meta(meta):
    """校验主表行内嵌的 _meta；返回错误消息或 None。"""
    if not isinstance(meta, dict):
        return f"{META_KEY} 必须是对象"
    for f_ in LIST_FIELDS:
        st = meta.get(f_)
        if st not in STATES:
            return f"{META_KEY}.{f_} 状态非法（应为 absent/null/list）"
        ln = meta.get(f_ + "_len")
        if not isinstance(ln, int) or isinstance(ln, bool) or ln < 0:
            return f"{META_KEY}.{f_}_len 必须是非负整数"
        if st != STATE_LIST and ln != 0:
            return f"{META_KEY}.{f_}_len 与状态 {st} 不一致"
    return None


def _fmt_seqs(seqs) -> str:
    seqs = list(seqs)
    if len(seqs) <= SEQ_SHOW_LIMIT:
        return ",".join(str(s) for s in seqs)
    head = ",".join(str(s) for s in seqs[:SEQ_SHOW_LIMIT])
    return f"{head}…共{len(seqs)}个"


# ---------------------------------------------------------------------------
# 拆表：巡检 JSONL -> 三表 ZIP
# ---------------------------------------------------------------------------

def split_jsonl(src_path, work_dir) -> SplitResult:
    """把巡检 JSONL 拆成 主表/测点表/照片表 三个 JSONL 并打成 ZIP。

    任一记录出错即整体失败（不产出 ZIP），错误全部收集在返回值中。
    """
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    errors = ErrorCollector()
    stats = {"records": 0, "main_rows": 0, "points_rows": 0, "photos_rows": 0}

    db = sqlite3.connect(str(work / f"split-{uuid.uuid4().hex}.db"))
    _tune_db(db)
    # 巡检号唯一性检查放在磁盘库中，内存不随行数增长
    db.execute("CREATE TABLE seen (no TEXT PRIMARY KEY)")

    paths = {t: work / PKG_ENTRIES[t] for t in PKG_ENTRIES}
    files = {t: open(paths[t], "w", encoding="utf-8", newline="\n") for t in PKG_ENTRIES}
    try:
        for lineno, raw in _iter_lines(src_path, MAX_LINE_BYTES, errors, "inspections"):
            stats["records"] += 1
            rec, err = _parse_obj(raw)
            if err:
                errors.add(KIND_JSON, "inspections", lineno, err)
                continue
            no = rec.get(INSPECT_NO_FIELD)
            if not isinstance(no, str) or not no:
                errors.add(KIND_STRUCT, "inspections", lineno,
                           f"缺少有效的巡检号字段 {INSPECT_NO_FIELD}（非空字符串）")
                continue
            try:
                db.execute("INSERT INTO seen (no) VALUES (?)", (no,))
            except sqlite3.IntegrityError:
                errors.add(KIND_DUP_NO, "inspections", lineno, f"巡检号 {no!r} 重复")
                continue
            if META_KEY in rec:
                errors.add(KIND_STRUCT, "inspections", lineno,
                           f"{META_KEY} 为保留字段，不得出现在巡检单中")
                continue

            meta = {}
            bad = False
            for f_ in LIST_FIELDS:
                if f_ not in rec:
                    meta[f_] = STATE_ABSENT
                    meta[f_ + "_len"] = 0
                    continue
                v = rec[f_]
                if v is None:
                    meta[f_] = STATE_NULL
                    meta[f_ + "_len"] = 0
                    continue
                if not isinstance(v, list):
                    errors.add(KIND_STRUCT, "inspections", lineno,
                               f"字段 {f_} 必须是数组、null 或缺省")
                    bad = True
                    break
                meta[f_] = STATE_LIST
                meta[f_ + "_len"] = len(v)
            if bad:
                continue

            # 先构造全部明细行，任一失败则整单作废，避免写出半截记录
            detail_rows = {TABLE_POINTS: [], TABLE_PHOTOS: []}
            for f_ in LIST_FIELDS:
                if meta[f_] != STATE_LIST:
                    continue
                t_ = FIELD_TO_TABLE[f_]
                for i, item in enumerate(rec[f_]):
                    if not isinstance(item, dict):
                        errors.add(KIND_STRUCT, "inspections", lineno,
                                   f"{f_}[{i}] 必须是 JSON 对象")
                        bad = True
                        break
                    if DETAIL_NO_KEY in item or DETAIL_SEQ_KEY in item:
                        errors.add(KIND_STRUCT, "inspections", lineno,
                                   f"{f_}[{i}] 含保留字段 {DETAIL_NO_KEY}/{DETAIL_SEQ_KEY}")
                        bad = True
                        break
                    row = {DETAIL_NO_KEY: no, DETAIL_SEQ_KEY: i}
                    row.update(item)
                    detail_rows[t_].append(row)
                if bad:
                    break
            if bad:
                continue

            main_obj = {k: v for k, v in rec.items() if k not in LIST_FIELDS}
            main_obj[META_KEY] = meta
            _write_row(files[TABLE_MAIN], main_obj)
            stats["main_rows"] += 1
            for t_ in DETAIL_TABLES:
                for row in detail_rows[t_]:
                    _write_row(files[t_], row)
            stats["points_rows"] += len(detail_rows[TABLE_POINTS])
            stats["photos_rows"] += len(detail_rows[TABLE_PHOTOS])
    finally:
        for fh in files.values():
            fh.close()
        db.close()

    if errors:
        return SplitResult(False, None, errors, stats)

    zip_path = work / "tables.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for t_ in PKG_ENTRIES:
            entry = PKG_ENTRIES[t_]
            with open(paths[t_], "rb") as src_f, zf.open(entry, "w") as dst:
                shutil.copyfileobj(src_f, dst, 256 * 1024)
    stats["zip_bytes"] = zip_path.stat().st_size
    return SplitResult(True, str(zip_path), errors, stats)


# ---------------------------------------------------------------------------
# 还原：三表 ZIP -> 巡检 JSONL
# ---------------------------------------------------------------------------

def restore_zip(zip_path, work_dir) -> RestoreResult:
    """校验表包 ZIP 并还原为巡检 JSONL；任何错误都会退回整包。"""
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    errors = ErrorCollector()
    stats = {"main_rows": 0, "points_rows": 0, "photos_rows": 0, "ignored_entries": []}

    # 1) 解包（流式；不使用条目名作为输出路径，天然免疫 zip-slip）
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        errors.add(KIND_PACKAGE, "-", "-", "不是有效的 ZIP 文件")
        return RestoreResult(False, None, errors, stats)
    with zf:
        entry_map = _map_entries(zf, errors)
        if errors:
            return RestoreResult(False, None, errors, stats)
        used = {info.filename for info in entry_map.values()}
        stats["ignored_entries"] = sorted(
            i.filename for i in zf.infolist()
            if not i.is_dir() and i.filename not in used
        )
        extracted = {}
        for t_, info in entry_map.items():
            dst = work / PKG_ENTRIES[t_]
            with zf.open(info, "r") as src_f, open(dst, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f, 256 * 1024)
            extracted[t_] = dst

    # 2) 建库（磁盘），装载三表
    db = sqlite3.connect(str(work / f"restore-{uuid.uuid4().hex}.db"))
    _tune_db(db)
    db.execute("CREATE TABLE main (ord INTEGER, no TEXT, meta TEXT, payload TEXT)")
    db.execute("CREATE TABLE points (ord INTEGER, no TEXT, seq INTEGER, payload TEXT)")
    db.execute("CREATE TABLE photos (ord INTEGER, no TEXT, seq INTEGER, payload TEXT)")

    _load_main(db, extracted[TABLE_MAIN], errors, stats)
    for t_ in DETAIL_TABLES:
        _load_details(db, extracted[t_], t_, errors, stats)
    db.commit()
    db.execute("CREATE INDEX idx_main_no ON main (no)")
    db.execute("CREATE INDEX idx_points_no ON points (no)")
    db.execute("CREATE INDEX idx_photos_no ON photos (no)")

    # 3) 集合校验：重复巡检号 / 无主行 / 重复位置 / 缺位 / 多余
    _check_dup_main(db, errors)
    for t_ in DETAIL_TABLES:
        _check_orphans(db, errors, t_)
        _check_dup_positions(db, errors, t_)
    for f_ in LIST_FIELDS:
        _check_coverage(db, errors, FIELD_TO_TABLE[f_], f_)

    if errors:
        db.close()
        return RestoreResult(False, None, errors, stats)

    # 4) 全部通过：按主表行序输出，清单按 _seq 还原原顺序
    out_path = work / "restored.jsonl"
    n_out = 0
    with open(out_path, "w", encoding="utf-8", newline="\n") as out:
        cur = db.execute("SELECT no, meta, payload FROM main ORDER BY ord")
        while True:
            rows = cur.fetchmany(256)
            if not rows:
                break
            for no, meta_json, payload_json in rows:
                meta = json.loads(meta_json)
                obj = json.loads(payload_json)
                for f_ in LIST_FIELDS:
                    st = meta[f_]
                    if st == STATE_LIST:
                        t_ = FIELD_TO_TABLE[f_]
                        obj[f_] = [
                            json.loads(r[0])
                            for r in db.execute(
                                f"SELECT payload FROM {t_} WHERE no = ? ORDER BY seq",
                                (no,),
                            )
                        ]
                    elif st == STATE_NULL:
                        obj[f_] = None
                    # absent：不设置该字段，保持缺省
                out.write(_dump(obj))
                out.write("\n")
                n_out += 1
    db.close()
    stats["restored_records"] = n_out
    stats["out_bytes"] = out_path.stat().st_size
    return RestoreResult(True, str(out_path), errors, stats)


def _map_entries(zf: zipfile.ZipFile, errors: ErrorCollector) -> dict:
    """把包内条目映射到三张表；允许恰好一层目录前缀（如 tables/main.jsonl）。"""
    mapping = {}
    names = [i for i in zf.infolist() if not i.is_dir()]
    for t_, want in PKG_ENTRIES.items():
        exact = [i for i in names if i.filename == want]
        base = [i for i in names if i.filename.rsplit("/", 1)[-1] == want]
        if exact:
            mapping[t_] = exact[0]
        elif len(base) == 1:
            mapping[t_] = base[0]
        elif not base:
            errors.add(KIND_PACKAGE, "-", "-", f"包内缺少表文件 {want}")
        else:
            errors.add(KIND_PACKAGE, "-", "-", f"表文件 {want} 在包内不唯一")
    return mapping


def _load_main(db, path, errors: ErrorCollector, stats: dict) -> None:
    batch = []
    for lineno, raw in _iter_lines(path, MAX_PKG_LINE_BYTES, errors, TABLE_MAIN):
        rec, err = _parse_obj(raw)
        if err:
            errors.add(KIND_JSON, TABLE_MAIN, lineno, err)
            continue
        stats["main_rows"] += 1
        no = rec.get(INSPECT_NO_FIELD)
        if not isinstance(no, str) or not no:
            errors.add(KIND_STRUCT, TABLE_MAIN, lineno,
                       f"巡检号字段 {INSPECT_NO_FIELD} 缺失或非法")
            continue
        meta_err = _check_meta(rec.get(META_KEY))
        if meta_err:
            errors.add(KIND_STRUCT, TABLE_MAIN, lineno, meta_err)
            continue
        stray = [f_ for f_ in LIST_FIELDS if f_ in rec]
        if stray:
            errors.add(KIND_STRUCT, TABLE_MAIN, lineno,
                       f"主表行不得包含清单字段 {stray}")
            continue
        payload = {k: v for k, v in rec.items() if k != META_KEY}
        batch.append((lineno, no, _dump(rec[META_KEY]), _dump(payload)))
        if len(batch) >= 500:
            db.executemany("INSERT INTO main VALUES (?,?,?,?)", batch)
            batch.clear()
    if batch:
        db.executemany("INSERT INTO main VALUES (?,?,?,?)", batch)


def _load_details(db, path, table, errors: ErrorCollector, stats: dict) -> None:
    batch = []
    for lineno, raw in _iter_lines(path, MAX_PKG_LINE_BYTES, errors, table):
        rec, err = _parse_obj(raw)
        if err:
            errors.add(KIND_JSON, table, lineno, err)
            continue
        stats[f"{table}_rows"] += 1
        no = rec.get(DETAIL_NO_KEY)
        seq = rec.get(DETAIL_SEQ_KEY)
        if not isinstance(no, str) or not no:
            errors.add(KIND_STRUCT, table, lineno,
                       f"明细行缺少有效的 {DETAIL_NO_KEY}（巡检号）")
            continue
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            errors.add(KIND_STRUCT, table, lineno,
                       f"明细行 {DETAIL_SEQ_KEY} 必须是非负整数")
            continue
        payload = {k: v for k, v in rec.items()
                   if k not in (DETAIL_NO_KEY, DETAIL_SEQ_KEY)}
        batch.append((lineno, no, seq, _dump(payload)))
        if len(batch) >= 500:
            db.executemany(f"INSERT INTO {table} VALUES (?,?,?,?)", batch)
            batch.clear()
    if batch:
        db.executemany(f"INSERT INTO {table} VALUES (?,?,?,?)", batch)


def _check_dup_main(db, errors: ErrorCollector) -> None:
    cur = db.execute(
        "SELECT no, COUNT(*) c, MIN(ord) FROM main GROUP BY no HAVING c > 1"
    )
    for no, c, first_ord in cur:
        errors.add(KIND_DUP_NO, TABLE_MAIN, first_ord,
                   f"巡检号 {no!r} 在主表出现 {c} 次")


def _check_orphans(db, errors: ErrorCollector, table: str) -> None:
    cur = db.execute(
        f"SELECT p.no, COUNT(*) c, MIN(p.ord) FROM {table} p "
        f"LEFT JOIN main m ON m.no = p.no WHERE m.no IS NULL GROUP BY p.no"
    )
    for no, c, first_ord in cur:
        errors.add(KIND_ORPHAN, table, first_ord,
                   f"巡检号 {no!r} 在主表中不存在（{c} 行明细无主）")


def _check_dup_positions(db, errors: ErrorCollector, table: str) -> None:
    cur = db.execute(
        f"SELECT no, seq, COUNT(*) c FROM {table} GROUP BY no, seq HAVING c > 1"
    )
    for no, seq, c in cur:
        errors.add(KIND_DUP_POS, table, "-",
                   f"巡检号 {no!r} 序号 {seq} 出现 {c} 次")


def _check_coverage(db, errors: ErrorCollector, table: str, field_: str) -> None:
    """对照 _meta 声明的长度，检查每个巡检号的明细序号集合是否恰好覆盖 0..len-1。"""
    cur = db.execute(
        f"SELECT m.no, m.meta, COUNT(p.seq), COUNT(DISTINCT p.seq), "
        f"MIN(p.seq), MAX(p.seq) "
        f"FROM main m LEFT JOIN {table} p ON p.no = m.no GROUP BY m.no"
    )
    for no, meta_json, cnt, dcnt, mn, mx in cur:
        meta = json.loads(meta_json)
        expected = meta[field_ + "_len"] if meta[field_] == STATE_LIST else 0
        # 快速判定：去重后序号集合 == {0..expected-1}
        if dcnt == expected and (expected == 0 or (mn == 0 and mx == expected - 1)):
            continue
        actual = [r[0] for r in db.execute(
            f"SELECT DISTINCT seq FROM {table} WHERE no = ? ORDER BY seq "
            f"LIMIT {DETAIL_FETCH_LIMIT}", (no,))]
        aset = set(actual)
        eset = set(range(expected))
        missing = sorted(eset - aset)
        extra = sorted(aset - eset)
        suffix = ""
        if len(actual) >= DETAIL_FETCH_LIMIT:
            suffix = f"（明细过多，差集仅基于前 {DETAIL_FETCH_LIMIT} 个序号）"
        if missing:
            errors.add(KIND_MISSING, table, "-",
                       f"巡检号 {no!r}：声明 {expected} 条，缺序号 "
                       f"{_fmt_seqs(missing)}{suffix}")
        if extra:
            errors.add(KIND_EXTRA, table, "-",
                       f"巡检号 {no!r}：声明 {expected} 条，多出序号 "
                       f"{_fmt_seqs(extra)}{suffix}")
        # 集合相等但存在重复序号的情况已由“重复位置”报告，此处不再重复
