# 运维手册

部署、配置、排错、监控。读者：运维与值班。

---

## 1. 部署拓扑速查

| 主机 | IP | 角色 | 关键服务 |
|---|---|---|---|
| m1 | 43.138.19.118 | 主 API + worker | `aibook-web` `aibook-worker` `aibook-tunnel` `aibook-opencode-serve` `aibook-sync.timer` |
| m2 | 49.233.160.151 | 备 API + worker | 同上（无 sync.timer） |
| m3 | 82.157.27.78 | PostgreSQL 16 | — |
| m4 | 82.157.178.64 | 边缘 nginx | 容器名 `nginx`，mindos 共用 |

**红线**：m4 上 nginx 同时服务 mindos（`mindos.zxixing.com`）。任何 nginx 改动后必须验证 mindos `/health` 仍 200。systemd `nginx.service` 的 failed 状态是历史噪音，不要"修复"。

---

## 2. 服务管理

```bash
# 查看状态
systemctl is-active aibook-web aibook-worker aibook-tunnel aibook-opencode-serve

# 重启（改代码/配置后）
systemctl restart aibook-web aibook-worker

# 看日志（注意 worker 的 print 缺 flush，日志可能延迟）
journalctl -u aibook-web --since '30 min ago' --no-pager
journalctl -u aibook-worker -f
```

- **aibook-web**：FastAPI，监听 `127.0.0.1:8787`（不是 8000，8000 是 mindos-api docker）
- **aibook-worker**：`python pipeline/worker.py --daemon`
- **aibook-tunnel**：m1 → m4 的反向隧道
- **aibook-opencode-serve**：`opencode serve`，:4096
- **aibook-sync.timer**（仅 m1）：每 60s rsync 内容目录 m1→m2

---

## 3. 配置管理

配置文件：`/opt/aibook/app/pipeline/config.json`（m1/m2 各一份，需保持一致）

改配置流程：
1. 改前备份：`cp config.json config.json.bak`
2. 用脚本 patch（PowerShell 内联 JSON 会坏）
3. `systemctl restart aibook-web aibook-worker`
4. 验证：`systemctl is-active` + 四端点 200

**关键配置项**：

| 键 | 说明 | 注意 |
|---|---|---|
| `answer_chain` | 问答模型降级链 | 快而稳的放前面 |
| `model_timeouts` | 按模型超时（秒） | **防挂死关键**，免费模型必设短超时 |
| `answer_max_tokens` | 回答 token 预算 | 推理模型建议 ≥4000，太小会 content 为空 |
| `reasoning_fallback` | 把思考当正文 | 默认 false，开启会污染翻译 |
| `registry_db` | sqlite 路径或 PG DSN | 生产用 PG，本地开发 sqlite |

环境变量：`DEEP_MAX_CONCURRENCY`（默认 4）、`DEEP_TOTAL_TIMEOUT`（默认 60s）。

---

## 4. 数据库

- **生产**：m3 PostgreSQL 16，DSN 在 `config.json` 的 `registry_db`
- **本地开发**：sqlite `pipeline/registry.db`
- **12 张表**：books / chapters / jobs / interactions / faqs / users / sessions / api_tokens / answer_cache / translations / index_files / index_lines

迁移：代码启动时自动 `_migrate()`，无需手工执行 SQL。

备份：PG 走 `pg_dump`，内容目录走 m1→m2 rsync（已有）+ 可选异地备份。

---

## 5. 排错手册

### 问答挂死/超时
1. 先查是不是模型延迟：`curl` 单模型测延迟
2. 确认 `model_timeouts` 有设，免费模型超时 ≤20s
3. 推理模型 content 为空 → 检查 `answer_max_tokens` 是否 ≥4000
4. 检查 `DEEP_SEM` 是否被僵尸请求占满（deep 模式限流 4）

### 上传转换失败
1. `GET /api/jobs/{id}/log` 看 opencode 输出
2. 常见：模型不可用 → 换 model 重传；产物扫描不过 → 看 gate 报告
3. 同文件重复传会秒传（sha256 去重）

### 服务起不来
1. `journalctl -u aibook-web -n 50` 看报错
2. 端口占用：8787 被占？（注意 8000 是 mindos，别混淆）
3. 配置 JSON 语法错 → 用 `python -c "import json; json.load(open('config.json'))"` 验证

### 索引异常
```bash
# 回填全部索引（词面 index_lines，PG）
python pipeline/registry.py --reindex
# 单书重建（在 python 里）
import registry; registry.reindex_book(con, slug, skills_home)
```

### 向量索引（embed.db）— 每机本地
向量存 **每机本地** `pipeline/embed.db`（sqlite float32 BLOB，~51MB/机），不走 PG
（m1↔m3 冷加载 15-30 分钟，不可接受）。PG `index_embed` 表弃用留作备份。

```bash
# 每台机器（m1/m2）各自回填 —— 书重索引或换 embedding 模型后必做
cd /opt/aibook/app
/opt/aibook/venv/bin/python pipeline/embed_service.py --embed
# 确认行数（应一致，当前 12274）
/opt/aibook/venv/bin/python -c "
import sqlite3; con=sqlite3.connect('pipeline/embed.db')
print(con.execute('SELECT COUNT(*) FROM embeds').fetchone()[0])"
```

- 缓存指纹 `(count, model, updated_at)` 变化时自动重载，**无需重启**服务
- sidecar `aibook-embed.service` 端口 8799，`/health` 应 200
- 只跑 `--embed` 的那台机器会更新；另一台需单独执行（无自动同步）

### /tmp 遮蔽标准库
若 `/tmp` 有 `inspect.py` 等与 stdlib 同名的 `.py`，从 `/tmp` 跑任何 `import inspect` 的脚本都会崩。处理：`mv /tmp/*.py /opt/aibook/ops/debug-scripts/`。

---

## 6. 批量上传与并行处理

### 网页批量上传
上传页支持**多选文件**或**选择文件夹**（webkitdirectory），一次拖拽/选择多个 PDF，
前端以并发 3 逐个调用 `/api/upload`（复用单文件端点，含 sha256 秒传去重）。
进度显示在按钮上（`上传 N/M x%`），完成后自动跳到「处理任务」页。

### CLI 批量入队（服务器/本地通用）
```bash
# 扫描目录下所有 PDF（递归），逐个注册 convert 任务，不立即处理
python pipeline/worker.py --enqueue-dir /path/to/books
# 指定 space（默认 local）
python pipeline/worker.py --enqueue-dir /path/to/books --enqueue-space u433583281b64

# 入队后启动（或已在跑的）worker 消化队列
python pipeline/worker.py --daemon    # 常驻
python pipeline/worker.py --once      # 处理完退出
```

- **去重**：入队前按 sha256 查 `find_books_by_sha`，已发布同书直接跳过
- **文件位置**：PDF 复制进 `inbox/`，由 `aibook-sync.timer`（m1→m2）60s 内同步到对端
- **并行**：每台机器一个 `aibook-worker` 进程 = 2 路并行（m1+m2）；同机多开 `--daemon` 可再加路数
- **认领**：`claim_next_job` 乐观锁 + 同 slug 串行，多 worker 安全

---

## 7. 部署检查清单

改代码/配置后必查：

```bash
# 1. 服务
systemctl is-active aibook-web aibook-worker aibook-tunnel aibook-opencode-serve

# 2. 端点
curl -s -o /dev/null -w "%{http_code}" https://aibook.zxixing.com/
curl -s -o /dev/null -w "%{http_code}" https://aibook.zxixing.com/api/books
curl -s -o /dev/null -w "%{http_code}" https://aibook.zxixing.com/api/faq
curl -s -o /dev/null -w "%{http_code}" https://aibook.zxixing.com/static/aibook-kit.zip
# chat 需登录，匿名应 401（验证路由已注册）
curl -s -o /dev/null -w "%{http_code}" -X POST https://aibook.zxixing.com/api/chat \
  -H 'Content-Type: application/json' -d '{"question":"x"}'

# 3. mindos（红线）
curl -s -o /dev/null -w "%{http_code}" https://mindos.zxixing.com/health
```

全部应为 200。

---

## 8. 同步与一致性

- **代码**：m1/m2 需手动保持一致（rsync 不覆盖 `/opt/aibook/app` 代码，只同步内容目录）
- **内容**：`aibook-sync.timer` 每 60s rsync `skills/ covers/ sources/ inbox/ library/` m1→m2
- **数据库**：PG 单点（m3），双活 web 共享

改代码后需分别上传到 m1 和 m2，或用 sync 脚本。

---

## 9. 监控与告警（待建）

当前无自动告警。人工巡检项：
- 四端点 200 + mindos 200
- 服务 active
- `pg_stat_activity` 无长期阻塞查询
- 磁盘 / 内存 / 日志大小

P0 计划中的「用量与成本看板」会补上模型成功率/延迟监控。
