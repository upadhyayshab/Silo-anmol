# Manure Report — Model & Latency Benchmark

_Run 2026-10-01 · 7 images from `Gau-Gopalan-Seva/testing_img/` · 3 runs per image per variant · 84 scans in total · temperature 0 · KB = the 27 sub-codes of `master_table_v2.csv`._

## Recommendation

**Use the two-step flow on `gemini-3.8-flash` with LOW thinking** (now set in `app/services/manureReportService.py`).

- **Accuracy:** 15/15 correct scores on the unambiguous images. It is tied with 3.1 Pro LOW, and both are far ahead of the old flat call (3/15). The flat call put both watery Score 1 images at Score 2, as reported.
- **Latency:** 5.8s mean, 7.7s worst case. That is ~1.5× faster than 3.1 Pro LOW (8.6s) and ~2× faster than the old flat call (11.1s).
- **Cost:** about ₹0.51 per scan (₹512 per 1,000 scans), which is ~2.7× cheaper than 3.1 Pro LOW (₹1,358 per 1,000) and ~5× cheaper than the old flat call (₹2,645 per 1,000). **It doubles to ~₹1.02 per scan (₹1,023 per 1,000) on 2027-01-01**, when the 3.8 Flash promo price ends. It is still the cheapest option then.
- **Default thinking on 3.8 Flash is worse** (13/15, only 4/7 images stable). It is also slower and pricier than LOW, so pin LOW.
- **Still open:** two boundary images flip between runs on every model (`circular-shaped…`: 3 vs 5; `images (3).jpg`: 1-2 / 2). They need a team label before the boundary rules can be tuned.
- **Not validated:** the freshness gate passed every image on every model. That includes `circular-shaped…`, which looks weathered, so the gate is not proven to reject anything yet.

## Summary

| Variant | Model | Thinking | Score accuracy¹ | Stable² | Calls / scan | Latency mean · p50 · max | Input tok / scan | Output tok / scan | Thinking tok / scan | Cost / scan (INR) | Cost / 1,000 scans (INR) | Cost / scan (USD) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Old: single flat 27-way call | `gemini-3.1-pro-preview` | default | 3/15 (20%) | 7/7 | 1.0 | 11.1s · 10.2s · 20.2s | 7,670 | 76 | 944 | ₹2.645 | ₹2,645 | $0.0276 |
| Two-step · 3.1 Pro · LOW thinking | `gemini-3.1-pro-preview` | LOW | 15/15 (100%) | 6/7 | 2.0 | 8.6s · 8.1s · 13.6s | 5,477 | 267 | 0 | ₹1.358 | ₹1,358 | $0.0142 |
| Two-step · 3.8 Flash · default thinking | `gemini-3.8-flash` | default | 13/15 (87%) | 4/7 | 2.0 | 10.0s · 8.8s · 19.3s | 5,303 | 299 | 1,200 | ₹0.921 | ₹921 | $0.0096 |
| Two-step · 3.8 Flash · LOW thinking (now in code) | `gemini-3.8-flash` | LOW | 15/15 (100%) | 6/7 | 2.0 | 5.8s · 5.5s · 7.7s | 5,390 | 301 | 44 | ₹0.512 | ₹512 | $0.0053 |

¹ Correct 1-5 score over the 5 unambiguous images × 3 runs (15 scans). The 2 boundary images are excluded until labelled.  
² Images (of 7) where all 3 runs returned the same score and sub-code.  
Latency is wall time for the whole classification (both calls for the two-step variants), measured from this machine; it excludes the image download, DB write and HTTP overhead of `/manure/report`.

## Pricing used

Gemini API standard paid tier, per 1M tokens, from <https://ai.google.dev/gemini-api/docs/pricing> (read 2026-10-01). Thinking tokens are billed at the output rate; image tokens count as input.

INR at **$1 = ₹95.93** (market rate on 2026-10-01, FXStreet). Google bills in USD, so the INR amounts move with the exchange rate.

| Model | Input / 1M tokens | Output (incl. thinking) / 1M tokens | Note |
|---|---|---|---|
| `gemini-3.1-pro-preview` | ₹192 ($2.00) | ₹1,151 ($12.00) | prompts ≤ 200k tokens |
| `gemini-3.8-flash` | ₹72 ($0.75) | ₹360 ($3.75) | promotional until 2026-12-31; **₹144 / ₹719 ($1.50 / $7.50) from 2027-01-01** |

Cost per 1,000 scans at 2027 prices: Old: single flat 27-way call **₹2,645** · Two-step · 3.1 Pro · LOW thinking **₹1,358** · Two-step · 3.8 Flash · default thinking **₹1,841** · Two-step · 3.8 Flash · LOW thinking (now in code) **₹1,023**

## Per-image results

Each cell lists the predicted `score:sub_code` and how many of the runs returned it.

| Image | Expected score | Old: single flat 27-way call | Two-step · 3.1 Pro · LOW thinking | Two-step · 3.8 Flash · default thinking | Two-step · 3.8 Flash · LOW thinking (now in code) |
|---|---|---|---|---|---|
| `circular-shaped-cow-dung-small-260nw-2642702011.webp` | 3 or 4? (dip + pitted surface) | `3:3-G-O` ×3 | `5:5-DRY` ×3 | `3:3-G-O` ×2<br>`5:5-DRY` ×1 | `5:5-DRY` ×2<br>`3:3-G-O` ×1 |
| `close-up-shot-fresh-cow-dung-forest-uttarakhand-india-255967705.webp` | 5 | `4:4-S-W` ×3 ✗ | `5:5-DRY` ×3 | `5:5-DRY` ×3 | `5:5-DRY` ×3 |
| `images (2).jpg` | 1 | `2:2-YEL-A` ×3 ✗ | `1:1-YEL` ×3 | `1:1-YEL` ×2<br>`2:2-YEL-A` ×1 ✗ | `1:1-YEL` ×3 |
| `images (3).jpg` | 1 or 2? (flat spread, holds outline) | `2:2-P` ×3 | `1:1-P` ×2<br>`2:2-GRN-S` ×1 | `2:2-GRN-S` ×3 | `2:2-GRN-S` ×3 |
| `images (5).jpg` | 3 | `3:3-S-O` ×3 | `3:3-S-O` ×3 | `3:3-S-O` ×3 | `3:3-S-O` ×3 |
| `images (6).jpg` | 1 | `2:2-YEL-A` ×3 ✗ | `1:1-YEL` ×3 | `1:1-YEL` ×2<br>`2:2-GRN-S` ×1 ✗ | `1:1-YEL` ×3 |
| `istockphoto-1444657605-1024x1024.jpg` | 4 | `3:3-S-O` ×3 ✗ | `4:4-S-W` ×3 | `4:4-S-W` ×3 | `4:4-S-W` ×3 |

## Freshness gate (two-step variants)

`is_fresh=false` makes `/manure/report` return 422 `NOT_FRESH`. Shown as fresh runs / total runs.

| Image | Two-step · 3.1 Pro · LOW thinking | Two-step · 3.8 Flash · default thinking | Two-step · 3.8 Flash · LOW thinking (now in code) |
|---|---|---|---|
| `circular-shaped-cow-dung-small-260nw-2642702011.webp` | 3/3 | 2/3 | 3/3 |
| `close-up-shot-fresh-cow-dung-forest-uttarakhand-india-255967705.webp` | 3/3 | 3/3 | 3/3 |
| `images (2).jpg` | 3/3 | 3/3 | 3/3 |
| `images (3).jpg` | 3/3 | 3/3 | 3/3 |
| `images (5).jpg` | 3/3 | 3/3 | 3/3 |
| `images (6).jpg` | 3/3 | 3/3 | 3/3 |
| `istockphoto-1444657605-1024x1024.jpg` | 3/3 | 3/3 | 3/3 |

## Per-call token breakdown (two-step variants)

Mean per call. Stage 1 = quality gate + tags + 1-5 score; stage 2 = sub-code within the score.

| Variant | Stage | Input tokens (image part) | Output tokens | Thinking tokens | Latency | Cost (INR) |
|---|---|---|---|---|---|---|
| Two-step · 3.1 Pro · LOW thinking | 1 | 1,959 (1,089) | 189 | 0 | 4.6s | ₹0.594 |
| Two-step · 3.1 Pro · LOW thinking | 2 | 3,518 (1,089) | 77 | 0 | 4.0s | ₹0.764 |
| Two-step · 3.8 Flash · default thinking | 1 | 1,959 (1,089) | 223 | 729 | 6.0s | ₹0.483 |
| Two-step · 3.8 Flash · default thinking | 2 | 3,344 (1,089) | 76 | 471 | 4.0s | ₹0.437 |
| Two-step · 3.8 Flash · LOW thinking (now in code) | 1 | 1,959 (1,089) | 223 | 44 | 3.0s | ₹0.237 |
| Two-step · 3.8 Flash · LOW thinking (now in code) | 2 | 3,431 (1,089) | 78 | 0 | 2.8s | ₹0.275 |

## Method

- Script: `docs/benchmarks/bench_manure.py` (report: `make_report.py`, raw data: `manure_model_benchmark_raw.json`) calls the service's own `_score` / `_sub_score` (two-step) and `ManureSubcategoryService.classify` (flat) with the production prompts, schemas and temperature, varying only the model and thinking level.
- Token counts are Gemini's own `usage_metadata` for each call: `prompt_token_count` (input, incl. image), `candidates_token_count` (output) and `thoughts_token_count` (thinking).
- Runs for one variant execute concurrently (max 6 in flight); variants run one after another.
- Expected scores are a reviewer's visual judgement, not vet-verified labels. Two boundary images are deliberately left unlabelled.

Re-run from `Gau-Gopalan-Seva/app`:

```bash
PYTHONPATH=".;../SharedBackend/src" RUNS=3 OUT=../docs/benchmarks/manure_model_benchmark_raw.json python ../docs/benchmarks/bench_manure.py
python ../docs/benchmarks/make_report.py ../docs/benchmarks/manure_model_benchmark_raw.json ../docs/benchmarks/MANURE_MODEL_BENCHMARK.md
```
