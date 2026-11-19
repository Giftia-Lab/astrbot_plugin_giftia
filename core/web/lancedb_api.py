from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from ..memory.lancedb_versions import installed_lancedb_version


class LanceDBApi:
    """Expose LanceDB version status and manage a pending version change."""

    async def get_lancedb_status(self):
        """Read runtime and installation status without contacting the index."""
        versions = self.giftia.lancedb_versions
        return json_response(
            {
                "status": "success",
                "data": {
                    "running_version": self.giftia.ltm.lancedb_version,
                    "installed_version": installed_lancedb_version(),
                    "supported_range": str(versions.supported)
                    if versions.supported is not None
                    else "",
                    "switch_unavailable": versions.switch_unavailable,
                    "restart_deferred": versions.restart_deferred,
                    "plan": await versions.pending(),
                },
            }
        )

    async def get_lancedb_versions(self):
        """Check for available stable releases on PyPI."""
        try:
            versions = await self.giftia.lancedb_versions.available_versions(
                refresh=True
            )
        except Exception:
            logger.exception("[Giftia] Failed to query LanceDB releases")
            return error_response(
                "无法获取 LanceDB 版本列表，请稍后重试。", status_code=502
            )
        return json_response({"status": "success", "data": versions})

    async def set_lancedb_plan(self):
        """Schedule an allowed version or cancel the pending change."""
        versions = self.giftia.lancedb_versions
        if versions.switch_unavailable:
            return error_response(versions.switch_unavailable, status_code=403)
        if (request.content_type or "").split(";", 1)[
            0
        ].strip().lower() != "application/json":
            return error_response("请求需要 JSON 格式。")
        body = await request.json()
        if not isinstance(body, dict) or "version" not in body:
            return error_response("请选择 LanceDB 版本。")
        version = body["version"]
        if version is not None:
            if not isinstance(version, str) or len(version) > 64:
                return error_response("请选择可用的 LanceDB 版本。")
            if version == installed_lancedb_version():
                return error_response("已安装此版本。")
            try:
                available = await versions.available_versions()
            except Exception:
                logger.exception("[Giftia] Failed to check requested LanceDB release")
                return error_response("无法验证目标版本，请稍后重试。", status_code=502)
            if version not in available:
                return error_response("目标版本不在当前环境的可用列表中。")

        await versions.save_plan(version)
        return await self.get_lancedb_status()
