import fitz
import os
import tempfile

def highlight_text_in_pdf(pdf_path: str, text_to_highlight: str) -> str:
    """
    Opens a PDF, searches for the exact text (or chunks of it),
    draws a yellow highlight bounding box, and returns the path to a temp PDF.
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    doc = fitz.open(pdf_path)
    
    # Text might be broken across lines or pages, making exact match hard.
    # We can search for the first 50 chars as a heuristic.
    search_term = text_to_highlight[:50].replace('\n', ' ').strip()

    highlight_found = False
    
    for page in doc:
        # Search for the text on the current page
        text_instances = page.search_for(search_term)
        
        if text_instances:
            highlight_found = True
            for inst in text_instances:
                highlight = page.add_highlight_annot(inst)
                highlight.update()
                
    # Save to a temporary file
    fd, temp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    
    doc.save(temp_path)
    doc.close()
    
    return temp_path
