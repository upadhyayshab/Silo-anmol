"""Latency / token / cost benchmark of manure classification on testing_img/.
Run from Gau-Gopalan-Seva/app with PYTHONPATH=.;../SharedBackend/src

Variants:
  flat         — old single 27-way call (ManureSubcategoryService, /detect/manure/subcategory)
  two_default  — new two-step, model-default thinking
  two_low      — new two-step, LOW thinking (current setting)
"""
import asyncio
import contextvars
import csv
import json
import os
import sys
import time
from types import SimpleNamespace

from google.genai import types

sys.stdout.reconfigure(encoding="utf-8")
import services.manureReportService as mrs
from services.manureSubcategoryService import ManureSubcategoryService

RUNS = int(os.environ.get("RUNS", "3"))
DIR = "../testing_img"
OUT = os.environ["OUT"]
LOW = types.ThinkingLevel.LOW
# variant -> (model, stage-1 thinking, stage-2 thinking)
VARIANTS = {
    "two_pro31_low": ("gemini-3.1-pro-preview", LOW, LOW),
    "two_flash38_default": ("gemini-3.8-flash", None, None),
    "two_flash38_low": ("gemini-3.8-flash", LOW, LOW),
}

# KB rows from the seed CSV (identical to manure_kb) so the benchmark needs no DB.
KB = [SimpleNamespace(**{k.lower(): v for k, v in r.items()}, uid=r["Sub_Code"], updated_at=None)
      for r in csv.DictReader(open("../migrations/data/master_table_v2.csv", encoding="utf-8", newline=""))]
for r in KB:
    r.score = int(r.score)

# Every generate_content call records its usage into the calling task's list.
_calls: contextvars.ContextVar[list] = contextvars.ContextVar("calls")


def instrument(client):
    original = client.aio.models.generate_content

    async def wrapped(*args, **kwargs):
        t0 = time.monotonic()
        response = await original(*args, **kwargs)
        u = response.usage_metadata
        _calls.get().append({
            "latency_s": time.monotonic() - t0,
            "input_tokens": u.prompt_token_count or 0,
            "output_tokens": u.candidates_token_count or 0,
            "thinking_tokens": u.thoughts_token_count or 0,
            "total_tokens": u.total_token_count or 0,
            "input_by_modality": {str(d.modality.value if hasattr(d.modality, "value") else d.modality): d.token_count
                                  for d in (u.prompt_tokens_details or [])},
        })
        return response

    client.aio.models.generate_content = wrapped


svc, flat = mrs.ManureReportService(), ManureSubcategoryService()
instrument(svc._client)
instrument(flat._client)
sem = asyncio.Semaphore(6)


async def run_flat(b, mt):
    async with sem:
        _calls.set([])
        t0 = time.monotonic()
        r = await flat.classify(b, mt)
        return {"score": r["score"], "sub_code": r["sub_code"], "fresh": None,
                "wall_s": time.monotonic() - t0, "calls": _calls.get()}


async def run_two(b, mt, variant):
    async with sem:
        _calls.set([])
        mrs.MODEL, mrs.SCORE_THINKING_LEVEL, mrs.SUB_SCORE_THINKING_LEVEL = VARIANTS[variant]
        t0 = time.monotonic()
        s = await svc._score(b, mt, "en")
        m, _ = await svc._sub_score(b, mt, s.manure_score, [r for r in KB if r.score == s.manure_score])
        return {"score": s.manure_score, "sub_code": m.sub_code, "fresh": s.is_fresh, "is_manure": s.is_manure,
                "tags": [t.model_dump() for t in s.visual_evidence_as_tags],
                "wall_s": time.monotonic() - t0, "calls": _calls.get()}


async def main():
    results = []
    for name in sorted(os.listdir(DIR)):
        b = open(os.path.join(DIR, name), "rb").read()
        mt = "image/webp" if name.endswith("webp") else "image/jpeg"
        runs = {"flat": await asyncio.gather(*(run_flat(b, mt) for _ in range(RUNS)))}
        # Variants run one after another: the thinking level is module-global.
        for v in VARIANTS:
            runs[v] = await asyncio.gather(*(run_two(b, mt, v) for _ in range(RUNS)))
        results.append({"image": name, "bytes": len(b), "runs": runs})
        print(name, {v: [f"{r['score']}:{r['sub_code']}" for r in rs] for v, rs in runs.items()}, flush=True)
    json.dump({"variants": {k: [v[0], str(v[1]), str(v[2])] for k, v in VARIANTS.items()},
               "flat_model": "gemini-3.1-pro-preview", "runs_per_image": RUNS, "results": results},
              open(OUT, "w"), indent=1)

asyncio.run(main())
