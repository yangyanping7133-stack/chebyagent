from __future__ import annotations

import asyncio
import hashlib
import io
import multiprocessing
import os
import re
import secrets
import stat
import sys
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from .store import (
    AssetBindingError,
    AssetQuotaError,
    GatewayStore,
    ImageAssetRecord,
)


MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 25_000_000
MAX_EDGE_PIXELS = 12_000
READY_TTL = timedelta(hours=24)
HARD_TTL = timedelta(days=7)
EXPECTED_CODEX_VERSION = "0.144.6"
_FINAL_PREFIX = "img_"
_FINAL_NAME = re.compile(r"^img_[0-9a-f]{64}\.(?:jpg|png)$")


class ImageAssetError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class ImageUploadResult:
    record: ImageAssetRecord
    created: bool


def _utc_after(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat().replace("+00:00", "Z")


def _safe_limits() -> None:
    import resource

    for name, requested_soft in (
        ("RLIMIT_CPU", 8),
        ("RLIMIT_AS", 768 * 1024 * 1024),
        ("RLIMIT_FSIZE", 16 * 1024 * 1024),
        ("RLIMIT_NOFILE", 32),
    ):
        limit = getattr(resource, name, None)
        if limit is None:
            raise RuntimeError("required image-worker resource limit is unavailable")
        _, hard = resource.getrlimit(limit)
        bounded_soft = (
            requested_soft
            if hard == resource.RLIM_INFINITY
            else min(requested_soft, hard)
        )
        if bounded_soft == resource.RLIM_INFINITY or bounded_soft < 1:
            raise RuntimeError("image-worker resource limit cannot be bounded")
        # Any failure is intentionally fatal to the worker. Running an image
        # decoder without all four limits is not an allowed fallback.
        try:
            resource.setrlimit(limit, (bounded_soft, hard))
        except (OSError, ValueError):
            # Darwin exposes RLIMIT_AS but rejects attempts to set it. The
            # production image is Linux, where every required limit is a hard
            # capability gate and any failure must terminate the worker.
            if sys.platform.startswith("linux") or name != "RLIMIT_AS":
                raise


def _validate_png_structure(data: bytes) -> None:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid PNG signature")
    offset = 8
    saw_iend = False
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        if length > MAX_UPLOAD_BYTES or offset + 12 + length > len(data):
            raise ValueError("invalid PNG chunk")
        kind = data[offset + 4 : offset + 8]
        offset += 12 + length
        if kind == b"IEND":
            if length != 0 or offset != len(data):
                raise ValueError("PNG has trailing content")
            saw_iend = True
            break
    if not saw_iend:
        raise ValueError("PNG is truncated")


def _normalize_worker(input_path: str, output_path: str, sender: Any) -> None:
    _safe_limits()
    result: Dict[str, Any] = {"ok": False, "code": "IMAGE_UNSAFE"}
    try:
        from PIL import Image, ImageOps

        warnings.simplefilter("error", Image.DecompressionBombWarning)
        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        read_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            read_flags |= os.O_NOFOLLOW
        input_fd = os.open(input_path, read_flags)
        try:
            input_stat = os.fstat(input_fd)
            if (
                not stat.S_ISREG(input_stat.st_mode)
                or input_stat.st_nlink != 1
                or stat.S_IMODE(input_stat.st_mode) != 0o600
                or input_stat.st_uid != os.geteuid()
            ):
                raise ValueError("unsafe upload file")
            chunks = bytearray()
            while len(chunks) <= MAX_UPLOAD_BYTES:
                chunk = os.read(
                    input_fd,
                    min(1024 * 1024, MAX_UPLOAD_BYTES + 1 - len(chunks)),
                )
                if not chunk:
                    break
                chunks.extend(chunk)
            raw = bytes(chunks)
        finally:
            os.close(input_fd)
        if not raw or len(raw) > MAX_UPLOAD_BYTES:
            raise ValueError("upload size is invalid")
        source_sha256 = hashlib.sha256(raw).hexdigest()
        if raw.startswith(b"\x89PNG"):
            _validate_png_structure(raw)
        elif raw.startswith(b"\xff\xd8"):
            if not raw.endswith(b"\xff\xd9"):
                raise ValueError("JPEG is truncated or has trailing content")
        else:
            result = {"ok": False, "code": "IMAGE_UNSUPPORTED"}
            return

        with Image.open(io.BytesIO(raw)) as probe:
            media_format = str(probe.format or "")
            if media_format not in {"JPEG", "PNG"}:
                raise ValueError("unsupported decoded format")
            if int(getattr(probe, "n_frames", 1)) != 1:
                raise ValueError("multi-frame images are not allowed")
            width, height = probe.size
            if (
                width < 1
                or height < 1
                or max(width, height) > MAX_EDGE_PIXELS
                or width * height > MAX_PIXELS
            ):
                result = {"ok": False, "code": "IMAGE_TOO_LARGE"}
                return
            probe.verify()

        with Image.open(io.BytesIO(raw)) as decoded:
            decoded.load()
            oriented = ImageOps.exif_transpose(decoded)
            width, height = oriented.size
            if max(width, height) > MAX_EDGE_PIXELS or width * height > MAX_PIXELS:
                result = {"ok": False, "code": "IMAGE_TOO_LARGE"}
                return
            has_alpha = media_format == "PNG" and (
                "A" in oriented.getbands() or "transparency" in oriented.info
            )
            mode = "RGBA" if has_alpha else "RGB"
            converted = oriented.convert(mode)
            clean = Image.new(mode, converted.size)
            clean.paste(converted)

        write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            write_flags |= os.O_NOFOLLOW
        output_fd = os.open(output_path, write_flags, 0o600)
        try:
            with os.fdopen(output_fd, "wb", closefd=False) as output:
                if media_format == "JPEG":
                    clean.save(output, format="JPEG", quality=90, optimize=True, progressive=False)
                    media_type, extension = "image/jpeg", "jpg"
                else:
                    clean.save(output, format="PNG", optimize=True)
                    media_type, extension = "image/png", "png"
                output.flush()
                os.fsync(output_fd)
        finally:
            os.close(output_fd)
        output_size = os.path.getsize(output_path)
        if output_size < 1 or output_size > MAX_UPLOAD_BYTES:
            os.unlink(output_path)
            result = {"ok": False, "code": "IMAGE_TOO_LARGE"}
            return
        check_fd = os.open(output_path, read_flags)
        with open(check_fd, "rb", closefd=True) as check_handle, Image.open(check_handle) as check:
            check.load()
            if (
                check.format != media_format
                or check.size != (width, height)
                or int(getattr(check, "n_frames", 1)) != 1
                or bool(check.getexif())
                or any(key in check.info for key in ("exif", "icc_profile", "xmp", "comment"))
            ):
                raise ValueError("normalized image verification failed")
            check_handle.seek(0)
            normalized_sha256 = hashlib.sha256(check_handle.read()).hexdigest()
            normalized_stat = os.fstat(check_handle.fileno())
            if (
                not stat.S_ISREG(normalized_stat.st_mode)
                or normalized_stat.st_nlink != 1
                or stat.S_IMODE(normalized_stat.st_mode) != 0o600
                or normalized_stat.st_uid != os.geteuid()
            ):
                raise ValueError("normalized image has unsafe file metadata")
        result = {
            "ok": True,
            "media_type": media_type,
            "extension": extension,
            "width": width,
            "height": height,
            "byte_count": output_size,
            "normalized_sha256": normalized_sha256,
            "file_device": int(normalized_stat.st_dev),
            "file_inode": int(normalized_stat.st_ino),
            "source_sha256": source_sha256,
        }
    except Exception:
        result = {"ok": False, "code": "IMAGE_UNSAFE"}
    finally:
        try:
            sender.send(result)
        except (BrokenPipeError, EOFError):
            pass
        sender.close()


class ImageAssetManager:
    def __init__(self, root: str, store: GatewayStore) -> None:
        self.configured_root = os.path.abspath(root)
        self.root = os.path.realpath(self.configured_root)
        self.store = store
        self.enabled = False
        self._root_fd: Optional[int] = None
        self._worker_slots: Optional[asyncio.Semaphore] = None

    async def prepare(self, configured: bool, runtime_version: Optional[str], expected_version: str) -> bool:
        self.enabled = False
        self._worker_slots = asyncio.Semaphore(2)
        if not configured or expected_version != EXPECTED_CODEX_VERSION or runtime_version != EXPECTED_CODEX_VERSION:
            return False
        try:
            import PIL

            if PIL.__version__ != "12.3.0":
                return False
            configured_stat = os.lstat(self.configured_root)
            if stat.S_ISLNK(configured_stat.st_mode):
                return False
            root_stat = os.lstat(self.root)
            if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
                return False
            if stat.S_IMODE(root_stat.st_mode) != 0o700 or root_stat.st_uid != os.geteuid():
                return False
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            self._root_fd = os.open(self.root, flags)
            self.enabled = True
            await asyncio.to_thread(self.cleanup, True)
            return True
        except (ImportError, OSError):
            self.close()
            return False

    def close(self) -> None:
        if self._root_fd is not None:
            os.close(self._root_fd)
            self._root_fd = None
        self.enabled = False
        self._worker_slots = None

    async def upload(
        self,
        *,
        device_id: str,
        thread_id: str,
        client_message_id: str,
        client_asset_id: str,
        body: bytes,
    ) -> ImageUploadResult:
        if not self.enabled:
            raise ImageAssetError("CAPABILITY_UNAVAILABLE", "Local image input is unavailable", 503)
        if not body or len(body) > MAX_UPLOAD_BYTES:
            raise ImageAssetError("IMAGE_TOO_LARGE", "Image exceeds the upload limit", 413)
        worker_slots = self._worker_slots
        if worker_slots is None:
            raise ImageAssetError("CAPABILITY_UNAVAILABLE", "Local image input is unavailable", 503)
        async with worker_slots:
            return await asyncio.to_thread(
                self._upload_sync,
                device_id,
                thread_id,
                client_message_id,
                client_asset_id,
                body,
            )

    def _upload_sync(
        self,
        device_id: str,
        thread_id: str,
        client_message_id: str,
        client_asset_id: str,
        body: bytes,
    ) -> ImageUploadResult:
        source_sha256 = hashlib.sha256(body).hexdigest()
        upload_name = ".upload_%s.part" % secrets.token_hex(24)
        normalized_name = ".normalized_%s.part" % secrets.token_hex(24)
        upload_path = self._path(upload_name)
        normalized_path = self._path(normalized_name)
        self._write_exclusive(upload_name, body)
        try:
            normalized = self._run_worker(upload_path, normalized_path)
            if str(normalized["source_sha256"]) != source_sha256:
                raise ImageAssetError(
                    "IMAGE_UNSAFE", "Upload bytes changed before normalization"
                )
            final_name = "%s%s.%s" % (
                _FINAL_PREFIX,
                secrets.token_hex(32),
                normalized["extension"],
            )
            final_path = self._path(final_name)
            if self._root_fd is None:
                raise ImageAssetError(
                    "CAPABILITY_UNAVAILABLE", "Asset storage is unavailable", 503
                )
            # Atomic, no-replace publication: link fails on the astronomically
            # unlikely random-name collision instead of overwriting anything.
            os.link(
                normalized_name,
                final_name,
                src_dir_fd=self._root_fd,
                dst_dir_fd=self._root_fd,
                follow_symlinks=False,
            )
            os.unlink(normalized_name, dir_fd=self._root_fd)
            os.chmod(final_path, 0o600, follow_symlinks=False)
            self._fsync_root()
            file_stat = os.lstat(final_path)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_nlink != 1
                or stat.S_IMODE(file_stat.st_mode) != 0o600
                or file_stat.st_uid != os.geteuid()
                or int(file_stat.st_dev) != int(normalized["file_device"])
                or int(file_stat.st_ino) != int(normalized["file_inode"])
            ):
                raise ImageAssetError("IMAGE_UNSAFE", "Normalized image failed storage validation")
            normalized_sha256 = self._hash_file(final_path)
            if normalized_sha256 != str(normalized["normalized_sha256"]):
                raise ImageAssetError(
                    "IMAGE_UNSAFE", "Normalized image changed before publication"
                )
            try:
                record, created = self.store.register_image_asset(
                    asset_ref="ast_%s" % secrets.token_urlsafe(32),
                    device_id=device_id,
                    thread_id=thread_id,
                    client_message_id=client_message_id,
                    client_asset_id=client_asset_id,
                    storage_name=final_name,
                    media_type=str(normalized["media_type"]),
                    width=int(normalized["width"]),
                    height=int(normalized["height"]),
                    byte_count=int(normalized["byte_count"]),
                    source_sha256=source_sha256,
                    normalized_sha256=normalized_sha256,
                    file_device=int(file_stat.st_dev),
                    file_inode=int(file_stat.st_ino),
                    expires_at=_utc_after(READY_TTL),
                    hard_expires_at=_utc_after(HARD_TTL),
                )
            except AssetBindingError as exc:
                self._safe_unlink(final_name)
                raise ImageAssetError(
                    "ASSET_IDEMPOTENCY_CONFLICT",
                    "The client asset id is already bound to different bytes",
                    409,
                ) from exc
            except AssetQuotaError as exc:
                self._safe_unlink(final_name)
                raise ImageAssetError("ASSET_QUOTA_EXCEEDED", "Image asset quota exceeded", 429) from exc
            except Exception:
                self._safe_unlink(final_name)
                raise
            if not created:
                self._safe_unlink(final_name)
            return ImageUploadResult(record=record, created=created)
        finally:
            self._safe_unlink(upload_name)
            self._safe_unlink(normalized_name)

    def _run_worker(self, input_path: str, output_path: str) -> Dict[str, Any]:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=_normalize_worker, args=(input_path, output_path, sender))
        try:
            process.start()
            sender.close()
            process.join(12)
            if process.is_alive():
                process.kill()
                process.join(2)
                raise ImageAssetError("IMAGE_UNSAFE", "Image normalization timed out")
            if not receiver.poll() or process.exitcode != 0:
                raise ImageAssetError("IMAGE_UNSAFE", "Image normalization failed")
            result = receiver.recv()
        finally:
            sender.close()
            receiver.close()
            if process.is_alive():
                process.kill()
                process.join(2)
        if not result.get("ok"):
            code = str(result.get("code") or "IMAGE_UNSAFE")
            status = 413 if code == "IMAGE_TOO_LARGE" else 415 if code == "IMAGE_UNSUPPORTED" else 400
            raise ImageAssetError(code, "Image was rejected by the secure normalizer", status)
        return result

    def validate_records(
        self,
        records: Tuple[ImageAssetRecord, ...],
        *,
        require_claimed: bool = False,
    ) -> None:
        for record in records:
            if require_claimed and record.state != "claimed":
                raise ImageAssetError("ASSET_UNAVAILABLE", "Image asset is unavailable", 409)
            path = self._path(record.storage_name)
            try:
                file_stat = os.lstat(path)
            except OSError as exc:
                raise ImageAssetError("ASSET_UNAVAILABLE", "Image asset is unavailable", 409) from exc
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_nlink != 1
                or int(file_stat.st_dev) != record.file_device
                or int(file_stat.st_ino) != record.file_inode
                or stat.S_IMODE(file_stat.st_mode) != 0o600
                or file_stat.st_uid != os.geteuid()
                or file_stat.st_size != record.byte_count
            ):
                raise ImageAssetError("ASSET_UNAVAILABLE", "Image asset is unavailable", 409)
            if self._hash_file(path) != record.normalized_sha256:
                raise ImageAssetError("ASSET_UNAVAILABLE", "Image asset is unavailable", 409)

    def internal_path(self, record: ImageAssetRecord) -> str:
        if record.state != "claimed":
            raise ImageAssetError("ASSET_UNAVAILABLE", "Image asset is unavailable", 409)
        self._path(record.storage_name)
        if not _FINAL_NAME.fullmatch(record.storage_name):
            raise ImageAssetError("ASSET_UNAVAILABLE", "Image asset is unavailable", 409)
        return "/asset-staging/" + record.storage_name

    def cleanup(self, startup: bool = False) -> None:
        if not self.enabled:
            return
        self.store.queue_expired_image_assets()
        for storage_name in self.store.image_cleanup_queue():
            if self._safe_unlink(storage_name):
                self.store.complete_image_cleanup(storage_name)
        referenced = set(self.store.referenced_image_storage_names())
        cutoff = time.time() if startup else time.time() - 3600
        entries = list(os.scandir(self.root))
        if startup:
            self._cleanup_interrupted_link_publish(entries, referenced)
            entries = list(os.scandir(self.root))
        for entry in entries:
            name = entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                continue
            if name.endswith(".part") and info.st_mtime <= cutoff:
                self._safe_unlink(name)
            elif name.startswith(_FINAL_PREFIX) and name not in referenced and info.st_mtime <= cutoff:
                self._safe_unlink(name)

    def _path(self, name: str) -> str:
        if not name or os.path.basename(name) != name:
            raise ImageAssetError("IMAGE_UNSAFE", "Invalid asset storage name")
        path = os.path.join(self.root, name)
        if os.path.dirname(os.path.realpath(path)) != self.root:
            raise ImageAssetError("IMAGE_UNSAFE", "Asset path escaped the staging root")
        return path

    def _write_exclusive(self, name: str, body: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self._path(name), flags, 0o600)
        try:
            view = memoryview(body)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _safe_unlink(self, name: str) -> bool:
        if os.path.basename(name) != name or self._root_fd is None:
            return False
        try:
            info = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                os.unlink(name, dir_fd=self._root_fd)
                self._fsync_root()
                return True
        except FileNotFoundError:
            return True
        return False

    def _cleanup_interrupted_link_publish(
        self,
        entries: list,
        referenced: set,
    ) -> None:
        if self._root_fd is None:
            return
        by_inode: Dict[Tuple[int, int], list] = {}
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                by_inode.setdefault((int(info.st_dev), int(info.st_ino)), []).append(
                    (entry.name, info)
                )
        for linked in by_inode.values():
            names = {name for name, _ in linked}
            link_count = int(linked[0][1].st_nlink)
            has_part = any(name.startswith(".normalized_") and name.endswith(".part") for name in names)
            finals = {name for name in names if name.startswith(_FINAL_PREFIX)}
            if (
                has_part
                and finals
                and finals.isdisjoint(referenced)
                and link_count == len(names)
            ):
                for name in names:
                    os.unlink(name, dir_fd=self._root_fd)
                self._fsync_root()

    @staticmethod
    def _hash_file(path: str) -> str:
        digest = hashlib.sha256()
        flags = os.O_RDONLY | (getattr(os, "O_NOFOLLOW", 0))
        fd = os.open(path, flags)
        try:
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        finally:
            os.close(fd)
        return digest.hexdigest()

    def _fsync_root(self) -> None:
        if self._root_fd is not None:
            os.fsync(self._root_fd)
