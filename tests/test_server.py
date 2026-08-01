import base64
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server import PageConflictError, WikiHandler, WikiState, _environment_flag, create_server, legacy_redirect


class ServerTests(unittest.TestCase):
    def test_strict_mode_is_opt_in(self) -> None:
        self.assertFalse(_environment_flag("MDWIKI_TEST_MISSING"))
        with patch.dict("os.environ", {"MDWIKI_TEST_STRICT": "true"}):
            self.assertTrue(_environment_flag("MDWIKI_TEST_STRICT"))

    def test_static_content_and_health(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_text("wiki home", encoding="utf-8")
            handler = object.__new__(WikiHandler)
            handler.directory = str(root)
            handler.wfile = BytesIO()
            handler.request_version = "HTTP/1.1"
            handler.protocol_version = "HTTP/1.0"
            handler.requestline = "GET /healthz HTTP/1.1"
            handler.command = "GET"
            handler.client_address = ("127.0.0.1", 12345)
            handler._health(head_only=False)
            response = handler.wfile.getvalue()
            self.assertIn(b"HTTP/1.0 200 OK", response)
            self.assertIn(b"X-Content-Type-Options: nosniff", response)
            self.assertTrue(response.endswith(b'{"status":"ok"}'))
            handler.path = "/"
            self.assertEqual(Path(handler.translate_path("/")), root)

    def test_legacy_and_clean_page_urls(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_text("home", encoding="utf-8")
            (root / "pianobar").mkdir()
            (root / "pianobar" / "index.html").write_text("page", encoding="utf-8")
            (root / "wiki" / "syntax").mkdir(parents=True)
            (root / "wiki" / "syntax" / "index.html").write_text("syntax", encoding="utf-8")

            self.assertEqual(legacy_redirect("/pianobar", root), (True, "/pianobar/"))
            self.assertEqual(legacy_redirect("/Pianobar", root), (True, "/pianobar/"))
            self.assertEqual(legacy_redirect("/title/Pianobar", root), (True, "/pianobar/"))
            self.assertEqual(legacy_redirect("/doku.php?id=wiki%3Asyntax", root), (True, "/wiki/syntax/"))
            self.assertEqual(legacy_redirect("/doku.php?id=missing", root), (True, None))

    def test_legacy_media_url(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "media" / "wiki").mkdir(parents=True)
            (root / "media" / "wiki" / "logo.png").write_bytes(b"png")
            self.assertEqual(
                legacy_redirect("/lib/exe/fetch.php?media=wiki%3Alogo.png", root),
                (True, "/media/wiki/logo.png"),
            )

    def test_page_upload_path_is_normalized_and_confined(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = WikiState(root / "content", root / "site", root / "mkdocs.yml")
            state.content_root.mkdir()
            saved, location = state.save_page("Runbooks/My Database Page.md", "# Database\n")
            self.assertEqual(saved.relative_to(state.content_root).as_posix(), "runbooks/my_database_page.md")
            self.assertEqual(location, "/runbooks/my_database_page/")
            saved, location = state.save_page("guides/index.md", "# Guides\n")
            self.assertEqual(location, "/guides/")
            with self.assertRaises(ValueError):
                state.save_page("../private.md", "no")

    def test_batch_import_validates_before_writing_and_protects_existing_pages(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = WikiState(root / "content", root / "site", root / "mkdocs.yml")
            state.content_root.mkdir()
            saved = state.save_pages(
                [("Handbook/Start Here.md", "# Start\n"), ("Handbook/Nested/SSH.md", "# SSH\n")]
            )
            self.assertEqual(
                [path.relative_to(state.content_root).as_posix() for path, _ in saved],
                ["handbook/start_here.md", "handbook/nested/ssh.md"],
            )
            with self.assertRaises(PageConflictError):
                state.save_pages([("handbook/start_here.md", "# Replacement\n")])
            state.save_pages([("handbook/start_here.md", "# Replacement\n")], overwrite=True)
            self.assertEqual((state.content_root / "handbook/start_here.md").read_text(), "# Replacement\n")

            with self.assertRaises(ValueError):
                state.save_pages([("A Page.md", "one"), ("a_page.md", "two")])
            self.assertFalse((state.content_root / "a_page.md").exists())

    def test_batch_publish_rebuilds_once(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = WikiState(root / "content", root / "site", root / "mkdocs.yml")
            state.content_root.mkdir()
            rebuilds: list[bool] = []
            state.rebuild = lambda: rebuilds.append(True)  # type: ignore[method-assign]
            result = state.publish_pages([("one.md", "# One\n"), ("two.md", "# Two\n")])
            self.assertEqual(len(result), 2)
            self.assertEqual(rebuilds, [True])

    def test_authenticated_batch_import_endpoint(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content_root, site_root = root / "content", root / "site"
            content_root.mkdir()
            site_root.mkdir()
            (site_root / "index.html").write_text("site", encoding="utf-8")
            state = WikiState(content_root, site_root, root / "mkdocs.yml")
            state.admin_user = "editor"
            state.admin_password = "secret"
            state.rebuild = lambda: None  # type: ignore[method-assign]
            server = create_server(site_root, 0, state)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = json.dumps(
                    {
                        "overwrite": False,
                        "pages": [
                            {"path": "Handbook/index.md", "content": "# Handbook\n"},
                            {"path": "Handbook/SSH.md", "content": "# SSH\n"},
                        ],
                    }
                ).encode()
                authorization = base64.b64encode(b"editor:secret").decode()
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/pages/import",
                    data=body,
                    headers={"Authorization": f"Basic {authorization}", "Content-Type": "application/json"},
                )
                with urlopen(request, timeout=3) as response:
                    payload = json.load(response)
                    self.assertEqual(response.status, 201)
                    self.assertEqual(payload["count"], 2)
                self.assertTrue((content_root / "handbook/index.md").is_file())
                self.assertTrue((content_root / "handbook/ssh.md").is_file())

                with self.assertRaises(HTTPError) as conflict:
                    urlopen(request, timeout=3)
                self.assertEqual(conflict.exception.code, 409)
                conflict.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
