# RAG module (future phase)

Reserved for retrieval over **product documents and descriptions only** —
never numeric/structured data, which is always served by SQL.

Planned components:
- `embedder.py` — embed product docs
- `vector_store.py` — pgvector (on the same RDS) or OpenSearch
- `retriever.py` — top-k context for descriptive questions
- routing in `query_service`: numeric → SQL agent, descriptive → RAG

Kept empty in Faz 1 so the architecture stays explicit and open.
