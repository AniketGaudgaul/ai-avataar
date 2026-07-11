"""Canonical profile card — the deterministic résumé skeleton.

Chunked retrieval returns top-k *fragments*, which is the wrong shape for broad
"give me an overview of Aniket" or recruiter-fit questions: the whole picture
arrives in pieces and something load-bearing (a role, a date, a headline metric)
can fall below the cut. This is a single, always-complete block the retrieve node
prepends as citation `[1]` when the router flags such a question
(`include_profile`), so recall of the skeleton facts is guaranteed regardless of
what the vector search returned.

Kept deterministic and in-code (not read from a file) so it ships with the
container and can't drift or go missing at runtime. It is the source's summary,
not new facts — everything here is also in the graph and the narrative.
"""

from __future__ import annotations

# One compact, factual block. Cited as "Résumé — Profile" ([1]) when injected.
PROFILE_CARD = """\
Aniket Gaudgaul — Generative AI Engineer based in Pune, India. 2+ years of \
experience in multi-agent architectures and production RAG systems. Shipped 3 \
end-to-end client AI products (biotech, retail, marketing) that collectively drove \
$80k+ in revenue. Published researcher at ECIR 2024. Winner of the Tata Trent × \
NASSCOM national AI challenge (1st of 75+ teams).

Experience:
- Yarnit Innovations — Generative AI Engineer, Aug 2024–May 2026 (Bengaluru). \
Built product apps (Humanizer, AskYarnit, Dreambrush) and 3 client projects; \
first-year evaluation 4.5/5, "Exceptional" (from the CEO).
- AlgoAnalytics — Data Science Intern, Feb–May 2024 (Remote). Cut LLM API costs \
70% by migrating GPT-3.5 → LLaMA-3-8B, RAGAS-validated with no quality loss.
- IIT Patna, AI-ML-NLP Lab — Research Intern, Apr–Oct 2023 (Remote). Built \
MedSumm; co-authored the ECIR 2024 paper.

Education: B.E. Computer Engineering (Honours in AI & ML), PVG's COET / Savitribai \
Phule Pune University, 2021–2024, CGPA 9.43/10.

Key projects: Agentic RAG Presentation Generator (biotech; Neo4j knowledge graph \
+ hybrid retrieval; −60% domain misattribution, +40% retrieval precision); \
Concept-to-Catwalk (Tata Trent × NASSCOM hackathon, 1st place; agentic \
fashion-trend pipeline over 300+ social profiles); Product Discovery AI Assistant \
(5,000+ SKUs/brand; LLM routing for 1,000+ concurrent users; sub-30s latency).

Headline skills: RAG, multi-agent systems, prompt engineering, LLM fine-tuning, \
LLM evaluation, knowledge graphs, retrieval optimization, conversational AI, \
generative image editing.

Core stack: Python, LangChain, LangGraph, LlamaIndex, OpenAI, Anthropic Claude, \
Google Gemini, Meta LLaMA, QLoRA, Neo4j, Qdrant/FAISS/Pinecone, RAGAS, LangSmith, \
Langfuse, FastAPI, Docker, AWS Bedrock, GCP Vertex AI."""
