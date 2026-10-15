#!/usr/bin/env python3
"""aibook-kit 安装器（跨平台，仅用标准库）。

把「客户端 MCP + book-to-skill + pipeline 本地转换栈」部署到目标目录，
生成 pipeline/config.json、合并 opencode.json 的 MCP 段、安装依赖并自检。

用法:
  python setup.py --root DIR --token ak_xxx [--base-url URL]
                     [--skills-home DIR] [--model MODEL]
                     [--force] [--no-deps] [--yes]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent
DEFAULT_BASE_URL = "https://aibook.zxixing.com"
DEFAULT_MODEL = "opencode/mimo-v2.6-flash-free"

# 拷贝到 root 的目录 / 文件
TREES = ["pipeline", "skill-server", ".opencode", "skill-src"]
FILES = ["mcp_server.py", "requirements.txt", "setup.py", "install.ps1",
         "install.sh", "README.md", "opencode.json.example",
         "config.json.template"]
# 生成结果（永不被拷贝覆盖）
GENERATED = {Path("pipeline/config.json"), Path("opencode.json")}
# 拷贝时跳过的目录
SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".pytest_cache",
             ".mypy_cache", "dist", "build"}
# 运行期由 worker 创建的目录
RUN_DIRS = ["inbox", "workdir", "logs", "covers", "library"]
REQUIRED_MODULES = ["mcp", "pymupdf", "httpx"]

_ok, _warn, _err = [], [], []


def say(tag, msg):
    print(f"[{tag}] {msg}")


def die(msg):
    say("ERR", msg)
    sys.exit(1)


def same(p: Path, q: Path) -> bool:
    try:
        return p.resolve() == q.resolve()
    except OSError:
        return False


def norm(p: Path) -> str:
    return str(p).replace("\\", "/")


def sync_tree(src_root: Path, dst_root: Path, force: bool):
    n = 0
    for tree in TREES:
        s = src_root / tree
        if not s.is_dir():
            continue
        for p in sorted(s.rglob("*")):
            rel = p.relative_to(src_root)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            if rel in GENERATED and not force:
                continue
            d = dst_root / rel
            if p.is_dir():
                d.mkdir(parents=True, exist_ok=True)
                continue
            if same(p, d):
                continue
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, d)
            n += 1
    for name in FILES:
        p = src_root / name
        if not p.is_file():
            continue
        d = dst_root / name
        if same(p, d):
            continue
        if d.exists() and not force:
            continue
        shutil.copy2(p, d)
        n += 1
    return n


def install_skills(skills_home: Path, force: bool):
    """skill-src/<name>/ -> <skills_home>/<name>/ 以及 .opencode/skills/* -> 同处。"""
    srcs = []
    if (KIT / "skill-src").is_dir():
        srcs += [d for d in sorted((KIT / "skill-src").iterdir()) if d.is_dir()]
    if (KIT / ".opencode" / "skills").is_dir():
        srcs += [d for d in sorted((KIT / ".opencode" / "skills").iterdir())
                 if d.is_dir()]
    installed = []
    skills_home.mkdir(parents=True, exist_ok=True)
    for s in srcs:
        d = skills_home / s.name
        if same(s, d):
            installed.append(f"{s.name} (原地)")
            continue
        if d.exists() and not force:
            installed.append(f"{s.name} (已存在，跳过)")
            continue
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(s, d, ignore=shutil.ignore_patterns(*SKIP_DIRS))
        installed.append(s.name)
    return installed


def render(text: str, mapping: dict) -> str:
    """把 {{KEY}} 填进 JSON 文本；值按 JSON 字符串转义，避免反斜杠破坏解析。"""
    def repl(m):
        k = m.group(1)
        if k not in mapping:
            die("模板占位符未填充: " + k)
        return json.dumps(str(mapping[k]), ensure_ascii=False)[1:-1]
    return re.sub(r"\{\{(\w+)\}\}", repl, text)


def find_opencode_candidates():
    cands = []
    home = Path.home()
    for c in (home / ".local" / "bin" / "opencode.exe",
              home / ".local" / "bin" / "opencode",
              home / ".opencode" / "bin" / "opencode.exe",
              home / ".opencode" / "bin" / "opencode",
              home / ".local" / "share" / "opencode" / "bin" / "opencode"):
        if c.exists():
            cands.append(str(c))
    for name in ("opencode.exe", "opencode"):
        p = shutil.which(name)
        if p and p not in cands:
            cands.append(p)
    return cands


def _vtuple(v):
    try:
        return tuple(int(x) for x in v.split(".")[:3])
    except (ValueError, AttributeError):
        return (0, 0, 0)


def pick_opencode():
    """在所有可执行的 opencode 里挑最好的：优先非 1.2.x，其次版本最高。"""
    runnable = []
    for c in find_opencode_candidates():
        v, err = opencode_version(c)
        if v and not err:
            runnable.append((_vtuple(v), c, v))
    if runnable:
        good = [x for x in runnable if not re.match(r"^1\.2\.", x[2])]
        _vt, path, ver = max(good or runnable, key=lambda x: x[0])
        return path, ver
    return "opencode", None


def opencode_version(binary):
    try:
        r = subprocess.run([binary, "--version"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    m = re.search(r"\d+\.\d+\.\d+", out)
    return (m.group(0) if m else out or None), None


def write_config(root: Path, mapping: dict, force: bool):
    tpl = KIT / "config.json.template"
    dst = root / "pipeline" / "config.json"
    if dst.exists() and not force:
        say("skip", f"{dst} 已存在（--force 覆盖）")
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(render(tpl.read_text(encoding="utf-8"), mapping),
                   encoding="utf-8")
    say("ok", f"写入 {dst}")
    return True


def merge_opencode(root: Path, python: str, mapping: dict, force: bool):
    path = root / "opencode.json"
    cfg = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            if not force:
                die(f"opencode.json 解析失败({e})，修复后重跑或加 --force")
            cfg = {}
    if not isinstance(cfg, dict):
        if not force:
            die("opencode.json 顶层不是对象，加 --force 重建")
        cfg = {}
    cfg.setdefault("$schema", "https://opencode.ai/config.json")

    example = json.loads((KIT / "opencode.json.example")
                         .read_text(encoding="utf-8"))
    mcp = cfg.setdefault("mcp", {})
    for name, entry in example["mcp"].items():
        entry = json.loads(render(json.dumps(entry, ensure_ascii=False),
                                  {**mapping, "PYTHON": python}))
        if name in mcp and not force:
            say("skip", f"mcp.{name} 已存在，保留原值（--force 覆盖）")
            continue
        mcp[name] = entry
        say("ok", f"mcp.{name} -> {entry['command'][1]}")

    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    say("ok", f"写入 {path}")
    return path


def run_pip(python: str, req: Path, enabled: bool):
    if not enabled:
        say("skip", "跳过依赖安装 (--no-deps)")
        return True
    say("run", f"{python} -m pip install -r {req}")
    r = subprocess.run([python, "-m", "pip", "install", "-r", str(req)])
    return r.returncode == 0


def verify(root: Path, python: str, skills_home: Path,
           opencode_bin: str, opencode_ver):
    bad = []
    code = ("import importlib.util, json;"
            "mods=%r;"
            "print(json.dumps({m: bool(importlib.util.find_spec(m))"
            "                   for m in mods}))" % REQUIRED_MODULES)
    try:
        r = subprocess.run([python, "-c", code], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=120)
        found = json.loads((r.stdout or "{}").strip() or "{}")
        for m in REQUIRED_MODULES:
            if not found.get(m):
                bad.append(m)
    except Exception as e:
        bad.append(f"检查失败: {e}")
    if bad:
        _warn.append("缺少依赖: " + ", ".join(bad) +
                     f"  →  {python} -m pip install -r {root/'requirements.txt'}")
    else:
        say("ok", "依赖可用: " + ", ".join(REQUIRED_MODULES))

    # pipeline 模块自检
    env = dict(os.environ, PYTHONUTF8="1", PYTHONPATH=norm(root / "pipeline"))
    code = "import registry, gate, llm, cosutil, worker"  # noqa: F821
    try:
        r = subprocess.run([python, "-c", code], capture_output=True, cwd=str(root),
                           text=True, encoding="utf-8", errors="replace",
                           env=env, timeout=120)
        if r.returncode == 0:
            say("ok", "pipeline 模块导入正常 (registry/gate/llm/cosutil/worker)")
        else:
            _err.append("pipeline 导入失败: " +
                        (r.stderr or "").strip().splitlines()[-1])
    except Exception as e:
        _err.append(f"pipeline 导入检查异常: {e}")

    # MCP 入口自检
    for f in ("mcp_server.py", "skill-server/server.py"):
        try:
            r = subprocess.run([python, "-m", "py_compile", norm(root / f)],
                               capture_output=True, cwd=str(root),
                               timeout=60)
            if r.returncode != 0:
                _err.append(f"{f} 语法错误")
        except Exception as e:
            _err.append(f"{f} 检查异常: {e}")

    # book-to-skill 自检
    if not (skills_home / "book-to-skill" / "tools"
            / "scan_generated_skill.py").is_file():
        _err.append("book-to-skill 未安装到 skills_home，转换与安全扫描都不可用")
    elif not (skills_home / "book-to-skill" / "scripts" / "extract.py").is_file():
        _err.append("book-to-skill 缺 scripts/extract.py")
    else:
        say("ok", f"book-to-skill @ {skills_home / 'book-to-skill'}")

    # opencode
    if not opencode_bin or opencode_bin == "opencode":
        _warn.append("未找到可用的 opencode。本地转换需要它: "
                     "https://opencode.ai  → 装好后重跑 setup.py")
    else:
        say("ok", f"opencode {opencode_ver} @ {opencode_bin}")
        if opencode_ver and re.match(r"^1\.2\.", opencode_ver):
            _warn.append(f"opencode {opencode_ver} 与本流水线不兼容，"
                         "请升级到 >=1.19.0")


def main():
    ap = argparse.ArgumentParser(description="aibook-kit 安装器")
    ap.add_argument("--root", default=str(KIT),
                    help="安装目标目录（默认即 kit 自身目录）")
    ap.add_argument("--token", default=os.environ.get("AIBOOK_TOKEN", ""),
                    help="aibook API 令牌（ak_ 开头），也可用环境变量 AIBOOK_TOKEN")
    ap.add_argument("--base-url", default=os.environ.get(
        "AIBOOK_BASE_URL", DEFAULT_BASE_URL))
    ap.add_argument("--skills-home", default="",
                    help="生成的书籍 skill 存放目录（默认 ~/.agents/skills）")
    ap.add_argument("--model", default=os.environ.get("AIBOOK_MODEL", DEFAULT_MODEL),
                    help=f"默认模型（默认 {DEFAULT_MODEL}）")
    ap.add_argument("--force", action="store_true",
                    help="覆盖已存在的 config.json / 已安装 skill / MCP 段")
    ap.add_argument("--no-deps", action="store_true", help="跳过 pip 安装")
    ap.add_argument("--yes", "-y", action="store_true", help="不再提示")
    args = ap.parse_args()

    if sys.version_info < (3, 9):
        die("需要 Python >= 3.9")

    root = Path(args.root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    skills_home = (Path(args.skills_home).expanduser().resolve()
                   if args.skills_home
                   else (Path.home() / ".agents" / "skills").resolve())
    python = sys.executable or "python"
    opencode_bin, opencode_ver = pick_opencode()

    say("run", f"root={root}")
    say("run", f"skills_home={skills_home}")
    say("run", f"python={python}")

    # 1) 代码与脚本
    if same(KIT, root):
        say("skip", "root == kit 目录，跳过复制")
    else:
        n = sync_tree(KIT, root, args.force)
        say("ok", f"复制 {n} 个文件 -> {root}")

    # 2) 运行期目录
    for d in RUN_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "pipeline" / "conf").mkdir(parents=True, exist_ok=True)

    # 3) skills
    inst = install_skills(skills_home, args.force)
    if inst:
        say("ok", "skills: " + ", ".join(inst))

    # 4) 配置
    mapping = {
        "ROOT": norm(root),
        "SKILLS_HOME": norm(skills_home),
        "SCAN_TOOL": norm(skills_home / "book-to-skill" / "tools"
                          / "scan_generated_skill.py"),
        "OPENCODE_BIN": opencode_bin,
        "MODEL": args.model,
        "BASE_URL": args.base_url.rstrip("/"),
        "AIBOOK_TOKEN": args.token,
    }
    write_config(root, mapping, args.force)
    merge_opencode(root, python, mapping, args.force)

    if not args.token:
        _warn.append("未提供令牌：aibook MCP 的写操作会 401。"
                     "到网页「设置→API 令牌」创建后重跑 "
                     f"python setup.py --token ak_xxx")

    # 5) 依赖 + 自检
    run_pip(python, root / "requirements.txt", not args.no_deps)
    verify(root, python, skills_home, opencode_bin, opencode_ver)

    print()
    for m in _err:
        say("ERR", m)
    for m in _warn:
        say("warn", m)
    if _err:
        sys.exit(1)

    print()
    say("done", "安装完成。下一步：")
    print(f"""  1. opencode 登录（免费模型）:  opencode auth login
  2. 令牌:  {args.base_url} → 顶部「令牌」创建，然后
       python setup.py --token ak_xxx
  3. 在 {root} 里启动 opencode，验证：
       list_books() / upload_book('/path/book.pdf')
  4. 只用远端转换（推荐，不需要本地 opencode）：
       交给 upload_book()，转换在服务端完成。
  5. 本地转换 + 推送到服务器：
       python pipeline/worker.py --pdf /path/book.pdf --upload
  6. 本地翻译某本书的章节并回传：
       python pipeline/worker.py --translate <slug> --force
  7. 直接消费任务队列：
       python pipeline/worker.py --daemon
""")


if __name__ == "__main__":
    main()
