#!/usr/bin/env python3
"""Sanitize captured authenticated web pages into UTF-8 Markdown and optionally index into local XERJ.

Zero third-party dependencies (Python 3.9+ stdlib only).
- Converts HTML / Markdown / JSONL captures into clean Markdown with YAML frontmatter.
- Strips scripts, styles, SVGs, HTML comments, and navigation boilerplate while preserving CJK text.
- Redacts accidental credentials (Authorization Bearer/ApiKey, Cookie/Set-Cookie, hf_, ghp_, sk-, JWTs)
  into <REDACTED> BEFORE writing any output file.
- Enforces loopback-only XERJ endpoints by default (127.0.0.1, ::1, localhost) and rejects path traversal.
- Defaults to --dry-run (writes cleaned Markdown only); pass --index to run `xerj autoindex`.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Sequence

DEFAULT_XERJ_URL = "http://127.0.0.1:9200"
REDACTED = "<REDACTED>"

SUPPORTED_SUFFIXES = frozenset({".html", ".htm", ".md", ".markdown", ".txt", ".jsonl"})
FORBIDDEN_SUFFIXES = frozenset(
    {".pem", ".key", ".p12", ".pfx", ".jks", ".kdbx", ".sqlite", ".sqlite3", ".db"}
)
FORBIDDEN_FILENAMES = frozenset(
    {".env", ".sessfork-state.json", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}
)

SKIP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "svg",
        "iframe",
        "canvas",
        "template",
        "object",
        "embed",
        "nav",
        "footer",
        "aside",
        "form",
        "dialog",
        "button",
        "select",
        "textarea",
    }
)
VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
BLOCK_TAGS = frozenset({"p", "div", "section", "article", "main", "blockquote", "tr"})
PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Ordered from most specific structured headers to raw token patterns.
_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
        REDACTED,
    ),
    (
        re.compile(
            r"(?i)\b((?:Proxy-)?Authorization\s*:\s*(?:Bearer|ApiKey|Basic|Token)\s+)(?!<REDACTED>)\S+"
        ),
        r"\1" + REDACTED,
    ),
    (
        re.compile(
            r"(?i)\b((?:Proxy-)?Authorization\s*:\s*)(?!(?:Bearer|ApiKey|Basic|Token)\b)(?!<REDACTED>)\S+"
        ),
        r"\1" + REDACTED,
    ),
    (
        re.compile(r"(?i)\b(Bearer\s+)(?!<REDACTED>)[A-Za-z0-9._~+/=-]{8,}"),
        r"\1" + REDACTED,
    ),
    (
        re.compile(r"(?i)\b(ApiKey\s+)(?!<REDACTED>)[A-Za-z0-9._~+/=-]{8,}"),
        r"\1" + REDACTED,
    ),
    (
        re.compile(r"(?im)^(\s*(?:Set-)?Cookie\s*:\s*)(?!<REDACTED>).+$"),
        r"\1" + REDACTED,
    ),
    (
        re.compile(
            r"(?i)(?<!\w)([\"']?(?:Set-)?Cookie[\"']?\s*:\s*[\"']?)(?!<REDACTED>)"
            r"[A-Za-z0-9_.-]+=[^;\n\r\"'<>]+(?:\s*;\s*[A-Za-z0-9_.-]+(?:=[^;\n\r\"'<>]*)?)*"
        ),
        r"\1" + REDACTED,
    ),
    (
        re.compile(
            r"(?i)\b(X-(?:API|Auth|CSRF|Session|Access)-?(?:Key|Token)\s*:\s*)(?!<REDACTED>)\S+"
        ),
        r"\1" + REDACTED,
    ),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        REDACTED,
    ),
    (
        re.compile(r"\bhf_[A-Za-z0-9]{10,}\b"),
        REDACTED,
    ),
    (
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{20,})\b"),
        REDACTED,
    ),
    (
        re.compile(r"\bsk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_-]{12,}\b"),
        REDACTED,
    ),
    (
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
        REDACTED,
    ),
    (
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        REDACTED,
    ),
    (
        re.compile(
            r"(?i)([?&](?:access_token|id_token|refresh_token|api_key|apikey|"
            r"token|auth_token|secret|signature|session_id|password|passwd)=)"
            r"(?!<REDACTED>)[^&#\s\"'<>]+"
        ),
        r"\1" + REDACTED,
    ),
)

_HTML_SCRIPT_STYLE_RE = re.compile(
    r"(?is)<(script|style|noscript|svg)\b[^>]*>.*?</\1>|<!--[\s\S]*?-->"
)


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_safe_path(raw: str | Path, *, label: str = "path") -> Path:
    """Reject null bytes and any '..' path traversal segment across POSIX and Windows separators."""
    raw_str = os.fspath(raw)
    if not isinstance(raw_str, str) or not raw_str.strip():
        raise ValueError(f"{label} must be a non-empty path")
    if "\x00" in raw_str:
        raise ValueError(f"Null byte is forbidden in {label}")
    segments = re.split(r"[/\\]+", raw_str)
    if ".." in segments:
        raise ValueError(f"Path traversal ('..') is forbidden in {label}: {raw_str}")
    return Path(raw_str)


def _assert_not_sensitive_file(path: Path) -> None:
    """Refuse to ingest session stores, key material, or .env files."""
    raw_parts = {p.lower() for p in path.parts}
    resolved = path.resolve()
    resolved_parts = {p.lower() for p in resolved.parts}
    all_parts = raw_parts | resolved_parts
    name_lower = path.name.lower()
    resolved_name_lower = resolved.name.lower()

    if ".sessfork" in all_parts:
        raise ValueError(f"Refusing to ingest sessfork session/key directory file: {path}")
    for candidate in (name_lower, resolved_name_lower):
        if candidate in FORBIDDEN_FILENAMES or candidate.startswith(".env."):
            raise ValueError(f"Refusing to ingest sensitive credential file: {path}")
    if path.suffix.lower() in FORBIDDEN_SUFFIXES or resolved.suffix.lower() in FORBIDDEN_SUFFIXES:
        raise ValueError(f"Refusing to ingest key/database file: {path}")


def validate_xerj_url(url: str, *, allow_remote: bool = False) -> str:
    """Validate XERJ endpoint URL; restrict to loopback unless --allow-remote with https://."""
    cleaned = url.strip()
    if not cleaned or any(ch.isspace() or ord(ch) < 32 for ch in cleaned):
        raise ValueError(f"Invalid XERJ URL: {url!r}")
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"XERJ URL must use http:// or https://: {url!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Credentials embedded in XERJ URL are forbidden")
    if parsed.query or parsed.fragment:
        raise ValueError("XERJ URL must not contain query parameters or fragments")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(f"XERJ URL is missing a hostname: {url!r}")

    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False

    if not is_loopback:
        if not allow_remote:
            raise ValueError(
                f"Refusing non-loopback XERJ URL '{cleaned}' without --allow-remote "
                "(default allows only 127.0.0.1, ::1, or localhost)"
            )
        if parsed.scheme != "https":
            raise ValueError(
                f"Remote XERJ URL '{cleaned}' with --allow-remote must use https://"
            )

    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def validate_prefix(prefix: str) -> str:
    cleaned = prefix.strip()
    if not PREFIX_RE.fullmatch(cleaned):
        raise ValueError(
            f"Invalid --prefix {prefix!r}: must start with alphanumeric and contain only [A-Za-z0-9._-]"
        )
    return cleaned


def redact_secrets(text: str) -> tuple[str, int]:
    """Scrub accidental auth tokens, headers, cookies, and keys into <REDACTED>."""
    total = 0
    current = text
    for pattern, repl in _REDACTION_PATTERNS:
        current, count = pattern.subn(repl, current)
        total += count
    return current, total


class _CleanHTMLToMarkdown(HTMLParser):
    """Minimal zero-dependency HTML-to-Markdown converter preserving CJK and stripping chrome."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_stack: list[str] = []
        self._chunks: list[str] = []
        self._title_parts: list[str] = []
        self._in_title = False
        self._in_pre = False
        self._in_code = False
        self._first_cell_in_row = True
        self.canonical_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}

        if tag == "link" and "canonical" in attr_map.get("rel", "").lower().split():
            href = attr_map.get("href", "").strip()
            if href and not self.canonical_url:
                self.canonical_url = href
            return
        if tag == "meta":
            prop = attr_map.get("property", "").lower() or attr_map.get("name", "").lower()
            if prop == "og:url" and not self.canonical_url:
                content = attr_map.get("content", "").strip()
                if content:
                    self.canonical_url = content
            return

        if tag == "title" and not self._skip_stack:
            self._in_title = True
            return

        if self._skip_stack:
            if tag in SKIP_TAGS and tag not in VOID_TAGS:
                self._skip_stack.append(tag)
            return

        if tag in SKIP_TAGS:
            if tag not in VOID_TAGS:
                self._skip_stack.append(tag)
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            self._ensure_blank_line()
            self._chunks.append("#" * level + " ")
        elif tag == "li":
            self._ensure_newline()
            self._chunks.append("- ")
        elif tag == "br":
            self._chunks.append("\n")
        elif tag == "hr":
            self._ensure_blank_line()
            self._chunks.append("---\n\n")
        elif tag == "pre":
            self._ensure_blank_line()
            self._chunks.append("```\n")
            self._in_pre = True
        elif tag == "code" and not self._in_pre:
            self._chunks.append("`")
            self._in_code = True
        elif tag == "blockquote":
            self._ensure_blank_line()
            self._chunks.append("> ")
        elif tag == "tr":
            self._ensure_newline()
            self._first_cell_in_row = True
        elif tag in ("td", "th"):
            if not self._first_cell_in_row:
                self._chunks.append(" | ")
            self._first_cell_in_row = False
        elif tag in BLOCK_TAGS:
            self._ensure_blank_line()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
            return

        if self._skip_stack:
            if tag in self._skip_stack:
                while self._skip_stack:
                    popped = self._skip_stack.pop()
                    if popped == tag:
                        break
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._ensure_blank_line()
        elif tag == "pre":
            if self._chunks and not self._chunks[-1].endswith("\n"):
                self._chunks.append("\n")
            self._chunks.append("```\n\n")
            self._in_pre = False
        elif tag == "code" and self._in_code and not self._in_pre:
            self._chunks.append("`")
            self._in_code = False
        elif tag == "li":
            self._ensure_newline()
        elif tag in ("ul", "ol"):
            self._ensure_blank_line()
        elif tag in BLOCK_TAGS:
            self._ensure_blank_line()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
            return
        if self._skip_stack:
            return
        if self._in_pre:
            self._chunks.append(data)
            return

        collapsed = re.sub(r"[ \t\r\n]+", " ", data)
        if not collapsed:
            return
        if collapsed == " ":
            if not self._chunks or self._chunks[-1].endswith(("\n", " ", "# ", "- ", "> ", "| ")):
                return
            self._chunks.append(" ")
            return

        if not self._chunks or self._chunks[-1].endswith(("\n", " ", "# ", "- ", "> ", "| ")):
            collapsed = collapsed.lstrip(" ")
        if collapsed:
            self._chunks.append(collapsed)

    def _ensure_newline(self) -> None:
        if not self._chunks:
            return
        if not self._chunks[-1].endswith("\n"):
            self._chunks.append("\n")

    def _ensure_blank_line(self) -> None:
        if not self._chunks:
            return
        tail = "".join(self._chunks[-2:])
        if tail.endswith("\n\n"):
            return
        if tail.endswith("\n"):
            self._chunks.append("\n")
        else:
            self._chunks.append("\n\n")

    def render(self) -> tuple[str, str | None, str | None]:
        raw = "".join(self._chunks)
        lines = [line.rstrip() for line in raw.splitlines()]
        cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        title = re.sub(r"\s+", " ", "".join(self._title_parts)).strip() or None
        return cleaned, title, self.canonical_url


def html_to_markdown(html_text: str) -> tuple[str, str | None, str | None]:
    parser = _CleanHTMLToMarkdown()
    parser.feed(html_text)
    parser.close()
    return parser.render()


def _parse_existing_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end_idx = text.find("\n---\n", 4)
    if end_idx == -1:
        return {}, text
    header_block = text[4:end_idx]
    body = text[end_idx + 5 :]
    meta: dict[str, str] = {}
    for line in header_block.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        k = key.strip().lower()
        v = val.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            try:
                v = json.loads(v) if v[0] == '"' else v[1:-1]
            except json.JSONDecodeError:
                v = v[1:-1]
        if k:
            meta[k] = str(v)
    return meta, body


def _infer_markdown_title(body: str, fallback: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            candidate = stripped.lstrip("#").strip()
            if candidate:
                return candidate
    return fallback


def _extract_domain(source_url: str, fallback: str = "local") -> str:
    if source_url:
        parsed = urllib.parse.urlsplit(source_url)
        if parsed.hostname:
            return parsed.hostname.lower()
    return fallback


def _sanitize_filename_stem(name: str, fallback: str = "doc") -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", name.strip()).strip("-._")
    if not cleaned or cleaned in (".", ".."):
        return fallback
    return cleaned[:80]


def build_markdown_document(
    *,
    body: str,
    source_url: str,
    captured_at: str,
    title: str,
    domain: str,
) -> tuple[str, int]:
    """Apply zero-secret-leakage redaction to metadata and body, then render UTF-8 Markdown."""
    clean_url, c1 = redact_secrets(source_url.strip())
    clean_title, c2 = redact_secrets(title.strip() or "Untitled")
    clean_domain, c3 = redact_secrets(domain.strip() or _extract_domain(clean_url))
    clean_body, c4 = redact_secrets(body.strip())
    total_redactions = c1 + c2 + c3 + c4

    frontmatter = "\n".join(
        [
            "---",
            f"source_url: {json.dumps(clean_url, ensure_ascii=False)}",
            f"captured_at: {json.dumps(captured_at.strip(), ensure_ascii=False)}",
            f"title: {json.dumps(clean_title, ensure_ascii=False)}",
            f"domain: {json.dumps(clean_domain, ensure_ascii=False)}",
            "---",
        ]
    )
    rendered = f"{frontmatter}\n\n{clean_body}\n" if clean_body else f"{frontmatter}\n"
    return rendered, total_redactions


def _collect_input_files(inputs: Sequence[Path]) -> list[tuple[Path, Path]]:
    """Return list of (file_path, relative_display_path) after traversal, symlink, and secret guards."""
    collected: list[tuple[Path, Path]] = []
    for raw_in in inputs:
        safe_in = validate_safe_path(raw_in, label="input")
        if not safe_in.exists() and not safe_in.is_symlink():
            raise FileNotFoundError(f"Input path does not exist: {safe_in}")

        if safe_in.is_file():
            _assert_not_sensitive_file(safe_in)
            if safe_in.suffix.lower() not in SUPPORTED_SUFFIXES:
                raise ValueError(
                    f"Unsupported input file extension '{safe_in.suffix}' for {safe_in}; "
                    f"supported: {sorted(SUPPORTED_SUFFIXES)}"
                )
            collected.append((safe_in, Path(safe_in.name)))
            continue

        if safe_in.is_dir():
            root_resolved = safe_in.resolve()
            for dirpath, dirnames, filenames in os.walk(safe_in, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                current_dir = Path(dirpath)
                for fname in sorted(filenames):
                    if fname.startswith("."):
                        continue
                    candidate = current_dir / fname
                    if candidate.is_symlink():
                        target_resolved = candidate.resolve()
                        if not target_resolved.is_relative_to(root_resolved):
                            raise ValueError(
                                f"Symlink escapes input directory root: {candidate} -> {target_resolved}"
                            )
                    if candidate.suffix.lower() not in SUPPORTED_SUFFIXES:
                        continue
                    _assert_not_sensitive_file(candidate)
                    rel = candidate.relative_to(safe_in)
                    collected.append((candidate, rel))
            continue

        raise ValueError(f"Input path is neither a regular file nor directory: {safe_in}")

    return collected


def _unique_output_path(out_root: Path, rel_name: Path, used: set[Path]) -> Path:
    safe_rel = validate_safe_path(rel_name, label="output filename")
    if safe_rel.is_absolute():
        raise ValueError(f"Output filename must be relative: {rel_name}")
    candidate = out_root / safe_rel
    if candidate.suffix.lower() != ".md":
        candidate = candidate.with_suffix(".md")

    resolved_root = out_root.resolve()
    resolved_candidate = candidate.resolve()
    if not resolved_candidate.is_relative_to(resolved_root):
        raise ValueError(f"Output path escapes --out directory: {candidate}")

    if candidate not in used:
        used.add(candidate)
        return candidate

    stem = candidate.stem
    parent = candidate.parent
    counter = 2
    while True:
        alt = parent / f"{stem}-{counter}.md"
        if alt not in used:
            used.add(alt)
            return alt
        counter += 1


def ingest_paths(
    inputs: Sequence[str | Path],
    out_dir: str | Path,
    *,
    source_url: str = "",
    captured_at: str | None = None,
    title: str | None = None,
    domain: str | None = None,
) -> list[dict[str, Any]]:
    """Convert raw HTML / Markdown / JSONL inputs into redacted UTF-8 Markdown under out_dir."""
    safe_out = validate_safe_path(out_dir, label="--out")
    if ".sessfork" in {p.lower() for p in safe_out.parts} or ".sessfork" in {
        p.lower() for p in safe_out.resolve().parts
    }:
        raise ValueError(f"Refusing to write output inside .sessfork directory: {safe_out}")
    safe_inputs = [validate_safe_path(p, label="input") for p in inputs]
    files = _collect_input_files(safe_inputs)
    if not files:
        raise ValueError("No supported (.html, .htm, .md, .markdown, .txt, .jsonl) files found to ingest")

    safe_out.mkdir(parents=True, exist_ok=True)
    default_ts = captured_at or utc_now_iso()
    used_paths: set[Path] = set()
    written: list[dict[str, Any]] = []

    for src_file, rel_path in files:
        raw_text = src_file.read_text(encoding="utf-8")
        suffix = src_file.suffix.lower()

        if suffix == ".jsonl":
            for idx, line in enumerate(raw_text.splitlines(), start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                record = json.loads(stripped)
                if not isinstance(record, dict):
                    raise ValueError(f"JSONL line {idx} in {src_file} must be a JSON object")

                rec_url = str(record.get("source_url") or record.get("url") or source_url or "")
                rec_ts = str(record.get("captured_at") or default_ts)
                rec_title = str(record.get("title") or title or "")
                rec_domain = str(record.get("domain") or domain or "")

                if "html" in record and record["html"]:
                    body, html_title, canonical_url = html_to_markdown(str(record["html"]))
                    rec_url = rec_url or canonical_url or ""
                    rec_title = rec_title or html_title or _infer_markdown_title(body, f"{src_file.stem}-{idx}")
                else:
                    raw_body = str(
                        record.get("markdown")
                        or record.get("content")
                        or record.get("body")
                        or record.get("text")
                        or ""
                    )
                    body = _HTML_SCRIPT_STYLE_RE.sub("", raw_body).strip()
                    rec_title = rec_title or _infer_markdown_title(body, f"{src_file.stem}-{idx}")

                rec_domain = rec_domain or _extract_domain(rec_url)
                explicit_name = record.get("output_name") or record.get("filename") or record.get("path")
                if explicit_name is not None:
                    rel_out = validate_safe_path(str(explicit_name), label="jsonl output_name")
                    if rel_out.is_absolute():
                        raise ValueError(f"JSONL output_name must be relative: {explicit_name}")
                else:
                    slug = _sanitize_filename_stem(rec_title, fallback=f"item-{idx}")
                    short_hash = hashlib.sha256(f"{rec_url}|{rec_title}|{idx}".encode("utf-8")).hexdigest()[:8]
                    rel_out = Path(f"{_sanitize_filename_stem(src_file.stem)}-{idx:04d}-{slug}-{short_hash}.md")

                rendered, redactions = build_markdown_document(
                    body=body,
                    source_url=rec_url,
                    captured_at=rec_ts,
                    title=rec_title,
                    domain=rec_domain,
                )
                dest = _unique_output_path(safe_out, rel_out, used_paths)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(rendered, encoding="utf-8")
                written.append(
                    {
                        "source_file": str(src_file),
                        "output_path": str(dest),
                        "title": rec_title,
                        "domain": rec_domain,
                        "redactions": redactions,
                    }
                )
            continue

        if suffix in (".html", ".htm"):
            body, html_title, canonical_url = html_to_markdown(raw_text)
            doc_url = source_url or canonical_url or ""
            doc_title = title or html_title or _infer_markdown_title(body, src_file.stem)
            doc_ts = default_ts
            doc_domain = domain or _extract_domain(doc_url)
        else:
            existing_meta, stripped_body = _parse_existing_frontmatter(raw_text)
            body = _HTML_SCRIPT_STYLE_RE.sub("", stripped_body).strip()
            doc_url = source_url or existing_meta.get("source_url") or existing_meta.get("url") or ""
            doc_ts = captured_at or existing_meta.get("captured_at") or default_ts
            doc_title = title or existing_meta.get("title") or _infer_markdown_title(body, src_file.stem)
            doc_domain = domain or existing_meta.get("domain") or _extract_domain(doc_url)

        rendered, redactions = build_markdown_document(
            body=body,
            source_url=doc_url,
            captured_at=doc_ts,
            title=doc_title,
            domain=doc_domain,
        )
        safe_rel_parts = [_sanitize_filename_stem(p, fallback="doc") for p in rel_path.with_suffix(".md").parts]
        dest = _unique_output_path(safe_out, Path(*safe_rel_parts), used_paths)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rendered, encoding="utf-8")
        written.append(
            {
                "source_file": str(src_file),
                "output_path": str(dest),
                "title": doc_title,
                "domain": doc_domain,
                "redactions": redactions,
            }
        )

    return written


def build_xerj_command(
    *,
    xerj_bin: str,
    url: str,
    out_dir: str | Path,
    prefix: str | None = None,
    state_dir: str | Path | None = None,
    allow_remote: bool = False,
) -> list[str]:
    """Construct an argv list for `xerj --url <url> autoindex <out_dir>` without shell expansion."""
    bin_cleaned = xerj_bin.strip()
    if not bin_cleaned or any(ch.isspace() for ch in bin_cleaned):
        raise ValueError(f"Invalid --xerj-bin executable: {xerj_bin!r}")
    if "/" in bin_cleaned or "\\" in bin_cleaned:
        validate_safe_path(bin_cleaned, label="--xerj-bin")

    safe_url = validate_xerj_url(url, allow_remote=allow_remote)
    safe_out = validate_safe_path(out_dir, label="--out").resolve()
    cmd = [bin_cleaned, "--url", safe_url, "autoindex", str(safe_out)]
    if prefix is not None:
        cmd.extend(["--prefix", validate_prefix(prefix)])
    if state_dir is not None:
        safe_state = validate_safe_path(state_dir, label="--state-dir").resolve()
        cmd.extend(["--state-dir", str(safe_state)])
    return cmd


def run_xerj_autoindex(cmd: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["XERJ_DISABLE_FEEDBACK"] = "true"
    return subprocess.run(
        cmd,
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ingest_to_xerj.py",
        description=(
            "Convert captured authenticated HTML/Markdown/JSONL files into redacted UTF-8 Markdown "
            "and optionally index them into a local XERJ node."
        ),
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Input HTML, Markdown, TXT, or JSONL files or directories to sanitize.",
    )
    parser.add_argument(
        "-o",
        "--out",
        required=True,
        help="Destination directory for sanitized UTF-8 Markdown documents.",
    )
    parser.add_argument("--source-url", default="", help="Fallback source_url for frontmatter.")
    parser.add_argument("--domain", default=None, help="Override domain in frontmatter.")
    parser.add_argument("--title", default=None, help="Override title in frontmatter.")
    parser.add_argument("--captured-at", default=None, help="Override ISO-8601 UTC captured_at.")
    parser.add_argument(
        "--url",
        default=DEFAULT_XERJ_URL,
        help=f"Target XERJ URL (loopback only by default; default: {DEFAULT_XERJ_URL}).",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow non-loopback XERJ URLs (requires https://).",
    )
    parser.add_argument("--prefix", default=None, help="XERJ autoindex --prefix value.")
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Optional XERJ autoindex --state-dir location.",
    )
    parser.add_argument(
        "--xerj-bin",
        default="xerj",
        help="Path or binary name for xerj CLI (default: xerj).",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Write sanitized Markdown without invoking xerj (this is the default).",
    )
    mode.add_argument(
        "--index",
        action="store_true",
        help="Invoke `xerj --url <url> autoindex <out_dir>` after writing sanitized Markdown.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON summary to stdout.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    dry_run = not args.index

    try:
        validated_url = validate_xerj_url(args.url, allow_remote=args.allow_remote)
        if args.prefix is not None:
            validate_prefix(args.prefix)
        validate_safe_path(args.out, label="--out")
        if args.state_dir is not None:
            validate_safe_path(args.state_dir, label="--state-dir")

        written = ingest_paths(
            args.inputs,
            args.out,
            source_url=args.source_url,
            captured_at=args.captured_at,
            title=args.title,
            domain=args.domain,
        )

        total_redactions = sum(int(item["redactions"]) for item in written)
        cmd: list[str] | None = None
        indexed = False
        xerj_stdout = ""
        xerj_stderr = ""

        if not dry_run:
            cmd = build_xerj_command(
                xerj_bin=args.xerj_bin,
                url=validated_url,
                out_dir=args.out,
                prefix=args.prefix,
                state_dir=args.state_dir,
                allow_remote=args.allow_remote,
            )
            proc = run_xerj_autoindex(cmd)
            xerj_stdout = proc.stdout
            xerj_stderr = proc.stderr
            if proc.returncode != 0:
                print(
                    f"xerj autoindex failed (exit {proc.returncode}): {xerj_stderr or xerj_stdout}".strip(),
                    file=sys.stderr,
                )
                return proc.returncode
            indexed = True

        summary: dict[str, Any] = {
            "dry_run": dry_run,
            "indexed": indexed,
            "url": validated_url,
            "out_dir": str(Path(args.out).resolve()),
            "documents_written": len(written),
            "total_redactions": total_redactions,
            "files": [item["output_path"] for item in written],
        }
        if cmd is not None:
            summary["command"] = cmd
        if xerj_stdout:
            summary["xerj_stdout"] = xerj_stdout.strip()

        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            mode_label = "INDEXED" if indexed else "DRY-RUN"
            print(
                f"[{mode_label}] Wrote {len(written)} sanitized Markdown file(s) to "
                f"{summary['out_dir']} (redacted {total_redactions} secret match(es))."
            )
            if dry_run:
                print("Pass --index to run `xerj autoindex` on this directory.")
        return 0
    except (ValueError, FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        print(f"ingest_to_xerj error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
