#!/usr/bin/env python3
"""
Notion Export Cleanup Script

Processes a Notion markdown export to:
1. Extract title→hash mappings from markdown links
2. Identify ## sections and their associated links
3. Create subfolders for each section
4. Move files into appropriate subfolders
5. Remove hashes from filenames (handling duplicates)
6. Update all links to reflect new paths
"""

import os
import re
import shutil
from pathlib import Path
from urllib.parse import unquote, quote
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LinkInfo:
    """Represents a markdown link with its metadata."""
    original_text: str      # Full markdown link text [title](path)
    title: str              # Link title
    original_path: str      # Original path (URL-encoded)
    decoded_path: str       # URL-decoded path
    filename: str           # Just the filename
    hash_id: Optional[str]  # Extracted hash (if any)
    clean_name: str         # Filename without hash
    line_number: int = 0    # Line number where link appears
    section: Optional[str] = None  # Section this link belongs to
    new_path: Optional[str] = None  # Updated path after processing


@dataclass
class Section:
    """Represents a ## section in the markdown."""
    title: str
    start_line: int
    end_line: Optional[int] = None
    links: list[LinkInfo] = field(default_factory=list)
    folder_name: str = ""


def extract_hash_from_filename(filename: str) -> tuple[str, Optional[str]]:
    """
    Extract the Notion hash from a filename.

    Pattern: "Name With Spaces HASH.md" -> ("Name With Spaces", "HASH")
    Hash is typically 32 hex chars at the end before .md
    """
    # Remove .md extension
    base = filename[:-3] if filename.endswith('.md') else filename

    # Match pattern: everything up to last space, then 32 hex chars
    match = re.match(r'^(.+)\s+([a-f0-9]{32})$', base)
    if match:
        return match.group(1), match.group(2)

    # Also try shorter hashes (some Notion exports use different lengths)
    match = re.match(r'^(.+)\s+([a-f0-9]{20,})$', base)
    if match:
        return match.group(1), match.group(2)

    return base, None


def parse_markdown_links(content: str) -> list[LinkInfo]:
    """Extract all markdown links from content with line numbers."""
    # Pattern for [title](path.md)
    link_pattern = r'\[([^\]]+)\]\(([^)]+\.md)\)'
    links = []

    lines = content.split('\n')
    for line_num, line in enumerate(lines):
        for match in re.finditer(link_pattern, line):
            original_text = match.group(0)
            title = match.group(1)
            original_path = match.group(2)
            decoded_path = unquote(original_path)
            filename = os.path.basename(decoded_path)
            clean_name, hash_id = extract_hash_from_filename(filename)

            links.append(LinkInfo(
                original_text=original_text,
                title=title,
                original_path=original_path,
                decoded_path=decoded_path,
                filename=filename,
                hash_id=hash_id,
                clean_name=clean_name,
                line_number=line_num
            ))

    return links


def parse_sections(content: str) -> list[Section]:
    """Parse ## sections from markdown content."""
    lines = content.split('\n')
    sections = []
    current_section = None

    # Pattern for ## headers (but not ### or more)
    section_pattern = r'^##\s+(.+)$'

    for i, line in enumerate(lines):
        match = re.match(section_pattern, line)
        if match:
            # Close previous section
            if current_section:
                current_section.end_line = i - 1
                sections.append(current_section)

            section_title = match.group(1).strip()
            current_section = Section(
                title=section_title,
                start_line=i,
                folder_name=section_title  # Use title as folder name
            )

    # Close final section
    if current_section:
        current_section.end_line = len(lines) - 1
        sections.append(current_section)

    return sections


def assign_links_to_sections(sections: list[Section], links: list[LinkInfo]) -> None:
    """Assign each link to its containing section based on line numbers."""
    for link in links:
        for section in sections:
            end_line = section.end_line if section.end_line is not None else float('inf')
            if section.start_line <= link.line_number <= end_line:
                link.section = section.title
                section.links.append(link)
                break


def generate_unique_filename(base_name: str, existing: set[str]) -> str:
    """Generate a unique filename, incrementing if necessary."""
    candidate = f"{base_name}.md"
    if candidate.lower() not in existing:
        return candidate

    counter = 1
    while True:
        candidate = f"{base_name} ({counter}).md"
        if candidate.lower() not in existing:
            return candidate
        counter += 1


def find_content_directory(index_path: Path) -> Path:
    """
    Find the content directory for a Notion export.

    The content folder has the same base name as the index file (without hash).
    """
    index_clean_name, _ = extract_hash_from_filename(index_path.name)

    # Look for folder matching the clean name
    parent = index_path.parent
    for item in parent.iterdir():
        if item.is_dir():
            folder_clean, _ = extract_hash_from_filename(item.name)
            if folder_clean == index_clean_name:
                return item

    # Fallback: use the stem of the index file
    return parent / index_path.stem


def process_notion_export(index_file: str, dry_run: bool = False) -> dict:
    """
    Main processing function.

    Args:
        index_file: Path to the main index markdown file
        dry_run: If True, only report what would be done

    Returns:
        Dictionary with processing results
    """
    index_path = Path(index_file).resolve()
    base_dir = index_path.parent

    # Find content directory (handles hashed folder names)
    content_dir = find_content_directory(index_path)

    # Get clean names for paths
    index_clean_name, index_hash = extract_hash_from_filename(index_path.name)
    content_clean_name, content_hash = extract_hash_from_filename(content_dir.name)

    print(f"Index file: {index_path}")
    print(f"Index clean name: {index_clean_name}")
    print(f"Content directory: {content_dir}")
    print(f"Content clean name: {content_clean_name}")

    if not content_dir.exists():
        print(f"ERROR: Content directory not found: {content_dir}")
        return {}

    # Read the index file
    with open(index_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Parse sections and links
    sections = parse_sections(content)
    links = parse_markdown_links(content)

    print(f"\nFound {len(sections)} sections:")
    for s in sections:
        print(f"  - {s.title} (lines {s.start_line}-{s.end_line})")

    print(f"\nFound {len(links)} links")

    # Assign links to sections
    assign_links_to_sections(sections, links)

    # Build hash -> clean_name mapping
    hash_to_clean = {}
    for link in links:
        if link.hash_id:
            hash_to_clean[link.hash_id] = link.clean_name

    print(f"\nHash mappings ({len(hash_to_clean)}):")
    for h, name in list(hash_to_clean.items())[:5]:
        print(f"  {h[:8]}... -> {name}")
    if len(hash_to_clean) > 5:
        print(f"  ... and {len(hash_to_clean) - 5} more")

    # Track file operations
    operations = []
    link_updates = {}  # old_path -> new_path

    # Process each section
    for section in sections:
        if not section.links:
            continue

        section_folder = content_dir / section.folder_name

        print(f"\nSection: {section.title}")
        print(f"  Folder: {section_folder}")
        print(f"  Links: {len(section.links)}")

        # Track existing files in this section folder (for duplicate detection)
        existing_in_folder = set()
        if section_folder.exists():
            existing_in_folder = {f.name.lower() for f in section_folder.iterdir() if f.is_file()}

        for link in section.links:
            # Extract the relative path from the link
            link_relative = link.decoded_path

            # Remove the content folder prefix if present
            if link_relative.startswith(content_dir.name + '/'):
                link_relative = link_relative[len(content_dir.name) + 1:]
            elif link_relative.startswith(content_clean_name + '/'):
                link_relative = link_relative[len(content_clean_name) + 1:]

            # Try to find the source file
            source_path = content_dir / link_relative

            if not source_path.exists():
                # Try direct path from base_dir
                source_path = base_dir / link.decoded_path

            if not source_path.exists():
                # Try in the content directory root
                source_path = content_dir / link.filename

            if not source_path.exists():
                print(f"    WARNING: Source not found: {link.filename}")
                print(f"      Tried: {content_dir / link_relative}")
                continue

            # Generate clean filename (handling duplicates)
            clean_filename = generate_unique_filename(link.clean_name, existing_in_folder)
            existing_in_folder.add(clean_filename.lower())

            # Destination path
            dest_path = section_folder / clean_filename

            # New link path (URL-encoded, using clean content folder name)
            new_link_path = f"{content_clean_name}/{quote(section.folder_name)}/{quote(clean_filename)}"

            link.new_path = new_link_path
            link_updates[link.original_path] = new_link_path

            operations.append({
                'type': 'move',
                'source': str(source_path),
                'dest': str(dest_path),
                'section': section.title
            })

            print(f"    {link.filename}")
            print(f"      -> {section.folder_name}/{clean_filename}")

    # Execute operations
    if not dry_run:
        print("\n" + "="*60)
        print("EXECUTING OPERATIONS")
        print("="*60)

        # Create folders
        folders_created = set()
        for op in operations:
            dest = Path(op['dest'])
            folder = dest.parent
            if folder not in folders_created:
                folder.mkdir(parents=True, exist_ok=True)
                folders_created.add(folder)
                print(f"Created folder: {folder}")

        # Move files
        for op in operations:
            source = Path(op['source'])
            dest = Path(op['dest'])
            if source.exists() and source != dest:
                shutil.move(str(source), str(dest))
                print(f"Moved: {source.name} -> {dest.parent.name}/{dest.name}")

        # Rename the index file to remove hash
        new_index_path = base_dir / f"{index_clean_name}.md"
        if index_path != new_index_path:
            shutil.move(str(index_path), str(new_index_path))
            print(f"Renamed index: {index_path.name} -> {new_index_path.name}")
            index_path = new_index_path

        # Rename content directory to remove hash
        new_content_dir = base_dir / content_clean_name
        if content_dir != new_content_dir and content_dir.exists():
            if new_content_dir.exists():
                # Merge directories
                for item in content_dir.iterdir():
                    dest = new_content_dir / item.name
                    if item.is_file() and not dest.exists():
                        shutil.move(str(item), str(dest))
                    elif item.is_dir():
                        if dest.exists():
                            for subitem in item.iterdir():
                                shutil.move(str(subitem), str(dest / subitem.name))
                            item.rmdir()
                        else:
                            shutil.move(str(item), str(dest))
                # Remove old directory if empty
                try:
                    content_dir.rmdir()
                except OSError:
                    pass
            else:
                shutil.move(str(content_dir), str(new_content_dir))
            print(f"Renamed content dir: {content_dir.name} -> {new_content_dir.name}")
            content_dir = new_content_dir

        # Update links in the index file
        with open(index_path, 'r', encoding='utf-8') as f:
            new_content = f.read()

        for old_path, new_path in link_updates.items():
            new_content = new_content.replace(f"]({old_path})", f"]({new_path})")

        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"\nUpdated links in: {index_path.name}")

        # Update any sub-index files
        update_nested_links(content_dir, links, hash_to_clean)

    return {
        'sections': sections,
        'links': links,
        'operations': operations,
        'hash_mappings': hash_to_clean
    }


def update_nested_links(content_dir: Path, links: list[LinkInfo], hash_to_clean: dict) -> None:
    """Update links in nested markdown files."""
    for md_file in content_dir.rglob("*.md"):
        try:
            with open(md_file, 'r', encoding='utf-8') as f:
                file_content = f.read()

            original_content = file_content

            # Find all links in this file
            link_pattern = r'\[([^\]]+)\]\(([^)]+\.md)\)'

            def replace_link(match):
                title = match.group(1)
                path = match.group(2)
                decoded = unquote(path)
                filename = os.path.basename(decoded)
                clean_name, hash_id = extract_hash_from_filename(filename)

                if hash_id:
                    # Remove hash from the path
                    new_filename = f"{clean_name}.md"
                    new_path = path.rsplit('/', 1)
                    if len(new_path) > 1:
                        new_path = new_path[0] + '/' + quote(new_filename)
                    else:
                        new_path = quote(new_filename)
                    return f"[{title}]({new_path})"
                return match.group(0)

            file_content = re.sub(link_pattern, replace_link, file_content)

            if file_content != original_content:
                with open(md_file, 'w', encoding='utf-8') as f:
                    f.write(file_content)
                print(f"Updated links in: {md_file.name}")
        except Exception as e:
            print(f"Warning: Could not process {md_file}: {e}")


def cleanup_empty_folders(directory: Path) -> None:
    """Remove empty folders after moving files."""
    for folder in sorted(directory.rglob("*"), reverse=True):
        if folder.is_dir():
            try:
                folder.rmdir()  # Only succeeds if empty
                print(f"Removed empty folder: {folder}")
            except OSError:
                pass  # Not empty, skip


def rename_files_remove_hashes(directory: Path, dry_run: bool = False) -> dict[str, str]:
    """
    Standalone function to remove hashes from all .md filenames in a directory.

    Returns mapping of old_path -> new_path for link updates.
    """
    renames = {}

    for md_file in directory.rglob("*.md"):
        clean_name, hash_id = extract_hash_from_filename(md_file.name)
        if hash_id:
            # Check for duplicates in same folder
            existing = {f.name.lower() for f in md_file.parent.iterdir() if f.is_file() and f != md_file}
            new_filename = generate_unique_filename(clean_name, existing)
            new_path = md_file.parent / new_filename

            renames[str(md_file)] = str(new_path)

            if not dry_run:
                shutil.move(str(md_file), str(new_path))
                print(f"Renamed: {md_file.name} -> {new_filename}")

    return renames


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python notion_cleanup.py <index_file.md> [--dry-run]")
        print("\nExample:")
        print("  python notion_cleanup.py WebSpec.md")
        print("  python notion_cleanup.py 'WebSpec 2c942c9038be80c2b26ee86a5ea677c5.md'")
        print("  python notion_cleanup.py WebSpec.md --dry-run")
        sys.exit(1)

    index_file = sys.argv[1]
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        print("DRY RUN MODE - No changes will be made\n")

    result = process_notion_export(index_file, dry_run=dry_run)

    # Cleanup orphaned files (files not linked in index but still have hashes)
    if not dry_run and result:
        base_dir = Path(index_file).parent
        index_clean_name, _ = extract_hash_from_filename(Path(index_file).name)
        content_dir = base_dir / index_clean_name
        if content_dir.exists():
            print("\n" + "="*60)
            print("CLEANING UP ORPHANED FILES")
            print("="*60)
            orphan_renames = rename_files_remove_hashes(content_dir)
            if orphan_renames:
                # Update links in all md files for orphaned files too
                update_nested_links(content_dir, [], {})
            cleanup_empty_folders(content_dir)

    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    if result:
        print(f"Sections processed: {len(result['sections'])}")
        print(f"Links processed: {len(result['links'])}")
        print(f"File operations: {len(result['operations'])}")
        print(f"Hash mappings extracted: {len(result['hash_mappings'])}")
