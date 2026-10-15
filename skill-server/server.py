import json
import os
import re
import sys
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "pipeline"))

import registry
from mcp.server.mcpserver import MCPServer

SKILLS_HOME = Path(os.environ.get("SKILLS_HOME", "C:/Users/Icy/.agents/skills"))
DB_PATH = os.environ.get("REGISTRY_DB", str(PROJECT / "pipeline" / "registry.db"))
SPACE = os.environ.get("SPACE_ID", "local")
USER = os.environ.get("USER_NAME", "local")
MAX_CHARS = 40000

server = MCPServer("book-library", description="跨书知识库: 检索与阅读本地书库 skill 集合", version="0.1.0")


def db():
    return registry.connect(DB_PATH)


def _slug_ok(slug):
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", slug or ""))


def _skill_dir(slug):
    return SKILLS_HOME / slug


def _j(obj):
    return json.dumps(obj, ensure_ascii=False, indent=1)


def _log(con, tool, slug, question, count):
    try:
        registry.log_interaction(con, SPACE, USER, slug, "mcp", tool, question, count)
    except Exception:
        pass


@server.tool()
def list_books(q: str = "", domain: str = "") -> str:
    """列出书库中的书籍。可按关键词 q(标题/作者/简介) 或 domain 过滤。返回书籍元数据JSON列表。"""
    con = db()
    rows = [dict(r) for r in con.execute(
        "SELECT slug,title,author,intro,description,pages,cover_url,chapter_count,"
        "token_estimate,status,source_sha256 FROM books WHERE status='published' ORDER BY title")]
    out = []
    for r in rows:
        hay = " ".join(str(r.get(k) or "") for k in ("slug", "title", "author", "intro", "description"))
        if q and q.lower() not in hay.lower():
            continue
        if domain and domain not in hay:
            continue
        out.append(r)
    return _j(out)


@server.tool()
def get_book(slug: str) -> str:
    """获取某本书的 SKILL.md 概览(核心框架+章节索引)。slug 用 list_books 获取。"""
    if not _slug_ok(slug):
        return _j({"error": "bad slug"})
    p = _skill_dir(slug) / "SKILL.md"
    if not p.exists():
        return _j({"error": "not found", "slug": slug})
    text = p.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f"\n\n...[truncated, total {len(text)} chars; 用 read_chapter 读具体章节]"
    con = db()
    _log(con, "get_book", slug, "", 1)
    return text


@server.tool()
def read_chapter(slug: str, chapter: str) -> str:
    """读取指定书的某章全文。chapter 可为 'ch04'、'4' 或文件名。"""
    if not _slug_ok(slug):
        return _j({"error": "bad slug"})
    ch_dir = _skill_dir(slug) / "chapters"
    if not ch_dir.is_dir():
        return _j({"error": "no chapters", "slug": slug})
    files = sorted(ch_dir.glob("*.md"))
    target = None
    c = chapter.strip().lower().replace(".md", "").replace("chapters/", "")
    m = re.fullmatch(r"(\d+)", c)
    if m:
        c = f"ch{int(m.group(1)):02d}"
    for f in files:
        if f.stem == c or f.stem.startswith(c + "-") or f.name.lower() == c:
            target = f
            break
    if target is None:
        for f in files:
            if c in f.stem:
                target = f
                break
    if target is None:
        return _j({"error": "chapter not found", "available": [f.name for f in files]})
    text = target.read_text(encoding="utf-8", errors="replace")
    con = db()
    _log(con, "read_chapter", slug, "", 1)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n...[truncated]"
    return text


@server.tool()
def search(query: str, slug: str = "", limit: int = 8) -> str:
    """全文检索书籍内容。query 支持中文; slug 限定单书, 留空跨全库。返回指针(书/文件/行号/片段),不含全文。"""
    if not query.strip():
        return _j({"error": "empty query"})
    con = db()
    roots = []
    if slug:
        if not _slug_ok(slug):
            return _j({"error": "bad slug"})
        roots = [(_skill_dir(slug), slug)]
    else:
        for r in con.execute("SELECT slug, skill_path FROM books WHERE status='published'"):
            d = Path(r["skill_path"]) if r["skill_path"] else _skill_dir(r["slug"])
            if d.is_dir():
                roots.append((d, r["slug"]))
    results = []
    for d, s in roots:
        hits = registry.search_auto(con, s, d, query, limit_files=limit)
        for h in hits:
            h["slug"] = s
        results.extend(hits)
    results.sort(key=lambda x: -x["score"])
    results = results[:limit]
    _log(con, "search", slug or None, query, len(results))
    return _j(results)


@server.tool()
def pack_context(slugs: list, question: str, max_per_book: int = 3) -> str:
    """多书打包: 对每本书按 question 检索, 返回各书最相关章节指针清单(不含全文), 供客户端读取后综合。"""
    con = db()
    out = []
    for s in slugs:
        if not _slug_ok(s):
            continue
        hits = registry.search_auto(con, s, _skill_dir(s), question,
                                    limit_files=max_per_book)
        out.append({"slug": s, "hits": hits})
    _log(con, "pack_context", ",".join(slugs), question,
         sum(len(o["hits"]) for o in out))
    return _j(out)


if __name__ == "__main__":
    server.run("stdio")
