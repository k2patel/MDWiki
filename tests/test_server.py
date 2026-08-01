from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from server import WikiHandler, WikiState, legacy_redirect


class ServerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
