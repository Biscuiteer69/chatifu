from fastapi import FastAPI, HTTPException
import os
from pydantic import BaseModel
from dotenv import load_dotenv
from supabase import create_client, Client
import google.generativeai as genai

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

app = FastAPI(title="IFU Search API")

class SearchRequest(BaseModel):
    query: str
    limit: int = 5

supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
gemini_key = os.environ.get("GEMINI_API_KEY")

if all([supabase_url, supabase_key, gemini_key]):
    supabase: Client = create_client(supabase_url, supabase_key)
    genai.configure(api_key=gemini_key)
else:
    supabase = None

@app.get("/health")
def health_check():
    return {"status": "ok", "db_connected": supabase is not None}

@app.post("/search")
async def search_ifus(request: SearchRequest):
    if not supabase:
        raise HTTPException(status_code=500, detail="API keys not configured")
    
    try:
        # Generate embedding for the search query
        result = genai.embed_content(
            model="models/gemini-embedding-001",
            content=request.query,
            task_type="retrieval_query",
            output_dimensionality=768
        )
        query_embedding = result['embedding']
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate embedding: {str(e)}")

    # Call the match_documents Postgres function via Supabase RPC
    response = supabase.rpc(
        "match_documents",
        {
            "query_embedding": query_embedding,
            "match_count": request.limit,
            "filter": {}
        }
    ).execute()
    
    retrieved_docs = response.data

    if not retrieved_docs:
        return {"answer": "No relevant IFU sections found.", "results": []}

    # Synthesize the answer using Gemini
    context = "\n\n".join([f"Source [{i+1}] (File: {doc['metadata'].get('source', 'Unknown')}):\n{doc['content']}" for i, doc in enumerate(retrieved_docs)])
    
    prompt = f"""You are a precise medical device assistant helping healthcare professionals.
Answer the user's question based strictly on the provided IFU document excerpts below.
If the answer is not explicitly contained in the excerpts, state that clearly. Do not guess.
Use inline citations like [1] to refer to the source excerpts.

Context:
{context}

User Query: {request.query}

Answer:"""

    try:
        model = genai.GenerativeModel('gemini-1.5-pro')
        gen_response = model.generate_content(prompt)
        answer = gen_response.text
    except Exception as e:
        answer = f"Could not generate synthesis. Raw context retrieved.\n\nError: {str(e)}"

    return {"answer": answer, "results": retrieved_docs}
