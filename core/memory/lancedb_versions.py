import asyncio
import json
import platform
import sys
from contextlib import suppress
from importlib import invalidate_caches, metadata
from pathlib import Path
from time import monotonic

from aiohttp import ClientSession, ClientTimeout
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import InvalidWheelFilename, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from astrbot.api import logger
from astrbot.core import DEMO_MODE
from astrbot.core.utils.astrbot_path import get_astrbot_path
from astrbot.core.utils.runtime_env import is_packaged_desktop_runtime

from ..database.database import Database

_REQUIREMENTS_PATH = Path(__file__).resolve().parents[2] / "requirements.txt"
_PLAN_KEY = "lancedb_version_switch"
_INDEX_URL = "https://pypi.org/simple/lancedb/"


def installed_lancedb_version() -> str | None:
    """Read the installed version without importing the native module."""
    try:
        return metadata.version("lancedb")
    except metadata.PackageNotFoundError:
        return None


class LanceDBVersions:
    """Schedule a version change for startup, before LanceDB is imported."""

    def __init__(self, db: Database, session: ClientSession):
        self.db = db
        self.session = session
        try:
            self.supported = next(
                req.specifier
                for line in _REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines()
                if (spec := line.partition("#")[0].strip()) and not spec.startswith("-")
                if (req := Requirement(spec)).name.lower() == "lancedb"
            )
        except (OSError, UnicodeError, InvalidRequirement, StopIteration) as error:
            logger.warning("[Giftia] Cannot read LanceDB requirements: %s", error)
            self.supported = None
        self._available: list[str] = []
        self._checked_at: float | None = None
        self.restart_deferred = False
        self.switch_unavailable = ""
        if DEMO_MODE:
            self.switch_unavailable = "演示模式仅支持查看版本。"
        elif is_packaged_desktop_runtime():
            # The packaged runtime may preload LanceDB before plugin initialization.
            self.switch_unavailable = "桌面打包版请在 AstrBot 控制台切换版本。"
        elif self.supported is None:
            self.switch_unavailable = "无法读取 Giftia 的 LanceDB 依赖范围。"

    async def available_versions(self, *, refresh: bool = False) -> list[str]:
        """Find stable PyPI releases with a wheel for this interpreter and platform.

        Args:
            refresh: Fetch a fresh index even if a recent result is cached.

        Returns:
            Matching versions in descending order.
        """
        if self.supported is None:
            return []
        if (
            not refresh
            and self._checked_at is not None
            and monotonic() - self._checked_at < 300
        ):
            return self._available

        async with self.session.get(
            _INDEX_URL,
            headers={"Accept": "application/vnd.pypi.simple.v1+json"},
            timeout=ClientTimeout(total=8),
        ) as response:
            response.raise_for_status()
            index = await response.json()

        tags = set(sys_tags())
        python_version = Version(platform.python_version())
        versions: set[Version] = set()
        for file in index["files"]:
            filename = file["filename"]
            if file.get("yanked", False) is not False or not filename.endswith(".whl"):
                continue
            try:
                name, version, _, wheel_tags = parse_wheel_filename(filename)
                python_range = SpecifierSet(file.get("requires-python") or "")
            except (InvalidSpecifier, InvalidVersion, InvalidWheelFilename):
                continue
            if (
                name == "lancedb"
                and self.supported.contains(version, prereleases=False)
                and version.local is None
                and not wheel_tags.isdisjoint(tags)
                and python_range.contains(python_version, prereleases=True)
            ):
                versions.add(version)
        self._available = [str(version) for version in sorted(versions, reverse=True)]
        self._checked_at = monotonic()
        return self._available

    async def pending(self) -> dict:
        """Read the queued version and any error from the last attempt."""
        raw = await self.db.get_kv_data(_PLAN_KEY)
        if raw:
            try:
                saved = json.loads(raw)
                if isinstance(saved, dict) and isinstance(saved.get("version"), str):
                    return saved
            except (TypeError, ValueError):
                logger.warning("[Giftia] Invalid saved LanceDB version change")
        return {}

    async def save_plan(self, version: str | None) -> None:
        """Save or cancel a version change already validated by the dashboard.

        Args:
            version: Target release, or None to cancel a queued change.
        """
        if version is None:
            await self.db.delete_kv_data(_PLAN_KEY)
        else:
            await self.db.upsert_kv_data(_PLAN_KEY, json.dumps({"version": version}))
        self.restart_deferred = False

    async def apply_pending(self) -> None:
        """Apply a saved change before use and record failures for the dashboard."""
        if self.switch_unavailable or self.supported is None:
            return
        plan = await self.pending()
        if not plan or plan.get("error"):
            return
        target = plan["version"]
        try:
            version = Version(target)
            if (
                not self.supported.contains(version, prereleases=False)
                or version.local is not None
            ):
                await self.db.upsert_kv_data(
                    _PLAN_KEY,
                    json.dumps(
                        {
                            "version": target,
                            "error": "目标版本已超出当前 Giftia 的依赖范围。",
                        }
                    ),
                )
                return
            if installed_lancedb_version() != str(version):
                if "lancedb" in sys.modules or "lancedb._lancedb" in sys.modules:
                    self.restart_deferred = True
                    return
                await self._install(version)
                invalidate_caches()
                if installed_lancedb_version() != str(version):
                    raise RuntimeError(
                        "The installed LanceDB version did not match the request"
                    )
        except Exception:
            logger.exception("[Giftia] Failed to switch LanceDB to %r", target)
            await self.db.upsert_kv_data(
                _PLAN_KEY,
                json.dumps(
                    {"version": target, "error": "安装失败，请查看 AstrBot 日志。"}
                ),
            )
            return
        await self.save_plan(None)

    async def _install(self, version: Version) -> None:
        """Run AstrBot's configured installer in a disposable Python process.

        Args:
            version: Validated stable LanceDB release to install.

        Raises:
            RuntimeError: The installer reported a failure.
        """
        # The host installer invokes pip in-process. Keep pip's import hooks
        # out of the interpreter that will load LanceDB and other plugins.
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import asyncio, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from astrbot.core import pip_installer\n"
            "asyncio.run(pip_installer.install(package_name=sys.argv[2]))\n",
            get_astrbot_path(),
            f"lancedb=={version}\n--only-binary=:all:\n--no-input\n--timeout=15\n--retries=1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await process.communicate()
        except asyncio.CancelledError:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.terminate()
                await process.wait()
            raise
        if process.returncode:
            logger.error(
                "[Giftia] LanceDB installer output:\n%s",
                output[-8000:].decode(errors="replace"),
            )
            raise RuntimeError(f"Installer exited with code {process.returncode}")
