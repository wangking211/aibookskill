# API 参考

线上 base：`https://aibook.zxixing.com`

鉴权两种方式：
- **Cookie 会话**：`POST /api/login` 后浏览器自动携带
- **API 令牌**：`Authorization: Bearer ak_...`（网页「设置→API 令牌」创建）

除非标注「公开」，所有端点都需要登录或令牌。

---

## 1. 认证与令牌

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/register` | `{identifier, password, display_name?}` 注册并自动登录 |
| POST | `/api/login` | `{identifier, password}` → Set-Cookie（30 天） |
| POST | `/api/logout` | 销毁当前会话 |
| GET | `/api/me` | 当前用户 `{id, identifier, display_name, space_id}` |
| POST | `/api/tokens` | 创建 API 令牌，返回 `{token: "ak_...", name}` |
| GET | `/api/tokens` | 列出当前用户全部令牌 |
| DELETE | `/api/tokens/{token}` | 删除指定令牌 |

---

## 2. 书库浏览

### GET /api/books
列出可见书籍。可见空间 = `["local"] + [你的 space_id]`。

响应：
```json
[{"slug": "english-framework", "title": "英语框架", "author": "...",
  "intro": "...", "cover_url": "...", "chapter_count": 12,
  "token_estimate": 285180, "status": "published",
  "visibility": "public", "space_id": "local", "lang": "orig"}]
```

### GET /api/books/{slug}
单本书详情 + 章节列表 + 已有译文。

### GET /api/books/{slug}/cover
封面图（PNG，302 到 COS 或本地）。

### GET /api/books/{slug}/chapters/{no}?lang=orig|zh|bi
章节正文。`lang`：
- `orig` 原文
- `zh` 中文译文（未译则 404）
- `bi` 中英对照（服务端拼接，前端 `applyBilingual` 左右分栏）

### GET /api/books/{slug}/download/source
原始 PDF/EPUB 下载。

### GET /api/books/{slug}/download/skill
skill 包 zip（SKILL.md + chapters/）。

---

## 3. 导入与转换

### POST /api/upload
multipart 上传 PDF。字段：
- `file`：PDF 文件
- `slug`：可选，留空自动生成
- `type`：`text` / `technical`（可空）
- `depth`：`study` / `reference`（可空）
- `model`：指定转换模型（可空=自动）
- `lang`：`zh` / `bi`（可空=只转不译）

同 sha256 已存在则秒传。返回 `{job_id, slug, deduped: bool}`。

### POST /api/upload/presign + /api/upload/complete
大文件分片上传两步走：先换 COS 预签名 URL，传完后确认入库。

### POST /api/books/import
导入已转换的 skill 目录（需含 SKILL.md + chapters/），不走 LLM。

### POST /api/books/{slug}/process
重新触发转换（已入库的书重跑）。参数同 upload 的可选字段。

### POST /api/books/{slug}/translate
`{lang_mode: "zh"|"bi", model?}` 触发翻译任务。

### POST /api/books/{slug}/upload-zh
上传现成中文译文 zip（含 `chapters_zh/` 或 `chapters_bi/`），跳过翻译直接入库。

### POST /api/books/{slug}/visibility
`{visibility: "public"|"private"}`。仅作者或管理员可改。

---

## 4. 任务队列

### GET /api/jobs
当前用户任务列表，按时间倒序。

```json
[{"job_id": "...", "kind": "convert", "status": "success",
  "slug": "...", "pdf_name": "...", "model": "...",
  "created_at": 1791983728, "started_at": ..., "finished_at": ...,
  "detail": "...", "attempts": 1}]
```

`status`：`queued` → `running` → `success` / `failed`

### GET /api/jobs/{job_id}/log
任务执行日志（转换过程的 opencode 输出）。

---

## 5. 问答

### POST /api/ask — 单书问答
```json
{"slug": "english-framework", "question": "楼房模型是什么？", "deep": false}
```

响应：
```json
{"hits": [{"path": "chapters/ch03-....md", "score": 2.1, "snippet": "...",
           "chapter_no": 3, "chapter_title": "..."}],
 "answer": "...", "answer_error": null, "mode": "fast", "cached": false}
```

- `deep=false`（fast）：行级索引检索 top-6 片段 → 一次 LLM 调用，秒级
- `deep=true`：工具调用 agent（search/read），分钟级，`DEEP_SEM` 限流 4 并发
- 相同 `norm_question` + mode 命中 `answer_cache`

### POST /api/library/ask — 跨书问答
```json
{"question": "这几本书对「框架」和「决策」怎么看？", "deep": false}
```

响应额外含 `books`（涉及的书）：
```json
{"hits": [{"book_slug": "...", "book_title": "...", "path": "...",
           "chapter_title": "...", "score": ...}],
 "answer": "...【引用:《错误的行为》·Chapter 4】...",
 "books": [{"slug": "...", "title": "..."}]}
```

原理：对全部可见书的 `index_lines` 检索 → 每书取 top 片段 → 拼 catalog + excerpts → 一次 LLM 综合。

**注意**：提问会自动写入 `interactions`，并进入 FAQ 聚类池。

---

## 6. FAQ

### GET /api/faq
从历史问答聚类的高频问题。排序：已回答 > 出现次数。

```json
[{"scope_slug": "", "norm_q": "...", "question": "损失厌恶是什么",
  "count": 5, "answer": "...", "answered": true}]
```

`scope_slug=""` 表示全局（含跨书问答），非空表示单书。

### GET /api/hot-questions
最近高频问题（用于首页推荐）。

### POST /api/faq
`{scope_slug, norm_q, question}` 手动添加 FAQ 条目。

### POST /api/faq/answer
`{scope_slug, norm_q, answer}` 补充/更新答案。

---

## 7. MCP 工具

### 远端 MCP（`mcp_server.py`，10 工具）

配 opencode：
```json
{"mcp": {"aibook": {"type": "local", "command": ["python", "mcp_server.py"],
  "environment": {"AIBOOK_BASE_URL": "https://aibook.zxixing.com",
                  "AIBOOK_TOKEN": "ak_xxx"}}}}
```

| 工具 | 参数 | 说明 |
|---|---|---|
| `list_books` | `q?` | 按关键词搜书 |
| `get_book` | `slug` | 书详情 + 章节列表 |
| `get_chapter` | `slug, no, lang=orig` | 读章节 |
| `ask` | `slug, question, deep=false` | 单书问答 |
| `ask_library` | `question, deep=false` | 跨书问答 |
| `search` | `query, slug?, limit=8` | 内容检索 |
| `pack_context` | `slugs, question, max_per_book=3` | 多书打包上下文 |
| `upload_book` | `file_path, slug?, lang_mode?, model?` | 导入本地 PDF |
| `translate_book` | `slug, lang_mode=zh` | 触发翻译 |
| `get_job` / `download_skill` / `download_source` | — | 任务轮询与产物下载 |
| `set_visibility` | `slug, visibility` | 切换公开/私有 |

### 本地 MCP（`skill-server/server.py`，5 工具）

读本地 `skills_home` + sqlite，不走网络。工具：`list_books` `get_book` `read_chapter` `search` `pack_context`。

---

## 8. 错误码

| 码 | 含义 | 处理 |
|---|---|---|
| 400 | 参数缺失/非法 | 检查请求体 |
| 401 | 未登录 / 令牌无效 | 重新登录或重建令牌 |
| 403 | 书存在但不在可见空间 | 换有权限的账号，别重试 |
| 404 | 书/章节/任务不存在 | 确认 slug 与 no |
| 409 | slug 冲突 | 换 slug 或用已有书 |
| 422 | 请求体校验失败 | 检查 JSON 格式与必填字段 |
| 429 | 排队较多（deep/跨书问答限流） | 稍后重试 |
| 500 | 服务端错误 | 看 `detail`；模型不可用会自动降级 |

模型链全部失败时返回 `answer_error`（含每个模型的失败原因），不返回 500。
