"""
semantic_cache.py — Hardened semantic-cache core primitives

This module implements the on-device, CPU-only semantic-cache stage that sits
after the pre-inference redaction stage and before compression + inference. It
embeds the Redacted_Prompt locally, performs a hardened cluster-based lookup in
a persistent local ChromaDB collection, and stores produced results on a miss.

Structure (built bottom-up, mirroring ``redactor.py`` conventions):

  * Configuration defaults + ``CacheDecision`` frozen dataclass.
  * ``hardened_decision`` / ``naive_decision`` — PURE decision helpers, the
    single source of truth for the hit/miss policy; no I/O, no network.
  * ``load_embedding_model`` / ``warm_up_embedding_model`` / ``embed`` — the
    ``all-MiniLM-L6-v2`` embedding loader (CPU-only) with a build/setup-time
    warm-up that downloads + caches the weights so runtime needs no network.
  * ``open_vector_store`` — persistent local ChromaDB collection opener.
  * ``lookup`` — hardened cluster-based lookup returning a ``CacheDecision``.
  * ``store`` — store-on-miss with upsert-on-near-duplicate.

Heavy dependencies (``sentence_transformers``, ``chromadb``) are imported
LAZILY inside the loaders so this module imports cleanly without them, exactly
like ``redactor.load_ner_model``. Every loader returns ``None`` on ANY failure
and NEVER raises. At RUNTIME nothing leaves the machine; the only allowed
network access is the build/setup-time model download performed by
``warm_up_embedding_model`` (see below), invoked once during setup — e.g.
``python -m semantic_cache`` — so the artifact is cached locally before serving.

Design references: "Components and Interfaces → backend/semantic_cache.py",
"Hardened lookup algorithm", "Cached_Result schema in ChromaDB", and the
cosine-similarity derivation in design.md.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration defaults (Req 3.3)
# ---------------------------------------------------------------------------

DEFAULT_TOP_K: int = 5
DEFAULT_MIN_SIMILARITY: float = 0.85
DEFAULT_MARGIN_THRESHOLD: float = 0.05

# Tiny tolerance applied ONLY to the margin comparison so IEEE-754 float dust at
# an exactly-threshold margin still counts as a hit. Example: 0.95 - 0.90 ==
# 0.04999999999999993 (not exactly 0.05), so at margin_threshold=0.05 the naive
# ``margin >= margin_threshold`` would MISS a margin that is mathematically 0.05.
# ``margin >= margin_threshold - MARGIN_EPSILON`` treats that boundary as a hit.
MARGIN_EPSILON: float = 1e-9

# Sentinel Runner_Up Similarity_Score used when no runner-up exists (Req 3.9,
# 4.4). 0.0 is a defined "no runner-up present" marker.
NO_RUNNER_UP_SENTINEL: float = 0.0

# ChromaDB persistent directory (local, on-device). Absolute path anchored to
# this file so the store location is stable regardless of the process CWD.
DEFAULT_CACHE_DIR: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cache_store"
)
COLLECTION_NAME: str = "semantic_cache"


# ---------------------------------------------------------------------------
# CacheDecision — in-memory record for a single lookup
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CacheDecision:
    """Immutable outcome of a single hardened lookup.

    Carries only the decision, the scores/margin, the lookup latency, the
    Top_Match id (only on a hit), and the number of candidates considered —
    never any raw prompt or sensitive value, so it is safe to log/serialize.
    """

    decision: str                    # "hit" | "miss"
    top_score: float                 # Top_Match Similarity_Score (0.0 if none)
    runner_up_score: float           # Runner_Up Similarity_Score (sentinel if none)
    margin: float                    # Confidence_Margin = top - runner_up (Req 3.4)
    latency_ms: float                # lookup start -> decision (Req 10.1)
    entry_id: str | None = None      # Top_Match id when hit, else None
    candidate_count: int = 0         # number of candidates returned (0..top_k)


# ---------------------------------------------------------------------------
# Pure decision helpers (Req 3.4-3.10, 5.4) — single source of truth
# ---------------------------------------------------------------------------

def hardened_decision(
    scores: list[float],
    *,
    min_similarity: float,
    margin_threshold: float,
) -> tuple[str, float, float, float]:
    """Evaluate the hardened cluster-based policy over ``scores``.

    PURE — no I/O, no network. ``scores`` is a list of candidate cosine
    similarities in ANY order; this is the single source of truth for the
    hit/miss rule (Req 3.4-3.10) and is exercised directly by the stress test
    and property tests.

    Returns ``(decision, top_score, runner_up_score, margin)``:

      * empty list -> ("miss", 0.0, NO_RUNNER_UP_SENTINEL, 0.0)   (Req 3.10)
      * else sort DESCENDING; ``top = scores_sorted[0]``.
      * ``runner_up`` = ``scores_sorted[1]`` when >= 2 candidates, else the
        NO_RUNNER_UP_SENTINEL (0.0)                               (Req 3.9)
      * ``margin`` = ``top - runner_up``                          (Req 3.4)
      * exact tie for the max (``scores_sorted[1] == top``) forces
        ``margin = 0.0``                                          (Req 3.8)
      * decision = "hit" iff ``top >= min_similarity`` AND
        ``margin >= margin_threshold - MARGIN_EPSILON``, else "miss"
                                                             (Req 3.5/3.6/3.7)

    The margin comparison carries a ``1e-9`` epsilon (``MARGIN_EPSILON``) so
    float dust at an exactly-threshold margin still counts as a hit. Under
    IEEE-754, e.g. ``0.95 - 0.90 == 0.04999999999999993`` would otherwise miss
    at ``margin_threshold=0.05`` even though the margin is mathematically 0.05.
    Only the margin comparison gets the epsilon — ``min_similarity`` is compared
    exactly. The tie rule still holds: an exact top tie forces ``margin = 0.0``,
    and ``0.0 >= 0.05 - 1e-9`` is false, so a tie remains a miss when
    ``margin_threshold > 0``.
    """
    if not scores:
        return ("miss", 0.0, NO_RUNNER_UP_SENTINEL, 0.0)

    scores_sorted = sorted(scores, reverse=True)
    top = scores_sorted[0]

    if len(scores_sorted) >= 2:
        runner_up = scores_sorted[1]
    else:
        runner_up = NO_RUNNER_UP_SENTINEL

    margin = top - runner_up

    # Exact tie for the top score (including identical embeddings): the match
    # is maximally ambiguous, so force the margin to 0.0 (Req 3.8).
    if len(scores_sorted) >= 2 and scores_sorted[1] == top:
        margin = 0.0

    if top >= min_similarity and margin >= margin_threshold - MARGIN_EPSILON:
        decision = "hit"
    else:
        # below-min (Req 3.6), ambiguous small margin (Req 3.7), or tie (Req 3.8)
        decision = "miss"

    return (decision, top, runner_up, margin)


def naive_decision(scores: list[float], *, min_similarity: float) -> str:
    """Baseline single-threshold policy for the stress test (Req 5.4).

    PURE. Serves the Top_Match whenever its Similarity_Score clears
    ``min_similarity``, IGNORING the Confidence_Margin. Empty -> "miss". This
    deliberately diverges from ``hardened_decision`` on ambiguous top-two cases
    so the stress test can quantify the hardening benefit.
    """
    if not scores:
        return "miss"
    top = max(scores)
    return "hit" if top >= min_similarity else "miss"


# ---------------------------------------------------------------------------
# Embedding model loader + warm-up + embed (Req 1)
# ---------------------------------------------------------------------------

def load_embedding_model():
    """Load ``all-MiniLM-L6-v2`` (CPU-only), or return ``None`` on ANY failure.

    Mirrors ``redactor.load_ner_model``: ``sentence_transformers`` is imported
    LAZILY here so this module imports cleanly without it, and any exception
    (missing artifact, import error, incompatible build, ...) is caught, logged
    as a WARNING, and turned into a ``None`` return so a load failure can never
    crash the server or a request — the cache stage simply degrades to
    always-miss (Req 1.6, 1.7).

    ``device="cpu"`` forces CPU; a GPU is never required (Req 1.4).
    sentence-transformers resolves the model from the LOCAL Hugging Face cache
    at runtime; the artifact is warmed (downloaded + cached) at build/setup
    time by ``warm_up_embedding_model`` so runtime makes no network call.
    """
    try:
        from sentence_transformers import SentenceTransformer  # lazy import

        # Strictly CPU — never GPU (Req 1.4).
        return SentenceTransformer("all-MiniLM-L6-v2", device="cpu")
    except Exception as err:  # noqa: BLE001 — degrade on ANY failure (Req 1.6/1.7)
        logger.warning(
            "Embedding model failed to load; disabling semantic cache: %s", err
        )
        return None


def warm_up_embedding_model() -> bool:
    """Build/setup-time step: ensure the model weights are cached locally.

    Invoked ONCE at setup (e.g. ``python -m semantic_cache`` or a direct call
    from a setup script) while the network is available. It calls
    ``load_embedding_model`` — which triggers the sentence-transformers
    download into the local Hugging Face cache on first use — and, when a model
    is returned, runs a tiny ``model.encode("warmup", ...)`` to force full
    initialization of the weights. After this succeeds, every RUNTIME embed
    resolves from the local cache with no network call, satisfying the
    runtime-offline requirement (Req 1.4/1.5).

    Returns ``True`` on success, ``False`` on failure (logged WARNING, never
    raises). Keeps ``device="cpu"``.
    """
    try:
        model = load_embedding_model()
        if model is None:
            logger.warning("Embedding model warm-up failed: model unavailable")
            return False
        # Force full initialization / weight materialization.
        model.encode("warmup", normalize_embeddings=True)
        return True
    except Exception as err:  # noqa: BLE001 — never raise from warm-up
        logger.warning("Embedding model warm-up failed: %s", err)
        return False


def embed(model, text: str) -> list[float]:
    """Return the deterministic unit-length embedding for ``text`` (Req 1.1/1.3/1.8).

    ``encode(text, normalize_embeddings=True)`` yields a unit-length vector so
    the cosine space is well-behaved; the result is converted to a plain
    ``list[float]`` (via ``.tolist()``) so callers/ChromaDB never depend on the
    numpy/torch return type. The same text embedded twice by the same loaded
    model produces an identical vector (Req 1.8).
    """
    vector = model.encode(text, normalize_embeddings=True)
    # numpy arrays / torch tensors expose .tolist(); guard for plain lists too.
    return vector.tolist() if hasattr(vector, "tolist") else list(vector)


# ---------------------------------------------------------------------------
# Vector store opener (Req 2)
# ---------------------------------------------------------------------------

def open_vector_store(path: str = DEFAULT_CACHE_DIR):
    """Open a persistent local Chroma collection, or ``None`` on ANY failure.

    ``chromadb`` is imported LAZILY here (module still imports without it).
    A ``PersistentClient`` at ``path`` gives on-device, cross-restart storage;
    ``get_or_create_collection`` with ``metadata={"hnsw:space": "cosine"}``
    configures the collection so query distances are COSINE distances — that is
    what makes ``similarity = 1 - distance`` valid in ``lookup`` (Req 3.1).
    No network call is made (Req 2.5). Any exception is caught, logged as a
    WARNING, and reported as ``None`` so a failed open cleanly disables the
    cache instead of crashing (Req 2.6).
    """
    try:
        import chromadb  # lazy import

        client = chromadb.PersistentClient(path=path)
        collection = client.get_or_create_collection(
            COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        return collection
    except Exception as err:  # noqa: BLE001 — degrade on ANY failure (Req 2.6)
        logger.warning("Vector store failed to open; disabling semantic cache: %s", err)
        return None


# ---------------------------------------------------------------------------
# Hardened lookup (Req 3)
# ---------------------------------------------------------------------------

def lookup(
    collection,
    embedding,
    *,
    top_k: int = DEFAULT_TOP_K,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    margin_threshold: float = DEFAULT_MARGIN_THRESHOLD,
) -> tuple[CacheDecision, dict | None]:
    """Perform a hardened cluster-based lookup of ``embedding``.

    Retrieves up to ``top_k`` nearest Cache_Entries (fewer when the store holds
    fewer), converts each cosine distance to a similarity ``clamp(1 - d, -1, 1)``
    (Req 3.1), sorts DESCENDING (defensive — Chroma returns ascending distance),
    delegates the hit/miss rule to ``hardened_decision``, and builds a
    ``CacheDecision``. Returns ``(decision, entry)`` where ``entry`` is the
    Top_Match ``{"id", "document", "metadata"}`` ONLY on a hit, else ``None``.

    An empty store is an immediate miss (Req 3.10).
    """
    t0 = time.perf_counter()

    n = collection.count()
    if n == 0:  # Req 3.10 — zero candidates -> miss
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return (
            CacheDecision(
                "miss", 0.0, NO_RUNNER_UP_SENTINEL, 0.0, latency_ms, None, 0
            ),
            None,
        )

    k = min(top_k, n)
    res = collection.query(
        query_embeddings=[embedding],
        n_results=k,
        include=["distances", "documents", "metadatas"],
    )

    distances = res["distances"][0]
    ids = res["ids"][0]                 # ids are always returned by Chroma
    documents = res["documents"][0]
    metadatas = res["metadatas"][0]

    # cosine similarity = 1 - cosine distance (hnsw:space="cosine"); clamp to
    # [-1.0, 1.0] defensively against float noise (Req 3.1).
    sims = [max(-1.0, min(1.0, 1.0 - d)) for d in distances]

    # Pair each similarity with its candidate, then sort by similarity
    # DESCENDING (defensive even though Chroma returns ascending distance).
    candidates = list(zip(sims, ids, documents, metadatas))
    candidates.sort(key=lambda c: c[0], reverse=True)

    scores = [c[0] for c in candidates]
    decision_str, top, runner_up, margin = hardened_decision(
        scores, min_similarity=min_similarity, margin_threshold=margin_threshold
    )

    latency_ms = (time.perf_counter() - t0) * 1000.0

    if decision_str == "hit":
        top_sim, top_id, top_doc, top_meta = candidates[0]
        entry = {"id": top_id, "document": top_doc, "metadata": top_meta}
        entry_id = top_id
    else:
        entry = None
        entry_id = None

    return (
        CacheDecision(decision_str, top, runner_up, margin, latency_ms, entry_id, k),
        entry,
    )


# ---------------------------------------------------------------------------
# Store on miss, with upsert-on-near-duplicate (Req 6)
# ---------------------------------------------------------------------------

def store(collection, embedding, result_text: str, metadata: dict) -> bool:
    """Store a Cache_Entry, updating in place on a near-duplicate (Req 6).

    Before adding, run one internal ``lookup`` (default thresholds) of the
    just-produced embedding: if that would itself be a HIT against an existing
    entry, ``update`` that entry's embedding/document/metadata in place (same
    id) so the store never accumulates redundant near-identical vectors
    (upsert-on-near-duplicate). Otherwise ``add`` a fresh ``uuid4`` entry.

    The stored ``document`` is the RESULT text (a PERFORM answer or a BUILD
    code string) — NEVER the prompt. The caller supplies scalar-only
    ``metadata`` (``task_mode``, ``created_at``, ``real_input_tokens``,
    ``real_output_tokens``, ``real_total_tokens``, ``inference_time_ms``,
    ``result_char_len``); this function stores exactly what it is given plus
    the embedding and adds NOTHING else. No raw/redacted prompt text is ever
    passed as ``document`` or ``metadata`` — the prompt is represented only by
    the embedding vector and an opaque id (Req 6.4, 2.4).

    Returns ``True`` on success. Catches ANY exception, logs a WARNING, and
    returns ``False`` so a store failure never raises into the request path
    (Req 6.8).
    """
    try:
        decision, _entry = lookup(collection, embedding)
        if decision.decision == "hit" and decision.entry_id is not None:
            # Upsert-on-near-duplicate: refresh the existing entry in place.
            collection.update(
                ids=[decision.entry_id],
                embeddings=[embedding],
                documents=[result_text],
                metadatas=[metadata],
            )
        else:
            collection.add(
                ids=[str(uuid.uuid4())],
                embeddings=[embedding],
                documents=[result_text],
                metadatas=[metadata],
            )
        return True
    except Exception as err:  # noqa: BLE001 — never raise into the request path (Req 6.8)
        logger.warning("Failed to store cache entry; leaving store unchanged: %s", err)
        return False


# ---------------------------------------------------------------------------
# Setup entry point — warm the model artifact into the local cache.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Run once at build/setup time (network allowed here) so the runtime path
    # never needs to download anything: ``python -m semantic_cache``.
    logging.basicConfig(level=logging.INFO)
    ok = warm_up_embedding_model()
    print(f"warm_up_embedding_model -> {ok}")
