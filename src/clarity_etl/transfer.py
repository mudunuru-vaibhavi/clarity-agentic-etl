"""Move nightly drops from the Clarity outbox to the local landing zone.

Two transports share one interface:
  SftpTransport   the real thing: pulls over SFTP (used in CI against an SFTP container)
  LocalTransport  copies from a folder, for laptops without an SFTP server

A drop counts as ready only once its manifest.json exists, and it is downloaded into a
hidden .partial folder and renamed into place, so the loader never sees half a drop.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from datetime import date
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse

from .source.extract import MANIFEST

DROP_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Transport(Protocol):
    def list_drops(self) -> list[str]: ...
    def fetch(self, drop: str, dest: Path) -> None: ...
    def close(self) -> None: ...


class LocalTransport:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def list_drops(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(
            p.name for p in self.root.iterdir()
            if p.is_dir() and DROP_NAME.match(p.name) and (p / MANIFEST).exists()
        )

    def fetch(self, drop: str, dest: Path) -> None:
        src = self.root / drop
        dest.mkdir(parents=True, exist_ok=True)
        for f in sorted(src.iterdir(), key=lambda p: p.name == MANIFEST):  # manifest last
            shutil.copy2(f, dest / f.name)

    def close(self) -> None:
        pass


class SftpTransport:
    def __init__(self, host: str, port: int, username: str, password: str | None, root: str) -> None:
        import paramiko

        self._ssh = paramiko.SSHClient()
        self._ssh.load_system_host_keys()
        # Throwaway CI containers have new host keys every run; real servers must be known.
        if os.environ.get("SFTP_TRUST_NEW_HOST") == "1":
            self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._ssh.connect(host, port=port, username=username, password=password,
                          look_for_keys=password is None, allow_agent=False, timeout=30)
        self._sftp = self._ssh.open_sftp()
        self.root = root.rstrip("/") or "."

    def list_drops(self) -> list[str]:
        ready = []
        for entry in self._sftp.listdir_attr(self.root):
            if stat.S_ISDIR(entry.st_mode or 0) and DROP_NAME.match(entry.filename):
                try:
                    self._sftp.stat(f"{self.root}/{entry.filename}/{MANIFEST}")
                    ready.append(entry.filename)
                except FileNotFoundError:
                    pass  # still being written
        return sorted(ready)

    def fetch(self, drop: str, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        remote = f"{self.root}/{drop}"
        names = sorted(self._sftp.listdir(remote), key=lambda n: n == MANIFEST)
        for name in names:
            self._sftp.get(f"{remote}/{name}", str(dest / name))

    def close(self) -> None:
        self._sftp.close()
        self._ssh.close()


def make_transport(source: str) -> Transport:
    """`sftp://user@host:22/outbox` (password from SFTP_PASSWORD) or a local folder path."""
    if source.startswith("sftp://"):
        u = urlparse(source)
        return SftpTransport(
            u.hostname or "localhost", u.port or 22, unquote(u.username or ""),
            os.environ.get("SFTP_PASSWORD"), u.path or ".",
        )
    return LocalTransport(Path(source))


def landed_drops(landing: Path) -> list[str]:
    if not landing.exists():
        return []
    return sorted(
        p.name for p in landing.iterdir()
        if p.is_dir() and DROP_NAME.match(p.name) and (p / MANIFEST).exists()
    )


def pull_new_drops(transport: Transport, landing: Path, already_loaded: set[str],
                   as_of: date | None = None) -> list[str]:
    """Download drops that are neither loaded nor already landed. Returns their names."""
    landing.mkdir(parents=True, exist_ok=True)
    have = set(landed_drops(landing)) | already_loaded
    pulled = []
    for drop in transport.list_drops():
        if drop in have or (as_of and date.fromisoformat(drop) > as_of):
            continue
        partial = landing / f".partial-{drop}"
        shutil.rmtree(partial, ignore_errors=True)
        transport.fetch(drop, partial)
        partial.rename(landing / drop)
        pulled.append(drop)
    return pulled
