"""Turn bench3.json into the markdown benchmark report."""
import json
import statistics
import sys
from collections import Counter

src, dst = sys.argv[1], sys.argv[2]
data = json.load(open(src, encoding="utf-8"))

# Standard paid tier, per 1M tokens (ai.google.dev/gemini-api/docs/pricing, read 2026-10-01).
# Thinking tokens are billed as output. Image tokens are billed as input.
PRICES = {
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "gemini-3.8-flash": (0.75, 3.75),  # promo through 2026-12-31; $1.50 / $7.50 from 2027-01-01
}
# USD -> INR, market rate on 2026-10-01 (FXStreet). Update when re-running.
USD_INR = 95.93
PRICES_2027 = {"gemini-3.1-pro-preview": (2.00, 12.00), "gemini-3.8-flash": (1.50, 7.50)}

# Expected scores where the image is unambiguous (reviewer judgement; the two
# boundary images are excluded from accuracy until labelled by the team).
EXPECTED = {
    "images (2).jpg": 1,
    "images (6).jpg": 1,
    "images (5).jpg": 3,
    "close-up-shot-fresh-cow-dung-forest-uttarakhand-india-255967705.webp": 5,
    "istockphoto-1444657605-1024x1024.jpg": 4,
}
UNLABELLED = {
    "circular-shaped-cow-dung-small-260nw-2642702011.webp": "3 or 4? (dip + pitted surface)",
    "images (3).jpg": "1 or 2? (flat spread, holds outline)",
}

LABELS = {
    "flat": "Old: single flat 27-way call",
    "two_pro31_low": "Two-step · 3.1 Pro · LOW thinking",
    "two_flash38_default": "Two-step · 3.8 Flash · default thinking",
    "two_flash38_low": "Two-step · 3.8 Flash · LOW thinking (now in code)",
}
MODEL_OF = {"flat": data["flat_model"], **{k: v[0] for k, v in data["variants"].items()}}
THINK_OF = {"flat": "default", **{k: ("default" if v[1] == "None" else "LOW") for k, v in data["variants"].items()}}


def cost(model, call, prices=PRICES):
    pin, pout = prices[model]
    return (call["input_tokens"] * pin + (call["output_tokens"] + call["thinking_tokens"]) * pout) / 1e6


variants = list(LABELS)
summary = {}
for v in variants:
    scans = [run for res in data["results"] for run in res["runs"][v]]
    calls = [c for s in scans for c in s["calls"]]
    per_scan = lambda key: statistics.mean(sum(c[key] for c in s["calls"]) for s in scans)
    walls = sorted(s["wall_s"] for s in scans)
    correct = total = 0
    stable = 0
    for res in data["results"]:
        runs = res["runs"][v]
        if len({(r["score"], r["sub_code"]) for r in runs}) == 1:
            stable += 1
        if res["image"] in EXPECTED:
            total += len(runs)
            correct += sum(r["score"] == EXPECTED[res["image"]] for r in runs)
    summary[v] = {
        "accuracy": f"{correct}/{total} ({100 * correct / total:.0f}%)",
        "stable": f"{stable}/{len(data['results'])}",
        "calls_per_scan": len(calls) / len(scans),
        "lat_mean": statistics.mean(walls),
        "lat_p50": walls[len(walls) // 2],
        "lat_max": walls[-1],
        "in": per_scan("input_tokens"),
        "out": per_scan("output_tokens"),
        "think": per_scan("thinking_tokens"),
        "cost": statistics.mean(sum(cost(MODEL_OF[v], c) for c in s["calls"]) for s in scans),
        "cost27": statistics.mean(sum(cost(MODEL_OF[v], c, PRICES_2027) for c in s["calls"]) for s in scans),
    }

L = []
w = L.append
runs_n = data["runs_per_image"]
n_img = len(data["results"])
w("# Manure Report — Model & Latency Benchmark\n")
w(f"_Run 2026-10-01 · {n_img} images from `Gau-Gopalan-Seva/testing_img/` · {runs_n} runs per image per variant · "
  f"{n_img * runs_n * len(variants)} scans in total · temperature 0 · KB = the 27 sub-codes of `master_table_v2.csv`._\n")

w("## Recommendation\n")
w("_Write the recommendation here after reviewing the tables below._\n")

w("## Summary\n")
w("| Variant | Model | Thinking | Score accuracy¹ | Stable² | Calls / scan | Latency mean · p50 · max | "
  "Input tok / scan | Output tok / scan | Thinking tok / scan | Cost / scan (INR) | Cost / 1,000 scans (INR) | Cost / scan (USD) |")
w("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for v in variants:
    s = summary[v]
    w(f"| {LABELS[v]} | `{MODEL_OF[v]}` | {THINK_OF[v]} | {s['accuracy']} | {s['stable']} | {s['calls_per_scan']:.1f} | "
      f"{s['lat_mean']:.1f}s · {s['lat_p50']:.1f}s · {s['lat_max']:.1f}s | {s['in']:,.0f} | {s['out']:,.0f} | "
      f"{s['think']:,.0f} | ₹{s['cost'] * USD_INR:.3f} | ₹{s['cost'] * 1000 * USD_INR:,.0f} | ${s['cost']:.4f} |")
w("")
w(f"¹ Correct 1-5 score over the {len(EXPECTED)} unambiguous images × {runs_n} runs "
  f"({len(EXPECTED) * runs_n} scans). The {len(UNLABELLED)} boundary images are excluded until labelled.  ")
w(f"² Images (of {n_img}) where all {runs_n} runs returned the same score and sub-code.  ")
w("Latency is wall time for the whole classification (both calls for the two-step variants), measured from "
  "this machine; it excludes the image download, DB write and HTTP overhead of `/manure/report`.\n")

w("## Pricing used\n")
w("Gemini API standard paid tier, per 1M tokens, from <https://ai.google.dev/gemini-api/docs/pricing> "
  "(read 2026-10-01). Thinking tokens are billed at the output rate; image tokens count as input.\n")
w(f"INR at **$1 = ₹{USD_INR}** (market rate on 2026-10-01, FXStreet). Google bills in USD, so the INR "
  "amounts move with the exchange rate.\n")
w("| Model | Input / 1M tokens | Output (incl. thinking) / 1M tokens | Note |")
w("|---|---|---|---|")
w(f"| `gemini-3.1-pro-preview` | ₹{2.00 * USD_INR:,.0f} ($2.00) | ₹{12.00 * USD_INR:,.0f} ($12.00) | prompts ≤ 200k tokens |")
w(f"| `gemini-3.8-flash` | ₹{0.75 * USD_INR:,.0f} ($0.75) | ₹{3.75 * USD_INR:,.0f} ($3.75) | promotional until 2026-12-31; "
  f"**₹{1.50 * USD_INR:,.0f} / ₹{7.50 * USD_INR:,.0f} ($1.50 / $7.50) from 2027-01-01** |")
w("")
w("Cost per 1,000 scans at 2027 prices: " + " · ".join(
    f"{LABELS[v]} **₹{summary[v]['cost27'] * 1000 * USD_INR:,.0f}**" for v in variants) + "\n")

w("## Per-image results\n")
w("Each cell lists the predicted `score:sub_code` and how many of the runs returned it.\n")
w("| Image | Expected score | " + " | ".join(LABELS[v] for v in variants) + " |")
w("|---|---|" + "---|" * len(variants))
for res in data["results"]:
    exp = EXPECTED.get(res["image"], UNLABELLED.get(res["image"], "?"))
    cells = []
    for v in variants:
        cnt = Counter(f"{r['score']}:{r['sub_code']}" for r in res["runs"][v])
        cell = "<br>".join(f"`{k}` ×{n}" for k, n in cnt.most_common())
        if res["image"] in EXPECTED and any(r["score"] != EXPECTED[res["image"]] for r in res["runs"][v]):
            cell += " ✗"
        cells.append(cell)
    w(f"| `{res['image']}` | {exp} | " + " | ".join(cells) + " |")
w("")

w("## Freshness gate (two-step variants)\n")
w("`is_fresh=false` makes `/manure/report` return 422 `NOT_FRESH`. Shown as fresh runs / total runs.\n")
w("| Image | " + " | ".join(LABELS[v] for v in variants[1:]) + " |")
w("|---|" + "---|" * (len(variants) - 1))
for res in data["results"]:
    w(f"| `{res['image']}` | " + " | ".join(
        f"{sum(bool(r['fresh']) for r in res['runs'][v])}/{len(res['runs'][v])}" for v in variants[1:]) + " |")
w("")

w("## Per-call token breakdown (two-step variants)\n")
w("Mean per call. Stage 1 = quality gate + tags + 1-5 score; stage 2 = sub-code within the score.\n")
w("| Variant | Stage | Input tokens (image part) | Output tokens | Thinking tokens | Latency | Cost (INR) |")
w("|---|---|---|---|---|---|---|")
for v in variants[1:]:
    for stage in (0, 1):
        calls = [s["calls"][stage] for res in data["results"] for s in res["runs"][v] if len(s["calls"]) > stage]
        img = statistics.mean(c["input_by_modality"].get("IMAGE", 0) for c in calls)
        w(f"| {LABELS[v]} | {stage + 1} | {statistics.mean(c['input_tokens'] for c in calls):,.0f} ({img:,.0f}) | "
          f"{statistics.mean(c['output_tokens'] for c in calls):,.0f} | {statistics.mean(c['thinking_tokens'] for c in calls):,.0f} | "
          f"{statistics.mean(c['latency_s'] for c in calls):.1f}s | ₹{statistics.mean(cost(MODEL_OF[v], c) for c in calls) * USD_INR:.3f} |")
w("")

w("## Method\n")
w("- Script: `bench_manure_v2.py` calls the service's own `_score` / `_sub_score` (two-step) and "
  "`ManureSubcategoryService.classify` (flat) with the production prompts, schemas and temperature, varying only "
  "the model and thinking level.")
w("- Token counts are Gemini's own `usage_metadata` for each call: `prompt_token_count` (input, incl. image), "
  "`candidates_token_count` (output) and `thoughts_token_count` (thinking).")
w("- Runs for one variant execute concurrently (max 6 in flight); variants run one after another.")
w("- Expected scores are a reviewer's visual judgement, not vet-verified labels. "
  "Two boundary images are deliberately left unlabelled.")
open(dst, "w", encoding="utf-8").write("\n".join(L) + "\n")
json.dump(summary, open(dst + ".summary.json", "w"), indent=1)
print(json.dumps(summary, indent=1))
