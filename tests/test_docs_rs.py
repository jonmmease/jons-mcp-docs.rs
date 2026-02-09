"""Tests for the Rust docs MCP server."""

import json
from pathlib import Path

import pytest
import zstandard as zstd

import src.jons_mcp_docs_rs as docs
from src.jons_mcp_docs_rs import (
    build_rustdoc_json_urls,
    convert_url_to_key,
    list_project_crates,
    lookup_feature_flags,
    lookup_item_signature,
    lookup_pages,
    normalize_crate_path,
    normalize_item_to_key,
    paginate_content,
    parse_feature_flags_map,
    search_crates,
    search_docs,
    select_preferred_version,
)


class TestUtilityFunctions:
    """Test utility functions."""

    def test_normalize_crate_path(self):
        assert normalize_crate_path("/datafusion/latest/") == "datafusion/latest"
        assert normalize_crate_path("datafusion//latest") == "datafusion/latest"
        assert normalize_crate_path("///datafusion/latest///") == "datafusion/latest"

    def test_convert_url_to_key(self):
        assert (
            convert_url_to_key("https://docs.rs/datafusion/latest/datafusion/index.html")
            == "datafusion/latest/datafusion/index"
        )
        assert convert_url_to_key("https://docs.rs/tokio/latest/tokio/") == "tokio/latest/tokio"
        assert (
            convert_url_to_key("/datafusion/struct.DataFrame.html")
            == "datafusion/struct.DataFrame"
        )
        assert convert_url_to_key("struct.DataFrame.html") == "struct.DataFrame"

    def test_normalize_item_to_key(self):
        assert (
            normalize_item_to_key("docs.rs://tokio/latest/tokio/runtime/struct.Runtime")
            == "tokio/latest/tokio/runtime/struct.Runtime"
        )
        assert (
            normalize_item_to_key(
                "https://docs.rs/tokio/latest/tokio/runtime/struct.Runtime.html"
            )
            == "tokio/latest/tokio/runtime/struct.Runtime"
        )
        assert (
            normalize_item_to_key(
                "https://doc.rust-lang.org/nightly/std/vec/struct.Vec.html"
            )
            == "rust-lang/nightly/std/vec/struct.Vec"
        )

    def test_select_preferred_version(self):
        assert select_preferred_version(["1.2.0-alpha.1", "1.1.9", "1.2.0"]) == "1.2.0"
        assert (
            select_preferred_version(["1.2.0-alpha.1", "1.2.0-beta.2"])
            == "1.2.0-beta.2"
        )

    def test_paginate_content(self):
        content = "Hello, this is a test content for pagination."
        paginated, total = paginate_content(content, 0, 10)
        assert paginated == "Hello, thi"
        assert total == len(content)

        paginated, total = paginate_content(content, 10, 10)
        assert paginated == "s is a tes"
        assert total == len(content)

        paginated, total = paginate_content(content, 100, 10)
        assert paginated == ""
        assert total == len(content)

    def test_parse_feature_flags_map(self):
        features_map = {
            "default": ["rt", "dep:mio"],
            "rt": [],
            "net": ["rt", "dep:socket2"],
        }
        features, default_features, optional_deps = parse_feature_flags_map(features_map)
        by_name = {feature["name"]: feature for feature in features}

        assert by_name["default"]["is_default"] is True
        assert "rt" in default_features
        assert by_name["rt"]["enabled_by"] == ["default", "net"]
        assert optional_deps == ["mio", "socket2"]

    def test_build_rustdoc_json_urls(self):
        assert build_rustdoc_json_urls("serde_json", "1.0.140") == [
            "https://docs.rs/crate/serde_json/1.0.140/json.gz",
            "https://docs.rs/crate/serde_json/1.0.140/json",
        ]
        assert build_rustdoc_json_urls(
            "serde_json", "1.0.140", target="x86_64-unknown-linux-gnu", rustdoc_format=57
        ) == [
            "https://docs.rs/crate/serde_json/1.0.140/x86_64-unknown-linux-gnu/json/57.gz",
            "https://docs.rs/crate/serde_json/1.0.140/x86_64-unknown-linux-gnu/json/57",
        ]


@pytest.mark.asyncio
class TestTools:
    """Test the MCP tools."""

    async def test_search_docs_structure(self):
        result = await search_docs(
            "tokio", "Runtime", version="1.48.0", offset=0, limit=10
        )
        assert result.get("error") is None
        assert "crate" in result
        assert "version" in result
        assert "query" in result
        assert "results" in result
        assert "total_results" in result
        assert "search_url" in result
        assert "format_version" in result
        assert "target_triple" in result

    async def test_search_crates_structure(self):
        result = await search_crates("serde", page=1)
        assert result.get("error") is None
        assert "query" in result
        assert "page" in result
        assert "crates" in result
        assert "has_next_page" in result
        assert "search_url" in result
        assert result["total_on_page"] >= 1

    async def test_lookup_item_signature_integration(self):
        result = await lookup_item_signature(
            "docs.rs://tokio/latest/tokio/runtime/struct.Runtime"
        )
        assert result.get("error") is None
        assert result["kind"] == "struct"
        assert result["name"] == "Runtime"
        assert isinstance(result["signature"], str) and result["signature"]
        assert isinstance(result["format_version"], int)
        assert isinstance(result["target_triple"], str)

    async def test_lookup_item_signature_trait_members(self):
        result = await lookup_item_signature(
            "docs.rs://tokio/latest/tokio/io/trait.AsyncRead",
            include_members=True,
            member_limit=10,
        )
        assert result.get("error") is None
        assert result["kind"] == "trait"
        assert result["members"]
        assert any(member["kind"] == "function" for member in result["members"])

    async def test_lookup_feature_flags_integration(self):
        result = await lookup_feature_flags("tokio")
        assert result.get("error") is None
        assert result["feature_count"] > 0
        assert isinstance(result["features"], list)
        assert isinstance(result["default_features"], list)
        assert result["source_url"].startswith("https://crates.io/")

    async def test_analyze_dependencies_contains_features(self):
        result = await docs.analyze_dependencies("tokio")
        assert result.get("error") is None
        deps = result["dependencies"]
        assert "features" in deps
        assert isinstance(deps["features"], list)
        assert len(deps["features"]) > 0

    async def test_lookup_pages_order_stable_with_concurrency(self, monkeypatch):
        async def fake_lookup_single_page(page_key, version, target, rustdoc_format):
            if "page1" in page_key:
                await docs.asyncio.sleep(0.05)
            else:
                await docs.asyncio.sleep(0.01)
            return ({"key": page_key, "ok": True}, f"\n# Page: {page_key}\n")

        monkeypatch.setattr(docs, "_lookup_single_page", fake_lookup_single_page)

        result = await lookup_pages(
            ["crate/latest/crate/page1", "crate/latest/crate/page2"],
            limit=10_000,
        )
        keys = [page["key"] for page in result["pages"]]
        assert keys == ["crate/latest/crate/page1", "crate/latest/crate/page2"]

    async def test_list_project_crates_classification(self, tmp_path: Path, monkeypatch):
        (tmp_path / "Cargo.toml").write_text(
            """
[package]
name = "demo-root"
version = "0.1.0"
edition = "2021"

[dependencies]
serde = "1"
serde_alias = { package = "serde_json", version = "1" }

[dev-dependencies]
tokio = "1"

[target.'cfg(unix)'.dependencies]
libc = "0.2"
""".strip()
        )

        (tmp_path / "Cargo.lock").write_text(
            """
version = 3

[[package]]
name = "serde"
version = "1.0.228"
source = "registry+https://github.com/rust-lang/crates.io-index"

[[package]]
name = "serde_json"
version = "1.0.145"
source = "registry+https://github.com/rust-lang/crates.io-index"

[[package]]
name = "tokio"
version = "1.49.0"
source = "registry+https://github.com/rust-lang/crates.io-index"

[[package]]
name = "libc"
version = "0.2.177"
source = "registry+https://github.com/rust-lang/crates.io-index"

[[package]]
name = "itoa"
version = "1.0.15"
source = "registry+https://github.com/rust-lang/crates.io-index"
""".strip()
        )

        versions, packages = docs.parse_cargo_lock(tmp_path / "Cargo.lock")
        monkeypatch.setattr(docs, "_project_dir", tmp_path)
        monkeypatch.setattr(docs, "_cargo_lock_versions", versions)
        monkeypatch.setattr(docs, "_cargo_lock_packages", packages)

        result = await list_project_crates()

        assert result["cargo_lock_loaded"] is True
        direct_names = {crate["name"] for crate in result["direct_crates"]}
        assert {"serde", "serde_json", "tokio", "libc"}.issubset(direct_names)

        transitive_names = {crate["name"] for crate in result["transitive_crates"]}
        assert "itoa" in transitive_names
        assert "serde_alias" not in direct_names

    async def test_fetch_rustdoc_json_payload_falls_back_to_plain_json(self, monkeypatch):
        payload = {
            "format_version": 57,
            "target": {"triple": "x86_64-unknown-linux-gnu"},
            "root": 1,
            "index": {"1": {"id": 1, "inner": {"module": {"items": []}}, "visibility": "public"}},
            "paths": {},
            "external_crates": {},
        }
        calls: list[str] = []

        async def fake_fetch_bytes(url: str):
            calls.append(url)
            if url.endswith("/json.gz"):
                response = docs.httpx.Response(404, request=docs.httpx.Request("GET", url))
                raise docs.httpx.HTTPStatusError(
                    "404 Not Found", request=response.request, response=response
                )
            return json.dumps(payload).encode("utf-8"), url, {"content-type": "application/json"}

        monkeypatch.setattr(docs, "fetch_bytes", fake_fetch_bytes)
        docs._rustdoc_cache.clear()

        result = await docs.fetch_rustdoc_json_payload("serde_json", "1.0.140", None, None)
        assert result["format_version"] == 57
        assert calls[0].endswith("/json.gz")
        assert calls[1].endswith("/json")

    async def test_fetch_rustdoc_json_payload_decodes_zstd_json(self, monkeypatch):
        payload = {
            "format_version": 57,
            "target": {"triple": "x86_64-unknown-linux-gnu"},
            "root": 1,
            "index": {"1": {"id": 1, "inner": {"module": {"items": []}}, "visibility": "public"}},
            "paths": {},
            "external_crates": {},
        }
        calls: list[str] = []
        compressed = zstd.ZstdCompressor().compress(json.dumps(payload).encode("utf-8"))

        async def fake_fetch_bytes(url: str):
            calls.append(url)
            if url.endswith("/json.gz"):
                response = docs.httpx.Response(404, request=docs.httpx.Request("GET", url))
                raise docs.httpx.HTTPStatusError(
                    "404 Not Found", request=response.request, response=response
                )
            return compressed, url, {"content-type": "application/json"}

        monkeypatch.setattr(docs, "fetch_bytes", fake_fetch_bytes)
        docs._rustdoc_cache.clear()

        result = await docs.fetch_rustdoc_json_payload("serde_json", "1.0.140", None, None)
        assert result["format_version"] == 57
        assert calls[0].endswith("/json.gz")
        assert calls[1].endswith("/json")
