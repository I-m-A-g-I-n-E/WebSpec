#!/usr/bin/env python3
"""Sync MkDocs docs to GitHub Wiki format.

Reads mkdocs.yml nav structure, flattens docs into wiki-compatible flat files,
rewrites internal links, and generates _Sidebar.md.

Flattening scheme:
  docs/auth/token-structure.md  -> Authentication---Token-Structure.md
  docs/auth/index.md            -> Authentication.md
  docs/index.md                 -> Home.md
"""

import re
import shutil
import sys
from pathlib import Path

import yaml


def slugify(text: str) -> str:
    """Convert a nav title to a wiki-safe slug."""
    text = text.strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text


def build_page_map(nav: list, docs_dir: Path, section_title: str = "") -> dict:
    """Walk the mkdocs nav and build {source_path: (wiki_name, display_title)} mapping."""
    page_map = {}

    for item in nav:
        if isinstance(item, str):
            # Top-level page like "index.md"
            src = item
            if src == "index.md":
                page_map[src] = ("Home", "Home")
            else:
                title = Path(src).stem.replace("-", " ").title()
                page_map[src] = (slugify(title), title)

        elif isinstance(item, dict):
            for title, value in item.items():
                if isinstance(value, str):
                    # Named page: "Title: path.md"
                    src = value
                    if src == "index.md":
                        page_map[src] = ("Home", "Home")
                    elif src.endswith("/index.md"):
                        # Section index -> use section title
                        page_map[src] = (slugify(title), title)
                    elif section_title:
                        wiki_name = f"{slugify(section_title)}---{slugify(title)}"
                        page_map[src] = (wiki_name, title)
                    else:
                        page_map[src] = (slugify(title), title)

                elif isinstance(value, list):
                    # Section with children
                    child_map = build_page_map(value, docs_dir, section_title=title)
                    page_map.update(child_map)

    return page_map


def rewrite_links(content: str, current_src: str, page_map: dict) -> str:
    """Rewrite relative markdown links to wiki page names."""
    current_dir = str(Path(current_src).parent)

    def replace_link(match):
        text = match.group(1)
        target = match.group(2)

        # Skip external links and anchors
        if target.startswith(("http://", "https://", "#", "mailto:")):
            return match.group(0)

        # Split off anchor
        anchor = ""
        if "#" in target:
            target, anchor = target.split("#", 1)
            anchor = f"#{anchor}"

        # Resolve relative path
        if current_dir and current_dir != ".":
            resolved = str(Path(current_dir) / target)
        else:
            resolved = target

        # Normalize
        resolved = str(Path(resolved))

        # Look up in page map
        if resolved in page_map:
            wiki_name = page_map[resolved][0]
            return f"[[{wiki_name}{anchor}|{text}]]"

        # Try without .md extension
        if not resolved.endswith(".md"):
            resolved_md = resolved + ".md"
            if resolved_md in page_map:
                wiki_name = page_map[resolved_md][0]
                return f"[[{wiki_name}{anchor}|{text}]]"

        # Try as directory index
        resolved_index = str(Path(resolved) / "index.md")
        if resolved_index in page_map:
            wiki_name = page_map[resolved_index][0]
            return f"[[{wiki_name}{anchor}|{text}]]"

        return match.group(0)

    # Match [text](target) but not ![image](url)
    return re.sub(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)", replace_link, content)


def generate_sidebar(nav: list, page_map: dict, indent: int = 0) -> str:
    """Generate _Sidebar.md from nav structure."""
    lines = []
    prefix = "  " * indent

    for item in nav:
        if isinstance(item, str):
            if item in page_map:
                wiki_name, title = page_map[item]
                lines.append(f"{prefix}* [[{wiki_name}|{title}]]")

        elif isinstance(item, dict):
            for title, value in item.items():
                if isinstance(value, str):
                    if value in page_map:
                        wiki_name, display = page_map[value]
                        lines.append(f"{prefix}* [[{wiki_name}|{title}]]")
                    else:
                        lines.append(f"{prefix}* {title}")

                elif isinstance(value, list):
                    # Section header - check if first child is an index
                    first_child = value[0] if value else None
                    section_page = None

                    if isinstance(first_child, str) and first_child.endswith("/index.md"):
                        section_page = first_child
                    elif isinstance(first_child, dict):
                        for _, v in first_child.items():
                            if isinstance(v, str) and v.endswith("/index.md"):
                                section_page = v
                            break

                    if section_page and section_page in page_map:
                        wiki_name = page_map[section_page][0]
                        lines.append(f"{prefix}* **[[{wiki_name}|{title}]]**")
                    else:
                        lines.append(f"{prefix}* **{title}**")

                    lines.append(generate_sidebar(value, page_map, indent + 1))

    return "\n".join(lines)


def main():
    repo_root = Path(__file__).resolve().parent.parent.parent
    docs_dir = repo_root / "docs"
    mkdocs_path = repo_root / "mkdocs.yml"

    if len(sys.argv) < 2:
        print("Usage: sync_wiki.py <wiki_dir>")
        sys.exit(1)

    wiki_dir = Path(sys.argv[1])
    wiki_dir.mkdir(parents=True, exist_ok=True)

    # Load mkdocs config
    with open(mkdocs_path) as f:
        config = yaml.safe_load(f)

    nav = config.get("nav", [])
    page_map = build_page_map(nav, docs_dir)

    print(f"Found {len(page_map)} pages to sync")

    # Clean existing wiki markdown files (preserve .git)
    for item in wiki_dir.iterdir():
        if item.name == ".git":
            continue
        if item.is_file():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)

    # Copy and transform each page
    for src_path, (wiki_name, title) in page_map.items():
        src_file = docs_dir / src_path
        if not src_file.exists():
            print(f"  SKIP (missing): {src_path}")
            continue

        content = src_file.read_text()
        content = rewrite_links(content, src_path, page_map)

        dest = wiki_dir / f"{wiki_name}.md"
        dest.write_text(content)
        print(f"  {src_path} -> {wiki_name}.md")

    # Generate sidebar
    sidebar = generate_sidebar(nav, page_map)
    (wiki_dir / "_Sidebar.md").write_text(sidebar)
    print("Generated _Sidebar.md")

    # Generate footer
    footer = (
        "---\n"
        "[View on GitHub Pages](https://webspec.gimme.tools) | "
        "[Edit on GitHub](https://github.com/I-m-A-g-I-n-E/WebSpec)\n"
    )
    (wiki_dir / "_Footer.md").write_text(footer)
    print("Generated _Footer.md")


if __name__ == "__main__":
    main()
