#!/usr/bin/env python3
"""
Example usage of the Rust docs MCP server.

This demonstrates how to navigate docs.rs using the MCP tools.
"""
import asyncio

from src.jons_mcp_docs_rs import (
    lookup_main_page,
    lookup_pages,
    search_crates,
    search_docs,
)


async def example_navigation():
    """Example: Navigate from main page to specific documentation."""
    print("=== Example: Navigating DataFusion Documentation ===\n")

    # Step 1: Get the main page
    print("1. Fetching DataFusion main page...")
    main_result = await lookup_main_page("datafusion", limit=1000)

    if "error" not in main_result:
        print(f"   ✓ Found {main_result['total_links']} links")
        print(f"   ✓ Total content: {main_result['total_characters']} characters")

        # Show first few links as examples
        print("\n   Example links (use these keys with lookup_pages):")
        for link in main_result["links"][:5]:
            print(f"     - {link['text']}: {link['key']}")

    # Step 2: Look up specific pages
    print("\n2. Looking up specific DataFusion pages...")
    pages_to_lookup = [
        "datafusion/latest/datafusion/dataframe/struct.DataFrame",
        "datafusion/latest/datafusion/execution/context/struct.SessionContext",
    ]

    pages_result = await lookup_pages(pages_to_lookup, limit=2000)

    if pages_result["pages"]:
        print(f"   ✓ Retrieved {len(pages_result['pages'])} pages")
        print(f"   ✓ Combined content: {pages_result['total_characters']} characters")

        # Show snippet of content
        content_preview = pages_result["content"][:300]
        print(f"\n   Content preview:\n{content_preview}...")


async def example_search():
    """Example: Search within documentation."""
    print("\n\n=== Example: Searching Tokio Documentation ===\n")

    # Search for "spawn" in tokio docs
    print("Searching for 'spawn' in tokio documentation...")
    search_result = await search_docs("tokio", "spawn", limit=5)

    if "error" not in search_result:
        print(f"   ✓ Found {search_result['total_results']} results")
        print(f"   ✓ Search URL: {search_result['search_url']}")

        print("\n   Top results:")
        for i, result in enumerate(search_result["results"], 1):
            print(f"   {i}. {result['title']}")
            print(f"      Key: {result['key']}")
            print(f"      URL: {result['url']}")


async def example_pagination():
    """Example: Using pagination for large content."""
    print("\n\n=== Example: Pagination ===\n")

    print("Fetching serde documentation in chunks...")

    # First chunk
    result1 = await lookup_main_page("serde", offset=0, limit=500)
    print(f"1. First chunk: characters 0-500 of {result1['total_characters']}")

    # Second chunk
    if result1["has_more"]:
        result2 = await lookup_main_page("serde", offset=500, limit=500)
        print(f"2. Second chunk: characters 500-1000 of {result2['total_characters']}")
        print(f"   Has more content: {result2['has_more']}")


async def example_crate_search():
    """Example: Search for crates by name."""
    print("\n\n=== Example: Searching for Crates ===\n")

    print("Searching for crates with 'async' in the name...")
    result = await search_crates("async", page=1)

    if "error" not in result:
        print(f"   ✓ Found {result['total_on_page']} crates on page 1")
        print(f"   ✓ Has more pages: {result['has_next_page']}")

        print("\n   Top 5 results:")
        for i, crate in enumerate(result["crates"][:5], 1):
            print(f"   {i}. {crate['name']} v{crate['version']}")
            print(f"      {crate['description']}")

        # Example of fetching page 2
        if result["has_next_page"]:
            print("\n   Fetching page 2...")
            result2 = await search_crates("async", page=2)
            if "error" not in result2:
                print(f"   ✓ Found {result2['total_on_page']} more crates on page 2")


async def main():
    """Run all examples."""
    await example_navigation()
    await example_search()
    await example_pagination()
    await example_crate_search()

    print("\n\n=== Examples Complete ===")
    print("\nYou can use these patterns to navigate any Rust crate documentation!")
    print("Remember: Convert URLs to keys using the key field from links.")


if __name__ == "__main__":
    asyncio.run(main())
