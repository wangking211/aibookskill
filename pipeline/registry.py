import argparse
import contextlib
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import sys
import threading
import time
import unicodedata
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS books(
  id INTEGER PRIMARY KEY,
  space_id TEXT NOT NULL DEFAULT 'local',
  slug TEXT UNIQUE NOT NULL,
  title TEXT, author TEXT, publisher TEXT, isbn TEXT, pub_date TEXT,
  language TEXT, domain TEXT, description TEXT,
  pages INTEGER, cover_url TEXT, intro TEXT, download_url TEXT,
  source_file TEXT, source_sha256 TEXT,
  license_type TEXT NOT NULL DEFAULT 'personal',
  visibility TEXT NOT NULL DEFAULT 'private',
  skill_path TEXT, book_type TEXT, depth TEXT,
  skill_version INTEGER NOT NULL DEFAULT 1,
  chapter_count INTEGER, token_estimate INTEGER,
  scan_status TEXT, model_used TEXT,
  status TEXT NOT NULL DEFAULT 'published',
  uploaded_by INTEGER,
  created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS chapters(
  book_slug TEXT NOT NULL,
  no INTEGER NOT NULL,
  file_slug TEXT,
  title TEXT,
  path TEXT,
  token_estimate INTEGER,
  PRIMARY KEY(book_slug, no)
);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY,
  space_id TEXT NOT NULL DEFAULT 'local',
  kind TEXT NOT NULL DEFAULT 'convert',
  pdf_path TEXT NOT NULL,
  slug TEXT,
  state TEXT NOT NULL DEFAULT 'queued',
  detail TEXT, attempt INTEGER NOT NULL DEFAULT 0,
  error TEXT, log_path TEXT,
  model TEXT, user_id INTEGER,
  started_at INTEGER, finished_at INTEGER, elapsed_sec INTEGER,
  token_estimate INTEGER, token_actual INTEGER, usage_raw TEXT,
  free_fail INTEGER NOT NULL DEFAULT 0, not_before BIGINT,
  created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS interactions(
  id INTEGER PRIMARY KEY,
  space_id TEXT NOT NULL DEFAULT 'local',
  user_name TEXT NOT NULL DEFAULT 'local',
  slug TEXT, channel TEXT NOT NULL DEFAULT 'web',
  tool TEXT NOT NULL DEFAULT 'ask',
  question TEXT, result_count INTEGER,
  created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS faqs(
  space_id TEXT NOT NULL DEFAULT 'local',
  scope_slug TEXT NOT NULL DEFAULT '',
  norm_q TEXT NOT NULL,
  question TEXT,
  answer TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  PRIMARY KEY(space_id, scope_slug, norm_q)
);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  identifier TEXT UNIQUE NOT NULL,
  id_type TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  salt TEXT NOT NULL,
  display_name TEXT,
  role TEXT NOT NULL DEFAULT 'user',
  space_id TEXT UNIQUE NOT NULL,
  created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  last_login INTEGER
);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS api_tokens(
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  name TEXT,
  created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  last_used INTEGER
);
CREATE TABLE IF NOT EXISTS answer_cache(
  slug TEXT NOT NULL,
  norm_q TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'fast',
  question TEXT,
  answer TEXT NOT NULL,
  model TEXT,
  created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  PRIMARY KEY(slug, norm_q, mode)
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, created_at);
CREATE INDEX IF NOT EXISTS idx_inter_q ON interactions(space_id, slug, created_at);
CREATE TABLE IF NOT EXISTS translations(
  book_slug TEXT NOT NULL,
  lang TEXT NOT NULL,
  dir TEXT NOT NULL,
  model TEXT,
  created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  PRIMARY KEY(book_slug, lang)
);
CREATE TABLE IF NOT EXISTS index_files(
  book_slug TEXT NOT NULL,
  path TEXT NOT NULL,
  lang TEXT NOT NULL DEFAULT 'orig',
  tokens INTEGER NOT NULL DEFAULT 0,
  mtime INTEGER,
  file_size INTEGER,
  PRIMARY KEY(book_slug, path)
);
CREATE TABLE IF NOT EXISTS index_lines(
  book_slug TEXT NOT NULL,
  path TEXT NOT NULL,
  line INTEGER NOT NULL,
  text TEXT NOT NULL,
  PRIMARY KEY(book_slug, path, line)
);
CREATE INDEX IF NOT EXISTS idx_index_lines_book ON index_lines(book_slug, path);
CREATE TABLE IF NOT EXISTS index_embed(
  book_slug TEXT NOT NULL,
  path TEXT NOT NULL,
  line INTEGER NOT NULL,
  model TEXT,
  dim INTEGER,
  vec TEXT,
  PRIMARY KEY(book_slug, path, line)
);
"""

MIGRATIONS = [
    "ALTER TABLE interactions ADD COLUMN answer TEXT",
    "ALTER TABLE books ADD COLUMN uploaded_by BIGINT",
    "ALTER TABLE jobs ADD COLUMN user_id BIGINT",
    "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'",
    "ALTER TABLE jobs ADD COLUMN free_fail INTEGER DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN not_before BIGINT",
    "ALTER TABLE jobs ADD COLUMN worker_id TEXT",
    "ALTER TABLE jobs ADD COLUMN lease_until BIGINT",
    "ALTER TABLE interactions ADD COLUMN model TEXT",
    "ALTER TABLE interactions ADD COLUMN latency_ms INTEGER",
    "ALTER TABLE interactions ADD COLUMN tokens_in INTEGER",
    "ALTER TABLE interactions ADD COLUMN tokens_out INTEGER",
    "ALTER TABLE index_embed ADD COLUMN updated_at BIGINT",
]


def _pg(con):
    return getattr(con, "dialect", "sqlite") == "pg"


def _ddl_raced(e):
    state = getattr(getattr(e, "diag", None), "sqlstate", None)
    return state in ("42P07", "42710", "42701", "23505")


def _migrate(con):
    for stmt in MIGRATIONS:
        table = stmt.split()[2]
        col = stmt.split("ADD COLUMN ")[1].split()[0]
        if _pg(con):
            rows = con.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name=? AND table_schema=current_schema()", (table,))
            cols = {r["column_name"] for r in rows}
        else:
            cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            try:
                con.execute(stmt)
            except Exception as e:
                if not (_pg(con) and _ddl_raced(e)):
                    raise
    con.commit()


SCHEMA_PG = [
    """CREATE TABLE IF NOT EXISTS books(
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  space_id TEXT NOT NULL DEFAULT 'local',
  slug TEXT UNIQUE NOT NULL,
  title TEXT, author TEXT, publisher TEXT, isbn TEXT, pub_date TEXT,
  language TEXT, domain TEXT, description TEXT,
  pages INTEGER, cover_url TEXT, intro TEXT, download_url TEXT,
  source_file TEXT, source_sha256 TEXT,
  license_type TEXT NOT NULL DEFAULT 'personal',
  visibility TEXT NOT NULL DEFAULT 'private',
  skill_path TEXT, book_type TEXT, depth TEXT,
  skill_version INTEGER NOT NULL DEFAULT 1,
  chapter_count INTEGER, token_estimate INTEGER,
  scan_status TEXT, model_used TEXT,
  status TEXT NOT NULL DEFAULT 'published',
  uploaded_by BIGINT,
  created_at BIGINT NOT NULL DEFAULT floor(extract(epoch from now()))::bigint,
  updated_at BIGINT NOT NULL DEFAULT floor(extract(epoch from now()))::bigint
)""",
    """CREATE TABLE IF NOT EXISTS chapters(
  book_slug TEXT NOT NULL,
  no INTEGER NOT NULL,
  file_slug TEXT, title TEXT, path TEXT, token_estimate INTEGER,
  PRIMARY KEY(book_slug, no)
)""",
    """CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY,
  space_id TEXT NOT NULL DEFAULT 'local',
  kind TEXT NOT NULL DEFAULT 'convert',
  pdf_path TEXT NOT NULL,
  slug TEXT,
  state TEXT NOT NULL DEFAULT 'queued',
  detail TEXT, attempt INTEGER NOT NULL DEFAULT 0,
  error TEXT, log_path TEXT, model TEXT, user_id BIGINT,
  started_at BIGINT, finished_at BIGINT, elapsed_sec INTEGER,
  token_estimate INTEGER, token_actual INTEGER, usage_raw TEXT,
  created_at BIGINT NOT NULL DEFAULT floor(extract(epoch from now()))::bigint,
  updated_at BIGINT NOT NULL DEFAULT floor(extract(epoch from now()))::bigint
)""",
    """CREATE TABLE IF NOT EXISTS interactions(
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  space_id TEXT NOT NULL DEFAULT 'local',
  user_name TEXT NOT NULL DEFAULT 'local',
  slug TEXT, channel TEXT NOT NULL DEFAULT 'web',
  tool TEXT NOT NULL DEFAULT 'ask',
  question TEXT, result_count INTEGER, answer TEXT,
  created_at BIGINT NOT NULL DEFAULT floor(extract(epoch from now()))::bigint
)""",
    """CREATE TABLE IF NOT EXISTS faqs(
  space_id TEXT NOT NULL DEFAULT 'local',
  scope_slug TEXT NOT NULL DEFAULT '',
  norm_q TEXT NOT NULL,
  question TEXT, answer TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  updated_at BIGINT NOT NULL DEFAULT floor(extract(epoch from now()))::bigint,
  PRIMARY KEY(space_id, scope_slug, norm_q)
)""",
    """CREATE TABLE IF NOT EXISTS users(
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  identifier TEXT UNIQUE NOT NULL,
  id_type TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  salt TEXT NOT NULL,
  display_name TEXT,
  role TEXT NOT NULL DEFAULT 'user',
  space_id TEXT UNIQUE NOT NULL,
  created_at BIGINT NOT NULL DEFAULT floor(extract(epoch from now()))::bigint,
  last_login BIGINT
)""",
    """CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  user_id BIGINT NOT NULL,
  expires_at BIGINT NOT NULL,
  created_at BIGINT NOT NULL DEFAULT floor(extract(epoch from now()))::bigint
)""",
    """CREATE TABLE IF NOT EXISTS api_tokens(
  token TEXT PRIMARY KEY,
  user_id BIGINT NOT NULL,
  name TEXT,
  created_at BIGINT NOT NULL DEFAULT floor(extract(epoch from now()))::bigint,
  last_used BIGINT
)""",
    """CREATE TABLE IF NOT EXISTS answer_cache(
  slug TEXT NOT NULL,
  norm_q TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'fast',
  question TEXT, answer TEXT NOT NULL, model TEXT,
  created_at BIGINT NOT NULL DEFAULT floor(extract(epoch from now()))::bigint,
  PRIMARY KEY(slug, norm_q, mode)
)""",
    "CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_inter_q ON interactions(space_id, slug, created_at)",
    """CREATE TABLE IF NOT EXISTS translations(
  book_slug TEXT NOT NULL,
  lang TEXT NOT NULL,
  dir TEXT NOT NULL,
  model TEXT,
  created_at BIGINT NOT NULL DEFAULT floor(extract(epoch from now()))::bigint,
  PRIMARY KEY(book_slug, lang)
)""",
    """CREATE TABLE IF NOT EXISTS index_files(
  book_slug TEXT NOT NULL,
  path TEXT NOT NULL,
  lang TEXT NOT NULL DEFAULT 'orig',
  tokens INTEGER NOT NULL DEFAULT 0,
  mtime BIGINT,
  file_size BIGINT,
  PRIMARY KEY(book_slug, path)
)""",
    """CREATE TABLE IF NOT EXISTS index_lines(
  book_slug TEXT NOT NULL,
  path TEXT NOT NULL,
  line INTEGER NOT NULL,
  text TEXT NOT NULL,
  PRIMARY KEY(book_slug, path, line)
)""",
    "CREATE INDEX IF NOT EXISTS idx_index_lines_book ON index_lines(book_slug, path)",
    """CREATE TABLE IF NOT EXISTS index_embed(
  book_slug TEXT NOT NULL,
  path TEXT NOT NULL,
  line INTEGER NOT NULL,
  model TEXT,
  dim INTEGER,
  vec TEXT,
  PRIMARY KEY(book_slug, path, line)
)""",
]

_PG_SINGLETONS = {}
_PG_LOCK = threading.Lock()


def _qmark(sql):
    out = []
    in_str = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if in_str:
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    out.append("''")
                    i += 2
                    continue
                in_str = False
            out.append(ch)
        elif ch == "'":
            in_str = True
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


class _PgResult:
    def __init__(self, rows, rowcount):
        self._rows = rows
        self._i = 0
        self.rowcount = rowcount

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        r = self._rows[self._i]
        self._i += 1
        return r

    def fetchall(self):
        rows = self._rows[self._i:]
        self._i = len(self._rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class _PgConn:
    dialect = "pg"

    def __init__(self, dsn):
        self._dsn = dsn
        self._conn = None
        self._lock = threading.RLock()
        self._in_tx = False

    def _open(self):
        import psycopg
        from psycopg.rows import dict_row
        self._conn = psycopg.connect(self._dsn, autocommit=False,
                                     row_factory=dict_row, connect_timeout=5)

    def _get(self):
        if self._conn is None or self._conn.closed:
            try:
                self._open()
            except Exception:
                time.sleep(0.3)
                self._open()
        return self._conn

    def _reset(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _rollback_quiet(self):
        if self._conn is not None:
            try:
                self._conn.rollback()
            except Exception:
                pass

    def _call(self, fn):
        import psycopg
        with self._lock:
            try:
                return fn(self._get())
            except (psycopg.OperationalError, psycopg.InterfaceError):
                self._reset()
                raise
            except Exception:
                if not self._in_tx:
                    self._rollback_quiet()
                raise

    def execute(self, sql, params=None):
        q = _qmark(sql)
        p = tuple(params) if params else None

        def run(conn):
            cur = conn.execute(q, p)
            if cur.description is not None:
                rows = cur.fetchall()
                result = _PgResult(rows, len(rows))
            else:
                result = _PgResult([], cur.rowcount)
            if not self._in_tx:
                conn.commit()
            return result

        return self._call(run)

    def executemany(self, sql, seq):
        rows_in = list(seq)
        if not rows_in:
            return _PgResult([], 0)
        q = _qmark(sql)

        def run(conn):
            with conn.cursor() as cur:
                cur.executemany(q, rows_in)
                rc = cur.rowcount
            if not self._in_tx:
                conn.commit()
            return _PgResult([], rc if rc and rc > 0 else 0)

        return self._call(run)

    @contextlib.contextmanager
    def transaction(self):
        with self._lock:
            conn = self._get()
            self._in_tx = True
            try:
                yield self
                conn.commit()
            except BaseException:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                self._in_tx = False

    def commit(self):
        pass

    def rollback(self):
        with self._lock:
            if not self._in_tx:
                self._rollback_quiet()

    def close(self):
        with self._lock:
            self._reset()

    def ensure_schema(self):
        with self._lock:
            for stmt in SCHEMA_PG:
                try:
                    self.execute(stmt)
                except Exception as e:
                    if not _ddl_raced(e):
                        raise
            _migrate(self)


def _pg_connect(dsn):
    with _PG_LOCK:
        con = _PG_SINGLETONS.get(dsn)
        if con is None:
            con = _PgConn(dsn)
            con.ensure_schema()
            _PG_SINGLETONS[dsn] = con
        return con

SOURCE_KEYWORDS = [
    ("computer-principles", ["计算机原理"]),
    ("behavioral-economics", ["行为经济学"]),
    ("life-truth", ["生命的真相"]),
    ("english-framework", ["英语框架"]),
]


def resolve_db(value, base=None):
    s = str(value)
    if s.startswith(("postgres://", "postgresql://")):
        return s
    p = Path(s)
    if base is not None and not p.is_absolute():
        p = Path(base) / p
    return p


def connect(db_path):
    s = str(db_path)
    if s.startswith(("postgres://", "postgresql://")):
        return _pg_connect(s)
    p = Path(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def estimate_tokens(text):
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return int(cjk + (len(text) - cjk) / 4)


def parse_frontmatter(md):
    name = desc = ""
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end > 0:
            block = md[3:end]
            m = re.search(r"^name:\s*(.+)$", block, re.M)
            if m:
                name = m.group(1).strip().strip('"')
            m = re.search(r"^description:\s*[\"']?(.*?)[\"']?\s*$", block, re.M | re.S)
            if m:
                desc = m.group(1).strip()
    return name, desc


def first_heading(md):
    for line in md.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def norm_question(q):
    s = unicodedata.normalize("NFKC", q or "").lower()
    return re.sub(r"[\s\W_]+", "", s, flags=re.UNICODE)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def import_existing(con, skills_home):
    imported = 0
    for d in sorted(Path(skills_home).iterdir()):
        if not d.is_dir():
            continue
        skill_md = d / "SKILL.md"
        chapters_dir = d / "chapters"
        if not skill_md.exists() or not chapters_dir.is_dir():
            continue
        md = skill_md.read_text(encoding="utf-8", errors="replace")
        fm_name, fm_desc = parse_frontmatter(md)
        title = first_heading(md) or fm_name or d.name
        ch_files = sorted(chapters_dir.glob("*.md"))
        ch_rows = []
        tokens = estimate_tokens(md)
        for i, cf in enumerate(ch_files, 1):
            txt = cf.read_text(encoding="utf-8", errors="replace")
            tk = estimate_tokens(txt)
            tokens += tk
            m = re.match(r"ch(\d+)-(.*)", cf.stem)
            no = int(m.group(1)) if m else i
            ch_rows.append((d.name, no, m.group(2) if m else cf.stem,
                            first_heading(txt) or cf.stem,
                            str(cf).replace("\\", "/"), tk))
        now = int(time.time())
        con.execute(
            "INSERT INTO books(slug,title,description,skill_path,chapter_count,token_estimate,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET title=excluded.title,"
            " description=excluded.description, skill_path=excluded.skill_path,"
            " chapter_count=excluded.chapter_count, token_estimate=excluded.token_estimate, updated_at=excluded.updated_at",
            (d.name, title, fm_desc, str(d).replace("\\", "/"), len(ch_rows), tokens, now, now))
        con.execute("DELETE FROM chapters WHERE book_slug=?", (d.name,))
        con.executemany(
            "INSERT INTO chapters(book_slug,no,file_slug,title,path,token_estimate) VALUES(?,?,?,?,?,?)", ch_rows)
        imported += 1
    con.commit()
    return imported


def link_sources(con, source_dir):
    linked = 0
    pdfs = list(Path(source_dir).glob("*.pdf")) if Path(source_dir).is_dir() else []
    for slug, kws in SOURCE_KEYWORDS:
        row = con.execute("SELECT source_sha256 FROM books WHERE slug=?", (slug,)).fetchone()
        if row and row["source_sha256"]:
            continue
        for p in pdfs:
            if any(k in p.name for k in kws):
                con.execute("UPDATE books SET source_file=?, source_sha256=?, updated_at=? WHERE slug=?",
                            (str(p), sha256_file(p), int(time.time()), slug))
                linked += 1
                break
    con.commit()
    return linked


def register_user(con, identifier, password, display_name=""):
    identifier = (identifier or "").strip()
    if re.fullmatch(r"1[3-9]\d{9}", identifier):
        id_type = "phone"
    elif re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", identifier):
        id_type = "email"
    else:
        raise ValueError("请输入有效的邮箱或手机号")
    if len(password or "") < 6:
        raise ValueError("密码至少 6 位")
    if con.execute("SELECT 1 FROM users WHERE identifier=?", (identifier,)).fetchone():
        raise ValueError("该账号已注册")
    salt = secrets.token_hex(16)
    pw_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200000).hex()
    space = "u" + secrets.token_hex(6)
    insert_sql = ("INSERT INTO users(identifier,id_type,password_hash,salt,display_name,space_id)"
                  " VALUES(?,?,?,?,?,?)")
    values = (identifier, id_type, pw_hash, salt,
              (display_name or "").strip() or identifier, space)
    try:
        if _pg(con):
            uid = con.execute(insert_sql + " RETURNING id", values).fetchone()["id"]
        else:
            cur = con.execute(insert_sql, values)
            con.commit()
            uid = cur.lastrowid
    except Exception as e:
        if "unique" in str(e).lower() or "duplicate key" in str(e).lower():
            raise ValueError("该账号已注册") from e
        raise
    return user_by_id(con, uid)


def user_by_id(con, uid):
    r = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(r) if r else None


def auth_user(con, identifier, password):
    r = con.execute("SELECT * FROM users WHERE identifier=?",
                    ((identifier or "").strip(),)).fetchone()
    if not r:
        return None
    pw_hash = hashlib.pbkdf2_hmac("sha256", (password or "").encode(),
                                  r["salt"].encode(), 200000).hex()
    if not hmac.compare_digest(pw_hash, r["password_hash"]):
        return None
    con.execute("UPDATE users SET last_login=? WHERE id=?", (int(time.time()), r["id"]))
    con.commit()
    return dict(r)


def create_session(con, user_id, days=30):
    token = secrets.token_hex(24)
    con.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)",
                (token, user_id, int(time.time()) + days * 86400))
    con.commit()
    return token


def user_by_token(con, token):
    if not token:
        return None
    r = con.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id"
        " WHERE s.token=? AND s.expires_at>?", (token, int(time.time()))).fetchone()
    return dict(r) if r else None


def destroy_session(con, token):
    if token:
        con.execute("DELETE FROM sessions WHERE token=?", (token,))
        con.commit()


def create_api_token(con, user_id, name=None):
    token = "ak_" + secrets.token_hex(24)
    con.execute("INSERT INTO api_tokens(token,user_id,name) VALUES(?,?,?)",
                (token, user_id, name))
    con.commit()
    return token


def user_by_api_token(con, token):
    if not token or not token.startswith("ak_"):
        return None
    r = con.execute(
        "SELECT u.* FROM api_tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",
        (token,)).fetchone()
    if r:
        con.execute("UPDATE api_tokens SET last_used=? WHERE token=?",
                    (int(time.time()), token))
        con.commit()
    return dict(r) if r else None


def list_api_tokens(con, user_id):
    rows = con.execute(
        "SELECT token,name,created_at,last_used FROM api_tokens WHERE user_id=?"
        " ORDER BY created_at DESC", (user_id,)).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        r["token_masked"] = r["token"][:8] + "…" + r["token"][-4:]
        out.append(r)
    return out


def delete_api_token(con, user_id, token):
    cur = con.execute("DELETE FROM api_tokens WHERE user_id=? AND token=?", (user_id, token))
    con.commit()
    return cur.rowcount > 0


def find_book(con, slug):
    return con.execute("SELECT * FROM books WHERE slug=?", (slug,)).fetchone()


def upsert_book(con, meta):
    keys = ["space_id", "slug", "title", "author", "publisher", "isbn", "pub_date", "language",
            "domain", "description", "pages", "cover_url", "intro", "download_url",
            "source_file", "source_sha256", "license_type", "visibility", "skill_path",
            "book_type", "depth", "skill_version", "chapter_count", "token_estimate",
            "scan_status", "model_used", "status", "uploaded_by"]
    cols = [k for k in keys if k in meta and meta[k] not in (None, "")]
    if "slug" not in cols:
        raise ValueError("slug required")
    now = int(time.time())
    vals = [meta[k] for k in cols] + [now]
    cols_with = cols + ["updated_at"]
    sets = ",".join(f"{k}=excluded.{k}" for k in cols_with if k != "slug")
    sql = (f"INSERT INTO books({','.join(cols_with)})"
           f" VALUES({','.join('?' * len(vals))})"
           f" ON CONFLICT(slug) DO UPDATE SET {sets}")
    con.execute(sql, vals)
    con.commit()


def upsert_chapters(con, slug, rows):
    insert_sql = ("INSERT INTO chapters(book_slug,no,file_slug,title,path,token_estimate)"
                  " VALUES(?,?,?,?,?,?)")
    if _pg(con):
        with con.transaction():
            con.execute("DELETE FROM chapters WHERE book_slug=?", (slug,))
            con.executemany(insert_sql, rows)
    else:
        con.execute("DELETE FROM chapters WHERE book_slug=?", (slug,))
        con.executemany(insert_sql, rows)
    con.commit()


def find_books_by_sha(con, sha):
    sha = (sha or "").strip().lower()
    if not sha:
        return []
    rows = con.execute(
        "SELECT * FROM books WHERE source_sha256=? AND status='published'"
        " ORDER BY updated_at DESC", (sha,)).fetchall()
    return [dict(r) for r in rows]


def copy_book(con, src, new_slug, space_id, uploaded_by=None):
    src = dict(src)
    keys = ["title", "author", "publisher", "isbn", "pub_date", "language", "domain",
            "description", "pages", "cover_url", "intro", "download_url",
            "source_file", "source_sha256", "license_type", "visibility", "skill_path",
            "book_type", "depth", "skill_version", "chapter_count", "token_estimate",
            "scan_status", "model_used", "status", "uploaded_by"]
    meta = {"space_id": space_id, "slug": new_slug}
    meta.update({k: src[k] for k in keys if src.get(k) not in (None, "")})
    if uploaded_by is not None:
        meta["uploaded_by"] = uploaded_by
    upsert_book(con, meta)
    rows = con.execute(
        "SELECT no,file_slug,title,path,token_estimate FROM chapters"
        " WHERE book_slug=? ORDER BY no", (src["slug"],)).fetchall()
    if rows:
        vals = [(r["no"], r["file_slug"], r["title"], r["path"], r["token_estimate"])
                for r in rows]
        upsert_chapters(con, new_slug, [(new_slug,) + v for v in vals])
    return new_slug


def chapter_rows_from_disk(slug, skill_dir):
    rows = []
    ch_dir = Path(skill_dir) / "chapters"
    files = sorted(ch_dir.glob("*.md")) if ch_dir.is_dir() else []
    for i, cf in enumerate(files, 1):
        txt = cf.read_text(encoding="utf-8", errors="replace")
        m = re.match(r"ch(\d+)-(.*)", cf.stem)
        no = int(m.group(1)) if m else i
        rows.append((slug, no, m.group(2) if m else cf.stem,
                     first_heading(txt) or cf.stem, str(cf).replace("\\", "/"),
                     estimate_tokens(txt)))
    return rows


def register_job(con, job_id, space_id, kind, pdf_path, slug, model=None):
    con.execute(
        "INSERT INTO jobs(id,space_id,kind,pdf_path,slug,state,model) VALUES(?,?,?,?,?,'queued',?)",
        (job_id, space_id, kind, str(pdf_path), slug, model))
    con.commit()


def claim_next_job(con, worker_id=None, lease_sec=300):
    now = int(time.time())
    row = con.execute(
        "SELECT * FROM jobs WHERE state='queued'"
        " AND (not_before IS NULL OR not_before<=?)"
        " AND (slug IS NULL OR slug='' OR NOT EXISTS ("
        "  SELECT 1 FROM jobs j2 WHERE j2.slug=jobs.slug"
        "  AND j2.state IN ('queued','running')"
        "  AND (j2.created_at<jobs.created_at"
        "       OR (j2.created_at=jobs.created_at AND j2.id<jobs.id))))"
        " ORDER BY created_at LIMIT 1", (now,)).fetchone()
    if not row:
        return None
    cur = con.execute(
        "UPDATE jobs SET state='running', started_at=?, updated_at=?,"
        " worker_id=?, lease_until=? WHERE id=? AND state='queued'",
        (now, now, worker_id, now + int(lease_sec), row["id"]))
    con.commit()
    if cur.rowcount == 1:
        return con.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
    return None


def heartbeat_job(con, job_id, worker_id=None, lease_sec=300):
    """续租：确认任务仍由本 worker 持有。返回是否仍属于自己。"""
    now = int(time.time())
    if worker_id:
        cur = con.execute(
            "UPDATE jobs SET lease_until=?, updated_at=?"
            " WHERE id=? AND state='running' AND worker_id=?",
            (now + int(lease_sec), now, job_id, worker_id))
    else:
        cur = con.execute(
            "UPDATE jobs SET lease_until=?, updated_at=?"
            " WHERE id=? AND state='running'",
            (now + int(lease_sec), now, job_id))
    con.commit()
    return cur.rowcount == 1


# 无租约的 running（部署本版之前的遗留/崩溃残留）超过该秒数视为僵尸
_LEGACY_STALE_SEC = 7200


def reap_stale_jobs(con, grace_sec=60, max_attempts=5):
    """回收租约过期的僵尸 running 任务（worker 崩溃/机器重启），放回队列。

    超过 max_attempts 仍反复崩溃则判失败，避免无限重跑。
    返回回收条数。
    """
    now = int(time.time())
    stale = con.execute(
        "SELECT id, attempt FROM jobs WHERE state='running'"
        " AND ((lease_until IS NOT NULL AND lease_until<?)"
        "      OR (lease_until IS NULL AND COALESCE(started_at,0)<?))",
        (now, now - _LEGACY_STALE_SEC)).fetchall()
    n = 0
    for r in stale:
        r = dict(r)
        if (r["attempt"] or 0) >= max_attempts:
            con.execute(
                "UPDATE jobs SET state='failed', worker_id=NULL, lease_until=NULL,"
                " error=?, finished_at=?, updated_at=? WHERE id=? AND state='running'",
                ("reclaimed too many times (worker crashed repeatedly)", now, now, r["id"]))
        else:
            con.execute(
                "UPDATE jobs SET state='queued', worker_id=NULL, lease_until=NULL,"
                " not_before=?, attempt=attempt+1, detail=?, updated_at=?"
                " WHERE id=? AND state='running'",
                (now + int(grace_sec), "reclaimed: worker lease expired", now, r["id"]))
        n += 1
    con.commit()
    if n:
        print(f"[reaper] reclaimed {n} stale job(s)", flush=True)
    return n


def update_job(con, job_id, **fields):
    if not fields:
        return
    fields["updated_at"] = int(time.time())
    sets = ",".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))
    con.commit()


def put_translation(con, slug, lang, dir_, model=""):
    con.execute(
        "INSERT INTO translations(book_slug,lang,dir,model,created_at) VALUES(?,?,?,?,?)"
        " ON CONFLICT(book_slug,lang) DO UPDATE SET dir=excluded.dir,"
        " model=excluded.model, created_at=excluded.created_at",
        (slug, lang, str(dir_), model, int(time.time())))
    con.commit()


def get_translations(con, slug):
    return [dict(r) for r in con.execute(
        "SELECT lang,dir,model,created_at FROM translations WHERE book_slug=? ORDER BY lang",
        (slug,))]


def get_translation(con, slug, lang):
    return con.execute("SELECT * FROM translations WHERE book_slug=? AND lang=?",
                       (slug, lang)).fetchone()


def log_interaction(con, space_id, user_name, slug, channel, tool, question, result_count,
                    answer=None, model=None, latency_ms=None, tokens_in=None, tokens_out=None):
    con.execute(
        "INSERT INTO interactions(space_id,user_name,slug,channel,tool,question,result_count,"
        "answer,model,latency_ms,tokens_in,tokens_out)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (space_id, user_name, slug, channel, tool, question, result_count, answer,
         model, latency_ms, tokens_in, tokens_out))
    con.commit()


def admin_stats(con, days=7):
    """最近 N 天的问答用量统计：总量、按天、按模型成功率与延迟。"""
    since = int(time.time()) - days * 86400
    rows = con.execute(
        "SELECT tool, model, answer, latency_ms, created_at FROM interactions"
        " WHERE created_at>=? AND tool IN ('ask','ask_library')",
        (since,)).fetchall()
    total = len(rows)
    ok = sum(1 for r in rows if r["answer"])
    by_day = {}
    by_model = {}
    for r in rows:
        day = time.strftime("%Y-%m-%d", time.localtime(int(r["created_at"] or 0)))
        d = by_day.setdefault(day, {"count": 0, "ok": 0, "lat": []})
        d["count"] += 1
        if r["answer"]:
            d["ok"] += 1
        if r["latency_ms"]:
            d["lat"].append(int(r["latency_ms"]))
        m = r["model"] or ""
        if m:
            mm = by_model.setdefault(m, {"count": 0, "ok": 0, "lat": []})
            mm["count"] += 1
            if r["answer"]:
                mm["ok"] += 1
            if r["latency_ms"]:
                mm["lat"].append(int(r["latency_ms"]))

    def _pct(vals, p):
        if not vals:
            return None
        vals = sorted(vals)
        k = max(0, min(len(vals) - 1, int(len(vals) * p) - 1))
        return vals[k]

    days_out = []
    for day in sorted(by_day):
        d = by_day[day]
        days_out.append({
            "day": day, "count": d["count"], "ok": d["ok"],
            "ok_rate": round(d["ok"] / d["count"], 3) if d["count"] else 0,
            "p50_ms": _pct(d["lat"], 0.5), "p95_ms": _pct(d["lat"], 0.95),
        })
    models_out = []
    for m in sorted(by_model, key=lambda x: -by_model[x]["count"]):
        mm = by_model[m]
        models_out.append({
            "model": m, "count": mm["count"], "ok": mm["ok"],
            "ok_rate": round(mm["ok"] / mm["count"], 3) if mm["count"] else 0,
            "p50_ms": _pct(mm["lat"], 0.5), "p95_ms": _pct(mm["lat"], 0.95),
        })
    return {
        "days": days, "total": total, "ok": ok,
        "ok_rate": round(ok / total, 3) if total else 0,
        "by_day": days_out, "by_model": models_out,
    }


def faq_groups(con, scope_slug=None, limit=50):
    sql = ("SELECT slug, question, answer FROM interactions"
           " WHERE tool IN ('ask','search','pack_context','ask_library')"
           " AND question IS NOT NULL AND question<>''")
    args = []
    if scope_slug:
        sql += " AND slug=?"
        args.append(scope_slug)
    groups = {}
    for r in con.execute(sql, args):
        nq = norm_question(r["question"])
        if not nq:
            continue
        key = (r["slug"] or "", nq)
        g = groups.setdefault(key, {"count": 0, "question": r["question"]})
        g["count"] += 1
        if r["answer"] and not g.get("answer"):
            g["answer"] = r["answer"]
    for m in con.execute("SELECT scope_slug, norm_q, question, answer FROM faqs"):
        key = (m["scope_slug"] or "", m["norm_q"])
        g = groups.setdefault(key, {"count": 0, "question": m["question"] or ""})
        if m["answer"]:
            g["answer"] = m["answer"]
    out = [{"scope_slug": k[0], "norm_q": k[1], "question": v["question"],
            "count": v["count"], "answer": v.get("answer"),
            "answered": bool(v.get("answer"))}
           for k, v in groups.items()]
    out.sort(key=lambda x: (-int(bool(x["answer"])), -x["count"]))
    return out[:limit]


def put_cached_answer(con, slug, norm_q, mode, question, answer, model=""):
    if not norm_q or not answer:
        return
    con.execute(
        "INSERT INTO answer_cache(slug,norm_q,mode,question,answer,model,created_at)"
        " VALUES(?,?,?,?,?,?,?) ON CONFLICT(slug,norm_q,mode)"
        " DO UPDATE SET question=excluded.question, answer=excluded.answer,"
        " model=excluded.model, created_at=excluded.created_at",
        (slug, norm_q, mode, question, answer, model, int(time.time())))
    con.commit()


def get_cached_answer(con, slug, norm_q, mode="fast", ttl=7 * 86400):
    if not norm_q:
        return None
    r = con.execute("SELECT answer, created_at FROM answer_cache"
                    " WHERE slug=? AND norm_q=? AND mode=?",
                    (slug, norm_q, mode)).fetchone()
    if not r:
        return None
    if int(time.time()) - (r["created_at"] or 0) > ttl:
        con.execute("DELETE FROM answer_cache WHERE slug=? AND norm_q=? AND mode=?",
                    (slug, norm_q, mode))
        con.commit()
        return None
    return r["answer"]


def hot_questions(con, scope_slug=None, limit=8):
    sql = ("SELECT question FROM interactions"
           " WHERE question IS NOT NULL AND question<>''")
    args = []
    if scope_slug:
        sql += " AND slug=?"
        args.append(scope_slug)
    groups = {}
    for r in con.execute(sql, args):
        nq = norm_question(r["question"])
        if not nq:
            continue
        g = groups.setdefault(nq, {"question": r["question"], "count": 0})
        g["count"] += 1
    out = sorted(groups.values(), key=lambda x: -x["count"])
    return out[:limit]


def save_faq(con, scope_slug, question, answer):
    nq = norm_question(question)
    if not nq:
        raise ValueError("empty question")
    con.execute(
        "INSERT INTO faqs(space_id,scope_slug,norm_q,question,answer,source,updated_at)"
        " VALUES('local',?,?,?,?,?,?) ON CONFLICT(space_id,scope_slug,norm_q)"
        " DO UPDATE SET question=excluded.question, answer=excluded.answer, updated_at=excluded.updated_at",
        (scope_slug or "", nq, question, answer, "manual", int(time.time())))
    con.commit()


STOPWORDS = "的了是怎么为何什吗呢啊吧在有和跟与从对于后前后要把被就都还也很更最我会能可需要想让给对使通过关于据按照因由于个一二三四五六七八九十大中小上下里外"


CJK = r"[\u4e00-\u9fff]"
# 检索词切分：空白、中英标点、引号与各类括号都算分隔符
SPLIT_RE = re.compile(
    r"""[\s,，。；;？?！!、：:·…~～\-—－_/\\|"'`()（）\[\]{}【】「」『』《》〈〉“”‘’]+""")


def _terms(query):
    parts = [t for t in SPLIT_RE.split(query.strip()) if t]
    out = []
    for t in parts:
        if re.fullmatch(CJK + "+", t) and all(ch in STOPWORDS for ch in t):
            continue
        if len(t) > 4 and re.fullmatch(CJK + "+", t):
            pieces = [p for p in re.split(f"[{STOPWORDS}]+", t) if len(p) >= 2]
            out.extend(pieces or [t])
        else:
            out.append(t)
    seen = set()
    return [t for t in out if not (t in seen or seen.add(t))]


def _search_terms(root, terms, limit_files, max_lines):
    results = []
    for md in Path(root).rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        total = sum(text.count(t) for t in terms)
        if total == 0:
            continue
        lines = []
        for i, line in enumerate(text.splitlines(), 1):
            if any(t in line for t in terms):
                lines.append({"line": i, "snippet": line.strip()[:180]})
                if len(lines) >= max_lines:
                    break
        rel = str(md.relative_to(root)).replace("\\", "/")
        results.append({"path": rel, "score": total, "lines": lines,
                        "tokens": estimate_tokens(text)})
    results.sort(key=lambda x: -x["score"])
    return results[:limit_files]


def search_files(root, query, limit_files=6, max_lines=3):
    terms = _terms(query)
    if not terms:
        return []
    res = _search_terms(root, terms, limit_files, max_lines)
    if res:
        return res
    grams = []
    for t in terms:
        if len(t) >= 4 and re.fullmatch(r"[\u4e00-\u9fff]+", t):
            grams += [t[i:i + 2] for i in range(len(t) - 1)]
    grams = list(dict.fromkeys(g for g in grams if g not in terms))
    if not grams:
        return []
    return _search_terms(root, grams, limit_files, max_lines)


# ---------------------------------------------------------------- 内容索引
# 把书籍 skill 目录的 *.md 逐行落库（index_files / index_lines），
# 跨书检索不再逐个读盘。sqlite 与 PG 通用（LIKE 参数化，? 由 _qmark 转换）。

MAX_INDEX_LINE_CHARS = 400
PER_TERM_LIMIT = 2000


def _index_lang(rel):
    return "zh" if rel.startswith("chapters_zh/") else "orig"


def _disk_sig(skill_dir):
    """skill 目录内所有 *.md 的 (相对路径, mtime, size) 指纹，用于判断索引是否过期。"""
    root = Path(skill_dir)
    sig = []
    if root.is_dir():
        for md in root.rglob("*.md"):
            if "__pycache__" in md.parts:
                continue
            try:
                st = md.stat()
            except OSError:
                continue
            sig.append((md.relative_to(root).as_posix(),
                        int(st.st_mtime), int(st.st_size)))
    return sorted(sig)


def index_status(con, slug):
    rows = con.execute(
        "SELECT path, mtime, file_size FROM index_files WHERE book_slug=?",
        (slug,)).fetchall()
    return sorted((r["path"], int(r["mtime"] or 0), int(r["file_size"] or 0))
                  for r in rows)


def _index_write(con, slug, files, lines):
    del_sql = ["DELETE FROM index_lines WHERE book_slug=?",
               "DELETE FROM index_files WHERE book_slug=?"]
    ins_f = ("INSERT INTO index_files(book_slug,path,lang,tokens,mtime,file_size)"
             " VALUES(?,?,?,?,?,?)")
    ins_l = "INSERT INTO index_lines(book_slug,path,line,text) VALUES(?,?,?,?)"
    if _pg(con):
        with con.transaction():
            for s in del_sql:
                con.execute(s, (slug,))
            if files:
                con.executemany(ins_f, files)
            if lines:
                con.executemany(ins_l, lines)
        return
    for s in del_sql:
        con.execute(s, (slug,))
    if files:
        con.executemany(ins_f, files)
    if lines:
        con.executemany(ins_l, lines)
    con.commit()


def reindex_book(con, slug, skill_dir, sig=None, max_line=MAX_INDEX_LINE_CHARS):
    """全量重建某书的行级索引（删光重插）。返回 {files, lines} 统计。"""
    root = Path(skill_dir)
    sig = _disk_sig(skill_dir) if sig is None else sig
    files, lines = [], []
    for rel, mtime, size in sig:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files.append((slug, rel, _index_lang(rel), estimate_tokens(text),
                      mtime, size))
        for i, ln in enumerate(text.splitlines(), 1):
            s = ln.strip()
            if s:
                lines.append((slug, rel, i, s[:max_line]))
    _index_write(con, slug, files, lines)
    return {"files": len(files), "lines": len(lines)}


def reindex_book_incremental(con, slug, skill_dir, sig=None,
                             max_line=MAX_INDEX_LINE_CHARS):
    """按文件 diff 增量更新索引：只重抽新增/变更文件的行，删除消失文件的行，
    未变文件不动。返回 {files, lines, added, changed, removed, unchanged} 统计。"""
    root = Path(skill_dir)
    sig = _disk_sig(skill_dir) if sig is None else sig
    old_rows = con.execute(
        "SELECT path, mtime, file_size FROM index_files WHERE book_slug=?",
        (slug,)).fetchall()
    old = {r["path"]: (int(r["mtime"] or 0), int(r["file_size"] or 0))
           for r in old_rows}
    new = {rel: (mtime, size) for rel, mtime, size in sig}

    added = [rel for rel in new if rel not in old]
    changed = [rel for rel in new
               if rel in old and old[rel] != new[rel]]
    removed = [rel for rel in old if rel not in new]
    unchanged = [rel for rel in new
                 if rel in old and old[rel] == new[rel]]

    if not added and not changed and not removed:
        return {"files": len(unchanged), "lines": 0,
                "added": 0, "changed": 0, "removed": 0,
                "unchanged": len(unchanged)}

    files_ins, lines_ins = [], []
    for rel in added + changed:
        mtime, size = new[rel]
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files_ins.append((slug, rel, _index_lang(rel), estimate_tokens(text),
                          mtime, size))
        for i, ln in enumerate(text.splitlines(), 1):
            s = ln.strip()
            if s:
                lines_ins.append((slug, rel, i, s[:max_line]))

    ins_f = ("INSERT INTO index_files(book_slug,path,lang,tokens,mtime,file_size)"
             " VALUES(?,?,?,?,?,?)")
    ins_l = "INSERT INTO index_lines(book_slug,path,line,text) VALUES(?,?,?,?)"
    del_paths = removed + changed

    def _apply():
        for rel in del_paths:
            con.execute(
                "DELETE FROM index_lines WHERE book_slug=? AND path=?",
                (slug, rel))
            con.execute(
                "DELETE FROM index_files WHERE book_slug=? AND path=?",
                (slug, rel))
        if files_ins:
            con.executemany(ins_f, files_ins)
        if lines_ins:
            con.executemany(ins_l, lines_ins)

    if _pg(con):
        with con.transaction():
            _apply()
    else:
        _apply()
        con.commit()

    return {"files": len(files_ins), "lines": len(lines_ins),
            "added": len(added), "changed": len(changed),
            "removed": len(removed), "unchanged": len(unchanged)}


def ensure_index(con, slug, skill_dir, force=False):
    """索引缺失或与磁盘不一致时增量重建；未变化返回 None。force=True 全量重建。"""
    sig = _disk_sig(skill_dir)
    if not sig:
        return None
    if not force and index_status(con, slug) == sig:
        return None
    if force:
        return reindex_book(con, slug, skill_dir, sig=sig)
    return reindex_book_incremental(con, slug, skill_dir, sig=sig)


def _cap_per_book(items, per_book):
    seen, out = {}, []
    for it in items:
        s = it.get("book_slug") or ""
        if seen.get(s, 0) >= per_book:
            continue
        seen[s] = seen.get(s, 0) + 1
        out.append(it)
    return out


def _index_terms(con, slugs, terms, limit_files, max_lines, per_book=0):
    """每个检索词单独查（OR 语义，与逐文件扫描一致），再在内存里合并计分。"""
    ph = ",".join("?" * len(slugs))
    agg = {}
    for t in terms:
        rows = con.execute(
            f"SELECT book_slug, path, line, text FROM index_lines"
            f" WHERE book_slug IN ({ph}) AND text LIKE ?"
            f" ORDER BY book_slug, path, line LIMIT {PER_TERM_LIMIT}",
            list(slugs) + ["%" + t + "%"]).fetchall()
        for r in rows:
            key = (r["book_slug"], r["path"])
            a = agg.get(key)
            if a is None:
                a = agg[key] = {"score": 0, "lines": {}}
            a["score"] += r["text"].count(t)
            a["lines"].setdefault(r["line"], r["text"][:180])
    if not agg:
        return []
    tok = {}
    for r in con.execute(
            f"SELECT book_slug, path, tokens FROM index_files"
            f" WHERE book_slug IN ({ph})", slugs):
        tok[(r["book_slug"], r["path"])] = r["tokens"] or 0
    out = []
    for k, v in agg.items():
        lines = [{"line": n, "snippet": s}
                 for n, s in sorted(v["lines"].items())[:max_lines]]
        out.append({"book_slug": k[0], "path": k[1], "score": v["score"],
                    "lines": lines, "tokens": tok.get(k, 0)})
    out.sort(key=lambda x: (-x["score"], x["book_slug"], x["path"]))
    if per_book:
        out = _cap_per_book(out, per_book)
    return out[:limit_files]


def search_index(con, slugs, query, limit_files=6, max_lines=3, per_book=0):
    """在已建索引的书籍上检索（slugs 为 slug 列表）。返回与 search_files 同构的结果。"""
    if isinstance(slugs, str):
        slugs = [slugs]
    slugs = [s for s in slugs if s]
    terms = _terms(query)
    if not slugs or not terms:
        return []
    res = _index_terms(con, slugs, terms, limit_files, max_lines, per_book)
    if res:
        return res
    grams = []
    for t in terms:
        if len(t) >= 4 and re.fullmatch(r"[\u4e00-\u9fff]+", t):
            grams += [t[i:i + 2] for i in range(len(t) - 1)]
    grams = list(dict.fromkeys(g for g in grams if g not in terms))
    if not grams:
        return []
    return _index_terms(con, slugs, grams, limit_files, max_lines, per_book)


def search_auto(con, slug, root, query, limit_files=6, max_lines=3):
    """单书检索：优先走索引，索引不可用则回退到逐文件扫描。"""
    root = Path(root)
    try:
        if not index_status(con, slug):
            ensure_index(con, slug, root)
        if index_status(con, slug):
            return search_index(con, [slug], query, limit_files, max_lines)
    except Exception:
        pass
    return search_files(root, query, limit_files, max_lines)


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def _embed_db_path(cfg):
    """本地向量库路径。向量是派生数据：每台机器各存一份 embed.db（float32 BLOB），
    避免跨机拉取 PG 中 index_embed（~100MB，低速链路下冷加载需 15 分钟+）。"""
    v = str((cfg or {}).get("embed_db") or "pipeline/embed.db")
    if v.startswith(("postgres://", "postgresql://")):
        return None
    p = Path(v)
    return p if p.is_absolute() else Path(__file__).parent.parent / p


_EMBED_SCHEMA = """CREATE TABLE IF NOT EXISTS index_embed(
  book_slug TEXT NOT NULL,
  path TEXT NOT NULL,
  line INTEGER NOT NULL,
  model TEXT,
  dim INTEGER,
  vec TEXT,
  PRIMARY KEY(book_slug, path, line)
)"""


def _embed_con(cfg):
    """打开本地向量库（每次新建连接；sqlite WAL 支持并发读写）。失败返回 None。"""
    p = _embed_db_path(cfg)
    if p is None:
        return None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(p), timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        con.execute(_EMBED_SCHEMA)
        cols = {r[1] for r in con.execute("PRAGMA table_info(index_embed)")}
        if "updated_at" not in cols:
            con.execute("ALTER TABLE index_embed ADD COLUMN updated_at BIGINT")
            con.commit()
        return con
    except Exception:
        return None


def embed_book(con, slug, cfg, batch=64):
    """为某书全部已索引行生成嵌入并写入**本地** embed.db。返回嵌入行数。
    index_lines 从 con（PG/共享）读，向量写本地：WAN 只承载文本。"""
    import struct

    import llm
    base, key, model = llm._embedding_endpoint(cfg)
    if not base:
        return 0
    econ = _embed_con(cfg)
    if econ is None:
        return 0
    try:
        rows = con.execute(
            "SELECT path, line, text FROM index_lines WHERE book_slug=?",
            (slug,)).fetchall()
        if not rows:
            return 0
        items = [(r["path"], r["line"], r["text"]) for r in rows]
        written = 0
        for i in range(0, len(items), batch):
            chunk = items[i:i + batch]
            vecs = llm.embed_texts(cfg, [t for _, _, t in chunk], timeout=60,
                                   mode="doc")
            if not vecs:
                continue
            dim = len(vecs[0])
            now = int(time.time())
            pairs = [(slug, p, ln, model, dim,
                      struct.pack(f"<{len(v)}f", *v), now)
                     for (p, ln, _), v in zip(chunk, vecs)]
            _embed_write(econ, pairs)
            written += len(pairs)
        return written
    finally:
        econ.close()


def _embed_write(con, pairs):
    ins = ("INSERT INTO index_embed(book_slug,path,line,model,dim,vec,updated_at)"
           " VALUES(?,?,?,?,?,?,?) ON CONFLICT(book_slug,path,line)"
           " DO UPDATE SET model=excluded.model, dim=excluded.dim,"
           " vec=excluded.vec, updated_at=excluded.updated_at")
    if _pg(con):
        with con.transaction():
            con.executemany(ins, pairs)
        return
    con.executemany(ins, pairs)
    con.commit()


try:
    import numpy as _np
except ImportError:  # 无 numpy（如本地未装 fastembed）时回退纯 Python
    _np = None

_VEC_CACHE = {}
_VEC_CACHE_LOCK = threading.Lock()


def _embed_fingerprint(con, slugs):
    """向量缓存指纹：行数 + 模型 + 最近写入时间（--embed 重跑即失效）。"""
    ph = ",".join("?" * len(slugs))
    r = con.execute(
        f"SELECT COUNT(*) AS n, MAX(model) AS m, MAX(updated_at) AS u"
        f" FROM index_embed WHERE book_slug IN ({ph})", list(slugs)).fetchone()
    return (r["n"], r["m"], r["u"])


def _vec_cache_load(con, slugs):
    """加载本地向量库并构建 numpy 矩阵（BLOB float32 零解析；兼容旧 TEXT json）。
    无 numpy 或异常返回 None。"""
    if _np is None:
        return None
    ph = ",".join("?" * len(slugs))
    rows = con.execute(
        f"SELECT book_slug, path, line, vec FROM index_embed"
        f" WHERE book_slug IN ({ph})", list(slugs)).fetchall()
    keys, lines, vec_rows = [], [], []
    for r in rows:
        raw = r["vec"]
        if isinstance(raw, (bytes, bytearray, memoryview)):
            arr = _np.frombuffer(raw, dtype=_np.float32)
        else:
            try:
                arr = _np.asarray(json.loads(raw), dtype=_np.float32)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        if arr.size == 0:
            continue
        keys.append((r["book_slug"], r["path"]))
        lines.append(r["line"])
        vec_rows.append(arr)
    if not vec_rows:
        return {"keys": [], "lines": [], "mat": None, "norms": None, "dim": 0}
    try:
        mat = _np.vstack(vec_rows)
    except ValueError:  # 维度不一致
        return None
    return {"keys": keys, "lines": lines, "mat": mat,
            "norms": _np.linalg.norm(mat, axis=1), "dim": int(mat.shape[1])}


def _vec_cache_get(con, slugs, key, dim):
    """带指纹校验的进程内向量缓存；count/model/updated_at 变化即重载。
    无 numpy 或加载失败返回 None（调用方走纯 Python 路径）。"""
    if _np is None:
        return None
    try:
        fp = _embed_fingerprint(con, slugs)
    except Exception:
        return None
    with _VEC_CACHE_LOCK:
        ent = _VEC_CACHE.get(key)
    if ent and ent.get("fp") == fp and ent.get("dim") in (0, dim):
        return ent
    try:
        ent = _vec_cache_load(con, slugs)
    except Exception:
        return None
    if ent is None or (ent["mat"] is not None and ent["dim"] != dim):
        return None
    ent["fp"] = fp
    with _VEC_CACHE_LOCK:
        if len(_VEC_CACHE) > 32:
            _VEC_CACHE.clear()
        _VEC_CACHE[key] = ent
    return ent


def _agg_files(con, agg, top_files, max_lines):
    """聚合 (book_slug,path) -> 文件级结果（含 top 行片段），结构同 _index_terms。
    片段用行值 IN 一次性批量取（跨机 RTT 高，避免每文件一查）。"""
    plans = []
    wanted = []
    for (bs, path), v in agg.items():
        line_nos = sorted(v["lines"], key=lambda n: -v["lines"][n][0])[:max_lines]
        plans.append(((bs, path), line_nos))
        wanted.extend((bs, path, n) for n in line_nos)
    snippets = {}
    for i in range(0, len(wanted), 200):
        chunk = wanted[i:i + 200]
        ph = ",".join("(?,?,?)" for _ in chunk)
        args = [x for t in chunk for x in t]
        for lr in con.execute(
                f"SELECT book_slug, path, line, text FROM index_lines"
                f" WHERE (book_slug,path,line) IN ({ph})", args):
            snippets[(lr["book_slug"], lr["path"], lr["line"])] = \
                lr["text"][:180]
    out = []
    for (bs, path), line_nos in plans:
        lines = [{"line": n, "snippet": snippets.get((bs, path, n), "")}
                 for n in line_nos]
        out.append({"book_slug": bs, "path": path, "score": agg[(bs, path)]["score"],
                    "lines": lines, "tokens": 0})
    out.sort(key=lambda x: (-x["score"], x["book_slug"], x["path"]))
    return out[:top_files]


def _vector_search_slow(econ, con, slugs, query_vec, top_files, max_lines):
    ph = ",".join("?" * len(slugs))
    rows = econ.execute(
        f"SELECT book_slug, path, line, vec FROM index_embed"
        f" WHERE book_slug IN ({ph})", list(slugs)).fetchall()
    agg = {}
    for r in rows:
        raw = r["vec"]
        if isinstance(raw, (bytes, bytearray, memoryview)):
            import struct
            v = list(struct.unpack(f"<{len(raw) // 4}f", bytes(raw)))
        else:
            try:
                v = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
        if not isinstance(v, list) or len(v) != len(query_vec):
            continue
        score = _cosine(query_vec, v)
        if score <= 0:
            continue
        key = (r["book_slug"], r["path"])
        a = agg.get(key)
        if a is None:
            a = agg[key] = {"score": 0, "lines": {}}
        a["score"] += score
        a["lines"].setdefault(r["line"], (score, None))
    return _agg_files(con, agg, top_files, max_lines)


def _vector_search(con, slugs, query_vec, top_files=20, max_lines=3, cfg=None):
    """在**本地** embed.db 上做余弦近邻检索，按文件聚合。结果结构与 _index_terms
    相同。有 numpy 时矩阵+进程内缓存（ms 级），否则逐行纯 Python。
    片段仍从 con 的 index_lines 补（小流量）。"""
    if not query_vec:
        return []
    econ = _embed_con(cfg)
    if econ is None:
        return []
    try:
        key = (str(_embed_db_path(cfg)),) + tuple(sorted(slugs))
        entry = _vec_cache_get(econ, slugs, key, len(query_vec))
        if entry is None:
            return _vector_search_slow(econ, con, slugs, query_vec,
                                       top_files, max_lines)
        if entry["mat"] is None:
            return []
        q = _np.asarray(query_vec, dtype=_np.float32)
        qn = float(_np.linalg.norm(q))
        if not qn:
            return []
        scores = (entry["mat"] @ q) / (entry["norms"] * qn + 1e-12)
        agg = {}
        for i, score in enumerate(scores.tolist()):
            if score <= 0:
                continue
            key2 = entry["keys"][i]
            a = agg.get(key2)
            if a is None:
                a = agg[key2] = {"score": 0, "lines": {}}
            a["score"] += score
            a["lines"].setdefault(entry["lines"][i], (score, None))
        return _agg_files(con, agg, top_files, max_lines)
    finally:
        econ.close()


def _rrf_fuse(lexical, vector, k=60):
    """Reciprocal Rank Fusion：合并词面与向量两路结果。"""
    scores = {}
    payloads = {}
    for rank, it in enumerate(lexical, 1):
        key = (it.get("book_slug", ""), it["path"])
        scores[key] = scores.get(key, 0) + 1.0 / (k + rank)
        payloads[key] = it
    for rank, it in enumerate(vector, 1):
        key = (it.get("book_slug", ""), it["path"])
        scores[key] = scores.get(key, 0) + 1.0 / (k + rank)
        if key not in payloads:
            payloads[key] = it
        else:
            # 合并行片段
            old = payloads[key]
            seen = {(l["line"]) for l in old.get("lines", [])}
            for l in it.get("lines", []):
                if l["line"] not in seen:
                    old.setdefault("lines", []).append(l)
            old["score"] = scores[key]
    out = []
    for key, sc in scores.items():
        it = dict(payloads[key])
        it["score"] = sc
        out.append(it)
    out.sort(key=lambda x: (-x["score"], x.get("book_slug", ""), x["path"]))
    return out


def search_index_hybrid(con, slugs, query, cfg, limit_files=6, max_lines=3,
                        per_book=0):
    """混合检索：词面 + 向量 RRF 融合。未配置嵌入或无向量时退化为纯词面。"""
    lexical = search_index(con, slugs, query, limit_files, max_lines, per_book)
    try:
        import llm
        base, _key, _m = llm._embedding_endpoint(cfg)
    except Exception:
        base = None
    if not base:
        return lexical
    try:
        qvec = llm.embed_texts(cfg, [query], timeout=20, mode="query")
    except Exception:
        qvec = None
    if not qvec or not qvec[0]:
        return lexical
    try:
        vector = _vector_search(con, slugs, qvec[0],
                                top_files=limit_files * 2, max_lines=max_lines,
                                cfg=cfg)
    except Exception:
        vector = []
    if not vector:
        return lexical
    fused = _rrf_fuse(lexical, vector)
    if per_book:
        fused = _cap_per_book(fused, per_book)
    return fused[:limit_files]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None,
                    help="db 路径或 postgres URL；缺省取 config.registry_db"
                         "（相对路径按仓库根解析），再缺省 pipeline/registry.db")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--sources", default="H:/book")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--reindex", nargs="?", const="", default=None,
                    help="重建索引：留空=全部；或给 slug 列表(逗号分隔)")
    ap.add_argument("--embed", nargs="?", const="", default=None,
                    help="生成行级嵌入(需配置 embedding_model)：留空=全部；或 slug 列表")
    ap.add_argument("--config", default=str(Path(__file__).parent / "config.json"))
    a = ap.parse_args()
    try:
        cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = {}
    if a.db:
        db = a.db
    elif cfg.get("registry_db"):
        db = resolve_db(cfg["registry_db"], Path(__file__).parent.parent)
    else:
        db = Path(__file__).parent / "registry.db"
    con = connect(db)
    if a.init:
        print("initialized:", a.db)
    if a.seed:
        n = import_existing(con, "C:/Users/Icy/.agents/skills")
        m = link_sources(con, a.sources)
        print(f"imported {n} skills, linked {m} sources")
    if a.embed is not None:
        want = {s.strip() for s in a.embed.split(",") if s.strip()}
        rows = con.execute("SELECT slug FROM books").fetchall()
        total = 0
        for r in rows:
            if want and r["slug"] not in want:
                continue
            n = embed_book(con, r["slug"], cfg)
            total += n
            print(f"{r['slug']:<32} {n:>6} embeds")
        print(f"embedded {total} lines")
    if a.reindex is not None:
        want = {s.strip() for s in a.reindex.split(",") if s.strip()}
        rows = con.execute("SELECT slug, skill_path FROM books").fetchall()
        total_f = total_l = 0
        for r in rows:
            if want and r["slug"] not in want:
                continue
            sp = r["skill_path"]
            if not sp or not Path(sp).is_dir():
                continue
            st = reindex_book(con, r["slug"], sp)
            total_f += st["files"]
            total_l += st["lines"]
            print(f"{r['slug']:<32} {st['files']:>3} files {st['lines']:>6} lines")
        print(f"indexed {total_f} files, {total_l} lines")
    if a.list or not (a.init or a.seed or a.reindex is not None):
        for r in con.execute("SELECT slug,title,chapter_count,token_estimate,status FROM books ORDER BY slug"):
            print(f"{r['slug']:<24} {r['chapter_count'] or 0:>3}ch {r['token_estimate'] or 0:>7}tok "
                  f"{r['status']:<10} {r['title']}")


if __name__ == "__main__":
    main()
