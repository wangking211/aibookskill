# AI Book Skill — 跨书知识库与书籍转换平台

把 PDF/EPUB 转成结构化 skill（SKILL.md + 分章 Markdown），存入多租户书库，支持网页问答、跨书综合问答、中英对照阅读，并通过 MCP 让任意 agent（opencode / Claude 等）共享访问。

线上地址：**https://aibook.zxixing.com**

---

## 目录

- [能做什么](#能做什么)
- [架构总览](#架构总览)
- [部署拓扑](#部署拓扑)
- [代码结构](#代码结构)
- [数据模型](#数据模型)
- [核心链路](#核心链路)
- [配置项](#配置项)
- [本地开发](#本地开发)
- [测试](#测试)
- [文档索引](#文档索引)
- [下阶段规划](ROADMAP.md)

---

## 能做什么

| 能力 | 入口 | 说明 |
|---|---|---|
| 书籍导入 | 网页「上传」/ `POST /api/upload` / MCP `upload_book` | PDF → opencode 转换 → 自动发布入库；同文件 sha256 去重秒传 |
| 多语言 | 上传时选 `zh`/`bi`，或 `POST /api/books/{slug}/translate` | 中文译文 / 中英对照（左原右译） |
| 单书问答 | `POST /api/ask` / MCP `ask` | fast（默认，秒级）/ deep（工具调用，分钟级） |
| **跨书问答** | `POST /api/library/ask` / MCP `ask_library` | 行级索引检索全部可见书，综合作答，引用形如 `【引用:《书名》·章节】` |
| 章节阅读 | `GET /api/books/{slug}/chapters/{no}` | `lang=orig\|zh\|bi`，bi 走 `applyBilingual` 左右分栏 |
| FAQ | `GET /api/faq` / `POST /api/faq/answer` | 从历史问答自动聚类，人工补答后优先命中 |
| 可见性 | `POST /api/books/{slug}/visibility` | `public` 全员可见 / `private` 仅自己与管理员；新上传默认 private |
| API 令牌 | 网页「设置→API 令牌」/ `POST /api/tokens` | `ak_` 前缀，供 MCP 与脚本调用 |
| 本地转换栈 | `kit/aibook-kit.zip` | 一键安装：pipeline + MCP + skills 到任意机器 |

---

## 架构总览

```
                     ┌──────────────────────────────────────┐
  浏览器 / agent ───►│  m4  edge nginx (82.157.178.64)      │
                     │  容器名 nginx，TLS 终止 + 反代         │
                     └───────────────┬──────────────────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      │
     ┌────────────────┐     ┌────────────────┐              │
     │ m1  aibook-web │     │ m2  aibook-web │◄─────────────┘
     │ :8787 (本地)   │     │ :8787 (本地)   │   轮询/主备
     │ aibook-worker  │     │ aibook-worker  │
     │ opencode-serve │     │ opencode-serve │
     │ tunnel→m4      │     │                │
     └───────┬────────┘     └───────┬────────┘
             │   rsync 60s          │
             │  skills/covers/      │
             │  sources/inbox/      │
             │  library  m1→m2      │
             ▼                      ▼
     ┌──────────────────────────────────────┐
     │ m3  PostgreSQL 16 (82.157.27.78)     │
     │  registry：books/chapters/jobs/...   │
     │  12 张表，sqlite 本地同 schema        │
     └──────────────────────────────────────┘
```

- **web（FastAPI）**：鉴权、书库 CRUD、问答、FAQ、任务队列。仅监听 `127.0.0.1:8787`，由 tunnel/m4 暴露。
- **worker**：轮询 `jobs` 表领取任务（lease + heartbeat + reaper），调 `opencode run` 转换/翻译，发布后写回。
- **opencode-serve**：本地 `opencode serve`（:4096），供 worker 走 `opencode/` 前缀模型。
- **m1→m2 rsync**：每 60s 同步内容目录（skills/covers/sources/inbox/library），保证双活可切换。
- **registry**：sqlite（本地开发）/ PG（生产）双后端，SQL 用 `?` 占位符，`_qmark` 自动转 `%s`。

---

## 部署拓扑

| 主机 | IP | 角色 | 关键服务 |
|---|---|---|---|
| m1 | 43.138.19.118 | 主 API + worker | `aibook-web` `aibook-worker` `aibook-tunnel` `aibook-opencode-serve` `aibook-sync.timer` |
| m2 | 49.233.160.151 | 备 API + worker | `aibook-web` `aibook-worker` `aibook-tunnel` `aibook-opencode-serve` |
| m3 | 82.157.27.78 | 数据库 | PostgreSQL 16 |
| m4 | 82.157.178.64 | 边缘入口 | nginx（容器名 `nginx`），mindos 共用 |

**红线**：m4 上 nginx 同时服务 mindos（`mindos.zxixing.com`）。部署改动必须确认 mindos `/health` 仍 200。systemd `nginx.service` 的 failed 状态是历史噪音，不要去"修复"。

SSH 工具：`sshx.py <m1|m2|m3|m4> "cmd"`，上传 `sshx.py <host> --file <local> <remote>`（host 必须是第 1 个参数）。复杂命令写脚本文件再上传执行，PowerShell 会吃 `$变量` 和内层引号。

---

## 代码结构

```
ai-book-skill/
├── web/
│   ├── app.py              # FastAPI 主应用（30 个端点）
│   └── static/             # index.html + app.js + style.css + vendor/
├── pipeline/
│   ├── worker.py           # 转换/翻译执行器（CLI + daemon）
│   ├── registry.py         # 数据访问层（12 表 + 索引 + FAQ 聚类）
│   ├── llm.py              # 模型链客户端（降级/超时/深度问答）
│   ├── gate.py             # 产物安全扫描（注入/越权模式）
│   ├── cosutil.py          # 腾讯云 COS 上传下载
│   └── config.json         # 运行配置（gitignore，模板见 kit/）
├── skill-server/
│   └── server.py           # 本地书库 MCP（5 工具，读 sqlite + skills 目录）
├── mcp_server.py           # 远端书库 MCP（10 工具，走 HTTP API）
├── kit/                    # 安装包源（setup.py / install.ps1 / install.sh）
├── .opencode/
│   ├── agent/publisher.md  # 无人值守转换 agent
│   └── skills/aibook/      # 远端书库 skill（何时用 + 怎么串）
└── docs/                   # 架构 / API / 运维 / 规划
```

---

## 数据模型

12 张表（sqlite / PG 同 schema）：

| 表 | 用途 |
|---|---|
| `books` | 书籍元数据：slug、title、author、intro、space_id、visibility、status、token_estimate |
| `chapters` | 章节：book_slug、no、title、path、lang |
| `jobs` | 任务队列：kind（convert/translate）、status、lease、attempts、model |
| `interactions` | 问答日志：channel、tool、question、result_count、answer |
| `faqs` | FAQ 人工答案：scope_slug、norm_q、question、answer |
| `users` / `sessions` / `api_tokens` | 账号体系（密码哈希、30 天会话、`ak_` 令牌） |
| `answer_cache` | 问答缓存：slug + norm_q + mode 唯一 |
| `translations` | 译文：lang（zh/bi）、dir、model |
| `index_files` / `index_lines` | **行级内容索引**：每行文本 + 所属文件/书，供跨书检索 |

**跨书检索原理**：`index_lines` 存每行 ≤400 字的文本；查询时按停用词分词，**逐词独立 OR 检索 + 内存合并计分**（非 AND，避免多词查询零命中），按 `(-score, book_slug, path)` 排序。索引 8 书 / 172 文件 / 11,435 行约 23s，检索 0.004s vs 文件扫描 0.04s。

---

## 核心链路

### 1. 导入转换

```
upload (PDF) → sha256 去重 → jobs 表入队 (kind=convert)
  → worker claim (lease 300s + heartbeat)
  → opencode run (publisher agent, Steps 0-10)
  → gate.py 扫描产物 (EXIT=0 才放行)
  → publish: 写 skills/<slug>/ + upsert books/chapters
  → reindex_book (填 index_files/index_lines)
  → push_peer (m1→m2) + 可选 COS
```

### 2. 问答降级链

`answer_chain` 逐个尝试，任一成功即返回：

```json
"answer_chain": ["deepseek/deepseek-chat", "xiaopuyun/deepseek-v4-flash", "opencode/mimo-v2.6-flash-free"]
"model_timeouts": {"deepseek/deepseek-chat": 25, "xiaopuyun/deepseek-v4-flash": 40, "opencode/mimo-v2.6-flash-free": 20}
"answer_max_tokens": 4000
```

- **推理模型陷阱**：`xiaopuyun/deepseek-v4-flash` 会把 token 预算花在 `reasoning_content` 上，`max_tokens` 太小会导致 `content` 为空 → 视为失败降级到下一模型。`reasoning_fallback` 默认 `false`（把思考当正文会污染翻译）。
- **免费模型黑洞**：`opencode/mimo-v2.6-flash-free` 经 `opencode serve` 可能 100s 无响应 → 必须设短超时。
- deep 模式：`deep_answer_async` 走工具调用（search/read），`DEEP_SEM` 限流 4 并发，总超时 60s。

### 3. 鉴权

- Cookie 会话（`/api/login` → 30 天）用于网页
- `Authorization: Bearer ak_...` 用于 API/MCP
- 可见空间：`["local"] + [user.space_id]`，`public` 书在 `local` 空间

---

## 配置项

`pipeline/config.json`（生产在 `/opt/aibook/app/pipeline/config.json`）：

| 键 | 说明 |
|---|---|
| `model` / `model_chain` | 转换用模型与降级链 |
| `translate_chain` | 翻译模型链 |
| `answer_model` / `answer_chain` | 问答模型链 |
| `model_timeouts` | 按模型覆盖超时（秒）——**防挂死的关键** |
| `answer_max_tokens` / `deep_max_tokens` | 回答 token 预算（推理模型建议 ≥4000） |
| `reasoning_fallback` | 是否把 reasoning_content 当正文（默认 false） |
| `skills_home` | 书库存放根目录 |
| `registry_db` | sqlite 路径或 `postgresql://` DSN |
| `opencode_bin` / `opencode_serve_url` | opencode 可执行与 serve 地址 |

环境变量：`DEEP_MAX_CONCURRENCY`（默认 4）、`DEEP_TOTAL_TIMEOUT`（默认 60s）、`AIBOOK_BASE_URL`、`AIBOOK_TOKEN`。

---

## 本地开发

```powershell
# 依赖
pip install -r requirements.txt

# 起 web（sqlite 模式）
python web/app.py

# 起 worker（处理队列）
python pipeline/worker.py --daemon

# 单次转换
python pipeline/worker.py --pdf path/to/book.pdf --slug my-book

# 回填索引
python pipeline/registry.py --reindex
```

默认账号：`test@example.com` / `test123456`（admin）。

---

## 测试

回归脚本在 `%TEMP%\opencode\`（不入库）：

| 脚本 | 覆盖 |
|---|---|
| `test_index.py` | 行级索引检索（28 项） |
| `test_library_ask.py` | 跨书问答端点（23 项） |
| `visibility_e2e.py` | 可见性权限（13 项） |
| `upload_zh_neg.py` | 中文上传负例（8 项） |
| `test_zh_zip.py` | 中文 zip 解析（10 项） |
| `test_kit_e2e.py` | 安装包端到端（60 项） |
| `public_e2e.py` | 公网全链路（含 fast+deep 问答） |

部署后必查：四端点 200 + mindos `/health` 200 + 服务 active。

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [docs/API.md](docs/API.md) | 30 个 HTTP 端点 + 15 个 MCP 工具完整参考 |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | 部署、配置、排错、监控运维手册 |
| [ROADMAP.md](ROADMAP.md) | 下一阶段功能规划 |
| [kit/README.md](kit/README.md) | 安装包使用说明 |
| [.opencode/skills/aibook/SKILL.md](.opencode/skills/aibook/SKILL.md) | agent 侧使用指引 |
