use crate::browser::BrowserManager;
use serde_json::{json, Value};

const SERVER_NAME: &str = "sessfork-mcp";
const SERVER_VERSION: &str = env!("CARGO_PKG_VERSION");
const DEFAULT_MAX_SNAPSHOT_CHARS: usize = 32_000;

pub fn tools_manifest() -> Value {
    json!([
        {
            "name": "browser_navigate",
            "description": "Navigate the current tab to a URL and return the page title, URL, and compact accessibility snapshot.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": { "type": "string", "description": "Target http(s):// URL" },
                    "snapshot": { "type": "boolean", "description": "Include accessibility snapshot (default: true)" },
                    "maxChars": { "type": "integer", "description": "Max snapshot characters (default: 32000)" }
                },
                "required": ["url"]
            }
        },
        {
            "name": "browser_snapshot",
            "description": "Capture the accessibility tree of the current page with clickable [ref=eN] handles.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "maxChars": { "type": "integer", "description": "Max snapshot characters (default: 32000)" }
                }
            }
        },
        {
            "name": "browser_tabs",
            "description": "List, create (optionally in a named isolatedContext sandbox), select, or close tabs.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": { "type": "string", "enum": ["list", "new", "select", "close"] },
                    "index": { "type": "integer", "description": "0-based tab index for select/close" },
                    "isolatedContext": { "type": "string", "description": "Named isolated cookie/storage context for action='new'" }
                },
                "required": ["action"]
            }
        },
        {
            "name": "browser_set_storage_state",
            "description": "Hot-reload Cookies and LocalStorage from a sessfork Playwright storageState JSON file inside --output-dir into the current context (credentials are never echoed).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "filename": { "type": "string", "description": "Path to storageState JSON inside allowed --output-dir" }
                },
                "required": ["filename"]
            }
        },
        {
            "name": "browser_click",
            "description": "Click an element by snapshot ref (e.g. 'e3') or CSS selector.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ref": { "type": "string", "description": "Snapshot element ref (e.g. 'e1')" },
                    "selector": { "type": "string", "description": "Fallback CSS selector" },
                    "element": { "type": "string", "description": "Human-readable element label (optional)" }
                }
            }
        },
        {
            "name": "browser_type",
            "description": "Type text into an input/textarea by snapshot ref or CSS selector (React/Vue compatible).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ref": { "type": "string", "description": "Snapshot element ref" },
                    "selector": { "type": "string", "description": "Fallback CSS selector" },
                    "text": { "type": "string", "description": "Text to type" },
                    "clear": { "type": "boolean", "description": "Replace existing value (default: true)" },
                    "submit": { "type": "boolean", "description": "Press Enter / submit form after typing" }
                },
                "required": ["text"]
            }
        },
        {
            "name": "browser_press_key",
            "description": "Dispatch a keyboard key press (e.g. 'Enter', 'Escape', 'Tab').",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "key": { "type": "string" }
                },
                "required": ["key"]
            }
        },
        {
            "name": "browser_evaluate",
            "description": "Evaluate a JavaScript expression or arrow function in the current page.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "function": { "type": "string", "description": "JavaScript arrow function or expression" },
                    "expression": { "type": "string", "description": "Alias for function" }
                }
            }
        },
        {
            "name": "browser_navigate_back",
            "description": "Go back to the previous page in tab history.",
            "inputSchema": { "type": "object", "properties": {} }
        },
        {
            "name": "browser_wait_for",
            "description": "Wait for specific text to appear on the page or sleep for a bounded duration.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": { "type": "string" },
                    "time": { "type": "number", "description": "Seconds to wait (max 30)" }
                }
            }
        },
        {
            "name": "browser_console_messages",
            "description": "Return recent browser console log messages for the current tab.",
            "inputSchema": { "type": "object", "properties": {} }
        },
        {
            "name": "browser_take_screenshot",
            "description": "Capture a PNG screenshot into --output-dir (requires --layout).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "filename": { "type": "string", "description": "Output filename inside --output-dir" }
                }
            }
        },
        {
            "name": "browser_extract_markdown",
            "description": "Extract clean, structure-first Markdown from the current page DOM without triggering layout or paint (optionally scoped to a CSS selector).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "selector": { "type": "string", "description": "Optional CSS selector to scope extraction (e.g. 'main', 'article')" },
                    "maxChars": { "type": "integer", "description": "Max Markdown characters (default: 32000)" }
                }
            }
        },
        {
            "name": "browser_webmcp_list",
            "description": "List native WebMCP tools registered by the current page (and its child frames) via Moli's WebMCP CDP domain.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "frameId": { "type": "string", "description": "Optional frameId to filter tools to a single frame" },
                    "waitMs": { "type": "integer", "description": "Optional milliseconds to wait for late-registering tools if empty (max 5000)" }
                }
            }
        },
        {
            "name": "browser_webmcp_call",
            "description": "Invoke a native WebMCP tool registered by the current page with JSON arguments (zero clicks, inherits current tab auth session).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": { "type": "string", "description": "Registered WebMCP tool name from browser_webmcp_list" },
                    "arguments": { "type": "object", "description": "JSON object of tool input arguments (default: {})" },
                    "frameId": { "type": "string", "description": "Optional frameId when multiple frames register the same tool name" },
                    "confirm": { "type": "boolean", "description": "Explicitly confirm & submit a declarative WebMCP form that lacks autosubmit (default: false)" },
                    "timeoutMs": { "type": "integer", "description": "Response timeout in milliseconds (500..30000, default: 15000)" }
                },
                "required": ["name"]
            }
        },
        {
            "name": "browser_close",
            "description": "Close the underlying Moli browser instance cleanly.",
            "inputSchema": { "type": "object", "properties": {} }
        }
    ])
}

pub fn handle_jsonrpc_message(msg: &Value, browser: &mut BrowserManager) -> Option<Value> {
    let method = msg.get("method").and_then(|v| v.as_str()).unwrap_or("");
    // JSON-RPC notifications have no id and must not receive a response.
    let req_id = msg.get("id")?.clone();

    match method {
        "initialize" => {
            let client_proto = msg
                .pointer("/params/protocolVersion")
                .and_then(|v| v.as_str())
                .unwrap_or("2024-11-05");
            Some(json!({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": client_proto,
                    "capabilities": { "tools": {} },
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION
                    }
                }
            }))
        }
        "ping" => Some(json!({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {}
        })),
        "tools/list" => Some(json!({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": { "tools": tools_manifest() }
        })),
        "resources/list" => Some(json!({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": { "resources": [] }
        })),
        "prompts/list" => Some(json!({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": { "prompts": [] }
        })),
        "tools/call" => {
            let tool_name = msg
                .pointer("/params/name")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let args = msg
                .pointer("/params/arguments")
                .cloned()
                .unwrap_or_else(|| json!({}));

            let outcome = dispatch_tool(tool_name, &args, browser);
            let (text, is_error) = match outcome {
                Ok(t) => (t, false),
                Err(e) => (e, true),
            };
            Some(json!({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{ "type": "text", "text": text }],
                    "isError": is_error
                }
            }))
        }
        other => Some(json!({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32601,
                "message": format!("Method not found: {}", other)
            }
        })),
    }
}

fn dispatch_tool(name: &str, args: &Value, browser: &mut BrowserManager) -> Result<String, String> {
    match name {
        "browser_navigate" => {
            let url = args
                .get("url")
                .and_then(|v| v.as_str())
                .ok_or_else(|| "Missing required parameter 'url'".to_string())?;
            let include_snapshot = args
                .get("snapshot")
                .and_then(|v| v.as_bool())
                .unwrap_or(true);
            let max_chars = args
                .get("maxChars")
                .and_then(|v| v.as_u64())
                .map(|v| v.clamp(512, 200_000) as usize)
                .unwrap_or(DEFAULT_MAX_SNAPSHOT_CHARS);
            browser.tool_navigate(url, include_snapshot, max_chars)
        }
        "browser_snapshot" => {
            let max_chars = args
                .get("maxChars")
                .and_then(|v| v.as_u64())
                .map(|v| v.clamp(512, 200_000) as usize)
                .unwrap_or(DEFAULT_MAX_SNAPSHOT_CHARS);
            browser.tool_snapshot(max_chars)
        }
        "browser_tabs" => {
            let action = args
                .get("action")
                .and_then(|v| v.as_str())
                .ok_or_else(|| "Missing required parameter 'action'".to_string())?;
            let index = args
                .get("index")
                .and_then(|v| v.as_u64())
                .map(|v| v as usize);
            let iso = args.get("isolatedContext").and_then(|v| v.as_str());
            browser.tool_tabs(action, index, iso)
        }
        "browser_set_storage_state" => {
            let filename = args
                .get("filename")
                .and_then(|v| v.as_str())
                .ok_or_else(|| "Missing required parameter 'filename'".to_string())?;
            browser.tool_set_storage_state(filename)
        }
        "browser_click" => {
            let ref_id = args.get("ref").and_then(|v| v.as_str());
            let selector = args.get("selector").and_then(|v| v.as_str());
            browser.tool_click(ref_id, selector)
        }
        "browser_type" => {
            let ref_id = args.get("ref").and_then(|v| v.as_str());
            let selector = args.get("selector").and_then(|v| v.as_str());
            let text = args
                .get("text")
                .and_then(|v| v.as_str())
                .ok_or_else(|| "Missing required parameter 'text'".to_string())?;
            let clear = args.get("clear").and_then(|v| v.as_bool()).unwrap_or(true);
            let submit = args
                .get("submit")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            browser.tool_type(ref_id, selector, text, clear, submit)
        }
        "browser_press_key" => {
            let key = args
                .get("key")
                .and_then(|v| v.as_str())
                .ok_or_else(|| "Missing required parameter 'key'".to_string())?;
            browser.tool_press_key(key)
        }
        "browser_evaluate" => {
            let script = args
                .get("function")
                .and_then(|v| v.as_str())
                .or_else(|| args.get("expression").and_then(|v| v.as_str()))
                .ok_or_else(|| {
                    "Missing required parameter 'function' or 'expression'".to_string()
                })?;
            browser.tool_evaluate(script)
        }
        "browser_navigate_back" => browser.tool_navigate_back(),
        "browser_wait_for" => {
            let text = args.get("text").and_then(|v| v.as_str());
            let time = args.get("time").and_then(|v| v.as_f64());
            browser.tool_wait_for(text, time)
        }
        "browser_console_messages" => browser.tool_console_messages(),
        "browser_take_screenshot" => {
            let filename = args.get("filename").and_then(|v| v.as_str());
            browser.tool_take_screenshot(filename)
        }
        "browser_extract_markdown" => {
            let selector = args.get("selector").and_then(|v| v.as_str());
            let max_chars = args
                .get("maxChars")
                .and_then(|v| v.as_u64())
                .map(|v| v.clamp(512, 200_000) as usize)
                .unwrap_or(DEFAULT_MAX_SNAPSHOT_CHARS);
            browser.tool_extract_markdown(selector, max_chars)
        }
        "browser_webmcp_list" => {
            let frame_id = args.get("frameId").and_then(|v| v.as_str());
            let wait_ms = args
                .get("waitMs")
                .and_then(|v| v.as_u64())
                .map(|v| v.min(5_000))
                .unwrap_or(0);
            browser.tool_webmcp_list(frame_id, wait_ms)
        }
        "browser_webmcp_call" => {
            let tool_name = args
                .get("name")
                .and_then(|v| v.as_str())
                .ok_or_else(|| "Missing required parameter 'name'".to_string())?;
            let tool_args = args.get("arguments").cloned().unwrap_or_else(|| json!({}));
            let frame_id = args.get("frameId").and_then(|v| v.as_str());
            let confirm = args
                .get("confirm")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let timeout_ms = args
                .get("timeoutMs")
                .and_then(|v| v.as_u64())
                .map(|v| v.clamp(500, 30_000))
                .unwrap_or(15_000);
            browser.tool_webmcp_call(tool_name, tool_args, frame_id, confirm, timeout_ms)
        }
        "browser_close" => browser.tool_close(),
        other => Err(format!("Unknown tool '{}'", other)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::browser::BrowserConfig;
    use std::path::PathBuf;

    #[test]
    fn handles_initialize_and_tools_list_without_exposing_raw_cookie_readers() {
        let mut mgr = BrowserManager::new(BrowserConfig {
            moli_bin: Some(PathBuf::from("moli")),
            cdp_url: None,
            initial_storage_state: None,
            output_dir: std::env::temp_dir(),
            enable_layout: false,
        });

        let init = handle_jsonrpc_message(
            &json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}),
            &mut mgr,
        )
        .expect("init response");
        assert_eq!(init["result"]["serverInfo"]["name"], "sessfork-mcp");

        // Notifications return None
        assert!(handle_jsonrpc_message(
            &json!({"jsonrpc":"2.0","method":"notifications/initialized"}),
            &mut mgr
        )
        .is_none());

        let list = handle_jsonrpc_message(
            &json!({"jsonrpc":"2.0","id":2,"method":"tools/list"}),
            &mut mgr,
        )
        .expect("tools list");
        let tools = list["result"]["tools"].as_array().expect("tools array");
        let names: Vec<&str> = tools
            .iter()
            .filter_map(|t| t.get("name").and_then(|n| n.as_str()))
            .collect();
        assert!(names.contains(&"browser_navigate"));
        assert!(names.contains(&"browser_set_storage_state"));
        assert!(names.contains(&"browser_tabs"));
        assert!(names.contains(&"browser_extract_markdown"));
        assert!(names.contains(&"browser_webmcp_list"));
        assert!(names.contains(&"browser_webmcp_call"));
        assert_eq!(names.len(), 16);
        // Redaction guarantee: raw credential dump tools must not be registered.
        assert!(!names.contains(&"browser_cookie_list"));
        assert!(!names.contains(&"browser_storage_state"));
        assert!(!names.contains(&"browser_localstorage_get"));

        // Input validation fail-closed checks (must reject before spawning Moli).
        for (req_id, arguments, expected_substr) in [
            (
                3,
                json!({ "name": "add_numbers", "arguments": [1, 2] }),
                "JSON object",
            ),
            (
                4,
                json!({ "name": "add_numbers", "arguments": null }),
                "JSON object",
            ),
            (5, json!({}), "Missing required parameter 'name'"),
            (6, json!({ "name": "   " }), "non-empty WebMCP tool name"),
        ] {
            let resp = handle_jsonrpc_message(
                &json!({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "tools/call",
                    "params": {
                        "name": "browser_webmcp_call",
                        "arguments": arguments
                    }
                }),
                &mut mgr,
            )
            .expect("validation response");
            assert_eq!(resp["result"]["isError"], true);
            assert!(resp["result"]["content"][0]["text"]
                .as_str()
                .unwrap_or("")
                .contains(expected_substr));
        }
    }
}
