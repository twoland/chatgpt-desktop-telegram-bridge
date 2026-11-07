"""Explicit local-file artifacts and authorized Telegram attachment downloads."""
from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .telegram_api import MAX_UPLOAD_BYTES, TelegramApiError, TelegramFileTooLarge


@dataclass(frozen=True)
class IncomingFile:
    file_id: str
    name: str
    kind: str
    size: int | None


def incoming_files(message: dict) -> list[IncomingFile]:
    photos = message.get("photo") or []
    if photos:
        best = max(photos, key=lambda p: (int(p.get("width") or 0) * int(p.get("height") or 0), int(p.get("file_size") or 0)))
        return [IncomingFile(best["file_id"], "photo.jpg", "photo", best.get("file_size"))]
    for kind, extension in [("document", ".bin"), ("video", ".mp4"), ("animation", ".mp4"), ("audio", ".mp3"), ("voice", ".ogg"), ("video_note", ".mp4"), ("sticker", ".webp")]:
        item = message.get(kind)
        if isinstance(item, dict) and isinstance(item.get("file_id"), str):
            filename = item.get("file_name") or kind + extension
            return [IncomingFile(item["file_id"], str(filename), kind, item.get("file_size"))]
    return []


def safe_filename(name: str) -> str:
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^\w. ()-]", "_", name, flags=re.UNICODE).strip(" .")
    return name[:160] or "attachment.bin"


async def receive_files(api, *, base: Path, thread_id: str, chat_id: int, message_id: int, attachments: list[IncomingFile]) -> list[Path]:
    task_key = hashlib.sha256(thread_id.encode()).hexdigest()[:24]
    folder = base / task_key / str(int(chat_id)) / str(int(message_id))
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    outputs = []
    for index, item in enumerate(attachments):
        target = folder / f"{index + 1:02d}-{safe_filename(item.name)}"
        receipt = target.with_name(target.name + ".receipt.json")
        cached = False
        if target.is_file() and receipt.is_file():
            saved = json.loads(receipt.read_text())
            cached = saved.get("file_id") == item.file_id and saved.get("sha256") == hashlib.sha256(target.read_bytes()).hexdigest()
        if not cached:
            target.with_name(target.name + ".partial").unlink(missing_ok=True)
            await api.download_file(file_id=item.file_id, destination=target, expected_size=item.size)
            receipt.write_text(json.dumps({"file_id": item.file_id, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}, indent=2) + "\n")
            receipt.chmod(0o600)
        outputs.append(target)
    return outputs


@dataclass(frozen=True)
class LocalArtifact:
    path: Path
    embedded: bool


LINK = re.compile(r'(?P<embed>!?)\[(?:\\.|[^\]\\])*\]\((?:<(?P<angle>[^>\n]+)>|(?P<plain>(?:\\.|[^()\n]|\([^()\n]*\))+))\)')


def local_artifacts(text: str) -> list[LocalArtifact]:
    # Examples in code blocks are not attachment declarations.
    text = re.sub(r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`", "", text)
    result, seen = [], set()
    for match in LINK.finditer(text):
        raw = (match["angle"] or match["plain"]).strip()
        raw = re.sub(r'\s+"[^"\n]*"$', '', raw)
        raw = re.sub(r"\\([() _])", r"\1", raw)
        if raw.startswith("file://"):
            parsed = urlsplit(raw)
            if parsed.netloc not in {"", "localhost"}:
                continue
            raw = parsed.path
        if not raw.startswith("/"):
            continue  # Never fetch remote URLs or arbitrary URL schemes.
        raw = unquote(raw)
        if re.search(r":\d+(?::\d+)?$", raw):
            continue  # Source-code references are not file attachments.
        if raw not in seen:
            result.append(LocalArtifact(Path(raw), bool(match["embed"])))
            seen.add(raw)
    return result


class ArtifactBlocked(ValueError):
    pass


def validate_artifact(artifact: LocalArtifact, roots: list[Path]) -> Path:
    path = artifact.path.resolve(strict=True)
    relative = None
    for root in roots:
        try:
            relative = path.relative_to(root.resolve(strict=True))
            break
        except (ValueError, OSError):
            continue
    if relative is None:
        raise ArtifactBlocked("File is outside this agent's configured project folders")
    blocked = {"private", "secrets", "credentials", "node_modules", "__pycache__"}
    if any(part.startswith(".") or part.casefold() in blocked for part in relative.parts):
        raise ArtifactBlocked("Private or hidden project files are not exported")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ArtifactBlocked("Only individual regular files are sent")
    if info.st_size > MAX_UPLOAD_BYTES:
        raise TelegramFileTooLarge("File exceeds Telegram's 50 MB upload limit; use a smaller preview or a shared link")
    if info.st_size == 0:
        raise ArtifactBlocked("The file is empty")
    return path


def upload_kind(artifact: LocalArtifact, path: Path) -> str:
    extension = path.suffix.casefold()
    if artifact.embedded and extension in {".png", ".jpg", ".jpeg"} and path.stat().st_size <= 10 * 1024 * 1024:
        return "photo"
    if extension == ".mp4":
        return "video"
    if extension in {".mp3", ".m4a"}:
        return "audio"
    if artifact.embedded and extension == ".gif":
        return "animation"
    return "document"  # File links preserve original bytes, including image files.


def artifact_key(turn_id: str, item_id: str, artifact: LocalArtifact) -> str:
    return hashlib.sha256(json.dumps([turn_id, item_id, str(artifact.path)], ensure_ascii=False).encode()).hexdigest()
