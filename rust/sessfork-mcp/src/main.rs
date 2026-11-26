mod ax;
mod browser;
mod cdp;
mod markdown;
mod mcp;
mod native_host;
mod storage;
mod webmcp;
mod ws_cdp;

use browser::{BrowserConfig, BrowserManager};
use native_host::default_home_dir;
use serde_json::Value;
use std::env;
use std::io::{self, BufRead, Write};
use std::path::{Path, PathBuf};
use std::process;

#[cfg(any(windows, test))]
fn local_app_data_moli_candidates(local_app_data: &Path) -> Vec<PathBuf> {
    vec![
        // Official `moli-installer.ps1` default location: `%LOCALAPPDATA%\Moli\bin\moli.exe`
        local_app_data.join("Moli").join("bin").join("moli.exe"),
        local_app_data.join("moli").join("moli.exe"),
        local_app_data
            .join("Programs")
            .join("Moli")
            .join("bin")
            .join("moli.exe"),
        local_app_data
            .join("Programs")
            .join("moli")
            .join("moli.exe"),
    ]
}

fn find_moli_binary(explicit: Option<&str>) -> Result<PathBuf, String> {
    if let Some(p) = explicit {
        let pb = PathBuf::from(p);
        if pb.is_file() {
            return Ok(pb);
        }
        return Err(format!(
            "Specified --moli binary not found: {}",
            pb.display()
        ));
    }
    if let Ok(env_p) = env::var("SESSFORK_MOLI_PATH") {
        let pb = PathBuf::from(env_p.trim());
        if pb.is_file() {
            return Ok(pb);
        }
    }
    let bin_name = if cfg!(windows) { "moli.exe" } else { "moli" };
    if let Ok(path_var) = env::var("PATH") {
        for dir in env::split_paths(&path_var) {
            let cand = dir.join(bin_name);
            if cand.is_file() {
                return Ok(cand);
            }
        }
    }
    let home = default_home_dir();
    let mut candidates = vec![
        home.join(".local").join("bin").join(bin_name),
        home.join(".cargo").join("bin").join(bin_name),
    ];
    #[cfg(unix)]
    {
        candidates.push(PathBuf::from("/opt/homebrew/bin/moli"));
        candidates.push(PathBuf::from("/usr/local/bin/moli"));
        // Handle profile-isolated HOME on macOS by also probing /Users/<USER>/.local/bin/moli.
        if let Ok(user) = env::var("USER") {
            let trimmed = user.trim();
            if !trimmed.is_empty()
                && !trimmed.contains("..")
                && trimmed
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'))
            {
                candidates.push(Path::new("/Users").join(trimmed).join(".local/bin/moli"));
            }
        }
    }
    #[cfg(windows)]
    {
        if let Ok(local_app) = env::var("LOCALAPPDATA") {
            let trimmed = local_app.trim();
            if !trimmed.is_empty() {
                candidates.extend(local_app_data_moli_candidates(Path::new(trimmed)));
            }
        }
    }
    for cand in candidates {
        if cand.is_file() {
            return Ok(cand);
        }
    }
    if cfg!(windows) {
        Err("Could not locate `moli.exe`. Install via PowerShell:\n  powershell -ExecutionPolicy ByPass -c \"irm https://github.com/lexmount/moli/releases/latest/download/moli-installer.ps1 | iex\"\nor pass --moli <path>.".to_string())
    } else {
        Err("Could not locate `moli` binary. Install via `curl -fsSL https://github.com/lexmount/moli/releases/latest/download/moli-installer.sh | sh` or pass --moli <path>.".to_string())
    }
}

fn print_help() {
    println!(
        "sessfork-mcp {} — Native Rust CDP MCP server & Native Messaging host for sessfork + lexmount/moli\n\n\
         USAGE:\n\
           sessfork-mcp [OPTIONS]                      Run stdio MCP server (default)\n\
           sessfork-mcp native-host [OPTIONS]          Run Chrome/Edge Native Messaging session bridge\n\
           sessfork-mcp register-host --extension-id <ID> [--browser chrome|edge|brave|chromium|all]\n\
           sessfork-mcp setup --mcp-file <PATH>        Wire sessfork-moli into an MCP JSON config\n\n\
         MCP OPTIONS:\n\
           --moli <PATH>           Path to moli binary (auto-detected by default)\n\
           --cdp-url <URL>         Attach to any loopback CDP server (Chromium/CEF/Owl/Moli,\n\
                                   e.g. http://127.0.0.1:9222 or ws://127.0.0.1:9222/devtools/...)\n\
           --storage-state <PATH>  Initial Playwright storageState JSON file\n\
           --output-dir <PATH>     Allowed root directory for storageState hot-reload & screenshots\n\
           --layout                Enable Moli layout engine (required for screenshots)\n\
           -V, --version           Print version\n\
           -h, --help              Print help",
        env!("CARGO_PKG_VERSION")
    );
}

fn parse_args(args: &[String]) -> Result<BrowserConfig, i32> {
    let mut moli_arg: Option<String> = None;
    let mut cdp_url_arg: Option<String> = None;
    let mut state_arg: Option<PathBuf> = None;
    let mut output_dir: Option<PathBuf> = None;
    let mut enable_layout = false;

    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        if a == "-h" || a == "--help" {
            print_help();
            return Err(0);
        } else if a == "-V" || a == "--version" {
            println!("sessfork-mcp {}", env!("CARGO_PKG_VERSION"));
            return Err(0);
        } else if a == "--layout" {
            enable_layout = true;
        } else if let Some(val) = a.strip_prefix("--moli=") {
            moli_arg = Some(val.to_string());
        } else if a == "--moli" && i + 1 < args.len() {
            i += 1;
            moli_arg = Some(args[i].clone());
        } else if let Some(val) = a.strip_prefix("--cdp-url=") {
            cdp_url_arg = Some(val.to_string());
        } else if a == "--cdp-url" && i + 1 < args.len() {
            i += 1;
            cdp_url_arg = Some(args[i].clone());
        } else if let Some(val) = a.strip_prefix("--storage-state=") {
            state_arg = Some(PathBuf::from(val));
        } else if a == "--storage-state" && i + 1 < args.len() {
            i += 1;
            state_arg = Some(PathBuf::from(&args[i]));
        } else if let Some(val) = a.strip_prefix("--output-dir=") {
            output_dir = Some(PathBuf::from(val));
        } else if a == "--output-dir" && i + 1 < args.len() {
            i += 1;
            output_dir = Some(PathBuf::from(&args[i]));
        } else {
            eprintln!("sessfork-mcp: unknown argument '{}'", a);
            return Err(2);
        }
        i += 1;
    }

    if let Some(ref raw_url) = cdp_url_arg {
        if let Err(e) = ws_cdp::parse_loopback_cdp_endpoint(raw_url) {
            eprintln!("sessfork-mcp error: {}", e);
            return Err(2);
        }
    }

    let moli_bin = if cdp_url_arg.is_some() {
        None
    } else {
        match find_moli_binary(moli_arg.as_deref()) {
            Ok(p) => Some(p),
            Err(e) => {
                eprintln!("sessfork-mcp error: {}", e);
                return Err(1);
            }
        }
    };

    let out_dir = output_dir.unwrap_or_else(|| {
        if let Some(ref st) = state_arg {
            st.parent()
                .map(|p| p.join("playwright-output"))
                .unwrap_or_else(|| {
                    default_home_dir()
                        .join(".sessfork")
                        .join("playwright-output")
                })
        } else {
            default_home_dir()
                .join(".sessfork")
                .join("playwright-output")
        }
    });

    Ok(BrowserConfig {
        moli_bin,
        cdp_url: cdp_url_arg,
        initial_storage_state: state_arg,
        output_dir: out_dir,
        enable_layout,
    })
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();

    // Auto-detect Chrome/Edge Native Messaging host launch (argv[1] is `chrome-extension://<id>/`
    // or Windows passes `--parent-window=<hwnd>`), as well as explicit subcommands.
    if let Some(first) = args.first() {
        if first == "native-host" {
            process::exit(native_host::run_stdio_native_host(&args[1..]));
        }
        if first.starts_with("chrome-extension://")
            || args.iter().any(|a| a.starts_with("--parent-window="))
        {
            process::exit(native_host::run_stdio_native_host(&args));
        }
        if first == "register-host" {
            process::exit(native_host::run_register_host(&args[1..]));
        }
        if first == "setup" {
            process::exit(native_host::run_setup_mcp(&args[1..]));
        }
    }

    let cfg = match parse_args(&args) {
        Ok(c) => c,
        Err(code) => process::exit(code),
    };

    let mut manager = BrowserManager::new(cfg);
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut out = io::BufWriter::new(stdout.lock());

    for line_res in stdin.lock().lines() {
        let Ok(line) = line_res else {
            break;
        };
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let msg: Value = match serde_json::from_str(trimmed) {
            Ok(v) => v,
            Err(e) => {
                let err_resp = serde_json::json!({
                    "jsonrpc": "2.0",
                    "id": Value::Null,
                    "error": { "code": -32700, "message": format!("Parse error: {}", e) }
                });
                let _ = writeln!(out, "{}", err_resp);
                let _ = out.flush();
                continue;
            }
        };

        if let Some(resp) = mcp::handle_jsonrpc_message(&msg, &mut manager) {
            if let Ok(s) = serde_json::to_string(&resp) {
                if writeln!(out, "{}", s).is_err() || out.flush().is_err() {
                    break;
                }
            }
        }
    }
    // Dropping `manager` closes FD 3 -> sends EOF to `moli serve --remote-debugging-pipe` -> clean exit.
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn includes_official_windows_moli_installer_bin_path() {
        let base = Path::new("/tmp/fake_local_app_data");
        let cands = local_app_data_moli_candidates(base);
        assert_eq!(
            cands.first().cloned(),
            Some(base.join("Moli").join("bin").join("moli.exe"))
        );
    }
}
