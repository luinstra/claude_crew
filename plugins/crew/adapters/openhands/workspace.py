"""File capabilities shared by the native SDK tools and cancellation."""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class AccessDenied(ValueError):
    pass


class Workspace:
    """No shell or filesystem escape; revocation joins any current file operation."""

    def __init__(
        self, root: Path, *, writable: bool = False, staging: Path | None = None
    ) -> None:
        self._original_root = root.absolute()
        self.root = root.resolve(strict=True)
        self.writable = writable
        self.staging = self.relative(staging) if staging is not None else None
        self._lock = threading.RLock()
        self._revoked = False
        self._fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def relative(self, path: str | Path) -> str:
        value = Path(path)
        if value.is_absolute():
            try:
                if value.is_relative_to(self._original_root):
                    value = value.relative_to(self._original_root)
                else:
                    value = value.relative_to(self.root)
            except ValueError as exc:
                raise AccessDenied("Path is outside the issued workspace") from exc
        if ".." in value.parts:
            raise AccessDenied("Parent traversal is not allowed")
        return str(value)

    @contextmanager
    def _parent(
        self, path: str | Path, *, writing: bool = False
    ) -> Iterator[tuple[int, str]]:
        relative = self.relative(path)
        parts = Path(relative).parts
        if not parts:
            raise AccessDenied("A file path is required")
        if (
            writing
            and relative != self.staging
            and (
                not self.writable
                or any(p in {".git", ".crew", ".agents", ".codex"} for p in parts)
            )
        ):
            raise AccessDenied("Writing this path was not authorized by the action")
        with self._lock:
            if self._revoked:
                raise AccessDenied("This action's file access has ended")
            parent = os.dup(self._fd)
            try:
                for part in parts[:-1]:
                    if writing:
                        try:
                            os.mkdir(part, mode=0o700, dir_fd=parent)
                        except FileExistsError:
                            pass
                    child = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=parent,
                    )
                    os.close(parent)
                    parent = child
                yield parent, parts[-1]
            finally:
                os.close(parent)

    def read(self, path: str) -> str:
        with self._parent(path) as (parent, name):
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise AccessDenied("Only regular files may be read")
                data = source.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise AccessDenied("File exceeds the one MiB read limit")
                return data.decode("utf-8")

    def list(self, path: str = ".") -> list[str]:
        relative = self.relative(path)
        with self._lock:
            if self._revoked:
                raise AccessDenied("This action's file access has ended")
            fd = os.dup(self._fd)
            try:
                for part in Path(relative).parts:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                    os.close(fd)
                    fd = child
                return sorted(os.listdir(fd))[:2000]
            finally:
                os.close(fd)

    def write(self, path: str, content: str) -> None:
        data = content.encode("utf-8")
        if len(data) > 1024 * 1024:
            raise AccessDenied("Write exceeds the one MiB limit")
        with self._parent(path, writing=True) as (parent, name):
            # Inspect before truncating, including hard links into another directory.
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=parent,
            )
            with os.fdopen(fd, "wb") as target:
                info = os.fstat(target.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise AccessDenied(
                        "Only a singly linked regular workspace file may be changed"
                    )
                os.ftruncate(target.fileno(), 0)
                target.write(data)

    def revoke(self) -> None:
        with self._lock:
            self._revoked = True

    def close(self) -> None:
        with self._lock:
            self._revoked = True
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
