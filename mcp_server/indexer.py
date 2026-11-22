"""Chunking + BM25 full-text index for the tuition policy markdown document.

Design
------
* Chunking follows the document structure, not a fixed character window:
  every numbered heading (``## 3.`` / ``### 3.2``) starts a new chunk, so each
  search hit maps to exactly one citable policy section. Sections that are
  longer than ``MAX_CHUNK_WORDS`` are further split on paragraph boundaries
  (never mid-sentence) and the parts keep the same section reference.
* Ranking is Okapi BM25 (k1=1.5, b=0.75). Section titles and the parent
  section title are added to each chunk's indexed text with extra weight,
  which helps short queries such as "refund" or "late fee".
* The index is built ONCE (``python -m mcp_server.indexer``) and persisted as
  JSON together with the SHA-256 of the source document. The MCP server
  loads it at start-up and only rebuilds if the document changed — queries
  never trigger re-indexing.

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOC = ROOT / "docs" / "University_Tuition_Policies.md"
DEFAULT_INDEX = Path(__file__).resolve().parent / "index" / "policy_index.json"

MAX_CHUNK_WORDS = 160
TITLE_WEIGHT = 3        # section title tokens are counted this many times
PARENT_TITLE_WEIGHT = 2
BM25_K1 = 1.5
BM25_B = 0.75
INDEX_FORMAT_VERSION = 3

STOPWORDS = frozenset(
    """a an and are as at be been by can do does for from has have how i if in
    into is it its me my of on or our so than that the their them then there
    these they this to was we what when where which who will with you your
    am any all also only not no yes should would could may might must""".split()
)

# Small domain thesaurus applied to queries (not to documents) so that
# everyday wording finds the policy vocabulary.
QUERY_SYNONYMS: dict[str, list[str]] = {
    "instalment": ["installment"],
    "split": ["installment"],
    "monthly": ["installment"],
    "due": ["deadline"],
    "penalty": ["late", "fee"],
    "fine": ["late", "fee"],
    "money": ["refund"],
    "back": ["refund"],
    "withdraw": ["withdrawal"],
    "quit": ["withdrawal"],
    "discount": ["scholarship"],
    "iban": ["bank", "transfer"],
    "wire": ["bank", "transfer"],
    "eft": ["bank", "transfer"],
    "block": ["hold"],
    "miss": ["default"],
    "missed": ["default"],
    "late": ["overdue"],
    "partial": ["partial"],
    "part": ["partial"],
}


def _stem(token: str) -> str:
    """Very light suffix stripping — enough to merge 'installments' and
    'installment', 'payments'/'paying'/'paid' -> 'pay'. Deliberately
    conservative to avoid over-stemming short words."""
    if token in {"paid", "pays", "paying", "payment", "payments"}:
        return "pay"
    for long, short in (("ibility", "ible"), ("ability", "able")):   # eligibility -> eligible
        if token.endswith(long):
            return token[: -len(long)] + short
    for suffix, min_len in (("ments", 7), ("ment", 7), ("ings", 6), ("ing", 6),
                            ("ies", 5), ("ed", 5), ("es", 5), ("s", 4)):
        if token.endswith(suffix) and len(token) >= min_len:
            base = token[: -len(suffix)]
            if suffix == "ies":
                base += "y"
            return base
    return token


def tokenize(text: str) -> list[str]:
    raw = re.findall(r"[a-z0-9]+(?:[.,][0-9]+)?", text.lower())
    return [_stem(t) for t in raw if t not in STOPWORDS and len(t) > 1]


@dataclass
class Chunk:
    chunk_id: str
    section: str          # e.g. "3.2"
    title: str            # e.g. "Plan Structure"
    parent_title: str     # e.g. "Installment Plans"
    text: str
    part: int = 1
    tokens: list[str] = field(default_factory=list, repr=False)

    @property
    def reference(self) -> str:
        return f"Section {self.section} — {self.parent_title} › {self.title}" \
            if self.parent_title and self.parent_title != self.title \
            else f"Section {self.section} — {self.title}"


HEADING_RE = re.compile(r"^(#{2,3})\s+(\d+(?:\.\d+)?)\.?\s+(.*)$")


def chunk_markdown(markdown: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    parent_titles: dict[str, str] = {}
    current: dict | None = None

    def flush() -> None:
        if current is None:
            return
        body = "\n".join(current["lines"]).strip()
        if not body:
            return
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        parts: list[list[str]] = [[]]
        words = 0
        for p in paragraphs:
            n = len(p.split())
            if parts[-1] and words + n > MAX_CHUNK_WORDS:
                parts.append([])
                words = 0
            parts[-1].append(p)
            words += n
        for i, part in enumerate(parts, start=1):
            sec = current["section"]
            chunks.append(Chunk(
                chunk_id=f"s{sec}" + (f"-p{i}" if len(parts) > 1 else ""),
                section=sec,
                title=current["title"],
                parent_title=parent_titles.get(sec.split(".")[0], ""),
                text="\n\n".join(part),
                part=i,
            ))

    in_fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            flush()
            level, section, title = m.groups()
            title = title.strip()
            if level == "##":
                parent_titles[section] = title
            current = {"section": section, "title": title, "lines": []}
        elif current is not None:
            current["lines"].append(line)
    flush()

    for c in chunks:
        weighted = (" ".join([c.title] * TITLE_WEIGHT) + " " +
                    " ".join([c.parent_title] * PARENT_TITLE_WEIGHT) + " " + c.text)
        c.tokens = tokenize(weighted)
    return chunks


class PolicyIndex:
    def __init__(self, chunks: list[Chunk], doc_sha256: str, source: str, document_id: str = ""):
        self.chunks = chunks
        self.document_id = document_id
        self.doc_sha256 = doc_sha256
        self.source = source
        self.doc_freq: Counter[str] = Counter()
        for c in chunks:
            self.doc_freq.update(set(c.tokens))
        self.term_freqs = [Counter(c.tokens) for c in chunks]
        self.avg_len = sum(len(c.tokens) for c in chunks) / max(len(chunks), 1)

    # ---- building / persistence -------------------------------------
    @classmethod
    def build(cls, doc_path: Path = DEFAULT_DOC) -> "PolicyIndex":
        text = doc_path.read_text(encoding="utf-8")
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        m = re.search(r"\*\*Document ID:\*\*\s*([A-Za-z0-9._-]+)", text)
        return cls(chunk_markdown(text), sha, doc_path.name, m.group(1) if m else "")

    def save(self, path: Path = DEFAULT_INDEX) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format": INDEX_FORMAT_VERSION,
            "source": self.source,
            "doc_sha256": self.doc_sha256,
            "document_id": self.document_id,
            "chunks": [asdict(c) for c in self.chunks],
        }
        path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path = DEFAULT_INDEX) -> "PolicyIndex":
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") != INDEX_FORMAT_VERSION:
            raise ValueError("index format mismatch")
        return cls([Chunk(**c) for c in data["chunks"]], data["doc_sha256"], data["source"],
                   data.get("document_id", ""))

    @classmethod
    def load_or_build(cls, doc_path: Path = DEFAULT_DOC,
                      index_path: Path = DEFAULT_INDEX) -> tuple["PolicyIndex", bool]:
        """Returns (index, rebuilt). Rebuilds only when the persisted index is
        missing, from an older format, or the document content changed."""
        current_sha = hashlib.sha256(doc_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        if index_path.exists():
            try:
                idx = cls.load(index_path)
                if idx.doc_sha256 == current_sha:
                    return idx, False
            except (ValueError, KeyError, json.JSONDecodeError):
                pass
        idx = cls.build(doc_path)
        idx.save(index_path)
        return idx, True

    # ---- querying -----------------------------------------------------
    def _expand(self, query: str) -> list[str]:
        base = tokenize(query)
        raw = re.findall(r"[a-z0-9]+", query.lower())
        extra = [_stem(s) for w in raw for s in QUERY_SYNONYMS.get(w, [])]
        return base + [t for t in extra if t not in base]

    def _idf(self, term: str) -> float:
        n = len(self.chunks)
        df = self.doc_freq.get(term, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, top_k: int = 3, min_score: float = 0.5) -> list[dict]:
        q_terms = self._expand(query)
        if not q_terms:
            return []
        scored: list[tuple[float, int, list[str]]] = []
        for i, (chunk, tf) in enumerate(zip(self.chunks, self.term_freqs)):
            score = 0.0
            matched: list[str] = []
            dl = len(chunk.tokens)
            for term in set(q_terms):
                f = tf.get(term, 0)
                if not f:
                    continue
                matched.append(term)
                denom = f + BM25_K1 * (1 - BM25_B + BM25_B * dl / self.avg_len)
                score += self._idf(term) * f * (BM25_K1 + 1) / denom
            if score >= min_score:
                scored.append((score, i, matched))
        scored.sort(key=lambda s: (-s[0], s[1]))
        results = []
        for score, i, matched in scored[:top_k]:
            c = self.chunks[i]
            results.append({                       # shape follows Section 9 of the policy doc
                "section": c.section,
                "heading": c.title,
                "text": c.text,
                "reference": c.reference,
                "score": round(score, 3),
                "matched_terms": sorted(matched),
                "chunk_id": c.chunk_id,
            })
        return results


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the policy search index.")
    ap.add_argument("--doc", type=Path, default=DEFAULT_DOC)
    ap.add_argument("--out", type=Path, default=DEFAULT_INDEX)
    args = ap.parse_args()
    idx = PolicyIndex.build(args.doc)
    idx.save(args.out)
    print(f"Indexed {len(idx.chunks)} chunks from {args.doc.name} "
          f"(vocabulary {len(idx.doc_freq)} terms) -> {args.out}")


if __name__ == "__main__":
    main()
