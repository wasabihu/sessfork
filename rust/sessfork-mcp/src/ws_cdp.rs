//! Zero-Tokio loopback RFC 6455 WebSocket client + `/json/version` discovery for CDP over TCP,
//! plus Win32 Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) so `moli.exe` never leaks
//! orphan processes on Windows even if `sessfork-mcp.exe` is terminated abruptly.
//!
//! Compiled on all platforms so WebSocket framing, SHA-1 handshake, port discovery, and
//! security guards are exercised by unit & live tests on macOS/Linux as well as Windows.

use serde_json::Value;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{Shutdown, SocketAddr, TcpStream};
use std::process::{Child, ChildStderr};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const MAX_HTTP_HEADER_BYTES: usize = 64 * 1024;
const MAX_HTTP_LINE_BYTES: usize = 8 * 1024;
const MAX_JSON_VERSION_BYTES: usize = 1024 * 1024;
const MAX_WS_CONTROL_PAYLOAD_BYTES: usize = 125;
const WS_GUID: &str = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

#[cfg(windows)]
pub(crate) struct Win32JobGuard {
    handle: windows_sys::Win32::Foundation::HANDLE,
}

#[cfg(windows)]
// SAFETY: Win32 Job Object handles are process-wide kernel handles that can safely be
// moved across threads and closed via `CloseHandle` from any thread.
unsafe impl Send for Win32JobGuard {}

#[cfg(windows)]
impl Win32JobGuard {
    /// Create an anonymous Win32 Job Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`
    /// and assign `child` to it so the Windows kernel terminates `child` (and any grandchildren)
    /// automatically if `sessfork-mcp.exe` exits or is killed.
    pub(crate) fn bind_child(child: &Child) -> Result<Self, String> {
        use std::mem::{size_of, zeroed};
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
        use windows_sys::Win32::System::JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };

        // SAFETY: Passing null security attributes and null name creates an unnamed Job Object
        // owned by the current process.
        let job: HANDLE = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if job.is_null() {
            return Err(format!(
                "CreateJobObjectW failed: {}",
                std::io::Error::last_os_error()
            ));
        }

        // SAFETY: `JOBOBJECT_EXTENDED_LIMIT_INFORMATION` is a plain C struct; zero-initialization
        // sets all unused limits cleanly before enabling `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { zeroed() };
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

        // SAFETY: `job` is a valid Job Object handle and `info` has exact matching size.
        let set_ok = unsafe {
            SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                (&raw const info).cast(),
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if set_ok == 0 {
            let err = std::io::Error::last_os_error();
            // SAFETY: `job` was successfully created above and is not owned elsewhere.
            unsafe {
                CloseHandle(job);
            }
            return Err(format!("SetInformationJobObject failed: {err}"));
        }

        let proc_handle = child.as_raw_handle() as HANDLE;
        // SAFETY: `job` is valid and `proc_handle` is borrowed from a live `Child`.
        let assign_ok = unsafe { AssignProcessToJobObject(job, proc_handle) };
        if assign_ok == 0 {
            let err = std::io::Error::last_os_error();
            // SAFETY: Clean up `job` handle on failure.
            unsafe {
                CloseHandle(job);
            }
            return Err(format!("AssignProcessToJobObject failed: {err}"));
        }

        Ok(Self { handle: job })
    }
}

#[cfg(windows)]
impl Drop for Win32JobGuard {
    fn drop(&mut self) {
        if !self.handle.is_null() {
            // SAFETY: `self.handle` is owned exclusively by this guard; closing it triggers
            // `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` in the Windows kernel.
            unsafe {
                windows_sys::Win32::Foundation::CloseHandle(self.handle);
            }
            self.handle = std::ptr::null_mut();
        }
    }
}

/// Strip ANSI CSI escape sequences (e.g. `\x1b[32m`) from Moli log lines.
fn strip_ansi_escapes(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    let mut chars = input.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '\x1b' && chars.peek() == Some(&'[') {
            chars.next();
            for c in chars.by_ref() {
                if ('@'..='~').contains(&c) {
                    break;
                }
            }
        } else {
            out.push(ch);
        }
    }
    out
}

/// Extract the kernel-assigned loopback port from a `moli serve --host 127.0.0.1 --port 0`
/// stderr log line such as `INFO protocol server listening addr=127.0.0.1:65161`.
pub(crate) fn parse_listening_loopback_port(line: &str) -> Option<u16> {
    let clean = strip_ansi_escapes(line);
    let idx = clean.find("127.0.0.1:")?;
    let rest = &clean[idx + "127.0.0.1:".len()..];
    let digits: String = rest.chars().take_while(char::is_ascii_digit).collect();
    if digits.is_empty() {
        return None;
    }
    digits.parse::<u16>().ok().filter(|&p| p > 0)
}

/// Read the OS-assigned loopback port directly from `moli serve --host 127.0.0.1 --port 0`
/// stderr (eliminating bind-then-close port TOCTOU completely), and keep draining stderr in a
/// background thread so Moli can never block on a full OS pipe buffer.
pub(crate) fn read_moli_bound_port_from_stderr(
    stderr: ChildStderr,
    child: &mut Child,
    timeout: Duration,
) -> Result<u16, String> {
    let (tx, rx) = mpsc::channel::<Result<u16, String>>();
    thread::spawn(move || {
        let mut reader = BufReader::new(stderr);
        let mut sent = false;
        let mut tail = String::new();
        loop {
            let mut buf = Vec::new();
            match reader.by_ref().take(8192).read_until(b'\n', &mut buf) {
                Ok(0) => break,
                Ok(_) => {
                    let line = String::from_utf8_lossy(&buf);
                    if !sent {
                        if let Some(port) = parse_listening_loopback_port(&line) {
                            let _ = tx.send(Ok(port));
                            sent = true;
                        } else if tail.len() < 512 {
                            tail.push_str(&strip_ansi_escapes(&line));
                        }
                    }
                }
                Err(_) => break,
            }
        }
        if !sent {
            let _ = tx.send(Err(format!(
                "moli stderr closed before reporting listening port ({})",
                tail.trim()
            )));
        }
    });

    let deadline = Instant::now() + timeout;
    loop {
        let now = Instant::now();
        if now >= deadline {
            return Err("Timed out waiting for moli serve --port 0 listening address".to_string());
        }
        let step = deadline
            .saturating_duration_since(now)
            .min(Duration::from_millis(50));
        match rx.recv_timeout(step) {
            Ok(Ok(port)) => return Ok(port),
            Ok(Err(e)) => return Err(e),
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                return Err("moli stderr reader disconnected before reporting port".to_string());
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {
                if let Ok(Some(status)) = child.try_wait() {
                    return Err(format!(
                        "moli serve exited prematurely with status {status} before opening CDP"
                    ));
                }
            }
        }
    }
}

/// Poll `http://127.0.0.1:<port>/json/version` until Moli responds, returning the validated
/// loopback WebSocket path (e.g. `/devtools/browser/<uuid>`).
pub(crate) fn wait_for_ws_debugger_path(
    port: u16,
    child: &mut Child,
    timeout: Duration,
) -> Result<String, String> {
    let deadline = Instant::now() + timeout;
    let mut last_err = String::from("timed out waiting for moli CDP endpoint");

    while Instant::now() < deadline {
        if let Ok(Some(status)) = child.try_wait() {
            return Err(format!(
                "moli serve exited prematurely with status {status} before opening CDP"
            ));
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        match fetch_json_version_ws_path(port, remaining.min(Duration::from_secs(2))) {
            Ok(path) => return Ok(path),
            Err(e) => {
                last_err = e;
                thread::sleep(Duration::from_millis(20));
            }
        }
    }
    Err(format!(
        "Timed out waiting for moli CDP on 127.0.0.1:{port}: {last_err}"
    ))
}

fn set_remaining_timeouts(stream: &TcpStream, deadline: Instant) -> Result<(), String> {
    let rem = deadline.saturating_duration_since(Instant::now());
    if rem.is_zero() {
        return Err("HTTP operation exceeded overall deadline".to_string());
    }
    let _ = stream.set_read_timeout(Some(rem));
    let _ = stream.set_write_timeout(Some(rem));
    Ok(())
}

fn fetch_json_version_ws_path(port: u16, timeout: Duration) -> Result<String, String> {
    let deadline = Instant::now() + timeout;
    let addr: SocketAddr = ([127, 0, 0, 1], port).into();
    let mut stream = TcpStream::connect_timeout(&addr, timeout.min(Duration::from_millis(400)))
        .map_err(|e| format!("connect failed: {e}"))?;
    set_remaining_timeouts(&stream, deadline)?;

    let req = format!(
        "GET /json/version HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\nAccept: application/json\r\n\r\n"
    );
    stream
        .write_all(req.as_bytes())
        .and_then(|()| stream.flush())
        .map_err(|e| format!("HTTP write failed: {e}"))?;

    let mut reader = BufReader::new(stream);
    let status_line = read_http_line_deadline(&mut reader, deadline)?;
    let mut parts = status_line.split_whitespace();
    let proto = parts.next().unwrap_or("");
    let code = parts.next().unwrap_or("");
    if !(proto == "HTTP/1.1" || proto == "HTTP/1.0") || code != "200" {
        return Err(format!("unexpected status: {}", status_line.trim()));
    }

    let mut total_hdr = status_line.len();
    let mut content_length: Option<usize> = None;
    let mut chunked = false;
    loop {
        let line = read_http_line_deadline(&mut reader, deadline)?;
        if line.is_empty() {
            return Err("unexpected EOF inside /json/version HTTP headers".to_string());
        }
        total_hdr += line.len();
        if total_hdr > MAX_HTTP_HEADER_BYTES {
            return Err("HTTP headers exceeded safety cap".to_string());
        }
        if line == "\r\n" || line == "\n" {
            break;
        }
        if let Some((k, v)) = line.split_once(':') {
            let k_low = k.trim().to_ascii_lowercase();
            let v_trim = v.trim();
            if k_low == "content-length" {
                content_length = v_trim.parse::<usize>().ok();
            } else if k_low == "transfer-encoding"
                && v_trim.to_ascii_lowercase().contains("chunked")
            {
                chunked = true;
            }
        }
    }

    set_remaining_timeouts(reader.get_ref(), deadline)?;
    let body = if chunked {
        read_chunked_http_body(&mut reader, deadline)?
    } else if let Some(len) = content_length {
        if len > MAX_JSON_VERSION_BYTES {
            return Err("/json/version response exceeded 1 MiB".to_string());
        }
        let mut buf = vec![0u8; len];
        reader
            .read_exact(&mut buf)
            .map_err(|e| format!("body read failed: {e}"))?;
        buf
    } else {
        let mut buf = Vec::new();
        reader
            .take((MAX_JSON_VERSION_BYTES + 1) as u64)
            .read_to_end(&mut buf)
            .map_err(|e| format!("body read failed: {e}"))?;
        if buf.len() > MAX_JSON_VERSION_BYTES {
            return Err("/json/version response exceeded 1 MiB".to_string());
        }
        buf
    };

    let parsed: Value =
        serde_json::from_slice(&body).map_err(|e| format!("invalid /json/version JSON: {e}"))?;
    let ws_url = parsed
        .get("webSocketDebuggerUrl")
        .and_then(Value::as_str)
        .ok_or_else(|| "missing webSocketDebuggerUrl in /json/version".to_string())?;
    parse_loopback_ws_path(ws_url, port)
}

/// Strictly validated loopback CDP endpoint (`http://127.0.0.1:<port>` or `ws://127.0.0.1:<port>/<path>`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum LoopbackCdpEndpoint {
    HttpDiscovery { port: u16 },
    DirectWs { port: u16, path: String },
}

fn parse_loopback_host_port(authority: &str) -> Result<u16, String> {
    if authority.contains('@')
        || authority.contains('/')
        || authority.contains('?')
        || authority.contains('#')
        || authority.bytes().any(|b| !b.is_ascii_graphic())
    {
        return Err(format!(
            "Refusing malformed or userinfo-bearing CDP authority '{authority}'"
        ));
    }
    let (host, port_str) = authority.rsplit_once(':').ok_or_else(|| {
        format!("Refusing CDP endpoint without explicit loopback port: '{authority}'")
    })?;
    if !host.eq_ignore_ascii_case("127.0.0.1") && !host.eq_ignore_ascii_case("localhost") {
        return Err(format!(
            "Refusing non-loopback CDP host '{host}' (only 127.0.0.1 / localhost are allowed)"
        ));
    }
    let port = port_str
        .parse::<u16>()
        .ok()
        .filter(|&p| p > 0)
        .ok_or_else(|| format!("Refusing invalid CDP port '{port_str}'"))?;
    Ok(port)
}

/// Parse and fail-closed validate an external `--cdp-url` endpoint.
/// Accepts ONLY `http://127.0.0.1:<port>` (or `localhost`, optional trailing `/`) for `/json/version`
/// discovery, or `ws://127.0.0.1:<port>/<path>` (or `localhost`) for direct WebSocket attach.
pub(crate) fn parse_loopback_cdp_endpoint(raw: &str) -> Result<LoopbackCdpEndpoint, String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() || trimmed.bytes().any(|b| !b.is_ascii_graphic() || b == b'#') {
        return Err(format!("Refusing invalid --cdp-url '{raw}'"));
    }
    if let Some(rest) = trimmed.strip_prefix("http://") {
        let authority = rest.strip_suffix('/').unwrap_or(rest);
        let port = parse_loopback_host_port(authority)?;
        return Ok(LoopbackCdpEndpoint::HttpDiscovery { port });
    }
    if let Some(rest) = trimmed.strip_prefix("ws://") {
        let (authority, path_rest) = rest
            .split_once('/')
            .ok_or_else(|| format!("Direct ws:// --cdp-url must include a path: '{raw}'"))?;
        let port = parse_loopback_host_port(authority)?;
        let path = format!("/{path_rest}");
        if path_rest.is_empty() || path.bytes().any(|b| !b.is_ascii_graphic() || b == b'#') {
            return Err(format!(
                "Refusing invalid WebSocket path in --cdp-url '{raw}'"
            ));
        }
        return Ok(LoopbackCdpEndpoint::DirectWs { port, path });
    }
    Err(format!(
        "Refusing --cdp-url '{raw}': only loopback http:// or ws:// URLs are permitted"
    ))
}

/// Resolve a validated loopback CDP endpoint to `(port, ws_path)` by querying `/json/version`
/// when given `http://127.0.0.1:<port>`, or returning the direct WebSocket path immediately.
pub(crate) fn resolve_loopback_cdp_ws(
    endpoint: &LoopbackCdpEndpoint,
    timeout: Duration,
) -> Result<(u16, String), String> {
    match endpoint {
        LoopbackCdpEndpoint::DirectWs { port, path } => Ok((*port, path.clone())),
        LoopbackCdpEndpoint::HttpDiscovery { port } => {
            let path = fetch_json_version_ws_path(*port, timeout)?;
            Ok((*port, path))
        }
    }
}

/// Strictly validate that `webSocketDebuggerUrl` points back to `ws://127.0.0.1:<expected_port>/...`
/// (or `ws://localhost:<expected_port>/...`) with ASCII-graphic path and no fragment.
pub(crate) fn parse_loopback_ws_path(ws_url: &str, expected_port: u16) -> Result<String, String> {
    let rest = ws_url
        .strip_prefix("ws://")
        .ok_or_else(|| format!("Refusing non-ws debugger URL: {ws_url}"))?;
    let (authority, path_rest) = rest
        .split_once('/')
        .ok_or_else(|| format!("Debugger URL missing path: {ws_url}"))?;
    let expected_ip = format!("127.0.0.1:{expected_port}");
    let expected_host = format!("localhost:{expected_port}");
    if !authority.eq_ignore_ascii_case(&expected_ip)
        && !authority.eq_ignore_ascii_case(&expected_host)
    {
        return Err(format!(
            "Refusing debugger URL targeting non-matching authority '{authority}'"
        ));
    }
    let path = format!("/{path_rest}");
    if path.bytes().any(|b| !b.is_ascii_graphic() || b == b'#') {
        return Err(
            "Refusing debugger URL path with non-ASCII, whitespace, or fragment".to_string(),
        );
    }
    Ok(path)
}

fn read_http_line_deadline(
    reader: &mut BufReader<TcpStream>,
    deadline: Instant,
) -> Result<String, String> {
    set_remaining_timeouts(reader.get_ref(), deadline)?;
    read_http_line(reader)
}

fn read_http_line<R: BufRead>(reader: &mut R) -> Result<String, String> {
    let mut buf = Vec::new();
    let n = reader
        .take((MAX_HTTP_LINE_BYTES + 1) as u64)
        .read_until(b'\n', &mut buf)
        .map_err(|e| format!("HTTP line read failed: {e}"))?;
    if n == 0 {
        return Ok(String::new());
    }
    if buf.len() > MAX_HTTP_LINE_BYTES {
        return Err("HTTP line exceeded safety cap".to_string());
    }
    Ok(String::from_utf8_lossy(&buf).into_owned())
}

fn read_chunked_http_body(
    reader: &mut BufReader<TcpStream>,
    deadline: Instant,
) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    loop {
        let size_line = read_http_line_deadline(reader, deadline)?;
        let hex = size_line.split(';').next().unwrap_or("").trim();
        let sz =
            usize::from_str_radix(hex, 16).map_err(|_| format!("invalid chunk size '{hex}'"))?;
        if sz == 0 {
            break;
        }
        if out.len().saturating_add(sz) > MAX_JSON_VERSION_BYTES {
            return Err("chunked /json/version exceeded 1 MiB".to_string());
        }
        set_remaining_timeouts(reader.get_ref(), deadline)?;
        let start = out.len();
        out.resize(start + sz, 0);
        if let Some(slice) = out.get_mut(start..) {
            reader
                .read_exact(slice)
                .map_err(|e| format!("truncated chunk body: {e}"))?;
        }
        let mut crlf = [0u8; 2];
        reader
            .read_exact(&mut crlf)
            .map_err(|e| format!("missing chunk CRLF: {e}"))?;
        if crlf != *b"\r\n" {
            return Err("malformed chunk terminator".to_string());
        }
    }
    Ok(out)
}

/// Thread-safe RFC 6455 masked client writer backed by a loopback `TcpStream`.
pub(crate) struct WsFrameWriter {
    stream: TcpStream,
    mask_counter: AtomicU64,
}

impl WsFrameWriter {
    pub(crate) fn send_text(&mut self, payload: &[u8]) -> Result<(), String> {
        self.write_frame(0x1, payload)
    }

    pub(crate) fn send_pong(&mut self, payload: &[u8]) -> Result<(), String> {
        let bounded = payload
            .get(..payload.len().min(MAX_WS_CONTROL_PAYLOAD_BYTES))
            .unwrap_or(&[]);
        self.write_frame(0xA, bounded)
    }

    /// Best-effort Close frame with a strict 200 ms write timeout so Drop never stalls for 10s
    /// against a wedged child process.
    pub(crate) fn send_close(&mut self) {
        let _ = self
            .stream
            .set_write_timeout(Some(Duration::from_millis(200)));
        let _ = self.write_frame(0x8, &[]);
        let _ = self.stream.shutdown(Shutdown::Both);
    }

    fn next_mask(&self) -> [u8; 4] {
        let seq = self.mask_counter.fetch_add(1, Ordering::Relaxed);
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0x9E37_79B9_7F4A_7C15);
        // SplitMix64 mixing for loopback RFC 6455 client frame masking.
        let mut z = nanos.wrapping_add(seq.wrapping_mul(0x9E37_79B9_7F4A_7C15));
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^= z >> 31;
        let bytes = (z as u32).to_le_bytes();
        if bytes == [0, 0, 0, 0] {
            [0xA5, 0x5A, 0x3C, 0xC3]
        } else {
            bytes
        }
    }

    fn write_frame(&mut self, opcode: u8, payload: &[u8]) -> Result<(), String> {
        let mask = self.next_mask();
        let len = payload.len();
        let mut frame = Vec::with_capacity(len + 14);
        frame.push(0x80 | (opcode & 0x0F)); // FIN=1 | opcode
        if len < 126 {
            frame.push(0x80 | (len as u8)); // MASK=1
        } else if len <= 65_535 {
            frame.push(0x80 | 126);
            frame.extend_from_slice(&(len as u16).to_be_bytes());
        } else {
            frame.push(0x80 | 127);
            frame.extend_from_slice(&(len as u64).to_be_bytes());
        }
        frame.extend_from_slice(&mask);
        for (idx, &b) in payload.iter().enumerate() {
            let m = mask[idx & 3];
            frame.push(b ^ m);
        }
        self.stream
            .write_all(&frame)
            .and_then(|()| self.stream.flush())
            .map_err(|e| format!("CDP WebSocket write failed: {e}"))
    }
}

/// Connect to `ws://127.0.0.1:<port><ws_path>`, complete strict RFC 6455 handshake, and return
/// `(reader, writer)` split over cloned loopback sockets.
pub(crate) fn connect_loopback_ws(
    port: u16,
    ws_path: &str,
) -> Result<(BufReader<TcpStream>, WsFrameWriter), String> {
    let deadline = Instant::now() + Duration::from_secs(5);
    let addr: SocketAddr = ([127, 0, 0, 1], port).into();
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(5))
        .map_err(|e| format!("Failed to connect CDP WebSocket on 127.0.0.1:{port}: {e}"))?;
    let _ = stream.set_nodelay(true);
    set_remaining_timeouts(&stream, deadline)?;

    let key_b64 = generate_ws_key_b64();
    let expected_accept = compute_ws_accept(&key_b64);

    let handshake = format!(
        "GET {ws_path} HTTP/1.1\r\n\
         Host: 127.0.0.1:{port}\r\n\
         Upgrade: websocket\r\n\
         Connection: Upgrade\r\n\
         Sec-WebSocket-Key: {key_b64}\r\n\
         Sec-WebSocket-Version: 13\r\n\r\n"
    );
    stream
        .write_all(handshake.as_bytes())
        .and_then(|()| stream.flush())
        .map_err(|e| format!("CDP WebSocket handshake write failed: {e}"))?;

    let mut reader = BufReader::new(stream);
    let status_line = read_http_line_deadline(&mut reader, deadline)?;
    let mut status_parts = status_line.split_whitespace();
    let proto = status_parts.next().unwrap_or("");
    let code = status_parts.next().unwrap_or("");
    if proto != "HTTP/1.1" || code != "101" {
        return Err(format!(
            "CDP WebSocket handshake rejected: {}",
            status_line.trim()
        ));
    }

    let mut total_hdr = status_line.len();
    let mut accept_ok = false;
    let mut upgrade_ok = false;
    let mut connection_ok = false;
    loop {
        let line = read_http_line_deadline(&mut reader, deadline)?;
        if line.is_empty() {
            return Err("Unexpected EOF inside WebSocket handshake headers".to_string());
        }
        total_hdr += line.len();
        if total_hdr > MAX_HTTP_HEADER_BYTES {
            return Err("CDP WebSocket handshake headers exceeded cap".to_string());
        }
        if line == "\r\n" || line == "\n" {
            break;
        }
        if let Some((k, v)) = line.split_once(':') {
            let k_trim = k.trim();
            let v_trim = v.trim();
            if k_trim.eq_ignore_ascii_case("sec-websocket-accept") && v_trim == expected_accept {
                accept_ok = true;
            } else if k_trim.eq_ignore_ascii_case("upgrade")
                && v_trim.eq_ignore_ascii_case("websocket")
            {
                upgrade_ok = true;
            } else if k_trim.eq_ignore_ascii_case("connection")
                && v_trim
                    .split(',')
                    .any(|tok| tok.trim().eq_ignore_ascii_case("upgrade"))
            {
                connection_ok = true;
            }
        }
    }
    if !(accept_ok && upgrade_ok && connection_ok) {
        return Err(
            "CDP WebSocket handshake missing valid Upgrade/Connection/Sec-WebSocket-Accept"
                .to_string(),
        );
    }

    // Clear handshake read timeout so the background CDP event reader blocks cleanly on idle.
    let _ = reader.get_ref().set_read_timeout(None);
    let write_stream = reader
        .get_ref()
        .try_clone()
        .map_err(|e| format!("Failed to clone CDP WebSocket stream: {e}"))?;
    let _ = write_stream.set_write_timeout(Some(Duration::from_secs(5)));

    Ok((
        reader,
        WsFrameWriter {
            stream: write_stream,
            mask_counter: AtomicU64::new(1),
        },
    ))
}

/// Read the next complete RFC 6455 text/binary message payload (reassembling fragmented frames
/// and invoking `on_ping` for Ping control frames). Returns `Ok(None)` on clean Close/EOF.
pub(crate) fn read_next_ws_message(
    reader: &mut BufReader<TcpStream>,
    max_message_bytes: usize,
    mut on_ping: impl FnMut(&[u8]),
) -> Result<Option<Vec<u8>>, String> {
    let mut message = Vec::new();
    let mut in_fragment = false;

    loop {
        let mut head = [0u8; 2];
        match reader.read_exact(&mut head) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => return Ok(None),
            Err(e) => return Err(format!("WebSocket header read failed: {e}")),
        }

        let fin = (head[0] & 0x80) != 0;
        let rsv = head[0] & 0x70;
        if rsv != 0 {
            return Err(format!("WebSocket frame has non-zero RSV bits (0x{rsv:x})"));
        }
        let opcode = head[0] & 0x0F;
        let masked = (head[1] & 0x80) != 0;
        let len_code = head[1] & 0x7F;
        let is_control = opcode >= 0x8;

        let payload_len: usize = match len_code {
            0..=125 => usize::from(len_code),
            126 => {
                let mut ext = [0u8; 2];
                reader
                    .read_exact(&mut ext)
                    .map_err(|e| format!("WebSocket 16-bit len read failed: {e}"))?;
                usize::from(u16::from_be_bytes(ext))
            }
            127 => {
                let mut ext = [0u8; 8];
                reader
                    .read_exact(&mut ext)
                    .map_err(|e| format!("WebSocket 64-bit len read failed: {e}"))?;
                let raw = u64::from_be_bytes(ext);
                usize::try_from(raw).map_err(|_| "WebSocket frame length overflow".to_string())?
            }
            _ => unreachable!(),
        };

        if is_control {
            if !fin || payload_len > MAX_WS_CONTROL_PAYLOAD_BYTES {
                return Err(format!(
                    "Invalid WebSocket control frame (fin={fin}, len={payload_len})"
                ));
            }
        } else if payload_len > max_message_bytes
            || message.len().saturating_add(payload_len) > max_message_bytes
        {
            return Err(format!(
                "WebSocket CDP message exceeds {max_message_bytes}-byte safety cap"
            ));
        }

        let mask_key = if masked {
            let mut m = [0u8; 4];
            reader
                .read_exact(&mut m)
                .map_err(|e| format!("WebSocket mask read failed: {e}"))?;
            Some(m)
        } else {
            None
        };

        let mut payload = vec![0u8; payload_len];
        if payload_len > 0 {
            reader
                .read_exact(&mut payload)
                .map_err(|e| format!("WebSocket payload read failed: {e}"))?;
            if let Some(m) = mask_key {
                for (i, b) in payload.iter_mut().enumerate() {
                    *b ^= m[i & 3];
                }
            }
        }

        match opcode {
            // Control frames may be interleaved mid-fragment per RFC 6455 §5.4 and do NOT
            // count against the fragmented data message size cap.
            0x8 => return Ok(None),   // Close
            0x9 => on_ping(&payload), // Ping
            0xA => {}                 // Pong
            0x1 | 0x2 => {
                if in_fragment {
                    return Err(
                        "Unexpected new WebSocket data frame while fragmented message is in progress"
                            .to_string(),
                    );
                }
                if fin {
                    // Fast zero-copy path for normal unfragmented CDP messages.
                    return Ok(Some(payload));
                }
                message = payload;
                in_fragment = true;
            }
            0x0 if in_fragment => {
                message.extend_from_slice(&payload);
                if fin {
                    return Ok(Some(message));
                }
            }
            other => {
                return Err(format!("Unexpected WebSocket opcode 0x{other:x}"));
            }
        }
    }
}

fn generate_ws_key_b64() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0x0123_4567_89AB_CDEF_FEDC_BA98_7654_3210);
    let mut bytes = nanos.to_le_bytes();
    let pid = std::process::id().to_le_bytes();
    for (i, &p) in pid.iter().enumerate() {
        bytes[i] ^= p;
    }
    base64_encode(&bytes)
}

/// RFC 6455 §1.3 `Sec-WebSocket-Accept = base64(SHA-1(key + GUID))`.
pub(crate) fn compute_ws_accept(client_key: &str) -> String {
    let mut input = Vec::with_capacity(client_key.len() + WS_GUID.len());
    input.extend_from_slice(client_key.as_bytes());
    input.extend_from_slice(WS_GUID.as_bytes());
    let digest = sha1_digest(&input);
    base64_encode(&digest)
}

/// Minimal pure-Rust FIPS 180-4 SHA-1 used strictly for RFC 6455 `Sec-WebSocket-Accept` handshake.
fn sha1_digest(data: &[u8]) -> [u8; 20] {
    let mut h0: u32 = 0x6745_2301;
    let mut h1: u32 = 0xEFCD_AB89;
    let mut h2: u32 = 0x98BA_DCFE;
    let mut h3: u32 = 0x1032_5476;
    let mut h4: u32 = 0xC3D2_E1F0;

    let bit_len = (data.len() as u64).wrapping_mul(8);
    let mut msg = Vec::with_capacity(data.len() + 72);
    msg.extend_from_slice(data);
    msg.push(0x80);
    while (msg.len() % 64) != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());

    for chunk in msg.as_chunks::<64>().0 {
        let mut w = [0u32; 80];
        for (slot, word) in w.iter_mut().zip(chunk.as_chunks::<4>().0) {
            *slot = u32::from_be_bytes(*word);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }

        let (mut a, mut b, mut c, mut d, mut e) = (h0, h1, h2, h3, h4);
        for (i, &wi) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | ((!b) & d), 0x5A82_7999u32),
                20..=39 => (b ^ c ^ d, 0x6ED9_EBA1u32),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1B_BCDCu32),
                _ => (b ^ c ^ d, 0xCA62_C1D6u32),
            };
            let temp = a
                .rotate_left(5)
                .wrapping_add(f)
                .wrapping_add(e)
                .wrapping_add(k)
                .wrapping_add(wi);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = temp;
        }
        h0 = h0.wrapping_add(a);
        h1 = h1.wrapping_add(b);
        h2 = h2.wrapping_add(c);
        h3 = h3.wrapping_add(d);
        h4 = h4.wrapping_add(e);
    }

    let mut out = [0u8; 20];
    out[0..4].copy_from_slice(&h0.to_be_bytes());
    out[4..8].copy_from_slice(&h1.to_be_bytes());
    out[8..12].copy_from_slice(&h2.to_be_bytes());
    out[12..16].copy_from_slice(&h3.to_be_bytes());
    out[16..20].copy_from_slice(&h4.to_be_bytes());
    out
}

fn base64_encode(input: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let b0 = u32::from(chunk[0]);
        let b1 = if chunk.len() > 1 {
            u32::from(chunk[1])
        } else {
            0
        };
        let b2 = if chunk.len() > 2 {
            u32::from(chunk[2])
        } else {
            0
        };
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(TABLE[((triple >> 18) & 0x3F) as usize] as char);
        out.push(TABLE[((triple >> 12) & 0x3F) as usize] as char);
        if chunk.len() > 1 {
            out.push(TABLE[((triple >> 6) & 0x3F) as usize] as char);
        } else {
            out.push('=');
        }
        if chunk.len() > 2 {
            out.push(TABLE[(triple & 0x3F) as usize] as char);
        } else {
            out.push('=');
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn computes_rfc6455_test_vector_accept_header() {
        assert_eq!(
            compute_ws_accept("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
        );
    }

    #[test]
    fn parses_ansi_moli_listening_port_and_validates_loopback_urls() {
        let line = "\x1b[2m2026-11-25T13:20:00Z\x1b[0m \x1b[32m INFO\x1b[0m protocol server listening \x1b[3maddr\x1b[0m\x1b[2m=\x1b[0m127.0.0.1:65161\n";
        assert_eq!(parse_listening_loopback_port(line), Some(65161));
        assert_eq!(parse_listening_loopback_port("127.0.0.1:0"), None);

        assert_eq!(
            parse_loopback_ws_path("ws://127.0.0.1:9222/devtools/browser/abc-123", 9222)
                .expect("valid loopback URL"),
            "/devtools/browser/abc-123"
        );
        assert!(parse_loopback_ws_path("ws://127.0.0.1:9999/devtools/browser/abc", 9222).is_err());
        assert!(
            parse_loopback_ws_path("ws://evil.example:9222/devtools/browser/abc", 9222).is_err()
        );
        assert!(parse_loopback_ws_path("wss://127.0.0.1:9222/devtools/browser/abc", 9222).is_err());
        assert!(parse_loopback_ws_path("ws://127.0.0.1:9222/a#frag", 9222).is_err());
        assert!(parse_loopback_ws_path("ws://127.0.0.1:9222/a\u{0080}", 9222).is_err());
    }

    #[test]
    fn round_trips_fragmented_frames_interleaved_ping_near_cap_and_rejects_bad_rsv() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
        let port = listener.local_addr().expect("addr").port();

        let srv = thread::spawn(move || {
            let (mut sock, _) = listener.accept().expect("accept");
            let mut reader = BufReader::new(sock.try_clone().expect("clone"));
            let mut key = String::new();
            loop {
                let line = read_http_line(&mut reader).expect("hdr");
                if line == "\r\n" || line.is_empty() {
                    break;
                }
                if let Some((k, v)) = line.split_once(':') {
                    if k.trim().eq_ignore_ascii_case("sec-websocket-key") {
                        key = v.trim().to_string();
                    }
                }
            }
            let accept = compute_ws_accept(&key);
            let resp = format!(
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
            );
            sock.write_all(resp.as_bytes()).expect("101");

            // Read 1 masked text frame from client.
            let mut hdr = [0u8; 2];
            reader.read_exact(&mut hdr).expect("client hdr");
            let len = usize::from(hdr[1] & 0x7F);
            let mut mask = [0u8; 4];
            reader.read_exact(&mut mask).expect("mask");
            let mut body = vec![0u8; len];
            reader.read_exact(&mut body).expect("body");
            for (i, b) in body.iter_mut().enumerate() {
                *b ^= mask[i & 3];
            }
            assert_eq!(String::from_utf8(body).expect("utf8"), r#"{"id":1}"#);

            // Fragmented 28-byte message with interleaved 2-byte Ping; exact cap = 29 bytes
            // so Ping MUST NOT push message over cap!
            let part1 = b"{\"id\":1,";
            let part2 = b"\"result\":{\"ok\":true}}";
            sock.write_all(&[0x01, part1.len() as u8]).expect("f1");
            sock.write_all(part1).expect("f1b");
            sock.write_all(&[0x89, 2, b'h', b'i']).expect("ping");
            sock.write_all(&[0x80, part2.len() as u8]).expect("f2");
            sock.write_all(part2).expect("f2b");
            // Next frame: illegal non-zero RSV1 bit must be rejected!
            sock.write_all(&[0xC1, 2, b'{', b'}']).expect("bad rsv");
            sock.flush().expect("flush");

            let mut phdr = [0u8; 2];
            reader.read_exact(&mut phdr).expect("pong hdr");
            assert_eq!(phdr[0], 0x8A);
            let mut pmask = [0u8; 4];
            reader.read_exact(&mut pmask).expect("pmask");
            let mut pbody = [0u8; 2];
            reader.read_exact(&mut pbody).expect("pbody");
            pbody[0] ^= pmask[0];
            pbody[1] ^= pmask[1];
            assert_eq!(&pbody, b"hi");
        });

        let (mut ws_reader, ws_writer) =
            connect_loopback_ws(port, "/devtools/browser/test").expect("ws connect");
        let shared_writer = std::sync::Arc::new(std::sync::Mutex::new(Some(ws_writer)));
        shared_writer
            .lock()
            .expect("lock")
            .as_mut()
            .expect("writer")
            .send_text(br#"{"id":1}"#)
            .expect("send");

        let exact_cap = r#"{"id":1,"result":{"ok":true}}"#.len();
        let msg = read_next_ws_message(&mut ws_reader, exact_cap, |ping_data| {
            if let Ok(mut guard) = shared_writer.lock() {
                if let Some(ref mut w) = *guard {
                    let _ = w.send_pong(ping_data);
                }
            }
        })
        .expect("read msg")
        .expect("some msg");
        assert_eq!(
            String::from_utf8(msg).expect("utf8"),
            r#"{"id":1,"result":{"ok":true}}"#
        );

        let err = read_next_ws_message(&mut ws_reader, 1024, |_| {}).unwrap_err();
        assert!(err.contains("RSV"));
        srv.join().expect("server thread");
    }

    #[test]
    fn external_cdp_url_enforces_strict_loopback_and_blocks_ssrf() {
        assert_eq!(
            parse_loopback_cdp_endpoint("http://127.0.0.1:9222").expect("http ip"),
            LoopbackCdpEndpoint::HttpDiscovery { port: 9222 }
        );
        assert_eq!(
            parse_loopback_cdp_endpoint("http://localhost:9222/").expect("http localhost"),
            LoopbackCdpEndpoint::HttpDiscovery { port: 9222 }
        );
        assert_eq!(
            parse_loopback_cdp_endpoint("ws://127.0.0.1:9222/devtools/browser/abc-123")
                .expect("direct ws"),
            LoopbackCdpEndpoint::DirectWs {
                port: 9222,
                path: "/devtools/browser/abc-123".to_string(),
            }
        );

        for bad in [
            "http://169.254.169.254:80",
            "http://10.0.0.1:9222",
            "http://evil.com:9222",
            "http://127.0.0.1.evil.com:9222",
            "http://127.0.0.1@evil.com:9222",
            "ws://evil@127.0.0.1:9222/devtools/browser/x",
            "http://0.0.0.0:9222",
            "https://127.0.0.1:9222",
            "wss://127.0.0.1:9222/devtools/browser/x",
            "file:///etc/passwd",
            "http://127.0.0.1:0",
            "http://127.0.0.1",
            "http://127.0.0.1:9222/extra/path",
            "ws://127.0.0.1:9222/",
            "ws://127.0.0.1:9222/devtools/browser/x#frag",
        ] {
            assert!(
                parse_loopback_cdp_endpoint(bad).is_err(),
                "expected SSRF / invalid --cdp-url to be rejected: {bad}"
            );
        }
    }
}
