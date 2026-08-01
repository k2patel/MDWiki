"""Generate a topic index immediately before MkDocs discovers its files."""

from __future__ import annotations

import html
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote


TOPICS: dict[str, tuple[str, ...]] = {
    "Containers & orchestration": ("docker", "kubernetes", "k8s", "container", "helm"),
    "Shell & command line": ("bash", "shell", "command", "cron", "sed", "regex", "tmux", "screen", "vim", "grep"),
    "Web servers & proxies": ("apache", "nginx", "lighttpd", "lightttpd", "squid", "tomcat", "webmin", "php", "letsencrypt"),
    "Databases": ("mysql", "mariadb", "postgres", "pgsql", "elasticsearch", "database"),
    "Networking & security": ("network", "dns", "bind", "firewall", "iptables", "ipfw", "ssh", "ssl", "openvpn", "security"),
    "Storage & backup": ("backup", "snapshot", "rsync", "zfs", "lvm", "xfs", "iscsi", "nfs", "filesystem", "storage"),
    "Monitoring & performance": ("monitor", "nagios", "cacti", "snmp", "awstats", "sar", "mpstat", "tuning", "performance"),
    "Automation & development": ("puppet", "python", "perl", "ruby", "git", "subversion", "script", "rpm", "automation"),
    "Virtualization": ("vmware", "kvm", "xen", "libvirt", "virtual machine", "virtualization"),
    "Mail & messaging": ("postfix", "qmail", "sendmail", "smtp", "mail"),
    "Operating systems & desktop": ("freebsd", "solaris", "opensuse", "arch", "fedora", "centos", "windows", "mac", "desktop"),
}


def _front_matter_tags(text: str) -> list[str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not match:
        return []
    tags: list[str] = []
    in_tags = False
    for line in match.group(1).splitlines():
        if re.match(r"^tags\s*:", line, re.I):
            in_tags = True
            inline = line.split(":", 1)[1].strip().strip("[]")
            if inline:
                tags.extend(item.strip().strip("'\"") for item in inline.split(",") if item.strip())
            continue
        if in_tags and re.match(r"^\s+-\s+", line):
            tags.append(re.sub(r"^\s+-\s+", "", line).strip().strip("'\""))
        elif in_tags and line and not line[0].isspace():
            break
    return tags


def _title(text: str, path: Path) -> str:
    match = re.search(r"^#\s+(.+?)\s*$", text, re.M)
    if match:
        title = re.sub(r"[*_`<>]", "", match.group(1)).strip()
        if title:
            return title
    return path.stem.replace("_", " ").replace("-", " ").strip().capitalize()


def _canonical_topic(tag: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", tag.lower()).strip()
    for topic, keywords in TOPICS.items():
        if normalized == re.sub(r"[^a-z0-9]+", " ", topic.lower()).strip() or normalized in keywords:
            return topic
    return tag.strip().title()


def _inferred_topics(text: str, path: Path, title: str) -> list[str]:
    headings = " ".join(re.findall(r"^#{1,3}\s+(.+)$", text, re.M)[:8])
    haystack = f"{path.as_posix()} {title} {headings}".lower().replace("_", " ").replace("-", " ")
    scores: list[tuple[int, str]] = []
    for topic, keywords in TOPICS.items():
        score = sum(1 for keyword in keywords if re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", haystack))
        if score:
            scores.append((score, topic))
    scores.sort(key=lambda item: (-item[0], item[1]))
    return [topic for _, topic in scores[:3]] or ["Other notes"]


def _page_url(path: Path) -> str:
    target = path.with_suffix("").as_posix()
    return "../" + quote(target, safe="/._-") + "/"


def _render(groups: dict[str, list[tuple[str, Path]]], pages: list[tuple[str, Path]]) -> str:
    lines = [
        "---",
        "hide:",
        "  - toc",
        "---",
        "",
        "# Topic index",
        "",
        "Every page is indexed automatically. Add `tags` in a page's front matter for precise placement; migrated pages are classified from their titles and headings.",
        "",
        '<div class="topic-index">',
    ]
    preferred = list(TOPICS) + [name for name in sorted(groups) if name not in TOPICS]
    for topic in preferred:
        entries = sorted(groups.get(topic, []), key=lambda item: item[0].casefold())
        if not entries:
            continue
        lines.extend([
            '<section class="topic-card">',
            f"<h2>{html.escape(topic)}</h2>",
            f'<p class="topic-card__count">{len(entries)} {"page" if len(entries) == 1 else "pages"}</p>',
            "<ul>",
        ])
        for title, path in entries:
            lines.append(f'<li><a href="{_page_url(path)}">{html.escape(title)}</a></li>')
        lines.extend(["</ul>", "</section>"])
    lines.extend(["</div>", "", "<details class=\"all-pages\">", f"<summary>All pages ({len(pages)})</summary>", "<ul>"])
    for title, path in sorted(pages, key=lambda item: item[0].casefold()):
        lines.append(f'<li><a href="{_page_url(path)}">{html.escape(title)}</a></li>')
    lines.extend(["</ul>", "</details>", ""])
    return "\n".join(lines)


def on_config(config):  # type: ignore[no-untyped-def]
    docs_dir = Path(config["docs_dir"])
    topic_path = docs_dir / "topics" / "index.md"
    groups: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    pages: list[tuple[str, Path]] = []

    for page in sorted(docs_dir.rglob("*.md")):
        relative = page.relative_to(docs_dir)
        if relative == Path("topics/index.md"):
            continue
        text = page.read_text(encoding="utf-8")
        title = _title(text, relative)
        pages.append((title, relative))
        explicit = [_canonical_topic(tag) for tag in _front_matter_tags(text)]
        for topic in dict.fromkeys(explicit or _inferred_topics(text, relative, title)):
            groups[topic].append((title, relative))

    rendered = _render(groups, pages)
    topic_path.parent.mkdir(parents=True, exist_ok=True)
    if not topic_path.exists() or topic_path.read_text(encoding="utf-8") != rendered:
        topic_path.write_text(rendered, encoding="utf-8")
    return config
