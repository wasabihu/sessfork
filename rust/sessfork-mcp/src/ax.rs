use serde_json::Value;
use std::collections::HashMap;

pub struct AxSnapshot {
    pub yaml: String,
    pub ref_map: HashMap<String, i64>,
    pub content_lines: usize,
    pub looks_like_loading_shell: bool,
    #[cfg(test)]
    pub truncated: bool,
}

fn is_focusable_or_clickable(node: &Value) -> bool {
    let Some(props) = node.get("properties").and_then(|v| v.as_array()) else {
        return false;
    };
    props.iter().any(|p| {
        let pname = p.get("name").and_then(|v| v.as_str()).unwrap_or("");
        matches!(pname, "focusable" | "clickable")
            && p.pointer("/value/value").and_then(|v| v.as_bool()) == Some(true)
    })
}

fn is_loading_placeholder_text(s: &str) -> bool {
    let low = s.trim().to_ascii_lowercase();
    if low.is_empty() {
        return false;
    }
    low == "loading"
        || low == "loading..."
        || low == "loading…"
        || low.starts_with("loading…")
        || low.starts_with("loading...")
        || s.contains("加载中")
        || s.contains("正在加载")
}

fn is_interactive_role(role: &str) -> bool {
    matches!(
        role,
        "button"
            | "link"
            | "textbox"
            | "searchbox"
            | "combobox"
            | "checkbox"
            | "radio"
            | "menuitem"
            | "menuitemcheckbox"
            | "menuitemradio"
            | "tab"
            | "switch"
            | "option"
            | "slider"
            | "spinbutton"
            | "listbox"
    )
}

fn is_structural_noise(role: &str) -> bool {
    matches!(
        role,
        "none" | "generic" | "GenericContainer" | "InlineTextBox" | "LineBreak" | "paragraph"
    )
}

fn clean_inline_text(s: &str) -> String {
    let mut out = String::with_capacity(s.len().min(200));
    let mut prev_ws = false;
    for ch in s.chars() {
        if ch.is_whitespace() {
            if !prev_ws && !out.is_empty() {
                out.push(' ');
                prev_ws = true;
            }
        } else {
            out.push(ch);
            prev_ws = false;
        }
        if out.len() >= 180 {
            out.push_str("...");
            break;
        }
    }
    out.trim().to_string()
}

fn escape_yaml_quote(s: &str) -> String {
    s.replace('\\', "\\\\").replace('"', "\\\"")
}

/// Convert CDP `Accessibility.getFullAXTree` `nodes` into a compact Playwright-style YAML snapshot
/// with deterministic `[ref=eN]` handles mapped to `backendDOMNodeId`.
pub fn format_ax_tree(nodes: &[Value], max_chars: usize) -> AxSnapshot {
    if nodes.is_empty() {
        return AxSnapshot {
            yaml: String::new(),
            ref_map: HashMap::new(),
            content_lines: 0,
            looks_like_loading_shell: true,
            #[cfg(test)]
            truncated: false,
        };
    }

    let mut id_to_idx: HashMap<&str, usize> = HashMap::with_capacity(nodes.len());
    for (idx, node) in nodes.iter().enumerate() {
        if let Some(id) = node.get("nodeId").and_then(|v| v.as_str()) {
            id_to_idx.insert(id, idx);
        }
    }

    let mut yaml = String::with_capacity(4096);
    let mut ref_map: HashMap<String, i64> = HashMap::new();
    let mut next_ref: usize = 1;
    let mut content_lines: usize = 0;
    let mut saw_loading_placeholder = false;
    let mut truncated = false;
    let mut visited = vec![false; nodes.len()];

    struct Ctx<'a> {
        nodes: &'a [Value],
        id_to_idx: &'a HashMap<&'a str, usize>,
        yaml: &'a mut String,
        ref_map: &'a mut HashMap<String, i64>,
        next_ref: &'a mut usize,
        content_lines: &'a mut usize,
        saw_loading_placeholder: &'a mut bool,
        max_chars: usize,
        truncated: &'a mut bool,
        visited: &'a mut [bool],
    }

    fn walk(ctx: &mut Ctx<'_>, idx: usize, depth: usize) {
        if *ctx.truncated || idx >= ctx.nodes.len() || ctx.visited[idx] || depth > 48 {
            return;
        }
        ctx.visited[idx] = true;

        let node = &ctx.nodes[idx];
        let ignored = node
            .get("ignored")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let role = node
            .pointer("/role/value")
            .and_then(|v| v.as_str())
            .unwrap_or("none");
        let name = node
            .pointer("/name/value")
            .and_then(|v| v.as_str())
            .map(clean_inline_text)
            .unwrap_or_default();
        let val_str = node
            .pointer("/value/value")
            .and_then(|v| v.as_str())
            .map(clean_inline_text)
            .unwrap_or_default();
        let backend_id = node.get("backendDOMNodeId").and_then(|v| v.as_i64());
        let focusable_clickable = is_focusable_or_clickable(node)
            && !matches!(role, "RootWebArea" | "document" | "WebArea" | "StaticText");

        let emit = !ignored
            && role != "InlineTextBox"
            && (!is_structural_noise(role) || !name.is_empty() || focusable_clickable);

        let child_depth = if emit {
            if !matches!(role, "RootWebArea" | "document" | "WebArea") {
                *ctx.content_lines += 1;
                if is_loading_placeholder_text(&name) {
                    *ctx.saw_loading_placeholder = true;
                }
            }
            let mut line = String::with_capacity(96);
            for _ in 0..depth {
                line.push_str("  ");
            }
            line.push_str("- ");
            if role == "StaticText" && !name.is_empty() {
                line.push_str("text: \"");
                line.push_str(&escape_yaml_quote(&name));
                line.push('"');
            } else {
                line.push_str(role);
                if !name.is_empty() {
                    line.push_str(" \"");
                    line.push_str(&escape_yaml_quote(&name));
                    line.push('"');
                }
                if !val_str.is_empty() && val_str != name {
                    line.push_str(" value=\"");
                    line.push_str(&escape_yaml_quote(&val_str));
                    line.push('"');
                }
                if let Some(bid) = backend_id {
                    if is_interactive_role(role) || (focusable_clickable && !name.is_empty()) {
                        let ref_id = format!("e{}", *ctx.next_ref);
                        *ctx.next_ref += 1;
                        ctx.ref_map.insert(ref_id.clone(), bid);
                        line.push_str(" [ref=");
                        line.push_str(&ref_id);
                        line.push(']');
                    }
                }
            }
            line.push('\n');

            if ctx.yaml.len() + line.len() > ctx.max_chars {
                *ctx.truncated = true;
                return;
            }
            ctx.yaml.push_str(&line);
            depth + 1
        } else {
            depth
        };

        if let Some(children) = node.get("childIds").and_then(|v| v.as_array()) {
            for cid in children {
                if let Some(cid_str) = cid.as_str() {
                    if let Some(&c_idx) = ctx.id_to_idx.get(cid_str) {
                        walk(ctx, c_idx, child_depth);
                        if *ctx.truncated {
                            return;
                        }
                    }
                }
            }
        }
    }

    let mut ctx = Ctx {
        nodes,
        id_to_idx: &id_to_idx,
        yaml: &mut yaml,
        ref_map: &mut ref_map,
        next_ref: &mut next_ref,
        content_lines: &mut content_lines,
        saw_loading_placeholder: &mut saw_loading_placeholder,
        max_chars,
        truncated: &mut truncated,
        visited: &mut visited,
    };

    // Start from root node (index 0) and any unvisited roots.
    walk(&mut ctx, 0, 0);
    if truncated {
        yaml.push_str("... (snapshot truncated)\n");
    }

    let looks_like_loading_shell =
        content_lines == 0 || (content_lines <= 2 && saw_loading_placeholder);

    AxSnapshot {
        yaml,
        ref_map,
        content_lines,
        looks_like_loading_shell,
        #[cfg(test)]
        truncated,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn compresses_ignored_nodes_and_assigns_interactive_refs() {
        let nodes = vec![
            json!({
                "nodeId": "AX-0",
                "ignored": false,
                "role": {"value": "RootWebArea"},
                "name": {"value": "Demo"},
                "childIds": ["AX-1"]
            }),
            json!({
                "nodeId": "AX-1",
                "ignored": true,
                "role": {"value": "none"},
                "childIds": ["AX-2", "AX-3"]
            }),
            json!({
                "nodeId": "AX-2",
                "backendDOMNodeId": 1002,
                "ignored": false,
                "role": {"value": "button"},
                "name": {"value": "Submit"},
                "childIds": []
            }),
            json!({
                "nodeId": "AX-3",
                "backendDOMNodeId": 1003,
                "ignored": false,
                "role": {"value": "textbox"},
                "name": {"value": "Email"},
                "childIds": []
            }),
        ];
        let snap = format_ax_tree(&nodes, 10_000);
        assert_eq!(snap.ref_map.get("e1"), Some(&1002));
        assert_eq!(snap.ref_map.get("e2"), Some(&1003));
        assert!(snap.yaml.contains("button \"Submit\" [ref=e1]"));
        assert!(snap.yaml.contains("textbox \"Email\" [ref=e2]"));
        assert!(!snap.looks_like_loading_shell);
        assert!(!snap.truncated);
    }

    #[test]
    fn detects_spa_loading_shell_and_assigns_focusable_div_ref() {
        let loading_nodes = vec![
            json!({
                "nodeId": "AX-0",
                "ignored": false,
                "role": {"value": "RootWebArea"},
                "name": {"value": "https://x.com/test"},
                "childIds": ["AX-1"]
            }),
            json!({
                "nodeId": "AX-1",
                "ignored": false,
                "role": {"value": "generic"},
                "name": {"value": "Loading…"},
                "childIds": []
            }),
        ];
        let snap_loading = format_ax_tree(&loading_nodes, 10_000);
        assert!(snap_loading.looks_like_loading_shell);

        let div_btn_nodes = vec![
            json!({
                "nodeId": "AX-0",
                "ignored": false,
                "role": {"value": "RootWebArea"},
                "name": {"value": "Dashboard"},
                "childIds": ["AX-1"]
            }),
            json!({
                "nodeId": "AX-1",
                "backendDOMNodeId": 2001,
                "ignored": false,
                "role": {"value": "generic"},
                "name": {"value": "笔记管理"},
                "properties": [{"name": "focusable", "value": {"value": true}}],
                "childIds": []
            }),
        ];
        let snap_div = format_ax_tree(&div_btn_nodes, 10_000);
        assert_eq!(snap_div.ref_map.get("e1"), Some(&2001));
        assert!(snap_div.yaml.contains("generic \"笔记管理\" [ref=e1]"));
        assert!(!snap_div.looks_like_loading_shell);
    }
}
