import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from string import Template

import gate
import llm
import registry
import cosutil

PROJECT = Path(__file__).resolve().parent.parent
AUTO_CONF = PROJECT / "pipeline" / "conf" / "auto_opencode.json"
FIX_PROMPT = (
    "上一次转换未通过质检。问题: {problems}\n"
    "请直接修复 {skill_dir} 中的产物(缺文件则补写), 必须重新运行 scan 直到 EXIT=0, "
    "然后按规范重新输出一行 RESULT_JSON 单行JSON。"
)


def load_config():
    cfg = json.loads((PROJECT / "pipeline" / "config.json").read_text(encoding="utf-8"))
    cfg["_model"] = cfg.get("model") or ""
    key = cfg.get("api_key") or ""
    if not key and cfg.get("api_key_env"):
        import os
        key = os.environ.get(cfg["api_key_env"], "")
    cfg["_key"] = key
    return cfg


def path_in(cfg, key):
    p = Path(cfg[key])
    return p if p.is_absolute() else PROJECT / p


def gen_auto_config(cfg):
    auto = {"$schema": "https://opencode.ai/config.json", "share": "disabled",
            "permission": {"external_directory": "allow"}}
    if cfg["_model"]:
        auto["model"] = cfg["_model"]
        provider = cfg["_model"].split("/", 1)[0] if "/" in cfg["_model"] else ""
        if provider and cfg["_key"] and provider not in ("opencode", "xiaopuyun"):
            auto["provider"] = {provider: {"options": {"apiKey": cfg["_key"]}}}
    if cfg.get("xiaopuyun_api_key") and cfg.get("xiaopuyun_base_url"):
        auto.setdefault("provider", {})
        auto["provider"]["xiaopuyun"] = {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Xiaopuyun",
            "options": {"baseURL": cfg["xiaopuyun_base_url"],
                        "apiKey": cfg["xiaopuyun_api_key"]},
            "models": {m: {"name": m} for m in cfg.get("xiaopuyun_models") or []},
        }
    AUTO_CONF.parent.mkdir(parents=True, exist_ok=True)
    AUTO_CONF.write_text(json.dumps(auto, ensure_ascii=False, indent=2), encoding="utf-8")
    return AUTO_CONF


def slugify(name, sha):
    s = re.sub(r"[^a-z0-9]+", "-", Path(name).stem.lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s if len(s) >= 3 else f"book-{sha[:6]}"


def push_peer(cfg, items):
    host = cfg.get("peer_host") or ""
    key = cfg.get("peer_key") or ""
    if not host or not items:
        return True
    ssh_opts = ["-i", key, "-o", "BatchMode=yes", "-o",
                "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]
    ssh_e = "ssh " + " ".join(ssh_opts)
    ok = True
    for p, is_dir in items:
        p = Path(p)
        if not p.exists():
            continue
        src = str(p).rstrip("/") + ("/" if is_dir else "")
        dest = f"root@{host}:{str(p).rstrip('/')}" + ("/" if is_dir else "")
        if not is_dir:
            subprocess.run(["ssh", *ssh_opts, f"root@{host}", "mkdir", "-p",
                            str(p.parent)], capture_output=True, timeout=60)
        argv = ["rsync", "-a"] + (["--delete"] if is_dir else []) + \
               ["-e", ssh_e, src, dest]
        last = ""
        for _ in range(3):
            try:
                r = subprocess.run(argv, capture_output=True, text=True, timeout=180)
            except subprocess.TimeoutExpired:
                last = "rsync timeout"
                r = None
            if r is not None and r.returncode == 0:
                last = ""
                break
            last = (last or (r.stderr or r.stdout if r else ""))[-300:]
            time.sleep(2)
        if last:
            ok = False
            print(f"push failed {src} -> {dest}: {last}")
    return ok


def wait_file_release(con, job, now):
    raw = job["detail"] or ""
    d = {}
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            d = {}
    since = d.get("wait_file_since") or now
    if now - since > 300:
        return False
    d["wait_file_since"] = since
    d["waiting"] = "pdf sync from peer"
    registry.update_job(con, job["id"], state="queued",
                        worker_id=None, lease_until=None,
                        detail=json.dumps(d, ensure_ascii=False))
    return True


def render_prompt(cfg, vars_):
    tpl = Template((PROJECT / "pipeline" / "prompt_template.txt").read_text(encoding="utf-8"))
    mode = cfg.get("lang_mode") or ("zh" if cfg.get("translate_zh") else "")
    if mode == "bi":
        rule = ("中英对照模式：每章正文按『段落对』成对输出——先输出一个原文块(标题/段落/列表)，"
                "紧接着单独一行 '===zh===' ，再输出该块对应的简体中文译文；如此交替覆盖全章，"
                "严格保持原文顺序与结构(标题也要成对)。除 '===zh===' 分隔行外不要使用其他分隔标记。"
                "中文术语首次出现时保留英文原文并括号注明")
    elif mode == "zh":
        rule = ("章节、概览与定义一律使用简体中文输出;"
                "专业术语首次出现时保留英文原文并括号注明")
    else:
        rule = "章节、概览与定义使用与原书正文一致的语言,禁止擅自翻译成其他语言"
    base = {"book_type": cfg["book_type"], "depth": cfg["depth"],
            "language_rule": rule}
    base.update(vars_)
    return tpl.safe_substitute(base)


def detect_language(sample):
    if "===zh===" in sample:
        return "bi"
    cjk = len(re.findall(r"[\u4e00-\u9fff]", sample))
    latin = len(re.findall(r"[A-Za-z]", sample))
    return "zh" if cjk >= max(20, latin * 0.05) else "en"


def walk_tokens(o, seen):
    if isinstance(o, dict):
        tok = o.get("tokens")
        if isinstance(tok, dict) and ("input" in tok or "output" in tok):
            key = str(o.get("id") or o.get("messageID") or o.get("messageId") or "")
            if not key:
                key = hashlib.md5(json.dumps(tok, sort_keys=True).encode()).hexdigest()
            seen[key] = tok
        for v in o.values():
            walk_tokens(v, seen)
    elif isinstance(o, list):
        for v in o:
            walk_tokens(v, seen)


def summarize_usage(seen):
    ti = to = 0
    for tok in seen.values():
        try:
            ti += int(tok.get("input") or 0)
            to += int(tok.get("output") or 0)
        except (TypeError, ValueError):
            pass
    return ti, to, json.dumps(seen, ensure_ascii=False)[:4000]


def event_detail(obj):
    if not isinstance(obj, dict):
        return None
    t = str(obj.get("type", ""))
    if "tool" in t.lower():
        name = obj.get("name") or obj.get("tool") or ""
        args = obj.get("args") or obj.get("input") or {}
        f = ""
        if isinstance(args, dict):
            f = str(args.get("file") or args.get("path") or args.get("command") or "")[:80]
        return f"tool:{name} {f}".strip()
    return None


MODEL_UNAVAILABLE_MARKERS = (
    "Insufficient Balance", "AI_APICallError", "statusCode\":401",
    "statusCode\":402", "statusCode\":403", "statusCode\":429",
    "Invalid API key", "AuthenticationError", "Rate limit",
    "rate_limit", "model not found", "Model not found",
    "NotFound: model", "context_length_exceeded")


def _build_chain(cfg, explicit_model=""):
    """构造转换模型降级链。

    显式指定的模型排在首位（仍是首选），其余按 model_chain 顺序跟上，
    这样即使显式模型不可用（402/403/429…）也仍能继续降级，而不是把链压成单元素。
    """
    base = cfg.get("model_chain") or (
        [cfg.get("_model")] + ([cfg["model_fallback"]]
                               if cfg.get("model_fallback")
                               and cfg.get("model_fallback") != cfg.get("_model") else []))
    base = [m for m in base if m]
    explicit = (explicit_model or "").strip()
    if explicit:
        chain = [explicit] + [m for m in base if m != explicit]
    else:
        chain = base
    return [m for m in chain if m]


def _model_unavailable_reason(run):
    tail = (run or {}).get("stdout", "")[-12000:]
    for m in MODEL_UNAVAILABLE_MARKERS:
        if m in tail:
            return m
    return None


def _is_model_unavailable(run):
    return _model_unavailable_reason(run) is not None


def opencode_bin(cfg):
    """opencode 可执行文件。本地 1.2.15 的 serve API 与 llm.py 不兼容时需指定 1.19.0。"""
    return (cfg.get("opencode_bin") or "").strip() or "opencode"


def run_opencode(cfg, prompt, job_id, log_path, workdir, session_id=None, cancel=None):
    cmd = [opencode_bin(cfg), "run", "--format", "json", "--print-logs", "--log-level", "INFO",
           "--agent", "publisher", "--title", job_id]
    if cfg["_model"]:
        cmd += ["-m", cfg["_model"]]
    if session_id:
        cmd += ["--session", session_id]
    cmd.append(prompt)
    auto = gen_auto_config(cfg)
    env = {**__import__("os").environ, "OPENCODE_CONFIG": str(auto), "PYTHONUTF8": "1"}
    deadline = time.time() + cfg.get("timeout_min", 90) * 60
    stdout_all = []
    session = session_id
    seen_tokens = {}
    last_detail = 0
    detail = ""
    with open(log_path, "w", encoding="utf-8") as logf:
        proc = subprocess.Popen(cmd, cwd=str(PROJECT), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", bufsize=1)
        if cancel is not None:
            # 看门狗：cancel 一旦置位立刻杀进程，不依赖 stdout 是否还有输出
            def _watch():
                while proc.poll() is None:
                    if cancel.wait(1.0):
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        return
            threading.Thread(target=_watch, daemon=True, name=f"k-{job_id}").start()
        for line in proc.stdout:
            if cancel is not None and cancel.is_set():
                proc.kill()
                detail = "cancelled"
                break
            if time.time() > deadline:
                proc.kill()
                detail = "timeout killed"
                break
            stdout_all.append(line)
            logf.write(line)
            if not session:
                m = re.search(r'"session[Ii][Dd]"\s*:\s*"([^"]+)"', line)
                if m:
                    session = m.group(1)
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            walk_tokens(obj, seen_tokens)
            d = event_detail(obj)
            if d and time.time() - last_detail > 2:
                detail = d
                last_detail = time.time()
        proc.wait(timeout=60)
    text = "".join(stdout_all)
    ti, to, raw = summarize_usage(seen_tokens)
    return {"stdout": text, "session": session, "exit": proc.returncode,
            "tokens_in": ti, "tokens_out": to, "usage_raw": raw, "detail": detail,
            "cancelled": bool(cancel is not None and cancel.is_set())}


def render_cover(pdf_path, out_png):
    try:
        import pymupdf
        doc = pymupdf.open(str(pdf_path))
        pix = doc[0].get_pixmap(dpi=100)
        pix.save(str(out_png))
        pages = doc.page_count
        doc.close()
        return str(out_png), pages
    except Exception:
        return "", None


def publish(cfg, con, job, skill_dir, res, run):
    slug = job["slug"]
    covers = path_in(cfg, "covers")
    library = path_in(cfg, "library")
    covers.mkdir(parents=True, exist_ok=True)
    cover_path = covers / f"{slug}.jpg"
    cover_url, pages = render_cover(job["pdf_path"], cover_path)
    arch_dir = library / slug
    arch_dir.mkdir(parents=True, exist_ok=True)
    arch = arch_dir / "source.pdf"
    if not arch.exists():
        try:
            shutil.copy2(job["pdf_path"], arch)
        except OSError:
            arch = Path(job["pdf_path"])
    ch_rows = registry.chapter_rows_from_disk(slug, skill_dir)
    skill_md = (Path(skill_dir) / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    fm_name, fm_desc = registry.parse_frontmatter(md := skill_md)
    title = (res or {}).get("title") or registry.first_heading(skill_md) or fm_name or slug
    sample = ""
    for cf in sorted((Path(skill_dir) / "chapters").glob("*.md"))[:3]:
        sample += cf.read_text(encoding="utf-8", errors="replace")[:1500]
        if len(sample) > 4000:
            break
    existing = registry.find_book(con, slug)
    space = (existing["space_id"] if existing
             else job.get("space_id") or "local")
    meta = {
        "space_id": space,
        "visibility": "public" if space == "local" else "private",
        "slug": slug, "title": title,
        "author": (res or {}).get("author") or "",
        "publisher": (res or {}).get("publisher") or "",
        "intro": (res or {}).get("intro") or "",
        "description": fm_desc, "pages": pages or (res or {}).get("pages") or None,
        "cover_url": cover_url, "source_file": str(arch),
        "source_sha256": registry.sha256_file(job["pdf_path"]),
        "skill_path": str(skill_dir).replace("\\", "/"),
        "book_type": cfg["book_type"], "depth": cfg["depth"],
        "chapter_count": len(ch_rows),
        "token_estimate": sum(r[5] for r in ch_rows) + registry.estimate_tokens(skill_md),
        "scan_status": "pass", "model_used": cfg["_model"] or "opencode-auth",
        "status": "published",
        "uploaded_by": ((existing["uploaded_by"] if existing else None)
                        or job.get("user_id")),
        "language": detect_language(sample),
    }
    registry.upsert_book(con, meta)
    registry.upsert_chapters(con, slug, ch_rows)
    try:
        registry.reindex_book(con, slug, skill_dir)
    except Exception as e:
        print(f"warn: reindex {slug} failed: {e}", flush=True)
    return meta


def push_to_server(pdf_path, skill_dir, slug, title="", author="", intro="",
                   publisher="", book_type="", depth="", model_used="",
                   lang_mode=""):
    """把本地转换成品(skill 包 + 原 PDF)推给服务端 /api/books/import 执行 publish。"""
    import io
    import urllib.error
    import urllib.request
    import zipfile

    base = os.environ.get("AIBOOK_BASE_URL", "https://aibook.zxixing.com").rstrip("/")
    token = os.environ.get("AIBOOK_TOKEN", "")
    if not token:
        raise RuntimeError("推送需要环境变量 AIBOOK_TOKEN")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(Path(skill_dir).rglob("*")):
            if f.is_file():
                z.write(f, str(f.relative_to(skill_dir)))
    zip_bytes = buf.getvalue()
    pdf = Path(pdf_path)

    boundary = "----imp" + uuid.uuid4().hex
    body = bytearray()
    for k, v in (("slug", slug), ("title", title), ("author", author),
                 ("intro", intro), ("publisher", publisher),
                 ("book_type", book_type), ("depth", depth),
                 ("model_used", model_used), ("lang_mode", lang_mode)):
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f"name=\"{k}\"\r\n\r\n{v or ''}\r\n").encode("utf-8")
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
             f"filename=\"{pdf.name}\"\r\nContent-Type: application/pdf\r\n\r\n"
             ).encode("utf-8")
    body += pdf.read_bytes() + b"\r\n"
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"skill\"; "
             f"filename=\"{slug}-skill.zip\"\r\n"
             f"Content-Type: application/zip\r\n\r\n").encode("utf-8")
    body += zip_bytes + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(base + "/api/books/import",
                                 data=bytes(body), method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": e.code,
                "detail": e.read().decode("utf-8", "replace")[:500]}


def _serve_alive(url, timeout=1):
    """探测本地 opencode serve 是否在监听。"""
    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(url.rstrip("/"), timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def ensure_opencode_serve(cfg, wait=45):
    """确保本地 opencode serve 已启动（免费模型走 serve 才能用）。返回 base url。"""
    base = (cfg.get("opencode_serve_url") or "http://127.0.0.1:4096").rstrip("/")
    if _serve_alive(base):
        return base
    port = base.rsplit(":", 1)[-1] or "4096"
    subprocess.Popen([opencode_bin(cfg), "serve", "--port", port],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     cwd=str(PROJECT))
    deadline = time.time() + wait
    while time.time() < deadline:
        if _serve_alive(base):
            return base
        time.sleep(1)
    raise RuntimeError(f"opencode serve 启动超时: {base}")


def _api_base_token():
    base = os.environ.get("AIBOOK_BASE_URL", "https://aibook.zxixing.com").rstrip("/")
    token = os.environ.get("AIBOOK_TOKEN", "")
    if not token:
        raise RuntimeError("需要环境变量 AIBOOK_TOKEN")
    return base, token


def _safe_extract(data, dest):
    """解包（平台无关的路径穿越校验）。"""
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for zi in zf.infolist():
            raw = str(zi.filename).replace("\\", "/")
            parts = [x for x in raw.split("/") if x and x != "."]
            if raw.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
                raise ValueError(f"非法路径: {zi.filename}")
        zf.extractall(dest)


def download_skill(cfg, slug, force=False):
    """从服务端拉取 skill 包到本地 skills_home/slug。

    返回 (skill_dir, downloaded)。
    """
    import urllib.error
    import urllib.request

    dest = Path(cfg["skills_home"]) / slug
    if (dest / "SKILL.md").is_file() and not force:
        return dest, False
    base, token = _api_base_token()
    req = urllib.request.Request(base + f"/api/books/{slug}/download/skill")
    req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"下载 skill 失败: {e.code} "
            f"{e.read().decode('utf-8', 'replace')[:300]}")
    tmp = Path(tempfile.mkdtemp(prefix="sk-"))
    try:
        _safe_extract(data, tmp)
        if (dest / "SKILL.md").is_file():
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), str(dest))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dest, True


def push_zh_to_server(slug, zh_dir, model=""):
    """把本地生成的 chapters_zh 译文包回传服务端 /api/books/{slug}/upload-zh。"""
    import io
    import urllib.error
    import urllib.request
    import zipfile

    base, token = _api_base_token()
    files = sorted(Path(zh_dir).glob("*.md"))
    if not files:
        return {"error": 0, "detail": f"no chapter md in {zh_dir}"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, "chapters_zh/" + f.name)

    boundary = "----zh" + uuid.uuid4().hex
    body = bytearray()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; "
             f"name=\"model\"\r\n\r\n{model or ''}\r\n").encode("utf-8")
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
             f"filename=\"{slug}-zh.zip\"\r\n"
             f"Content-Type: application/zip\r\n\r\n").encode("utf-8")
    body += buf.getvalue() + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(base + f"/api/books/{slug}/upload-zh",
                                 data=bytes(body), method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": e.code,
                "detail": e.read().decode("utf-8", "replace")[:500]}


def rm_cos_pdf(job, pdf):
    if pdf and cosutil.is_cos_path(job.get("pdf_path") or ""):
        try:
            Path(pdf).unlink(missing_ok=True)
        except OSError:
            pass


SYS_ONE = ("把下面这段书籍内容翻译成简体中文。保留 Markdown 标记(标题/列表/表格/代码)；"
           "纯英文标题也要翻译(如 '# Chapter 1: Introduction' -> '# 第1章 简介')；"
           "只输出译文本身，不要保留英文原文、不要任何解释。")


def _split_blocks(text):
    return [p.strip() for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]


def _looks_zh(orig, out):
    ol = len(re.findall(r"[A-Za-z]", orig))
    if ol <= 15:
        return True
    zc = len(re.findall(r"[\u4e00-\u9fff]", out))
    zl = len(re.findall(r"[A-Za-z]", out))
    return zc >= 3 and zl < ol * 0.5


def _join_pairs(blocks, parts):
    segs = []
    for b, p in zip(blocks, parts):
        segs.append(b.strip())
        segs.append(p.strip())
    return "\n\n===zh===\n\n".join(segs)


def _trans_chain(cfg):
    chain = cfg.get("translate_chain") or []
    if not chain:
        chain = [cfg.get("answer_model") or "opencode/mimo-v2.6-flash-free"]
    return [m for m in chain if m]


def _translate_blocks(cfg, text, timeout=None):
    """逐块独立翻译：必然按块 1:1 对齐。返回 (pairs_md, info)。"""
    if timeout is None:
        timeout = int(cfg.get("translate_block_timeout", 180))
    blocks = _split_blocks(text)
    if not blocks:
        return "", ""
    chain = _trans_chain(cfg)
    used = chain[0]
    zs = []
    for i, b in enumerate(blocks):
        best = None
        for model in chain:
            sub = {**cfg, "answer_model": model}
            out, info = llm.chat(sub, SYS_ONE, b, timeout=max(60, timeout))
            if not out:
                continue
            best = out
            if _looks_zh(b, out):
                used = model
                break
        if not best:
            return None, f"translate failed at block {i+1}/{len(blocks)}"
        zs.append(best.strip())
    return _join_pairs(blocks, zs), used


def _translate_one(cfg, f, out_dir):
    """单章翻译（供线程池调用）。只做文件读写与 LLM 调用，不碰 DB。

    返回 {"name", "status": "ok|skip|error", "model", "error"}。
    """
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            return {"name": f.name, "status": "skip", "model": "", "error": ""}
        pairs, info = _translate_blocks(cfg, text)
        if not pairs:
            return {"name": f.name, "status": "error", "model": "",
                    "error": str(info)}
        (out_dir / f.name).write_text(pairs, encoding="utf-8")
        return {"name": f.name, "status": "ok",
                "model": info if isinstance(info, str) else "", "error": ""}
    except Exception as e:
        return {"name": f.name, "status": "error", "model": "", "error": str(e)}


def process_translate(cfg, con, job, force=False, cancel=None):
    job = dict(job)
    opts = {}
    try:
        if job["detail"] and job["detail"].startswith("{"):
            opts = json.loads(job["detail"]) or {}
    except json.JSONDecodeError:
        opts = {}
    lang = opts.get("lang_mode")
    job_id = job["id"]
    slug = job["slug"]
    if lang not in ("zh", "bi"):
        registry.update_job(con, job_id, state="failed",
                            error="invalid lang_mode", detail="failed: invalid lang_mode",
                            finished_at=int(time.time()))
        return "failed"
    skill_dir = Path(cfg["skills_home"]) / slug
    ch_dir = skill_dir / "chapters"
    if not ch_dir.is_dir():
        registry.update_job(con, job_id, state="failed",
                            error="no chapters to translate",
                            detail="failed: no chapters", finished_at=int(time.time()))
        return "failed"
    out_dir = skill_dir / "chapters_zh"
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(ch_dir.glob("*.md"))
    started = time.time()
    registry.update_job(con, job_id, state="running", started_at=int(started), attempt=1,
                        detail=f"translating 0/{len(files)}")
    model_used = ""
    done = 0
    try:
        conc = max(1, int(cfg.get("translate_concurrency", 4)))
        first_err = None
        models = []
        cancelled = False
        # 章节级并行：总并发 = min(池大小, 章节数)，块内仍串行 → 并发可控不打爆配额
        with ThreadPoolExecutor(max_workers=conc) as ex:
            futs = [ex.submit(_translate_one, cfg, f, out_dir) for f in files]
            for fut in as_completed(futs):
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    for f2 in futs:
                        f2.cancel()
                    break
                r = fut.result()
                if r["status"] == "error":
                    first_err = first_err or r["error"]
                    continue
                if r["status"] == "skip":
                    continue
                done += 1
                if r["model"]:
                    models.append(r["model"])
                registry.update_job(con, job_id, detail=f"translating {done}/{len(files)}")
        if cancelled:
            return _abort_cancelled(con, job_id, "during translate")
        if first_err:
            registry.update_job(con, job_id, state="failed",
                                error=f"translate failed: {first_err}",
                                detail=f"failed: {str(first_err)[:120]}",
                                finished_at=int(time.time()),
                                elapsed_sec=int(time.time() - started))
            return "failed"
        if models:
            model_used = max(set(models), key=models.count)
        registry.put_translation(con, slug, "zh", out_dir, model=model_used)
        push_peer(cfg, [(out_dir, True)])
    except Exception as e:
        registry.update_job(con, job_id, state="failed",
                            error=f"translate error: {e}",
                            detail=f"failed: {str(e)[:120]}",
                            finished_at=int(time.time()),
                            elapsed_sec=int(time.time() - started))
        return "failed"
    finished = time.time()
    registry.update_job(con, job_id, state="success",
                        detail=f"translated {done} chapters (zh)",
                        finished_at=int(finished), elapsed_sec=int(finished - started),
                        model=model_used or None)
    return "success"


def _job_state(con, job_id):
    row = con.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    if hasattr(row, "keys"):
        return row["state"]
    return row[0]


def _abort_cancelled(con, job_id, reason="external cancel"):
    """外部取消时的收尾：优先保留外部已写入的状态，仅当仍是 running 才补一次收尾。"""
    state = _job_state(con, job_id)
    if state == "running":
        registry.update_job(con, job_id, state="failed",
                            error=f"cancelled: {reason}",
                            detail=f"failed: cancelled ({reason})",
                            finished_at=int(time.time()))
    print(f"cancel: {job_id} aborted ({reason}), db state={state}", flush=True)
    return "cancelled"


def start_heartbeat(db, job_id, worker_id, lease_sec, interval=60,
                    cancel=None, cancel_poll_sec=5):
    """后台续租 + 取消检测线程。用独立连接，避免跨线程共用连接（SQLite 禁止跨线程）。

    任务状态被外部改成非 running（人工取消）、或租约已不属于自己（被接管/回收）时，
    置位 cancel 事件，供主流程立即中止进行中的转换，而不是继续跑完整个重试循环。
    返回 stop()，任务结束后调用以结束线程。
    """
    stop = threading.Event()
    # 取消检测要快（默认 5s），续租不需要那么频繁 → 取两者的较快节拍
    tick = max(1, min(int(interval), int(cancel_poll_sec)))

    def beat():
        try:
            con = registry.connect(db)
        except Exception as e:
            print(f"heartbeat connect failed: {e}", flush=True)
            return
        while not stop.wait(tick):
            try:
                state = _job_state(con, job_id)
                if state is not None and state != "running":
                    print(f"heartbeat: job {job_id} externally set to {state}, "
                          f"cancelling", flush=True)
                    if cancel is not None:
                        cancel.set()
                    break
                if not registry.heartbeat_job(con, job_id, worker_id, lease_sec):
                    print(f"heartbeat: lost ownership of {job_id}", flush=True)
                    if cancel is not None:
                        cancel.set()
                    break
            except Exception as e:
                print(f"heartbeat error: {e}", flush=True)
        try:
            con.close()
        except Exception:
            pass

    threading.Thread(target=beat, daemon=True, name=f"hb-{job_id}").start()

    def stop_fn():
        stop.set()
    return stop_fn


def process_job(cfg, con, job, force=False, cancel=None):
    job = dict(job)
    if job.get("kind") == "translate":
        return process_translate(cfg, con, job, force=force, cancel=cancel)
    opts = {}
    try:
        if job["detail"] and job["detail"].startswith("{"):
            opts = json.loads(job["detail"]) or {}
    except json.JSONDecodeError:
        opts = {}
    lang_mode = opts.get("lang_mode")
    if not lang_mode:
        lang_mode = "zh" if opts.get("translate_zh") else ""
    if lang_mode not in ("", "zh", "bi"):
        lang_mode = ""
    cfg = {**cfg,
           "book_type": opts.get("book_type") or cfg.get("book_type", "text"),
           "depth": opts.get("depth") or cfg.get("depth", "study"),
           "lang_mode": lang_mode,
           "translate_zh": lang_mode in ("zh", "bi")}
    job_id = job["id"]
    slug = job["slug"]
    log_path = path_in(cfg, "logs") / f"{job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    registry.update_job(con, job_id, log_path=str(log_path),
                        model=cfg["_model"] or "opencode-auth")
    pdf = Path(job["pdf_path"])
    if cosutil.is_cos_path(job["pdf_path"]):
        parsed = cosutil.parse_cos_path(job["pdf_path"])
        dest = path_in(cfg, "inbox") / (Path(parsed[1]).name if parsed else "cos.pdf")
        got = cosutil.download_to(cfg, job["pdf_path"], dest)
        if not got:
            registry.update_job(con, job_id, state="failed",
                                error="cos download failed",
                                detail="failed: cos download failed",
                                finished_at=int(time.time()))
            return "failed"
        pdf = got
    if not pdf.exists():
        registry.update_job(con, job_id, state="failed", error="pdf not found")
        return "failed"
    sha = registry.sha256_file(pdf)
    book = registry.find_book(con, slug) if slug else None
    if book and book["source_sha256"] == sha and book["status"] == "published" and not force:
        push_peer(cfg, [
            (Path(cfg["skills_home"]) / slug, True),
            (Path(cfg["covers"]) / f"{slug}.jpg", False),
            (Path(cfg["library"]) / slug, True),
        ])
        registry.update_job(con, job_id, state="success", detail="skipped: unchanged source",
                            finished_at=int(time.time()), elapsed_sec=0)
        rm_cos_pdf(job, pdf)
        return "skipped"
    if not force:
        dups = [b for b in registry.find_books_by_sha(con, sha)
                if b["slug"] != slug]
        if lang_mode in ("zh", "bi"):
            dups = [b for b in dups if b.get("language") in ("zh", "bi")]
        if dups:
            space = job.get("space_id") or "local"
            mine = next((b for b in dups if b["space_id"] == space), None)
            if mine:
                target = mine["slug"]
            else:
                src = dups[0]
                base = src["slug"]
                target = f"{base}-2"
                n = 2
                while registry.find_book(con, target):
                    n += 1
                    target = f"{base}-{n}"
                registry.copy_book(con, src, target, space,
                                   uploaded_by=job.get("user_id"))
            registry.update_job(con, job_id, state="success", slug=target,
                                detail=f"duplicate: reused {target}",
                                finished_at=int(time.time()), elapsed_sec=0)
            try:
                log_path.write_text(
                    f"duplicate content, reuse existing book: {target}\n",
                    encoding="utf-8")
            except OSError:
                pass
            rm_cos_pdf(job, pdf)
            return "duplicate"
    if not slug:
        slug = slugify(pdf.name, sha)
    work_root = path_in(cfg, "workdir") / f"{slug}-{job_id[:8]}"
    if work_root.exists():
        shutil.rmtree(work_root, ignore_errors=True)
    work_root.mkdir(parents=True)
    local_pdf = work_root / "source.pdf"
    shutil.copy2(pdf, local_pdf)
    skill_dir = Path(cfg["skills_home"]) / slug
    bak_dir = work_root / "skill.bak"
    if skill_dir.exists():
        shutil.move(str(skill_dir), str(bak_dir))
    started = time.time()
    registry.update_job(con, job_id, slug=slug, state="running",
                        started_at=int(started), attempt=job["attempt"] + 1)
    session = None
    attempts = 1 + max(0, cfg.get("max_retries", 2) - 1)
    rep = None
    run = None
    explicit_model = (job.get("model") or "").strip()
    chain = _build_chain(cfg, explicit_model)
    chain_i = 0
    active_cfg = {**cfg, "_model": chain[0]} if chain else cfg
    switched = False
    for attempt in range(attempts):
        if cancel is not None and cancel.is_set():
            return _abort_cancelled(con, job_id, "before attempt")
        if attempt == 0 or switched:
            prompt = render_prompt(active_cfg, {"slug": slug, "pdf_path": str(local_pdf),
                                                "workdir": str(work_root), "skill_dir": str(skill_dir),
                                                "scan_tool": cfg["scan_tool"]})
        else:
            prompt = FIX_PROMPT.format(problems="; ".join(rep["problems"]), skill_dir=skill_dir)
        switched = False
        run = run_opencode(active_cfg, prompt, job_id, log_path, work_root,
                           session_id=session, cancel=cancel)
        session = run["session"] or session
        if run.get("cancelled") or (cancel is not None and cancel.is_set()):
            return _abort_cancelled(con, job_id, "during conversion run")
        rep = gate.evaluate(skill_dir, cfg["scan_tool"], run["stdout"])
        registry.update_job(con, job_id, attempt=attempt + 1, model=active_cfg["_model"] or None,
                            detail=run["detail"] or f"attempt {attempt + 1}",
                            token_actual=(run["tokens_in"] + run["tokens_out"]) or None)
        if rep["ok"]:
            break
        unavailable = _is_model_unavailable(run)
        is_free = (active_cfg["_model"] or "").startswith("opencode/")
        free_tries = job.get("free_fail") or 0
        if (unavailable and is_free
                and free_tries + 1 < cfg.get("free_nodes", 2)):
            if skill_dir.exists():
                shutil.rmtree(skill_dir, ignore_errors=True)
            if bak_dir.exists() and not skill_dir.exists():
                shutil.move(str(bak_dir), str(skill_dir))
            shutil.rmtree(work_root, ignore_errors=True)
            registry.update_job(con, job_id, state="queued", slug=slug,
                                free_fail=free_tries + 1, attempt=job["attempt"],
                                detail="requeue: free model unavailable, retry on peer",
                                not_before=int(time.time()) + 15)
            return "requeued"
        if (attempt + 1 < attempts and unavailable and chain_i + 1 < len(chain)):
            chain_i += 1
            active_cfg = {**cfg, "_model": chain[chain_i]}
            switched = True
            for d in (skill_dir, work_root / "work"):
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)
            print(f"[{time.strftime('%H:%M:%S')}] {job_id} model unavailable, "
                  f"fallback to {chain[chain_i]}")
    finished = time.time()
    fields = {"finished_at": int(finished), "elapsed_sec": int(finished - started),
              "token_actual": (run["tokens_in"] + run["tokens_out"]) or None,
              "usage_raw": run["usage_raw"]}
    if rep and rep["ok"]:
        meta = publish(active_cfg, con, {**job, "slug": slug, "pdf_path": pdf}, skill_dir,
                       rep["result"], run)
        pushed = push_peer(cfg, [
            (skill_dir, True),
            (Path(cfg["covers"]) / f"{slug}.jpg", False),
            (Path(cfg["library"]) / slug, True),
        ])
        if pushed:
            fields.update(state="success", error=None, detail="published",
                          token_estimate=meta["token_estimate"])
            state = "success"
        else:
            # 本机已发布成功：peer push 失败只记警告，不作废已完成的转换成果
            fields.update(state="success", error=None,
                          detail="published (peer push pending: will sync later)",
                          token_estimate=meta["token_estimate"])
            state = "success"
            print(f"[{time.strftime('%H:%M:%S')}] {job_id} WARN peer push failed "
                  f"(non-fatal, published locally)", flush=True)
        if skill_dir.exists():
            shutil.rmtree(bak_dir, ignore_errors=True)
        else:
            shutil.move(str(bak_dir), str(skill_dir))
        shutil.rmtree(work_root, ignore_errors=True)
    else:
        if skill_dir.exists():
            shutil.rmtree(skill_dir, ignore_errors=True)
        if bak_dir.exists():
            shutil.move(str(bak_dir), str(skill_dir))
        problems = "; ".join((rep or {}).get("problems") or ["unknown"])
        real = _model_unavailable_reason(run)
        if real:
            msg = f"model unavailable ({real}); {problems}"
            fields.update(state="failed", error=msg[:500],
                          detail=f"failed: {msg[:160]}")
        else:
            fields.update(state="failed", error=problems[:500],
                          detail=f"failed: {problems[:120]}")
        state = "failed"
    push_peer(cfg, [(log_path, False)])
    registry.update_job(con, job_id, **fields)
    rm_cos_pdf(job, pdf)
    return state


def enqueue(cfg, con, pdf, slug, kind="convert", space="local", model=None):
    job_id = uuid.uuid4().hex[:12]
    registry.register_job(con, job_id, space, kind, pdf, slug, model or cfg["_model"] or None)
    return job_id


def interactive_pick(cfg, con):
    inbox = path_in(cfg, "inbox")
    inbox.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(inbox.glob("*.pdf"))
    print("\n== 收件箱待转换 ==")
    for i, p in enumerate(pdfs, 1):
        print(f"  [{i}] {p.name}")
    print("== 已入库(输入 slug 可重新处理) ==")
    for r in con.execute("SELECT slug,title,source_file FROM books ORDER BY slug"):
        mark = "" if r["source_file"] else " (无源文件)"
        print(f"  {r['slug']:<24} {r['title']}{mark}")
    print("  [q] 退出")
    sel = input("选择要处理的书或PDF编号: ").strip()
    if sel.lower() == "q":
        return None
    if sel.isdigit() and 1 <= int(sel) <= len(pdfs):
        pdf = pdfs[int(sel) - 1]
        sha = registry.sha256_file(pdf)
        sugg = slugify(pdf.name, sha)
        slug = input(f"skill slug [{sugg}]: ").strip() or sugg
        return {"pdf": pdf, "slug": slug, "kind": "convert"}
    row = con.execute("SELECT slug,source_file FROM books WHERE slug=?", (sel,)).fetchone()
    if row and row["source_file"] and Path(row["source_file"]).exists():
        return {"pdf": Path(row["source_file"]), "slug": row["slug"], "kind": "update"}
    print("无效选择")
    return None


def main():
    ap = argparse.ArgumentParser(description="book-skill 转换 worker")
    ap.add_argument("--pdf")
    ap.add_argument("--slug")
    ap.add_argument("--job", help="处理指定 job id")
    ap.add_argument("--rebuild", help="重建已入库 slug")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--daemon", action="store_true", help="轮询处理队列")
    ap.add_argument("--once", action="store_true", help="处理完所有排队任务后退出")
    ap.add_argument("--no-input", action="store_true")
    ap.add_argument("--upload", action="store_true",
                    help="本地转换成功后把 skill 成品+PDF 推送到服务器")
    ap.add_argument("--translate", metavar="SLUG",
                    help="本地翻译指定 slug 的章节并回传服务端")
    a = ap.parse_args()

    cfg = load_config()
    con = registry.connect(registry.resolve_db(cfg["registry_db"], PROJECT))

    if a.list:
        print(f"{'job':<14} {'state':<9} {'slug':<24} {'sec':>6} {'tok':>8} detail")
        for r in con.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 30"):
            print(f"{r['id']:<14} {r['state']:<9} {str(r['slug'] or ''):<24} "
                  f"{r['elapsed_sec'] or 0:>6} {r['token_actual'] or 0:>8} {r['detail'] or ''}")
        return

    if a.job:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (a.job,)).fetchone()
        if not row:
            print("job not found")
            sys.exit(1)
        state = process_job(cfg, con, row, force=a.force)
        print(f"job {a.job}: {state}")
        return

    if a.rebuild:
        row = registry.find_book(con, a.rebuild)
        if not row or not row["source_file"]:
            print("no source file for slug")
            sys.exit(1)
        jid = enqueue(cfg, con, row["source_file"], a.rebuild, kind="update")
        print(f"job {jid}: " + process_job(cfg, con,
              con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone(), force=True))
        return

    if a.translate:
        slug = a.translate
        skill_dir = Path(cfg["skills_home"]) / slug
        if not (skill_dir / "chapters").is_dir():
            skill_dir, pulled = download_skill(cfg, slug)
            print(f"skill <- server (downloaded={pulled})")
        if not (skill_dir / "chapters").is_dir():
            sys.exit(f"本地与服务端都没有 chapters: {slug}")
        out_dir = skill_dir / "chapters_zh"
        have = sorted(out_dir.glob("*.md")) if out_dir.is_dir() else []
        jid = None
        if have and not a.force:
            print(f"chapters_zh 已有 {len(have)} 章，跳过翻译直接回传（--force 可重译）")
        else:
            jid = uuid.uuid4().hex[:12]
            brow = registry.find_book(con, slug)
            space = (dict(brow).get("space_id") if brow else None) or "local"
            registry.register_job(con, jid, space, "translate",
                                  str(skill_dir), slug)
            con.execute("UPDATE jobs SET detail=? WHERE id=?",
                        (json.dumps({"lang_mode": "zh"}), jid))
            con.commit()
            job = con.execute("SELECT * FROM jobs WHERE id=?",
                              (jid,)).fetchone()
            chain = cfg.get("translate_chain") or []
            if any(m.startswith("opencode/") for m in chain):
                try:
                    print("serve -> " + ensure_opencode_serve(cfg))
                except RuntimeError as e:
                    print(f"warn: {e}，回退到非 opencode 模型")
            st = process_translate(cfg, con, job, force=a.force)
            print(f"translate {jid} -> {st}")
            if st != "success":
                sys.exit(1)
        used = cfg.get("answer_model") or ""
        if jid:
            row = con.execute("SELECT model FROM jobs WHERE id=?",
                              (jid,)).fetchone()
            used = (row["model"] if row else None) or used
        res = push_zh_to_server(slug, out_dir, model=used)
        print("push-zh -> " + json.dumps(res, ensure_ascii=False))
        if res.get("error"):
            sys.exit(1)
        return

    if a.daemon or a.once:
        db = registry.resolve_db(cfg["registry_db"], PROJECT)
        worker_id = f"{socket.gethostname()}:{os.getpid()}"
        lease_sec = int(cfg.get("lease_sec", 300))
        hb_interval = max(15, lease_sec // 3)
        reap_every = int(cfg.get("reap_interval_sec", 60))
        next_reap = 0.0
        print(f"[{time.strftime('%H:%M:%S')}] worker {worker_id} "
              f"lease={lease_sec}s reap={reap_every}s", flush=True)
        while True:
            if time.time() >= next_reap:
                try:
                    registry.reap_stale_jobs(
                        con,
                        grace_sec=int(cfg.get("reap_grace_sec", 60)),
                        max_attempts=int(cfg.get("reap_max_attempts", 5)))
                except Exception as e:
                    print(f"reaper error: {e}", flush=True)
                next_reap = time.time() + reap_every
            job = registry.claim_next_job(con, worker_id=worker_id, lease_sec=lease_sec)
            if job and not cosutil.is_cos_path(job["pdf_path"]) \
                    and not Path(job["pdf_path"]).exists():
                if wait_file_release(con, job, int(time.time())):
                    print(f"[{time.strftime('%H:%M:%S')}] {job['id']} wait pdf sync")
                    time.sleep(3)
                    continue
            if job:
                print(f"[{time.strftime('%H:%M:%S')}] run {job['id']} {job['slug'] or job['pdf_path']}")
                cancel = threading.Event()
                stop_hb = start_heartbeat(db, job["id"], worker_id, lease_sec, hb_interval,
                                          cancel=cancel)
                try:
                    st = process_job(cfg, con, job, cancel=cancel)
                finally:
                    stop_hb()
                print(f"[{time.strftime('%H:%M:%S')}] {job['id']} -> {st}")
            elif a.once:
                return
            else:
                time.sleep(3)
        return

    if a.pdf:
        pdf = Path(a.pdf)
        slug = a.slug or slugify(pdf.name, registry.sha256_file(pdf))
        if a.dry_run:
            work = PROJECT / "workdir" / f"{slug}-dry"
            work.mkdir(parents=True, exist_ok=True)
            local = work / "source.pdf"
            if not local.exists():
                shutil.copy2(pdf, local)
            prompt = render_prompt(cfg, {"slug": slug, "pdf_path": str(local),
                                         "workdir": str(work),
                                         "skill_dir": Path(cfg["skills_home"]) / slug,
                                         "scan_tool": cfg["scan_tool"]})
            print("=== CMD ===\nopencode run --format json --agent publisher "
                  f"-m {cfg['_model'] or '(auth)'} <prompt>")
            print("=== PROMPT ===")
            print(prompt)
            print(f"=== AUTO CONF ===\n{AUTO_CONF.read_text(encoding='utf-8') if AUTO_CONF.exists() else '(not generated)'}")
            return
        jid = enqueue(cfg, con, pdf, slug, kind="convert")
        job = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        state = process_job(cfg, con, job, force=a.force)
        print(f"job {jid} -> " + state)
        if a.upload:
            if state != "success":
                sys.exit(f"本地转换未成功({state})，跳过推送")
            meta = dict(registry.find_book(con, slug) or {})
            res = push_to_server(pdf, Path(cfg["skills_home"]) / slug, slug,
                                 title=meta.get("title") or "",
                                 author=meta.get("author") or "",
                                 intro=meta.get("intro") or "",
                                 publisher=meta.get("publisher") or "",
                                 book_type=cfg["book_type"], depth=cfg["depth"],
                                 model_used=cfg["_model"],
                                 lang_mode=cfg.get("lang_mode") or "")
            print("push -> " + json.dumps(res, ensure_ascii=False))
            if res.get("error"):
                sys.exit(1)
        return

    if a.no_input:
        sys.exit("no --pdf/--job/--rebuild given")
    pick = interactive_pick(cfg, con)
    if not pick:
        return
    jid = enqueue(cfg, con, pick["pdf"], pick["slug"], kind=pick["kind"])
    job = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    print(f"job {jid} -> " + process_job(cfg, con, job))


if __name__ == "__main__":
    main()
