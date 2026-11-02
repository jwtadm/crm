"""AI helpers: chat + embeddings, both via OpenRouter (OpenAI-compatible)."""
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI

from . import settings


def _openrouter(timeout: float = 15.0) -> OpenAI:
    return OpenAI(
        base_url=settings.get("OPENROUTER_BASE"),
        api_key=settings.require("OPENROUTER_API_KEY"),
        timeout=timeout,
    )


def _try_chat(client: OpenAI, model: str, messages: list[dict], temperature: float) -> str | None:
    try:
        resp = client.chat.completions.create(
            model=model, messages=messages, temperature=temperature,
        )
        content = (resp.choices[0].message.content or "").strip()
        return content or None
    except Exception:
        return None


def _chat(messages: list[dict], temperature: float = 0.4) -> str:
    """Try the primary chat model, fall through configured fallbacks."""
    primary = settings.get("OPENROUTER_CHAT_MODEL")
    fallbacks = settings.get_list("OPENROUTER_CHAT_FALLBACKS")
    if not primary:
        raise RuntimeError("Open /settings and set OPENROUTER_CHAT_MODEL.")
    client = _openrouter()
    for model in [primary, *fallbacks]:
        out = _try_chat(client, model, messages, temperature)
        if out:
            return out
    raise RuntimeError(f"All chat models failed: {[primary, *fallbacks]}")


def _strip_codefence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        lines = [l for l in s.split("\n") if not l.strip().startswith("```")]
        s = "\n".join(lines).strip()
    return s


def generate_search_queries(profile_description: str, n: int = 10, platforms: list[str] | None = None) -> list[str]:
    """Given a free-form audience/creator description, produce ~n short search keywords.

    Returns deduplicated, lowercased phrases suitable for username/keyword search.
    """
    plat_str = ", ".join(platforms) if platforms else "instagram, tiktok, x"
    prompt = f"""You are helping discover social-media creators / influencers / brands matching a target audience profile.

Target profile:
\"\"\"{profile_description.strip()}\"\"\"

Generate exactly {n} short search queries (1-3 words each) that would surface relevant accounts when typed into the user-search box on {plat_str}. Mix:
- niche keywords (e.g. "vegan recipes", "skincare review")
- audience descriptors (e.g. "fitness mom", "indie hacker")
- adjacent communities the target overlaps with
- 1-2 non-English variants if the profile suggests a non-English market

Return strict JSON, no commentary:
{{"queries": ["...", "..."]}}"""
    raw = _chat([{"role": "user", "content": prompt}], temperature=0.6)
    raw = _strip_codefence(raw)
    try:
        data = json.loads(raw)
        queries = data.get("queries") or []
    except Exception:
        # Fallback: split on lines
        queries = [l.strip("- *0123456789.\t ") for l in raw.split("\n") if l.strip()]
    # Dedup, normalize
    seen, out = set(), []
    for q in queries:
        q = re.sub(r"\s+", " ", q).strip().lower()
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out[:n]


# ---------- ICP filter (LLM gatekeeper) ----------

# Batch size for classify_creators(). Big enough to amortize the prompt
# (instructions + ICP brief), small enough that one slow request doesn't
# stall the whole bulk run. 15 is roughly two seeds' worth of similar hits.
ICP_BATCH_SIZE = 15

# Cap for parallel LLM calls per classify_creators() invocation. Each call
# is I/O-bound (waiting on OpenRouter); within one seed there are only ~6
# chunks for a 77-candidate result so we'd never approach this in practice,
# but the cap is here to protect against pathological seeds that somehow
# return huge candidate lists. With this cap, all chunks for a normal seed
# fire concurrently and the per-seed LLM wall-clock equals one call (~20s)
# instead of six (~120s).
ICP_PARALLEL_WORKERS = 32


def classify_creators(briefs: list[dict], icp_brief: str) -> dict[str, bool]:
    """Decide which accounts match the ICP brief. Returns {username: keep}.

    Each item in `briefs` should carry at minimum {username, full_name, bio,
    category}. Empty briefs short-circuit to all-keep. On any LLM error we
    also fall back to all-keep — refusing to ingest because the gatekeeper
    is down is worse than letting a few off-target accounts through (a human
    can reject them later via the existing blacklist flow).

    Chunks are dispatched to OpenRouter in parallel (ICP_PARALLEL_WORKERS).
    Empty-username rows are force-kept and never sent — one bogus
    `{"username": "", "verdict": "drop"}` from the model would otherwise drop
    every blank-username row in the chunk via dict-key collision.
    """
    if not briefs or not icp_brief.strip():
        return {b.get("username", ""): True for b in briefs}

    # Slice into batches and pre-split named vs. blank for each batch.
    batches: list[tuple[list[dict], list[dict]]] = []  # (full_chunk, named_only)
    for chunk_start in range(0, len(briefs), ICP_BATCH_SIZE):
        chunk = briefs[chunk_start: chunk_start + ICP_BATCH_SIZE]
        named = [b for b in chunk if (b.get("username") or "").strip()]
        batches.append((chunk, named))

    # Dispatch each named-batch to a worker; empty batches need no call.
    def _run(named: list[dict]) -> dict[str, bool]:
        return _classify_chunk(named, icp_brief) if named else {}

    if batches:
        # Fan every batch out at once — chunks are independent, OpenRouter
        # handles concurrency fine at this volume. Capped at
        # ICP_PARALLEL_WORKERS as a backstop.
        workers = min(ICP_PARALLEL_WORKERS, max(1, len(batches)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            per_batch_verdicts = list(ex.map(_run, [named for _, named in batches]))
    else:
        per_batch_verdicts = []

    # Merge verdicts back into username → keep_bool. Default-keep when the
    # model's response is missing a row (likely truncated or hallucinated).
    out: dict[str, bool] = {}
    for (chunk, _named), verdicts in zip(batches, per_batch_verdicts):
        for b in chunk:
            uname = (b.get("username") or "").strip()
            if not uname:
                out[b.get("username", "")] = True
                continue
            out[uname] = verdicts.get(uname, True)
    return out


def _classify_chunk(chunk: list[dict], icp_brief: str) -> dict[str, bool]:
    """Single LLM round for up to ICP_BATCH_SIZE accounts. Returns {username:
    keep_bool}. On any failure returns {} so the caller defaults each row
    to keep."""
    accounts_json = json.dumps([
        {
            "username": b.get("username", ""),
            "full_name": b.get("full_name", ""),
            "bio": (b.get("bio") or "")[:600],   # cap to keep prompts cheap
            "category": b.get("category", ""),
        }
        for b in chunk
    ], ensure_ascii=False)

    system = (
        "You are a strict but fair gatekeeper for a creator-discovery pipeline. "
        "For each social-media account, decide whether it matches the Ideal "
        "Customer Profile (ICP) below. When uncertain, default to 'keep' — "
        "the team can reject later, but a 'drop' is permanent for this run."
    )
    user = (
        f"ICP brief:\n\"\"\"\n{icp_brief.strip()}\n\"\"\"\n\n"
        f"Judge these accounts. Return STRICT JSON, no commentary:\n"
        f'{{"decisions": [{{"username": "...", "verdict": "keep" or "drop", '
        f'"reason": "<≤12 words>"}}, ...]}}\n\n'
        f"Accounts:\n{accounts_json}"
    )
    try:
        raw = _chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            temperature=0.1,
        )
        data = json.loads(_strip_codefence(raw))
        decisions = data.get("decisions") or []
    except Exception:
        return {}
    out: dict[str, bool] = {}
    for d in decisions:
        if not isinstance(d, dict):
            continue
        uname = (d.get("username") or "").strip()
        verdict = (d.get("verdict") or "").strip().lower()
        if uname:
            # anything not explicitly 'drop' = keep (default-keep policy)
            out[uname] = (verdict != "drop")
    return out


# ---------- Embeddings (OpenRouter, OpenAI-compatible) ----------

def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch embed. Empty/blank inputs get a zero vector matching the returned dim."""
    if not texts:
        return []
    model = settings.get("OPENROUTER_EMBED_MODEL")
    if not model:
        raise RuntimeError("Open /settings and set OPENROUTER_EMBED_MODEL.")
    # The API errors on empty strings; substitute a placeholder and zero-out post-hoc
    safe = [t if t and t.strip() else " " for t in texts]
    resp = _openrouter(timeout=30.0).embeddings.create(model=model, input=safe)
    vecs = [d.embedding for d in resp.data]
    dim = len(vecs[0]) if vecs else 0
    for i, t in enumerate(texts):
        if not t or not t.strip():
            vecs[i] = [0.0] * dim
    return vecs


def embed_text(text: str) -> list[float]:
    return embed_texts([text])[0]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
