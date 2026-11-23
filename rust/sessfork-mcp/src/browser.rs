use crate::ax::format_ax_tree;
use crate::cdp::CdpPipeClient;
use crate::storage::{
    build_live_local_storage_sync_script, build_local_storage_init_script, cookies_to_cdp_params,
    load_storage_state_file, validate_allowed_path, StorageState,
};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};

const DEFAULT_CDP_TIMEOUT: Duration = Duration::from_secs(20);
const DEFAULT_LOAD_TIMEOUT: Duration = Duration::from_secs(15);
const SPA_HYDRATION_SETTLE_TIMEOUT: Duration = Duration::from_millis(2500);
const DEFAULT_MAX_SNAPSHOT_CHARS: usize = 32_000;

struct ContextState {
    browser_context_id: String,
    init_script_source: Option<String>,
    state_rev: u64,
}

struct TabState {
    target_id: String,
    session_id: String,
    context_name: String,
    url: String,
    title: String,
    init_script_id: Option<String>,
    ref_map: HashMap<String, i64>,
}

pub struct BrowserConfig {
    pub moli_bin: PathBuf,
    pub initial_storage_state: Option<PathBuf>,
    pub output_dir: PathBuf,
    pub enable_layout: bool,
}

pub struct BrowserManager {
    cfg: BrowserConfig,
    cdp: Option<CdpPipeClient>,
    contexts: HashMap<String, ContextState>,
    tabs: Vec<TabState>,
    current_tab: usize,
}

impl BrowserManager {
    pub fn new(cfg: BrowserConfig) -> Self {
        Self {
            cfg,
            cdp: None,
            contexts: HashMap::new(),
            tabs: Vec::new(),
            current_tab: 0,
        }
    }

    fn ensure_started(&mut self) -> Result<(), String> {
        if self.cdp.is_some() {
            return Ok(());
        }
        let client = CdpPipeClient::spawn_moli(&self.cfg.moli_bin, self.cfg.enable_layout)?;
        self.cdp = Some(client);

        // Create default isolated context + initial blank tab (index 0).
        let _ = self.open_tab_in_context("default")?;

        // Auto-load initial --storage-state if present and readable.
        if let Some(ref state_path) = self.cfg.initial_storage_state.clone() {
            if state_path.is_file() {
                let st = load_storage_state_file(state_path)?;
                self.apply_storage_state_to_context("default", &st)?;
            }
        }
        Ok(())
    }

    fn cdp(&self) -> Result<&CdpPipeClient, String> {
        self.cdp
            .as_ref()
            .ok_or_else(|| "Browser engine is not started".to_string())
    }

    fn ensure_context(&mut self, context_name: &str) -> Result<String, String> {
        if let Some(ctx) = self.contexts.get(context_name) {
            return Ok(ctx.browser_context_id.clone());
        }
        let res = self.cdp()?.call(
            "Target.createBrowserContext",
            json!({}),
            None,
            DEFAULT_CDP_TIMEOUT,
        )?;
        let ctx_id = res
            .get("browserContextId")
            .and_then(|v| v.as_str())
            .ok_or_else(|| "CDP Target.createBrowserContext returned no browserContextId".to_string())?
            .to_string();
        self.contexts.insert(
            context_name.to_string(),
            ContextState {
                browser_context_id: ctx_id.clone(),
                init_script_source: None,
                state_rev: 0,
            },
        );
        Ok(ctx_id)
    }

    fn open_tab_in_context(&mut self, context_name: &str) -> Result<usize, String> {
        let ctx_id = self.ensure_context(context_name)?;
        let tgt = self.cdp()?.call(
            "Target.createTarget",
            json!({ "url": "about:blank", "browserContextId": ctx_id }),
            None,
            DEFAULT_CDP_TIMEOUT,
        )?;
        let target_id = tgt
            .get("targetId")
            .and_then(|v| v.as_str())
            .ok_or_else(|| "CDP Target.createTarget returned no targetId".to_string())?
            .to_string();

        let att = self.cdp()?.call(
            "Target.attachToTarget",
            json!({ "targetId": target_id, "flatten": true }),
            None,
            DEFAULT_CDP_TIMEOUT,
        )?;
        let session_id = att
            .get("sessionId")
            .and_then(|v| v.as_str())
            .ok_or_else(|| "CDP Target.attachToTarget returned no sessionId".to_string())?
            .to_string();

        for domain in ["Page.enable", "DOM.enable", "Runtime.enable", "Accessibility.enable"] {
            let _ = self
                .cdp()?
                .call(domain, json!({}), Some(&session_id), DEFAULT_CDP_TIMEOUT);
        }

        let mut init_script_id = None;
        if let Some(src) = self
            .contexts
            .get(context_name)
            .and_then(|c| c.init_script_source.clone())
        {
            if let Ok(r) = self.cdp()?.call(
                "Page.addScriptToEvaluateOnNewDocument",
                json!({ "source": src }),
                Some(&session_id),
                DEFAULT_CDP_TIMEOUT,
            ) {
                init_script_id = r
                    .get("identifier")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string());
            }
        }

        self.tabs.push(TabState {
            target_id,
            session_id,
            context_name: context_name.to_string(),
            url: "about:blank".to_string(),
            title: String::new(),
            init_script_id,
            ref_map: HashMap::new(),
        });
        let idx = self.tabs.len() - 1;
        self.current_tab = idx;
        Ok(idx)
    }

    fn apply_storage_state_to_context(
        &mut self,
        context_name: &str,
        state: &StorageState,
    ) -> Result<(usize, usize), String> {
        let ctx_id = self.ensure_context(context_name)?;
        let cdp_cookies = cookies_to_cdp_params(&state.cookies);

        self.cdp()?.call(
            "Storage.clearCookies",
            json!({ "browserContextId": ctx_id }),
            None,
            DEFAULT_CDP_TIMEOUT,
        )?;

        if !cdp_cookies.is_empty() {
            self.cdp()?.call(
                "Storage.setCookies",
                json!({
                    "browserContextId": ctx_id,
                    "cookies": cdp_cookies,
                }),
                None,
                DEFAULT_CDP_TIMEOUT,
            )?;
        }

        let next_rev = if let Some(ctx) = self.contexts.get_mut(context_name) {
            ctx.state_rev += 1;
            ctx.state_rev
        } else {
            1
        };
        let init_src = build_local_storage_init_script(&state.origins, next_rev);
        let live_src = build_live_local_storage_sync_script(&state.origins, next_rev);
        if let Some(ctx) = self.contexts.get_mut(context_name) {
            ctx.init_script_source = Some(init_src.clone());
        }

        let tab_indices: Vec<usize> = self
            .tabs
            .iter()
            .enumerate()
            .filter(|(_, t)| t.context_name == context_name)
            .map(|(i, _)| i)
            .collect();

        for idx in tab_indices {
            let sid = self.tabs[idx].session_id.clone();
            let old_id = self.tabs[idx].init_script_id.take();
            if let Some(old) = old_id {
                let _ = self.cdp()?.call(
                    "Page.removeScriptToEvaluateOnNewDocument",
                    json!({ "identifier": old }),
                    Some(&sid),
                    DEFAULT_CDP_TIMEOUT,
                );
            }
            if let Ok(r) = self.cdp()?.call(
                "Page.addScriptToEvaluateOnNewDocument",
                json!({ "source": &init_src }),
                Some(&sid),
                DEFAULT_CDP_TIMEOUT,
            ) {
                self.tabs[idx].init_script_id = r
                    .get("identifier")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string());
            }
            let cur_url = &self.tabs[idx].url;
            if cur_url.starts_with("http://") || cur_url.starts_with("https://") {
                let _ = self.cdp()?.call(
                    "Runtime.evaluate",
                    json!({ "expression": &live_src, "returnByValue": true }),
                    Some(&sid),
                    DEFAULT_CDP_TIMEOUT,
                );
            }
        }

        Ok((cdp_cookies.len(), state.origins.len()))
    }

    fn current_session_id(&self) -> Result<String, String> {
        self.tabs
            .get(self.current_tab)
            .map(|t| t.session_id.clone())
            .ok_or_else(|| "No active tab open".to_string())
    }

    fn format_open_tabs(&self) -> String {
        let mut out = String::from("### Open tabs\n");
        for (i, t) in self.tabs.iter().enumerate() {
            let marker = if i == self.current_tab { "(current) " } else { "" };
            let title = if t.title.is_empty() {
                "about:blank"
            } else {
                &t.title
            };
            out.push_str(&format!("- {}: {}[{}]({})\n", i, marker, title, t.url));
        }
        out
    }

    fn sync_current_page_meta(&mut self) -> Result<(String, String, String), String> {
        let sid = self.current_session_id()?;
        let ev = self.cdp()?.call(
            "Runtime.evaluate",
            json!({
                "expression": "JSON.stringify({url: String(location.href || ''), title: String(document.title || ''), readyState: String(document.readyState || '')})",
                "returnByValue": true,
            }),
            Some(&sid),
            DEFAULT_CDP_TIMEOUT,
        )?;
        let raw = ev
            .pointer("/result/value")
            .and_then(|v| v.as_str())
            .unwrap_or("{}");
        let parsed: Value = serde_json::from_str(raw).unwrap_or_else(|_| json!({}));
        let url = parsed
            .get("url")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let title = parsed
            .get("title")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let ready_state = parsed
            .get("readyState")
            .and_then(|v| v.as_str())
            .unwrap_or("complete")
            .to_string();
        if let Some(tab) = self.tabs.get_mut(self.current_tab) {
            if !url.is_empty() {
                tab.url = url.clone();
            }
            tab.title = title.clone();
        }
        Ok((url, title, ready_state))
    }

    fn capture_snapshot(&mut self, max_chars: usize) -> Result<crate::ax::AxSnapshot, String> {
        let sid = self.current_session_id()?;
        let ax = self.cdp()?.call(
            "Accessibility.getFullAXTree",
            json!({}),
            Some(&sid),
            DEFAULT_CDP_TIMEOUT,
        )?;
        let nodes = ax
            .get("nodes")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        let snap = format_ax_tree(&nodes, max_chars);
        if let Some(tab) = self.tabs.get_mut(self.current_tab) {
            tab.ref_map = snap.ref_map.clone();
        }
        Ok(snap)
    }

    fn format_page_response(
        &mut self,
        include_snapshot: bool,
        max_chars: usize,
        settle_spa: bool,
    ) -> Result<String, String> {
        let (mut url, mut title, mut ready_state) = self.sync_current_page_meta()?;
        if !include_snapshot {
            return Ok(format!("### Page\n- Page URL: {}\n- Page Title: {}\n", url, title));
        }

        let mut snap = self.capture_snapshot(max_chars)?;
        let is_http = url.starts_with("http://") || url.starts_with("https://");
        if settle_spa && is_http && (snap.looks_like_loading_shell || ready_state != "complete") {
            let deadline = Instant::now() + SPA_HYDRATION_SETTLE_TIMEOUT;
            while Instant::now() < deadline
                && (snap.looks_like_loading_shell || ready_state != "complete")
            {
                thread::sleep(Duration::from_millis(150));
                if let Ok((u, t, rs)) = self.sync_current_page_meta() {
                    url = u;
                    title = t;
                    ready_state = rs;
                }
                if let Ok(next_snap) = self.capture_snapshot(max_chars) {
                    let improved = next_snap.content_lines > snap.content_lines
                        || !next_snap.looks_like_loading_shell;
                    snap = next_snap;
                    if improved && !snap.looks_like_loading_shell && ready_state == "complete" {
                        break;
                    }
                }
            }
        }

        let mut out = format!("### Page\n- Page URL: {}\n- Page Title: {}\n", url, title);
        out.push_str("\n### Snapshot\n```yaml\n");
        out.push_str(&snap.yaml);
        out.push_str("```\n");
        Ok(out)
    }

    fn resolve_target_object_id(
        &self,
        sid: &str,
        ref_id: Option<&str>,
        selector: Option<&str>,
    ) -> Result<String, String> {
        if let Some(r) = ref_id.filter(|s| !s.is_empty()) {
            let bid = self
                .tabs
                .get(self.current_tab)
                .and_then(|t| t.ref_map.get(r).copied())
                .ok_or_else(|| {
                    format!(
                        "Element ref '{}' not found in current snapshot (run browser_snapshot first)",
                        r
                    )
                })?;
            let resolved = self.cdp()?.call(
                "DOM.resolveNode",
                json!({ "backendNodeId": bid }),
                Some(sid),
                DEFAULT_CDP_TIMEOUT,
            )?;
            return resolved
                .pointer("/object/objectId")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string())
                .ok_or_else(|| format!("Failed to resolve DOM node for ref '{}'", r));
        }

        if let Some(sel) = selector.filter(|s| !s.is_empty()) {
            let expr = format!(
                "document.querySelector({})",
                serde_json::to_string(sel).unwrap_or_else(|_| "\"\"".to_string())
            );
            let ev = self.cdp()?.call(
                "Runtime.evaluate",
                json!({ "expression": expr, "returnByValue": false }),
                Some(sid),
                DEFAULT_CDP_TIMEOUT,
            )?;
            return ev
                .pointer("/result/objectId")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string())
                .ok_or_else(|| format!("No element matched CSS selector '{}'", sel));
        }

        Err("Either 'ref' or 'selector' is required".to_string())
    }

    pub fn tool_tabs(
        &mut self,
        action: &str,
        index: Option<usize>,
        isolated_context: Option<&str>,
    ) -> Result<String, String> {
        self.ensure_started()?;
        match action {
            "list" => Ok(self.format_open_tabs()),
            "new" => {
                let ctx_name = isolated_context
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .unwrap_or("default");
                let _ = self.open_tab_in_context(ctx_name)?;
                Ok(self.format_open_tabs())
            }
            "select" => {
                let idx = index.ok_or_else(|| "browser_tabs action='select' requires 'index'".to_string())?;
                if idx >= self.tabs.len() {
                    return Err(format!(
                        "Tab index {} out of range (open tabs: {})",
                        idx,
                        self.tabs.len()
                    ));
                }
                self.current_tab = idx;
                Ok(self.format_open_tabs())
            }
            "close" => {
                let idx = index.unwrap_or(self.current_tab);
                if idx >= self.tabs.len() {
                    return Err(format!("Tab index {} out of range", idx));
                }
                let removed = self.tabs.remove(idx);
                let _ = self.cdp()?.call(
                    "Target.closeTarget",
                    json!({ "targetId": removed.target_id }),
                    None,
                    DEFAULT_CDP_TIMEOUT,
                );
                if self.tabs.is_empty() {
                    let _ = self.open_tab_in_context("default")?;
                } else if self.current_tab >= self.tabs.len() {
                    self.current_tab = self.tabs.len() - 1;
                }
                Ok(self.format_open_tabs())
            }
            other => Err(format!(
                "Unsupported browser_tabs action '{}'; expected list, new, select, or close",
                other
            )),
        }
    }

    pub fn tool_set_storage_state(&mut self, filename: &str) -> Result<String, String> {
        self.ensure_started()?;
        let allowed_dirs = vec![self.cfg.output_dir.clone()];
        let allowed_files: Vec<PathBuf> = self.cfg.initial_storage_state.iter().cloned().collect();
        let validated = validate_allowed_path(Path::new(filename), &allowed_dirs, &allowed_files)?;
        let st = load_storage_state_file(&validated)?;
        let ctx_name = self
            .tabs
            .get(self.current_tab)
            .map(|t| t.context_name.clone())
            .unwrap_or_else(|| "default".to_string());
        let (n_cookies, n_origins) = self.apply_storage_state_to_context(&ctx_name, &st)?;
        Ok(format!(
            "Loaded storage state into context '{}': {} cookie(s), {} origin(s).",
            ctx_name, n_cookies, n_origins
        ))
    }

    pub fn tool_navigate(
        &mut self,
        url: &str,
        include_snapshot: bool,
        max_chars: usize,
    ) -> Result<String, String> {
        let trimmed = url.trim();
        let allowed_scheme = trimmed.starts_with("http://")
            || trimmed.starts_with("https://")
            || trimmed.starts_with("about:")
            || trimmed.starts_with("data:");
        if !allowed_scheme {
            return Err(format!(
                "Blocked URL '{}': only http://, https://, about:, and data: schemes are permitted",
                trimmed
            ));
        }
        self.ensure_started()?;
        let sid = self.current_session_id()?;
        let prev_seq = self.cdp()?.current_load_seq(&sid);

        let nav_res = self.cdp()?.call(
            "Page.navigate",
            json!({ "url": trimmed }),
            Some(&sid),
            DEFAULT_CDP_TIMEOUT,
        )?;
        if let Some(err_text) = nav_res.get("errorText").and_then(|v| v.as_str()) {
            if !err_text.is_empty() {
                return Err(format!("Navigation failed: {}", err_text));
            }
        }
        let _ = self
            .cdp()?
            .wait_for_load_advance(&sid, prev_seq, DEFAULT_LOAD_TIMEOUT);

        self.format_page_response(include_snapshot, max_chars, true)
    }

    pub fn tool_snapshot(&mut self, max_chars: usize) -> Result<String, String> {
        self.ensure_started()?;
        self.format_page_response(true, max_chars, false)
    }

    pub fn tool_click(
        &mut self,
        ref_id: Option<&str>,
        selector: Option<&str>,
    ) -> Result<String, String> {
        self.ensure_started()?;
        let sid = self.current_session_id()?;
        let before_url = self
            .tabs
            .get(self.current_tab)
            .map(|t| t.url.clone())
            .unwrap_or_default();
        let obj_id = self.resolve_target_object_id(&sid, ref_id, selector)?;
        let prev_seq = self.cdp()?.current_load_seq(&sid);

        self.cdp()?.call(
            "Runtime.callFunctionOn",
            json!({
                "objectId": obj_id,
                "functionDeclaration": "function(){ if(typeof this.scrollIntoView === 'function'){ try{this.scrollIntoView({block:'center'});}catch(_){} } this.focus && this.focus(); this.click(); }",
                "returnByValue": true,
            }),
            Some(&sid),
            DEFAULT_CDP_TIMEOUT,
        )?;
        // Short window to detect click-triggered navigations; if URL changes or readyState drops
        // from complete (e.g. GitHub Turbo / SPA route transition), wait for full load + settle.
        let loaded = self
            .cdp()?
            .wait_for_load_advance(&sid, prev_seq, Duration::from_millis(300));
        let mut should_settle = loaded;
        if let Ok((after_url, _, ready_state)) = self.sync_current_page_meta() {
            if after_url != before_url || ready_state != "complete" {
                should_settle = true;
                if !loaded && ready_state != "complete" {
                    let _ = self
                        .cdp()?
                        .wait_for_load_advance(&sid, prev_seq, Duration::from_secs(8));
                }
            }
        }
        self.format_page_response(true, DEFAULT_MAX_SNAPSHOT_CHARS, should_settle)
    }

    pub fn tool_type(
        &mut self,
        ref_id: Option<&str>,
        selector: Option<&str>,
        text: &str,
        clear: bool,
        submit: bool,
    ) -> Result<String, String> {
        self.ensure_started()?;
        let sid = self.current_session_id()?;
        let obj_id = self.resolve_target_object_id(&sid, ref_id, selector)?;

        // React/Vue-compatible native value setter + input/change event dispatch.
        let func = r#"function(val, doClear, doSubmit) {
            this.focus && this.focus();
            const proto = this.tagName === 'TEXTAREA'
                ? HTMLTextAreaElement.prototype
                : HTMLInputElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
            const nextVal = doClear ? val : ((this.value || '') + val);
            if (desc && typeof desc.set === 'function') {
                desc.set.call(this, nextVal);
            } else if ('value' in this) {
                this.value = nextVal;
            } else {
                this.textContent = nextVal;
            }
            this.dispatchEvent(new Event('input', { bubbles: true }));
            this.dispatchEvent(new Event('change', { bubbles: true }));
            if (doSubmit) {
                this.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
                this.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }));
                if (this.form && typeof this.form.requestSubmit === 'function') {
                    this.form.requestSubmit();
                } else if (this.form) {
                    this.form.submit();
                }
            }
        }"#;

        self.cdp()?.call(
            "Runtime.callFunctionOn",
            json!({
                "objectId": obj_id,
                "functionDeclaration": func,
                "arguments": [
                    { "value": text },
                    { "value": clear },
                    { "value": submit },
                ],
                "returnByValue": true,
            }),
            Some(&sid),
            DEFAULT_CDP_TIMEOUT,
        )?;
        self.format_page_response(true, DEFAULT_MAX_SNAPSHOT_CHARS, false)
    }

    pub fn tool_press_key(&mut self, key: &str) -> Result<String, String> {
        self.ensure_started()?;
        let sid = self.current_session_id()?;
        for event_type in ["keyDown", "keyUp"] {
            let _ = self.cdp()?.call(
                "Input.dispatchKeyEvent",
                json!({ "type": event_type, "key": key }),
                Some(&sid),
                DEFAULT_CDP_TIMEOUT,
            );
        }
        self.format_page_response(false, DEFAULT_MAX_SNAPSHOT_CHARS, false)
    }

    pub fn tool_evaluate(&mut self, script: &str) -> Result<String, String> {
        self.ensure_started()?;
        let sid = self.current_session_id()?;
        let trimmed = script.trim();
        let expr = format!(
            "(() => {{ const __r = ({}); return typeof __r === 'function' ? __r() : __r; }})()",
            trimmed
        );
        let ev = self.cdp()?.call(
            "Runtime.evaluate",
            json!({
                "expression": expr,
                "awaitPromise": true,
                "returnByValue": true,
            }),
            Some(&sid),
            DEFAULT_CDP_TIMEOUT,
        )?;
        if let Some(exc) = ev.get("exceptionDetails") {
            return Err(format!("JavaScript exception: {}", exc));
        }
        let val = ev
            .pointer("/result/value")
            .cloned()
            .unwrap_or_else(|| ev.get("result").cloned().unwrap_or(Value::Null));
        Ok(serde_json::to_string_pretty(&val).unwrap_or_else(|_| val.to_string()))
    }

    pub fn tool_navigate_back(&mut self) -> Result<String, String> {
        self.ensure_started()?;
        let sid = self.current_session_id()?;
        let hist = self.cdp()?.call(
            "Page.getNavigationHistory",
            json!({}),
            Some(&sid),
            DEFAULT_CDP_TIMEOUT,
        )?;
        let cur = hist
            .get("currentIndex")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        if cur > 0 {
            if let Some(entry_id) = hist
                .pointer(&format!("/entries/{}/id", cur - 1))
                .and_then(|v| v.as_i64())
            {
                let prev_seq = self.cdp()?.current_load_seq(&sid);
                let _ = self.cdp()?.call(
                    "Page.navigateToHistoryEntry",
                    json!({ "entryId": entry_id }),
                    Some(&sid),
                    DEFAULT_CDP_TIMEOUT,
                );
                let _ = self
                    .cdp()?
                    .wait_for_load_advance(&sid, prev_seq, DEFAULT_LOAD_TIMEOUT);
            }
        }
        self.format_page_response(true, DEFAULT_MAX_SNAPSHOT_CHARS, true)
    }

    pub fn tool_wait_for(&mut self, text: Option<&str>, seconds: Option<f64>) -> Result<String, String> {
        self.ensure_started()?;
        if let Some(sec) = seconds {
            let bounded = sec.clamp(0.0, 30.0);
            thread::sleep(Duration::from_secs_f64(bounded));
            return Ok(format!("Waited {:.2}s.", bounded));
        }
        if let Some(needle) = text.filter(|s| !s.is_empty()) {
            let sid = self.current_session_id()?;
            let deadline = Instant::now() + Duration::from_secs(15);
            let expr = format!(
                "(document.body && document.body.innerText || '').includes({})",
                serde_json::to_string(needle).unwrap_or_else(|_| "\"\"".to_string())
            );
            while Instant::now() < deadline {
                if let Ok(ev) = self.cdp()?.call(
                    "Runtime.evaluate",
                    json!({ "expression": expr, "returnByValue": true }),
                    Some(&sid),
                    Duration::from_secs(5),
                ) {
                    if ev.pointer("/result/value").and_then(|v| v.as_bool()) == Some(true) {
                        return Ok(format!("Found text '{}'.", needle));
                    }
                }
                thread::sleep(Duration::from_millis(100));
            }
            return Err(format!("Timed out waiting for text '{}'", needle));
        }
        Err("browser_wait_for requires either 'text' or 'time'".to_string())
    }

    pub fn tool_console_messages(&mut self) -> Result<String, String> {
        self.ensure_started()?;
        let sid = self.current_session_id()?;
        let logs = self.cdp()?.console_logs(&sid);
        if logs.is_empty() {
            Ok("(no console messages)".to_string())
        } else {
            Ok(logs.join("\n"))
        }
    }

    pub fn tool_take_screenshot(&mut self, filename: Option<&str>) -> Result<String, String> {
        self.ensure_started()?;
        if !self.cfg.enable_layout {
            return Err(
                "Screenshots require layout rendering. Restart sessfork-mcp with --layout (or fall back to sessfork-browser Chrome)."
                    .to_string(),
            );
        }
        let sid = self.current_session_id()?;
        let res = self.cdp()?.call(
            "Page.captureScreenshot",
            json!({ "format": "png" }),
            Some(&sid),
            DEFAULT_CDP_TIMEOUT,
        )?;
        let b64 = res
            .get("data")
            .and_then(|v| v.as_str())
            .ok_or_else(|| "Page.captureScreenshot returned no image data".to_string())?;
        let bytes = decode_base64(b64)?;

        fs::create_dir_all(&self.cfg.output_dir)
            .map_err(|e| format!("Failed to create output dir: {}", e))?;
        let safe_name = filename
            .and_then(|f| Path::new(f).file_name())
            .and_then(|s| s.to_str())
            .filter(|s| !s.is_empty() && !s.contains(".."))
            .unwrap_or("screenshot.png");
        let out_path = self.cfg.output_dir.join(safe_name);
        fs::write(&out_path, &bytes)
            .map_err(|e| format!("Failed to save screenshot '{}': {}", out_path.display(), e))?;
        Ok(format!(
            "Saved screenshot ({} bytes) -> {}",
            bytes.len(),
            out_path.display()
        ))
    }

    pub fn tool_close(&mut self) -> Result<String, String> {
        self.cdp.take();
        self.contexts.clear();
        self.tabs.clear();
        self.current_tab = 0;
        Ok("Closed Moli browser session cleanly.".to_string())
    }
}

fn decode_base64(input: &str) -> Result<Vec<u8>, String> {
    let mut out = Vec::with_capacity((input.len() * 3) / 4);
    let mut buf: u32 = 0;
    let mut bits: u8 = 0;
    for b in input.bytes() {
        let val = match b {
            b'A'..=b'Z' => b - b'A',
            b'a'..=b'z' => b - b'a' + 26,
            b'0'..=b'9' => b - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            b'=' | b'\n' | b'\r' | b' ' | b'\t' => continue,
            _ => return Err("Invalid base64 character in screenshot payload".to_string()),
        };
        buf = (buf << 6) | u32::from(val);
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push(((buf >> bits) & 0xFF) as u8);
        }
    }
    Ok(out)
}
