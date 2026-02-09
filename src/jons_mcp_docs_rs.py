#!/usr/bin/env python3
"""
FastMCP server that provides tools for looking up Rust documentation from docs.rs.

Implementation note:
This server is intentionally JSON-native and does not scrape HTML pages from docs.rs.
It consumes:
- docs.rs rustdoc JSON endpoints (`/crate/{crate}/{version}/json*.gz`)
- crates.io JSON APIs for crate metadata, dependency metadata, and features
- crates.io crate downloads for source files (used with rustdoc span metadata)
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import fnmatch
import gzip
import io
import logging
import os
import re
import signal
import sys
import tarfile
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import quote, urlparse

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

import httpx
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
CRATES_IO_BASE_URL = "https://crates.io/api/v1"
DEFAULT_VERSION = "latest"
DEFAULT_LIMIT = 50
MAX_CONTENT_LENGTH = 100000
HTTP_TIMEOUT_SECONDS = 30.0
MAX_HTTP_RETRIES = 1
LOOKUP_CONCURRENCY = 8
CACHE_TTL_SECONDS = 300
CACHE_MAX_ENTRIES = 256

# Cargo.lock metadata (populated at startup if --project-dir is provided)
_cargo_lock_versions: dict[str, list[str]] = {}
_cargo_lock_packages: list[dict[str, Any]] = []
_project_dir: Path | None = None

# Shared HTTP client + lightweight in-memory cache
_http_client: httpx.AsyncClient | None = None
_http_client_loop: asyncio.AbstractEventLoop | None = None
_byte_cache: OrderedDict[str, tuple[float, bytes, str, dict[str, str]]] = OrderedDict()
_byte_cache_lock = Lock()

# Higher-level data caches
_rustdoc_cache: OrderedDict[
    tuple[str, str, str | None, int | None], tuple[float, dict[str, Any]]
] = OrderedDict()
_crates_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
_crate_source_cache: OrderedDict[tuple[str, str], tuple[float, bytes]] = OrderedDict()
_data_cache_lock = Lock()


class DataError(Exception):
    """Structured internal error for external API/data failures."""

    def __init__(self, code: str, message: str, *, context: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = context or {}


def _parse_semver(version: str) -> tuple[int, int, int, tuple[Any, ...], bool] | None:
    """Parse a semver-like string used by Cargo.lock."""
    match = re.match(
        r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+.*)?$",
        version.strip(),
    )
    if not match:
        return None

    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3))
    prerelease = match.group(4)
    is_stable = prerelease is None

    pre_parts: tuple[Any, ...] = ()
    if prerelease:
        parsed_parts: list[Any] = []
        for part in prerelease.split("."):
            if part.isdigit():
                parsed_parts.append((0, int(part)))
            else:
                parsed_parts.append((1, part))
        pre_parts = tuple(parsed_parts)

    return major, minor, patch, pre_parts, is_stable


def _version_sort_key(version: str) -> tuple[int, int, int, int, tuple[Any, ...], str]:
    parsed = _parse_semver(version)
    if not parsed:
        return -1, -1, -1, -1, (), version

    major, minor, patch, prerelease_parts, is_stable = parsed
    stable_rank = 1 if is_stable else 0
    return major, minor, patch, stable_rank, prerelease_parts, version


def _sort_versions_desc(versions: set[str] | list[str]) -> list[str]:
    return sorted(set(versions), key=_version_sort_key, reverse=True)


def select_preferred_version(versions: list[str]) -> str | None:
    """Pick deterministic preferred version: highest stable else highest prerelease."""
    if not versions:
        return None

    stable_versions: list[str] = []
    for version in versions:
        parsed = _parse_semver(version)
        if parsed and parsed[4]:
            stable_versions.append(version)
    if stable_versions:
        return max(stable_versions, key=_version_sort_key)
    return max(versions, key=_version_sort_key)


def parse_cargo_lock(
    cargo_lock_path: Path,
) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    """Parse Cargo.lock and return (crate->versions, package_records)."""
    with open(cargo_lock_path, "rb") as f:
        data = tomllib.load(f)

    version_map: dict[str, set[str]] = {}
    package_records: list[dict[str, Any]] = []

    for pkg in data.get("package", []):
        name = pkg.get("name")
        version = pkg.get("version")
        if not name or not version:
            continue

        version_map.setdefault(name, set()).add(version)
        package_records.append(
            {
                "name": name,
                "version": version,
                "source": pkg.get("source"),
            }
        )

    sorted_map = {
        name: _sort_versions_desc(versions) for name, versions in version_map.items()
    }
    return sorted_map, package_records


def resolve_version(
    crate_name: str, explicit_version: str | None
) -> tuple[str, str]:
    """Resolve crate version for lookups.

    Returns (version, version_source) where version_source is one of:
    - "explicit": caller provided version parameter
    - "cargo_lock": version resolved from Cargo.lock
    - "default": fell back to "latest"
    """
    if explicit_version is not None:
        return explicit_version, "explicit"
    if crate_name in _cargo_lock_versions:
        preferred = select_preferred_version(_cargo_lock_versions[crate_name])
        if preferred:
            return preferred, "cargo_lock"
    return DEFAULT_VERSION, "default"


def _dependency_source_from_spec(spec: Any) -> str | None:
    if not isinstance(spec, dict):
        return None
    if "path" in spec:
        return f"path:{spec['path']}"
    if "git" in spec:
        return f"git:{spec['git']}"
    if "registry" in spec:
        return f"registry:{spec['registry']}"
    if spec.get("workspace") is True:
        return "workspace"
    return None


def _dependency_name_from_entry(dep_name: str, dep_spec: Any) -> str:
    if isinstance(dep_spec, dict) and isinstance(dep_spec.get("package"), str):
        return dep_spec["package"]
    return dep_name


def _collect_dependency_entries_from_manifest_table(
    table: dict[str, Any],
    manifest_rel_path: str,
    scope_prefix: str,
    include_dev: bool,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    sections: list[tuple[str, str]] = [
        ("dependencies", "runtime"),
        ("build-dependencies", "build"),
    ]
    if include_dev:
        sections.append(("dev-dependencies", "dev"))

    for section_name, base_kind in sections:
        section = table.get(section_name)
        if not isinstance(section, dict):
            continue

        kind = base_kind if scope_prefix == "root" else f"target-{base_kind}"
        for dep_key, dep_spec in section.items():
            if not isinstance(dep_key, str):
                continue
            entries.append(
                {
                    "name": _dependency_name_from_entry(dep_key, dep_spec),
                    "kind": kind,
                    "declared_in": manifest_rel_path,
                    "source": _dependency_source_from_spec(dep_spec),
                }
            )

    return entries


def _collect_direct_dependencies_from_manifest(
    manifest_data: dict[str, Any],
    manifest_path: Path,
    project_dir: Path,
    include_dev: bool,
) -> list[dict[str, Any]]:
    rel_manifest = str(manifest_path.relative_to(project_dir))
    entries = _collect_dependency_entries_from_manifest_table(
        manifest_data, rel_manifest, "root", include_dev
    )

    target_table = manifest_data.get("target")
    if isinstance(target_table, dict):
        for target_spec in target_table.values():
            if isinstance(target_spec, dict):
                entries.extend(
                    _collect_dependency_entries_from_manifest_table(
                        target_spec, rel_manifest, "target", include_dev
                    )
                )

    return entries


def _manifest_included_by_workspace(
    manifest_path: Path,
    project_dir: Path,
    member_patterns: list[str],
    exclude_patterns: list[str],
) -> bool:
    rel_path = str(manifest_path.parent.relative_to(project_dir))
    if rel_path == ".":
        return False

    included = any(fnmatch.fnmatch(rel_path, pattern) for pattern in member_patterns)
    if not included:
        return False
    excluded = any(fnmatch.fnmatch(rel_path, pattern) for pattern in exclude_patterns)
    return not excluded


def discover_project_manifests(
    project_dir: Path,
    include_workspace_members: bool,
) -> list[Path]:
    root_manifest = project_dir / "Cargo.toml"
    if not root_manifest.exists():
        return []

    manifests = [root_manifest]
    if not include_workspace_members:
        return manifests

    try:
        with open(root_manifest, "rb") as f:
            root_data = tomllib.load(f)
    except Exception:
        return manifests

    workspace = root_data.get("workspace")
    if not isinstance(workspace, dict):
        return manifests

    member_patterns = workspace.get("members", [])
    exclude_patterns = workspace.get("exclude", [])
    if not isinstance(member_patterns, list):
        member_patterns = []
    if not isinstance(exclude_patterns, list):
        exclude_patterns = []

    seen = {root_manifest.resolve()}
    for manifest in project_dir.rglob("Cargo.toml"):
        resolved_manifest = manifest.resolve()
        if resolved_manifest in seen:
            continue
        if _manifest_included_by_workspace(
            manifest, project_dir, member_patterns, exclude_patterns
        ):
            manifests.append(manifest)
            seen.add(resolved_manifest)

    return manifests


def _source_for_crate_version(crate_name: str, selected_version: str | None) -> str | None:
    if not selected_version:
        return None
    for package in _cargo_lock_packages:
        if package.get("name") == crate_name and package.get("version") == selected_version:
            return package.get("source")
    return None


def normalize_crate_path(path: str) -> str:
    """Normalize a crate path for consistent handling."""
    path = path.strip("/")
    path = re.sub(r"/+", "/", path)
    return path


def convert_url_to_key(url: str) -> str:
    """Convert a docs.rs URL to a key for page lookup."""
    parsed = urlparse(url)

    if parsed.netloc == "docs.rs":
        path = parsed.path.strip("/")
        if path.endswith(".html"):
            path = path[:-5]
        return path

    path = url.strip("/")
    if path.endswith(".html"):
        path = path[:-5]
    return path


def normalize_item_to_key(item: str) -> str:
    """Normalize item input into a docs key.

    Accepts:
    - docs.rs keys (crate/version/path)
    - docs.rs://crate/version/path
    - https://docs.rs/... URLs
    - https://doc.rust-lang.org/... URLs (unsupported by JSON backend but normalized)
    """
    raw = item.strip()
    if raw.startswith("docs.rs://"):
        raw = raw[10:]
        return normalize_crate_path(raw)

    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        if parsed.netloc == "docs.rs":
            return normalize_crate_path(convert_url_to_key(raw))
        if parsed.netloc == "doc.rust-lang.org":
            path = parsed.path.strip("/")
            if path.endswith(".html"):
                path = path[:-5]
            return normalize_crate_path(f"rust-lang/{path}")

    return normalize_crate_path(convert_url_to_key(raw))


def parse_key_metadata(key: str) -> dict[str, Any]:
    normalized = normalize_crate_path(key)
    parts = normalized.split("/")

    if normalized.startswith("rust-lang/"):
        rust_path = normalized[len("rust-lang/") :]
        return {
            "key": normalized,
            "crate": "rust-lang",
            "version": rust_path.split("/", 1)[0] if "/" in rust_path else "",
            "path": rust_path,
            "is_rust_lang": True,
            "parts": parts,
        }

    if len(parts) < 2:
        return {
            "key": normalized,
            "crate": "",
            "version": "",
            "path": "",
            "is_rust_lang": False,
            "parts": parts,
        }

    return {
        "key": normalized,
        "crate": parts[0],
        "version": parts[1],
        "path": "/".join(parts[2:]) if len(parts) > 2 else "",
        "is_rust_lang": False,
        "parts": parts,
    }


def resolve_item_key_and_version_source(
    item_key: str, version_override: str | None
) -> tuple[str, str]:
    """Resolve a key with optional version override and return version source."""
    meta = parse_key_metadata(item_key)
    if meta["is_rust_lang"]:
        raise DataError(
            "rustdoc_item_not_found",
            "rust-lang standard library keys are not supported by the rustdoc JSON backend",
            context={"item_key": item_key},
        )

    parts = meta["parts"]
    if len(parts) < 2:
        return item_key, "item_key"

    if version_override is not None:
        parts[1] = version_override
        return "/".join(parts), "explicit"

    if parts[1] == DEFAULT_VERSION and parts[0]:
        resolved_version, source = resolve_version(parts[0], None)
        parts[1] = resolved_version
        return "/".join(parts), source

    return "/".join(parts), "item_key"


def _is_transient_http_error(exc: Exception) -> bool:
    return isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout))


async def _get_http_client() -> httpx.AsyncClient:
    global _http_client, _http_client_loop

    current_loop = asyncio.get_running_loop()
    needs_new_client = (
        _http_client is None
        or _http_client.is_closed
        or _http_client_loop is not current_loop
    )

    if needs_new_client:
        if _http_client is not None and not _http_client.is_closed:
            try:
                await _http_client.aclose()
            except RuntimeError:
                pass
        _http_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        _http_client_loop = current_loop

    return _http_client


async def close_http_client() -> None:
    global _http_client, _http_client_loop
    if _http_client is not None:
        try:
            await _http_client.aclose()
        except RuntimeError:
            pass
        _http_client = None
        _http_client_loop = None


def _get_cached_bytes(url: str) -> tuple[bytes, str, dict[str, str]] | None:
    now = time.time()
    with _byte_cache_lock:
        cached = _byte_cache.get(url)
        if not cached:
            return None

        timestamp, body, final_url, headers = cached
        if now - timestamp > CACHE_TTL_SECONDS:
            _byte_cache.pop(url, None)
            return None

        _byte_cache.move_to_end(url)
        return body, final_url, headers


def _set_cached_bytes(
    url: str, body: bytes, final_url: str, headers: dict[str, str]
) -> None:
    now = time.time()
    with _byte_cache_lock:
        for cache_key in (url, final_url):
            _byte_cache[cache_key] = (now, body, final_url, headers)
            _byte_cache.move_to_end(cache_key)

        while len(_byte_cache) > CACHE_MAX_ENTRIES:
            _byte_cache.popitem(last=False)


async def fetch_bytes(url: str) -> tuple[bytes, str, dict[str, str]]:
    """Fetch bytes and return (body, final_url, headers)."""
    cached = _get_cached_bytes(url)
    if cached is not None:
        return cached

    client = await _get_http_client()
    last_error: Exception | None = None

    for attempt in range(MAX_HTTP_RETRIES + 1):
        try:
            response = await client.get(url)
            response.raise_for_status()
            body = response.content
            final_url = str(response.url)
            headers = {k.lower(): v for k, v in response.headers.items()}
            _set_cached_bytes(url, body, final_url, headers)
            return body, final_url, headers
        except Exception as exc:
            last_error = exc
            if attempt >= MAX_HTTP_RETRIES or not _is_transient_http_error(exc):
                raise

    assert last_error is not None
    raise last_error


def _get_cached_dict(
    cache: OrderedDict[Any, tuple[float, dict[str, Any]]], key: Any
) -> dict[str, Any] | None:
    now = time.time()
    with _data_cache_lock:
        cached = cache.get(key)
        if not cached:
            return None
        timestamp, payload = cached
        if now - timestamp > CACHE_TTL_SECONDS:
            cache.pop(key, None)
            return None

        cache.move_to_end(key)
        return payload


def _set_cached_dict(
    cache: OrderedDict[Any, tuple[float, dict[str, Any]]], key: Any, value: dict[str, Any]
) -> None:
    now = time.time()
    with _data_cache_lock:
        cache[key] = (now, value)
        cache.move_to_end(key)
        while len(cache) > CACHE_MAX_ENTRIES:
            cache.popitem(last=False)


def _get_cached_source_bytes(key: tuple[str, str]) -> bytes | None:
    now = time.time()
    with _data_cache_lock:
        cached = _crate_source_cache.get(key)
        if not cached:
            return None
        timestamp, data = cached
        if now - timestamp > CACHE_TTL_SECONDS:
            _crate_source_cache.pop(key, None)
            return None
        _crate_source_cache.move_to_end(key)
        return data


def _set_cached_source_bytes(key: tuple[str, str], data: bytes) -> None:
    now = time.time()
    with _data_cache_lock:
        _crate_source_cache[key] = (now, data)
        _crate_source_cache.move_to_end(key)
        while len(_crate_source_cache) > CACHE_MAX_ENTRIES:
            _crate_source_cache.popitem(last=False)


async def fetch_json_from_url(url: str) -> dict[str, Any]:
    cached = _get_cached_dict(_crates_cache, url)
    if cached is not None:
        return cached

    try:
        body, _, _ = await fetch_bytes(url)
        payload = httpx.Response(200, content=body).json()
    except httpx.HTTPStatusError as exc:
        raise DataError(
            "crates_io_unavailable",
            f"Failed to fetch crates.io JSON: {exc}",
            context={"url": url},
        ) from exc
    except Exception as exc:
        raise DataError(
            "crates_io_unavailable",
            f"Failed to parse crates.io JSON: {exc}",
            context={"url": url},
        ) from exc

    _set_cached_dict(_crates_cache, url, payload)
    return payload


async def fetch_crates_io_crate(crate_name: str) -> dict[str, Any]:
    return await fetch_json_from_url(f"{CRATES_IO_BASE_URL}/crates/{quote(crate_name)}")


async def fetch_crates_io_dependencies(crate_name: str, version: str) -> dict[str, Any]:
    return await fetch_json_from_url(
        f"{CRATES_IO_BASE_URL}/crates/{quote(crate_name)}/{quote(version)}/dependencies"
    )


async def fetch_crates_io_search(query: str, page: int, per_page: int = 30) -> dict[str, Any]:
    return await fetch_json_from_url(
        f"{CRATES_IO_BASE_URL}/crates?q={quote(query)}&page={page}&per_page={per_page}"
    )


def _normalize_version_for_crates_io(version: str, crate_record: dict[str, Any]) -> str:
    if version != DEFAULT_VERSION:
        return version
    max_stable = crate_record.get("crate", {}).get("max_stable_version")
    newest = crate_record.get("crate", {}).get("newest_version")
    default = crate_record.get("crate", {}).get("max_version")
    return max_stable or newest or default or DEFAULT_VERSION


async def resolve_crates_io_version(
    crate_name: str,
    explicit_version: str | None,
) -> tuple[str, str, dict[str, Any], dict[str, Any] | None]:
    requested_version, version_source = resolve_version(crate_name, explicit_version)
    crate_record = await fetch_crates_io_crate(crate_name)
    resolved_version = _normalize_version_for_crates_io(requested_version, crate_record)

    version_record = None
    for record in crate_record.get("versions", []):
        if record.get("num") == resolved_version:
            version_record = record
            break

    return resolved_version, version_source, crate_record, version_record


def build_rustdoc_json_url(
    crate_name: str,
    version: str,
    target: str | None = None,
    rustdoc_format: int | None = None,
) -> str:
    target_segment = f"/{target}" if target else ""
    if rustdoc_format is None:
        return f"{BASE_URL}/crate/{crate_name}/{version}{target_segment}/json.gz"
    return f"{BASE_URL}/crate/{crate_name}/{version}{target_segment}/json/{rustdoc_format}.gz"


def build_rustdoc_json_urls(
    crate_name: str,
    version: str,
    target: str | None = None,
    rustdoc_format: int | None = None,
) -> list[str]:
    """Build preferred rustdoc JSON endpoint candidates."""
    target_segment = f"/{target}" if target else ""
    base = f"{BASE_URL}/crate/{crate_name}/{version}{target_segment}"
    if rustdoc_format is None:
        return [f"{base}/json.gz", f"{base}/json"]
    return [f"{base}/json/{rustdoc_format}.gz", f"{base}/json/{rustdoc_format}"]


async def fetch_rustdoc_json_payload(
    crate_name: str,
    version: str,
    target: str | None,
    rustdoc_format: int | None,
) -> dict[str, Any]:
    cache_key = (crate_name, version, target, rustdoc_format)
    cached = _get_cached_dict(_rustdoc_cache, cache_key)
    if cached is not None:
        return cached

    attempted_urls = build_rustdoc_json_urls(crate_name, version, target, rustdoc_format)
    body: bytes | None = None
    final_url: str | None = None
    headers: dict[str, str] | None = None
    last_error: Exception | None = None

    for url in attempted_urls:
        try:
            body, final_url, headers = await fetch_bytes(url)
            break
        except httpx.HTTPStatusError as exc:
            last_error = exc
            status_code = exc.response.status_code if exc.response else None
            if status_code == 404:
                continue
            raise DataError(
                "rustdoc_json_unavailable",
                f"Failed to fetch rustdoc JSON: {exc}",
                context={
                    "crate": crate_name,
                    "version": version,
                    "target": target,
                    "rustdoc_format": rustdoc_format,
                    "url": url,
                    "attempted_urls": attempted_urls,
                },
            ) from exc
        except Exception as exc:
            last_error = exc
            raise DataError(
                "rustdoc_json_unavailable",
                f"Failed to fetch rustdoc JSON: {exc}",
                context={
                    "crate": crate_name,
                    "version": version,
                    "target": target,
                    "rustdoc_format": rustdoc_format,
                    "url": url,
                    "attempted_urls": attempted_urls,
                },
            ) from exc

    if body is None or final_url is None or headers is None:
        raise DataError(
            "rustdoc_json_unavailable",
            "Rustdoc JSON not available for requested crate/version/target/format",
            context={
                "crate": crate_name,
                "version": version,
                "target": target,
                "rustdoc_format": rustdoc_format,
                "url": attempted_urls[0],
                "attempted_urls": attempted_urls,
                "last_error": str(last_error) if last_error else None,
            },
        )

    try:
        payload = _decode_rustdoc_json_bytes(body, final_url, headers)
    except DataError as exc:
        raise DataError(
            exc.code,
            exc.message,
            context={**exc.context, "attempted_urls": attempted_urls},
        ) from exc

    _set_cached_dict(_rustdoc_cache, cache_key, payload)
    return payload


def json_loads_bytes(data: bytes) -> dict[str, Any]:
    """JSON loader helper (kept separate for easier monkeypatching in tests)."""
    import json

    return json.loads(data)


def _looks_like_zstd(data: bytes) -> bool:
    # Zstandard frame magic number: 28 B5 2F FD
    return data.startswith(b"\x28\xb5\x2f\xfd")


def _decode_rustdoc_json_bytes(
    body: bytes,
    final_url: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    content_type = headers.get("content-type", "")
    content_encoding = headers.get("content-encoding", "")
    content_disposition = headers.get("content-disposition", "")

    # Some docs.rs payloads are transport-compressed and also distributed as
    # compressed files (e.g. .json.zst), which can result in nested layers.
    queue: list[tuple[bytes, int]] = [(body, 0)]
    seen: set[tuple[int, bytes]] = set()
    max_decode_depth = 3
    last_error: Exception | None = None
    zstd_import_error: ModuleNotFoundError | None = None

    while queue:
        candidate, depth = queue.pop(0)
        key = (len(candidate), candidate[:16])
        if key in seen:
            continue
        seen.add(key)

        try:
            return json_loads_bytes(candidate)
        except Exception as exc:
            last_error = exc
        if depth >= max_decode_depth:
            continue

        gzip_hint = (
            candidate.startswith(b"\x1f\x8b")
            or (depth == 0 and final_url.endswith(".gz"))
            or (depth == 0 and "gzip" in content_type)
            or (depth == 0 and "gzip" in content_encoding)
        )
        if gzip_hint:
            try:
                queue.append((gzip.decompress(candidate), depth + 1))
            except Exception:
                pass

        zstd_hint = (
            _looks_like_zstd(candidate)
            or (depth == 0 and "zstd" in content_encoding)
            or (depth == 0 and ".zst" in content_disposition)
        )
        if zstd_hint:
            try:
                import zstandard as zstd

                dctx = zstd.ZstdDecompressor()
                with dctx.stream_reader(io.BytesIO(candidate)) as reader:
                    queue.append((reader.read(), depth + 1))
            except ModuleNotFoundError as exc:
                zstd_import_error = exc
            except Exception:
                pass

    if zstd_import_error is not None:
        raise DataError(
            "rustdoc_format_unsupported",
            "Rustdoc JSON payload appears zstd-compressed, but 'zstandard' is not installed",
            context={"url": final_url},
        ) from zstd_import_error

    raise DataError(
        "rustdoc_format_unsupported",
        f"Failed to parse rustdoc JSON payload: {last_error}",
        context={"url": final_url},
    )


@dataclass
class RustdocSnapshot:
    crate_name: str
    version: str
    version_source: str
    format_version: int
    target_triple: str
    root_id: str
    index: dict[str, dict[str, Any]]
    paths: dict[str, dict[str, Any]]
    external_crates: dict[str, dict[str, Any]]
    local_key_to_id: dict[str, str]
    id_to_local_key: dict[str, str]
    alias_local_key_to_id: dict[str, str]


def _kind_prefix(kind: str) -> str:
    return {
        "struct": "struct",
        "enum": "enum",
        "trait": "trait",
        "function": "fn",
        "type_alias": "type",
        "macro": "macro",
        "constant": "constant",
        "static": "static",
        "union": "union",
        "primitive": "primitive",
        "derive": "derive",
        "trait_alias": "traitalias",
    }.get(kind, kind)


def local_key_from_path_entry(path_entry: dict[str, Any]) -> str | None:
    path = path_entry.get("path")
    kind = path_entry.get("kind")
    if not isinstance(path, list) or not path:
        return None

    if kind == "module":
        return "/".join(path)

    name = path[-1]
    prefix = _kind_prefix(kind)
    parent = "/".join(path[:-1])
    if not parent:
        return f"{prefix}.{name}"
    return f"{parent}/{prefix}.{name}"


def normalize_local_key(local_key: str) -> str:
    key = normalize_crate_path(local_key)
    if key.endswith("/index"):
        key = key[:-6]
    return key


async def get_rustdoc_snapshot(
    crate_name: str,
    version: str | None,
    target: str | None,
    rustdoc_format: int | None,
) -> RustdocSnapshot:
    resolved_version, version_source = resolve_version(crate_name, version)
    payload = await fetch_rustdoc_json_payload(
        crate_name, resolved_version, target, rustdoc_format
    )

    format_version = payload.get("format_version")
    if not isinstance(format_version, int):
        raise DataError(
            "rustdoc_format_unsupported",
            "Rustdoc JSON payload missing integer format_version",
            context={"crate": crate_name, "version": resolved_version},
        )

    target_triple = payload.get("target", {}).get("triple")
    if not isinstance(target_triple, str):
        target_triple = "unknown"

    index = payload.get("index", {})
    paths = payload.get("paths", {})
    external_crates = payload.get("external_crates", {})
    root_id = str(payload.get("root"))

    if not isinstance(index, dict) or root_id not in index:
        raise DataError(
            "rustdoc_format_unsupported",
            "Rustdoc JSON payload missing root/index graph",
            context={"crate": crate_name, "version": resolved_version},
        )

    local_key_to_id: dict[str, str] = {}
    id_to_local_key: dict[str, str] = {}
    alias_local_key_to_id: dict[str, str] = {}

    # Root module key always exists as crate name.
    root_key = crate_name
    local_key_to_id[root_key] = root_id
    id_to_local_key[root_id] = root_key

    for item_id, path_entry in paths.items():
        if not isinstance(path_entry, dict):
            continue
        if path_entry.get("crate_id") != 0:
            continue
        local_key = local_key_from_path_entry(path_entry)
        if not local_key:
            continue
        local_key = normalize_local_key(local_key)
        local_key_to_id[local_key] = str(item_id)
        id_to_local_key[str(item_id)] = local_key

    # Build alias keys from module-level `use` re-exports so docs.rs page paths
    # that differ from canonical paths still resolve to the same target items.
    for module_key, module_id in list(local_key_to_id.items()):
        module_item = index.get(module_id)
        if not isinstance(module_item, dict) or inner_kind(module_item) != "module":
            continue

        module_data = module_item.get("inner", {}).get("module", {})
        child_ids = module_data.get("items", []) if isinstance(module_data, dict) else []
        for child_id in child_ids:
            child_item = index.get(str(child_id))
            if not isinstance(child_item, dict) or inner_kind(child_item) != "use":
                continue

            use_data = child_item.get("inner", {}).get("use", {})
            if not isinstance(use_data, dict):
                continue

            alias_name = use_data.get("name")
            target_id_raw = use_data.get("id")
            if not isinstance(alias_name, str) or target_id_raw is None:
                continue

            target_id = str(target_id_raw)
            target_item = index.get(target_id)
            if not isinstance(target_item, dict):
                continue

            target_kind = inner_kind(target_item)
            if target_kind == "module":
                alias_local_key = normalize_local_key(f"{module_key}/{alias_name}")
            else:
                alias_local_key = normalize_local_key(
                    f"{module_key}/{_kind_prefix(target_kind)}.{alias_name}"
                )

            alias_local_key_to_id.setdefault(alias_local_key, target_id)

    return RustdocSnapshot(
        crate_name=crate_name,
        version=resolved_version,
        version_source=version_source,
        format_version=format_version,
        target_triple=target_triple,
        root_id=root_id,
        index=index,
        paths=paths,
        external_crates=external_crates,
        local_key_to_id=local_key_to_id,
        id_to_local_key=id_to_local_key,
        alias_local_key_to_id=alias_local_key_to_id,
    )


def parse_absolute_item_key(item_key: str) -> tuple[str, str, str]:
    normalized = normalize_item_to_key(item_key)
    parts = normalized.split("/")
    if len(parts) < 3:
        raise DataError(
            "rustdoc_item_not_found",
            "Item key must include crate/version/path",
            context={"item_key": item_key},
        )
    crate_name = parts[0]
    version = parts[1]
    local_key = normalize_local_key("/".join(parts[2:]))
    return crate_name, version, local_key


def docs_url_for_local_key(snapshot: RustdocSnapshot, local_key: str) -> str:
    normalized = normalize_local_key(local_key)
    if normalized == snapshot.crate_name:
        return f"{BASE_URL}/{snapshot.crate_name}/{snapshot.version}/"
    return f"{BASE_URL}/{snapshot.crate_name}/{snapshot.version}/{normalized}.html"


def docs_protocol_for_local_key(snapshot: RustdocSnapshot, local_key: str) -> str:
    return f"docs.rs://{snapshot.crate_name}/{snapshot.version}/{local_key}"


def inner_kind(item: dict[str, Any]) -> str:
    inner = item.get("inner")
    if isinstance(inner, dict) and inner:
        return next(iter(inner.keys()))
    return "unknown"


def normalize_kind(kind: str) -> str:
    return {
        "function": "function",
        "struct": "struct",
        "enum": "enum",
        "trait": "trait",
        "type_alias": "type_alias",
        "module": "module",
        "macro": "macro",
        "constant": "constant",
        "static": "static",
        "union": "union",
        "primitive": "primitive",
        "assoc_type": "associated_type",
        "assoc_const": "associated_const",
        "impl": "impl",
        "variant": "variant",
        "struct_field": "field",
        "use": "use",
        "trait_alias": "trait_alias",
    }.get(kind, kind)


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def render_visibility(visibility: Any) -> str:
    if visibility == "public":
        return "pub "
    return ""


def render_generic_args(args: Any) -> str:
    if not isinstance(args, dict):
        return ""

    if "angle_bracketed" in args:
        ab = args["angle_bracketed"]
        rendered: list[str] = []
        for arg in ab.get("args", []):
            if "type" in arg:
                rendered.append(render_type(arg["type"]))
            elif "lifetime" in arg:
                rendered.append(arg["lifetime"])
            elif "const" in arg:
                rendered.append(str(arg["const"]))
            elif "infer" in arg:
                rendered.append("_")
        for constraint in ab.get("constraints", []):
            rendered.append(str(constraint))
        return f"<{', '.join(rendered)}>" if rendered else ""

    if "parenthesized" in args:
        pr = args["parenthesized"]
        inputs = ", ".join(render_type(t) for t in pr.get("inputs", []))
        output = pr.get("output")
        if output is None:
            return f"({inputs})"
        return f"({inputs}) -> {render_type(output)}"

    return ""


def render_type(value: Any) -> str:
    if not isinstance(value, dict):
        return str(value)

    if "resolved_path" in value:
        rp = value["resolved_path"]
        path = rp.get("path") or ""
        return f"{path}{render_generic_args(rp.get('args'))}"

    if "generic" in value:
        return value["generic"]

    if "primitive" in value:
        return value["primitive"]

    if "borrowed_ref" in value:
        br = value["borrowed_ref"]
        lifetime = br.get("lifetime") or ""
        lifetime_text = f"{lifetime} " if lifetime else ""
        mut = "mut " if br.get("is_mutable") else ""
        return f"&{lifetime_text}{mut}{render_type(br.get('type'))}".strip()

    if "raw_pointer" in value:
        rp = value["raw_pointer"]
        mut = "mut " if rp.get("is_mutable") else "const "
        return f"*{mut}{render_type(rp.get('type'))}"

    if "tuple" in value:
        return f"({', '.join(render_type(v) for v in value['tuple'])})"

    if "slice" in value:
        return f"[{render_type(value['slice'])}]"

    if "array" in value:
        arr = value["array"]
        return f"[{render_type(arr.get('type'))}; {arr.get('len')}]"

    if "qualified_path" in value:
        qp = value["qualified_path"]
        self_type = render_type(qp.get("self_type"))
        trait = qp.get("trait", {}).get("path", "")
        name = qp.get("name", "")
        args = render_generic_args(qp.get("args"))
        trait_part = f" as {trait}" if trait else ""
        return f"<{self_type}{trait_part}>::{name}{args}"

    if "function_pointer" in value:
        fp = value["function_pointer"]
        sig = fp.get("sig", {})
        inputs = ", ".join(render_type(t[1]) for t in sig.get("inputs", []))
        output = render_type(sig.get("output"))
        return f"fn({inputs}) -> {output}"

    if "impl_trait" in value:
        bounds = value["impl_trait"]
        return "impl " + " + ".join(render_generic_bound(b) for b in bounds)

    if "dyn_trait" in value:
        dyn = value["dyn_trait"]
        traits = dyn.get("traits", [])
        rendered = " + ".join(render_generic_bound(t) for t in traits)
        return f"dyn {rendered}" if rendered else "dyn _"

    if "infer" in value:
        return "_"

    return normalize_whitespace(str(value))


def render_generic_bound(bound: Any) -> str:
    if not isinstance(bound, dict):
        return str(bound)

    if "trait_bound" in bound:
        tb = bound["trait_bound"]
        trait = tb.get("trait", {})
        path = trait.get("path", "")
        args = render_generic_args(trait.get("args"))
        modifier = tb.get("modifier")
        if modifier == "maybe":
            return f"?{path}{args}"
        return f"{path}{args}"

    if "outlives" in bound:
        return bound["outlives"]

    if "use" in bound:
        return f"use<{', '.join(bound['use'])}>"

    return normalize_whitespace(str(bound))


def render_generic_params(generics: dict[str, Any]) -> str:
    params = generics.get("params", []) if isinstance(generics, dict) else []
    rendered: list[str] = []
    for param in params:
        name = param.get("name")
        kind = param.get("kind", {})
        if "lifetime" in kind:
            rendered.append(name)
            continue
        if "const" in kind:
            ty = render_type(kind["const"].get("type"))
            rendered.append(f"const {name}: {ty}")
            continue
        if "type" in kind:
            bounds = kind["type"].get("bounds", [])
            if bounds:
                rendered.append(f"{name}: {' + '.join(render_generic_bound(b) for b in bounds)}")
            else:
                rendered.append(name)
            continue
        rendered.append(name)

    return f"<{', '.join(rendered)}>" if rendered else ""


def render_where_clause(generics: dict[str, Any]) -> str | None:
    where_predicates = generics.get("where_predicates", []) if isinstance(generics, dict) else []
    if not where_predicates:
        return None

    parts: list[str] = []
    for pred in where_predicates:
        if "bound_predicate" in pred:
            bp = pred["bound_predicate"]
            ty = render_type(bp.get("type"))
            bounds = bp.get("bounds", [])
            bounds_text = " + ".join(render_generic_bound(b) for b in bounds)
            if bounds_text:
                parts.append(f"{ty}: {bounds_text}")
            else:
                parts.append(ty)
        elif "region_predicate" in pred:
            rp = pred["region_predicate"]
            lifetime = rp.get("lifetime", "")
            bounds = " + ".join(rp.get("bounds", []))
            parts.append(f"{lifetime}: {bounds}" if bounds else lifetime)
        elif "eq_predicate" in pred:
            ep = pred["eq_predicate"]
            lhs = render_type(ep.get("lhs"))
            rhs = render_term(ep.get("rhs"))
            parts.append(f"{lhs} = {rhs}")
        else:
            parts.append(normalize_whitespace(str(pred)))

    if not parts:
        return None
    return "where " + ", ".join(parts)


def render_term(term: Any) -> str:
    if isinstance(term, dict):
        if "type" in term:
            return render_type(term["type"])
        if "constant" in term:
            return str(term["constant"])
    return str(term)


def render_function_signature(name: str, item: dict[str, Any]) -> tuple[str, str | None, str | None]:
    function_data = item.get("inner", {}).get("function", {})
    sig = function_data.get("sig", {})
    generics = function_data.get("generics", {})
    header = function_data.get("header", {})

    qualifiers: list[str] = []
    if item.get("visibility") == "public":
        qualifiers.append("pub")
    if header.get("is_const"):
        qualifiers.append("const")
    if header.get("is_async"):
        qualifiers.append("async")
    if header.get("is_unsafe"):
        qualifiers.append("unsafe")

    abi = header.get("abi")
    if abi and abi != "Rust":
        qualifiers.append(f'extern "{abi}"')

    inputs: list[str] = []
    for arg_name, arg_type in sig.get("inputs", []):
        inputs.append(f"{arg_name}: {render_type(arg_type)}")

    if sig.get("is_c_variadic"):
        inputs.append("...")

    output = sig.get("output")
    output_text = "" if output is None else f" -> {render_type(output)}"

    generics_text = render_generic_params(generics)
    where_clause = render_where_clause(generics)
    qualifier_text = " ".join(qualifiers)
    head = f"{qualifier_text} fn {name}{generics_text}({', '.join(inputs)}){output_text}".strip()
    return normalize_whitespace(head), where_clause, generics_text[1:-1] if generics_text else None


def render_struct_signature(name: str, item: dict[str, Any]) -> tuple[str, str | None, str | None]:
    struct_data = item.get("inner", {}).get("struct", {})
    generics = struct_data.get("generics", {})
    generics_text = render_generic_params(generics)
    where_clause = render_where_clause(generics)
    visibility = render_visibility(item.get("visibility"))

    kind_data = struct_data.get("kind", {})
    if "tuple" in kind_data:
        signature = f"{visibility}struct {name}{generics_text}(...)"
    elif "unit" in kind_data:
        signature = f"{visibility}struct {name}{generics_text};"
    else:
        signature = f"{visibility}struct {name}{generics_text} {{ ... }}"
    return normalize_whitespace(signature), where_clause, generics_text[1:-1] if generics_text else None


def render_enum_signature(name: str, item: dict[str, Any]) -> tuple[str, str | None, str | None]:
    enum_data = item.get("inner", {}).get("enum", {})
    generics = enum_data.get("generics", {})
    generics_text = render_generic_params(generics)
    where_clause = render_where_clause(generics)
    visibility = render_visibility(item.get("visibility"))
    signature = f"{visibility}enum {name}{generics_text} {{ ... }}"
    return normalize_whitespace(signature), where_clause, generics_text[1:-1] if generics_text else None


def render_trait_signature(name: str, item: dict[str, Any]) -> tuple[str, str | None, str | None]:
    trait_data = item.get("inner", {}).get("trait", {})
    generics = trait_data.get("generics", {})
    bounds = trait_data.get("bounds", [])
    bounds_text = ""
    if bounds:
        bounds_text = " : " + " + ".join(render_generic_bound(b) for b in bounds)

    generics_text = render_generic_params(generics)
    where_clause = render_where_clause(generics)
    visibility = render_visibility(item.get("visibility"))
    trait_prefix = "unsafe trait" if trait_data.get("is_unsafe") else "trait"
    signature = f"{visibility}{trait_prefix} {name}{generics_text}{bounds_text}"
    return normalize_whitespace(signature), where_clause, generics_text[1:-1] if generics_text else None


def render_type_alias_signature(name: str, item: dict[str, Any]) -> tuple[str, str | None, str | None]:
    alias_data = item.get("inner", {}).get("type_alias", {})
    generics = alias_data.get("generics", {})
    generics_text = render_generic_params(generics)
    where_clause = render_where_clause(generics)
    alias_type = render_type(alias_data.get("type"))
    visibility = render_visibility(item.get("visibility"))
    signature = f"{visibility}type {name}{generics_text} = {alias_type};"
    return normalize_whitespace(signature), where_clause, generics_text[1:-1] if generics_text else None


def render_module_signature(name: str, item: dict[str, Any]) -> tuple[str, str | None, str | None]:
    visibility = render_visibility(item.get("visibility"))
    return normalize_whitespace(f"{visibility}mod {name}"), None, None


def render_macro_signature(name: str, item: dict[str, Any]) -> tuple[str, str | None, str | None]:
    macro_data = item.get("inner", {}).get("macro")
    if isinstance(macro_data, str) and macro_data.strip():
        signature = normalize_whitespace(macro_data)
        return signature, None, None
    visibility = render_visibility(item.get("visibility"))
    return normalize_whitespace(f"{visibility}macro_rules! {name} {{ ... }}"), None, None


def render_constant_signature(name: str, item: dict[str, Any]) -> tuple[str, str | None, str | None]:
    const_data = item.get("inner", {}).get("constant", {})
    visibility = render_visibility(item.get("visibility"))
    ty = render_type(const_data.get("type"))
    expr = const_data.get("expr")
    expr_text = f" = {expr}" if expr is not None else ""
    return normalize_whitespace(f"{visibility}const {name}: {ty}{expr_text};"), None, None


def render_static_signature(name: str, item: dict[str, Any]) -> tuple[str, str | None, str | None]:
    static_data = item.get("inner", {}).get("static", {})
    visibility = render_visibility(item.get("visibility"))
    mut = "mut " if static_data.get("mutable") else ""
    ty = render_type(static_data.get("type"))
    expr = static_data.get("expr")
    expr_text = f" = {expr}" if expr is not None else ""
    return normalize_whitespace(f"{visibility}static {mut}{name}: {ty}{expr_text};"), None, None


def render_union_signature(name: str, item: dict[str, Any]) -> tuple[str, str | None, str | None]:
    union_data = item.get("inner", {}).get("union", {})
    generics = union_data.get("generics", {})
    generics_text = render_generic_params(generics)
    where_clause = render_where_clause(generics)
    visibility = render_visibility(item.get("visibility"))
    return normalize_whitespace(f"{visibility}union {name}{generics_text} {{ ... }}"), where_clause, generics_text[1:-1] if generics_text else None


def build_item_signature(
    item: dict[str, Any],
) -> tuple[str | None, str | None, str | None]:
    name = item.get("name") or ""
    kind = inner_kind(item)

    if kind == "function":
        return render_function_signature(name, item)
    if kind == "struct":
        return render_struct_signature(name, item)
    if kind == "enum":
        return render_enum_signature(name, item)
    if kind == "trait":
        return render_trait_signature(name, item)
    if kind == "type_alias":
        return render_type_alias_signature(name, item)
    if kind == "module":
        return render_module_signature(name, item)
    if kind == "macro":
        return render_macro_signature(name, item)
    if kind == "constant":
        return render_constant_signature(name, item)
    if kind == "static":
        return render_static_signature(name, item)
    if kind == "union":
        return render_union_signature(name, item)

    return None, None, None


def _link_target_from_id(snapshot: RustdocSnapshot, target_id: str) -> tuple[str | None, str | None]:
    target_id = str(target_id)

    local_key = snapshot.id_to_local_key.get(target_id)
    if local_key:
        abs_key = f"{snapshot.crate_name}/{snapshot.version}/{local_key}"
        return abs_key, docs_url_for_local_key(snapshot, local_key)

    path_entry = snapshot.paths.get(target_id)
    if isinstance(path_entry, dict):
        local_external = local_key_from_path_entry(path_entry)
        if local_external:
            path_parts = path_entry.get("path", [])
            crate_name = path_parts[0] if path_parts else ""
            if crate_name:
                url = f"https://docs.rs/{crate_name}/latest/{local_external[len(crate_name):].lstrip('/')}"
                key = convert_url_to_key(url)
                return key, url

    return None, None


def extract_item_links(snapshot: RustdocSnapshot, item: dict[str, Any]) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    raw_links = item.get("links") or {}
    if not isinstance(raw_links, dict):
        return links

    for text, target_id in raw_links.items():
        key, url = _link_target_from_id(snapshot, str(target_id))
        if not key or not url:
            continue
        links.append({"text": str(text), "key": key, "url": url})

    return links


def paginate_content(
    content: str, offset: int = 0, limit: int = DEFAULT_LIMIT
) -> tuple[str, int]:
    total_length = len(content)
    if offset >= total_length:
        return "", total_length

    end = min(offset + limit, total_length)
    paginated = content[offset:end]
    return paginated, total_length


def _is_public_item(item: dict[str, Any]) -> bool:
    return item.get("visibility") == "public"


def _deprecation_payload(item: dict[str, Any]) -> dict[str, Any] | None:
    dep = item.get("deprecation")
    if isinstance(dep, dict):
        return {"since": dep.get("since"), "note": dep.get("note")}
    return None


def _resolve_local_key_for_lookup(snapshot: RustdocSnapshot, local_key: str) -> str:
    normalized = normalize_local_key(local_key)
    if normalized in snapshot.local_key_to_id:
        return normalized
    if normalized in snapshot.alias_local_key_to_id:
        return normalized

    if normalized.endswith("/index"):
        candidate = normalized[:-6]
        if candidate in snapshot.local_key_to_id:
            return candidate
        if candidate in snapshot.alias_local_key_to_id:
            return candidate

    # Fallback for re-exported docs.rs paths that omit intermediate modules.
    # Example: tokio/runtime/struct.Runtime -> tokio/runtime/runtime/struct.Runtime
    parts = normalized.split("/")
    if len(parts) >= 3:
        leaf = parts[-1]
        crate_name = parts[0]
        requested_modules = parts[1:-1]

        def collapse_adjacent(values: list[str]) -> list[str]:
            collapsed: list[str] = []
            for value in values:
                if not collapsed or collapsed[-1] != value:
                    collapsed.append(value)
            return collapsed

        best_key: str | None = None
        best_score = -1
        for candidate in snapshot.local_key_to_id:
            candidate_parts = candidate.split("/")
            if (
                len(candidate_parts) < 3
                or candidate_parts[0] != crate_name
                or candidate_parts[-1] != leaf
            ):
                continue

            candidate_modules = candidate_parts[1:-1]
            score = 0
            if candidate_modules == requested_modules:
                score += 100
            if collapse_adjacent(candidate_modules) == requested_modules:
                score += 80
            if candidate_modules == collapse_adjacent(requested_modules):
                score += 70
            if collapse_adjacent(candidate_modules) == collapse_adjacent(requested_modules):
                score += 50

            common_prefix = 0
            for left, right in zip(candidate_modules, requested_modules):
                if left != right:
                    break
                common_prefix += 1
            score += common_prefix * 5

            score -= abs(len(candidate_modules) - len(requested_modules))
            if score > best_score:
                best_score = score
                best_key = candidate

        if best_key is not None and best_score > 0:
            return best_key

    return normalized


def _item_id_from_local_key(snapshot: RustdocSnapshot, local_key: str) -> str:
    resolved = _resolve_local_key_for_lookup(snapshot, local_key)
    item_id = snapshot.local_key_to_id.get(resolved)
    if not item_id:
        item_id = snapshot.alias_local_key_to_id.get(resolved)
    if item_id:
        return item_id
    raise DataError(
        "rustdoc_item_not_found",
        "Item not found in rustdoc JSON",
        context={
            "crate": snapshot.crate_name,
            "version": snapshot.version,
            "item_key": local_key,
        },
    )


def _display_title_for_item(item: dict[str, Any]) -> str:
    kind = normalize_kind(inner_kind(item))
    name = item.get("name") or "(anonymous)"
    return f"{kind} {name}"


def _render_item_markdown(snapshot: RustdocSnapshot, item_id: str) -> str:
    item = snapshot.index.get(str(item_id))
    if not isinstance(item, dict):
        return ""

    kind = normalize_kind(inner_kind(item))
    name = item.get("name") or "(anonymous)"
    signature, where_clause, _ = build_item_signature(item)
    docs = item.get("docs") or ""

    lines: list[str] = [f"# {kind.capitalize()} {name}"]
    if signature:
        lines.append("")
        lines.append(f"`{signature}`")
    if where_clause:
        lines.append("")
        lines.append(where_clause)
    if docs:
        lines.append("")
        lines.append(docs)

    links = extract_item_links(snapshot, item)
    if links:
        lines.append("")
        lines.append("## Links")
        for link in links[:30]:
            lines.append(f"- [{link['text']}](docs.rs://{link['key']})")

    return "\n".join(lines).strip()


def _module_item_rows(snapshot: RustdocSnapshot, module_item_id: str) -> list[tuple[str, dict[str, Any]]]:
    module_item = snapshot.index.get(str(module_item_id), {})
    module_data = module_item.get("inner", {}).get("module", {})
    item_ids = module_data.get("items", []) if isinstance(module_data, dict) else []

    rows: list[tuple[str, dict[str, Any]]] = []
    for child_id in item_ids:
        child_item = snapshot.index.get(str(child_id))
        if not isinstance(child_item, dict):
            continue
        if not _is_public_item(child_item):
            continue
        child_key = snapshot.id_to_local_key.get(str(child_id))
        if not child_key:
            continue
        rows.append((child_key, child_item))
    return rows


async def _collect_trait_or_impl_members(
    snapshot: RustdocSnapshot,
    item: dict[str, Any],
    member_limit: int,
) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []

    kind = inner_kind(item)
    item_ids: list[Any] = []

    if kind == "trait":
        item_ids = (item.get("inner", {}).get("trait", {}).get("items") or [])
    elif kind == "impl":
        item_ids = (item.get("inner", {}).get("impl", {}).get("items") or [])
    else:
        return members

    for child_id in item_ids:
        if len(members) >= member_limit:
            break
        child = snapshot.index.get(str(child_id))
        if not isinstance(child, dict):
            continue
        child_kind = normalize_kind(inner_kind(child))
        child_signature, _, _ = build_item_signature(child)
        members.append(
            {
                "name": child.get("name"),
                "kind": child_kind,
                "signature": child_signature,
                "section": "associated-items",
            }
        )

    return members


def _score_search_result(
    query_lower: str,
    name: str,
    path_text: str,
    docs_text: str,
) -> int:
    score = 0
    if name == query_lower:
        score += 100
    elif name.startswith(query_lower):
        score += 80
    elif query_lower in name:
        score += 60

    if path_text.startswith(query_lower):
        score += 50
    elif query_lower in path_text:
        score += 30

    if query_lower in docs_text:
        score += 10

    return score


def _make_snippet(docs_text: str, query: str) -> str:
    docs_text = normalize_whitespace(docs_text)
    if not docs_text:
        return ""

    q = query.lower()
    lower = docs_text.lower()
    idx = lower.find(q)
    if idx < 0:
        return docs_text[:180]

    start = max(0, idx - 70)
    end = min(len(docs_text), idx + 110)
    snippet = docs_text[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(docs_text):
        snippet += "..."
    return snippet


def parse_feature_flags_map(features_map: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Parse crates.io feature map into graph representation."""
    feature_entries: dict[str, dict[str, Any]] = {}

    for feature_name, enables_raw in features_map.items():
        enables = [str(entry) for entry in (enables_raw or [])]
        feature_entries[feature_name] = {
            "name": feature_name,
            "enables": sorted(set(enables)),
            "enabled_by": [],
            "is_default": feature_name == "default",
        }

    default_features: list[str] = []
    optional_dependencies: set[str] = set()

    if "default" in feature_entries:
        default_features = [
            token
            for token in feature_entries["default"]["enables"]
            if not token.startswith("dep:")
        ]

    for feature in feature_entries.values():
        for token in feature["enables"]:
            if token.startswith("dep:"):
                dep_name = token.split(":", 1)[1].strip()
                if dep_name:
                    optional_dependencies.add(dep_name)
                continue
            if token in feature_entries:
                feature_entries[token]["enabled_by"].append(feature["name"])

    features = sorted(feature_entries.values(), key=lambda item: item["name"])
    for feature in features:
        feature["enabled_by"] = sorted(set(feature["enabled_by"]))

    return features, sorted(set(default_features)), sorted(optional_dependencies)


async def fetch_feature_flag_data(
    crate_name: str,
    version: str | None = None,
) -> dict[str, Any]:
    """Fetch feature metadata from crates.io version features map."""
    resolved_version, version_source, crate_record, version_record = await resolve_crates_io_version(
        crate_name, version
    )

    if version_record is None:
        return {
            "crate": crate_name,
            "version": resolved_version,
            "version_source": version_source,
            "features": [],
            "default_features": [],
            "optional_dependencies": [],
            "feature_count": 0,
            "source_url": f"{CRATES_IO_BASE_URL}/crates/{quote(crate_name)}",
            "error": "crates_io_unavailable",
            "message": "Version record not found on crates.io",
        }

    features_map = version_record.get("features") or {}
    if not isinstance(features_map, dict):
        features_map = {}

    features, default_features, optional_dependencies = parse_feature_flags_map(features_map)

    return {
        "crate": crate_name,
        "version": resolved_version,
        "version_source": version_source,
        "features": features,
        "default_features": default_features,
        "optional_dependencies": optional_dependencies,
        "feature_count": len(features),
        "source_url": f"{CRATES_IO_BASE_URL}/crates/{quote(crate_name)}",
        "crate_metadata": crate_record.get("crate", {}),
    }


async def fetch_crate_download_bytes(crate_name: str, version: str) -> bytes:
    cache_key = (crate_name, version)
    cached = _get_cached_source_bytes(cache_key)
    if cached is not None:
        return cached

    download_url = f"{CRATES_IO_BASE_URL}/crates/{quote(crate_name)}/{quote(version)}/download"
    body, _, _ = await fetch_bytes(download_url)
    _set_cached_source_bytes(cache_key, body)
    return body


def read_source_file_from_archive(
    archive_bytes: bytes,
    filename: str,
) -> str:
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        normalized = filename.replace("\\", "/")
        candidate = None

        for member in tar.getmembers():
            if not member.isfile():
                continue
            member_name = member.name.replace("\\", "/")
            if member_name.endswith(f"/{normalized}"):
                candidate = member
                break

        if candidate is None:
            raise DataError(
                "source_file_not_found",
                "Source file referenced by rustdoc span not found in crate archive",
                context={"filename": filename},
            )

        file_obj = tar.extractfile(candidate)
        if file_obj is None:
            raise DataError(
                "source_file_not_found",
                "Unable to extract source file from crate archive",
                context={"filename": filename},
            )

        return file_obj.read().decode("utf-8", errors="replace")


@mcp.tool()
async def lookup_main_page(
    crate_name: str,
    version: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    target: str | None = None,
    rustdoc_format: int | None = None,
) -> dict[str, Any]:
    """Look up crate root docs from rustdoc JSON."""
    try:
        snapshot = await get_rustdoc_snapshot(crate_name, version, target, rustdoc_format)
        root_item = snapshot.index[snapshot.root_id]
        content = _render_item_markdown(snapshot, snapshot.root_id)

        module_rows = _module_item_rows(snapshot, snapshot.root_id)
        if module_rows:
            content += "\n\n## Top-Level Items\n"
            for local_key, child in module_rows[:80]:
                kind = normalize_kind(inner_kind(child))
                name = child.get("name") or "(anonymous)"
                content += f"- {kind}: [{name}](docs.rs://{snapshot.crate_name}/{snapshot.version}/{local_key})\n"

        links: list[dict[str, str]] = []
        for local_key, child in module_rows:
            name = child.get("name") or local_key.split("/")[-1]
            abs_key = f"{snapshot.crate_name}/{snapshot.version}/{local_key}"
            links.append(
                {
                    "key": abs_key,
                    "text": str(name),
                    "url": docs_url_for_local_key(snapshot, local_key),
                }
            )

        paginated_content, total_chars = paginate_content(content, offset, limit)

        return {
            "crate": crate_name,
            "version": snapshot.version,
            "version_source": snapshot.version_source,
            "format_version": snapshot.format_version,
            "target_triple": snapshot.target_triple,
            "content": paginated_content,
            "total_characters": total_chars,
            "offset": offset,
            "limit": limit,
            "has_more": (offset + limit) < total_chars,
            "links": links[:20],
            "total_links": len(links),
            "url": docs_url_for_local_key(snapshot, snapshot.crate_name),
            "deprecation": _deprecation_payload(root_item),
        }
    except DataError as exc:
        return {
            "error": exc.code,
            "message": exc.message,
            "context": exc.context,
            "crate": crate_name,
            "version": version,
            "target": target,
            "rustdoc_format": rustdoc_format,
        }
    except Exception as exc:
        return {
            "error": "unexpected_error",
            "message": str(exc),
            "crate": crate_name,
            "version": version,
            "target": target,
            "rustdoc_format": rustdoc_format,
        }


async def _lookup_single_page(
    page_key: str,
    version: str | None,
    target: str | None,
    rustdoc_format: int | None,
) -> tuple[dict[str, Any], str]:
    normalized = normalize_item_to_key(page_key)

    try:
        resolved_abs_key, version_source = resolve_item_key_and_version_source(
            normalized, version
        )
        crate_name, resolved_version, local_key = parse_absolute_item_key(resolved_abs_key)
        snapshot = await get_rustdoc_snapshot(
            crate_name, resolved_version, target, rustdoc_format
        )
        item_id = _item_id_from_local_key(snapshot, local_key)
        item = snapshot.index[item_id]
        links = extract_item_links(snapshot, item)
        content = _render_item_markdown(snapshot, item_id)

        span = item.get("span") or {}
        source_available = isinstance(span, dict) and isinstance(span.get("filename"), str)
        source_url = (
            f"{BASE_URL}/crate/{snapshot.crate_name}/{snapshot.version}/source/{span.get('filename')}"
            if source_available
            else None
        )

        result = {
            "key": f"{snapshot.crate_name}/{snapshot.version}/{local_key}",
            "url": docs_url_for_local_key(snapshot, local_key),
            "content_length": len(content),
            "links_count": len(links),
            "source_available": source_available,
            "source_url": source_url,
            "format_version": snapshot.format_version,
            "target_triple": snapshot.target_triple,
            "version_source": version_source,
            "deprecation": _deprecation_payload(item),
        }
        return result, f"\n\n# Page: {result['key']}\n\n{content}"

    except DataError as exc:
        return (
            {
                "key": normalized,
                "error": exc.code,
                "message": exc.message,
                "context": exc.context,
            },
            "",
        )
    except Exception as exc:
        return (
            {
                "key": normalized,
                "error": "unexpected_error",
                "message": str(exc),
            },
            "",
        )


@mcp.tool()
async def lookup_pages(
    pages: list[str],
    version: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    target: str | None = None,
    rustdoc_format: int | None = None,
) -> dict[str, Any]:
    """Look up one or more pages from rustdoc JSON item graph."""
    semaphore = asyncio.Semaphore(LOOKUP_CONCURRENCY)

    async def with_limit(page: str) -> tuple[dict[str, Any], str]:
        async with semaphore:
            return await _lookup_single_page(page, version, target, rustdoc_format)

    payloads = await asyncio.gather(*(with_limit(page) for page in pages))
    results = [result for result, _ in payloads]
    content_blocks = [content for _, content in payloads if content]

    full_content = "".join(content_blocks)
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
async def lookup_item_signature(
    item: str,
    version: str | None = None,
    include_members: bool = False,
    member_limit: int = 20,
    target: str | None = None,
    rustdoc_format: int | None = None,
) -> dict[str, Any]:
    """Fetch a compact structured signature for a rustdoc item."""
    try:
        normalized_item = normalize_item_to_key(item)
        resolved_key, version_source = resolve_item_key_and_version_source(
            normalized_item, version
        )
        crate_name, resolved_version, local_key = parse_absolute_item_key(resolved_key)
        snapshot = await get_rustdoc_snapshot(
            crate_name, resolved_version, target, rustdoc_format
        )

        item_id = _item_id_from_local_key(snapshot, local_key)
        rustdoc_item = snapshot.index[item_id]
        kind = normalize_kind(inner_kind(rustdoc_item))
        name = rustdoc_item.get("name") or local_key.split("/")[-1]

        signature, where_clause, generics = build_item_signature(rustdoc_item)
        if signature is None:
            raise DataError(
                "rustdoc_format_unsupported",
                "Unable to build signature for requested item kind",
                context={"item_kind": kind, "item_key": local_key},
            )

        bounded_member_limit = max(1, min(member_limit, 100))
        members = []
        if include_members:
            members = await _collect_trait_or_impl_members(
                snapshot, rustdoc_item, bounded_member_limit
            )

        return {
            "item_key": f"{snapshot.crate_name}/{snapshot.version}/{local_key}",
            "resolved_url": docs_url_for_local_key(snapshot, local_key),
            "kind": kind,
            "name": name,
            "signature": signature,
            "where_clause": where_clause,
            "generics": generics,
            "members": members,
            "version_source": version_source,
            "format_version": snapshot.format_version,
            "target_triple": snapshot.target_triple,
            "deprecation": _deprecation_payload(rustdoc_item),
        }

    except DataError as exc:
        normalized = normalize_item_to_key(item)
        inferred_kind = normalize_kind(parse_key_metadata(normalized).get("path", "unknown"))
        return {
            "item_key": normalized,
            "resolved_url": None,
            "kind": inferred_kind,
            "name": normalized.split("/")[-1] if normalized else "",
            "signature": None,
            "where_clause": None,
            "generics": None,
            "members": [],
            "version_source": "unknown",
            "error": exc.code,
            "message": exc.message,
            "context": exc.context,
        }
    except Exception as exc:
        normalized = normalize_item_to_key(item)
        return {
            "item_key": normalized,
            "resolved_url": None,
            "kind": "unknown",
            "name": normalized.split("/")[-1] if normalized else "",
            "signature": None,
            "where_clause": None,
            "generics": None,
            "members": [],
            "version_source": "unknown",
            "error": "unexpected_error",
            "message": str(exc),
        }


@mcp.tool()
async def search_docs(
    crate_name: str,
    query: str,
    version: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    target: str | None = None,
    rustdoc_format: int | None = None,
) -> dict[str, Any]:
    """Search crate docs using in-memory rustdoc JSON index."""
    try:
        snapshot = await get_rustdoc_snapshot(crate_name, version, target, rustdoc_format)
        query_lower = query.strip().lower()

        scored: list[tuple[int, str, dict[str, Any]]] = []
        for local_key, item_id in snapshot.local_key_to_id.items():
            item = snapshot.index.get(item_id)
            if not isinstance(item, dict):
                continue
            if not _is_public_item(item):
                continue

            name = (item.get("name") or "").lower()
            path_text = local_key.replace("/", "::").lower()
            docs_text = (item.get("docs") or "").lower()
            score = _score_search_result(query_lower, name, path_text, docs_text)
            if score <= 0:
                continue

            scored.append((score, local_key, item))

        scored.sort(
            key=lambda row: (
                -row[0],
                len(row[1]),
                row[1],
            )
        )

        results = []
        for score, local_key, item in scored[offset : offset + limit]:
            kind = normalize_kind(inner_kind(item))
            name = item.get("name") or local_key.split("/")[-1]
            abs_key = f"{snapshot.crate_name}/{snapshot.version}/{local_key}"
            results.append(
                {
                    "key": abs_key,
                    "title": f"{kind.capitalize()} {name}",
                    "url": docs_url_for_local_key(snapshot, local_key),
                    "snippet": _make_snippet(item.get("docs") or "", query),
                    "score": score,
                    "deprecation": _deprecation_payload(item),
                }
            )

        total_results = len(scored)
        return {
            "crate": crate_name,
            "version": snapshot.version,
            "version_source": snapshot.version_source,
            "query": query,
            "results": results,
            "total_results": total_results,
            "offset": offset,
            "limit": limit,
            "has_more": (offset + limit) < total_results,
            "search_url": build_rustdoc_json_url(
                snapshot.crate_name,
                snapshot.version,
                target,
                rustdoc_format,
            ),
            "format_version": snapshot.format_version,
            "target_triple": snapshot.target_triple,
        }

    except DataError as exc:
        return {
            "error": exc.code,
            "message": exc.message,
            "context": exc.context,
            "crate": crate_name,
            "version": version,
            "query": query,
        }
    except Exception as exc:
        return {
            "error": "unexpected_error",
            "message": str(exc),
            "crate": crate_name,
            "version": version,
            "query": query,
        }


@mcp.tool()
async def search_crates(
    query: str,
    page: int = 1,
) -> dict[str, Any]:
    """Search crates by name using crates.io JSON API."""
    try:
        payload = await fetch_crates_io_search(query, page, per_page=30)
        crates = []

        for crate in payload.get("crates", []):
            crate_name = crate.get("name")
            version = (
                crate.get("max_stable_version")
                or crate.get("newest_version")
                or crate.get("max_version")
                or crate.get("default_version")
                or "unknown"
            )
            crates.append(
                {
                    "name": crate_name,
                    "version": version,
                    "description": crate.get("description") or "",
                    "date": crate.get("updated_at") or crate.get("created_at") or "",
                    "url": crate.get("documentation")
                    or f"{BASE_URL}/{crate_name}/{DEFAULT_VERSION}/{crate_name}/",
                }
            )

        meta = payload.get("meta") or {}
        total = meta.get("total")
        per_page = meta.get("per_page") or 30
        has_next_page = False
        if isinstance(total, int):
            has_next_page = (page * per_page) < total
        else:
            has_next_page = len(crates) >= per_page

        return {
            "query": query,
            "page": page,
            "crates": crates,
            "total_on_page": len(crates),
            "has_next_page": has_next_page,
            "search_url": f"{CRATES_IO_BASE_URL}/crates?q={quote(query)}&page={page}&per_page=30",
        }

    except DataError as exc:
        return {
            "error": exc.code,
            "message": exc.message,
            "context": exc.context,
            "query": query,
            "page": page,
        }
    except Exception as exc:
        return {
            "error": "unexpected_error",
            "message": str(exc),
            "query": query,
            "page": page,
        }


@mcp.tool()
async def get_source_code(
    page_key: str,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    target: str | None = None,
    rustdoc_format: int | None = None,
) -> dict[str, Any]:
    """Get item source code via rustdoc span + crates.io crate archive."""
    normalized = normalize_item_to_key(page_key)
    try:
        crate_name, version, local_key = parse_absolute_item_key(normalized)
        snapshot = await get_rustdoc_snapshot(
            crate_name, version, target, rustdoc_format
        )
        item_id = _item_id_from_local_key(snapshot, local_key)
        item = snapshot.index[item_id]

        span = item.get("span") or {}
        filename = span.get("filename")
        if not isinstance(filename, str):
            return {
                "key": f"{snapshot.crate_name}/{snapshot.version}/{local_key}",
                "error": "source_file_not_found",
                "message": "Item has no span filename in rustdoc JSON",
            }

        crate_bytes = await fetch_crate_download_bytes(snapshot.crate_name, snapshot.version)
        source_code = read_source_file_from_archive(crate_bytes, filename)

        total_lines = source_code.count("\n") + 1
        paginated_content, total_chars = paginate_content(source_code, offset, limit)

        return {
            "key": f"{snapshot.crate_name}/{snapshot.version}/{local_key}",
            "content": paginated_content,
            "total_characters": total_chars,
            "offset": offset,
            "limit": limit,
            "has_more": (offset + limit) < total_chars,
            "total_lines": total_lines,
            "language": "rust",
            "source_url": f"{BASE_URL}/crate/{snapshot.crate_name}/{snapshot.version}/source/{filename}",
            "span": span,
            "format_version": snapshot.format_version,
            "target_triple": snapshot.target_triple,
        }

    except DataError as exc:
        return {
            "key": normalized,
            "error": exc.code,
            "message": exc.message,
            "context": exc.context,
        }
    except Exception as exc:
        return {
            "key": normalized,
            "error": "unexpected_error",
            "message": str(exc),
        }


def _iter_module_subtree_ids(
    snapshot: RustdocSnapshot,
    start_id: str,
    max_items: int = 5000,
) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    stack = [str(start_id)]

    while stack and len(ordered) < max_items:
        current_id = stack.pop()
        if current_id in seen:
            continue
        seen.add(current_id)
        ordered.append(current_id)

        item = snapshot.index.get(current_id)
        if not isinstance(item, dict):
            continue
        if inner_kind(item) != "module":
            continue

        module_data = item.get("inner", {}).get("module", {})
        child_ids = module_data.get("items", []) if isinstance(module_data, dict) else []
        for child_id in reversed(child_ids):
            stack.append(str(child_id))

    return ordered


def _extract_fenced_code_blocks(markdown: str) -> list[tuple[str, str]]:
    pattern = re.compile(r"```([A-Za-z0-9_+\-]*)\n(.*?)\n```", re.DOTALL)
    blocks: list[tuple[str, str]] = []
    for match in pattern.finditer(markdown or ""):
        lang = (match.group(1) or "text").strip().lower() or "text"
        code = match.group(2)
        blocks.append((lang, code))
    return blocks


@mcp.tool()
async def extract_code_examples(
    crate_name: str,
    module_path: str | None = None,
    filter_text: str | None = None,
    only_complete: bool = False,
    version: str | None = None,
    target: str | None = None,
    rustdoc_format: int | None = None,
) -> dict[str, Any]:
    """Extract code examples from rustdoc markdown docs across module subtree."""
    try:
        snapshot = await get_rustdoc_snapshot(crate_name, version, target, rustdoc_format)

        if module_path:
            start_local_key = normalize_local_key(f"{crate_name}/{module_path}")
        else:
            start_local_key = crate_name

        start_id = _item_id_from_local_key(snapshot, start_local_key)
        subtree_ids = _iter_module_subtree_ids(snapshot, start_id)

        examples: list[dict[str, Any]] = []
        filter_lc = filter_text.lower() if filter_text else None

        for item_id in subtree_ids:
            item = snapshot.index.get(item_id)
            if not isinstance(item, dict):
                continue

            docs = item.get("docs") or ""
            if not docs:
                continue

            source_page = snapshot.id_to_local_key.get(item_id)
            if not source_page:
                continue
            abs_source_page = f"{snapshot.crate_name}/{snapshot.version}/{source_page}"

            for language, code in _extract_fenced_code_blocks(docs):
                if filter_lc and filter_lc not in code.lower():
                    continue

                is_complete = False
                if language in {"rust", "rs"}:
                    is_complete = (
                        "fn main(" in code
                        or "#[test]" in code
                        or "#[tokio::main]" in code
                        or (
                            code.count("{") >= 1
                            and code.count("{") == code.count("}")
                            and "fn " in code
                        )
                    )
                if only_complete and language in {"rust", "rs"} and not is_complete:
                    continue

                examples.append(
                    {
                        "source_page": abs_source_page,
                        "code": code,
                        "language": language,
                        "context": _display_title_for_item(item),
                        "is_complete": is_complete if language in {"rust", "rs"} else None,
                    }
                )

                if len(examples) >= 200:
                    break
            if len(examples) >= 200:
                break

        return {
            "crate": crate_name,
            "version": snapshot.version,
            "version_source": snapshot.version_source,
            "format_version": snapshot.format_version,
            "target_triple": snapshot.target_triple,
            "search_pattern": filter_text,
            "examples": examples[:50],
            "total_found": len(examples),
            "debug_info": {
                "page_searched": start_local_key,
                "parsing_note": "Examples are extracted from fenced code blocks in rustdoc markdown docs.",
                "suggestion": "If empty, the crate may not include inline code examples in rustdoc comments.",
            }
            if not examples
            else None,
            "fallback_raw_examples": None,
        }

    except DataError as exc:
        return {
            "crate": crate_name,
            "error": exc.code,
            "message": exc.message,
            "context": exc.context,
        }
    except Exception as exc:
        return {
            "crate": crate_name,
            "error": "unexpected_error",
            "message": str(exc),
        }


def _resolve_trait_local_key(crate_name: str, trait_path: str) -> str:
    normalized = normalize_crate_path(trait_path)
    if normalized.startswith(f"{crate_name}/"):
        return normalized
    return normalize_local_key(f"{crate_name}/{normalized}")


def _type_target_from_impl(snapshot: RustdocSnapshot, impl_data: dict[str, Any]) -> tuple[str | None, str]:
    target = impl_data.get("for")
    name = render_type(target)

    if not isinstance(target, dict):
        return None, name

    resolved = target.get("resolved_path")
    if isinstance(resolved, dict):
        target_id = str(resolved.get("id")) if resolved.get("id") is not None else None
        if target_id and target_id in snapshot.id_to_local_key:
            return snapshot.id_to_local_key[target_id], name

    return None, name


@mcp.tool()
async def find_trait_implementors(
    crate_name: str,
    trait_path: str,
    version: str | None = None,
    target: str | None = None,
    rustdoc_format: int | None = None,
) -> dict[str, Any]:
    """Find implementors for a trait using rustdoc JSON trait implementation ids."""
    try:
        snapshot = await get_rustdoc_snapshot(crate_name, version, target, rustdoc_format)
        trait_local_key = _resolve_trait_local_key(crate_name, trait_path)
        trait_item_id = _item_id_from_local_key(snapshot, trait_local_key)
        trait_item = snapshot.index[trait_item_id]

        if inner_kind(trait_item) != "trait":
            raise DataError(
                "rustdoc_item_not_found",
                "Requested item is not a trait",
                context={"trait_path": trait_path, "resolved_key": trait_local_key},
            )

        trait_data = trait_item.get("inner", {}).get("trait", {})
        impl_ids = [str(value) for value in (trait_data.get("implementations") or [])]

        implementors: list[dict[str, Any]] = []
        seen: set[tuple[str | None, str]] = set()
        blanket_count = 0

        for impl_id in impl_ids:
            impl_item = snapshot.index.get(impl_id)
            if not isinstance(impl_item, dict):
                continue
            impl_data = impl_item.get("inner", {}).get("impl", {})
            if not isinstance(impl_data, dict):
                continue

            if impl_data.get("blanket_impl") is not None:
                blanket_count += 1

            local_key, type_name = _type_target_from_impl(snapshot, impl_data)
            key_tuple = (local_key, type_name)
            if key_tuple in seen:
                continue
            seen.add(key_tuple)

            if local_key:
                abs_key = f"{snapshot.crate_name}/{snapshot.version}/{local_key}"
                module = "/".join(local_key.split("/")[1:-1]) or "root"
            else:
                abs_key = None
                module = "external"

            implementors.append(
                {
                    "name": type_name,
                    "key": abs_key,
                    "module": module,
                    "impl_id": int(impl_id) if impl_id.isdigit() else impl_id,
                }
            )

        direct_count = max(0, len(implementors) - blanket_count)

        return {
            "crate": crate_name,
            "version": snapshot.version,
            "version_source": snapshot.version_source,
            "trait_path": trait_path,
            "trait_url": docs_url_for_local_key(snapshot, trait_local_key),
            "implementors": implementors,
            "total_implementors": len(implementors),
            "direct_implementors": direct_count,
            "blanket_implementors": blanket_count,
            "debug_info": None if implementors else {
                "parsing_note": "No trait implementations were found in rustdoc JSON for this trait.",
            },
            "fallback_impl_mentions": None,
            "format_version": snapshot.format_version,
            "target_triple": snapshot.target_triple,
            "deprecation": _deprecation_payload(trait_item),
        }

    except DataError as exc:
        return {
            "crate": crate_name,
            "trait_path": trait_path,
            "error": exc.code,
            "message": exc.message,
            "context": exc.context,
        }
    except Exception as exc:
        return {
            "crate": crate_name,
            "trait_path": trait_path,
            "error": "unexpected_error",
            "message": str(exc),
        }


@mcp.tool()
async def lookup_feature_flags(
    crate_name: str,
    version: str | None = None,
) -> dict[str, Any]:
    """Get structured feature-flag information for a crate via crates.io JSON."""
    result = await fetch_feature_flag_data(crate_name, version)
    return result


@mcp.tool()
async def list_project_crates(
    include_transitive: bool = True,
    include_workspace_members: bool = True,
    include_dev: bool = True,
) -> dict[str, Any]:
    """List project crates with direct/transitive classification."""
    if _project_dir is None:
        return {
            "error": "Project directory is not configured. Start server with --project-dir.",
            "project_dir": None,
            "cargo_lock_loaded": False,
            "direct_crates": [],
            "transitive_crates": [],
            "unresolved_direct": [],
            "counts": {},
        }

    manifests = discover_project_manifests(_project_dir, include_workspace_members)
    if not manifests:
        return {
            "error": f"No Cargo.toml found under project directory: {_project_dir}",
            "project_dir": str(_project_dir),
            "cargo_lock_loaded": bool(_cargo_lock_versions),
            "direct_crates": [],
            "transitive_crates": [],
            "unresolved_direct": [],
            "counts": {},
        }

    direct_entries: list[dict[str, Any]] = []
    manifest_errors: list[str] = []
    for manifest_path in manifests:
        try:
            with open(manifest_path, "rb") as f:
                manifest_data = tomllib.load(f)
            direct_entries.extend(
                _collect_direct_dependencies_from_manifest(
                    manifest_data, manifest_path, _project_dir, include_dev
                )
            )
        except Exception as exc:
            manifest_errors.append(f"{manifest_path}: {exc}")

    direct_map: dict[str, dict[str, Any]] = {}
    unresolved_direct: list[str] = []

    for entry in direct_entries:
        crate_name = entry["name"]
        crate_info = direct_map.setdefault(
            crate_name,
            {
                "name": crate_name,
                "versions": [],
                "selected_version": None,
                "dependency_kinds": set(),
                "declared_in": set(),
                "source": None,
            },
        )
        crate_info["dependency_kinds"].add(entry["kind"])
        crate_info["declared_in"].add(entry["declared_in"])
        if crate_info["source"] is None and entry["source"] is not None:
            crate_info["source"] = entry["source"]

    for crate_name, crate_info in direct_map.items():
        versions = _cargo_lock_versions.get(crate_name, [])
        crate_info["versions"] = versions
        crate_info["selected_version"] = select_preferred_version(versions)
        if crate_info["source"] is None:
            crate_info["source"] = _source_for_crate_version(
                crate_name, crate_info["selected_version"]
            )
        if not versions:
            unresolved_direct.append(crate_name)

    direct_crates = [
        {
            "name": item["name"],
            "versions": item["versions"],
            "selected_version": item["selected_version"],
            "dependency_kinds": sorted(item["dependency_kinds"]),
            "declared_in": sorted(item["declared_in"]),
            "source": item["source"],
        }
        for item in sorted(direct_map.values(), key=lambda value: value["name"])
    ]

    transitive_crates: list[dict[str, Any]] = []
    if include_transitive:
        direct_names = {item["name"] for item in direct_crates}
        for crate_name in sorted(_cargo_lock_versions):
            if crate_name in direct_names:
                continue
            versions = _cargo_lock_versions[crate_name]
            selected_version = select_preferred_version(versions)
            transitive_crates.append(
                {
                    "name": crate_name,
                    "versions": versions,
                    "selected_version": selected_version,
                    "dependency_kinds": ["transitive"],
                    "declared_in": [],
                    "source": _source_for_crate_version(crate_name, selected_version),
                }
            )

    result: dict[str, Any] = {
        "project_dir": str(_project_dir),
        "cargo_lock_loaded": bool(_cargo_lock_versions),
        "direct_crates": direct_crates,
        "transitive_crates": transitive_crates if include_transitive else [],
        "unresolved_direct": sorted(set(unresolved_direct)),
        "counts": {
            "direct": len(direct_crates),
            "transitive": len(transitive_crates) if include_transitive else 0,
            "unresolved_direct": len(set(unresolved_direct)),
            "lockfile_crates": len(_cargo_lock_versions),
            "manifests_scanned": len(manifests),
        },
    }

    if manifest_errors:
        result["debug_info"] = {"manifest_parse_errors": manifest_errors}

    return result


@mcp.tool()
async def analyze_dependencies(
    crate_name: str,
    version: str | None = None,
) -> dict[str, Any]:
    """Get crate dependencies and features via crates.io APIs."""
    try:
        resolved_version, version_source, _, version_record = await resolve_crates_io_version(
            crate_name, version
        )

        deps_payload = await fetch_crates_io_dependencies(crate_name, resolved_version)
        dependencies = deps_payload.get("dependencies", [])

        direct: list[dict[str, Any]] = []
        dev: list[dict[str, Any]] = []
        build: list[dict[str, Any]] = []

        for dep in dependencies:
            dep_entry = {
                "name": dep.get("crate_id"),
                "version_req": dep.get("req"),
                "optional": bool(dep.get("optional")),
                "target": dep.get("target"),
                "url": f"{BASE_URL}/{dep.get('crate_id')}/{DEFAULT_VERSION}/{dep.get('crate_id')}/",
                "default_features": bool(dep.get("default_features")),
                "features": dep.get("features") or [],
            }

            kind = dep.get("kind")
            if kind == "dev":
                dev.append(dep_entry)
            elif kind == "build":
                build.append(dep_entry)
            else:
                direct.append(dep_entry)

        features_map = (version_record or {}).get("features") or {}
        if not isinstance(features_map, dict):
            features_map = {}

        features, default_features, optional_dependencies = parse_feature_flags_map(features_map)

        return {
            "crate": crate_name,
            "version": resolved_version,
            "version_source": version_source,
            "dependencies": {
                "direct": direct,
                "dev": dev,
                "build": build,
                "features": features,
                "default_features": default_features,
                "optional_dependencies": optional_dependencies,
                "total": len(direct) + len(dev) + len(build),
            },
            "source_url": f"{CRATES_IO_BASE_URL}/crates/{quote(crate_name)}/{quote(resolved_version)}/dependencies",
            "debug_info": None
            if (direct or dev or build or features)
            else {
                "parsing_note": "No dependency/feature metadata returned by crates.io for this version."
            },
        }

    except DataError as exc:
        return {
            "crate": crate_name,
            "version": version,
            "error": exc.code,
            "message": exc.message,
            "context": exc.context,
        }
    except Exception as exc:
        return {
            "crate": crate_name,
            "version": version,
            "error": "unexpected_error",
            "message": str(exc),
        }


def _module_items_by_kind(snapshot: RustdocSnapshot, module_item_id: str) -> dict[str, list[str]]:
    rows = _module_item_rows(snapshot, module_item_id)
    buckets: dict[str, list[str]] = {
        "structs": [],
        "enums": [],
        "traits": [],
        "functions": [],
        "types": [],
        "macros": [],
        "constants": [],
    }

    for _, child in rows:
        child_kind = inner_kind(child)
        name = child.get("name")
        if not name:
            continue
        if child_kind == "struct":
            buckets["structs"].append(name)
        elif child_kind == "enum":
            buckets["enums"].append(name)
        elif child_kind == "trait":
            buckets["traits"].append(name)
        elif child_kind == "function":
            buckets["functions"].append(name)
        elif child_kind == "type_alias":
            buckets["types"].append(name)
        elif child_kind == "macro":
            buckets["macros"].append(name)
        elif child_kind in {"constant", "static"}:
            buckets["constants"].append(name)

    for key in buckets:
        buckets[key].sort()

    return buckets


@mcp.tool()
async def get_module_hierarchy(
    crate_name: str,
    start_module: str | None = None,
    max_depth: int = 3,
    version: str | None = None,
    target: str | None = None,
    rustdoc_format: int | None = None,
) -> dict[str, Any]:
    """Get module hierarchy from rustdoc JSON module graph."""
    try:
        snapshot = await get_rustdoc_snapshot(crate_name, version, target, rustdoc_format)
        if start_module:
            start_key = normalize_local_key(f"{crate_name}/{start_module}")
        else:
            start_key = crate_name

        start_id = _item_id_from_local_key(snapshot, start_key)

        def explore(module_item_id: str, depth: int) -> dict[str, Any] | None:
            if depth > max_depth:
                return None

            item = snapshot.index.get(module_item_id)
            if not isinstance(item, dict):
                return None
            if inner_kind(item) != "module":
                return None

            local_key = snapshot.id_to_local_key.get(module_item_id, crate_name)
            module_name = item.get("name") or local_key.split("/")[-1]

            module_info = {
                "name": module_name,
                "path": local_key,
                "key": f"{snapshot.crate_name}/{snapshot.version}/{local_key}",
                "submodules": [],
                "items": _module_items_by_kind(snapshot, module_item_id),
            }

            if depth >= max_depth:
                return module_info

            module_data = item.get("inner", {}).get("module", {})
            for child_id in module_data.get("items", []):
                child = snapshot.index.get(str(child_id))
                if not isinstance(child, dict):
                    continue
                if inner_kind(child) != "module":
                    continue
                if not _is_public_item(child):
                    continue
                child_info = explore(str(child_id), depth + 1)
                if child_info:
                    module_info["submodules"].append(child_info)

            module_info["submodules"].sort(key=lambda m: m["name"])
            return module_info

        root_module = explore(start_id, 0)
        if not root_module:
            return {
                "crate": crate_name,
                "version": version,
                "error": "rustdoc_item_not_found",
                "message": "Failed to fetch module hierarchy",
            }

        def count_modules(module: dict[str, Any]) -> int:
            count = 1
            for submodule in module.get("submodules", []):
                count += count_modules(submodule)
            return count

        total_modules = count_modules(root_module)

        return {
            "crate": crate_name,
            "version": snapshot.version,
            "version_source": snapshot.version_source,
            "start_module": start_module or "root",
            "modules": root_module,
            "total_modules": total_modules,
            "max_depth": max_depth,
            "format_version": snapshot.format_version,
            "target_triple": snapshot.target_triple,
        }

    except DataError as exc:
        return {
            "crate": crate_name,
            "version": version,
            "error": exc.code,
            "message": exc.message,
            "context": exc.context,
        }
    except Exception as exc:
        return {
            "crate": crate_name,
            "version": version,
            "error": "unexpected_error",
            "message": str(exc),
        }


def _public_api_keys(snapshot: RustdocSnapshot) -> set[str]:
    keys: set[str] = set()
    for local_key, item_id in snapshot.local_key_to_id.items():
        item = snapshot.index.get(item_id)
        if not isinstance(item, dict):
            continue
        if not _is_public_item(item):
            continue
        keys.add(local_key)
    return keys


def _render_full_item_content(snapshot: RustdocSnapshot, local_key: str) -> str | None:
    try:
        item_id = _item_id_from_local_key(snapshot, local_key)
    except DataError:
        return None
    return _render_item_markdown(snapshot, item_id)


@mcp.tool()
async def compare_versions(
    crate_name: str,
    version1: str,
    version2: str,
    page_path: str | None = None,
    comparison_type: str = "api_surface",
    target: str | None = None,
    rustdoc_format: int | None = None,
) -> dict[str, Any]:
    """Compare crate versions using rustdoc JSON snapshots."""
    try:
        if comparison_type not in ["api_surface", "full_content"]:
            return {
                "crate": crate_name,
                "version1": version1,
                "version2": version2,
                "error": "invalid_comparison_type",
                "message": "comparison_type must be 'api_surface' or 'full_content'",
            }

        snapshot1 = await get_rustdoc_snapshot(crate_name, version1, target, rustdoc_format)
        snapshot2 = await get_rustdoc_snapshot(crate_name, version2, target, rustdoc_format)

        if comparison_type == "api_surface":
            keys1 = _public_api_keys(snapshot1)
            keys2 = _public_api_keys(snapshot2)

            added = sorted(keys2 - keys1)
            removed = sorted(keys1 - keys2)
            common = sorted(keys1 & keys2)

            differences = {
                "added": {"items": added},
                "removed": {"items": removed},
                "common": {"items": common},
                "summary": (
                    "No API changes detected between versions"
                    if not added and not removed
                    else f"{len(added)} items added, {len(removed)} items removed"
                ),
            }

            return {
                "crate": crate_name,
                "version1": snapshot1.version,
                "version2": snapshot2.version,
                "comparison_type": comparison_type,
                "differences": differences,
                "format_version1": snapshot1.format_version,
                "format_version2": snapshot2.format_version,
                "target_triple1": snapshot1.target_triple,
                "target_triple2": snapshot2.target_triple,
            }

        if not page_path:
            return {
                "crate": crate_name,
                "version1": version1,
                "version2": version2,
                "error": "missing_page_path",
                "message": "page_path is required for full_content comparison",
            }

        local_path = normalize_local_key(
            f"{crate_name}/{normalize_crate_path(page_path)}"
            if not normalize_crate_path(page_path).startswith(f"{crate_name}/")
            else normalize_crate_path(page_path)
        )

        content1 = _render_full_item_content(snapshot1, local_path)
        content2 = _render_full_item_content(snapshot2, local_path)

        if content1 is None and content2 is None:
            return {
                "crate": crate_name,
                "version1": snapshot1.version,
                "version2": snapshot2.version,
                "page_path": page_path,
                "error": "rustdoc_item_not_found",
                "message": "Requested page_path not found in either version",
            }

        differences = {
            "content1": content1 or "(Not found in this version)",
            "content2": content2 or "(Not found in this version)",
            "length_change": (len(content2) if content2 else 0)
            - (len(content1) if content1 else 0),
        }

        return {
            "crate": crate_name,
            "version1": snapshot1.version,
            "version2": snapshot2.version,
            "page_path": page_path,
            "comparison_type": comparison_type,
            "differences": differences,
            "format_version1": snapshot1.format_version,
            "format_version2": snapshot2.format_version,
            "target_triple1": snapshot1.target_triple,
            "target_triple2": snapshot2.target_triple,
        }

    except DataError as exc:
        return {
            "crate": crate_name,
            "version1": version1,
            "version2": version2,
            "error": exc.code,
            "message": exc.message,
            "context": exc.context,
        }
    except Exception as exc:
        return {
            "crate": crate_name,
            "version1": version1,
            "version2": version2,
            "error": "unexpected_error",
            "message": str(exc),
        }


def cleanup() -> None:
    """Cleanup function to be called on exit."""
    try:
        asyncio.run(close_http_client())
    except RuntimeError:
        pass
    logger.info("Rust docs MCP server shutting down gracefully")


# Register cleanup handler
atexit.register(cleanup)


def main() -> None:
    """Initialize and run the FastMCP server."""
    global _cargo_lock_versions, _cargo_lock_packages, _project_dir

    parser = argparse.ArgumentParser(
        prog="jons-mcp-docs-rs",
        description="MCP server for Rust documentation from docs.rs",
    )
    parser.add_argument(
        "--project-dir",
        type=str,
        default=None,
        help="Path to project directory containing Cargo.lock",
    )
    args = parser.parse_args()

    _project_dir = Path(args.project_dir).resolve() if args.project_dir else None

    if _project_dir:
        cargo_lock_path = _project_dir / "Cargo.lock"
        if cargo_lock_path.exists():
            _cargo_lock_versions, _cargo_lock_packages = parse_cargo_lock(cargo_lock_path)
            logger.info(
                f"Loaded {len(_cargo_lock_versions)} crate versions from {cargo_lock_path}"
            )
        else:
            print(f"Warning: Cargo.lock not found at {cargo_lock_path}", file=sys.stderr)

    def signal_handler(sig, frame):
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal_handler)

    try:
        logger.info("Starting Rust docs MCP server...")
        mcp.run()
    except Exception as exc:
        import traceback

        print(f"MCP server error: {exc}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
