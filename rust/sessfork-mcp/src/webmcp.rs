use crate::markdown::truncate_chars;
use crate::storage::normalize_origin;
use serde_json::{json, Value};
use std::collections::HashMap;

const MAX_WEBMCP_DESC_CHARS: usize = 2_048;
const MAX_WEBMCP_SCHEMA_BYTES: usize = 16 * 1024;
const MAX_WEBMCP_OUTPUT_CHARS: usize = 32_000;

#[derive(Debug, Clone, PartialEq)]
pub struct FrameOriginInfo {
    pub frame_id: String,
    pub url: String,
    pub origin: String,
    pub is_top: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct WebMcpTool {
    pub name: String,
    pub description: String,
    pub frame_id: String,
    pub input_schema: Value,
    pub backend_node_id: Option<i64>,
    pub autosubmit: bool,
    pub raw_annotations: Option<Value>,
}

fn clamp_json_size(val: Value, max_bytes: usize, fallback: Value) -> Value {
    match serde_json::to_string(&val) {
        Ok(s) if s.len() <= max_bytes => val,
        _ => fallback,
    }
}

impl WebMcpTool {
    pub fn from_cdp_value(val: &Value) -> Option<Self> {
        let name = val.get("name")?.as_str()?.trim();
        let frame_id = val.get("frameId")?.as_str()?.trim();
        if name.is_empty() || frame_id.is_empty() {
            return None;
        }
        let raw_desc = val
            .get("description")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let description = truncate_chars(raw_desc, MAX_WEBMCP_DESC_CHARS);
        let raw_schema = val
            .get("inputSchema")
            .filter(|v| v.is_object())
            .cloned()
            .unwrap_or_else(|| json!({ "type": "object", "properties": {} }));
        let input_schema = clamp_json_size(
            raw_schema,
            MAX_WEBMCP_SCHEMA_BYTES,
            json!({ "type": "object", "properties": {}, "_truncated": true }),
        );
        // CDP backendNodeId 0 (or negative) is not a valid DOM node handle.
        let backend_node_id = val
            .get("backendNodeId")
            .and_then(|v| v.as_i64())
            .filter(|&id| id > 0);
        let raw_annotations = val
            .get("annotations")
            .filter(|v| !v.is_null())
            .cloned()
            .and_then(|ann| {
                let s = serde_json::to_string(&ann).ok()?;
                if s.len() <= MAX_WEBMCP_SCHEMA_BYTES {
                    Some(ann)
                } else {
                    None
                }
            });
        let autosubmit = raw_annotations
            .as_ref()
            .and_then(|a| a.get("autosubmit"))
            .and_then(|v| v.as_bool())
            .unwrap_or(false);

        Some(Self {
            name: name.to_string(),
            description,
            frame_id: frame_id.to_string(),
            input_schema,
            backend_node_id,
            autosubmit,
            raw_annotations,
        })
    }

    pub fn is_declarative_form(&self) -> bool {
        self.backend_node_id.is_some()
    }

    pub fn requires_manual_confirmation(&self) -> bool {
        self.is_declarative_form() && !self.autosubmit
    }

    pub fn to_summary_json(&self, frame_info: Option<&FrameOriginInfo>, top_origin: &str) -> Value {
        let kind = if self.is_declarative_form() {
            if self.autosubmit {
                "declarative_form_autosubmit"
            } else {
                "declarative_form_manual_confirm"
            }
        } else {
            "imperative"
        };
        let same_origin = is_same_origin_frame(frame_info, top_origin);
        let mut obj = json!({
            "name": self.name,
            "description": self.description,
            "frameId": self.frame_id,
            "frameUrl": frame_info.map(|f| f.url.as_str()).unwrap_or(""),
            "frameOrigin": frame_info.map(|f| f.origin.as_str()).unwrap_or(""),
            "isTopFrame": frame_info.map(|f| f.is_top).unwrap_or(false),
            "sameOriginWithTop": same_origin,
            "kind": kind,
            "requiresConfirm": self.requires_manual_confirmation(),
            "inputSchema": self.input_schema,
        });
        if let Some(ref ann) = self.raw_annotations {
            if let Some(map) = obj.as_object_mut() {
                map.insert("annotations".to_string(), ann.clone());
            }
        }
        obj
    }
}

pub fn is_same_origin_frame(frame_info: Option<&FrameOriginInfo>, top_origin: &str) -> bool {
    let Some(info) = frame_info else {
        return false;
    };
    if info.is_top {
        return true;
    }
    let norm_top = normalize_origin(top_origin);
    let norm_frame = normalize_origin(&info.origin);
    !norm_top.is_empty() && norm_top != "null" && norm_frame == norm_top
}

pub fn select_tool<'a>(
    tools: &'a [WebMcpTool],
    name: &str,
    frame_id: Option<&str>,
    frame_map: &HashMap<String, FrameOriginInfo>,
    top_origin: &str,
) -> Result<&'a WebMcpTool, String> {
    let target_name = name.trim();
    if target_name.is_empty() {
        return Err("Parameter 'name' must be a non-empty WebMCP tool name".to_string());
    }
    let filter_frame = frame_id.map(str::trim).filter(|s| !s.is_empty());

    if let Some(fid) = filter_frame {
        let matches: Vec<&WebMcpTool> = tools
            .iter()
            .filter(|t| t.name == target_name && t.frame_id == fid)
            .collect();
        return match matches.as_slice() {
            [] => Err(format!(
                "WebMCP tool '{}' is not registered in frame '{}' (call browser_webmcp_list first)",
                target_name, fid
            )),
            [single] => Ok(single),
            _ => Err(format!(
                "WebMCP tool '{}' appeared multiple times in frame '{}'",
                target_name, fid
            )),
        };
    }

    let all_matches: Vec<&WebMcpTool> = tools.iter().filter(|t| t.name == target_name).collect();
    if all_matches.is_empty() {
        return Err(format!(
            "WebMCP tool '{}' is not registered on the current page (call browser_webmcp_list first)",
            target_name
        ));
    }

    // Without explicit `frameId`, restrict implicit selection to top frame or same-origin frames
    // so cross-origin third-party iframes cannot silently hijack tool calls or gain userGesture.
    let same_origin_matches: Vec<&WebMcpTool> = all_matches
        .iter()
        .copied()
        .filter(|t| is_same_origin_frame(frame_map.get(&t.frame_id), top_origin))
        .collect();

    match same_origin_matches.as_slice() {
        [single] => Ok(single),
        [] => {
            let cross_frames: Vec<String> = all_matches
                .iter()
                .map(|t| {
                    let orig = frame_map
                        .get(&t.frame_id)
                        .map(|f| f.origin.as_str())
                        .filter(|s| !s.is_empty())
                        .unwrap_or("unknown-origin");
                    format!("{} ({})", t.frame_id, orig)
                })
                .collect();
            Err(format!(
                "WebMCP tool '{}' is only registered in cross-origin iframe(s) [{}]; inspect browser_webmcp_list and pass an explicit 'frameId' to invoke cross-origin tools",
                target_name,
                cross_frames.join(", ")
            ))
        }
        many => {
            let frames: Vec<&str> = many.iter().map(|t| t.frame_id.as_str()).collect();
            Err(format!(
                "WebMCP tool '{}' exists in multiple frames ({}); pass 'frameId' to disambiguate",
                target_name,
                frames.join(", ")
            ))
        }
    }
}

pub fn format_tool_catalog(
    url: &str,
    title: &str,
    tools: &[WebMcpTool],
    frame_map: &HashMap<String, FrameOriginInfo>,
    top_origin: &str,
) -> String {
    let mut out = format!("### Page\n- Page URL: {}\n- Page Title: {}\n", url, title);
    if tools.is_empty() {
        out.push_str("\n### WebMCP Tools (0)\n(no native WebMCP tools registered on this page)\n");
        return out;
    }

    let items: Vec<Value> = tools
        .iter()
        .map(|t| t.to_summary_json(frame_map.get(&t.frame_id), top_origin))
        .collect();
    let rendered =
        serde_json::to_string_pretty(&items).unwrap_or_else(|_| Value::Array(items).to_string());
    let bounded = truncate_chars(&rendered, MAX_WEBMCP_OUTPUT_CHARS);
    out.push_str(&format!(
        "\n### WebMCP Tools ({})\n```json\n{}\n```\n",
        tools.len(),
        bounded
    ));
    out
}

fn extract_non_empty_detail(val: &Value) -> Option<String> {
    match val {
        Value::Null => None,
        Value::String(s) => {
            let trimmed = s.trim();
            if trimmed.is_empty() {
                None
            } else {
                Some(trimmed.to_string())
            }
        }
        other => Some(other.to_string()),
    }
}

pub fn format_invocation_outcome(tool_name: &str, response: &Value) -> Result<String, String> {
    let status = response
        .get("status")
        .and_then(|v| v.as_str())
        .unwrap_or("Unknown");
    if status == "Completed" {
        let payload = json!({
            "tool": tool_name,
            "status": status,
            "output": response.get("output").cloned().unwrap_or(Value::Null),
        });
        let raw = serde_json::to_string_pretty(&payload).unwrap_or_else(|_| payload.to_string());
        return Ok(truncate_chars(&raw, MAX_WEBMCP_OUTPUT_CHARS));
    }

    let detail = ["errorText", "error", "exception", "output"]
        .iter()
        .find_map(|key| response.get(*key).and_then(extract_non_empty_detail))
        .unwrap_or_else(|| response.to_string());
    let bounded_detail = truncate_chars(&detail, 4_096);
    Err(format!(
        "WebMCP tool '{}' ended with status '{}': {}",
        tool_name, status, bounded_detail
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_imperative_declarative_and_malformed_cdp_tool_values() {
        let imp = WebMcpTool::from_cdp_value(&json!({
            "name": " add_numbers ",
            "description": "Add two numbers",
            "frameId": " F1 ",
            "inputSchema": null,
            "backendNodeId": 0
        }))
        .expect("imperative tool with backendNodeId=0 and null schema");
        assert_eq!(imp.name, "add_numbers");
        assert_eq!(imp.frame_id, "F1");
        assert_eq!(imp.backend_node_id, None);
        assert!(!imp.is_declarative_form());
        assert!(!imp.requires_manual_confirmation());
        assert!(imp.input_schema.is_object());

        let auto_form = WebMcpTool::from_cdp_value(&json!({
            "name": "order_pizza",
            "description": "Auto submit form",
            "frameId": "F1",
            "backendNodeId": 42,
            "annotations": { "autosubmit": true }
        }))
        .expect("autosubmit form");
        assert!(auto_form.is_declarative_form());
        assert!(!auto_form.requires_manual_confirmation());

        let manual_form = WebMcpTool::from_cdp_value(&json!({
            "name": "submit_payment",
            "description": "Manual form",
            "frameId": "F1",
            "backendNodeId": 99
        }))
        .expect("manual form");
        assert!(manual_form.is_declarative_form());
        assert!(manual_form.requires_manual_confirmation());

        // Missing or blank required fields must be rejected.
        assert!(WebMcpTool::from_cdp_value(&json!({"name": "", "frameId": "F1"})).is_none());
        assert!(WebMcpTool::from_cdp_value(&json!({"name": "x", "frameId": "   "})).is_none());
    }

    #[test]
    fn disambiguates_frames_and_blocks_implicit_cross_origin_selection() {
        let mut frames = HashMap::new();
        frames.insert(
            "F1".to_string(),
            FrameOriginInfo {
                frame_id: "F1".to_string(),
                url: "https://trusted.example/app".to_string(),
                origin: "https://trusted.example".to_string(),
                is_top: true,
            },
        );
        frames.insert(
            "F2".to_string(),
            FrameOriginInfo {
                frame_id: "F2".to_string(),
                url: "https://trusted.example/frame".to_string(),
                origin: "https://trusted.example".to_string(),
                is_top: false,
            },
        );
        frames.insert(
            "F_X".to_string(),
            FrameOriginInfo {
                frame_id: "F_X".to_string(),
                url: "https://evil.example/widget".to_string(),
                origin: "https://evil.example".to_string(),
                is_top: false,
            },
        );

        let t1 = WebMcpTool::from_cdp_value(&json!({
            "name": "search",
            "frameId": "F1"
        }))
        .expect("t1");
        let t2 = WebMcpTool::from_cdp_value(&json!({
            "name": "search",
            "frameId": "F2"
        }))
        .expect("t2");
        let cross = WebMcpTool::from_cdp_value(&json!({
            "name": "steal_data",
            "frameId": "F_X"
        }))
        .expect("cross");
        let list = vec![t1, t2, cross];

        assert!(
            select_tool(&list, "   ", None, &frames, "https://trusted.example")
                .expect_err("empty name")
                .contains("non-empty")
        );

        let ambig = select_tool(&list, "search", None, &frames, "https://trusted.example")
            .expect_err("ambiguous without frameId");
        assert!(ambig.contains("multiple frames"));
        assert!(ambig.contains("F1") && ambig.contains("F2"));

        let chosen = select_tool(
            &list,
            "search",
            Some("F2"),
            &frames,
            "https://trusted.example",
        )
        .expect("explicit frameId");
        assert_eq!(chosen.frame_id, "F2");

        // Cross-origin iframe tool MUST NOT be selected implicitly without explicit frameId.
        let cross_err = select_tool(
            &list,
            "steal_data",
            None,
            &frames,
            "https://trusted.example",
        )
        .expect_err("cross-origin implicit selection must fail closed");
        assert!(cross_err.contains("cross-origin iframe"));
        assert!(cross_err.contains("https://evil.example"));

        // Explicit frameId allows deliberate invocation after inspecting catalog.
        let explicit_cross = select_tool(
            &list,
            "steal_data",
            Some("F_X"),
            &frames,
            "https://trusted.example",
        )
        .expect("explicit cross-origin frameId");
        assert_eq!(explicit_cross.frame_id, "F_X");

        let wrong_frame = select_tool(
            &list,
            "search",
            Some("F99"),
            &frames,
            "https://trusted.example",
        )
        .expect_err("wrong frame");
        assert!(wrong_frame.contains("frame 'F99'"));

        let missing = select_tool(
            &list,
            "nonexistent",
            None,
            &frames,
            "https://trusted.example",
        )
        .expect_err("missing tool");
        assert!(missing.contains("not registered"));
    }

    #[test]
    fn formats_catalog_empty_populated_and_cross_origin_attribution() {
        let frames = HashMap::new();
        let empty = format_tool_catalog(
            "https://example.com",
            "Empty",
            &[],
            &frames,
            "https://example.com",
        );
        assert!(empty.contains("WebMCP Tools (0)"));

        let tool = WebMcpTool::from_cdp_value(&json!({
            "name": "ping",
            "description": "Ping service",
            "frameId": "F1"
        }))
        .expect("tool");
        let mut fmap = HashMap::new();
        fmap.insert(
            "F1".to_string(),
            FrameOriginInfo {
                frame_id: "F1".to_string(),
                url: "https://evil.example/embed".to_string(),
                origin: "https://evil.example".to_string(),
                is_top: false,
            },
        );
        let rendered = format_tool_catalog(
            "https://example.com",
            "Demo",
            &[tool],
            &fmap,
            "https://example.com",
        );
        assert!(rendered.contains("WebMCP Tools (1)"));
        assert!(rendered.contains("\"name\": \"ping\""));
        assert!(rendered.contains("\"frameOrigin\": \"https://evil.example\""));
        assert!(rendered.contains("\"sameOriginWithTop\": false"));
        assert!(rendered.contains("\"requiresConfirm\": false"));
    }

    #[test]
    fn formats_completed_canceled_and_empty_errortext_fallback_outcomes() {
        let ok = format_invocation_outcome(
            "add_numbers",
            &json!({"invocationId": "1", "status": "Completed", "output": {"sum": 42}}),
        )
        .expect("completed");
        let parsed: Value = serde_json::from_str(&ok).expect("valid json");
        assert_eq!(parsed["tool"], "add_numbers");
        assert_eq!(parsed["output"]["sum"], 42);

        // Empty errorText must fall through to exception details instead of returning blank error.
        let fallback_err = format_invocation_outcome(
            "add_numbers",
            &json!({"invocationId": "2", "status": "Error", "errorText": "  ", "exception": {"message": "TypeError"}}),
        )
        .expect_err("failed status must return Err");
        assert!(fallback_err.contains("Error"));
        assert!(fallback_err.contains("TypeError"));

        let canceled = format_invocation_outcome(
            "add_numbers",
            &json!({"invocationId": "3", "status": "Canceled"}),
        )
        .expect_err("canceled status must return Err");
        assert!(canceled.contains("Canceled"));
    }
}
