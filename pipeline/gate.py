import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def extract_result_json(stdout_text):
    for line in (stdout_text or "").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        for s in _walk_strings(obj):
            res = _from_marker(s)
            if res is not None:
                return res
    m = re.search(r"RESULT_JSON", stdout_text or "")
    if m:
        rest = stdout_text[m.end():]
        return _parse_balanced(rest)
    return None


def _walk_strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _walk_strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_strings(v)


def _from_marker(s):
    idx = s.find("RESULT_JSON")
    if idx < 0:
        return None
    return _parse_balanced(s[idx + len("RESULT_JSON"):])


def _parse_balanced(s):
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i, c in enumerate(s[start:], start):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def run_scan(scan_tool, skill_dir):
    try:
        r = subprocess.run([sys.executable, scan_tool, str(skill_dir)],
                           capture_output=True, text=True, timeout=120)
        return r.returncode == 0, (r.stdout + r.stderr).strip()[-4000:]
    except Exception as e:
        return False, f"scan error: {e}"


def check_structure(skill_dir):
    d = Path(skill_dir)
    errors = []
    warnings = []
    if not (d / "SKILL.md").exists():
        errors.append("missing SKILL.md")
    ch = list((d / "chapters").glob("*.md")) if (d / "chapters").is_dir() else []
    if not ch:
        errors.append("no chapter files")
    for f in ("glossary.md", "patterns.md", "cheatsheet.md"):
        if not (d / f).exists():
            warnings.append(f"missing {f}")
    return errors, warnings


def evaluate(skill_dir, scan_tool, stdout_text):
    result = extract_result_json(stdout_text)
    struct_errors, warnings = check_structure(skill_dir)
    scan_ok, scan_out = run_scan(scan_tool, skill_dir)
    ok = scan_ok and not struct_errors and result is not None and result.get("status") == "ok"
    problems = list(struct_errors)
    if not scan_ok:
        problems.append("scan failed")
    if result is None:
        problems.append("RESULT_JSON not found")
    elif result.get("status") != "ok":
        problems.append(f"agent reported: {result.get('error', 'error')}")
    return {"ok": ok, "problems": problems, "warnings": warnings,
            "result": result, "scan_ok": scan_ok, "scan_out": scan_out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("skill_dir")
    ap.add_argument("--scan-tool",
                    default="C:/Users/Icy/.agents/skills/book-to-skill/tools/scan_generated_skill.py")
    ap.add_argument("--stdout-file")
    a = ap.parse_args()
    stdout_text = Path(a.stdout_file).read_text(encoding="utf-8", errors="replace") if a.stdout_file else ""
    rep = evaluate(a.skill_dir, a.scan_tool, stdout_text)
    print(json.dumps({k: rep[k] for k in ("ok", "problems", "warnings")},
                     ensure_ascii=False, indent=2))
    print(rep["scan_out"][-800:])
    sys.exit(0 if rep["ok"] else 1)


if __name__ == "__main__":
    main()
