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


def transform_markdown_links(markdown_content: str, base_url: str) -> str:
    """Transform markdown links to use docs.rs:// protocol.

    This function:
    1. Converts docs.rs URLs to docs.rs:// protocol
    2. Resolves relative URLs based on the base URL
    3. Converts doc.rust-lang.org URLs to docs.rs://rust-lang/ format
    """
    # Parse base URL to get the current page context
    parsed_base = urlparse(base_url)

    # Regular expression to find markdown links
    link_pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

    def transform_link(match):
        link_text = match.group(1)
        link_url = match.group(2)

        # Skip if already using docs.rs:// protocol
        if link_url.startswith("docs.rs://"):
            return match.group(0)

        # Parse the URL
        parsed = urlparse(link_url)

        # Handle absolute URLs
        if parsed.netloc:
            if parsed.netloc == "docs.rs":
                # Convert docs.rs URLs to docs.rs:// protocol
                path = parsed.path.strip("/")
                if path.endswith(".html"):
                    path = path[:-5]
                return f"[{link_text}](docs.rs://{path})"
            elif parsed.netloc == "doc.rust-lang.org":
                # Convert rust-lang.org URLs to docs.rs://rust-lang/ format
                path = parsed.path.strip("/")
                if path.endswith(".html"):
                    path = path[:-5]
                # Replace 'stable' or 'nightly' with the version
                # e.g., /nightly/alloc/string/struct.String -> rust-lang/nightly/alloc/string/struct.String
                return f"[{link_text}](docs.rs://rust-lang/{path})"
            else:
                # Keep other absolute URLs as-is
                return match.group(0)

        # Handle relative URLs
        else:
            # Resolve relative URL based on the base URL
            if parsed_base.netloc == "docs.rs":
                # Get the base path without the file name
                base_path = parsed_base.path.strip("/")
                if base_path.endswith(".html"):
                    # Remove the file part to get the directory
                    base_parts = base_path.split("/")
                    base_parts = base_parts[:-1]  # Remove file
                    base_path = "/".join(base_parts)

                # Resolve the relative path
                if link_url.startswith("../"):
                    # Go up directories
                    parts = base_path.split("/")
                    relative_parts = link_url.split("/")

                    # Count how many directories to go up
                    up_count = 0
                    for part in relative_parts:
                        if part == "..":
                            up_count += 1
                        else:
                            break

                    # Remove directories from base path
                    if up_count > 0 and len(parts) >= up_count:
                        parts = parts[:-up_count]

                    # Add the remaining relative path
                    remaining = "/".join(relative_parts[up_count:])
                    if remaining:
                        parts.append(remaining)

                    resolved_path = "/".join(parts)
                elif not link_url.startswith("/"):
                    # Relative to current directory
                    resolved_path = f"{base_path}/{link_url}"
                else:
                    # Absolute path on the same domain
                    resolved_path = link_url.strip("/")

                # Remove .html extension
                if resolved_path.endswith(".html"):
                    resolved_path = resolved_path[:-5]

                return f"[{link_text}](docs.rs://{resolved_path})"
            else:
                # For non-docs.rs base URLs, keep relative URLs as-is
                return match.group(0)

    # Transform all links in the content
    return link_pattern.sub(transform_link, markdown_content)


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


def find_source_link(html_content: str, base_url: str) -> str | None:
    """Find the source code link in HTML content."""
    soup = BeautifulSoup(html_content, "html.parser")
    
    # Look for <a class="src"> or <a class="srclink">
    source_link = soup.find("a", class_="src") or soup.find("a", class_="srclink")
    
    if source_link and source_link.get("href"):
        # Convert to absolute URL
        return urljoin(base_url, source_link["href"])
    
    return None


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
        A dictionary containing:
        - crate: The crate name
        - version: The version being viewed
        - content: The documentation content as markdown with transformed links
        - total_characters: Total length of the full content
        - offset: The starting position of this chunk
        - limit: Maximum characters requested
        - has_more: Boolean indicating if more content exists beyond this chunk
        - links: List of extracted links (limited to first 20), each containing:
            - key: Navigation key to use with lookup_pages tool
            - text: Display text of the link
            - url: Full URL of the link
        - total_links: Total number of links found on the page
        - url: The final URL after any redirects

        The 'key' values in links can be passed directly to the lookup_pages tool
        to navigate to those specific documentation pages. For example, if a link
        has key "tokio/latest/tokio/runtime/struct.Runtime", you can pass this
        exact string to lookup_pages to view that struct's documentation.

        Links in the markdown content are transformed to use the docs.rs:// protocol:
        - docs.rs links: [text](docs.rs://crate/version/path)
        - rust-lang.org links: [text](docs.rs://rust-lang/version/path)
        - Relative links are resolved to absolute docs.rs:// links

        This allows easy navigation by extracting the path from docs.rs:// links
        and using it with the lookup_pages tool.
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

        # Transform links to use docs.rs:// protocol
        markdown_content = transform_markdown_links(markdown_content, final_url)

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
        pages: List of page keys or docs.rs:// URLs
               (e.g., ["datafusion/latest/datafusion/dataframe/struct.DataFrame"] or
                ["docs.rs://tokio/latest/tokio/runtime/struct.Runtime"])
        version: Override version for all pages (optional)
        offset: Character offset for combined pagination
        limit: Maximum number of characters to return across all pages

    Returns:
        A dictionary containing:
        - pages: List of page results, each containing:
            - key: The page key that was requested
            - url: The final URL of the page
            - content_length: Total length of this page's content
            - links_count: Number of links found on this page
            - source_available: Boolean indicating if source code is available
            - error: Error message if the page failed to load (optional)
        - content: Combined markdown content from all pages with transformed links
        - total_characters: Total length of all combined content
        - offset: The starting position of this chunk
        - limit: Maximum characters requested
        - has_more: Boolean indicating if more content exists beyond this chunk
        - pages_count: Number of pages requested

        Page keys can be obtained from:
        1. The 'key' field in links returned by lookup_main_page
        2. The 'key' field in search results from search_docs
        3. Manually constructed using the pattern: "crate/version/path/to/item"
        4. Extracted from docs.rs:// links in markdown content
        5. Using "rust-lang/version/path" for Rust standard library docs

        Special handling:
        - Keys starting with "rust-lang/" fetch from doc.rust-lang.org
        - Keys can include the "docs.rs://" prefix (it will be stripped)
        - All links in returned content use the docs.rs:// protocol

        Example: To view DataFrame documentation after finding it in search results,
        pass its key "datafusion/latest/datafusion/dataframe/struct.DataFrame"
        or "docs.rs://datafusion/latest/datafusion/dataframe/struct.DataFrame"
        to this tool.
    """
    results = []
    combined_content = []

    for page_key in pages:
        try:
            # Normalize the page key
            page_key = normalize_crate_path(page_key)

            # Handle docs.rs:// protocol
            if page_key.startswith("docs.rs://"):
                page_key = page_key[10:]  # Remove "docs.rs://" prefix

            # Check if this is a rust-lang documentation request
            if page_key.startswith("rust-lang/"):
                # Extract the path after rust-lang/
                rust_path = page_key[10:]  # Remove "rust-lang/"
                # Construct URL for doc.rust-lang.org
                url = f"https://doc.rust-lang.org/{rust_path}.html"
            else:
                # If version override is provided, replace the version in the key
                if version and "/" in page_key:
                    parts = page_key.split("/")
                    if len(parts) >= 2:
                        parts[1] = version
                        page_key = "/".join(parts)

                # Construct the URL for docs.rs
                url = f"{BASE_URL}/{page_key}.html"

            # Fetch the page
            html_content, final_url = await fetch_page(url)

            # Convert HTML to markdown
            markdown_content = h2t.handle(html_content)

            # Transform links to use docs.rs:// protocol
            markdown_content = transform_markdown_links(markdown_content, final_url)

            # Extract links for this page
            links = extract_links_as_keys(html_content, final_url)
            
            # Check for source code link
            source_url = find_source_link(html_content, final_url)

            results.append(
                {
                    "key": page_key,
                    "url": final_url,
                    "content_length": len(markdown_content),
                    "links_count": len(links),
                    "source_available": source_url is not None,
                    "source_url": source_url,  # Include for internal use
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
        A dictionary containing:
        - crate: The crate name that was searched
        - version: The version that was searched
        - query: The search query used
        - results: List of search results, each containing:
            - key: Navigation key to use with lookup_pages tool
            - title: Display title of the result
            - url: Full URL of the result
            - snippet: Text snippet (currently empty in basic parsing)
        - total_results: Total number of results found
        - offset: The starting position in the results
        - limit: Maximum results requested
        - has_more: Boolean indicating if more results exist beyond this chunk
        - search_url: The actual search URL used

        The 'key' field in each result can be passed to the lookup_pages tool
        to view the full documentation for that item. This enables navigation
        from search results directly to the relevant documentation pages.
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


@mcp.tool()
async def search_crates(
    query: str,
    page: int = 1,
) -> dict[str, Any]:
    """Search for Rust crates by name.

    Args:
        query: The search query for crate names
        page: Page number (1-indexed) for pagination

    Returns:
        A dictionary containing:
        - query: The search query used
        - page: The current page number
        - crates: List of found crates, each containing:
            - name: The crate name
            - version: Latest version of the crate
            - description: Brief description of the crate
            - date: Publication date (ISO format)
            - url: Direct URL to the crate's documentation
        - total_on_page: Number of crates on this page
        - has_next_page: Boolean indicating if more pages are available
        - search_url: The actual search URL used
        - error: Error message if something went wrong (optional)

        Note: docs.rs uses token-based pagination internally, but this tool
        abstracts it to simple page numbers. To get results from page 2+,
        the tool will automatically fetch intermediate pages as needed.

        The URLs in the crate results point directly to each crate's main
        documentation page, which can then be explored using lookup_main_page.
    """
    try:
        # For page 1, use the simple search URL
        if page == 1:
            search_url = f"{BASE_URL}/releases/search?query={quote(query)}"
        else:
            # For subsequent pages, we need to fetch pages sequentially
            # because pagination uses tokens, not page numbers
            current_url = f"{BASE_URL}/releases/search?query={quote(query)}"

            for i in range(1, page):
                # Fetch the current page
                html_content, _ = await fetch_page(current_url)
                soup = BeautifulSoup(html_content, "html.parser")

                # Find the next page link
                next_link = None
                pagination_div = soup.find("div", class_="pagination")
                if pagination_div:
                    for link in pagination_div.find_all("a"):
                        if "Next" in link.get_text(strip=True) and link.get("href"):
                            next_link = link
                            break

                if not next_link:
                    # No more pages available
                    return {
                        "query": query,
                        "page": page,
                        "crates": [],
                        "has_next_page": False,
                        "error": f"Page {page} not found (max page reached: {i})",
                    }

                # Update URL for next iteration
                current_url = urljoin(BASE_URL, next_link["href"])

            search_url = current_url

        # Fetch the requested page
        html_content, final_url = await fetch_page(search_url)
        soup = BeautifulSoup(html_content, "html.parser")

        # Parse crate results
        crates = []
        release_links = soup.find_all("a", class_="release")

        for link in release_links:
            name_div = link.find("div", class_="name")
            desc_div = link.find("div", class_="description")
            date_div = link.find("div", class_="date")

            if name_div:
                # Parse crate name and version from "crate-name-version" format
                full_name = name_div.get_text(strip=True)
                # Find the last hyphen followed by a version number
                match = re.match(r"^(.+)-(\d+\.\d+\.\d+(?:-[\w.]+)?)$", full_name)
                if match:
                    crate_name = match.group(1)
                    version = match.group(2)
                else:
                    # Fallback: treat the whole thing as the name
                    crate_name = full_name
                    version = "unknown"

                crate_info = {
                    "name": crate_name,
                    "version": version,
                    "description": desc_div.get_text(strip=True) if desc_div else "",
                    "date": date_div.get("title", "") if date_div else "",
                    "url": urljoin(BASE_URL, link["href"]) if link.get("href") else "",
                }
                crates.append(crate_info)

        # Check if there's a next page
        # Look in pagination div for more reliable detection
        pagination_div = soup.find("div", class_="pagination")
        has_next_page = False
        if pagination_div:
            for link in pagination_div.find_all("a"):
                link_text = link.get_text(strip=True)
                if "Next" in link_text and link.get("href"):
                    has_next_page = True
                    break

        return {
            "query": query,
            "page": page,
            "crates": crates,
            "total_on_page": len(crates),
            "has_next_page": has_next_page,
            "search_url": final_url,
        }

    except httpx.HTTPError as e:
        return {
            "error": f"Failed to search crates: {str(e)}",
            "query": query,
            "page": page,
        }
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}", "query": query, "page": page}


@mcp.tool()
async def get_source_code(
    page_key: str,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Get the source code for a Rust item.
    
    Args:
        page_key: Page key (same format as lookup_pages)
        offset: Character offset for pagination
        limit: Maximum number of characters to return
    
    Returns:
        A dictionary containing:
        - key: The page key that was requested
        - content: Source code content
        - total_characters: Total length of the source code
        - offset: The starting position of this chunk
        - limit: Maximum characters requested
        - has_more: Boolean indicating if more content exists
        - total_lines: Total number of lines in the source
        - language: Programming language (usually "rust")
        - error: Error message if source not available (optional)
        
    Note: Source code must be available for the page (check source_available
    in lookup_pages result). The source is returned as syntax-highlighted
    code extracted from the HTML source viewer.
    """
    try:
        # First, get the documentation page to find the source link
        pages_result = await lookup_pages([page_key], limit=1)
        
        if "error" in pages_result or not pages_result.get("pages"):
            return {
                "key": page_key,
                "error": "Failed to fetch documentation page"
            }
        
        page_info = pages_result["pages"][0]
        
        if "error" in page_info:
            return {
                "key": page_key,
                "error": page_info["error"]
            }
        
        if not page_info.get("source_available"):
            return {
                "key": page_key,
                "error": "Source code not available for this item"
            }
        
        source_url = page_info.get("source_url")
        if not source_url:
            return {
                "key": page_key,
                "error": "Source URL not found"
            }
        
        # Fetch the source code page
        html_content, final_url = await fetch_page(source_url)
        
        # Parse the source code from HTML
        soup = BeautifulSoup(html_content, "html.parser")
        
        # docs.rs shows source in a <pre class="rust"> or similar
        code_element = soup.find("pre", class_="rust") or soup.find("pre")
        
        if not code_element:
            # Try alternative: numbered lines in divs
            numbered_lines = soup.find_all("span", class_="line-numbers")
            if numbered_lines:
                # Extract code from line-numbered format
                code_lines = []
                for line_elem in soup.find_all("span", id=lambda x: x and x.isdigit()):
                    code_lines.append(line_elem.get_text())
                source_code = "\n".join(code_lines)
            else:
                return {
                    "key": page_key,
                    "error": "Could not extract source code from page"
                }
        else:
            # Extract text from pre element
            source_code = code_element.get_text()
        
        # Count lines
        total_lines = source_code.count('\n') + 1
        
        # Paginate the source code
        paginated_content, total_chars = paginate_content(source_code, offset, limit)
        
        return {
            "key": page_key,
            "content": paginated_content,
            "total_characters": total_chars,
            "offset": offset,
            "limit": limit,
            "has_more": (offset + limit) < total_chars,
            "total_lines": total_lines,
            "language": "rust",
            "source_url": final_url
        }
        
    except httpx.HTTPError as e:
        return {
            "key": page_key,
            "error": f"Failed to fetch source code: {str(e)}"
        }
    except Exception as e:
        return {
            "key": page_key,
            "error": f"Unexpected error: {str(e)}"
        }


@mcp.tool()
async def extract_code_examples(
    crate_name: str,
    module_path: str | None = None,
    filter_text: str | None = None,
    only_complete: bool = False,
) -> dict[str, Any]:
    """Extract code examples from documentation.
    
    Args:
        crate_name: The name of the crate
        module_path: Optional module path to search within (e.g., "dataframe")
        filter_text: Optional text to filter examples (case-insensitive)
        only_complete: If True, only return examples that appear to be complete programs
    
    Returns:
        A dictionary containing:
        - crate: The crate name
        - examples: List of code examples, each containing:
            - code: The code example text
            - language: Language tag (usually "rust")
            - context: Surrounding text context (up to 200 chars)
            - module: Module where the example was found
            - is_complete: Boolean indicating if it appears to be a complete example
        - total_found: Total number of examples found
        - filtered_count: Number after filtering (if filter applied)
        
    Complete examples are detected by looking for main() functions or
    test functions. This is a heuristic and may not be perfect.
    """
    try:
        # Determine which page to fetch
        if module_path:
            page_key = f"{crate_name}/latest/{crate_name}/{module_path}"
        else:
            page_key = f"{crate_name}/latest/{crate_name}"
        
        # Fetch the documentation page
        result = await lookup_main_page(crate_name, limit=MAX_CONTENT_LENGTH)
        
        if "error" in result:
            return {
                "crate": crate_name,
                "error": result["error"]
            }
        
        content = result["content"]
        
        # Extract code blocks using regex
        # Match ```rust or ```{.rust or ``` blocks
        code_pattern = re.compile(
            r'```(?:rust|{\.rust.*?}|)\s*\n(.*?)\n```',
            re.DOTALL | re.MULTILINE
        )
        
        examples = []
        
        for match in code_pattern.finditer(content):
            code = match.group(1).strip()
            
            # Skip empty blocks
            if not code:
                continue
            
            # Determine if it's a complete example
            is_complete = (
                "fn main()" in code or
                "#[test]" in code or
                "#[cfg(test)]" in code or
                (code.count("{") == code.count("}") and "fn " in code and code.count("{") > 2)
            )
            
            # Skip if only_complete is True and this isn't complete
            if only_complete and not is_complete:
                continue
            
            # Skip if filter_text is provided and not found
            if filter_text and filter_text.lower() not in code.lower():
                continue
            
            # Get context (text before the code block)
            start_pos = max(0, match.start() - 200)
            context = content[start_pos:match.start()].strip()
            if start_pos > 0:
                context = "..." + context
            
            # Extract module from context or use main
            module = "main"
            if "## " in context:
                # Look for module header
                module_match = re.search(r'## (?:Module |Struct |Trait |Function )?(\w+)', context)
                if module_match:
                    module = module_match.group(1)
            
            examples.append({
                "code": code,
                "language": "rust",
                "context": context[-200:],  # Limit context length
                "module": module,
                "is_complete": is_complete
            })
        
        # If we have many examples, prioritize complete ones
        if len(examples) > 20 and not only_complete:
            complete = [e for e in examples if e["is_complete"]]
            incomplete = [e for e in examples if not e["is_complete"]]
            examples = complete + incomplete[:20 - len(complete)]
        
        return {
            "crate": crate_name,
            "module_path": module_path,
            "examples": examples[:50],  # Limit to 50 examples
            "total_found": len(examples),
            "filtered_count": len(examples) if filter_text or only_complete else None
        }
        
    except Exception as e:
        return {
            "crate": crate_name,
            "error": f"Failed to extract examples: {str(e)}"
        }


@mcp.tool()
async def find_trait_implementors(
    crate_name: str,
    trait_path: str,
    version: str = "latest",
) -> dict[str, Any]:
    """Find all types that implement a specific trait.
    
    Args:
        crate_name: The name of the crate containing the trait
        trait_path: Path to the trait (e.g., "logical_expr/trait.ScalarUDFImpl")
        version: Version of the crate (defaults to "latest")
    
    Returns:
        A dictionary containing:
        - crate: The crate name
        - trait_path: The trait path
        - trait_name: The trait name extracted from the path
        - implementors: List of implementors, each containing:
            - type_name: Name of the implementing type
            - key: Navigation key for the type's documentation
            - in_crate: Boolean indicating if it's in the same crate
            - module: Module containing the type
        - foreign_implementors: Count of implementors from other crates
        - total_implementors: Total count
        - error: Error message if trait not found (optional)
        
    Note: This extracts information from the "Implementors" section of trait
    documentation pages. Foreign implementors (from other crates) may have
    limited information available.
    """
    try:
        # Construct the trait page key
        page_key = f"{crate_name}/{version}/{crate_name}/{trait_path}"
        
        # Fetch the trait documentation page HTML directly
        url = f"{BASE_URL}/{page_key}.html"
        html_content, final_url = await fetch_page(url)
        
        # Parse HTML to find implementors
        soup = BeautifulSoup(html_content, "html.parser")
        
        # Extract trait name from path
        trait_name = trait_path.split(".")[-1] if "." in trait_path else trait_path.split("/")[-1]
        
        implementors = []
        foreign_count = 0
        
        # Look for implementors section
        # docs.rs typically has <h2 id="implementors">Implementors</h2>
        implementors_section = soup.find("h2", id="implementors")
        
        if implementors_section:
            # Find the next sibling div or section that contains the implementors
            current = implementors_section.find_next_sibling()
            
            while current and current.name not in ["h2", "h1"]:
                # Look for implementor entries
                if current.name == "div" and "impl" in current.get("class", []):
                    # Extract implementor information
                    code_elem = current.find("code")
                    if code_elem:
                        impl_text = code_elem.get_text()
                        
                        # Parse the impl text to extract type name
                        # Pattern: "impl TraitName for TypeName"
                        impl_match = re.search(r'impl\s+(?:\S+\s+for\s+)?(\S+)', impl_text)
                        if impl_match:
                            type_name = impl_match.group(1)
                            
                            # Try to find a link to the type
                            type_link = current.find("a", href=True)
                            if type_link:
                                href = type_link["href"]
                                # Convert to navigation key
                                absolute_url = urljoin(final_url, href)
                                key = convert_url_to_key(absolute_url)
                                
                                # Determine module from key
                                key_parts = key.split("/")
                                if len(key_parts) > 3:
                                    module = "/".join(key_parts[3:-1])
                                else:
                                    module = "root"
                                
                                implementors.append({
                                    "type_name": type_name,
                                    "key": key,
                                    "in_crate": crate_name in key,
                                    "module": module
                                })
                            else:
                                # No link, might be a local/private type
                                implementors.append({
                                    "type_name": type_name,
                                    "key": None,
                                    "in_crate": True,
                                    "module": "unknown"
                                })
                
                current = current.find_next_sibling()
        
        # Also check for foreign implementors section
        foreign_section = soup.find("h2", id="foreign-impls")
        if foreign_section:
            # Count foreign implementors (we can't get full details)
            current = foreign_section.find_next_sibling()
            while current and current.name not in ["h2", "h1"]:
                if current.name == "div" and "impl" in current.get("class", []):
                    foreign_count += 1
                current = current.find_next_sibling()
        
        # Alternative: Look for a simpler list format
        if not implementors:
            # Try finding ul.impl-list or similar
            impl_list = soup.find("ul", class_=lambda x: x and "impl" in x)
            if impl_list:
                for li in impl_list.find_all("li"):
                    link = li.find("a")
                    if link and link.get("href"):
                        type_name = link.get_text(strip=True)
                        href = link["href"]
                        absolute_url = urljoin(final_url, href)
                        key = convert_url_to_key(absolute_url)
                        
                        implementors.append({
                            "type_name": type_name,
                            "key": key,
                            "in_crate": crate_name in key,
                            "module": "unknown"
                        })
        
        return {
            "crate": crate_name,
            "version": version,
            "trait_path": trait_path,
            "trait_name": trait_name,
            "implementors": implementors,
            "foreign_implementors": foreign_count,
            "total_implementors": len(implementors) + foreign_count,
            "page_url": final_url
        }
        
    except httpx.HTTPError as e:
        return {
            "crate": crate_name,
            "trait_path": trait_path,
            "error": f"Failed to fetch trait documentation: {str(e)}"
        }
    except Exception as e:
        return {
            "crate": crate_name,
            "trait_path": trait_path,
            "error": f"Unexpected error: {str(e)}"
        }


@mcp.tool()
async def analyze_dependencies(
    crate_name: str,
    version: str = "latest",
) -> dict[str, Any]:
    """Get crate dependencies and feature flags.
    
    Args:
        crate_name: The name of the crate to analyze
        version: Version of the crate (defaults to "latest")
    
    Returns:
        A dictionary containing:
        - crate: The crate name
        - version: The crate version
        - dependencies: List of runtime dependencies, each containing:
            - name: Dependency crate name
            - version_req: Version requirement string
            - optional: Boolean indicating if it's optional
            - features: List of features enabled for this dependency
        - dev_dependencies: List of development dependencies (same format)
        - build_dependencies: List of build dependencies (same format)
        - features: Dictionary of feature flags, each containing:
            - default: List of crates/features enabled by default
            - [feature_name]: List of crates/features enabled by this feature
        - total_dependencies: Total count across all categories
        - error: Error message if analysis fails (optional)
        
    Note: This information is extracted from the crate's main documentation
    page sidebar, which shows dependencies and features from Cargo.toml.
    """
    try:
        # Fetch the crate's main page HTML
        url = f"{BASE_URL}/{crate_name}/{version}/"
        html_content, final_url = await fetch_page(url)
        
        # Parse HTML
        soup = BeautifulSoup(html_content, "html.parser")
        
        dependencies = []
        dev_dependencies = []
        build_dependencies = []
        features = {}
        
        # Look for dependencies in the sidebar
        # docs.rs typically shows these in <div class="block dependencies">
        dep_sections = soup.find_all("div", class_="block")
        
        for section in dep_sections:
            header = section.find(["h3", "h2"])
            if not header:
                continue
                
            header_text = header.get_text(strip=True).lower()
            
            if "dependencies" in header_text:
                # Determine dependency type
                is_dev = "dev" in header_text
                is_build = "build" in header_text
                
                # Find dependency list
                dep_list = section.find("ul")
                if dep_list:
                    for li in dep_list.find_all("li"):
                        # Parse dependency entry
                        dep_text = li.get_text(strip=True)
                        
                        # Extract name and version
                        # Format: "name version (optional)"
                        match = re.match(r'(\S+)\s+([^\s(]+)(?:\s+\((.*?)\))?', dep_text)
                        if match:
                            dep_name = match.group(1)
                            dep_version = match.group(2)
                            dep_flags = match.group(3) or ""
                            
                            dep_entry = {
                                "name": dep_name,
                                "version_req": dep_version,
                                "optional": "optional" in dep_flags,
                                "features": []  # docs.rs doesn't show enabled features
                            }
                            
                            # Check if it's also a link
                            link = li.find("a")
                            if link and link.get("href"):
                                dep_entry["docs_url"] = urljoin(BASE_URL, link["href"])
                            
                            if is_dev:
                                dev_dependencies.append(dep_entry)
                            elif is_build:
                                build_dependencies.append(dep_entry)
                            else:
                                dependencies.append(dep_entry)
            
            elif "features" in header_text:
                # Parse features section
                feature_list = section.find("ul")
                if feature_list:
                    for li in feature_list.find_all("li"):
                        feature_text = li.get_text(strip=True)
                        
                        # Parse feature format: "feature_name = [dep1, dep2]"
                        match = re.match(r'(\S+)\s*=\s*\[(.*?)]', feature_text)
                        if match:
                            feature_name = match.group(1)
                            feature_deps = [
                                dep.strip().strip('"') 
                                for dep in match.group(2).split(",") 
                                if dep.strip()
                            ]
                            features[feature_name] = feature_deps
                        else:
                            # Simple feature flag
                            features[feature_text] = []
        
        # Alternative: Look for dependency information in a different format
        if not dependencies and not dev_dependencies and not build_dependencies:
            # Try to find in the main content area
            content = soup.find("div", class_="content") or soup.find("main")
            if content:
                # Look for sections with dependency info
                for heading in content.find_all(["h2", "h3"]):
                    if "dependencies" in heading.get_text(strip=True).lower():
                        next_elem = heading.find_next_sibling()
                        if next_elem and next_elem.name == "ul":
                            for li in next_elem.find_all("li"):
                                dep_text = li.get_text(strip=True)
                                # Simple parsing
                                parts = dep_text.split()
                                if parts:
                                    dependencies.append({
                                        "name": parts[0],
                                        "version_req": parts[1] if len(parts) > 1 else "*",
                                        "optional": False,
                                        "features": []
                                    })
        
        # Extract actual version from the page
        actual_version = version
        version_elem = soup.find("span", class_="version") or soup.find("div", class_="version")
        if version_elem:
            version_text = version_elem.get_text(strip=True)
            # Extract version number
            version_match = re.search(r'(\d+\.\d+\.\d+(?:-[\w.]+)?)', version_text)
            if version_match:
                actual_version = version_match.group(1)
        
        return {
            "crate": crate_name,
            "version": actual_version,
            "dependencies": dependencies,
            "dev_dependencies": dev_dependencies,
            "build_dependencies": build_dependencies,
            "features": features,
            "total_dependencies": len(dependencies) + len(dev_dependencies) + len(build_dependencies),
            "page_url": final_url
        }
        
    except httpx.HTTPError as e:
        return {
            "crate": crate_name,
            "version": version,
            "error": f"Failed to fetch crate information: {str(e)}"
        }
    except Exception as e:
        return {
            "crate": crate_name,
            "version": version,
            "error": f"Unexpected error: {str(e)}"
        }


@mcp.tool()
async def get_module_hierarchy(
    crate_name: str,
    start_module: str | None = None,
    max_depth: int = 3,
    version: str = "latest",
) -> dict[str, Any]:
    """Get the module structure and hierarchy of a crate.
    
    Args:
        crate_name: The name of the crate
        start_module: Optional starting module path (defaults to root)
        max_depth: Maximum depth to traverse (default 3)
        version: Version of the crate (defaults to "latest")
    
    Returns:
        A dictionary containing:
        - crate: The crate name
        - version: The crate version
        - start_module: The starting module path
        - modules: Hierarchical module structure, each module containing:
            - name: Module name
            - path: Full module path
            - key: Navigation key for the module
            - submodules: List of child modules (recursive)
            - items: Dictionary of items in the module:
                - structs: List of struct names
                - enums: List of enum names
                - traits: List of trait names
                - functions: List of function names
                - types: List of type alias names
        - total_modules: Total count of modules found
        - error: Error message if analysis fails (optional)
        
    Note: This extracts the module structure from the navigation sidebar
    and module documentation pages. Deep hierarchies may require multiple
    requests.
    """
    try:
        # Construct starting page
        if start_module:
            page_key = f"{crate_name}/{version}/{crate_name}/{start_module}"
        else:
            page_key = f"{crate_name}/{version}/{crate_name}"
        
        # Recursive function to build module hierarchy
        async def explore_module(module_path: str, depth: int) -> dict[str, Any] | None:
            if depth > max_depth:
                return None
            
            try:
                # Fetch module page
                url = f"{BASE_URL}/{module_path}/index.html"
                html_content, final_url = await fetch_page(url)
                soup = BeautifulSoup(html_content, "html.parser")
                
                # Extract module name from path
                module_name = module_path.split("/")[-1] if "/" in module_path else crate_name
                
                # Initialize module info
                module_info = {
                    "name": module_name,
                    "path": module_path,
                    "key": module_path,
                    "submodules": [],
                    "items": {
                        "structs": [],
                        "enums": [],
                        "traits": [],
                        "functions": [],
                        "types": [],
                        "macros": [],
                        "constants": []
                    }
                }
                
                # Look for module contents in the main area
                main_content = soup.find("div", class_="content") or soup.find("main")
                
                if main_content:
                    # Find sections for different item types
                    sections = main_content.find_all("section")
                    
                    for section in sections:
                        section_id = section.get("id", "")
                        
                        # Find items in this section
                        item_list = section.find("ul") or section.find("div", class_="item-table")
                        
                        if item_list:
                            items = []
                            for item in item_list.find_all(["li", "div"], class_=lambda x: x and "item" in x):
                                link = item.find("a")
                                if link and link.get_text(strip=True):
                                    items.append(link.get_text(strip=True))
                            
                            # Categorize items by section ID
                            if "structs" in section_id:
                                module_info["items"]["structs"] = items
                            elif "enums" in section_id:
                                module_info["items"]["enums"] = items
                            elif "traits" in section_id:
                                module_info["items"]["traits"] = items
                            elif "functions" in section_id:
                                module_info["items"]["functions"] = items
                            elif "types" in section_id or "type" in section_id:
                                module_info["items"]["types"] = items
                            elif "macros" in section_id:
                                module_info["items"]["macros"] = items
                            elif "constants" in section_id or "consts" in section_id:
                                module_info["items"]["constants"] = items
                
                # Look for submodules
                # Check sidebar for module listing
                sidebar = soup.find("nav", class_="sidebar") or soup.find("div", class_="sidebar")
                
                if sidebar:
                    # Look for modules section
                    modules_section = None
                    for heading in sidebar.find_all(["h2", "h3"]):
                        if "modules" in heading.get_text(strip=True).lower():
                            modules_section = heading.find_next_sibling()
                            break
                    
                    if modules_section:
                        for link in modules_section.find_all("a"):
                            submodule_name = link.get_text(strip=True)
                            if submodule_name and link.get("href"):
                                # Build submodule path
                                submodule_path = f"{module_path}/{submodule_name}"
                                
                                # Recursively explore submodule
                                submodule_info = await explore_module(submodule_path, depth + 1)
                                if submodule_info:
                                    module_info["submodules"].append(submodule_info)
                
                # Alternative: Look for modules in main content
                if not module_info["submodules"] and main_content:
                    modules_heading = main_content.find(
                        ["h2", "h3"], 
                        id=lambda x: x and "modules" in x
                    )
                    
                    if modules_heading:
                        modules_list = modules_heading.find_next_sibling(["ul", "div"])
                        if modules_list:
                            for item in modules_list.find_all(["li", "a"]):
                                link = item if item.name == "a" else item.find("a")
                                if link and link.get_text(strip=True):
                                    submodule_name = link.get_text(strip=True)
                                    submodule_path = f"{module_path}/{submodule_name}"
                                    
                                    submodule_info = await explore_module(submodule_path, depth + 1)
                                    if submodule_info:
                                        module_info["submodules"].append(submodule_info)
                
                return module_info
                
            except Exception:
                # Module doesn't exist or can't be accessed
                return None
        
        # Start exploration
        root_module = await explore_module(page_key, 0)
        
        if not root_module:
            return {
                "crate": crate_name,
                "version": version,
                "error": "Failed to fetch module hierarchy"
            }
        
        # Count total modules
        def count_modules(module: dict) -> int:
            count = 1
            for submodule in module.get("submodules", []):
                count += count_modules(submodule)
            return count
        
        total_modules = count_modules(root_module)
        
        return {
            "crate": crate_name,
            "version": version,
            "start_module": start_module or "root",
            "modules": root_module,
            "total_modules": total_modules,
            "max_depth": max_depth
        }
        
    except httpx.HTTPError as e:
        return {
            "crate": crate_name,
            "version": version,
            "error": f"Failed to fetch module hierarchy: {str(e)}"
        }
    except Exception as e:
        return {
            "crate": crate_name,
            "version": version,
            "error": f"Unexpected error: {str(e)}"
        }


@mcp.tool()
async def compare_versions(
    crate_name: str,
    version1: str,
    version2: str,
    page_path: str | None = None,
    comparison_type: str = "api_surface"
) -> dict[str, Any]:
    """Compare documentation between two versions of a crate.
    
    Args:
        crate_name: The name of the crate
        version1: First version to compare
        version2: Second version to compare  
        page_path: Optional specific page/module to compare (e.g., "module/struct.Name")
        comparison_type: Type of comparison - "api_surface" (default) or "full_content"
    
    Returns:
        A dictionary containing:
        - crate: The crate name
        - version1: First version
        - version2: Second version
        - page_path: The compared page path (if specified)
        - comparison_type: The type of comparison performed
        - differences: Object containing comparison results:
            For api_surface:
                - added: Items present in version2 but not version1
                - removed: Items present in version1 but not version2
                - common: Items present in both versions
                - summary: Brief summary of changes
            For full_content:
                - content1: Markdown content from version1
                - content2: Markdown content from version2
                - length_change: Character count difference
        - error: Error message if comparison fails (optional)
        
    Note: This tool helps identify API changes between versions. Use api_surface
    for a quick overview of added/removed items, or full_content for detailed
    comparison of specific pages.
    """
    try:
        if comparison_type not in ["api_surface", "full_content"]:
            return {
                "crate": crate_name,
                "version1": version1,
                "version2": version2,
                "error": "Invalid comparison_type. Use 'api_surface' or 'full_content'"
            }
        
        if comparison_type == "full_content" and not page_path:
            return {
                "crate": crate_name,
                "version1": version1,
                "version2": version2,
                "error": "page_path is required for full_content comparison"
            }
        
        if comparison_type == "api_surface":
            # Compare the main module listings
            async def get_module_items(version: str) -> dict[str, list[str]]:
                """Extract all public items from a crate version."""
                items = {
                    "modules": [],
                    "structs": [],
                    "enums": [],
                    "traits": [],
                    "functions": [],
                    "types": [],
                    "macros": [],
                    "constants": []
                }
                
                try:
                    # Fetch main page
                    url = f"{BASE_URL}/{crate_name}/{version}/{crate_name}/index.html"
                    html_content, _ = await fetch_page(url)
                    soup = BeautifulSoup(html_content, "html.parser")
                    
                    # Look for main content area
                    main_content = soup.find("div", class_="content") or soup.find("main")
                    
                    if main_content:
                        # Find all sections
                        sections = main_content.find_all("section")
                        
                        for section in sections:
                            section_id = section.get("id", "")
                            
                            # Extract items from this section
                            item_names = []
                            item_list = section.find("ul") or section.find("div", class_="item-table")
                            
                            if item_list:
                                for item in item_list.find_all(["li", "div"], class_=lambda x: x and "item" in x):
                                    link = item.find("a")
                                    if link and link.get_text(strip=True):
                                        item_names.append(link.get_text(strip=True))
                            
                            # Categorize by section
                            if "modules" in section_id:
                                items["modules"] = item_names
                            elif "structs" in section_id:
                                items["structs"] = item_names
                            elif "enums" in section_id:
                                items["enums"] = item_names
                            elif "traits" in section_id:
                                items["traits"] = item_names
                            elif "functions" in section_id:
                                items["functions"] = item_names
                            elif "types" in section_id or "type" in section_id:
                                items["types"] = item_names
                            elif "macros" in section_id:
                                items["macros"] = item_names
                            elif "constants" in section_id or "consts" in section_id:
                                items["constants"] = item_names
                                
                except Exception:
                    pass
                
                return items
            
            # Get items for both versions
            items1 = await get_module_items(version1)
            items2 = await get_module_items(version2)
            
            # Compare items
            differences = {
                "added": {},
                "removed": {},
                "common": {},
                "summary": ""
            }
            
            # Check each item type
            for item_type in items1.keys():
                set1 = set(items1[item_type])
                set2 = set(items2[item_type])
                
                added = sorted(list(set2 - set1))
                removed = sorted(list(set1 - set2))
                common = sorted(list(set1 & set2))
                
                if added:
                    differences["added"][item_type] = added
                if removed:
                    differences["removed"][item_type] = removed
                if common:
                    differences["common"][item_type] = common
            
            # Generate summary
            total_added = sum(len(items) for items in differences["added"].values())
            total_removed = sum(len(items) for items in differences["removed"].values())
            
            if total_added == 0 and total_removed == 0:
                differences["summary"] = "No API changes detected between versions"
            else:
                summary_parts = []
                if total_added > 0:
                    summary_parts.append(f"{total_added} items added")
                if total_removed > 0:
                    summary_parts.append(f"{total_removed} items removed")
                differences["summary"] = ", ".join(summary_parts)
            
            return {
                "crate": crate_name,
                "version1": version1,
                "version2": version2,
                "comparison_type": comparison_type,
                "differences": differences
            }
            
        else:  # full_content comparison
            # Fetch content for both versions
            page_key1 = f"{crate_name}/{version1}/{crate_name}/{page_path}"
            page_key2 = f"{crate_name}/{version2}/{crate_name}/{page_path}"
            
            async def get_page_content(page_key: str) -> str | None:
                try:
                    url = f"{BASE_URL}/{page_key}.html"
                    html_content, _ = await fetch_page(url)
                    soup = BeautifulSoup(html_content, "html.parser")
                    
                    # Extract main content
                    main_content = soup.find("div", class_="docblock") or soup.find("div", class_="content")
                    
                    if main_content:
                        # Convert to markdown
                        markdown = h2t.handle(str(main_content))
                        return markdown.strip()
                    
                    return None
                    
                except Exception:
                    return None
            
            content1 = await get_page_content(page_key1)
            content2 = await get_page_content(page_key2)
            
            if content1 is None and content2 is None:
                return {
                    "crate": crate_name,
                    "version1": version1,
                    "version2": version2,
                    "page_path": page_path,
                    "error": "Could not fetch content for either version"
                }
            
            differences = {
                "content1": content1 or "(Not found in this version)",
                "content2": content2 or "(Not found in this version)",
                "length_change": (len(content2) if content2 else 0) - (len(content1) if content1 else 0)
            }
            
            return {
                "crate": crate_name,
                "version1": version1,
                "version2": version2,
                "page_path": page_path,
                "comparison_type": comparison_type,
                "differences": differences
            }
            
    except httpx.HTTPError as e:
        return {
            "crate": crate_name,
            "version1": version1,
            "version2": version2,
            "error": f"Failed to compare versions: {str(e)}"
        }
    except Exception as e:
        return {
            "crate": crate_name,
            "version1": version1,
            "version2": version2,
            "error": f"Unexpected error: {str(e)}"
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
