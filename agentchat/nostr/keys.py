"""NIP-01 keypair management for agentchat v1.2.

Wraps pynostr's PrivateKey with:
    - chmod-600 file load/save (refuses to load less-strict permissions)
    - npub/nsec bech32 convenience
    - shared-secret computation for NIP-44 (used in NIP-17 DMs)

Public API:
    NostrKeys            - main class; wraps a keypair
    load_keys(path)      - load from chmod-600 JSON file
    save_keys(keys, path) - save to chmod-600 JSON file

The on-disk JSON format is:
    {
        "name": "<human label>",          # optional
        "private_key_hex": "<64 hex chars>",
        "nsec": "nsec1..."                 # bech32 form of the same key
    }

Only `private_key_hex` is required; the rest is metadata. We never
store the public key on disk because it's derivable from the private
key (saves a sync bug class).
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from pynostr.key import PrivateKey


@dataclass
class NostrKeys:
    """A Nostr keypair (NIP-01). Holds the private key in memory only."""

    private_key_hex: str

    @classmethod
    def generate(cls) -> "NostrKeys":
        """Create a fresh keypair from OS entropy (pynostr's PrivateKey())."""
        return cls(PrivateKey().hex())

    @classmethod
    def from_nsec(cls, nsec: str) -> "NostrKeys":
        """Load from bech32 nsec form (NIP-19)."""
        sk = PrivateKey.from_nsec(nsec)
        return cls(sk.hex())

    @property
    def private_key(self) -> PrivateKey:
        """Return the pynostr PrivateKey (compute-cached)."""
        return PrivateKey(bytes.fromhex(self.private_key_hex))

    @property
    def public_key_hex(self) -> str:
        """32-byte hex of the x-only schnorr public key (NIP-01)."""
        return self.private_key.public_key.hex()

    @property
    def npub(self) -> str:
        """Bech32 npub form (NIP-19)."""
        return self.private_key.public_key.bech32()

    @property
    def nsec(self) -> str:
        """Bech32 nsec form (NIP-19). NEVER log this; never send it over the wire."""
        return self.private_key.bech32()

    def shared_secret(self, other_pubkey_hex: str) -> bytes:
        """ECDH shared secret with another pubkey (NIP-44)."""
        return self.private_key.compute_shared_secret(other_pubkey_hex)

    def __repr__(self) -> str:
        # Safe repr — never expose the secret in logs / tracebacks.
        return f"NostrKeys(public_key_hex={self.public_key_hex!r})"


def _check_strict_mode(path: Path) -> None:
    """Refuse to load a key file that group/world can read."""
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        octal = oct(mode & 0o7777)
        raise PermissionError(
            f"Refusing to load key file {path}: mode {octal} allows "
            "group/world access. Run: chmod 600 <path>"
        )


def load_keys(path: Union[str, Path]) -> NostrKeys:
    """Load a Nostr keypair from a chmod-600 JSON file.

    Raises:
        FileNotFoundError: if the file does not exist
        PermissionError:   if the file's mode is wider than 0o600
        json.JSONDecodeError: if the file is not valid JSON
        KeyError:          if `private_key_hex` is missing
    """
    p = Path(path)
    _check_strict_mode(p)
    data = json.loads(p.read_text())
    if "private_key_hex" not in data:
        raise KeyError(f"Key file {p} missing 'private_key_hex'")
    return NostrKeys(private_key_hex=data["private_key_hex"])


def save_keys(
    keys: NostrKeys,
    path: Union[str, Path],
    name: Optional[str] = None,
    overwrite: bool = False,
) -> None:
    """Save a Nostr keypair to a chmod-600 JSON file.

    Creates parent directories. Refuses to overwrite unless `overwrite=True`.
    Always sets file mode to 0o600 after write.

    Raises:
        FileExistsError: if the file exists and overwrite is False
    """
    p = Path(path)
    if p.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing {p}; pass overwrite=True")
    p.parent.mkdir(parents=True, exist_ok=True)

    payload = {"private_key_hex": keys.private_key_hex, "nsec": keys.nsec}
    if name:
        payload["name"] = name

    # Write atomically: temp file + rename, so a partial write can't leave
    # a 0o600 file with truncated JSON.
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    os.chmod(p, 0o600)