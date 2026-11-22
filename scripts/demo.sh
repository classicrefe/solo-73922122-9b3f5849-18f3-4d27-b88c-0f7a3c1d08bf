#!/usr/bin/env bash
# 端到端演示：拆表 -> 下载表包 -> 还原 -> 语义比对 -> 错误注入（无主行/缺位）
# 用法：scripts/demo.sh [BASE_URL]   默认 http://127.0.0.1:8000
set -euo pipefail

BASE="${1:-http://127.0.0.1:8000}"
PY="${PYTHON:-python3}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d /tmp/bridge-demo-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

step "0. 健康检查"
curl -fsS "$BASE/healthz"

step "1. 拆表：上传 examples/inspections.jsonl"
curl -fsS -F "file=@$ROOT/examples/inspections.jsonl" \
  "$BASE/split?format=json" | tee "$WORK/split.json" | "$PY" -m json.tool
DL=$("$PY" -c "import json,sys; print(json.load(open('$WORK/split.json'))['download_url'])")

step "2. 下载表包 ZIP 并查看内容"
curl -fsS -o "$WORK/tables.zip" "$BASE$DL"
"$PY" - <<EOF
import zipfile
zf = zipfile.ZipFile("$WORK/tables.zip")
for info in zf.infolist():
    print(f"  {info.filename}  {info.file_size} bytes")
for name in ("main.jsonl", "points.jsonl", "photos.jsonl"):
    lines = zf.read(name).decode("utf-8").splitlines()
    print(f"--- {name}（{len(lines)} 行，首行）---")
    print(" ", lines[0] if lines else "(空)")
EOF

step "3. 还原：上传表包，校验通过后才开放下载"
curl -fsS -F "file=@$WORK/tables.zip" \
  "$BASE/restore?format=json" | tee "$WORK/restore.json" | "$PY" -m json.tool
DL2=$("$PY" -c "import json,sys; print(json.load(open('$WORK/restore.json'))['download_url'])")
curl -fsS -o "$WORK/restored.jsonl" "$BASE$DL2"

step "4. 语义比对：还原结果 == 原始巡检单（含缺省/null/空列表/清单顺序）"
"$PY" - "$ROOT/examples/inspections.jsonl" "$WORK/restored.jsonl" <<'EOF'
import json, sys
def load(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
a = {r["inspect_no"]: r for r in load(sys.argv[1])}
b = {r["inspect_no"]: r for r in load(sys.argv[2])}
assert a.keys() == b.keys(), "巡检号集合不一致"
for no, orig in a.items():
    got = b[no]
    assert got == orig, f"{no} 内容不一致:\n  原: {orig}\n  还: {got}"
    for f in ("points", "photos"):
        if f not in orig:
            assert f not in got, f"{no}.{f} 缺省未保留"
        elif orig[f] is None:
            assert got[f] is None, f"{no}.{f} null 未保留"
print(f"OK：{len(a)} 张巡检单逐字段一致，缺省/null/空列表与清单顺序均保留")
EOF

step "5. 错误注入：伪造 无主行 + 缺位明细 的表包，应整包退回"
"$PY" - "$WORK/tables.zip" "$WORK/bad.zip" <<'EOF'
import json, sys, zipfile
src, dst = sys.argv[1], sys.argv[2]
zf = zipfile.ZipFile(src)
tables = {n: zf.read(n).decode("utf-8").splitlines() for n in zf.namelist()}
# 无主行：引用不存在的巡检号
tables["points.jsonl"].append(json.dumps(
    {"_no": "GHOST-404", "_seq": 0, "position": "虚构测点"}, ensure_ascii=False))
# 缺位明细：删掉 JX-2026-0001 的最后一条测点
pts = [json.loads(l) for l in tables["points.jsonl"]]
pts = [p for p in pts if not (p["_no"] == "JX-2026-0001" and p["_seq"] == 2)]
tables["points.jsonl"] = [json.dumps(p, ensure_ascii=False) for p in pts]
with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as out:
    for name, lines in tables.items():
        out.writestr(name, "\n".join(lines) + "\n")
print("已构造坏包：无主行 GHOST-404 + JX-2026-0001 缺测点 seq=2")
EOF
HTTP=$(curl -sS -o "$WORK/bad.json" -w "%{http_code}" \
  -F "file=@$WORK/bad.zip" "$BASE/restore?format=json")
echo "HTTP $HTTP（预期 422）"
"$PY" - "$WORK/bad.json" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
assert r["ok"] is False and r["download_url"] is None, "坏包不应产生下载链接"
kinds = r["errors"]["by_kind"]
print("错误分类：", kinds)
assert "无主行" in kinds and "缺位明细" in kinds, "应检出无主行与缺位明细"
for e in r["errors"]["items"]:
    print(f"  [{e['kind']}] {e['table']} 行{e['line']}: {e['message']}")
print("OK：整包已退回，未开放下载")
EOF

step "6. 拆表侧校验：重复巡检号应被拒绝"
printf '%s\n' '{"inspect_no":"DUP"}' '{"inspect_no":"DUP"}' > "$WORK/dup.jsonl"
HTTP=$(curl -sS -o "$WORK/dup.json" -w "%{http_code}" \
  -F "file=@$WORK/dup.jsonl" "$BASE/split?format=json")
echo "HTTP $HTTP（预期 422）"
"$PY" -c "import json; r=json.load(open('$WORK/dup.json')); print('错误分类：', r['errors']['by_kind']); assert '重复巡检号' in r['errors']['by_kind']"

step "全部演示通过"
