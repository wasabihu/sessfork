---
name: xerj-auth-ingest
description: Silently capture authenticated web pages (Notion, Feishu, Confluence, internal research portals) via sessfork + sessfork-moli / sessfork-browser with zero window focus theft, strip boilerplate and scrub accidental credentials into <REDACTED>, and index clean UTF-8 Markdown into a local loopback XERJ search node.
---

# xerj-auth-ingest — Zero-Focus-Theft Auth-to-XERJ Ingest

Use this skill when you want to read authenticated pages (Notion, Feishu/Lark, Confluence, internal wikis, or subscription research portals) in the background via `sessfork` and make their sanitized content searchable in a local [`xerj`](https://github.com/xerj-org/xerj) knowledge base — **without stealing Chrome focus and without ever leaking cookies or tokens into your index**.

## End-to-End Workflow

### 1. Fork Only the Needed Domain (Zero Focus Theft)

Never pass `--all-domains`. Fork only the exact site you need:

```bash
sessfork notion.so                 # Standard cookie + LocalStorage sites
sessfork feishu.cn                 # Feishu / Lark docs
sessfork app.example.com --no-cookies  # Pure SPA / JWT LocalStorage (zero Keychain prompt)
```

Hot-reload the forked auth state into your running headless MCP browser without restarting your editor:
- **Fast DOM-first path (`sessfork-moli`, recommended when `--engine dual` or `moli` is configured)**: read pages directly via `sessfork-moli` (`605 KB` Rust MCP, `1.7 MB` idle RSS, built-in structure-first Markdown extraction).
- **Playwright fallback (`sessfork-browser`)**: call `browser_set_storage_state` with the hot-reload path from `sessfork --status` (`~/.sessfork/playwright-output/.sessfork-state.json`), then navigate and capture clean HTML or Markdown.
- **Batch / Deep-Crawl path (`Crawl4AI` + `examples/crawl4ai_auth_crawl.py`)**: for multi-page authenticated docs or portals where you want Crawl4AI's `PruningContentFilter` (`fit_markdown`), export a domain-scoped `0600` state file and emit JSONL directly into this pipeline:
  ```bash
  sessfork docs.example.com --target none --state-file ~/.sessfork/crawl4ai.json
  python3 examples/crawl4ai_auth_crawl.py \
    --state-file ~/.sessfork/crawl4ai.json \
    --url https://docs.example.com/private/guide \
    --fit-markdown \
    --out-jsonl ./captured-auth/crawl4ai.jsonl
  ```
  `ingest_to_xerj.py` natively recognizes Crawl4AI JSONL records (`fit_markdown`, `markdown` dict `{fit_markdown, raw_markdown}`, and `metadata.title`) and runs the full `<REDACTED>` scrubber before writing Markdown.

Save captured pages into a dedicated staging directory (for example `./captured-auth/` as `.html`, `.md`, or `.jsonl` records with `source_url`, `title`, `captured_at`, and `html`/`markdown`).

### 2. Sanitize & Preview First (`--dry-run`, Default)

Always run `skills/xerj-auth-ingest/ingest_to_xerj.py` in its default `--dry-run` mode first. It:
1. Strips `<script>`, `<style>`, `<noscript>`, `<svg>`, HTML comments, `<nav>`, `<footer>`, `<aside>`, and form chrome while preserving UTF-8 CJK and technical notation.
2. Adds standardized YAML frontmatter (`source_url`, `captured_at`, `title`, `domain`).
3. Runs a built-in **zero-secret-leakage redaction filter** that scrubs accidental `Authorization: Bearer ...`, `ApiKey ...`, `Cookie: ...`, `Set-Cookie: ...`, `hf_...`, `ghp_...`, `sk-...`, JWTs (`eyJ...`), PEM private keys, and sensitive URL query tokens into `<REDACTED>` **before** writing any file.

```bash
python3 skills/xerj-auth-ingest/ingest_to_xerj.py ./captured-auth \
  --out ./xerj-clean \
  --json
```

Inspect the generated Markdown files in `./xerj-clean` (and check `total_redactions`) to confirm no sensitive content slipped through.

### 3. Index into Local Loopback XERJ (`--index`)

Once verified, pass `--index` to invoke `xerj --url http://127.0.0.1:9200 autoindex <out_dir> --prefix <prefix>` using safe list-argument `subprocess.run` (no shell expansion):

```bash
python3 skills/xerj-auth-ingest/ingest_to_xerj.py ./captured-auth \
  --out ./xerj-clean \
  --index \
  --url http://127.0.0.1:9200 \
  --prefix auth-notes
```

### 4. Read-Only Retrieval & Verifiable Citations

After indexing into local XERJ:
- Query the corpus safely from AI agents through [`wasabihu/kbwall`](https://github.com/wasabihu/kbwall) (`kb_search`, air-gapped loopback-only read-only MCP firewall).
- Verify verbatim quotes and `xerj://` evidence URIs in downstream reports with [`wasabihu/claimcite`](https://github.com/wasabihu/claimcite).

## Security Guardrails

1. **Never Ingest Auth State or Secrets**: NEVER pass `~/.sessfork/state.json`, `~/.sessfork/keys.json`, `~/.sessfork/playwright-output/.sessfork-state.json`, `.env`, private keys, cookies, or raw `Authorization` / `Cookie` headers to `ingest_to_xerj.py` or `xerj`. The script refuses `.sessfork/`, `.env*`, and key files and scrubs accidental secrets to `<REDACTED>` before disk write.
2. **Loopback-Only XERJ by Default**: `--url` only accepts loopback addresses (`127.0.0.1`, `::1`, `localhost`) by default to prevent SSRF or accidental exfiltration. Remote hosts require both `--allow-remote` and `https://`.
3. **High-Risk Sites Prohibited**: NEVER run `sessfork` or this ingest workflow on brokerage/trading terminals, banking portals, real-name identity verification flows, or 2FA/MFA security settings pages.
4. **No Path Traversal or Shell Injection**: All CLI paths and JSONL output names reject `..` traversal segments and symlink escapes outside the input root; `xerj autoindex` is always invoked with `shell=False` and validated `--prefix` tokens.
5. **Respect Site Policies & Refresh-Token Rotation**: Only capture content you are authorized to access and store locally, comply with each site's Terms of Service, and avoid sites that invalidate sessions on single-use refresh-token rotation.
