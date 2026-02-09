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
- Looking up compact item signatures
- Inspecting crate feature-flag graphs
- Listing project crates from Cargo manifests and Cargo.lock
- Looking up release notes, changelogs, and upgrade guides
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
- **Item Signatures**: Fetch compact structured signatures without full-page markdown
- **Feature Flag Analysis**: Extract feature graph, defaults, and optional dependency flags
- **Project Crate Inventory**: List direct and transitive crates for the configured project
- **Release Notes Lookup**: Aggregate release notes/changelog/upgrade guides for dependency upgrades
- **Module Hierarchy**: Explore the complete module structure of a crate
- **Version Comparison**: Compare API surface or content between different versions
- **Smart Pagination**: Character-based pagination for handling large documentation
- **Link Extraction**: Automatically extract and convert links to navigation keys
- **Version Control**: Support for specific crate versions or default to latest
- **Rustdoc JSON Native**: Uses docs.rs rustdoc JSON and crates.io APIs with no HTML fallback

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

# Optional: load Cargo.lock and Cargo.toml metadata from a project
uv run jons-mcp-docs-rs --project-dir /path/to/rust/project
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
claude mcp add jons-mcp-docs-rs uvx -- --from git+https://github.com/jonmmease/jons-mcp-docs.rs jons-mcp-docs-rs --project-dir /path/to/rust/project
```

## Tools

### lookup_main_page

Fetch the main documentation page for a Rust crate.

**Parameters:**
- `crate_name` (required): The name of the crate (e.g., "datafusion")
- `version` (optional): The version to look up (defaults to "latest")
- `offset` (optional): Character offset for pagination (default: 0)
- `limit` (optional): Maximum number of characters to return (default: 50)
- `target` (optional): Rust target triple for rustdoc JSON selection
- `rustdoc_format` (optional): Pin rustdoc JSON format version

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
- `target` (optional): Rust target triple for rustdoc JSON selection
- `rustdoc_format` (optional): Pin rustdoc JSON format version

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

### lookup_item_signature

Fetch a compact, structured signature for a single item.

**Parameters:**
- `item` (required): docs key, `docs.rs://...`, or `https://docs.rs/...`
- `version` (optional): Override item version
- `include_members` (optional): Include methods/associated items for trait/impl sections
- `member_limit` (optional): Max members to return (default: 20, max: 100)

**Response fields:**
- `item_key`, `resolved_url`, `kind`, `name`
- `signature`, `where_clause`, `generics`
- `members`, `version_source`

### lookup_feature_flags

Fetch feature-flag graph data for a crate.

**Parameters:**
- `crate_name` (required): The crate name
- `version` (optional): Version to inspect

**Response fields:**
- `crate`, `version`, `version_source`
- `features`: feature list with `name`, `enables`, `enabled_by`, `is_default`
- `default_features`, `optional_dependencies`, `feature_count`, `source_url`

### list_project_crates

List direct and transitive crates for the configured `--project-dir`.

**Parameters:**
- `include_transitive` (optional): Include lockfile transitives (default: true)
- `include_workspace_members` (optional): Include workspace member manifests (default: true)
- `include_dev` (optional): Include dev dependencies in direct classification (default: true)

**Response fields:**
- `project_dir`, `cargo_lock_loaded`
- `direct_crates`, `transitive_crates`, `unresolved_direct`
- `counts`

### lookup_release_notes

Find release notes, changelog sections, and upgrade guides for a crate/version interval.

**Parameters:**
- `crate_name` (required): Crate name
- `from_version` (optional): Lower bound version (inclusive)
- `to_version` (optional): Upper bound version (inclusive)
- `include_upgrade_guides` (optional): Include docs/homepage upgrade guides (default: true)
- `include_release_descriptions` (optional): Include hosted release descriptions (default: true)
- `include_changelog_files` (optional): Include changelog/migration files from repo (default: true)
- `max_items` (optional): Maximum items to return (default: 20, max: 100)
- `project_dir` (optional): Override Cargo.lock source used for `to_version` resolution

**Response fields:**
- `crate`, `from_version`, `to_version`, `version_source`
- `items`: normalized items with `source_type`, `title`, `url`, `published_at`, `version_tag`, `content_excerpt`, `content_markdown`, `relevance_score`, `provenance`
- `summary`: `breaking_changes`, `migration_steps`, `deprecations`, `new_features`, `notes_quality`
- `sources_scanned`, `coverage`
- `error` (when no items): may be `release_notes_not_found`, `tags_only_no_release_notes`, `release_tag_present_no_release_object`, `release_objects_without_notes`, `rate_limited`, `unsupported_repository_host`

**Notes:**
- Best-effort coverage: some crates do not publish release notes/changelogs.
- Supported repository hosts in v1: GitHub and GitLab.
- Optional auth tokens improve reliability for API rate limits:
  - `GITHUB_TOKEN` or `GH_TOKEN`
- `GITLAB_TOKEN`
- Startup convenience: if GitHub token env vars are unset and `gh` is installed/authenticated, the server automatically tries `gh auth token` once at startup.
- Recommended upgrade workflow: combine `compare_versions` (API/doc diffs) with `lookup_release_notes` (human-authored migration guidance).
- Empty-result diagnostics include:
  - `context.classification`
  - `context.confirmed_tag_urls`
  - `context.release_probe` (`release_list_pages_scanned`, `release_list_items_seen`, `release_tag_api_hits`, `release_tag_api_404`, `empty_release_bodies`)
  - `context.available_release_refs` (found tags/releases without note body)

### research_release_notes_coverage

Run deterministic source-availability research for workspace dependencies in one or more manifests.

**Parameters:**
- `manifest_paths` (optional): List of `Cargo.toml` files. Defaults to:
  - `/Users/jmease/repos/vl-convert/Cargo.toml`
  - `/Users/jmease/repos/vegafusion/Cargo.toml`
  - `/Users/jmease/repos/avenger/Cargo.toml`
- `max_crates` (optional): Limit crates analyzed (0 means all)
- `project_dir` (optional): Project root containing `Cargo.lock`; used by transitive scan mode
- `include_transitive` (optional): Scan lockfile-resolved transitive crates (default: false)
- `include_workspace_members` (optional): Include workspace member manifests for direct mode (default: true)
- `include_dev` (optional): Include dev-dependencies in direct mode (default: true)

**Response fields:**
- `manifest_paths`, `total_crates`
- `scan_mode`: `direct_manifest` or `lockfile_full`
- `truncated`, `truncation_reason`
- `results` per crate with coverage/error metadata
- `summary` counts (`found`, `unsupported`, `not_found`, `rate_limited`)

### search_docs

Search within a crate's documentation.

**Parameters:**
- `crate_name` (required): The name of the crate to search in
- `query` (required): The search query
- `version` (optional): The version to search (defaults to "latest")
- `offset` (optional): Result offset for pagination (default: 0)
- `limit` (optional): Maximum number of results to return (default: 50)
- `target` (optional): Rust target triple for rustdoc JSON selection
- `rustdoc_format` (optional): Pin rustdoc JSON format version

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
  "search_url": "https://docs.rs/crate/datafusion/latest/json.gz"
}
```

### search_crates

Search for Rust crates by name using the crates.io API.

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
  "search_url": "https://crates.io/api/v1/crates?q=serde&page=1&per_page=30"
}
```

### get_source_code

Get source code using rustdoc JSON spans and crates.io crate archives.

**Parameters:**
- `page_key` (required): The page key for the item (e.g., "tokio/latest/tokio/runtime/struct.Runtime")
- `offset` (optional): Character offset for pagination (default: 0)
- `limit` (optional): Maximum number of characters to return (default: 50)
- `target` (optional): Rust target triple for rustdoc JSON selection
- `rustdoc_format` (optional): Pin rustdoc JSON format version

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
  "key": "datafusion/latest/datafusion/logical_expr/trait.ScalarUDFImpl",
  "content": "pub trait ScalarUDFImpl: Debug + Send + Sync {\n    ...",
  "total_characters": 5000,
  "offset": 0,
  "limit": 2000,
  "has_more": true,
  "source_url": "https://docs.rs/crate/datafusion/latest/source/src/logical_expr/udf.rs",
  "span": {
    "filename": "src/logical_expr/udf.rs",
    "begin": [120, 1],
    "end": [200, 2]
  }
}
```

### extract_code_examples

Extract code examples from documentation.

**Parameters:**
- `crate_name` (required): The name of the crate
- `module_path` (optional): Limit extraction to a module subtree
- `filter_text` (optional): Filter examples by code text
- `only_complete` (optional): Keep only complete Rust examples
- `version` (optional): Version of the crate (defaults to "latest")
- `target` (optional): Rust target triple for rustdoc JSON selection
- `rustdoc_format` (optional): Pin rustdoc JSON format version

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
  "total_found": 5
}
```

### find_trait_implementors

Find types that implement a specific trait.

**Parameters:**
- `crate_name` (required): The name of the crate containing the trait
- `trait_path` (required): Path to the trait (e.g., "prelude/trait.Debug")
- `version` (optional): Version of the crate (defaults to "latest")
- `target` (optional): Rust target triple for rustdoc JSON selection
- `rustdoc_format` (optional): Pin rustdoc JSON format version

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
        "version_req": "^1.0.1",
        "optional": true,
        "url": "https://docs.rs/mio"
      }
    ],
    "dev": [],
    "build": [],
    "features": [
      {
        "name": "full",
        "enables": ["rt", "macros", "sync", "time"],
        "enabled_by": [],
        "is_default": false
      }
    ],
    "default_features": ["rt", "macros"],
    "optional_dependencies": ["mio"],
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
- `target` (optional): Rust target triple for rustdoc JSON selection
- `rustdoc_format` (optional): Pin rustdoc JSON format version

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
- `target` (optional): Rust target triple for rustdoc JSON selection
- `rustdoc_format` (optional): Pin rustdoc JSON format version

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
- **httpx**: Async HTTP client for docs.rs/crates.io APIs
- **docs.rs rustdoc JSON**: Structured API docs and item graph
- **crates.io API + crate archives**: Metadata, dependencies, features, and source extraction

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
