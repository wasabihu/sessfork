"""Zero-dependency Chromium cookie decryption for macOS and Linux.

Uses macOS built-in CommonCrypto (`/usr/lib/libSystem.B.dylib`) or Linux OpenSSL
(`libcrypto`) via `ctypes` in memory so `sessfork` requires zero third-party pip
packages and never exposes derived keys in subprocess command lines.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import os
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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


def _default_key_cache_path() -> Path:
    """Resolve `<real-user-home>/.sessfork/keys.json` without importing chrome.py."""
    override = os.environ.get("SESSFORK_HOME")
    if override:
        home = Path(override).expanduser()
    else:
        home = Path.home()
        try:
            pw_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
            if pw_home.is_dir():
                home = pw_home
        except KeyError:
            pass
    return home / ".sessfork" / "keys.json"


def _decode_aes_hex(value: object) -> Optional[bytes]:
    if not isinstance(value, str):
        return None
    try:
        raw = bytes.fromhex(value.strip())
    except ValueError:
        return None
    return raw if len(raw) == AES_BLOCK_SIZE else None


def _load_key_cache_dict(target: Path) -> Dict[str, str]:
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    keys = data.get("keys")
    if not isinstance(keys, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in keys.items()
        if isinstance(k, str) and _decode_aes_hex(v) is not None
    }


def _write_key_cache_atomic(keys: Dict[str, str], target: Path) -> Path:
    resolved = target.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(resolved.parent, 0o700)
    except OSError:
        pass

    fd, tmp_name = tempfile.mkstemp(dir=str(resolved.parent), prefix=".keys_", suffix=".tmp")
    try:
        os.chmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"keys": dict(sorted(keys.items()))}, fh, indent=2)
            fh.write("\n")
        os.replace(tmp_name, resolved)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return resolved


def load_cached_aes_key(browser: str, cache_path: Optional[Path] = None) -> Optional[bytes]:
    """Load cached PBKDF2-derived 16-byte AES key from 0600 keys.json if present."""
    target = (cache_path or _default_key_cache_path()).expanduser()
    keys = _load_key_cache_dict(target)
    return _decode_aes_hex(keys.get(browser))


def save_cached_aes_key_atomic(
    browser: str,
    key: bytes,
    cache_path: Optional[Path] = None,
) -> Path:
    """Persist PBKDF2-derived 16-byte AES key atomically with 0600 permissions."""
    if len(key) != AES_BLOCK_SIZE:
        raise CryptoError(f"Derived AES key must be {AES_BLOCK_SIZE} bytes, got {len(key)}")
    target = (cache_path or _default_key_cache_path()).expanduser().resolve()
    keys = _load_key_cache_dict(target)
    keys[browser] = key.hex()
    return _write_key_cache_atomic(keys, target)


def clear_cached_aes_keys(
    browser: Optional[str] = None,
    cache_path: Optional[Path] = None,
) -> int:
    """Remove cached derived AES key(s) for a specific browser or all browsers."""
    target = (cache_path or _default_key_cache_path()).expanduser()
    if not target.is_file():
        return 0
    keys = _load_key_cache_dict(target)
    if browser is None:
        count = len(keys)
        try:
            target.unlink()
        except OSError:
            pass
        return count
    if browser not in keys:
        return 0
    del keys[browser]
    if not keys:
        try:
            target.unlink()
        except OSError:
            pass
    else:
        _write_key_cache_atomic(keys, target)
    return 1


def list_cached_key_browsers(cache_path: Optional[Path] = None) -> List[str]:
    """Return sorted browser names present in the derived-key cache (never exposes key bytes)."""
    target = (cache_path or _default_key_cache_path()).expanduser()
    return sorted(_load_key_cache_dict(target).keys())


def derive_aes_key(password: bytes, iterations: int) -> bytes:
    """Derive 16-byte AES-128 key using PBKDF2-HMAC-SHA1 per Chromium spec."""
    return hashlib.pbkdf2_hmac("sha1", password, CHROMIUM_SALT, iterations, dklen=16)


def get_safe_storage_key(
    browser: str = "chrome",
    platform: Optional[str] = None,
    use_cache: bool = False,
    refresh: bool = False,
    cache_path: Optional[Path] = None,
) -> bytes:
    """Fetch Safe Storage passphrase from OS keyring and derive the 16-byte AES key.

    The derived-key cache (`~/.sessfork/keys.json`) is strictly opt-in (`use_cache=True`,
    CLI `--cache-key`). Caching trades OS keyring authorization for convenience: any
    process running as the same user could read the cached key, so it is off by default.
    """
    plat = platform or sys.platform
    if browser not in BROWSER_KEYRING_MAP:
        raise CryptoError(f"Unsupported browser '{browser}'. Choose from: {', '.join(BROWSER_KEYRING_MAP)}")

    effective_cache = use_cache and (cache_path is not None or platform is None or platform == sys.platform)
    if effective_cache and not refresh:
        cached = load_cached_aes_key(browser, cache_path=cache_path)
        if cached is not None:
            return cached

    service, account, linux_app = BROWSER_KEYRING_MAP[browser]

    if plat == "darwin":
        password = _read_macos_keychain(service, account)
        # Chrome Canary sometimes shares the main Chrome keychain entry (only fallback if Canary item is absent)
        if (
            password is None
            and browser == "canary"
            and not _macos_keychain_item_exists("Chrome Safe Storage", "Chrome Canary")
        ):
            password = _read_macos_keychain("Chrome Safe Storage", "Chrome")
        if password is None:
            raise CryptoError(
                f"Could not read '{service}' from macOS Keychain (or access was denied). "
                "Click 'Allow' / 'Always Allow' when macOS prompts for Keychain access."
            )
        key = derive_aes_key(password, MACOS_ITERATIONS)
    elif plat.startswith("linux"):
        password = _read_linux_secret_tool(linux_app, service) or b"peanuts"
        key = derive_aes_key(password, LINUX_ITERATIONS)
    else:
        raise CryptoError(f"Platform '{plat}' is not supported yet (macOS and Linux supported).")

    if effective_cache:
        save_cached_aes_key_atomic(browser, key, cache_path=cache_path)
    return key


def _macos_keychain_item_exists(service: str, account: str) -> bool:
    """Probe Keychain metadata without `-w` (never triggers a macOS password dialog)."""
    try:
        proc = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _read_macos_keychain(service: str, account: str, timeout: int = 120) -> Optional[bytes]:
    """Probe metadata first (0 popups), then prompt at most once with a generous timeout."""
    secret_cmd = ["/usr/bin/security", "find-generic-password", "-w", "-s", service, "-a", account]
    try:
        probe = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if probe.returncode == 44:
            fallback_probe = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", service],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if fallback_probe.returncode != 0:
                return None
            secret_cmd = ["/usr/bin/security", "find-generic-password", "-w", "-s", service]
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        proc = subprocess.run(secret_cmd, capture_output=True, timeout=timeout, check=False)
        if proc.returncode == 0:
            secret = proc.stdout.rstrip(b"\r\n")
            if secret:
                return secret
    except (OSError, subprocess.SubprocessError):
        return None
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
# In-memory AES-128-CBC via ctypes (macOS CommonCrypto / Linux libcrypto)
# ---------------------------------------------------------------------------

_cc_lib: Optional[ctypes.CDLL] = None
_libcrypto_lib: Optional[ctypes.CDLL] = None


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


def _get_linux_libcrypto() -> ctypes.CDLL:
    global _libcrypto_lib
    if _libcrypto_lib is not None:
        return _libcrypto_lib

    candidates = [
        ctypes.util.find_library("crypto"),
        "libcrypto.so.3",
        "libcrypto.so.1.1",
        "libcrypto.so",
    ]
    lib: Optional[ctypes.CDLL] = None
    for cand in candidates:
        if not cand:
            continue
        try:
            lib = ctypes.CDLL(cand)
            break
        except OSError:
            continue

    if lib is None:
        raise CryptoError("Could not load OpenSSL libcrypto shared library")

    lib.EVP_CIPHER_CTX_new.argtypes = []
    lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
    lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
    lib.EVP_CIPHER_CTX_free.restype = None
    lib.EVP_aes_128_cbc.argtypes = []
    lib.EVP_aes_128_cbc.restype = ctypes.c_void_p
    lib.EVP_CipherInit_ex.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    lib.EVP_CipherInit_ex.restype = ctypes.c_int
    lib.EVP_CIPHER_CTX_set_padding.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.EVP_CIPHER_CTX_set_padding.restype = ctypes.c_int
    lib.EVP_CipherUpdate.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    lib.EVP_CipherUpdate.restype = ctypes.c_int
    lib.EVP_CipherFinal_ex.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.EVP_CipherFinal_ex.restype = ctypes.c_int

    _libcrypto_lib = lib
    return _libcrypto_lib


def _linux_libcrypto_crypt(encrypt: bool, data: bytes, key: bytes, iv: bytes) -> bytes:
    lib = _get_linux_libcrypto()
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise CryptoError("OpenSSL EVP_CIPHER_CTX_new failed")
    try:
        if lib.EVP_CipherInit_ex(ctx, lib.EVP_aes_128_cbc(), None, key, iv, 1 if encrypt else 0) != 1:
            raise CryptoError("OpenSSL EVP_CipherInit_ex failed")
        if lib.EVP_CIPHER_CTX_set_padding(ctx, 0) != 1:
            raise CryptoError("OpenSSL EVP_CIPHER_CTX_set_padding failed")

        out_buf = ctypes.create_string_buffer(len(data) + AES_BLOCK_SIZE)
        out_len1 = ctypes.c_int(0)
        out_len2 = ctypes.c_int(0)
        if lib.EVP_CipherUpdate(ctx, out_buf, ctypes.byref(out_len1), data, len(data)) != 1:
            raise CryptoError("OpenSSL EVP_CipherUpdate failed")
        final_ptr = ctypes.c_void_p(ctypes.addressof(out_buf) + out_len1.value)
        if lib.EVP_CipherFinal_ex(ctx, final_ptr, ctypes.byref(out_len2)) != 1:
            raise CryptoError("OpenSSL EVP_CipherFinal_ex failed")
        return out_buf.raw[: out_len1.value + out_len2.value]
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def aes_128_cbc_decrypt_raw(ciphertext: bytes, key: bytes, iv: bytes = CHROMIUM_CBC_IV) -> bytes:
    """Decrypt raw AES-128-CBC blocks without stripping PKCS#7 padding."""
    if len(key) != 16:
        raise CryptoError(f"AES-128 key must be 16 bytes, got {len(key)}")
    if not ciphertext or len(ciphertext) % AES_BLOCK_SIZE != 0:
        raise CryptoError("Ciphertext length must be a positive multiple of 16 bytes")

    if sys.platform == "darwin":
        return _macos_cc_crypt(1, ciphertext, key, iv)
    return _linux_libcrypto_crypt(False, ciphertext, key, iv)


def aes_128_cbc_encrypt_pkcs7(plaintext: bytes, key: bytes, iv: bytes = CHROMIUM_CBC_IV) -> bytes:
    """Encrypt plaintext with AES-128-CBC + PKCS#7 padding (used in self-tests)."""
    pad_len = AES_BLOCK_SIZE - (len(plaintext) % AES_BLOCK_SIZE)
    padded = plaintext + bytes([pad_len]) * pad_len
    if sys.platform == "darwin":
        return _macos_cc_crypt(0, padded, key, iv)
    return _linux_libcrypto_crypt(True, padded, key, iv)


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
