# ChatIFU Architecture

## Overview
ChatIFU is an AI-powered document retrieval platform for medical device IFUs. It strictly functions as a locator and search engine, never an advisor.

## Tech Stack
- **Frontend**: Next.js (App Router), React, TailwindCSS, shadcn/ui.
- **Backend (API + Ingestion)**: Python with FastAPI. Heavy document processing belongs in Python.
- **Document Processing/RAG**: LlamaIndex. Better out-of-the-box abstractions for document parsing and embedding overLangChain.
- **Database / Vector Store / Auth / Storage**: Supabase.
  - *Why?* Consolidating Clerk, Auth0, S3, and Pinecone/Weaviate into a single Postgres-backed platform (Supabase provides pgvector, edge storage, and secure auth). This drastically reduces the surface area for credential leaks, cross-service latency, and maintenance overhead.
- **LLM**: Google Gemini (via `llama-index-llms-gemini` and `llama-index-embeddings-gemini`) for embedding generation and strict query interpretation.
- **Hosting**: Vercel (Frontend), Railway or AWS ECS (FastAPI Backend).

## Core Systems
1. **Ingestion Pipeline**: Upload PDFs -> LlamaIndex parsing -> Chunking with metadata (Page, Section) -> Embedding -> Supabase Vector.
2. **Query Engine**: User Natural Language -> Semantic Search via Supabase Vector -> Retrieval of Top K Chunks -> LLM strictly formats output as citations (No paraphrasing clinical info).
