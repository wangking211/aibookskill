"""本地嵌入 sidecar：fastembed + BAAI/bge-small-zh-v1.5，OpenAI 兼容接口。

仅监听 127.0.0.1（EMBED_PORT，默认 8799），供 registry/llm 通过
config.embedding_base_url=http://127.0.0.1:8799/v1 调用。

接口:
  POST /v1/embeddings  {model, input: str|[str], mode?: "query"}  -> OpenAI 格式
      mode=="query" 时给每条输入加 bge 检索指令前缀（文档侧不加）。
  GET  /health         -> {status, model, loaded, error}

模型加载失败时 /v1/embeddings 返回 503，调用方 embed_texts 收 None
自动回退纯词面检索（零风险降级）。
"""
import os
import threading

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

EMBED_PORT = int(os.environ.get("EMBED_PORT", "8799"))
EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

app = FastAPI()
_model = None
_load_err = None
_lock = threading.Lock()


def _get_model():
    """懒加载 fastembed 模型；失败缓存错误（不会反复重试拖垮依赖方）。"""
    global _model, _load_err
    if _model is None and _load_err is None:
        with _lock:
            if _model is None and _load_err is None:
                try:
                    from fastembed import TextEmbedding
                    _model = TextEmbedding(model_name=EMBED_MODEL)
                except Exception as e:
                    _load_err = f"{type(e).__name__}: {e}"
    return _model


class EmbedBody(BaseModel):
    model: str = ""
    input: object = Field(default_factory=list)
    mode: str = ""


@app.get("/health")
def health():
    return {"status": "ok", "model": EMBED_MODEL,
            "loaded": _model is not None, "error": _load_err}


@app.post("/v1/embeddings")
def embeddings(body: EmbedBody):
    raw = body.input
    if isinstance(raw, str):
        texts = [raw]
    elif isinstance(raw, list):
        texts = [t if isinstance(t, str) else str(t) for t in raw]
    else:
        return JSONResponse({"error": "input must be string or array of strings"},
                            status_code=400)
    if not texts:
        return JSONResponse({"error": "empty input"}, status_code=400)
    if body.mode == "query":
        texts = [QUERY_INSTRUCTION + t for t in texts]
    m = _get_model()
    if m is None:
        return JSONResponse({"error": f"model unavailable: {_load_err}"},
                            status_code=503)
    try:
        with _lock:
            vecs = list(m.embed(texts, batch_size=max(len(texts), 32)))
    except Exception as e:
        return JSONResponse({"error": f"embed failed: {type(e).__name__}: {e}"},
                            status_code=503)
    data = [{"object": "embedding", "index": i,
             "embedding": v.tolist() if hasattr(v, "tolist") else list(v)}
            for i, v in enumerate(vecs)]
    if len(data) != len(texts):
        return JSONResponse({"error": "embedding count mismatch"}, status_code=503)
    return {"object": "list", "data": data, "model": EMBED_MODEL,
            "usage": {"prompt_tokens": 0, "total_tokens": 0}}


@app.on_event("startup")
def _warm():
    threading.Thread(target=_get_model, daemon=True).start()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=EMBED_PORT, log_level="info")
