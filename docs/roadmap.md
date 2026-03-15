# ChatIFU Development Roadmap

## Phase 1 — Core Retrieval Engine (MVP)
- Document ingestion (PDF parsing, metadata extraction)
- Embedding generation (LlamaIndex + Gemini)
- Vector database setup (Supabase pgvector)
- Query interface (Next.js frontend)
- Passage retrieval (Strict citation formatting)

## Phase 2 — Source Viewer
- Embedded PDF page viewer
- Highlighted text passages within the source document
- Improved passage ranking and context windowing

## Phase 3 — Medical Knowledge Layer & Data Acquisition
- Structured device metadata
- Automated scraping pipelines for manufacturer sites (Medtronic, Stryker, J&J)
- User-driven upload loop (surgeons/staff upload missing IFUs in exchange for priority access)
- Advanced search filters (by manufacturer, device class, etc.)
- Manufacturer tagging and official updates

## Phase 4 — Professional Network Features
- Verified healthcare provider accounts (NPI validation, etc.)
- Device discussion threads
- Peer insights and usage notes
