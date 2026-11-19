from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio
from core.conversation import media_captioner
from core.conversation.media_captioner import MediaCaptioner
from core.database.data_cache import DataCache
from core.database.database import Database
from core.database.schema import initialize_database
from core.llm.prompt import (
    build_decision_prompt,
    build_reply_prompt,
    build_reply_review_prompt,
    parse_caption_to_str,
)
from core.utils.http_manager import HttpManager
from core.utils.message_media import MessageMediaFormatter
from core.utils.schemas import MediaCaption, MessageData, XmlLlmResult
from core.utils.video_utils import format_file_size
from PIL import Image
from xxhash import xxh3_64_hexdigest

from astrbot.api.message_components import Plain


@pytest.fixture(params=["PNG", "GIF"])
def image_bytes(request):
    buffer = BytesIO()
    image = Image.new("RGB", (24, 12), "red")
    if request.param == "GIF":
        image.save(
            buffer,
            format="GIF",
            save_all=True,
            append_images=[Image.new("RGB", (24, 12), "blue")],
            duration=100,
        )
    else:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest_asyncio.fixture
async def metadata_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(media_captioner.StarTools, "get_data_dir", lambda _: tmp_path)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await initialize_database(conn)
        db = Database(conn)
        cache = DataCache(db, None, None, msg_number=20)
        plugin = SimpleNamespace(
            db=db,
            data_cache=cache,
            conf={"caption_config": {"image_caption_enabled": False}},
            http_manager=SimpleNamespace(download_media=AsyncMock()),
        )
        yield SimpleNamespace(
            plugin=plugin,
            cache=cache,
            db=db,
            captioner=MediaCaptioner(plugin),
            cache_dir=tmp_path / "media_cache",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "disabled",
        "deferred",
        "limited",
        "custom",
        "legacy",
        "failed",
        "unprocessed",
        "success",
    ],
)
async def test_received_images_keep_original_metadata_through_caption_paths(
    metadata_runtime, image_bytes, mode
):
    runtime = metadata_runtime
    if mode == "legacy":
        await runtime.cache.set_caption(
            MediaCaption(
                hash_val="a" * 16,
                file_name="upload.png",
                media_type="image",
                caption="Legacy caption",
            )
        )
    http = SimpleNamespace(
        download_media=AsyncMock(return_value=image_bytes),
        handle_image=HttpManager.handle_image,
    )
    caller = SimpleNamespace(
        plugin=runtime.plugin,
        call_llm_image_caption=AsyncMock(
            return_value=None if mode == "failed" else MediaCaption(caption="A picture")
        ),
    )
    if mode == "unprocessed":
        http.handle_image = lambda _: ([], False)
    formatter = MessageMediaFormatter(
        data_cache=runtime.cache,
        http_manager=http,
        image_caption_enabled=mode != "disabled",
        audio_caption_enabled=False,
        call_llm=caller,
        rate_limiter=SimpleNamespace(acquire=AsyncMock(return_value=mode != "limited")),
    )
    tag, result = await formatter.format_image_ref(
        "https://example.com/image",
        file_name="upload.png",
        defer_caption=mode == "deferred",
        custom_desc="Custom sticker" if mode == "custom" else None,
        event=SimpleNamespace(get_group_id=lambda: "group"),
    )

    [media_id] = result.media_id_list
    media = await runtime.cache.get_caption_by_hash(media_id)
    assert media_id == xxh3_64_hexdigest(image_bytes)
    assert (
        tag
        == f"[图片:{media_id}][尺寸:24×12px, 大小:{format_file_size(len(image_bytes))}]"
    )
    assert (media.width, media.height, media.file_size) == (24, 12, len(image_bytes))
    assert media.is_captioned == (mode not in {"deferred", "limited"})
    assert (runtime.cache_dir / media_id).read_bytes() == image_bytes
    assert caller.call_llm_image_caption.await_count == int(
        mode in {"failed", "success"}
    )

    # A different URL for identical bytes must reuse the same image metadata.
    _, repeated = await formatter.get_image_caption(
        "https://example.com/another-url",
        file_name="another-upload.png",
        defer_caption=True,
    )
    assert (repeated.width, repeated.height, repeated.file_size) == (
        24,
        12,
        len(image_bytes),
    )
    runtime.cache.caption.clear()
    runtime.cache.filename_to_hash.clear()
    for restored in (
        await runtime.cache.get_caption_by_hash(media_id),
        (await runtime.cache.get_caption_by_filename("another-upload.png"))[1],
    ):
        assert (restored.width, restored.height, restored.file_size) == (
            24,
            12,
            len(image_bytes),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_metadata", [False, True])
async def test_explicit_image_refresh_fills_missing_metadata_and_keeps_known_values(
    metadata_runtime, image_bytes, existing_metadata
):
    runtime = metadata_runtime
    media = MediaCaption(
        hash_val=xxh3_64_hexdigest(image_bytes),
        media_type="image",
        caption="Old caption",
        width=24 if existing_metadata else 0,
        height=12 if existing_metadata else 0,
        file_size=len(image_bytes) if existing_metadata else 0,
    )
    await runtime.cache.set_caption(media)
    runtime.cache_dir.mkdir()
    (runtime.cache_dir / media.hash_val).write_bytes(image_bytes)
    runtime.plugin.conf["caption_config"]["image_caption_enabled"] = True
    runtime.plugin.call_llm = SimpleNamespace(
        inspect_media_provider_ids=["inspect"],
        call_llm_image_caption=AsyncMock(
            return_value=MediaCaption(caption="New caption")
        ),
    )
    runtime.plugin.http_manager.handle_image = HttpManager.handle_image

    _, result = await runtime.captioner.inspect_media(
        media.hash_val, question="Describe the image again"
    )

    assert "New caption" in result
    restored = await runtime.db.get_media_caption_by_hash(media.hash_val)
    assert restored.caption == "New caption"
    assert (restored.width, restored.height, restored.file_size) == (
        24,
        12,
        len(image_bytes),
    )
    runtime.plugin.http_manager.download_media.assert_not_awaited()


@pytest.mark.parametrize("caption_kind", ["short", "long", "repeated", "none"])
def test_stored_image_metadata_reaches_each_prompt_without_duplication(caption_kind):
    media = MediaCaption(
        hash_val="0123456789abcdef",
        media_type="image",
        width=1920,
        height=1080,
        file_size=2048,
        caption="A picture"
        if caption_kind != "long"
        else "A long picture description " * 10,
        is_captioned=caption_kind != "none",
    )
    if caption_kind == "none":
        media.caption = ""
    placeholder = f"[图片:{media.hash_val}]"
    details = "[尺寸:1920×1080px, 大小:2.0KB]"
    messages = [
        MessageData(
            content=placeholder + details,
            message_id=str(index),
            user_id="user",
            media_id_list=[media.hash_val],
        )
        for index in range(2 if caption_kind == "repeated" else 1)
    ]
    captions = [media] if media.is_captioned else []

    reply_prompt = build_reply_prompt(
        recent_messages=[],
        reply_messages=messages,
        media_captions=captions,
        bot_status=None,
    )

    assert reply_prompt.count(details) == len(messages)
    if caption_kind == "short":
        assert "[描述:画面: A picture]" in reply_prompt
    assert ("<media_content>" in reply_prompt) == (caption_kind in {"long", "repeated"})
    assert all(message.content == placeholder + details for message in messages)

    decision_prompt = build_decision_prompt(
        user_id="user",
        group_data="",
        recent_messages=[],
        pending_messages=messages,
        media_captions=captions,
        bot_status=None,
    )
    review_prompt = build_reply_review_prompt(
        reply_messages=messages,
        new_messages=[],
        llm_result=XmlLlmResult(msg_chains=[[Plain("Reply")]]),
        media_captions=captions,
        nickname="Bot",
        self_id="self",
        session_id="group",
    )
    for context in (decision_prompt, review_prompt):
        assert context.count(details) == len(messages)
    caption_xml = parse_caption_to_str(media)
    assert "尺寸:" not in caption_xml and "大小:" not in caption_xml


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "width,height,file_size,expected",
    [
        (0, 0, 0, ""),
        (100, 0, 0, ""),
        (100, 200, 0, "[尺寸:100×200px]"),
        (0, 0, 512, "[大小:512B]"),
    ],
)
async def test_new_image_reference_omits_unknown_metadata_fields(
    metadata_runtime, width, height, file_size, expected
):
    media = MediaCaption(
        hash_val="0123456789abcdef",
        media_type="image",
        width=width,
        height=height,
        file_size=file_size,
    )
    formatter = MessageMediaFormatter(
        data_cache=metadata_runtime.cache,
        http_manager=None,
        image_caption_enabled=False,
        audio_caption_enabled=False,
        call_llm=None,
    )
    formatter.get_image_caption = AsyncMock(return_value=(media.hash_val, media))
    tag, result = await formatter.format_image_ref(
        "https://example.com/image", None, True
    )
    assert tag == f"[图片:{media.hash_val}]{expected}"
    assert result.media_id_list == [media.hash_val]


def test_unreadable_image_still_has_a_file_size():
    assert HttpManager.get_image_metadata(b"Unrecognized image data") == (0, 0, 23)
