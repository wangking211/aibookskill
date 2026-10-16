# PLAN — P0 收尾与下一阶段（A1 → A4 → A2 → B7）

> 依据 ROADMAP.md + 实测环境数据。批准顺序：A1 配置 embedding → A4 端到端验收 → A2 召回标注集 → B7 多轮问答。每个 Phase 独立部署、独立验证、独立提交。

## 环境事实（已实测）

- 无可用 embedding API：xiaopuyun 96 个模型无 embedding（404）；openrouter 余额不足（402）；无 ollama
- m1/m2：16 核 / 30GB，docker 有但不用；hf-mirror.com 可达（200，0.76s）；pypi 可达（200）
- 库规模：9 本书 / 185 文件 / 12,274 行 index_lines（PG，m1/m2 共享）→ 回填一次即可
- 边缘 nginx（m4 容器 nginx）aibook.conf 已全局 `proxy_buffering off`、gzip 不含 text/event-stream → SSE 可直通
- venv 已有 fastapi/uvicorn，无 numpy/onnxruntime
- 本地 Windows 有 Edge（msedge.exe），无 playwright/puppeteer；npm registry=npmmirror
- `_migrate` 只支持 ADD COLUMN；**新表必须进 SCHEMA/SCHEMA_PG**（connect 时 executescript 自动建）

---

## Phase 1 — A1 混合检索上线（+A3 token 采集捎带）

**目标**：hybrid 真正启用（当前优雅降级为词面），查询嵌入本地 <50ms；顺手补 P0-5 缺的 tokens_in/out。

### 1a. 本地嵌入 sidecar（新文件 `pipeline/embed_service.py`）
- fastembed（ONNX，pip 装入 `/opt/aibook/venv`）加载 `BAAI/bge-small-zh-v1.5`（512 维，90MB，ROADMAP 指定）
- FastAPI 提供 **OpenAI 兼容** `POST /v1/embeddings`（`{model,input,mode?}` → `{data:[{embedding}]}`）+ `GET /health`
- `mode=="query"` 时给输入加 bge 检索前缀「为这个句子生成表示以用于检索相关文章：」（文档回填不加）
- 单例加载模型；systemd 单元 `aibook-embed.service`：127.0.0.1:8799、`Environment=HF_ENDPOINT=https://hf-mirror.com`（模型下载走镜像）、Restart=always
- sidecar down → `embed_texts` 报错 → 自动回退纯词面（已内建，零风险）

### 1b. config（m1/m2 各自 `/opt/aibook/app/pipeline/config.json`，先 `.bak4`）
```json
"embedding_model": "bge-small-zh-v1.5",
"embedding_base_url": "http://127.0.0.1:8799/v1",
"embedding_api_key": "local"
```
（`_embedding_endpoint` 自动拼 `/embeddings`，无需改 llm.py 端点逻辑）

### 1c. 代码改动（已按实测修正架构）
- `pipeline/llm.py`
  - `embed_texts(..., mode="query"|"doc")` → payload 带 `mode`（None 不带，兼容未来远端 API）
  - **A3 token 采集**：`_acc_usage` + `usage_out: dict|None` 可选参（chat/chat_async/chat_async_stream/_post_stream_async/answer_*_async/deep_answer_async 透传，返回签名不变）；流式 payload 加 `stream_options:{"include_usage":true}`（deepseek 实测接受）
- `pipeline/registry.py`
  - `embed_book` `mode="doc"`；`search_index_hybrid` `mode="query"`
  - **向量库改为每机本地 `pipeline/embed.db`（sqlite, float32 BLOB）**——原设计存 PG 共享被否决：实测 m1↔m3 链路 ~50-100KB/s，PG `index_embed` 达 101MB（TEXT float 全精度 + TOAST），每次冷加载 15-30 分钟（pg_stat_activity 卡在 ClientWrite）。向量是派生数据 → 每机各自 `--embed` 从本地 sidecar 回填（~2min/机），WAN 只承载文本
  - `_embed_con/_embed_db_path`（cfg `embed_db`，默认 `pipeline/embed.db`）；BLOB 存取（`struct.pack("<Nf")`，加载 `np.frombuffer`，兼容旧 TEXT json）；numpy 矩阵 + 进程内缓存，指纹 `(count, MAX(model), MAX(updated_at))`（MIGRATIONS 加 `index_embed.updated_at`）；无 numpy → 纯 Python 慢路径；`_agg_files` 片段行值 IN 批量化（跨机 RTT 高，避免每文件一查）
  - `main()`：`--db` 缺省取 config `registry_db`（服务器=PG DSN）；`--embed` 读 index_lines 走共享 con、向量写本地 embed.db
- `web/app.py`：ask/ask-stream/library-ask 三处接 `usage_out` → log_interaction tokens_in/out

### 1d. 部署与验收（m1、m2）✅ 已完成
1. `pip install fastembed`（0.9.0 + numpy 2.5.4 + onnxruntime 1.31.0）×2
2. `embed_service.py` + systemd `aibook-embed.service` ×2（127.0.0.1:8799、`HF_ENDPOINT=hf-mirror`、**`HF_HUB_DISABLE_XET=1`**（m1 首启 xet CAS 401，禁用后正常）、Restart=always）
3. config `.bak4` + 3 embedding 键 ×2 → restart web/worker
4. 回填 **每机各跑一次**（本地 embed.db）：`/opt/aibook/venv/bin/python -u pipeline/registry.py --embed` → 双机各 12,274 行（embed.db 51MB）
5. 验收 ✅：
   - sidecar `/health` 200 loaded=true ×2；embed.db COUNT=12,274 ×2
   - 换词面救回：hybrid_accept 2 轮均 rescued≥1（lex=n→hyb=Y）；词面 0 命中时向量补 6 文件（ask_probe）
   - 热延迟：单书 99-220ms；全库跨书 1.5-2.0s（词面 LIKE 的跨机 RTT 占大头，向量增量 <40ms）
   - edge_e2e ALL PASS（登录/ask 200+答案/SSE 流式 model=deepseek 含 usage/admin stats by_model 出现 deepseek/deepseek-chat/登出 401）
   - 四端点 200 + mindos `/health` 200 + systemctl is-active 全绿（含 aibook-embed）
6. 注意：**书内容重索引/换 embedding 模型后需重跑 `--embed`（每机）**；PG 中旧 `index_embed`（101MB）留作备份未删

---

## Phase 2 — A4 浏览器端到端验收

**目标**：真实浏览器验证 P0 五项体验，发现问题当场修。

### 2a. 工具（本地，不污染仓库）
- `C:\Users\Icy\AppData\Local\Temp\opencode\e2e\` 下 `npm i playwright-core`（npmmirror）+ `channel: "msedge"`（用系统 Edge，**零浏览器下载**）

### 2b. 验收脚本 `e2e_p0.py`（对 https://aibook.zxixing.com）
1. 登录 test@example.com
2. 单书 fast 问答：记录 **TTFB**（fetch 首个 SSE token 时间）与 token 增量到达 → 验收 <2s 首字节 + 打字机效果
3. 答案渲染后：点击 `.cite-link` → 断言切到对应书章节、`.src-hl` 高亮存在
4. 跨书问答：引用链接带正确 data-slug、点击跳转
5. 用量看板：admin 登录 → 切 `#tab-admin` → 断言 stat-card/stat-table 有数据渲染
6. 每步截图存 temp（失败时排查用）
7. curl 层补充：边缘域名测 `/api/ask/stream` TTFB（验证 nginx 不缓冲）；若缓冲 → app.py 流式响应加 `X-Accel-Buffering: no` 头（保险，不动 m4 配置）
8. mindos `/health` 200（例行）

### 2c. 预期修复项
- 流式 TTFB 超标 → 查 answer_chain 首模型首 token 延迟（deepseek 通常 <2s；不达标再评估调 chain/timeout）
- 引用跳转/高亮 DOM 断言失败 → 修 app.js
- 修完重跑全脚本至绿；四端点+mindos 终验

---

## Phase 3 — A2 召回标注集（M1 里程碑验收）

**目标**：30 条「换一种问法」测试集，hybrid 召回 ≥75%（词面基线预期 ~40%）。

### 3a. 标注（新 `eval/recall_set.json`，进 git）
- 格式：`[{id, question(换说法, 刻意避开原文关键词), book, gold:[{path, line}...], note}]`
- 生成方式：本地读 skills 原文（`C:\Users\Icy\.agents\skills\<slug>\`，与服务器 index 路径同构）挑 9 本书覆盖（~30 条，大书多条），人工改写问法
- 先跑**词面基线**：若基线 ≥75% → 说明问法不够「换」，重写该条（保证集合有区分度）

### 3b. 评测脚本（新 `eval/run_recall.py`，进 git）
- 连接：服务器上跑（m1 venv + PG + 本地 sidecar）
- 每题分别跑 `search_index`（词面）与 `search_index_hybrid`（混合）
- 指标：**recall@10（文件级）** + hit@6；输出对比表（每题命中与否 + 汇总）
- 断言 hybrid ≥75%；不达标 → 调优（bge query 前缀、RRF k、`limit_files`、按书截断）再测，循环

### 3c. 报告
- 结果写入 `eval/REPORT.md`（基线 vs hybrid、逐题表、调优记录）→ ROADMAP M1 打勾依据

---

## Phase 4 — B7 会话式多轮问答

**目标**：「刚才第二点展开讲讲」可用；会话持久化。

### 4a. 存储（进 SCHEMA + SCHEMA_PG，connect 自动建，无迁移风险）
```sql
chat_sessions(id TEXT PRIMARY KEY, space_id TEXT, user_id BIGINT, slug TEXT,   -- slug 空串=跨书
  title TEXT, created_at BIGINT, updated_at BIGINT)
chat_messages(id BIGSERIAL/IDENTITY PK, session_id TEXT, role TEXT,            -- user/assistant
  content TEXT, hits_json TEXT, created_at BIGINT)
```
- registry CRUD：`create_chat_session` / `list_chat_sessions` / `get_chat_messages`（限最近 N=12 条）/ `append_chat_message` / `delete_chat_session`；index_sessions(user, updated_at)

### 4b. API（web/app.py）
- `POST /api/chat` `{session_id?, slug?, question}` → **SSE**（复用 Phase-1 流式 gen 机制）
  - 上下文 = 最近 12 条消息（每条截断 1500 字符，总预算 ~6k tokens）+ 当题实时检索 hits（单书 `_hits_for` / 跨书 `_library_hits`）
  - **不写 answer_cache**（上下文相关，缓存会串味）；interactions 记 `tool="chat"`
  - 首轮自动建 session，done 事件带 `session_id` + hits（citation 机制复用）
- `GET /api/chat/sessions?slug=`（最近会话列表）、`GET /api/chat/sessions/{id}`、`DELETE /api/chat/sessions/{id}`
- 鉴权：`require_user`（同现有 ask）

### 4c. 前端（app.js/index.html/style.css）
- 书页问答区改**对话流**：已有单发答案作为第一条 assistant 消息；下方常驻追问输入框
- 「新对话」按钮 + 侧边「历史会话」列表（点开加载）；消息内 citation 同样可跳转（linkifyCitations 复用）
- 单书 + 跨书两处都接入；旧 `/api/ask`、`/api/ask/stream` 端点保留（MCP/API 兼容，不动）

### 4d. 部署验收
- 四端点 200 + mindos 200 + e2e 脚本扩展：提问 → 追问「展开讲讲」→ 断言回答引用了上文语境（非重新检索的泛答）+ 会话列表/刷新持久
- 回归：py_compile/node check/既有测试全绿

---

## Phase 5 — 文档同步 + 提交推送

- `docs/API.md`：`POST /api/ask/stream`(SSE 协议)、`GET /api/admin/stats`、`POST /api/chat` + sessions、`/v1/embeddings`(sidecar，内部)
- `docs/OPERATIONS.md`：aibook-embed 服务管理、`--embed` 回填命令、模型下载镜像、eval 运行方法
- `ROADMAP.md`：P0 五项打勾、M1 里程碑状态、A2 结果回填
- 提交按 Phase 分批 commit（或最后一批），askpass 推 GitHub 后删 askpass.bat

---

## 风险与对策

| 风险 | 对策 |
|---|---|
| fastembed/onnxruntime 装不上（wheel） | pypi 已测可达；失败则退 `modelscope`+onnxruntime 手搓或报回 |
| hf-mirror 模型下载慢/失败 | sidecar 加 `HF_ENDPOINT`；备选 modelscope 镜像 |
| 向量检索延迟拖垮 TTFB | numpy+count 缓存（1c），验收含 <300ms 断言 |
| 流式被边缘缓冲 | conf 已 `proxy_buffering off`；保险加 `X-Accel-Buffering: no` |
| eval 基线过高（题不够“换”） | 3b 先测词面基线，超 75% 重写题目 |
| 多轮上下文撑爆 prompt | 12 条 ×1500 字符截断 + 现有 answer_max_tokens |
| **mindos 影响** | 全程不动 m4/nginx；每 Phase 收尾必测 mindos `/health`=200 |

## 交付物清单

| Phase | 新文件 | 改动文件 | 服务器改动 |
|---|---|---|---|
| 1 | pipeline/embed_service.py | llm.py, registry.py, app.py | pip fastembed, systemd aibook-embed ×2, config .bak4 ×2, PG 回填 |
| 2 | eval 脚本(temp) | (修复项) | 无（或 app.py 流式头） |
| 3 | eval/recall_set.json, eval/run_recall.py, eval/REPORT.md | registry(调参) | 无 |
| 4 | — | registry.py, app.py, app.js, index.html, style.css | 重启 web/worker |
| 5 | — | API.md, OPERATIONS.md, ROADMAP.md | 无 |
