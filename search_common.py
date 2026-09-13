#!/usr/bin/env python3
"""Shared plumbing for the semantic trade index: paths, the embedding model, the Milvus
Lite client, and the read-only theme helpers the monitors use.

Two collections, because they answer different questions:

  `tickers`    one row per distinct symbol (~500), vector = the company's business
               description. This is the only collection that costs an embedding.
  `positions`  one row per member+ticker (~730), scalar facts (gain/amount/dates) plus a
               *copy* of its ticker's vector, so a filtered vector search still works.

Embedding the business description rather than a rendered sentence is what makes the
vector half earn its keep: "clean energy" should retrieve Enphase because of what Enphase
does, not because the word "Energy" appears in its sector string. Scalar facts stay scalar
-- "$4,231 of estimated gain" is a number to filter on, not a phrase to embed.

Heavy imports (sentence-transformers/torch, pymilvus) are deferred to call time so the pure
helpers and the tests stay importable without them installed. `ThemeIndex` deliberately
never touches the embedding model: it reads vectors that are already in the store, which is
what lets the cron monitors add theme context without pulling torch into an alert path.
"""

from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
INDEX_PATH = REPO_DIR / "trades_index.db"          # Milvus Lite store (a directory)
META_PATH = REPO_DIR / "ticker_meta.json"          # ticker -> {name, sector, industry, summary}
TICKERS = "tickers"
POSITIONS = "positions"

# bge asks for an instruction prefix on the *query* side only; documents are embedded bare.
# Both sides are L2-normalised so the collection can score with plain cosine similarity.
MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384
METRIC = "COSINE"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# Similarity floor for two tickers to count as the same theme, measured on *mean-centred*
# vectors (see ThemeIndex). Raw cosine is unusable here: every business summary shares the
# same corporate register, so unrelated pairs floor around 0.60 and INTC/MSFT (0.75) score
# higher than NVDA/AMD (0.74). Centring removes that common component -- unrelated pairs
# fall to ~0.00 while same-industry pairs hold 0.25-0.46 -- and 0.25 is where the probe set
# splits chips, oil, banks and pharma cleanly without shattering them into singletons.
THEME_SIMILARITY = 0.25
MIN_THEME_SIZE = 2

# A theme is only worth naming if its members genuinely share the name: below this share,
# the modal industry is a label for a minority and reads as a false claim.
LABEL_COVERAGE = 0.5

_MODEL = None


def load_model():
    """Load (once) and return the sentence-transformers embedding model."""
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer(MODEL_NAME)
    return _MODEL


def embed_documents(texts: list[str]):
    return load_model().encode(texts, normalize_embeddings=True,
                               batch_size=64, show_progress_bar=False)


def embed_query(text: str):
    return load_model().encode([QUERY_PREFIX + text], normalize_embeddings=True)


def open_client(load: bool = True, path: Path | None = None):
    """Open the Milvus Lite store. `load=True` pages the collections in for search/query
    (a freshly opened client leaves them in the 'released' state otherwise)."""
    from pymilvus import MilvusClient
    client = MilvusClient(str(path or INDEX_PATH))
    if load:
        for name in (TICKERS, POSITIONS):
            if client.has_collection(name):
                client.load_collection(name)
    return client


def quote_literal(value: str) -> str:
    """Escape a string for use as a literal in a Milvus boolean expression.

    Facet values arrive from argv and from member names, so an unescaped quote is at best
    a parse error and at worst a rewritten predicate: `--member '%" or gain > 0 or member
    like "%'` would otherwise turn a name filter into "everything profitable".
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def truncate_bytes(text: str, max_bytes: int) -> str:
    """Cut `text` to fit `max_bytes` of UTF-8, never splitting a character.

    Milvus counts VARCHAR `max_length` in bytes while Python slices in characters, so a
    single non-ASCII company name in an otherwise short field is enough to fail an insert.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore")


def cluster_vectors(vectors, threshold: float = THEME_SIMILARITY) -> list[list[int]]:
    """Group row indices whose vectors are mutually similar, densest cluster first.

    Leader clustering: repeatedly take the unassigned row with the most neighbours above
    `threshold` and absorb its unassigned neighbours. Deterministic, O(n^2) on a few
    hundred vectors, and it needs no extra dependency -- sklearn would be a CPU-heavy
    install for one function in a cron script.

    Vectors must be L2-normalised, so the dot product is the cosine similarity.
    """
    import numpy as np

    mat = np.asarray(vectors, dtype="float32")
    if mat.ndim != 2 or len(mat) == 0:
        return []
    sim = mat @ mat.T
    adjacent = sim >= threshold
    unassigned = set(range(len(mat)))
    clusters: list[list[int]] = []
    while unassigned:
        idx = sorted(unassigned)
        # Rank candidates by how many *available* rows they would pull in, so the densest
        # theme forms first and leftovers fall out as singletons rather than the reverse.
        leader = max(idx, key=lambda i: (sum(1 for j in idx if adjacent[i, j]), -i))
        # The leader always joins its own cluster. Its self-similarity is ||v||^2, which
        # dips below the threshold for a vector that is not exactly unit length, and an
        # empty `members` would leave `unassigned` unchanged and spin forever.
        members = sorted({leader} | {j for j in idx if adjacent[leader, j]})
        clusters.append(members)
        unassigned -= set(members)
    clusters.sort(key=lambda c: (-len(c), c[0]))
    return clusters


class ThemeIndex:
    """Read-only view of the `tickers` collection for the alert paths.

    Every method degrades to empty rather than raising: a monitor that cannot reach the
    index must still deliver its alert, just without the extra context line. `available`
    is false when pymilvus is missing, the store has not been built, or it is empty.
    """

    def __init__(self, path: Path | None = None):
        self.available = False
        self.generated_at = 0
        self._meta: dict[str, dict] = {}
        self._vec: dict = {}
        try:
            client = open_client(load=True, path=path)
            if not client.has_collection(TICKERS):
                return
            rows = client.query(
                TICKERS, filter="", limit=16384,
                output_fields=["ticker", "name", "sector", "industry",
                               "generated_at", "vector"])
        except Exception:
            # ImportError (no pymilvus), a missing/corrupt store, a schema from an older
            # build -- all mean "no context available", never "kill the alert".
            return
        for r in rows:
            self._meta[r["ticker"]] = r
            self.generated_at = max(self.generated_at, r.get("generated_at", 0))
        self.available = bool(self._meta)
        self._client = client
        self._centre()

    def _centre(self) -> None:
        """Cache each ticker's mean-centred, re-normalised vector.

        The corpus mean is the "this is a company description" direction that every
        summary shares; subtracting it is what turns a 0.60-0.82 band where unrelated
        pairs outrank related ones into a 0.00-0.46 band that actually separates.
        """
        import numpy as np
        order = sorted(self._meta)
        raw = np.asarray([self._meta[t]["vector"] for t in order], dtype="float32")
        shifted = raw - raw.mean(0)
        norms = np.linalg.norm(shifted, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._vec = dict(zip(order, shifted / norms))

    def holdings(self, member: str) -> list[str]:
        """Every ticker this member already holds, per the indexed positions.

        `member` must be the `display_name()` spelling, which is what the index stores.
        """
        if self._client is None or not member:
            return []
        try:
            rows = self._client.query(
                POSITIONS, filter=f"member == {quote_literal(member)}",
                output_fields=["ticker"], limit=4096)
        except Exception:
            return []
        return sorted({r["ticker"] for r in rows})

    def known(self, tickers) -> list[str]:
        """The subset of `tickers` present in the index, de-duplicated, order preserved."""
        seen, out = set(), []
        for t in tickers:
            tk = t.upper()
            if tk in self._meta and tk not in seen:
                seen.add(tk)
                out.append(tk)
        return out

    def vectors_for(self, tickers: list[str]):
        """Centred, unit-length vectors, so a dot product is the similarity used above."""
        import numpy as np
        return np.asarray([self._vec[t] for t in tickers], dtype="float32")

    def label(self, tickers: list[str]) -> str | None:
        """Name a group by the industry its members actually share, falling back to the
        sector, then to nothing.

        Returning None matters: at the coarse end of the corpus a cluster can be a
        genuine mix, and naming it after whichever industry happens to be modal ships a
        confident wrong claim into a Discord alert. No name, no line.
        """
        from collections import Counter
        for field in ("industry", "sector"):
            names = [self._meta[t][field] for t in tickers
                     if self._meta[t].get(field) not in (None, "", "Unknown")]
            if not names:
                continue
            name, count = Counter(names).most_common(1)[0]
            if count >= max(2, round(len(tickers) * LABEL_COVERAGE)):
                return name
        return None

    def themes(self, tickers, threshold: float = THEME_SIMILARITY,
               min_size: int = MIN_THEME_SIZE) -> list[tuple[str, list[str]]]:
        """[(label, [ticker, ...]), ...] for the groups of at least `min_size`."""
        known = self.known(tickers)
        if len(known) < min_size:
            return []
        groups = cluster_vectors(self.vectors_for(known), threshold)
        out = []
        for g in groups:
            members = [known[i] for i in g]
            name = self.label(members) if len(members) >= min_size else None
            if name:
                out.append((name, members))
        return out

    def closest(self, ticker: str, universe) -> tuple[str, float] | None:
        """The member of `universe` most similar to `ticker`, as (ticker, cosine).

        Used to answer "is this buy a new theme for this member, or more of the same?",
        which an exact-ticker lookup cannot: it cannot know ENPH relates to FSLR.
        """
        import numpy as np
        others = [t for t in self.known(universe) if t.upper() != ticker.upper()]
        if ticker.upper() not in self._vec or not others:
            return None
        sims = self.vectors_for(others) @ self._vec[ticker.upper()]
        best = int(np.argmax(sims))
        return others[best], float(sims[best])
