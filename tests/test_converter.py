from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
import unittest

from tools.convert_dokuwiki import DokuWikiConverter, output_path


class ConverterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.pages = root / "pages"
        self.media = root / "media"
        self.pages.mkdir()
        (self.pages / "start.txt").write_text("====== Home ======\n", encoding="utf-8")
        (self.pages / "other.txt").write_text("====== Other Page ======\n", encoding="utf-8")
        (self.media / "wiki").mkdir(parents=True)
        (self.media / "wiki" / "logo.png").write_bytes(b"png")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def converter(self) -> DokuWikiConverter:
        return DokuWikiConverter(self.pages, self.media)

    def test_dot_in_filename_is_preserved(self) -> None:
        self.assertEqual(output_path(PurePosixPath("release_4.11.txt")), PurePosixPath("release_4.11.md"))

    def test_home_page_uses_custom_template(self) -> None:
        converted = self.converter().convert_page("====== Home ======\n", PurePosixPath("start.txt"))
        self.assertTrue(converted.startswith("---\ntemplate: home.html\n"))

    def test_links_media_and_shell_tests(self) -> None:
        source = """====== Test ======
[[other|Go]] [[https://example.com|Web]]
[[https://example.com|{{wiki:logo.png}}]]
<code bash>
if [[ $value == yes ]]; then echo yes; fi
</code>
"""
        converted = self.converter().convert_page(source, PurePosixPath("test.txt"))
        self.assertIn("[Go](other.md)", converted)
        self.assertIn("[Web](https://example.com)", converted)
        self.assertIn("[![wiki:logo](media/wiki/logo.png)](https://example.com)", converted)
        self.assertIn("if [[ $value == yes ]]", converted)

    def test_blocks_tables_notes_and_footnotes(self) -> None:
        source = """===== Section =====
  * one
^ A ^ B ^
| 1 | 2 |
<note warning>Careful</note>
Text ((Details))
"""
        converted = self.converter().convert_page(source, PurePosixPath("test.txt"))
        self.assertIn("# Test", converted)
        self.assertIn("## Section", converted)
        self.assertIn("- one", converted)
        self.assertIn("| A | B |", converted)
        self.assertIn("> **Warning:** Careful", converted)
        self.assertIn("Text [^1]", converted)
        self.assertIn("[^1]: Details", converted)

    def test_dangerous_script_is_rendered_as_code(self) -> None:
        source = "<script>\nalert('x')\n</script>\n"
        converted = self.converter().convert_page(source, PurePosixPath("test.txt"))
        self.assertIn("```html\n<script>", converted)


if __name__ == "__main__":
    unittest.main()
