# 桥梁巡检单表包工具

档案员工作流程：巡检单入仓后，**测点清单**与**照片清单**被分表保存。本工具提供网页完成两件事：

1. **拆表**：上传巡检 JSONL（每行一张巡检单，单行 ≤ 64 KiB），按唯一巡检号拆成
   主表 / 测点表 / 照片表，三表以 JSONL 打成 ZIP 下载（表包内各表行序不限）。
2. **还原**：上传表包 ZIP，先校验整包（无主行、重复位置、缺位 / 多余明细等），
   **全部通过后才开放还原 JSONL 下载**；任一问题即整包退回并逐条列出问题。

## 数据约定

巡检 JSONL 每行一个 JSON 对象：

| 字段 | 说明 |
| --- | --- |
| `inspect_no` | 巡检号，非空字符串，全文件唯一（必填） |
| `points` | 测点清单：数组 / `null` / 缺省 |
| `photos` | 照片清单：数组 / `null` / 缺省 |
| 其余字段 | 任意，全部进主表 |

表包 ZIP 固定三个成员（允许一层目录前缀），各表行序不限：

- `main.jsonl`：巡检单去掉两类清单后的全部字段，外加 `_meta`，例如
  `{"inspect_no":"JX-1", ...,"_meta":{"points":"list","points_len":3,"photos":"absent","photos_len":0}}`
- `points.jsonl` / `photos.jsonl`：`{"_no": 巡检号, "_seq": 原序号(0 起), ...清单项原字段...}`

`_meta` / `_no` / `_seq` 为保留字段。清单项必须是 JSON 对象。

## 实现要点

- **忠实还原**：`_meta` 记录两类清单的状态（`absent`/`null`/`list`）与声明长度。
  还原时缺省的不补字段、`null` 的还原为 `null`、空列表还原为 `[]`；
  明细行携带 `_seq`，还原按序号排序，因此**两类清单各自的原顺序与表内行序无关**。
- **内存不随全包行数增长**（拆表与还原均是）：
  - 全程逐行流式读写；上传分块落盘；ZIP 成员流式解压 / 压缩；
  - 巡检号去重、主从关联、聚合校验全部放在 **SQLite 磁盘库**
    （`temp_store=FILE`，页缓存约 2 MiB），Python 侧只保留常数级状态；
  - 单行有上限（巡检单 64 KiB；表包行 1 MiB 防御性限制），超长行截断跳过不载入内存；
  - 错误报告只保留前 200 条明细，总数与分类计数完整统计。
- **还原校验**（任一失败即退回整包，不产生下载链接）：
  - 无主行：明细行巡检号在主表不存在；
  - 重复位置：同一巡检号在同一明细表内 `_seq` 重复；
  - 缺位明细：实际序号集合未覆盖 `_meta` 声明的 `0..len-1`；
  - 多余明细：出现声明范围之外的明细（含清单为 absent/null 却有明细行）；
  - 结构错误：重复巡检号、非法 JSON、非法 `_meta`/`_seq`、主表夹带清单字段、缺表等。
- **拆表校验**：重复巡检号、行超长、非法 JSON、清单字段非数组、清单项非对象、保留字段冲突。

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

### 本地开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/gunicorn --workers 2 --bind 127.0.0.1:8000 "app.web:create_app()"
```

### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 双表单页面 |
| POST | `/split` | 表单字段 `file`：巡检 JSONL → 结果页（含表包 ZIP 下载链接） |
| POST | `/restore` | 表单字段 `file`：表包 ZIP → 校验通过才有还原 JSONL 下载链接 |
| GET | `/download/<token>` | 下载结果文件 |
| GET | `/healthz` | 健康检查 |

两个 POST 接口加 `?format=json`（或 `Accept: application/json`）返回 JSON，便于脚本集成。
环境变量：`APP_WORK_DIR`（工作目录，默认系统临时目录）、`MAX_UPLOAD_BYTES`（上传总量上限，默认 1 GiB）。

## 测试与演示

```bash
.venv/bin/python -m unittest discover -s tests -v   # 24 项单元/接口测试
./scripts/demo.sh http://127.0.0.1:8000             # 端到端演示（拆表→还原→比对→错误注入）
```

实测（本仓库交付前）：

- 24 项测试全部通过：往返一致性（缺省 / null / 空列表原样保留）、表行乱序后清单顺序仍保持、
  无主行 / 重复位置 / 缺位 / 多余 / 重复巡检号 / 行超长 / 非法 JSON 等错误注入；
- 内存冒烟：20,000 张巡检单，拆表与还原的 Python 侧峰值均 < 1 MiB（SQLite 状态落盘）；
- `scripts/demo.sh` 全流程通过：5 张样例单拆表 → 还原 → 逐字段语义一致；
  注入“无主行 + 缺位明细”的坏包返回 422 并逐条列出问题，不产生下载链接。

## 限制与未完成项

- 本环境未安装 Docker，`docker compose up` 未实际执行；Dockerfile / compose 配置经静态校验，
  应用本体以与 Dockerfile 相同的 gunicorn 命令实测通过。
- 还原输出的记录顺序按主表行序（表包不携带原始上传顺序）；对象内 `points`/`photos`
  键固定在末尾；数字等 JSON 文本表示可能变化（语义不变）。
- 结果文件无 TTL 清理（`/download/<token>` 长期有效，token 不可猜测）；未做鉴权与多用户隔离。
- 同步处理，超大包会占用单个请求的工作进程（未引入任务队列）。
- 表包行上限 1 MiB、错误报告展示上限 200 条为硬编码常量（`app/core.py`）。
