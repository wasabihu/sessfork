//! Built-in Chrome/Edge/Brave Native Messaging host + host registrar + MCP setup helper.
//!
//! Enables zero-Python, zero-injection, user-consented session syncing on Windows (solving
//! Chrome 127+ `v20` App-Bound Encryption and SQLite file locking cleanly via Chrome's own
//! `chrome.cookies` + `chrome.scripting` APIs), as well as an optional zero-Keychain-prompt
//! workflow on macOS and Linux.

use crate::storage::{
    load_storage_state_file, normalize_origin, LocalStorageItem, OriginState, PlaywrightCookie,
    StorageState,
};
use serde_json::{json, Value};
use std::collections::{BTreeSet, HashSet};
use std::env;
use std::fs::{self, OpenOptions};
use std::io::{self, ErrorKind, Read, Write};
use std::net::IpAddr;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

#[cfg(unix)]
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
#[cfg(unix)]
use std::os::unix::io::AsRawFd;

pub(crate) const NATIVE_HOST_NAME: &str = "com.wasabihu.sessfork";
/// Deterministic Chrome/Edge extension ID derived from the public RSA key in `extension/manifest.json`.
pub(crate) const DEFAULT_EXTENSION_ID: &str = "eipgnmlpcpmadpbignmifidnbjfmlphm";
const MAX_INBOUND_MSG_BYTES: usize = 8 * 1024 * 1024;
const MAX_OUTBOUND_MSG_BYTES: usize = 512 * 1024;
const MAX_COOKIES_PER_SYNC: usize = 2_000;
const MAX_LS_ITEMS_PER_ORIGIN: usize = 500;
const MAX_ORIGINS_PER_SYNC: usize = 32;

static TMP_COUNTER: AtomicU64 = AtomicU64::new(1);

const UNSAFE_PUBLIC_SUFFIXES: &[&str] = &[
    // Multi-label country/registry eTLDs (aligned with src/sessfork/chrome.py & extension/popup.js)
    "co.uk",
    "org.uk",
    "ac.uk",
    "gov.uk",
    "com.cn",
    "net.cn",
    "org.cn",
    "gov.cn",
    "edu.cn",
    "com.au",
    "net.au",
    "org.au",
    "edu.au",
    "gov.au",
    "co.jp",
    "ne.jp",
    "or.jp",
    "ac.jp",
    "go.jp",
    "co.kr",
    "com.br",
    "com.hk",
    "com.tw",
    "com.sg",
    "gov.sg",
    "co.in",
    "co.nz",
    "co.za",
    "co.id",
    "co.il",
    "com.mx",
    "com.ar",
    "com.my",
    "com.tr",
    "com.vn",
    // Multi-tenant shared hosting & cloud public suffixes
    "github.io",
    "gitlab.io",
    "vercel.app",
    "netlify.app",
    "pages.dev",
    "workers.dev",
    "web.app",
    "firebaseapp.com",
    "herokuapp.com",
    "onrender.com",
    "fly.dev",
    "railway.app",
    "appspot.com",
    "run.app",
    "azurewebsites.net",
    "cloudfront.net",
    "amazonaws.com",
    "s3.amazonaws.com",
    "googleusercontent.com",
    "myshopify.com",
    "notion.site",
    "hf.space",
    "streamlit.app",
    "supabase.co",
    "ngrok.io",
    "ngrok-free.app",
    "trycloudflare.com",
    "deno.dev",
    "replit.app",
    "replit.dev",
    "glitch.me",
    "surge.sh",
    "bitbucket.io",
    "azurestaticapps.net",
];

const GENERIC_SECOND_LEVEL_PREFIXES: &[&str] = &[
    "co", "com", "net", "org", "gov", "edu", "ac", "go", "or", "ne", "mil", "sch", "ltd", "plc",
    "me",
];

fn is_generic_second_level_cctld(host: &str) -> bool {
    let Some((first, second)) = host.split_once('.') else {
        return false;
    };
    !second.contains('.')
        && second.len() == 2
        && second.bytes().all(|b| b.is_ascii_lowercase())
        && GENERIC_SECOND_LEVEL_PREFIXES.contains(&first)
}

pub(crate) fn is_unsafe_public_suffix(host: &str) -> bool {
    UNSAFE_PUBLIC_SUFFIXES.contains(&host) || is_generic_second_level_cctld(host)
}

pub(crate) fn default_home_dir() -> PathBuf {
    if let Ok(override_home) = env::var("SESSFORK_HOME") {
        let trimmed = override_home.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    #[cfg(windows)]
    {
        if let Ok(h) = env::var("USERPROFILE") {
            let trimmed = h.trim();
            if !trimmed.is_empty() {
                return PathBuf::from(trimmed);
            }
        }
        if let Ok(h) = env::var("HOME") {
            let trimmed = h.trim();
            if !trimmed.is_empty() {
                return PathBuf::from(trimmed);
            }
        }
    }
    #[cfg(not(windows))]
    {
        if let Ok(h) = env::var("HOME") {
            let trimmed = h.trim();
            if !trimmed.is_empty() {
                return PathBuf::from(trimmed);
            }
        }
        if let Ok(h) = env::var("USERPROFILE") {
            let trimmed = h.trim();
            if !trimmed.is_empty() {
                return PathBuf::from(trimmed);
            }
        }
    }
    PathBuf::from(".")
}

/// Strip Windows extended-length verbatim path prefixes (`\\?\` / `\\?\UNC\`) produced by
/// `fs::canonicalize`, which break Chrome Native Messaging manifests and Node/Electron MCP configs.
pub(crate) fn strip_windows_verbatim_prefix(path: &Path) -> PathBuf {
    #[cfg(windows)]
    {
        let s = path.to_string_lossy();
        if let Some(unc) = s.strip_prefix(r"\\?\UNC\") {
            return PathBuf::from(format!(r"\\{unc}"));
        }
        if let Some(stripped) = s.strip_prefix(r"\\?\") {
            return PathBuf::from(stripped);
        }
    }
    path.to_path_buf()
}

fn canonicalize_clean(path: &Path) -> PathBuf {
    let resolved = fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    strip_windows_verbatim_prefix(&resolved)
}

pub(crate) fn default_state_paths() -> (PathBuf, PathBuf) {
    let base = default_home_dir().join(".sessfork");
    let state_path = base.join("state.json");
    let hot_reload_path = base.join("playwright-output").join(".sessfork-state.json");
    (state_path, hot_reload_path)
}

fn is_valid_rfc1123_hostname(host: &str) -> bool {
    if host.is_empty() || host.len() > 253 {
        return false;
    }
    if host == "localhost" || host.parse::<IpAddr>().is_ok() {
        return true;
    }
    if !host.contains('.') {
        return false;
    }
    host.split('.').all(|lbl| {
        !lbl.is_empty()
            && lbl.len() <= 63
            && !lbl.starts_with('-')
            && !lbl.ends_with('-')
            && lbl
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
    })
}

pub(crate) fn validate_domain(raw: &str) -> Result<String, String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() || trimmed.bytes().any(|b| b <= 0x20 || b == 0x7F) {
        return Err("Domain must be non-empty without whitespace or control chars".to_string());
    }
    let mut host = trimmed.to_ascii_lowercase();
    if let Some((scheme, rest)) = host.split_once("://") {
        if scheme != "http" && scheme != "https" {
            return Err(format!("Unsupported URL scheme '{scheme}'"));
        }
        host = rest.to_string();
    }
    let authority = host.split(&['/', '?', '#'][..]).next().unwrap_or("");
    if authority.contains('@') {
        return Err("Refusing domain/URL containing userinfo '@'".to_string());
    }
    let clean = authority
        .split(':')
        .next()
        .unwrap_or("")
        .trim_start_matches('.')
        .trim_end_matches('.')
        .to_string();

    if !is_valid_rfc1123_hostname(&clean) {
        return Err(format!("Invalid domain format '{clean}'"));
    }
    if clean != "localhost" && clean.parse::<IpAddr>().is_err() && is_unsafe_public_suffix(&clean) {
        return Err(format!(
            "Refusing overly broad public suffix '{clean}'; specify a concrete site domain"
        ));
    }
    Ok(clean)
}

fn sanitize_cookie_domain(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    if trimmed.is_empty()
        || trimmed.bytes().any(|b| b <= 0x20 || b == 0x7F)
        || trimmed.contains('/')
        || trimmed.contains('@')
        || trimmed.contains("..")
        || trimmed.ends_with('.')
    {
        return None;
    }
    let lower = trimmed.to_ascii_lowercase();
    let (leading_dot, bare) = if let Some(rest) = lower.strip_prefix('.') {
        (true, rest)
    } else {
        (false, lower.as_str())
    };
    if !is_valid_rfc1123_hostname(bare) {
        return None;
    }
    if leading_dot && (bare == "localhost" || bare.parse::<IpAddr>().is_ok()) {
        return None;
    }
    Some(if leading_dot {
        format!(".{bare}")
    } else {
        bare.to_string()
    })
}

fn host_matches_domain(host: &str, domain: &str) -> bool {
    let h = host
        .trim()
        .trim_start_matches('.')
        .trim_end_matches('.')
        .to_ascii_lowercase();
    let d = domain
        .trim()
        .trim_start_matches('.')
        .trim_end_matches('.')
        .to_ascii_lowercase();
    if h.is_empty() || d.is_empty() {
        return false;
    }
    // Never allow suffix matching on IP literals or localhost (e.g. `evil.127.0.0.1`).
    if d == "localhost" || d.parse::<IpAddr>().is_ok() {
        return h == d;
    }
    h == d || h.ends_with(&format!(".{d}"))
}

fn origin_host(origin: &str) -> Option<String> {
    let trimmed = origin.trim();
    if trimmed.is_empty() || trimmed.bytes().any(|b| b <= 0x20 || b == 0x7F) {
        return None;
    }
    let norm = normalize_origin(trimmed);
    let rest = norm
        .strip_prefix("https://")
        .or_else(|| norm.strip_prefix("http://"))?;
    let authority = rest.split(&['/', '?', '#'][..]).next()?;
    if authority.is_empty() || authority.contains('@') {
        return None;
    }
    let host = if let Some(bracketed) = authority.strip_prefix('[') {
        bracketed.split(']').next()?
    } else {
        authority.split(':').next()?
    };
    let lower = host.to_ascii_lowercase();
    if is_valid_rfc1123_hostname(&lower) {
        Some(lower)
    } else {
        None
    }
}

fn normalize_same_site(raw: Option<&str>) -> Option<String> {
    match raw?.trim().to_ascii_lowercase().as_str() {
        "strict" => Some("Strict".to_string()),
        "lax" => Some("Lax".to_string()),
        "none" | "no_restriction" => Some("None".to_string()),
        _ => None,
    }
}

fn normalize_parent_dir(target: &Path) -> &Path {
    match target.parent() {
        Some(p) if !p.as_os_str().is_empty() => p,
        _ => Path::new("."),
    }
}

fn ensure_dir_no_symlink(dir: &Path, enforce_private: bool) -> Result<(), String> {
    if fs::symlink_metadata(dir)
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err(format!("Refusing symlinked directory '{}'", dir.display()));
    }
    #[cfg(unix)]
    {
        if !dir.exists() {
            let mut builder = fs::DirBuilder::new();
            builder.recursive(true);
            if enforce_private {
                builder.mode(0o700);
            }
            builder
                .create(dir)
                .map_err(|e| format!("Failed to create '{}': {e}", dir.display()))?;
        }
        let meta = fs::symlink_metadata(dir)
            .map_err(|e| format!("Failed to stat '{}': {e}", dir.display()))?;
        if meta.file_type().is_symlink() || !meta.is_dir() {
            return Err(format!(
                "Path '{}' is not a regular directory",
                dir.display()
            ));
        }
        if enforce_private {
            fs::set_permissions(dir, fs::Permissions::from_mode(0o700))
                .map_err(|e| format!("Failed to enforce 0700 on '{}': {e}", dir.display()))?;
        }
    }
    #[cfg(not(unix))]
    {
        let _ = enforce_private;
        fs::create_dir_all(dir)
            .map_err(|e| format!("Failed to create '{}': {e}", dir.display()))?;
    }
    Ok(())
}

/// Atomically write `target` using an unpredictable `create_new(true)` sibling temp file.
/// When `enforce_private` is true (session credential files), locks directory to `0700` and
/// file to `0600` on Unix; when false (public config files like `mcp.json` or Native Messaging
/// manifests), preserves existing target permissions without mutating parent directory modes.
pub(crate) fn write_file_atomic(
    target: &Path,
    content: &[u8],
    enforce_private: bool,
) -> Result<(), String> {
    let parent = normalize_parent_dir(target);
    ensure_dir_no_symlink(parent, enforce_private)?;

    if fs::symlink_metadata(target)
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err(format!(
            "Refusing to overwrite symlink '{}'",
            target.display()
        ));
    }
    let existing_perms = if enforce_private {
        None
    } else {
        fs::metadata(target).ok().map(|m| m.permissions())
    };

    let file_name = target
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("state.json");
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let seq = TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let tmp_path = parent.join(format!(
        ".{file_name}.tmp.{}.{nanos}.{seq}",
        std::process::id()
    ));

    let mut opts = OpenOptions::new();
    opts.write(true).create_new(true);
    #[cfg(unix)]
    {
        if enforce_private {
            opts.mode(0o600);
        }
        opts.custom_flags(libc::O_NOFOLLOW);
    }
    let mut file = opts
        .open(&tmp_path)
        .map_err(|e| format!("Failed to create temp file '{}': {e}", tmp_path.display()))?;
    #[cfg(unix)]
    if enforce_private {
        file.set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|e| format!("Failed to enforce 0600 on '{}': {e}", tmp_path.display()))?;
    }
    if let Some(perms) = existing_perms {
        let _ = file.set_permissions(perms);
    }
    if let Err(e) = file.write_all(content).and_then(|()| file.flush()) {
        let _ = fs::remove_file(&tmp_path);
        return Err(format!("Failed to write '{}': {e}", tmp_path.display()));
    }
    drop(file);

    if let Err(e) = fs::rename(&tmp_path, target) {
        let _ = fs::remove_file(&tmp_path);
        return Err(format!(
            "Failed to atomically replace '{}': {e}",
            target.display()
        ));
    }
    Ok(())
}

struct StateLockGuard {
    #[cfg(unix)]
    file: fs::File,
}

impl StateLockGuard {
    fn acquire(state_path: &Path) -> Result<Self, String> {
        let parent = normalize_parent_dir(state_path);
        ensure_dir_no_symlink(parent, true)?;
        #[cfg(unix)]
        {
            let fname = state_path
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("state.json");
            let lock_path = parent.join(format!(".{fname}.lock"));
            if fs::symlink_metadata(&lock_path)
                .map(|m| m.file_type().is_symlink())
                .unwrap_or(false)
            {
                return Err(format!(
                    "Refusing symlinked lock file '{}'",
                    lock_path.display()
                ));
            }
            let file = OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW)
                .open(&lock_path)
                .map_err(|e| format!("Failed to open state lock '{}': {e}", lock_path.display()))?;
            let _ = file.set_permissions(fs::Permissions::from_mode(0o600));
            // SAFETY: `file.as_raw_fd()` is a valid open descriptor owned by `file`.
            if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } != 0 {
                return Err(format!(
                    "Failed to acquire state lock: {}",
                    io::Error::last_os_error()
                ));
            }
            Ok(Self { file })
        }
        #[cfg(not(unix))]
        {
            Ok(Self {})
        }
    }
}

#[cfg(unix)]
impl Drop for StateLockGuard {
    fn drop(&mut self) {
        // SAFETY: Unlocking the open file descriptor owned by `self.file` before closing.
        let _ = unsafe { libc::flock(self.file.as_raw_fd(), libc::LOCK_UN) };
    }
}

/// Strictly load existing `state.json`: returns empty state ONLY when the file does not exist
/// (`NotFound`). Propagates I/O or JSON parse errors so a transient read failure never silently
/// overwrites and wipes other synced domains!
fn load_existing_state_strict(state_path: &Path) -> Result<StorageState, String> {
    match fs::symlink_metadata(state_path) {
        Err(e) if e.kind() == ErrorKind::NotFound => Ok(StorageState::default()),
        Err(e) => Err(format!("Cannot inspect existing state file: {e}")),
        Ok(meta) => {
            if meta.file_type().is_symlink() {
                return Err(format!(
                    "Refusing to read symlinked state file '{}'",
                    state_path.display()
                ));
            }
            load_storage_state_file(state_path)
        }
    }
}

fn save_state_and_mirror(
    state: &StorageState,
    state_path: &Path,
    hot_reload_path: Option<&Path>,
) -> Result<(), String> {
    let encoded = serde_json::to_vec_pretty(state)
        .map_err(|e| format!("Failed to serialize storage state: {e}"))?;
    write_file_atomic(state_path, &encoded, true)?;
    if let Some(mirror) = hot_reload_path {
        if mirror != state_path {
            write_file_atomic(mirror, &encoded, true)?;
        }
    }
    Ok(())
}

fn summarize_domains(state: &StorageState) -> Vec<String> {
    let mut set = BTreeSet::new();
    for c in &state.cookies {
        let d = c.domain.trim().trim_start_matches('.').to_ascii_lowercase();
        if !d.is_empty() {
            set.insert(d);
        }
    }
    for o in &state.origins {
        if let Some(h) = origin_host(&o.origin) {
            set.insert(h);
        }
    }
    set.into_iter().collect()
}

fn summarize_domain_stats(state: &StorageState) -> Vec<Value> {
    use std::collections::BTreeMap;

    #[derive(Default)]
    struct Counts {
        cookies: usize,
        http_only: usize,
        origins: usize,
        ls_keys: usize,
    }

    let mut map: BTreeMap<String, Counts> = BTreeMap::new();
    for c in &state.cookies {
        let d = c.domain.trim().trim_start_matches('.').to_ascii_lowercase();
        if !d.is_empty() {
            let entry = map.entry(d).or_default();
            entry.cookies += 1;
            if c.http_only {
                entry.http_only += 1;
            }
        }
    }
    for o in &state.origins {
        if let Some(h) = origin_host(&o.origin) {
            let entry = map.entry(h).or_default();
            entry.origins += 1;
            entry.ls_keys += o.local_storage.len();
        }
    }

    map.into_iter()
        .map(|(domain, c)| {
            json!({
                "domain": domain,
                "cookies": c.cookies,
                "httpOnlyCookies": c.http_only,
                "origins": c.origins,
                "localStorageKeys": c.ls_keys,
            })
        })
        .collect()
}

/// Handle one decoded Chrome Native Messaging request JSON without ever echoing secret values.
pub(crate) fn handle_native_message(
    msg: &Value,
    state_path: &Path,
    hot_reload_path: Option<&Path>,
) -> Value {
    let action = msg.get("action").and_then(Value::as_str).unwrap_or("");
    match action {
        "ping" => json!({
            "ok": true,
            "host": NATIVE_HOST_NAME,
            "version": env!("CARGO_PKG_VERSION"),
            "statePath": state_path.display().to_string(),
            "hotReloadPath": hot_reload_path.map(|p| p.display().to_string()),
        }),
        "status" => match load_existing_state_strict(state_path) {
            Ok(state) => {
                let domains = summarize_domains(&state);
                let domain_stats = summarize_domain_stats(&state);
                json!({
                    "ok": true,
                    "version": env!("CARGO_PKG_VERSION"),
                    "cookieCount": state.cookies.len(),
                    "originCount": state.origins.len(),
                    "domains": domains,
                    "domainStats": domain_stats,
                    "statePath": state_path.display().to_string(),
                })
            }
            Err(e) => json!({ "ok": false, "error": e }),
        },
        "sync" => {
            let raw_domain = msg.get("domain").and_then(Value::as_str).unwrap_or("");
            let domain = match validate_domain(raw_domain) {
                Ok(d) => d,
                Err(e) => return json!({ "ok": false, "error": e }),
            };

            let mut incoming_cookies: Vec<PlaywrightCookie> = Vec::new();
            if let Some(arr) = msg.get("cookies").and_then(Value::as_array) {
                for item in arr.iter().take(MAX_COOKIES_PER_SYNC) {
                    let name = item
                        .get("name")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .trim();
                    let value = item.get("value").and_then(Value::as_str).unwrap_or("");
                    let Some(clean_cookie_domain) = item
                        .get("domain")
                        .and_then(Value::as_str)
                        .and_then(sanitize_cookie_domain)
                    else {
                        continue;
                    };
                    if name.is_empty() || !host_matches_domain(&clean_cookie_domain, &domain) {
                        continue;
                    }
                    let path = item
                        .get("path")
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty() && s.starts_with('/'))
                        .unwrap_or("/")
                        .to_string();
                    let expires = item
                        .get("expires")
                        .or_else(|| item.get("expirationDate"))
                        .and_then(Value::as_f64)
                        .filter(|v| v.is_finite() && *v > 0.0);
                    let http_only = item
                        .get("httpOnly")
                        .and_then(Value::as_bool)
                        .unwrap_or(false);
                    let secure = item.get("secure").and_then(Value::as_bool).unwrap_or(false);
                    let same_site =
                        normalize_same_site(item.get("sameSite").and_then(Value::as_str));

                    incoming_cookies.push(PlaywrightCookie {
                        name: name.to_string(),
                        value: value.to_string(),
                        domain: clean_cookie_domain,
                        path,
                        expires,
                        http_only,
                        secure,
                        same_site,
                    });
                }
            }

            let mut incoming_origins: Vec<OriginState> = Vec::new();
            let mut incoming_origin_set: HashSet<String> = HashSet::new();
            if let Some(arr) = msg.get("origins").and_then(Value::as_array) {
                for item in arr.iter().take(MAX_ORIGINS_PER_SYNC) {
                    let raw_origin = item.get("origin").and_then(Value::as_str).unwrap_or("");
                    let Some(host) = origin_host(raw_origin) else {
                        continue;
                    };
                    if !host_matches_domain(&host, &domain) {
                        continue;
                    }
                    let norm_origin = normalize_origin(raw_origin);
                    let mut ls_items = Vec::new();
                    if let Some(entries) = item.get("localStorage").and_then(Value::as_array) {
                        for kv in entries.iter().take(MAX_LS_ITEMS_PER_ORIGIN) {
                            let Some(k) = kv.get("name").and_then(Value::as_str) else {
                                continue;
                            };
                            let Some(v) = kv.get("value").and_then(Value::as_str) else {
                                continue;
                            };
                            if !k.is_empty() {
                                ls_items.push(LocalStorageItem {
                                    name: k.to_string(),
                                    value: v.to_string(),
                                });
                            }
                        }
                    }
                    incoming_origin_set.insert(norm_origin.clone());
                    if !ls_items.is_empty() {
                        incoming_origins.push(OriginState {
                            origin: norm_origin,
                            local_storage: ls_items,
                        });
                    }
                }
            }

            let cookies_synced = incoming_cookies.len();
            let origins_synced = incoming_origins.len();
            let ls_keys_synced: usize =
                incoming_origins.iter().map(|o| o.local_storage.len()).sum();

            let _lock = match StateLockGuard::acquire(state_path) {
                Ok(g) => g,
                Err(e) => return json!({ "ok": false, "error": e }),
            };
            let mut merged = match load_existing_state_strict(state_path) {
                Ok(s) => s,
                Err(e) => return json!({ "ok": false, "error": e }),
            };

            // Replace targeted domain's cookies, and replace ONLY the exact incoming origin(s)'
            // localStorage entries so sibling subdomains' saved localStorage are never wiped.
            merged
                .cookies
                .retain(|c| !host_matches_domain(&c.domain, &domain));
            merged.cookies.extend(incoming_cookies);
            if !incoming_origin_set.is_empty() {
                merged
                    .origins
                    .retain(|o| !incoming_origin_set.contains(&normalize_origin(&o.origin)));
                merged.origins.extend(incoming_origins);
            }

            if let Err(e) = save_state_and_mirror(&merged, state_path, hot_reload_path) {
                return json!({ "ok": false, "error": e });
            }

            json!({
                "ok": true,
                "domain": domain,
                "cookiesSynced": cookies_synced,
                "originsSynced": origins_synced,
                "localStorageKeysSynced": ls_keys_synced,
                "totalCookies": merged.cookies.len(),
                "totalOrigins": merged.origins.len(),
                "hotReloadPath": hot_reload_path.map(|p| p.display().to_string()),
            })
        }
        "clear" => {
            let clear_all = msg.get("all").and_then(Value::as_bool).unwrap_or(false);
            let target_domain = if clear_all {
                None
            } else {
                let raw_domain = msg.get("domain").and_then(Value::as_str).unwrap_or("");
                match validate_domain(raw_domain) {
                    Ok(d) => Some(d),
                    Err(e) => return json!({ "ok": false, "error": e }),
                }
            };

            let _lock = match StateLockGuard::acquire(state_path) {
                Ok(g) => g,
                Err(e) => return json!({ "ok": false, "error": e }),
            };
            let mut state = match load_existing_state_strict(state_path) {
                Ok(s) => s,
                Err(e) => return json!({ "ok": false, "error": e }),
            };

            if let Some(domain) = target_domain {
                state
                    .cookies
                    .retain(|c| !host_matches_domain(&c.domain, &domain));
                state.origins.retain(|o| {
                    origin_host(&o.origin)
                        .map(|h| !host_matches_domain(&h, &domain))
                        .unwrap_or(true)
                });
            } else {
                state.cookies.clear();
                state.origins.clear();
            }
            if let Err(e) = save_state_and_mirror(&state, state_path, hot_reload_path) {
                return json!({ "ok": false, "error": e });
            }
            json!({
                "ok": true,
                "totalCookies": state.cookies.len(),
                "totalOrigins": state.origins.len(),
            })
        }
        other => json!({
            "ok": false,
            "error": format!("Unsupported native-host action '{other}'")
        }),
    }
}

pub(crate) fn read_native_frame<R: Read>(reader: &mut R) -> io::Result<Option<Vec<u8>>> {
    let mut len_buf = [0u8; 4];
    match reader.read_exact(&mut len_buf) {
        Ok(()) => {}
        Err(e) if e.kind() == ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(e),
    }
    let len = u32::from_ne_bytes(len_buf) as usize;
    if len == 0 || len > MAX_INBOUND_MSG_BYTES {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            format!("Native Messaging frame length {len} out of bounds"),
        ));
    }
    let mut payload = vec![0u8; len];
    reader.read_exact(&mut payload)?;
    Ok(Some(payload))
}

pub(crate) fn write_native_frame<W: Write>(writer: &mut W, resp: &Value) -> io::Result<()> {
    let encoded = serde_json::to_vec(resp).map_err(|e| {
        io::Error::new(
            ErrorKind::InvalidData,
            format!("Failed to encode native reply: {e}"),
        )
    })?;
    if encoded.len() > MAX_OUTBOUND_MSG_BYTES {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "Native Messaging reply exceeded 512 KiB cap",
        ));
    }
    let len_bytes = (encoded.len() as u32).to_ne_bytes();
    writer.write_all(&len_bytes)?;
    writer.write_all(&encoded)?;
    writer.flush()
}

pub(crate) fn serve_native_io<R: Read, W: Write>(
    reader: &mut R,
    writer: &mut W,
    state_path: &Path,
    hot_reload_path: Option<&Path>,
) {
    loop {
        match read_native_frame(reader) {
            Ok(Some(payload)) => {
                let resp = match serde_json::from_slice::<Value>(&payload) {
                    Ok(v) => handle_native_message(&v, state_path, hot_reload_path),
                    Err(e) => json!({ "ok": false, "error": format!("Invalid JSON: {e}") }),
                };
                if write_native_frame(writer, &resp).is_err() {
                    break;
                }
            }
            Ok(None) => break,
            Err(e) => {
                let _ = write_native_frame(
                    writer,
                    &json!({ "ok": false, "error": format!("Native framing error: {e}") }),
                );
                break;
            }
        }
    }
}

pub(crate) fn run_stdio_native_host(args: &[String]) -> i32 {
    let (mut state_path, mut hot_reload_path) = default_state_paths();
    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        if a == "-h" || a == "--help" {
            println!(
                "USAGE:\n  sessfork-mcp native-host [--state-path <PATH>] [--output-dir <PATH>]\n\n\
                 Speaks Chrome/Edge Native Messaging (4-byte native-endian length-prefixed JSON on stdin/stdout).\n\
                 Auto-activated when launched by Chrome/Edge with a `chrome-extension://<id>/` argument."
            );
            return 0;
        } else if let Some(val) = a.strip_prefix("--state-path=") {
            state_path = PathBuf::from(val);
        } else if a == "--state-path" && i + 1 < args.len() {
            i += 1;
            state_path = PathBuf::from(&args[i]);
        } else if let Some(val) = a.strip_prefix("--output-dir=") {
            hot_reload_path = PathBuf::from(val).join(".sessfork-state.json");
        } else if a == "--output-dir" && i + 1 < args.len() {
            i += 1;
            hot_reload_path = PathBuf::from(&args[i]).join(".sessfork-state.json");
        }
        i += 1;
    }

    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut reader = stdin.lock();
    let mut writer = stdout.lock();
    serve_native_io(
        &mut reader,
        &mut writer,
        &state_path,
        Some(&hot_reload_path),
    );
    0
}

pub(crate) fn validate_chrome_extension_id(raw: &str) -> Result<String, String> {
    let id = raw.trim().to_ascii_lowercase();
    if id.len() != 32 || !id.bytes().all(|b| (b'a'..=b'p').contains(&b)) {
        return Err(format!(
            "Invalid Chrome extension ID '{raw}': expected 32 lowercase letters in 'a'..'p'"
        ));
    }
    Ok(id)
}

pub(crate) fn build_native_host_manifest(host_bin: &Path, extension_id: &str) -> Value {
    json!({
        "name": NATIVE_HOST_NAME,
        "description": "sessfork native session bridge (user-consented Cookie + LocalStorage sync for headless AI agents)",
        "path": host_bin.display().to_string(),
        "type": "stdio",
        "allowed_origins": [
            format!("chrome-extension://{extension_id}/")
        ]
    })
}

pub(crate) fn run_register_host(args: &[String]) -> i32 {
    let mut ext_id: Option<String> = None;
    let mut browser = "all".to_string();
    let mut host_bin_arg: Option<PathBuf> = None;

    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        if a == "-h" || a == "--help" {
            println!(
                "USAGE:\n  sessfork-mcp register-host [--extension-id <32-CHAR-ID>] [--browser chrome|edge|brave|chromium|all] [--host-bin <PATH>]\n\n\
                 Defaults --extension-id to the official sessfork companion extension ID ({DEFAULT_EXTENSION_ID})."
            );
            return 0;
        } else if let Some(val) = a.strip_prefix("--extension-id=") {
            ext_id = Some(val.to_string());
        } else if a == "--extension-id" && i + 1 < args.len() {
            i += 1;
            ext_id = Some(args[i].clone());
        } else if let Some(val) = a.strip_prefix("--browser=") {
            browser = val.to_ascii_lowercase();
        } else if a == "--browser" && i + 1 < args.len() {
            i += 1;
            browser = args[i].to_ascii_lowercase();
        } else if let Some(val) = a.strip_prefix("--host-bin=") {
            host_bin_arg = Some(PathBuf::from(val));
        } else if a == "--host-bin" && i + 1 < args.len() {
            i += 1;
            host_bin_arg = Some(PathBuf::from(&args[i]));
        } else {
            eprintln!("sessfork-mcp register-host: unknown argument '{a}'");
            return 2;
        }
        i += 1;
    }

    let raw_id = ext_id.unwrap_or_else(|| DEFAULT_EXTENSION_ID.to_string());
    let clean_id = match validate_chrome_extension_id(&raw_id) {
        Ok(id) => id,
        Err(e) => {
            eprintln!("sessfork-mcp register-host error: {e}");
            return 2;
        }
    };

    let host_bin = match host_bin_arg.or_else(|| env::current_exe().ok()) {
        Some(p) => canonicalize_clean(&p),
        None => {
            eprintln!("sessfork-mcp register-host: could not resolve host executable path");
            return 1;
        }
    };
    let manifest = build_native_host_manifest(&host_bin, &clean_id);
    let rendered = match serde_json::to_string_pretty(&manifest) {
        Ok(s) => format!("{s}\n"),
        Err(e) => {
            eprintln!("Failed to serialize manifest: {e}");
            return 1;
        }
    };

    match register_manifest_for_os(&browser, &rendered) {
        Ok(targets) => {
            for t in targets {
                println!("Registered {NATIVE_HOST_NAME} -> {t}");
            }
            0
        }
        Err(e) => {
            eprintln!("sessfork-mcp register-host error: {e}");
            1
        }
    }
}

#[cfg(target_os = "macos")]
fn register_manifest_for_os(browser: &str, rendered: &str) -> Result<Vec<String>, String> {
    let app_support = default_home_dir()
        .join("Library")
        .join("Application Support");
    let candidates: &[(&str, PathBuf)] = &[
        (
            "chrome",
            app_support.join("Google/Chrome/NativeMessagingHosts"),
        ),
        (
            "edge",
            app_support.join("Microsoft Edge/NativeMessagingHosts"),
        ),
        (
            "brave",
            app_support.join("BraveSoftware/Brave-Browser/NativeMessagingHosts"),
        ),
        (
            "chromium",
            app_support.join("Chromium/NativeMessagingHosts"),
        ),
    ];
    write_unix_manifests(browser, candidates, rendered)
}

#[cfg(all(unix, not(target_os = "macos")))]
fn register_manifest_for_os(browser: &str, rendered: &str) -> Result<Vec<String>, String> {
    let cfg = env::var("XDG_CONFIG_HOME")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| default_home_dir().join(".config"));
    let candidates: &[(&str, PathBuf)] = &[
        ("chrome", cfg.join("google-chrome/NativeMessagingHosts")),
        ("edge", cfg.join("microsoft-edge/NativeMessagingHosts")),
        (
            "brave",
            cfg.join("BraveSoftware/Brave-Browser/NativeMessagingHosts"),
        ),
        ("chromium", cfg.join("chromium/NativeMessagingHosts")),
    ];
    write_unix_manifests(browser, candidates, rendered)
}

#[cfg(unix)]
fn write_unix_manifests(
    browser: &str,
    candidates: &[(&str, PathBuf)],
    rendered: &str,
) -> Result<Vec<String>, String> {
    let mut written = Vec::new();
    for (name, dir) in candidates {
        let selected = if browser == "all" {
            *name == "chrome" || dir.parent().is_some_and(Path::is_dir)
        } else {
            *name == browser
        };
        if !selected {
            continue;
        }
        let target = dir.join(format!("{NATIVE_HOST_NAME}.json"));
        write_file_atomic(&target, rendered.as_bytes(), false)?;
        written.push(target.display().to_string());
    }
    if written.is_empty() {
        return Err(format!(
            "Unknown browser '{browser}' (expected chrome, edge, brave, chromium, or all)"
        ));
    }
    Ok(written)
}

#[cfg(windows)]
fn register_manifest_for_os(browser: &str, rendered: &str) -> Result<Vec<String>, String> {
    use std::os::windows::process::CommandExt;
    use std::process::Command;

    let manifest_dir = default_home_dir().join(".sessfork").join("native-host");
    let manifest_path = manifest_dir.join(format!("{NATIVE_HOST_NAME}.json"));
    write_file_atomic(&manifest_path, rendered.as_bytes(), false)?;
    let clean_manifest = canonicalize_clean(&manifest_path);
    let manifest_str = clean_manifest.display().to_string();

    // Always invoke qualified `%SystemRoot%\System32\reg.exe` so PATH/CWD cannot hijack reg.exe.
    let system_root = env::var("SystemRoot")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| r"C:\Windows".to_string());
    let reg_exe = Path::new(&system_root).join("System32").join("reg.exe");

    let reg_keys: &[(&str, &str)] = &[
        (
            "chrome",
            r"HKCU\Software\Google\Chrome\NativeMessagingHosts\com.wasabihu.sessfork",
        ),
        (
            "edge",
            r"HKCU\Software\Microsoft\Edge\NativeMessagingHosts\com.wasabihu.sessfork",
        ),
        (
            "brave",
            r"HKCU\Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\com.wasabihu.sessfork",
        ),
        (
            "chromium",
            r"HKCU\Software\Chromium\NativeMessagingHosts\com.wasabihu.sessfork",
        ),
    ];

    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    let mut written = Vec::new();
    for (name, key) in reg_keys {
        if browser != "all" && *name != browser {
            continue;
        }
        let output = Command::new(&reg_exe)
            .args(["add", key, "/ve", "/t", "REG_SZ", "/d", &manifest_str, "/f"])
            .creation_flags(CREATE_NO_WINDOW)
            .output()
            .map_err(|e| format!("Failed to run {} for {name}: {e}", reg_exe.display()))?;
        if !output.status.success() {
            let detail = String::from_utf8_lossy(&output.stderr);
            return Err(format!(
                "reg.exe failed for {key} with status {} ({})",
                output.status,
                detail.trim()
            ));
        }
        written.push(format!("{key} -> {manifest_str}"));
    }
    if written.is_empty() {
        return Err(format!(
            "Unknown browser '{browser}' (expected chrome, edge, brave, chromium, or all)"
        ));
    }
    Ok(written)
}

/// Zero-Python MCP client wiring helper (`sessfork-mcp setup --mcp-file <PATH> [--name sessfork-moli]`).
pub(crate) fn run_setup_mcp(args: &[String]) -> i32 {
    let mut mcp_file: Option<PathBuf> = None;
    let mut server_name = "sessfork-moli".to_string();
    let mut moli_arg: Option<PathBuf> = None;
    let (mut state_path, _) = default_state_paths();
    let mut output_dir = default_home_dir()
        .join(".sessfork")
        .join("playwright-output");

    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        if a == "-h" || a == "--help" {
            println!(
                "USAGE:\n  sessfork-mcp setup --mcp-file <PATH> [--name sessfork-moli] [--moli <PATH>] [--storage-state <PATH>] [--output-dir <PATH>]"
            );
            return 0;
        } else if let Some(val) = a.strip_prefix("--mcp-file=") {
            mcp_file = Some(PathBuf::from(val));
        } else if a == "--mcp-file" && i + 1 < args.len() {
            i += 1;
            mcp_file = Some(PathBuf::from(&args[i]));
        } else if let Some(val) = a.strip_prefix("--name=") {
            server_name = val.to_string();
        } else if a == "--name" && i + 1 < args.len() {
            i += 1;
            server_name = args[i].clone();
        } else if let Some(val) = a.strip_prefix("--moli=") {
            moli_arg = Some(PathBuf::from(val));
        } else if a == "--moli" && i + 1 < args.len() {
            i += 1;
            moli_arg = Some(PathBuf::from(&args[i]));
        } else if let Some(val) = a.strip_prefix("--storage-state=") {
            state_path = PathBuf::from(val);
        } else if a == "--storage-state" && i + 1 < args.len() {
            i += 1;
            state_path = PathBuf::from(&args[i]);
        } else if let Some(val) = a.strip_prefix("--output-dir=") {
            output_dir = PathBuf::from(val);
        } else if a == "--output-dir" && i + 1 < args.len() {
            i += 1;
            output_dir = PathBuf::from(&args[i]);
        } else {
            eprintln!("sessfork-mcp setup: unknown argument '{a}'");
            return 2;
        }
        i += 1;
    }

    let Some(target_file) = mcp_file else {
        eprintln!("sessfork-mcp setup: missing required --mcp-file <PATH>");
        return 2;
    };
    let self_bin = env::current_exe()
        .ok()
        .map(|p| canonicalize_clean(&p))
        .unwrap_or_else(|| PathBuf::from("sessfork-mcp"));

    let mut srv_args = vec![
        format!("--storage-state={}", state_path.display()),
        format!("--output-dir={}", output_dir.display()),
    ];
    if let Some(moli) = moli_arg {
        srv_args.insert(0, format!("--moli={}", canonicalize_clean(&moli).display()));
    }

    let mut root: Value = if target_file.is_file() {
        match fs::read_to_string(&target_file)
            .map_err(|e| format!("Failed to read '{}': {e}", target_file.display()))
            .and_then(|s| {
                serde_json::from_str(&s)
                    .map_err(|e| format!("Invalid JSON in '{}': {e}", target_file.display()))
            }) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("sessfork-mcp setup error: {e}");
                return 1;
            }
        }
    } else {
        json!({})
    };

    let Some(obj) = root.as_object_mut() else {
        eprintln!(
            "sessfork-mcp setup error: expected JSON object in '{}'",
            target_file.display()
        );
        return 1;
    };
    let servers = obj.entry("mcpServers").or_insert_with(|| json!({}));
    let Some(srv_map) = servers.as_object_mut() else {
        eprintln!("sessfork-mcp setup error: 'mcpServers' is not an object");
        return 1;
    };
    srv_map.insert(
        server_name.clone(),
        json!({
            "command": self_bin.display().to_string(),
            "args": srv_args,
        }),
    );

    let rendered = match serde_json::to_string_pretty(&root) {
        Ok(s) => format!("{s}\n"),
        Err(e) => {
            eprintln!("sessfork-mcp setup error: {e}");
            return 1;
        }
    };
    if let Err(e) = write_file_atomic(&target_file, rendered.as_bytes(), false) {
        eprintln!("sessfork-mcp setup error: {e}");
        return 1;
    }
    println!(
        "Wired MCP server '{server_name}' -> {}",
        target_file.display()
    );
    0
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn tmp_dir(tag: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let dir = env::temp_dir().join(format!("sessfork_nh_test_{tag}_{nanos}"));
        let _ = fs::create_dir_all(&dir);
        dir
    }

    #[test]
    fn validates_domains_userinfo_public_suffixes_and_ip_isolation() {
        assert_eq!(
            validate_domain("https://github.com/wasabihu").unwrap(),
            "github.com"
        );
        assert_eq!(validate_domain("localhost").unwrap(), "localhost");
        assert_eq!(validate_domain("127.0.0.1").unwrap(), "127.0.0.1");
        assert!(validate_domain("com.cn").is_err());
        assert!(validate_domain("co.nz").is_err());
        assert!(validate_domain("co.za").is_err());
        assert!(validate_domain("gov.au").is_err());
        assert!(validate_domain("github.io").is_err());
        assert!(validate_domain("*.github.com").is_err());
        assert!(validate_domain("intranet").is_err());
        assert!(validate_domain("https://github.com@evil.com").is_err());

        // Origin userinfo confusion & IP suffix bypass must be rejected.
        assert_eq!(origin_host("https://github.com:443@evil.com"), None);
        assert_eq!(origin_host("https://evil.com@sub.github.com"), None);
        assert_eq!(
            origin_host("https://app.github.com:8443"),
            Some("app.github.com".to_string())
        );
        assert!(!host_matches_domain("evil.127.0.0.1", "127.0.0.1"));
        assert!(host_matches_domain("127.0.0.1", "127.0.0.1"));
        assert!(!host_matches_domain("evil.localhost", "localhost"));

        // Cookie domain sanitizer rejects malformed injection strings.
        assert_eq!(
            sanitize_cookie_domain(".GitHub.COM"),
            Some(".github.com".to_string())
        );
        assert_eq!(sanitize_cookie_domain("evil.com/.github.com"), None);
        assert_eq!(sanitize_cookie_domain("a\r\n.github.com"), None);
        assert_eq!(sanitize_cookie_domain("..github.com"), None);
    }

    #[test]
    fn validates_chrome_extension_ids_strictly() {
        assert!(validate_chrome_extension_id(DEFAULT_EXTENSION_ID).is_ok());
        assert!(validate_chrome_extension_id("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa").is_ok());
        assert!(validate_chrome_extension_id("abcdefghijklmnopabcdefghijklmnop").is_ok());
        assert!(validate_chrome_extension_id("short").is_err());
        assert!(validate_chrome_extension_id("zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz").is_err());
    }

    #[test]
    fn native_messaging_sync_preserves_sibling_subdomains_rejects_corrupt_state_and_mirrors() {
        let dir = tmp_dir("sync");
        let state_path = dir.join("state.json");
        let mirror_path = dir.join("playwright-output").join(".sessfork-state.json");

        // 1. Sync app.github.com + github.com (includes malicious foreign cookie & userinfo origin!).
        let req1 = json!({
            "action": "sync",
            "domain": "github.com",
            "cookies": [
                { "name": "user_session", "value": "gh_tok", "domain": ".github.com", "httpOnly": true, "secure": true, "sameSite": "lax" },
                { "name": "stolen", "value": "bad", "domain": ".evil.com" },
                { "name": "injected", "value": "bad", "domain": "evil.com/.github.com" }
            ],
            "origins": [
                { "origin": "https://app.github.com", "localStorage": [{ "name": "app_jwt", "value": "secret_app" }] },
                { "origin": "https://github.com:443@evil.com", "localStorage": [{ "name": "spoof", "value": "bad" }] }
            ]
        });
        // 2. Re-sync github.com from https://www.github.com -> MUST preserve https://app.github.com localStorage!
        let req2 = json!({
            "action": "sync",
            "domain": "github.com",
            "cookies": [
                { "name": "user_session", "value": "gh_tok_v2", "domain": ".github.com", "sameSite": "no_restriction" }
            ],
            "origins": [
                { "origin": "https://www.github.com", "localStorage": [{ "name": "theme", "value": "dark" }] }
            ]
        });
        let req3 = json!({ "action": "status" });

        let mut wire = Vec::new();
        for r in [&req1, &req2, &req3] {
            write_native_frame(&mut wire, r).expect("encode req");
        }
        let mut reader = Cursor::new(wire);
        let mut out_buf = Vec::new();
        serve_native_io(&mut reader, &mut out_buf, &state_path, Some(&mirror_path));

        let mut resp_reader = Cursor::new(out_buf);
        let r1: Value =
            serde_json::from_slice(&read_native_frame(&mut resp_reader).unwrap().unwrap()).unwrap();
        let r2: Value =
            serde_json::from_slice(&read_native_frame(&mut resp_reader).unwrap().unwrap()).unwrap();
        let r3: Value =
            serde_json::from_slice(&read_native_frame(&mut resp_reader).unwrap().unwrap()).unwrap();

        assert_eq!(r1["ok"], true);
        assert_eq!(r1["cookiesSynced"], 1);
        assert_eq!(r1["originsSynced"], 1);
        assert!(!r1.to_string().contains("gh_tok"));

        assert_eq!(r2["ok"], true);
        assert_eq!(r2["totalCookies"], 1);
        assert_eq!(r2["totalOrigins"], 2); // BOTH app.github.com and www.github.com preserved!

        assert_eq!(r3["ok"], true);
        assert_eq!(r3["originCount"], 2);
        assert_eq!(r3["version"], env!("CARGO_PKG_VERSION"));
        assert!(!r3.to_string().contains("gh_tok"));
        assert!(!r3.to_string().contains("secret_app"));
        let stats = r3["domainStats"]
            .as_array()
            .expect("domainStats array present");
        let gh_stat = stats
            .iter()
            .find(|s| s["domain"] == "github.com")
            .expect("github.com stats present");
        assert_eq!(gh_stat["cookies"], 1);
        let app_stat = stats
            .iter()
            .find(|s| s["domain"] == "app.github.com")
            .expect("app.github.com stats present");
        assert_eq!(app_stat["origins"], 1);
        assert_eq!(app_stat["localStorageKeys"], 1);

        let saved = load_storage_state_file(&state_path).expect("load state");
        let mirrored = load_storage_state_file(&mirror_path).expect("load mirror");
        assert_eq!(saved.cookies[0].same_site.as_deref(), Some("None"));
        assert_eq!(mirrored.origins.len(), 2);

        // 3. Corrupt state.json MUST fail closed instead of silently wiping existing data!
        fs::write(&state_path, "{corrupt json").expect("corrupt write");
        let err_resp = handle_native_message(&req2, &state_path, Some(&mirror_path));
        assert_eq!(err_resp["ok"], false);
        assert_eq!(fs::read_to_string(&state_path).unwrap(), "{corrupt json");

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn native_host_help_exits_zero_without_blocking_on_stdin() {
        assert_eq!(run_stdio_native_host(&["--help".to_string()]), 0);
        assert_eq!(run_stdio_native_host(&["-h".to_string()]), 0);
    }
}
