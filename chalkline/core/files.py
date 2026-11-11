"""Content-addressed files in the current profile's plugin data directory."""

from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

from plugins.plugin_storage import plugin_data_dir

from .. import SLUG


def file_digest(path: str | Path) -> str:
    """Hash the current bytes at a stored path."""
    with Path(path).open("rb") as stream:
        digest = sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()


def save_bytes(directory: str, name: str, data: bytes) -> Path:
    """Publish a complete file atomically in the plugin's storage."""
    target = plugin_data_dir(SLUG) / directory / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.read_bytes() == data:
        return target
    with NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        scratch = Path(stream.name)
        stream.write(data)
    try:
        scratch.replace(target)
    finally:
        scratch.unlink(missing_ok=True)
    return target


def copy_source(path: str | Path) -> tuple[str, str]:
    """Copy bounded input bytes to a content-addressed path and return path/hash."""
    source = Path(path)
    with source.open("rb") as stream:
        data = stream.read(32 * 1024 * 1024 + 1)
    if not data or len(data) > 32 * 1024 * 1024:
        raise ValueError("Evidence files must be between 1 byte and 32 MiB.")
    digest = sha256(data).hexdigest()
    extension = source.suffix.lower()
    if len(extension) > 12 or not extension[1:].isascii() or not extension[1:].isalnum():
        extension = ""
    return str(save_bytes("sources", digest + extension, data)), digest
