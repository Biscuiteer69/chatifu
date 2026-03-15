-- Enable pgvector extension
create extension if not exists vector;

-- Drop table and function if they exist for clean slate
drop function if exists match_documents;
drop table if exists documents;

-- Create table to store document chunks
create table documents (
    id uuid primary key default gen_random_uuid(),
    content text,
    metadata jsonb,
    embedding vector(768) -- 768 dimensions for Gemini embeddings
);

-- Create an HNSW index for fast nearest-neighbor search
create index on documents using hnsw (embedding vector_ip_ops);

-- Create match_documents function for similarity search
create function match_documents (
  query_embedding vector(768),
  match_count int DEFAULT null,
  filter jsonb DEFAULT '{}'
) returns table (
  id uuid,
  content text,
  metadata jsonb,
  similarity float
)
language plpgsql
as $$
#variable_conflict use_column
begin
  return query
  select
    id,
    content,
    metadata,
    1 - (documents.embedding <=> query_embedding) as similarity
  from documents
  where metadata @> filter
  order by documents.embedding <=> query_embedding
  limit match_count;
end;
$$;