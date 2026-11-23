import asyncio
import base64
import re
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio
from core.conversation.media_captioner import MediaCaptioner
from core.database import migrations
from core.database.data_cache import DataCache
from core.database.database import Database
from core.database.schema import initialize_database
from core.utils import message_media, video_utils
from core.utils.http_manager import HttpManager
from core.utils.message_parse import MessageParser
from core.utils.schemas import MediaCaption
from core.web import media_api
from PIL import Image

from astrbot.api.message_components import Video
from astrbot.api.star import StarTools

COLLIDING_ID = "0123456789abcdef"
FIRST_URL = "https://example.com/first"
SECOND_URL = "https://example.com/second"


@pytest_asyncio.fixture
async def media_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(StarTools, "get_data_dir", lambda _: tmp_path)
    contents = {}

    async def download(url, *, max_bytes=None):
        return contents[url]

    http = SimpleNamespace(
        download_media=AsyncMock(side_effect=download),
        handle_image=HttpManager.handle_image,
    )
    caller = SimpleNamespace(
        plugin=None,
        call_llm_image_caption=AsyncMock(),
        call_llm_audio_caption=AsyncMock(),
        call_llm_video_caption=AsyncMock(
            return_value=MediaCaption(caption="Second video")
        ),
        media_caption_provider_ids=[],
    )
    async with aiosqlite.connect(tmp_path / "media.db") as conn:
        conn.row_factory = aiosqlite.Row
        await initialize_database(conn)
        db = Database(conn)

        def new_parser():
            return MessageParser(DataCache(db, http, None), http, True, True, caller)

        yield SimpleNamespace(
            db=db,
            parser=new_parser(),
            new_parser=new_parser,
            contents=contents,
            http=http,
            caller=caller,
            cache_dir=tmp_path / "media_cache",
        )


def bitmap(color, width):
    with BytesIO() as buffer:
        Image.new("RGB", (width, 4), color).save(buffer, "BMP")
        return buffer.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_size", [False, True])
async def test_image_collision_preserves_both_files_and_reuses_the_new_id(
    media_runtime, monkeypatch, same_size
):
    runtime = media_runtime
    monkeypatch.setattr(message_media, "xxh3_64_hexdigest", lambda _: COLLIDING_ID)
    first_bytes = bitmap("red", 4)
    second_bytes = bitmap("blue", 4 if same_size else 8)
    runtime.contents.update(
        {
            FIRST_URL: first_bytes,
            SECOND_URL: second_bytes,
            SECOND_URL + "?retry=1": second_bytes,
        }
    )
    runtime.cache_dir.mkdir()
    (runtime.cache_dir / COLLIDING_ID).write_bytes(first_bytes)
    # A pre-upgrade row only has size and cached bytes to verify its identity.
    await runtime.db.insert_media_caption(
        MediaCaption(
            hash_val=COLLIDING_ID,
            file_name="shared.bmp",
            url=FIRST_URL,
            media_type="image",
            file_size=len(first_bytes),
            caption="First image",
        )
    )
    runtime.caller.call_llm_image_caption.return_value = MediaCaption(
        caption="Second image"
    )
    formatter = runtime.parser.media_formatter
    first, duplicate = await asyncio.gather(
        formatter.format_image_ref(SECOND_URL, "shared.bmp", False),
        formatter.format_image_ref(SECOND_URL + "?retry=1", "shared.bmp", False),
    )
    [new_id] = first[1].media_id_list
    assert duplicate[1].media_id_list == [new_id]
    assert new_id != COLLIDING_ID and re.fullmatch(r"[0-9a-f]{16}", new_id)
    assert (runtime.cache_dir / COLLIDING_ID).read_bytes() == first_bytes
    assert (runtime.cache_dir / new_id).read_bytes() == second_bytes
    old = await runtime.db.get_media_caption_by_hash(COLLIDING_ID)
    new = await runtime.db.get_media_caption_by_hash(new_id)
    assert (old.caption, old.url, old.file_size) == (
        "First image",
        FIRST_URL,
        len(first_bytes),
    )
    assert (new.caption, new.file_size) == ("Second image", len(second_bytes))

    (runtime.cache_dir / new_id).unlink()
    reloaded = runtime.new_parser().media_formatter
    _, repeated = await reloaded.format_image_ref(SECOND_URL, "shared.bmp", False)
    assert repeated.media_id_list == [new_id]
    assert (runtime.cache_dir / new_id).read_bytes() == second_bytes
    runtime.caller.call_llm_image_caption.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_image_with_the_same_filename_gets_its_own_caption(media_runtime):
    runtime = media_runtime
    runtime.contents[FIRST_URL] = bitmap("red", 4)
    formatter = runtime.parser.media_formatter
    first_id, _ = await formatter.get_image_caption(
        FIRST_URL, "shared.bmp", custom_desc="First image"
    )
    runtime.contents[FIRST_URL] = bitmap("blue", 4)
    runtime.caller.call_llm_image_caption.return_value = MediaCaption(
        caption="Second image"
    )
    second_id, second = await formatter.get_image_caption(FIRST_URL, "shared.bmp")
    assert first_id != second_id
    assert second.caption == "Second image"


@pytest.mark.asyncio
async def test_old_media_size_detects_a_collision_without_a_cached_file(
    media_runtime, monkeypatch
):
    runtime = media_runtime
    monkeypatch.setattr(message_media, "xxh3_64_hexdigest", lambda _: COLLIDING_ID)
    await runtime.db.insert_media_caption(
        MediaCaption(
            hash_val=COLLIDING_ID, media_type="image", caption="First", file_size=1
        )
    )
    runtime.contents[SECOND_URL] = bitmap("blue", 4)
    new_id, new = await runtime.parser.media_formatter.get_image_caption(
        SECOND_URL, defer_caption=True
    )
    old = await runtime.db.get_media_caption_by_hash(COLLIDING_ID)
    assert new_id != COLLIDING_ID and not new.is_captioned
    assert (old.caption, old.file_size) == ("First", 1)


@pytest.mark.asyncio
async def test_audio_collision_transcribes_the_new_file_and_retains_the_old_one(
    media_runtime, monkeypatch
):
    runtime = media_runtime
    monkeypatch.setattr(message_media, "xxh3_64_hexdigest", lambda _: COLLIDING_ID)
    runtime.contents[FIRST_URL] = b"first voice"
    runtime.caller.call_llm_audio_caption.side_effect = [
        MediaCaption(caption="First audio"),
        MediaCaption(caption="Second audio"),
    ]
    formatter = runtime.parser.media_formatter
    _, first = await formatter.format_audio_ref(FIRST_URL, "voice.wav", False)
    runtime.contents[FIRST_URL] = b"other voice"
    _, second = await formatter.format_audio_ref(FIRST_URL, "voice.wav", False)
    [new_id] = second.media_id_list
    assert first.media_id_list == [COLLIDING_ID]
    assert new_id != COLLIDING_ID and re.fullmatch(r"[0-9a-f]{16}", new_id)
    assert (runtime.cache_dir / COLLIDING_ID).read_bytes() == b"first voice"
    assert (runtime.cache_dir / new_id).read_bytes() == b"other voice"
    old = await runtime.db.get_media_caption_by_hash(COLLIDING_ID)
    new = await runtime.db.get_media_caption_by_hash(new_id)
    assert (old.caption, new.caption, new.file_size) == (
        "First audio",
        "Second audio",
        11,
    )
    [caption_path] = runtime.caller.call_llm_audio_caption.call_args.args[0]
    assert Path(caption_path) == runtime.cache_dir / new_id

    runtime.contents[SECOND_URL] = b"other voice"
    reloaded = runtime.new_parser().media_formatter
    _, repeated = await reloaded.format_audio_ref(SECOND_URL, "voice.wav", False)
    assert repeated.media_id_list == [new_id]
    assert runtime.caller.call_llm_audio_caption.await_count == 2


@pytest.mark.asyncio
async def test_video_uses_the_whole_platform_filename_and_preserves_legacy_id(
    media_runtime, monkeypatch
):
    runtime = media_runtime
    monkeypatch.setattr(
        message_media, "get_remote_video_info", AsyncMock(return_value=(100, 5))
    )
    first_name = COLLIDING_ID + "a" * 16 + ".mp4"
    second_name = COLLIDING_ID + "b" * 16 + ".mp4"
    await runtime.db.insert_media_caption(
        MediaCaption(
            hash_val=COLLIDING_ID,
            file_name=first_name,
            url=FIRST_URL,
            media_type="video",
            file_size=100,
            caption="First video",
        )
    )
    formatter = runtime.parser.media_formatter
    _, second = await formatter.format_video_ref(SECOND_URL, second_name, True)
    [new_id] = second.media_id_list
    assert new_id != COLLIDING_ID and re.fullmatch(r"[0-9a-f]{16}", new_id)
    _, first = await formatter.format_video_ref(FIRST_URL, first_name, True)
    assert first.media_id_list == [COLLIDING_ID]
    old = await runtime.db.get_media_caption_by_hash(COLLIDING_ID)
    new = await runtime.db.get_media_caption_by_hash(new_id)
    assert (old.caption, old.file_name, old.url) == (
        "First video",
        first_name,
        FIRST_URL,
    )
    assert (new.caption, new.file_name, new.url) == ("", second_name, SECOND_URL)
    runtime.http.download_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_video_url_retains_resource_parameters_without_authentication(
    media_runtime, monkeypatch
):
    runtime = media_runtime
    monkeypatch.setattr(
        message_media, "get_remote_video_info", AsyncMock(return_value=(100, 5))
    )
    formatter = runtime.parser.media_formatter
    source = f"https://example.com/{COLLIDING_ID}.mp4"
    _, first = await formatter.format_video_ref(
        source + "?id=1&rkey=old&quality=sd", "video.mp4", True
    )
    _, refreshed = await formatter.format_video_ref(
        source + "?token=new&id=1&quality=sd", "video.mp4", True
    )
    _, other_id = await formatter.format_video_ref(
        source + "?id=2&quality=sd", "video.mp4", True
    )
    _, other_quality = await formatter.format_video_ref(
        source + "?id=1&quality=hd", "video.mp4", True
    )
    assert first.media_id_list == refreshed.media_id_list
    assert (
        len(
            {
                *first.media_id_list,
                *other_id.media_id_list,
                *other_quality.media_id_list,
            }
        )
        == 3
    )
    row = await runtime.db.get_media_caption_by_hash(first.media_id_list[0])
    assert row.url == source + "?token=new&id=1&quality=sd"
    runtime.http.download_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_video_comparison_uses_old_original_and_transcribes_new_original(
    media_runtime, monkeypatch
):
    runtime = media_runtime
    first_bytes = b"\x00\x00\x00\x0cftyp" + b"old!"
    second_bytes = b"\x00\x00\x00\x0cftyp" + b"new!"
    monkeypatch.setattr(
        message_media,
        "get_remote_video_info",
        AsyncMock(return_value=(len(first_bytes), 1)),
    )
    formatter = runtime.parser.media_formatter
    _, first = await formatter.format_video_ref(FIRST_URL, None, True)
    [first_id] = first.media_id_list
    old = await runtime.db.get_media_caption_by_hash(first_id)
    old.is_captioned = True
    old.caption = "First video"
    await runtime.db.update_media_caption(old)
    runtime.parser.data_cache.caption.clear()
    runtime.cache_dir.mkdir()
    old_file = runtime.cache_dir / f"{first_id}.mp4"
    old_file.write_bytes(first_bytes)

    runtime.contents[FIRST_URL] = second_bytes
    _, second = await formatter.format_video_ref(FIRST_URL, None, True)
    [new_id] = second.media_id_list
    assert new_id != first_id
    assert old_file.read_bytes() == first_bytes
    assert (runtime.cache_dir / new_id).read_bytes() == second_bytes
    old = await runtime.db.get_media_caption_by_hash(first_id)
    assert old.caption == "First video"
    new = await runtime.db.get_media_caption_by_hash(new_id)
    assert not new.is_captioned
    captioner = MediaCaptioner(
        SimpleNamespace(
            conf={},
            call_llm=runtime.caller,
            http_manager=runtime.http,
            data_cache=runtime.parser.data_cache,
        )
    )
    assert await captioner.transcribe_video_media(new) == "Second video"
    argument = runtime.caller.call_llm_video_caption.call_args.kwargs["video_url"]
    assert base64.b64decode(argument.split(",", 1)[1]) == second_bytes

    [forwarded] = await message_media.format_node_components(
        f"[视频:{first_id}]", runtime.db
    )
    assert forwarded.file == old_file.as_uri()
    monkeypatch.setattr(media_api, "json_response", lambda body: body)
    api = media_api.MediaApi(SimpleNamespace(db=runtime.db))
    old_preview = await api.get_media_file_b64(first_id)
    new_preview = await api.get_media_file_b64(new_id)
    assert base64.b64decode(old_preview["base64"]) == first_bytes
    assert base64.b64decode(new_preview["base64"]) == second_bytes
    assert old_preview["content_type"] == new_preview["content_type"] == "video/mp4"

    old_file.unlink()
    (runtime.cache_dir / new_id).unlink()
    reloaded = runtime.new_parser().media_formatter
    _, repeated = await reloaded.format_video_ref(FIRST_URL, None, True)
    assert repeated.media_id_list == [new_id]


@pytest.mark.asyncio
async def test_explicit_platform_id_is_used_in_normal_and_forwarded_video(
    media_runtime, monkeypatch
):
    runtime = media_runtime
    monkeypatch.setattr(
        message_media, "get_remote_video_info", AsyncMock(return_value=(100, 5))
    )
    data = {
        "file": "display-name.mp4",
        "url": FIRST_URL,
        "file_id": "platform-file/one",
    }
    segment = {"type": "video", "data": data}
    event = SimpleNamespace(
        platform_meta=SimpleNamespace(id="adapter"),
        message_obj=SimpleNamespace(raw_message=SimpleNamespace(message=[segment])),
    )
    parsed = await runtime.parser.chain_to_result(
        [Video(file=data["file"], url=data["url"])], defer_caption=True, event=event
    )
    data = {**data, "url": SECOND_URL, "file": "different-display-name.mp4"}
    forwarded = await runtime.parser.forward_parser.onebot_segments_to_result(
        [{"type": "video", "data": data}], True, event, {}, 1
    )
    other_file = await runtime.parser.forward_parser.onebot_segments_to_result(
        [{"type": "video", "data": {**data, "file_id": "platform-file/two"}}],
        True,
        event,
        {},
        1,
    )
    assert len(parsed.media_id_list) == 1
    assert parsed.media_id_list == forwarded.media_id_list
    assert parsed.media_id_list != other_file.media_id_list


@pytest.mark.asyncio
async def test_video_size_comes_from_the_matching_segment(monkeypatch):
    probe = AsyncMock()
    monkeypatch.setattr(video_utils, "_fetch_http_video_size", probe)
    segments = [
        {
            "type": "video",
            "data": {
                "url": FIRST_URL,
                "file": "shared.mp4",
                "file_size": 100,
                "duration": 4,
            },
        },
        {
            "type": "video",
            "data": {
                "url": SECOND_URL,
                "file": "shared.mp4",
                "file_size": 200,
                "duration": 8,
            },
        },
        {"type": "image", "data": {"file_size": 300}},
    ]
    event = SimpleNamespace(
        message_obj=SimpleNamespace(raw_message={"message": segments})
    )
    assert await video_utils.get_remote_video_info(
        FIRST_URL, "shared.mp4", event=event
    ) == (100, 4)
    assert await video_utils.get_remote_video_info(
        SECOND_URL, "shared.mp4", event=event
    ) == (200, 8)
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_media_identity_migration_keeps_existing_record(tmp_path):
    async with aiosqlite.connect(tmp_path / "v10.db") as conn:
        conn.row_factory = aiosqlite.Row
        for version, migration in migrations.MIGRATIONS:
            if version > 10:
                break
            await migration(conn)
            await conn.execute(f"PRAGMA user_version={version}")
        await conn.execute(
            "INSERT INTO media_caption(hash_val, media_type, caption, file_size) "
            "VALUES (?, 'image', 'Legacy', 200)",
            (COLLIDING_ID,),
        )
        await conn.commit()
        await initialize_database(conn)
        db = Database(conn)
        old = await db.get_media_caption_by_hash(COLLIDING_ID)
        assert (old.caption, old.file_size, old.content_digest, old.source_key) == (
            "Legacy",
            200,
            "",
            "",
        )
        old.content_digest = "digest"
        await db.insert_media_caption(old)
        assert await db.find_media_ids("image", "digest", "") == [COLLIDING_ID]
