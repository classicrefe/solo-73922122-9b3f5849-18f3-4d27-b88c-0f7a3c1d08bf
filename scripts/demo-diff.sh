#!/usr/bin/env bash
# 端到端演示：拆表 -> 差异导出（成功）-> 重复单号重放 -> 换包拒绝 -> 坏包整单退回
# 用法：scripts/demo-diff.sh [BASE_URL]   默认 http://127.0.0.1:8000
set -euo pipefail

BASE="${1:-http://127.0.0.1:8000}"
PY="${PYTHON:-python3}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d /tmp/bridge-diff-demo-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
post_json() { curl -sS -o "$2" -F "file=@$1" "$BASE$3" >/dev/null; }

step "0. 健康检查"
curl -fsS "$BASE/healthz"

step "1. 拆表：基线巡检单 -> 基线表包"
post_json "$ROOT/examples/inspections.jsonl" "$WORK/base.json" "/split?format=json"
DL=$("$PY" -c "import json,sys; print(json.load(open('$WORK/base.json'))['download_url'])")
curl -fsS -o "$WORK/baseline.zip" "$BASE$DL"

step "2. 拆表：当前巡检单 -> 当前表包"
post_json "$ROOT/examples/inspections-current.jsonl" "$WORK/cur.json" "/split?format=json"
DL=$("$PY" -c "import json,sys; print(json.load(open('$WORK/cur.json'))['download_url'])")
curl -fsS -o "$WORK/current.zip" "$BASE$DL"

step "3. 差异导出：基线 + 当前 + 导出单号（预期 200，首次导出）"
HTTP=$(curl -sS -o "$WORK/diff.json" -w "%{http_code}" \
  -F "baseline=@$WORK/baseline.zip" -F "current=@$WORK/current.zip" \
  -F "batch_no=EXP-DEMO-0001" "$BASE/diff?format=json")
echo "HTTP $HTTP（预期 200）"
"$PY" - "$WORK/diff.json" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
assert r["ok"] and r["download_url"], r
assert r["batch_no"] == "EXP-DEMO-0001" and r["replayed"] is False, r
print("统计：", {k: v for k, v in r["stats"].items() if k != "result_sha256"})
print("增量流 SHA-256：", r["result_sha256"])
EOF
DLD=$("$PY" -c "import json,sys; print(json.load(open('$WORK/diff.json'))['download_url'])")
curl -fsS -o "$WORK/diff.jsonl" "$BASE$DLD"

step "4. 增量流内容（每个变更组一行，巡检号升序）"
cat "$WORK/diff.jsonl"
"$PY" - "$WORK/diff.jsonl" <<'EOF'
import json, sys
groups = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
nos = [g["inspect_no"] for g in groups]
assert nos == sorted(nos), "输出顺序不确定"
by_no = {g["inspect_no"]: g for g in groups}
assert by_no["JX-2026-0001"]["change"] == "modified"
assert by_no["JX-2026-0003"]["change"] == "deleted"
assert by_no["JX-2026-0006"]["change"] == "added"
# 删除项保留原值与序号
deleted_point = [c for c in by_no["JX-2026-0003"]["photos"]]
assert deleted_point and deleted_point[0]["change"] == "deleted"
assert deleted_point[0]["before"]["file"] == "p101.jpg"
assert set(deleted_point[0]) == {"seq", "change", "before"}
# 主记录与两类明细在同一组
m = by_no["JX-2026-0001"]
assert {"before", "after", "points", "photos"} <= set(m)
assert any(c["change"] == "modified" for c in m["points"])
assert any(c["seq"] == 3 and c["change"] == "added" for c in m["points"])
print(f"OK：{len(groups)} 个变更组，顺序确定，删除项保留原值与序号")
EOF

step "5. 幂等重放：同单号 + 相同两包（预期 200，返回原结果且字节一致）"
HTTP=$(curl -sS -o "$WORK/replay.json" -w "%{http_code}" \
  -F "baseline=@$WORK/baseline.zip" -F "current=@$WORK/current.zip" \
  -F "batch_no=EXP-DEMO-0001" "$BASE/diff?format=json")
echo "HTTP $HTTP（预期 200）"
"$PY" - "$WORK/replay.json" "$WORK/diff.json" "$WORK/diff.jsonl" "$BASE" <<'EOF'
import json, sys, urllib.request
replay, first, path, base = (json.load(open(sys.argv[1])), json.load(open(sys.argv[2])),
                             sys.argv[3], sys.argv[4])
assert replay["ok"] and replay["replayed"] is True, replay
assert replay["download_url"] == first["download_url"], "应返回首次结果"
assert replay["result_sha256"] == first["result_sha256"]
again = urllib.request.urlopen(base + replay["download_url"]).read()
assert again == open(path, "rb").read(), "重放内容不一致"
print("OK：同单号相同两包返回原结果，未重复计算")
EOF

step "6. 换包拒绝：同单号 + 不同当前包（预期 409）"
post_json "$ROOT/examples/inspections.jsonl" "$WORK/same.json" "/split?format=json"
DL3=$("$PY" -c "import json,sys; print(json.load(open('$WORK/same.json'))['download_url'])")
curl -fsS -o "$WORK/other.zip" "$BASE$DL3"
HTTP=$(curl -sS -o "$WORK/conflict.json" -w "%{http_code}" \
  -F "baseline=@$WORK/baseline.zip" -F "current=@$WORK/other.zip" \
  -F "batch_no=EXP-DEMO-0001" "$BASE/diff?format=json")
echo "HTTP $HTTP（预期 409）"
"$PY" - "$WORK/conflict.json" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
assert r["ok"] is False and r["download_url"] is None, r
assert "导出单号冲突" in r["errors"]["by_kind"], r["errors"]
print("OK：换包已拒绝")
EOF

step "7. 坏包整单退回：当前包注入 无主行 + 重复序号（预期 422，无下载）"
"$PY" - "$WORK/current.zip" "$WORK/bad.zip" <<'EOF'
import json, sys, zipfile
src, dst = sys.argv[1], sys.argv[2]
zf = zipfile.ZipFile(src)
tables = {n: zf.read(n).decode("utf-8").splitlines() for n in zf.namelist()}
lines = [json.loads(line) for line in tables["points.jsonl"]]
# 无主行：引用不存在的巡检号
lines.append({"_no": "GHOST-404", "_seq": 0, "position": "虚构测点"})
# 同一巡检号的同一序号出现两次（序号含糊）
first = next(line for line in lines if line["_no"] == "JX-2026-0001")
lines.append(dict(first))
tables["points.jsonl"] = [json.dumps(line, ensure_ascii=False) for line in lines]
with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as out:
    for name, rows in tables.items():
        out.writestr(name, "\n".join(rows) + "\n")
print("已构造坏包：无主行 GHOST-404 + JX-2026-0001 重复序号")
EOF
HTTP=$(curl -sS -o "$WORK/bad.json" -w "%{http_code}" \
  -F "baseline=@$WORK/baseline.zip" -F "current=@$WORK/bad.zip" \
  -F "batch_no=EXP-DEMO-0002" "$BASE/diff?format=json")
echo "HTTP $HTTP（预期 422）"
"$PY" - "$WORK/bad.json" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
assert r["ok"] is False and r["download_url"] is None, "坏包不应产生下载链接"
kinds = r["errors"]["by_kind"]
print("错误分类：", kinds)
assert "无主行" in kinds and "重复位置" in kinds, kinds
for e in r["errors"]["items"][:8]:
    print(f"  [{e['kind']}] {e['table']} 行{e['line']}: {e['message']}")
print("OK：整单已退回，未生成增量文件")
EOF

step "8. 失败不占单号：修好包后同单号可成功（预期 200）"
HTTP=$(curl -sS -o "$WORK/retry.json" -w "%{http_code}" \
  -F "baseline=@$WORK/baseline.zip" -F "current=@$WORK/current.zip" \
  -F "batch_no=EXP-DEMO-0002" "$BASE/diff?format=json")
echo "HTTP $HTTP（预期 200）"
"$PY" -c "import json,sys; r=json.load(open('$WORK/retry.json')); assert r['ok'] and r['replayed'] is False; print('OK：失败未占用导出单号')"

step "9. 空导出单号 / 未选文件（预期 400，无下载）"
HTTP=$(curl -sS -o "$WORK/nobatch.json" -w "%{http_code}" \
  -F "baseline=@$WORK/baseline.zip" -F "current=@$WORK/current.zip" \
  "$BASE/diff?format=json")
echo "HTTP $HTTP（预期 400）"
"$PY" - "$WORK/nobatch.json" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
assert r["ok"] is False and r["download_url"] is None, r
assert "导出单号" in r["message"], r["message"]
print("OK：缺少导出单号被拒绝")
EOF

step "全部演示通过"
