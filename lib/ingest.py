"""Shared ingestion pipeline used by both `/api/similarity/run` and `/api/query/run`.

Inputs: a list of `(normalized_lite, source)` candidates from upstream platform
calls, where `normalized_lite` is the search/similar response (no email yet).

Output: a structured report:
    {
        "new_accounts":       [account_dict, ...]   # platform_accounts truly new
        "cross_platform":     [{"account": {...}, "person_id": int,
                                "other_accounts": [{...}, ...]}, ...]
                              # account is new on this platform but the person
                              # was already known via another platform (matched by email)
        "skipped_pre":        int   # candidate dropped by (platform, platform_user_id) DB hit
        "skipped_email":      int   # candidate dropped by email match (we already know this person + already have this platform)
        "hydrate_errors":     [{"username": ..., "error": ...}, ...]
        "considered":         int   # raw candidate count
    }

The pipeline:
  1) Pre-dedup candidates by (platform, platform_user_id) against DB and within batch.
  2) Apply per-platform quota distribution to honor `target_count` fairly.
  3) Hydrate each candidate (full fetch_user_info) in parallel.
  4) Post-hydrate: if hydrated email matches an existing person AND that person
     already owns an account on the same platform, drop as duplicate. Otherwise
     upsert and classify as new vs cross-platform link.
  5) Schedule embedding for the new bios.
"""
from __future__ import annotations
import math
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import db, ai, settings
from platforms import get as get_platform


def _llm_filter_enabled() -> bool:
    """Truthy-string check on LLM_FILTER_ENABLED. Anything in this set turns
    the gate on; everything else (incl. empty) leaves it off. Defined here
    so ingest is the single decision point — the worker / routes don't each
    re-implement the rule."""
    raw = (settings.get("LLM_FILTER_ENABLED") or "").strip().lower()
    return raw in {"1", "on", "true", "yes", "y"}


# ---------- rejection-pool cache (negative-sample filter) ----------

# Threshold: a candidate whose bio embedding is closer than this to ANY rejected
# account is dropped. 0.88 is empirically near "obvious dup" for OpenAI embeds.
REJECTED_COSINE_THRESHOLD = 0.88

_REJ_LOCK = threading.Lock()
_REJ_CACHE: dict = {"signature": None, "vectors": [], "matrix": None}
# vectors: list[(account_id, username, vec)]
# matrix:  unit-normalised float32 (n_pool, dim) numpy array for the fast path,
#          or None when numpy is unavailable (keep _load_rejection_pool in sync).


def _rejection_pool_signature() -> tuple[int, int]:
    """Cheap cache key: (count, max_rejected_at). Changes when pool is mutated."""
    with db.ro() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(rejected_at),0) AS m "
            "FROM platform_accounts WHERE is_rejected=1 AND bio_embedding IS NOT NULL"
        ).fetchone()
    return (row["n"], row["m"])


def _load_rejection_pool() -> list[tuple[int, str, list[float] | None]]:
    sig = _rejection_pool_signature()
    with _REJ_LOCK:
        if _REJ_CACHE["signature"] == sig:
            return _REJ_CACHE["vectors"]
        with db.tx() as c:
            rows = c.execute(
                "SELECT id, username, bio_embedding FROM platform_accounts "
                "WHERE is_rejected=1 AND bio_embedding IS NOT NULL"
            ).fetchall()

        blobs = [r["bio_embedding"] for r in rows]
        matrix = ai.matrix_from_packed(blobs)
        if matrix is not None:
            # Fast path available. Do NOT unpack the vectors into Python float
            # lists: 5063 x 4096 floats is ~633MB of RSS (each float is its own
            # object) vs 79MB for the float32 matrix, and the matmul never
            # touches the lists. Only id + username are needed for reporting.
            vecs: list[tuple[int, str, list[float] | None]] = [
                (r["id"], r["username"] or "", None) for r in rows
            ]
        else:
            # No numpy (or ragged blobs): the per-pair cosine fallback needs the
            # unpacked vectors, so pay the memory.
            vecs = [
                (r["id"], r["username"] or "", db.unpack_embedding(b))
                for r, b in zip(rows, blobs)
            ]
        _REJ_CACHE["signature"] = sig
        _REJ_CACHE["vectors"] = vecs
        _REJ_CACHE["matrix"] = matrix
        return vecs


def _filter_against_rejected(candidate_norms: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (kept, rejected_filtered). `rejected_filtered` items carry the
    matched-against username + score so callers can show what got pruned.

    Embeds candidates' bios on the fly using the same text that ingest later
    persists (`_embed_text_for`). Skips silently if no rejected pool or no text.
    """
    pool = _load_rejection_pool()
    if not pool or not candidate_norms:
        return candidate_norms, []
    texts = [_embed_text_for({
        "full_name": n.get("full_name", ""),
        "biography": n.get("biography", ""),
        "category":  n.get("category", ""),
    }) for n in candidate_norms]
    # Mark candidates with no usable text — keep them (we can't judge).
    indices_with_text = [i for i, t in enumerate(texts) if t]
    if not indices_with_text:
        return candidate_norms, []
    try:
        cand_vecs = ai.embed_texts([texts[i] for i in indices_with_text])
    except Exception:
        # Embedding failure shouldn't block discovery; keep everything.
        return candidate_norms, []
    vec_by_idx = dict(zip(indices_with_text, cand_vecs))
    kept: list[dict] = []
    rejected_filtered: list[dict] = []

    # Fast path: one matmul for the whole batch against the cached pool matrix
    # (9.6 ms for 5k pool rows vs ~20 s for the per-pair cosine loop).
    # best_score/best_match semantics are identical — 0.0 / "" when nothing
    # scores above zero, first maximum on ties.
    fast = None
    if ai.numpy_available():
        matrix = _REJ_CACHE.get("matrix")
        if matrix is not None:
            q_idx = [i for i in indices_with_text if vec_by_idx.get(i)]
            res = ai.best_match([vec_by_idx[i] for i in q_idx], matrix)
            if res is not None:
                fast = dict(zip(q_idx, zip(res[0], res[1])))

    if fast is None and pool and pool[0][2] is None:
        # The pool was cached in matrix-only form (see _load_rejection_pool) but
        # the fast path could not run — e.g. the candidate embeddings no longer
        # match the pool's dimension. We cannot score anything, so keep
        # everything: a similarity filter must never silently drop creators.
        return candidate_norms, []

    for i, n in enumerate(candidate_norms):
        v = vec_by_idx.get(i)
        if not v:
            kept.append(n)
            continue
        if fast is not None:
            score, pool_pos = fast[i]
            if score > 0:
                best_score, best_match = score, pool[pool_pos][1]
            else:
                best_score, best_match = 0.0, ""
        else:
            best_score = 0.0
            best_match = ""
            for _aid, uname, rv in pool:
                score = ai.cosine(v, rv)
                if score > best_score:
                    best_score = score
                    best_match = uname
        if best_score > REJECTED_COSINE_THRESHOLD:
            rejected_filtered.append({
                "username": n.get("username") or "?",
                "platform": n.get("platform"),
                "matched_against_username": best_match,
                "score": round(best_score, 4),
            })
        else:
            kept.append(n)
    return kept, rejected_filtered


def _filter_against_icp_llm(candidate_norms: list[dict]) -> tuple[list[dict], list[dict]]:
    """Positive ICP gatekeeper — call OpenRouter on each hydrated bio and
    drop accounts the LLM judges as off-target.

    Settings-gated (LLM_FILTER_ENABLED). When off, returns (input, []).
    When on but the LLM call fails, ai.classify_creators() falls back to
    all-keep, so a flaky LLM never blocks ingest. The dropped rows surface
    in `llm_filtered` on the report for transparency."""
    if not candidate_norms or not _llm_filter_enabled():
        return candidate_norms, []
    brief = settings.get("LLM_FILTER_BRIEF") or ""
    if not brief.strip():
        return candidate_norms, []
    payload = [
        {
            "username": n.get("username", ""),
            "full_name": n.get("full_name", ""),
            "bio": n.get("biography", ""),
            "category": n.get("category", ""),
        }
        for n in candidate_norms
    ]
    verdicts = ai.classify_creators(payload, brief)
    kept: list[dict] = []
    dropped: list[dict] = []
    for n in candidate_norms:
        uname = n.get("username") or ""
        # default-keep: any account the model didn't mention (e.g. dropped
        # from the response by accident) survives
        if verdicts.get(uname, True):
            kept.append(n)
        else:
            dropped.append({
                "username": uname,
                "platform": n.get("platform"),
                "full_name": n.get("full_name", ""),
            })
    return kept, dropped


def distribute_quota(candidates_by_platform: dict[str, list], target: int) -> dict[str, int]:
    """Allocate cap fairly across platforms; underflow on one redistributed to others."""
    if target <= 0 or not candidates_by_platform:
        return {p: 0 for p in candidates_by_platform}
    plats = list(candidates_by_platform.keys())
    base = target // len(plats)
    remainder = target - base * len(plats)
    quota = {p: base for p in plats}
    # Round-robin the remainder by platform alpha order (stable)
    for p in sorted(plats)[:remainder]:
        quota[p] += 1
    # Redistribute excess from short platforms to ones with leftover candidates
    while True:
        slack = 0
        short = []
        for p in plats:
            avail = len(candidates_by_platform[p])
            if quota[p] > avail:
                slack += quota[p] - avail
                quota[p] = avail
            elif quota[p] < avail:
                short.append(p)
        if slack == 0 or not short:
            break
        # Distribute slack across short platforms
        bumped = 0
        for p in short:
            avail = len(candidates_by_platform[p])
            room = avail - quota[p]
            take = min(room, slack - bumped)
            quota[p] += take
            bumped += take
            if bumped >= slack:
                break
        if bumped == 0:
            break
    return quota


def _hydrate(item: tuple[dict, str]) -> tuple[dict, str, str | None]:
    """Fetch full info for a candidate. Returns (final_norm, source, error_or_none)."""
    norm, src = item
    plat = norm["platform"]
    handle = norm.get("username") or ""
    if not handle:
        return norm, src, "missing username"
    try:
        full = get_platform(plat).fetch_user_info(handle)
        return full, src, None
    except Exception as e:
        return norm, src, str(e)


def run(candidates: list[tuple[dict, str]], target_count: int, hydrate: bool = True) -> dict:
    # -- 1. Pre-dedup against DB + within batch --
    seen_keys: set[tuple[str, str]] = set()
    pre_kept: list[tuple[dict, str]] = []
    skipped_pre = 0
    with db.tx() as c:
        for norm, src in candidates:
            key = (norm["platform"], str(norm.get("platform_user_id") or ""))
            if not key[1] or key in seen_keys:
                skipped_pre += 1
                continue
            seen_keys.add(key)
            if db.has_account(c, key[0], key[1]):
                skipped_pre += 1
                continue
            pre_kept.append((norm, src))

    # -- 2. Per-platform quota --
    by_plat: dict[str, list[tuple[dict, str]]] = defaultdict(list)
    for item in pre_kept:
        by_plat[item[0]["platform"]].append(item)
    quota = distribute_quota(by_plat, target_count)
    selected: list[tuple[dict, str]] = []
    for p, items in by_plat.items():
        selected.extend(items[: quota.get(p, 0)])

    # -- 3. Hydrate in parallel --
    hydrate_errors: list[dict] = []
    if hydrate and selected:
        with ThreadPoolExecutor(max_workers=4) as ex:
            hydrated_raw = list(ex.map(_hydrate, selected))
    else:
        hydrated_raw = [(n, s, None) for (n, s) in selected]

    # -- 3.5 Negative-sample filter (drop candidates near rejected pool) --
    if hydrated_raw:
        norms_only = [n for n, _, _ in hydrated_raw]
        kept_norms, rejected_filtered = _filter_against_rejected(norms_only)
        kept_set = {id(n) for n in kept_norms}
        hydrated_raw = [(n, s, e) for (n, s, e) in hydrated_raw if id(n) in kept_set]
    else:
        rejected_filtered = []

    # -- 3.6 Positive ICP filter (LLM gatekeeper, settings-gated) --
    # Runs *after* the rejected-pool filter so the LLM only judges accounts
    # we'd otherwise ingest — saves a few LLM tokens on obvious negatives.
    if hydrated_raw:
        norms_only = [n for n, _, _ in hydrated_raw]
        kept_norms, llm_filtered = _filter_against_icp_llm(norms_only)
        kept_set = {id(n) for n in kept_norms}
        hydrated_raw = [(n, s, e) for (n, s, e) in hydrated_raw if id(n) in kept_set]
    else:
        llm_filtered = []

    # -- 4. Upsert with email-aware dedup --
    new_accounts: list[dict] = []
    cross_platform: list[dict] = []
    skipped_email = 0
    fresh_account_ids: list[int] = []

    with db.tx() as c:
        for norm, src, err in hydrated_raw:
            if err:
                hydrate_errors.append({"username": norm.get("username", "?"), "platform": norm.get("platform"), "error": err})
                # Use the lite record so we still capture the find
            email = (norm.get("email") or "").lower().strip()
            existing_person_id = db.find_person_by_email(c, email) if email else None
            already_on_platform = False
            if existing_person_id:
                # Does that person already own an account on this same platform?
                rows = db.list_accounts_for_person_light(c, existing_person_id)
                already_on_platform = any(r["platform"] == norm["platform"] for r in rows)

            if existing_person_id and already_on_platform:
                skipped_email += 1
                continue

            # Upsert (email or platform_id will route persons correctly)
            try:
                acct_id, person_id, created = db.upsert_account(c, norm, source=src)
            except Exception as e:
                hydrate_errors.append({
                    "username": norm.get("username", "?"),
                    "platform": norm.get("platform"),
                    "error": f"upsert failed: {e}",
                })
                continue

            row = db.get_account_light(c, acct_id)
            account_dict = db.row_to_dict(row)
            fresh_account_ids.append(acct_id)

            if existing_person_id and existing_person_id == person_id:
                # Email matched an existing CRM person. Two sub-cases:
                #   a. that person was email-only (no handles before) → "新关联到已有客户"
                #   b. that person already had handles on other platforms → 真正的跨平台发现
                others = [
                    db.row_to_dict(r)
                    for r in db.list_accounts_for_person_light(c, person_id)
                    if r["id"] != acct_id
                ]
                person_row = db.get_person(c, person_id)
                cross_platform.append({
                    "account": account_dict,
                    "person_id": person_id,
                    "person_email": person_row["email"] if person_row else None,
                    "person_full_name": person_row["full_name"] if person_row else None,
                    "person_status": person_row["status"] if person_row else None,
                    "other_accounts": others,
                })
            else:
                new_accounts.append(account_dict)

    # -- 5. Embed new bios (best effort, async-style sync) --
    try:
        _embed_accounts(fresh_account_ids)
    except Exception:
        pass

    return {
        "considered": len(candidates),
        "skipped_pre": skipped_pre,
        "skipped_email": skipped_email,
        "new_accounts": new_accounts,
        "cross_platform": cross_platform,
        "hydrate_errors": hydrate_errors,
        "rejected_filtered": rejected_filtered,
        "llm_filtered": llm_filtered,
        "quota": quota,
    }


# ---------- Embedding helper (kept here so app.py stays slim) ----------

def _embed_text_for(account_dict: dict) -> str:
    parts = [
        account_dict.get("full_name", "") or "",
        account_dict.get("biography", "") or "",
        account_dict.get("category", "") or "",
    ]
    return " — ".join(p for p in parts if p)


def _embed_accounts(account_ids: list[int]) -> None:
    if not account_ids:
        return
    with db.tx() as c:
        # Text columns + an embedding-exists flag. embed_missing() can hand this
        # function 45k ids; SELECT * here meant ~1GB of raw JSON + blobs just
        # to rebuild the text and test whether an embedding was present.
        rows = db.get_accounts_for_embedding(c, account_ids)
    pending = []
    for r in rows:
        text = _embed_text_for(dict(r))
        if not text:
            continue
        if r["embedding_text"] == text and r["has_embedding"]:
            continue
        pending.append((r["id"], text))
    if not pending:
        return
    BATCH = 25
    for i in range(0, len(pending), BATCH):
        chunk = pending[i:i + BATCH]
        vecs = ai.embed_texts([t for _, t in chunk])
        with db.tx() as c:
            for (aid, text), vec in zip(chunk, vecs):
                db.update_embedding(c, aid, vec, text)


def embed_missing() -> int:
    with db.tx() as c:
        # Select a length() flag instead of the blob itself: the blob was only
        # ever tested for truthiness, but pulling it dragged ~623MB of
        # embeddings into memory and doubled the cost (1050ms -> 597ms).
        # COALESCE(length(...),0)=0 matches `not blob` exactly (None or empty).
        rows = c.execute(
            "SELECT id, biography, full_name, category, embedding_text, "
            "       COALESCE(length(bio_embedding), 0) = 0 AS no_emb "
            "  FROM platform_accounts"
        ).fetchall()
    todo = []
    for r in rows:
        text = _embed_text_for({
            "full_name": r["full_name"], "biography": r["biography"], "category": r["category"],
        })
        if text and (r["embedding_text"] != text or r["no_emb"]):
            todo.append(r["id"])
    if not todo:
        return 0
    _embed_accounts(todo)
    return len(todo)
