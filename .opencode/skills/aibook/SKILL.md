---
name: aibook
description: Remote aibook.zxixing.com book-library client. Use when researching or writing and a
  shared-library source book might exist, importing a local PDF/EPUB, translating a book to
  Chinese/bilingual, asking a question about a specific book, downloading converted artifacts, or
  changing a book's public/private visibility. Needs the aibook MCP (10 tools) connected.
---

# aibook 书库（远端 MCP）

连 https://aibook.zxixing.com，全部工具需 `AIBOOK_TOKEN`（`ak_` 开头）。

## 何时用

- 研究/写作怀疑**书库已有此书** → 先检索再引用，不要凭记忆编造引文
- 导入本地 PDF/EPUB、把书译成中文/中英对照、围绕某书提问、下载产物

不适用：查本机已安装的书 → 用本地 `book-library` MCP（它的 `list_books` 与本套同名，别混用）。

## 工作流

**查书**：`list_books(q)` 拿 slug → `get_book(slug)` 看章节 → `get_chapter(slug, no, lang)`

- slug **必须**取自 `list_books` / `get_book`，不要猜
- `lang`：`orig` 原文 | `zh` 中文 | `bi` 中英对照

**问答**：已知章节直接 `get_chapter` 最快；跨章归纳 / 不知在哪章才 `ask(slug, question)`。
`deep=false` 日常够用，`deep=true` 更全但可能数十秒，仅需完整答案时开。

**跨书问答**：问题**跨多本书**、或不知道在哪本书里时，用 `ask_library(question)` —— 它在全部
可见书里检索并综合，回答带 `【引用:《书名》·章节】`。比逐本 `ask` 便宜得多；只要单本已知书时仍用 `ask`。

**导入**：`upload_book(file_path)` → `job_id` → `get_job(job_id)` 轮询至 `success` / `failed`。
转换以分钟计，**别高频轮询**；同文件重复传会秒传。

**翻译**：`translate_book(slug, lang_mode)` → `get_job` 轮询 → `get_chapter(lang=zh|bi)` 验收。
`lang_mode`：`zh` 只中文 | `bi` 中英对照。

**下载**：`download_skill` 产物 zip / `download_source` 原始文件，`dest` 留空存当前目录。

**可见性**：`set_visibility(slug, visibility)` — `public` 所有人可见 | `private` 仅自己与管理员。
只有**作者或管理员**能切换；返回新 `visibility` 与 `space_id`。

## 约定与排错

- **新上传的书默认 `private`**（落在你自己的空间）；要分享给别人需显式 `set_visibility(public)`
- 现存少数书是 `public`（`space_id=local`），人人可读

| 情况 | 处理 |
|---|---|
| `401` | 令牌缺失/被删 → 网页「设置→API 令牌」重建并更新 `AIBOOK_TOKEN` |
| `403` | 书存在但不在可见空间，别重试 |
| `job failed` | 看 `detail`；换 `model` 重传 |
| `job_id` 找不到 | 可能已清理，重新触发 |

只讲"何时用 + 怎么串"，参数签名以 MCP 工具描述为准。
