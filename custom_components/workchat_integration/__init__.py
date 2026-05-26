"""企微通集成入口."""
from __future__ import annotations

import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers.network import get_url
from homeassistant.const import Platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN, 
    PLATFORMS, 
    CONF_EXTERNAL_URL, 
    CONF_CORP_ID, 
    CONF_SECRET, 
    CONF_AGENT_ID, 
    CONF_PROXY,
    CONF_AES_KEY,
    CONF_TOKEN
)
from .api import WorkChatApi
from .coordinator import WorkChatCoordinator
from .encrypt_helper import EncryptHelper
from .views import WorkChatCallbackView

_LOGGER = logging.getLogger(__name__)

# 定义类型别名，方便代码提示
type WorkChatConfigEntry = ConfigEntry[WorkChatCoordinator]

async def async_setup_entry(hass: HomeAssistant, entry: WorkChatConfigEntry) -> bool:
    """设置集成入口."""

    # 1. 初始化底层 API 客户端
    api = WorkChatApi(
        session=async_get_clientsession(hass),
        corp_id=entry.data[CONF_CORP_ID],
        secret=entry.data[CONF_SECRET],
        agent_id=entry.data[CONF_AGENT_ID],
        proxy=entry.data.get(CONF_PROXY)
    )

    # 2. 初始化协调器
    coordinator = WorkChatCoordinator(hass, api, entry)
    
    # 3. 初始化并注入加密助手和外部 URL
    coordinator.encryptor = EncryptHelper(
        entry.data[CONF_AES_KEY], 
        entry.data[CONF_TOKEN]
    )
    coordinator.external_url = entry.data.get(CONF_EXTERNAL_URL) or get_url(hass)

    # 4. 将协调器存入 runtime_data
    entry.runtime_data = coordinator

    # 5. 注册 Webhook 回调视图
    hass.http.register_view(WorkChatCallbackView(coordinator))

    # --- 6. 注册自定义动作 (与 services.yaml 对应) ---

    async def handle_notify(call: ServiceCall):
        """处理 workchat_integration.notify 动作."""
        # 对应 YAML 中的所有字段，通过 **call.data 透传
        await coordinator.async_send_message(**call.data)

    async def handle_upload_media(call: ServiceCall):
        """处理 workchat_integration.upload_media 动作."""
        # 对应 YAML 中的 type, file_path, file_name 字段
        media_id = await coordinator.async_upload_media_file(
            media_type=call.data["type"],
            file_path=call.data["file_path"],
            file_name=call.data.get("file_name")
        )
        # 返回 media_id，供自动化流水线后续步骤使用
        return {"media_id": media_id}

    # 注册发送通知动作
    hass.services.async_register(
        DOMAIN, 
        "notify", 
        handle_notify
    )

    # 注册媒体上传动作 (支持服务响应)
    hass.services.async_register(
        DOMAIN, 
        "upload_media", 
        handle_upload_media, 
        supports_response=SupportsResponse.ONLY
    )

    # 7. 启动关联平台 (Sensor, Notify)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True

async def async_unload_entry(hass: HomeAssistant, entry: WorkChatConfigEntry) -> bool:
    """卸载集成入口."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    if unload_ok:
        # 清理已注册的动作
        hass.services.async_remove(DOMAIN, "notify")
        hass.services.async_remove(DOMAIN, "upload_media")
        
    return unload_ok