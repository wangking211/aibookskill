import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

KIT_SRC = Path(__file__).resolve().parent
REPO = KIT_SRC.parent
STAGE = Path(tempfile.gettempdir()) / "opencode" / "aibook-kit"
NAME = "aibook-kit"

PIPELINE_FILES = ["worker.py", "gate.py", "llm.py", "registry.py",
                  "cosutil.py", "prompt_template.txt"]
SKIP = {"__pycache__", ".git", "node_modules", ".pytest_cache"}

# 目标相对路径 -> 源绝对路径
plan = {}


def add_tree(src: Path, dst_rel: str, only=None, exclude=()):
    if not src.is_dir():
        print("MISSING TREE", src)
        return
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        if any(part in SKIP for part in rel.parts):
            continue
        if rel.suffix in (".pyc", ".pyo"):
            continue
        if only is not None and rel.parts[0] not in only:
            continue
        if rel.parts and rel.parts[0] in exclude:
            continue
        plan[str(Path(dst_rel) / rel).replace("\\", "/")] = p


# 1) kit 自带文件
add_tree(KIT_SRC, NAME)
# 2) pipeline
for f in PIPELINE_FILES:
    p = REPO / "pipeline" / f
    if p.is_file():
        plan[f"{NAME}/pipeline/{f}"] = p
    else:
        print("MISSING", p)
# 3) skill-server / mcp_server
plan[f"{NAME}/skill-server/server.py"] = REPO / "skill-server" / "server.py"
plan[f"{NAME}/mcp_server.py"] = REPO / "mcp_server.py"
# 4) .opencode（只要 agent + .gitignore；skills 走 skill-src；node 产物不要）
add_tree(REPO / ".opencode", f"{NAME}/.opencode")
for drop in [k for k in plan
             if "/.opencode/skills/" in k
             or k.rsplit("/", 1)[-1] in ("package.json", "package-lock.json",
                                         "bun.lock")]:
    del plan[drop]
# 5) skill-src: aibook 技能
src_skill = REPO / ".opencode" / "skills" / "aibook" / "SKILL.md"
if src_skill.is_file():
    plan[f"{NAME}/skill-src/aibook/SKILL.md"] = src_skill
# 6) skill-src: book-to-skill（仓库外，来自全局技能目录）
BTS = Path(os.environ.get("BOOK_TO_SKILL_SRC")
           or (Path.home() / ".agents" / "skills" / "book-to-skill"))
BTS_EXCLUDE = {".github", "docs", "evals", "tests", "__pycache__", ".git",
               "node_modules"}
if BTS.is_dir():
    for p in sorted(BTS.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(BTS)
        if any(part in BTS_EXCLUDE for part in rel.parts):
            continue
        if rel.suffix in (".pyc", ".pyo"):
            continue
        plan[f"{NAME}/skill-src/book-to-skill/{rel}".replace("\\", "/")] = p
else:
    print("MISSING book-to-skill:", BTS)

missing = [k for k, v in plan.items() if not v.is_file()]
if missing:
    print("MISSING SOURCES:")
    for m in missing:
        print("  ", m)
    sys.exit(1)

if STAGE.exists():
    shutil.rmtree(STAGE)
STAGE.mkdir(parents=True)

for rel, src in sorted(plan.items()):
    dst = STAGE / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)

# 自检：setup.py 需要的输入是否齐
need = [f"{NAME}/setup.py", f"{NAME}/config.json.template",
        f"{NAME}/opencode.json.example", f"{NAME}/requirements.txt",
        f"{NAME}/install.ps1", f"{NAME}/install.sh", f"{NAME}/README.md",
        f"{NAME}/mcp_server.py", f"{NAME}/skill-server/server.py",
        f"{NAME}/skill-src/aibook/SKILL.md",
        f"{NAME}/skill-src/book-to-skill/SKILL.md",
        f"{NAME}/skill-src/book-to-skill/tools/scan_generated_skill.py",
        f"{NAME}/skill-src/book-to-skill/scripts/extract.py",
        f"{NAME}/skill-src/book-to-skill/pyproject.toml",
        f"{NAME}/.opencode/agent/publisher.md"]
for f in PIPELINE_FILES:
    need.append(f"{NAME}/pipeline/{f}")
bad = [f for f in need if not (STAGE / f).is_file()]
if bad:
    print("STAGE MISSING:")
    for b in bad:
        print("  ", b)
    sys.exit(1)

# 打 zip（顶层带 aibook-kit/ 目录）
out = REPO / "web" / "static" / "aibook-kit.zip"
out.parent.mkdir(parents=True, exist_ok=True)
if out.exists():
    out.unlink()
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for p in sorted(STAGE.rglob("*")):
        if p.is_file():
            z.write(p, str(p.relative_to(STAGE)).replace("\\", "/"))

total = sum(p.stat().st_size for p in STAGE.rglob("*") if p.is_file())
print(f"files={len(plan)} staged_bytes={total:,} zip={out} "
      f"zip_bytes={out.stat().st_size:,}")

# 输出 zip 清单，便于核对
with zipfile.ZipFile(out) as z:
    for n in z.namelist():
        print("  ", n)
