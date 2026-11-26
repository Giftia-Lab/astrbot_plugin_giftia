import asyncio
import base64 as b64_module
import contextlib
import os
import re
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field, replace
from hashlib import sha256
from itertools import chain, count
from pathlib import Path
from typing import Any

from xxhash import xxh3_64_hexdigest

from astrbot.api import logger
from astrbot.api.message_components import Image, Plain, Video
from astrbot.core.message.components import BaseMessageComponent
from astrbot.core.star.star_tools import StarTools

from ..database.data_cache import DataCache, is_temp_or_local_path
from ..llm.call_llm import CallLLM
from .event_utils import get_adapter_id, resolve_bot_name
from .http_manager import HttpManager
from .message_parse_types import ChainParseResult
from .path_security import original_media_path, save_media_cache_file
from .schemas import IMAGE_METADATA_PATTERN, MediaCaption
from .video_utils import format_duration, format_file_size, get_remote_video_info

# 支持的图片文件格式
SUPPORTED_FILE_FORMATS_WITH_DOT = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".mpo",
)
VIDEO_AUTH_QUERY_PARAMS = frozenset({"rkey", "token"})


class SessionCaptionRateLimiter:
    """Limit automatic image captions per session in a sliding time window."""

    def __init__(self, window_seconds: float = 60.0, max_requests: int = 8):
        self.window_seconds = window_seconds
        self.max_requests = max_requests
        self._history: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        session_key: str,
        *,
        received_at: float | None = None,
        check_only: bool = False,
    ) -> bool:
        """Reserve a caption slot for the incoming image.

        Args:
            session_key: Conversation whose caption quota applies.
            received_at: Monotonic receipt time, or None to use the current time.
            check_only: Check eligibility without reserving a slot.

        Returns:
            Whether to start captioning the image now.
        """
        if not session_key:
            return True
        async with self._lock:
            now = time.monotonic()
            received_at = now if received_at is None else min(received_at, now)
            if self.max_requests <= 0 or now - received_at > self.window_seconds:
                return False
            queue = self._history.get(session_key)
            # A slot that expired while the message was queued still counts for it.
            if (
                queue
                and len(queue) == self.max_requests
                and received_at - queue[0] <= self.window_seconds
            ):
                return False
            if check_only:
                return True
            if queue is None:
                queue = self._history[session_key] = deque(maxlen=self.max_requests)
            queue.append(now)
            if len(self._history) > 1000:
                # Recent arrivals can still look back one window from receipt.
                stale_keys = [
                    key
                    for key, timestamps in self._history.items()
                    if now - timestamps[-1] > 2 * self.window_seconds
                ]
                for key in stale_keys:
                    self._history.pop(key, None)
            return True


@dataclass
class ImageCaptionDelivery:
    senders: int = 0
    pending: dict[str, float] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None


class LockManager:
    def __init__(self):
        self._locks = {}
        self._global_lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def lock(self, key: str):
        async with self._global_lock:
            if key not in self._locks:
                self._locks[key] = [asyncio.Lock(), 0]
            lock_info = self._locks[key]
            lock_info[1] += 1

        try:
            async with lock_info[0]:
                yield
        finally:
            async with self._global_lock:
                lock_info[1] -= 1
                if lock_info[1] <= 0:
                    self._locks.pop(key, None)


class MessageMediaFormatter:
    def __init__(
        self,
        data_cache: DataCache,
        http_manager: HttpManager,
        image_caption_enabled: bool,
        audio_caption_enabled: bool,
        call_llm: CallLLM,
        url_locks=None,
        hash_locks=None,
        rate_limiter=None,
    ):
        self.data_cache = data_cache
        self.http_manager = http_manager
        self.image_caption_enabled = image_caption_enabled
        self.audio_caption_enabled = audio_caption_enabled
        self.call_llm = call_llm
        self.url_locks = url_locks if url_locks is not None else LockManager()
        self.hash_locks = hash_locks if hash_locks is not None else LockManager()
        self.rate_limiter = (
            rate_limiter if rate_limiter is not None else SessionCaptionRateLimiter()
        )
        self.image_caption_locks = LockManager()
        self._image_caption_deliveries: dict[tuple[str, str], ImageCaptionDelivery] = {}

    @contextlib.contextmanager
    def defer_image_captions(self, bot_name: str, group_or_user_id: str):
        """Postpone automatic image captions until all reply segments are sent.

        Args:
            bot_name: Bot delivering the reply.
            group_or_user_id: Destination conversation.

        Yields:
            Control to the sender while automatic caption requests are deferred.
        """
        session = (bot_name, group_or_user_id)
        delivery = self._image_caption_deliveries.setdefault(
            session, ImageCaptionDelivery()
        )
        delivery.senders += 1
        try:
            yield
        finally:
            delivery.senders -= 1
            if not delivery.senders and delivery.task is None:
                plugin = getattr(self.call_llm, "plugin", None)
                if delivery.pending and not getattr(plugin, "_terminated", False):
                    task = asyncio.create_task(
                        self._caption_deferred_images(session, delivery)
                    )
                    delivery.task = task
                    if (
                        running_tasks := getattr(plugin, "running_tasks", None)
                    ) is not None:
                        task_id = str(id(task))
                        running_tasks[task_id] = task
                        task.add_done_callback(
                            lambda _: running_tasks.pop(task_id, None)
                        )
                else:
                    self._image_caption_deliveries.pop(session)

    @staticmethod
    def first_media_url(url: str | None, file_name: str | None) -> str:
        for candidate in (url, file_name):
            if not isinstance(candidate, str):
                continue
            candidate = candidate.strip()
            if candidate.startswith(("http://", "https://", "file://")):
                return candidate
        return ""

    @staticmethod
    def filename_stable_hash(file_name: str | None) -> str | None:
        if not file_name or is_temp_or_local_path(file_name):
            return None
        stem = re.sub(r"\.[^.]+$", "", os.path.basename(file_name))
        if re.fullmatch(r"[a-fA-F0-9]{16,64}", stem):
            return stem.lower()[:16]
        return None

    @staticmethod
    def is_filename_stable_hash(hash_val: str | None) -> bool:
        return bool(hash_val and re.fullmatch(r"[a-fA-F0-9]{16,64}", str(hash_val)))

    @staticmethod
    def _legacy_video_hash(file_name: str | None, url: str) -> str:
        for reference in (file_name, url):
            if not reference or is_temp_or_local_path(reference):
                continue
            path = (
                urllib.parse.urlparse(reference).path
                if reference.startswith("http")
                else reference
            )
            stem = Path(path).stem
            if re.fullmatch(r"[a-fA-F0-9]{16,64}", stem):
                return stem.lower()[:16]
        if url.startswith(("http://", "https://")):
            parsed = urllib.parse.urlparse(url)
            url = parsed.netloc + parsed.path
        return xxh3_64_hexdigest((url or file_name or "").encode())

    @staticmethod
    def _video_reference(
        file_name: str | None,
        url: str,
        file_id: str | int | None = None,
        adapter_id: str = "",
    ) -> str:
        """Resolve a full platform ID or a URL retaining resource selectors.

        Args:
            file_name: Platform file reference or display name.
            url: Video URL or local path.
            file_id: Explicit ID supplied by the adapter.
            adapter_id: Scope for opaque platform IDs.

        Returns:
            Full source reference for video deduplication.
        """
        if isinstance(file_id, (str, int)) and str(file_id).strip():
            return f"file:{len(adapter_id)}:{adapter_id}:{str(file_id).strip()}"
        if file_name and not is_temp_or_local_path(file_name):
            match = re.fullmatch(r"([a-fA-F0-9]{16,64})(?:\.[a-zA-Z0-9]+)?", file_name)
            if match:
                return f"file:0::{match[1].lower()}"
        if url.startswith(("http://", "https://")):
            parsed = urllib.parse.urlsplit(url)
            query = "&".join(
                parameter
                for parameter in parsed.query.split("&")
                if urllib.parse.unquote_plus(parameter.partition("=")[0]).lower()
                not in VIDEO_AUTH_QUERY_PARAMS
            )
            return urllib.parse.urlunsplit(parsed._replace(query=query, fragment=""))
        return url or file_name or ""

    @staticmethod
    def _refresh_caption(
        existing: MediaCaption, incoming: MediaCaption
    ) -> MediaCaption:
        return replace(
            existing,
            url=incoming.url or existing.url,
            file_name=incoming.file_name or existing.file_name,
            duration=existing.duration or incoming.duration,
            file_size=incoming.file_size or existing.file_size,
            width=existing.width or incoming.width,
            height=existing.height or incoming.height,
            content_digest=incoming.content_digest or existing.content_digest,
            source_key=incoming.source_key or existing.source_key,
        )

    @contextlib.asynccontextmanager
    async def _media_slot(
        self,
        incoming: MediaCaption,
        content: bytes | None,
        *,
        legacy_id: str = "",
        video_limit: int = 0,
    ):
        """Reserve a matching short ID through the caller's database write.

        Args:
            incoming: Media metadata with the candidate ID and identity evidence.
            content: Original bytes, or None if a video can be fetched on demand.
            legacy_id: Pre-upgrade video key used only with a matching full reference.
            video_limit: Maximum bytes to download for a video comparison.

        Yields:
            The matching previous record, if any. The draft receives the final ID.
        """
        base_id = incoming.hash_val
        is_video = incoming.media_type == "video"
        identity = (
            incoming.source_key
            if is_video
            else (incoming.content_digest or incoming.source_key)
        )
        cache_dir = StarTools.get_data_dir("astrbot_plugin_giftia") / "media_cache"
        async with self.hash_locks.lock(f"{incoming.media_type}:{identity or base_id}"):
            known_ids = await self.data_cache.db.find_media_ids(
                incoming.media_type,
                "" if is_video else incoming.content_digest,
                incoming.source_key,
            )
            existing_ids = set(known_ids)
            if legacy_id:
                existing_ids.add(legacy_id)
            candidates = chain(
                dict.fromkeys(filter(None, (*known_ids, legacy_id, base_id))),
                (
                    sha256(f"giftia-media:{base_id}:{attempt}".encode()).hexdigest()[
                        :16
                    ]
                    for attempt in count(1)
                ),
            )
            for candidate in candidates:
                async with self.hash_locks.lock(candidate):
                    existing = await self.data_cache.get_caption_by_hash(candidate)
                    if (
                        existing is None
                        and candidate in existing_ids
                        and candidate != base_id
                    ):
                        continue
                    if existing and existing.media_type != incoming.media_type:
                        continue

                    cached = original_media_path(cache_dir, candidate)
                    try:
                        cached_size = (
                            cached.stat().st_size if cached.is_file() else None
                        )
                    except OSError:
                        cached_size = None
                    old_size = (
                        cached_size
                        if cached_size is not None
                        else (existing.file_size if existing else 0)
                    )
                    if (
                        old_size > 0
                        and incoming.file_size > 0
                        and old_size != incoming.file_size
                    ):
                        continue

                    previous_source = existing.source_key if existing else ""
                    if existing and not previous_source and incoming.source_key:
                        reference = existing.url or ""
                        if is_video:
                            reference = self._video_reference(
                                existing.file_name, reference
                            )
                        if reference:
                            previous_source = sha256(reference.encode()).hexdigest()
                    source_differs = bool(
                        previous_source and incoming.source_key != previous_source
                    )
                    if is_video and source_differs:
                        continue

                    if (
                        content is None
                        and incoming.url
                        and video_limit > 0
                        and incoming.file_size <= video_limit
                        and (
                            cached_size is not None
                            or (existing and existing.content_digest)
                        )
                    ):
                        content = await self.http_manager.download_media(
                            incoming.url, max_bytes=video_limit
                        )
                        if content:
                            incoming.file_size = len(content)
                            incoming.content_digest = sha256(content).hexdigest()
                            if old_size > 0 and old_size != incoming.file_size:
                                continue

                    bytes_match = False
                    if content and cached_size is not None:
                        try:
                            bytes_match = (
                                await asyncio.to_thread(cached.read_bytes) == content
                            )
                        except OSError:
                            cached_size = None
                        else:
                            if not bytes_match:
                                continue
                    old_digest = existing.content_digest if existing else ""
                    if old_digest and incoming.content_digest:
                        if old_digest != incoming.content_digest:
                            continue
                    elif source_differs and not bytes_match:
                        continue
                    if not existing and cached_size is not None and not bytes_match:
                        continue
                    if (
                        existing
                        and candidate == legacy_id
                        and not previous_source
                        and not bytes_match
                        and not (old_digest and old_digest == incoming.content_digest)
                    ):
                        continue

                    if content and cached_size is None:
                        path = cache_dir / candidate
                        saved = save_media_cache_file(path, content)
                        if not saved and path.is_file():
                            try:
                                if await asyncio.to_thread(path.read_bytes) != content:
                                    continue
                            except FileNotFoundError:
                                pass
                    incoming.hash_val = candidate
                    yield existing
                    return

    async def _caption_from_filename(
        self, file_name: str | None, media_type: str
    ) -> MediaCaption | None:
        if not file_name or is_temp_or_local_path(file_name):
            return None
        _, caption = await self.data_cache.get_caption_by_filename(file_name)
        if not caption and media_type == "image":
            legacy_id = self.filename_stable_hash(file_name)
            if legacy_id:
                legacy = await self.data_cache.get_caption_by_hash(legacy_id)
                if legacy and legacy.file_name == file_name:
                    caption = legacy
        return caption if caption and caption.media_type == media_type else None

    async def format_image_ref(
        self,
        url: str,
        file_name: str | None,
        defer_caption: bool,
        custom_desc: str | None = None,
        event=None,
        *,
        received_at: float | None = None,
        caption_session: tuple[str, str] | None = None,
    ) -> tuple[str, ChainParseResult]:
        result = ChainParseResult()
        decision_url = self.first_media_url(url, file_name)
        if decision_url:
            result.image_urls.append(decision_url)

        if not url and not file_name:
            return "[图片]", result
        # Resolve bot_name and group_or_user_id from event
        bot_name = ""
        group_or_user_id = ""
        if caption_session is not None:
            bot_name, group_or_user_id = caption_session
        elif event and getattr(self.call_llm, "plugin", None):
            bot_name = resolve_bot_name(self.call_llm.plugin, event)
            group_or_user_id = event.get_group_id() or event.get_sender_id() or ""

        hash_val, media_caption = await self.get_image_caption(
            url,
            file_name,
            defer_caption,
            custom_desc=custom_desc,
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            received_at=received_at,
        )
        if not hash_val or not media_caption:
            return "[图片]", result

        result.media_id_list.append(hash_val)
        tag = f"[图片:{hash_val}]"
        details = []
        if media_caption.width > 0 and media_caption.height > 0:
            details.append(f"尺寸:{media_caption.width}×{media_caption.height}px")
        if size := format_file_size(media_caption.file_size):
            details.append(f"大小:{size}")
        if details:
            tag += "[" + ", ".join(details) + "]"
        return tag, result

    async def format_audio_ref(
        self, url: str, file_name: str | None, defer_caption: bool, event=None
    ) -> tuple[str, ChainParseResult]:
        result = ChainParseResult()
        decision_url = self.first_media_url(url, file_name)
        if decision_url:
            result.audio_urls.append(decision_url)

        if not self.audio_caption_enabled or not (url or file_name):
            return "[语音]", result
        # Resolve bot_name and group_or_user_id from event
        bot_name = ""
        group_or_user_id = ""
        if event and getattr(self.call_llm, "plugin", None):
            bot_name = resolve_bot_name(self.call_llm.plugin, event)
            group_or_user_id = event.get_group_id() or event.get_sender_id() or ""

        hash_val, media_caption = await self.get_audio_caption(
            url,
            file_name,
            defer_caption,
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
        )
        if hash_val and media_caption:
            result.media_id_list.append(hash_val)
            return f"[语音:{hash_val}]", result
        return "[语音]", result

    async def format_video_ref(
        self,
        url: str,
        file_name: str | None,
        defer_caption: bool,
        event=None,
        path: str | None = None,
        file_id: str | int | None = None,
    ) -> tuple[str, ChainParseResult]:
        result = ChainParseResult()
        decision_url = self.first_media_url(url, file_name)
        source = decision_url or url or path or ""

        # 1. 尝试多维获取远程/本地视频元数据 (file_size, duration)
        file_size, duration = await get_remote_video_info(
            url=decision_url or url or "",
            file_name=str(file_id or file_name or ""),
            path=path or "",
            event=event,
        )

        # 2. 从配置获取限制
        caption_config = {}
        if self.call_llm and hasattr(self.call_llm, "plugin") and self.call_llm.plugin:
            caption_config = getattr(self.call_llm.plugin, "conf", {}).get(
                "caption_config", {}
            )

        max_size_mb = caption_config.get("video_max_file_size_mb", 50)
        max_size_bytes = max_size_mb * 1024 * 1024

        reference = self._video_reference(
            file_name, source, file_id, get_adapter_id(event)
        )
        if not reference:
            return "[视频]", result
        legacy_id = self._legacy_video_hash(file_name, source)
        content = None
        source = source or file_name or ""
        local_source = source if is_temp_or_local_path(source) else path
        if is_temp_or_local_path(local_source) and file_size <= max_size_bytes:
            content = await self.http_manager.download_media(
                local_source, max_bytes=max_size_bytes
            )
        incoming = MediaCaption(
            hash_val=xxh3_64_hexdigest(reference.encode()),
            file_name=file_name or "",
            url=source,
            media_type="video",
            duration=duration,
            file_size=len(content) if content else file_size,
            content_digest=sha256(content).hexdigest() if content else "",
            source_key=sha256(reference.encode()).hexdigest(),
            is_captioned=False,
        )
        async with self._media_slot(
            incoming, content, legacy_id=legacy_id, video_limit=max_size_bytes
        ) as existing:
            size_str = format_file_size(incoming.file_size)
            dur_str = format_duration(duration)
            tag = f"[视频:{incoming.hash_val}]"
            if existing:
                media_caption = self._refresh_caption(existing, incoming)
            else:
                media_caption = incoming
            if incoming.file_size > max_size_bytes:
                tag += f"[文件过大:{size_str}]"
                if not existing:
                    media_caption.is_captioned = True
                    media_caption.text = media_caption.caption = (
                        f"[视频体积过大已限制转述: {size_str}]"
                    )
            elif dur_str and size_str:
                tag += f"[时长:{dur_str}, 大小:{size_str}]"
            elif size_str:
                tag += f"[大小:{size_str}]"
            elif dur_str:
                tag += f"[时长:{dur_str}]"
            await self.data_cache.set_caption(media_caption)

        result.media_id_list.append(incoming.hash_val)
        return tag, result

    async def get_image_caption(
        self,
        url: str,
        file_name: str | None = None,
        defer_caption: bool = False,
        custom_desc: str | None = None,
        bot_name: str = "",
        group_or_user_id: str = "",
        *,
        received_at: float | None = None,
    ) -> tuple[str | None, MediaCaption | None]:
        """Get or generate an image caption using the confirmed media identity.

        Args:
            url: Image source URL.
            file_name: Platform file reference, local path, or base64 payload.
            defer_caption: Store the image for later captioning.
            custom_desc: Supplied description to store on the matching image.
            bot_name: Bot requesting the caption.
            group_or_user_id: Conversation requesting the caption.
            received_at: Monotonic receipt time used for the automatic caption quota.

        Returns:
            The resolved media ID and caption, or two empty values.
        """
        source = url or file_name or ""
        async with self.url_locks.lock(source):
            content = b""
            file_reference = file_name or source
            if is_temp_or_local_path(file_reference):
                content = await self.http_manager.download_media(file_reference)
            elif file_reference.startswith("base64://"):
                payload = file_reference[9:].split(",", 1)[-1]
                try:
                    content = b64_module.b64decode(payload)
                except ValueError as error:
                    logger.error(f"[Giftia] Could not decode base64 image: {error}")
            if not content and source != file_reference:
                content = await self.http_manager.download_media(source)
            elif not content and self.first_media_url(source, None):
                content = await self.http_manager.download_media(source)

            if not content:
                legacy = await self._caption_from_filename(file_name, "image")
                if legacy and (not url or not legacy.url or url == legacy.url):
                    return legacy.hash_val, legacy
                return None, None

        base_id = xxh3_64_hexdigest(content)
        width, height, file_size = await asyncio.to_thread(
            HttpManager.get_image_metadata, content
        )
        incoming = MediaCaption(
            hash_val=base_id,
            url=source,
            file_name=file_name or "",
            media_type="image",
            width=width,
            height=height,
            file_size=file_size,
            content_digest=sha256(content).hexdigest(),
            is_captioned=False,
        )
        async with self._media_slot(incoming, content) as existing:
            if incoming.url.startswith("base64://"):
                incoming.url = f"base64://{incoming.hash_val}"
            if incoming.file_name.startswith("base64://"):
                incoming.file_name = f"base64://{incoming.hash_val}"

            if existing:
                incoming = self._refresh_caption(existing, incoming)
            elif not custom_desc and incoming.hash_val == base_id:
                legacy = await self._caption_from_filename(file_name, "image")
                if legacy and await self._matches_legacy_image(
                    legacy, incoming, content
                ):
                    incoming = self._refresh_caption(legacy, incoming)
                    incoming.hash_val = base_id

            if custom_desc:
                incoming = replace(
                    incoming, caption=custom_desc, genre="表情包", is_captioned=True
                )
            elif not self.image_caption_enabled:
                incoming.is_captioned = True
            await self.data_cache.set_caption(incoming)

        # Delivery and message logging can reuse the original while a model call
        # for the same image is already running.
        if not incoming.is_captioned and not defer_caption:
            incoming = (
                await self._caption_image(
                    incoming.hash_val,
                    (bot_name, group_or_user_id),
                    received_at,
                    content=content,
                    content_digest=incoming.content_digest,
                )
                or incoming
            )
        return incoming.hash_val, incoming

    def _queue_image_while_sending(
        self, media_id: str, session: tuple[str, str], received_at: float
    ) -> bool:
        """Keep a bounded set of images eligible to caption after the reply.

        Args:
            media_id: Cached original image identifier.
            session: Bot and destination of the originating message.
            received_at: Monotonic receipt time for the rate limiter.

        Returns:
            Whether a reply is currently being sent for this session.
        """
        delivery = self._image_caption_deliveries.get(session)
        if delivery is None or not delivery.senders:
            return False
        now = time.monotonic()
        for queued_id, queued_at in list(delivery.pending.items()):
            if now - queued_at > self.rate_limiter.window_seconds:
                del delivery.pending[queued_id]
        if (
            now - received_at <= self.rate_limiter.window_seconds
            and len(delivery.pending) < self.rate_limiter.max_requests
        ):
            delivery.pending.setdefault(media_id, received_at)
        return True

    async def _caption_image(
        self,
        media_id: str,
        session: tuple[str, str],
        received_at: float | None,
        *,
        content: bytes | None = None,
        content_digest: str | None = None,
    ) -> MediaCaption | None:
        """Apply regular captioning rules to an image with a confirmed media ID.

        Args:
            media_id: Resolved image identifier.
            session: Bot and destination of the originating message.
            received_at: Monotonic receipt time for the rate limiter.
            content: Original bytes if the image was just parsed.
            content_digest: Identity of the supplied original bytes, if available.

        Returns:
            The caption record when available.
        """
        received_at = time.monotonic() if received_at is None else received_at
        if self._queue_image_while_sending(media_id, session, received_at):
            return None
        bot_name, group_or_user_id = session
        session_key = f"{bot_name}:{group_or_user_id}" if any(session) else ""
        if session_key and not await self.rate_limiter.acquire(
            session_key, received_at=received_at, check_only=True
        ):
            logger.info(
                "[Giftia] Image caption deferred by rate limit: session=%s hash=%s",
                session_key,
                media_id,
            )
            return None

        async with self.image_caption_locks.lock(media_id):
            image = await self.data_cache.get_caption_by_hash(media_id)
            if (
                image is None
                or image.media_type != "image"
                or (
                    content_digest is not None
                    and image.content_digest != content_digest
                )
            ):
                return None
            if image.is_captioned or self._queue_image_while_sending(
                media_id, session, received_at
            ):
                return image
            if content is None:
                cache_dir = (
                    StarTools.get_data_dir("astrbot_plugin_giftia") / "media_cache"
                )
                content = await asyncio.to_thread((cache_dir / media_id).read_bytes)
            base64s, _ = await asyncio.to_thread(
                self.http_manager.handle_image, content
            )
            # A reply may become ready while the image is being prepared.
            if self._queue_image_while_sending(media_id, session, received_at):
                return image
            if session_key and not await self.rate_limiter.acquire(
                session_key, received_at=received_at
            ):
                return image
            described = None
            if base64s:
                described = await self.call_llm.call_llm_image_caption(
                    base64s, bot_name=bot_name, group_or_user_id=group_or_user_id
                )

            async with self.hash_locks.lock(media_id):
                current = await self.data_cache.get_caption_by_hash(media_id)
                if (
                    current is None
                    or current.media_type != "image"
                    or current.content_digest != image.content_digest
                ):
                    return None
                if current.is_captioned:
                    return current
                current = replace(current, is_captioned=True)
                if described:
                    current.genre = described.genre
                    current.character = described.character
                    current.source = described.source
                    current.text = described.text
                    current.caption = described.caption
                await self.data_cache.update_caption(current)
                return current

    async def _caption_deferred_images(
        self, session: tuple[str, str], delivery: ImageCaptionDelivery
    ) -> None:
        """Caption queued originals, yielding to any newly accepted reply.

        Args:
            session: Bot and destination that deferred captioning.
            delivery: Current send count and queued image IDs for that session.
        """
        plugin = getattr(self.call_llm, "plugin", None)
        try:
            while (
                delivery.pending
                and not delivery.senders
                and not getattr(plugin, "_terminated", False)
            ):
                media_id = next(iter(delivery.pending))
                received_at = delivery.pending.pop(media_id)
                try:
                    await self._caption_image(media_id, session, received_at)
                except FileNotFoundError:
                    continue
                except Exception:
                    logger.exception(
                        "[Giftia] Deferred image caption failed: %s", media_id
                    )
        finally:
            delivery.task = None
            if not delivery.senders:
                self._image_caption_deliveries.pop(session)

    @staticmethod
    async def _matches_legacy_image(
        legacy: MediaCaption, incoming: MediaCaption, content: bytes
    ) -> bool:
        """Validate an old filename alias before carrying its description forward.

        Args:
            legacy: The image row associated with the legacy filename.
            incoming: Metadata for the original image just received.
            content: Original image bytes.

        Returns:
            Whether the legacy description can be carried forward.
        """
        if legacy.content_digest or (
            legacy.file_size and legacy.file_size != incoming.file_size
        ):
            return False
        if re.fullmatch(r"[a-fA-F0-9]{16,64}", legacy.hash_val):
            path = (
                StarTools.get_data_dir("astrbot_plugin_giftia")
                / "media_cache"
                / legacy.hash_val
            )
            if path.is_file():
                try:
                    return path.stat().st_size == len(content) and (
                        await asyncio.to_thread(path.read_bytes) == content
                    )
                except OSError:
                    return False
        return not legacy.url or legacy.url == incoming.url

    async def get_audio_caption(
        self,
        url: str,
        file_name: str | None = None,
        defer_caption: bool = False,
        bot_name: str = "",
        group_or_user_id: str = "",
    ) -> tuple[str | None, MediaCaption | None]:
        """Get or generate an audio caption using the confirmed media identity.

        Args:
            url: Audio source URL.
            file_name: Platform file reference or local path.
            defer_caption: Store the audio for later captioning.
            bot_name: Bot requesting the caption.
            group_or_user_id: Conversation requesting the caption.

        Returns:
            The resolved media ID and caption, or two empty values.
        """
        source = url or file_name or ""
        local_source = file_name if is_temp_or_local_path(file_name) else None
        if not local_source and is_temp_or_local_path(source):
            local_source = source
        if not local_source and not self.first_media_url(source, None):
            legacy = await self._caption_from_filename(file_name, "audio")
            return (legacy.hash_val, legacy) if legacy else (None, None)
        async with self.url_locks.lock(source):
            content = await self.http_manager.download_media(local_source or source)
            if not content and local_source and local_source != source:
                content = await self.http_manager.download_media(source)

        incoming = MediaCaption(
            hash_val=xxh3_64_hexdigest(
                content if local_source and content else source.encode()
            ),
            file_name=file_name or "",
            url=source,
            media_type="audio",
            file_size=len(content),
            source_key=sha256(source.encode()).hexdigest(),
            content_digest=sha256(content).hexdigest() if content else "",
            is_captioned=False,
        )
        async with self._media_slot(incoming, content) as existing:
            if existing:
                incoming = self._refresh_caption(existing, incoming)
            if not incoming.is_captioned and not defer_caption:
                cached = (
                    StarTools.get_data_dir("astrbot_plugin_giftia")
                    / "media_cache"
                    / incoming.hash_val
                )
                described = await self.call_llm.call_llm_audio_caption(
                    [str(cached) if cached.is_file() else source],
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                )
                if described:
                    incoming.genre = described.genre
                    incoming.character = described.character
                    incoming.source = described.source
                    incoming.text = described.text
                    incoming.caption = described.caption
                    incoming.is_captioned = True
            await self.data_cache.set_caption(incoming)
            return incoming.hash_val, incoming


async def format_node_components(
    content_str: str,
    db: Any = None,
    media_cache_map: dict[str, BaseMessageComponent] | None = None,
) -> list[BaseMessageComponent]:
    """将消息文本及媒体标签 [图片:hash] [视频:hash] [语音:hash] 解析为可发送的 AstrBot 消息组件"""
    if not content_str:
        return [Plain("[无内容]")]

    if media_cache_map is None:
        media_cache_map = {}

    cache_dir = StarTools.get_data_dir("astrbot_plugin_giftia") / "media_cache"

    # 匹配所有媒体标签 [图片:xxx], [视频:xxx], [语音:xxx]
    media_tag_pattern = re.compile(r"(\[(?:图片|语音|视频):[^\]\s]+\])")
    tokens = media_tag_pattern.split(IMAGE_METADATA_PATTERN.sub(r"\1", content_str))
    components: list[BaseMessageComponent] = []

    for token in tokens:
        if not token:
            continue

        img_match = re.match(r"^\[图片:([^\]\s]+)\]$", token)
        video_match = re.match(r"^\[视频:([^\]\s]+)\]$", token)
        audio_match = re.match(r"^\[语音:([^\]\s]+)\]$", token)

        if img_match:
            hash_val = img_match.group(1)
            cached_comp = media_cache_map.get(f"img_{hash_val}")
            if cached_comp:
                components.append(cached_comp)
                continue

            cache_file = cache_dir / hash_val
            comp: BaseMessageComponent
            if cache_file.is_file():
                comp = Image.fromFileSystem(str(cache_file))
            elif db and hasattr(db, "get_media_caption_by_hash"):
                try:
                    caption = await db.get_media_caption_by_hash(hash_val)
                    if caption and caption.url:
                        comp = Image.fromURL(caption.url)
                    else:
                        comp = Plain("[图片]")
                except Exception:
                    comp = Plain("[图片]")
            else:
                comp = Plain("[图片]")

            media_cache_map[f"img_{hash_val}"] = comp
            components.append(comp)

        elif video_match:
            hash_val = video_match.group(1)
            cached_comp = media_cache_map.get(f"vid_{hash_val}")
            if cached_comp:
                components.append(cached_comp)
                continue

            cache_file = cache_dir / hash_val
            comp_v: BaseMessageComponent
            if cache_file.is_file():
                comp_v = Video.fromFileSystem(str(cache_file))
            elif db and hasattr(db, "get_media_caption_by_hash"):
                try:
                    caption = await db.get_media_caption_by_hash(hash_val)
                    if caption and caption.media_type == "video":
                        cache_file = original_media_path(cache_dir, hash_val)
                    if cache_file.is_file():
                        comp_v = Video.fromFileSystem(str(cache_file))
                    elif caption and caption.url:
                        comp_v = Video.fromURL(caption.url)
                    else:
                        comp_v = Plain("[视频]")
                except Exception:
                    comp_v = Plain("[视频]")
            else:
                comp_v = Plain("[视频]")

            media_cache_map[f"vid_{hash_val}"] = comp_v
            components.append(comp_v)

        elif audio_match:
            # 语音消息不支持合并转发，占位即可
            components.append(Plain("[语音]"))
        else:
            components.append(Plain(token))

    return components or [Plain("[无内容]")]
