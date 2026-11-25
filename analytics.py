# -*- coding: utf-8 -*-
"""学习数据聚合：logs/generations.jsonl → 摘要统计 + markdown 报告。

对应论文 §6：课程档案作为后训练数据的"仪表盘"——
  · 语义覆盖率均值与按天趋势（语义-参数流畅性的代理指标）
  · 字段命中率排行（哪些语义最常被学生使用/被接住）
  · 风格预设使用分布、解析引擎分布、树木达成率

用法：py analytics.py        → 打印摘要并写 logs/analytics.md
      app.py 经 /api/analytics 复用 summarize()
"""
import io
import json
import os
import sys
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_JSONL = os.path.join(BASE, "logs", "generations.jsonl")
REPORT_MD = os.path.join(BASE, "logs", "analytics.md")

_FIELDS = ("site.shape", "site.size", "roads", "plaza", "water",
           "trees", "terrain", "green", "paving")


def _load():
    recs = []
    if not os.path.isfile(LOG_JSONL):
        return recs
    with open(LOG_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except ValueError:
                pass                    # 容忍个别坏行
    return recs


def summarize():
    recs = _load()
    if not recs:
        return {"total": 0}
    covs, by_day = [], defaultdict(list)
    fields, styles, engines = Counter(), Counter(), Counter()
    negations = Counter()
    trees_placed = trees_req = 0
    for r in recs:
        spec = r.get("spec") or {}
        audit = spec.get("audit") or {}
        cov = audit.get("coverage")
        if cov is not None:
            covs.append(cov)
            day = (r.get("ts") or "")[:10]
            if day:
                by_day[day].append(cov)
        for f in audit.get("matched", []):
            fields[f] += 1
        for f in audit.get("negated", []):
            negations[f] += 1
        if spec.get("style"):
            styles[spec["style"]] += 1
        engines[r.get("engine", "?")] += 1
        st = r.get("stats") or {}
        trees_placed += st.get("trees", 0)
        trees_req += st.get("trees_requested", 0)
    return {
        "total": len(recs),
        "coverage_mean": round(sum(covs) / len(covs), 3) if covs else None,
        "coverage_min": min(covs) if covs else None,
        "coverage_max": max(covs) if covs else None,
        "coverage_by_day": [[d, round(sum(v) / len(v), 3), len(v)]
                            for d, v in sorted(by_day.items())],
        "fields": dict(fields.most_common()),
        "negations": dict(negations.most_common()),
        "styles": dict(styles.most_common()),
        "engines": dict(engines.most_common()),
        "trees": {"placed": trees_placed, "requested": trees_req,
                  "rate": round(trees_placed / trees_req, 3) if trees_req else None},
        "recent": [r.get("prompt", "")[:40] for r in recs[-5:]],
    }


def render_md(s):
    if not s.get("total"):
        return "# 学习数据聚合\n\n（暂无数据）\n"
    L = ["# 学习数据聚合（generations.jsonl）", "",
         "- 总生成数：**%d**" % s["total"],
         "- 语义覆盖率：均值 **%.0f%%**（min %.0f%% / max %.0f%%）"
         % (s["coverage_mean"] * 100, s["coverage_min"] * 100, s["coverage_max"] * 100),
         "- 树木达成率：%s" % (("%d/%d（%.0f%%）" % (s["trees"]["placed"],
             s["trees"]["requested"], s["trees"]["rate"] * 100))
             if s["trees"]["rate"] is not None else "-"),
         "- 引擎分布：" + "、".join("%s×%d" % kv for kv in s["engines"].items()),
         "- 风格使用：" + ("、".join("%s×%d" % kv for kv in s["styles"].items())
                            or "（未使用）"),
         "", "## 按天覆盖率趋势", "", "| 日期 | 均值 | 样本 |", "|---|---|---|"]
    for d, m, n in s["coverage_by_day"]:
        L.append("| %s | %.0f%% | %d |" % (d, m * 100, n))
    L += ["", "## 字段命中率（被学生说中的语义）", "", "| 字段 | 次数 |", "|---|---|"]
    for f, c in s["fields"].items():
        L.append("| %s | %d |" % (f, c))
    if s["negations"]:
        L += ["", "## 显式否定（学生明确不要的）", "",
              "| 字段 | 次数 |", "|---|---|"]
        for f, c in s["negations"].items():
            L.append("| %s | %d |" % (f, c))
    L += ["", "## 最近 5 条 prompt", ""]
    L += ["- " + p for p in s["recent"]]
    return "\n".join(L) + "\n"


def main():
    s = summarize()
    print(json.dumps(s, ensure_ascii=False, indent=2))
    os.makedirs(os.path.dirname(REPORT_MD), exist_ok=True)
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write(render_md(s))
    print("已写", REPORT_MD)


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    main()
