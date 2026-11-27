#!/usr/bin/env python3
"""Feed a sessfork-exported Playwright storageState directly into Crawl4AI.

Zero re-login, zero multi-GB Chrome profile copy, zero cloud upload.

Usage:
    # 1. Fork only the domain you need (zero window focus theft, 0600 JSON)
    sessfork docs.example.com --target none --state-file ~/.sessfork/crawl4ai.json

    # 2. Crawl authenticated URLs locally with Crawl4AI
    python3 examples/crawl4ai_auth_crawl.py \
        --state-file ~/.sessfork/crawl4ai.json \
        --url https://docs.example.com/private/architecture \
        --fit-markdown

    # 3. Optional: export JSONL straight into skills/xerj-auth-ingest/ingest_to_xerj.py
    python3 examples/crawl4ai_auth_crawl.py \
        --state-file ~/.sessfork/crawl4ai.json \
        --url https://docs.example.com/private/architecture \
        --out-jsonl ./captured-auth/crawl4ai.jsonl
    python3 skills/xerj-auth-ingest/ingest_to_xerj.py ./captured-auth/crawl4ai.jsonl \
        --out ./xerj-clean --json

Honest boundaries:
    - Complements Crawl4AI's ManagedBrowser (not a replacement for strict
      anti-bot / device-fingerprint portals that require a persistent profile).
    - Avoid concurrent foreground/headless refreshes on sites that enforce
      single-use OAuth refresh-token rotation.
    - Never run against brokerage, banking, KYC, or 2FA settings portals.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Sequence


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_http_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned.startswith(("http://", "https://")):
        raise ValueError(f"Only http:// and https:// URLs are supported: {url!r}")
    return cleaned


def extract_crawl4ai_markdown(result: Any, *, prefer_fit: bool = True) -> str:
    """Extract clean Markdown text from a Crawl4AI CrawlResult across v0.5-v0.9+."""
    md_obj = getattr(result, "markdown", None)
    if md_obj is None:
        return ""
    if isinstance(md_obj, str):
        return md_obj.strip()
    if isinstance(md_obj, dict):
        keys = ("fit_markdown", "raw_markdown") if prefer_fit else ("raw_markdown", "fit_markdown")
        for key in keys:
            val = md_obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""
    attrs = ("fit_markdown", "raw_markdown") if prefer_fit else ("raw_markdown", "fit_markdown")
    for attr in attrs:
        val = getattr(md_obj, attr, None)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def crawl_result_to_record(
    result: Any,
    *,
    prefer_fit: bool = True,
    captured_at: str | None = None,
) -> dict[str, Any]:
    """Convert a Crawl4AI CrawlResult into a JSONL record for ingest_to_xerj.py."""
    metadata = getattr(result, "metadata", None)
    title = ""
    if isinstance(metadata, dict):
        title = str(metadata.get("title") or "").strip()
    source_url = str(getattr(result, "url", "") or "").strip()
    markdown = extract_crawl4ai_markdown(result, prefer_fit=prefer_fit)
    return {
        "source_url": source_url,
        "title": title,
        "captured_at": captured_at or _utc_now_iso(),
        "markdown": markdown,
        "status_code": getattr(result, "status_code", None),
    }


async def run_authenticated_crawl(
    *,
    urls: Sequence[str],
    state_file: Path,
    fit_markdown: bool = False,
    out_jsonl: Path | None = None,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
    except ImportError as exc:
        raise RuntimeError(
            "crawl4ai is not installed in this Python environment. "
            "Install it with: pip install crawl4ai && crawl4ai-setup"
        ) from exc

    resolved_state = state_file.expanduser().resolve()
    if not resolved_state.is_file():
        raise FileNotFoundError(
            f"storageState file not found: {resolved_state}\n"
            f"Fork your target domain first, e.g.:\n"
            f"  sessfork example.com --target none --state-file {state_file}"
        )

    validated_urls = [validate_http_url(u) for u in urls]
    if not validated_urls:
        raise ValueError("At least one --url is required")

    markdown_generator = None
    if fit_markdown:
        from crawl4ai.content_filter_strategy import PruningContentFilter
        from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

        markdown_generator = DefaultMarkdownGenerator(
            content_filter=PruningContentFilter(threshold=0.45, threshold_type="fixed")
        )

    browser_cfg = BrowserConfig(
        headless=True,
        verbose=verbose,
        storage_state=str(resolved_state),
    )
    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        markdown_generator=markdown_generator,
    )

    records: list[dict[str, Any]] = []
    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        for target_url in validated_urls:
            result = await crawler.arun(url=target_url, config=run_cfg)
            if not getattr(result, "success", False):
                err = getattr(result, "error_message", "unknown crawl error")
                raise RuntimeError(f"Crawl failed for {target_url}: {err}")
            records.append(crawl_result_to_record(result, prefer_fit=fit_markdown))

    if out_jsonl is not None:
        out_path = out_jsonl.expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    return records


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawl4ai_auth_crawl.py",
        description="Run local authenticated Crawl4AI scrapes using a sessfork storageState file.",
    )
    parser.add_argument(
        "--state-file",
        default="~/.sessfork/crawl4ai.json",
        help="Path to the 0600 Playwright storageState JSON written by sessfork.",
    )
    parser.add_argument(
        "--url",
        action="append",
        required=True,
        dest="urls",
        help="Authenticated http(s) URL to crawl (repeatable).",
    )
    parser.add_argument(
        "--fit-markdown",
        action="store_true",
        help="Enable Crawl4AI PruningContentFilter fit_markdown output.",
    )
    parser.add_argument(
        "--out-jsonl",
        default=None,
        help="Optional JSONL path ready for skills/xerj-auth-ingest/ingest_to_xerj.py.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show Crawl4AI progress logs.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        records = asyncio.run(
            run_authenticated_crawl(
                urls=args.urls,
                state_file=Path(args.state_file),
                fit_markdown=args.fit_markdown,
                out_jsonl=Path(args.out_jsonl) if args.out_jsonl else None,
                verbose=args.verbose,
            )
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.out_jsonl:
        print(
            json.dumps(
                {
                    "ok": True,
                    "pages_crawled": len(records),
                    "out_jsonl": str(Path(args.out_jsonl).expanduser()),
                },
                ensure_ascii=False,
            )
        )
    else:
        for rec in records:
            print(f"# {rec['title'] or rec['source_url']}\n")
            print(rec["markdown"])
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
