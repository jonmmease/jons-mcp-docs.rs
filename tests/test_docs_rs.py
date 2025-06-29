"""
Tests for the Rust docs MCP server.
"""

import pytest

from src.jons_mcp_docs_rs import (
    convert_url_to_key,
    lookup_main_page,
    lookup_pages,
    normalize_crate_path,
    paginate_content,
    search_crates,
    search_docs,
)


class TestUtilityFunctions:
    """Test utility functions."""

    def test_normalize_crate_path(self):
        """Test crate path normalization."""
        assert normalize_crate_path("/datafusion/latest/") == "datafusion/latest"
        assert normalize_crate_path("datafusion//latest") == "datafusion/latest"
        assert normalize_crate_path("///datafusion/latest///") == "datafusion/latest"

    def test_convert_url_to_key(self):
        """Test URL to key conversion."""
        # Full URLs
        assert (
            convert_url_to_key(
                "https://docs.rs/datafusion/latest/datafusion/index.html"
            )
            == "datafusion/latest/datafusion/index"
        )
        assert (
            convert_url_to_key("https://docs.rs/tokio/latest/tokio/")
            == "tokio/latest/tokio"
        )

        # Relative URLs
        assert (
            convert_url_to_key("/datafusion/struct.DataFrame.html")
            == "datafusion/struct.DataFrame"
        )
        assert convert_url_to_key("struct.DataFrame.html") == "struct.DataFrame"

    def test_paginate_content(self):
        """Test content pagination."""
        content = "Hello, this is a test content for pagination."

        # Test basic pagination
        paginated, total = paginate_content(content, 0, 10)
        assert paginated == "Hello, thi"
        assert total == len(content)

        # Test offset
        paginated, total = paginate_content(content, 10, 10)
        assert paginated == "s is a tes"

        # Test beyond content length
        paginated, total = paginate_content(content, 100, 10)
        assert paginated == ""
        assert total == len(content)


@pytest.mark.asyncio
class TestTools:
    """Test the MCP tools."""

    async def test_lookup_main_page_structure(self):
        """Test that lookup_main_page returns the expected structure."""
        # This test checks the structure without making actual HTTP requests
        # In a real test environment, you'd mock the HTTP calls
        result = await lookup_main_page(
            "test-crate", version="1.0.0", offset=0, limit=100
        )

        # Check that result has expected keys
        assert "crate" in result
        assert "version" in result

        # If there's an error, it should have error key
        if "error" in result:
            assert isinstance(result["error"], str)
        else:
            # Otherwise, check for success keys
            assert "content" in result
            assert "total_characters" in result
            assert "offset" in result
            assert "limit" in result
            assert "has_more" in result
            assert "links" in result
            assert "total_links" in result
            assert "url" in result

    async def test_lookup_pages_structure(self):
        """Test that lookup_pages returns the expected structure."""
        result = await lookup_pages(["test/page1", "test/page2"], offset=0, limit=100)

        assert "pages" in result
        assert "content" in result
        assert "total_characters" in result
        assert "offset" in result
        assert "limit" in result
        assert "has_more" in result
        assert "pages_count" in result

    async def test_search_docs_structure(self):
        """Test that search_docs returns the expected structure."""
        result = await search_docs(
            "test-crate", "test query", version="1.0.0", offset=0, limit=10
        )

        assert "crate" in result
        assert "version" in result
        assert "query" in result

        if "error" in result:
            assert isinstance(result["error"], str)
        else:
            assert "results" in result
            assert "total_results" in result
            assert "offset" in result
            assert "limit" in result
            assert "has_more" in result
            assert "search_url" in result

    async def test_search_crates_structure(self):
        """Test that search_crates returns the expected structure."""
        result = await search_crates("test-crate", page=1)

        assert "query" in result
        assert "page" in result

        if "error" in result:
            assert isinstance(result["error"], str)
        else:
            assert "crates" in result
            assert "total_on_page" in result
            assert "has_next_page" in result
            assert "search_url" in result

            # If there are crates, check their structure
            if result["crates"]:
                crate = result["crates"][0]
                assert "name" in crate
                assert "version" in crate
                assert "description" in crate
                assert "date" in crate
                assert "url" in crate
