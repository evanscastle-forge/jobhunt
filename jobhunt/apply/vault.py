"""The vault: portal logins (Workday and friends), one per employer site, encrypted at rest.

    profiles/<name>/data/vault.enc     Fernet-encrypted JSON: {site: {"email", "password", "created", "verified"}}
    .env  JOBHUNT_VAULT_KEY=...        the key, written by `jobhunt.py vault init`, which you run yourself

Passwords are generated here and never shown: not in logs, run folders, issues or the dashboard. The
key is never printed either. An encrypted file was chosen over the macOS Keychain for low
overhead when unsticking things by hand; `jobhunt.py vault show <site>` prints one login on request.
"""
from __future__ import annotations

import json
import os
import secrets
import string
from datetime import datetime
from pathlib import Path
from typing import Optional

KEY_VAR = "JOBHUNT_VAULT_KEY"


class VaultError(RuntimeError):
    pass


def _env_key(root: Path) -> Optional[str]:
    if os.environ.get(KEY_VAR):
        return os.environ[KEY_VAR]
    env = Path(root) / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(KEY_VAR + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def init_key(root: Path) -> bool:
    """Append a new key to .env unless one exists. Returns whether it wrote one. Never prints the key."""
    from cryptography.fernet import Fernet
    if _env_key(root):
        return False
    env = Path(root) / ".env"
    with env.open("a", encoding="utf-8") as f:
        f.write(f"\n{KEY_VAR}={Fernet.generate_key().decode()}\n")
    os.chmod(env, 0o600)
    return True


def new_password(length: int = 20) -> str:
    """Meets Workday's rules (upper, lower, digit, special, 8+) with room to spare."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)
                and any(c in "!@#$%^&*-_" for c in pw)):
            return pw


class Vault:
    def __init__(self, path: Path, root: Path):
        self.path, self.root = Path(path), Path(root)
        key = _env_key(root)
        if not key:
            raise VaultError(f"no {KEY_VAR} in .env: run `uv run python jobhunt.py vault init` yourself first")
        from cryptography.fernet import Fernet, InvalidToken
        self._f = Fernet(key.encode())
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self._f.decrypt(self.path.read_bytes()).decode())
            except InvalidToken as e:
                raise VaultError("the vault does not open with the key in .env") from e

    def get(self, site: str) -> Optional[dict]:
        return self.data.get(site)

    def create(self, site: str, email: str) -> dict:
        """A new login for a site, saved before it is used (a crash must not lose a password a site now has)."""
        rec = {"email": email, "password": new_password(), "created": datetime.now().isoformat(timespec="seconds"),
               "verified": False}
        self.data[site] = rec
        self.save()
        return rec

    def mark(self, site: str, **fields) -> None:
        if site in self.data:
            self.data[site].update(fields)
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(self._f.encrypt(json.dumps(self.data).encode()))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def sites(self) -> list[str]:
        return sorted(self.data)
