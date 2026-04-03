import streamlit as st
import base64
import os
from supabase import create_client, Client
import google.generativeai as genai
import fitz

st.set_page_config(page_title="ChatIFU", page_icon="🩺", layout="wide", initial_sidebar_state="collapsed")

# Custom CSS for a beautiful, simple, OpenEvidence-like UI
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    
    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
        background-color: #FFFFFF;
        color: #111827;
    }
    .block-container { 
        padding-top: 2rem !important; 
        padding-bottom: 0rem !important; 
        max-width: 1400px;
    }
    .logo-container { display: flex; align-items: center; margin-bottom: 2rem; }
    .logo-icon { font-size: 28px; margin-right: 12px; }
    .logo-text { font-size: 28px; font-weight: 700; letter-spacing: -0.5px; color: #111827; }
    .stTextInput>div>div>input {
        border-radius: 12px !important; padding: 16px 20px !important;
        font-size: 16px !important; border: 1px solid #E5E7EB !important;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05) !important;
        background-color: #F9FAFB !important;
    }
    .stTextInput>div>div>input:focus { border-color: #3B82F6 !important; background-color: #FFFFFF !important; }
    .answer-box { background-color: #FFFFFF; border: 1px solid #E5E7EB; padding: 24px; border-radius: 12px; font-size: 16px; color: #374151; margin: 24px 0; box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.1); }
    h3 { font-size: 18px !important; font-weight: 600 !important; color: #111827 !important; margin-bottom: 12px !important; }
    .streamlit-expanderHeader { font-size: 14px !important; font-weight: 500 !important; color: #4B5563 !important; border-radius: 8px !important; background-color: #F9FAFB !important; }
    .source-box { font-size: 14px; color: #6B7280; padding: 16px; border-radius: 8px; background-color: #F3F4F6; margin-bottom: 12px; }
    .stButton>button { border-radius: 8px !important; border: 1px solid #E5E7EB !important; background-color: #FFFFFF !important; color: #374151 !important; font-weight: 500 !important; padding: 4px 12px !important; }
    .empty-state { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; min-height: 600px; background-color: #F9FAFB; border-radius: 12px; border: 1px dashed #D1D5DB; color: #9CA3AF; text-align: center; padding: 40px; }
    .pdf-viewer-header { font-size: 14px; font-weight: 500; color: #4B5563; margin-bottom: 12px; padding-bottom: 12px; border-bottom: 1px solid #E5E7EB; }
    </style>
    """,
    unsafe_allow_html=True
)

# Load secrets directly in Streamlit
import os
try:
    from dotenv import load_dotenv
    # Try to load backend/.env if it exists for local testing
    env_path = os.path.join(os.path.dirname(__file__), "..", "backend", ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path)
except ImportError:
    pass

try:
    SUPABASE_URL = st.secrets.get("SUPABASE_URL") or os.environ.get("SUPABASE_URL")
    SUPABASE_KEY = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    GEMINI_KEY = st.secrets.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
except FileNotFoundError:
    # Fallback to os.environ for local testing if secrets.toml isn't set up
    SUPABASE_URL = os.environ.get("SUPABASE_URL")
    SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

# Initialize API clients
if all([SUPABASE_URL, SUPABASE_KEY, GEMINI_KEY]):
    # Strip any whitespace/newlines that might have snuck into the secrets
    SUPABASE_URL = SUPABASE_URL.strip()
    SUPABASE_KEY = SUPABASE_KEY.strip()
    GEMINI_KEY = GEMINI_KEY.strip()
    
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    genai.configure(api_key=GEMINI_KEY)
else:
    st.error("Missing API Keys! Please configure secrets in Streamlit Cloud or local environment.")
    st.stop()

# Initialize session state for holding search results
if "query" not in st.session_state:
    st.session_state.query = ""
if "answer" not in st.session_state:
    st.session_state.answer = None
if "sources" not in st.session_state:
    st.session_state.sources = []
if "selected_pdf" not in st.session_state:
    st.session_state.selected_pdf = None
if "pdf_bytes" not in st.session_state:
    st.session_state.pdf_bytes = None
if "pdf_page" not in st.session_state:
    st.session_state.pdf_page = 1

def highlight_pdf(filename: str, text_to_highlight: str):
    """Local Highlight Engine - replaces the backend call"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Try multiple possible locations
    possible_paths = [
        os.path.join(base_dir, "data", "ifus", filename),
        os.path.join(base_dir, "..", "backend", "data", filename),
        os.path.join(base_dir, "..", "backend", "data", "ifus", filename)
    ]
    
    pdf_path = None
    for p in possible_paths:
        if os.path.exists(p):
            pdf_path = p
            break
            
    if not pdf_path:
        raise Exception(f"PDF {filename} not found on server. Tried: {possible_paths}")

    doc = fitz.open(pdf_path)
    search_term = text_to_highlight[:40].replace('\n', ' ').strip()
    found_page = None

    if search_term:
        for page in doc:
            text_instances = page.search_for(search_term)
            if text_instances:
                found_page = page.number # PyMuPDF pages are 0-indexed
                for inst in text_instances:
                    highlight = page.add_highlight_annot(inst)
                    highlight.set_colors(stroke=(1, 1, 0)) # Yellow
                    highlight.update()
                break

    pdf_bytes = doc.write()
    doc.close()
    return pdf_bytes, (found_page + 1) if found_page is not None else 1

def load_highlighted_pdf(filename: str, text_to_highlight: str):
    """Highlight and store the PDF bytes directly."""
    try:
        bytes_data, page_num = highlight_pdf(filename, text_to_highlight)
        if bytes_data:
            st.session_state.pdf_bytes = bytes_data
            st.session_state.pdf_page = page_num
    except Exception as e:
        st.error(f"Failed to process PDF: {str(e)}")

def perform_search():
    q = st.session_state.search_input
    if not q:
        return
        
    st.session_state.query = q
    st.session_state.answer = None
    st.session_state.sources = []
    st.session_state.selected_pdf = None
    st.session_state.pdf_bytes = None
    
    try:
        # 1. Generate Embedding
        result = genai.embed_content(
            model="models/gemini-embedding-001",
            content=q,
            task_type="retrieval_query",
            output_dimensionality=768
        )
        query_embedding = result['embedding']
        
        # 2. Vector Search via Supabase RPC
        response = supabase.rpc(
            "match_documents",
            {
                "query_embedding": query_embedding,
                "match_count": 5,
                "filter": {}
            }
        ).execute()
        
        retrieved_docs = response.data
        if not retrieved_docs:
            st.session_state.answer = "No relevant IFU sections found."
            return
            
        # 3. Synthesize Answer
        context = "\\n\\n".join([f"Source [{i+1}] (File: {doc['metadata'].get('source', 'Unknown')}):\\n{doc['content']}" for i, doc in enumerate(retrieved_docs)])
        
        prompt = f"""You are a precise medical device assistant helping healthcare professionals.
Answer the user's question based strictly on the provided IFU document excerpts below.
If the answer is not explicitly contained in the excerpts, state that clearly. Do not guess.
Use inline citations like [1] to refer to the source excerpts.

Context:
{context}

User Query: {q}

Answer:"""
        
        model = genai.GenerativeModel('models/gemini-2.5-pro')
        gen_response = model.generate_content(prompt)
        
        st.session_state.answer = gen_response.text
        st.session_state.sources = retrieved_docs
        
        # 4. Auto-highlight first document
        if retrieved_docs:
            first_src = retrieved_docs[0]
            first_source = first_src['metadata'].get('source')
            first_content = first_src['content']
            if first_source:
                st.session_state.selected_pdf = first_source
                load_highlighted_pdf(first_source, first_content)
                
    except Exception as e:
        st.session_state.answer = f"Search failed: {str(e)}"

def display_pdf():
    """Displays the currently loaded PDF bytes in an iframe."""
    if st.session_state.pdf_bytes:
        base64_pdf = base64.b64encode(st.session_state.pdf_bytes).decode('utf-8')
        page_num = st.session_state.pdf_page
        pdf_display = f'<iframe src="data:application/pdf;base64,{base64_pdf}#page={page_num}&toolbar=0&navpanes=0&scrollbar=1" width="100%" height="800" style="border: 1px solid #E5E7EB; border-radius: 8px;" type="application/pdf"></iframe>'
        st.markdown(pdf_display, unsafe_allow_html=True)
    else:
        st.warning("📄 PDF bytes not loaded.")

# --- Layout: 2 Columns ---
col1, col2 = st.columns([1, 1.2], gap="large")

with col1:
    logo_path = os.path.join(os.path.dirname(__file__), 'logo.jpg')
    with open(logo_path, 'rb') as f:
        logo_base64 = base64.b64encode(f.read()).decode()

    st.markdown(f"""
        <div class="logo-container">
            <img class="logo-icon" src="data:image/jpeg;base64,{logo_base64}" style="width: 240px; height: auto; border-radius: 8px; object-fit: contain; margin-bottom: 10px;">
        </div>
    """, unsafe_allow_html=True)

    # Search Bar
    st.text_input(
        "", 
        placeholder="Ask a medical device question (e.g., 'Sterilization cycle for Stryker System 7?')", 
        key="search_input", 
        on_change=perform_search,
        label_visibility="collapsed"
    )

    # Display Results
    if st.session_state.answer:
        st.markdown("### Answer")
        st.markdown(f"<div class='answer-box'>{st.session_state.answer}</div>", unsafe_allow_html=True)
        
        if st.session_state.sources:
            st.markdown("### Sources")
            for i, src in enumerate(st.session_state.sources):
                doc_name = src['metadata'].get('source', 'Unknown Document')
                content_text = src['content']
                
                display_name = doc_name.replace(".pdf", "").replace("_", " ")
                if len(display_name) > 40:
                    display_name = display_name[:40] + "..."
                
                with st.expander(f"[{i+1}] {display_name}"):
                    st.markdown(f"<div class='source-box'>{content_text}</div>", unsafe_allow_html=True)
                    if st.button(f"View Highlighted Original", key=f"view_{i}"):
                        st.session_state.selected_pdf = doc_name
                        load_highlighted_pdf(doc_name, content_text)

with col2:
    if st.session_state.selected_pdf and st.session_state.pdf_bytes:
        display_name = st.session_state.selected_pdf.replace(".pdf", "").replace("_", " ")
        st.markdown(f"<div class='pdf-viewer-header'>Viewing Source: <b>{display_name}</b> (Page {st.session_state.pdf_page})</div>", unsafe_allow_html=True)
        display_pdf()
    else:
        st.markdown("""
            <div class="empty-state">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                </svg>
                <p>Search for a device and select a citation<br>to view the original document here.</p>
            </div>
        """, unsafe_allow_html=True)
