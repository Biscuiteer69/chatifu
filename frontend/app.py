import streamlit as st
import requests

st.set_page_config(page_title="OpenIFU", page_icon="⚕️", layout="centered")

# Custom CSS for a clean, OpenEvidence-like UI
st.markdown("""
    <style>
    .main { max-width: 800px; margin: 0 auto; }
    .stTextInput>div>div>input {
        border-radius: 20px;
        padding: 15px 25px;
        font-size: 16px;
        border: 1px solid #ddd;
        box-shadow: 0px 4px 10px rgba(0,0,0,0.05);
    }
    .answer-box {
        background-color: #f9fbfd;
        border-left: 4px solid #1a73e8;
        padding: 20px;
        border-radius: 8px;
        font-size: 16px;
        line-height: 1.6;
        color: #333;
        margin-top: 20px;
        margin-bottom: 20px;
    }
    .source-box {
        font-size: 13px;
        color: #666;
        padding: 10px;
        border: 1px solid #eee;
        border-radius: 5px;
        margin-bottom: 10px;
        background-color: #fff;
    }
    .logo-title { font-size: 36px; font-weight: 700; color: #1a73e8; margin-bottom: 5px; text-align: center; }
    .subtitle { font-size: 16px; color: #666; margin-bottom: 40px; text-align: center; }
    </style>
""", unsafe_allow_html=True)

st.markdown("<div class='logo-title'>⚕️ OpenIFU</div>", unsafe_allow_html=True)
st.markdown("<div class='subtitle'>Instant, verified answers from medical device Instructions for Use.</div>", unsafe_allow_html=True)

API_URL = "http://localhost:8000/search"

# Initialize session state for holding search results
if "query" not in st.session_state:
    st.session_state.query = ""
if "answer" not in st.session_state:
    st.session_state.answer = None
if "sources" not in st.session_state:
    st.session_state.sources = []

def perform_search():
    q = st.session_state.search_input
    if not q:
        return
        
    st.session_state.query = q
    st.session_state.answer = None
    st.session_state.sources = []
    
    try:
        response = requests.post(API_URL, json={"query": q, "limit": 5}, timeout=30)
        if response.status_code == 200:
            data = response.json()
            st.session_state.answer = data.get("answer", "No answer generated.")
            st.session_state.sources = data.get("results", [])
        else:
            st.session_state.answer = f"Error: {response.status_code} - {response.text}"
    except Exception as e:
        st.session_state.answer = f"Connection failed: {str(e)}"

# Centered Search Bar
st.text_input(
    "", 
    placeholder="Ask a question (e.g., 'What are the sterilization instructions for the Stryker battery drill?')", 
    key="search_input", 
    on_change=perform_search
)

# Display Results
if st.session_state.answer:
    st.markdown("### Synthesized Answer")
    st.markdown(f"<div class='answer-box'>{st.session_state.answer}</div>", unsafe_allow_html=True)
    
    if st.session_state.sources:
        st.markdown("### Citations & Sources")
        for i, src in enumerate(st.session_state.sources):
            with st.expander(f"[{i+1}] {src['metadata'].get('source', 'Unknown Document')} (Match: {src['similarity']:.2f})"):
                st.markdown(f"<div class='source-box'>{src['content']}</div>", unsafe_allow_html=True)
