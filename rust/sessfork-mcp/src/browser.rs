use crate::ax::{
    compute_snapshot_diff, format_ax_tree_with_options, format_snapshot_diff, AxSnapshot,
    RefRegistry, SnapshotEntry, SnapshotOptions,
};
use crate::cdp::{CdpPipeClient, WebMcpInvokeWait, WebMcpResponseWait};
use crate::markdown::{
    build_markdown_extract_expression, format_markdown_response, truncate_chars,
};
use crate::storage::{
    build_local_storage_init_script, build_single_origin_local_storage_sync_script,
    cookies_to_cdp_params, load_storage_state_file, normalize_origin, validate_allowed_path,
    StorageState,
};
use crate::webmcp::{format_invocation_outcome, format_tool_catalog, select_tool, FrameOriginInfo};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(unix)]
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};

const DEFAULT_CDP_TIMEOUT: Duration = Duration::from_secs(20);
const DEFAULT_LOAD_TIMEOUT: Duration = Duration::from_secs(15);
const SPA_HYDRATION_SETTLE_TIMEOUT: Duration = Duration::from_millis(2500);
const NO_OP_RECHECK_DELAY: Duration = Duration::from_millis(150);
const DEFAULT_MAX_SNAPSHOT_CHARS: usize = 32_000;
const ISOLATED_WORLD_NAME: &str = "__sessfork_ls_isolated";

fn origin_from_url(url: &str) -> String {
    let trimmed = url.trim();
    if trimmed.starts_with("http://") || trimmed.starts_with("https://") {
        normalize_origin(trimmed)
    } else {
        String::new()
    }
}

fn collect_frame_tree_node(
    node: &Value,
    is_top: bool,
    out: &mut HashMap<String, FrameOriginInfo>,
) -> Option<FrameOriginInfo> {
    let frame = node.get("frame")?;
    let fid = frame.get("id")?.as_str()?.trim().to_string();
    if fid.is_empty() {
        return None;
    }
    let url = frame
        .get("url")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let raw_origin = frame
        .get("securityOrigin")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim();
    let origin = if raw_origin == "null" || raw_origin == "://" {
        // Explicit opaque securityOrigin (e.g. sandboxed iframe without allow-same-origin)
        // must stay opaque and NEVER inherit the URL's origin.
        "null".to_string()
    } else if !raw_origin.is_empty() {
        normalize_origin(raw_origin)
    } else {
        origin_from_url(&url)
    };
    let info = FrameOriginInfo {
        frame_id: fid.clone(),
        url,
        origin,
        is_top,
    };
    out.insert(fid, info.clone());
    if let Some(children) = node.get("childFrames").and_then(|v| v.as_array()) {
        for child in children {
            let _ = collect_frame_tree_node(child, false, out);
        }
    }
    Some(info)
}

fn validate_screenshot_filename(filename: Option<&str>) -> Result<String, String> {
    let Some(raw) = filename.map(str::trim).filter(|s| !s.is_empty()) else {
        return Ok("screenshot.png".to_string());
    };
    if raw.contains('/')
        || raw.contains('\\')
        || raw.contains("..")
        || raw.contains('\0')
        || raw.starts_with('.')
    {
        return Err(format!(
            "Invalid screenshot filename '{}': must be a plain non-hidden .png filename inside --output-dir",
            raw
        ));
    }
    if !raw.to_ascii_lowercase().ends_with(".png") || raw.len() <= 4 {
        return Err(format!(
            "Invalid screenshot filename '{}': must end with .png",
            raw
        ));
    }
    Ok(raw.to_string())
}

fn write_private_screenshot_file(
    output_dir: &Path,
    filename: &str,
    bytes: &[u8],
) -> Result<PathBuf, String> {
    if fs::symlink_metadata(output_dir)
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err("Refusing to write screenshot into symlinked --output-dir".to_string());
    }
    #[cfg(unix)]
    {
        if !output_dir.exists() {
            fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(output_dir)
                .map_err(|e| format!("Failed to create output dir: {}", e))?;
        }
        fs::set_permissions(output_dir, fs::Permissions::from_mode(0o700))
            .map_err(|e| format!("Failed to enforce 0700 on output dir: {}", e))?;
    }
    #[cfg(not(unix))]
    {
        fs::create_dir_all(output_dir)
            .map_err(|e| format!("Failed to create output dir: {}", e))?;
    }

    let canon_dir =
        fs::canonicalize(output_dir).map_err(|e| format!("Failed to resolve output dir: {}", e))?;
    let out_path = canon_dir.join(filename);
    if fs::symlink_metadata(&out_path)
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err(format!(
            "Refusing to overwrite symlinked screenshot target '{}'",
            out_path.display()
        ));
    }

    let mut opts = OpenOptions::new();
    opts.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        opts.mode(0o600).custom_flags(libc::O_NOFOLLOW);
    }
    let mut file = opts.open(&out_path).map_err(|e| {
        format!(
            "Failed to open screenshot target '{}': {}",
            out_path.display(),
            e
        )
    })?;
    #[cfg(unix)]
    {
        file.set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|e| {
                format!(
                    "Failed to enforce 0600 on screenshot '{}': {}",
                    out_path.display(),
                    e
                )
            })?;
    }
    file.write_all(bytes)
        .map_err(|e| format!("Failed to save screenshot '{}': {}", out_path.display(), e))?;
    Ok(out_path)
}

struct ContextState {
    browser_context_id: String,
    init_script_source: Option<String>,
    state_rev: u64,
}

struct LastSnapshotState {
    url: String,
    title: String,
    entries: Vec<SnapshotEntry>,
    active_modal: Option<String>,
    opts: SnapshotOptions,
    truncated: bool,
}

struct TabState {
    target_id: String,
    session_id: String,
    context_name: String,
    url: String,
    title: String,
    init_script_id: Option<String>,
    ref_map: HashMap<String, i64>,
    ref_registry: RefRegistry,
    last_snapshot: Option<LastSnapshotState>,
    webmcp_error: Option<String>,
}

pub struct BrowserConfig {
    pub moli_bin: Option<PathBuf>,
    pub cdp_url: Option<String>,
    pub initial_storage_state: Option<PathBuf>,
    pub output_dir: PathBuf,
    pub enable_layout: bool,
    pub default_compact: bool,
    pub default_diff: bool,
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
        let client = if let Some(ref cdp_url) = self.cfg.cdp_url {
            CdpPipeClient::connect_external_cdp(cdp_url)?
        } else {
            let moli_bin = self
                .cfg
                .moli_bin
                .as_deref()
                .ok_or_else(|| "Neither --moli nor --cdp-url is configured".to_string())?;
            CdpPipeClient::spawn_moli(moli_bin, self.cfg.enable_layout)?
        };
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
            .ok_or_else(|| {
                "CDP Target.createBrowserContext returned no browserContextId".to_string()
            })?
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

        for domain in [
            "Page.enable",
            "DOM.enable",
            "Runtime.enable",
            "Accessibility.enable",
        ] {
            let _ = self
                .cdp()?
                .call(domain, json!({}), Some(&session_id), DEFAULT_CDP_TIMEOUT);
        }
        let webmcp_error = self
            .cdp()?
            .call(
                "WebMCP.enable",
                json!({}),
                Some(&session_id),
                DEFAULT_CDP_TIMEOUT,
            )
            .err();

        let mut init_script_id = None;
        if let Some(src) = self
            .contexts
            .get(context_name)
            .and_then(|c| c.init_script_source.clone())
        {
            if let Ok(r) = self.cdp()?.call(
                "Page.addScriptToEvaluateOnNewDocument",
                json!({ "source": src, "worldName": ISOLATED_WORLD_NAME }),
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
            ref_registry: RefRegistry::new(),
            last_snapshot: None,
            webmcp_error,
        });
        let idx = self.tabs.len() - 1;
        self.current_tab = idx;
        Ok(idx)
    }

    fn query_frame_tree(
        &self,
        sid: &str,
    ) -> Result<(FrameOriginInfo, HashMap<String, FrameOriginInfo>), String> {
        let resp = self.cdp()?.call(
            "Page.getFrameTree",
            json!({}),
            Some(sid),
            DEFAULT_CDP_TIMEOUT,
        )?;
        let mut map = HashMap::new();
        let top = resp
            .get("frameTree")
            .and_then(|ft| collect_frame_tree_node(ft, true, &mut map))
            .ok_or_else(|| "CDP Page.getFrameTree returned no top frame".to_string())?;
        Ok((top, map))
    }

    fn eval_in_isolated_world(
        &self,
        sid: &str,
        frame_id: &str,
        expression: &str,
    ) -> Result<Value, String> {
        if !frame_id.is_empty() {
            if let Ok(world) = self.cdp()?.call(
                "Page.createIsolatedWorld",
                json!({
                    "frameId": frame_id,
                    "worldName": ISOLATED_WORLD_NAME,
                }),
                Some(sid),
                DEFAULT_CDP_TIMEOUT,
            ) {
                if let Some(ctx_id) = world.get("executionContextId").and_then(|v| v.as_i64()) {
                    return self.cdp()?.call(
                        "Runtime.evaluate",
                        json!({
                            "expression": expression,
                            "contextId": ctx_id,
                            "returnByValue": true,
                        }),
                        Some(sid),
                        DEFAULT_CDP_TIMEOUT,
                    );
                }
            }
        }
        self.cdp()?.call(
            "Runtime.evaluate",
            json!({
                "expression": expression,
                "returnByValue": true,
            }),
            Some(sid),
            DEFAULT_CDP_TIMEOUT,
        )
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
                json!({ "source": &init_src, "worldName": ISOLATED_WORLD_NAME }),
                Some(&sid),
                DEFAULT_CDP_TIMEOUT,
            ) {
                self.tabs[idx].init_script_id = r
                    .get("identifier")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string());
            }
            // Use browser-authoritative CDP FrameTree origin (never untrusted JS location.href)
            // and send ONLY entries belonging to this tab's exact origin inside an isolated world.
            if let Ok((top_frame, _)) = self.query_frame_tree(&sid) {
                if !top_frame.url.is_empty() {
                    self.tabs[idx].url = top_frame.url.clone();
                }
                if top_frame.url.starts_with("http://") || top_frame.url.starts_with("https://") {
                    let live_src = build_single_origin_local_storage_sync_script(
                        &top_frame.origin,
                        &state.origins,
                        next_rev,
                    );
                    let _ = self.eval_in_isolated_world(&sid, &top_frame.frame_id, &live_src);
                }
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
            let marker = if i == self.current_tab {
                "(current) "
            } else {
                ""
            };
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
        // Obtain authoritative URL from browser-side CDP FrameTree (never trust main-world JS).
        let (top_frame, _) = self.query_frame_tree(&sid)?;
        let url = top_frame.url;
        let ev = self.eval_in_isolated_world(
            &sid,
            &top_frame.frame_id,
            "({title: String(document.title || ''), readyState: String(document.readyState || '')})",
        )?;
        let parsed = ev
            .pointer("/result/value")
            .cloned()
            .unwrap_or_else(|| json!({}));
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

    fn resolve_snapshot_options(
        &self,
        compact: Option<bool>,
        scope_modal: Option<bool>,
    ) -> SnapshotOptions {
        let interactive_only = compact.unwrap_or(self.cfg.default_compact);
        let scope = scope_modal.unwrap_or(interactive_only);
        SnapshotOptions {
            interactive_only,
            scope_modal: scope,
        }
    }

    fn resolve_diff_mode(&self, diff: Option<bool>) -> bool {
        diff.unwrap_or(self.cfg.default_diff)
    }

    fn capture_snapshot(
        &mut self,
        max_chars: usize,
        opts: SnapshotOptions,
        reset_refs: bool,
    ) -> Result<AxSnapshot, String> {
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
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        let mut fallback_reg = RefRegistry::new();
        let snap = if let Some(tab) = self.tabs.get_mut(self.current_tab) {
            if reset_refs {
                tab.ref_registry.reset();
            }
            let s = format_ax_tree_with_options(nodes, max_chars, opts, &mut tab.ref_registry);
            tab.ref_map = s.ref_map.clone();
            s
        } else {
            format_ax_tree_with_options(nodes, max_chars, opts, &mut fallback_reg)
        };
        Ok(snap)
    }

    fn capture_settled_snapshot(
        &mut self,
        max_chars: usize,
        opts: SnapshotOptions,
        settle_spa: bool,
        force_reset_refs: bool,
        reset_if_url_ne: Option<&str>,
    ) -> Result<(String, String, AxSnapshot), String> {
        let (mut url, mut title, mut ready_state) = self.sync_current_page_meta()?;
        let reset_refs = force_reset_refs || reset_if_url_ne.is_some_and(|prev| prev != url);
        let mut snap = self.capture_snapshot(max_chars, opts, reset_refs)?;
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
                if let Ok(next_snap) = self.capture_snapshot(max_chars, opts, false) {
                    let improved = next_snap.content_lines > snap.content_lines
                        || !next_snap.looks_like_loading_shell;
                    snap = next_snap;
                    if improved && !snap.looks_like_loading_shell && ready_state == "complete" {
                        break;
                    }
                }
            }
        }
        Ok((url, title, snap))
    }

    fn format_page_header(url: &str, title: &str, snap: Option<&AxSnapshot>) -> String {
        let mut out = format!("### Page\n- Page URL: {}\n- Page Title: {}\n", url, title);
        if let Some(s) = snap {
            if s.modal_scoped {
                if let Some(ref m) = s.active_modal {
                    out.push_str(&format!(
                        "- Active Modal: \"{}\" (scoped; {} background control(s) suppressed)\n",
                        m, s.obscured_interactive_count
                    ));
                }
            }
        }
        out
    }

    fn record_last_snapshot(
        &mut self,
        url: String,
        title: String,
        entries: Vec<SnapshotEntry>,
        active_modal: Option<String>,
        opts: SnapshotOptions,
        truncated: bool,
    ) {
        if let Some(tab) = self.tabs.get_mut(self.current_tab) {
            tab.last_snapshot = Some(LastSnapshotState {
                url,
                title,
                entries,
                active_modal,
                opts,
                truncated,
            });
        }
    }

    fn format_page_response(
        &mut self,
        include_snapshot: bool,
        max_chars: usize,
        settle_spa: bool,
        opts: SnapshotOptions,
        diff_mode: bool,
        reset_refs: bool,
    ) -> Result<String, String> {
        if !include_snapshot {
            let (url, title, _) = self.sync_current_page_meta()?;
            return Ok(Self::format_page_header(&url, &title, None));
        }

        let prev = if reset_refs {
            None
        } else {
            self.tabs
                .get_mut(self.current_tab)
                .and_then(|t| t.last_snapshot.take())
        };

        let (url, title, snap) =
            self.capture_settled_snapshot(max_chars, opts, settle_spa, reset_refs, None)?;
        let mut out = Self::format_page_header(&url, &title, Some(&snap));

        let can_diff = diff_mode
            && prev.as_ref().is_some_and(|p| {
                p.url == url
                    && p.opts == opts
                    && p.active_modal == snap.active_modal
                    && !p.truncated
                    && !snap.truncated
            });

        if can_diff {
            if let Some(ref p) = prev {
                let diff = compute_snapshot_diff(&p.entries, &snap.entries);
                if diff.is_empty() && p.title == title {
                    out.push_str("\n### Diff\n(no changes since previous snapshot)\n");
                } else if diff.is_empty() {
                    out.push_str(&format!("\n### Diff\nTitle changed: \"{}\"\n", title));
                } else {
                    out.push_str("\n### Diff\n```diff\n");
                    out.push_str(&format_snapshot_diff(&diff, max_chars));
                    out.push_str("```\n");
                }
            }
        } else {
            out.push_str("\n### Snapshot\n```yaml\n");
            out.push_str(&snap.yaml);
            out.push_str("```\n");
        }

        self.record_last_snapshot(
            url,
            title,
            snap.entries,
            snap.active_modal,
            opts,
            snap.truncated,
        );
        Ok(out)
    }

    fn wait_after_interaction(
        &mut self,
        sid: &str,
        before_url: &str,
        prev_seq: u64,
        wait_load_ms: u64,
    ) -> (bool, bool) {
        let mut loaded = if wait_load_ms > 0 {
            self.cdp()
                .map(|c| {
                    c.wait_for_load_advance(sid, prev_seq, Duration::from_millis(wait_load_ms))
                })
                .unwrap_or(false)
        } else {
            false
        };
        let mut should_settle = loaded;
        if let Ok((after_url, _, ready_state)) = self.sync_current_page_meta() {
            if after_url != before_url || ready_state != "complete" {
                should_settle = true;
                if !loaded && ready_state != "complete" {
                    if let Ok(cdp) = self.cdp() {
                        if cdp.wait_for_load_advance(sid, prev_seq, Duration::from_secs(8)) {
                            loaded = true;
                        }
                    }
                }
            }
        }
        (should_settle, loaded)
    }

    #[allow(clippy::too_many_arguments)]
    fn format_action_response(
        &mut self,
        action_label: &str,
        before_url: &str,
        blocked_reason: Option<&str>,
        max_chars: usize,
        should_settle: bool,
        page_reloaded: bool,
        opts: SnapshotOptions,
        diff_mode: bool,
    ) -> Result<String, String> {
        let prev = self
            .tabs
            .get_mut(self.current_tab)
            .and_then(|t| t.last_snapshot.take());

        // Fast path for explicitly blocked actions (disabled / readonly) in diff mode when we
        // already hold a baseline snapshot: skip redundant AXTree fetch AND preserve `prev` so
        // any concurrent background DOM change is never swallowed.
        if let Some(reason) = blocked_reason {
            if diff_mode {
                if let Some(p) = prev {
                    let out = format!(
                        "### Page\n- Page URL: {}\n- Page Title: {}\n\n### Receipt\nWARNING: NO_OP | {} target is {} (0 page changes). Resolve prerequisite validation or enable the control first.\n",
                        p.url, p.title, action_label, reason
                    );
                    if let Some(tab) = self.tabs.get_mut(self.current_tab) {
                        tab.last_snapshot = Some(p);
                    }
                    return Ok(out);
                }
            }
        }

        let (mut url, mut title, mut snap) = self.capture_settled_snapshot(
            max_chars,
            opts,
            should_settle,
            page_reloaded,
            Some(before_url),
        )?;

        let mut prev_url = prev.as_ref().map(|p| p.url.as_str()).unwrap_or(before_url);
        let mut url_changed = url != prev_url;
        let mut title_changed = prev.as_ref().is_some_and(|p| p.title != title);
        let mut modal_changed = prev
            .as_ref()
            .is_some_and(|p| p.active_modal != snap.active_modal);

        let mut comparable = !page_reloaded
            && !url_changed
            && !modal_changed
            && !snap.truncated
            && prev
                .as_ref()
                .is_some_and(|p| p.opts == opts && !p.truncated);
        let mut diff = if comparable {
            prev.as_ref()
                .map(|p| compute_snapshot_diff(&p.entries, &snap.entries))
        } else {
            None
        };

        let mut is_no_op = blocked_reason.is_some()
            || (comparable
                && !title_changed
                && diff.as_ref().is_some_and(crate::ax::SnapshotDiff::is_empty));

        // Catch short async SPA DOM updates (50–150ms) with ONE bounded re-check
        // only when the immediate post-action snapshot looks like a NO_OP.
        if is_no_op && blocked_reason.is_none() && !should_settle {
            thread::sleep(NO_OP_RECHECK_DELAY);
            let (u2, t2, s2) =
                self.capture_settled_snapshot(max_chars, opts, false, false, Some(before_url))?;
            let (u2, t2, s2) = if u2 != before_url && s2.looks_like_loading_shell {
                self.capture_settled_snapshot(max_chars, opts, true, false, None)?
            } else {
                (u2, t2, s2)
            };
            url = u2;
            title = t2;
            snap = s2;
            prev_url = prev.as_ref().map(|p| p.url.as_str()).unwrap_or(before_url);
            url_changed = url != prev_url;
            title_changed = prev.as_ref().is_some_and(|p| p.title != title);
            modal_changed = prev
                .as_ref()
                .is_some_and(|p| p.active_modal != snap.active_modal);
            comparable = !page_reloaded
                && !url_changed
                && !modal_changed
                && !snap.truncated
                && prev
                    .as_ref()
                    .is_some_and(|p| p.opts == opts && !p.truncated);
            diff = if comparable {
                prev.as_ref()
                    .map(|p| compute_snapshot_diff(&p.entries, &snap.entries))
            } else {
                None
            };
            is_no_op = comparable
                && !title_changed
                && diff.as_ref().is_some_and(crate::ax::SnapshotDiff::is_empty);
        }

        // Build structured Action Receipt.
        let receipt = if let Some(reason) = blocked_reason {
            format!(
                "WARNING: NO_OP | {} target is {} (0 page changes). Resolve prerequisite validation or enable the control first.",
                action_label, reason
            )
        } else if is_no_op {
            if opts.interactive_only {
                format!(
                    "WARNING: NO_OP | {} produced 0 URL, title, modal, or interactive-control changes. Element may be disabled, obscured, already active, or awaiting prior form validation.",
                    action_label
                )
            } else {
                format!(
                    "WARNING: NO_OP | {} produced 0 URL, title, modal, or accessibility-tree changes. Element may be disabled, obscured, already active, or awaiting prior form validation.",
                    action_label
                )
            }
        } else {
            let mut parts: Vec<String> = Vec::with_capacity(4);
            if url_changed {
                parts.push(format!("URL changed: {} -> {}", prev_url, url));
            } else if page_reloaded {
                parts.push("Page reloaded".to_string());
            }
            if modal_changed {
                match snap.active_modal.as_deref() {
                    Some(m) if snap.modal_scoped => parts.push(format!(
                        "Modal opened: \"{}\" | Scoped ({} interactive, {} background suppressed)",
                        m,
                        snap.ref_map.len(),
                        snap.obscured_interactive_count
                    )),
                    Some(m) => parts.push(format!("Modal opened: \"{}\"", m)),
                    None => parts.push("Modal closed".to_string()),
                }
            }
            if title_changed && !url_changed {
                parts.push(format!("Title changed: \"{}\"", title));
            }
            if let Some(ref d) = diff {
                if !d.is_empty() {
                    parts.push(format!("+{} -{} elements", d.added.len(), d.removed.len()));
                }
            } else if url_changed || page_reloaded {
                parts.push(format!(
                    "{} elements ({} interactive)",
                    snap.entries.len(),
                    snap.ref_map.len()
                ));
            }
            if parts.is_empty() {
                format!("OK | {}", action_label)
            } else {
                format!("OK | {}", parts.join(" | "))
            }
        };

        let mut out = Self::format_page_header(&url, &title, Some(&snap));
        out.push_str("\n### Receipt\n");
        out.push_str(&receipt);
        out.push('\n');

        if diff_mode && comparable {
            if !is_no_op {
                if let Some(ref d) = diff {
                    if !d.is_empty() {
                        out.push_str("\n### Diff\n```diff\n");
                        out.push_str(&format_snapshot_diff(d, max_chars));
                        out.push_str("```\n");
                    }
                }
            }
        } else {
            out.push_str("\n### Snapshot\n```yaml\n");
            out.push_str(&snap.yaml);
            out.push_str("```\n");
        }

        self.record_last_snapshot(
            url,
            title,
            snap.entries,
            snap.active_modal,
            opts,
            snap.truncated,
        );
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
                let idx = index
                    .ok_or_else(|| "browser_tabs action='select' requires 'index'".to_string())?;
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
                // Clear session buffers AFTER Target.closeTarget finishes so trailing
                // teardown events cannot re-populate a permanently orphaned entry.
                self.cdp()?.clear_session_state(&removed.session_id);
                if self.tabs.is_empty() {
                    let _ = self.open_tab_in_context("default")?;
                } else if idx < self.current_tab {
                    self.current_tab -= 1;
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
        compact: Option<bool>,
        scope_modal: Option<bool>,
    ) -> Result<String, String> {
        let trimmed = url.trim();
        let allowed_scheme = trimmed.starts_with("http://")
            || trimmed.starts_with("https://")
            || trimmed == "about:blank";
        if !allowed_scheme {
            return Err(format!(
                "Blocked URL '{}': only http://, https://, and about:blank are permitted",
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

        if let Some(tab) = self.tabs.get_mut(self.current_tab) {
            tab.ref_map.clear();
            tab.last_snapshot = None;
            tab.ref_registry.reset();
        }
        let opts = self.resolve_snapshot_options(compact, scope_modal);
        self.format_page_response(include_snapshot, max_chars, true, opts, false, true)
    }

    pub fn tool_snapshot(
        &mut self,
        max_chars: usize,
        compact: Option<bool>,
        diff: Option<bool>,
        scope_modal: Option<bool>,
    ) -> Result<String, String> {
        self.ensure_started()?;
        let opts = self.resolve_snapshot_options(compact, scope_modal);
        // Explicit `browser_snapshot` defaults to full view unless `diff=true` is explicitly passed.
        let diff_mode = diff.unwrap_or(false);
        self.format_page_response(true, max_chars, false, opts, diff_mode, false)
    }

    pub fn tool_click(
        &mut self,
        ref_id: Option<&str>,
        selector: Option<&str>,
        compact: Option<bool>,
        diff: Option<bool>,
        scope_modal: Option<bool>,
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

        let click_res = self.cdp()?.call(
            "Runtime.callFunctionOn",
            json!({
                "objectId": obj_id,
                "functionDeclaration": r#"function(){
                    const isDisabled = Boolean(
                        this.disabled
                        || (typeof this.matches === 'function' && this.matches(':disabled'))
                        || (this.getAttribute && this.getAttribute('aria-disabled') === 'true')
                    );
                    if (isDisabled) { return { blocked: 'disabled' }; }
                    if (typeof this.scrollIntoView === 'function'){ try{this.scrollIntoView({block:'center'});}catch(_){} }
                    this.focus && this.focus();
                    this.click();
                    return { blocked: null };
                }"#,
                "returnByValue": true,
            }),
            Some(&sid),
            DEFAULT_CDP_TIMEOUT,
        )?;
        if let Some(exc) = click_res.get("exceptionDetails") {
            return Err(format!("Click JavaScript exception: {}", exc));
        }
        let blocked_reason = click_res
            .pointer("/result/value/blocked")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());

        let (should_settle, page_reloaded) = if blocked_reason.is_some() {
            (false, false)
        } else {
            self.wait_after_interaction(&sid, &before_url, prev_seq, 300)
        };
        let opts = self.resolve_snapshot_options(compact, scope_modal);
        let diff_mode = self.resolve_diff_mode(diff);
        let label = format!("browser_click({})", ref_id.or(selector).unwrap_or("target"));
        self.format_action_response(
            &label,
            &before_url,
            blocked_reason.as_deref(),
            DEFAULT_MAX_SNAPSHOT_CHARS,
            should_settle,
            page_reloaded,
            opts,
            diff_mode,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub fn tool_type(
        &mut self,
        ref_id: Option<&str>,
        selector: Option<&str>,
        text: &str,
        clear: bool,
        submit: bool,
        compact: Option<bool>,
        diff: Option<bool>,
        scope_modal: Option<bool>,
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

        // React/Vue-compatible native value setter + input/change event dispatch.
        let func = r#"function(val, doClear, doSubmit) {
            if (
                this.disabled
                || (typeof this.matches === 'function' && this.matches(':disabled'))
                || (this.getAttribute && this.getAttribute('aria-disabled') === 'true')
            ) {
                return { blocked: 'disabled' };
            }
            if (
                this.readOnly
                || (this.getAttribute && this.getAttribute('aria-readonly') === 'true')
            ) {
                return { blocked: 'readonly' };
            }
            this.focus && this.focus();
            const isInput = this.tagName === 'INPUT';
            const isTextarea = this.tagName === 'TEXTAREA';
            const currentVal = (isInput || isTextarea || 'value' in this)
                ? (this.value || '')
                : (this.textContent || '');
            const nextVal = doClear ? val : (currentVal + val);
            if (isInput || isTextarea) {
                const proto = isTextarea
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                if (desc && typeof desc.set === 'function') {
                    desc.set.call(this, nextVal);
                } else {
                    this.value = nextVal;
                }
            } else if ('value' in this) {
                this.value = nextVal;
            } else {
                this.textContent = nextVal;
            }
            this.dispatchEvent(new Event('input', { bubbles: true }));
            this.dispatchEvent(new Event('change', { bubbles: true }));
            if (doSubmit) {
                const kd = new KeyboardEvent('keydown', {
                    key: 'Enter',
                    code: 'Enter',
                    keyCode: 13,
                    bubbles: true,
                    cancelable: true
                });
                const allowDefault = this.dispatchEvent(kd);
                this.dispatchEvent(new KeyboardEvent('keyup', {
                    key: 'Enter',
                    code: 'Enter',
                    keyCode: 13,
                    bubbles: true
                }));
                if (allowDefault && this.form) {
                    if (typeof this.form.requestSubmit === 'function') {
                        this.form.requestSubmit();
                    } else {
                        this.form.submit();
                    }
                }
            }
            return { blocked: null };
        }"#;

        let type_res = self.cdp()?.call(
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
        if let Some(exc) = type_res.get("exceptionDetails") {
            return Err(format!("Type JavaScript exception: {}", exc));
        }
        let blocked_reason = type_res
            .pointer("/result/value/blocked")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());

        let (should_settle, page_reloaded) = if submit && blocked_reason.is_none() {
            self.wait_after_interaction(&sid, &before_url, prev_seq, 300)
        } else {
            (false, false)
        };
        let opts = self.resolve_snapshot_options(compact, scope_modal);
        let diff_mode = self.resolve_diff_mode(diff);
        let label = format!(
            "browser_type({}{})",
            ref_id.or(selector).unwrap_or("target"),
            if submit { ", submit=true" } else { "" }
        );
        self.format_action_response(
            &label,
            &before_url,
            blocked_reason.as_deref(),
            DEFAULT_MAX_SNAPSHOT_CHARS,
            should_settle,
            page_reloaded,
            opts,
            diff_mode,
        )
    }

    pub fn tool_press_key(
        &mut self,
        key: &str,
        compact: Option<bool>,
        diff: Option<bool>,
        scope_modal: Option<bool>,
    ) -> Result<String, String> {
        self.ensure_started()?;
        let sid = self.current_session_id()?;
        let before_url = self
            .tabs
            .get(self.current_tab)
            .map(|t| t.url.clone())
            .unwrap_or_default();
        let prev_seq = self.cdp()?.current_load_seq(&sid);
        for event_type in ["keyDown", "keyUp"] {
            self.cdp()?.call(
                "Input.dispatchKeyEvent",
                json!({ "type": event_type, "key": key }),
                Some(&sid),
                DEFAULT_CDP_TIMEOUT,
            )?;
        }

        // Preserve legacy zero-snapshot fast response when no ergonomic flags are enabled.
        let ergonomic_active = self.cfg.default_diff
            || self.cfg.default_compact
            || diff.is_some()
            || compact.is_some()
            || scope_modal.is_some();
        if !ergonomic_active {
            let opts = SnapshotOptions::default();
            return self.format_page_response(
                false,
                DEFAULT_MAX_SNAPSHOT_CHARS,
                false,
                opts,
                false,
                false,
            );
        }

        let wait_ms = if matches!(key, "Enter" | "Return") {
            300
        } else {
            0
        };
        let (should_settle, page_reloaded) =
            self.wait_after_interaction(&sid, &before_url, prev_seq, wait_ms);
        let opts = self.resolve_snapshot_options(compact, scope_modal);
        let diff_mode = self.resolve_diff_mode(diff);
        let label = format!("browser_press_key({})", key);
        self.format_action_response(
            &label,
            &before_url,
            None,
            DEFAULT_MAX_SNAPSHOT_CHARS,
            should_settle,
            page_reloaded,
            opts,
            diff_mode,
        )
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
        let rendered = serde_json::to_string_pretty(&val).unwrap_or_else(|_| val.to_string());
        Ok(truncate_chars(&rendered, DEFAULT_MAX_SNAPSHOT_CHARS))
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
        let mut navigated = false;
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
                navigated = true;
            }
        }
        if navigated {
            if let Some(tab) = self.tabs.get_mut(self.current_tab) {
                tab.ref_map.clear();
                tab.last_snapshot = None;
                tab.ref_registry.reset();
            }
        }
        let opts = self.resolve_snapshot_options(None, None);
        self.format_page_response(
            true,
            DEFAULT_MAX_SNAPSHOT_CHARS,
            navigated,
            opts,
            false,
            navigated,
        )
    }

    pub fn tool_wait_for(
        &mut self,
        text: Option<&str>,
        seconds: Option<f64>,
    ) -> Result<String, String> {
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
            Ok(truncate_chars(&logs.join("\n"), DEFAULT_MAX_SNAPSHOT_CHARS))
        }
    }

    pub fn tool_take_screenshot(&mut self, filename: Option<&str>) -> Result<String, String> {
        self.ensure_started()?;
        if !self.cfg.enable_layout && self.cfg.cdp_url.is_none() {
            return Err(
                "Screenshots require layout rendering. Restart sessfork-mcp with --layout (or attach to Chromium via --cdp-url)."
                    .to_string(),
            );
        }
        let safe_name = validate_screenshot_filename(filename)?;
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

        let out_path = write_private_screenshot_file(&self.cfg.output_dir, &safe_name, &bytes)?;
        Ok(format!(
            "Saved screenshot ({} bytes) -> {}",
            bytes.len(),
            out_path.display()
        ))
    }

    pub fn tool_extract_markdown(
        &mut self,
        selector: Option<&str>,
        max_chars: usize,
    ) -> Result<String, String> {
        self.ensure_started()?;
        let sid = self.current_session_id()?;
        let (top_frame, _) = self.query_frame_tree(&sid)?;
        let expr = build_markdown_extract_expression(selector)?;
        let ev = self.eval_in_isolated_world(&sid, &top_frame.frame_id, &expr)?;
        if let Some(exc) = ev.get("exceptionDetails") {
            return Err(format!("Markdown extraction JavaScript exception: {}", exc));
        }
        let val = ev
            .pointer("/result/value")
            .ok_or_else(|| "Markdown extractor returned no object payload".to_string())?;
        format_markdown_response(&top_frame.url, val, max_chars)
    }

    fn ensure_webmcp_supported(&self) -> Result<(), String> {
        if let Some(err) = self
            .tabs
            .get(self.current_tab)
            .and_then(|t| t.webmcp_error.as_deref())
        {
            return Err(format!(
                "Current Moli/browser build does not support the WebMCP CDP domain ({})",
                err
            ));
        }
        Ok(())
    }

    pub fn tool_webmcp_list(
        &mut self,
        frame_id: Option<&str>,
        wait_ms: u64,
    ) -> Result<String, String> {
        self.ensure_started()?;
        self.ensure_webmcp_supported()?;
        let sid = self.current_session_id()?;
        // First wait for any late-registering tools (after a lightweight barrier flush if wait_ms == 0).
        if wait_ms == 0 {
            let _ = self.cdp()?.call(
                "Page.getFrameTree",
                json!({}),
                Some(&sid),
                DEFAULT_CDP_TIMEOUT,
            )?;
        }
        let tools = self
            .cdp()?
            .webmcp_tools(&sid, frame_id, Duration::from_millis(wait_ms));
        // Query authoritative frame tree AFTER waiting so late-loaded child iframes are attributed.
        let (top_frame, frame_map) = self.query_frame_tree(&sid)?;
        let title_ev =
            self.eval_in_isolated_world(&sid, &top_frame.frame_id, "String(document.title || '')");
        let title = title_ev
            .ok()
            .and_then(|ev| {
                ev.pointer("/result/value")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string())
            })
            .unwrap_or_default();
        if let Some(tab) = self.tabs.get_mut(self.current_tab) {
            if !top_frame.url.is_empty() {
                tab.url = top_frame.url.clone();
            }
            tab.title = title.clone();
        }
        Ok(format_tool_catalog(
            &top_frame.url,
            &title,
            &tools,
            &frame_map,
            &top_frame.origin,
        ))
    }

    fn confirm_declarative_webmcp_form(
        &self,
        sid: &str,
        backend_node_id: i64,
    ) -> Result<(), String> {
        let resolved = self.cdp()?.call(
            "DOM.resolveNode",
            json!({ "backendNodeId": backend_node_id }),
            Some(sid),
            DEFAULT_CDP_TIMEOUT,
        )?;
        let object_id = resolved
            .pointer("/object/objectId")
            .and_then(|v| v.as_str())
            .ok_or_else(|| {
                format!(
                    "Failed to resolve declarative WebMCP form node {}",
                    backend_node_id
                )
            })?
            .to_string();

        let submit_result = self.cdp()?.call(
            "Runtime.callFunctionOn",
            json!({
                "objectId": &object_id,
                "userGesture": true,
                "functionDeclaration": r#"function() {
                    if (!this.matches(':tool-form-active')) {
                        throw new Error('The declarative WebMCP form is no longer active.');
                    }
                    const button = Array.from(this.elements || []).find(el => el.matches(':tool-submit-active'))
                        || (typeof this.querySelector === 'function' ? this.querySelector(':tool-submit-active') : null);
                    if (button) {
                        if (button.matches(':disabled')) {
                            throw new Error('The declarative WebMCP submit button is disabled.');
                        }
                        button.click();
                    } else if (typeof this.requestSubmit === 'function') {
                        this.requestSubmit();
                    } else {
                        this.submit();
                    }
                }"#,
                "returnByValue": true,
            }),
            Some(sid),
            DEFAULT_CDP_TIMEOUT,
        );

        let _ = self.cdp()?.call(
            "Runtime.releaseObject",
            json!({ "objectId": &object_id }),
            Some(sid),
            Duration::from_secs(5),
        );

        let res = submit_result?;
        if let Some(exc) = res.get("exceptionDetails") {
            return Err(format!(
                "Declarative WebMCP form confirmation failed: {}",
                exc
            ));
        }
        Ok(())
    }

    fn cancel_webmcp_best_effort(&self, sid: &str, invocation_id: &str) {
        if let Ok(cdp) = self.cdp() {
            if !cdp.is_closed() {
                let _ = cdp.call(
                    "WebMCP.cancelInvocation",
                    json!({ "invocationId": invocation_id }),
                    Some(sid),
                    Duration::from_secs(5),
                );
            }
        }
    }

    pub fn tool_webmcp_call(
        &mut self,
        name: &str,
        arguments: Value,
        frame_id: Option<&str>,
        confirm: bool,
        timeout_ms: u64,
    ) -> Result<String, String> {
        if name.trim().is_empty() {
            return Err("Parameter 'name' must be a non-empty WebMCP tool name".to_string());
        }
        if !arguments.is_object() {
            return Err("Parameter 'arguments' must be a JSON object".to_string());
        }
        self.ensure_started()?;
        self.ensure_webmcp_supported()?;
        let sid = self.current_session_id()?;
        let (top_frame, frame_map) = self.query_frame_tree(&sid)?;
        let tools = self.cdp()?.webmcp_tools(&sid, None, Duration::ZERO);
        let tool = select_tool(&tools, name, frame_id, &frame_map, &top_frame.origin)?.clone();

        if tool.requires_manual_confirmation() && !confirm {
            return Err(format!(
                "WebMCP tool '{}' is a declarative form without autosubmit and requires explicit confirmation. Review the arguments and re-call browser_webmcp_call with confirm=true.",
                tool.name
            ));
        }

        let nav_gen = self.cdp()?.current_webmcp_nav_gen(&sid);
        let inv = self.cdp()?.call(
            "WebMCP.invokeTool",
            json!({
                "frameId": tool.frame_id,
                "toolName": tool.name,
                "input": arguments,
            }),
            Some(&sid),
            DEFAULT_CDP_TIMEOUT,
        )?;
        let invocation_id = inv
            .get("invocationId")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| "CDP WebMCP.invokeTool returned no invocationId".to_string())?
            .to_string();

        let timeout = Duration::from_millis(timeout_ms);
        let deadline = Instant::now() + timeout;

        if tool.requires_manual_confirmation() {
            if let Some(backend_node_id) = tool.backend_node_id {
                let wait_cap = deadline
                    .saturating_duration_since(Instant::now())
                    .min(Duration::from_secs(5));
                match self.cdp()?.wait_for_webmcp_invoked_or_responded(
                    &sid,
                    &invocation_id,
                    nav_gen,
                    wait_cap,
                ) {
                    WebMcpInvokeWait::Responded(resp) => {
                        return format_invocation_outcome(&tool.name, &resp);
                    }
                    WebMcpInvokeWait::Invoked => {
                        if let Err(err) =
                            self.confirm_declarative_webmcp_form(&sid, backend_node_id)
                        {
                            // A response may have arrived concurrently (e.g. page auto-completed
                            // right before submit); deliver it instead of discarding it.
                            if let Some(resp) =
                                self.cdp()?.take_webmcp_response(&sid, &invocation_id)
                            {
                                return format_invocation_outcome(&tool.name, &resp);
                            }
                            self.cancel_webmcp_best_effort(&sid, &invocation_id);
                            return Err(err);
                        }
                    }
                    WebMcpInvokeWait::Navigated => {
                        return Err(format!(
                            "Page navigated away while declarative WebMCP form '{}' was pending",
                            tool.name
                        ));
                    }
                    WebMcpInvokeWait::TimedOut => {
                        self.cancel_webmcp_best_effort(&sid, &invocation_id);
                        return Err(format!(
                            "Timed out waiting for declarative WebMCP form '{}' to activate",
                            tool.name
                        ));
                    }
                    WebMcpInvokeWait::Closed => {
                        return Err(
                            "CDP pipe closed while waiting for WebMCP form activation".to_string()
                        );
                    }
                }
            }
        }

        let remaining = deadline.saturating_duration_since(Instant::now());
        match self
            .cdp()?
            .wait_for_webmcp_response(&sid, &invocation_id, nav_gen, remaining)
        {
            WebMcpResponseWait::Responded(resp) => format_invocation_outcome(&tool.name, &resp),
            WebMcpResponseWait::Navigated => Err(format!(
                "Page navigated away while WebMCP tool '{}' was running",
                tool.name
            )),
            WebMcpResponseWait::TimedOut => {
                self.cancel_webmcp_best_effort(&sid, &invocation_id);
                Err(format!(
                    "WebMCP tool '{}' invocation '{}' timed out after {}ms",
                    tool.name,
                    invocation_id,
                    timeout.as_millis()
                ))
            }
            WebMcpResponseWait::Closed => {
                Err("CDP pipe closed while waiting for WebMCP tool response".to_string())
            }
        }
    }

    pub fn tool_close(&mut self) -> Result<String, String> {
        // When attached to an external browser (--cdp-url), dispose only the isolated
        // BrowserContexts we created so zero tabs/cookies leak inside the host browser.
        if self.cfg.cdp_url.is_some() {
            if let Some(ref cdp) = self.cdp {
                for ctx in self.contexts.values() {
                    let _ = cdp.call(
                        "Target.disposeBrowserContext",
                        json!({ "browserContextId": ctx.browser_context_id }),
                        None,
                        Duration::from_secs(3),
                    );
                }
            }
        }
        self.cdp.take();
        self.contexts.clear();
        self.tabs.clear();
        self.current_tab = 0;
        if self.cfg.cdp_url.is_some() {
            Ok(
                "Detached from external CDP browser cleanly (created contexts disposed)."
                    .to_string(),
            )
        } else {
            Ok("Closed Moli browser session cleanly.".to_string())
        }
    }
}

impl Drop for BrowserManager {
    fn drop(&mut self) {
        if self.cdp.is_some() {
            let _ = self.tool_close();
        }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn screenshot_filename_validation_blocks_traversal_hidden_and_non_png() {
        assert_eq!(
            validate_screenshot_filename(None).expect("default"),
            "screenshot.png"
        );
        assert_eq!(
            validate_screenshot_filename(Some("ok_shot.png")).expect("valid png"),
            "ok_shot.png"
        );
        for bad in [
            "../../escape.png",
            "sub/shot.png",
            ".sessfork-state.json",
            ".hidden.png",
            "state.json",
            ".png",
        ] {
            assert!(
                validate_screenshot_filename(Some(bad)).is_err(),
                "expected rejection for {}",
                bad
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn write_private_screenshot_enforces_0600_and_rejects_symlinks() {
        use std::os::unix::fs::symlink;
        use std::time::{SystemTime, UNIX_EPOCH};

        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let base = std::env::temp_dir().join(format!("sf_shot_test_{}", nanos));
        let out_dir = base.join("out");
        let outside = base.join("victim.png");
        fs::create_dir_all(&out_dir).expect("mkdir");
        fs::write(&outside, b"untouched").expect("victim");

        let link_path = out_dir.join("link.png");
        symlink(&outside, &link_path).expect("symlink");
        assert!(write_private_screenshot_file(&out_dir, "link.png", b"PNG").is_err());
        assert_eq!(fs::read(&outside).expect("read victim"), b"untouched");

        let saved = write_private_screenshot_file(&out_dir, "good.png", b"PNGDATA").expect("write");
        let mode = fs::metadata(&saved).expect("meta").permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);

        let _ = fs::remove_dir_all(&base);
    }

    #[test]
    fn collect_frame_tree_preserves_opaque_sandbox_iframe_origin() {
        let tree = json!({
            "frame": {
                "id": "TOP",
                "url": "https://trusted.example/app",
                "securityOrigin": "https://trusted.example"
            },
            "childFrames": [
                {
                    "frame": {
                        "id": "SANDBOX",
                        "url": "https://trusted.example/widget",
                        "securityOrigin": "null"
                    }
                }
            ]
        });
        let mut map = HashMap::new();
        let top = collect_frame_tree_node(&tree, true, &mut map).expect("top frame");
        assert_eq!(top.origin, "https://trusted.example");
        let sandbox = map.get("SANDBOX").expect("sandbox frame");
        assert_eq!(sandbox.origin, "null");
        assert!(!crate::webmcp::is_same_origin_frame(
            Some(sandbox),
            &top.origin
        ));
    }
}
