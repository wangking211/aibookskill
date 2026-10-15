import re
import time
import uuid
from pathlib import Path

try:
    from qcloud_cos import CosConfig, CosS3Client
except Exception:  # SDK 未安装时保持可导入，直传模式由 is_configured 关闭
    CosConfig = CosS3Client = None

MAX_UPLOAD_BYTES = 500 * 1024 * 1024


def is_configured(cfg):
    return bool(CosS3Client and cfg.get("cos_bucket") and cfg.get("cos_region")
                and cfg.get("cos_secret_id") and cfg.get("cos_secret_key"))


def _client(cfg):
    return CosS3Client(CosConfig(Region=cfg["cos_region"],
                                 SecretId=cfg["cos_secret_id"],
                                 SecretKey=cfg["cos_secret_key"],
                                 Scheme="https"))


def safe_name(filename):
    name = Path(filename or "book.pdf").name.replace("\x00", "").strip()
    return name or "book.pdf"


def object_key(cfg, filename):
    prefix = (cfg.get("cos_prefix") or "uploads/").strip("/") + "/"
    return f"{prefix}{time.strftime('%Y%m%d')}/{uuid.uuid4().hex[:12]}-{safe_name(filename)}"


def presign_put(cfg, key, expires=3600):
    return _client(cfg).get_presigned_url(
        Bucket=cfg["cos_bucket"], Key=key, Method="PUT", Expired=expires)


def presign_get(cfg, key, expires=7200):
    return _client(cfg).get_presigned_url(
        Bucket=cfg["cos_bucket"], Key=key, Method="GET", Expired=expires)


def head_size(cfg, key):
    try:
        h = _client(cfg).head_object(Bucket=cfg["cos_bucket"], Key=key)
    except Exception:
        return 0
    for k in ("Content-Length", "content-length"):
        if h and h.get(k):
            return int(h[k])
    return 0


def delete_object(cfg, key):
    try:
        _client(cfg).delete_object(Bucket=cfg["cos_bucket"], Key=key)
    except Exception:
        pass


def is_cos_path(pdf_path):
    return str(pdf_path).startswith("cos://")


def parse_cos_path(pdf_path):
    rest = str(pdf_path)[len("cos://"):]
    bucket, sep, key = rest.partition("/")
    if not sep or not bucket or not key:
        return None
    return bucket, key


def download_to(cfg, cos_path, dest, timeout=300):
    import requests
    parsed = parse_cos_path(cos_path)
    if not parsed:
        return None
    bucket, key = parsed
    url = presign_get(cfg, key)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    if chunk:
                        f.write(chunk)
        tmp.replace(dest)
    except Exception as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        print(f"cos download failed {key}: {e}")
        return None
    return dest
