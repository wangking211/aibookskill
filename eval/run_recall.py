"""A2 召回评测：词面 vs 混合检索，文件级 recall@10 / hit@6。

在服务器上运行（需要 PG + 本地 embed.db + sidecar）：
  cd /opt/aibook/app
  /opt/aibook/venv/bin/python eval/run_recall.py --set /tmp/recall_set.json --out /tmp/REPORT.md

本地只做语法检查（search 依赖服务器数据，不在本地跑）。
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# registry.search_index_hybrid 内部用绝对 `import llm`（与 worker/web 同款运行方式），
# 必须把 pipeline/ 也加入 sys.path，否则静默退化为纯词面。
sys.path.insert(0, str(ROOT / "pipeline"))

from pipeline import registry  # noqa: E402


def top_paths(hits):
    return [h["path"] for h in hits]


def eval_one(con, cfg, item, limit_files=10):
    book = item["book"]
    q = item["question"]
    gold_paths = {g["path"] for g in item["gold"]}
    t0 = time.time()
    lex = registry.search_index(con, [book], q, limit_files=limit_files)
    t_lex = (time.time() - t0) * 1000
    t0 = time.time()
    hyb = registry.search_index_hybrid(con, [book], q, cfg, limit_files=limit_files)
    t_hyb = (time.time() - t0) * 1000

    def score(hits, k):
        tp = top_paths(hits)[:k]
        found = gold_paths & set(tp)
        # hit@k：任一 gold 文件进入 top-k；recall@k：gold 文件被召回比例
        return (1 if found else 0), (len(found) / len(gold_paths) if gold_paths else 0.0)

    lex_hit10, lex_rec10 = score(lex, 10)
    lex_hit6, _ = score(lex, 6)
    hyb_hit10, hyb_rec10 = score(hyb, 10)
    hyb_hit6, _ = score(hyb, 6)
    return {
        "id": item["id"], "book": book, "question": q,
        "gold": sorted(gold_paths),
        "lex_hit10": lex_hit10, "lex_rec10": lex_rec10, "lex_hit6": lex_hit6,
        "hyb_hit10": hyb_hit10, "hyb_rec10": hyb_rec10, "hyb_hit6": hyb_hit6,
        "lex_top": top_paths(lex)[:10], "hyb_top": top_paths(hyb)[:10],
        "t_lex_ms": round(t_lex), "t_hyb_ms": round(t_hyb),
        "rescued": bool(hyb_hit10 and not lex_hit10),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default=str(ROOT / "eval" / "recall_set.json"))
    ap.add_argument("--out", default="")
    ap.add_argument("--limit-files", type=int, default=10)
    args = ap.parse_args()

    data = json.loads(Path(args.set).read_text(encoding="utf-8"))
    items = data["items"] if isinstance(data, dict) else data
    cfg = json.loads((ROOT / "pipeline" / "config.json").read_text(encoding="utf-8"))
    con = registry.connect(registry.resolve_db(cfg["registry_db"], ROOT))

    rows = []
    for it in items:
        r = eval_one(con, cfg, it, limit_files=args.limit_files)
        rows.append(r)
        flag = "RESCUE" if r["rescued"] else ("lexOK" if r["lex_hit10"] else "miss")
        print(f"{r['id']:<6} {flag:<6} lex={r['lex_hit10']}/{r['lex_hit6']} "
              f"hyb={r['hyb_hit10']}/{r['hyb_hit6']} "
              f"[{r['t_lex_ms']}ms/{r['t_hyb_ms']}ms] {r['question'][:36]}")

    n = len(rows)
    agg = {
        "n": n,
        "lex_hit10": sum(r["lex_hit10"] for r in rows) / n,
        "lex_hit6": sum(r["lex_hit6"] for r in rows) / n,
        "lex_rec10": sum(r["lex_rec10"] for r in rows) / n,
        "hyb_hit10": sum(r["hyb_hit10"] for r in rows) / n,
        "hyb_hit6": sum(r["hyb_hit6"] for r in rows) / n,
        "hyb_rec10": sum(r["hyb_rec10"] for r in rows) / n,
        "rescued": sum(1 for r in rows if r["rescued"]),
        "regressed": sum(1 for r in rows if r["hyb_hit10"] < r["lex_hit10"]),
        "t_lex_p50": sorted(r["t_lex_ms"] for r in rows)[n // 2],
        "t_hyb_p50": sorted(r["t_hyb_ms"] for r in rows)[n // 2],
    }
    print("\n=== SUMMARY ===")
    for k, v in agg.items():
        print(f"  {k}: {v}")

    if args.out:
        lines = [
            "# A2 召回评测报告",
            "",
            f"- 标注集：{Path(args.set).name}（{n} 条换说法问句）",
            f"- 词面 hit@10 **{agg['lex_hit10']:.0%}** / hit@6 {agg['lex_hit6']:.0%} / recall@10 {agg['lex_rec10']:.0%}",
            f"- 混合 hit@10 **{agg['hyb_hit10']:.0%}** / hit@6 {agg['hyb_hit6']:.0%} / recall@10 {agg['hyb_rec10']:.0%}",
            f"- 词面救回（hyb 中而 lex 未中）：{agg['rescued']}；回退（hyb 丢 lex 中）：{agg['regressed']}",
            f"- P50 延迟：词面 {agg['t_lex_p50']}ms / 混合 {agg['t_hyb_p50']}ms（单书、warm）",
            f"- 门槛：hybrid hit@10 ≥ 75% → {'✅ PASS' if agg['hyb_hit10'] >= 0.75 else '❌ FAIL'}",
            "",
            "## 逐题结果",
            "",
            "| id | lex@10 | hyb@10 | rescue | 问题 |",
            "|---|---|---|---|---|",
        ]
        for r in rows:
            lines.append(
                f"| {r['id']} | {'✓' if r['lex_hit10'] else '✗'} "
                f"| {'✓' if r['hyb_hit10'] else '✗'} "
                f"| {'✓' if r['rescued'] else ''} | {r['question'][:40]} |")
        Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"report -> {args.out}")

    sys.exit(0 if agg["hyb_hit10"] >= 0.75 else 1)


if __name__ == "__main__":
    main()
