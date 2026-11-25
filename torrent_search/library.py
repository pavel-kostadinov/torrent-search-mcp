"""Downloaded-files library: the NAS download folder mounted into the container.

Set LIBRARY_DIR to the container path of the folder Download Station saves
into (e.g. /downloads). Each top-level entry is one download (a file or a
folder). Deletes are permanent and confined to direct children of the root.
"""

import shutil
from asyncio import to_thread
from dataclasses import dataclass
from os import getenv
from pathlib import Path

# Synology metadata / recycle-bin folders and dotfiles are never shown.
_HIDDEN_PREFIXES = (".", "@", "#")


class LibraryError(Exception):
    """Raised for user-facing library errors."""


@dataclass(frozen=True)
class LibraryItem:
    name: str
    is_dir: bool
    size: int
    modified: float


def _tree_size(path: Path) -> int:
    if not path.is_dir():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                total += child.stat().st_size
        except OSError:
            continue
    return total


class Library:
    def __init__(self, root: Path, protected: frozenset[str] = frozenset()) -> None:
        self.root = root.resolve()
        # Lower-cased top-level names that are never listed or deleted
        # (e.g. an existing "movies" library living in the same share).
        self.protected = frozenset(n.casefold() for n in protected)

    @classmethod
    def from_env(cls) -> "Library | None":
        raw = (getenv("LIBRARY_DIR") or "").strip()
        if not raw:
            return None
        protected = frozenset(
            n.strip() for n in (getenv("LIBRARY_PROTECT") or "").split(",") if n.strip()
        )
        return cls(Path(raw), protected)

    def is_protected(self, name: str) -> bool:
        return name.casefold() in self.protected

    def _entry(self, name: str) -> Path:
        """Resolve a top-level entry, refusing anything outside the root."""
        if (
            not name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or name.startswith(_HIDDEN_PREFIXES)
        ):
            raise LibraryError("invalid name")
        if self.is_protected(name):
            raise LibraryError(f"“{name}” is protected and can't be deleted from here")
        path = self.root / name
        if path.resolve().parent != self.root and not path.is_symlink():
            raise LibraryError("invalid name")
        return path

    def _list(self) -> list[LibraryItem]:
        if not self.root.is_dir():
            raise LibraryError(f"download folder {self.root} is not mounted")
        items = []
        for path in self.root.iterdir():
            if path.name.startswith(_HIDDEN_PREFIXES) or self.is_protected(path.name):
                continue
            try:
                stat = path.lstat()
                items.append(
                    LibraryItem(
                        path.name,
                        path.is_dir() and not path.is_symlink(),
                        _tree_size(path),
                        stat.st_mtime,
                    )
                )
            except OSError:
                continue
        return sorted(items, key=lambda i: i.modified, reverse=True)

    def _delete(self, name: str) -> None:
        path = self._entry(name)
        if not path.exists() and not path.is_symlink():
            raise LibraryError(f"“{name}” is not in the download folder")
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    def exists(self, name: str) -> bool:
        try:
            path = self._entry(name)
        except LibraryError:
            return False
        return path.exists() or path.is_symlink()

    async def list(self) -> list[LibraryItem]:
        return await to_thread(self._list)

    async def delete(self, name: str) -> None:
        await to_thread(self._delete, name)
