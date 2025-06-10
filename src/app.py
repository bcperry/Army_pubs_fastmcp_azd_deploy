from typing import Any
import httpx
from fastmcp import FastMCP
from urllib.parse import quote
from bs4 import BeautifulSoup
import re
import PyPDF2
import io
import ebooklib
from ebooklib import epub
import mimetypes
import tempfile
import os

mcp = FastMCP("Army Pubs")

# bare bones example of a FastMCP streamable-http server

# Add MCP functionality with decorators
# Constants
ARMY_PUBS_API_BASE = "https://armypubs.army.mil/ProductMaps/PubForm/ContentSearch.aspx"
USER_AGENT = "microsoft-army-pubs-mcp/0.1"

async def make_pubs_request(url: str) -> str | None:
    """Make a request to the Army publications API with proper error handling."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    async with httpx.AsyncClient() as client:
        try:
            print(f"(make_pubs_request) Requesting Army publications API: {url}")
            response = await client.get(url, headers=headers, timeout=30.0)
            print(f"(make_pubs_request) Response status code: {response.status_code}")
            response.raise_for_status()
            return response.text
        except Exception as e:
            print(f"(make_pubs_request) Error making request: {e}")
            return None

def parse_search_results(html_content: str) -> list[dict[str, Any]]:
    """Parse the HTML search results and extract reference data."""
    soup = BeautifulSoup(html_content, 'html.parser')
    results = []
    
    # Find the results table
    results_table = soup.find('div', {'id': 'MainContent_tblContentSearchResults'})
    if not results_table:
        print("(parse_search_results) No results table found")
        return results
    
    # Find all publication entries
    links = results_table.find_all('a', href=True)
    
    for i, link in enumerate(links):
        if 'epubs' in link['href'] or 'pub/eforms' in link['href']:
            # Extract title and document type
            title_text = link.get_text(strip=True)
            
            # Skip "Record Details" links
            if "Record Details" in title_text:
                continue
                
            # Parse title to extract document number and name
            title_parts = title_text.split(' — ', 1)
            doc_number = title_parts[0] if title_parts else title_text
            doc_title = title_parts[1] if len(title_parts) > 1 else ""
            
            # Extract document type from number (e.g., "TC", "AR", "ATP", etc.)
            doc_type_match = re.match(r'^([A-Z]+)', doc_number)
            doc_type = doc_type_match.group(1) if doc_type_match else "Unknown"
            
            # Get file format from the span after the link
            file_format = "pdf"  # default
            next_span = link.find_next('span')
            if next_span and 'font-size:smaller' in str(next_span):
                file_format = next_span.get_text(strip=True)
            
            # Find the description/date text (next <td> after the link)
            description_td = link.find_parent('td')
            if description_td:
                next_td = description_td.find_next_sibling('td')
                if not next_td:
                    # Look for the next row
                    next_tr = description_td.find_parent('tr').find_next_sibling('tr')
                    if next_tr:
                        next_td = next_tr.find('td')
                
                date_text = ""
                description = ""
                if next_td:
                    full_text = next_td.get_text(strip=True)
                    
                    # Extract date (look for pattern like "May 13, 2019" or "Feb 11, 2025")
                    date_match = re.search(r'(\w{3}\s+\d{1,2},\s+\d{4})', full_text)
                    if date_match:
                        date_text = date_match.group(1)
                    
                    # Clean up description text
                    description = full_text
                    
                    # Handle CAC-required documents
                    if "Common Access Card (CAC) to view it" in full_text:
                        description = "This publication or form requires Common Access Card (CAC) to view it"
            
            result = {
                "document_number": doc_number,
                "title": doc_title,
                "document_type": doc_type,
                "file_format": file_format,
                "date": date_text,
                "description": description,
                "url": link['href'] if link['href'].startswith('http') else f"https://armypubs.army.mil{link['href']}"
            }
            
            results.append(result)
    
    return results
        

@mcp.tool()
async def search_pubs(query: str) -> str:
    """Get a specific document from the Army publications website.

    Args:
        query: any string to search for in the Army publications
    """
    encoded_query = quote(query)
    url = f"{ARMY_PUBS_API_BASE}?q={encoded_query}"
    print(f"(search_pubs) Search URL: {url}")
    
    html_content = await make_pubs_request(url)
    if not html_content:
        return "Unable to fetch search results. Please try again later."
    
    # Parse the HTML and extract structured data
    results = parse_search_results(html_content)
    
    print(f"(search_pubs) Found {len(results)} publications")
    return results


async def extract_pdf_text(doc_content: bytes | str) -> str | None:
    """Extract text from a PDF document.
    
    Args:
        doc_content: The PDF content as bytes or the result from make_pubs_request
        
    Returns:
        Extracted text as string, or None if extraction fails
    """
    try:
        # If we got a string response from make_pubs_request, we need to fetch the actual PDF
        if isinstance(doc_content, str):
            print("(extract_pdf_text) Content appears to be HTML, not PDF bytes")
            return None
            
        # Create a PDF reader from bytes
        pdf_file = io.BytesIO(doc_content)
        pdf_reader = PyPDF2.PdfReader(pdf_file)
        
        # Extract text from all pages
        text = ""
        for page_num in range(len(pdf_reader.pages)):
            page = pdf_reader.pages[page_num]
            text += page.extract_text() + "\n"
        
        print(f"(extract_pdf_text) Successfully extracted text from {len(pdf_reader.pages)} pages")
        return text.strip()
        
    except Exception as e:
        print(f"(extract_pdf_text) Error extracting PDF text: {e}")
        return None

async def extract_epub_text(doc_content: bytes | str) -> str | None:
    """Extract text from an EPUB document.
    
    Args:
        doc_content: The EPUB content as bytes or the result from make_pubs_request
        
    Returns:
        Extracted text as string, or None if extraction fails
    """
    try:
        # If we got a string response from make_pubs_request, we need to fetch the actual EPUB
        if isinstance(doc_content, str):
            print("(extract_epub_text) Content appears to be HTML, not EPUB bytes")
            return None
            
        # Create a temporary file since ebooklib requires a file path
        with tempfile.NamedTemporaryFile(suffix='.epub', delete=False) as temp_file:
            temp_file.write(doc_content)
            temp_path = temp_file.name
        
        try:
            # Read the EPUB from the temporary file
            book = epub.read_epub(temp_path)
            
            # Extract text from all chapters
            text_content = []
            
            # Get all items that are documents (chapters)
            for item in book.get_items():
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    # Parse HTML content and extract text
                    soup = BeautifulSoup(item.get_content(), 'html.parser')
                    # Remove script and style elements
                    for script in soup(["script", "style"]):
                        script.decompose()
                    
                    # Get text and clean it up
                    text = soup.get_text(separator='\n', strip=True)
                    if text.strip():  # Only add non-empty content
                        text_content.append(text)
            
            full_text = "\n\n".join(text_content)
            print(f"(extract_epub_text) Successfully extracted text from {len(text_content)} chapters")
            return full_text.strip()
            
        finally:
            # Clean up temporary file
            try:
                os.unlink(temp_path)
            except:
                pass
        
    except Exception as e:
        print(f"(extract_epub_text) Error extracting EPUB text: {e}")
        return None

async def extract_document_text(doc_content: bytes | str, file_format: str = None, url: str = None) -> str | None:
    """Extract text from a document (PDF or EPUB) based on format or URL.
    
    Args:
        doc_content: The document content as bytes
        file_format: The format hint ('pdf', 'epub', 'ebook', etc.)
        url: The URL to help determine format from extension
        
    Returns:
        Extracted text as string, or None if extraction fails
    """
    # Determine format from various sources
    format_type = None
    
    if file_format:
        format_lower = file_format.lower()
        if 'pdf' in format_lower:
            format_type = 'pdf'
        elif 'epub' in format_lower or 'ebook' in format_lower:
            format_type = 'epub'
    
    # If no format hint, try to determine from URL
    if not format_type and url:
        url_lower = url.lower()
        if url_lower.endswith('.pdf'):
            format_type = 'pdf'
        elif url_lower.endswith('.epub'):
            format_type = 'epub'
        else:
            # Try to guess from MIME type
            mime_type, _ = mimetypes.guess_type(url)
            if mime_type == 'application/pdf':
                format_type = 'pdf'
            elif mime_type == 'application/epub+zip':
                format_type = 'epub'
    
    # If still no format, try to detect from content
    if not format_type and isinstance(doc_content, bytes):
        # Check for PDF magic bytes
        if doc_content.startswith(b'%PDF'):
            format_type = 'pdf'
        # Check for EPUB magic bytes (it's a ZIP file)
        elif doc_content.startswith(b'PK') and b'epub' in doc_content[:1024].lower():
            format_type = 'epub'
    
    print(f"(extract_document_text) Detected format: {format_type}")
    
    # Extract based on format
    if format_type == 'pdf':
        return await extract_pdf_text(doc_content)
    elif format_type == 'epub':
        return await extract_epub_text(doc_content)
    else:
        print(f"(extract_document_text) Unsupported or unknown format: {format_type}")
        return None
    

async def get_document_text_from_url(doc_url: str, file_format: str = None) -> str | None:
    """Download a document from URL and extract its text content (supports PDF and EPUB).
    
    Args:
        doc_url: URL of the document
        file_format: Optional format hint ('pdf', 'epub', 'ebook', etc.)
        
    Returns:
        Extracted text as string, or None if extraction fails
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/pdf,application/epub+zip,*/*",
    }
    
    async with httpx.AsyncClient() as client:
        try:
            print(f"(get_document_text_from_url) Downloading document: {doc_url}")
            response = await client.get(doc_url, headers=headers, timeout=60.0)
            response.raise_for_status()
            
            # Check content type
            content_type = response.headers.get('content-type', '')
            print(f"(get_document_text_from_url) Content-Type: {content_type}")
            
            return await extract_document_text(response.content, file_format, doc_url)
            
        except Exception as e:
            print(f"(get_document_text_from_url) Error downloading/processing document: {e}")
            # Check if the error indicates CAC authentication is required
            if "redirect" in str(e).lower() and "federation.eams.army" in str(e).lower():
                return "CAC_REQUIRED: This document requires Common Access Card (CAC) authentication"
            return None

@mcp.tool()
async def get_publication(publication_url: str) -> str:
    """Get the content of a specific publication from the Army Pubs website.

    Args:
        publication_url: the fully qualified URL of the publication
    """
    # Extract file extension from URL
    filetype = publication_url.split('.')[-1] if '.' in publication_url else None
    document_text = await get_document_text_from_url(publication_url, filetype)

    if document_text:
        print(f"Text extracted successfully ({len(document_text)} characters)")
        print("First 500 characters:")
        print(document_text[:500] + "..." if len(document_text) > 500 else document_text)
    else:
        return("Failed to extract text from the publication. It may not be a supported format or the content could not be retrieved.")

    return document_text


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)

