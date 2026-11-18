"""Zero-dependency Chromium cookie decryption for macOS and Linux.

Uses macOS built-in CommonCrypto (`/usr/lib/libSystem.B.dylib`) via `ctypes` or
Linux `openssl` CLI so `sessfork` requires zero third-party pip packages.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import subprocess
import sys
from typing import Optional, Tuple

AES_BLOCK_SIZE = 16
CHROMIUM_CBC_IV = b" " * AES_BLOCK_SIZE
CHROMIUM_SALT = b"saltysalt"
MACOS_ITERATIONS = 1003
LINUX_ITERATIONS = 1

# Browser -> (macOS Keychain Service, macOS Keychain Account, Linux application id)
BROWSER_KEYRING_MAP: dict[str, Tuple[str, str, str]] = {
    "chrome": ("Chrome Safe Storage", "Chrome", "chrome"),
    "canary": ("Chrome Safe Storage", "Chrome Canary", "chrome"),
    "brave": ("Brave Safe Storage", "Brave", "brave"),
    "edge": ("Microsoft Edge Safe Storage", "Microsoft Edge", "chromium"),
    "arc": ("Arc Safe Storage", "Arc", "chromium"),
}


class CryptoError(RuntimeError):
    """Raised when OS keychain retrieval or AES decryption fails."""


def derive_aes_key(password: bytes, iterations: int) -> bytes:
    """Derive 16-byte AES-128 key using PBKDF2-HMAC-SHA1 per Chromium spec."""
    return hashlib.pbkdf2_hmac("sha1", password, CHROMIUM_SALT, iterations, dklen=16)


def get_safe_storage_key(browser: str = "chrome", platform: Optional[str] = None) -> bytes:
    """Fetch Safe Storage passphrase from OS keyring and derive the 16-byte AES key."""
    plat = platform or sys.platform
    if browser not in BROWSER_KEYRING_MAP:
        raise CryptoError(f"Unsupported browser '{browser}'. Choose from: {', '.join(BROWSER_KEYRING_MAP)}")

    service, account, linux_app = BROWSER_KEYRING_MAP[browser]

    if plat == "darwin":
        password = _read_macos_keychain(service, account)
        # Chrome Canary sometimes shares the main Chrome keychain entry
        if password is None and browser == "canary":
            password = _read_macos_keychain("Chrome Safe Storage", "Chrome")
        if password is None:
            raise CryptoError(
                f"Could not read '{service}' from macOS Keychain (or access was denied). "
                "Click 'Allow' / 'Always Allow' when macOS prompts for Keychain access."
            )
        return derive_aes_key(password, MACOS_ITERATIONS)

    if plat.startswith("linux"):
        password = _read_linux_secret_tool(linux_app, service) or b"peanuts"
        return derive_aes_key(password, LINUX_ITERATIONS)

    raise CryptoError(f"Platform '{plat}' is not supported yet (macOS and Linux supported).")


def _read_macos_keychain(service: str, account: str) -> Optional[bytes]:
    for cmd in (
        ["/usr/bin/security", "find-generic-password", "-w", "-s", service, "-a", account],
        ["/usr/bin/security", "find-generic-password", "-w", "-s", service],
    ):
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=15, check=False)
            if proc.returncode == 0:
                secret = proc.stdout.rstrip(b"\r\n")
                if secret:
                    return secret
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _read_linux_secret_tool(app_name: str, service: str) -> Optional[bytes]:
    for cmd in (
        ["secret-tool", "lookup", "application", app_name],
        ["secret-tool", "lookup", "Title", service],
    ):
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=5, check=False)
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.rstrip(b"\r\n")
        except (OSError, subprocess.SubprocessError):
            continue
    return None


# ---------------------------------------------------------------------------
# AES-128-CBC (macOS CommonCrypto via ctypes / Linux openssl CLI)
# ---------------------------------------------------------------------------

_cc_lib: Optional[ctypes.CDLL] = None


def _get_macos_common_crypto() -> ctypes.CDLL:
    global _cc_lib
    if _cc_lib is None:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        lib.CCCrypt.argtypes = [
            ctypes.c_uint32,  # op: kCCEncrypt=0, kCCDecrypt=1
            ctypes.c_uint32,  # alg: kCCAlgorithmAES128=0
            ctypes.c_uint32,  # options: 0 (raw CBC, manual PKCS#7 validation)
            ctypes.c_void_p,  # key
            ctypes.c_size_t,  # keyLength
            ctypes.c_void_p,  # iv
            ctypes.c_void_p,  # dataIn
            ctypes.c_size_t,  # dataInLength
            ctypes.c_void_p,  # dataOut
            ctypes.c_size_t,  # dataOutAvailable
            ctypes.POINTER(ctypes.c_size_t),  # dataOutMoved
        ]
        lib.CCCrypt.restype = ctypes.c_int32
        _cc_lib = lib
    return _cc_lib


def _macos_cc_crypt(op: int, data: bytes, key: bytes, iv: bytes) -> bytes:
    lib = _get_macos_common_crypto()
    out_buf = ctypes.create_string_buffer(len(data) + AES_BLOCK_SIZE)
    moved = ctypes.c_size_t(0)
    status = lib.CCCrypt(
        op,
        0,  # kCCAlgorithmAES128
        0,  # raw block mode; we handle PKCS#7 padding deterministically
        key,
        len(key),
        iv,
        data,
        len(data),
        out_buf,
        len(out_buf),
        ctypes.byref(moved),
    )
    if status != 0:
        raise CryptoError(f"CommonCrypto CCCrypt failed with status {status}")
    return out_buf.raw[: moved.value]


def _openssl_cli_crypt(encrypt: bool, data: bytes, key: bytes, iv: bytes) -> bytes:
    flag = "-e" if encrypt else "-d"
    proc = subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-128-cbc",
            flag,
            "-nopad",
            "-K",
            key.hex(),
            "-iv",
            iv.hex(),
        ],
        input=data,
        capture_output=True,
        timeout=5,
        check=False,
    )
    if proc.returncode != 0:
        raise CryptoError(f"openssl AES-128-CBC failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def aes_128_cbc_decrypt_raw(ciphertext: bytes, key: bytes, iv: bytes = CHROMIUM_CBC_IV) -> bytes:
    """Decrypt raw AES-128-CBC blocks without stripping PKCS#7 padding."""
    if len(key) != 16:
        raise CryptoError(f"AES-128 key must be 16 bytes, got {len(key)}")
    if not ciphertext or len(ciphertext) % AES_BLOCK_SIZE != 0:
        raise CryptoError("Ciphertext length must be a positive multiple of 16 bytes")

    if sys.platform == "darwin":
        return _macos_cc_crypt(1, ciphertext, key, iv)
    return _openssl_cli_crypt(False, ciphertext, key, iv)


def aes_128_cbc_encrypt_pkcs7(plaintext: bytes, key: bytes, iv: bytes = CHROMIUM_CBC_IV) -> bytes:
    """Encrypt plaintext with AES-128-CBC + PKCS#7 padding (used in self-tests)."""
    pad_len = AES_BLOCK_SIZE - (len(plaintext) % AES_BLOCK_SIZE)
    padded = plaintext + bytes([pad_len]) * pad_len
    if sys.platform == "darwin":
        return _macos_cc_crypt(0, padded, key, iv)
    return _openssl_cli_crypt(True, padded, key, iv)


def strip_pkcs7_padding(data: bytes) -> bytes:
    """Validate and strip PKCS#7 padding."""
    if not data or len(data) % AES_BLOCK_SIZE != 0:
        raise CryptoError("Invalid decrypted block length")
    pad = data[-1]
    if pad < 1 or pad > AES_BLOCK_SIZE:
        raise CryptoError("Invalid PKCS#7 padding byte (wrong Safe Storage key?)")
    if data[-pad:] != bytes([pad]) * pad:
        raise CryptoError("Corrupted PKCS#7 padding sequence")
    return data[:-pad]


def decrypt_chromium_cookie(
    encrypted_value: bytes,
    plain_value: str,
    host_key: str,
    aes_key: bytes,
    db_version: int = 24,
) -> Optional[str]:
    """Decrypt a Chromium cookie row, handling v10/v11 envelopes and DB v24+ SHA256 prefixes."""
    if plain_value:
        return plain_value
    if not encrypted_value:
        return ""

    if encrypted_value[:3] not in (b"v10", b"v11"):
        try:
            return encrypted_value.decode("utf-8")
        except UnicodeDecodeError:
            return None

    payload = encrypted_value[3:]
    if not payload:
        return ""

    try:
        raw = aes_128_cbc_decrypt_raw(payload, aes_key)
        unpadded = strip_pkcs7_padding(raw)
    except CryptoError:
        return None

    # Chrome 130+ (cookie DB meta.version >= 24) prepends SHA256(host_key) (32 bytes)
    host_digest = hashlib.sha256(host_key.encode("utf-8")).digest()
    if len(unpadded) >= 32 and (unpadded[:32] == host_digest or db_version >= 24):
        unpadded = unpadded[32:]

    try:
        return unpadded.decode("utf-8")
    except UnicodeDecodeError:
        return unpadded.decode("utf-8", errors="replace")
