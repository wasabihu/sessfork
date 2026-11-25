use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Deserialize, Default)]
pub struct StorageState {
    #[serde(default)]
    pub cookies: Vec<PlaywrightCookie>,
    #[serde(default)]
    pub origins: Vec<OriginState>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PlaywrightCookie {
    pub name: String,
    pub value: String,
    pub domain: String,
    #[serde(default = "default_path")]
    pub path: String,
    #[serde(default)]
    pub expires: Option<f64>,
    #[serde(default)]
    pub http_only: bool,
    #[serde(default)]
    pub secure: bool,
    #[serde(default)]
    pub same_site: Option<String>,
}

fn default_path() -> String {
    "/".to_string()
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct OriginState {
    pub origin: String,
    #[serde(default)]
    pub local_storage: Vec<LocalStorageItem>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct LocalStorageItem {
    pub name: String,
    pub value: String,
}

const MAX_STORAGE_STATE_BYTES: u64 = 16 * 1024 * 1024;
const ACCESS_DENIED_MSG: &str = "Access denied: path is outside allowed roots (--output-dir)";

/// Strictly verify `requested` resolves inside one of `allowed_dirs` or matches an `allowed_file`.
/// Rejects path traversal (`..`) and symlinks escaping allowed boundaries without leaking
/// whether arbitrary outside paths exist or what file type they are.
pub fn validate_allowed_path(
    requested: &Path,
    allowed_dirs: &[PathBuf],
    allowed_files: &[PathBuf],
) -> Result<PathBuf, String> {
    let canonical = fs::canonicalize(requested).map_err(|_| ACCESS_DENIED_MSG.to_string())?;
    if !canonical.is_file() {
        return Err(ACCESS_DENIED_MSG.to_string());
    }

    for dir in allowed_dirs {
        if let Ok(canon_dir) = fs::canonicalize(dir) {
            if canonical.starts_with(&canon_dir) {
                return Ok(canonical);
            }
        }
    }
    for file in allowed_files {
        if let Ok(canon_file) = fs::canonicalize(file) {
            if canonical == canon_file {
                return Ok(canonical);
            }
        }
    }

    Err(ACCESS_DENIED_MSG.to_string())
}

pub fn load_storage_state_file(path: &Path) -> Result<StorageState, String> {
    let meta = fs::metadata(path).map_err(|_| "Failed to read storage state file".to_string())?;
    if !meta.is_file() {
        return Err("Storage state path is not a regular file".to_string());
    }
    if meta.len() > MAX_STORAGE_STATE_BYTES {
        return Err(format!(
            "Storage state file exceeds {} byte safety limit",
            MAX_STORAGE_STATE_BYTES
        ));
    }
    let raw =
        fs::read_to_string(path).map_err(|_| "Failed to read storage state file".to_string())?;
    // Sanitize serde_json errors so unexpected string field values are never echoed to the LLM.
    serde_json::from_str::<StorageState>(&raw).map_err(|e| {
        format!(
            "Invalid storage state JSON ({:?} at line {}, column {})",
            e.classify(),
            e.line(),
            e.column()
        )
    })
}

pub fn cookies_to_cdp_params(cookies: &[PlaywrightCookie]) -> Vec<Value> {
    let mut out = Vec::with_capacity(cookies.len());
    for c in cookies {
        if c.name.is_empty() || c.domain.is_empty() {
            continue;
        }
        let mut obj = serde_json::Map::new();
        obj.insert("name".to_string(), json!(c.name));
        obj.insert("value".to_string(), json!(c.value));
        obj.insert("domain".to_string(), json!(c.domain));
        obj.insert(
            "path".to_string(),
            json!(if c.path.is_empty() { "/" } else { &c.path }),
        );
        obj.insert("httpOnly".to_string(), json!(c.http_only));
        obj.insert("secure".to_string(), json!(c.secure));
        if let Some(exp) = c.expires {
            if exp.is_finite() && exp > 0.0 {
                obj.insert("expires".to_string(), json!(exp));
            }
        }
        if let Some(ref ss) = c.same_site {
            match ss.as_str() {
                "Strict" | "Lax" | "None" => {
                    obj.insert("sameSite".to_string(), json!(ss));
                }
                _ => {}
            }
        }
        out.push(Value::Object(obj));
    }
    out
}

pub fn normalize_origin(raw: &str) -> String {
    let trimmed = raw.trim().trim_end_matches('/');
    if let Some((scheme, rest)) = trimmed.split_once("://") {
        let authority = rest.split(&['/', '?', '#'][..]).next().unwrap_or("");
        if !authority.is_empty() {
            return format!(
                "{}://{}",
                scheme.to_ascii_lowercase(),
                authority.to_ascii_lowercase()
            );
        }
    }
    trimmed.to_ascii_lowercase()
}

/// Build an init script injected via `Page.addScriptToEvaluateOnNewDocument` inside an isolated
/// world (`worldName`) that hydrates `window.localStorage` once per storage-state revision (`rev`):
/// - Uses `Map` instead of a plain `{}` object so `Object.prototype` is never consulted even as
///   defense-in-depth if a browser engine ignores `worldName`.
/// - On a new revision (including hot-reload), clears stale keys from prior revisions before
///   writing the exact new origin entries (so deleted/logged-out tokens never linger).
/// - On subsequent in-session navigations at the same revision, preserves app-written keys.
pub fn build_local_storage_init_script(origins: &[OriginState], rev: u64) -> String {
    let mut map: HashMap<String, Vec<(&str, &str)>> = HashMap::new();
    for o in origins {
        if o.origin.is_empty() {
            continue;
        }
        let normalized = normalize_origin(&o.origin);
        let entries = map.entry(normalized).or_default();
        for item in &o.local_storage {
            entries.push((&item.name, &item.value));
        }
    }
    let pairs: Vec<(String, Vec<(&str, &str)>)> = map.into_iter().collect();
    let serialized = serde_json::to_string(&pairs).unwrap_or_else(|_| "[]".to_string());
    format!(
        "(()=>{{try{{const __rev={rev};const __k='__sessfork_ls_rev';if(sessionStorage.getItem(__k)===String(__rev))return;const __m=new Map({pairs});localStorage.clear();const __e=__m.get(location.origin.toLowerCase());if(__e){{for(const [k,v] of __e){{localStorage.setItem(k,v);}}}}sessionStorage.setItem(__k,String(__rev));}}catch(_){{}}}})();",
        rev = rev,
        pairs = serialized,
    )
}

/// Build a single-origin live sync script containing ONLY the entries for `target_origin`.
/// Untrusted origins receive an empty list `[]` (zero cross-origin secrets ever cross into
/// another origin's renderer process or JS world).
pub fn build_single_origin_local_storage_sync_script(
    target_origin: &str,
    origins: &[OriginState],
    rev: u64,
) -> String {
    let wanted = normalize_origin(target_origin);
    let mut entries: Vec<(&str, &str)> = Vec::new();
    if !wanted.is_empty() {
        for o in origins {
            if normalize_origin(&o.origin) == wanted {
                for item in &o.local_storage {
                    entries.push((&item.name, &item.value));
                }
            }
        }
    }
    let serialized = serde_json::to_string(&entries).unwrap_or_else(|_| "[]".to_string());
    format!(
        "(()=>{{try{{const __rev={rev};const __k='__sessfork_ls_rev';if(sessionStorage.getItem(__k)===String(__rev))return;const __e={entries};localStorage.clear();for(const [k,v] of __e){{localStorage.setItem(k,v);}}sessionStorage.setItem(__k,String(__rev));}}catch(_){{}}}})();",
        rev = rev,
        entries = serialized,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn unique_tmp_dir(tag: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let dir = std::env::temp_dir().join(format!("sessfork_mcp_test_{}_{}", tag, nanos));
        let _ = fs::create_dir_all(&dir);
        dir
    }

    #[test]
    fn rejects_path_outside_allowed_root() {
        let allowed = unique_tmp_dir("allowed");
        let outside = unique_tmp_dir("outside");
        let inside_file = allowed.join("ok.json");
        let outside_file = outside.join("secret.json");
        fs::write(&inside_file, "{}").ok();
        fs::write(&outside_file, "{}").ok();

        assert!(validate_allowed_path(&inside_file, std::slice::from_ref(&allowed), &[]).is_ok());
        let err = validate_allowed_path(&outside_file, std::slice::from_ref(&allowed), &[]);
        assert!(err.is_err());
        assert!(err
            .err()
            .unwrap_or_default()
            .contains("outside allowed roots"));

        let _ = fs::remove_dir_all(&allowed);
        let _ = fs::remove_dir_all(&outside);
    }

    #[test]
    fn normalizes_cookies_and_builds_origin_script() {
        let raw = r#"{
            "cookies": [
                {"name":"sid","value":"abc","domain":"127.0.0.1","path":"/","expires":-1,"httpOnly":true,"secure":false,"sameSite":"Lax"}
            ],
            "origins": [
                {"origin":"http://127.0.0.1:8080/","localStorage":[{"name":"tok","value":"v1"}]}
            ]
        }"#;
        let st: StorageState = serde_json::from_str(raw).expect("valid json");
        let cdp_cookies = cookies_to_cdp_params(&st.cookies);
        assert_eq!(cdp_cookies.len(), 1);
        assert!(cdp_cookies[0].get("expires").is_none());
        assert_eq!(cdp_cookies[0]["sameSite"], "Lax");

        let script = build_local_storage_init_script(&st.origins, 1);
        assert!(script.contains("http://127.0.0.1:8080"));
        assert!(script.contains("new Map("));
        assert!(script.contains("localStorage.clear()"));
        assert!(script.contains("localStorage.setItem"));

        // Single-origin live sync must never include other origins' secrets.
        let untrusted =
            build_single_origin_local_storage_sync_script("https://evil.example", &st.origins, 2);
        assert!(!untrusted.contains("v1"));
        assert!(!untrusted.contains("127.0.0.1:8080"));
        assert!(untrusted.contains("const __e=[];"));

        let trusted =
            build_single_origin_local_storage_sync_script("http://127.0.0.1:8080", &st.origins, 2);
        assert!(trusted.contains("v1"));
    }

    #[test]
    fn hides_path_existence_oracle_and_sanitizes_json_parse_errors() {
        let allowed = unique_tmp_dir("oracle_ok");
        let outside = unique_tmp_dir("oracle_out");
        let existing_out = outside.join("exists.json");
        let missing_out = outside.join("does_not_exist.json");
        fs::write(&existing_out, "{}").ok();

        let err_exists = validate_allowed_path(&existing_out, std::slice::from_ref(&allowed), &[])
            .expect_err("outside existing");
        let err_missing = validate_allowed_path(&missing_out, std::slice::from_ref(&allowed), &[])
            .expect_err("outside missing");
        let err_dir = validate_allowed_path(&outside, std::slice::from_ref(&allowed), &[])
            .expect_err("outside dir");
        assert_eq!(err_exists, err_missing);
        assert_eq!(err_exists, err_dir);

        // Secret string value inside malformed JSON must NOT be echoed back in error text.
        let bad_file = allowed.join("bad.json");
        fs::write(&bad_file, r#"{"cookies": "SUPER_SECRET_RAW_TOKEN_XYZ"}"#).ok();
        let parse_err = load_storage_state_file(&bad_file).expect_err("type mismatch");
        assert!(!parse_err.contains("SUPER_SECRET_RAW_TOKEN_XYZ"));
        assert!(parse_err.contains("Invalid storage state JSON"));

        let _ = fs::remove_dir_all(&allowed);
        let _ = fs::remove_dir_all(&outside);
    }
}
