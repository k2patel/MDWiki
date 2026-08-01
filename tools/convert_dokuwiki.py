#!/usr/bin/env python3
"""Convert a DokuWiki pages/media tree into portable Markdown.

The converter intentionally uses only the Python standard library so the
migration can be repeated without installing DokuWiki, Pandoc, or MkDocs.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import posixpath
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import quote


HEADING_RE = re.compile(r"^\s*(={2,6})\s*(.*?)\s*\1\s*$")
CODE_OPEN_RE = re.compile(r"^\s*<(code|file)(?:\s+([^>]*?))?\s*>\s*$", re.I)
CODE_CLOSE_RE = re.compile(r"^\s*</(?:code|file)(?:\s+[^>]*)?>\s*$", re.I)
PHP_OPEN_RE = re.compile(r"^\s*<php>\s*$", re.I)
PHP_CLOSE_RE = re.compile(r"^\s*</php>\s*$", re.I)
SCRIPT_OPEN_RE = re.compile(r"^\s*<(script|iframe|object|embed)\b", re.I)
LINK_RE = re.compile(r"\[\[(.+?)(?:\|(.*?))?\]\]")
MEDIA_RE = re.compile(r"\{\{(.*?)\}\}")
LIST_RE = re.compile(r"^(\s{2,})([*-])\s+(.*)$")
NOTE_OPEN_RE = re.compile(r"^\s*<note(?:\s+([^>]+))?>(.*)$", re.I)
FOOTNOTE_RE = re.compile(r"(?<!\$)\(\((.+?)\)\)")

LANGUAGES = {
    "apache": "apacheconf",
    "oobas": "basic",
    "rbuy": "ruby",
    "sh": "bash",
    "text": "",
    "txt": "",
}

INTERWIKI = {
    "doku": "https://www.dokuwiki.org/",
    "google": "https://www.google.com/search?q=",
    "man": "https://manpages.debian.org/cgi-bin/search.py?q=",
    "phpfn": "https://www.php.net/",
    "rfc": "https://www.rfc-editor.org/rfc/rfc",
    "wp": "https://en.wikipedia.org/wiki/",
}


def read_text(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    encodings = ("utf-8-sig", "utf-8", "cp1252", "latin-1") if raw.startswith(b"\xef\xbb\xbf") else ("utf-8", "cp1252", "latin-1")
    for encoding in encodings:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("unknown", raw, 0, len(raw), "unsupported encoding")


def normalize_id(value: str) -> str:
    value = value.strip().replace("/", ":").replace("\\", ":")
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    parts = []
    for part in value.split(":"):
        part = re.sub(r"\s+", "_", part)
        part = re.sub(r"[^a-z0-9_.-]+", "_", part)
        part = re.sub(r"_+", "_", part).strip("_.")
        if part:
            parts.append(part)
    return ":".join(parts)


def markdown_slug(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    value = value.replace("_", "-")
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    return value.strip("-")


def page_id(path: PurePosixPath) -> str:
    return ":".join(path.with_suffix("").parts)


def output_path(path: PurePosixPath) -> PurePosixPath:
    stem = path.name.removesuffix(".txt")
    if stem == "start":
        return path.parent / "index.md"
    return path.parent / f"{stem}.md"


def page_title(path: PurePosixPath) -> str:
    stem = path.name.removesuffix(".txt")
    if stem == "start" and path.parent == PurePosixPath("."):
        return "MDWiki"
    words = re.sub(r"[_-]+", " ", stem).strip()
    return words[:1].upper() + words[1:]


@dataclass
class Report:
    pages: int = 0
    media: int = 0
    encodings: dict[str, int] = field(default_factory=dict)
    broken_links: set[str] = field(default_factory=set)
    missing_media: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    source_sha256: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "pages": self.pages,
            "media": self.media,
            "encodings": dict(sorted(self.encodings.items())),
            "broken_links": sorted(self.broken_links),
            "missing_media": sorted(self.missing_media),
            "warnings": sorted(self.warnings),
            "source_sha256": self.source_sha256,
        }


class DokuWikiConverter:
    def __init__(self, pages_root: Path, media_root: Path):
        self.pages_root = pages_root
        self.media_root = media_root
        self.report = Report()
        self.page_files = sorted(p for p in pages_root.rglob("*.txt") if p.is_file())
        self.media_files = sorted(p for p in media_root.rglob("*") if p.is_file())
        self.ids: dict[str, PurePosixPath] = {}
        self.media_ids: dict[str, PurePosixPath] = {}
        self._index_sources()

    def _index_sources(self) -> None:
        digest = hashlib.sha256()
        for source in self.page_files:
            relative = PurePosixPath(source.relative_to(self.pages_root).as_posix())
            canonical = normalize_id(page_id(relative))
            target = output_path(relative)
            self.ids[canonical] = target
            if canonical == "start":
                self.ids[""] = target
            elif canonical.endswith(":start"):
                self.ids[canonical.removesuffix(":start")] = target
            digest.update(relative.as_posix().encode())
            digest.update(b"\0")
            digest.update(source.read_bytes())
        for source in self.media_files:
            relative = PurePosixPath(source.relative_to(self.media_root).as_posix())
            canonical = normalize_id(str(relative))
            self.media_ids[canonical] = relative
            digest.update(relative.as_posix().encode())
            digest.update(b"\0")
            digest.update(source.read_bytes())
        self.report.source_sha256 = digest.hexdigest()

    def convert_all(self) -> dict[PurePosixPath, str]:
        converted: dict[PurePosixPath, str] = {}
        for source in self.page_files:
            relative = PurePosixPath(source.relative_to(self.pages_root).as_posix())
            text, encoding = read_text(source)
            self.report.encodings[encoding] = self.report.encodings.get(encoding, 0) + 1
            converted[output_path(relative)] = self.convert_page(text, relative)
        self.report.pages = len(converted)
        self.report.media = len(self.media_files)
        return converted

    def convert_page(self, text: str, source: PurePosixPath) -> str:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines: list[str] = []
        for original_line in text.split("\n"):
            opening = re.search(r"<(?:code|file)(?:\s+[^>]*)?>", original_line, re.I)
            if opening and opening.start() and "</code" not in original_line.lower() and "</file" not in original_line.lower():
                prefix = original_line[: opening.start()]
                # A tag wrapped in DokuWiki monospace quotes is documentation,
                # not the beginning of a real block.
                if prefix.count("''") % 2 == 0:
                    lines.extend([prefix.rstrip(), opening.group(0)])
                    if original_line[opening.end() :]:
                        lines.append(original_line[opening.end() :])
                    continue
            elif opening and opening.start() == 0 and original_line[opening.end() :]:
                lines.append(opening.group(0))
                lines.append(original_line[opening.end() :])
                continue
            lines.append(original_line)
        output: list[str] = []
        footnotes: list[str] = []
        index = 0

        while index < len(lines):
            line = lines[index]

            code_match = CODE_OPEN_RE.match(line)
            if code_match or PHP_OPEN_RE.match(line):
                language_spec = code_match.group(2) if code_match else "php"
                block_type = code_match.group(1).lower() if code_match else "code"
                closing_tag = block_type if code_match else "php"
                close_pattern = re.compile(rf"</{closing_tag}(?:\s+[^>]*)?>", re.I)
                language_words = (language_spec or "").strip().split()
                language_name = language_words[0].strip("|").lower() if language_words else ""
                block: list[str] = []
                index += 1
                while index < len(lines):
                    close = close_pattern.search(lines[index])
                    if not close and block_type == "code" and language_name:
                        close = re.search(rf"</{re.escape(language_name)}\s*>", lines[index], re.I)
                    if close:
                        before = lines[index][: close.start()]
                        after = lines[index][close.end() :]
                        if before:
                            block.append(before)
                        if after:
                            lines.insert(index + 1, after)
                        break
                    block.append(lines[index])
                    index += 1
                if index == len(lines):
                    self.report.warnings.append(f"{source}: unclosed <{block_type}> block")
                output.extend(self._code_block(block, language_spec, block_type))
                index += 1
                continue

            if SCRIPT_OPEN_RE.match(line):
                tag = SCRIPT_OPEN_RE.match(line).group(1).lower()  # type: ignore[union-attr]
                block = [line]
                index += 1
                while index < len(lines):
                    block.append(lines[index])
                    if re.search(rf"</{tag}\s*>", lines[index], re.I):
                        break
                    index += 1
                output.extend(self._code_block(block, "html", "code"))
                index += 1
                continue

            note_match = NOTE_OPEN_RE.match(line)
            if note_match:
                kind = (note_match.group(1) or "note").strip().lower()
                note_lines = [note_match.group(2)]
                while not any("</note>" in item.lower() for item in note_lines) and index + 1 < len(lines):
                    index += 1
                    note_lines.append(lines[index])
                note_text = "\n".join(note_lines)
                note_text = re.sub(r"</note>\s*$", "", note_text, flags=re.I)
                label = {"important": "Important", "tip": "Tip", "warning": "Warning"}.get(kind, "Note")
                converted_note = [self._inline(part, source, footnotes) for part in note_text.split("\n")]
                output.append(f"> **{label}:** " + converted_note[0])
                output.extend("> " + part for part in converted_note[1:])
                index += 1
                continue

            if line.lstrip().startswith(("^", "|")) and line.rstrip().endswith(("^", "|")):
                table_lines = []
                while index < len(lines):
                    candidate = lines[index]
                    if not (candidate.lstrip().startswith(("^", "|")) and candidate.rstrip().endswith(("^", "|"))):
                        break
                    table_lines.append(candidate)
                    index += 1
                output.extend(self._table(table_lines, source, footnotes))
                continue

            heading = HEADING_RE.match(line)
            if heading:
                level = 7 - len(heading.group(1))
                output.append("#" * level + " " + self._inline(heading.group(2), source, footnotes))
                index += 1
                continue

            list_item = LIST_RE.match(line)
            if list_item:
                depth = max(1, len(list_item.group(1)) // 2)
                marker = "-" if list_item.group(2) == "*" else "1."
                body = self._inline(list_item.group(3), source, footnotes)
                output.append(" " * (4 * (depth - 1)) + f"{marker} {body}")
                index += 1
                continue

            if re.match(r"^\s*</?WRAP(?:\s+[^>]*)?>\s*$", line, re.I) or re.match(r"^\s*</?html>\s*$", line, re.I):
                index += 1
                continue

            if re.fullmatch(r"\s*~~(?:NOTOC|NOCACHE)~~\s*", line, flags=re.I):
                line = ""
            if line.startswith("  ") and line.strip():
                output.append("    " + line[2:])
            else:
                output.append(self._inline(line, source, footnotes))
            index += 1

        while output and not output[-1].strip():
            output.pop()
        has_h1 = any(re.match(r"^#\s+", line) for line in output)
        if not has_h1:
            output = [f"# {page_title(source)}", ""] + output
        if footnotes:
            output.extend(["", *footnotes])
        result = "\n".join(output) + "\n"
        result = re.sub(r"\n{4,}", "\n\n\n", result)
        if source == PurePosixPath("start.txt"):
            result = "---\ntemplate: home.html\nhide:\n  - toc\n---\n\n" + result
        return result

    def _code_block(self, block: list[str], spec: str | None, block_type: str) -> list[str]:
        spec = (spec or "").strip()
        language_part, _, title = spec.partition("|")
        spec_words = language_part.strip().split()
        language = spec_words[0] if spec_words else ""
        if not title and len(spec_words) > 1:
            title = " ".join(spec_words[1:])
        language = LANGUAGES.get(language.lower(), language.lower())
        if language == "-":
            language = ""
        title = title.strip().strip(">")
        if block_type == "file" and not title and language_part.strip() and not language:
            title = language_part.strip()
        longest = max((len(match.group(0)) for line in block for match in re.finditer(r"`+", line)), default=0)
        fence = "`" * max(3, longest + 1)
        rendered: list[str] = []
        if title:
            rendered.extend([f"**File: `{title}`**", ""])
        rendered.append(fence + language)
        rendered.extend(block)
        rendered.append(fence)
        return rendered

    def _table(self, lines: list[str], source: PurePosixPath, footnotes: list[str]) -> list[str]:
        rows: list[list[str]] = []
        first_is_header = lines[0].lstrip().startswith("^")
        for line in lines:
            stripped = line.strip()
            content = stripped[1:-1]
            cells = re.split(r"(?<!\\)[|^]", content)
            rows.append([self._inline(cell.strip().replace(r"\|", "|"), source, footnotes) for cell in cells])
        width = max((len(row) for row in rows), default=1)
        rows = [row + [""] * (width - len(row)) for row in rows]
        rendered: list[str] = []
        if first_is_header:
            header, body = rows[0], rows[1:]
        else:
            header, body = [""] * width, rows
        rendered.append("| " + " | ".join(cell.replace("|", r"\|") for cell in header) + " |")
        rendered.append("| " + " | ".join("---" for _ in range(width)) + " |")
        rendered.extend("| " + " | ".join(cell.replace("|", r"\|") for cell in row) + " |" for row in body)
        return rendered

    def _inline(self, line: str, source: PurePosixPath, footnotes: list[str]) -> str:
        line = LINK_RE.sub(lambda match: self._link(match, source, footnotes), line)
        line = MEDIA_RE.sub(lambda match: self._media(match.group(1), source), line)
        line = re.sub(r"''(.*?)''", lambda match: "`" + match.group(1).replace("`", r"\`") + "`", line)
        line = re.sub(r"%%(.*?)%%", lambda match: "`" + match.group(1).replace("`", r"\`") + "`", line)
        line = re.sub(r"<nowiki>(.*?)</nowiki>", lambda match: "`" + match.group(1).replace("`", r"\`") + "`", line, flags=re.I)
        line = re.sub(r"__(.+?)__", r"<u>\1</u>", line)
        if "http://" not in line and "https://" not in line:
            line = re.sub(r"(?<![:/])//(?!/)(.+?)(?<![:/])//", r"*\1*", line)
        line = re.sub(r"\\\\\s*$", "  ", line)
        line = re.sub(r"<(script|iframe|object|embed)\b", r"&lt;\1", line, flags=re.I)
        line = re.sub(r"</(script|iframe|object|embed)\s*>", r"&lt;/\1&gt;", line, flags=re.I)

        def footnote(match: re.Match[str]) -> str:
            number = len(footnotes) + 1
            footnotes.append(f"[^{number}]: {match.group(1).strip()}")
            return f"[^{number}]"

        return FOOTNOTE_RE.sub(footnote, line)

    def _link(self, match: re.Match[str], source: PurePosixPath, footnotes: list[str]) -> str:
        target = match.group(1).strip()
        raw_label = match.group(2)
        label = raw_label.strip() if raw_label is not None else ""

        if target.startswith(("$", "-", "!", "=", '"', "'")) or any(token in target for token in (" -eq ", " == ", " != ", " =~ ", "&&", "||", "${", "$(")):
            return match.group(0)

        if target.startswith("\\\\"):
            display = label or target
            return f"{display} (`{target}`)"

        interwiki = re.match(r"^([a-zA-Z0-9_]+)>(.*)$", target)
        if interwiki:
            prefix, destination = interwiki.groups()
            prefix = prefix.lower()
            if prefix == "this":
                display = label or destination
                return f"<span title=\"Legacy DokuWiki action link\">{html.escape(display)}</span>"
            base = INTERWIKI.get(prefix)
            if base:
                url = base + quote(destination.strip().replace(":", "/"), safe="/#?=&")
                display = label or destination or base
                display = self._render_link_label(display, source)
                return f"[{display}]({url})"

        if re.match(r"^(?:https?|ftp)://", target, re.I) or target.startswith("//"):
            url = target if not target.startswith("//") else "https:" + target
            display = label or url
            display = self._render_link_label(display, source)
            return f"[{display}]({url.replace(' ', '%20')})"
        if "@" in target and ":" not in target:
            return f"[{label or target}](mailto:{target})"

        page_part, separator, fragment = target.partition("#")
        current_namespace = normalize_id(":".join(source.with_suffix("").parts[:-1]))
        explicit_root = page_part.startswith(":")
        explicit_relative = page_part.startswith(".")
        clean_part = page_part.lstrip(":.")
        normalized = normalize_id(clean_part)
        flattened = normalize_id(clean_part.replace("/", " "))
        candidates: list[str]
        if not page_part:
            candidates = [normalize_id(page_id(source))]
        elif explicit_root or ":" in clean_part:
            candidates = [normalized]
        elif explicit_relative:
            candidates = [":".join(part for part in (current_namespace, normalized) if part)]
        else:
            local = ":".join(part for part in (current_namespace, normalized) if part)
            candidates = [local, normalized] if local != normalized else [normalized]
        if flattened and flattened not in candidates:
            candidates.append(flattened)
        destination_id = next((candidate for candidate in candidates if candidate in self.ids), candidates[0])
        destination = self.ids.get(destination_id)
        display = label or re.sub(r"[_:]+", " ", clean_part).strip() or fragment
        if not destination:
            self.report.broken_links.add(f"{source.as_posix()}: {target}")
            return f'<span class="missing-page" title="Missing DokuWiki page: {html.escape(target, quote=True)}">{html.escape(display)}</span>'
        current_output = output_path(source)
        relative = posixpath.relpath(destination.as_posix(), current_output.parent.as_posix())
        if separator and fragment:
            relative += "#" + markdown_slug(fragment)
        return f"[{display}]({relative})"

    def _render_link_label(self, label: str, source: PurePosixPath) -> str:
        return MEDIA_RE.sub(lambda match: self._media(match.group(1), source), label)

    def _media(self, raw: str, source: PurePosixPath) -> str:
        rss = re.match(r"\s*rss>(https?://\S+)", raw, re.I)
        if rss:
            return f"[RSS feed]({rss.group(1)})"
        leading_space = raw.startswith(" ")
        trailing_space = raw.endswith(" ")
        target_and_options, separator, caption = raw.strip().partition("|")
        target, query_separator, options = target_and_options.strip().partition("?")
        caption = caption.strip() if separator else ""
        is_external = bool(re.match(r"^https?://", target, re.I))
        if is_external:
            url = target
            relative_media = None
        else:
            current_namespace = normalize_id(":".join(source.with_suffix("").parts[:-1]))
            explicit_root = target.startswith(":")
            clean_target = target.lstrip(":")
            normalized = normalize_id(clean_target)
            local = ":".join(part for part in (current_namespace, normalized) if part)
            candidates = [normalized] if explicit_root or ":" in clean_target else [local, normalized]
            media_id = next((candidate for candidate in candidates if candidate in self.media_ids), candidates[0])
            relative_media = self.media_ids.get(media_id)
            if relative_media is None:
                self.report.missing_media.add(f"{source.as_posix()}: {target}")
                label = caption or PurePosixPath(clean_target).name or "missing media"
                return f'<span class="missing-media" title="Missing DokuWiki media: {html.escape(target, quote=True)}">{html.escape(label)}</span>'
            current_output = output_path(source)
            media_output = PurePosixPath("media") / relative_media
            url = posixpath.relpath(media_output.as_posix(), current_output.parent.as_posix())

        label = caption or PurePosixPath(target).stem.replace("_", " ")
        option_tokens = [token for token in re.split(r"[&,]", options) if token]
        if "linkonly" in option_tokens:
            return f"[{label}]({url})"
        size = re.search(r"(?:^|[&,])(\d+)(?:x(\d+))?(?:$|[&,])", options)
        if size or leading_space or trailing_space:
            attrs = [f'src="{html.escape(url, quote=True)}"', f'alt="{html.escape(label, quote=True)}"']
            if size:
                attrs.append(f'width="{size.group(1)}"')
                if size.group(2):
                    attrs.append(f'height="{size.group(2)}"')
            alignment = "center" if leading_space and trailing_space else "right" if leading_space else "left" if trailing_space else ""
            if alignment:
                attrs.append(f'class="media-{alignment}"')
            return "<img " + " ".join(attrs) + ">"
        return f"![{label}]({url})"


def render_report(report: Report) -> str:
    return json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n"


def render_manifest(converter: DokuWikiConverter, pages: dict[PurePosixPath, str]) -> str:
    manifest = {
        "markdown": sorted(path.as_posix() for path in pages),
        "media": sorted(source.relative_to(converter.media_root).as_posix() for source in converter.media_files),
    }
    return json.dumps(manifest, indent=2) + "\n"


def write_conversion(converter: DokuWikiConverter, pages: dict[PurePosixPath, str], output: Path, media_output: Path, report_path: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / ".dokuwiki-generated.json"
    previous = {"markdown": [], "media": []}
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    expected = {Path(path.as_posix()) for path in pages}
    for relative in previous.get("markdown", []):
        stale = output / relative
        if Path(relative) not in expected and stale.is_file():
            stale.unlink()
    for relative, content in pages.items():
        destination = output / Path(relative.as_posix())
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    expected_media = {source.relative_to(converter.media_root) for source in converter.media_files}
    for relative in previous.get("media", []):
        stale = media_output / relative
        if Path(relative) not in expected_media and stale.is_file():
            stale.unlink()
    for source in converter.media_files:
        relative = source.relative_to(converter.media_root)
        destination = media_output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    manifest_path.write_text(render_manifest(converter, pages), encoding="utf-8")
    report_path.write_text(render_report(converter.report), encoding="utf-8")


def check_conversion(converter: DokuWikiConverter, pages: dict[PurePosixPath, str], output: Path, media_output: Path, report_path: Path) -> list[str]:
    errors: list[str] = []
    for relative, content in pages.items():
        destination = output / Path(relative.as_posix())
        if not destination.exists() or destination.read_text(encoding="utf-8") != content:
            errors.append(f"stale Markdown: {relative}")
    for source in converter.media_files:
        relative = source.relative_to(converter.media_root)
        destination = media_output / relative
        if not destination.exists() or destination.read_bytes() != source.read_bytes():
            errors.append(f"stale media: {relative}")
    if not report_path.exists() or report_path.read_text(encoding="utf-8") != render_report(converter.report):
        errors.append("conversion report is stale")
    manifest_path = output / ".dokuwiki-generated.json"
    if not manifest_path.exists() or manifest_path.read_text(encoding="utf-8") != render_manifest(converter, pages):
        errors.append("DokuWiki generated-file manifest is stale")
    return errors


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-pages", type=Path, default=Path("data/data/pages"))
    parser.add_argument("--source-media", type=Path, default=Path("data/data/media"))
    parser.add_argument("--output", type=Path, default=Path("docs"))
    parser.add_argument("--media-output", type=Path)
    parser.add_argument("--report", type=Path, default=Path("conversion-report.json"))
    parser.add_argument("--check", action="store_true", help="fail if committed Markdown differs from a fresh conversion")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    media_output = args.media_output or args.output / "media"
    if not args.source_pages.is_dir():
        print(f"error: pages directory does not exist: {args.source_pages}", file=sys.stderr)
        return 2
    converter = DokuWikiConverter(args.source_pages, args.source_media)
    pages = converter.convert_all()
    if args.check:
        errors = check_conversion(converter, pages, args.output, media_output, args.report)
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print(f"Conversion is current: {converter.report.pages} pages, {converter.report.media} media files")
        return 0
    write_conversion(converter, pages, args.output, media_output, args.report)
    print(
        f"Converted {converter.report.pages} pages and {converter.report.media} media files; "
        f"{len(converter.report.broken_links)} broken links and {len(converter.report.missing_media)} missing media references"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
