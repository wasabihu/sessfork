use serde_json::{json, Value};

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

impl WebMcpTool {
    pub fn from_cdp_value(val: &Value) -> Option<Self> {
        let name = val.get("name")?.as_str()?.trim();
        let frame_id = val.get("frameId")?.as_str()?.trim();
        if name.is_empty() || frame_id.is_empty() {
            return None;
        }
        let description = val
            .get("description")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let input_schema = val
            .get("inputSchema")
            .filter(|v| v.is_object())
            .cloned()
            .unwrap_or_else(|| json!({ "type": "object", "properties": {} }));
        // CDP backendNodeId 0 (or negative) is not a valid DOM node handle.
        let backend_node_id = val
            .get("backendNodeId")
            .and_then(|v| v.as_i64())
            .filter(|&id| id > 0);
        let raw_annotations = val.get("annotations").filter(|v| !v.is_null()).cloned();
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

    pub fn to_summary_json(&self) -> Value {
        let kind = if self.is_declarative_form() {
            if self.autosubmit {
                "declarative_form_autosubmit"
            } else {
                "declarative_form_manual_confirm"
            }
        } else {
            "imperative"
        };
        let mut obj = json!({
            "name": self.name,
            "description": self.description,
            "frameId": self.frame_id,
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

pub fn select_tool<'a>(
    tools: &'a [WebMcpTool],
    name: &str,
    frame_id: Option<&str>,
) -> Result<&'a WebMcpTool, String> {
    let target_name = name.trim();
    if target_name.is_empty() {
        return Err("Parameter 'name' must be a non-empty WebMCP tool name".to_string());
    }
    let filter_frame = frame_id.map(str::trim).filter(|s| !s.is_empty());
    let matches: Vec<&WebMcpTool> = tools
        .iter()
        .filter(|t| t.name == target_name && filter_frame.is_none_or(|fid| t.frame_id == fid))
        .collect();

    match matches.as_slice() {
        [] => {
            if let Some(fid) = filter_frame {
                Err(format!(
                    "WebMCP tool '{}' is not registered in frame '{}' (call browser_webmcp_list first)",
                    target_name, fid
                ))
            } else {
                Err(format!(
                    "WebMCP tool '{}' is not registered on the current page (call browser_webmcp_list first)",
                    target_name
                ))
            }
        }
        [single] => Ok(single),
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

pub fn format_tool_catalog(url: &str, title: &str, tools: &[WebMcpTool]) -> String {
    let mut out = format!("### Page\n- Page URL: {}\n- Page Title: {}\n", url, title);
    if tools.is_empty() {
        out.push_str("\n### WebMCP Tools (0)\n(no native WebMCP tools registered on this page)\n");
        return out;
    }

    let items: Vec<Value> = tools.iter().map(WebMcpTool::to_summary_json).collect();
    let rendered =
        serde_json::to_string_pretty(&items).unwrap_or_else(|_| Value::Array(items).to_string());
    out.push_str(&format!(
        "\n### WebMCP Tools ({})\n```json\n{}\n```\n",
        tools.len(),
        rendered
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
        return Ok(serde_json::to_string_pretty(&payload).unwrap_or_else(|_| payload.to_string()));
    }

    let detail = ["errorText", "error", "exception", "output"]
        .iter()
        .find_map(|key| response.get(*key).and_then(extract_non_empty_detail))
        .unwrap_or_else(|| response.to_string());
    Err(format!(
        "WebMCP tool '{}' ended with status '{}': {}",
        tool_name, status, detail
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
    fn disambiguates_frames_and_rejects_empty_missing_or_wrong_frame() {
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
        let list = vec![t1, t2];

        assert!(select_tool(&list, "   ", None)
            .expect_err("empty name")
            .contains("non-empty"));

        let ambig = select_tool(&list, "search", None).expect_err("ambiguous without frameId");
        assert!(ambig.contains("multiple frames"));
        assert!(ambig.contains("F1") && ambig.contains("F2"));

        let chosen = select_tool(&list, "search", Some("F2")).expect("explicit frameId");
        assert_eq!(chosen.frame_id, "F2");

        let wrong_frame = select_tool(&list, "search", Some("F99")).expect_err("wrong frame");
        assert!(wrong_frame.contains("frame 'F99'"));

        let missing = select_tool(&list, "nonexistent", None).expect_err("missing tool");
        assert!(missing.contains("not registered"));
    }

    #[test]
    fn formats_catalog_empty_and_populated() {
        let empty = format_tool_catalog("https://example.com", "Empty", &[]);
        assert!(empty.contains("WebMCP Tools (0)"));

        let tool = WebMcpTool::from_cdp_value(&json!({
            "name": "ping",
            "description": "Ping service",
            "frameId": "F1"
        }))
        .expect("tool");
        let rendered = format_tool_catalog("https://example.com", "Demo", &[tool]);
        assert!(rendered.contains("WebMCP Tools (1)"));
        assert!(rendered.contains("\"name\": \"ping\""));
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
