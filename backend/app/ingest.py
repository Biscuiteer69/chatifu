import os
import fitz  # PyMuPDF
import google.generativeai as genai
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

def get_text_chunks(text, chunk_size=1024, overlap=128):
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks

def ingest_documents():
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")

    if not all([supabase_url, supabase_key, gemini_key]):
        print("❌ Missing API keys in backend/.env.")
        return

    print("🔌 Connecting to Supabase...")
    supabase: Client = create_client(supabase_url, supabase_key)
    
    print("🧠 Initializing Gemini API...")
    genai.configure(api_key=gemini_key)
    
    data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
    if not os.path.exists(data_dir) or not os.listdir(data_dir):
        print(f"📁 No PDFs found in {data_dir}. Please place a test PDF there and run again.")
        os.makedirs(data_dir, exist_ok=True)
        return

    print(f"📄 Reading PDFs from {data_dir}...")
    
    for filename in os.listdir(data_dir):
        if not filename.lower().endswith(".pdf"):
            continue
            
        filepath = os.path.join(data_dir, filename)
        doc = fitz.open(filepath)
        full_text = ""
        for page in doc:
            full_text += page.get_text() + "\n"
            
        chunks = get_text_chunks(full_text)
        print(f"Extracted {len(chunks)} chunks from {filename}")
        
        for i, chunk in enumerate(chunks):
            # Generate embedding
            result = genai.embed_content(
                model="models/gemini-embedding-001",
                content=chunk,
                task_type="retrieval_document",
                output_dimensionality=768
            )
            embedding = result['embedding']
            
            # Insert into Supabase
            supabase.table("documents").insert({
                "content": chunk,
                "metadata": {"source": filename, "chunk_index": i},
                "embedding": embedding
            }).execute()
            
    print("✅ Ingestion complete! Data is now in Supabase.")

if __name__ == "__main__":
    ingest_documents()