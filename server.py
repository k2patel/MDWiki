#!/usr/bin/env python3
"""MDWiki runtime: build Markdown from persistent storage and serve it safely."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import shutil
import threading
import time
import unicodedata
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, unquote, urlsplit


LEGACY_PAGE_ENDPOINTS = {"/", "/doku.php", "/index.php"}
LEGACY_MEDIA_ENDPOINTS = {"/lib/exe/fetch.php", "/lib/exe/detail.php"}
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_SINGLE_REQUEST_BYTES = MAX_PAGE_BYTES + 64 * 1024
MAX_BATCH_REQUEST_BYTES = 32 * 1024 * 1024
MAX_BATCH_PAGES = 1000
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _environment_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class PageConflictError(ValueError):
    """Raised when a protected bulk import would replace existing pages."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        super().__init__(f"{len(paths)} page path{'s' if len(paths) != 1 else ''} already exist")


def _normalize_part(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^a-z0-9_.-]+", "_", value)
    return re.sub(r"_+", "_", value).strip("_.")


def _page_location(root: Path, page_id: str) -> str | None:
    raw_parts = [part for part in re.split(r"[:/]+", unquote(page_id).strip(" :/")) if part]
    parts = [_normalize_part(part) for part in raw_parts]
    parts = [part for part in parts if part and part not in {".", ".."}]
    if not parts or parts == ["start"]:
        return "/" if (root / "index.html").is_file() else None
    if parts[-1] == "start":
        parts.pop()
    target = root.joinpath(*parts, "index.html").resolve()
    if root.resolve() not in target.parents or not target.is_file():
        return None
    return "/" + "/".join(quote(part, safe="._-") for part in parts) + "/"


def _media_location(root: Path, media_id: str) -> str | None:
    raw_parts = [part for part in re.split(r"[:/]+", unquote(media_id).strip(" :/")) if part]
    parts = [_normalize_part(part) for part in raw_parts]
    parts = [part for part in parts if part and part not in {".", ".."}]
    if not parts:
        return None
    target = root.joinpath("media", *parts).resolve()
    media_root = (root / "media").resolve()
    if media_root not in target.parents or not target.is_file():
        return None
    return "/media/" + "/".join(quote(part, safe="._-") for part in parts)


def legacy_redirect(url: str, root: Path) -> tuple[bool, str | None]:
    """Return whether a URL is legacy-like and its safe canonical location."""
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    if parsed.path in LEGACY_MEDIA_ENDPOINTS:
        media_id = (query.get("media") or query.get("id") or [""])[0]
        return True, _media_location(root, media_id)
    if parsed.path in LEGACY_PAGE_ENDPOINTS and "id" in query:
        return True, _page_location(root, query["id"][0])
    if parsed.path in {"/doku.php", "/index.php"}:
        return True, "/" if (root / "index.html").is_file() else None

    clean_path = unquote(parsed.path).strip("/")
    arch_style = clean_path.lower().startswith("title/")
    if arch_style:
        clean_path = clean_path.split("/", 1)[1]
    if clean_path and (arch_style or not parsed.path.endswith("/")) and "." not in Path(clean_path).name:
        location = _page_location(root, clean_path)
        if location:
            return True, location
        if arch_style:
            return True, None
    return False, None


def _environment_brand() -> dict[str, object]:
    topics = [item.strip() for item in os.environ.get("MDWIKI_TOPICS", "Guides,Operations,Reference,Runbooks,Notes").split(",")]
    topics = [item for item in topics if item][:8]

    def color(name: str, fallback: str) -> str:
        value = os.environ.get(name, fallback)
        return value if COLOR.fullmatch(value) else fallback

    return {
        "eyebrow": os.environ.get("MDWIKI_HERO_EYEBROW", "Portable Markdown knowledge base"),
        "title": os.environ.get("MDWIKI_HERO_TITLE", "Find it. Fix it. Share it."),
        "accent": os.environ.get("MDWIKI_HERO_ACCENT", "Your documentation, on your infrastructure."),
        "description": os.environ.get(
            "MDWIKI_HERO_DESCRIPTION",
            "Searchable Markdown with automatic topic indexing, clean URLs, and storage you control.",
        ),
        "topics": topics,
        "primary_light": color("MDWIKI_PRIMARY_LIGHT", "#123047"),
        "primary_dark": color("MDWIKI_PRIMARY_DARK", "#0b1828"),
        "accent_light": color("MDWIKI_ACCENT_LIGHT", "#075f80"),
        "accent_dark": color("MDWIKI_ACCENT_DARK", "#63cbe8"),
    }


class WikiState:
    """Own persistent content, ephemeral build output, and rebuild state."""

    def __init__(self, content_root: Path, site_root: Path, config_path: Path) -> None:
        self.content_root = content_root.resolve()
        self.site_root = site_root.resolve()
        self.config_path = config_path.resolve()
        self.admin_user = os.environ.get("MDWIKI_ADMIN_USER", "admin")
        self.admin_password = os.environ.get("MDWIKI_ADMIN_PASSWORD", "")
        self.lock = threading.Lock()
        self.mutation_lock = threading.Lock()
        self.ready = False
        self.last_error = ""
        self.last_built = 0.0

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_password)

    def ensure_content(self) -> None:
        self.content_root.mkdir(parents=True, exist_ok=True)
        if any(self.content_root.rglob("*.md")):
            return
        site_name = os.environ.get("MDWIKI_SITE_NAME", "MDWiki")
        starter = (
            "---\ntemplate: home.html\nhide:\n  - toc\n  - navigation\n---\n\n"
            f"# Welcome to {site_name}\n\n"
            "Add Markdown files to the content volume or use the authenticated admin page. "
            "MDWiki will index and publish them automatically.\n"
        )
        (self.content_root / "index.md").write_text(starter, encoding="utf-8")

    def content_signature(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(item for item in self.content_root.rglob("*") if item.is_file()):
            relative = path.relative_to(self.content_root).as_posix()
            stat = path.stat()
            digest.update(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
        return digest.hexdigest()

    def rebuild(self) -> None:
        with self.lock:
            work_root = self.site_root.parent / "mdwiki-build"
            docs_root = work_root / "docs"
            staged_site = work_root / "site"
            try:
                shutil.rmtree(work_root, ignore_errors=True)
                shutil.copytree(self.content_root, docs_root)
                staged_site.mkdir(parents=True)

                from mkdocs.commands.build import build
                from mkdocs.config import load_config

                config = load_config(
                    config_file=str(self.config_path),
                    docs_dir=str(docs_root),
                    site_dir=str(staged_site),
                    # A wiki accepts operator-supplied pages at runtime. Broken
                    # links and other Markdown warnings must not take the whole
                    # service down; strict mode remains available as an opt-in.
                    strict=_environment_flag("MDWIKI_STRICT"),
                )
                config["site_name"] = os.environ.get("MDWIKI_SITE_NAME", "MDWiki")
                config["site_description"] = os.environ.get("MDWIKI_SITE_DESCRIPTION", "A portable Markdown knowledge base")
                config["copyright"] = os.environ.get("MDWIKI_COPYRIGHT", f"{config['site_name']} · Powered by MDWiki")
                config["repo_url"] = os.environ.get("MDWIKI_REPO_URL", "")
                config["repo_name"] = os.environ.get("MDWIKI_REPO_NAME", "")
                config["edit_uri"] = os.environ.get("MDWIKI_EDIT_URI", "")
                config["extra"] = {
                    "generator": False,
                    "admin_enabled": self.admin_enabled,
                    "brand": _environment_brand(),
                }
                build(config)

                old_site = self.site_root.parent / "mdwiki-site-old"
                shutil.rmtree(old_site, ignore_errors=True)
                if self.site_root.exists():
                    self.site_root.rename(old_site)
                staged_site.rename(self.site_root)
                shutil.rmtree(old_site, ignore_errors=True)
                self.ready = True
                self.last_error = ""
                self.last_built = time.time()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                if not self.site_root.is_dir():
                    self.ready = False
                raise
            finally:
                shutil.rmtree(work_root, ignore_errors=True)

    def watch(self) -> None:
        signature = self.content_signature()
        interval = max(1.0, float(os.environ.get("MDWIKI_POLL_SECONDS", "2")))
        while True:
            time.sleep(interval)
            current = self.content_signature()
            if current == signature:
                continue
            try:
                self.rebuild()
                signature = self.content_signature()
            except Exception as exc:
                print(f"MDWiki rebuild failed: {exc}", flush=True)

    def authorized(self, authorization: str | None) -> bool:
        if not self.admin_enabled or not authorization or not authorization.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
            user, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(user, self.admin_user) and hmac.compare_digest(password, self.admin_password)

    def normalize_page_path(self, requested_path: str) -> PurePosixPath:
        raw_path = requested_path.strip()
        if not raw_path or raw_path.startswith("/") or "\\" in raw_path or any(ord(character) < 32 for character in raw_path):
            raise ValueError("Invalid page path")
        pure = PurePosixPath(raw_path)
        if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
            raise ValueError("Invalid page path")
        raw_parts = list(pure.parts)
        filename = PurePosixPath(raw_parts[-1])
        stem = filename.stem if filename.suffix.lower() == ".md" else filename.name
        normalized_parts = [_normalize_part(part) for part in raw_parts[:-1]] + [f"{_normalize_part(stem)}.md"]
        if any(not part or part == ".md" for part in normalized_parts):
            raise ValueError("Page path must contain a valid name")
        return PurePosixPath(*normalized_parts)

    @staticmethod
    def page_location(path: PurePosixPath) -> str:
        page_parts = list(path.with_suffix("").parts)
        if page_parts[-1] == "index":
            page_parts.pop()
        return "/" if not page_parts else "/" + "/".join(quote(part, safe="._-") for part in page_parts) + "/"

    def save_pages(self, pages: list[tuple[str, str]], overwrite: bool = False) -> list[tuple[Path, str]]:
        if not pages or len(pages) > MAX_BATCH_PAGES:
            raise ValueError(f"Import between 1 and {MAX_BATCH_PAGES} Markdown pages at a time")

        planned: list[tuple[PurePosixPath, Path, str]] = []
        seen: set[PurePosixPath] = set()
        for requested_path, content in pages:
            if not isinstance(requested_path, str) or not isinstance(content, str):
                raise ValueError("Every page needs a text path and Markdown content")
            if not content.strip():
                raise ValueError(f"Markdown content cannot be empty: {requested_path}")
            if len(content.encode("utf-8")) > MAX_PAGE_BYTES:
                raise ValueError(f"Markdown page exceeds 2 MiB: {requested_path}")
            pure = self.normalize_page_path(requested_path)
            if pure in seen:
                raise ValueError(f"Two selected files normalize to the same path: {pure.as_posix()}")
            seen.add(pure)
            target = self.content_root.joinpath(*pure.parts).resolve()
            if self.content_root not in target.parents:
                raise ValueError("Invalid page path")
            planned.append((pure, target, content))

        conflicts = [pure.as_posix() for pure, target, _ in planned if target.exists()]
        if conflicts and not overwrite:
            raise PageConflictError(conflicts)

        saved: list[tuple[Path, str]] = []
        for pure, target, content in planned:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.upload-{os.getpid()}-{threading.get_ident()}")
            try:
                temporary.write_text(content, encoding="utf-8")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            saved.append((target, self.page_location(pure)))
        return saved

    def publish_pages(self, pages: list[tuple[str, str]], overwrite: bool = False) -> list[tuple[Path, str]]:
        """Write a validated batch and rebuild search/topics exactly once."""
        with self.mutation_lock:
            saved = self.save_pages(pages, overwrite=overwrite)
            self.rebuild()
            return saved

    def save_page(self, requested_path: str, content: str) -> tuple[Path, str]:
        return self.save_pages([(requested_path, content)], overwrite=True)[0]


class WikiHandler(SimpleHTTPRequestHandler):
    server_version = "MDWiki/1.0"
    sys_version = ""
    state: WikiState | None = None

    def version_string(self) -> str:
        return self.server_version

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._health(head_only=False)
            return
        if path in {"/admin", "/admin/"}:
            self._admin()
            return
        if self._legacy_response():
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        if urlsplit(self.path).path == "/healthz":
            self._health(head_only=True)
            return
        if self._legacy_response():
            return
        super().do_HEAD()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if path == "/api/pages":
            self._upload_page()
        elif path == "/api/pages/import":
            self._import_pages()
        else:
            self.send_error(404, "Not found")

    def _legacy_response(self) -> bool:
        handled, location = legacy_redirect(self.path, Path(self.directory))
        if not handled:
            return False
        if location is None:
            self.send_error(404, "Legacy wiki target not found")
            return True
        self.send_response(301)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def _health(self, head_only: bool) -> None:
        ready = self.state.ready if self.state else True
        payload: dict[str, object] = {"status": "ok" if ready else "building"}
        if self.state:
            payload["last_built"] = self.state.last_built
            if self.state.last_error:
                payload["last_error"] = self.state.last_error
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(200 if ready else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _authenticate(self) -> bool:
        if self.state and self.state.authorized(self.headers.get("Authorization")):
            return True
        if not self.state or not self.state.admin_enabled:
            self.send_error(404, "Admin uploads are disabled")
            return False
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="MDWiki administration", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _admin(self) -> None:
        if not self._authenticate():
            return
        admin_file = Path(os.environ.get("MDWIKI_ADMIN_FILE", "/app/admin/index.html"))
        body = admin_file.read_text(encoding="utf-8").replace(
            "{{SITE_NAME}}", html.escape(os.environ.get("MDWIKI_SITE_NAME", "MDWiki"))
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _upload_page(self) -> None:
        if not self._authenticate():
            return
        payload = self._read_json(MAX_SINGLE_REQUEST_BYTES)
        if payload is None:
            return
        try:
            requested_path = payload["path"]
            content = payload["content"]
            if not isinstance(requested_path, str) or not isinstance(content, str):
                raise ValueError("Page path and Markdown content must be text")
            assert self.state is not None
            saved, location = self.state.publish_pages([(requested_path, content)], overwrite=True)[0]
            self._send_json(
                201,
                {"saved": saved.relative_to(self.state.content_root).as_posix(), "url": location},
            )
        except (KeyError, TypeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": f"Page saved but the site rebuild failed: {exc}"})

    def _import_pages(self) -> None:
        if not self._authenticate():
            return
        payload = self._read_json(MAX_BATCH_REQUEST_BYTES)
        if payload is None:
            return
        try:
            raw_pages = payload["pages"]
            overwrite = payload.get("overwrite", False)
            if not isinstance(raw_pages, list) or not isinstance(overwrite, bool):
                raise ValueError("Pages must be a list and overwrite must be true or false")
            if not raw_pages or len(raw_pages) > MAX_BATCH_PAGES:
                raise ValueError(f"Import between 1 and {MAX_BATCH_PAGES} Markdown pages at a time")
            pages: list[tuple[str, str]] = []
            for page in raw_pages:
                if not isinstance(page, dict) or not isinstance(page.get("path"), str) or not isinstance(page.get("content"), str):
                    raise ValueError("Every page needs a text path and Markdown content")
                pages.append((page["path"], page["content"]))
            assert self.state is not None
            saved = self.state.publish_pages(pages, overwrite=overwrite)
            self._send_json(
                201,
                {
                    "count": len(saved),
                    "pages": [
                        {"saved": path.relative_to(self.state.content_root).as_posix(), "url": location}
                        for path, location in saved
                    ],
                },
            )
        except PageConflictError as exc:
            self._send_json(409, {"error": str(exc), "conflicts": exc.paths})
        except (KeyError, TypeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": f"Pages saved but the site rebuild failed: {exc}"})

    def _read_json(self, maximum_bytes: int) -> dict[str, object] | None:
        if self.headers.get_content_type() != "application/json":
            self._send_json(415, {"error": "Expected application/json"})
            return None
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            self._send_json(403, {"error": "Cross-origin writes are not allowed"})
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > maximum_bytes:
            self._send_json(413, {"error": f"Request must be between 1 byte and {maximum_bytes // (1024 * 1024)} MiB"})
            return None
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("JSON request must be an object")
            return payload
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": f"Invalid JSON: {exc}"})
            return None

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; font-src 'self' data:; connect-src 'self'",
        )
        super().end_headers()

    def list_directory(self, path: str):  # type: ignore[no-untyped-def]
        self.send_error(404, "Not found")
        return None


def create_server(root: Path, port: int, state: WikiState | None = None) -> ThreadingHTTPServer:
    class BoundWikiHandler(WikiHandler):
        pass

    BoundWikiHandler.state = state
    handler = partial(BoundWikiHandler, directory=str(root))
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    server.daemon_threads = True
    return server


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("MDWIKI_LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    content_root = Path(os.environ.get("MDWIKI_CONTENT_DIR", "/data/mdwiki"))
    site_root = Path(os.environ.get("MDWIKI_SITE_DIR", "/tmp/mdwiki-site"))
    config_path = Path(os.environ.get("MDWIKI_CONFIG", "/app/mkdocs.yml"))
    port = int(os.environ.get("PORT", "8080"))
    state = WikiState(content_root, site_root, config_path)
    state.ensure_content()
    state.rebuild()
    threading.Thread(target=state.watch, name="content-watcher", daemon=True).start()

    class RuntimeWikiHandler(WikiHandler):
        pass

    RuntimeWikiHandler.state = state
    handler = partial(RuntimeWikiHandler, directory=str(site_root))
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    server.daemon_threads = True
    print(f"Serving {os.environ.get('MDWIKI_SITE_NAME', 'MDWiki')} on 0.0.0.0:{port} from {content_root}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
