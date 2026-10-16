import asyncio
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

ENDPOINTS = {
    "deepseek": "https://api.deepseek.com/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
}


def _auth_key(provider):
    p = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    v = data.get(provider)
    if not isinstance(v, dict):
        return None
    for k in ("key", "token", "apiKey", "api_key"):
        if isinstance(v.get(k), str) and v[k]:
            return v[k]
    return None


def resolve(cfg):
    model = cfg.get("answer_model") or ""
    if not model:
        return None, None, None, "未配置 answer_model"
    base = cfg.get("answer_base_url") or ""
    provider = model.split("/", 1)[0] if "/" in model and not base else ""
    if provider == "opencode":
        short = model.split("/", 1)[1]
        base = cfg.get("opencode_serve_url") or "http://127.0.0.1:4096"
        return short, base, "", None
    if provider == "xiaopuyun":
        short = model.split("/", 1)[1]
        base = (cfg.get("xiaopuyun_base_url") or "").rstrip("/")
        key = cfg.get("xiaopuyun_api_key") or ""
        if not base or not key:
            return None, None, None, "未配置 xiaopuyun base_url/api_key"
        return short, base + "/chat/completions", key, None
    if not base:
        base = ENDPOINTS.get(provider)
    if not base:
        return None, None, None, f"provider {provider or model} 无直连端点"
    key = cfg.get("api_key") or ""
    if not key and cfg.get("api_key_env"):
        key = os.environ.get(cfg["api_key_env"], "")
    if not key:
        key = _auth_key(provider) or ""
    if not key:
        return None, None, None, f"未找到 {provider} 的 API key"
    short = model.split("/", 1)[1] if provider and model.startswith(provider + "/") else model
    return short, base, key, None


def _is_opencode_model(cfg):
    return (cfg.get("answer_model") or "").startswith("opencode/")


DEFAULT_MAX_TOKENS = 2000


def _max_tokens(cfg, key="answer_max_tokens", default=DEFAULT_MAX_TOKENS):
    try:
        return int(cfg.get(key) or default)
    except (TypeError, ValueError):
        return default


def _model_timeout(cfg, model, default):
    """按模型覆盖超时（config.model_timeouts），避免慢/坏模型拖垮整条链。"""
    tm = cfg.get("model_timeouts")
    if isinstance(tm, dict):
        try:
            v = tm.get(model)
            if v:
                return float(v)
        except (TypeError, ValueError):
            pass
    return default


def _acc_usage(usage_out, data):
    """把 OpenAI usage 累计进 usage_out（tokens_in/tokens_out）。多轮调用累加；
    usage_out 为 None 时不采集（向后兼容，返回签名不变）。"""
    if not isinstance(usage_out, dict) or not isinstance(data, dict):
        return
    u = data.get("usage")
    if not isinstance(u, dict):
        return
    for keys, dst in ((("prompt_tokens", "input_tokens"), "tokens_in"),
                      (("completion_tokens", "output_tokens"), "tokens_out")):
        for k in keys:
            if u.get(k) is not None:
                try:
                    usage_out[dst] = int(usage_out.get(dst) or 0) + int(u[k])
                except (TypeError, ValueError):
                    pass
                break


def _msg_content(data, allow_reasoning=False):
    """取回复正文。推理模型（如 deepseek-v4-flash）可能把 token 预算全花在
    reasoning_content 上导致 content 为空：此时视为失败、交给链上下一个模型，
    除非显式开启 cfg.reasoning_fallback（会把推理过程当正文，慎用）。"""
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    text = (msg.get("content") or "").strip()
    if text or not allow_reasoning:
        return text
    return (msg.get("reasoning_content") or "").strip()


def _answer_chain(cfg):
    chain = cfg.get("answer_chain") or []
    if not chain and cfg.get("answer_model"):
        chain = [cfg["answer_model"]]
    return [m for m in chain if m]


def _extract_serve_reply(msgs):
    for it in reversed(msgs if isinstance(msgs, list) else []):
        if it.get("type") != "assistant":
            continue
        content = it.get("content") or []
        text = "".join(p.get("text", "") for p in content
                       if isinstance(p, dict) and p.get("type") == "text")
        if text.strip():
            return text
    return None


def _serve_chat_sync(cfg, system, user, timeout=90):
    model, base, _key, err = resolve(cfg)
    if err:
        return None, None, err

    def call(path, payload=None, t=timeout):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            base + path, data=data, method="POST" if payload is not None else "GET",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=t) as r:
            body = r.read().decode("utf-8")
        return json.loads(body) if body else None

    try:
        sid = ((call("/api/session", {}) or {}).get("data") or {}).get("id")
        if not sid:
            return None, model, "opencode serve: no session"
        call(f"/api/session/{sid}/model",
             {"model": {"providerID": "opencode", "id": model}}, t=15)
        text = f"{system}\n\n{user}" if system else user
        call(f"/api/session/{sid}/prompt", {"prompt": {"text": text}}, t=15)
        deadline = time.time() + timeout
        while time.time() < deadline:
            msgs = (call(f"/api/session/{sid}/message", t=15) or {}).get("data") or []
            reply = _extract_serve_reply(msgs)
            if reply:
                return reply, model, None
            time.sleep(2)
        return None, model, "opencode serve: 回复超时"
    except Exception as e:
        return None, model, f"opencode serve error: {e}"


async def _serve_chat_async(cfg, system, user, timeout=90):
    model, base, _key, err = resolve(cfg)
    if err:
        return None, None, err
    client = _get_async_client()

    async def call(path, payload=None, t=timeout):
        kw = {"timeout": t, "headers": {"Content-Type": "application/json"}}
        if payload is not None:
            kw["json"] = payload
            r = await client.post(base + path, **kw)
        else:
            r = await client.get(base + path, **kw)
        r.raise_for_status()
        return r.json() if r.text else None

    try:
        sid = ((await call("/api/session", {})).get("data") or {}).get("id")
        if not sid:
            return None, model, "opencode serve: no session"
        await call(f"/api/session/{sid}/model",
                   {"model": {"providerID": "opencode", "id": model}}, t=15)
        text = f"{system}\n\n{user}" if system else user
        await call(f"/api/session/{sid}/prompt", {"prompt": {"text": text}}, t=15)
        deadline = time.time() + timeout
        while time.time() < deadline:
            msgs = (await call(f"/api/session/{sid}/message", t=15)).get("data") or []
            reply = _extract_serve_reply(msgs)
            if reply:
                return reply, model, None
            await asyncio.sleep(2)
        return None, model, "opencode serve: 回复超时"
    except Exception as e:
        return None, model, f"opencode serve error: {e}"


def _post(cfg, payload, timeout=90):
    model, base, key, err = resolve(cfg)
    if err:
        return None, None, err
    payload = {**payload, "model": model}
    req = urllib.request.Request(
        base, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data, model, None
    except Exception as e:
        return None, model, f"llm error: {e}"


def chat(cfg, system, user, timeout=90, usage_out=None):
    errs = []
    mt = _max_tokens(cfg)
    allow_r = bool(cfg.get("reasoning_fallback"))
    for model in _answer_chain(cfg):
        sub = {**cfg, "answer_model": model}
        to = _model_timeout(cfg, model, timeout)
        if _is_opencode_model(sub):
            text, _m, err = _serve_chat_sync(sub, system, user, to)
        else:
            data, _m, err = _post(sub, {
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": 0.3, "max_tokens": mt}, to)
            text = None if err else _msg_content(data, allow_r)
            if not err:
                _acc_usage(usage_out, data)
        if not err and text:
            return text, model
        errs.append(f"{model}: {err or 'empty reply'}")
    return None, (" | ".join(errs) if errs else "未配置 answer_model")


_async_client = None
_async_loop = None


def _get_async_client():
    global _async_client, _async_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if (_async_client is None or _async_loop is not loop
            or (loop is not None and loop.is_closed())):
        import httpx
        _async_client = httpx.AsyncClient()
        _async_loop = loop
    return _async_client


async def _post_async(cfg, payload, timeout=90):
    model, base, key, err = resolve(cfg)
    if err:
        return None, None, err
    payload = {**payload, "model": model}
    try:
        r = await _get_async_client().post(
            base, json=payload,
            headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
        r.raise_for_status()
        return r.json(), model, None
    except Exception as e:
        return None, model, f"llm error: {e}"


def _embedding_endpoint(cfg):
    """返回 (base, key, model) 或 (None, None, None)（未配置嵌入）。"""
    model = cfg.get("embedding_model") or ""
    if not model:
        return None, None, None
    base = (cfg.get("embedding_base_url") or "").rstrip("/")
    if not base:
        provider = model.split("/", 1)[0] if "/" in model else ""
        base = ENDPOINTS.get(provider, "")
        if base.endswith("/chat/completions"):
            base = base.rsplit("/chat/completions", 1)[0] + "/embeddings"
    if not base.endswith("/embeddings"):
        base = base + "/embeddings" if base else ""
    key = cfg.get("embedding_api_key") or cfg.get("api_key") or ""
    if not key and cfg.get("api_key_env"):
        key = os.environ.get(cfg["api_key_env"], "")
    short = model.split("/", 1)[1] if "/" in model else model
    return (base, key, short) if base and short else (None, None, None)


def embed_texts(cfg, texts, timeout=30, mode=None):
    """批量嵌入。未配置或失败返回 None（调用方应回退到纯词面检索）。
    mode: "query" 时 sidecar 加检索指令前缀，"doc"/None 不加。"""
    base, key, model = _embedding_endpoint(cfg)
    if not base or not texts:
        return None
    payload = {"model": model, "input": list(texts)}
    if mode:
        payload["mode"] = mode
    data, _m, err = _post_sync_raw(base, payload, key, timeout)
    if err:
        return None
    try:
        items = sorted(data["data"], key=lambda x: x.get("index", 0))
        vecs = [it["embedding"] for it in items]
        return vecs if len(vecs) == len(texts) else None
    except (KeyError, TypeError):
        return None


def _post_sync_raw(base, payload, key, timeout=30):
    try:
        req = urllib.request.Request(
            base, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8")
        return json.loads(body) if body else None, None, None
    except Exception as e:
        return None, None, f"embed error: {e}"


async def chat_async(cfg, system, user, timeout=90, usage_out=None):
    errs = []
    mt = _max_tokens(cfg)
    allow_r = bool(cfg.get("reasoning_fallback"))
    for model in _answer_chain(cfg):
        sub = {**cfg, "answer_model": model}
        to = _model_timeout(cfg, model, timeout)
        if _is_opencode_model(sub):
            text, _m, err = await _serve_chat_async(sub, system, user, to)
        else:
            data, _m, err = await _post_async(sub, {
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": 0.3, "max_tokens": mt}, to)
            text = None if err else _msg_content(data, allow_r)
            if not err:
                _acc_usage(usage_out, data)
        if not err and text:
            return text, model
        errs.append(f"{model}: {err or 'empty reply'}")
    return None, (" | ".join(errs) if errs else "未配置 answer_model")


async def _post_stream_async(cfg, payload, timeout=90, usage_out=None):
    """流式 POST：返回 (async_generator, model, err)。generator 产出增量文本。"""
    model, base, key, err = resolve(cfg)
    if err:
        return None, None, err
    payload = {**payload, "model": model, "stream": True}
    import httpx
    try:
        client = _get_async_client()
        req = client.build_request(
            "POST", base, json=payload,
            headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
        resp = await client.send(req, stream=True)
        resp.raise_for_status()
    except Exception as e:
        return None, model, f"llm stream error: {e}"

    async def gen():
        try:
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if isinstance(chunk.get("usage"), dict):
                    _acc_usage(usage_out, chunk)
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content") or ""
                if text:
                    yield text
        finally:
            await resp.aclose()

    return gen(), model, None


async def chat_async_stream(cfg, system, user, timeout=90, usage_out=None):
    """流式问答：yield 增量文本；末尾通过返回值难以传递 model，
    故用属性挂在 generator 上（实际用法：async for chunk, 最终 model 见函数内 log）。
    返回 (generator, model_used, err)。若整链失败，generator 产出空、err 非空。"""
    errs = []
    mt = _max_tokens(cfg)
    allow_r = bool(cfg.get("reasoning_fallback"))
    for model in _answer_chain(cfg):
        sub = {**cfg, "answer_model": model}
        to = _model_timeout(cfg, model, timeout)
        if _is_opencode_model(sub):
            continue  # opencode serve 不支持流式，跳过
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        gen, _m, err = await _post_stream_async(
            sub, {"messages": messages, "temperature": 0.3, "max_tokens": mt,
                  "stream_options": {"include_usage": True}}, to,
            usage_out=usage_out)
        if err or gen is None:
            errs.append(f"{model}: {err or 'no stream'}")
            continue
        # 先探一个 chunk 判断是否立即失败/空回复
        buffer = []
        first = None
        try:
            async for chunk in gen:
                first = chunk
                break
        except Exception as e:
            errs.append(f"{model}: stream failed at start: {e}")
            continue
        if first is None:
            errs.append(f"{model}: empty stream")
            continue

        async def chained(g=gen, f=first):
            yield f
            async for c in g:
                yield c
        return chained(), model, None
    return None, None, (" | ".join(errs) if errs else "未配置 answer_model 或不支持流式")


ANSWER_SYS = (
    "你是读书助手，回答用中文、简洁准确。"
    "优先基于提供的书摘与概览归纳作答，包括总览型、概括型问题；"
    "若材料与问题不完全对应，给出书中最接近的概念并说明侧重点；"
    "仅当材料与问题完全无关时才回答'书中相关内容不足以回答'，不得编造。"
    "回答末尾用一行列出引用的章节，格式：【引用:章节名】。"
)


def build_excerpts(question, hits, title=""):
    parts = [f"书名: {title}" if title else ""]
    for h in hits[:5]:
        src = h.get("chapter_title") or h.get("path", "")
        lines = " / ".join(l["snippet"] for l in h.get("lines", [])[:3])
        parts.append(f"[{src}]\n{lines}")
    body = "\n\n".join(p for p in parts if p)
    return f"{body}\n\n问题: {question}"


def _answer_prompt(question, hits, title, fallback):
    if hits:
        user = build_excerpts(question, hits, title)
        if fallback:
            user += f"\n\n书的概览(可据此归纳全书主旨):\n{fallback[:1500]}"
        return ANSWER_SYS, user
    if fallback:
        return ANSWER_SYS, (f"书名: {title}\n\n书的概览(SKILL):\n{fallback[:3000]}\n\n"
                            f"问题: {question}")
    return None, None


def answer_question(cfg, question, hits, title="", fallback="", usage_out=None):
    system, user = _answer_prompt(question, hits, title, fallback)
    if user is None:
        return None, "no excerpts"
    return chat(cfg, system, user, usage_out=usage_out)


async def answer_question_async(cfg, question, hits, title="", fallback="",
                                usage_out=None):
    system, user = _answer_prompt(question, hits, title, fallback)
    if user is None:
        return None, "no excerpts"
    return await chat_async(cfg, system, user, usage_out=usage_out)


LIBRARY_SYS = (
    "你是一个跨书籍知识库助手。下面是来自**多本书**的原文片段（每条带《书名》与章节）。"
    "回答用中文、简洁准确，优先**综合多本书**的观点，指出它们的一致与分歧。"
    "引用的来源必须来自给定片段，不得编造书名或章节。"
    "若不同书的结论冲突，明确说明各自的侧重或前提。"
    "若片段与问题完全无关，只回答'书库中相关内容不足以回答'。"
    "回答末尾用一行列出引用，格式：【引用:《书名》·章节名】。"
)


def build_library_excerpts(question, hits, catalog="", max_hits=8):
    parts = []
    if catalog:
        parts.append("书库目录:\n" + catalog)
    for h in hits[:max_hits]:
        src = h.get("chapter_title") or h.get("path", "")
        book = h.get("book_title") or h.get("book_slug", "")
        label = f"《{book}》·{src}" if book else src
        lines = " / ".join(l["snippet"] for l in h.get("lines", [])[:3])
        parts.append(f"[{label}]\n{lines}")
    body = "\n\n".join(p for p in parts if p)
    return f"{body}\n\n问题: {question}"


async def answer_library_async(cfg, question, hits, catalog="", timeout=90,
                               usage_out=None):
    if not hits:
        return None, "no excerpts"
    return await chat_async(cfg, LIBRARY_SYS,
                            build_library_excerpts(question, hits, catalog),
                            timeout, usage_out=usage_out)


def answer_library(cfg, question, hits, catalog="", timeout=90, usage_out=None):
    if not hits:
        return None, "no excerpts"
    return chat(cfg, LIBRARY_SYS,
                build_library_excerpts(question, hits, catalog), timeout,
                usage_out=usage_out)


DEEP_TOOLS = [
    {"type": "function", "function": {
        "name": "search",
        "description": "在本书全部内容中全文检索，返回相关文件路径与片段。回答前先用它定位。",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "检索词"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read",
        "description": "读取本书某个文件正文（如 SKILL.md、chapters/ch02-*.md）。"
                       "定位后用它深入阅读，复杂问题至少读 1-2 个完整文件。",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string", "description": "相对路径"}},
                       "required": ["path"]}}},
]

DEEP_SYS = (
    "你是读书助手，回答用中文、简洁准确。"
    "你可以反复调用 search 和 read 工具调查这本书：先 search 定位相关文件，"
    "再 read 深入阅读（可先 read SKILL.md 了解全书结构；跨章节问题要读多个文件）。"
    "当你不再需要调用工具时，直接输出最终回答，末尾用一行列出引用：【引用:章节名】。"
    "若材料确实与问题无关，才回答'书中相关内容不足以回答'，不得编造。"
)


def _find_opencode():
    import shutil
    p = shutil.which("opencode")
    if p:
        return p
    for cand in ("/opt/node-v20.19.0-linux-x64/bin/opencode",):
        if Path(cand).exists():
            return cand
    return "opencode"


def _deep_run_opencode(cfg, question, title, intro, workdir, timeout=420):
    model = cfg.get("answer_model") or ""
    prompt = (DEEP_SYS + "\n\n"
              + (f"书名: {title}\n" if title else "")
              + (f"简介: {intro[:300]}\n" if intro else "")
              + f"问题: {question}\n\n"
              "本书文件就在当前工作目录：先查看目录结构和 SKILL.md 了解全书，"
              "再检索并阅读相关章节原文后作答。")
    cmd = [_find_opencode(), "run", "-m", model, "--title", "deep-qa", prompt]
    env = {**os.environ, "PYTHONUTF8": "1"}
    try:
        proc = subprocess.run(
            cmd, cwd=str(workdir), env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "opencode deep run 超时"
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    lines = [l for l in out.splitlines() if not l.startswith("> ")]
    text = "\n".join(lines).strip()
    if not text or proc.returncode != 0:
        return None, f"opencode deep run failed (exit {proc.returncode}): {out[-300:]}"
    return text, model


async def _deep_openai_async(cfg, question, title, intro, search_fn, read_fn,
                             max_rounds, timeout, usage_out=None):
    user = f"书名: {title}\n"
    if intro:
        user += f"简介: {intro[:300]}\n"
    user += f"问题: {question}"
    messages = [{"role": "system", "content": DEEP_SYS},
                {"role": "user", "content": user}]
    last_model = None
    mt = _max_tokens(cfg, "deep_max_tokens", 2000)
    allow_r = bool(cfg.get("reasoning_fallback"))
    for _ in range(max_rounds):
        data, last_model, err = await _post_async(cfg, {
            "messages": messages, "tools": DEEP_TOOLS,
            "temperature": 0.3, "max_tokens": mt}, timeout)
        if err:
            return None, last_model, err
        _acc_usage(usage_out, data)
        msg = data["choices"][0]["message"]
        messages.append({k: msg[k] for k in ("role", "content", "tool_calls")
                         if msg.get(k) is not None})
        tcs = msg.get("tool_calls") or []
        if not tcs:
            return _msg_content(data, allow_r), last_model, None
        for tc in tcs:
            fn = (tc.get("function") or {}).get("name") or ""
            try:
                args = json.loads(tc.get("function", {}).get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if fn == "search" and search_fn:
                out = await asyncio.to_thread(search_fn, str(args.get("query", "")))
            elif fn == "read" and read_fn:
                out = await asyncio.to_thread(read_fn, str(args.get("path", "")))
            else:
                out = f"未知工具: {fn}"
            if not isinstance(out, str):
                out = json.dumps(out, ensure_ascii=False)
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "content": out[:12000]})
    messages.append({"role": "user",
                     "content": "请基于已获得的材料直接给出最终回答，不要再调用工具。"})
    data, last_model, err = await _post_async(cfg, {
        "messages": messages, "temperature": 0.3, "max_tokens": mt}, timeout)
    if err:
        return None, last_model, err
    _acc_usage(usage_out, data)
    return _msg_content(data, allow_r), last_model, None


async def deep_answer_async(cfg, question, title="", intro="", search_fn=None,
                            read_fn=None, max_rounds=6, timeout=45, workdir=None,
                            usage_out=None):
    errs = []
    for model in _answer_chain(cfg):
        sub = {**cfg, "answer_model": model}
        if _is_opencode_model(sub):
            if not workdir:
                errs.append(f"{model}: no workdir")
                continue
            text, info = await asyncio.to_thread(
                _deep_run_opencode, sub, question, title, intro, workdir)
            if text:
                return text, model
            errs.append(f"{model}: {info}")
            continue
        text, info, err = await _deep_openai_async(
            sub, question, title, intro, search_fn, read_fn, max_rounds,
            _model_timeout(cfg, model, timeout), usage_out=usage_out)
        if text:
            return text, model
        errs.append(f"{model}: {err or info}")
    return None, (" | ".join(errs) if errs else "未配置 answer_model")


def deep_answer(cfg, question, title="", intro="", search_fn=None, read_fn=None,
                max_rounds=6, timeout=45, workdir=None, usage_out=None):
    return asyncio.run(deep_answer_async(
        cfg, question, title=title, intro=intro,
        search_fn=search_fn, read_fn=read_fn,
        max_rounds=max_rounds, timeout=timeout, workdir=workdir,
        usage_out=usage_out))
