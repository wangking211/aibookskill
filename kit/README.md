# aibook-kit —— 书库客户端 + 本地转换栈 安装包

解压后在本目录运行一条命令即可安装（Windows）：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

macOS / Linux：

```bash
bash install.sh
```

或直接调用安装器（参数两者等价）：

```bash
python setup.py --root . --token ak_xxx
```

## 装了什么

| 内容 | 位置 | 作用 |
|---|---|---|
| `mcp_server.py` | `<root>/` | **aibook MCP 客户端**（10 个工具，连 aibook.zxixing.com） |
| `skill-server/server.py` | `<root>/skill-server/` | **book-library MCP**（查询本机已转换的书） |
| `.opencode/agent/publisher.md` | `<root>/.opencode/agent/` | 本地转换用的无人值守 agent |
| `pipeline/*` | `<root>/pipeline/` | 本地转换/翻译/推送栈（worker、gate、llm、registry） |
| `pipeline/config.json` | 生成 | 上面这些的运行配置（绝对路径已按本机填充） |
| `opencode.json` | 合并 | 往你已有的 `opencode.json` 里 upsert 两个 MCP 段 |
| `book-to-skill` | `<skills-home>/book-to-skill/` | 书籍 → skill 的转换技能（扫描门禁脚本也在这里） |
| `aibook` | `<skills-home>/aibook/` | 何时用 aibook MCP 的技能说明 |
| 依赖 | 当前 Python 环境 | `mcp` / `pymupdf` / `httpx` |

`<skills-home>` 默认 `~/.agents/skills`，即 opencode 全局技能目录，
这样**转换出来的书会自动变成可被 agent 调用的 skill**。
用 `--skills-home` 可改。

## 前置条件

1. **Python >= 3.9**（安装脚本会用运行 `setup.py` 的那个解释器装依赖，
   并把它的绝对路径写进 `opencode.json`，保证 opencode 拉起 MCP 时用的是同一份环境）
2. **opencode**（只要用本地转换 / 本地翻译才需要；纯远端转换不用）
   - 版本必须 **>= 1.19.0**。1.2.x 的 `opencode serve` 与本流水线不兼容。
   - 装好后 `opencode auth login` 登录，免费模型 `opencode/mimo-v2.6-flash-free` 才可用。
3. **API 令牌**：打开 https://aibook.zxixing.com → 顶部「令牌」→ 创建，
   然后重跑安装器把令牌写进配置：

```bash
python setup.py --token ak_xxx
```

不给令牌也能装，但 aibook MCP 的写操作会返回 401。

## 常用命令

```bash
python setup.py --help                       # 全部参数
python setup.py --token ak_x --force         # 覆盖已生成的配置/已装 skill
python setup.py --no-deps                    # 只写配置，不跑 pip
python setup.py --model deepseek/deepseek-chat   # 换默认模型
```

常用参数：`--root` `--token` `--base-url` `--skills-home` `--model` `--force` `--no-deps`

## 两种转换方式

**A. 服务端转换（推荐，零本地依赖）**
在 opencode 里调 `upload_book('/path/book.pdf')` → `get_job(id)` 轮询到 `success` →
`download_skill(slug)`。转换全部在服务器完成。

**B. 本地转换 + 推送**（需要 opencode + 本包）

```bash
# 转换并把成品 skill + 原始 PDF 推到服务器
python pipeline/worker.py --pdf /path/book.pdf --upload

# 先看会发什么 prompt，不实际执行
python pipeline/worker.py --pdf /path/book.pdf --dry-run

# 把某本书拉到本地、译成中文并回传（已译过则跳过，--force 重译）
python pipeline/worker.py --translate <slug> --force

# 常驻消费任务队列
python pipeline/worker.py --daemon

# 看本地任务
python pipeline/worker.py --list
```

## MCP 工具（aibook，远端）

`list_books` `get_book` `get_chapter` `ask` `ask_library` `upload_book`
`translate_book` `get_job` `download_skill` `download_source` `set_visibility`

跨书问题用 `ask_library(question)`：在全部可见书里检索并综合，回答带
`【引用:《书名》·章节】`；只问某一本已知的书时仍用 `ask(slug, question)`。

`book-library`（本机）：`list_books` `get_book` `get_chapter` `ask`
`pack_context` `read_chapter` `search` —— 两套都有 `list_books`，别混用：
查远端书库用 `aibook`，查本机已装的书用 `book-library`。

## 配置文件

**`pipeline/config.json`**（由 `config.json.template` 渲染）

- `model` / `model_chain` —— 转换用的模型与降级链
- `translate_chain` —— 翻译模型链（第一个失败才落到第二个）
- `answer_model` / `answer_chain` —— 问答用；`opencode/` 前缀会走本地 `opencode serve`
- `model_timeouts` —— 按模型覆盖超时（秒）。免费模型常会长时间不响应，
  默认给 `opencode/mimo-v2.6-flash-free` 20 秒，超时后立刻降级到下一个模型
- `answer_max_tokens` / `deep_max_tokens` —— 回答 token 预算。**推理模型**
  （如 `xiaopuyun/deepseek-v4-flash`）会把预算先花在思考上，太小会导致正文为空，
  体积大的提问建议 ≥4000
- `reasoning_fallback` —— 默认 `false`。开启后，若模型只回思考过程不回正文，
  就把思考过程当答案（会污染翻译，非必要别开）
- `opencode_bin` —— 安装器自动探测到的 opencode 路径
- `skills_home` / `scan_tool` —— 书 skill 目录与安全扫描脚本
- `registry_db` —— 相对路径会解析到 `<root>/pipeline/registry.db`

**`opencode.json`** —— 只 upsert `mcp.aibook` 与 `mcp.book-library`，
你已有的其它字段一律保留。`command[0]` 是解释器绝对路径，
`AIBOOK_TOKEN` 明文写在文件里（与 opencode 的凭证文件同一保护级别）。

## 排错

| 现象 | 处理 |
|---|---|
| `[ERR] 未找到 Python` | 装 Python >=3.9 并确保在 PATH 里，或用 `py setup.py ...` |
| `缺少依赖: mcp ...` | `python -m pip install -r requirements.txt` |
| `未找到 opencode` | 只影响本地转换；装 https://opencode.ai 后重跑 `python setup.py` |
| `opencode 1.2.x 与本流水线不兼容` | 升级到 >=1.19.0 |
| MCP `401` | 令牌缺失/被删 → 网页「令牌」重建 → `python setup.py --token ak_xxx` |
| MCP `403` | 书存在但不在你可见的空间，换书或让作者设为 `public` |
| 免费模型报 geo/403 | 间歇性；重试，或 `python setup.py --model deepseek/deepseek-chat` |
| 转换卡住 | `python pipeline/worker.py --list` 看 job；`--job <id>` 单跑一次 |
| 想彻底移除 | 删 `<root>` 下本包文件 + `<skills-home>/{book-to-skill,aibook}` + `<root>/opencode.json` 里的两个 MCP 段 |
