#!/usr/bin/env python3
"""Small-scale 'full mining' canary.

Runs the REAL similarity pipeline (fetch_similar_users + ingest + LLM ICP +
embeddings) over a random sample of 待联系 seeds — i.e. the population a full
mining run would expand from — WITHOUT creating a similarity_jobs row, so it
never collides with the UI's single-job lock.

It measures the only numbers our cost model can't predict on paper:
  • real candidates returned per seed
  • how many survive pre-dedup → actual hydrate calls (TikHub $)
  • real OpenRouter token usage for ICP chat + embeddings ($)

…then extrapolates to the full eligible pool.

Writes real rows to scout.db (newly discovered accounts) and spends real
TikHub + OpenRouter credit — that's the point of a canary. Keep N small.

Usage (run from the app root, e.g. /opt/ig_scraper):
    .venv/bin/python tools/similarity_canary.py [N] [status]
    N      : number of seeds to sample (default 12)
    status : CRM status to sample from   (default 待联系)
"""
import random
import sys
import time

from lib import ai, bulk_similarity, settings

# --- prices (USD) -----------------------------------------------------------
TIKHUB = {
    ("instagram", "similar"): 0.002,
    ("tiktok", "similar"): 0.001,
    ("instagram", "hydrate"): 0.001,
    ("tiktok", "hydrate"): 0.001,
}
OR_CHAT_IN = 0.0983 / 1_000_000   # deepseek/deepseek-v4-flash, input
OR_CHAT_OUT = 0.1966 / 1_000_000  # deepseek/deepseek-v4-flash, output
OR_EMBED = 0.01 / 1_000_000       # qwen/qwen3-embedding-8b

# --- OpenRouter usage capture (monkeypatch) ---------------------------------
U = {"chat_in": 0, "chat_out": 0, "chat_calls": 0, "embed_tok": 0, "embed_calls": 0}

_orig_try_chat = ai._try_chat
def _try_chat(client, model, messages, temperature):
    try:
        resp = client.chat.completions.create(
            model=model, messages=messages, temperature=temperature)
    except Exception:
        return None
    u = getattr(resp, "usage", None)
    if u:
        U["chat_in"] += getattr(u, "prompt_tokens", 0) or 0
        U["chat_out"] += getattr(u, "completion_tokens", 0) or 0
    U["chat_calls"] += 1
    content = (resp.choices[0].message.content or "").strip()
    return content or None
ai._try_chat = _try_chat

_orig_openrouter = ai._openrouter
def _openrouter(timeout=15.0):
    client = _orig_openrouter(timeout)
    _orig_embed_create = client.embeddings.create
    def wrapped(*a, **k):
        resp = _orig_embed_create(*a, **k)
        u = getattr(resp, "usage", None)
        if u:
            U["embed_tok"] += getattr(u, "total_tokens", 0) or getattr(u, "prompt_tokens", 0) or 0
        U["embed_calls"] += 1
        return resp
    client.embeddings.create = wrapped
    return client
ai._openrouter = _openrouter


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    status = sys.argv[2] if len(sys.argv) > 2 else "待联系"

    print(f"settings: chat={settings.get('OPENROUTER_CHAT_MODEL')!r} "
          f"embed={settings.get('OPENROUTER_EMBED_MODEL')!r} "
          f"llm_filter={settings.get('LLM_FILTER_ENABLED')!r}")

    pool = bulk_similarity.list_eligible_seeds(statuses=[status])
    print(f"eligible '{status}' seeds: {len(pool)}  → sampling {n}")
    if not pool:
        print("no seeds; abort"); return
    sample = random.sample(pool, min(n, len(pool)))

    progress = bulk_similarity._blank_progress()
    seed_plat = {"instagram": 0, "tiktok": 0}
    t_start = time.time()
    for i, seed in enumerate(sample):
        seed_plat[seed["platform"]] = seed_plat.get(seed["platform"], 0) + 1
        entry = {"seed_id": seed.get("aid"), "idx": i,
                 "username": seed.get("username", "?"),
                 "platform": seed.get("platform", "?"),
                 "new": 0, "considered": 0, "error": None, "duration_ms": 0}
        before = dict(progress)
        t0 = time.time()
        try:
            bulk_similarity._process_one_seed(seed, progress, entry)
        except Exception as e:
            entry["error"] = str(e)[:200]
            progress["seed_errors"] += 1
        entry["duration_ms"] = int((time.time() - t0) * 1000)
        cand = progress["considered"] - before["considered"]
        dd = progress["skipped_pre"] - before["skipped_pre"]
        hyd = cand - dd
        print(f"  #{i+1:>2} @{entry['username'][:22]:22} {entry['platform']:9} "
              f"cand={cand:>3} dedup={dd:>3} hydrate={hyd:>3} "
              f"new={entry['new']:>3} {entry['duration_ms']:>6}ms"
              + (f"  ERR:{entry['error']}" if entry["error"] else ""))

    elapsed = time.time() - t_start
    p = progress
    total_hydrate = p["considered"] - p["skipped_pre"]
    ok_seeds = len(sample) - p["seed_errors"]

    # --- measured cost for THIS run ---
    similar_cost = sum(TIKHUB[(plat, "similar")] * cnt for plat, cnt in seed_plat.items() if cnt)
    hydrate_cost = total_hydrate * 0.001   # both platforms $0.001
    tikhub_cost = similar_cost + hydrate_cost
    chat_cost = U["chat_in"] * OR_CHAT_IN + U["chat_out"] * OR_CHAT_OUT
    embed_cost = U["embed_tok"] * OR_EMBED
    or_cost = chat_cost + embed_cost

    print("\n===== SAMPLE TOTALS =====")
    print(f"seeds run: {len(sample)} ({seed_plat})  errors: {p['seed_errors']}  elapsed: {elapsed:.0f}s")
    print(f"candidates considered : {p['considered']}")
    print(f"  pre-dedup (in db)   : {p['skipped_pre']}")
    print(f"  → hydrated (TikHub) : {total_hydrate}")
    print(f"  same-person dedup   : {p['skipped_email']}")
    print(f"  LLM filtered out    : {p['llm_filtered']}")
    print(f"  new accounts ingest : {p['new']}   cross-platform: {p['cross_platform']}")
    print(f"  hydrate errors      : {p['hydrate_errors']}")
    if ok_seeds:
        print(f"\nper-seed avg (ok seeds={ok_seeds}): "
              f"cand={p['considered']/ok_seeds:.1f}  hydrate={total_hydrate/ok_seeds:.1f}  "
              f"new={p['new']/ok_seeds:.1f}  {elapsed/len(sample):.1f}s/seed")
    print(f"\nOpenRouter usage: chat {U['chat_calls']} calls "
          f"(in {U['chat_in']} / out {U['chat_out']} tok)  "
          f"embed {U['embed_calls']} calls ({U['embed_tok']} tok)")
    print(f"\ncost THIS run: TikHub ${tikhub_cost:.4f} "
          f"(similar ${similar_cost:.4f} + hydrate ${hydrate_cost:.4f})  "
          f"OpenRouter ${or_cost:.4f} (chat ${chat_cost:.4f} + embed ${embed_cost:.4f})  "
          f"TOTAL ${tikhub_cost+or_cost:.4f}")

    # --- extrapolate to full eligible pool ---
    if ok_seeds and total_hydrate:
        full = bulk_similarity.list_eligible_seeds(statuses=[status])
        full_ig = sum(1 for s in full if s["platform"] == "instagram")
        full_tt = sum(1 for s in full if s["platform"] == "tiktok")
        full_n = len(full)
        avg_hyd = total_hydrate / ok_seeds
        f_similar = full_ig * 0.002 + full_tt * 0.001
        f_hydrate = avg_hyd * full_n * 0.001
        # OpenRouter scales with hydrated candidates
        or_per_hyd = or_cost / total_hydrate
        f_or = or_per_hyd * avg_hyd * full_n
        f_total = f_similar + f_hydrate + f_or
        print(f"\n===== EXTRAPOLATION → full '{status}' pool ({full_n}: "
              f"IG {full_ig} / TT {full_tt}) =====")
        print(f"assuming avg hydrate {avg_hyd:.1f}/seed (from sample):")
        print(f"  TikHub  : similar ${f_similar:.0f} + hydrate ${f_hydrate:.0f} = ${f_similar+f_hydrate:.0f}")
        print(f"  OpenRouter (${or_per_hyd*1000:.4f}/1k hydrated) : ${f_or:.0f}")
        print(f"  >>> FULL TOTAL ≈ ${f_total:.0f}")
        est_hours = (full_n + avg_hyd * full_n) / 4 / 3600  # 4 RPS global cap
        print(f"  runtime @4 RPS cap ≈ {est_hours:.0f}h ({est_hours/24:.1f} days)")


if __name__ == "__main__":
    main()
