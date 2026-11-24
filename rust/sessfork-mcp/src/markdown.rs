use serde_json::Value;

/// Build a zero-layout DOM-to-Markdown JavaScript extractor.
/// Reads live DOM nodes directly without calling getComputedStyle/getBoundingClientRect,
/// keeping Moli on its structure-first (no layout/paint) fast path.
pub fn build_markdown_extract_expression(selector: Option<&str>) -> Result<String, String> {
    let sel_json = match selector.map(str::trim).filter(|s| !s.is_empty()) {
        Some(s) => {
            serde_json::to_string(s).map_err(|e| format!("Invalid selector encoding: {}", e))?
        }
        None => "null".to_string(),
    };

    Ok(format!(
        r##"(() => {{
  const sel = {sel_json};
  const root = sel ? document.querySelector(sel) : (document.body || document.documentElement);
  if (!root) {{
    return JSON.stringify({{ ok: false, error: sel ? ("No element matched CSS selector: " + sel) : "Document has no body" }});
  }}

  const MAX_DEPTH = 64;
  const SKIP_TAGS = new Set([
    "SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "CANVAS", "IFRAME", "HEAD", "META", "LINK", "OBJECT", "EMBED"
  ]);

  function tagOf(node) {{
    return String(node.tagName || "").toUpperCase();
  }}

  function isHidden(el) {{
    if (el.hidden) return true;
    const ariaHidden = el.getAttribute && el.getAttribute("aria-hidden");
    if (ariaHidden === "true") return true;
    const st = el.style;
    if (st && (st.display === "none" || st.visibility === "hidden")) return true;
    return false;
  }}

  function attrString(el, prop, attr) {{
    const v = el[prop];
    if (typeof v === "string" && v) return v;
    return (el.getAttribute && el.getAttribute(attr)) || "";
  }}

  function cleanInline(s) {{
    return String(s || "").replace(/\s+/g, " ");
  }}

  function wrapInlineCode(raw) {{
    let maxRun = 0;
    let cur = 0;
    for (let i = 0; i < raw.length; i++) {{
      if (raw[i] === "`") {{
        cur++;
        if (cur > maxRun) maxRun = cur;
      }} else {{
        cur = 0;
      }}
    }}
    const fence = "`".repeat(maxRun + 1);
    const pad = raw.startsWith("`") || raw.endsWith("`") ? " " : "";
    return fence + pad + raw + pad + fence;
  }}

  function renderChildren(node, ctx, depth) {{
    if (depth >= MAX_DEPTH) return "";
    let out = "";
    for (let child = node.firstChild; child; child = child.nextSibling) {{
      out += renderNode(child, ctx, depth + 1);
    }}
    return out;
  }}

  function collectDirectTableRows(table) {{
    const rows = [];
    for (let child = table.firstChild; child; child = child.nextSibling) {{
      if (child.nodeType !== 1 || isHidden(child)) continue;
      const t = tagOf(child);
      if (t === "TR") {{
        rows.push(child);
      }} else if (t === "THEAD" || t === "TBODY" || t === "TFOOT") {{
        for (let r = child.firstChild; r; r = r.nextSibling) {{
          if (r.nodeType === 1 && !isHidden(r) && tagOf(r) === "TR") {{
            rows.push(r);
          }}
        }}
      }}
    }}
    return rows;
  }}

  function renderTable(table, ctx, depth) {{
    const rows = collectDirectTableRows(table);
    if (!rows.length) return "";
    const grid = [];
    for (const tr of rows) {{
      const cells = [];
      for (let c = tr.firstChild; c; c = c.nextSibling) {{
        if (c.nodeType !== 1 || isHidden(c)) continue;
        const ct = tagOf(c);
        if (ct === "TH" || ct === "TD") {{
          cells.push(cleanInline(renderChildren(c, ctx, depth + 1)).trim().replace(/\|/g, "\\|"));
        }}
      }}
      if (cells.length > 0) grid.push(cells);
    }}
    if (!grid.length) return "";
    const cols = grid.reduce((m, r) => (r.length > m ? r.length : m), 0);
    const pad = r => Array.from({{ length: cols }}, (_, i) => r[i] || "");
    const header = pad(grid[0]);
    const sep = Array.from({{ length: cols }}, () => "---");
    const lines = [
      "| " + header.join(" | ") + " |",
      "| " + sep.join(" | ") + " |",
      ...grid.slice(1).map(r => "| " + pad(r).join(" | ") + " |")
    ];
    return "\n\n" + lines.join("\n") + "\n\n";
  }}

  function renderNode(node, ctx, depth) {{
    if (!node || depth > MAX_DEPTH) return "";
    if (node.nodeType === 3) {{
      return cleanInline(node.nodeValue || "");
    }}
    if (node.nodeType !== 1) return "";
    const tag = tagOf(node);
    if (SKIP_TAGS.has(tag) || isHidden(node)) return "";

    if (tag === "BR") return "  \n";
    if (tag === "HR") return "\n\n---\n\n";

    if (/^H[1-6]$/.test(tag)) {{
      const level = Number(tag[1]);
      const inner = renderChildren(node, ctx, depth).trim();
      return inner ? ("\n\n" + "#".repeat(level) + " " + inner + "\n\n") : "";
    }}

    if (tag === "PRE") {{
      const codeEl = node.querySelector("code");
      const raw = (codeEl ? codeEl.textContent : node.textContent) || "";
      const langMatch = codeEl && typeof codeEl.className === "string"
        ? codeEl.className.match(/language-([a-zA-Z0-9_+-]+)/)
        : null;
      const lang = langMatch ? langMatch[1] : "";
      const trimmed = raw.replace(/^\n+|\n+$/g, "");
      const fence = trimmed.includes("```") ? "~~~~" : "```";
      return "\n\n" + fence + lang + "\n" + trimmed + "\n" + fence + "\n\n";
    }}

    if (tag === "CODE") {{
      const raw = (node.textContent || "").trim();
      return raw ? wrapInlineCode(raw) : "";
    }}

    if (tag === "STRONG" || tag === "B") {{
      const inner = renderChildren(node, ctx, depth).trim();
      return inner ? ("**" + inner + "**") : "";
    }}

    if (tag === "EM" || tag === "I") {{
      const inner = renderChildren(node, ctx, depth).trim();
      return inner ? ("*" + inner + "*") : "";
    }}

    if (tag === "A") {{
      const label = renderChildren(node, ctx, depth).trim();
      const href = attrString(node, "href", "href");
      if (!label) return "";
      if (!href || href.toLowerCase().startsWith("javascript:")) return label;
      return "[" + label.replace(/\]/g, "\\]") + "](" + href + ")";
    }}

    if (tag === "IMG") {{
      const alt = (node.getAttribute("alt") || "").trim();
      const rawSrc = attrString(node, "src", "src");
      if (!alt && !rawSrc) return "";
      if (!rawSrc) return alt;
      const src = rawSrc.startsWith("data:") && rawSrc.length > 200 ? "inline-image" : rawSrc;
      return "![" + alt.replace(/\]/g, "\\]") + "](" + src + ")";
    }}

    if (tag === "BLOCKQUOTE") {{
      const inner = renderChildren(node, ctx, depth).trim();
      if (!inner) return "";
      const quoted = inner.split("\n").map(line => "> " + line).join("\n");
      return "\n\n" + quoted + "\n\n";
    }}

    if (tag === "UL" || tag === "OL") {{
      const ordered = tag === "OL";
      const nextCtx = {{ listDepth: ctx.listDepth + 1 }};
      const indent = "  ".repeat(ctx.listDepth);
      let idx = 1;
      const lines = [];
      for (let child = node.firstChild; child; child = child.nextSibling) {{
        if (child.nodeType === 1 && !isHidden(child) && tagOf(child) === "LI") {{
          const body = renderChildren(child, nextCtx, depth + 1).trim().replace(/\n+/g, "\n" + indent + "  ");
          if (body) {{
            const bullet = ordered ? (idx++ + ". ") : "- ";
            lines.push(indent + bullet + body);
          }}
        }}
      }}
      if (!lines.length) return "";
      return (ctx.listDepth ? "\n" : "\n\n") + lines.join("\n") + (ctx.listDepth ? "" : "\n\n");
    }}

    if (tag === "TABLE") {{
      return renderTable(node, ctx, depth);
    }}

    if (tag === "P" || tag === "SECTION" || tag === "ARTICLE" || tag === "MAIN" || tag === "HEADER" || tag === "FOOTER" || tag === "DIV" || tag === "FORM") {{
      const inner = renderChildren(node, ctx, depth).trim();
      if (!inner) return "";
      if (tag === "P" || tag === "ARTICLE" || tag === "SECTION" || tag === "MAIN") {{
        return "\n\n" + inner + "\n\n";
      }}
      return "\n" + inner + "\n";
    }}

    return renderChildren(node, ctx, depth);
  }}

  const raw = renderNode(root, {{ listDepth: 0 }}, 0);
  const normalized = raw
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{{3,}}/g, "\n\n")
    .trim();

  return JSON.stringify({{
    ok: true,
    url: String(location.href || ""),
    title: String(document.title || ""),
    markdown: normalized
  }});
}})()"##,
        sel_json = sel_json
    ))
}

pub fn format_markdown_response(raw_json: &str, max_chars: usize) -> Result<String, String> {
    let parsed: Value = serde_json::from_str(raw_json)
        .map_err(|e| format!("Failed to parse markdown extractor output: {}", e))?;

    if parsed.get("ok").and_then(|v| v.as_bool()) != Some(true) {
        let err_msg = parsed
            .get("error")
            .and_then(|v| v.as_str())
            .unwrap_or("Unknown markdown extraction failure");
        return Err(err_msg.to_string());
    }

    let url = parsed.get("url").and_then(|v| v.as_str()).unwrap_or("");
    let title = parsed.get("title").and_then(|v| v.as_str()).unwrap_or("");
    let md = parsed
        .get("markdown")
        .and_then(|v| v.as_str())
        .unwrap_or("");

    let body = truncate_chars(md, max_chars);
    let mut out = format!(
        "### Page\n- Page URL: {}\n- Page Title: {}\n\n### Markdown\n",
        url, title
    );
    if body.is_empty() {
        out.push_str("(empty page content)\n");
    } else {
        out.push_str(&body);
        if !body.ends_with('\n') {
            out.push('\n');
        }
    }
    Ok(out)
}

fn truncate_chars(input: &str, max_chars: usize) -> String {
    let count = input.chars().count();
    if count <= max_chars {
        return input.to_string();
    }
    let truncated: String = input.chars().take(max_chars).collect();
    format!(
        "{}\n\n... [truncated at {} chars; total {} chars]",
        truncated.trim_end(),
        max_chars,
        count
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_empty_and_truncates_utf8_markdown_safely() {
        let payload = serde_json::json!({
            "ok": true,
            "url": "https://example.com/post",
            "title": "Demo Post",
            "markdown": "# 你好世界\n\n这是一段包含中文字符的测试文本。"
        })
        .to_string();

        let full = format_markdown_response(&payload, 1000).expect("full format");
        assert!(full.contains("- Page URL: https://example.com/post"));
        assert!(full.contains("# 你好世界"));

        let cut = format_markdown_response(&payload, 6).expect("utf8 safe truncation");
        assert!(cut.contains("[truncated at 6 chars"));
        assert!(cut.contains("# 你好世界"));

        let exact = format_markdown_response(
            &serde_json::json!({"ok": true, "url": "https://a.test", "title": "T", "markdown": "123456"}).to_string(),
            6,
        )
        .expect("exact boundary");
        assert!(!exact.contains("truncated"));

        let empty = format_markdown_response(
            &serde_json::json!({"ok": true, "url": "https://a.test", "title": "T", "markdown": ""})
                .to_string(),
            100,
        )
        .expect("empty page");
        assert!(empty.contains("(empty page content)"));
    }

    #[test]
    fn surfaces_selector_miss_and_malformed_json_errors() {
        let payload = serde_json::json!({
            "ok": false,
            "error": "No element matched CSS selector: article.missing"
        })
        .to_string();
        let err = format_markdown_response(&payload, 1000).expect_err("must fail closed");
        assert!(err.contains("article.missing"));

        let bad_json = format_markdown_response("{not-json", 1000).expect_err("invalid json");
        assert!(bad_json.contains("Failed to parse markdown extractor output"));
    }

    #[test]
    fn escapes_css_selector_safely_in_expression() {
        let expr = build_markdown_extract_expression(Some("main[data-id=\"a'b\"]"))
            .expect("selector json encoding");
        assert!(expr.contains(r#"const sel = "main[data-id=\"a'b\"]";"#));
        assert!(expr.contains("grid.reduce("));
        assert!(!expr.contains("Math.max(..."));
    }
}
