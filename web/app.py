import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "pipeline"))

import registry
import llm
import cosutil
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="book-skill-library", version="0.1.0")

CFG = json.loads((PROJECT / "pipeline" / "config.json").read_text(encoding="utf-8"))
DB = registry.resolve_db(CFG["registry_db"], PROJECT)
SKILLS_HOME = Path(CFG["skills_home"])

DEEP_SEM = asyncio.Semaphore(int(os.environ.get("DEEP_MAX_CONCURRENCY", "4")))
DEEP_TOTAL_TIMEOUT = float(os.environ.get("DEEP_TOTAL_TIMEOUT", "60"))


def con():
    return registry.connect(DB)


def p_in(key):
    p = Path(CFG[key])
    return p if p.is_absolute() else PROJECT / p


def current_user(request):
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        u = registry.user_by_api_token(con(), auth[7:].strip())
        if u:
            return u
    return registry.user_by_token(con(), request.cookies.get("sid"))


def _can_access_book(u, b):
    if not u or not b:
        return False
    if is_admin(u):
        return True
    if b.get("space_id") in visible_spaces(u):
        return True
    return b.get("uploaded_by") == u["id"]


def require_user(request):
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="请先登录")
    return u


def public_user(u):
    return {"identifier": u["identifier"], "id_type": u["id_type"],
            "display_name": u["display_name"], "space_id": u["space_id"],
            "role": u["role"] or "user"}


def is_admin(u):
    try:
        return (u["role"] or "user") == "admin"
    except (KeyError, IndexError, TypeError):
        return False


def visible_spaces(user):
    return ["local"] + ([user["space_id"]] if user else [])


def _hits_for(slug, root, question, limit_files=6, max_lines=3):
    """在独立线程里检索：用 thread-local 新建连接，优先走内容索引。"""
    c = con()
    try:
        if not registry.index_status(c, slug):
            registry.ensure_index(c, slug, root)
        if registry.index_status(c, slug):
            return registry.search_index_hybrid(
                c, [slug], question, CFG, limit_files, max_lines)
    except Exception:
        pass
    return registry.search_files(root, question, limit_files, max_lines)


def _visible_books(c, user):
    spaces = visible_spaces(user)
    ph = ",".join("?" * len(spaces))
    return [dict(r) for r in c.execute(
        "SELECT slug,title,space_id FROM books WHERE space_id IN (" + ph + ")"
        " AND status='published' ORDER BY title", spaces)]


def _chapter_titles(c, slugs):
    out = {}
    for s in slugs:
        for r in c.execute("SELECT no,title,path FROM chapters WHERE book_slug=?", (s,)):
            if r["path"]:
                out[(s, Path(r["path"]).name)] = {"no": r["no"], "title": r["title"]}
    return out


def _ensure_indexes(slugs, skills_home):
    c = con()
    built = []
    for s in slugs:
        try:
            if registry.ensure_index(c, s, Path(skills_home) / s) is not None:
                built.append(s)
        except Exception:
            pass
    return built


def _library_hits(slugs, question, limit_files, max_lines, per_book):
    return registry.search_index_hybrid(con(), slugs, question, CFG,
                                        limit_files, max_lines, per_book)


@app.on_event("startup")
def _startup():
    for k in ("inbox", "covers", "library", "logs"):
        p_in(k).mkdir(parents=True, exist_ok=True)


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/books")
def api_books(request: Request, q: str = ""):
    user = current_user(request) if request else None
    spaces = visible_spaces(user)
    ph = ",".join("?" * len(spaces))
    c = con()
    rows = [dict(r) for r in c.execute(
        f"SELECT slug,title,author,publisher,pages,intro,description,cover_url,chapter_count,"
        f"token_estimate,model_used,depth,book_type,status,updated_at,source_file,language"
        f" FROM books WHERE space_id IN ({ph}) ORDER BY updated_at DESC", spaces)]
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in " ".join(str(r.get(k) or "") for k in
               ("slug", "title", "author", "intro", "description")).lower()]
    for r in rows:
        r["has_source"] = bool(r.get("source_file") and Path(r["source_file"]).exists())
        r["cover"] = f"/api/books/{r['slug']}/cover" if r.get("cover_url") else ""
        r.pop("source_file", None)
    return rows


@app.get("/api/books/{slug}")
def api_book(request: Request, slug: str):
    c = con()
    b = registry.find_book(c, slug)
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    d = dict(b)
    d["chapters"] = [dict(r) for r in c.execute(
        "SELECT no,file_slug,title,token_estimate FROM chapters WHERE book_slug=? ORDER BY no", (slug,))]
    d["has_source"] = bool(d.get("source_file") and Path(d["source_file"]).exists())
    d["cover"] = f"/api/books/{slug}/cover" if d.get("cover_url") else ""
    d["translations"] = [t["lang"] for t in registry.get_translations(c, slug)]
    user = current_user(request)
    owner = d.get("uploaded_by")
    d["can_process"] = bool(
        user and d["has_source"]
        and (is_admin(user) or (owner is not None and owner == user["id"])))
    d["can_manage"] = bool(
        user and (is_admin(user) or (owner is not None and owner == user["id"])))
    d["visibility"] = "public" if d.get("space_id") == "local" else "private"
    for k in ("created_at", "updated_at"):
        d.pop(k, None)
    return d


class VisibilityBody(BaseModel):
    visibility: str


@app.post("/api/books/{slug}/visibility")
def api_set_visibility(request: Request, slug: str, body: VisibilityBody):
    user = require_user(request)
    c = con()
    b = registry.find_book(c, slug)
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _can_access_book(user, b):
        raise HTTPException(status_code=403, detail="无权管理此书")
    owner = b.get("uploaded_by")
    if not (is_admin(user) or (owner is not None and owner == user["id"])):
        raise HTTPException(status_code=403, detail="仅作者或管理员可切换可见性")
    v = (body.visibility or "").strip().lower()
    if v not in ("public", "private"):
        raise HTTPException(status_code=400, detail="visibility 只能是 public 或 private")
    if v == "public":
        space, vis = "local", "public"
    else:
        space = user["space_id"]
        if space == "local":
            raise HTTPException(status_code=400, detail="当前账号无独立空间，无法设为私有")
        vis = "private"
    c.execute("UPDATE books SET space_id=?, visibility=?, updated_at=? WHERE slug=?",
              (space, vis, int(time.time()), slug))
    c.commit()
    return {"slug": slug, "visibility": vis, "space_id": space}


@app.get("/api/books/{slug}/cover")
def api_cover(slug: str):
    c = con()
    b = registry.find_book(c, slug)
    if not b or not b["cover_url"] or not Path(b["cover_url"]).exists():
        return JSONResponse({"error": "no cover"}, status_code=404)
    return FileResponse(b["cover_url"])


def _zh_only(pairs_md):
    if "===zh===" not in pairs_md:
        return pairs_md
    parts = re.split(r"(?m)^===zh===\s*$", pairs_md)
    return "\n\n".join(p.strip() for p in parts[1::2] if p.strip())


@app.get("/api/books/{slug}/chapters/{no}")
def api_chapter(slug: str, no: int, lang: str = ""):
    c = con()
    row = c.execute("SELECT path,title,no FROM chapters WHERE book_slug=? AND no=?",
                    (slug, no)).fetchone()
    if not row or not Path(row["path"]).exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    path = Path(row["path"])
    content = path.read_text(encoding="utf-8", errors="replace")
    if lang in ("zh", "bi"):
        cand = SKILLS_HOME / slug / "chapters_zh" / path.name
        if not cand.exists():
            return JSONResponse({"error": "该章尚无中文译本"}, status_code=404)
        raw = cand.read_text(encoding="utf-8", errors="replace")
        content = raw if lang == "bi" else _zh_only(raw)
    return {"no": row["no"], "title": row["title"], "content": content}


_SRC_MEDIA = {
    "pdf": "application/pdf", "epub": "application/epub+zip",
    "mobi": "application/x-mobipocket-ebook", "azw3": "application/vnd.amazon.ebook",
    "azw": "application/vnd.amazon.ebook",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain", "md": "text/markdown", "html": "text/html",
}


@app.get("/api/books/{slug}/download/source")
def api_download_source(request: Request, slug: str):
    u = require_user(request)
    c = con()
    b = registry.find_book(c, slug)
    if not b or not b.get("source_file") or not Path(b["source_file"]).exists():
        return JSONResponse({"error": "no source"}, status_code=404)
    if not _can_access_book(u, b):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    src = Path(b["source_file"])
    ext = (src.suffix or ".bin").lower()
    media = _SRC_MEDIA.get(ext.lstrip("."), "application/octet-stream")
    return FileResponse(src, media_type=media, filename=f"{slug}-source{ext}")


@app.get("/api/books/{slug}/download/skill")
def api_download_skill(request: Request, slug: str):
    u = require_user(request)
    c = con()
    b = registry.find_book(c, slug)
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _can_access_book(u, b):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    skill_dir = SKILLS_HOME / slug
    if not skill_dir.is_dir():
        return JSONResponse({"error": "skill not found"}, status_code=404)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(skill_dir.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(skill_dir))
    return FileResponse(tmp.name, media_type="application/zip",
                        filename=f"{slug}-skill.zip",
                        background=_unlink(tmp.name))


MAX_SKILL_ZIP = 30 * 1024 * 1024


def _extract_skill_zip(data: bytes, dest: Path) -> Path:
    """解包客户端回传的 skill 包（防路径穿越），返回含 SKILL.md 的根目录。"""
    import io
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        if not infos:
            raise ValueError("skill 包为空")
        for zi in infos:
            raw = str(zi.filename).replace("\\", "/")
            parts = [x for x in raw.split("/") if x and x != "."]
            if (raw.startswith("/") or ".." in parts
                    or (parts and ":" in parts[0])):
                raise ValueError(f"非法路径: {zi.filename}")
        zf.extractall(dest)
    if (dest / "SKILL.md").is_file():
        return dest
    cands = [d for d in dest.iterdir() if d.is_dir() and (d / "SKILL.md").is_file()]
    if len(cands) == 1:
        return cands[0]
    raise ValueError("skill 包中找不到 SKILL.md")


def _extract_zh_zip(data: bytes, dest: Path) -> Path:
    """解包客户端回传的译文包（防路径穿越），返回含 *.md 的译文目录。"""
    import io
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if not infos:
            raise ValueError("译文包为空")
        for zi in infos:
            raw = str(zi.filename).replace("\\", "/")
            parts = [x for x in raw.split("/") if x and x != "."]
            if (raw.startswith("/") or ".." in parts
                    or (parts and ":" in parts[0])):
                raise ValueError(f"非法路径: {zi.filename}")
        zf.extractall(dest)
    cands = [dest] + sorted(d for d in dest.iterdir() if d.is_dir())
    for c in cands:
        if c.name == "chapters_zh" and list(c.glob("*.md")):
            return c
        if (c / "chapters_zh").is_dir() and list((c / "chapters_zh").glob("*.md")):
            return c / "chapters_zh"
    if list(dest.glob("*.md")):
        return dest
    raise ValueError("包中找不到章节译文 (*.md)")


@app.post("/api/books/{slug}/upload-zh")
async def api_upload_zh(request: Request, slug: str,
                        file: UploadFile = File(...),
                        model: str = Form("")):
    user = require_user(request)
    c = con()
    b = registry.find_book(c, slug)
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    owner = b["uploaded_by"]
    if not (is_admin(user) or (owner is not None and owner == user["id"])):
        return JSONResponse({"error": "只有上传者或管理员可以上传译文"},
                            status_code=403)
    ch_dir = SKILLS_HOME / slug / "chapters"
    if not ch_dir.is_dir():
        return JSONResponse({"error": "该书暂无可对照的章节"}, status_code=400)
    data = await file.read()
    if not data:
        return JSONResponse({"error": "译文包为空"}, status_code=400)
    if len(data) > MAX_SKILL_ZIP:
        return JSONResponse({"error": "译文包超过 30MB"}, status_code=413)
    tmp = Path(tempfile.mkdtemp(prefix=".zh-"))
    stage = None
    zh = []
    try:
        try:
            src = _extract_zh_zip(data, tmp)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        zh = sorted(p.name for p in src.glob("*.md"))
        srcs = sorted(p.name for p in ch_dir.glob("*.md"))
        if not zh:
            return JSONResponse({"error": "译文包中没有章节文件"},
                                status_code=400)
        missing = sorted(set(srcs) - set(zh))
        extra = sorted(set(zh) - set(srcs))
        if missing or extra:
            return JSONResponse({"error": "章节与原书不匹配", "missing": missing,
                                 "extra": extra}, status_code=400)
        out = SKILLS_HOME / slug / "chapters_zh"
        stage = SKILLS_HOME / slug / f".chapters_zh.{uuid.uuid4().hex[:8]}"
        shutil.move(str(src), str(stage))
        if out.exists():
            shutil.rmtree(out, ignore_errors=True)
        stage.replace(out)
        stage = None
    finally:
        if stage is not None and Path(stage).exists():
            shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    registry.put_translation(c, slug, "zh", str(out),
                             model=(model or "").strip())
    try:
        await asyncio.to_thread(
            lambda: registry.reindex_book(con(), slug, SKILLS_HOME / slug))
    except Exception:
        pass
    return {"ok": True, "slug": slug, "lang": "zh", "chapters": len(zh),
            "model": (model or "").strip()}


@app.post("/api/books/import")
async def api_import_book(request: Request,
                          file: UploadFile = File(...),
                          skill: UploadFile = File(...),
                          slug: str = Form(""),
                          title: str = Form(""),
                          author: str = Form(""),
                          intro: str = Form(""),
                          publisher: str = Form(""),
                          book_type: str = Form(""),
                          depth: str = Form(""),
                          model_used: str = Form(""),
                          lang_mode: str = Form("")):
    """导入本地转换成品：客户端本地跑 pipeline，回传 skill 包 + 原 PDF，服务端只做 publish。"""
    user = require_user(request)
    pdf_bytes = await file.read()
    skill_bytes = await skill.read()
    if not pdf_bytes:
        return JSONResponse({"error": "PDF 为空"}, status_code=400)
    if len(skill_bytes) > MAX_SKILL_ZIP:
        return JSONResponse({"error": "skill 包超过 30MB"}, status_code=413)

    inbox = p_in("inbox")
    inbox.mkdir(parents=True, exist_ok=True)
    name = Path(file.filename or "book.pdf").name
    dest = inbox / name
    i = 1
    while dest.exists():
        dest = inbox / f"{Path(name).stem}-{i}.pdf"
        i += 1
    dest.write_bytes(pdf_bytes)

    c = con()
    sha = registry.sha256_file(dest)
    lm = lang_mode.strip() if lang_mode.strip() in ("", "zh", "bi") else ""
    dup = _dup_result(c, user, sha, translate_zh=bool(lm))
    if dup:
        dest.unlink(missing_ok=True)
        dup["job_id"] = None
        dup["pdf"] = name
        dup["imported"] = "duplicate"
        return dup

    slug = (slug or "").strip().lower() or _worker_slugify(name, sha)
    existing = registry.find_book(c, slug)
    if existing:
        if not _can_access_book(user, existing):
            dest.unlink(missing_ok=True)
            return JSONResponse({"error": "仅作者或管理员可覆盖该书"}, status_code=403)
    else:
        base, n = slug, 2
        while registry.find_book(c, slug):
            slug = f"{base}-{n}"
            n += 1

    tmp = Path(tempfile.mkdtemp(prefix="import-"))
    target = SKILLS_HOME / slug
    bak = SKILLS_HOME / f".{slug}.bak-{uuid.uuid4().hex[:8]}"
    try:
        try:
            skill_root = _extract_skill_zip(skill_bytes, tmp)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        ch_dir = skill_root / "chapters"
        chs = sorted(ch_dir.glob("*.md")) if ch_dir.is_dir() else []
        if not chs:
            return JSONResponse({"error": "skill 包缺少 chapters/*.md"}, status_code=400)
        if not (skill_root / "SKILL.md").read_text(encoding="utf-8",
                                                    errors="replace").strip():
            return JSONResponse({"error": "SKILL.md 为空"}, status_code=400)

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.rename(bak)
        shutil.move(str(skill_root), str(target))

        import worker
        cfg = worker.load_config()
        cfg = {**cfg,
               "book_type": book_type.strip() or cfg.get("book_type", "text"),
               "depth": depth.strip() or cfg.get("depth", "study"),
               "_model": model_used.strip()}
        job = {"slug": slug, "pdf_path": str(dest),
               "space_id": user["space_id"], "user_id": user["id"]}
        res = {"title": title.strip(), "author": author.strip(),
               "intro": intro.strip(), "publisher": publisher.strip()}
        meta = worker.publish(cfg, c, job, target, res, None)
    except Exception as e:
        shutil.rmtree(target, ignore_errors=True)
        if bak.exists():
            bak.rename(target)
        dest.unlink(missing_ok=True)
        return JSONResponse({"error": f"publish 失败: {e}"}, status_code=500)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(bak, ignore_errors=True)

    return {"imported": "ok", "slug": meta["slug"], "title": meta["title"],
            "chapter_count": meta["chapter_count"], "space_id": meta["space_id"],
            "visibility": meta["visibility"], "cover_url": meta["cover_url"],
            "source_sha256": meta["source_sha256"], "skill_path": meta["skill_path"]}


def _dup_result(c, user, sha, translate_zh=False):
    """内容级去重：同 sha 已有已发布书籍则复用（公共库或自己 space）；否则复制进自己 space（默认私有）。"""
    books = registry.find_books_by_sha(c, sha)
    if translate_zh:
        books = [b for b in books if b.get("language") in ("zh", "bi")]
    if not books:
        return None
    visible = {"local", user["space_id"]}
    hit = next((b for b in books if b["space_id"] in visible), None)
    if hit:
        return {"duplicate": True, "slug": hit["slug"], "title": hit["title"]}
    src = books[0]
    base = src["slug"]
    slug = f"{base}-2"
    n = 2
    while registry.find_book(c, slug):
        n += 1
        slug = f"{base}-{n}"
    registry.copy_book(c, src, slug, user["space_id"], uploaded_by=user["id"])
    return {"duplicate": True, "slug": slug, "title": src["title"],
            "shared_from": src["slug"]}


@app.post("/api/upload")
async def api_upload(request: Request, file: UploadFile = File(...), slug: str = Form(""),
                     book_type: str = Form(""), depth: str = Form(""),
                     translate_zh: str = Form(""), lang_mode: str = Form(""), model: str = Form("")):
    user = require_user(request)
    lm = lang_mode.strip() if lang_mode.strip() in ("", "zh", "bi") else ""
    if not lm and translate_zh in ("1", "true", "on"):
        lm = "zh"
    inbox = p_in("inbox")
    inbox.mkdir(parents=True, exist_ok=True)
    name = Path(file.filename or "book.pdf").name
    dest = inbox / name
    i = 1
    while dest.exists():
        dest = inbox / f"{Path(name).stem}-{i}.pdf"
        i += 1
    content = await file.read()
    dest.write_bytes(content)
    c = con()
    sha = registry.sha256_file(dest)
    dup = _dup_result(c, user, sha, translate_zh=bool(lm))
    if dup:
        dest.unlink(missing_ok=True)
        dup["job_id"] = None
        dup["pdf"] = name
        return dup
    slug = (slug or "").strip().lower()
    if not slug:
        slug = _worker_slugify(name, sha)
    base = slug
    n = 2
    while registry.find_book(c, slug):
        slug = f"{base}-{n}"
        n += 1
    job_id = uuid.uuid4().hex[:12]
    registry.register_job(c, job_id, user["space_id"], "convert", dest, slug)
    c.execute("UPDATE jobs SET model=?, user_id=?, detail=? WHERE id=?",
              (model.strip() or None, user["id"],
               json.dumps({"book_type": book_type, "depth": depth,
                           "lang_mode": lm, "translate_zh": bool(lm)}),
               job_id))
    c.commit()
    return {"job_id": job_id, "slug": slug, "pdf": name}


class PresignBody(BaseModel):
    filename: str
    size: int = 0


class ProcessBody(BaseModel):
    model: str = ""
    lang_mode: str = ""


class TranslateBody(BaseModel):
    lang_mode: str = ""
    model: str = ""


@app.post("/api/books/{slug}/translate")
def api_translate(request: Request, slug: str, body: TranslateBody):
    user = require_user(request)
    c = con()
    b = registry.find_book(c, slug)
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    owner = b["uploaded_by"]
    if not (is_admin(user) or (owner is not None and owner == user["id"])):
        return JSONResponse({"error": "只有上传者或管理员可以发起翻译"},
                            status_code=403)
    lang = (body.lang_mode or "").strip()
    if lang not in ("zh", "bi"):
        return JSONResponse({"error": "lang_mode 需为 zh 或 bi"}, status_code=400)
    if not (SKILLS_HOME / slug / "chapters").is_dir():
        return JSONResponse({"error": "该书暂无可翻译的章节"}, status_code=400)
    job_id = uuid.uuid4().hex[:12]
    registry.register_job(c, job_id, user["space_id"], "translate",
                          str(SKILLS_HOME / slug), slug)
    c.execute("UPDATE jobs SET model=?, user_id=?, detail=? WHERE id=?",
              (body.model.strip() or None, user["id"],
               json.dumps({"lang_mode": lang}), job_id))
    c.commit()
    return {"job_id": job_id, "slug": slug, "lang_mode": lang}


class CompleteBody(BaseModel):
    key: str
    filename: str = ""
    slug: str = ""
    book_type: str = ""
    depth: str = ""
    sha256: str = ""
    translate_zh: bool = False
    lang_mode: str = ""
    model: str = ""


@app.post("/api/upload/presign")
def api_upload_presign(request: Request, body: PresignBody):
    require_user(request)
    if not cosutil.is_configured(CFG):
        return {"mode": "proxy"}
    if body.size and body.size > cosutil.MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "文件超过 500MB 上限"}, status_code=413)
    key = cosutil.object_key(CFG, body.filename)
    return {"mode": "cos", "key": key,
            "put_url": cosutil.presign_put(CFG, key), "expires": 3600}


@app.post("/api/upload/complete")
def api_upload_complete(request: Request, body: CompleteBody):
    user = require_user(request)
    if not cosutil.is_configured(CFG):
        return JSONResponse({"error": "COS 未配置"}, status_code=400)
    prefix = (CFG.get("cos_prefix") or "uploads/").strip("/") + "/"
    if not body.key.startswith(prefix):
        return JSONResponse({"error": "非法对象 key"}, status_code=400)
    size = cosutil.head_size(CFG, body.key)
    if size <= 0:
        return JSONResponse({"error": "COS 对象不存在，请重新上传"}, status_code=400)
    if size > cosutil.MAX_UPLOAD_BYTES:
        cosutil.delete_object(CFG, body.key)
        return JSONResponse({"error": "文件超过 500MB 上限"}, status_code=413)
    c = con()
    lm = body.lang_mode.strip() if body.lang_mode.strip() in ("", "zh", "bi") else ""
    if not lm and body.translate_zh:
        lm = "zh"
    if body.sha256:
        dup = _dup_result(c, user, body.sha256, translate_zh=bool(lm))
        if dup:
            cosutil.delete_object(CFG, body.key)
            dup["job_id"] = None
            dup["mode"] = "cos"
            return dup
    name = cosutil.safe_name(body.filename or Path(body.key).name)
    slug = (body.slug or "").strip().lower()
    if not slug:
        slug = _worker_slugify(name, uuid.uuid4().hex)
    base = slug
    n = 2
    while registry.find_book(c, slug):
        slug = f"{base}-{n}"
        n += 1
    job_id = uuid.uuid4().hex[:12]
    pdf_ref = f"cos://{CFG['cos_bucket']}/{body.key}"
    registry.register_job(c, job_id, user["space_id"], "convert", pdf_ref, slug)
    c.execute("UPDATE jobs SET model=?, user_id=?, detail=? WHERE id=?",
              (body.model.strip() or None, user["id"],
               json.dumps({"book_type": body.book_type, "depth": body.depth,
                           "lang_mode": lm, "translate_zh": bool(lm)}), job_id))
    c.commit()
    return {"job_id": job_id, "slug": slug, "mode": "cos"}


@app.post("/api/books/{slug}/process")
def api_process(request: Request, slug: str, force: bool = False, body: ProcessBody | None = None):
    user = require_user(request)
    c = con()
    b = registry.find_book(c, slug)
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    owner = b["uploaded_by"]
    if not (is_admin(user) or (owner is not None and owner == user["id"])):
        return JSONResponse({"error": "只有上传者或管理员可以重新处理"},
                            status_code=403)
    if not b["source_file"] or not Path(b["source_file"]).exists():
        return JSONResponse({"error": "no source file for this book"}, status_code=400)
    job_id = uuid.uuid4().hex[:12]
    registry.register_job(c, job_id, user["space_id"], "update", b["source_file"], slug)
    chosen = (body.model.strip() if body and body.model else "") or None
    lm = (body.lang_mode.strip() if body and body.lang_mode else "")
    if lm not in ("", "zh", "bi"):
        lm = ""
    c.execute("UPDATE jobs SET model=?, user_id=?, detail=? WHERE id=?",
              (chosen, user["id"],
               json.dumps({"lang_mode": lm, "translate_zh": bool(lm)}), job_id))
    c.commit()
    return {"job_id": job_id, "slug": slug, "model": chosen, "lang_mode": lm}


@app.get("/api/jobs")
def api_jobs(request: Request, limit: int = 50):
    user = current_user(request)
    spaces = visible_spaces(user)
    ph = ",".join("?" * len(spaces))
    c = con()
    now = int(time.time())
    out = []
    for r in c.execute(f"SELECT * FROM jobs WHERE space_id IN ({ph})"
                       f" ORDER BY created_at DESC LIMIT ?", (*spaces, limit)):
        d = dict(r)
        if d["state"] == "running" and d.get("started_at"):
            d["elapsed_sec"] = now - d["started_at"]
        d.pop("usage_raw", None)
        d.pop("pdf_path", None)
        out.append(d)
    return out


@app.get("/api/jobs/{job_id}/log")
def api_job_log(job_id: str, tail: int = 6000):
    c = con()
    r = c.execute("SELECT log_path FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r or not r["log_path"] or not Path(r["log_path"]).exists():
        return JSONResponse({"error": "no log"}, status_code=404)
    data = Path(r["log_path"]).read_text(encoding="utf-8", errors="replace")
    return PlainTextResponse(data[-tail:])


class AskBody(BaseModel):
    slug: str
    question: str
    deep: bool = False


@app.post("/api/ask")
async def api_ask(request: Request, body: AskBody):
    user = require_user(request)
    c = con()
    b = registry.find_book(c, body.slug)
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    chapters = {r["path"]: dict(r) for r in c.execute(
        "SELECT no,title,path FROM chapters WHERE book_slug=?", (body.slug,))}
    skill_root = (SKILLS_HOME / body.slug).as_posix()

    def enrich(hs):
        for h in hs:
            ch = chapters.get(h["path"]) or chapters.get(f"{skill_root}/{h['path']}")
            h["chapter_no"] = ch["no"] if ch else None
            h["chapter_title"] = ch["title"] if ch else h["path"]
        return hs

    hits = enrich(await asyncio.to_thread(
        _hits_for, body.slug, SKILLS_HOME / body.slug, body.question, 6, 3))
    nq = registry.norm_question(body.question)
    mode = "deep" if body.deep else "fast"
    cached = registry.get_cached_answer(c, body.slug, nq, mode) if nq else None
    if cached:
        registry.log_interaction(c, user["space_id"], user["display_name"], body.slug,
                                 "web", "ask", body.question, len(hits), answer=cached)
        return {"hits": hits, "answer": cached, "answer_error": None,
                "mode": mode, "cached": True}

    if body.deep:
        try:
            await asyncio.wait_for(DEEP_SEM.acquire(), timeout=8)
        except (asyncio.TimeoutError, TimeoutError):
            return JSONResponse(
                {"error": "深度提问排队较多，请稍后再试"}, status_code=429)
        root = (SKILLS_HOME / body.slug).resolve()
        t0 = time.time()
        try:
            answer, info = await asyncio.wait_for(llm.deep_answer_async(
                CFG, body.question, b["title"] or "", b["intro"] or "",
                search_fn=lambda q: json.dumps(
                    enrich(_hits_for(body.slug, root, q, 5, 3)),
                    ensure_ascii=False),
                read_fn=lambda rel: _safe_read(root, rel),
                workdir=root),
                timeout=DEEP_TOTAL_TIMEOUT)
        except (asyncio.TimeoutError, TimeoutError):
            answer, info = None, (
                f"深度回答超时（{int(DEEP_TOTAL_TIMEOUT)}秒），"
                "请缩小问题范围或改用快速模式")
        finally:
            DEEP_SEM.release()
    else:
        skill_md = SKILLS_HOME / body.slug / "SKILL.md"
        fallback = skill_md.read_text(encoding="utf-8", errors="replace")[:3000] \
            if skill_md.exists() else ""
        if b["intro"]:
            fallback = f"{b['intro']}\n\n{fallback}" if fallback else b["intro"]
        t0 = time.time()
        answer, info = await llm.answer_question_async(
            CFG, body.question, hits, b["title"] or "", fallback)
    latency_ms = int((time.time() - t0) * 1000)
    model = info if answer else None
    if answer and nq:
        registry.put_cached_answer(c, body.slug, nq, mode, body.question, answer, model=model or "")
    registry.log_interaction(c, user["space_id"], user["display_name"], body.slug,
                             "web", "ask", body.question, len(hits), answer=answer,
                             model=model, latency_ms=latency_ms)
    return {"hits": hits, "answer": answer,
            "answer_error": None if answer else info,
            "mode": mode, "cached": False}


@app.post("/api/ask/stream")
async def api_ask_stream(request: Request, body: AskBody):
    """SSE 流式问答（仅 fast 模式）。事件：token / done / error。"""
    user = require_user(request)
    c = con()
    b = registry.find_book(c, body.slug)
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    chapters = {r["path"]: dict(r) for r in c.execute(
        "SELECT no,title,path FROM chapters WHERE book_slug=?", (body.slug,))}
    skill_root = (SKILLS_HOME / body.slug).as_posix()

    def enrich(hs):
        for h in hs:
            ch = chapters.get(h["path"]) or chapters.get(f"{skill_root}/{h['path']}")
            h["chapter_no"] = ch["no"] if ch else None
            h["chapter_title"] = ch["title"] if ch else h["path"]
        return hs

    hits = enrich(await asyncio.to_thread(
        _hits_for, body.slug, SKILLS_HOME / body.slug, body.question, 6, 3))
    nq = registry.norm_question(body.question)
    mode = "fast"
    cached = registry.get_cached_answer(c, body.slug, nq, mode) if nq else None

    def sse(obj):
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    if cached:
        async def gen_cached():
            yield sse({"type": "token", "text": cached})
            yield sse({"type": "done", "hits": hits, "model": None,
                       "cached": True, "mode": mode})
        registry.log_interaction(c, user["space_id"], user["display_name"], body.slug,
                                 "web", "ask", body.question, len(hits), answer=cached)
        return StreamingResponse(gen_cached(), media_type="text/event-stream")

    skill_md = SKILLS_HOME / body.slug / "SKILL.md"
    fallback = skill_md.read_text(encoding="utf-8", errors="replace")[:3000] \
        if skill_md.exists() else ""
    if b["intro"]:
        fallback = f"{b['intro']}\n\n{fallback}" if fallback else b["intro"]
    system, user_prompt = llm._answer_prompt(body.question, hits, b["title"] or "", fallback)
    if user_prompt is None:
        return JSONResponse({"error": "no excerpts"}, status_code=400)

    t0 = time.time()
    chunks = []
    model_used = None

    async def gen_stream():
        nonlocal model_used
        stream, model, err = await llm.chat_async_stream(CFG, system, user_prompt, 90)
        if err or stream is None:
            yield sse({"type": "error", "error": err or "stream unavailable"})
            return
        model_used = model
        try:
            async for tok in stream:
                chunks.append(tok)
                yield sse({"type": "token", "text": tok})
        except Exception as e:
            yield sse({"type": "error", "error": f"stream interrupted: {e}"})
            return
        answer = "".join(chunks)
        latency_ms = int((time.time() - t0) * 1000)
        if answer and nq:
            registry.put_cached_answer(c, body.slug, nq, mode, body.question, answer,
                                       model=model_used or "")
        registry.log_interaction(c, user["space_id"], user["display_name"], body.slug,
                                 "web", "ask", body.question, len(hits), answer=answer,
                                 model=model_used, latency_ms=latency_ms)
        yield sse({"type": "done", "hits": hits, "model": model_used,
                   "cached": False, "mode": mode})

    return StreamingResponse(gen_stream(), media_type="text/event-stream")


class LibraryAskBody(BaseModel):
    question: str
    deep: bool = False


@app.post("/api/library/ask")
async def api_library_ask(request: Request, body: LibraryAskBody):
    """跨书提问：在可见书库里检索多本相关书，综合作答并给出《书名》·章节 引用。"""
    user = require_user(request)
    q = (body.question or "").strip()
    if not q:
        return JSONResponse({"error": "question required"}, status_code=400)
    c = con()
    books = _visible_books(c, user)
    if not books:
        return {"hits": [], "answer": None, "answer_error": "书库暂无可见书籍",
                "books": [], "deep": bool(body.deep)}
    by_slug = {b["slug"]: b for b in books}
    slugs = list(by_slug)
    per_book = 3 if body.deep else 2
    limit_files = min(14, max(6, len(slugs) * per_book))

    try:
        await asyncio.wait_for(DEEP_SEM.acquire(), timeout=8)
    except (asyncio.TimeoutError, TimeoutError):
        return JSONResponse({"error": "跨书提问排队较多，请稍后再试"}, status_code=429)
    try:
        await asyncio.to_thread(_ensure_indexes, slugs, SKILLS_HOME)
        try:
            hits = await asyncio.wait_for(
                asyncio.to_thread(_library_hits, slugs, q, limit_files, 3, per_book),
                timeout=DEEP_TOTAL_TIMEOUT)
        except (asyncio.TimeoutError, TimeoutError):
            hits = []
        titles = _chapter_titles(c, slugs)
        for h in hits:
            b = by_slug.get(h.get("book_slug")) or {}
            h["book_title"] = b.get("title") or h.get("book_slug") or ""
            meta = titles.get((h.get("book_slug"), Path(h.get("path") or "").name))
            if meta:
                h["chapter_no"] = meta["no"]
                h["chapter_title"] = meta["title"]
            else:
                h["chapter_no"] = None
                h["chapter_title"] = Path(h.get("path") or "").stem
        catalog = "\n".join(f"- 《{b['title'] or b['slug']}》" for b in books)
        t0 = time.time()
        answer, info = await llm.answer_library_async(
            CFG, q, hits, catalog, 150 if body.deep else 90)
        latency_ms = int((time.time() - t0) * 1000)
        model = info if answer else None
    finally:
        DEEP_SEM.release()

    if answer:
        registry.log_interaction(c, user["space_id"], user["display_name"], "",
                                 "web", "ask_library", q, len(hits), answer=answer,
                                 model=model, latency_ms=latency_ms)
    return {"hits": hits, "answer": answer,
            "answer_error": None if answer else info,
            "books": [b["slug"] for b in books], "deep": bool(body.deep)}


@app.get("/api/faq")
def api_faq(slug: str = ""):
    return registry.faq_groups(con(), slug or None)


@app.get("/api/hot-questions")
def api_hot_questions(slug: str = "", limit: int = 8):
    return registry.hot_questions(con(), slug or None, min(limit, 50))


def _safe_read(root, rel):
    try:
        p = (root / rel).resolve()
    except (OSError, ValueError):
        return "文件不存在"
    if not p.is_relative_to(root) or not p.is_file() or p.suffix != ".md":
        return "文件不存在或不允许读取"
    return p.read_text(encoding="utf-8", errors="replace")[:40000]


class FaqBody(BaseModel):
    scope_slug: str = ""
    question: str
    answer: str = ""


@app.post("/api/faq")
def api_save_faq(request: Request, body: FaqBody):
    require_user(request)
    try:
        registry.save_faq(con(), body.scope_slug, body.question, body.answer)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


class FaqGenBody(BaseModel):
    scope_slug: str = ""
    question: str


@app.post("/api/faq/answer")
async def api_faq_answer(request: Request, body: FaqGenBody):
    user = require_user(request)
    c = con()
    spaces = visible_spaces(user)
    ph = ",".join("?" * len(spaces))
    if body.scope_slug:
        rows = c.execute(f"SELECT slug,title FROM books WHERE slug=? AND space_id IN ({ph})",
                         (body.scope_slug, *spaces)).fetchall()
    else:
        rows = c.execute(f"SELECT slug,title FROM books WHERE space_id IN ({ph})", spaces).fetchall()
    all_hits = []
    title = ""
    for r in rows:
        hits = registry.search_files(SKILLS_HOME / r["slug"], body.question,
                                     limit_files=3, max_lines=2)
        for h in hits:
            h["slug"] = r["slug"]
        if hits and not title:
            title = r["title"] or ""
        all_hits.extend(hits)
    all_hits.sort(key=lambda x: -x["score"])
    fallback = ""
    if body.scope_slug:
        skill_md = SKILLS_HOME / body.scope_slug / "SKILL.md"
        if skill_md.exists():
            fallback = skill_md.read_text(encoding="utf-8", errors="replace")[:3000]
    answer, info = await llm.answer_question_async(
        CFG, body.question, all_hits[:6], title, fallback)
    if not answer:
        return JSONResponse({"error": info}, status_code=400)
    registry.save_faq(c, body.scope_slug, body.question, answer)
    if body.scope_slug:
        registry.put_cached_answer(c, body.scope_slug,
                                   registry.norm_question(body.question),
                                   "fast", body.question, answer)
    return {"answer": answer}


@app.get("/api/admin/stats")
def api_admin_stats(request: Request, days: int = 7):
    user = require_user(request)
    if not is_admin(user):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return registry.admin_stats(con(), max(1, min(days, 90)))


class AuthBody(BaseModel):
    identifier: str
    password: str
    display_name: str = ""


def _session_response(user, token):
    resp = JSONResponse(public_user(user))
    resp.set_cookie("sid", token, max_age=30 * 86400, httponly=True,
                    samesite="lax", path="/")
    return resp


@app.post("/api/register")
def api_register(body: AuthBody):
    c = con()
    try:
        user = registry.register_user(c, body.identifier, body.password, body.display_name)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return _session_response(user, registry.create_session(c, user["id"]))


@app.post("/api/login")
def api_login(body: AuthBody):
    c = con()
    user = registry.auth_user(c, body.identifier, body.password)
    if not user:
        return JSONResponse({"error": "账号或密码错误"}, status_code=401)
    return _session_response(user, registry.create_session(c, user["id"]))


@app.post("/api/logout")
def api_logout(request: Request):
    registry.destroy_session(con(), request.cookies.get("sid"))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("sid", path="/")
    return resp


@app.get("/api/me")
def api_me(request: Request):
    u = current_user(request)
    return public_user(u) if u else None


class TokenBody(BaseModel):
    name: str = ""


@app.post("/api/tokens")
def api_tokens_create(request: Request, body: TokenBody = Body(default=TokenBody())):
    u = require_user(request)
    tok = registry.create_api_token(con(), u["id"], body.name or None)
    return {"token": tok, "name": body.name}


@app.get("/api/tokens")
def api_tokens_list(request: Request):
    u = require_user(request)
    return registry.list_api_tokens(con(), u["id"])


@app.delete("/api/tokens/{token}")
def api_tokens_delete(request: Request, token: str):
    u = require_user(request)
    ok = registry.delete_api_token(con(), u["id"], token)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True}


def _unlink(path):
    from starlette.background import BackgroundTask
    return BackgroundTask(lambda: Path(path).unlink(missing_ok=True))


def _worker_slugify(name, sha):
    import re as _re
    s = _re.sub(r"[^a-z0-9]+", "-", Path(name).stem.lower()).strip("-")
    s = _re.sub(r"-{2,}", "-", s)
    return s if len(s) >= 3 else f"book-{sha[:6]}"


app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


def main():
    import uvicorn
    root = str(PROJECT)
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ["PYTHONPATH"] = root + os.pathsep + os.environ.get("PYTHONPATH", "")
    host = "127.0.0.1"
    port = int(os.environ.get("PORT", "8787"))
    workers = int(os.environ.get("WEB_WORKERS", "4"))
    if workers > 1:
        uvicorn.run("web.app:app", host=host, port=port, workers=workers)
    else:
        uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
