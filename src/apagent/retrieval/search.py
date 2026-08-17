"""Contract retrieval: find the clause that answers a question.

Structured data (POs, GRNs, invoices) is served by exact-lookup tools —
retrieval has no business there, because "the PO whose id is PO-2026-1005"
is not a similarity question. Contracts are different: they are prose, the
agent does not know which section holds the answer, and the whole document
is too long to paste into every prompt. So we search: score each section
against the question and hand the agent only the best few.

Why BM25 and not embeddings + a vector store:
Six contracts of five sections each is ~30 chunks. BM25 over 30 chunks is
exact, instant, has zero dependencies and zero services to run, and every
score is explainable (term frequency times rarity). An embedding model would
add an API call, a cache, and a new failure mode to search a corpus that
fits in one screen. If the corpus ever grows to thousands of documents,
revisit; the tool interface below would not change.

Why chunks are whole sections:
Contracts are written in self-contained numbered sections, and a tolerance
clause split across two chunks is a retrieval bug you cannot fix with
ranking. Sections in our corpus are 40-90 words — small enough to hand the
model several of them.
"""

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

import pdfplumber

# BM25 constants, the textbook defaults. k1 saturates term frequency (a word
# appearing 10x is not 10x as relevant); b scales down long chunks so they
# do not win just by containing more words.
K1 = 1.5
B = 0.75

# Below this score a chunk is noise, not an answer. Returning nothing and
# saying so beats returning the least-irrelevant paragraph — the agent must
# be able to trust that "no clause found" means the contract is silent.
MIN_SCORE = 1.0


@dataclass
class Chunk:
    """One contract section, ready to be searched and quoted."""

    vendor_id: str
    source: str  # file name, so the agent can cite where a clause came from
    heading: str
    text: str


def _tokenize(text: str) -> list[str]:
    """Lowercase word tokens. '5%' becomes ['5'] — good enough: queries about
    tolerances mention the words around the number, not the number itself."""
    return re.findall(r"[a-z0-9]+", text.lower())


def load_contracts(contracts_dir: Path) -> list[Chunk]:
    """Read every vendor agreement PDF into section chunks.

    The vendor id comes from the file name (V005_supply_agreement.pdf), which
    the dataset generator guarantees. Section boundaries are the numbered
    headings ("2. Pricing and Price Variance") — the same structure the
    generator writes, so this split is reliable for our corpus. Real scanned
    contracts would need a smarter splitter; that is an extraction problem,
    not a retrieval one.
    """
    chunks: list[Chunk] = []
    for pdf_path in sorted(contracts_dir.glob("*.pdf")):
        vendor_id = pdf_path.name.split("_")[0]
        with pdfplumber.open(pdf_path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)

        # Split on numbered headings, keeping the heading with its body.
        # The lookahead split keeps the delimiter, unlike a plain split.
        parts = re.split(r"(?=^\d+\. )", text, flags=re.MULTILINE)
        for part in parts:
            part = part.strip()
            match = re.match(r"^(\d+\. [^\n]+)\n(.+)", part, flags=re.DOTALL)
            if not match:
                continue  # the title block before section 1, or the footer
            heading, body = match.group(1), match.group(2)
            # Drop the synthetic-document footer if it got glued to the last section
            body = body.split("This is a synthetic document")[0]
            chunks.append(
                Chunk(
                    vendor_id=vendor_id,
                    source=pdf_path.name,
                    heading=heading.strip(),
                    text=" ".join(body.split()),
                )
            )
    return chunks


class ContractIndex:
    """BM25 index over contract chunks. Build once, search many times."""

    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self._chunk_tokens = [_tokenize(f"{c.heading} {c.text}") for c in chunks]
        self._avg_len = sum(len(t) for t in self._chunk_tokens) / max(len(chunks), 1)
        # document frequency: in how many chunks does each term appear
        self._df: dict[str, int] = {}
        for tokens in self._chunk_tokens:
            for term in set(tokens):
                self._df[term] = self._df.get(term, 0) + 1

    def _score(self, query_terms: list[str], idx: int) -> float:
        tokens = self._chunk_tokens[idx]
        n_chunks = len(self.chunks)
        score = 0.0
        for term in query_terms:
            df = self._df.get(term, 0)
            if df == 0:
                continue
            tf = tokens.count(term)
            if tf == 0:
                continue
            idf = math.log(1 + (n_chunks - df + 0.5) / (df + 0.5))
            norm = tf * (K1 + 1) / (tf + K1 * (1 - B + B * len(tokens) / self._avg_len))
            score += idf * norm
        return score

    def search(
        self, query: str, vendor_id: str | None = None, top_k: int = 3
    ) -> list[tuple[float, Chunk]]:
        """Best chunks for the query, optionally limited to one vendor.

        The vendor filter is applied before ranking, not after — otherwise a
        strongly-worded clause from the wrong vendor's contract could push
        the right vendor's clause out of the top_k.
        """
        query_terms = _tokenize(query)
        results = []
        for idx, chunk in enumerate(self.chunks):
            if vendor_id is not None and chunk.vendor_id != vendor_id:
                continue
            score = self._score(query_terms, idx)
            if score >= MIN_SCORE:
                results.append((score, chunk))
        results.sort(key=lambda pair: pair[0], reverse=True)
        return results[:top_k]


def register_contract_tools(registry, contracts_dir: Path) -> None:
    """Register the search_vendor_contract tool into an agent tool registry.

    The index is built here, once, at registration — six small PDFs parse in
    well under a second, and rebuilding per call would re-read the files on
    every agent round.
    """
    from apagent.agent.registry import Tool

    index = ContractIndex(load_contracts(contracts_dir))

    def search_handler(args: dict) -> str:
        query = args.get("query", "")
        vendor_id = args.get("vendor_id")
        if not query:
            return "Error: 'query' is required."
        hits = index.search(query, vendor_id=vendor_id)
        if not hits:
            scope = f"for vendor {vendor_id} " if vendor_id else ""
            return (
                f"No contract clause found {scope}matching {query!r}. "
                "Treat the contract as silent and apply the default policy."
            )
        return json.dumps(
            [
                {
                    "vendor_id": chunk.vendor_id,
                    "source": chunk.source,
                    "section": chunk.heading,
                    "text": chunk.text,
                    "score": round(score, 2),
                }
                for score, chunk in hits
            ]
        )

    registry.register(
        Tool(
            name="search_vendor_contract",
            description=(
                "Search the supplier agreements for clauses relevant to a "
                "question. Call this before deciding on a discrepancy: vendors "
                "may have negotiated terms (price variance allowance, payment "
                "days) that differ from the default tolerances. Pass vendor_id "
                "to search one vendor's agreement; omit it to search all. "
                "Returns matching sections with their source file, or a "
                "message saying the contracts are silent on the topic."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "What to look for, in plain words, e.g. 'unit price variance tolerance'"
                        ),
                    },
                    "vendor_id": {
                        "type": "string",
                        "description": "Limit the search to one vendor, e.g. 'V005'",
                    },
                },
                "required": ["query"],
            },
            handler=search_handler,
        )
    )
