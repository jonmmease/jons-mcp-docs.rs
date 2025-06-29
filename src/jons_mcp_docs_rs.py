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
