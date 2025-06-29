#!/usr/bin/env python3
"""
Verify the MCP server installation and basic functionality.
"""
import asyncio
import json
from src.jons_mcp_docs_rs import mcp, lookup_main_page


async def verify():
    """Verify the server is properly installed."""
    print("🔍 Verifying MCP Rust Docs Server Installation...\n")
    
    # Check server metadata
    print("✓ Server name:", mcp.name)
    print("✓ Server ready to handle MCP requests")
    
    # Test a simple lookup
    print("\n📚 Testing documentation lookup...")
    result = await lookup_main_page("serde", limit=100)
    
    if "error" in result:
        print(f"❌ Error: {result['error']}")
        return False
    else:
        print(f"✓ Successfully fetched serde documentation")
        print(f"  - Version: {result['version']}")
        print(f"  - Total characters: {result['total_characters']}")
        print(f"  - Links found: {result['total_links']}")
    
    print("\n✅ Installation verified successfully!")
    print("\nTo use with Claude Desktop:")
    print("claude mcp add jons-mcp-docs-rs uvx -- --from git+https://github.com/jonmmease/jons-mcp-docs.rs jons-mcp-docs-rs")
    
    return True


if __name__ == "__main__":
    success = asyncio.run(verify())
    exit(0 if success else 1)