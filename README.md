# Jons MCP Docs.rs

A Python MCP (Model Context Protocol) server for looking up Rust documentation from docs.rs. This server provides advanced navigation capabilities including pagination, multi-page lookup, and search functionality.

## Overview

This MCP server enables AI assistants to browse and search Rust crate documentation from docs.rs. Unlike simple documentation fetchers, this server provides a complete navigation system that allows:

- Fetching main documentation pages with pagination
- Looking up specific documentation pages (structs, traits, modules, etc.)
- Searching within crate documentation
- Converting docs.rs URLs to navigation keys for seamless browsing

## Features

- **Main Page Lookup**: Fetch the main documentation page for any Rust crate with configurable version
- **Multi-Page Lookup**: Fetch multiple documentation pages in a single request with combined pagination
- **Search Functionality**: Search within a crate's documentation and get paginated results
- **Smart Pagination**: Character-based pagination for handling large documentation
- **Link Extraction**: Automatically extract and convert links to navigation keys
- **Version Control**: Support for specific crate versions or default to latest
- **HTML to Markdown**: Clean markdown output for better readability

## Installation

### Using uv (recommended)

```bash
# Clone the repository
git clone https://github.com/jonmmease/jons-mcp-docs.rs
cd jons-mcp-docs.rs

# Install with uv
uv pip install -e .

# Run the server
uv run jons-mcp-docs-rs
```

### Using pip

```bash
# Clone the repository
git clone https://github.com/jonmmease/jons-mcp-docs.rs
cd jons-mcp-docs.rs

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install
pip install -e .

# Run the server
jons-mcp-docs-rs
```

### Adding to Claude Desktop

Add this server to Claude Desktop by running:

```bash
claude mcp add jons-mcp-docs-rs uvx -- --from git+https://github.com/jonmmease/jons-mcp-docs.rs jons-mcp-docs-rs
```

## Tools

### lookup_main_page

Fetch the main documentation page for a Rust crate.

**Parameters:**
- `crate_name` (required): The name of the crate (e.g., "datafusion")
- `version` (optional): The version to look up (defaults to "latest")
- `offset` (optional): Character offset for pagination (default: 0)
- `limit` (optional): Maximum number of characters to return (default: 50)

**Example:**
```json
{
  "crate_name": "tokio",
  "version": "latest",
  "offset": 0,
  "limit": 1000
}
```

**Response:**
```json
{
  "crate": "tokio",
  "version": "latest",
  "content": "# Crate tokio\n\n...",
  "total_characters": 25000,
  "offset": 0,
  "limit": 1000,
  "has_more": true,
  "links": [
    {
      "key": "tokio/latest/tokio/runtime/struct.Runtime",
      "text": "Runtime",
      "url": "https://docs.rs/tokio/latest/tokio/runtime/struct.Runtime.html"
    }
  ],
  "total_links": 150,
  "url": "https://docs.rs/tokio/latest/"
}
```

### lookup_pages

Fetch one or more specific documentation pages.

**Parameters:**
- `pages` (required): List of page keys (e.g., ["tokio/latest/tokio/runtime/struct.Runtime"])
- `version` (optional): Override version for all pages
- `offset` (optional): Character offset for combined pagination (default: 0)
- `limit` (optional): Maximum characters to return across all pages (default: 50)

**Example:**
```json
{
  "pages": [
    "datafusion/latest/datafusion/dataframe/struct.DataFrame",
    "datafusion/latest/datafusion/execution/context/struct.SessionContext"
  ],
  "offset": 0,
  "limit": 5000
}
```

**Response:**
```json
{
  "pages": [
    {
      "key": "datafusion/latest/datafusion/dataframe/struct.DataFrame",
      "url": "https://docs.rs/datafusion/latest/datafusion/dataframe/struct.DataFrame.html",
      "content_length": 15000,
      "links_count": 45
    }
  ],
  "content": "# Page: datafusion/latest/datafusion/dataframe/struct.DataFrame\n\n...",
  "total_characters": 30000,
  "offset": 0,
  "limit": 5000,
  "has_more": true,
  "pages_count": 2
}
```

### search_docs

Search within a crate's documentation.

**Parameters:**
- `crate_name` (required): The name of the crate to search in
- `query` (required): The search query
- `version` (optional): The version to search (defaults to "latest")
- `offset` (optional): Result offset for pagination (default: 0)
- `limit` (optional): Maximum number of results to return (default: 50)

**Example:**
```json
{
  "crate_name": "datafusion",
  "query": "udf",
  "version": "latest",
  "offset": 0,
  "limit": 10
}
```

**Response:**
```json
{
  "crate": "datafusion",
  "version": "latest",
  "query": "udf",
  "results": [
    {
      "key": "datafusion/latest/datafusion/physical_plan/udf",
      "title": "Module datafusion::physical_plan::udf",
      "url": "https://docs.rs/datafusion/latest/datafusion/physical_plan/udf/index.html",
      "snippet": ""
    }
  ],
  "total_results": 25,
  "offset": 0,
  "limit": 10,
  "has_more": true,
  "search_url": "https://docs.rs/datafusion/latest/datafusion/?search=udf"
}
```

## Navigation System

The server converts docs.rs URLs into navigation keys that can be used with the `lookup_pages` tool. This allows AI assistants to navigate the documentation site without dealing with URLs directly.

### URL to Key Conversion Examples:

- `https://docs.rs/tokio/latest/tokio/index.html` → `tokio/latest/tokio/index`
- `https://docs.rs/datafusion/latest/datafusion/dataframe/struct.DataFrame.html` → `datafusion/latest/datafusion/dataframe/struct.DataFrame`

### Using Navigation Keys:

1. Call `lookup_main_page` to get the main page and extract links
2. Use the `key` field from links with `lookup_pages` to navigate to specific pages
3. Search results also provide keys for direct navigation

## Development

### Setup

```bash
# Clone the repository
git clone https://github.com/jonmmease/jons-mcp-docs.rs
cd jons-mcp-docs.rs

# Create virtual environment with uv
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install in development mode
uv pip install -e ".[dev,test]"
```

### Running Tests

```bash
# Run all tests
uv run pytest

# Run with verbose output
uv run pytest -v

# Run specific test file
uv run pytest tests/test_docs_rs.py

# Run with coverage
uv run pytest --cov=src
```

### Code Quality

```bash
# Format code
black src tests

# Lint code
ruff check src tests
```

## Architecture

The server is built with:

- **FastMCP**: Framework for building MCP servers
- **httpx**: Async HTTP client for fetching documentation
- **html2text**: Converting HTML to clean markdown
- **BeautifulSoup4**: HTML parsing for link extraction

Key design decisions:

1. **Pagination Strategy**: Character-based pagination allows precise control over response sizes
2. **Key-based Navigation**: URLs are converted to stable keys for consistent navigation
3. **Combined Page Loading**: Multiple pages can be fetched and paginated together
4. **Async Operations**: All HTTP operations are async for better performance

## Troubleshooting

### Server won't start
- Ensure Python 3.10+ is installed
- Check all dependencies: `uv pip install -e .`
- Look for error messages in stderr output

### Documentation not loading
- Verify the crate name is correct
- Check if the crate exists on docs.rs
- Try with a known working crate like "tokio" or "serde"

### Search not returning results
- Some crates may have limited search functionality
- Try broader search terms
- Check the search_url in the response to see the actual search performed

## License

MIT License - see LICENSE file for details.