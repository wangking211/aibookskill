"""AIBOOK MCP server — 通过 HTTP API 访问 aibook.zxixing.com 书库，供 opencode/Claude 等 agent 共享使用。

配置（环境变量）:
  AIBOOK_BASE_URL  服务地址，默认 https://aibook.zxixing.com
  AIBOOK_TOKEN     API 令牌（ak_ 前缀），在网页"设置→API 令牌"创建，或 POST /api/tokens

运行（stdio）:
  AIBOOK_TOKEN=ak_xxx python mcp_server.py
opencode 配置示例见文件末尾注释。
"""
import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE = os.environ.get("AIBOOK_BASE_URL", "https://aibook.zxixing.com").rstrip("/")
TOKEN = os.environ.get("AIBOOK_TOKEN", "")


def _need_token():
    if not TOKEN:
        raise RuntimeError("缺少环境变量 AIBOOK_TOKEN（ak_ 开头），请先在网页创建 API 令牌")


def _req(path, payload=None, method=None, timeout=90):
    _need_token()
    req = urllib.request.Request(BASE + path, data=(
        json.dumps(payload).encode() if payload is not None else None), method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode("utf-8", "replace")[:300]}
    try:
        return json.loads(raw)
    except Exception:
        return raw.decode("utf-8", "replace")


def _req_bytes(path, timeout=180):
    _need_token()
    req = urllib.request.Request(BASE + path)
    req.add_header("Authorization", "Bearer " + TOKEN)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Disposition", "")


def _post_multipart(path, fields, files, timeout=300):
    """fields: {k:v}; files: {name: (filename, bytes, content_type)} → dict 响应。"""
    _need_token()
    boundary = "----aibookBoundary" + uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode()
    for name, (fname, data, ctype) in files.items():
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{name}"; filename="{fname}"\r\n'
                 f"Content-Type: {ctype}\r\n\r\n").encode()
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(BASE + path, data=bytes(body), method="POST")
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode("utf-8", "replace")[:300]}
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw.decode("utf-8", "replace")[:300]}


def _dl(path, dest, default_name):
    data, cd = _req_bytes(path)
    if dest:
        out = Path(dest)
    else:
        name = default_name
        if "filename=" in (cd or ""):
            name = cd.split("filename=")[-1].strip('" ')
        out = Path.cwd() / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return json.dumps({"saved": str(out), "bytes": len(data)}, ensure_ascii=False)


try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from mcp.server.mcpserver import MCPServer as FastMCP

mcp = FastMCP(
    "aibook",
    instructions=("访问 aibook.zxixing.com 本地书库：搜索/浏览书籍、读取章节（原文/中文/中英对照）、"
                  "对书籍提问、触发翻译、下载原始文件与处理产物。需环境变量 AIBOOK_TOKEN。"))


@mcp.tool()
def list_books(q: str = "") -> str:
    """列出书库书籍；可用 q 按书名/作者/简介关键词过滤。"""
    path = "/api/books" + (("?q=" + urllib.parse.quote(q)) if q else "")
    return json.dumps(_req(path), ensure_ascii=False, indent=2)


@mcp.tool()
def upload_book(file_path: str, slug: str = "", lang_mode: str = "", model: str = "") -> str:
    """上传一本本地书（PDF/EPUB 等）到服务器并加入 AI 转换队列。

    file_path: 本机文件绝对路径。slug 可选书名标识；lang_mode: ""(原文)|zh|bi；
    model 可指定转换模型（留空走自动链）。返回 job_id，用 get_job 轮询转换进度。
    """
    p = Path(file_path).expanduser()
    if not p.is_file():
        return json.dumps({"error": f"file not found: {file_path}"}, ensure_ascii=False)
    fields = {}
    if slug:
        fields["slug"] = slug
    if lang_mode:
        fields["lang_mode"] = lang_mode
    if model:
        fields["model"] = model
    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    resp = _post_multipart("/api/upload", fields,
                           {"file": (p.name, p.read_bytes(), ctype)}, timeout=600)
    return json.dumps(resp, ensure_ascii=False, indent=2)


@mcp.tool()
def get_book(slug: str) -> str:
    """按 slug 获取书籍详情与章节列表。"""
    return json.dumps(_req(f"/api/books/{urllib.parse.quote(slug)}"),
                      ensure_ascii=False, indent=2)


@mcp.tool()
def get_chapter(slug: str, no: int, lang: str = "orig") -> str:
    """读取某书第 no 章内容。lang: orig(原文) | zh(中文) | bi(中英对照)。"""
    return json.dumps(_req(f"/api/books/{urllib.parse.quote(slug)}/chapters/{no}?lang={lang}"),
                      ensure_ascii=False, indent=2)


@mcp.tool()
def ask(slug: str, question: str, deep: bool = False) -> str:
    """对某书提问。deep=False 快速回答；deep=True 深度回答（更慢更全，可能数十秒）。"""
    return json.dumps(_req("/api/ask", {"slug": slug, "question": question, "deep": deep},
                           timeout=120 if deep else 60), ensure_ascii=False, indent=2)


@mcp.tool()
def ask_library(question: str, deep: bool = False) -> str:
    """跨书库提问：在你可见的所有书籍里检索相关片段并综合作答，回答带《书名》·章节 引用。

    适合"这几本书对 X 怎么看""书库里有没有讲过 Y""把 N 本书的 Z 对比一下"这类问题。
    只问某一本已知的书时，用 ask(slug, question) 更快更准。
    deep=True 检索更广（更多本书/更多片段），耗时更长。
    """
    return json.dumps(_req("/api/library/ask", {"question": question, "deep": deep},
                           timeout=180 if deep else 120),
                      ensure_ascii=False, indent=2)


@mcp.tool()
def translate_book(slug: str, lang_mode: str = "zh") -> str:
    """触发翻译任务（异步）。lang_mode: zh(中文) | bi(中英对照)。返回 job_id，用 get_job 轮询。"""
    return json.dumps(_req(f"/api/books/{urllib.parse.quote(slug)}/translate",
                           {"lang_mode": lang_mode}), ensure_ascii=False, indent=2)


@mcp.tool()
def get_job(job_id: str) -> str:
    """查询任务状态（翻译/转换）。轮询至 state=success 或 failed。"""
    jobs = _req("/api/jobs")
    if isinstance(jobs, list):
        for j in jobs:
            if j.get("id") == job_id:
                return json.dumps(j, ensure_ascii=False, indent=2)
        return json.dumps({"error": "job not found", "job_id": job_id}, ensure_ascii=False)
    return json.dumps(jobs, ensure_ascii=False, indent=2)


@mcp.tool()
def download_skill(slug: str, dest: str = "") -> str:
    """下载处理产物（章节/译本/封面等）打包为 zip 到本地。dest 为空则存当前目录。"""
    return _dl(f"/api/books/{urllib.parse.quote(slug)}/download/skill", dest,
               f"{slug}-skill.zip")


@mcp.tool()
def download_source(slug: str, dest: str = "") -> str:
    """下载原始上传文件（PDF/EPUB 等）到本地。dest 为空则存当前目录。"""
    return _dl(f"/api/books/{urllib.parse.quote(slug)}/download/source", dest,
               f"{slug}-source.bin")


@mcp.tool()
def set_visibility(slug: str, visibility: str) -> str:
    """切换书籍可见性。visibility: public(所有人可见) | private(仅自己与管理员)。
    只有作者或管理员能切换；返回新的 visibility 与 space_id。"""
    return json.dumps(_req(f"/api/books/{urllib.parse.quote(slug)}/visibility",
                           {"visibility": visibility}),
                      ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()

# opencode 配置示例（opencode.json / .opencode/config）:
#   "mcp": {
#     "aibook": {
#       "type": "local",
#       "command": ["python", "G:/workspace/AIProject/ai-book-skill/mcp_server.py"],
#       "environment": {
#         "AIBOOK_BASE_URL": "https://aibook.zxixing.com",
#         "AIBOOK_TOKEN": "ak_xxx你的令牌"
#       },
#       "enabled": true
#     }
#   }
