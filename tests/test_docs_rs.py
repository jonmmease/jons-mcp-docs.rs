"""Tests for the Rust docs MCP server."""

import json
from pathlib import Path

import pytest
import zstandard as zstd

import src.jons_mcp_docs_rs as docs
from src.jons_mcp_docs_rs import (
    _build_release_notes_summary,
    _datafusion_upgrade_link_candidates,
    _extract_github_release_body_from_html,
    _extract_relevant_markdown,
    _release_notes_changelog_paths,
    _release_tag_candidates,
    bootstrap_github_token_from_gh_cli,
    build_rustdoc_json_urls,
    convert_url_to_key,
    list_project_crates,
    lookup_feature_flags,
    lookup_item_signature,
    lookup_pages,
    lookup_release_notes,
    normalize_crate_path,
    normalize_item_to_key,
    paginate_content,
    parse_feature_flags_map,
    research_release_notes_coverage,
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

    def test_release_tag_candidates(self):
        assert _release_tag_candidates("serde_json", "json", "1.0.149") == [
            "v1.0.149",
            "1.0.149",
            "serde_json-1.0.149",
            "json-1.0.149",
        ]
        assert _release_tag_candidates("tokio", "tokio", "1.49.0") == [
            "tokio-1.49.0",
            "v1.49.0",
            "1.49.0",
        ]

    def test_release_notes_changelog_paths_datafusion(self):
        paths = _release_notes_changelog_paths("datafusion", ["48.0.1", "52.1.0"])
        assert "CHANGELOG.md" in paths
        assert "dev/changelog/48.0.1.md" in paths
        assert "dev/changelog/48.0.0.md" in paths
        assert "dev/changelog/52.1.0.md" in paths

    def test_datafusion_upgrade_link_candidates(self):
        html = """
        <html><body>
          <a href="/library-user-guide/upgrading/datafusion-48.0.0.html">DataFusion 48</a>
          <a href="/library-user-guide/upgrading/datafusion-53.0.0.html">DataFusion 53</a>
          <a href="/library-user-guide/upgrading/index.html">Index</a>
        </body></html>
        """
        candidates = _datafusion_upgrade_link_candidates(
            html,
            "https://datafusion.apache.org/library-user-guide/upgrading/index.html",
            {"48.0.1", "52.1.0"},
        )
        assert len(candidates) == 1
        assert candidates[0][0].endswith("/datafusion-48.0.0.html")
        assert candidates[0][1] == "48.0.0"
        assert candidates[0][2] is False

    def test_extract_github_release_body_from_html_ignores_chrome_only(self):
        html = """
        <html><body>
          <header>GitHub</header>
          <article><nav>Navigation</nav><footer>Footer</footer></article>
        </body></html>
        """
        assert _extract_github_release_body_from_html(html) == ""

    def test_build_release_notes_summary_strips_html_heading_noise(self):
        items = [
            {
                "content_markdown": "<h2>Added</h2>\n- Breaking: Renamed API\n- Migration: update calls",
                "content_excerpt": "",
            }
        ]
        summary = _build_release_notes_summary(items)
        assert "Added" not in summary["new_features"]
        assert summary["breaking_changes"]
        assert summary["migration_steps"]

    def test_extract_relevant_markdown(self):
        markdown = """
# Changelog

## v1.0.0
- Initial release

## v1.1.0
- Breaking: Renamed API

## v1.2.0
- Added feature X
""".strip()
        excerpt, selected, matched = _extract_relevant_markdown(markdown, {"1.1.0"})
        assert matched is True
        assert "1.1.0" in selected
        assert "Breaking" in excerpt

    def test_bootstrap_github_token_from_gh_cli_success(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setattr(docs, "_github_token_from_cli", None)
        monkeypatch.setattr(docs, "_github_token_bootstrap_attempted", False)
        monkeypatch.setattr(docs.shutil, "which", lambda _: "/usr/bin/gh")

        def fake_run(*args, **kwargs):
            return docs.subprocess.CompletedProcess(
                args=["gh", "auth", "token"],
                returncode=0,
                stdout="ghp_example_token\n",
                stderr="",
            )

        monkeypatch.setattr(docs.subprocess, "run", fake_run)
        assert bootstrap_github_token_from_gh_cli() is True
        headers = docs.github_api_headers()
        assert headers.get("Authorization") == "Bearer ghp_example_token"

    def test_bootstrap_github_token_from_gh_cli_skips_when_env_set(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "env_token")
        monkeypatch.setattr(docs, "_github_token_from_cli", None)
        monkeypatch.setattr(docs, "_github_token_bootstrap_attempted", False)

        def fail_run(*args, **kwargs):
            raise AssertionError("subprocess.run should not be called when env token is set")

        monkeypatch.setattr(docs.subprocess, "run", fail_run)
        assert bootstrap_github_token_from_gh_cli() is True
        assert docs.github_api_headers().get("Authorization") == "Bearer env_token"

    def test_parse_link_header_next(self):
        headers = {
            "link": '<https://api.github.com/repos/o/r/releases?per_page=100&page=2>; rel="next", '
            '<https://api.github.com/repos/o/r/releases?per_page=100&page=10>; rel="last"'
        }
        assert (
            docs._parse_link_header_next(headers)
            == "https://api.github.com/repos/o/r/releases?per_page=100&page=2"
        )
        assert docs._parse_link_header_next({}) is None


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

    async def test_lookup_release_notes_mocked_github(self, monkeypatch):
        async def fake_interval(crate_name, from_version, to_version, project_dir):
            return (
                "1.0.140",
                "1.0.149",
                "explicit",
                ["1.0.140", "1.0.149"],
                {"crate": {"repository": "https://github.com/serde-rs/json"}},
            )

        async def fake_github(*args, **kwargs):
            return [
                {
                    "source_type": "github_release",
                    "title": "v1.0.149",
                    "url": "https://github.com/serde-rs/json/releases/tag/v1.0.149",
                    "published_at": "2026-01-06T00:00:00Z",
                    "version_tag": "v1.0.149",
                    "content_excerpt": "Breaking: X",
                    "content_markdown": "- Breaking: Renamed API\n- Migration: update call sites",
                    "relevance_score": 100,
                    "provenance": {
                        "host": "github.com",
                        "repo": "serde-rs/json",
                        "path": "releases/tags/v1.0.149",
                        "selection_reason": "exact_tag_match",
                    },
                }
            ]

        async def fake_guides(*args, **kwargs):
            return []

        monkeypatch.setattr(docs, "resolve_release_notes_interval", fake_interval)
        monkeypatch.setattr(docs, "_collect_github_release_items", fake_github)
        monkeypatch.setattr(docs, "_collect_upgrade_guide_items", fake_guides)
        docs._release_notes_result_cache.clear()

        result = await lookup_release_notes("serde_json", from_version="1.0.140", to_version="1.0.149")
        assert result.get("error") is None
        assert result["coverage"]["found_count"] == 1
        assert result["summary"]["notes_quality"] in {"medium", "high"}
        assert result["summary"]["breaking_changes"]
        assert result["version_source"] == "explicit"

    async def test_lookup_release_notes_tags_only_classification(self, monkeypatch):
        async def fake_interval(crate_name, from_version, to_version, project_dir):
            return (
                "0.22.1",
                "0.22.1",
                "explicit",
                ["0.22.1"],
                {"crate": {"repository": "https://github.com/example/base64"}},
            )

        async def fake_github(*args, **kwargs):
            kwargs["release_probe"].update(
                {
                    "release_list_pages_scanned": 1,
                    "release_list_items_seen": 0,
                    "release_tag_api_hits": 0,
                    "release_tag_api_404": 4,
                    "empty_release_bodies": 0,
                }
            )
            kwargs["confirmed_tag_urls"].append(
                "https://github.com/example/base64/releases/tag/v0.22.1"
            )
            kwargs["available_release_refs"].append(
                {
                    "tag": "v0.22.1",
                    "url": "https://github.com/example/base64/releases/tag/v0.22.1",
                    "status": 200,
                    "source": "github_release_page",
                }
            )
            return []

        async def fake_guides(*args, **kwargs):
            return []

        monkeypatch.setattr(docs, "resolve_release_notes_interval", fake_interval)
        monkeypatch.setattr(docs, "_collect_github_release_items", fake_github)
        monkeypatch.setattr(docs, "_collect_upgrade_guide_items", fake_guides)
        docs._release_notes_result_cache.clear()

        result = await lookup_release_notes(
            "base64",
            from_version="0.22.1",
            to_version="0.22.1",
            include_upgrade_guides=False,
        )
        assert result["error"] == "tags_only_no_release_notes"
        assert result["context"]["classification"] == "tags_only_no_release_notes"
        assert result["context"]["confirmed_tag_urls"]
        assert result["context"]["release_probe"]["release_list_items_seen"] == 0

    async def test_lookup_release_notes_tag_present_no_release_object(self, monkeypatch):
        async def fake_interval(crate_name, from_version, to_version, project_dir):
            return (
                "2.5.0",
                "2.5.0",
                "explicit",
                ["2.5.0"],
                {"crate": {"repository": "https://github.com/tokio-rs/tokio"}},
            )

        async def fake_github(*args, **kwargs):
            kwargs["release_probe"].update(
                {
                    "release_list_pages_scanned": 3,
                    "release_list_items_seen": 300,
                    "release_tag_api_hits": 0,
                    "release_tag_api_404": 4,
                    "empty_release_bodies": 0,
                }
            )
            kwargs["confirmed_tag_urls"].append(
                "https://github.com/tokio-rs/tokio/releases/tag/tokio-macros-2.5.0"
            )
            return []

        async def fake_guides(*args, **kwargs):
            return []

        monkeypatch.setattr(docs, "resolve_release_notes_interval", fake_interval)
        monkeypatch.setattr(docs, "_collect_github_release_items", fake_github)
        monkeypatch.setattr(docs, "_collect_upgrade_guide_items", fake_guides)
        docs._release_notes_result_cache.clear()

        result = await lookup_release_notes(
            "tokio-macros",
            from_version="2.5.0",
            to_version="2.5.0",
            include_upgrade_guides=False,
        )
        assert result["error"] == "release_tag_present_no_release_object"
        assert result["context"]["classification"] == "release_tag_present_no_release_object"
        assert result["context"]["release_probe"]["release_tag_api_hits"] == 0

    async def test_lookup_release_notes_release_objects_without_notes(self, monkeypatch):
        async def fake_interval(crate_name, from_version, to_version, project_dir):
            return (
                "1.0.0",
                "1.0.1",
                "explicit",
                ["1.0.0", "1.0.1"],
                {"crate": {"repository": "https://github.com/nical/lyon"}},
            )

        async def fake_github(*args, **kwargs):
            kwargs["release_probe"].update(
                {
                    "release_list_pages_scanned": 1,
                    "release_list_items_seen": 5,
                    "release_tag_api_hits": 1,
                    "release_tag_api_404": 4,
                    "empty_release_bodies": 1,
                }
            )
            kwargs["available_release_refs"].append(
                {
                    "tag": "1.0.0",
                    "url": "https://github.com/nical/lyon/releases/tag/1.0.0",
                    "status": 200,
                    "source": "github_release_api",
                }
            )
            return []

        async def fake_guides(*args, **kwargs):
            return []

        monkeypatch.setattr(docs, "resolve_release_notes_interval", fake_interval)
        monkeypatch.setattr(docs, "_collect_github_release_items", fake_github)
        monkeypatch.setattr(docs, "_collect_upgrade_guide_items", fake_guides)
        docs._release_notes_result_cache.clear()

        result = await lookup_release_notes(
            "lyon",
            from_version="1.0.0",
            to_version="1.0.1",
            include_upgrade_guides=False,
        )
        assert result["error"] == "release_objects_without_notes"
        assert result["context"]["classification"] == "release_objects_without_notes"
        assert result["context"]["available_release_refs"]

    async def test_collect_github_release_items_paginates_release_list(self, monkeypatch):
        repo = docs.RepositoryRef(
            host="github.com",
            owner="tokio-rs",
            name="tokio",
            repo_path="tokio-rs/tokio",
        )

        page1 = "https://api.github.com/repos/tokio-rs/tokio/releases?per_page=100"
        page2 = "https://api.github.com/repos/tokio-rs/tokio/releases?per_page=100&page=2"

        async def fake_fetch(url, headers=None):
            if url == "https://api.github.com/repos/tokio-rs/tokio":
                return (
                    200,
                    json.dumps({"default_branch": "main"}).encode("utf-8"),
                    url,
                    {"content-type": "application/json"},
                )
            if url.startswith("https://api.github.com/repos/tokio-rs/tokio/releases/tags/"):
                return 404, None, url, {"content-type": "application/json"}
            if url == page1:
                body = [{"tag_name": "tokio-1.49.0", "body": "unrelated"}]
                link = f'<{page2}>; rel="next", <{page2}>; rel="last"'
                return (
                    200,
                    json.dumps(body).encode("utf-8"),
                    url,
                    {"content-type": "application/json", "link": link},
                )
            if url == page2:
                body = [
                    {
                        "tag_name": "tokio-macros-2.5.0",
                        "name": "tokio-macros 2.5.0",
                        "body": "- Breaking: update macros",
                        "html_url": "https://github.com/tokio-rs/tokio/releases/tag/tokio-macros-2.5.0",
                        "published_at": "2025-01-01T00:00:00Z",
                    }
                ]
                return (
                    200,
                    json.dumps(body).encode("utf-8"),
                    url,
                    {"content-type": "application/json"},
                )
            return 404, None, url, {"content-type": "application/json"}

        monkeypatch.setattr(docs, "github_api_headers", lambda: {})
        monkeypatch.setattr(docs, "fetch_release_notes_resource", fake_fetch)

        sources_scanned: list[dict[str, object]] = []
        warnings: list[str] = []
        release_probe = docs._new_release_probe()
        confirmed_tag_urls: list[str] = []
        available_release_refs: list[dict[str, object]] = []
        items = await docs._collect_github_release_items(
            crate_name="tokio-macros",
            repo=repo,
            versions_in_range=["2.5.0"],
            include_release_descriptions=True,
            include_changelog_files=False,
            sources_scanned=sources_scanned,
            warnings=warnings,
            release_probe=release_probe,
            confirmed_tag_urls=confirmed_tag_urls,
            available_release_refs=available_release_refs,
        )
        assert len(items) == 1
        assert items[0]["version_tag"] == "tokio-macros-2.5.0"
        assert release_probe["release_list_pages_scanned"] == 2
        assert release_probe["release_list_items_seen"] == 2
        assert any(scan["source_type"] == "github_releases_list_page" for scan in sources_scanned)

    async def test_collect_upgrade_guide_items_follows_datafusion_version_links(
        self, monkeypatch
    ):
        index_html = """
        <html><body>
          <a href="/library-user-guide/upgrading/datafusion-48.0.0.html">Upgrade 48</a>
        </body></html>
        """
        detail_html = """
        <html><body><main>
          <h1>Upgrade to DataFusion 48.0.0</h1>
          <p>Breaking: planner API changed.</p>
          <p>Migration: update extension planner trait impls.</p>
        </main></body></html>
        """

        async def fake_fetch(url, headers=None):
            if url.endswith("/upgrading/index.html") or url.endswith("/upgrading/"):
                return (
                    200,
                    index_html.encode("utf-8"),
                    "https://datafusion.apache.org/library-user-guide/upgrading/index.html",
                    {"content-type": "text/html"},
                )
            if "datafusion-48.0.0.html" in url:
                return (
                    200,
                    detail_html.encode("utf-8"),
                    "https://datafusion.apache.org/library-user-guide/upgrading/datafusion-48.0.0.html",
                    {"content-type": "text/html"},
                )
            return 404, None, url, {"content-type": "text/html"}

        monkeypatch.setattr(docs, "fetch_release_notes_resource", fake_fetch)
        scanned: list[dict[str, object]] = []
        items = await docs._collect_upgrade_guide_items(
            crate_name="datafusion",
            crate_meta={},
            versions_in_range=["48.0.1"],
            sources_scanned=scanned,
        )
        assert any(
            item["provenance"]["selection_reason"] == "datafusion_versioned_upgrade_guide"
            for item in items
        )
        assert any(item.get("version_tag") == "48.0.0" for item in items)
        assert not any(item["url"].endswith("/upgrading/index.html") for item in items)

    async def test_lookup_release_notes_unsupported_host(self, monkeypatch):
        async def fake_interval(crate_name, from_version, to_version, project_dir):
            return (
                "1.0.0",
                "1.1.0",
                "latest",
                ["1.0.0", "1.1.0"],
                {"crate": {"repository": "https://bitbucket.org/example/proj"}},
            )

        monkeypatch.setattr(docs, "resolve_release_notes_interval", fake_interval)
        docs._release_notes_result_cache.clear()

        result = await lookup_release_notes(
            "example-crate",
            include_upgrade_guides=False,
            include_release_descriptions=True,
            include_changelog_files=True,
        )
        assert result["error"] == "unsupported_repository_host"

    async def test_lookup_release_notes_interval_error(self, monkeypatch):
        async def fake_interval(crate_name, from_version, to_version, project_dir):
            raise docs.DataError(
                "version_interval_invalid",
                "from_version must be less than or equal to to_version",
                context={"crate": crate_name},
            )

        monkeypatch.setattr(docs, "resolve_release_notes_interval", fake_interval)
        docs._release_notes_result_cache.clear()

        result = await lookup_release_notes("serde_json", from_version="2.0.0", to_version="1.0.0")
        assert result["error"] == "version_interval_invalid"

    async def test_research_release_notes_coverage_with_mocked_inventory(self, monkeypatch):
        def fake_inventory(*args, **kwargs):
            return {
                "serde_json": {
                    "name": "serde_json",
                    "source": "registry",
                    "versions": ["1.0.149"],
                    "declared_in": ["Cargo.toml"],
                    "sources": [],
                },
                "rstar": {
                    "name": "rstar",
                    "source": "git",
                    "versions": [],
                    "declared_in": ["Cargo.toml"],
                    "sources": ["git:https://github.com/georust/rstar"],
                },
            }

        async def fake_lookup(crate_name, **kwargs):
            if crate_name == "serde_json":
                return {
                    "coverage": {"found_count": 1, "source_types_found": ["github_release"], "confidence": "high"},
                    "context": None,
                }
            return {
                "coverage": {"found_count": 0, "source_types_found": [], "confidence": "low"},
                "error": "release_notes_not_found",
                "context": {"missing_reasons": ["none"]},
            }

        monkeypatch.setattr(docs, "_direct_dependency_inventory", fake_inventory)
        monkeypatch.setattr(docs, "lookup_release_notes", fake_lookup)

        result = await research_release_notes_coverage(["Cargo.toml"])
        assert result["summary"]["found"] == 1
        assert result["summary"]["unsupported"] == 1
        assert result["scan_mode"] == "direct_manifest"
        assert result["truncated"] is False

    async def test_research_release_notes_coverage_transitive_mode(self, monkeypatch, tmp_path):
        (tmp_path / "Cargo.lock").write_text("", encoding="utf-8")

        def fake_lock_inventory(_):
            return {
                "serde_json": {
                    "name": "serde_json",
                    "source": "registry",
                    "versions": ["1.0.149"],
                    "declared_in": [str(tmp_path / "Cargo.lock")],
                    "sources": ["registry+https://github.com/rust-lang/crates.io-index"],
                },
                "rstar": {
                    "name": "rstar",
                    "source": "git",
                    "versions": ["0.12.2"],
                    "declared_in": [str(tmp_path / "Cargo.lock")],
                    "sources": ["git+https://github.com/georust/rstar"],
                },
            }

        async def fake_lookup(crate_name, **kwargs):
            return {
                "coverage": {"found_count": 1, "source_types_found": ["github_release"], "confidence": "high"},
                "context": None,
            }

        monkeypatch.setattr(docs, "_lockfile_dependency_inventory", fake_lock_inventory)
        monkeypatch.setattr(docs, "lookup_release_notes", fake_lookup)

        result = await research_release_notes_coverage(
            manifest_paths=["Cargo.toml"],
            include_transitive=True,
            project_dir=str(tmp_path),
        )
        assert result["scan_mode"] == "lockfile_full"
        assert result["summary"]["found"] == 1
        assert result["summary"]["unsupported"] == 1
