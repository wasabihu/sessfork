#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/wasabihu/sessfork"
# Pinned release by default; override with SESSFORK_REF=main (or another tag) for bleeding edge
SESSFORK_REF="${SESSFORK_REF:-v0.1.1}"
TARBALL_URL="${REPO_URL}/archive/${SESSFORK_REF}.tar.gz"
RUNTIME_DIR="${HOME}/.sessfork/runtime"
BIN_DIR="${HOME}/.local/bin"

echo ""
echo "sessfork — Zero-Focus-Theft Browser Session Forking"
echo "--------------------------------------------------"

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
  mkdir -p "${HOME}/.sessfork" "${RUNTIME_DIR}" "${BIN_DIR}"
  chmod 700 "${HOME}/.sessfork"
  TMP_TAR="$(mktemp "${TMPDIR:-/tmp}/sessfork_src.XXXXXX")"
  trap 'rm -f "${TMP_TAR}"' EXIT
  curl -fsSL "${TARBALL_URL}" -o "${TMP_TAR}"
  rm -rf "${RUNTIME_DIR:?}"/*
  tar -xzf "${TMP_TAR}" -C "${RUNTIME_DIR}" --strip-components=1
  cat > "${BIN_DIR}/sessfork" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
PYTHONPATH="${HOME}/.sessfork/runtime/src${PYTHONPATH:+:${PYTHONPATH}}" exec python3 -m sessfork "$@"
EOF
  chmod +x "${BIN_DIR}/sessfork"
  SESSFORK_BIN="${BIN_DIR}/sessfork"
fi

echo "-> Auto-wiring MCP config (sessfork-browser) and Agent SKILL.md..."
"${SESSFORK_BIN}" --setup "$@"

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
