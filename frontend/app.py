import streamlit as st
import requests
import base64
import os

st.set_page_config(page_title="ChatIFU", page_icon="🩺", layout="wide", initial_sidebar_state="collapsed")

# Custom CSS for a beautiful, simple, OpenEvidence-like UI
st.markdown("""
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
    /* Global Font */
    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
        background-color: #FFFFFF;
        color: #111827;
    }
    
    /* Hide the top padding in Streamlit */
    .block-container { 
        padding-top: 2rem !important; 
        padding-bottom: 0rem !important; 
        max-width: 1400px;
    }
    
    /* Logo / Title */
    .logo-container {
        display: flex;
        align-items: center;
        margin-bottom: 2rem;
    }
    .logo-icon {
        font-size: 28px;
        margin-right: 12px;
    }
    .logo-text {
        font-size: 28px;
        font-weight: 700;
        letter-spacing: -0.5px;
        color: #111827;
    }
    
    /* Clean Search Bar */
    .stTextInput>div>div>input {
        border-radius: 12px !important;
        padding: 16px 20px !important;
        font-size: 16px !important;
        border: 1px solid #E5E7EB !important;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -1px rgba(0, 0, 0, 0.03) !important;
        transition: all 0.2s ease;
        background-color: #F9FAFB !important;
    }
    .stTextInput>div>div>input:focus {
        border-color: #3B82F6 !important;
        box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.2) !important;
        background-color: #FFFFFF !important;
    }
    
    /* Answer Box */
    .answer-box {
        background-color: #FFFFFF;
        border: 1px solid #E5E7EB;
        padding: 24px;
        border-radius: 12px;
        font-size: 16px;
        line-height: 1.6;
        color: #374151;
        margin-top: 24px;
        margin-bottom: 24px;
        box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.1);
    }
    .answer-box p {
        margin-bottom: 0;
    }
    
    /* Typography */
    h3 {
        font-size: 18px !important;
        font-weight: 600 !important;
        color: #111827 !important;
        margin-bottom: 12px !important;
    }
    
    /* Source Citations */
    .streamlit-expanderHeader {
        font-size: 14px !important;
        font-weight: 500 !important;
        color: #4B5563 !important;
        border-radius: 8px !important;
        background-color: #F9FAFB !important;
    }
    .source-box {
        font-size: 14px;
        line-height: 1.5;
        color: #6B7280;
        padding: 16px;
        border-radius: 8px;
        background-color: #F3F4F6;
        margin-bottom: 12px;
    }
    
    /* Buttons */
    .stButton>button {
        border-radius: 8px !important;
        border: 1px solid #E5E7EB !important;
        background-color: #FFFFFF !important;
        color: #374151 !important;
        font-weight: 500 !important;
        padding: 4px 12px !important;
        transition: all 0.2s ease;
    }
    .stButton>button:hover {
        background-color: #F3F4F6 !important;
        border-color: #D1D5DB !important;
        color: #111827 !important;
    }
    
    /* Right Column Empty State */
    .empty-state {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        height: 100%;
        min-height: 600px;
        background-color: #F9FAFB;
        border-radius: 12px;
        border: 1px dashed #D1D5DB;
        color: #9CA3AF;
        text-align: center;
        padding: 40px;
    }
    .empty-state svg {
        margin-bottom: 16px;
        width: 48px;
        height: 48px;
        opacity: 0.5;
    }
    
    /* PDF Viewer Container */
    .pdf-viewer-header {
        font-size: 14px;
        font-weight: 500;
        color: #4B5563;
        margin-bottom: 12px;
        padding-bottom: 12px;
        border-bottom: 1px solid #E5E7EB;
    }
    </style>
""", unsafe_allow_html=True)

API_URL = "http://localhost:8000/search"
HIGHLIGHT_API_URL = "http://localhost:8000/highlight_pdf"

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
        response = requests.post(API_URL, json={"query": q, "limit": 5}, timeout=30)
        if response.status_code == 200:
            data = response.json()
            st.session_state.answer = data.get("answer", "No answer generated.")
            st.session_state.sources = data.get("results", [])
            
            # Auto-select the first PDF if available
            if st.session_state.sources:
                first_src = st.session_state.sources[0]
                first_source = first_src.get('metadata', {}).get('source')
                first_content = first_src.get('content', '')
                if first_source:
                    st.session_state.selected_pdf = first_source
                    load_highlighted_pdf(first_source, first_content)
        else:
            st.session_state.answer = f"Error: {response.status_code} - {response.text}"
    except Exception as e:
        st.session_state.answer = f"Connection failed: {str(e)}"

def load_highlighted_pdf(filename: str, text_to_highlight: str):
    """Hits the backend highlight API and stores the returned PDF bytes in session state."""
    try:
        response = requests.post(HIGHLIGHT_API_URL, json={"filename": filename, "text_to_highlight": text_to_highlight}, timeout=15)
        if response.status_code == 200:
            st.session_state.pdf_bytes = response.content
            # Read the page number from custom header (default to 1)
            page_header = response.headers.get("X-Found-Page", "1")
            st.session_state.pdf_page = int(page_header) if page_header.isdigit() else 1
        else:
            st.error(f"Failed to load PDF: {response.status_code}")
    except Exception as e:
        st.error(f"Failed to connect to PDF server: {str(e)}")


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
    st.markdown("""
        <div class="logo-container">
            <span class="logo-icon">🩺</span>
            <span class="logo-text">ChatIFU</span>
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
                
                # Clean up the document name for display
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
