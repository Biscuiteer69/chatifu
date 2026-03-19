from fastapi import FastAPI, HTTPException, Response, Request
import os
from pydantic import BaseModel
from dotenv import load_dotenv
from supabase import create_client, Client
import google.generativeai as genai
import tempfile
import fitz

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

app = FastAPI(title="IFU Search API")

class SearchRequest(BaseModel):
    query: str
    limit: int = 5

class HighlightRequest(BaseModel):
    filename: str
    text_to_highlight: str

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

@app.post("/highlight_pdf")
async def get_highlighted_pdf(request: HighlightRequest):
    data_dir = os.path.join(os.path.dirname(__file__), '..', 'data', 'ifus')
    # fallback to just 'data' if 'data/ifus' doesn't exist
    if not os.path.exists(data_dir):
        data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
        
    pdf_path = os.path.join(data_dir, request.filename)
    
    if not os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail=f"PDF {request.filename} not found.")

    doc = fitz.open(pdf_path)
    
    # We take the first 40 characters as a search term to find the right page/block
    # Because full chunks might have paragraph breaks that mess up exact string matching
    search_term = request.text_to_highlight[:40].replace('\n', ' ').strip()
    
    found_page = None

    if search_term:
        for page in doc:
            text_instances = page.search_for(search_term)
            if text_instances:
                found_page = page.number # PyMuPDF pages are 0-indexed
                for inst in text_instances:
                    # Draw a yellow highlight
                    highlight = page.add_highlight_annot(inst)
                    highlight.set_colors(stroke=(1, 1, 0)) # Yellow
                    highlight.update()
                break # Only highlight the first occurrence to avoid overwhelming the doc

    # Save to temp bytes
    pdf_bytes = doc.write()
    doc.close()
    
    headers = {"Content-Disposition": f"inline; filename={request.filename}"}
    if found_page is not None:
        # We pass the found page back in a custom header so the frontend can scroll to it
        headers["X-Found-Page"] = str(found_page + 1)
        
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
