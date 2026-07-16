"""Tamper-evident audit mirror stored outside an agent's workspace."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path


class AuditMirror:
    """Mirror run artifacts and append a keyed hash-chain manifest."""

    def __init__(self, root: str | Path, run_id: str):
        self.root = Path(root).expanduser().resolve()
        self.dir = self.root / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        _chmod(self.root, 0o700)
        _chmod(self.dir, 0o700)
        self._key = _load_or_create_key(self.root.parent / "audit.key")
        self._seq = 0
        self._previous = "0" * 64
        manifest = self.dir / "audit.jsonl"
        if manifest.is_file():
            try:
                last = json.loads(manifest.read_text(encoding="utf-8").splitlines()[-1])
                self._seq = int(last["seq"])
                self._previous = str(last["hmac"])
            except (OSError, ValueError, KeyError, IndexError, json.JSONDecodeError):
                raise RuntimeError(f"cannot resume malformed audit chain: {manifest}")

    def write(self, relative_path: str, content: str) -> None:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("audit artifact path must stay inside the run directory")
        target = self.dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _chmod(target.parent, 0o700)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(content, encoding="utf-8")
        _chmod(temp, 0o600)
        temp.replace(target)
        _chmod(target, 0o600)
        self._record(relative.as_posix(), content.encode("utf-8"))

    def _record(self, path: str, content: bytes) -> None:
        self._seq += 1
        digest = hashlib.sha256(content).hexdigest()
        payload = {
            "seq": self._seq,
            "path": path,
            "sha256": digest,
            "previous": self._previous,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        signature = hmac.new(self._key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        payload["hmac"] = signature
        manifest = self.dir / "audit.jsonl"
        with manifest.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        _chmod(manifest, 0o600)
        self._previous = signature


def default_audit_root() -> Path:
    configured = os.environ.get("ROUNDTABLE_AUDIT_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".roundtable" / "audit"


def _load_or_create_key(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod(path.parent, 0o700)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return path.read_bytes()
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        _chmod(path, 0o600)
        return key


def _chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass
