#!/usr/bin/env python3
"""
FastMCP server that provides tools for looking up Rust documentation from docs.rs.
"""
import atexit
import logging
import os
import re
import signal
import sys
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import html2text
import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

# Configure logging - reduce to WARNING to avoid MCP protocol interference
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "WARNING"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Create FastMCP server instance
mcp = FastMCP("jons-mcp-docs-rs")

# Configuration
BASE_URL = "https://docs.rs"
DEFAULT_VERSION = "latest"
DEFAULT_LIMIT = 50
MAX_CONTENT_LENGTH = 100000  # Maximum content length per request

# Initialize HTML to text converter
h2t = html2text.HTML2Text()
h2t.ignore_links = False
h2t.body_width = 0  # Don't wrap lines
h2t.skip_internal_links = False
h2t.single_line_break = True


def normalize_crate_path(path: str) -> str:
    """Normalize a crate path for consistent handling."""
    # Remove leading/trailing slashes
    path = path.strip("/")
    # Ensure no double slashes
    path = re.sub(r"/+", "/", path)
    return path


def convert_url_to_key(url: str) -> str:
    """Convert a docs.rs URL to a key for the page lookup tool."""
    parsed = urlparse(url)

    # Remove https://docs.rs/ prefix
    if parsed.netloc == "docs.rs":
        path = parsed.path.strip("/")
        # Remove .html extension if present
        if path.endswith(".html"):
            path = path[:-5]
        return path

    # For relative URLs, just clean them up
    path = url.strip("/")
    if path.endswith(".html"):
        path = path[:-5]
    return path


def extract_links_as_keys(html_content: str, base_url: str) -> list[dict[str, str]]:
    """Extract links from HTML and convert them to keys."""
    soup = BeautifulSoup(html_content, "html.parser")
    links = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)

        # Convert relative URLs to absolute
        absolute_url = urljoin(base_url, href)

        # Only include docs.rs links
        if "docs.rs" in absolute_url:
            key = convert_url_to_key(absolute_url)
            if key and text:
                links.append({"key": key, "text": text, "url": absolute_url})

    return links


async def fetch_page(url: str) -> tuple[str, str]:
    """Fetch a page from docs.rs and return (html, final_url)."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(url, timeout=30.0)
        response.raise_for_status()
        return response.text, str(response.url)


def paginate_content(
    content: str, offset: int = 0, limit: int = DEFAULT_LIMIT
) -> tuple[str, int]:
    """Paginate content by character count."""
    total_length = len(content)

    # Handle offset beyond content length
    if offset >= total_length:
        return "", total_length

    # Extract the requested slice
    end = min(offset + limit, total_length)
    paginated = content[offset:end]

    return paginated, total_length


@mcp.tool()
async def lookup_main_page(
    crate_name: str,
    version: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Look up the main documentation page for a Rust crate.

    Args:
        crate_name: The name of the crate (e.g., "datafusion")
        version: The version to look up (defaults to "latest")
        offset: Character offset for pagination
        limit: Maximum number of characters to return

    Returns:
        A dictionary containing the documentation content, links, and pagination info
    """
    try:
        version = version or DEFAULT_VERSION
        url = f"{BASE_URL}/{crate_name}/{version}/"

        # Fetch the page
        html_content, final_url = await fetch_page(url)

        # Extract links as keys
        links = extract_links_as_keys(html_content, final_url)

        # Convert HTML to markdown
        markdown_content = h2t.handle(html_content)

        # Paginate the content
        paginated_content, total_chars = paginate_content(
            markdown_content, offset, limit
        )

        return {
            "crate": crate_name,
            "version": version,
            "content": paginated_content,
            "total_characters": total_chars,
            "offset": offset,
            "limit": limit,
            "has_more": (offset + limit) < total_chars,
            "links": links[:20],  # Limit to first 20 links to avoid huge responses
            "total_links": len(links),
            "url": final_url,
        }

    except httpx.HTTPError as e:
        return {
            "error": f"Failed to fetch documentation: {str(e)}",
            "crate": crate_name,
            "version": version,
        }
    except Exception as e:
        return {
            "error": f"Unexpected error: {str(e)}",
            "crate": crate_name,
            "version": version,
        }


@mcp.tool()
async def lookup_pages(
    pages: list[str],
    version: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Look up one or more specific documentation pages.

    Args:
        pages: List of page keys (e.g., ["datafusion/latest/datafusion/dataframe/struct.DataFrame"])
        version: Override version for all pages (optional)
        offset: Character offset for combined pagination
        limit: Maximum number of characters to return across all pages

    Returns:
        A dictionary containing the combined documentation content with pagination
    """
    results = []
    combined_content = []

    for page_key in pages:
        try:
            # Normalize the page key
            page_key = normalize_crate_path(page_key)

            # If version override is provided, replace the version in the key
            if version and "/" in page_key:
                parts = page_key.split("/")
                if len(parts) >= 2:
                    parts[1] = version
                    page_key = "/".join(parts)

            # Construct the URL
            url = f"{BASE_URL}/{page_key}.html"

            # Fetch the page
            html_content, final_url = await fetch_page(url)

            # Convert HTML to markdown
            markdown_content = h2t.handle(html_content)

            # Extract links for this page
            links = extract_links_as_keys(html_content, final_url)

            results.append(
                {
                    "key": page_key,
                    "url": final_url,
                    "content_length": len(markdown_content),
                    "links_count": len(links),
                }
            )

            combined_content.append(f"\n\n# Page: {page_key}\n\n{markdown_content}")

        except Exception as e:
            results.append({"key": page_key, "error": str(e)})

    # Combine all content
    full_content = "".join(combined_content)

    # Paginate the combined content
    paginated_content, total_chars = paginate_content(full_content, offset, limit)

    return {
        "pages": results,
        "content": paginated_content,
        "total_characters": total_chars,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + limit) < total_chars,
        "pages_count": len(pages),
    }


@mcp.tool()
async def search_docs(
    crate_name: str,
    query: str,
    version: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Search within a crate's documentation.

    Args:
        crate_name: The name of the crate to search in
        query: The search query
        version: The version to search (defaults to "latest")
        offset: Result offset for pagination
        limit: Maximum number of results to return

    Returns:
        A dictionary containing search results with pagination info
    """
    try:
        version = version or DEFAULT_VERSION
        # Construct search URL
        search_url = (
            f"{BASE_URL}/{crate_name}/{version}/{crate_name}/?search={quote(query)}"
        )

        # Fetch the search results page
        html_content, final_url = await fetch_page(search_url)

        # Parse the search results
        soup = BeautifulSoup(html_content, "html.parser")

        # Find search results - docs.rs uses specific structure for search results
        search_results = []

        # Look for search result items (this may need adjustment based on actual HTML structure)
        result_items = soup.find_all("div", class_="search-results") or soup.find_all(
            "a", class_="result-name"
        )

        if not result_items:
            # Fallback: look for any links that might be search results
            content_div = soup.find("div", class_="content") or soup.find("main")
            if content_div:
                links = content_div.find_all("a", href=True)
                for link in links:
                    href = link["href"]
                    text = link.get_text(strip=True)
                    if text and href:
                        key = convert_url_to_key(urljoin(final_url, href))
                        if key:
                            search_results.append(
                                {
                                    "key": key,
                                    "title": text,
                                    "url": urljoin(final_url, href),
                                    "snippet": "",  # No snippet available in basic parsing
                                }
                            )

        # Apply pagination to results
        total_results = len(search_results)
        paginated_results = search_results[offset : offset + limit]

        return {
            "crate": crate_name,
            "version": version,
            "query": query,
            "results": paginated_results,
            "total_results": total_results,
            "offset": offset,
            "limit": limit,
            "has_more": (offset + limit) < total_results,
            "search_url": search_url,
        }

    except httpx.HTTPError as e:
        return {
            "error": f"Failed to search documentation: {str(e)}",
            "crate": crate_name,
            "version": version,
            "query": query,
        }
    except Exception as e:
        return {
            "error": f"Unexpected error: {str(e)}",
            "crate": crate_name,
            "version": version,
            "query": query,
        }


def cleanup():
    """Cleanup function to be called on exit."""
    logger.info("Rust docs MCP server shutting down gracefully")


# Register cleanup handler
atexit.register(cleanup)


def main():
    """Initialize and run the FastMCP server."""

    # Handle signals gracefully
    def signal_handler(sig, frame):
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal_handler)

    try:
        # Log startup
        logger.info("Starting Rust docs MCP server...")

        # Run the server
        mcp.run()
    except Exception as e:
        # Log any startup errors to stderr
        import traceback

        print(f"MCP server error: {e}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
