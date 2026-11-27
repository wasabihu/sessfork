use serde_json::Value;
use std::collections::{HashMap, HashSet};

const MAX_PERSISTENT_REFS: usize = 4096;
const MAX_YAML_INDENT: usize = 48;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct SnapshotOptions {
    /// Zero-layout distillation: emit flat interactive controls plus high-signal
    /// semantic anchors (headings, alerts, status, modal text) without deep indentation.
    pub interactive_only: bool,
    /// When an active modal dialog (`role="alertdialog"` or `role="dialog"` with `modal=true`)
    /// is present and non-empty, scope snapshot output to the topmost modal subtree.
    pub scope_modal: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SnapshotEntry {
    /// Structural key without `[ref=eN]` (used when printing `- removed` lines without stale refs).
    pub key: String,
    /// Full rendered single-line representation WITH `[ref=eN]` when interactive.
    /// Because `RefRegistry` assigns stable per-node refs across same-document updates,
    /// diffing on `rendered` stays shift-invariant while still catching DOM remounts and
    /// duplicate-label insertions accurately.
    pub rendered: String,
}

pub struct AxSnapshot {
    pub yaml: String,
    pub ref_map: HashMap<String, i64>,
    pub content_lines: usize,
    pub looks_like_loading_shell: bool,
    pub entries: Vec<SnapshotEntry>,
    /// Title of the topmost active modal dialog detected in the AX tree (even when unscoped).
    pub active_modal: Option<String>,
    /// True when output was actively scoped to `active_modal`.
    pub modal_scoped: bool,
    /// Number of interactive elements outside the modal suppressed by modal scoping.
    pub obscured_interactive_count: usize,
    pub truncated: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SnapshotDiff {
    pub added: Vec<SnapshotEntry>,
    pub removed: Vec<String>,
}

impl SnapshotDiff {
    pub fn is_empty(&self) -> bool {
        self.added.is_empty() && self.removed.is_empty()
    }
}

/// Stable per-document mapping from CDP `backendDOMNodeId` to short `[ref=eN]` handles.
/// Preserves existing ref IDs across same-page DOM updates so diff responses remain actionable.
#[derive(Debug, Clone)]
pub struct RefRegistry {
    backend_to_ref: HashMap<i64, String>,
    next_ref: usize,
}

impl Default for RefRegistry {
    fn default() -> Self {
        Self::new()
    }
}

impl RefRegistry {
    pub fn new() -> Self {
        Self {
            backend_to_ref: HashMap::new(),
            next_ref: 1,
        }
    }

    pub fn reset(&mut self) {
        self.backend_to_ref.clear();
        self.next_ref = 1;
    }

    pub fn get_or_assign(&mut self, backend_id: i64) -> String {
        if let Some(existing) = self.backend_to_ref.get(&backend_id) {
            return existing.clone();
        }
        let ref_id = format!("e{}", self.next_ref);
        self.next_ref += 1;
        self.backend_to_ref.insert(backend_id, ref_id.clone());
        ref_id
    }

    /// Garbage-collect detached DOM node IDs when a long-lived SPA exceeds `MAX_PERSISTENT_REFS`,
    /// keeping memory strictly bounded while preserving stable refs for all currently live nodes.
    pub fn prune_if_oversized(&mut self, live_ref_map: &HashMap<String, i64>) {
        if self.backend_to_ref.len() > MAX_PERSISTENT_REFS {
            let live_bids: HashSet<i64> = live_ref_map.values().copied().collect();
            self.backend_to_ref.retain(|bid, _| live_bids.contains(bid));
        }
    }
}

#[derive(Default, Clone, Copy)]
struct NodeProps {
    hidden: bool,
    focusable_or_clickable: bool,
    modal: bool,
    disabled: bool,
    readonly: bool,
    checked: bool,
    selected: bool,
    expanded: bool,
    pressed: bool,
    invalid: bool,
}

fn value_is_truthy_prop(val: Option<&Value>) -> bool {
    match val {
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => {
            let t = s.trim();
            t.eq_ignore_ascii_case("true")
                || t.eq_ignore_ascii_case("grammar")
                || t.eq_ignore_ascii_case("spelling")
        }
        _ => false,
    }
}

/// Extract all relevant ARIA / AX properties in a single pass over `node.properties`
/// with zero heap allocation.
fn parse_node_props(node: &Value) -> NodeProps {
    let mut out = NodeProps::default();
    let Some(props) = node.get("properties").and_then(|v| v.as_array()) else {
        return out;
    };
    for p in props {
        let Some(name) = p.get("name").and_then(|v| v.as_str()) else {
            continue;
        };
        let val = p.pointer("/value/value");
        match name {
            "hidden" => out.hidden |= value_is_truthy_prop(val),
            "focusable" | "clickable" => {
                out.focusable_or_clickable |= val.and_then(|v| v.as_bool()) == Some(true);
            }
            "modal" => out.modal |= value_is_truthy_prop(val),
            "disabled" => out.disabled |= value_is_truthy_prop(val),
            "readonly" => out.readonly |= value_is_truthy_prop(val),
            "checked" => out.checked |= value_is_truthy_prop(val),
            "selected" => out.selected |= value_is_truthy_prop(val),
            "expanded" => out.expanded |= value_is_truthy_prop(val),
            "pressed" => out.pressed |= value_is_truthy_prop(val),
            "invalid" => out.invalid |= value_is_truthy_prop(val),
            _ => {}
        }
    }
    out
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

/// Container / landmark roles that commonly carry `tabindex="-1"` (`focusable: true`) for
/// programmatic focus management (e.g. WAI-ARIA modals) and MUST NOT be treated as clickable buttons.
fn is_non_actionable_focus_container(role: &str) -> bool {
    matches!(
        role,
        "RootWebArea"
            | "document"
            | "WebArea"
            | "StaticText"
            | "dialog"
            | "alertdialog"
            | "alert"
            | "status"
            | "log"
            | "heading"
            | "main"
            | "region"
            | "navigation"
            | "banner"
            | "contentinfo"
            | "complementary"
            | "form"
            | "article"
            | "section"
    )
}

fn is_semantic_callout_role(role: &str) -> bool {
    matches!(role, "alert" | "status" | "dialog" | "alertdialog" | "log")
}

fn is_semantic_anchor_role(role: &str) -> bool {
    matches!(
        role,
        "heading" | "alert" | "status" | "dialog" | "alertdialog"
    )
}

fn is_structural_noise(role: &str) -> bool {
    matches!(
        role,
        "none" | "generic" | "GenericContainer" | "InlineTextBox" | "LineBreak" | "paragraph"
    )
}

fn is_modal_dialog_node(props: NodeProps, role: &str) -> bool {
    role == "alertdialog" || (role == "dialog" && props.modal)
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

fn push_escaped_yaml_quote(out: &mut String, s: &str) {
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            _ => out.push(ch),
        }
    }
}

fn raw_non_empty_name(node: &Value) -> bool {
    node.pointer("/name/value")
        .and_then(|v| v.as_str())
        .is_some_and(|s| !s.trim().is_empty())
}

fn node_is_actionable(node: &Value, role: &str, props: NodeProps, has_name: bool) -> bool {
    if node
        .get("backendDOMNodeId")
        .and_then(|v| v.as_i64())
        .is_none()
    {
        return false;
    }
    if is_interactive_role(role) {
        return true;
    }
    props.focusable_or_clickable && !is_non_actionable_focus_container(role) && has_name
}

struct SubtreeSummary {
    interactive_count: usize,
    has_visible_content: bool,
    first_heading: Option<String>,
}

fn summarize_subtree(
    nodes: &[Value],
    id_to_idx: &HashMap<&str, usize>,
    root_idx: usize,
) -> SubtreeSummary {
    let mut stack: Vec<usize> = Vec::with_capacity(32);
    let mut visited = vec![false; nodes.len()];
    let mut summary = SubtreeSummary {
        interactive_count: 0,
        has_visible_content: false,
        first_heading: None,
    };
    stack.push(root_idx);

    while let Some(idx) = stack.pop() {
        if idx >= nodes.len() || visited[idx] {
            continue;
        }
        visited[idx] = true;
        let Some(node) = nodes.get(idx) else {
            continue;
        };
        let props = parse_node_props(node);
        if props.hidden {
            continue;
        }
        let ignored = node
            .get("ignored")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        if !ignored && idx != root_idx {
            let role = node
                .pointer("/role/value")
                .and_then(|v| v.as_str())
                .unwrap_or("none");
            let has_name = raw_non_empty_name(node);

            if role == "heading" && has_name && summary.first_heading.is_none() {
                if let Some(raw) = node.pointer("/name/value").and_then(|v| v.as_str()) {
                    let cleaned = clean_inline_text(raw);
                    if !cleaned.is_empty() {
                        summary.first_heading = Some(cleaned);
                    }
                }
            }
            if node_is_actionable(node, role, props, has_name) {
                summary.interactive_count += 1;
                summary.has_visible_content = true;
            } else if role == "StaticText" && has_name {
                summary.has_visible_content = true;
            }
        }

        if let Some(children) = node.get("childIds").and_then(|v| v.as_array()) {
            // Push in reverse so iterative DFS visits children in visual/DOM order.
            for cid in children.iter().rev() {
                if let Some(cid_str) = cid.as_str() {
                    if let Some(&c_idx) = id_to_idx.get(cid_str) {
                        stack.push(c_idx);
                    }
                }
            }
        }
    }

    summary
}

struct ModalInfo {
    root_idx: usize,
    title: String,
    interactive_count: usize,
}

fn detect_topmost_modal(
    nodes: &[Value],
    id_to_idx: &HashMap<&str, usize>,
) -> (Option<ModalInfo>, usize) {
    let mut stack: Vec<usize> = Vec::with_capacity(64);
    let mut visited = vec![false; nodes.len()];
    let mut total_interactive: usize = 0;
    let mut modal_candidates: Vec<usize> = Vec::new();

    if !nodes.is_empty() {
        stack.push(0);
    }

    while let Some(idx) = stack.pop() {
        if idx >= nodes.len() || visited[idx] {
            continue;
        }
        visited[idx] = true;
        let Some(node) = nodes.get(idx) else {
            continue;
        };
        let props = parse_node_props(node);
        if props.hidden {
            continue;
        }
        let ignored = node
            .get("ignored")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        if !ignored {
            let role = node
                .pointer("/role/value")
                .and_then(|v| v.as_str())
                .unwrap_or("none");
            let has_name = raw_non_empty_name(node);

            if node_is_actionable(node, role, props, has_name) {
                total_interactive += 1;
            }
            if is_modal_dialog_node(props, role) {
                modal_candidates.push(idx);
            }
        }

        if let Some(children) = node.get("childIds").and_then(|v| v.as_array()) {
            for cid in children.iter().rev() {
                if let Some(cid_str) = cid.as_str() {
                    if let Some(&c_idx) = id_to_idx.get(cid_str) {
                        stack.push(c_idx);
                    }
                }
            }
        }
    }

    // Pick the last (topmost in DOM stacking order) modal that actually has visible descendants.
    for modal_idx in modal_candidates.into_iter().rev() {
        let summary = summarize_subtree(nodes, id_to_idx, modal_idx);
        if summary.has_visible_content {
            let direct_name = nodes
                .get(modal_idx)
                .and_then(|n| n.pointer("/name/value"))
                .and_then(|v| v.as_str())
                .map(clean_inline_text)
                .filter(|s| !s.is_empty());
            let title = direct_name
                .or(summary.first_heading)
                .unwrap_or_else(|| "Dialog".to_string());
            return (
                Some(ModalInfo {
                    root_idx: modal_idx,
                    title,
                    interactive_count: summary.interactive_count,
                }),
                total_interactive,
            );
        }
    }

    (None, total_interactive)
}

/// Convert CDP `Accessibility.getFullAXTree` `nodes` into a compact Playwright-style YAML snapshot
/// with deterministic `[ref=eN]` handles mapped to `backendDOMNodeId`.
#[cfg_attr(not(test), allow(dead_code))]
pub fn format_ax_tree(nodes: &[Value], max_chars: usize) -> AxSnapshot {
    let mut reg = RefRegistry::new();
    format_ax_tree_with_options(nodes, max_chars, SnapshotOptions::default(), &mut reg)
}

/// Convert CDP `Accessibility.getFullAXTree` `nodes` with zero-layout distillation, optional
/// active-modal scoping, and stable per-document `[ref=eN]` assignment.
/// Uses strictly iterative DFS (zero recursion) so deeply nested adversarial DOMs cannot
/// overflow the Rust thread stack.
pub fn format_ax_tree_with_options(
    nodes: &[Value],
    max_chars: usize,
    opts: SnapshotOptions,
    registry: &mut RefRegistry,
) -> AxSnapshot {
    if nodes.is_empty() {
        return AxSnapshot {
            yaml: String::new(),
            ref_map: HashMap::new(),
            content_lines: 0,
            looks_like_loading_shell: true,
            entries: Vec::new(),
            active_modal: None,
            modal_scoped: false,
            obscured_interactive_count: 0,
            truncated: false,
        };
    }

    let mut id_to_idx: HashMap<&str, usize> = HashMap::with_capacity(nodes.len());
    for (idx, node) in nodes.iter().enumerate() {
        if let Some(id) = node.get("nodeId").and_then(|v| v.as_str()) {
            id_to_idx.insert(id, idx);
        }
    }

    let (modal_info, total_interactive) = detect_topmost_modal(nodes, &id_to_idx);
    let (walk_root, active_modal, modal_scoped, obscured_interactive_count) = match modal_info {
        Some(m) if opts.scope_modal => (
            m.root_idx,
            Some(m.title),
            true,
            total_interactive.saturating_sub(m.interactive_count),
        ),
        Some(m) => (0, Some(m.title), false, 0),
        None => (0, None, false, 0),
    };

    let mut yaml = String::with_capacity(4096);
    let mut ref_map: HashMap<String, i64> = HashMap::new();
    let mut entries: Vec<SnapshotEntry> = Vec::with_capacity(64);
    let mut content_lines: usize = 0;
    // Count raw visible content nodes BEFORE `interactive_only` filtering so plain-text pages
    // (e.g. `<p>Hello</p>`) don't look like empty SPA loading shells in `--compact` mode.
    let mut raw_content_nodes: usize = 0;
    let mut saw_loading_placeholder = false;
    let mut truncated = false;
    let mut visited = vec![false; nodes.len()];

    // Iterative DFS frame: (node_idx, indent_depth, in_callout, suppress_redundant_text)
    let mut stack: Vec<(usize, usize, bool, bool)> = Vec::with_capacity(64);
    stack.push((walk_root, 0, modal_scoped, false));

    while let Some((idx, depth, in_callout, suppress_redundant_text)) = stack.pop() {
        if truncated || idx >= nodes.len() || visited[idx] {
            continue;
        }
        visited[idx] = true;

        let Some(node) = nodes.get(idx) else {
            continue;
        };
        let props = parse_node_props(node);
        if props.hidden {
            continue;
        }
        let ignored = node
            .get("ignored")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let role = node
            .pointer("/role/value")
            .and_then(|v| v.as_str())
            .unwrap_or("none");
        let has_name = raw_non_empty_name(node);
        let backend_id = node.get("backendDOMNodeId").and_then(|v| v.as_i64());
        let is_actionable = !ignored && node_is_actionable(node, role, props, has_name);

        if !ignored
            && role != "InlineTextBox"
            && !matches!(role, "RootWebArea" | "document" | "WebArea")
            && (is_actionable || has_name || !is_structural_noise(role))
        {
            raw_content_nodes += 1;
            if has_name {
                if let Some(raw_n) = node.pointer("/name/value").and_then(|v| v.as_str()) {
                    if is_loading_placeholder_text(raw_n) {
                        saw_loading_placeholder = true;
                    }
                }
            }
        }

        let next_in_callout = in_callout || (!ignored && is_semantic_callout_role(role));
        // Inside compact mode, interactive controls and headings already carry their accessible
        // label, so repeating their immediate child StaticText wastes tokens.
        let next_suppress_text = suppress_redundant_text
            || (opts.interactive_only
                && !ignored
                && has_name
                && (is_actionable || role == "heading"));

        let emit = if ignored || role == "InlineTextBox" {
            false
        } else if opts.interactive_only {
            is_actionable
                || (is_semantic_anchor_role(role) && has_name)
                || (role == "StaticText" && has_name && in_callout && !suppress_redundant_text)
        } else {
            !is_structural_noise(role)
                || has_name
                || (props.focusable_or_clickable && !is_non_actionable_focus_container(role))
        };

        let child_depth = if emit {
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

            if !matches!(role, "RootWebArea" | "document" | "WebArea") {
                content_lines += 1;
                if is_loading_placeholder_text(&name) {
                    saw_loading_placeholder = true;
                }
            }

            let mut key = String::with_capacity(96);
            if role == "StaticText" && !name.is_empty() {
                key.push_str("text: \"");
                push_escaped_yaml_quote(&mut key, &name);
                key.push('"');
            } else {
                key.push_str(role);
                if !name.is_empty() {
                    key.push_str(" \"");
                    push_escaped_yaml_quote(&mut key, &name);
                    key.push('"');
                }
                if !val_str.is_empty() && val_str != name {
                    key.push_str(" value=\"");
                    push_escaped_yaml_quote(&mut key, &val_str);
                    key.push('"');
                }
                if props.disabled {
                    key.push_str(" [disabled]");
                }
                if props.readonly {
                    key.push_str(" [readonly]");
                }
                if props.checked {
                    key.push_str(" [checked]");
                }
                if props.selected {
                    key.push_str(" [selected]");
                }
                if props.expanded {
                    key.push_str(" [expanded]");
                }
                if props.pressed {
                    key.push_str(" [pressed]");
                }
                if props.invalid {
                    key.push_str(" [invalid]");
                }
            }

            let mut rendered = key.clone();
            if is_actionable {
                if let Some(bid) = backend_id {
                    let ref_id = registry.get_or_assign(bid);
                    ref_map.insert(ref_id.clone(), bid);
                    rendered.push_str(" [ref=");
                    rendered.push_str(&ref_id);
                    rendered.push(']');
                }
            }

            let indent = if opts.interactive_only {
                0
            } else {
                depth.min(MAX_YAML_INDENT)
            };
            let needed = indent * 2 + 2 + rendered.len() + 1;
            if yaml.len() + needed > max_chars {
                truncated = true;
                break;
            }
            for _ in 0..indent {
                yaml.push_str("  ");
            }
            yaml.push_str("- ");
            yaml.push_str(&rendered);
            yaml.push('\n');

            if !matches!(role, "RootWebArea" | "document" | "WebArea") {
                entries.push(SnapshotEntry { key, rendered });
            }

            if opts.interactive_only {
                0
            } else {
                (depth + 1).min(MAX_YAML_INDENT)
            }
        } else {
            depth
        };

        if let Some(children) = node.get("childIds").and_then(|v| v.as_array()) {
            for cid in children.iter().rev() {
                if let Some(cid_str) = cid.as_str() {
                    if let Some(&c_idx) = id_to_idx.get(cid_str) {
                        stack.push((c_idx, child_depth, next_in_callout, next_suppress_text));
                    }
                }
            }
        }
    }

    if truncated {
        yaml.push_str("... (snapshot truncated)\n");
    }
    registry.prune_if_oversized(&ref_map);

    let looks_like_loading_shell =
        raw_content_nodes == 0 || (raw_content_nodes <= 2 && saw_loading_placeholder);

    AxSnapshot {
        yaml,
        ref_map,
        content_lines,
        looks_like_loading_shell,
        entries,
        active_modal,
        modal_scoped,
        obscured_interactive_count,
        truncated,
    }
}

/// Compute a ref-aware O(N) multiset diff between two snapshots' entries.
/// Because `RefRegistry` preserves `[ref=eN]` for unchanged DOM nodes across same-document updates:
/// - Inserting a new element at the top does NOT shift existing refs (zero false diffs).
/// - Inserting a duplicate-label element (e.g. a new `button "Delete" [ref=e2]`) accurately
///   emits the NEW element's live ref rather than an older sibling.
/// - Replacing/remounting a DOM node with a new `backendDOMNodeId` emits `- old` and `+ new [ref=eM]`
///   so the agent never gets stuck with a stale ref.
pub fn compute_snapshot_diff(prev: &[SnapshotEntry], next: &[SnapshotEntry]) -> SnapshotDiff {
    let mut prev_counts: HashMap<&str, usize> = HashMap::with_capacity(prev.len());
    for entry in prev {
        *prev_counts.entry(entry.rendered.as_str()).or_insert(0) += 1;
    }

    let mut next_counts: HashMap<&str, usize> = HashMap::with_capacity(next.len());
    for entry in next {
        *next_counts.entry(entry.rendered.as_str()).or_insert(0) += 1;
    }

    // Walk `next` in visual/DOM order: entries not covered by `prev` carry their live `[ref=eN]`.
    let mut remaining_prev = prev_counts;
    let mut added: Vec<SnapshotEntry> = Vec::new();
    for entry in next {
        if let Some(cnt) = remaining_prev.get_mut(entry.rendered.as_str()) {
            if *cnt > 0 {
                *cnt -= 1;
                continue;
            }
        }
        added.push(entry.clone());
    }

    // Walk `prev` in original order: removed entries emit `key` only (never stale `[ref=eN]`).
    let mut remaining_next = next_counts;
    let mut removed: Vec<String> = Vec::new();
    for entry in prev {
        if let Some(cnt) = remaining_next.get_mut(entry.rendered.as_str()) {
            if *cnt > 0 {
                *cnt -= 1;
                continue;
            }
        }
        removed.push(entry.key.clone());
    }

    SnapshotDiff { added, removed }
}

/// Format a `SnapshotDiff` into a bounded diff block suitable for small-model context windows.
pub fn format_snapshot_diff(diff: &SnapshotDiff, max_chars: usize) -> String {
    let mut out = String::with_capacity(512);
    let mut truncated = false;

    for entry in &diff.added {
        let needed = 2 + entry.rendered.len() + 1;
        if out.len() + needed > max_chars {
            truncated = true;
            break;
        }
        out.push_str("+ ");
        out.push_str(&entry.rendered);
        out.push('\n');
    }
    if !truncated {
        for key in &diff.removed {
            let needed = 2 + key.len() + 1;
            if out.len() + needed > max_chars {
                truncated = true;
                break;
            }
            out.push_str("- ");
            out.push_str(key);
            out.push('\n');
        }
    }
    if truncated {
        out.push_str("... (diff truncated)\n");
    }
    out
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
    fn detects_spa_loading_shell_and_plain_text_page_is_not_loading_shell_in_compact() {
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

        // Plain-text page with zero interactive elements must NOT trigger 2.5s loading settle in compact mode.
        let plain_nodes = vec![
            json!({
                "nodeId": "0",
                "ignored": false,
                "role": {"value": "RootWebArea"},
                "name": {"value": "Home"},
                "childIds": ["1"]
            }),
            json!({
                "nodeId": "1",
                "ignored": false,
                "role": {"value": "StaticText"},
                "name": {"value": "Hello"},
                "childIds": []
            }),
        ];
        let mut reg = RefRegistry::new();
        let snap_plain = format_ax_tree_with_options(
            &plain_nodes,
            10_000,
            SnapshotOptions {
                interactive_only: true,
                scope_modal: true,
            },
            &mut reg,
        );
        assert!(!snap_plain.looks_like_loading_shell);

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

    #[test]
    fn compact_mode_distills_flat_controls_and_preserves_alerts_and_aria_states() {
        let nodes = vec![
            json!({
                "nodeId": "0",
                "ignored": false,
                "role": {"value": "RootWebArea"},
                "name": {"value": "Orders"},
                "childIds": ["1", "2", "3", "4", "5", "6"]
            }),
            json!({
                "nodeId": "1",
                "ignored": false,
                "role": {"value": "StaticText"},
                "name": {"value": "Long marketing boilerplate paragraph that should be stripped"},
                "childIds": []
            }),
            json!({
                "nodeId": "2",
                "ignored": false,
                "role": {"value": "heading"},
                "name": {"value": "Checkout"},
                "childIds": []
            }),
            json!({
                "nodeId": "3",
                "ignored": false,
                "role": {"value": "alert"},
                "name": {"value": ""},
                "childIds": ["3a"]
            }),
            json!({
                "nodeId": "3a",
                "ignored": false,
                "role": {"value": "StaticText"},
                "name": {"value": "Coupon code is required"},
                "childIds": []
            }),
            json!({
                "nodeId": "4",
                "backendDOMNodeId": 401,
                "ignored": false,
                "role": {"value": "checkbox"},
                "name": {"value": "Accept terms"},
                "properties": [{"name": "checked", "value": {"value": "true"}}],
                "childIds": []
            }),
            json!({
                "nodeId": "5",
                "backendDOMNodeId": 402,
                "ignored": false,
                "role": {"value": "button"},
                "name": {"value": "Pay Now"},
                "properties": [{"name": "disabled", "value": {"value": true}}],
                "childIds": []
            }),
            json!({
                "nodeId": "6",
                "backendDOMNodeId": 403,
                "ignored": false,
                "role": {"value": "button"},
                "name": {"value": "Details"},
                "properties": [
                    {"name": "expanded", "value": {"value": true}},
                    {"name": "pressed", "value": {"value": "true"}}
                ],
                "childIds": []
            }),
        ];

        let mut reg = RefRegistry::new();
        let snap = format_ax_tree_with_options(
            &nodes,
            10_000,
            SnapshotOptions {
                interactive_only: true,
                scope_modal: true,
            },
            &mut reg,
        );
        assert!(!snap.yaml.contains("marketing boilerplate"));
        assert!(snap.yaml.contains("- heading \"Checkout\""));
        assert!(snap.yaml.contains("- text: \"Coupon code is required\""));
        assert!(snap
            .yaml
            .contains("- checkbox \"Accept terms\" [checked] [ref=e1]"));
        assert!(snap
            .yaml
            .contains("- button \"Pay Now\" [disabled] [ref=e2]"));
        assert!(snap
            .yaml
            .contains("- button \"Details\" [expanded] [pressed] [ref=e3]"));
        for line in snap.yaml.lines() {
            assert!(line.starts_with("- "), "unexpected indent in: {}", line);
        }
    }

    #[test]
    fn focusable_tabindex_minus_one_modal_preserves_body_text_and_empty_modal_does_not_hijack() {
        // Real Radix/MUI/HeadlessUI modals set tabindex="-1" -> focusable=true on the dialog itself.
        let nodes = vec![
            json!({
                "nodeId": "0",
                "ignored": false,
                "role": {"value": "RootWebArea"},
                "childIds": ["bg1", "modal1", "empty_focusable_modal"]
            }),
            json!({
                "nodeId": "bg1",
                "backendDOMNodeId": 10,
                "ignored": false,
                "role": {"value": "button"},
                "name": {"value": "Background Action"},
                "childIds": []
            }),
            json!({
                "nodeId": "modal1",
                "backendDOMNodeId": 88,
                "ignored": false,
                "role": {"value": "dialog"},
                "name": {"value": "Confirm Payment"},
                "properties": [
                    {"name": "modal", "value": {"value": true}},
                    {"name": "focusable", "value": {"value": true}}
                ],
                "childIds": ["m_txt", "m_ok"]
            }),
            json!({
                "nodeId": "m_txt",
                "ignored": false,
                "role": {"value": "StaticText"},
                "name": {"value": "Charge $42.00 now?"},
                "childIds": []
            }),
            json!({
                "nodeId": "m_ok",
                "backendDOMNodeId": 99,
                "ignored": false,
                "role": {"value": "button"},
                "name": {"value": "Confirm"},
                "childIds": ["m_ok_txt"]
            }),
            json!({
                "nodeId": "m_ok_txt",
                "ignored": false,
                "role": {"value": "StaticText"},
                "name": {"value": "Confirm"},
                "childIds": []
            }),
            // Empty placeholder modal with tabindex="-1" must NOT hijack page scope.
            json!({
                "nodeId": "empty_focusable_modal",
                "backendDOMNodeId": 101,
                "ignored": false,
                "role": {"value": "dialog"},
                "name": {"value": "Empty Shell"},
                "properties": [
                    {"name": "modal", "value": {"value": true}},
                    {"name": "focusable", "value": {"value": true}}
                ],
                "childIds": []
            }),
        ];

        let mut reg = RefRegistry::new();
        let scoped = format_ax_tree_with_options(
            &nodes,
            10_000,
            SnapshotOptions {
                interactive_only: true,
                scope_modal: true,
            },
            &mut reg,
        );
        assert_eq!(scoped.active_modal.as_deref(), Some("Confirm Payment"));
        assert!(scoped.modal_scoped);
        assert_eq!(scoped.obscured_interactive_count, 1);
        assert!(scoped.yaml.contains("- text: \"Charge $42.00 now?\""));
        assert!(scoped.yaml.contains("- button \"Confirm\" [ref=e1]"));
        assert!(!scoped.yaml.contains("- text: \"Confirm\""));
        assert!(!scoped.yaml.contains("Background Action"));
        // The dialog container itself must NOT consume a clickable ref.
        assert_eq!(scoped.ref_map.len(), 1);
        assert_eq!(scoped.ref_map.get("e1"), Some(&99));
    }

    #[test]
    fn deep_dom_nesting_does_not_stack_overflow_in_compact_or_full_mode() {
        // 3,000 nested ignored/generic wrapper divs + 1 leaf button at the bottom.
        let depth = 3_000usize;
        let mut nodes = Vec::with_capacity(depth + 2);
        nodes.push(json!({
            "nodeId": "n0",
            "ignored": false,
            "role": {"value": "RootWebArea"},
            "childIds": ["n1"]
        }));
        for i in 1..depth {
            nodes.push(json!({
                "nodeId": format!("n{}", i),
                "ignored": true,
                "role": {"value": "generic"},
                "childIds": [format!("n{}", i + 1)]
            }));
        }
        nodes.push(json!({
            "nodeId": format!("n{}", depth),
            "backendDOMNodeId": 7777,
            "ignored": false,
            "role": {"value": "button"},
            "name": {"value": "DeepLeaf"},
            "childIds": []
        }));

        let mut reg = RefRegistry::new();
        let snap = format_ax_tree_with_options(
            &nodes,
            10_000,
            SnapshotOptions {
                interactive_only: true,
                scope_modal: true,
            },
            &mut reg,
        );
        assert!(snap.yaml.contains("- button \"DeepLeaf\" [ref=e1]"));
        assert_eq!(snap.ref_map.get("e1"), Some(&7777));
    }

    #[test]
    fn diff_handles_top_insertion_duplicate_labels_and_dom_remount_accurately() {
        let mut reg = RefRegistry::new();
        let opts = SnapshotOptions {
            interactive_only: true,
            scope_modal: true,
        };
        let initial = vec![
            json!({
                "nodeId": "0",
                "ignored": false,
                "role": {"value": "RootWebArea"},
                "childIds": ["b1"]
            }),
            json!({
                "nodeId": "b1",
                "backendDOMNodeId": 500,
                "ignored": false,
                "role": {"value": "button"},
                "name": {"value": "Delete"},
                "childIds": []
            }),
        ];
        let snap1 = format_ax_tree_with_options(&initial, 10_000, opts, &mut reg);
        assert_eq!(snap1.ref_map.get("e1"), Some(&500));

        // 1. Insert a DUPLICATE-LABEL button ABOVE existing Delete:
        //    Existing node (500) keeps e1; new node (999) gets e2; diff MUST report + [ref=e2]!
        let dup_insert = vec![
            json!({
                "nodeId": "0",
                "ignored": false,
                "role": {"value": "RootWebArea"},
                "childIds": ["b_new", "b1"]
            }),
            json!({
                "nodeId": "b_new",
                "backendDOMNodeId": 999,
                "ignored": false,
                "role": {"value": "button"},
                "name": {"value": "Delete"},
                "childIds": []
            }),
            json!({
                "nodeId": "b1",
                "backendDOMNodeId": 500,
                "ignored": false,
                "role": {"value": "button"},
                "name": {"value": "Delete"},
                "childIds": []
            }),
        ];
        let snap2 = format_ax_tree_with_options(&dup_insert, 10_000, opts, &mut reg);
        let diff_dup = compute_snapshot_diff(&snap1.entries, &snap2.entries);
        assert_eq!(diff_dup.added.len(), 1);
        assert_eq!(diff_dup.added[0].rendered, "button \"Delete\" [ref=e2]");
        assert!(diff_dup.removed.is_empty());

        // 2. React remount replaces backendDOMNodeId 500 with 600 (same label!):
        //    Diff MUST report - old and + new [ref=e3] so agent gets the live ref.
        let remounted = vec![
            json!({
                "nodeId": "0",
                "ignored": false,
                "role": {"value": "RootWebArea"},
                "childIds": ["b_new", "b_remount"]
            }),
            json!({
                "nodeId": "b_new",
                "backendDOMNodeId": 999,
                "ignored": false,
                "role": {"value": "button"},
                "name": {"value": "Delete"},
                "childIds": []
            }),
            json!({
                "nodeId": "b_remount",
                "backendDOMNodeId": 600,
                "ignored": false,
                "role": {"value": "button"},
                "name": {"value": "Delete"},
                "childIds": []
            }),
        ];
        let snap3 = format_ax_tree_with_options(&remounted, 10_000, opts, &mut reg);
        let diff_remount = compute_snapshot_diff(&snap2.entries, &snap3.entries);
        assert_eq!(diff_remount.added.len(), 1);
        assert_eq!(diff_remount.added[0].rendered, "button \"Delete\" [ref=e3]");
        assert_eq!(diff_remount.removed, vec!["button \"Delete\"".to_string()]);
        assert_eq!(
            format_snapshot_diff(&diff_remount, 20),
            "... (diff truncated)\n"
        );
    }

    #[test]
    fn ref_registry_prunes_dead_nodes_above_cap_while_preserving_live_refs() {
        let mut reg = RefRegistry::new();
        for bid in 1..=(MAX_PERSISTENT_REFS as i64 + 50) {
            let _ = reg.get_or_assign(bid);
        }
        let mut live = HashMap::new();
        live.insert("e10".to_string(), 10);
        reg.prune_if_oversized(&live);
        assert_eq!(reg.get_or_assign(10), "e10");
        // Detached node 1 was pruned so re-seeing it allocates a fresh ref rather than leaking unbounded memory.
        assert_ne!(reg.get_or_assign(1), "e1");
    }

    #[test]
    fn modal_detection_traverses_ignored_wrapper_divs_and_respects_unscoped_mode() {
        let nodes = vec![
            json!({
                "nodeId": "0",
                "ignored": false,
                "role": {"value": "RootWebArea"},
                "childIds": ["bg", "wrapper"]
            }),
            json!({
                "nodeId": "bg",
                "backendDOMNodeId": 10,
                "ignored": false,
                "role": {"value": "button"},
                "name": {"value": "Background"},
                "childIds": []
            }),
            json!({
                "nodeId": "wrapper",
                "ignored": true,
                "role": {"value": "generic"},
                "childIds": ["dlg"]
            }),
            json!({
                "nodeId": "dlg",
                "ignored": false,
                "role": {"value": "alertdialog"},
                "name": {"value": "Delete Item"},
                "childIds": ["body_txt", "ok_btn"]
            }),
            json!({
                "nodeId": "body_txt",
                "ignored": false,
                "role": {"value": "StaticText"},
                "name": {"value": "Are you sure?"},
                "childIds": []
            }),
            json!({
                "nodeId": "ok_btn",
                "backendDOMNodeId": 20,
                "ignored": false,
                "role": {"value": "button"},
                "name": {"value": "Confirm"},
                "childIds": ["ok_txt"]
            }),
            json!({
                "nodeId": "ok_txt",
                "ignored": false,
                "role": {"value": "StaticText"},
                "name": {"value": "Confirm"},
                "childIds": []
            }),
        ];
        let mut reg = RefRegistry::new();
        let scoped = format_ax_tree_with_options(
            &nodes,
            10_000,
            SnapshotOptions {
                interactive_only: true,
                scope_modal: true,
            },
            &mut reg,
        );
        assert_eq!(scoped.active_modal.as_deref(), Some("Delete Item"));
        assert!(scoped.modal_scoped);
        assert_eq!(scoped.obscured_interactive_count, 1);
        assert!(scoped.yaml.contains("- text: \"Are you sure?\""));
        assert!(scoped.yaml.contains("- button \"Confirm\" [ref=e1]"));
        assert!(!scoped.yaml.contains("- text: \"Confirm\""));
        assert!(!scoped.yaml.contains("Background"));

        // Negative control: scope_modal=false keeps background elements while still reporting active_modal.
        let mut reg2 = RefRegistry::new();
        let unscoped = format_ax_tree_with_options(
            &nodes,
            10_000,
            SnapshotOptions {
                interactive_only: false,
                scope_modal: false,
            },
            &mut reg2,
        );
        assert_eq!(unscoped.active_modal.as_deref(), Some("Delete Item"));
        assert!(!unscoped.modal_scoped);
        assert!(unscoped.yaml.contains("Background"));
    }
}
