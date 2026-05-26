"""处理企微回调的 Aiohttp 视图."""
from __future__ import annotations

import hashlib
import logging
import xml.etree.ElementTree as ET
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.util import dt as dt_util

from .coordinator import WorkChatCoordinator

_LOGGER = logging.getLogger(__name__)

class WorkChatCallbackView(HomeAssistantView):
    """处理企微回调的视图，包含 Web 状态诊断页."""
    
    url = "/api/workchat_callback/{token}"
    name = "api:workchat_callback"
    requires_auth = False # 企微服务器访问不需要 HA 登录态

    def __init__(self, coordinator: WorkChatCoordinator) -> None:
        """初始化视图."""
        self.coordinator = coordinator

    def _verify_signature(self, sig: str, ts: str, nonce: str, data: str) -> bool:
        """校验企微签名."""
        try:
            token = self.coordinator.entry.data["token"]
            tmp = sorted([token, ts, nonce, data])
            return hashlib.sha1("".join(tmp).encode()).hexdigest() == sig
        except Exception:
            return False

    async def get(self, request: web.Request, token: str) -> web.Response:
        """GET 请求：处理企微 URL 验证或显示诊断页."""
        if token != self.coordinator.entry.data["token"]:
            return web.Response(status=403, text="Token 不匹配")
        
        q = request.query
        echostr = q.get("echostr")

        # --- 情况 A: 如果没有 echostr，则显示集成诊断状态页 ---
        if not echostr:
            # 尝试检查一次 Token 状态以确保诊断页显示最新信息
            current_token = await self.coordinator.api.get_access_token()
            
            status_data = {
                "has_token": current_token is not None,
                "agent_id": self.coordinator.api.agent_id,
                "external_url": self.coordinator.external_url,
                "last_msg_time": self.coordinator.last_msg_time
            }
            return web.Response(
                text=self._get_status_html(status_data), 
                content_type="text/html"
            )

        # --- 情况 B: 企微后台的 URL 有效性验证逻辑 ---
        sig = q.get("msg_signature")
        ts = q.get("timestamp")
        nonce = q.get("nonce")
        
        if self._verify_signature(sig, ts, nonce, echostr):
            try:
                # 在线程池中解密 (解密属于 CPU 密集型)
                decrypted = await self.coordinator.hass.async_add_executor_job(
                    self.coordinator.encryptor.decrypt, echostr
                )
                return web.Response(text=decrypted)
            except Exception as e:
                _LOGGER.error("回调 URL 解密验证失败: %s", e)
        
        return web.Response(status=400, text="签名验证失败")

    async def post(self, request: web.Request, token: str) -> web.Response:
        """POST 请求：接收并处理加密的消息推送."""
        if token != self.coordinator.entry.data["token"]:
            return web.Response(status=403)
            
        try:
            body = await request.text()
            root = ET.fromstring(body)
            encrypt_msg = root.find('Encrypt').text
            
            q = request.query
            if not self._verify_signature(q.get("msg_signature"), q.get("timestamp"), q.get("nonce"), encrypt_msg):
                _LOGGER.warning("收到未经授权的消息推送")
                return web.Response(status=401)

            # 解密消息正文
            decrypted_xml = await self.coordinator.hass.async_add_executor_job(
                self.coordinator.encryptor.decrypt, encrypt_msg
            )
            
            # 解析 XML 并分发数据
            await self._process_xml(decrypted_xml)
            
            # 企微要求收到消息后必须回复 success 或空串
            return web.Response(text="success")
            
        except Exception as err:
            _LOGGER.error("回调 POST 处理失败: %s", err)
            return web.Response(status=500)

    async def _process_xml(self, xml_str: str):
        """解析解密后的 XML 数据并交给 Coordinator 处理."""
        root = ET.fromstring(xml_str)
        msg_type = root.find("MsgType").text
        
        event_data = {
            "user": root.find("FromUserName").text,
            "type": msg_type,
            "agent_id": root.find("AgentID").text if root.find("AgentID") is not None else "",
            "timestamp": root.find("CreateTime").text,
        }

        # 根据消息类型提取特定字段
        if msg_type == "text":
            event_data["content"] = root.find("Content").text
        elif msg_type == "image":
            event_data["media_id"] = root.find("MediaId").text
            event_data["pic_url"] = root.find("PicUrl").text
        elif msg_type == "location":
            event_data.update({
                "lat": root.find("Location_X").text,
                "lon": root.find("Location_Y").text,
                "label": root.find("Label").text if root.find("Label") is not None else ""
            })
        elif msg_type == "event":
            event_name = root.find("Event").text
            event_data["type"] = "menu_click" if event_name == "click" else "event"
            event_data["event"] = event_name
            if (ek := root.find("EventKey")) is not None:
                event_data["event_key"] = ek.text

        # 将解析好的数据交给协调器，它会触发事件并更新传感器
        self.coordinator.process_callback_data(event_data)

    def _get_status_html(self, status_data: dict[str, Any]) -> str:
        """生成诊断状态页的 HTML (完整保留原版样式)."""
        has_token = status_data['has_token']
        token_status = "有效 (Ready)" if has_token else "未获取 (Error)"
        token_color = "#27ae60" if has_token else "#e74c3c"
        
        return f"""
        <!DOCTYPE html>
        <html lang="zh-CN">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>企微通集成状态诊断</title>
            <style>
                body {{ font-family: -apple-system, system-ui, sans-serif; background: #f0f2f5; margin: 0; padding: 20px; color: #1c1e21; }}
                .container {{ max-width: 500px; margin: 0 auto; background: #fff; padding: 25px; border-radius: 12px; box-shadow: 0 8px 24px rgba(0,0,0,0.1); }}
                .header {{ display: flex; align-items: center; border-bottom: 1px solid #ebedf0; margin-bottom: 20px; padding-bottom: 15px; }}
                .logo {{ background: #07c160; color: white; width: 40px; height: 40px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-weight: bold; margin-right: 12px; }}
                h2 {{ margin: 0; font-size: 1.25rem; }}
                .status-row {{ display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #f0f2f5; font-size: 14px; }}
                .status-label {{ color: #65676b; }}
                .status-value {{ font-weight: 500; font-family: monospace; }}
                .badge {{ padding: 2px 8px; border-radius: 4px; color: white; font-size: 12px; }}
                .footer {{ text-align: center; margin-top: 20px; font-size: 12px; color: #8a8d91; }}
                .log-box {{ background: #1c1e21; color: #a3e635; padding: 10px; border-radius: 6px; font-size: 12px; margin-top: 15px; overflow-x: auto; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <div class="logo">企</div>
                    <h2>企微通集成状态</h2>
                </div>
                <div class="status-row">
                    <span class="status-label">Access Token</span>
                    <span class="status-value" style="color: {token_color}">{token_status}</span>
                </div>
                <div class="status-row">
                    <span class="status-label">Agent ID</span>
                    <span class="status-value">{status_data['agent_id']}</span>
                </div>
                <div class="status-row">
                    <span class="status-label">Webhook 校验</span>
                    <span class="status-value" style="color: #27ae60">配置正常 ✓</span>
                </div>
                <div class="status-row">
                    <span class="status-label">最近消息时间</span>
                    <span class="status-value">{status_data['last_msg_time'] or '等待首条消息...'}</span>
                </div>
                <div class="log-box">
                    System: Service is running on Coordinator Architecture.<br>
                    External URL: {self.coordinator.callback_url}
                </div>
            </div>
            <div class="footer">WorkChat Integration for Home Assistant</div>
        </body>
        </html>
        """