"""Password hashing for coach authentication (roadmap package 19, issue #74).

Uses `hashlib.scrypt` -- a recognised, memory-hard password hashing
algorithm available in the Python standard library, so this module has zero
third-party dependencies (see `tests/test_architecture.py`'s FOUNDATION
group: this stays a dependency-free leaf, importable by anything). Never
stores or compares plaintext passwords, and never uses a fast general-
purpose digest (e.g. bare SHA-256) or reversible encryption for password
storage.

`hash_password` picks a fresh random salt per password and encodes the
algorithm name and its cost parameters alongside the salt and derived key in
the stored string, so `verify_password` can check a hash produced under
different parameters (e.g. after a future cost-parameter increase) without
a schema migration -- the parameters travel with each hash, not as a
separate global.
"""

import hashlib
import hmac
import secrets

_ALGORITHM = "scrypt"
# n=cost factor (CPU/memory cost, must be a power of 2), r=block size,
# p=parallelisation. These are scrypt's own commonly recommended interactive
# login parameters (~tens of milliseconds per call) -- deliberately not
# configurable per call so every password in the database was hashed
# comparably; a future deliberate increase changes these constants and new
# hashes simply encode the new values (see module docstring).
_N = 2**14
_R = 8
_P = 1
_KEY_LENGTH = 32
_SALT_LENGTH = 16


def hash_password(password: str) -> str:
    """Return a self-describing hash string safe to store in
    `coach_credential.password_hash`. Never raises on the password's
    content; a plausibility check (non-empty) is the caller's
    responsibility (see `app.auth`)."""
    salt = secrets.token_bytes(_SALT_LENGTH)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_KEY_LENGTH)
    return f"{_ALGORITHM}${_N}${_R}${_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time comparison against a hash produced by `hash_password`.
    Returns False (never raises) for a malformed/unrecognised stored value,
    so a corrupt row fails closed as "wrong password" rather than a 500."""
    try:
        algorithm, n_str, r_str, p_str, salt_hex, derived_hex = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        n, r, p = int(n_str), int(r_str), int(p_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(derived_hex)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected))
    return hmac.compare_digest(candidate, expected)
