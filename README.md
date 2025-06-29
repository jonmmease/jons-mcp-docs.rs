# Jons MCP Docs.rs

A Python MCP (Model Context Protocol) server for looking up Rust documentation from docs.rs. This server provides advanced navigation capabilities including pagination, multi-page lookup, and search functionality.

## Overview

This MCP server enables AI assistants to browse and search Rust crate documentation from docs.rs. Unlike simple documentation fetchers, this server provides a complete navigation system that allows:

- Fetching main documentation pages with pagination
- Looking up specific documentation pages (structs, traits, modules, etc.)
- Searching within crate documentation
- Searching for crates by name with pagination
- Converting docs.rs URLs to navigation keys for seamless browsing
- Viewing source code for any Rust item
- Extracting code examples from documentation
- Finding trait implementors
- Analyzing crate dependencies
- Exploring module hierarchies
- Comparing API changes between versions

## Features

- **Main Page Lookup**: Fetch the main documentation page for any Rust crate with configurable version
- **Multi-Page Lookup**: Fetch multiple documentation pages in a single request with combined pagination and source availability detection
- **Search Functionality**: Search within a crate's documentation and get paginated results
- **Crate Search**: Search for crates by name across the entire docs.rs catalog
- **Source Code Viewing**: Access the source code of any Rust item directly from docs.rs
- **Code Example Extraction**: Extract and filter code examples from documentation
- **Trait Analysis**: Find all types that implement a specific trait
- **Dependency Analysis**: Analyze and extract crate dependencies from documentation
- **Module Hierarchy**: Explore the complete module structure of a crate
- **Version Comparison**: Compare API surface or content between different versions
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
      "links_count": 45,
      "source_available": true
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

**Note**: The `source_available` field indicates whether source code can be viewed for this item using the `get_source_code` tool.

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

### search_crates

Search for Rust crates by name on docs.rs.

**Parameters:**
- `query` (required): The search query for crate names
- `page` (optional): Page number (1-indexed) for pagination (default: 1)

**Example:**
```json
{
  "query": "serde",
  "page": 1
}
```

**Response:**
```json
{
  "query": "serde",
  "page": 1,
  "crates": [
    {
      "name": "serde",
      "version": "1.0.219",
      "description": "A generic serialization/deserialization framework",
      "date": "2025-06-17T02:58:14Z",
      "url": "https://docs.rs/serde/latest/serde/"
    }
  ],
  "total_on_page": 30,
  "has_next_page": true,
  "search_url": "https://docs.rs/releases/search?query=serde"
}
```

### get_source_code

Get the source code for a Rust item from the source viewer.

**Parameters:**
- `page_key` (required): The page key for the item (e.g., "tokio/latest/tokio/runtime/struct.Runtime")
- `offset` (optional): Character offset for pagination (default: 0)
- `limit` (optional): Maximum number of characters to return (default: 50)

**Example:**
```json
{
  "page_key": "datafusion/latest/datafusion/logical_expr/trait.ScalarUDFImpl",
  "offset": 0,
  "limit": 2000
}
```

**Response:**
```json
{
  "page_key": "datafusion/latest/datafusion/logical_expr/trait.ScalarUDFImpl",
  "source_code": "pub trait ScalarUDFImpl: Debug + Send + Sync {\n    ...",
  "total_characters": 5000,
  "offset": 0,
  "limit": 2000,
  "has_more": true,
  "url": "https://docs.rs/datafusion/latest/src/datafusion/logical_expr/udf.rs.html#123"
}
```

### extract_code_examples

Extract code examples from documentation.

**Parameters:**
- `crate_name` (required): The name of the crate
- `search_pattern` (optional): Pattern to search for in examples (e.g., "DataFrame")
- `version` (optional): Version of the crate (defaults to "latest")
- `max_examples` (optional): Maximum number of examples to return (default: 10)

**Example:**
```json
{
  "crate_name": "datafusion",
  "search_pattern": "SessionContext",
  "max_examples": 5
}
```

**Response:**
```json
{
  "crate": "datafusion",
  "version": "latest",
  "search_pattern": "SessionContext",
  "examples": [
    {
      "source_page": "datafusion/latest/datafusion",
      "code": "use datafusion::prelude::*;\n\nlet ctx = SessionContext::new();\n...",
      "language": "rust",
      "context": "Creating a new SessionContext"
    }
  ],
  "total_examples": 5
}
```

### find_trait_implementors

Find types that implement a specific trait.

**Parameters:**
- `crate_name` (required): The name of the crate containing the trait
- `trait_path` (required): Path to the trait (e.g., "prelude/trait.Debug")
- `version` (optional): Version of the crate (defaults to "latest")

**Example:**
```json
{
  "crate_name": "datafusion",
  "trait_path": "logical_expr/trait.ScalarUDFImpl"
}
```

**Response:**
```json
{
  "crate": "datafusion",
  "version": "latest",
  "trait_path": "logical_expr/trait.ScalarUDFImpl",
  "trait_url": "https://docs.rs/datafusion/latest/datafusion/logical_expr/trait.ScalarUDFImpl.html",
  "implementors": [
    {
      "name": "ArrayToString",
      "key": "datafusion/latest/datafusion/functions_array/struct.ArrayToString",
      "module": "functions_array"
    }
  ],
  "total_implementors": 15,
  "direct_implementors": 10,
  "blanket_implementors": 5
}
```

### analyze_dependencies

Analyze a crate's dependencies from its documentation.

**Parameters:**
- `crate_name` (required): The name of the crate
- `version` (optional): Version of the crate (defaults to "latest")

**Example:**
```json
{
  "crate_name": "tokio",
  "version": "latest"
}
```

**Response:**
```json
{
  "crate": "tokio",
  "version": "latest",
  "dependencies": {
    "direct": [
      {
        "name": "mio",
        "url": "https://docs.rs/mio",
        "context": "Event notification library"
      }
    ],
    "features": [
      {
        "name": "full",
        "dependencies": ["rt", "macros", "sync", "time"]
      }
    ],
    "total": 15
  }
}
```

### get_module_hierarchy

Get the module structure and hierarchy of a crate.

**Parameters:**
- `crate_name` (required): The name of the crate
- `start_module` (optional): Starting module path (defaults to root)
- `max_depth` (optional): Maximum depth to traverse (default: 3)
- `version` (optional): Version of the crate (defaults to "latest")

**Example:**
```json
{
  "crate_name": "datafusion",
  "max_depth": 2
}
```

**Response:**
```json
{
  "crate": "datafusion",
  "version": "latest",
  "start_module": "root",
  "modules": {
    "name": "datafusion",
    "path": "datafusion/latest/datafusion",
    "key": "datafusion/latest/datafusion",
    "submodules": [
      {
        "name": "prelude",
        "path": "datafusion/latest/datafusion/prelude",
        "items": {
          "structs": ["DataFrame", "SessionContext"],
          "traits": ["TableProvider"]
        }
      }
    ],
    "items": {
      "structs": ["DataFrame"],
      "enums": ["DataFusionError"],
      "traits": []
    }
  },
  "total_modules": 25,
  "max_depth": 2
}
```

### compare_versions

Compare documentation between two versions of a crate.

**Parameters:**
- `crate_name` (required): The name of the crate
- `version1` (required): First version to compare
- `version2` (required): Second version to compare
- `page_path` (optional): Specific page to compare (for full_content comparison)
- `comparison_type` (optional): "api_surface" (default) or "full_content"

**Example (API Surface Comparison):**
```json
{
  "crate_name": "tokio",
  "version1": "1.0.0",
  "version2": "1.35.0",
  "comparison_type": "api_surface"
}
```

**Response:**
```json
{
  "crate": "tokio",
  "version1": "1.0.0",
  "version2": "1.35.0",
  "comparison_type": "api_surface",
  "differences": {
    "added": {
      "structs": ["JoinSet", "LocalSet"],
      "functions": ["spawn_blocking"]
    },
    "removed": {
      "structs": ["Runtime::spawn"]
    },
    "common": {
      "structs": ["Runtime", "JoinHandle"]
    },
    "summary": "5 items added, 1 items removed"
  }
}
```

**Example (Full Content Comparison):**
```json
{
  "crate_name": "serde",
  "version1": "1.0.0",
  "version2": "1.0.100",
  "page_path": "ser/trait.Serialize",
  "comparison_type": "full_content"
}
```

## Quick Start Guide

### Learning from a Popular Crate

Let's explore how to understand and implement a trait by learning from DataFusion:

```python
# 1. Find real implementations of a trait
result = await find_trait_implementors("datafusion", "logical_expr/trait.ScalarUDFImpl")
# Returns 121 implementations including ArrayToString, CoalesceFunc, etc.

# 2. View the source of a specific implementation
impl = result["implementors"][0]  # Pick one implementor
source = await get_source_code(impl["key"])
# Shows the complete implementation with line numbers

# 3. Understand the module structure
hierarchy = await get_module_hierarchy("datafusion", "functions", max_depth=3)
# Shows all function modules and their organization
```

### Common Workflows

#### "How do I implement trait X?"

1. Find existing implementations:
   ```python
   impls = await find_trait_implementors("crate_name", "module/trait.TraitName")
   ```

2. Pick an implementor similar to your use case

3. View its source code:
   ```python
   source = await get_source_code(impl["key"])
   ```

4. Look for patterns in method implementations, especially required methods

#### "What examples exist for this crate?"

1. Try to extract examples:
   ```python
   examples = await extract_code_examples("crate_name")
   ```

2. If empty (common for many crates), find real usage:
   ```python
   # Find trait implementors and view their source
   impls = await find_trait_implementors("crate_name", "main_trait")
   source = await get_source_code(impls["implementors"][0]["key"])
   ```

#### "How is this crate organized?"

1. Get top-level view:
   ```python
   hierarchy = await get_module_hierarchy("crate_name")
   ```

2. Drill down into specific modules:
   ```python
   detailed = await get_module_hierarchy("crate_name", "specific_module", max_depth=3)
   ```

3. Navigate to specific items using the keys from hierarchy

## Understanding Tool Results

### When `extract_code_examples` Returns Empty

This is **NORMAL** for many popular crates! For example:
- **serde**: Hosts examples at serde.rs (separate site), not on docs.rs
- **tokio**: Has examples, but not on all pages
- **datafusion**: Most examples are in the implementors' source code

**What to do:** Use `find_trait_implementors` + `get_source_code` for real examples.

### When `analyze_dependencies` Shows Optional Dependencies

- `optional: true` means the dependency is behind a feature flag
- Check the crate's features to understand when it's included
- Dependencies marked as `dev` are only used for testing
- Dependencies marked as `build` are only used during compilation

### Understanding Debug Information

All tools provide `debug_info` when results might be unexpected:
- Explains what was searched
- Indicates why results might be empty
- Suggests alternative approaches

## Tool Philosophy

These tools embrace the reality that Rust documentation exists in multiple forms:

1. **API Reference** (always on docs.rs)
   - Type signatures, trait definitions, module structure
   - Use: `lookup_pages`, `get_module_hierarchy`

2. **Inline Examples** (sometimes on docs.rs)
   - Some crates include examples, many don't
   - Use: `extract_code_examples`

3. **Real Implementations** (always available via source)
   - The most valuable learning resource
   - Use: `find_trait_implementors` + `get_source_code`

4. **External Resources** (not accessible via these tools)
   - Many crates host tutorials elsewhere (serde.rs, tokio.rs, etc.)
   - The tools will indicate when this might be the case

## What These Tools Can and Cannot Do

### ✅ CAN DO:
- Navigate any crate's complete module structure
- Find all implementors of any public trait
- View source code with syntax highlighting and line numbers
- Analyze all dependencies and feature flags
- Compare API surfaces between versions
- Extract code examples when present
- Provide navigation keys for seamless browsing

### ❌ CANNOT DO:
- Access external documentation sites (e.g., serde.rs)
- Run or execute code examples
- Search across multiple crates simultaneously
- Access private or internal implementations
- View documentation for crates not on docs.rs

### 🤔 DEPENDS ON THE CRATE:
- Extract inline examples (many crates don't include them)
- Find usage examples in tests (not all test source is published)
- View macro implementations (depends on macro structure)

## Troubleshooting

### "Why am I getting empty results?"

1. **Check the `debug_info` field** - It explains what was searched and why it might be empty

2. **Try different approaches:**
   - No examples? → Use implementors + source code
   - No implementors? → The trait might be in a different module or crate
   - No dependencies? → The crate might be no_std or very minimal

3. **Use the navigation keys:**
   - Results include `key` fields that work with other tools
   - Example: Use a key from `find_trait_implementors` in `get_source_code`

### "How do I know which tool to use?"

- **Want to see how something is used?** → `find_trait_implementors`
- **Need the actual code?** → `get_source_code`
- **Exploring a new crate?** → `get_module_hierarchy`
- **Checking what a crate depends on?** → `analyze_dependencies`
- **Looking for examples?** → `extract_code_examples` (but expect empty for many crates)
- **Comparing versions?** → `compare_versions`

### "The tool returned an error"

- **404 errors**: The crate/version/module might not exist
- **Parsing errors**: The page might use a non-standard format
- **Network errors**: Retry the request
- **Check fallback data**: Look for `fallback_*` fields in the response

## Navigation System

The server converts docs.rs URLs into navigation keys that can be used with the `lookup_pages` tool. This allows AI assistants to navigate the documentation site without dealing with URLs directly.

All links in the documentation are automatically transformed to use the `docs.rs://` protocol, which provides a consistent format for navigation. This includes:
- docs.rs links (e.g., `https://docs.rs/...` → `docs.rs://...`)
- doc.rust-lang.org links (e.g., `https://doc.rust-lang.org/...` → `docs.rs://rust-lang/...`)
- Relative links are resolved to absolute `docs.rs://` format

### URL to Key Conversion Examples:

- `https://docs.rs/tokio/latest/tokio/index.html` → `tokio/latest/tokio/index`
- `https://docs.rs/datafusion/latest/datafusion/dataframe/struct.DataFrame.html` → `datafusion/latest/datafusion/dataframe/struct.DataFrame`

### Using Navigation Keys:

1. Call `lookup_main_page` to get the main page and extract links
2. Use the `key` field from links with `lookup_pages` to navigate to specific pages
3. Search results also provide keys for direct navigation

## Combining Tools for Maximum Insight

### Example: Deep Understanding of a Complex Trait

```python
# Goal: Understand how to implement AsyncRead trait

# 1. Find all implementors
impls = await find_trait_implementors("tokio", "io/trait.AsyncRead")

# 2. Group by module to understand organization patterns
by_module = {}
for impl in impls["implementors"]:
    module = impl["module"]
    by_module.setdefault(module, []).append(impl)

# 3. Study different implementation strategies
for module, implementors in by_module.items():
    print(f"\nModule {module}:")
    for impl in implementors[:2]:  # First 2 from each module
        source = await get_source_code(impl["key"])
        # Analyze implementation patterns

# 4. Check how the trait evolved
versions = await compare_versions("tokio", "1.0.0", "1.35.0")
# See what methods were added/removed
```

### Example: Exploring a New Crate

```python
# Goal: Understand DataFusion's architecture

# 1. Start with dependencies
deps = await analyze_dependencies("datafusion")
print(f"Built on: {[d['name'] for d in deps['dependencies']['direct'][:5]]}")

# 2. Explore module structure
hierarchy = await get_module_hierarchy("datafusion", max_depth=2)
# Identify key modules: logical_expr, physical_plan, execution

# 3. Find core traits
logical_page = await lookup_pages(["datafusion/latest/datafusion/logical_expr"])
# Look for trait definitions in the content

# 4. See implementations
impls = await find_trait_implementors("datafusion", "logical_expr/trait.ScalarUDFImpl")
# 121 implementations! Let's study a few

# 5. Extract patterns from source
for impl in impls["implementors"][:3]:
    source = await get_source_code(impl["key"])
    # Look for common patterns in invoke() and return_type() methods
```

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