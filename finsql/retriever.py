"""Schema retrieval: fit a warehouse with hundreds of tables into a prompt of a
few thousand tokens.

  1. Metric pinning  - "revenue" matches the net_revenue metric, which pins its
                       tables (fct_invoices) regardless of search scores.
  2. BM25 ranking    - over table "cards": name, description, grain, synonyms,
                       column names. Pure Python, no model to load in Lambda.
  3. Join expansion  - FK graph: add bridge tables so any two selected tables
                       can be joined, then 1-hop dimension lookups (fiscal
                       calendar, department names...).
  4. Column pruning  - wide tables keep keys + question-relevant columns only.
  5. Token budget    - tables are added in priority order until the budget
                       is spent; the rest are dropped and reported.
"""
import math
import re
from collections import Counter, deque
from dataclasses import dataclass, field

from . import config
from .catalog import estimate_tokens

_STOP = set("""a an and are as at be by for from has have how i in is it its last me my of on or our per show
the their them there this to top us was we were what when where which who why with vs versus give list many much
did do does all each by total tell compare than""".split())


def tokenize(text: str) -> list[str]:
    tokens = []
    for w in re.split(r"[^a-z0-9$]+", text.lower()):
        if len(w) < 2 or w in _STOP:
            continue
        if w.endswith("ies") and len(w) > 4:
            w = w[:-3] + "y"
        elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
            w = w[:-1]
        tokens.append(w)
    return tokens


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.75):
        self.docs = [Counter(d) for d in docs]
        self.lens = [len(d) for d in docs]
        self.avg = sum(self.lens) / max(len(docs), 1)
        n = len(docs)
        df = Counter(t for d in self.docs for t in d)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.k1, self.b = k1, b

    def scores(self, query: list[str]) -> list[float]:
        out = []
        for d, length in zip(self.docs, self.lens):
            s = 0.0
            for t in set(query):
                if t in d:
                    tf = d[t]
                    s += self.idf[t] * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * length / self.avg))
            out.append(s)
        return out


def _table_doc(t: dict) -> list[str]:
    # Name and synonyms are repeated to weigh more than individual column names.
    head = " ".join([t["name"].replace("_", " "), " ".join(t.get("synonyms", []))])
    body = " ".join([t.get("description", ""), t.get("grain", "")] +
                    [c["name"].replace("_", " ") for c in t["columns"]])
    # Unique, non-numeric body tokens: otherwise 70 `crm_attr_NNN` columns make a
    # table's document so long that BM25 length normalisation buries it.
    return tokenize(head) * 3 + [w for w in dict.fromkeys(tokenize(body)) if not w.isdigit()]


def render_table(t: dict, question_tokens: set[str] | None = None, max_cols: int | None = None) -> str:
    """Compact, DDL-like card. Roughly 3-5x fewer tokens than JSON with the same facts."""
    cols = t["columns"]
    omitted = 0
    if max_cols and len(cols) > max_cols:
        q = question_tokens or set()
        desc_count = Counter(c.get("description", "") for c in cols)

        def key(c):
            return c["pk"] or bool(c["fk"])

        def relevant(c):
            return bool(q & set(tokenize(c["name"] + " " + c.get("description", ""))))

        def generic(c):  # e.g. 70 "Custom CRM field" columns: only shown if the question names one
            d = c.get("description", "")
            return bool(d) and desc_count[d] > 5 and not c.get("samples")

        ranked = sorted(enumerate(cols), key=lambda ic: (not key(ic[1]), not relevant(ic[1]), generic(ic[1]), ic[0]))
        keep = sorted([ic for ic in ranked if key(ic[1]) or relevant(ic[1]) or not generic(ic[1])][:max_cols])
        omitted = len(cols) - len(keep)
        cols = [c for _, c in keep]

    head = f"TABLE {t['name']}"
    notes = [x for x in (t.get("description"), f"grain: {t['grain']}" if t.get("grain") else "") if x]
    lines = [head + (f"  -- {' | '.join(notes)}" if notes else "")]
    for c in cols:
        line = f"  {c['name']} {c['type']}"
        if c["pk"]:
            line += " PK"
        if c["fk"]:
            line += f" -> {c['fk']}"
        extra = [c.get("description", "")] if c.get("description") else []
        if c.get("samples"):
            extra.append("values: " + ", ".join(f"'{v}'" for v in c["samples"]))
        if extra:
            line += "  -- " + "; ".join(extra)
        lines.append(line)
    if omitted:
        lines.append(f"  ... {omitted} more columns not shown")
    return "\n".join(lines)


@dataclass
class Retrieval:
    tables: list[str]
    schema_text: str
    metrics: list[dict]
    examples: list[dict]
    stats: dict = field(default_factory=dict)


def _fk_graph(catalog: dict) -> dict[str, set[str]]:
    g: dict[str, set[str]] = {t: set() for t in catalog["tables"]}
    for name, t in catalog["tables"].items():
        for c in t["columns"]:
            if c["fk"]:
                parent = c["fk"].split(".")[0].lower()
                if parent in g and parent != name:
                    g[name].add(parent)
                    g[parent].add(name)
    return g


def _path(g: dict[str, set[str]], a: str, b: str, max_hops: int = 3) -> list[str] | None:
    prev, q = {a: None}, deque([a])
    while q:
        cur = q.popleft()
        if cur == b:
            path = []
            while cur:
                path.append(cur)
                cur = prev[cur]
            return path[::-1] if len(path) - 1 <= max_hops else None
        for nxt in g[cur]:
            if nxt not in prev:
                prev[nxt] = cur
                q.append(nxt)
    return None


def match_metrics(question: str, catalog: dict) -> list[dict]:
    q = f" {' '.join(tokenize(question))} "
    hits = []
    for m in catalog.get("metrics", []):
        phrases = [m["name"].replace("_", " ")] + m.get("synonyms", [])
        if any(f" {' '.join(tokenize(p))} " in q for p in phrases if tokenize(p)):
            hits.append(m)
    return hits


_index_cache: dict[int, tuple[list[str], BM25]] = {}


def _table_index(catalog: dict) -> tuple[list[str], BM25]:
    """BM25 over table cards, built once per loaded catalog."""
    key = id(catalog["tables"])
    if key not in _index_cache:
        names = list(catalog["tables"])
        _index_cache[key] = (names, BM25([_table_doc(catalog["tables"][n]) for n in names]))
    return _index_cache[key]


def rank_tables(text: str, catalog: dict) -> list[tuple[float, str]]:
    names, index = _table_index(catalog)
    return sorted(((s, n) for s, n in zip(index.scores(tokenize(text)), names) if s > 0), reverse=True)


def suggest_tables(bad_name: str, catalog: dict, k: int = 3) -> list[str]:
    """For the guard's error message: `revenue_2024` -> fct_invoices, by meaning
    (metric synonyms, BM25) first and spelling second."""
    from rapidfuzz import fuzz, process

    words = bad_name.replace("_", " ")
    by_metric = [t for m in match_metrics(words, catalog) for t in m["tables"]]
    by_search = [n for _, n in rank_tables(words, catalog)[:k]]
    by_spelling = [m[0] for m in process.extract(bad_name, list(catalog["tables"]), scorer=fuzz.WRatio, limit=k)]
    return list(dict.fromkeys(by_metric + by_search + by_spelling))[:k]


def retrieve(question: str, catalog: dict, top_k: int = config.TOP_K_TABLES,
             budget: int = config.SCHEMA_TOKEN_BUDGET, max_cols: int = config.MAX_COLUMNS_PER_TABLE) -> Retrieval:
    tables = catalog["tables"]
    q_tokens = tokenize(question)

    metrics = match_metrics(question, catalog)
    pinned = list(dict.fromkeys(t for m in metrics for t in m["tables"] if t in tables))

    scored = rank_tables(question, catalog)
    best = scored[0][0] if scored else 1
    loose = [n for s, n in scored if s >= 0.2 * best and n not in pinned]
    ranked = [n for s, n in scored if s >= 0.35 * best and n not in pinned]
    core = (pinned + loose)[: max(3, len(pinned))]
    extras = [n for n in ranked if n not in core][: max(0, top_k - len(core))]
    g = _fk_graph(catalog)
    bridges = []
    for i, a in enumerate(core):
        for b in core[i + 1:]:
            p = _path(g, a, b)
            bridges += [t for t in (p or [])[1:-1] if t not in core]
    lookups = [c["fk"].split(".")[0].lower() for t in core + bridges for c in tables[t]["columns"] if c["fk"]]
    priority = list(dict.fromkeys(core + bridges + lookups + extras))
    priority = [t for t in priority if t in tables]

    blocks, used, dropped, spent = [], [], [], 0
    qset = set(q_tokens)
    for t in priority:
        block = render_table(tables[t], qset, max_cols)
        cost = estimate_tokens(block)
        if blocks and spent + cost > budget:
            dropped.append(t)
            continue
        blocks.append(block)
        used.append(t)
        spent += cost

    ex_scores = BM25([tokenize(e["question"]) for e in catalog.get("examples", [])]).scores(q_tokens)
    examples = [e for s, e in sorted(zip(ex_scores, catalog.get("examples", [])), key=lambda x: -x[0]) if s > 0][:2]

    schema_text = "\n\n".join(blocks)
    return Retrieval(
        tables=used, schema_text=schema_text, metrics=metrics, examples=examples,
        stats={
            "catalog_tables": len(tables),
            "full_schema_tokens": catalog.get("stats", {}).get("full_schema_tokens"),
            "schema_tokens": estimate_tokens(schema_text),
            "pinned_by_metric": pinned,
            "dropped_for_budget": dropped,
        },
    )
