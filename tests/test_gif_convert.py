"""Exercise GIF conversion and safe sticker paths against real SDK components."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.utils.emoji_manager import GIF_CACHE_SUBDIR, resolve_sticker_path
from core.utils.gif_convert import GifConverter
from PIL import Image as PILImage

from astrbot.api.message_components import Image


class ResolveStickerPathTests(unittest.TestCase):
    """resolve_sticker_path 是 EmojiManager / GifConverter / StickerApi 共用的
    防穿越入口，任何情况下都不能解析出 stickers 目录之外的路径。"""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp()) / "stickers"
        (self.base / GIF_CACHE_SUBDIR).mkdir(parents=True)
        (self.base / "thumbnails").mkdir(parents=True)
        self.resolved_base = self.base.resolve()

    def _rel(self, name, is_thumbnail=False, subdir=""):
        got = resolve_sticker_path(self.base, name, is_thumbnail, subdir)
        if got is None:
            return None
        # 不变量：任何非 None 结果都必须留在 base 内
        self.assertTrue(got.is_relative_to(self.resolved_base), f"越界: {got}")
        return got.relative_to(self.resolved_base).as_posix()

    def test_resolves_into_base_and_subdirs(self):
        self.assertEqual(self._rel("abc.png"), "abc.png")
        self.assertEqual(self._rel("abc", is_thumbnail=True), "thumbnails/abc")
        self.assertEqual(
            self._rel("abc.gif", subdir=GIF_CACHE_SUBDIR), f"{GIF_CACHE_SUBDIR}/abc.gif"
        )

    def test_subdir_takes_precedence_over_is_thumbnail(self):
        self.assertEqual(
            self._rel("abc.gif", is_thumbnail=True, subdir=GIF_CACHE_SUBDIR),
            f"{GIF_CACHE_SUBDIR}/abc.gif",
        )

    def test_traversal_in_name_is_neutralized_not_escaped(self):
        # 设计是「只取文件名」把穿越中和掉，落回 base 内，而不是报错
        self.assertEqual(self._rel("../evil.png"), "evil.png")
        self.assertEqual(self._rel("../../evil.png"), "evil.png")
        self.assertEqual(self._rel("..\\..\\evil.png"), "evil.png")
        self.assertEqual(self._rel("/etc/passwd"), "passwd")
        self.assertEqual(self._rel("C:\\Windows\\win.ini"), "win.ini")
        self.assertEqual(
            self._rel("../../evil.gif", subdir=GIF_CACHE_SUBDIR),
            f"{GIF_CACHE_SUBDIR}/evil.gif",
        )

    def test_empty_and_dot_names_rejected(self):
        for name in ("", ".", ".."):
            self.assertIsNone(self._rel(name), f"应拒绝: {name!r}")

    def test_malicious_subdir_rejected(self):
        # subdir 由调用方给，只接受单层普通名字
        for subdir in ("../../etc", "a/b", "..\\etc", ".."):
            self.assertIsNone(self._rel("x.gif", subdir=subdir), f"应拒绝: {subdir!r}")


class GifConverterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stickers = Path(tempfile.mkdtemp()).resolve() / "stickers"
        self.stickers.mkdir()
        (self.stickers / "thumbnails").mkdir()

        self.png = self.stickers / "aaa11111.png"
        PILImage.new("RGBA", (64, 64), (255, 0, 0, 200)).save(self.png)

        self.already_gif = self.stickers / "ddd44444.gif"
        PILImage.new("P", (24, 24)).save(self.already_gif, format="GIF")

        self.broken = self.stickers / "eee55555.png"
        self.broken.write_bytes(b"\x89PNG\r\n\x1a\nNOT ACTUALLY A PNG")

        self.outside = self.stickers.parent / "outside.png"
        PILImage.new("RGB", (10, 10)).save(self.outside)

        emoji_manager = SimpleNamespace(stickers_dir=self.stickers)
        self.conv = GifConverter(emoji_manager)

    def _make_animated_webp(self, name: str, frame_count: int) -> Path:
        path = self.stickers / name
        frames = [
            PILImage.new("RGB", (32, 32), (i * 5 % 256, 0, 0))
            for i in range(frame_count)
        ]
        frames[0].save(
            path,
            format="WEBP",
            save_all=True,
            append_images=frames[1:],
            duration=80,
            loop=0,
        )
        return path

    # ── 判定 ─────────────────────────────────────────────────────────────

    def test_accepts_sticker_originals(self):
        self.assertEqual(self.conv.resolve_sticker_file(Image(str(self.png))), self.png)
        # file:// 形式也要认（部分适配器会带协议前缀）
        self.assertEqual(
            self.conv.resolve_sticker_file(Image(self.png.as_uri())), self.png
        )

    def test_skips_files_that_must_not_be_converted(self):
        cases = {
            "源文件已是 GIF": str(self.already_gif),
            "stickers 目录外（绘图结果等）": str(self.outside),
            "缩略图子目录": str(self.stickers / "thumbnails" / "x"),
            "网络 URL": "https://example.com/a.png",
            "base64": "base64://AAAA",
            "空": "",
            "不存在的文件": str(self.stickers / "nope.png"),
        }
        for label, value in cases.items():
            with self.subTest(label):
                self.assertIsNone(self.conv.resolve_sticker_file(Image(value)))

    # ── 转换与缓存 ────────────────────────────────────────────────────────

    async def test_static_image_converts_to_real_gif(self):
        path = await self.conv.get_gif_path(self.png)
        self.assertIsNotNone(path)
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes()[:4], b"GIF8")
        # 缓存按 sticker_id 命名放在 gif_cache/，删表情包时才好定位
        self.assertEqual(path.parent.name, GIF_CACHE_SUBDIR)
        self.assertEqual(path.name, "aaa11111.gif")

    async def test_second_call_reuses_cache_without_rewriting(self):
        first = await self.conv.get_gif_path(self.png)
        mtime = first.stat().st_mtime_ns
        second = await self.conv.get_gif_path(self.png)
        self.assertEqual(second, first)
        self.assertEqual(second.stat().st_mtime_ns, mtime)

    async def test_animation_is_capped_at_max_frames(self):
        source = self._make_animated_webp("ccc33333.webp", GifConverter.MAX_FRAMES + 20)
        path = await self.conv.get_gif_path(source)
        self.assertIsNotNone(path)
        with PILImage.open(path) as im:
            frames = getattr(im, "n_frames", 1)
        self.assertGreater(frames, 1, "动图不应被压成单帧")
        self.assertLessEqual(frames, GifConverter.MAX_FRAMES)

    async def test_broken_file_returns_none_instead_of_raising(self):
        # 转换失败必须静默回退，否则一张坏图就会中断整条回复
        with self.assertLogs("astrbot", level="WARNING"):
            self.assertIsNone(await self.conv.get_gif_path(self.broken))

    async def test_oversized_output_is_discarded(self):
        original = GifConverter.MAX_OUTPUT_BYTES
        GifConverter.MAX_OUTPUT_BYTES = 10
        try:
            big = self.stickers / "fff66666.png"
            PILImage.new("RGB", (200, 200), (7, 200, 90)).save(big)
            with self.assertLogs("astrbot", level="WARNING"):
                self.assertIsNone(await self.conv.get_gif_path(big))
        finally:
            GifConverter.MAX_OUTPUT_BYTES = original

        cache_dir = self.stickers / GIF_CACHE_SUBDIR
        self.assertFalse((cache_dir / "fff66666.gif").exists(), "不该留下超限产物")
        self.assertEqual(list(cache_dir.glob("*.tmp")), [], "不该留下 .tmp 残file")

    # ── 发送副本 ──────────────────────────────────────────────────────────

    async def test_build_send_chain_replaces_only_stickers(self):
        text_marker = object()
        original = [
            Image(str(self.png)),
            text_marker,
            Image(str(self.outside)),
            Image(str(self.already_gif)),
        ]
        snapshot = [getattr(c, "file", c) for c in original]

        sent = await self.conv.build_send_chain(original)

        # 原链必须原封不动：调用方随后要用它入库，换成 GIF 会污染 [图片:<sticker_id>]
        self.assertIsNot(sent, original)
        self.assertEqual([getattr(c, "file", c) for c in original], snapshot)

        self.assertTrue(sent[0].file.endswith("aaa11111.gif"))
        self.assertIs(sent[1], text_marker, "非 Image 组件应原样保留")
        self.assertIs(sent[2], original[2], "目录外图片不该被替换")
        self.assertIs(sent[3], original[3], "已是 GIF 的不该被替换")

    async def test_build_send_chain_returns_input_when_nothing_to_convert(self):
        # 没有可转项时原样返回入参，避免无谓的列表拷贝
        plain = [Image(str(self.outside)), object()]
        self.assertIs(await self.conv.build_send_chain(plain), plain)

        empty = []
        self.assertIs(await self.conv.build_send_chain(empty), empty)


if __name__ == "__main__":
    unittest.main()
