#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/wasabihu/sessfork"
# Pinned release by default; override with SESSFORK_REF=main (or another tag) for bleeding edge
SESSFORK_REF="${SESSFORK_REF:-v0.2.1}"
TARBALL_URL="${REPO_URL}/archive/${SESSFORK_REF}.tar.gz"
RUNTIME_DIR="${HOME}/.sessfork/runtime"
BIN_DIR="${HOME}/.local/bin"

INSTALL_RUST_MCP=0
RUST_MCP_ONLY=0
SETUP_ARGS=()
_SESSFORK_CLEANUP_PATHS=()

_cleanup() {
  local p
  if [ "${_SESSFORK_CLEANUP_PATHS+x}" = "x" ]; then
    for p in "${_SESSFORK_CLEANUP_PATHS[@]}"; do
      rm -rf "${p}"
    done
  fi
}
trap _cleanup EXIT

for arg in "$@"; do
  case "${arg}" in
    --with-rust-mcp)
      INSTALL_RUST_MCP=1
      ;;
    --rust-mcp-only)
      INSTALL_RUST_MCP=1
      RUST_MCP_ONLY=1
      ;;
    *)
      SETUP_ARGS+=("${arg}")
      ;;
  esac
done

install_rust_mcp_binary() {
  if ! command -v curl >/dev/null 2>&1; then
    echo "Error: curl is required to download prebuilt sessfork-mcp." >&2
    exit 1
  fi
  local os_name arch_name asset base_url tmp_dir sha_tool expected actual
  os_name="$(uname -s)"
  arch_name="$(uname -m)"
  case "${os_name}:${arch_name}" in
    Darwin:arm64|Darwin:aarch64) asset="sessfork-mcp-darwin-arm64.tar.gz" ;;
    Darwin:x86_64|Darwin:amd64)  asset="sessfork-mcp-darwin-x64.tar.gz" ;;
    Linux:x86_64|Linux:amd64)    asset="sessfork-mcp-linux-x64.tar.gz" ;;
    Linux:aarch64|Linux:arm64)   asset="sessfork-mcp-linux-arm64.tar.gz" ;;
    *)
      echo "Error: No prebuilt sessfork-mcp binary for ${os_name}/${arch_name}." >&2
      echo "Build from source instead: cargo install --git ${REPO_URL} sessfork-mcp" >&2
      exit 1
      ;;
  esac

  if [ -n "${SESSFORK_RELEASE_BASE_URL:-}" ]; then
    base_url="${SESSFORK_RELEASE_BASE_URL%/}"
  elif [ -n "${SESSFORK_RUST_MCP_TAG:-}" ]; then
    base_url="${REPO_URL}/releases/download/${SESSFORK_RUST_MCP_TAG}"
  elif [[ "${SESSFORK_REF}" == v* ]]; then
    base_url="${REPO_URL}/releases/download/${SESSFORK_REF}"
  else
    base_url="${REPO_URL}/releases/latest/download"
  fi

  local curl_opts=(-fsSL)
  if [[ "${base_url}" == https://* ]]; then
    curl_opts+=(--proto '=https' --tlsv1.2)
  fi

  mkdir -p "${BIN_DIR}"
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/sessfork_mcp_dl.XXXXXX")"
  _SESSFORK_CLEANUP_PATHS+=("${tmp_dir}")

  echo "-> Downloading prebuilt ${asset} + SHA256SUMS.txt..."
  curl "${curl_opts[@]}" "${base_url}/${asset}" -o "${tmp_dir}/${asset}"
  curl "${curl_opts[@]}" "${base_url}/SHA256SUMS.txt" -o "${tmp_dir}/SHA256SUMS.txt"

  expected="$(awk -v f="${asset}" '$2 == f || $2 == ("*" f) { print $1; exit }' "${tmp_dir}/SHA256SUMS.txt")"
  if [ -z "${expected}" ]; then
    echo "Error: Checksum entry for ${asset} not found in SHA256SUMS.txt." >&2
    exit 1
  fi

  if command -v sha256sum >/dev/null 2>&1; then
    sha_tool="sha256sum"
    actual="$(sha256sum "${tmp_dir}/${asset}" | awk '{print $1}')"
  elif command -v shasum >/dev/null 2>&1; then
    sha_tool="shasum -a 256"
    actual="$(shasum -a 256 "${tmp_dir}/${asset}" | awk '{print $1}')"
  else
    echo "Error: Neither sha256sum nor shasum found to verify download integrity." >&2
    exit 1
  fi

  if [ "${expected}" != "${actual}" ]; then
    echo "Error: SHA-256 mismatch for ${asset} (verified via ${sha_tool})!" >&2
    echo "  expected: ${expected}" >&2
    echo "  actual:   ${actual}" >&2
    exit 1
  fi

  tar -xzf "${tmp_dir}/${asset}" -C "${tmp_dir}" sessfork-mcp
  if [ ! -f "${tmp_dir}/sessfork-mcp" ] || [ -L "${tmp_dir}/sessfork-mcp" ]; then
    echo "Error: Extracted sessfork-mcp binary is missing or is a symlink." >&2
    exit 1
  fi
  # Atomic install onto a fresh inode (prevents Apple Silicon code-sign invalidation if running)
  install -m 755 "${tmp_dir}/sessfork-mcp" "${BIN_DIR}/sessfork-mcp"
  echo "-> Installed verified sessfork-mcp -> ${BIN_DIR}/sessfork-mcp"
}

echo ""
echo "sessfork — Zero-Focus-Theft Browser Session Forking"
echo "--------------------------------------------------"

if [ "${RUST_MCP_ONLY}" -eq 1 ]; then
  install_rust_mcp_binary
  echo ""
  echo "Done! Standalone Rust MCP installed at ${BIN_DIR}/sessfork-mcp"
  echo "See ${REPO_URL}/blob/main/rust/sessfork-mcp/README.md for standalone Moli MCP usage."
  echo ""
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 (>=3.9) is required. Install Python 3 first." >&2
  exit 1
fi

python3 -c '
import sys
if sys.version_info < (3, 9):
    sys.exit("Error: Python 3.9+ is required (found %d.%d)." % sys.version_info[:2])
'

SESSFORK_BIN=""

if command -v uv >/dev/null 2>&1; then
  echo "-> Installing sessfork ${SESSFORK_REF} via uv tool..."
  uv tool install --force "git+${REPO_URL}.git@${SESSFORK_REF}"
  if command -v sessfork >/dev/null 2>&1; then
    SESSFORK_BIN="$(command -v sessfork)"
  elif [ -x "${BIN_DIR}/sessfork" ]; then
    SESSFORK_BIN="${BIN_DIR}/sessfork"
  fi
fi

if [ -z "${SESSFORK_BIN}" ]; then
  if ! command -v curl >/dev/null 2>&1; then
    echo "Error: curl is required for standalone installation." >&2
    exit 1
  fi
  echo "-> Installing zero-dependency standalone runtime (${SESSFORK_REF}) into ${RUNTIME_DIR}..."
  if [ -L "${HOME}/.sessfork" ] || [ -L "${RUNTIME_DIR}" ]; then
    echo "Error: Refusing to install into symlinked directory under ${HOME}/.sessfork." >&2
    exit 1
  fi
  install -d -m 700 "${HOME}/.sessfork"
  mkdir -p "${BIN_DIR}"
  TMP_TAR="$(mktemp "${TMPDIR:-/tmp}/sessfork_src.XXXXXX")"
  TMP_STAGE="$(mktemp -d "${HOME}/.sessfork/runtime_stage.XXXXXX")"
  TMP_WRAPPER="$(mktemp "${TMPDIR:-/tmp}/sessfork_bin.XXXXXX")"
  _SESSFORK_CLEANUP_PATHS+=("${TMP_TAR}" "${TMP_STAGE}" "${TMP_WRAPPER}")
  curl --proto '=https' --tlsv1.2 -fsSL "${TARBALL_URL}" -o "${TMP_TAR}"
  tar -xzf "${TMP_TAR}" -C "${TMP_STAGE}" --strip-components=1
  if [ -e "${RUNTIME_DIR}" ]; then
    rm -rf "${RUNTIME_DIR}"
  fi
  mv "${TMP_STAGE}" "${RUNTIME_DIR}"
  cat > "${TMP_WRAPPER}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
PYTHONPATH="${HOME}/.sessfork/runtime/src${PYTHONPATH:+:${PYTHONPATH}}" exec python3 -m sessfork "$@"
EOF
  install -m 755 "${TMP_WRAPPER}" "${BIN_DIR}/sessfork"
  SESSFORK_BIN="${BIN_DIR}/sessfork"
fi

if [ "${INSTALL_RUST_MCP}" -eq 1 ]; then
  install_rust_mcp_binary
fi

echo "-> Auto-wiring MCP config (sessfork-browser) and Agent SKILL.md..."
if [ "${SETUP_ARGS+x}" = "x" ]; then
  "${SESSFORK_BIN}" --setup "${SETUP_ARGS[@]}"
else
  "${SESSFORK_BIN}" --setup
fi

echo ""
echo "Done! / 安装完成！"
if ! command -v sessfork >/dev/null 2>&1; then
  echo "Tip: Ensure ${BIN_DIR} is on your PATH, or run: ${SESSFORK_BIN}"
  echo "提示: 若终端提示找不到 sessfork，请将 ${BIN_DIR} 加入 PATH，或直接重启终端。"
fi
echo ""
echo "Try asking your AI agent (Antigravity / Claude Code / Cursor):"
echo "  \"Use sessfork to check my github.com notifications silently in the background.\""
echo "  \"用 sessfork 帮我在后台悄悄检查一下 github.com 的未读通知，别打断我当前浏览器。\""
echo ""
