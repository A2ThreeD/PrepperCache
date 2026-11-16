#!/usr/bin/env python3
# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Offline portal, content management, and device services.

import argparse
import errno
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import yaml
from collections import deque
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse
from urllib import error, parse, request

KIWIX_MIRROR_ROOT = "https://cdimage.debian.org/mirror/kiwix.org/zim/"
MAP_EXTRACTION_OPERATIONS = {"build", "countries", "regions"}
PROTOMAPS_DAILY_ROOT = "https://build.protomaps.com/"

APPLY_LOG_MAX_BYTES = 5 * 1024 * 1024
APPLY_LOG_KEEP_BYTES = 4 * 1024 * 1024
APPLY_PROGRESS_SCAN_BYTES = 1024 * 1024
CONTENT_UPDATE_INTERVAL = 7 * 86400
CONTENT_RETRY_INTERVAL = 60
CONTENT_RETRY_LIMIT = 3
CONTENT_DOWNLOAD_ACTIONS = {
    "full", "refresh-content", "download-extra-content", "update-content",
    "change-content-set", "downgrade-content-set",
}
ZIM_DOWNLOAD_STEPS = {
    "Downloading selected Kiwix content",
    "Downloading selected extra Kiwix content",
    "Downloading available ZIM updates",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PrepperCache portal API service.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--data-dir", required=True)
    return parser.parse_args()


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value
    return values


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    seen: set[str] = set()
    result: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _ = stripped.split("=", 1)
            if key in updates:
                result.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        result.append(line)

    for key, value in updates.items():
        if key not in seen:
            result.append(f"{key}={value}")

    path.write_text("\n".join(result).rstrip() + "\n")


def read_json(path: Path, default: dict) -> dict:
    candidate = path
    if not candidate.exists():
        if candidate.suffix == ".json":
            alternate = candidate.with_suffix(".yaml")
            candidate = alternate if alternate.exists() else candidate
        elif candidate.suffix in {".yaml", ".yml"}:
            alternate = candidate.with_suffix(".json")
            candidate = alternate if alternate.exists() else candidate
    if not candidate.exists():
        return default
    try:
        loaded = yaml.safe_load(candidate.read_text())
        return loaded if isinstance(loaded, dict) else default
    except yaml.YAMLError:
        return default


def read_last_lines(path: Path, lines: int = 30, *, max_bytes: int = 65536, start_offset: int = 0) -> list[str]:
    if not path.exists():
        return []

    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            file_size = handle.tell()
            remaining = max(0, file_size - start_offset)
            read_size = min(max_bytes, remaining)
            handle.seek(-read_size, os.SEEK_END)
            chunk = handle.read(read_size)
    except OSError:
        return []

    text = chunk.decode("utf-8", errors="replace")
    if read_size < remaining:
        newline_index = text.find("\n")
        text = text[newline_index + 1 :] if newline_index != -1 else text
    return list(deque(text.splitlines(), maxlen=lines))


def read_recent_text(path: Path, *, max_bytes: int) -> str:
    if not path.exists():
        return ""

    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            file_size = handle.tell()
            read_size = min(max_bytes, file_size)
            handle.seek(-read_size, os.SEEK_END)
            chunk = handle.read(read_size)
    except OSError:
        return ""

    text = chunk.decode("utf-8", errors="replace")
    if read_size < file_size:
        newline_index = text.find("\n")
        text = text[newline_index + 1 :] if newline_index != -1 else ""
    return text


def compact_log_file(
    path: Path,
    *,
    max_bytes: int = APPLY_LOG_MAX_BYTES,
    keep_bytes: int = APPLY_LOG_KEEP_BYTES,
) -> bool:
    if keep_bytes <= 0 or max_bytes <= keep_bytes:
        return False
    if not path.exists():
        return False

    try:
        file_size = path.stat().st_size
    except OSError:
        return False

    if file_size <= max_bytes:
        return False

    tail_text = read_recent_text(path, max_bytes=keep_bytes)
    retained_bytes = len(tail_text.encode("utf-8", errors="replace"))
    notice = (
        f"== Log truncated automatically after reaching {format_size_bytes(file_size)}. "
        f"Keeping the most recent {format_size_bytes(retained_bytes)}. ==\n"
    )
    try:
        path.write_text(notice + tail_text.lstrip("\n"))
    except OSError:
        return False
    return True


def inspect_pmtiles_file(path: Path) -> dict[str, object]:
    info: dict[str, object] = {
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "valid": False,
        "error": "File not found",
    }
    if not path.exists() or not path.is_file():
        return info

    try:
        with path.open("rb") as handle:
            header = handle.read(64)
    except OSError as exc:
        info["error"] = str(exc)
        return info

    if header.startswith(b"version https://git-lfs.github.com/spec/"):
        info["error"] = "Git LFS pointer file downloaded instead of PMTiles archive"
        return info

    if header.startswith(b"PMTiles") or header[:2] == b"PM":
        info["valid"] = True
        info["error"] = None
        return info

    info["error"] = "Wrong magic number for PMTiles archive"
    return info


def format_size_bytes(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 ** 3:
        return f"{size_bytes / (1024 ** 2):.1f} MB"
    return f"{size_bytes / (1024 ** 3):.1f} GB"


def preset_to_profile(preset: str) -> str | None:
    if preset == "compact":
        return "essential"
    if preset == "full":
        return "comprehensive"
    if preset == "empty":
        return None
    raise ValueError(f"Unknown content preset: {preset}")


def profile_to_preset(profile: str | None) -> str:
    if profile == "comprehensive":
        return "full"
    if profile in {"essential", "standard", None, ""}:
        return "compact" if profile else "empty"
    return "compact"


class DirectoryIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, object]] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []
        self._link_tag: str | None = None
        self._last_anchor: dict[str, object] | None = None
        self._tail_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"a", "span"}:
            return
        attributes = dict(attrs)
        href = attributes.get("href")
        if href:
            self._link_tag = tag
            self._current_href = href
            self._current_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag != self._link_tag or not self._current_href:
            return
        name = "".join(self._current_text).strip()
        entry = {
            "href": self._current_href,
            "name": name or self._current_href,
            "size_bytes": None,
            "size_label": None,
        }
        self.links.append(entry)
        self._last_anchor = entry
        self._tail_text = []
        self._link_tag = None
        self._current_href = None
        self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._link_tag is not None:
            self._current_text.append(data)
            return
        if not self._last_anchor:
            return
        self._tail_text.append(data)
        size_bytes, size_label = parse_apache_index_tail("".join(self._tail_text))
        if size_label:
            self._last_anchor["size_bytes"] = size_bytes
            self._last_anchor["size_label"] = size_label
            self._last_anchor = None
            self._tail_text = []


def parse_apache_index_tail(text: str) -> tuple[int | None, str | None]:
    cleaned = html.unescape(text).replace("\xa0", " ")
    tokens = cleaned.strip().split()
    if not tokens:
        return None, None
    if len(tokens) >= 2 and re.fullmatch(r"[KMGTP]IB", tokens[-1].upper()):
        label = tokens[-2] + tokens[-1][0].upper()
        size_bytes = parse_size_label_to_bytes(label)
        return size_bytes, label if size_bytes is not None else None
    label = tokens[-1].upper()
    if not re.fullmatch(r"(\d+(?:\.\d+)?[KMGTP]?|-)", label):
        return None, None
    if label == "-":
        return None, "-"
    return parse_size_label_to_bytes(label), label


def parse_size_label_to_bytes(label: str) -> int | None:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGTP]?)", label.strip())
    if not match:
        return None
    value = float(match.group(1))
    suffix = match.group(2)
    multiplier = {
        "": 1,
        "K": 1024,
        "M": 1024 ** 2,
        "G": 1024 ** 3,
        "T": 1024 ** 4,
        "P": 1024 ** 5,
    }[suffix]
    return int(value * multiplier)


class MapBuild(NamedTuple):
    filename: str
    label: str
    bounds: list[float]
    max_zoom: int
    geometry: dict | None = None


class AdminAccessError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class AdminAccess:
    COOKIE = "preppercache_admin"
    SESSION_SECONDS = 8 * 60 * 60
    HASH_ITERATIONS = 600_000
    ATTEMPT_LIMIT = 5
    ATTEMPT_WINDOW = 60

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.sessions: dict[str, float] = {}
        self.failures: deque[float] = deque()
        try:
            data = json.loads(path.read_text())
            salt = bytes.fromhex(data["salt"])
            digest = bytes.fromhex(data["digest"])
            if len(salt) != 32 or len(digest) != 32:
                raise ValueError("Invalid password record.")
            self.password_record = (salt, digest)
        except FileNotFoundError:
            self.password_record = None
        except (OSError, ValueError, KeyError, TypeError):
            # A damaged record must not turn protection off.
            print(f"Could not read the admin password record at {path}. Settings are locked.", flush=True)
            self.password_record = (b"", b"")

    def _authenticated(self, token: str) -> bool:
        return self.password_record is None or self.sessions.get(token, 0) > time.monotonic()

    def status(self, token: str) -> dict:
        with self.lock:
            return {"enabled": self.password_record is not None, "authenticated": self._authenticated(token)}

    def _hash(self, password: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, self.HASH_ITERATIONS)

    def _verify(self, password: object) -> None:
        now = time.monotonic()
        while self.failures and now - self.failures[0] >= self.ATTEMPT_WINDOW:
            self.failures.popleft()
        if len(self.failures) >= self.ATTEMPT_LIMIT:
            raise AdminAccessError(429, "Too many attempts. Wait a minute and try again.")
        salt, expected = self.password_record
        try:
            valid = bool(salt) and isinstance(password, str) and 0 < len(password) <= 128 and hmac.compare_digest(
                self._hash(password, salt), expected
            )
        except UnicodeEncodeError:
            valid = False
        if not valid:
            self.failures.append(now)
            raise AdminAccessError(403, "Incorrect admin password.")
        self.failures.clear()

    def _new_session(self) -> str:
        now = time.monotonic()
        self.sessions = {key: end for key, end in self.sessions.items() if end > now}
        if len(self.sessions) >= 32:
            self.sessions.pop(min(self.sessions, key=self.sessions.get))
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now + self.SESSION_SECONDS
        return token

    def login(self, password: object, token: str) -> str:
        with self.lock:
            if self.password_record is None:
                return ""
            self._verify(password)
            self.sessions.pop(token, None)
            return self._new_session()

    def logout(self, token: str) -> None:
        with self.lock:
            self.sessions.pop(token, None)

    def configure(self, token: str, enabled: object, current_password: object, new_password: object) -> str:
        with self.lock:
            if not self._authenticated(token):
                raise AdminAccessError(401, "Enter the admin password to unlock Settings.")
            if not isinstance(enabled, bool):
                raise AdminAccessError(400, "Choose whether to enable admin password protection.")
            if self.password_record is not None:
                self._verify(current_password)

            if enabled:
                if not isinstance(new_password, str) or not 8 <= len(new_password) <= 128 or not new_password.strip():
                    raise AdminAccessError(400, "Use an admin password between 8 and 128 characters.")
                salt = secrets.token_bytes(32)
                try:
                    digest = self._hash(new_password, salt)
                except UnicodeEncodeError as exc:
                    raise AdminAccessError(400, "Enter a valid admin password.") from exc
                with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as handle:
                    temporary = Path(handle.name)
                    try:
                        json.dump({"salt": salt.hex(), "digest": digest.hex()}, handle)
                        handle.flush()
                        os.fsync(handle.fileno())
                        temporary.replace(self.path)
                    finally:
                        temporary.unlink(missing_ok=True)
                self.password_record = (salt, digest)
            else:
                self.path.unlink(missing_ok=True)
                self.password_record = None
            self.sessions.clear()
            self.failures.clear()
            return self._new_session() if enabled else ""


class PortalState:
    def __init__(self, repo_root: Path, data_dir: Path) -> None:
        self.repo_root = repo_root
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.preppercache_env = Path(os.environ.get("PREPPERCACHE_ENV_FILE") or self.repo_root / "config" / "preppercache.env")
        self.install_profile_env = self.repo_root / "config" / "install-profile.env"
        self.admin_access = AdminAccess(self.data_dir / "admin-password.json")
        self.state_file = self.data_dir / "portal-state.json"
        self.apply_state_file = self.data_dir / "apply-state.json"
        self.apply_log_file = self.data_dir / "apply.log"
        self.content_update_file = self.data_dir / "content-update.json"
        self.map_sync_state_file = self.data_dir / "map-sync-state.json"
        self.map_sync_log_file = self.data_dir / "map-sync.log"
        self.content_catalog_cache_file = self.data_dir / "content-catalog-cache.json"
        self.external_maps_links_file = self.data_dir / "external-maps-links.json"
        self.external_zims_links_file = self.data_dir / "external-zims-links.json"
        self.storage_prepare_lock = threading.Lock()
        self.storage_prepare_job: dict = {}
        self.file_transfer_lock = threading.Lock()
        self.file_transfer: dict = {}
        self.apply_lock = threading.Lock()
        self.zim_upload_lock = threading.Lock()
        self.apply_thread: threading.Thread | None = None
        self.map_sync_lock = threading.Lock()
        self.map_build_cancel = threading.Event()
        self.map_sync_thread: threading.Thread | None = None
        self.content_catalog_lock = threading.Lock()
        self.content_catalog_refresh_thread: threading.Thread | None = None
        self.content_catalog_refresh_error: str | None = None
        self.content_catalog_refresh_attempt = 0.0
        self.write_maps_runtime_config()
        self.recover_interrupted_map_sync()
        self.sync_external_content_links()
        self.recover_interrupted_apply()

    def wikipedia_catalog(self) -> dict:
        catalog = read_json(
            self.repo_root / "catalog" / "wikipedia.yaml",
            {"spec_version": None, "options": []},
        )
        options = catalog.get("options")
        if not isinstance(options, list):
            options = []
        normalized: list[dict[str, object]] = []
        for option in options:
            if not isinstance(option, dict):
                continue
            option_id = str(option.get("id", "")).strip()
            if not option_id:
                continue
            normalized.append(
                {
                    "id": option_id,
                    "name": str(option.get("name", option_id)),
                    "description": str(option.get("description", "")),
                    "size_mb": int(option.get("size_mb", 0) or 0),
                    "size_label": str(option.get("size_label", "")),
                    "url": str(option.get("url", "")),
                    "version": str(option.get("version", "")),
                }
            )
        return {
            "spec_version": catalog.get("spec_version"),
            "options": normalized,
        }

    def kiwix_catalog(self) -> dict:
        return read_json(
            self.repo_root / "catalog" / "kiwix-categories.yaml",
            {"categories": []},
        )

    def kiwix_tier_catalog(self) -> dict[str, dict[str, object]]:
        level_order = {
            "essential": 1,
            "standard": 2,
            "comprehensive": 3,
        }
        document = self.kiwix_catalog()
        categories = document.get("collections") if "collections" in document else document.get("categories", [])
        loadout_key = "loadouts" if "collections" in document else "tiers"
        resource_key = "library_items" if "collections" in document else "resources"
        tier_details: dict[str, dict[str, object]] = {}

        for label, selected_level in level_order.items():
            seen_ids: set[str] = set()
            size_mb = 0
            summary_categories: list[dict[str, object]] = []
            tier_description = ""

            for category in categories:
                category_name = str(category.get("name", "")).strip()
                category_items: list[str] = []
                for tier in category.get(loadout_key, []):
                    tier_slug = str(tier.get("key") if loadout_key == "loadouts" else tier.get("slug", ""))
                    level = 0
                    for level_name, level_value in level_order.items():
                        if tier_slug.endswith(f"-{level_name}"):
                            level = level_value
                            break
                    if level == 0 or level > selected_level:
                        continue
                    if level == selected_level and not tier_description:
                        tier_description = str(tier.get("description", "")).strip()

                    for resource in tier.get(resource_key, []):
                        resource_id = str(resource.get("key") if resource_key == "library_items" else resource.get("id", "")).strip()
                        if not resource_id or resource_id in seen_ids:
                            continue
                        seen_ids.add(resource_id)
                        title = str(resource.get("title", resource_id)).strip()
                        size_value = resource.get("footprint_mb") if resource_key == "library_items" else resource.get("size_mb")
                        try:
                            size_mb += int(size_value or 0)
                        except (TypeError, ValueError):
                            pass
                        if title:
                            category_items.append(title)

                if category_name and category_items:
                    summary_categories.append(
                        {
                            "name": category_name,
                            "items": category_items,
                        }
                    )

            tier_details[label] = {
                "id": label,
                "name": label.capitalize(),
                "description": tier_description,
                "size_mb": size_mb,
                "size_label": format_size_bytes(size_mb * 1024 * 1024),
                "summary": summary_categories,
            }

        return tier_details

    def profile_library_size_mb(self, profile: str) -> int:
        level_order = {
            "essential": 1,
            "standard": 2,
            "comprehensive": 3,
        }
        selected_level = level_order.get(profile, 1)
        document = self.kiwix_catalog()
        categories = document.get("collections") if "collections" in document else document.get("categories", [])
        loadout_key = "loadouts" if "collections" in document else "tiers"
        resource_key = "library_items" if "collections" in document else "resources"
        seen_ids: set[str] = set()
        total_mb = 0

        for category in categories:
            for tier in category.get(loadout_key, []):
                tier_slug = str(tier.get("key") if loadout_key == "loadouts" else tier.get("slug", ""))
                level = 0
                for label, value in level_order.items():
                    if tier_slug.endswith(f"-{label}"):
                        level = value
                        break
                if level == 0 or level > selected_level:
                    continue

                for resource in tier.get(resource_key, []):
                    resource_id = str(resource.get("key") if resource_key == "library_items" else resource.get("id", "")).strip()
                    if not resource_id or resource_id in seen_ids:
                        continue
                    seen_ids.add(resource_id)
                    size_value = resource.get("footprint_mb") if resource_key == "library_items" else resource.get("size_mb")
                    try:
                        total_mb += int(size_value or 0)
                    except (TypeError, ValueError):
                        continue

        return total_mb

    def curated_resources(
        self,
        profile: str,
        wikipedia_choice: str,
    ) -> list[dict[str, object]]:
        level_order = {
            "essential": 1,
            "standard": 2,
            "comprehensive": 3,
        }
        selected_level = level_order.get(profile, 1)
        document = self.kiwix_catalog()
        categories = document.get("collections") if "collections" in document else document.get("categories", [])
        loadout_key = "loadouts" if "collections" in document else "tiers"
        resource_key = "library_items" if "collections" in document else "resources"
        resources: list[dict[str, object]] = []
        seen_ids: set[str] = set()

        for category in categories:
            for tier in category.get(loadout_key, []):
                tier_slug = str(tier.get("key") if loadout_key == "loadouts" else tier.get("slug", ""))
                level = 0
                for label, value in level_order.items():
                    if tier_slug.endswith(f"-{label}"):
                        level = value
                        break
                if level == 0 or level > selected_level:
                    continue

                for resource in tier.get(resource_key, []):
                    resource_id = str(resource.get("key") if resource_key == "library_items" else resource.get("id", "")).strip()
                    resource_url = str(resource.get("download_url") if resource_key == "library_items" else resource.get("url", "")).strip()
                    if not resource_id or not resource_url or resource_id in seen_ids:
                        continue
                    seen_ids.add(resource_id)
                    size_value = resource.get("footprint_mb") if resource_key == "library_items" else resource.get("size_mb")
                    try:
                        size_mb = int(size_value or 0)
                    except (TypeError, ValueError):
                        size_mb = 0
                    resources.append(
                        {
                            "id": resource_id,
                            "url": resource_url,
                            "filename": Path(urlparse(resource_url).path).name,
                            "title": resource.get("title") or resource.get("label") or resource_id,
                            "size_mb": size_mb,
                        }
                    )

        for option in self.wikipedia_catalog().get("options", []):
            if str(option.get("id", "")).strip() != wikipedia_choice:
                continue
            option_url = str(option.get("url", "")).strip()
            if option_url:
                resources.append(
                    {
                        "id": str(option.get("id", "")).strip(),
                        "url": option_url,
                        "filename": Path(urlparse(option_url).path).name,
                        "title": option.get("name") or option.get("label") or "Wikipedia",
                        "size_mb": int(option.get("size_mb", 0) or 0),
                    }
                )
            break

        return resources

    def missing_curated_size_mb(self, profile: str, wikipedia_choice: str) -> int:
        installed_names = {
            str(item.get("name", "")).strip()
            for item in self.list_installed_zims()
            if str(item.get("name", "")).strip()
        }
        total_mb = 0
        for resource in self.curated_resources(profile, wikipedia_choice):
            filename = str(resource.get("filename", "")).strip()
            if filename and filename in installed_names:
                continue
            total_mb += int(resource.get("size_mb", 0) or 0)
        return total_mb

    def missing_tier_size_mb(self, profile: str) -> int:
        installed_names = {
            str(item.get("name", "")).strip()
            for item in self.list_installed_zims()
            if str(item.get("name", "")).strip()
        }
        total_mb = 0
        for resource in self.curated_resources(profile, ""):
            filename = str(resource.get("filename", "")).strip()
            if filename and filename in installed_names:
                continue
            total_mb += int(resource.get("size_mb", 0) or 0)
        return total_mb

    def missing_wikipedia_size_mb(self, wikipedia_choice: str) -> int:
        installed_names = {
            str(item.get("name", "")).strip()
            for item in self.list_installed_zims()
            if str(item.get("name", "")).strip()
        }
        for option in self.wikipedia_catalog().get("options", []):
            if str(option.get("id", "")).strip() != wikipedia_choice:
                continue
            option_url = str(option.get("url", "")).strip()
            filename = Path(urlparse(option_url).path).name if option_url else ""
            if filename and filename in installed_names:
                return 0
            return int(option.get("size_mb", 0) or 0)
        return 0

    def setup_storage_summary(self) -> dict:
        total, used, free = shutil.disk_usage("/")
        volumes = self.storage_volumes()
        env = read_env_file(self.preppercache_env)
        profile = env.get("PREPPERCACHE_ZIM_PROFILE", "essential").strip().lower() or "essential"
        if profile not in {"essential", "standard", "comprehensive"}:
            profile = "essential"
        tier_catalog = self.kiwix_tier_catalog()
        kolibri_installed = Path("/usr/bin/kolibri").exists()
        return {
            "disk": {
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "total_gb": round(total / (1024 ** 3), 1),
                "used_gb": round(used / (1024 ** 3), 1),
                "free_gb": round(free / (1024 ** 3), 1),
            },
            "base_library_mb": self.profile_library_size_mb(profile),
            "content_tiers": {
                tier_id: {
                    **details,
                    "missing_mb": self.missing_tier_size_mb(tier_id),
                }
                for tier_id, details in tier_catalog.items()
            },
            "content_install_dir": str(self.preferred_zim_install_dir()),
            "content_install_destinations": self.install_destinations("zims"),
            "wikipedia_install_dir": str(self.preferred_wikipedia_install_dir()),
            "wikipedia_install_destinations": self.install_destinations("zims"),
            "missing_by_wikipedia": {
                str(option.get("id", "")).strip(): self.missing_wikipedia_size_mb(
                    str(option.get("id", "")).strip(),
                )
                for option in self.wikipedia_catalog().get("options", [])
                if str(option.get("id", "")).strip()
            },
            "zim_profile": profile,
            "kolibri_estimated_mb": 1500,
            "kolibri_installed": kolibri_installed,
            "kolibri_url": f"http://{self.detect_primary_host()}:{env.get('PREPPERCACHE_KOLIBRI_PORT', '8082')}/",
            "zim_mode": env.get("PREPPERCACHE_ZIM_MODE", "full"),
            "warning_free_percent": 10,
            "volumes": volumes,
        }

    def load_state(self) -> dict:
        preppercache = read_env_file(self.preppercache_env)
        profile = read_env_file(self.install_profile_env)
        custom_selection = self.read_custom_zim_selection()
        wikipedia_catalog = self.wikipedia_catalog()
        wikipedia_ids = [
            str(option.get("id", "")).strip()
            for option in wikipedia_catalog["options"]
            if str(option.get("id", "")).strip()
        ]
        wikipedia_option = preppercache.get("PREPPERCACHE_WIKIPEDIA_OPTION", "top-mini")
        if wikipedia_ids and wikipedia_option not in wikipedia_ids:
            wikipedia_option = "top-mini" if "top-mini" in wikipedia_ids else wikipedia_ids[0]
        state = read_json(
            self.state_file,
            {"setup_complete": False, "last_saved_at": None},
        )
        return {
            "setup_complete": bool(state.get("setup_complete", False)),
            "last_saved_at": state.get("last_saved_at"),
            "setup_options": {
                "wikipedia": wikipedia_catalog,
                "zim_mirrors": self.zim_mirrors(),
            },
            "profile": {
                "install_kolibri": profile.get("INSTALL_KOLIBRI", "0") == "1",
                "install_ka_lite": profile.get("INSTALL_KA_LITE", "0") == "1",
                "wikipedia_option": wikipedia_option,
                "zim_mirror": self.zim_catalog_root(),
                "ap_enabled": preppercache.get("PREPPERCACHE_AP_ENABLED", "0") == "1",
                "zim_mode": preppercache.get("PREPPERCACHE_ZIM_MODE", "full"),
                "zim_profile": preppercache.get("PREPPERCACHE_ZIM_PROFILE", "essential"),
                "custom_zim_count": len(custom_selection.get("selected_items", [])),
            },
            "storage": self.setup_storage_summary(),
        }

    def maps_env(self) -> dict[str, str]:
        return read_env_file(self.preppercache_env)

    def kiwix_library_dir(self) -> Path:
        env = self.maps_env()
        return Path(env.get("KIWIX_LIBRARY_DIR", "/library/zims/content"))

    def preferred_zim_install_dir(self) -> Path:
        env = self.maps_env()
        configured = env.get("PREPPERCACHE_ZIM_INSTALL_DIR", "").strip()
        return Path(configured) if configured else self.kiwix_library_dir()

    def preferred_wikipedia_install_dir(self) -> Path:
        env = self.maps_env()
        configured = env.get("PREPPERCACHE_WIKIPEDIA_INSTALL_DIR", "").strip()
        if configured:
            return Path(configured)
        return self.preferred_zim_install_dir()

    def preferred_map_install_dir(self) -> Path:
        env = self.maps_env()
        configured = env.get("PREPPERCACHE_MAP_INSTALL_DIR", "").strip()
        return Path(configured) if configured else self.maps_root()

    def custom_zim_manifest_path(self) -> Path:
        env = self.maps_env()
        configured = env.get("PREPPERCACHE_ZIM_CUSTOM_URL_FILE")
        if configured:
            return Path(configured)
        return self.repo_root / "config" / "kiwix-zim-urls.custom.txt"

    def extra_zim_manifest_path(self) -> Path:
        return self.data_dir / "kiwix-zim-urls.extra.txt"

    def custom_zim_selection_path(self) -> Path:
        env = self.maps_env()
        configured = env.get("PREPPERCACHE_ZIM_CUSTOM_SELECTION_FILE")
        if configured:
            return Path(configured)
        return self.repo_root / "config" / "kiwix-zim-selection.json"

    def custom_base_profile(self) -> str | None:
        env = self.maps_env()
        value = env.get("PREPPERCACHE_ZIM_CUSTOM_BASE_PROFILE", "essential").strip().lower()
        if value in {"", "none", "empty"}:
            return None
        if value in {"essential", "standard", "comprehensive"}:
            return value
        return "essential"

    def maps_root(self) -> Path:
        env = self.maps_env()
        return Path(env.get("PREPPERCACHE_MAP_PMTILES_ROOT", "/maps"))

    def install_destinations(self, kind: str) -> list[dict[str, object]]:
        if kind == "zims":
            internal_label = "Internal Library"
            internal_path = self.kiwix_library_dir()
            layout_key = "library"
            suffix_label = "Library"
            selected_path = self.preferred_zim_install_dir()
        elif kind == "maps":
            internal_label = "Internal Maps"
            internal_path = self.maps_root()
            layout_key = "maps"
            suffix_label = "Maps"
            selected_path = self.preferred_map_install_dir()
        else:
            raise ValueError(f"Unsupported install destination kind: {kind}")

        try:
            internal_total, internal_used, internal_free = shutil.disk_usage(internal_path)
        except OSError:
            internal_total = internal_used = internal_free = 0

        options: list[dict[str, object]] = [
            {
                "id": str(internal_path),
                "label": internal_label,
                "path": str(internal_path),
                "location": "internal",
                "mounted": True,
                "selected": str(selected_path) == str(internal_path),
                "size_bytes": int(internal_total),
                "used_bytes": int(internal_used),
                "free_bytes": int(internal_free),
            }
        ]

        seen_paths = {str(internal_path)}
        for volume in self.storage_volumes():
            if volume.get("location") != "external":
                continue
            if not volume.get("mounted"):
                continue
            layout = volume.get("preppercache_layout") or {}
            if not layout.get(layout_key):
                continue
            mountpoint = str(volume.get("mountpoint") or "").strip()
            if not mountpoint:
                continue
            if not mountpoint.startswith("/media/"):
                continue
            path = Path(mountpoint) / layout_key
            if str(path) in seen_paths:
                continue
            seen_paths.add(str(path))
            volume_name = str(volume.get("label") or volume.get("display_name") or volume.get("name") or "Drive").strip()
            size_label = format_size_bytes(int(volume.get("size_bytes") or 0)) if int(volume.get("size_bytes") or 0) else ""
            mount_label = str(volume.get("mountpoint") or "").strip()
            descriptor_parts = [part for part in (mount_label, size_label) if part]
            descriptor = f" ({' • '.join(descriptor_parts)})" if descriptor_parts else ""
            options.append(
                {
                    "id": str(path),
                    "label": f"External {volume_name} {suffix_label}{descriptor}",
                    "path": str(path),
                    "location": "external",
                    "mounted": path.exists(),
                    "selected": str(selected_path) == str(path),
                    "size_bytes": int(volume.get("size_bytes") or 0),
                    "used_bytes": int(volume.get("used_bytes") or 0),
                    "free_bytes": int(volume.get("free_bytes") or 0),
                }
            )
        return options

    def validate_install_destination(self, kind: str, path_value: str | None) -> str:
        options = self.install_destinations(kind)
        allowed = {str(option["path"]) for option in options}
        if not path_value:
            return str(self.kiwix_library_dir() if kind == "zims" else self.maps_root())
        if path_value not in allowed:
            raise ValueError("Selected install destination is not available.")
        return path_value

    def managed_links_manifest_path(self, kind: str) -> Path:
        if kind == "maps":
            return self.external_maps_links_file
        if kind == "zims":
            return self.external_zims_links_file
        raise ValueError(f"Unsupported managed link kind: {kind}")

    def read_managed_links_manifest(self, kind: str) -> dict[str, str]:
        data = read_json(self.managed_links_manifest_path(kind), {"links": {}})
        links = data.get("links", {})
        if not isinstance(links, dict):
            return {}
        return {
            str(name): str(target)
            for name, target in links.items()
            if isinstance(name, str) and isinstance(target, str)
        }

    def write_managed_links_manifest(self, kind: str, links: dict[str, str]) -> None:
        path = self.managed_links_manifest_path(kind)
        path.write_text(json.dumps({"links": links}, indent=2) + "\n")

    def external_storage_roots(self, directory_name: str) -> list[Path]:
        roots: list[Path] = []
        for volume in self.storage_volumes():
            if not volume.get("mounted"):
                continue
            layout = volume.get("preppercache_layout") or {}
            if not layout.get(directory_name):
                continue
            mountpoint = str(volume.get("mountpoint") or "").strip()
            if not mountpoint:
                continue
            roots.append(Path(mountpoint) / directory_name)
        return roots

    def sync_external_links(self, kind: str, canonical_root: Path, external_roots: list[Path], suffix: str) -> None:
        canonical_root.mkdir(parents=True, exist_ok=True)
        manifest = self.read_managed_links_manifest(kind)
        current_targets: dict[str, str] = {}

        for name, target in manifest.items():
            link_path = canonical_root / name
            try:
                if link_path.is_symlink():
                    resolved = str(link_path.resolve(strict=False))
                    if resolved == target or not Path(target).exists():
                        link_path.unlink()
                elif not link_path.exists():
                    pass
            except OSError:
                continue

        for external_root in external_roots:
            if not external_root.exists():
                continue
            for path in sorted(external_root.iterdir()):
                if not path.is_file() or path.suffix.lower() != suffix:
                    continue
                link_path = canonical_root / path.name
                current_targets[path.name] = str(path.resolve())
                if link_path.exists() or link_path.is_symlink():
                    if link_path.is_symlink():
                        try:
                            if str(link_path.resolve(strict=False)) == str(path.resolve()):
                                continue
                        except OSError:
                            pass
                    continue
                try:
                    link_path.symlink_to(path.resolve())
                except FileExistsError:
                    continue

        self.write_managed_links_manifest(kind, current_targets)

    def sync_external_content_links(self) -> None:
        self.sync_external_links("maps", self.maps_root(), self.external_storage_roots("maps"), ".pmtiles")
        zim_roots = self.external_storage_roots("library")
        wikipedia_root = self.preferred_wikipedia_install_dir()
        if (
            str(wikipedia_root) != str(self.kiwix_library_dir())
            and str(wikipedia_root) != str(self.preferred_zim_install_dir())
            and str(wikipedia_root).startswith("/media/")
        ):
            zim_roots.append(wikipedia_root)
        deduped_roots: list[Path] = []
        seen_roots: set[str] = set()
        for root in zim_roots:
            root_key = str(root.resolve(strict=False))
            if root_key in seen_roots:
                continue
            seen_roots.add(root_key)
            deduped_roots.append(root)
        self.sync_external_links("zims", self.kiwix_library_dir(), deduped_roots, ".zim")

    def remove_managed_linked_content(self, kind: str, canonical_root: Path, names: set[str]) -> None:
        manifest = self.read_managed_links_manifest(kind)
        changed = False
        for name in names:
            link_path = canonical_root / name
            target_path = Path(manifest.get(name, ""))
            if target_path and target_path.exists():
                try:
                    target_path.unlink()
                except FileNotFoundError:
                    pass
            try:
                if link_path.is_symlink():
                    link_path.unlink()
            except FileNotFoundError:
                pass
            if name in manifest:
                manifest.pop(name, None)
                changed = True
        if changed:
            self.write_managed_links_manifest(kind, manifest)

    def maps_web_root(self) -> Path:
        env = self.maps_env()
        return Path(env.get("PREPPERCACHE_MAPS_ROOT", "/srv/preppercache/www/maps"))

    def active_pmtiles_file(self) -> str:
        env = self.maps_env()
        return env.get("PREPPERCACHE_MAP_PMTILES_FILE", "basemap.pmtiles")

    def list_pmtiles_packages(self, valid_only: bool = False) -> list[str]:
        self.sync_external_content_links()
        root = self.maps_root()
        if not root.exists():
            return []
        packages: list[str] = []
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.suffix.lower() != ".pmtiles":
                continue
            if valid_only and not inspect_pmtiles_file(path).get("valid"):
                continue
            packages.append(path.name)
        return packages

    def pmtiles_inventory(self) -> list[dict[str, object]]:
        self.sync_external_content_links()
        root = self.maps_root()
        if not root.exists():
            return []
        inventory: list[dict[str, object]] = []
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.suffix.lower() != ".pmtiles":
                continue
            details = inspect_pmtiles_file(path)
            inventory.append(
                {
                    "name": path.name,
                    "size_bytes": details["size_bytes"],
                    "external": path.is_symlink(),
                    "valid": details["valid"],
                    "error": details["error"],
                }
            )
        return inventory

    def write_maps_runtime_config(self) -> None:
        env = self.maps_env()
        maps_web_root = self.maps_web_root()
        maps_web_root.mkdir(parents=True, exist_ok=True)
        status = self.maps_status()
        active_file = env.get("PREPPERCACHE_MAP_PMTILES_FILE", "basemap.pmtiles")
        active_path = self.maps_root() / active_file
        cache_buster = ""
        if active_path.exists() and active_path.is_file():
            stat = active_path.stat()
            cache_buster = f"?v={stat.st_mtime_ns}-{stat.st_size}"
        config = {
            "pmtilesUrl": f"/pmtiles/{active_file}{cache_buster}",
            "pmtilesFile": active_file,
            "flavor": env.get("PREPPERCACHE_MAP_STYLE_FLAVOR", "light"),
            "language": env.get("PREPPERCACHE_MAP_LANGUAGE", "en"),
            "defaultLat": float(env.get("PREPPERCACHE_MAP_DEFAULT_LAT", "39.8283")),
            "defaultLon": float(env.get("PREPPERCACHE_MAP_DEFAULT_LON", "-98.5795")),
            "defaultZoom": int(env.get("PREPPERCACHE_MAP_DEFAULT_ZOOM", "4")),
            "minZoom": int(env.get("PREPPERCACHE_MAP_MIN_ZOOM", "2")),
            "maxZoom": int(env.get("PREPPERCACHE_MAP_MAX_ZOOM", "20")),
        }
        (maps_web_root / "config.json").write_text(json.dumps(config, indent=2) + "\n")
        (maps_web_root / "state.json").write_text(json.dumps(status, indent=2) + "\n")

    def maps_status(self) -> dict:
        env = self.maps_env()
        root = self.maps_root()
        active_file = env.get("PREPPERCACHE_MAP_PMTILES_FILE", "basemap.pmtiles")
        active_path = root / active_file
        inventory = self.pmtiles_inventory()
        valid_packages = [item["name"] for item in inventory if item["valid"]]
        invalid_packages = [
            {"name": item["name"], "size_bytes": item["size_bytes"], "error": item["error"]}
            for item in inventory
            if not item["valid"]
        ]
        active_details = inspect_pmtiles_file(active_path)
        return {
            "root": str(root),
            "active_file": active_file,
            "active_url": f"/pmtiles/{active_file}",
            "active_exists": bool(active_details["exists"]),
            "active_valid": bool(active_details["valid"]),
            "active_error": active_details["error"],
            "active_size_bytes": active_details["size_bytes"],
            "available_files": valid_packages,
            "invalid_files": invalid_packages,
            "state_regions": {
                f"state-{state['id']}.pmtiles": {"id": region["id"], "name": region["name"], "title": state["name"]}
                for region in self.regional_map_definitions()
                for state in region["states"]
            },
            "inventory": inventory,
            "flavor": env.get("PREPPERCACHE_MAP_STYLE_FLAVOR", "light"),
            "language": env.get("PREPPERCACHE_MAP_LANGUAGE", "en"),
            "install_dir": str(self.preferred_map_install_dir()),
            "install_destinations": self.install_destinations("maps"),
        }

    def read_custom_zim_selection(self) -> dict:
        return read_json(
            self.custom_zim_selection_path(),
            {
                "catalog_root": self.zim_catalog_root(),
                "selected_items": [],
                "saved_at": None,
            },
        )

    def write_custom_zim_selection(self, items: list[dict[str, object]], catalog_root: str) -> dict:
        payload = {
            "catalog_root": catalog_root,
            "selected_items": items,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        path = self.custom_zim_selection_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n")
        return payload

    def read_manifest_urls(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        return [
            line.strip()
            for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    def curated_manifest_urls(self, profile: str | None) -> list[str]:
        if not profile:
            return []

        output_path = self.data_dir / f"curated-{profile}-manifest.txt"
        command = [
            "python3",
            str(self.repo_root / "scripts" / "build_kiwix_zim_manifest.py"),
            "--source",
            str(self.repo_root / "catalog" / "kiwix-categories.yaml"),
            "--output",
            str(output_path),
            "--profile",
            profile,
            "--wikipedia-options",
            str(self.repo_root / "catalog" / "wikipedia.yaml"),
            "--wikipedia-choice",
            self.maps_env().get("PREPPERCACHE_WIKIPEDIA_OPTION", "top-mini"),
        ]
        result = subprocess.run(
            command,
            cwd=self.repo_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("Unable to build curated Kiwix manifest")
        return self.read_manifest_urls(output_path)

    def write_custom_zim_manifest(
        self,
        items: list[dict[str, object]],
        catalog_root: str,
        base_profile: str | None,
    ) -> Path:
        manifest = self.custom_zim_manifest_path()
        manifest.parent.mkdir(parents=True, exist_ok=True)
        base_urls = self.curated_manifest_urls(base_profile)
        seen_urls: set[str] = set()
        merged_urls: list[tuple[str, str, str]] = []

        for url in base_urls:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            merged_urls.append(("Curated Base", profile_to_preset(base_profile), url))

        for item in items:
            url = str(item["download_url"])
            if url in seen_urls:
                continue
            seen_urls.add(url)
            merged_urls.append((str(item["category"]), str(item["name"]), url))

        lines = [
            f"# Custom PrepperCache ZIM selection from {catalog_root}",
            f"# Saved at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            f"# Base preset: {profile_to_preset(base_profile)}",
            f"# Resource count: {len(merged_urls)}",
            "",
        ]
        for category, title, url in merged_urls:
            lines.append(f"# {category} | {title}")
            lines.append(url)
            lines.append("")
        manifest.write_text("\n".join(lines).rstrip() + "\n")
        return manifest

    def write_extra_zim_manifest(
        self,
        items: list[dict[str, object]],
        catalog_root: str,
    ) -> Path:
        manifest = self.extra_zim_manifest_path()
        manifest.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# Extra PrepperCache ZIM selection from {catalog_root}",
            f"# Saved at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            f"# Resource count: {len(items)}",
            "",
        ]
        for item in items:
            lines.append(f"# {item.get('category', 'Extra Content')} | {item.get('name', 'ZIM')}")
            lines.append(str(item["download_url"]))
            lines.append("")
        manifest.write_text("\n".join(lines).rstrip() + "\n")
        return manifest

    def catalog_items_for_paths(self, selected_paths: list[str]) -> tuple[list[dict[str, object]], str]:
        catalog = self.fetch_kiwix_catalog_cached(force_refresh=False)
        catalog_by_path = {item["path"]: item for item in catalog["items"]}
        missing = [path for path in selected_paths if path not in catalog_by_path]
        if missing:
            raise ValueError(f"Selected ZIM is not in catalog: {missing[0]}")
        return [catalog_by_path[path] for path in selected_paths], catalog["source"]["root_url"]

    def list_installed_zims(self) -> list[dict[str, object]]:
        self.sync_external_content_links()
        root = self.kiwix_library_dir()
        if not root.exists():
            return []
        volumes = self.storage_volumes()
        inventory: list[dict[str, object]] = []
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.suffix.lower() != ".zim":
                continue
            stat = path.stat()
            resolved = path.resolve(strict=False)
            volume = self.volume_for_path(resolved, volumes)
            location = str(volume.get("location", "internal")) if volume else "internal"
            inventory.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "resolved_path": str(resolved),
                    "location": location,
                    "size_bytes": stat.st_size,
                    "size_label": format_size_bytes(stat.st_size),
                    "modified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
                }
            )
        return inventory

    def current_base_content_filenames(self) -> set[str]:
        env = self.maps_env()
        mode = env.get("PREPPERCACHE_ZIM_MODE", "full")
        wikipedia_option = env.get("PREPPERCACHE_WIKIPEDIA_OPTION", "top-mini")
        resources: list[dict[str, object]] = []
        if mode == "quick-test":
            for url in self.read_manifest_urls(self.repo_root / "config" / "kiwix-zim-urls.quick-test.txt"):
                resources.append({"filename": Path(urlparse(url).path).name})
        elif mode == "custom":
            base_profile = self.custom_base_profile()
            if base_profile:
                resources = self.curated_resources(base_profile, wikipedia_option)
        else:
            profile = env.get("PREPPERCACHE_ZIM_PROFILE", "essential").strip().lower() or "essential"
            resources = self.curated_resources(profile, wikipedia_option)
        return {
            str(item.get("filename", "")).strip()
            for item in resources
            if str(item.get("filename", "")).strip()
        }

    def fetch_directory_listing(self, url: str) -> list[dict[str, object]]:
        req = request.Request(
            url,
            headers={
                "User-Agent": "PrepperCache-Portal",
            },
        )
        with request.urlopen(req, timeout=30) as response:
            html = response.read().decode("utf-8", errors="replace")
        parser = DirectoryIndexParser()
        parser.feed(html)
        return parser.links

    def zim_mirrors(self) -> list[dict]:
        return read_json(self.repo_root / "catalog" / "zim-mirrors.json", {}).get("mirrors", [])

    def validate_zim_mirror(self, mirror: object) -> str:
        if not isinstance(mirror, str) or mirror not in {item["url"] for item in self.zim_mirrors()}:
            raise ValueError("Choose a listed ZIM download mirror.")
        return mirror

    def zim_catalog_root(self) -> str:
        root = self.maps_env().get("PREPPERCACHE_ZIM_CATALOG_URL", "").strip().rstrip("/")
        if root in {"", "https://download.kiwix.org/zim"}:
            return KIWIX_MIRROR_ROOT
        return root + "/"

    def load_content_catalog_cache(self) -> dict | None:
        if not self.content_catalog_cache_file.exists():
            return None
        try:
            cached = json.loads(self.content_catalog_cache_file.read_text())
            source = (cached.get("payload") or {}).get("source") or {}
            if source.get("root_url") == self.zim_catalog_root():
                return cached
            return None
        except json.JSONDecodeError:
            return None

    def build_kiwix_catalog_payload(self, *, cache: bool = True) -> dict:
        root_url = self.zim_catalog_root().rstrip("/") + "/"
        installed = {item["name"]: item for item in self.list_installed_zims()}
        custom_selection = self.read_custom_zim_selection()
        selected_paths = {
            item.get("path")
            for item in custom_selection.get("selected_items", [])
            if item.get("path")
        }

        try:
            root_links = self.fetch_directory_listing(root_url)
            directories = sorted(
                {
                    str(entry["href"]).rstrip("/")
                    for entry in root_links
                    if re.fullmatch(r"[A-Za-z0-9_-]+/", str(entry.get("href", "")))
                }
            )

            if not directories:
                raise RuntimeError("Kiwix did not return a valid category index.")
            items: list[dict[str, object]] = []
            for directory in directories:
                directory_url = parse.urljoin(root_url, directory + "/")
                file_links = self.fetch_directory_listing(directory_url)
                for entry in sorted(file_links, key=lambda item: str(item.get("href", ""))):
                    href = str(entry.get("href", ""))
                    if not href or href in {"../", "/"} or not href.endswith(".zim"):
                        continue
                    path = f"{directory}/{Path(href).name}"
                    name = Path(href).name
                    installed_item = installed.get(name)
                    items.append(
                        {
                            "path": path,
                            "name": name,
                            "category": directory,
                            "download_url": parse.urljoin(directory_url, href),
                            "size_bytes": int(entry["size_bytes"]) if entry.get("size_bytes") else 0,
                            "size_label": str(entry["size_label"]) if entry.get("size_label") else "",
                            "installed": name in installed,
                            "installed_size_bytes": int(installed_item["size_bytes"]) if installed_item else 0,
                            "installed_size_label": installed_item["size_label"] if installed_item else "",
                            "selected": path in selected_paths,
                        }
                    )
            if not items:
                raise RuntimeError("Kiwix returned no downloadable ZIM files.")
        except (error.URLError, OSError, RuntimeError) as exc:
            raise RuntimeError(f"Unable to fetch Kiwix catalog: {exc}") from exc

        payload = {
            "source": {
                "root_url": root_url,
            },
            "items": items,
            "stale": False,
            "error": None,
            "refreshing": False,
        }
        if cache:
            temporary_cache = self.content_catalog_cache_file.with_suffix(".tmp")
            temporary_cache.write_text(
                json.dumps({"fetched_at": time.time(), "payload": payload}, indent=2) + "\n"
            )
            temporary_cache.replace(self.content_catalog_cache_file)
        return payload

    def refresh_kiwix_catalog_in_background(self) -> None:
        root_url = self.zim_catalog_root()
        try:
            self.build_kiwix_catalog_payload()
        except Exception as exc:
            # Surface background failures to the polling UI, keeping saved data.
            with self.content_catalog_lock:
                if self.zim_catalog_root() == root_url:
                    self.content_catalog_refresh_error = str(exc)
        finally:
            with self.content_catalog_lock:
                self.content_catalog_refresh_thread = None

    def start_kiwix_catalog_refresh(self) -> bool:
        with self.content_catalog_lock:
            if self.content_catalog_refresh_thread and self.content_catalog_refresh_thread.is_alive():
                return False
            self.content_catalog_refresh_error = None
            self.content_catalog_refresh_attempt = time.time()
            self.content_catalog_refresh_thread = threading.Thread(
                target=self.refresh_kiwix_catalog_in_background,
                daemon=True,
            )
            self.content_catalog_refresh_thread.start()
            return True

    def fetch_kiwix_catalog_cached(self, force_refresh: bool = False) -> dict:
        cached = self.load_content_catalog_cache() or {}
        cached_payload = cached.get("payload") or {}
        # Old versions could cache the redirected HTML page as an empty success.
        if not cached_payload.get("items"):
            cached_payload = {}
        fresh = bool(cached_payload and time.time() - float(cached.get("fetched_at", 0)) < 3600)
        with self.content_catalog_lock:
            retry_allowed = time.time() - self.content_catalog_refresh_attempt >= 60
        if force_refresh or (not fresh and retry_allowed):
            self.start_kiwix_catalog_refresh()
        with self.content_catalog_lock:
            refreshing = bool(self.content_catalog_refresh_thread and self.content_catalog_refresh_thread.is_alive())
            refresh_error = self.content_catalog_refresh_error
        if not refreshing and not refresh_error:
            # A worker may have finished between the initial cache read and
            # the state check. Return its results before the UI stops polling.
            latest = self.load_content_catalog_cache() or {}
            if (latest.get("payload") or {}).get("items"):
                cached_payload = latest["payload"]
                fresh = time.time() - float(latest.get("fetched_at", 0)) < 3600
        payload = dict(cached_payload) if cached_payload else {
            "source": {"root_url": self.zim_catalog_root().rstrip("/") + "/"},
            "items": [],
        }
        payload["stale"] = bool(cached_payload and (not fresh or refresh_error))
        payload["refreshing"] = refreshing
        if refreshing:
            payload["error"] = (
                "Refreshing the Kiwix catalog in the background. Showing saved results for now."
                if cached_payload else "Loading the Kiwix catalog. Results will appear automatically."
            )
        else:
            payload["error"] = refresh_error
        return payload

    def content_status(self) -> dict:
        env = self.maps_env()
        custom_selection = self.read_custom_zim_selection()
        installed = self.list_installed_zims()
        installed_size_bytes = sum(int(item["size_bytes"]) for item in installed)
        installed_internal_size_bytes = sum(
            int(item["size_bytes"]) for item in installed if item.get("location") != "external"
        )
        installed_external_size_bytes = sum(
            int(item["size_bytes"]) for item in installed if item.get("location") == "external"
        )
        selected_paths = {
            item.get("path")
            for item in custom_selection.get("selected_items", [])
            if item.get("path")
        }
        selected_names = {
            Path(str(item.get("path", ""))).name
            for item in custom_selection.get("selected_items", [])
            if item.get("path")
        }
        base_filenames = self.current_base_content_filenames()
        inventory = []
        for item in installed:
            inventory.append(
                {
                    **item,
                    "selected": item["name"] in selected_names,
                    "in_base_set": item["name"] in base_filenames,
                    "is_extra": item["name"] not in base_filenames,
                }
            )
        mode = env.get("PREPPERCACHE_ZIM_MODE", "full")
        profile = env.get("PREPPERCACHE_ZIM_PROFILE", "essential")
        if mode == "custom":
            library_preset = "custom"
        elif mode == "quick-test":
            library_preset = "quick-test"
        else:
            library_preset = profile_to_preset(profile)
        return {
            "mode": mode,
            "profile": profile,
            "library_preset": library_preset,
            "custom_base_preset": profile_to_preset(self.custom_base_profile()),
            "wikipedia_option": env.get("PREPPERCACHE_WIKIPEDIA_OPTION", "top-mini"),
            "library_root": str(self.kiwix_library_dir()),
            "catalog_root": self.zim_catalog_root(),
            "zim_mirrors": self.zim_mirrors(),
            "automatic_updates": self.content_update_settings(),
            "custom_manifest": str(self.custom_zim_manifest_path()),
            "custom_selection_file": str(self.custom_zim_selection_path()),
            "custom_selected_paths": sorted(selected_paths),
            "custom_selected_count": len(selected_paths),
            "installed_count": len(inventory),
            "installed_size_bytes": installed_size_bytes,
            "installed_size_label": format_size_bytes(installed_size_bytes),
            "installed_internal_size_bytes": installed_internal_size_bytes,
            "installed_internal_size_label": format_size_bytes(installed_internal_size_bytes),
            "installed_external_size_bytes": installed_external_size_bytes,
            "installed_external_size_label": format_size_bytes(installed_external_size_bytes),
            "installed_items": inventory,
            "install_dir": str(self.preferred_zim_install_dir()),
            "install_destinations": self.install_destinations("zims"),
        }

    def content_set_removals(self, keep: set[str], destinations: set[Path]) -> list[dict]:
        root = self.kiwix_library_dir()
        removals = []
        for directory in sorted({root, *destinations}, key=str):
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                if path.name in keep or path.suffix.lower() != ".zim" or not path.is_file():
                    continue
                info = path.lstat()
                symlink = path.is_symlink()
                removals.append({
                    "path": str(path),
                    "name": path.name,
                    "identity": str(path.readlink()) if symlink else [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns],
                    "size_bytes": 0 if symlink else info.st_size,
                })
        return removals

    def content_set_plan(self, profile: str) -> tuple[dict, list[str]]:
        if profile not in ("essential", "standard", "comprehensive"):
            raise ValueError("Choose Essential, Standard, or Comprehensive.")

        env = self.maps_env()
        seen = {item["name"] for item in self.list_installed_zims()}
        wikipedia_choice = env.get("PREPPERCACHE_WIKIPEDIA_OPTION", "top-mini")
        resources = {item["filename"]: item for item in self.curated_resources(profile, wikipedia_choice)}
        levels = ("essential", "standard", "comprehensive")
        previous = env.get("PREPPERCACHE_ZIM_PROFILE", "essential")
        if env.get("PREPPERCACHE_ZIM_MODE") == "custom":
            previous = self.custom_base_profile()
        if env.get("PREPPERCACHE_ZIM_MODE") == "quick-test" or previous not in levels:
            previous = None
        downgrade = previous is not None and levels.index(profile) < levels.index(previous)
        previous_files = {item["filename"] for item in self.curated_resources(previous, wikipedia_choice)} if previous else set()
        library = self.preferred_zim_install_dir()
        wikipedia = self.preferred_wikipedia_install_dir()
        mounted = {Path(item["path"]) for item in self.install_destinations("zims") if item["mounted"]}
        requirements: dict[Path, int] = {}
        urls = []
        unknown_sizes = 0
        for name, resource in resources.items():
            url = resource["url"]
            if name in seen:
                continue
            seen.add(name)
            urls.append(url)
            directory = wikipedia if "/zim/wikipedia/" in url or name.startswith("wikipedia_") else library
            size = max(0, resource["size_mb"]) * 1024**2
            unknown_sizes += size == 0
            requirements[directory] = requirements.get(directory, 0) + size

        disks = {}
        problem = None
        for directory, required_bytes in requirements.items():
            try:
                if directory not in mounted:
                    raise FileNotFoundError(directory)
                device = directory.stat().st_dev
                free_bytes = shutil.disk_usage(directory).free
            except OSError:
                problem = f"Download destination unavailable: {directory}. Connect the drive or choose another destination."
                continue
            disk = disks.setdefault(device, {"destinations": [], "required_bytes": 0, "free_bytes": free_bytes})
            disk["destinations"].append(str(directory))
            disk["required_bytes"] += required_bytes

        if not problem and any(disk["required_bytes"] > disk["free_bytes"] for disk in disks.values()):
            problem = "Not enough free space. Free up space or choose another download destination."
        if not resources:
            problem = "The content set catalog is unavailable."
        removals = self.content_set_removals(set(resources), mounted) if downgrade and resources else []
        review_id = None
        if downgrade:
            review = {"profile": profile, "previous": previous, "required": sorted(resources), "removals": removals}
            review_id = hashlib.sha256(json.dumps(review, sort_keys=True).encode()).hexdigest()
        return {
            "download_count": len(urls),
            "included_count": len(resources),
            "included_bytes": sum(max(0, item["size_mb"]) * 1024**2 for item in resources.values()),
            "added_titles": [item.get("title", name) for name, item in resources.items() if name not in previous_files],
            "downgrade": downgrade,
            "remove_files": [{"name": item["name"], "path": item["path"]} for item in removals],
            "remove_bytes": sum(item["size_bytes"] for item in removals),
            "review_id": review_id,
            "_removals": removals,
            "_required_filenames": list(resources),

            "required_bytes": sum(requirements.values()),
            "unknown_sizes": unknown_sizes,
            "disks": list(disks.values()),
            "problem": problem,
        }, urls

    def change_content_set(self, profile: str, confirmed_review: str | None = None) -> dict:
        with self.apply_lock, self.map_sync_lock:
            if self.load_apply_state().get("status") == "running":
                raise RuntimeError("Wait for the current content job to finish.")
            plan, urls = self.content_set_plan(profile)
            if plan["problem"]:
                raise ValueError(plan["problem"])
            if plan["downgrade"]:
                if confirmed_review != plan["review_id"]:
                    raise ValueError("Review and confirm the current list of files to remove.")
                (self.data_dir / "content-downgrade.json").write_text(json.dumps({
                    "profile": profile,
                    "urls": urls,
                    "required": plan["_required_filenames"],
                    "remove": plan["_removals"],
                }))
                apply = self.launch_apply("downgrade-content-set", clear_log=True)
            else:
                if confirmed_review is not None:
                    raise ValueError("The selected set changed. Review it again before applying.")
                if urls:
                    (self.data_dir / "content-set-urls.txt").write_text("\n".join(urls) + "\n")
                self.activate_content_profile(profile)
                apply = self.launch_apply("change-content-set", clear_log=True) if urls else None
        return {"content": self.content_status(), "apply": apply}

    def activate_content_profile(self, profile: str) -> None:
        update_env_file(self.preppercache_env, {
            "PREPPERCACHE_ZIM_MODE": "full",
            "PREPPERCACHE_ZIM_PROFILE": profile,
        })

    def remove_downgrade_files(self, job: dict) -> None:
        if not job.get("required"):
            raise ValueError("The confirmed content set could not be loaded.")
        installed = {item["name"] for item in self.list_installed_zims()}
        if not set(job["required"]).issubset(installed):
            raise ValueError("The smaller set is incomplete. Existing ZIMs were kept.")
        approved = {item["path"]: item["identity"] for item in job["remove"]}
        mounted = {Path(item["path"]) for item in self.install_destinations("zims") if item["mounted"]}
        allowed = {self.kiwix_library_dir(), *mounted}
        if any(Path(path).parent not in allowed for path in approved):
            raise ValueError("A reviewed download drive is unavailable. Reconnect it and review again.")
        current = self.content_set_removals(set(job["required"]), mounted)
        if any(item["identity"] != approved.get(item["path"]) for item in current):
            raise ValueError("The installed files changed. Review the downgrade again.")
        # Remove managed links first. Inventory refresh can then recreate only links for files still present.
        paths = [Path(item["path"]) for item in current]
        for path in sorted(paths, key=lambda path: not path.is_symlink()):
            path.unlink(missing_ok=True)
        self.sync_external_content_links()
        if self.custom_zim_selection_path().is_file():
            self.write_custom_zim_selection([], self.zim_catalog_root())

    def save_content_settings(self, payload: dict) -> dict:
        if "zim_mirror" in payload:
            mirror = self.validate_zim_mirror(payload["zim_mirror"])
            if mirror != self.zim_catalog_root():
                update_env_file(self.preppercache_env, {"PREPPERCACHE_ZIM_CATALOG_URL": mirror})
                with self.content_catalog_lock:
                    self.content_catalog_refresh_attempt = 0
                    self.content_catalog_refresh_error = None
            return self.content_status()
        env = self.maps_env()
        library_preset = payload.get("library_preset")
        if library_preset is None:
            current_mode = env.get("PREPPERCACHE_ZIM_MODE", "full")
            current_profile = env.get("PREPPERCACHE_ZIM_PROFILE", "essential")
            library_preset = "custom" if current_mode == "custom" else profile_to_preset(current_profile)
        if library_preset not in {"compact", "full", "custom", "quick-test"}:
            raise ValueError("Invalid library_preset")

        custom_base_preset = payload.get("custom_base_preset")
        if custom_base_preset is None:
            custom_base_preset = profile_to_preset(self.custom_base_profile())
        if custom_base_preset not in {"compact", "full", "empty"}:
            raise ValueError("Invalid custom_base_preset")

        updates: dict[str, str] = {}
        install_dir = payload.get("install_dir")
        if install_dir is not None:
            validated_install_dir = self.validate_install_destination("zims", str(install_dir).strip())
            updates["PREPPERCACHE_ZIM_INSTALL_DIR"] = validated_install_dir
            # The Content settings page exposes a single destination selector for
            # new ZIM downloads, so keep the Wikipedia-specific path aligned too.
            updates["PREPPERCACHE_WIKIPEDIA_INSTALL_DIR"] = validated_install_dir
            if not any(key in payload for key in ("library_preset", "custom_base_preset", "selected_paths")):
                update_env_file(self.preppercache_env, updates)
                return self.content_status()
        if library_preset == "quick-test":
            updates["PREPPERCACHE_ZIM_MODE"] = "quick-test"
        elif library_preset == "custom":
            updates["PREPPERCACHE_ZIM_MODE"] = "custom"
            updates["PREPPERCACHE_ZIM_CUSTOM_BASE_PROFILE"] = preset_to_profile(custom_base_preset) or "none"
        else:
            updates["PREPPERCACHE_ZIM_MODE"] = "full"
            mapped_profile = preset_to_profile(library_preset)
            if mapped_profile:
                updates["PREPPERCACHE_ZIM_PROFILE"] = mapped_profile

        selected_paths = payload.get("selected_paths")
        catalog = None
        if selected_paths is not None:
            if not isinstance(selected_paths, list):
                raise ValueError("selected_paths must be a list")
            cleaned_paths = []
            for value in selected_paths:
                if not isinstance(value, str) or "/" not in value or not value.endswith(".zim"):
                    raise ValueError("Invalid custom ZIM selection")
                cleaned_paths.append(value)
            cleaned_paths = sorted(set(cleaned_paths))
            if library_preset == "custom":
                base_profile = preset_to_profile(custom_base_preset)
                selected_items, catalog_root = self.catalog_items_for_paths(cleaned_paths)
                self.write_custom_zim_selection(selected_items, catalog_root)
                self.write_custom_zim_manifest(selected_items, catalog_root, base_profile)
            elif cleaned_paths:
                # Preserve the saved custom selection even when mode is switched away from custom.
                if self.custom_zim_selection_path().exists():
                    existing = self.read_custom_zim_selection()
                    existing_by_path = {
                        item.get("path"): item
                        for item in existing.get("selected_items", [])
                        if item.get("path")
                    }
                    selected_items = [
                        existing_by_path[path]
                        for path in cleaned_paths
                        if path in existing_by_path
                    ]
                    self.write_custom_zim_selection(selected_items, existing.get("catalog_root", self.zim_catalog_root()))
                    self.write_custom_zim_manifest(
                        selected_items,
                        existing.get("catalog_root", self.zim_catalog_root()),
                        self.custom_base_profile(),
                    )

        update_env_file(self.preppercache_env, updates)
        return self.content_status()

    def download_selected_extra_zims(self, payload: dict) -> dict:
        selected_paths = payload.get("selected_paths")
        if not isinstance(selected_paths, list) or not selected_paths:
            raise ValueError("Select one or more ZIM files first.")

        cleaned_paths = []
        for value in selected_paths:
            if not isinstance(value, str) or "/" not in value or not value.endswith(".zim"):
                raise ValueError("Invalid extra ZIM selection")
            cleaned_paths.append(value)
        cleaned_paths = sorted(set(cleaned_paths))

        catalog = self.fetch_kiwix_catalog_cached()
        catalog_by_path = {item["path"]: item for item in catalog["items"]}
        skipped_paths = [path for path in cleaned_paths if path not in catalog_by_path]
        if skipped_paths and (catalog["refreshing"] or catalog["stale"] or catalog["error"]):
            raise ValueError("Check Online Catalog and wait for a successful refresh before downloading.")
        selected_items = [catalog_by_path[path] for path in cleaned_paths if path in catalog_by_path]
        if not selected_items:
            raise ValueError("None of the selected ZIM files are available in the current catalog. Select an available edition.")
        catalog_root = catalog["source"]["root_url"]
        with self.apply_lock, self.map_sync_lock:
            if self.load_apply_state().get("status") == "running":
                raise RuntimeError("A content job is already running.")
            installed = {item["name"] for item in self.list_installed_zims()}
            missing = [item for item in selected_items if item["name"] not in installed]
            if not missing:
                raise ValueError("All selected files are already installed.")
            self.write_custom_zim_selection(selected_items, catalog_root)
            self.write_extra_zim_manifest(missing, catalog_root)
            apply = self.launch_apply("download-extra-content", clear_log=True)
        return {"content": self.content_status(), "apply": apply, "skipped_paths": skipped_paths}

    def retry_extra_content(self) -> dict:
        with self.apply_lock, self.map_sync_lock:
            state = self.load_apply_state()
            failures = set(state.get("failed_download_names", []))
            if state.get("status") == "running":
                raise RuntimeError("A content job is already running.")
            if state.get("action") != "download-extra-content" or not failures:
                raise ValueError("No failed extra content downloads to retry.")
            manifest = self.extra_zim_manifest_path()
            urls = [url for url in self.read_manifest_urls(manifest) if url.rsplit("/", 1)[-1] in failures]
            if not urls:
                raise ValueError("The failed files are no longer in the download list.")
            pending = manifest.with_suffix(".retry.tmp")
            pending.write_text("\n".join(urls) + "\n")
            pending.replace(manifest)
            return self.launch_apply("download-extra-content", clear_log=True)

    def upload_zim(self, filename: str, length: int, source, *, destination: Path | None = None) -> dict:
        if (
            not filename.lower().endswith(".zim") or filename.startswith(".")
            or any(char in filename for char in "/\\")
            or any(ord(char) < 32 or ord(char) == 127 for char in filename)
            or len(filename.encode("utf-8")) > 255
        ):
            raise ValueError("Choose a ZIM file with a valid filename.")
        filename = filename[:-4] + ".zim"
        if length < 80:
            raise ValueError("This file is too small to be a ZIM archive.")
        if not self.zim_upload_lock.acquire(blocking=False):
            raise RuntimeError("Another ZIM upload is in progress.")
        try:
            destination = destination or self.preferred_zim_install_dir()
            if not any(item["mounted"] and Path(item["path"]) == destination
                       for item in self.install_destinations("zims")) or not destination.is_dir():
                raise ValueError("The selected content drive is unavailable.")
            target = destination / filename
            library_file = self.kiwix_library_dir() / filename
            if os.path.lexists(target) or os.path.lexists(library_file):
                raise FileExistsError("A ZIM with that name is already installed.")
            if shutil.disk_usage(destination).free < length:
                raise ValueError("There is not enough free space on the selected content drive.")

            header = source.read(80)
            if len(header) != 80 or header[:4] != b"ZIM\x04":
                raise ValueError("This file does not have a valid ZIM header.")
            identity = destination.stat()
            staging = destination / ".preppercache-upload-tmp"
            staging.mkdir(mode=0o700, exist_ok=True)
            if staging.is_symlink() or not staging.is_dir():
                raise ValueError("The upload staging directory is unavailable.")

            with tempfile.TemporaryDirectory(prefix="zim-", dir=staging) as job_dir:
                temporary = Path(job_dir) / "upload.part"
                with temporary.open("wb") as output:
                    output.write(header)
                    remaining = length - len(header)
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("The upload ended before the full file arrived.")
                        output.write(chunk)
                        remaining -= len(chunk)
                    output.flush()
                    os.fsync(output.fileno())

                with self.apply_lock:
                    current = destination.stat()
                    if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
                        raise ValueError("The selected content drive changed during the upload.")
                    if os.path.lexists(library_file):
                        raise FileExistsError("A ZIM with that name is already installed.")
                    # Hard links publish atomically. GNU mv covers drives such as exFAT.
                    try:
                        os.link(temporary, target)
                    except FileExistsError:
                        raise FileExistsError("A ZIM with that name is already installed.") from None
                    except OSError as exc:
                        if exc.errno not in {errno.EPERM, errno.EOPNOTSUPP}:
                            raise
                        subprocess.run(["mv", "-nT", "--", str(temporary), str(target)], check=True)
                        if temporary.exists():
                            raise FileExistsError("A ZIM with that name is already installed.")
                    self.sync_external_content_links()
            return {"name": filename, "size_bytes": length}
        finally:
            self.zim_upload_lock.release()

    def remove_zim_files(self, filenames: list[str]) -> dict:
        if not isinstance(filenames, list) or not filenames:
            raise ValueError("No ZIM files selected")

        self.sync_external_content_links()
        root = self.kiwix_library_dir()
        inventory = {item["name"]: item for item in self.list_installed_zims()}
        valid_names = {
            name
            for name in filenames
            if isinstance(name, str)
            and Path(name).name == name
            and name.endswith(".zim")
            and name in inventory
        }
        if not valid_names:
            raise ValueError("Selected ZIM files were not found")

        for name in valid_names:
            path = root / name
            if path.is_symlink():
                self.remove_managed_linked_content("zims", root, {name})
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue

        env = dict(os.environ)
        env.update(read_env_file(self.preppercache_env))
        env["PREPPERCACHE_ENV_FILE"] = str(self.preppercache_env)
        env["PREPPERCACHE_ENV_FILE"] = str(self.preppercache_env)
        subprocess.run(
            [str(self.repo_root / "scripts" / "rebuild_kiwix_library.sh")],
            cwd=self.repo_root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["systemctl", "restart", "preppercache-kiwix.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

        return self.content_status()

    def select_map_package(self, filename: str) -> dict:
        if not filename or Path(filename).name != filename or not filename.endswith(".pmtiles"):
            raise ValueError("Invalid PMTiles filename")

        packages = self.list_pmtiles_packages(valid_only=True)
        if filename not in packages:
            raise ValueError("Valid PMTiles package not found in map root")

        update_env_file(
            self.preppercache_env,
            {
                "PREPPERCACHE_MAP_PMTILES_FILE": filename,
            },
        )
        self.write_maps_runtime_config()
        return self.maps_status()

    def update_map_settings(self, payload: dict) -> dict:
        updates: dict[str, str] = {}

        filename = payload.get("filename")
        if filename is not None:
            if not filename or Path(filename).name != filename or not filename.endswith(".pmtiles"):
                raise ValueError("Invalid PMTiles filename")
            packages = self.list_pmtiles_packages(valid_only=True)
            if filename not in packages:
                raise ValueError("Valid PMTiles package not found in map root")
            updates["PREPPERCACHE_MAP_PMTILES_FILE"] = filename

        flavor = payload.get("flavor")
        if flavor is not None:
            if flavor not in {"light", "dark"}:
                raise ValueError("Invalid map flavor")
            updates["PREPPERCACHE_MAP_STYLE_FLAVOR"] = flavor

        install_dir = payload.get("install_dir")
        if install_dir is not None:
            updates["PREPPERCACHE_MAP_INSTALL_DIR"] = self.validate_install_destination("maps", str(install_dir).strip())

        if updates:
            update_env_file(self.preppercache_env, updates)
            self.write_maps_runtime_config()

        return self.maps_status()

    def remove_map_packages(self, filenames: list[str]) -> dict:
        if not isinstance(filenames, list) or not filenames:
            raise ValueError("No PMTiles packages selected")

        self.sync_external_content_links()
        root = self.maps_root()
        inventory = {item["name"]: item for item in self.pmtiles_inventory()}
        valid_names = {
            name for name in filenames
            if isinstance(name, str)
            and Path(name).name == name
            and name.endswith(".pmtiles")
            and name in inventory
        }

        if not valid_names:
            raise ValueError("Selected PMTiles packages were not found")

        for name in valid_names:
            path = root / name
            if path.is_symlink():
                self.remove_managed_linked_content("maps", root, {name})
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue

        remaining_valid = self.list_pmtiles_packages(valid_only=True)
        active = self.active_pmtiles_file()
        if active in valid_names:
            replacement = remaining_valid[0] if remaining_valid else "basemap.pmtiles"
            update_env_file(
                self.preppercache_env,
                {"PREPPERCACHE_MAP_PMTILES_FILE": replacement},
            )

        self.write_maps_runtime_config()
        return self.maps_status()

    def read_map_sync_log_tail(self, lines: int = 30) -> list[str]:
        return [
            line for line in read_last_lines(self.map_sync_log_file, lines * 8)
            if line.strip() and not line.lstrip().startswith("fetching chunks")
        ][-lines:]

    def recover_interrupted_map_sync(self) -> None:
        state = read_json(self.map_sync_state_file, {})
        if state.get("status") != "running":
            return
        operation = "build" if state.get("operation") in MAP_EXTRACTION_OPERATIONS else "sync"
        self.save_map_sync_state(
            {
                "status": "failed",
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "error": f"Map {operation} was interrupted. Start it again to finish installing the map.",
                "current_file": None,
                "cancel_requested": False,
            }
        )

    def load_map_sync_state(self) -> dict:
        state = read_json(
            self.map_sync_state_file,
            {
                "status": "idle",
                "started_at": None,
                "finished_at": None,
                "current_file": None,
                "current_index": None,
                "total_files": None,
                "progress_percent": 0,
                "error": None,
                "selected_files": [],
            },
        )
        state["log_tail"] = self.read_map_sync_log_tail()
        if state.get("operation") in MAP_EXTRACTION_OPERATIONS and state.get("status") == "running":
            extract_output = read_last_lines(
                self.map_sync_log_file, start_offset=state.get("current_log_offset", 0)
            )
            for line in reversed(extract_output):
                progress = re.search(r"fetching chunks\s+(\d{1,3})%", line)
                if progress:
                    state["current_percent"] = min(100, int(progress[1]))
                    break
        return state

    def save_map_sync_state(self, payload: dict) -> dict:
        current = read_json(self.map_sync_state_file, {})
        current.update(payload)
        with tempfile.NamedTemporaryFile(mode="w", dir=self.data_dir, delete=False) as handle:
            pending = Path(handle.name)
            try:
                handle.write(json.dumps(current, indent=2) + "\n")
                handle.close()
                pending.replace(self.map_sync_state_file)
            finally:
                pending.unlink(missing_ok=True)
        return self.load_map_sync_state()

    def map_build_source(self) -> str:
        return self.maps_env().get("PREPPERCACHE_MAP_BUILD_SOURCE", "").strip()

    def start_map_build(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Send the map name, bounding coordinates, and maximum zoom as a JSON object.")
        slug = payload.get("name")
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,47}[a-z0-9])?", slug):
            raise ValueError("Use a map name with up to 49 lowercase letters, numbers, or hyphens.")

        bounds = payload.get("bounds")
        if (
            not isinstance(bounds, list) or len(bounds) != 4
            or any(type(value) not in (float, int) or not -180 <= value <= 180 for value in bounds)
        ):
            raise ValueError("Enter four valid bounding coordinates.")
        west, south, east, north = bounds
        if not (-180 <= west < east <= 180 and -85.0511 <= south < north <= 85.0511):
            raise ValueError("Bounds must run west to east and south to north without crossing the date line.")

        max_zoom = payload.get("max_zoom", 13)
        if type(max_zoom) is not int or not 0 <= max_zoom <= 15:
            raise ValueError("Max zoom must be a whole number from 0 to 15.")

        return self.queue_map_builds([MapBuild(f"custom-{slug}.pmtiles", slug, bounds, max_zoom)], "build")

    @staticmethod
    def country_map_parts(country: dict) -> list[dict]:
        basename = f"country-{country['id'].lower()}"
        return [
            {**part, "filename": basename + (f"-{part['suffix']}" if part.get("suffix") else "") + ".pmtiles"}
            for part in [country, *country.get("additional_parts", [])]
        ]

    def country_map_catalog(self) -> dict:
        catalog = read_json(self.repo_root / "catalog" / "map-regions.json", {})
        installed = set(self.list_pmtiles_packages(valid_only=True))
        countries = []
        for country in catalog.get("countries", []):
            parts = self.country_map_parts(country)
            missing = [part for part in parts if part["filename"] not in installed]
            countries.append({
                **country,
                "estimated_bytes": sum(part["estimated_bytes"] for part in parts),
                "missing_bytes": sum(part["estimated_bytes"] for part in missing),
                "installed": not missing,
            })
        return {"bundles": catalog.get("bundles", []), "countries": countries}

    def start_country_map_builds(self, payload: dict) -> dict:
        ids = payload.get("country_ids") if isinstance(payload, dict) else None
        if not isinstance(ids, list) or not ids or any(not isinstance(code, str) for code in ids):
            raise ValueError("Select at least one country to install.")
        catalog = read_json(self.repo_root / "catalog" / "map-regions.json", {})
        countries = {country["id"]: country for country in catalog.get("countries", [])}
        if any(code not in countries for code in ids):
            raise ValueError("A selected country is missing from the map catalog.")
        installed = set(self.list_pmtiles_packages(valid_only=True))
        builds = [
            MapBuild(part["filename"], part["name"], part["bbox"], 15)
            for code in dict.fromkeys(ids)
            for part in self.country_map_parts(countries[code])
            if part["filename"] not in installed
        ]
        if not builds:
            raise ValueError("The selected country maps are already installed.")
        return self.queue_map_builds(builds, "countries")

    def regional_map_definitions(self) -> list[dict]:
        return read_json(self.repo_root / "catalog" / "us-map-regions.json", {}).get("regions", [])

    def regional_map_catalog(self) -> dict:
        installed = set(self.list_pmtiles_packages(valid_only=True))
        regions = []
        for region in self.regional_map_definitions():
            states = []
            for state in region["states"]:
                filename = f"state-{state['id']}.pmtiles"
                states.append({**state, "filename": filename, "installed": filename in installed})
            regions.append({**region, "states": states})
        return {"regions": regions}

    def start_regional_map_builds(self, payload: dict) -> dict:
        ids = payload.get("region_ids") if isinstance(payload, dict) else None
        if not isinstance(ids, list) or not ids or any(not isinstance(code, str) for code in ids):
            raise ValueError("Select at least one U.S. region to install.")
        regions = {region["id"]: region for region in self.regional_map_catalog()["regions"]}
        if any(region_id not in regions for region_id in ids):
            raise ValueError("A selected region is missing from the map catalog.")
        states = {
            state["id"]: state
            for region_id in dict.fromkeys(ids)
            for state in regions[region_id]["states"]
            if not state["installed"]
        }
        if not states:
            raise ValueError("The selected regional maps are already installed.")
        boundary_file = self.repo_root / "catalog" / "us-state-boundaries.geojson"
        try:
            boundaries = {
                feature["id"]: feature["geometry"]
                for feature in json.loads(boundary_file.read_text())["features"]
            }
            builds = [
                MapBuild(state["filename"], state["name"], state["bbox"], 15, boundaries[state_id])
                for state_id, state in states.items()
            ]
        except (OSError, ValueError, KeyError) as exc:
            raise RuntimeError("The bundled U.S. state boundaries could not be loaded.") from exc
        return self.queue_map_builds(builds, "regions")

    def queue_map_builds(self, builds: list[MapBuild], operation: str) -> dict:
        source = self.map_build_source()
        if source.startswith("https://"):
            parsed = urlparse(source)
            if not parsed.hostname or parsed.username or parsed.password or not parsed.path.endswith(".pmtiles"):
                raise RuntimeError("The map build source must be an HTTPS PMTiles URL without embedded credentials.")
        elif source and (not Path(source).is_absolute() or not Path(source).is_file() or not source.endswith(".pmtiles")):
            raise RuntimeError("The local map build source must be an existing absolute PMTiles file path.")

        binary = shutil.which("pmtiles")
        if not binary:
            raise RuntimeError("Install the Protomaps pmtiles CLI on the server to build maps.")

        with self.apply_lock, self.map_sync_lock:
            apply = self.load_apply_state()
            if apply.get("status") == "running" and apply.get("action") == "full":
                raise RuntimeError("Wait for setup to finish before building a map.")
            if self.load_map_sync_state().get("status") == "running":
                raise RuntimeError("Another map job is already running.")

            root = self.preferred_map_install_dir()
            self.validate_install_destination("maps", str(root))
            canonical_root = self.maps_root()
            for build in builds:
                filename = build.filename
                if any(path.exists() or path.is_symlink() for path in (root / filename, canonical_root / filename)):
                    raise ValueError(f"A map named {filename} already exists.")

            self.map_build_cancel.clear()
            self.map_sync_log_file.write_text("")
            self.save_map_sync_state({
                "status": "running", "operation": operation,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "finished_at": None, "cancel_requested": False, "current_file": builds[0].filename, "current_label": builds[0].label,
                "current_index": 1, "total_files": len(builds), "progress_percent": 0, "error": None,
                "selected_files": [build.filename for build in builds],
                "completed_files": [], "failed_files": [], "current_log_offset": 0,
            })
            self.map_sync_thread = threading.Thread(
                target=self.run_map_builds,
                args=(binary, source, builds, root, canonical_root),
                kwargs={"check_coverage": operation in {"countries", "regions"} and bool(source)},
                daemon=True,
            )
            self.map_sync_thread.start()
            return self.load_map_sync_state()

    def cancel_map_build(self) -> dict:
        with self.map_sync_lock:
            state = self.load_map_sync_state()
            if state.get("status") != "running" or state.get("operation") not in MAP_EXTRACTION_OPERATIONS:
                raise RuntimeError("There is no active map extraction to cancel.")
            self.map_build_cancel.set()
            return self.save_map_sync_state({"cancel_requested": True})

    def run_map_builds(
        self, binary: str, source: str, builds: list[MapBuild], root: Path, canonical_root: Path,
        *, check_coverage: bool = False,
    ) -> None:
        completed = []
        failed = []
        source_error = None
        if not source:
            try:
                source = self.find_daily_protomaps_source()
            except RuntimeError as exc:
                source_error = str(exc)
                failed = [{"file": build.filename, "name": build.label, "error": source_error} for build in builds]
        coverage = None
        if check_coverage:
            try:
                header = subprocess.check_output([binary, "show", "--header-json", source], timeout=30)
                coverage = json.loads(header)["bounds"]
            except (OSError, subprocess.SubprocessError, ValueError, KeyError):
                source_error = "Unable to read coverage from the configured map source."
                failed = [{"file": build.filename, "name": build.label, "error": source_error} for build in builds]
                source = ""
        for index, (filename, label, bounds, max_zoom, geometry) in enumerate(builds if source else [], 1):
            with self.map_sync_lock:
                if self.map_build_cancel.is_set():
                    break
                self.save_map_sync_state({
                    "current_file": filename, "current_label": label, "current_index": index,
                    "progress_percent": (index - 1) * 100 // len(builds),
                    "completed_files": completed, "failed_files": failed,
                    "current_log_offset": self.map_sync_log_file.stat().st_size,
                })
            west, south, east, north = bounds
            if coverage and not (
                coverage[0] <= west < east <= coverage[2] and coverage[1] <= south < north <= coverage[3]
            ):
                failure = "The configured basemap does not cover the selected area."
            else:
                failure = self.extract_map(
                    binary, source, bounds, max_zoom, root / filename, canonical_root / filename, geometry=geometry
                )
            if failure:
                failed.append({"file": filename, "name": label, "error": failure})
                with self.map_sync_log_file.open("a", encoding="utf-8") as log:
                    log.write(f"{label}: {failure}\n")
            elif (canonical_root / filename).is_file():
                completed.append(filename)

        with self.map_sync_lock:
            cancelled = self.map_build_cancel.is_set() and len(completed) + len(failed) < len(builds)
            error = failed[0]["error"] if len(builds) == 1 and failed else (
                f"{len(failed)} of {len(builds)} maps failed. See the map job log." if failed else None
            )
            error = source_error or error
            self.save_map_sync_state({
                "status": "cancelled" if cancelled else "failed" if failed else "succeeded",
                "cancel_requested": False, "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "current_file": None, "current_label": None, "error": error,
                "completed_files": completed, "failed_files": failed,
                "progress_percent": (len(completed) + len(failed)) * 100 // len(builds),
            })

    def find_daily_protomaps_source(self) -> str:
        today = datetime.now(timezone.utc).date()
        for days_ago in range(14):
            if self.map_build_cancel.is_set():
                return ""
            build_date = today - timedelta(days=days_ago)
            url = f"{PROTOMAPS_DAILY_ROOT}{build_date:%Y%m%d}.pmtiles"
            try:
                probe = request.Request(url, headers={"Range": "bytes=0-126", "User-Agent": "PrepperCache map builder"})
                with request.urlopen(probe, timeout=8) as response:
                    header = response.read(127) if response.status == 206 else b""
                    if len(header) == 127 and header.startswith(b"PMTiles\x03") and header[99] == 1:
                        with self.map_sync_log_file.open("a", encoding="utf-8") as log:
                            log.write(f"Using Protomaps daily build {build_date.isoformat()}\n")
                        return url
            except error.HTTPError as exc:
                if exc.code in (404, 410):
                    continue
                raise RuntimeError(f"Protomaps returned HTTP {exc.code} while checking daily builds.") from exc
            except (OSError, error.URLError) as exc:
                raise RuntimeError("Unable to reach Protomaps. Connect PrepperCache to the internet or configure a local basemap.") from exc
        raise RuntimeError("No Protomaps daily build was found in the last 14 days.")

    def extract_map(
        self, binary: str, source: str, bounds: list[float], max_zoom: int,
        destination: Path, canonical: Path, *, geometry: dict | None = None,
    ) -> str | None:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with self.map_sync_log_file.open("a", encoding="utf-8") as log, tempfile.TemporaryDirectory(
                prefix=".preppercache-map-build-", dir=destination.parent
            ) as staging:
                output = Path(staging) / destination.name
                if geometry is None:
                    bbox = ",".join(str(value) for value in bounds)
                    area_arg = f"--bbox={bbox}"
                    area_label = bbox
                else:
                    boundary = Path(staging) / "region.geojson"
                    boundary.write_text(json.dumps(geometry))
                    area_arg = f"--region={boundary}"
                    area_label = "the state boundary"
                log.write(f"Extracting {destination.name} at zoom 0–{max_zoom} within {area_label}\n")
                log.flush()
                command = [binary, "extract", source, str(output), area_arg, f"--maxzoom={max_zoom}"]
                deadline = time.monotonic() + 7200
                with subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT) as process:
                    while process.poll() is None:
                        if self.map_build_cancel.wait(0.25):
                            process.kill()
                            return None
                        if time.monotonic() >= deadline:
                            process.kill()
                            raise subprocess.TimeoutExpired(command, 7200)
                    if process.returncode:
                        raise subprocess.CalledProcessError(process.returncode, command)
                if self.map_build_cancel.is_set():
                    return None
                subprocess.run(
                    [binary, "verify", str(output)],
                    stdout=log, stderr=subprocess.STDOUT, check=True, timeout=600,
                )
                with output.open("rb") as handle:
                    header = handle.read(100)
                if not header.startswith(b"PMTiles\x03") or len(header) < 100 or header[99] != 1:
                    raise RuntimeError("The output must be a PMTiles v3 archive with MVT vector tiles.")
                if not int.from_bytes(header[72:80], "little"):
                    raise RuntimeError("The source contains no tiles for the selected area and zoom.")
                metadata_result = subprocess.run(
                    [binary, "show", "--metadata", str(output)],
                    stdout=subprocess.PIPE, stderr=log, check=True, timeout=60,
                )
                layers = json.loads(metadata_result.stdout).get("vector_layers", [])
                layer_ids = {layer.get("id") for layer in layers if isinstance(layer, dict)}
                if not {"earth", "landcover", "landuse", "water", "places"}.intersection(layer_ids):
                    raise RuntimeError("Use an archive with Protomaps basemap layers so PrepperCache can style the map.")
                if self.map_build_cancel.is_set():
                    return None
                if canonical != destination:
                    canonical.parent.mkdir(parents=True, exist_ok=True)
                if any(path.exists() or path.is_symlink() for path in (destination, canonical)):
                    raise RuntimeError("The map name was taken while the archive was being built.")
                output.chmod(0o644)
                output.replace(destination)
                if canonical != destination:
                    try:
                        canonical.symlink_to(destination.resolve())
                    except OSError:
                        destination.unlink()
                        raise
                if not inspect_pmtiles_file(self.maps_root() / self.active_pmtiles_file())["valid"]:
                    update_env_file(self.preppercache_env, {"PREPPERCACHE_MAP_PMTILES_FILE": destination.name})
                self.write_maps_runtime_config()
        except subprocess.TimeoutExpired:
            return "Map generation timed out. Try a smaller area or a lower maximum zoom."
        except subprocess.CalledProcessError:
            return "PMTiles could not build or verify this region. See the map job log for details."
        except Exception as exc:
            return str(exc)
        return None

    def save_setup(self, payload: dict) -> dict:
        mirror_updates = {}
        if "zim_mirror" in payload:
            mirror_updates["PREPPERCACHE_ZIM_CATALOG_URL"] = self.validate_zim_mirror(payload["zim_mirror"])
        wikipedia_option = payload.get("wikipedia_option", "top-mini")
        valid_wikipedia_options = {
            str(option.get("id", "")).strip()
            for option in self.wikipedia_catalog()["options"]
            if str(option.get("id", "")).strip()
        }
        if valid_wikipedia_options and wikipedia_option not in valid_wikipedia_options:
            raise ValueError("Invalid wikipedia_option")
        zim_profile = str(payload.get("zim_profile", "essential")).strip().lower()
        if zim_profile not in {"essential", "standard", "comprehensive"}:
            raise ValueError("Invalid zim_profile")
        content_install_dir = payload.get("content_install_dir")
        if content_install_dir is None:
            content_install_dir = str(self.preferred_zim_install_dir())
        content_install_dir = self.validate_install_destination("zims", str(content_install_dir).strip())
        if zim_profile == "essential":
            content_install_dir = str(self.kiwix_library_dir())
        wikipedia_install_dir = payload.get("wikipedia_install_dir")
        if wikipedia_install_dir is None:
            wikipedia_install_dir = str(self.preferred_wikipedia_install_dir())
        wikipedia_install_dir = self.validate_install_destination("zims", str(wikipedia_install_dir).strip())
        if wikipedia_option == "top-mini":
            wikipedia_install_dir = str(self.kiwix_library_dir())
        zim_mode = payload.get("zim_mode", "full")
        if zim_mode not in {"full", "quick-test", "custom"}:
            raise ValueError("Invalid zim_mode")
        install_kolibri = bool(payload.get("install_kolibri", False))
        install_ka_lite = bool(payload.get("install_ka_lite", False))
        ap_enabled = bool(payload.get("ap_enabled", False))
        setup_complete = bool(payload.get("setup_complete", True))
        update_env_file(
            self.install_profile_env,
            {
                "INSTALL_BASE_STACK": "1",
                "INSTALL_OPENSTREETMAPS": "1",
                "INSTALL_KIWIX": "1",
                "INSTALL_KOLIBRI": "1" if install_kolibri else "0",
                "INSTALL_KA_LITE": "1" if install_ka_lite else "0",
            },
        )
        update_env_file(
            self.preppercache_env,
            {
                **mirror_updates,
                "PREPPERCACHE_WIKIPEDIA_OPTION": wikipedia_option,
                "PREPPERCACHE_ZIM_PROFILE": zim_profile,
                "PREPPERCACHE_ZIM_INSTALL_DIR": content_install_dir,
                "PREPPERCACHE_WIKIPEDIA_INSTALL_DIR": wikipedia_install_dir,
                "PREPPERCACHE_AP_ENABLED": "1" if ap_enabled else "0",
                "PREPPERCACHE_ZIM_MODE": zim_mode,
            },
        )
        self.write_maps_runtime_config()

        state = {
            "setup_complete": setup_complete,
            "last_saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.state_file.write_text(json.dumps(state, indent=2) + "\n")
        return self.load_state()

    def status(self) -> dict:
        total, used, free = shutil.disk_usage("/")
        services = self.read_service_statuses(
            {
                "portal": "preppercache-portal.service",
                "kiwix": "preppercache-kiwix.service",
                "hostapd": "hostapd.service",
                "dnsmasq": "dnsmasq.service",
                "nginx": "nginx.service",
            }
        )
        return {
            "disk": {
                "total_gb": round(total / (1024 ** 3), 1),
                "used_gb": round(used / (1024 ** 3), 1),
                "free_gb": round(free / (1024 ** 3), 1),
            },
            "temperature_c": self.read_temperature(),
            "cpu": self.read_cpu_load(),
            "uptime": self.read_uptime(),
            "kiwix_url": f"http://{self.detect_primary_host()}:{read_env_file(self.preppercache_env).get('KIWIX_PORT', '8080')}/",
            "content_mode": read_env_file(self.preppercache_env).get(
                "PREPPERCACHE_ZIM_MODE", "full"
            ),
            "services": services,
        }

    def gpio_settings(self) -> dict:
        settings = {
            "shutdown_enabled": False,
            "shutdown_gpio": 17,
            "oled_enabled": False,
            "oled_address": "0x3c",
        }
        stored = read_json(self.data_dir / "gpio-settings.json", {})
        settings.update({key: stored[key] for key in settings if key in stored})
        return {
            **settings,
            "supported": Path("/etc/systemd/system/preppercache-gpio.service").is_file(),
            "i2c_available": Path("/dev/i2c-1").exists(),
            "shutdown_pins": [4, 5, 6, 12, 13, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27],
        }

    def save_gpio_settings(self, payload: dict) -> dict:
        current = self.gpio_settings()
        if not current["supported"]:
            raise ValueError("Install the Raspberry Pi GPIO service before configuring these controls.")
        if not isinstance(payload, dict):
            raise ValueError("Invalid GPIO settings.")
        pin = payload.get("shutdown_gpio")
        address = payload.get("oled_address")
        if type(pin) is not int or pin not in current["shutdown_pins"]:
            raise ValueError("Choose an available BCM GPIO pin.")
        if address not in ("0x3c", "0x3d"):
            raise ValueError("Choose OLED address 0x3C or 0x3D.")
        if any(type(payload.get(key)) is not bool for key in ("shutdown_enabled", "oled_enabled")):
            raise ValueError("Specify whether the button and OLED are enabled.")
        if payload["oled_enabled"] and not current["i2c_available"]:
            raise ValueError("Enable I²C in Raspberry Pi OS before turning on the OLED.")
        settings = {key: payload[key] for key in ("shutdown_enabled", "shutdown_gpio", "oled_enabled", "oled_address")}
        with tempfile.NamedTemporaryFile(mode="w", dir=self.data_dir, delete=False) as handle:
            pending = Path(handle.name)
            json.dump(settings, handle)
        try:
            pending.replace(self.data_dir / "gpio-settings.json")
        finally:
            pending.unlink(missing_ok=True)
        return {**current, **settings}

    def system_health(self) -> dict:
        storage = self.storage_health()
        return {
            "disk": storage["disk"],
            "storage": storage,
            "temperature_c": self.read_temperature(),
            "cpu": self.read_cpu_load(),
            "memory": self.read_memory_stats(),
            "uptime": self.read_uptime(),
            "services": self.system_service_health(),
        }

    def storage_health(self) -> dict:
        total, used, free = shutil.disk_usage("/")
        volumes = self.storage_volumes()
        targets = self.storage_targets(volumes)
        root_device = os.stat("/").st_dev
        counted_paths: set[Path] = set()
        root_content_bytes = 0
        for target in sorted(targets, key=lambda item: len(Path(item["resolved_path"]).parts)):
            path = Path(target["resolved_path"])
            if path in counted_paths or any(parent in counted_paths for parent in path.parents):
                continue
            try:
                if path.stat().st_dev != root_device:
                    continue
            except OSError:
                continue
            counted_paths.add(path)
            root_content_bytes += target["current_bytes"]
        return {
            "disk": {
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "total_gb": round(total / (1024 ** 3), 1),
                "used_gb": round(used / (1024 ** 3), 1),
                "free_gb": round(free / (1024 ** 3), 1),
            },
            "preppercache_bytes": min(root_content_bytes, used),
            "volumes": volumes,
            "targets": targets,
        }

    def storage_volumes(self) -> list[dict]:
        try:
            result = subprocess.run(
                [
                    "lsblk",
                    "-J",
                    "-b",
                    "-o",
                    "NAME,PATH,SIZE,TYPE,MOUNTPOINTS,RM,HOTPLUG,MODEL,LABEL,FSTYPE,UUID,TRAN,RO",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return []

        if result.returncode != 0:
            return []

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []

        volumes: list[dict] = []

        def visit(device: dict, inherited: dict[str, object] | None = None) -> None:
            inherited = inherited or {}
            children = device.get("children") or []
            mountpoints = [value for value in (device.get("mountpoints") or []) if value]
            device_type = str(device.get("type") or "").strip().lower()
            removable = (
                str(device.get("rm") or "0") == "1"
                or str(device.get("hotplug") or "0") == "1"
                or bool(inherited.get("removable"))
            )
            transport = str(device.get("tran") or inherited.get("transport") or "").strip().lower()
            external = removable or transport == "usb"
            keep = bool(mountpoints) or external or (device_type == "disk" and not children)
            if device_type in {"disk", "part"} and keep:
                mountpoint = mountpoints[0] if mountpoints else ""
                total_bytes = int(device.get("size") or 0)
                used_bytes = 0
                free_bytes = 0
                if mountpoint:
                    try:
                        disk_total, disk_used, disk_free = shutil.disk_usage(mountpoint)
                        total_bytes = disk_total
                        used_bytes = disk_used
                        free_bytes = disk_free
                    except OSError:
                        pass
                location = "external" if external else "internal"
                label = str(device.get("label") or "").strip()
                model = str(device.get("model") or inherited.get("model") or "").strip()
                display_name = label or model or str(device.get("name") or "").strip()
                filesystem = str(device.get("fstype") or "").strip()
                suggested_mountpoint = self.default_mountpoint_for_volume(
                    {
                        "name": str(device.get("name") or "").strip(),
                        "label": label,
                        "uuid": str(device.get("uuid") or "").strip(),
                    }
                )
                preppercache_layout = self.storage_layout_for_mount(mountpoint) if mountpoint else {}
                disk_sequence = ""
                if device_type == "disk" and transport == "usb":
                    try:
                        disk_sequence = (Path("/sys/class/block") / device["name"] / "diskseq").read_text().strip()
                    except (OSError, KeyError):
                        pass
                volumes.append(
                    {
                        "name": str(device.get("name") or "").strip(),
                        "path": str(device.get("path") or "").strip(),
                        "display_name": display_name,
                        "label": label,
                        "model": model,
                        "filesystem": filesystem,
                        "uuid": str(device.get("uuid") or "").strip(),
                        "location": location,
                        "mountpoint": mountpoint,
                        "mountpoints": mountpoints,
                        "mounted": bool(mountpoint),
                        "suggested_mountpoint": suggested_mountpoint,
                        "size_bytes": total_bytes,
                        "used_bytes": used_bytes,
                        "free_bytes": free_bytes,
                        "used_percent": round((used_bytes / total_bytes) * 100) if mountpoint and total_bytes else 0,
                        "is_partition": device_type == "part",
                        "transport": transport,
                        "parent_path": str(inherited.get("disk_path") or ""),
                        "can_mount": bool(filesystem and not mountpoint and (device_type == "part" or (external and not children))),
                        "can_unmount": bool(external and not children and mountpoint.startswith("/media/")),
                        "disk_sequence": disk_sequence,
                        "can_prepare": bool(disk_sequence and str(device.get("ro")).lower() in {"0", "false"}),
                        "preppercache_layout": preppercache_layout,
                    }
                )
            inherited_child = {
                "transport": transport,
                "disk_path": device.get("path") if device_type == "disk" else inherited.get("disk_path"),
                "model": str(device.get("model") or inherited.get("model") or "").strip(),
                "removable": removable,
            }
            for child in children:
                visit(child, inherited_child)

        for device in payload.get("blockdevices", []):
            visit(device)

        volumes.sort(
            key=lambda item: (
                0 if item.get("location") == "internal" else 1,
                0 if item.get("mounted") else 1,
                str(item.get("mountpoint") or item.get("path") or item.get("name")),
            )
        )
        return volumes

    def storage_layout_for_mount(self, mountpoint: str) -> dict[str, bool]:
        root = Path(mountpoint)
        return {
            "maps": (root / "maps").is_dir(),
            "library": (root / "library").is_dir(),
            "media": (root / "media").is_dir(),
        }

    def slugify_storage_name(self, value: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
        return cleaned.strip("-") or "drive"

    def default_mountpoint_for_volume(self, volume: dict) -> str:
        source = (
            str(volume.get("label") or "").strip()
            or str(volume.get("uuid") or "").strip()
            or str(volume.get("name") or "").strip()
        )
        slug = self.slugify_storage_name(source)
        return f"/media/preppercache/{slug}"

    def storage_volume_by_device(self, device_path: str) -> dict | None:
        for volume in self.storage_volumes():
            if str(volume.get("path")) == device_path:
                return volume
        return None

    def run_storage_command(self, command: list[str], error_hint: str, *, input_text: str | None = None) -> None:
        try:
            result = subprocess.run(
                command,
                input=input_text,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(error_hint) from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip() or error_hint
            raise RuntimeError(message)

    def validate_storage_label(self, value: str) -> str:
        label = str(value or "").strip()
        if not label:
            raise ValueError("Enter a drive name.")
        if len(label) > 16:
            raise ValueError("Drive names must be 16 characters or fewer.")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", label):
            raise ValueError("Drive names may only use letters, numbers, hyphens, and underscores.")
        return label

    def storage_label_command(self, volume: dict, label: str) -> list[str]:
        filesystem = str(volume.get("filesystem") or "").strip().lower()
        device_path = str(volume.get("path") or "").strip()
        if filesystem in {"ext2", "ext3", "ext4"}:
            return ["e2label", device_path, label]
        if filesystem in {"vfat", "fat", "fat16", "fat32"}:
            return ["fatlabel", device_path, label]
        if filesystem == "exfat":
            return ["exfatlabel", device_path, label]
        if filesystem in {"ntfs", "ntfs3"}:
            return ["ntfslabel", device_path, label]
        raise ValueError(f"Renaming is not supported for {filesystem or 'this filesystem'} drives.")

    def update_storage_install_paths_for_mount_change(self, old_mountpoint: str, new_mountpoint: str) -> None:
        old_mountpoint = str(old_mountpoint or "").strip()
        new_mountpoint = str(new_mountpoint or "").strip()
        if not old_mountpoint or not new_mountpoint or old_mountpoint == new_mountpoint:
            return

        preferred_zim_dir = self.preferred_zim_install_dir()
        preferred_map_dir = self.preferred_map_install_dir()
        updates: dict[str, str] = {}
        if str(preferred_zim_dir).startswith(f"{old_mountpoint}/"):
            suffix = str(preferred_zim_dir)[len(old_mountpoint):]
            updates["PREPPERCACHE_ZIM_INSTALL_DIR"] = f"{new_mountpoint}{suffix}"
        if str(preferred_map_dir).startswith(f"{old_mountpoint}/"):
            suffix = str(preferred_map_dir)[len(old_mountpoint):]
            updates["PREPPERCACHE_MAP_INSTALL_DIR"] = f"{new_mountpoint}{suffix}"
        if updates:
            update_env_file(self.preppercache_env, updates)

    def mount_storage_volume(self, payload: dict) -> dict:
        device_path = str(payload.get("device_path", "")).strip()
        if not device_path:
            raise ValueError("Choose a drive to mount.")
        volume = self.storage_volume_by_device(device_path)
        if not volume:
            raise ValueError("That drive was not found.")
        if volume.get("mounted"):
            return self.storage_health()
        if not volume.get("can_mount"):
            raise ValueError("Choose a drive or partition with a readable filesystem.")
        mountpoint = str(volume.get("suggested_mountpoint") or "").strip()
        if not mountpoint:
            raise RuntimeError("Unable to determine a mount point for this drive.")
        Path(mountpoint).mkdir(parents=True, exist_ok=True)
        self.run_storage_command(["mount", device_path, mountpoint], "Unable to mount the selected drive.")
        self.sync_external_content_links()
        return self.storage_health()

    def unmount_storage_volume(self, payload: dict) -> dict:
        device_path = str(payload.get("device_path", "")).strip()
        if not device_path:
            raise ValueError("Choose a drive to unmount.")
        volume = self.storage_volume_by_device(device_path)
        if not volume:
            raise ValueError("That drive was not found.")
        if volume.get("location") != "external":
            raise ValueError("Only external drives can be unmounted from PrepperCache.")
        if not volume.get("mounted"):
            return self.storage_health()

        mountpoint = str(volume.get("mountpoint") or "").strip()
        if not mountpoint or not mountpoint.startswith("/media/"):
            raise ValueError("This drive is not mounted at a removable-storage path.")

        preferred_zim_dir = self.preferred_zim_install_dir()
        preferred_map_dir = self.preferred_map_install_dir()
        updates: dict[str, str] = {}
        if str(preferred_zim_dir).startswith(f"{mountpoint}/"):
            updates["PREPPERCACHE_ZIM_INSTALL_DIR"] = str(self.kiwix_library_dir())
        if str(preferred_map_dir).startswith(f"{mountpoint}/"):
            updates["PREPPERCACHE_MAP_INSTALL_DIR"] = str(self.maps_root())
        if updates:
            update_env_file(self.preppercache_env, updates)

        self.run_storage_command(["umount", device_path], "Unable to unmount the selected drive.")
        self.sync_external_content_links()
        self.write_maps_runtime_config()

        env = dict(os.environ)
        env.update(read_env_file(self.preppercache_env))
        env["PREPPERCACHE_ENV_FILE"] = str(self.preppercache_env)
        env["PREPPERCACHE_ENV_FILE"] = str(self.preppercache_env)
        subprocess.run(
            [str(self.repo_root / "scripts" / "rebuild_kiwix_library.sh")],
            cwd=self.repo_root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["systemctl", "restart", "preppercache-kiwix.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return self.storage_health()

    def storage_prepare_status(self) -> dict:
        with self.storage_prepare_lock:
            return dict(self.storage_prepare_job)

    def check_usb_disk(self, device: str, sequence: str) -> list[dict]:
        volumes = self.storage_volumes()
        disk = next((volume for volume in volumes if volume["path"] == device), None)
        if not disk or not disk["can_prepare"] or disk["disk_sequence"] != sequence:
            raise ValueError("The selected USB drive changed or is no longer available. Refresh Storage and try again.")
        members = [volume for volume in volumes if volume["path"] == device or volume["parent_path"] == device]
        for volume in members:
            holders = Path("/sys/class/block") / volume["name"] / "holders"
            if any(holders.iterdir()):
                raise ValueError("This drive is in use by another block device and cannot be erased.")
            if any(not mount.startswith("/media/") for mount in volume["mountpoints"]):
                raise ValueError("This drive has a system or active mount. Unmount it outside PrepperCache first.")
        return members

    def start_storage_prepare(self, payload: dict) -> dict:
        device = str(payload.get("device_path") or "")
        sequence = str(payload.get("disk_sequence") or "")
        if not device or payload.get("confirm_text") != f"ERASE {device}":
            raise ValueError(f'Type "ERASE {device}" to confirm erasing the whole USB drive.')
        label = self.validate_storage_label(payload.get("label", "PrepperCacheDATA"))
        with self.storage_prepare_lock, self.file_transfer_lock:
            if self.storage_prepare_job.get("status") == "running":
                raise ValueError("A drive is already being prepared.")
            self.require_file_manager_idle()
            if any(job().get("status") == "running" for job in (self.load_apply_state, self.load_map_sync_state)):
                raise ValueError("Wait for content and map jobs to finish before preparing a drive.")
            self.check_usb_disk(device, sequence)
            for command in ("sfdisk", "udevadm", "mkfs.ext4", "mount", "umount"):
                if not shutil.which(command):
                    raise RuntimeError(f"Drive preparation requires {command}.")
            self.storage_prepare_job = {
                "status": "running", "device": device, "label": label,
                "step": f"Checking {device} before erasing all partitions.",
            }
            worker = threading.Thread(target=self.run_storage_prepare, args=(device, sequence, label), daemon=True)
            try:
                worker.start()
            except RuntimeError:
                self.storage_prepare_job = {}
                raise
            return dict(self.storage_prepare_job)

    def run_storage_prepare(self, device: str, sequence: str, label: str) -> None:
        def report(step: str, status: str = "running", **extra: str) -> None:
            with self.storage_prepare_lock:
                self.storage_prepare_job.update(step=step, status=status, **extra)
                if status != "running":
                    self.storage_prepare_job["finished_at"] = datetime.now(timezone.utc).isoformat()

        try:
            members = self.check_usb_disk(device, sequence)
            mountpoint = Path(self.default_mountpoint_for_volume({"label": label}))
            existing_mounts = {point for volume in members for point in volume["mountpoints"]}
            if mountpoint.is_symlink() or (mountpoint.exists() and (
                not mountpoint.is_dir() or (str(mountpoint) not in existing_mounts and
                (mountpoint.is_mount() or any(mountpoint.iterdir())))
            )):
                raise ValueError("A drive or folder already uses this drive name. Choose a different name.")
            if existing_mounts:
                report("Unmounting existing partitions.")
                for point in sorted(existing_mounts, reverse=True):
                    self.run_storage_command(["umount", "--", point], "Could not unmount an existing partition.")
            if any(volume["mounted"] for volume in self.check_usb_disk(device, sequence)):
                raise ValueError("The drive is still mounted. Unmount it before trying again.")
            updates = {}
            for key, current, fallback in (
                ("PREPPERCACHE_ZIM_INSTALL_DIR", self.preferred_zim_install_dir(), self.kiwix_library_dir()),
                ("PREPPERCACHE_MAP_INSTALL_DIR", self.preferred_map_install_dir(), self.maps_root()),
            ):
                if any(current.is_relative_to(point) for point in existing_mounts):
                    updates[key] = str(fallback)
            if updates:
                update_env_file(self.preppercache_env, updates)
                self.write_maps_runtime_config()

            report("Replacing the partition table with one Linux partition.")
            self.run_storage_command(
                ["sfdisk", "--lock=nonblock", "--wipe=always", "--wipe-partitions=always", "--", device],
                "Could not create a partition on the USB drive.",
                input_text="label: gpt\n\n, , linux\n",
            )
            self.run_storage_command(["udevadm", "settle", "--timeout=10"], "The new partition did not become available.")
            partitions = [volume for volume in self.check_usb_disk(device, sequence) if volume["is_partition"]]
            if len(partitions) != 1 or partitions[0]["mounted"]:
                raise RuntimeError("Expected one unmounted partition after replacing the partition table.")
            partition = partitions[0]["path"]
            report(f"Formatting {partition} as ext4.")
            self.run_storage_command(["mkfs.ext4", "-F", "-L", label, partition], "Could not format the new partition.")

            report("Mounting the drive and creating library, maps, and media folders.")
            self.check_usb_disk(device, sequence)
            mountpoint.mkdir(parents=True, exist_ok=True)
            if mountpoint.is_symlink() or mountpoint.is_mount() or any(mountpoint.iterdir()):
                raise RuntimeError("The new drive could not be mounted because its mount folder is already in use.")
            self.run_storage_command(["mount", "--", partition, str(mountpoint)], "Could not mount the prepared drive.")
            for directory in ("library", "maps", "media"):
                (mountpoint / directory).mkdir(exist_ok=True)
            (mountpoint / "PrepperCache-DRIVE.txt").write_text(
                "This drive was prepared by PrepperCache.\n\n"
                "library/ offline knowledge library content\n"
                "maps/    offline map archives\n"
                "media/   extra media files\n"
            )
            self.sync_external_content_links()
            report(f"{label} is mounted and ready for library and maps use.", "succeeded", partition=partition)
        except (OSError, RuntimeError, ValueError) as exc:
            report(str(exc), "failed")

    def rename_storage_volume(self, payload: dict) -> dict:
        device_path = str(payload.get("device_path", "")).strip()
        if not device_path:
            raise ValueError("Choose a drive to rename.")
        volume = self.storage_volume_by_device(device_path)
        if not volume:
            raise ValueError("That drive was not found.")
        if volume.get("location") != "external":
            raise ValueError("Only external drives can be renamed from PrepperCache.")
        if not volume.get("is_partition"):
            raise ValueError("Choose a partition instead of the whole disk.")
        if not volume.get("filesystem"):
            raise ValueError("This drive is not formatted yet. Prepare it first.")
        label = self.validate_storage_label(payload.get("label", ""))
        command = self.storage_label_command(volume, label)
        old_mountpoint = str(volume.get("mountpoint") or "").strip()
        self.run_storage_command(command, "Unable to rename the selected drive.")
        if old_mountpoint and old_mountpoint.startswith("/media/preppercache/"):
            refreshed_volume = self.storage_volume_by_device(device_path) or volume
            new_mountpoint = str(refreshed_volume.get("suggested_mountpoint") or "").strip()
            if new_mountpoint and new_mountpoint != old_mountpoint:
                self.run_storage_command(["umount", device_path], "Unable to remount the renamed drive.")
                Path(new_mountpoint).mkdir(parents=True, exist_ok=True)
                self.run_storage_command(["mount", device_path, new_mountpoint], "Unable to remount the renamed drive.")
                self.update_storage_install_paths_for_mount_change(old_mountpoint, new_mountpoint)
                self.sync_external_content_links()
                self.write_maps_runtime_config()
        return self.storage_health()

    def storage_targets(self, volumes: list[dict]) -> list[dict]:
        targets = [
            ("maps", "Maps", self.maps_root()),
            ("library", "Library", self.kiwix_library_dir()),
        ]
        resolved_targets = [
            self.describe_storage_target(target_id, label, path, volumes)
            for target_id, label, path in targets
        ]
        for volume in volumes:
            mountpoint = str(volume.get("mountpoint") or "").strip()
            if not mountpoint:
                continue
            layout = volume.get("preppercache_layout") or {}
            for directory_name, label in (
                ("maps", "Maps Folder"),
                ("library", "Library Folder"),
                ("media", "Media Folder"),
            ):
                if not layout.get(directory_name):
                    continue
                resolved_targets.append(
                    self.describe_storage_target(
                        f"available-{directory_name}-{volume.get('name')}",
                        label,
                        Path(mountpoint) / directory_name,
                        volumes,
                        role="available",
                        source_volume=volume,
                    )
                )
        return resolved_targets

    def describe_storage_target(
        self,
        target_id: str,
        label: str,
        path: Path,
        volumes: list[dict],
        role: str = "active",
        source_volume: dict | None = None,
    ) -> dict:
        resolved_path = path.resolve(strict=False)
        current_bytes = self.directory_size_bytes(path)
        volume = source_volume or self.volume_for_path(resolved_path, volumes)
        symlink_target = ""
        if path.is_symlink():
            try:
                symlink_target = os.readlink(path)
            except OSError:
                symlink_target = ""
        return {
            "id": target_id,
            "label": label,
            "configured_path": str(path),
            "resolved_path": str(resolved_path),
            "exists": path.exists(),
            "is_symlink": path.is_symlink(),
            "symlink_target": symlink_target,
            "current_bytes": current_bytes,
            "role": role,
            "volume": volume,
        }

    def directory_size_bytes(self, path: Path) -> int:
        path = path.resolve()
        try:
            path_stat = path.stat()
        except OSError:
            return 0
        if path.is_file():
            return path_stat.st_blocks * 512
        total = 0
        seen_inodes: set[int] = set()
        for root, directories, files in os.walk(path):
            root_path = Path(root)
            for name in directories + files:
                try:
                    entry = (root_path / name).lstat()
                except OSError:
                    if name in directories:
                        directories.remove(name)
                    continue
                if entry.st_dev != path_stat.st_dev:
                    if name in directories:
                        directories.remove(name)
                    continue
                if entry.st_ino not in seen_inodes:
                    seen_inodes.add(entry.st_ino)
                    total += entry.st_blocks * 512
        return total

    def volume_for_path(self, path: Path, volumes: list[dict]) -> dict | None:
        resolved = os.path.realpath(str(path))
        best_match: dict | None = None
        best_length = -1
        for volume in volumes:
            mountpoint = str(volume.get("mountpoint") or "").strip()
            if not mountpoint:
                continue
            mount_real = os.path.realpath(mountpoint)
            if resolved == mount_real or resolved.startswith(mount_real.rstrip("/") + "/"):
                if len(mount_real) > best_length:
                    best_match = volume
                    best_length = len(mount_real)
        return best_match

    def read_service_enabled_statuses(self, services: dict[str, str]) -> dict[str, str]:
        try:
            result = subprocess.run(
                ["systemctl", "is-enabled", *services.values()],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return {name: "unknown" for name in services}

        lines = [line.strip() or "unknown" for line in result.stdout.splitlines()]
        values = list(services.keys())
        resolved: dict[str, str] = {}
        for index, name in enumerate(values):
            resolved[name] = lines[index] if index < len(lines) else "unknown"
        return resolved

    def access_point_status(self) -> dict:
        env = read_env_file(self.preppercache_env)
        service_units = {
            "network": "preppercache-ap-network.service",
            "hostapd": "hostapd.service",
            "dnsmasq": "dnsmasq.service",
        }
        services = self.read_service_statuses(service_units)
        enabled_services = self.read_service_enabled_statuses(service_units)
        configured_enabled = env.get("PREPPERCACHE_AP_ENABLED", "0") == "1"
        running = (
            services.get("network") == "active"
            and services.get("hostapd") == "active"
            and services.get("dnsmasq") == "active"
        )
        return {
            "enabled": configured_enabled,
            "interface": env.get("PREPPERCACHE_AP_INTERFACE", "wlan0"),
            "ssid": env.get("PREPPERCACHE_AP_SSID", "PrepperCache"),
            "passphrase": env.get("PREPPERCACHE_AP_PASSPHRASE", "prepperlocal"),
            "country": env.get("PREPPERCACHE_AP_COUNTRY", "US"),
            "channel": env.get("PREPPERCACHE_AP_CHANNEL", "6"),
            "address": env.get("PREPPERCACHE_AP_ADDRESS", "10.10.10.10"),
            "netmask": env.get("PREPPERCACHE_AP_NETMASK", "255.255.255.0"),
            "cidr": env.get("PREPPERCACHE_AP_CIDR", "24"),
            "dhcp_start": env.get("PREPPERCACHE_AP_DHCP_START", "10.10.10.50"),
            "dhcp_end": env.get("PREPPERCACHE_AP_DHCP_END", "10.10.10.150"),
            "lease": env.get("PREPPERCACHE_AP_DHCP_LEASE", "12h"),
            "services": services,
            "enabled_services": enabled_services,
            "running": running,
        }

    def update_access_point_settings(self, payload: dict) -> dict:
        updates: dict[str, str] = {}

        field_map = {
            "interface": "PREPPERCACHE_AP_INTERFACE",
            "ssid": "PREPPERCACHE_AP_SSID",
            "passphrase": "PREPPERCACHE_AP_PASSPHRASE",
            "country": "PREPPERCACHE_AP_COUNTRY",
            "channel": "PREPPERCACHE_AP_CHANNEL",
            "address": "PREPPERCACHE_AP_ADDRESS",
            "netmask": "PREPPERCACHE_AP_NETMASK",
            "cidr": "PREPPERCACHE_AP_CIDR",
            "dhcp_start": "PREPPERCACHE_AP_DHCP_START",
            "dhcp_end": "PREPPERCACHE_AP_DHCP_END",
            "lease": "PREPPERCACHE_AP_DHCP_LEASE",
        }

        for payload_key, env_key in field_map.items():
            if payload_key not in payload:
                continue
            value = str(payload.get(payload_key, "")).strip()
            if not value:
                raise ValueError(f"Invalid access point setting: {payload_key}")
            updates[env_key] = value

        if "country" in payload:
            country = updates["PREPPERCACHE_AP_COUNTRY"].upper()
            if len(country) != 2 or not country.isalpha():
                raise ValueError("Country must be a 2-letter code")
            updates["PREPPERCACHE_AP_COUNTRY"] = country

        if "channel" in payload:
            channel = updates["PREPPERCACHE_AP_CHANNEL"]
            if not channel.isdigit():
                raise ValueError("Channel must be numeric")

        if "cidr" in payload:
            cidr = updates["PREPPERCACHE_AP_CIDR"]
            if not cidr.isdigit():
                raise ValueError("CIDR must be numeric")
            cidr_value = int(cidr)
            if cidr_value < 1 or cidr_value > 32:
                raise ValueError("CIDR must be between 1 and 32")

        if "passphrase" in payload:
            passphrase = updates["PREPPERCACHE_AP_PASSPHRASE"]
            if len(passphrase) < 8 or len(passphrase) > 63:
                raise ValueError("Passphrase must be between 8 and 63 characters")

        if "enabled" in payload:
            updates["PREPPERCACHE_AP_ENABLED"] = "1" if bool(payload.get("enabled")) else "0"

        if updates:
            update_env_file(self.preppercache_env, updates)

        return self.access_point_status()

    def run_access_point_config(self) -> str:
        script = self.repo_root / "scripts" / "configure_access_point.sh"
        env = os.environ.copy()
        env["PREPPERCACHE_ENV_FILE"] = str(self.preppercache_env)
        env["PREPPERCACHE_ENV_FILE"] = str(self.preppercache_env)
        try:
            result = subprocess.run(
                [str(script)],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Access point configuration script is unavailable") from exc

        output = (result.stdout or "").strip()
        errors = (result.stderr or "").strip()
        if result.returncode != 0:
            raise RuntimeError(errors or output or "Unable to apply access point settings")
        return output

    def apply_access_point_action(self, payload: dict) -> dict:
        action = payload.get("action", "save")
        if action not in {"save", "start", "stop", "apply"}:
            raise ValueError("Invalid access point action")

        updated_payload = dict(payload)
        if action == "start":
            updated_payload["enabled"] = True
        elif action == "stop":
            updated_payload["enabled"] = False

        state = self.update_access_point_settings(updated_payload)
        message = "Access point settings saved."

        if action in {"start", "stop", "apply"}:
            output = self.run_access_point_config()
            state = self.access_point_status()
            if action == "start":
                message = "Access point started."
            elif action == "stop":
                message = "Access point stopped."
            else:
                message = "Access point configuration applied."
            state["message"] = output or message
        else:
            state["message"] = message

        return state

    def access_point_clients(self) -> dict:
        ap = self.access_point_status()
        leases_path = Path("/var/lib/misc/dnsmasq.leases")
        leases_by_mac: dict[str, dict[str, object]] = {}

        if leases_path.exists():
            for line in leases_path.read_text().splitlines():
                parts = line.split()
                if len(parts) < 5:
                    continue
                expires_at, mac, ip_address, hostname, client_id = parts[:5]
                leases_by_mac[mac.lower()] = {
                    "mac": mac.lower(),
                    "ip": ip_address,
                    "hostname": "" if hostname == "*" else hostname,
                    "lease_expires_at": expires_at,
                    "client_id": "" if client_id == "*" else client_id,
                }

        stations: list[dict[str, object]] = []
        try:
            result = subprocess.run(
                ["iw", "dev", ap["interface"], "station", "dump"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            result = None

        current: dict[str, object] | None = None
        if result and result.returncode == 0:
            for raw_line in result.stdout.splitlines():
                line = raw_line.strip()
                if line.startswith("Station "):
                    if current:
                        stations.append(current)
                    mac = line.split()[1].lower()
                    lease = leases_by_mac.get(mac, {})
                    current = {
                        "mac": mac,
                        "ip": lease.get("ip"),
                        "hostname": lease.get("hostname"),
                        "lease_expires_at": lease.get("lease_expires_at"),
                        "connected": True,
                    }
                    continue
                if not current or ":" not in line:
                    continue
                key, value = [part.strip() for part in line.split(":", 1)]
                if key == "connected time":
                    current["connected_time_seconds"] = int(value.split()[0])
                elif key == "rx bytes":
                    current["rx_bytes"] = int(value)
                elif key == "tx bytes":
                    current["tx_bytes"] = int(value)
                elif key == "rx packets":
                    current["rx_packets"] = int(value)
                elif key == "tx packets":
                    current["tx_packets"] = int(value)
                elif key == "rx bitrate":
                    current["rx_bitrate"] = value
                elif key == "tx bitrate":
                    current["tx_bitrate"] = value
                elif key == "signal":
                    current["signal"] = value
            if current:
                stations.append(current)

        connected_macs = {item["mac"] for item in stations}
        for mac, lease in leases_by_mac.items():
            if mac in connected_macs:
                continue
            stations.append(
                {
                    "mac": mac,
                    "ip": lease.get("ip"),
                    "hostname": lease.get("hostname"),
                    "lease_expires_at": lease.get("lease_expires_at"),
                    "connected": False,
                }
            )

        stations.sort(key=lambda item: (not bool(item.get("connected")), str(item.get("hostname") or item.get("ip") or item["mac"])))
        return {
            "interface": ap["interface"],
            "running": ap["running"],
            "clients": stations,
        }

    def content_update_settings(self) -> dict:
        return read_json(self.content_update_file, {"enabled": False, "next_at": None, "last_attempt_at": None})

    def write_content_update_settings(self, settings: dict) -> dict:
        pending = self.content_update_file.with_suffix(".tmp")
        pending.write_text(json.dumps(settings, indent=2) + "\n")
        pending.replace(self.content_update_file)
        return settings

    def set_automatic_content_updates(self, enabled: object) -> dict:
        if type(enabled) is not bool:
            raise ValueError("Choose whether automatic updates are enabled.")
        with self.apply_lock:
            settings = self.content_update_settings()
            if settings["enabled"] != enabled:
                settings.update(enabled=enabled, next_at=time.time() + CONTENT_UPDATE_INTERVAL if enabled else None)
                self.write_content_update_settings(settings)
            return settings

    def run_content_update_schedule(self) -> None:
        while True:
            try:
                self.recover_interrupted_apply()
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"Content recovery deferred: {exc}", flush=True)
            settings = self.content_update_settings()
            if (settings.get("enabled") and (settings.get("next_at") or 0) <= time.time()
                    and read_json(self.state_file, {}).get("setup_complete")):
                try:
                    self.start_apply("update-content", scheduled=True)
                except (OSError, RuntimeError) as exc:
                    print(f"Automatic content update deferred: {exc}", flush=True)
            time.sleep(60)

    def prepare_content_update_manifest(self) -> Path:
        version_pattern = re.compile(r"(.+)_([0-9]{4}-(?:0[1-9]|1[0-2]))\.zim")
        installed_versions = {}
        for archive in self.list_installed_zims():
            match = version_pattern.fullmatch(archive["name"])
            if match:
                edition, version = match.groups()
                installed_versions[edition] = max(version, installed_versions.get(edition, ""))
        selected = {}
        if installed_versions:
            catalog = self.build_kiwix_catalog_payload(cache=False)
            for item in catalog["items"]:
                match = version_pattern.fullmatch(item["name"])
                if not match:
                    continue
                edition, version = match.groups()
                if edition in installed_versions and version > installed_versions[edition]:
                    if edition not in selected or version > selected[edition][0]:
                        selected[edition] = (version, catalog["source"]["root_url"] + parse.quote(item["path"], safe="/%"))
        manifest = self.data_dir / "content-update-urls.txt"
        manifest.write_text("".join(url + "\n" for _, url in selected.values()))
        return manifest

    def troubleshooting_report(self) -> str:
        apply = self.load_apply_state()
        maps = self.load_map_sync_state()
        sections = [
            "PrepperCache Troubleshooting Logs",
            time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            f"Content job: {apply.get('status')}",
            f"Map job: {maps.get('status')}",
            "",
            "Content and setup",
            *self.read_log_tail(300),
            "",
            "Map downloads",
            *self.read_map_sync_log_tail(300),
        ]
        # Strip credentials and query strings from URLs that appear in logs.
        report = re.sub(r"(https?://)([^/\s]+@)", r"\1[redacted]@", "\n".join(sections))
        report = re.sub(r"(https?://[^\s?]+)\?[^\s]*", r"\1?[redacted]", report)
        return report + "\n"

    def rejected_zim_names(self, log_tail: list[str]) -> list[str]:
        library_xml = self.maps_env().get("KIWIX_LIBRARY_XML", str(self.data_dir / "kiwix" / "library.xml"))
        try:
            report = Path(f"{library_xml}.rejected").read_bytes()
        except FileNotFoundError:
            # Older installs only recorded rejected archives in the job log.
            names = []
            for line in log_tail:
                if line.startswith("Kiwix could not index: "):
                    names.append(line.removeprefix("Kiwix could not index: "))
                elif match := re.fullmatch(r"Cannot add zim (.+) to the library\.", line):
                    names.append(Path(match[1]).name)
            return sorted(set(names))
        return sorted({os.fsdecode(name) for name in report.split(b"\0") if name})

    def load_apply_state(self) -> dict:
        state = read_json(
            self.apply_state_file,
            {
                "status": "idle",
                "action": "full",
                "total_steps": None,
                "current_step_index": None,
                "started_at": None,
                "finished_at": None,
                "step": None,
                "exit_code": None,
                "error": None,
            },
        )
        state["log_tail"] = self.read_log_tail()
        state["rejected_zim_names"] = self.rejected_zim_names(state["log_tail"])
        state.update(self.parse_apply_progress(state))
        step = str(state.get("step") or "")
        state["downloads_active"] = bool(
            state.get("status") == "running"
            and step in ZIM_DOWNLOAD_STEPS
        )
        state["can_leave_to_home"] = bool(
            state.get("status") == "succeeded"
            or state.get("downloads_active")
        )
        state["can_retry_download"] = (
            state.get("action") in CONTENT_DOWNLOAD_ACTIONS
            and (
                state.get("status") == "running" and not (self.apply_thread and self.apply_thread.is_alive())
                or state.get("status") in {"failed", "succeeded"}
                and (
                    bool(state["failed_download_names"])
                    or step in ZIM_DOWNLOAD_STEPS
                    and "Kiwix could not index any ZIM files. Kept the existing library index." not in state["log_tail"]
                )
            )
        )
        return state

    def save_apply_state(self, payload: dict) -> dict:
        current = read_json(self.apply_state_file, {})
        current.update(payload)
        with tempfile.NamedTemporaryFile(mode="w", dir=self.data_dir, delete=False) as handle:
            pending = Path(handle.name)
            json.dump(current, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            pending.replace(self.apply_state_file)
        finally:
            pending.unlink(missing_ok=True)
        return self.load_apply_state()

    def read_log_tail(self, lines: int = 30) -> list[str]:
        return [
            line for line in read_last_lines(self.apply_log_file, lines * 6)
            if line.strip() and not line.startswith("PROGRESS_DOWNLOAD_")
        ][-lines:]

    def launch_apply(
        self,
        action: str,
        *,
        clear_log: bool,
        started_at: str | None = None,
        resumed: bool = False,
        start_index: int = 1,
        retry_names: list[str] | None = None,
        automatic_retries: int = 0,
    ) -> dict:
        if clear_log:
            self.apply_log_file.write_text("")
        else:
            compact_log_file(self.apply_log_file)
            with self.apply_log_file.open("a", encoding="utf-8") as log_handle:
                log_handle.write(
                    "\n== Resuming interrupted apply workflow after restart ==\n"
                )

        self.save_apply_state(
            {
                "status": "running",
                "action": action,
                "total_steps": len(self.commands_for_action(action)),
                "current_step_index": start_index,
                "retry_download_names": retry_names or [],
                "automatic_retries": automatic_retries,
                "started_at": started_at
                or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "finished_at": None,
                "step": "Resuming interrupted apply workflow"
                if resumed
                else "Starting apply workflow",
                "exit_code": None,
                "error": None,
                "resumed_from_interruption": resumed,
                "result_message": None,
            }
        )
        self.apply_thread = threading.Thread(
            target=self.run_apply_workflow,
            args=(action, start_index, retry_names, resumed),
            daemon=True,
        )
        self.apply_thread.start()
        return self.load_apply_state()

    def start_apply(self, action: str = "full", *, scheduled: bool = False) -> dict:
        if action not in {"full", "refresh-content", "rebuild-library", "download-extra-content", "update-content"}:
            raise ValueError("Invalid apply action")

        with self.apply_lock, self.map_sync_lock:
            state = self.load_apply_state()
            if state.get("status") == "running":
                raise RuntimeError("Configuration apply is already running")
            if action == "full" and self.load_map_sync_state().get("status") == "running":
                raise RuntimeError("Wait for the current map job to finish before applying setup")

            if action == "update-content":
                settings = self.content_update_settings()
                retry_pending = (
                    state["can_retry_download"] and state["network_download_failures"]
                    and (state.get("automatic_retries") or 0) < CONTENT_RETRY_LIMIT
                )
                if scheduled and (retry_pending or not settings.get("enabled") or (settings.get("next_at") or 0) > time.time()):
                    return state
                now = time.time()
                settings.update(last_attempt_at=now, next_at=now + CONTENT_UPDATE_INTERVAL if settings["enabled"] else None)
                self.write_content_update_settings(settings)

            return self.launch_apply(action, clear_log=True)

    def recover_interrupted_apply(self, *, manual: bool = False) -> dict | None:
        with self.apply_lock, self.map_sync_lock:
            state = self.load_apply_state()
            action = state.get("action")
            status = state.get("status")
            if self.apply_thread and self.apply_thread.is_alive():
                if manual:
                    raise RuntimeError("A content job is already running.")
                return None

            interrupted = status == "running" and action in CONTENT_DOWNLOAD_ACTIONS | {"rebuild-library"}
            if not interrupted and not state["can_retry_download"]:
                if manual:
                    raise ValueError("There is no interrupted or failed content download to resume.")
                return None
            if manual and action not in CONTENT_DOWNLOAD_ACTIONS:
                raise ValueError("There is no content download to resume.")

            retries = state.get("automatic_retries") or 0
            if not manual and not interrupted:
                if not state["network_download_failures"] or retries >= CONTENT_RETRY_LIMIT:
                    return None
                finished_at = state.get("finished_at")
                if not finished_at:
                    return None
                elapsed = time.time() - datetime.fromisoformat(finished_at.replace("Z", "+00:00")).timestamp()
                if elapsed < CONTENT_RETRY_INTERVAL * 2**retries:
                    return None
                retries += 1

            if action == "full" and self.load_map_sync_state().get("status") == "running":
                if manual:
                    raise RuntimeError("Wait for the current map job to finish.")
                return None

            if interrupted:
                start_index = max(1, state.get("current_step_index") or 0)
                names = state.get("retry_download_names", [])
            else:
                start_index = next(index for index, (step, _) in enumerate(self.commands_for_action(action), 1)
                                   if step in ZIM_DOWNLOAD_STEPS)
                names = state["failed_download_names"]

            return self.launch_apply(
                action, clear_log=not interrupted, started_at=state.get("started_at"),
                resumed=True, start_index=start_index, retry_names=names,
                automatic_retries=0 if manual else retries,
            )

    def commands_for_action(self, action: str) -> list[tuple[str, list[str]]]:
        if action == "update-content":
            return [
                ("Checking for newer ZIM editions", ["__prepare_content_update__"]),
                ("Downloading available ZIM updates", [str(self.repo_root / "scripts" / "download_kiwix_zims.sh")]),
                ("Restarting Kiwix service", ["systemctl", "restart", "preppercache-kiwix.service"]),
            ]

        if action == "download-extra-content":
            return [
                (
                    "Downloading selected extra Kiwix content",
                    [str(self.repo_root / "scripts" / "download_kiwix_zims.sh")],
                ),
                (
                    "Restarting Kiwix service",
                    ["systemctl", "restart", "preppercache-kiwix.service"],
                ),
            ]

        if action == "downgrade-content-set":
            return [
                ("Downloading selected Kiwix content", [str(self.repo_root / "scripts" / "download_kiwix_zims.sh")]),
                ("Removing confirmed extra ZIMs", ["__remove_downgrade_files__"]),
                ("Rebuilding Kiwix library", [str(self.repo_root / "scripts" / "rebuild_kiwix_library.sh")]),
                ("Restarting Kiwix service", ["systemctl", "restart", "preppercache-kiwix.service"]),
            ]

        if action in {"refresh-content", "change-content-set"}:
            return [
                (
                    "Downloading selected Kiwix content",
                    [str(self.repo_root / "scripts" / "download_kiwix_zims.sh")],
                ),
                (
                    "Restarting Kiwix service",
                    ["systemctl", "restart", "preppercache-kiwix.service"],
                ),
            ]

        if action == "rebuild-library":
            return [
                (
                    "Rebuilding Kiwix library",
                    [str(self.repo_root / "scripts" / "rebuild_kiwix_library.sh")],
                ),
                (
                    "Restarting Kiwix service",
                    ["systemctl", "restart", "preppercache-kiwix.service"],
                ),
            ]

        return [
            (
                "Installing optional components",
                [str(self.repo_root / "scripts" / "install_optional_components.sh")],
            ),
            (
                "Applying wireless AP settings",
                [str(self.repo_root / "scripts" / "configure_access_point.sh")],
            ),
            (
                "Restarting core services",
                ["systemctl", "restart", "preppercache-kiwix.service"],
            ),
            (
                "Reloading Nginx",
                ["systemctl", "reload", "nginx"],
            ),
            (
                "Downloading selected Kiwix content",
                [str(self.repo_root / "scripts" / "download_kiwix_zims.sh")],
            ),
            (
                "Rebuilding Kiwix library",
                [str(self.repo_root / "scripts" / "rebuild_kiwix_library.sh")],
            ),
            (
                "Restarting core services",
                ["systemctl", "restart", "preppercache-kiwix.service"],
            ),
            (
                "Reloading Nginx",
                ["systemctl", "reload", "nginx"],
            ),
        ]

    def run_apply_workflow(
        self, action: str, start_index: int = 1, retry_names: list[str] | None = None, resumed: bool = False,
    ) -> None:
        commands = self.commands_for_action(action)

        env = dict(os.environ)
        env.update(read_env_file(self.preppercache_env))
        env.update(
            {
                "PREPPERCACHE_ENV_FILE": str(self.preppercache_env),
                "PREPPERCACHE_ENV_FILE": str(self.preppercache_env),
                "PREPPERCACHE_PROFILE_FILE": str(self.install_profile_env),
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            }
        )
        if action == "download-extra-content":
            env["PREPPERCACHE_ZIM_MODE"] = "custom"
            env["PREPPERCACHE_JOB_ZIM_MANIFEST"] = str(self.extra_zim_manifest_path())

        if action == "change-content-set":
            env["PREPPERCACHE_JOB_ZIM_MANIFEST"] = str(self.data_dir / "content-set-urls.txt")

        if resumed and action != "refresh-content":
            env["PREPPERCACHE_SKIP_INSTALLED_ZIMS"] = "1"
        if retry_names:
            env["PREPPERCACHE_RETRY_ZIM_NAMES"] = json.dumps(retry_names)
        if action == "update-content" and start_index > 1:
            env["PREPPERCACHE_JOB_ZIM_MANIFEST"] = str(self.data_dir / "content-update-urls.txt")

        downgrade = read_json(self.data_dir / "content-downgrade.json", {}) if action == "downgrade-content-set" else None
        exit_code = 0
        error_message = None
        result_message = None

        with self.apply_log_file.open("a", encoding="utf-8") as log_handle:
            for index, (step, command) in enumerate(commands, start=1):
                if index < start_index:
                    continue
                self.save_apply_state(
                    {
                        "step": step,
                        "current_step_index": index,
                        "total_steps": len(commands),
                    }
                )
                log_handle.write(f"\n== {step} ==\n")
                log_handle.flush()
                if downgrade is not None and index == 1:
                    installed = {item["name"] for item in self.list_installed_zims()}
                    pending = [url for url in downgrade.get("urls", []) if Path(urlparse(url).path).name not in installed]
                    if not pending:
                        continue
                    manifest = self.data_dir / "content-downgrade-urls.txt"
                    manifest.write_text("\n".join(pending) + "\n")
                    env["PREPPERCACHE_JOB_ZIM_MANIFEST"] = str(manifest)
                if command == ["__remove_downgrade_files__"]:
                    try:
                        self.remove_downgrade_files(downgrade)
                        self.activate_content_profile(downgrade["profile"])
                    except (ValueError, OSError) as exc:
                        exit_code, error_message = 1, str(exc)
                        break
                    result_message = "Smaller content set applied."
                    continue
                if command == ["__prepare_content_update__"]:
                    try:
                        manifest = self.prepare_content_update_manifest()
                    except (RuntimeError, OSError) as exc:
                        exit_code, error_message = 1, str(exc)
                        break
                    if not self.read_manifest_urls(manifest):
                        result_message = "Your installed content is up to date."
                        log_handle.write("No newer editions found for the installed ZIM files.\n")
                        break
                    env["PREPPERCACHE_ZIM_MODE"] = "custom"
                    env["PREPPERCACHE_JOB_ZIM_MANIFEST"] = str(manifest)
                    continue
                return_code = self.run_logged_command(
                    command,
                    env=env,
                    log_handle=log_handle,
                )
                if return_code != 0:
                    exit_code = return_code
                    error_message = f"{step} failed with exit code {return_code}"
                    break

        final_status = "succeeded" if exit_code == 0 else "failed"
        self.save_apply_state(
            {
                "status": final_status,
                "action": action,
                "current_step_index": len(commands) if exit_code == 0 else index,
                "total_steps": len(commands),
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "exit_code": exit_code,
                "error": error_message,
                "result_message": result_message,
                "step": None if exit_code == 0 else step,
            }
        )

    def run_logged_command(
        self,
        command: list[str],
        *,
        env: dict[str, str],
        log_handle,
    ) -> int:
        process = subprocess.Popen(
            command,
            cwd=self.repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        lines_since_compaction = 0
        try:
            if process.stdout:
                for line in process.stdout:
                    log_handle.write(line)
                    log_handle.flush()
                    lines_since_compaction += 1
                    if lines_since_compaction >= 100:
                        compact_log_file(self.apply_log_file)
                        lines_since_compaction = 0
            return process.wait()
        finally:
            if process.stdout:
                process.stdout.close()
            log_handle.flush()
            compact_log_file(self.apply_log_file)

    def parse_apply_progress(self, state: dict) -> dict:
        progress = {
            "progress_percent": 0,
            "current_file": None,
            "download_current": None,
            "download_total": None,
            "active_downloads": [],
            "download_failed": 0,
            "failed_download_names": [],
            "network_download_failures": [],
        }

        total_steps = state.get("total_steps") or 0
        current_step_index = state.get("current_step_index") or 0
        status = state.get("status")
        step = state.get("step") or ""

        if status == "succeeded":
            progress["progress_percent"] = 100
        elif total_steps > 0 and current_step_index > 0:
            progress["progress_percent"] = int(
                ((current_step_index - 1) / total_steps) * 100
            )

        recent_log_text = read_recent_text(
            self.apply_log_file,
            max_bytes=APPLY_PROGRESS_SCAN_BYTES,
        )
        if not recent_log_text:
            return progress

        total_pattern = re.compile(r"^PROGRESS_DOWNLOAD_TOTAL\|(\d+)$")
        file_pattern = re.compile(r"^PROGRESS_DOWNLOAD_FILE\|(\d+)\|(\d+)\|(.+)$")
        done_pattern = re.compile(r"^PROGRESS_DOWNLOAD_DONE\|(\d+)\|(\d+)\|(.+)$")
        finished_pattern = re.compile(r"^PROGRESS_DOWNLOAD_FINISHED\|(\d+)\|(\d+)\|(.*)$")
        complete_pattern = re.compile(r"^PROGRESS_DOWNLOAD_COMPLETE\|(\d+)$")
        bytes_pattern = re.compile(r"^PROGRESS_DOWNLOAD_BYTES\|(\d+)\|(?:(\d+)\|)?(.+)$")
        failed_prefix = "PROGRESS_DOWNLOAD_FAILED|"

        download_total = None
        current_file = None
        current_index = None
        last_done_index = None
        parallel_finished = None
        active_downloads: dict[str, tuple[int, int]] = {}

        for line in recent_log_text.splitlines():
            total_match = total_pattern.match(line)
            if total_match:
                download_total = int(total_match.group(1))
                active_downloads.clear()
                progress["download_failed"] = 0
                progress["failed_download_names"].clear()
                progress["network_download_failures"].clear()
                continue

            file_match = file_pattern.match(line)
            if file_match:
                current_index = int(file_match.group(1))
                download_total = int(file_match.group(2))
                current_file = file_match.group(3)
                active_downloads[current_file] = (0, 0)
                continue

            bytes_match = bytes_pattern.match(line)
            if bytes_match:
                active_downloads[bytes_match.group(3)] = (int(bytes_match.group(1)), int(bytes_match.group(2) or 0))
                continue

            network_failure = re.fullmatch(r"Failed: (.+) \((?:wget exited with code 4|408 .*|429 .*|5\d\d .*)\)", line)
            if network_failure:
                progress["network_download_failures"].append(network_failure[1])
                continue

            if line.startswith(failed_prefix):
                progress["download_failed"] += 1
                progress["failed_download_names"].append(line[len(failed_prefix):])
                continue

            done_match = done_pattern.match(line)
            if done_match:
                last_done_index = int(done_match.group(1))
                download_total = int(done_match.group(2))
                current_file = done_match.group(3)
                active_downloads.pop(current_file, None)
                continue

            finished_match = finished_pattern.match(line)
            if finished_match:
                parallel_finished = int(finished_match.group(1))
                download_total = int(finished_match.group(2))
                current_file = finished_match.group(3) or current_file
                active_downloads.pop(finished_match.group(3), None)
                continue

            complete_match = complete_pattern.match(line)
            if complete_match:
                download_total = int(complete_match.group(1))
                last_done_index = download_total
                current_index = download_total
                current_file = None
                active_downloads.clear()

        progress["current_file"] = current_file
        progress["download_total"] = download_total
        progress["download_current"] = parallel_finished if parallel_finished is not None else current_index or last_done_index

        if (
            status == "running"
            and step in ZIM_DOWNLOAD_STEPS
            and total_steps > 0
            and download_total
        ):
            progress["active_downloads"] = [
                {"filename": filename, "downloaded_bytes": downloaded_bytes, "total_bytes": total_bytes}
                for filename, (downloaded_bytes, total_bytes) in active_downloads.items()
            ]
            completed_within_download = parallel_finished if parallel_finished is not None else last_done_index or 0
            if parallel_finished is None and current_index and current_file and completed_within_download < current_index:
                completed_within_download = max(current_index - 1, 0)
            progress["progress_percent"] = int(
                (
                    ((current_step_index - 1) + (completed_within_download / download_total))
                    / total_steps
                )
                * 100
            )

        return progress

    def read_temperature(self) -> float | None:
        thermal = Path("/sys/class/thermal/thermal_zone0/temp")
        if not thermal.exists():
            return None
        try:
            return round(int(thermal.read_text().strip()) / 1000, 1)
        except ValueError:
            return None

    def read_uptime(self) -> str | None:
        uptime_path = Path("/proc/uptime")
        if not uptime_path.exists():
            return None
        try:
            seconds = int(float(uptime_path.read_text().split()[0]))
        except ValueError:
            return None
        hours, remainder = divmod(seconds, 3600)
        minutes = remainder // 60
        return f"{hours}h {minutes}m"

    def read_cpu_load(self) -> dict[str, float | int] | None:
        try:
            load1, _, _ = os.getloadavg()
        except (AttributeError, OSError):
            return None

        cpu_count = os.cpu_count() or 1
        load_percent = max(0, min(100, round((load1 / cpu_count) * 100)))
        return {
            "load_1m": round(load1, 2),
            "cpu_count": cpu_count,
            "load_percent": load_percent,
        }

    def read_memory_stats(self) -> dict[str, int | float] | None:
        meminfo = Path("/proc/meminfo")
        if not meminfo.exists():
            return None

        values: dict[str, int] = {}
        try:
            for line in meminfo.read_text().splitlines():
                if ":" not in line:
                    continue
                key, raw_value = line.split(":", 1)
                parts = raw_value.strip().split()
                if not parts:
                    continue
                values[key] = int(parts[0]) * 1024
        except (OSError, ValueError):
            return None

        total = int(values.get("MemTotal", 0))
        available = int(values.get("MemAvailable", values.get("MemFree", 0)))
        if total <= 0:
            return None
        used = max(0, total - available)
        used_percent = max(0, min(100, round((used / total) * 100)))
        return {
            "total_bytes": total,
            "used_bytes": used,
            "available_bytes": available,
            "used_percent": used_percent,
        }

    def read_service_statuses(self, services: dict[str, str]) -> dict[str, str]:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", *services.values()],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return {name: "unknown" for name in services}

        lines = [line.strip() or "unknown" for line in result.stdout.splitlines()]
        values = list(services.keys())
        resolved: dict[str, str] = {}
        for index, name in enumerate(values):
            resolved[name] = lines[index] if index < len(lines) else "unknown"
        return resolved

    def system_service_definitions(self) -> list[dict[str, str]]:
        return [
            {
                "id": "portal",
                "label": "Portal API",
                "unit": "preppercache-portal.service",
                "description": "Admin API and setup workflows.",
            },
            {
                "id": "kiwix",
                "label": "Kiwix",
                "unit": "preppercache-kiwix.service",
                "description": "Offline library server.",
            },
            {
                "id": "nginx",
                "label": "Nginx",
                "unit": "nginx.service",
                "description": "Main web front end for PrepperCache.",
            },
            {
                "id": "kolibri",
                "label": "Kolibri",
                "unit": "kolibri.service",
                "description": "Learning platform service.",
            },
            {
                "id": "access-point",
                "label": "Access Point",
                "unit": "preppercache-ap-network.service",
                "description": "Local Wi-Fi hotspot network service.",
            },
        ]

    def inspect_system_service(self, service: dict[str, str]) -> dict[str, str | bool]:
        unit = service["unit"]
        active_state = "unknown"
        enabled_state = "unknown"
        load_state = "unknown"
        sub_state = "unknown"

        try:
            result = subprocess.run(
                ["systemctl", "show", unit, "--property=LoadState,ActiveState,SubState,UnitFileState", "--value"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return {
                **service,
                "exists": False,
                "active_state": active_state,
                "enabled_state": enabled_state,
                "load_state": load_state,
                "sub_state": sub_state,
            }

        lines = result.stdout.splitlines()
        if len(lines) >= 4:
            load_state = lines[0].strip() or "unknown"
            active_state = lines[1].strip() or "unknown"
            sub_state = lines[2].strip() or "unknown"
            enabled_state = lines[3].strip() or "unknown"

        exists = load_state not in {"not-found", "unknown", ""}
        return {
            **service,
            "exists": exists,
            "active_state": active_state if exists else "not-installed",
            "enabled_state": enabled_state if exists else "not-installed",
            "load_state": load_state,
            "sub_state": sub_state,
        }

    def system_service_health(self) -> list[dict[str, str | bool]]:
        return [
            self.inspect_system_service(service)
            for service in self.system_service_definitions()
        ]

    def detect_primary_host(self) -> str:
        try:
            result = subprocess.run(
                ["hostname", "-I"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return "127.0.0.1"

        candidates = [value for value in result.stdout.split() if "." in value]
        return candidates[0] if candidates else "127.0.0.1"

    def request_power_action(self, action: str) -> dict:
        if action not in {"restart", "shutdown"}:
            raise ValueError("Invalid power action")
        systemctl_action = {
            "restart": "reboot",
            "shutdown": "poweroff",
        }[action]

        try:
            result = subprocess.run(
                ["systemctl", systemctl_action],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("systemctl is not available on this system") from exc

        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip() or f"systemctl {systemctl_action} failed"
            raise RuntimeError(message)

        return {
            "action": action,
            "message": f"System {action} requested. Controls locked until the device changes state.",
        }

    def restart_system_service(self, service_id: str) -> dict:
        services = {service["id"]: service for service in self.system_service_definitions()}
        service = services.get(service_id)
        if not service:
            raise ValueError("Unknown service.")

        details = self.inspect_system_service(service)
        if not details.get("exists"):
            raise ValueError(f'{service["label"]} is not installed on this device.')

        unit = service["unit"]
        try:
            result = subprocess.run(
                ["systemctl", "restart", unit],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("systemctl is not available on this system") from exc

        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip() or f"Unable to restart {service['label']}."
            raise RuntimeError(message)

        return {
            "service": self.inspect_system_service(service),
            "services": self.system_service_health(),
            "message": f'{service["label"]} restarted.',
        }

    def file_manager_roots(self, volumes: list[dict] | None = None) -> list[dict[str, str]]:
        roots: list[dict[str, str]] = [
            {
                "id": "internal-library",
                "label": "Internal Library",
                "path": str(self.kiwix_library_dir()),
            },
            {
                "id": "internal-maps",
                "label": "Internal Maps",
                "path": str(self.maps_root()),
            },
            {
                "id": "portal-data",
                "label": "PrepperCache Data",
                "path": str(self.data_dir),
            },
        ]
        seen = {item["path"] for item in roots}
        for volume in self.storage_volumes() if volumes is None else volumes:
            if volume["location"] != "external" or not volume["mounted"]:
                continue
            mountpoint = str(volume.get("mountpoint") or "").strip()
            if not mountpoint:
                continue
            display_name = str(volume.get("label") or volume.get("display_name") or volume.get("name") or "Drive").strip()
            if mountpoint not in seen:
                roots.append(
                    {
                        "id": f"volume-{volume.get('name', display_name)}",
                        "label": f"Drive: {display_name}",
                        "path": mountpoint,
                    }
                )
                seen.add(mountpoint)
            layout = volume.get("preppercache_layout") or {}
            for directory_name, label in (
                ("library", f"{display_name} Library"),
                ("maps", f"{display_name} Maps"),
                ("media", f"{display_name} Media"),
            ):
                if not layout.get(directory_name):
                    continue
                path = str(Path(mountpoint) / directory_name)
                if path in seen:
                    continue
                roots.append(
                    {
                        "id": f"{volume.get('name', display_name)}-{directory_name}",
                        "label": label,
                        "path": path,
                    }
                )
                seen.add(path)
        return roots

    def _allowed_file_roots(self) -> list[Path]:
        roots: list[Path] = []
        seen: set[str] = set()
        for item in self.file_manager_roots():
            path = Path(str(item.get("path", ""))).resolve(strict=False)
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            roots.append(path)
        return roots

    def _resolve_managed_file_path(self, path_value: str, *, allow_missing: bool = False) -> Path:
        candidate = Path(str(path_value or "").strip())
        if not candidate.is_absolute():
            raise ValueError("Choose a valid path.")
        resolved = candidate.resolve(strict=False)
        for root in self._allowed_file_roots():
            root_str = str(root)
            resolved_str = str(resolved)
            if resolved_str == root_str or resolved_str.startswith(root_str.rstrip("/") + "/"):
                if not allow_missing and not resolved.exists():
                    raise ValueError("The selected path is unavailable.")
                return resolved
        raise ValueError("That location is outside the allowed file-manager roots.")

    def _file_manager_parent(self, path: Path) -> str | None:
        parent = path.parent.resolve(strict=False)
        try:
            return str(self._resolve_managed_file_path(str(parent)))
        except ValueError:
            return None

    def _file_manager_entry(self, path: Path) -> dict[str, object]:
        stat = path.stat()
        is_dir = path.is_dir()
        return {
            "name": path.name,
            "path": str(path.resolve(strict=False)),
            "is_dir": is_dir,
            "size_bytes": 0 if is_dir else stat.st_size,
            "size_label": "--" if is_dir else format_size_bytes(stat.st_size),
            "modified_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
        }

    def file_manager_list(self, path_value: str | None = None) -> dict:
        volumes = self.storage_volumes()
        roots = self.file_manager_roots(volumes)
        default_path = roots[0]["path"] if roots else str(self.repo_root)
        path = self._resolve_managed_file_path(path_value or default_path)
        if not path.exists():
            raise ValueError("The selected path is unavailable.")
        if not path.is_dir():
            raise ValueError("Choose a folder to browse.")

        entries: list[dict[str, object]] = []
        for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            try:
                entries.append(self._file_manager_entry(child))
            except OSError:
                continue

        return {
            "path": str(path),
            "parent_path": self._file_manager_parent(path),
            "entries": entries,
            "roots": roots,
            "usb_connected": any(volume["transport"] == "usb" for volume in volumes),
            "unmounted_usb": [
                {"path": volume["path"], "display_name": volume["display_name"]}
                for volume in volumes if volume["transport"] == "usb" and volume["can_mount"]
            ],
        }

    def file_transfer_paths(self, operation: str, source_path: str, target_dir: str) -> tuple[Path, Path]:
        if operation not in ("copy", "move"):
            raise ValueError("Choose Copy or Move.")
        source = self._resolve_managed_file_path(source_path)
        folder = self._resolve_managed_file_path(target_dir)
        if not folder.is_dir():
            raise ValueError("Choose a destination folder.")
        if operation == "move" and source in self._allowed_file_roots():
            raise ValueError("Cannot move a protected root folder.")
        if source.is_dir() and (folder == source or source in folder.parents):
            raise ValueError("Choose a destination outside the selected folder.")
        destination = folder / source.name
        if destination.exists() or destination.is_symlink():
            raise ValueError("An item with that name already exists here. Choose another folder.")
        return source, destination

    def transfer_file(self, operation: str, source: Path, destination: Path) -> str:
        if operation == "move":
            try:
                source.rename(destination)
                return f"Moved {source.name}."
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise

        # Publish a complete copy only after writing all data to the target drive.
        with tempfile.TemporaryDirectory(prefix=".preppercache-transfer-", dir=destination.parent) as temp_dir:
            staged = Path(temp_dir) / source.name
            if source.is_dir():
                shutil.copytree(source, staged, symlinks=True)
            else:
                shutil.copy2(source, staged)
            if destination.exists() or destination.is_symlink():
                raise ValueError("An item with that name already exists in the destination.")
            staged.rename(destination)

        if operation == "move":
            try:
                if source.is_dir():
                    shutil.rmtree(source)
                else:
                    source.unlink()
            except OSError as exc:
                raise RuntimeError(f"Copied {source.name}, but could not fully remove the source: {exc}") from exc
            return f"Moved {source.name}."
        return f"Copied {source.name}."

    def zim_import_options(self) -> dict:
        primary = {}
        for volume in self.storage_volumes():
            if volume["transport"] != "usb" or not (volume["mounted"] or volume["can_mount"]):
                continue
            if volume["filesystem"] == "swap" or re.fullmatch(r"EFI( SYSTEM( PARTITION)?)?|ESP", volume["label"], re.I):
                continue
            disk = volume["parent_path"] or volume["path"]
            layout = volume["preppercache_layout"]
            rank = (bool(layout.get("library") and layout.get("maps")), volume["size_bytes"])
            if disk not in primary or rank > primary[disk][0]:
                primary[disk] = (rank, volume)
        return {
            "drives": [volume for _, volume in primary.values()],
            "libraries": [item for item in self.install_destinations("zims") if item["mounted"]],
        }

    def start_zim_import(self, device: str, target_dir: str) -> dict:
        with self.storage_prepare_lock, self.file_transfer_lock:
            self.require_file_manager_idle()
            options = self.zim_import_options()
            drive = next((item for item in options["drives"] if item["path"] == device), None)
            if not drive:
                raise ValueError("Choose a readable USB drive that is still connected.")
            if not any(item["path"] == target_dir for item in options["libraries"]):
                raise ValueError("The selected library is unavailable.")
            return self.launch_file_transfer({
                "status": "running", "operation": "import-zims", "source_dir": drive["mountpoint"],
                "target_dir": target_dir, "message": "Checking the USB drive for ZIMs…",
            }, self.run_zim_import, drive, Path(target_dir))

    def run_zim_import(self, drive: dict, destination: Path) -> None:
        def report(**fields) -> None:
            with self.file_transfer_lock:
                self.file_transfer.update(fields)

        imported = skipped = failed = 0
        errors = []
        try:
            device = drive["path"]
            if not drive["mounted"]:
                # The preparation API takes the same lock before changing a drive.
                with self.storage_prepare_lock:
                    current = self.storage_volume_by_device(device)
                    if not current or current["transport"] != "usb" or current["uuid"] != drive["uuid"]:
                        raise ValueError("The USB drive changed before it could be mounted.")
                    self.mount_storage_volume({"device_path": device})
            current = self.storage_volume_by_device(device)
            if not current or not current["mounted"] or current["uuid"] != drive["uuid"]:
                raise ValueError("The USB drive is no longer available.")
            root = Path(current["mountpoint"])
            identity = root.stat()
            report(source_dir=str(root))
            archives = sorted(
                entry for entry in root.iterdir()
                if entry.suffix.lower() == ".zim" and not entry.is_symlink() and entry.is_file()
            )
            self.sync_external_content_links()
            for index, archive in enumerate(archives, 1):
                report(message=f"Importing {index} of {len(archives)}: {archive.name}")
                try:
                    mounted = root.stat()
                    if (identity.st_dev, identity.st_ino) != (mounted.st_dev, mounted.st_ino):
                        raise ValueError("The USB drive was disconnected.")
                    with archive.open("rb") as source:
                        self.upload_zim(archive.name, os.fstat(source.fileno()).st_size, source, destination=destination)
                    imported += 1
                except FileExistsError:
                    skipped += 1
                except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
                    failed += 1
                    if len(errors) < 3:
                        errors.append(f"{archive.name}: {exc}")

            message = (
                f"Imported {imported}, skipped {skipped} already installed, {failed} failed."
                if archives else "No ZIM files found at the root of this USB drive."
            )
            if imported:
                try:
                    self.start_apply("rebuild-library")
                    message += " Kiwix library rebuild started."
                except (OSError, RuntimeError):
                    message += " Use Content → Rebuild Kiwix Library once other jobs finish."
            if errors:
                message += " " + " ".join(errors)
            report(status="complete" if imported or not failed else "failed", message=message, imported=imported)
        except Exception as exc:
            report(status="failed", message=f"USB import failed: {exc}", imported=imported)

    def file_transfer_status(self) -> dict:
        with self.file_transfer_lock:
            return dict(self.file_transfer)

    def require_file_manager_idle(self) -> None:
        if self.storage_prepare_job.get("status") == "running":
            raise ValueError("Wait for USB drive preparation to finish.")
        if self.file_transfer.get("status") == "running":
            raise ValueError("Wait for the current file transfer to finish.")

    def start_file_transfer(self, operation: str, source_path: str, target_dir: str) -> dict:
        with self.file_transfer_lock:
            self.require_file_manager_idle()
            source, destination = self.file_transfer_paths(operation, source_path, target_dir)
            return self.launch_file_transfer({
                "status": "running",
                "operation": operation,
                "source_dir": str(source.parent),
                "target_dir": str(destination.parent),
                "message": f'{"Copying" if operation == "copy" else "Moving"} {source.name}…',
            }, self.run_file_transfer, operation, source, destination)

    def launch_file_transfer(self, job: dict, run, *args) -> dict:
        self.file_transfer = job
        worker = threading.Thread(target=run, args=args, daemon=True)
        try:
            worker.start()
        except RuntimeError:
            self.file_transfer = {}
            raise
        return dict(job)

    def run_file_transfer(self, operation: str, source: Path, destination: Path) -> None:
        try:
            message = self.transfer_file(operation, source, destination)
            status = "complete"
        except Exception as exc:
            status = "failed"
            message = f"Transfer failed: {exc}"
        with self.file_transfer_lock:
            self.file_transfer.update(status=status, message=message)

    def file_manager_transfer(self, operation: str, source_path: str, target_dir: str) -> dict:
        source, destination = self.file_transfer_paths(operation, source_path, target_dir)
        return {
            "message": self.transfer_file(operation, source, destination),
            "source": self.file_manager_list(str(source.parent)),
            "target": self.file_manager_list(str(destination.parent)),
        }

    def file_manager_rename(self, source_path: str, new_name: str) -> dict:
        source = self._resolve_managed_file_path(source_path)
        name = str(new_name or "").strip()
        if not name or "/" in name or name in {".", ".."}:
            raise ValueError("Choose a valid new name.")
        destination = source.parent / name
        self._resolve_managed_file_path(str(destination), allow_missing=True)
        if destination.exists():
            raise ValueError("A file or folder with that name already exists.")
        source.rename(destination)
        return {
            "message": f"Renamed to {name}.",
            "pane": self.file_manager_list(str(destination.parent)),
        }

    def file_manager_delete(self, source_path: str) -> dict:
        source = self._resolve_managed_file_path(source_path)
        if str(source) in {str(root) for root in self._allowed_file_roots()}:
            raise ValueError("Cannot delete a protected root folder.")
        parent = source.parent
        if source.is_dir():
            shutil.rmtree(source)
        else:
            source.unlink()
        return {
            "message": f"Deleted {source.name}.",
            "pane": self.file_manager_list(str(parent)),
        }

    def file_manager_mkdir(self, parent_path: str, name: str) -> dict:
        parent = self._resolve_managed_file_path(parent_path)
        if not parent.is_dir():
            raise ValueError("Choose a folder first.")
        folder_name = str(name or "").strip()
        if not folder_name or "/" in folder_name or folder_name in {".", ".."}:
            raise ValueError("Choose a valid folder name.")
        new_dir = parent / folder_name
        self._resolve_managed_file_path(str(new_dir), allow_missing=True)
        if new_dir.exists():
            raise ValueError("A file or folder with that name already exists.")
        new_dir.mkdir(parents=False, exist_ok=False)
        return {
            "message": f"Created folder {folder_name}.",
            "pane": self.file_manager_list(str(parent)),
        }

        return {
            "status": "accepted",
            "action": action,
            "message": f"System {action} requested.",
        }


class PortalHandler(BaseHTTPRequestHandler):
    portal_state: PortalState

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_common_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = parse.urlparse(self.path)
        path = parsed.path
        query = parse.parse_qs(parsed.query)
        access = self.portal_state.admin_access.status(self.admin_token())
        if path == "/api/admin/session":
            self.send_json(access)
            return
        if not access["authenticated"]:
            if path == "/api/state":
                self.send_json({
                    "admin_locked": True,
                    "storage": {"kolibri_installed": Path("/usr/bin/kolibri").exists()},
                })
            else:
                self.send_json({"error": "Enter the admin password to unlock Settings."}, status=401)
            return

        if path == "/api/state":
            self.send_json(self.portal_state.load_state())
            return
        if path == "/api/status":
            self.send_json(self.portal_state.status())
            return
        if path == "/api/system/gpio":
            self.send_json(self.portal_state.gpio_settings())
            return
        if path == "/api/system/storage/prepare":
            self.send_json(self.portal_state.storage_prepare_status())
            return
        if path == "/api/system/storage/volumes":
            self.send_json({"volumes": [
                volume for volume in self.portal_state.storage_volumes()
                if volume["location"] == "external"
            ]})
            return
        if path == "/api/system/health":
            self.send_json(self.portal_state.system_health())
            return
        if path == "/api/system/access-point":
            self.send_json(self.portal_state.access_point_status())
            return
        if path == "/api/system/access-point/clients":
            self.send_json(self.portal_state.access_point_clients())
            return
        if path == "/api/diagnostics/logs":
            report = self.portal_state.troubleshooting_report().encode("utf-8")
            self.send_response(200)
            self.send_common_headers()
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="preppercache-troubleshooting.txt"')
            self.send_header("Content-Length", str(len(report)))
            self.end_headers()
            self.wfile.write(report)
            return
        if path == "/api/content":
            self.send_json(self.portal_state.content_status())
            return
        if path == "/api/content/set":
            try:
                plan, _ = self.portal_state.content_set_plan(query.get("profile", [""])[0])
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json({key: value for key, value in plan.items() if not key.startswith("_")})
            return
        if path == "/api/content/catalog":
            try:
                force_refresh = query.get("refresh", ["0"])[0] == "1"
                self.send_json(self.portal_state.fetch_kiwix_catalog_cached(force_refresh=force_refresh))
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=502)
            return
        if path == "/api/maps":
            self.portal_state.write_maps_runtime_config()
            self.send_json(self.portal_state.maps_status())
            return
        if path == "/api/maps/regions":
            self.send_json(self.portal_state.regional_map_catalog())
            return
        if path == "/api/maps/countries":
            self.send_json(self.portal_state.country_map_catalog())
            return
        if path == "/api/maps/sync":
            self.send_json(self.portal_state.load_map_sync_state())
            return
        if path == "/api/apply":
            self.send_json(self.portal_state.load_apply_state())
            return
        if path == "/api/files/zim-import":
            try:
                self.send_json(self.portal_state.zim_import_options())
            except OSError as exc:
                self.send_json({"error": str(exc)}, status=500)
            return
        if path == "/api/files/transfer":
            self.send_json(self.portal_state.file_transfer_status())
            return
        if path == "/api/files":
            try:
                self.send_json(self.portal_state.file_manager_list(query.get("path", [""])[0]))
            except (ValueError, OSError) as exc:
                self.send_json({"error": str(exc)}, status=400)
            return
        self.send_json({"error": "Not found"}, status=404)

    def do_POST(self) -> None:
        parsed = parse.urlparse(self.path)
        origin = self.headers.get("Origin")
        scheme = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
        if (origin is not None and origin != f"{scheme}://{self.headers.get('Host')}") or (
            self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}
        ):
            self.send_json({"error": "Open PrepperCache directly to change Settings."}, status=403)
            return

        token = self.admin_token()
        if parsed.path in {"/api/admin/login", "/api/admin/logout", "/api/admin/password"}:
            self.handle_admin_action(parsed.path, token, secure=scheme == "https")
            return
        if not self.portal_state.admin_access.status(token)["authenticated"]:
            self.send_json({"error": "Enter the admin password to unlock Settings."}, status=401)
            return

        if parsed.path == "/api/content/upload":
            self.receive_zim_upload(parsed.query)
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.send_json({"error": "Invalid JSON"}, status=400)
            return

        if self.path == "/api/setup":
            try:
                state = self.portal_state.save_setup(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(state)
            return

        if self.path == "/api/apply":
            try:
                state = self.portal_state.start_apply(payload.get("action", "full"))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=409)
                return
            self.send_json(state, status=202)
            return

        if self.path == "/api/content/set":
            try:
                result = self.portal_state.change_content_set(payload.get("profile"), payload.get("confirmed_review"))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=409)
                return
            self.send_json(result, status=202 if result["apply"] else 200)
            return

        if self.path == "/api/content/automatic-updates":
            try:
                settings = self.portal_state.set_automatic_content_updates(payload.get("enabled"))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(settings)
            return

        if self.path == "/api/content/settings":
            try:
                with self.portal_state.apply_lock:
                    if self.portal_state.load_apply_state().get("status") == "running":
                        raise RuntimeError("Wait for the current content job to finish.")
                    state = self.portal_state.save_content_settings(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=502)
                return
            self.send_json(state)
            return

        if self.path == "/api/content/resume-download":
            try:
                state = self.portal_state.recover_interrupted_apply(manual=True)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=409)
                return
            self.send_json(state, status=202)
            return

        if self.path == "/api/content/retry-downloads":
            try:
                state = self.portal_state.retry_extra_content()
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=409)
                return
            self.send_json(state, status=202)
            return

        if self.path == "/api/content/download-selected":
            try:
                state = self.portal_state.download_selected_extra_zims(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=409)
                return
            self.send_json(state, status=202)
            return

        if self.path == "/api/content/remove":
            try:
                with self.portal_state.apply_lock:
                    if self.portal_state.load_apply_state().get("status") == "running":
                        self.send_json({"error": "Wait for the current content job to finish."}, status=409)
                        return
                    state = self.portal_state.remove_zim_files(payload.get("filenames", []))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(state)
            return

        if self.path == "/api/maps/select":
            try:
                state = self.portal_state.select_map_package(payload.get("filename", ""))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(state)
            return

        if self.path == "/api/maps/settings":
            try:
                state = self.portal_state.update_map_settings(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(state)
            return

        if self.path == "/api/maps/remove":
            try:
                state = self.portal_state.remove_map_packages(payload.get("filenames", []))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(state)
            return

        if self.path in {"/api/maps/build", "/api/maps/countries", "/api/maps/regions", "/api/maps/build/cancel"}:
            try:
                if self.path == "/api/maps/build":
                    state = self.portal_state.start_map_build(payload)
                elif self.path == "/api/maps/countries":
                    state = self.portal_state.start_country_map_builds(payload)
                elif self.path == "/api/maps/regions":
                    state = self.portal_state.start_regional_map_builds(payload)
                else:
                    state = self.portal_state.cancel_map_build()
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=409)
                return
            self.send_json(state, status=202)
            return

        if self.path == "/api/system/gpio":
            try:
                self.send_json(self.portal_state.save_gpio_settings(payload))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
            return

        if self.path == "/api/system/power":
            try:
                state = self.portal_state.request_power_action(payload.get("action", ""))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=500)
                return
            self.send_json(state, status=202)
            return

        if self.path == "/api/system/service/restart":
            try:
                state = self.portal_state.restart_system_service(str(payload.get("service", "")).strip())
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=500)
                return
            self.send_json(state)
            return

        if self.path == "/api/files/zim-import":
            try:
                job = self.portal_state.start_zim_import(payload.get("device_path"), payload.get("target_dir"))
            except (ValueError, OSError, RuntimeError) as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(job, status=202)
            return
        if self.path == "/api/files/transfer":
            try:
                job = self.portal_state.start_file_transfer(
                    payload.get("operation"), payload.get("source_path"), payload.get("target_dir")
                )
            except (ValueError, OSError) as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=500)
                return
            self.send_json(job, status=202)
            return

        file_actions = {
            "/api/files/copy": (self.portal_state.file_manager_transfer, ("source_path", "target_dir")),
            "/api/files/move": (self.portal_state.file_manager_transfer, ("source_path", "target_dir")),
            "/api/files/rename": (self.portal_state.file_manager_rename, ("source_path", "new_name")),
            "/api/files/delete": (self.portal_state.file_manager_delete, ("source_path",)),
            "/api/files/mkdir": (self.portal_state.file_manager_mkdir, ("parent_path", "name")),
        }
        if self.path in file_actions:
            action, fields = file_actions[self.path]
            args = [str(payload.get(field, "")).strip() for field in fields]
            if self.path in {"/api/files/copy", "/api/files/move"}:
                args.insert(0, self.path.rsplit("/", 1)[-1])
            try:
                with self.portal_state.file_transfer_lock:
                    self.portal_state.require_file_manager_idle()
                    state = action(*args)
            except (ValueError, OSError, RuntimeError) as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(state)
            return

        if self.path == "/api/system/access-point":
            try:
                state = self.portal_state.apply_access_point_action(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=500)
                return
            self.send_json(state)
            return

        if self.path == "/api/system/storage/mount":
            try:
                with self.portal_state.storage_prepare_lock:
                    if self.portal_state.storage_prepare_job.get("status") == "running":
                        raise ValueError("Wait for USB drive preparation to finish.")
                    transfer = self.portal_state.file_transfer_status()
                    if transfer.get("operation") == "import-zims" and transfer.get("status") == "running":
                        raise ValueError("Wait for the USB ZIM import to finish.")
                    state = self.portal_state.mount_storage_volume(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=500)
                return
            self.send_json(state)
            return

        if self.path == "/api/system/storage/unmount":
            try:
                with self.portal_state.storage_prepare_lock:
                    if self.portal_state.storage_prepare_job.get("status") == "running":
                        raise ValueError("Wait for USB drive preparation to finish.")
                    transfer = self.portal_state.file_transfer_status()
                    if transfer.get("operation") == "import-zims" and transfer.get("status") == "running":
                        raise ValueError("Wait for the USB ZIM import to finish.")
                    state = self.portal_state.unmount_storage_volume(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=500)
                return
            self.send_json(state)
            return

        if self.path == "/api/system/storage/prepare":
            try:
                state = self.portal_state.start_storage_prepare(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except (OSError, RuntimeError) as exc:
                self.send_json({"error": str(exc)}, status=500)
                return
            self.send_json(state, status=202)
            return

        if self.path == "/api/system/storage/rename":
            try:
                with self.portal_state.storage_prepare_lock:
                    if self.portal_state.storage_prepare_job.get("status") == "running":
                        raise ValueError("Wait for USB drive preparation to finish.")
                    transfer = self.portal_state.file_transfer_status()
                    if transfer.get("operation") == "import-zims" and transfer.get("status") == "running":
                        raise ValueError("Wait for the USB ZIM import to finish.")
                    state = self.portal_state.rename_storage_volume(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=500)
                return
            self.send_json(state)
            return

        self.send_json({"error": "Not found"}, status=404)

    def admin_token(self) -> str:
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        entry = cookie.get(AdminAccess.COOKIE)
        return entry.value if entry else ""

    def handle_admin_action(self, path: str, token: str, *, secure: bool) -> None:
        access = self.portal_state.admin_access
        if self.headers.get_content_type() != "application/json":
            self.send_json({"error": "Send Settings changes as JSON."}, status=415)
            return
        length = self.headers.get("Content-Length", "")
        if not length.isascii() or not length.isdecimal() or len(length) > 4 or not 0 < int(length) <= 4096:
            self.send_json({"error": "Invalid admin request size."}, status=400)
            return
        try:
            payload = json.loads(self.rfile.read(int(length)))
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeError):
            self.send_json({"error": "Invalid admin request."}, status=400)
            return

        try:
            if path == "/api/admin/login":
                token = access.login(payload.get("password"), token)
            elif path == "/api/admin/logout":
                access.logout(token)
                token = ""
            else:
                token = access.configure(
                    token, payload.get("enabled"), payload.get("current_password"), payload.get("new_password")
                )
        except AdminAccessError as exc:
            self.send_json({"error": str(exc)}, status=exc.status)
            return
        except OSError:
            self.send_json({"error": "Could not save the admin password. Check device storage."}, status=500)
            return

        max_age = AdminAccess.SESSION_SECONDS if token else 0
        cookie = f"{AdminAccess.COOKIE}={token}; Path=/api/; Max-Age={max_age}; HttpOnly; SameSite=Strict"
        if secure:
            cookie += "; Secure"
        self.send_json(access.status(token), cookie=cookie)

    def receive_zim_upload(self, query: str) -> None:
        self.close_connection = True
        if self.headers.get_content_type() != "application/octet-stream":
            self.send_json({"error": "Send a ZIM file as a binary upload."}, status=415)
            return
        if self.headers.get("Transfer-Encoding") or not self.headers.get("Content-Length", "").isdigit():
            self.send_json({"error": "The upload must include its file size."}, status=411)
            return
        self.connection.settimeout(120)
        name = parse.parse_qs(query).get("name", [""])[0]
        try:
            result = self.portal_state.upload_zim(name, int(self.headers["Content-Length"]), self.rfile)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, status=400)
        except (FileExistsError, RuntimeError) as exc:
            self.send_json({"error": str(exc)}, status=409)
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"ZIM upload failed: {exc}", flush=True)
            self.send_json({"error": "Unable to finish the upload. Check the drive and try again."}, status=500)
        else:
            self.send_json(result, status=201)

    def send_json(self, payload: dict, status: int = 200, *, cookie: str | None = None) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_common_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(encoded)

    def send_common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")

    def log_message(self, fmt: str, *args) -> None:
        return


def main() -> None:
    args = parse_args()
    handler = PortalHandler
    handler.portal_state = PortalState(Path(args.repo_root), Path(args.data_dir))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    threading.Thread(target=handler.portal_state.run_content_update_schedule, daemon=True).start()
    print(f"PrepperCache portal API listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
