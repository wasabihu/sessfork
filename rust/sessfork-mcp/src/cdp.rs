use crate::webmcp::WebMcpTool;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet, VecDeque};
use std::fs::File;
use std::io::{Read, Write};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

#[cfg(unix)]
use std::os::unix::io::{FromRawFd, RawFd};
#[cfg(unix)]
use std::os::unix::process::CommandExt;

const MAX_CONSOLE_ENTRIES_PER_SESSION: usize = 200;
const MAX_WEBMCP_INVOKED_PER_SESSION: usize = 64;
const MAX_WEBMCP_RESPONSES_PER_SESSION: usize = 64;

#[derive(Default)]
struct WebMcpSessionState {
    tools: HashMap<(String, String), WebMcpTool>,
    invoked: HashSet<String>,
    invoked_order: VecDeque<String>,
    responses: HashMap<String, Value>,
    response_order: VecDeque<String>,
    nav_gen: u64,
}

#[derive(Debug, PartialEq)]
pub enum WebMcpInvokeWait {
    Responded(Value),
    Invoked,
    Navigated,
    TimedOut,
    Closed,
}

#[derive(Debug, PartialEq)]
pub enum WebMcpResponseWait {
    Responded(Value),
    Navigated,
    TimedOut,
    Closed,
}

struct SharedState {
    pending: Mutex<HashMap<u64, mpsc::Sender<Value>>>,
    load_seq: Mutex<HashMap<String, u64>>,
    load_cv: Condvar,
    console_logs: Mutex<HashMap<String, VecDeque<String>>>,
    webmcp: Mutex<HashMap<String, WebMcpSessionState>>,
    webmcp_cv: Condvar,
    closed: AtomicBool,
}

impl SharedState {
    fn new() -> Self {
        Self {
            pending: Mutex::new(HashMap::new()),
            load_seq: Mutex::new(HashMap::new()),
            load_cv: Condvar::new(),
            console_logs: Mutex::new(HashMap::new()),
            webmcp: Mutex::new(HashMap::new()),
            webmcp_cv: Condvar::new(),
            closed: AtomicBool::new(false),
        }
    }

    fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    fn mark_closed(&self) {
        self.closed.store(true, Ordering::Release);
        if let Ok(mut pending) = self.pending.lock() {
            pending.clear();
        }
        // Acquire each condvar's mutex briefly before notifying so a waiter
        // cannot observe closed==false and then miss the wakeup before wait_timeout.
        if let Ok(_guard) = self.load_seq.lock() {
            self.load_cv.notify_all();
        }
        if let Ok(_guard) = self.webmcp.lock() {
            self.webmcp_cv.notify_all();
        }
    }

    fn bump_load(&self, session_id: &str) {
        if let Ok(mut map) = self.load_seq.lock() {
            let entry = map.entry(session_id.to_string()).or_insert(0);
            *entry += 1;
            self.load_cv.notify_all();
        }
    }

    fn current_load_seq(&self, session_id: &str) -> u64 {
        self.load_seq
            .lock()
            .ok()
            .and_then(|m| m.get(session_id).copied())
            .unwrap_or(0)
    }

    fn wait_for_load_advance(&self, session_id: &str, after_seq: u64, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        let Ok(mut map) = self.load_seq.lock() else {
            return false;
        };
        loop {
            if map.get(session_id).copied().unwrap_or(0) > after_seq {
                return true;
            }
            if self.is_closed() {
                return false;
            }
            let now = Instant::now();
            if now >= deadline {
                return false;
            }
            let wait_for = deadline.saturating_duration_since(now);
            match self.load_cv.wait_timeout(map, wait_for) {
                Ok((guard, _)) => map = guard,
                Err(_) => return false,
            }
        }
    }

    fn push_console(&self, session_id: &str, line: String) {
        if let Ok(mut map) = self.console_logs.lock() {
            let q = map.entry(session_id.to_string()).or_default();
            if q.len() >= MAX_CONSOLE_ENTRIES_PER_SESSION {
                q.pop_front();
            }
            q.push_back(line);
        }
    }

    fn get_console(&self, session_id: &str) -> Vec<String> {
        self.console_logs
            .lock()
            .ok()
            .and_then(|m| m.get(session_id).map(|q| q.iter().cloned().collect()))
            .unwrap_or_default()
    }

    fn webmcp_add_tools(&self, session_id: &str, tools: Vec<WebMcpTool>) {
        if tools.is_empty() {
            return;
        }
        if let Ok(mut map) = self.webmcp.lock() {
            let st = map.entry(session_id.to_string()).or_default();
            for tool in tools {
                st.tools
                    .insert((tool.frame_id.clone(), tool.name.clone()), tool);
            }
            self.webmcp_cv.notify_all();
        }
    }

    fn webmcp_remove_tools(&self, session_id: &str, keys: Vec<(String, String)>) {
        if keys.is_empty() {
            return;
        }
        if let Ok(mut map) = self.webmcp.lock() {
            if let Some(st) = map.get_mut(session_id) {
                for key in keys {
                    st.tools.remove(&key);
                }
            }
            self.webmcp_cv.notify_all();
        }
    }

    fn webmcp_clear_frame(&self, session_id: &str, frame_id: &str, is_top_level: bool) {
        if let Ok(mut map) = self.webmcp.lock() {
            let st = map.entry(session_id.to_string()).or_default();
            if is_top_level {
                st.tools.clear();
                st.invoked.clear();
                st.invoked_order.clear();
                st.responses.clear();
                st.response_order.clear();
                st.nav_gen = st.nav_gen.wrapping_add(1);
            } else {
                st.tools.retain(|(fid, _), _| fid != frame_id);
            }
            self.webmcp_cv.notify_all();
        }
    }

    fn current_webmcp_nav_gen(&self, session_id: &str) -> u64 {
        self.webmcp
            .lock()
            .ok()
            .and_then(|m| m.get(session_id).map(|st| st.nav_gen))
            .unwrap_or(0)
    }

    fn webmcp_mark_invoked(&self, session_id: &str, invocation_id: String) {
        if let Ok(mut map) = self.webmcp.lock() {
            let st = map.entry(session_id.to_string()).or_default();
            if st.invoked.insert(invocation_id.clone()) {
                if st.invoked_order.len() >= MAX_WEBMCP_INVOKED_PER_SESSION {
                    if let Some(oldest) = st.invoked_order.pop_front() {
                        st.invoked.remove(&oldest);
                    }
                }
                st.invoked_order.push_back(invocation_id);
            }
            self.webmcp_cv.notify_all();
        }
    }

    fn webmcp_record_response(&self, session_id: &str, invocation_id: String, params: Value) {
        if let Ok(mut map) = self.webmcp.lock() {
            let st = map.entry(session_id.to_string()).or_default();
            if st.invoked.remove(&invocation_id) {
                st.invoked_order.retain(|id| id != &invocation_id);
            }
            if !st.responses.contains_key(&invocation_id) {
                if st.response_order.len() >= MAX_WEBMCP_RESPONSES_PER_SESSION {
                    if let Some(oldest) = st.response_order.pop_front() {
                        st.responses.remove(&oldest);
                    }
                }
                st.response_order.push_back(invocation_id.clone());
            }
            st.responses.insert(invocation_id, params);
            self.webmcp_cv.notify_all();
        }
    }

    fn take_webmcp_response(&self, session_id: &str, invocation_id: &str) -> Option<Value> {
        let mut map = self.webmcp.lock().ok()?;
        let st = map.get_mut(session_id)?;
        let resp = st.responses.remove(invocation_id)?;
        st.response_order.retain(|id| id != invocation_id);
        if st.invoked.remove(invocation_id) {
            st.invoked_order.retain(|id| id != invocation_id);
        }
        Some(resp)
    }

    fn snapshot_webmcp_tools(
        map: &HashMap<String, WebMcpSessionState>,
        session_id: &str,
        frame_filter: Option<&str>,
    ) -> Vec<WebMcpTool> {
        let filter = frame_filter.map(str::trim).filter(|s| !s.is_empty());
        let mut out: Vec<WebMcpTool> = map
            .get(session_id)
            .map(|st| {
                st.tools
                    .values()
                    .filter(|t| filter.is_none_or(|fid| t.frame_id == fid))
                    .cloned()
                    .collect()
            })
            .unwrap_or_default();
        out.sort_by(|a, b| (&a.frame_id, &a.name).cmp(&(&b.frame_id, &b.name)));
        out
    }

    fn wait_for_webmcp_tools(
        &self,
        session_id: &str,
        frame_filter: Option<&str>,
        timeout: Duration,
    ) -> Vec<WebMcpTool> {
        let deadline = Instant::now() + timeout;
        let Ok(mut map) = self.webmcp.lock() else {
            return Vec::new();
        };
        loop {
            let found = Self::snapshot_webmcp_tools(&map, session_id, frame_filter);
            if !found.is_empty() || timeout.is_zero() {
                return found;
            }
            if self.is_closed() {
                return Vec::new();
            }
            let now = Instant::now();
            if now >= deadline {
                return Vec::new();
            }
            let wait_for = deadline.saturating_duration_since(now);
            match self.webmcp_cv.wait_timeout(map, wait_for) {
                Ok((guard, _)) => map = guard,
                Err(_) => return Vec::new(),
            }
        }
    }

    fn wait_for_webmcp_invoked_or_responded(
        &self,
        session_id: &str,
        invocation_id: &str,
        expected_nav_gen: u64,
        timeout: Duration,
    ) -> WebMcpInvokeWait {
        let deadline = Instant::now() + timeout;
        let Ok(mut map) = self.webmcp.lock() else {
            return WebMcpInvokeWait::Closed;
        };
        loop {
            if let Some(st) = map.get_mut(session_id) {
                if let Some(resp) = st.responses.remove(invocation_id) {
                    st.response_order.retain(|id| id != invocation_id);
                    if st.invoked.remove(invocation_id) {
                        st.invoked_order.retain(|id| id != invocation_id);
                    }
                    return WebMcpInvokeWait::Responded(resp);
                }
                if st.nav_gen != expected_nav_gen {
                    return WebMcpInvokeWait::Navigated;
                }
                if st.invoked.contains(invocation_id) {
                    return WebMcpInvokeWait::Invoked;
                }
            }
            if self.is_closed() {
                return WebMcpInvokeWait::Closed;
            }
            let now = Instant::now();
            if now >= deadline {
                return WebMcpInvokeWait::TimedOut;
            }
            let wait_for = deadline.saturating_duration_since(now);
            match self.webmcp_cv.wait_timeout(map, wait_for) {
                Ok((guard, _)) => map = guard,
                Err(_) => return WebMcpInvokeWait::Closed,
            }
        }
    }

    fn wait_for_webmcp_response(
        &self,
        session_id: &str,
        invocation_id: &str,
        expected_nav_gen: u64,
        timeout: Duration,
    ) -> WebMcpResponseWait {
        let deadline = Instant::now() + timeout;
        let Ok(mut map) = self.webmcp.lock() else {
            return WebMcpResponseWait::Closed;
        };
        loop {
            if let Some(st) = map.get_mut(session_id) {
                if let Some(resp) = st.responses.remove(invocation_id) {
                    st.response_order.retain(|id| id != invocation_id);
                    if st.invoked.remove(invocation_id) {
                        st.invoked_order.retain(|id| id != invocation_id);
                    }
                    return WebMcpResponseWait::Responded(resp);
                }
                if st.nav_gen != expected_nav_gen {
                    return WebMcpResponseWait::Navigated;
                }
            }
            if self.is_closed() {
                return WebMcpResponseWait::Closed;
            }
            let now = Instant::now();
            if now >= deadline {
                return WebMcpResponseWait::TimedOut;
            }
            let wait_for = deadline.saturating_duration_since(now);
            match self.webmcp_cv.wait_timeout(map, wait_for) {
                Ok((guard, _)) => map = guard,
                Err(_) => return WebMcpResponseWait::Closed,
            }
        }
    }

    fn clear_session(&self, session_id: &str) {
        if let Ok(mut map) = self.load_seq.lock() {
            map.remove(session_id);
        }
        if let Ok(mut map) = self.console_logs.lock() {
            map.remove(session_id);
        }
        if let Ok(mut map) = self.webmcp.lock() {
            map.remove(session_id);
        }
    }
}

pub struct CdpPipeClient {
    child: Child,
    writer: Mutex<Option<File>>,
    next_id: AtomicU64,
    shared: Arc<SharedState>,
    reader_thread: Option<JoinHandle<()>>,
}

#[cfg(unix)]
fn create_cloexec_pipe() -> Result<(RawFd, RawFd), String> {
    let mut fds = [0 as RawFd; 2];
    let rc = unsafe { libc::pipe(fds.as_mut_ptr()) };
    if rc != 0 {
        return Err(format!(
            "pipe() failed: {}",
            std::io::Error::last_os_error()
        ));
    }
    for &fd in &fds {
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        if flags >= 0 {
            let _ = unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) };
        }
    }
    Ok((fds[0], fds[1]))
}

#[cfg(unix)]
fn close_fd_quiet(fd: RawFd) {
    let _ = unsafe { libc::close(fd) };
}

impl CdpPipeClient {
    #[cfg(unix)]
    pub fn spawn_moli(moli_bin: &Path, enable_layout: bool) -> Result<Self, String> {
        let (r_in, w_in) = create_cloexec_pipe()?;
        let (r_out, w_out) = match create_cloexec_pipe() {
            Ok(pair) => pair,
            Err(e) => {
                close_fd_quiet(r_in);
                close_fd_quiet(w_in);
                return Err(e);
            }
        };

        let mut cmd = Command::new(moli_bin);
        cmd.arg("serve").arg("--remote-debugging-pipe");
        if enable_layout {
            cmd.arg("--layout");
        }
        cmd.stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());

        // Move pipe ends safely onto FD 3 (read by moli) and FD 4 (written by moli).
        // First relocate above FD 4 if either happened to land on 3 or 4, avoiding dup2 clobbering.
        unsafe {
            cmd.pre_exec(move || {
                let mut src_in = r_in;
                let mut src_out = w_out;

                if src_in == 3 || src_in == 4 {
                    let moved = libc::fcntl(src_in, libc::F_DUPFD_CLOEXEC, 5);
                    if moved < 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                    src_in = moved;
                }
                if src_out == 3 || src_out == 4 {
                    let moved = libc::fcntl(src_out, libc::F_DUPFD_CLOEXEC, 5);
                    if moved < 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                    src_out = moved;
                }

                if libc::dup2(src_in, 3) < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                if libc::dup2(src_out, 4) < 0 {
                    return Err(std::io::Error::last_os_error());
                }

                // Ensure FD_CLOEXEC is explicitly cleared on inherited FDs 3 and 4.
                for target_fd in [3, 4] {
                    let flags = libc::fcntl(target_fd, libc::F_GETFD);
                    if flags >= 0 {
                        let _ = libc::fcntl(target_fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC);
                    }
                }
                Ok(())
            });
        }

        let child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                close_fd_quiet(r_in);
                close_fd_quiet(w_in);
                close_fd_quiet(r_out);
                close_fd_quiet(w_out);
                return Err(format!(
                    "Failed to spawn moli ({}): {}",
                    moli_bin.display(),
                    e
                ));
            }
        };

        // Close child-side ends in parent.
        close_fd_quiet(r_in);
        close_fd_quiet(w_out);

        let write_file = unsafe { File::from_raw_fd(w_in) };
        let read_file = unsafe { File::from_raw_fd(r_out) };
        let shared = Arc::new(SharedState::new());
        let reader_shared = Arc::clone(&shared);

        let reader_thread = thread::spawn(move || {
            reader_loop(read_file, reader_shared);
        });

        let client = Self {
            child,
            writer: Mutex::new(Some(write_file)),
            next_id: AtomicU64::new(1),
            shared,
            reader_thread: Some(reader_thread),
        };

        // Verify CDP pipe handshake immediately.
        let _ = client.call(
            "Browser.getVersion",
            json!({}),
            None,
            Duration::from_secs(10),
        )?;
        Ok(client)
    }

    #[cfg(not(unix))]
    pub fn spawn_moli(_moli_bin: &Path, _enable_layout: bool) -> Result<Self, String> {
        Err("--remote-debugging-pipe transport currently requires Unix (macOS/Linux)".to_string())
    }

    pub fn call(
        &self,
        method: &str,
        params: Value,
        session_id: Option<&str>,
        timeout: Duration,
    ) -> Result<Value, String> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let (tx, rx) = mpsc::channel();
        {
            let mut pending = self
                .shared
                .pending
                .lock()
                .map_err(|_| "CDP pending map poisoned".to_string())?;
            if self.shared.is_closed() {
                return Err("CDP pipe is closed".to_string());
            }
            pending.insert(id, tx);
        }

        let mut payload = json!({
            "id": id,
            "method": method,
            "params": params,
        });
        if let Some(sid) = session_id {
            if let Some(obj) = payload.as_object_mut() {
                obj.insert("sessionId".to_string(), Value::String(sid.to_string()));
            }
        }

        let mut encoded =
            serde_json::to_vec(&payload).map_err(|e| format!("CDP encode error: {}", e))?;
        encoded.push(0);

        {
            let mut guard = self
                .writer
                .lock()
                .map_err(|_| "CDP writer lock poisoned".to_string())?;
            let Some(ref mut f) = *guard else {
                return Err("CDP pipe is closed".to_string());
            };
            if let Err(e) = f.write_all(&encoded) {
                if let Ok(mut pending) = self.shared.pending.lock() {
                    pending.remove(&id);
                }
                return Err(format!("CDP pipe write failed for {}: {}", method, e));
            }
        }

        match rx.recv_timeout(timeout) {
            Ok(msg) => {
                if let Some(err) = msg.get("error") {
                    let err_msg = err
                        .get("message")
                        .and_then(|v| v.as_str())
                        .unwrap_or("unknown CDP error");
                    return Err(format!("CDP {} error: {}", method, err_msg));
                }
                Ok(msg.get("result").cloned().unwrap_or(Value::Null))
            }
            Err(_) => {
                if let Ok(mut pending) = self.shared.pending.lock() {
                    pending.remove(&id);
                }
                Err(format!(
                    "CDP {} timed out after {}ms",
                    method,
                    timeout.as_millis()
                ))
            }
        }
    }

    pub fn current_load_seq(&self, session_id: &str) -> u64 {
        self.shared.current_load_seq(session_id)
    }

    pub fn wait_for_load_advance(
        &self,
        session_id: &str,
        after_seq: u64,
        timeout: Duration,
    ) -> bool {
        self.shared
            .wait_for_load_advance(session_id, after_seq, timeout)
    }

    pub fn console_logs(&self, session_id: &str) -> Vec<String> {
        self.shared.get_console(session_id)
    }

    pub fn is_closed(&self) -> bool {
        self.shared.is_closed()
    }

    pub fn current_webmcp_nav_gen(&self, session_id: &str) -> u64 {
        self.shared.current_webmcp_nav_gen(session_id)
    }

    pub fn webmcp_tools(
        &self,
        session_id: &str,
        frame_filter: Option<&str>,
        wait: Duration,
    ) -> Vec<WebMcpTool> {
        self.shared
            .wait_for_webmcp_tools(session_id, frame_filter, wait)
    }

    pub fn take_webmcp_response(&self, session_id: &str, invocation_id: &str) -> Option<Value> {
        self.shared.take_webmcp_response(session_id, invocation_id)
    }

    pub fn wait_for_webmcp_invoked_or_responded(
        &self,
        session_id: &str,
        invocation_id: &str,
        expected_nav_gen: u64,
        timeout: Duration,
    ) -> WebMcpInvokeWait {
        self.shared.wait_for_webmcp_invoked_or_responded(
            session_id,
            invocation_id,
            expected_nav_gen,
            timeout,
        )
    }

    pub fn wait_for_webmcp_response(
        &self,
        session_id: &str,
        invocation_id: &str,
        expected_nav_gen: u64,
        timeout: Duration,
    ) -> WebMcpResponseWait {
        self.shared
            .wait_for_webmcp_response(session_id, invocation_id, expected_nav_gen, timeout)
    }

    pub fn clear_session_state(&self, session_id: &str) {
        self.shared.clear_session(session_id);
    }
}

fn reader_loop(mut reader: File, shared: Arc<SharedState>) {
    let mut buf = Vec::<u8>::with_capacity(65536);
    let mut chunk = [0u8; 65536];
    loop {
        match reader.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                buf.extend_from_slice(&chunk[..n]);
                let mut start = 0usize;
                for i in 0..buf.len() {
                    if buf[i] == 0 {
                        if i > start {
                            if let Ok(msg) = serde_json::from_slice::<Value>(&buf[start..i]) {
                                dispatch_cdp_message(msg, &shared);
                            }
                        }
                        start = i + 1;
                    }
                }
                if start > 0 {
                    buf.drain(..start);
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        }
    }
    shared.mark_closed();
}

fn dispatch_cdp_message(msg: Value, shared: &Arc<SharedState>) {
    if let Some(id) = msg.get("id").and_then(|v| v.as_u64()) {
        if let Ok(mut pending) = shared.pending.lock() {
            if let Some(tx) = pending.remove(&id) {
                let _ = tx.send(msg);
            }
        }
        return;
    }

    let method = msg.get("method").and_then(|v| v.as_str()).unwrap_or("");
    let session_id = msg.get("sessionId").and_then(|v| v.as_str()).unwrap_or("");
    match method {
        "Page.loadEventFired" | "Page.frameStoppedLoading" if !session_id.is_empty() => {
            shared.bump_load(session_id);
        }
        "Runtime.consoleAPICalled" if !session_id.is_empty() => {
            let level = msg
                .pointer("/params/type")
                .and_then(|v| v.as_str())
                .unwrap_or("log");
            let text = msg
                .pointer("/params/args")
                .and_then(|v| v.as_array())
                .map(|args| {
                    args.iter()
                        .map(|a| {
                            a.get("value")
                                .map(|v| match v {
                                    Value::String(s) => s.clone(),
                                    other => other.to_string(),
                                })
                                .or_else(|| {
                                    a.get("description")
                                        .and_then(|d| d.as_str())
                                        .map(|s| s.to_string())
                                })
                                .unwrap_or_default()
                        })
                        .collect::<Vec<_>>()
                        .join(" ")
                })
                .unwrap_or_default();
            shared.push_console(session_id, format!("[{}] {}", level, text));
        }
        "WebMCP.toolsAdded" if !session_id.is_empty() => {
            if let Some(arr) = msg.pointer("/params/tools").and_then(|v| v.as_array()) {
                let parsed: Vec<WebMcpTool> =
                    arr.iter().filter_map(WebMcpTool::from_cdp_value).collect();
                shared.webmcp_add_tools(session_id, parsed);
            }
        }
        "WebMCP.toolsRemoved" if !session_id.is_empty() => {
            if let Some(arr) = msg.pointer("/params/tools").and_then(|v| v.as_array()) {
                let keys: Vec<(String, String)> = arr
                    .iter()
                    .filter_map(|t| {
                        let fid = t.get("frameId")?.as_str()?.trim();
                        let name = t.get("name")?.as_str()?.trim();
                        if fid.is_empty() || name.is_empty() {
                            return None;
                        }
                        Some((fid.to_string(), name.to_string()))
                    })
                    .collect();
                shared.webmcp_remove_tools(session_id, keys);
            }
        }
        "WebMCP.toolInvoked" if !session_id.is_empty() => {
            if let Some(inv_id) = msg.pointer("/params/invocationId").and_then(|v| v.as_str()) {
                shared.webmcp_mark_invoked(session_id, inv_id.to_string());
            }
        }
        "WebMCP.toolResponded" if !session_id.is_empty() => {
            if let Some(params) = msg.get("params").cloned() {
                if let Some(inv_id) = params.get("invocationId").and_then(|v| v.as_str()) {
                    let id_owned = inv_id.to_string();
                    shared.webmcp_record_response(session_id, id_owned, params);
                }
            }
        }
        "Page.frameNavigated" if !session_id.is_empty() => {
            if let Some(frame) = msg.pointer("/params/frame") {
                if let Some(fid) = frame.get("id").and_then(|v| v.as_str()) {
                    let is_top_level = frame
                        .get("parentId")
                        .and_then(|v| v.as_str())
                        .is_none_or(str::is_empty);
                    shared.webmcp_clear_frame(session_id, fid, is_top_level);
                }
            }
        }
        "Page.frameDetached" if !session_id.is_empty() => {
            if let Some(fid) = msg.pointer("/params/frameId").and_then(|v| v.as_str()) {
                shared.webmcp_clear_frame(session_id, fid, false);
            }
        }
        _ => {}
    }
}

impl Drop for CdpPipeClient {
    fn drop(&mut self) {
        // Closing FD 3 sends EOF to `moli serve --remote-debugging-pipe`, prompting clean shutdown.
        if let Ok(mut guard) = self.writer.lock() {
            guard.take();
        }
        let deadline = Instant::now() + Duration::from_millis(1500);
        loop {
            match self.child.try_wait() {
                Ok(Some(_)) => break,
                Ok(None) => {
                    if Instant::now() >= deadline {
                        let _ = self.child.kill();
                        let _ = self.child.wait();
                        break;
                    }
                    thread::sleep(Duration::from_millis(15));
                }
                Err(_) => break,
            }
        }
        if let Some(handle) = self.reader_thread.take() {
            let _ = handle.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn webmcp_cdp_events_track_lifecycle_trim_removal_nav_and_early_responses() {
        let shared = Arc::new(SharedState::new());
        let sid = "SID-1";
        let other_sid = "SID-2";

        dispatch_cdp_message(
            json!({
                "sessionId": sid,
                "method": "WebMCP.toolsAdded",
                "params": {
                    "tools": [
                        { "name": " main_tool ", "frameId": " F-MAIN ", "description": "Main" },
                        { "name": "child_tool", "frameId": "F-CHILD", "description": "Child" },
                        { "name": "temp_tool", "frameId": "F-MAIN", "description": "Temp" }
                    ]
                }
            }),
            &shared,
        );
        dispatch_cdp_message(
            json!({
                "sessionId": other_sid,
                "method": "WebMCP.toolsAdded",
                "params": {
                    "tools": [{ "name": "other_tool", "frameId": "F-OTHER" }]
                }
            }),
            &shared,
        );
        assert_eq!(
            shared
                .wait_for_webmcp_tools(sid, None, Duration::ZERO)
                .len(),
            3
        );
        assert_eq!(
            shared
                .wait_for_webmcp_tools(other_sid, None, Duration::ZERO)
                .len(),
            1
        );

        // WebMCP.toolsRemoved normalizes whitespace just like toolsAdded.
        dispatch_cdp_message(
            json!({
                "sessionId": sid,
                "method": "WebMCP.toolsRemoved",
                "params": {
                    "tools": [{ "name": " temp_tool ", "frameId": " F-MAIN " }]
                }
            }),
            &shared,
        );
        assert_eq!(
            shared
                .wait_for_webmcp_tools(sid, None, Duration::ZERO)
                .len(),
            2
        );

        // Child-frame navigation clears only child-frame tools and does NOT bump top-level nav_gen.
        let nav_gen0 = shared.current_webmcp_nav_gen(sid);
        dispatch_cdp_message(
            json!({
                "sessionId": sid,
                "method": "Page.frameNavigated",
                "params": { "frame": { "id": "F-CHILD", "parentId": "F-MAIN", "url": "https://example.com/sub" } }
            }),
            &shared,
        );
        assert_eq!(shared.current_webmcp_nav_gen(sid), nav_gen0);
        let after_child_nav = shared.wait_for_webmcp_tools(sid, None, Duration::ZERO);
        assert_eq!(after_child_nav.len(), 1);
        assert_eq!(after_child_nav[0].name, "main_tool");

        // Early WebMCP.toolResponded arriving before wait is still delivered cleanly.
        dispatch_cdp_message(
            json!({
                "sessionId": sid,
                "method": "WebMCP.toolResponded",
                "params": { "invocationId": "inv-1", "status": "Completed", "output": { "ok": true } }
            }),
            &shared,
        );
        match shared.wait_for_webmcp_response(sid, "inv-1", nav_gen0, Duration::from_millis(50)) {
            WebMcpResponseWait::Responded(resp) => assert_eq!(resp["output"]["ok"], true),
            other => panic!("expected Responded, got {:?}", other),
        }

        // Top-level navigation clears stale tools & responses AND bumps nav_gen to abort pending waits.
        dispatch_cdp_message(
            json!({
                "sessionId": sid,
                "method": "WebMCP.toolResponded",
                "params": { "invocationId": "stale-inv", "status": "Completed", "output": {} }
            }),
            &shared,
        );
        dispatch_cdp_message(
            json!({
                "sessionId": sid,
                "method": "Page.frameNavigated",
                "params": { "frame": { "id": "F-MAIN", "url": "https://example.com/next" } }
            }),
            &shared,
        );
        assert!(shared
            .wait_for_webmcp_tools(sid, None, Duration::ZERO)
            .is_empty());
        assert!(shared.take_webmcp_response(sid, "stale-inv").is_none());
        assert_eq!(
            shared.wait_for_webmcp_response(sid, "pending", nav_gen0, Duration::from_millis(50)),
            WebMcpResponseWait::Navigated
        );
        // Other session stays isolated.
        assert_eq!(
            shared
                .wait_for_webmcp_tools(other_sid, None, Duration::ZERO)
                .len(),
            1
        );
        shared.clear_session(other_sid);
        assert!(shared
            .wait_for_webmcp_tools(other_sid, None, Duration::ZERO)
            .is_empty());
    }

    #[test]
    fn webmcp_waiters_handle_invoked_timeout_closed_eviction_and_async_tool_arrival() {
        let shared = Arc::new(SharedState::new());
        let sid = "SID-WAIT";
        let gen0 = shared.current_webmcp_nav_gen(sid);

        // Blocking wait_for_webmcp_tools wakes up when another thread delivers toolsAdded.
        let s_bg = Arc::clone(&shared);
        let producer = thread::spawn(move || {
            thread::sleep(Duration::from_millis(20));
            dispatch_cdp_message(
                json!({
                    "sessionId": sid,
                    "method": "WebMCP.toolsAdded",
                    "params": { "tools": [{ "name": "late_tool", "frameId": "F1" }] }
                }),
                &s_bg,
            );
        });
        let found = shared.wait_for_webmcp_tools(sid, None, Duration::from_millis(500));
        producer.join().expect("producer join");
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].name, "late_tool");

        // Invoked vs TimedOut vs Responded states.
        assert_eq!(
            shared.wait_for_webmcp_invoked_or_responded(
                sid,
                "inv-x",
                gen0,
                Duration::from_millis(15)
            ),
            WebMcpInvokeWait::TimedOut
        );
        dispatch_cdp_message(
            json!({
                "sessionId": sid,
                "method": "WebMCP.toolInvoked",
                "params": { "invocationId": "inv-x" }
            }),
            &shared,
        );
        assert_eq!(
            shared.wait_for_webmcp_invoked_or_responded(
                sid,
                "inv-x",
                gen0,
                Duration::from_millis(50)
            ),
            WebMcpInvokeWait::Invoked
        );
        assert_eq!(
            shared.wait_for_webmcp_response(sid, "inv-x", gen0, Duration::from_millis(15)),
            WebMcpResponseWait::TimedOut
        );

        // FIFO eviction caps responses at MAX_WEBMCP_RESPONSES_PER_SESSION.
        for i in 0..(MAX_WEBMCP_RESPONSES_PER_SESSION + 5) {
            dispatch_cdp_message(
                json!({
                    "sessionId": sid,
                    "method": "WebMCP.toolResponded",
                    "params": { "invocationId": format!("evict-{}", i), "status": "Completed" }
                }),
                &shared,
            );
        }
        assert!(shared.take_webmcp_response(sid, "evict-0").is_none());
        assert!(shared
            .take_webmcp_response(
                sid,
                &format!("evict-{}", MAX_WEBMCP_RESPONSES_PER_SESSION + 4)
            )
            .is_some());

        // mark_closed wakes blocked waiters immediately without waiting for timeout.
        let s_close = Arc::clone(&shared);
        let closer = thread::spawn(move || {
            thread::sleep(Duration::from_millis(20));
            s_close.mark_closed();
        });
        let t0 = Instant::now();
        let res = shared.wait_for_webmcp_response(sid, "never", gen0, Duration::from_secs(5));
        closer.join().expect("closer join");
        assert_eq!(res, WebMcpResponseWait::Closed);
        assert!(t0.elapsed() < Duration::from_secs(1));
        assert_eq!(
            shared.wait_for_webmcp_invoked_or_responded(sid, "never", gen0, Duration::from_secs(1)),
            WebMcpInvokeWait::Closed
        );
    }
}
