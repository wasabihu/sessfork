mod ax;
mod browser;
mod cdp;
mod mcp;
mod storage;

use browser::{BrowserConfig, BrowserManager};
use serde_json::Value;
use std::env;
use std::io::{self, BufRead, Write};
use std::path::{Path, PathBuf};
use std::process;

fn default_home_dir() -> PathBuf {
    if let Ok(h) = env::var("HOME") {
        if !h.is_empty() {
            return PathBuf::from(h);
        }
    }
    PathBuf::from(".")
}

fn find_moli_binary(explicit: Option<&str>) -> Result<PathBuf, String> {
    if let Some(p) = explicit {
        let pb = PathBuf::from(p);
        if pb.is_file() {
            return Ok(pb);
        }
        return Err(format!("Specified --moli binary not found: {}", pb.display()));
    }
    if let Ok(env_p) = env::var("SESSFORK_MOLI_PATH") {
        let pb = PathBuf::from(env_p.trim());
        if pb.is_file() {
            return Ok(pb);
        }
    }
    if let Ok(path_var) = env::var("PATH") {
        for dir in env::split_paths(&path_var) {
            let cand = dir.join("moli");
            if cand.is_file() {
                return Ok(cand);
            }
        }
    }
    let home = default_home_dir();
    let mut candidates = vec![
        home.join(".local/bin/moli"),
        PathBuf::from("/opt/homebrew/bin/moli"),
        PathBuf::from("/usr/local/bin/moli"),
    ];
    // Handle profile-isolated HOME on macOS by also probing /Users/<USER>/.local/bin/moli.
    if let Ok(user) = env::var("USER") {
        if !user.is_empty() {
            candidates.push(Path::new("/Users").join(&user).join(".local/bin/moli"));
        }
    }
    for cand in candidates {
        if cand.is_file() {
            return Ok(cand);
        }
    }
    Err("Could not locate `moli` binary. Install via `curl -fsSL https://moli.sh/install.sh | sh` or pass --moli <path>.".to_string())
}

fn print_help() {
    println!(
        "sessfork-mcp {} — Native Rust CDP MCP server for sessfork + lexmount/moli\n\n\
         USAGE:\n\
           sessfork-mcp [OPTIONS]\n\n\
         OPTIONS:\n\
           --moli <PATH>           Path to moli binary (auto-detected by default)\n\
           --storage-state <PATH>  Initial Playwright storageState JSON file\n\
           --output-dir <PATH>     Allowed root directory for storageState hot-reload & screenshots\n\
           --layout                Enable Moli layout engine (required for screenshots)\n\
           -V, --version           Print version\n\
           -h, --help              Print help",
        env!("CARGO_PKG_VERSION")
    );
}

fn parse_args() -> Result<BrowserConfig, i32> {
    let args: Vec<String> = env::args().skip(1).collect();
    let mut moli_arg: Option<String> = None;
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

    let moli_bin = match find_moli_binary(moli_arg.as_deref()) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("sessfork-mcp error: {}", e);
            return Err(1);
        }
    };

    let out_dir = output_dir.unwrap_or_else(|| {
        if let Some(ref st) = state_arg {
            st.parent()
                .map(|p| p.join("playwright-output"))
                .unwrap_or_else(|| default_home_dir().join(".sessfork/playwright-output"))
        } else {
            default_home_dir().join(".sessfork/playwright-output")
        }
    });

    Ok(BrowserConfig {
        moli_bin,
        initial_storage_state: state_arg,
        output_dir: out_dir,
        enable_layout,
    })
}

fn main() {
    let cfg = match parse_args() {
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
