use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::fs::File;
use std::io::{Read, Write};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

#[cfg(unix)]
use std::os::unix::io::{FromRawFd, RawFd};
#[cfg(unix)]
use std::os::unix::process::CommandExt;

const MAX_CONSOLE_ENTRIES_PER_SESSION: usize = 200;

struct SharedState {
    pending: Mutex<HashMap<u64, mpsc::Sender<Value>>>,
    load_seq: Mutex<HashMap<String, u64>>,
    load_cv: Condvar,
    console_logs: Mutex<HashMap<String, VecDeque<String>>>,
    closed: Mutex<bool>,
}

impl SharedState {
    fn new() -> Self {
        Self {
            pending: Mutex::new(HashMap::new()),
            load_seq: Mutex::new(HashMap::new()),
            load_cv: Condvar::new(),
            console_logs: Mutex::new(HashMap::new()),
            closed: Mutex::new(false),
        }
    }

    fn mark_closed(&self) {
        if let Ok(mut c) = self.closed.lock() {
            *c = true;
        }
        if let Ok(mut pending) = self.pending.lock() {
            pending.clear();
        }
        self.load_cv.notify_all();
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
            if self.closed.lock().map(|c| *c).unwrap_or(true) {
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
                return Err(format!("Failed to spawn moli ({}): {}", moli_bin.display(), e));
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
        let _ = client.call("Browser.getVersion", json!({}), None, Duration::from_secs(10))?;
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
