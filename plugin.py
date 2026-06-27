"""
MaiBot Minecraft 适配器插件。

通过 WebSocket 连接到 Minecraft 服务器上的 mod/plugin 提供的 WS 服务，实现：
- AI 可通过 Tool 执行 MC 服务器命令
- MC 聊天消息通过 MessageGateway 双向同步到 MaiBot 群聊流
"""

import asyncio
import time
import uuid
from typing import Any

import aiohttp
from pydantic import Field

from maibot_sdk import MaiBotPlugin, MessageGateway, Tool
from maibot_sdk.config import PluginConfigBase
from maibot_sdk.types import ToolParameterInfo, ToolParamType

GATEWAY_NAME = "minecraft_gateway"


# ============================================================
# 配置模型
# ============================================================


class PluginSection(PluginConfigBase):
    """插件基础设置（别动）"""

    __ui_label__ = "插件设置"

    config_version: str = Field(
        default="1.0.0",
        description="配置版本号，由 Runner 管理，请勿手动修改",
        json_schema_extra={"label": "配置版本", "disabled": True},
    )
    enabled: bool = Field(
        default=True,
        description="是否启用本插件",
        json_schema_extra={"label": "启用插件"},
    )


class ConnectionConfig(PluginConfigBase):
    """WebSocket 连接配置。"""

    __ui_label__ = "连接设置"

    ws_url: str = Field(
        default="ws://localhost:8080/minecraft",
        description="MC 服务器 WebSocket 地址",
        json_schema_extra={
            "label": "WS 地址",
            "placeholder": "ws://localhost:8080/minecraft",
        },
    )
    auth_token: str = Field(
        default="",
        description="WS 鉴权令牌，留空则不鉴权",
        json_schema_extra={
            "label": "鉴权令牌",
            "placeholder": "可选，由 MC 端 mod/plugin 配置",
        },
    )
    auto_reconnect: bool = Field(
        default=True,
        description="断开后是否自动重连",
        json_schema_extra={"label": "自动重连"},
    )
    reconnect_interval: int = Field(
        default=10,
        ge=1,
        description="重连间隔（秒）",
        json_schema_extra={"label": "重连间隔（秒）"},
    )
    heartbeat_interval: int = Field(
        default=30,
        ge=5,
        description="心跳间隔（秒）",
        json_schema_extra={"label": "心跳间隔（秒）"},
    )


class ChatConfig(PluginConfigBase):
    """聊天同步配置。"""

    __ui_label__ = "聊天设置"

    enabled: bool = Field(
        default=True,
        description="是否启用聊天同步",
        json_schema_extra={"label": "启用聊天同步"},
    )
    show_player_join_leave: bool = Field(
        default=False,
        description="是否在 MaiBot 中显示玩家加入/离开消息",
        json_schema_extra={"label": "显示玩家进出"},
    )
    chat_format: str = Field(
        default="[{player_name}] {message}",
        description="MC 聊天消息在 MaiBot 中的显示格式。{player_name} = 玩家名, {message} = 消息内容",
        json_schema_extra={
            "label": "聊天格式",
            "placeholder": "[{player_name}] {message}",
        },
    )
    ai_player_name: str = Field(
        default="MaiBot",
        description="AI 在 MC 游戏内显示的名称",
        json_schema_extra={"label": "AI 玩家名", "placeholder": "MaiBot"},
    )
    group_name: str = Field(
        default="Minecraft 服务器",
        description="在 MaiBot 中显示的群聊名称，会出现在 AI 的上下文中",
        json_schema_extra={"label": "群聊名称", "placeholder": "Minecraft 服务器"},
    )
    is_group: bool = Field(
        default=True,
        description="是否以群聊模式注册。MC 服务器通常为群聊；若连接单个玩家则为私聊",
        json_schema_extra={"label": "群聊模式"},
    )


class MinecraftAdapterConfig(PluginConfigBase):
    """Minecraft 适配器插件配置。"""

    __ui_label__ = "Minecraft 适配器"

    plugin: PluginSection = Field(default_factory=PluginSection)
    connection: ConnectionConfig = Field(default_factory=ConnectionConfig)
    chat: ChatConfig = Field(default_factory=ChatConfig)


# ============================================================
# 插件主体
# ============================================================


class MinecraftAdapterPlugin(MaiBotPlugin):
    """Minecraft 适配器插件。

    通过 WebSocket 与 MC 服务器通信，提供命令执行 Tool 和聊天消息网关。
    """

    config_model = MinecraftAdapterConfig

    def __init__(self) -> None:
        super().__init__()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws_connected: bool = False
        self._shutting_down: bool = False
        self._connect_task: asyncio.Task[Any] | None = None
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._pending_commands: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._reconnect_count: int = 0

    # ============================================================
    # 生命周期
    # ============================================================

    async def on_load(self) -> None:
        self.ctx.logger.info("Minecraft 适配器插件已加载")
        self._shutting_down = False
        self._connect_task = asyncio.create_task(self._ws_connect_loop())

    async def on_unload(self) -> None:
        self.ctx.logger.info("Minecraft 适配器插件正在卸载")
        self._shutting_down = True
        if self._connect_task:
            self._connect_task.cancel()
            self._connect_task = None
        await self._report_gateway_offline()
        await self._cleanup_connection()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != "self":
            return
        self.ctx.logger.info("插件配置已更新，将断开当前连接并重连")
        if self._connect_task:
            self._connect_task.cancel()
            self._connect_task = None
        await self._cleanup_connection()
        self._shutting_down = False
        self._connect_task = asyncio.create_task(self._ws_connect_loop())

    # ============================================================
    # MessageGateway — 双向消息网关
    # ============================================================

    @MessageGateway(
        route_type="duplex",
        name=GATEWAY_NAME,
        platform="minecraft",
        protocol="minecraft_ws",
        description="Minecraft 服务器消息网关，负责 MC 聊天与 MaiBot 之间的双向同步",
    )
    async def send_to_minecraft(self, message: Any, route: Any = None, metadata: Any = None, **kwargs: Any) -> dict[str, Any]:
        """将 MaiBot 消息发送到 Minecraft 服务器。

        由 Host 在需要向 MC 发送消息时调用。
        """
        if not self._ws_connected:
            return {"success": False, "reason": "未连接到 Minecraft 服务器"}

        texts = self._extract_texts(message)
        if not texts:
            return {"success": False, "reason": "消息内容为空"}

        try:
            for text in texts:
                await self._send_ws({
                    "type": "chat",
                    "player_name": self.config.chat.ai_player_name,
                    "message": text,
                })
            return {"success": True}
        except ConnectionError as e:
            return {"success": False, "reason": str(e)}

    # ============================================================
    # Tool — 执行 MC 命令
    # ============================================================

    @Tool(
        "execute_mc_command",
        brief_description="在 Minecraft 服务器上以控制台身份执行命令",
        detailed_description=(
            "参数说明：\n"
            "- command：string，必填。要在 MC 服务器上执行的命令（不需要前导斜杠 /）。\n"
            "  示例：'say 你好'、'list'、'time set day'、'give Steve diamond 64'"
        ),
        parameters=[
            ToolParameterInfo(
                name="command",
                param_type=ToolParamType.STRING,
                description="要执行的 MC 命令（不含 /），如 'say Hello'、'list'、'time set day'",
                required=True,
            ),
        ],
    )
    async def handle_execute_command(self, command: str = "", **kwargs: Any) -> dict[str, Any]:
        """执行 Minecraft 服务器命令并等待结果返回。"""
        if not self._ws_connected:
            return {"success": False, "output": "错误：未连接到 Minecraft 服务器"}

        if not command.strip():
            return {"success": False, "output": "错误：命令不能为空"}

        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_commands[request_id] = future

        try:
            await self._send_ws({
                "type": "command",
                "request_id": request_id,
                "command": command.strip(),
            })
            result = await asyncio.wait_for(future, timeout=10.0)
            return result
        except asyncio.TimeoutError:
            return {"success": False, "output": f"命令执行超时 (10s): /{command.strip()}"}
        except ConnectionError as e:
            return {"success": False, "output": f"连接错误: {e}"}
        finally:
            self._pending_commands.pop(request_id, None)

    # ============================================================
    # WebSocket 连接管理
    # ============================================================

    async def _ws_connect_loop(self) -> None:
        """WebSocket 连接主循环，负责连接保持和自动重连。"""
        while not self._shutting_down:
            auth_failed = await self._connect_and_run()

            if self._shutting_down:
                break

            if auth_failed:
                self.ctx.logger.warning("鉴权失败，停止重连。请检查 auth_token 配置")
                await self._report_gateway_offline()
                break

            conn_config = self.config.connection
            if not conn_config.auto_reconnect:
                self.ctx.logger.info("自动重连已禁用")
                await self._report_gateway_offline()
                break

            delay = conn_config.reconnect_interval
            self._reconnect_count += 1
            self.ctx.logger.info("将在 %d 秒后尝试第 %d 次重连...", delay, self._reconnect_count)
            await asyncio.sleep(delay)

    async def _connect_and_run(self) -> bool:
        """建立 WS 连接并进入消息接收循环。

        Returns:
            bool: True 表示因鉴权失败而退出，不应重连。
        """
        conn_config = self.config.connection

        try:
            self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(
                conn_config.ws_url,
                heartbeat=conn_config.heartbeat_interval,
            )
        except aiohttp.ClientResponseError as e:
            self.ctx.logger.error(
                "连接 MC 服务器失败 (%s): HTTP %s %s。"
                "若通过 frp/nginx 等反向代理连接，请确保使用 TCP/stream 模式而非 HTTP 模式，"
                "HTTP 代理通常不支持 WebSocket 协议升级。",
                conn_config.ws_url, e.status, e.message,
            )
            await self._close_session()
            return False
        except aiohttp.ClientConnectorError as e:
            self.ctx.logger.error(
                "无法连接 MC 服务器 (%s): %s。请检查地址是否正确、服务器是否已启动",
                conn_config.ws_url, e,
            )
            await self._close_session()
            return False
        except Exception as e:
            self.ctx.logger.error(
                "连接 MC 服务器失败 (%s): %s: %s",
                conn_config.ws_url, type(e).__name__, e,
            )
            await self._close_session()
            return False

        self.ctx.logger.info("已连接到 MC 服务器: %s", conn_config.ws_url)

        try:
            # 鉴权
            if conn_config.auth_token:
                await self._send_ws_raw({"type": "auth", "token": conn_config.auth_token})
                try:
                    auth_resp = await asyncio.wait_for(
                        self._ws.receive_json(), timeout=10.0  # type: ignore[union-attr]
                    )
                except asyncio.TimeoutError:
                    self.ctx.logger.error("鉴权超时（10s）")
                    return False

                if auth_resp.get("type") == "auth_fail":
                    self.ctx.logger.error("鉴权失败: %s", auth_resp.get("reason", "未知原因"))
                    return True  # 鉴权失败不重连
                self.ctx.logger.info("鉴权成功")

            self._ws_connected = True
            self._reconnect_count = 0
            await self._report_gateway_ready()
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            await self._receive_loop()
        except Exception as e:
            self.ctx.logger.error("WS 运行时异常: %s", e)
        finally:
            await self._cleanup_connection()
        return False

    async def _receive_loop(self) -> None:
        """消息接收循环。"""
        while not self._shutting_down and self._ws_connected and self._ws is not None:
            try:
                msg = await asyncio.wait_for(
                    self._ws.receive_json(),  # type: ignore[union-attr]
                    timeout=self.config.connection.heartbeat_interval + 15,
                )
            except asyncio.TimeoutError:
                continue
            except aiohttp.WebSocketError as e:
                self.ctx.logger.warning("WS 连接断开 (WebSocketError): %s", e)
                break
            except aiohttp.ClientConnectionError as e:
                self.ctx.logger.warning("WS 连接断开 (ConnectionError): %s", e)
                break
            except Exception as e:
                self.ctx.logger.error("接收消息异常 (%s): %s", type(e).__name__, e)
                break

            if isinstance(msg, dict):
                await self._dispatch_ws_message(msg)

    async def _heartbeat_loop(self) -> None:
        """心跳保活循环。"""
        interval = self.config.connection.heartbeat_interval
        while not self._shutting_down and self._ws_connected:
            await asyncio.sleep(interval)
            if not self._ws_connected:
                break
            try:
                await self._send_ws_raw({"type": "ping"})
            except Exception:
                break

    async def _cleanup_connection(self) -> None:
        """清理 WS 连接和相关资源。"""
        self._ws_connected = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        await self._close_session()
        self._fail_pending_commands()

    async def _close_session(self) -> None:
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    def _fail_pending_commands(self) -> None:
        """将未完成的命令请求标记为失败。"""
        for future in self._pending_commands.values():
            if not future.done():
                future.set_result({
                    "success": False,
                    "output": "错误：WebSocket 连接已断开",
                })
        self._pending_commands.clear()

    # ============================================================
    # WS 消息发送
    # ============================================================

    async def _send_ws(self, data: dict[str, Any]) -> None:
        """发送 WS 消息（JSON 格式），连接断开时抛出 ConnectionError。"""
        if not self._ws_connected or self._ws is None:
            raise ConnectionError("未连接到 MC 服务器")
        try:
            await self._ws.send_json(data)
        except Exception as e:
            self._ws_connected = False
            raise ConnectionError(f"发送消息失败: {e}")

    async def _send_ws_raw(self, data: dict[str, Any]) -> None:
        """发送 WS 消息，不检查连接状态（用于发送前的短暂窗口）。"""
        if self._ws is None:
            raise ConnectionError("WS 未初始化")
        await self._ws.send_json(data)

    # ============================================================
    # WS 消息分发
    # ============================================================

    async def _dispatch_ws_message(self, msg: dict[str, Any]) -> None:
        """根据消息类型分发处理。"""
        msg_type = msg.get("type", "")

        if msg_type == "chat":
            await self._handle_chat(msg)
        elif msg_type == "command_result":
            self._handle_command_result(msg)
        elif msg_type == "player_join":
            await self._handle_player_event(msg, "加入了服务器")
        elif msg_type == "player_leave":
            await self._handle_player_event(msg, "离开了服务器")
        elif msg_type == "server_start":
            self.ctx.logger.info("MC 服务器已启动")
        elif msg_type == "server_stop":
            self.ctx.logger.info("MC 服务器已停止")
        elif msg_type == "pong":
            pass
        elif msg_type == "error":
            self.ctx.logger.warning("MC 服务器返回错误: %s", msg.get("message", ""))

    # ============================================================
    # 消息处理
    # ============================================================

    async def _handle_chat(self, msg: dict[str, Any]) -> None:
        """处理 MC 聊天消息，路由到 MaiBot。"""
        chat_config = self.config.chat
        if not chat_config.enabled:
            return

        player_name = str(msg.get("player_name", "未知玩家"))
        message = str(msg.get("message", ""))
        formatted = chat_config.chat_format.format(
            player_name=player_name,
            message=message,
        )

        msg_id = f"mc_msg_{msg.get('timestamp', '')}_{player_name}"
        await self.ctx.gateway.route_message(
            gateway_name=GATEWAY_NAME,
            message=self._build_routed_message(
                msg_id=msg_id,
                text=formatted,
                sender_id=f"mc_{player_name}",
                sender_name=player_name,
            ),
            route_metadata=self._build_route_metadata(),
            external_message_id=msg_id,
            dedupe_key=msg_id,
        )

    def _handle_command_result(self, msg: dict[str, Any]) -> None:
        """处理命令执行结果，唤醒等待的 Future。"""
        request_id = msg.get("request_id", "")
        future = self._pending_commands.get(request_id)
        if future and not future.done():
            future.set_result({
                "success": bool(msg.get("success", False)),
                "output": str(msg.get("output", "")),
            })

    async def _handle_player_event(self, msg: dict[str, Any], action: str) -> None:
        """处理玩家加入/离开事件。"""
        if not self.config.chat.show_player_join_leave:
            return

        player_name = str(msg.get("player_name", "未知玩家"))
        text = f"{player_name} {action}"
        msg_id = f"mc_evt_{msg.get('type', '')}_{player_name}"

        await self.ctx.gateway.route_message(
            gateway_name=GATEWAY_NAME,
            message=self._build_routed_message(
                msg_id=msg_id,
                text=text,
                sender_id="mc_system",
                sender_name="系统",
            ),
            route_metadata=self._build_route_metadata(),
            external_message_id=msg_id,
            dedupe_key=msg_id,
        )

    # ============================================================
    # 网关状态上报
    # ============================================================

    async def _report_gateway_ready(self) -> None:
        """向 Host 上报网关就绪状态（参照官方 NapCat 示例）。"""
        try:
            accepted = await self.ctx.gateway.update_state(
                gateway_name=GATEWAY_NAME,
                ready=True,
                platform="minecraft",
                account_id=self.config.chat.ai_player_name,
                scope="primary",
                metadata={"protocol": "minecraft_ws"},
            )
            if accepted:
                self.ctx.logger.info("网关就绪状态已上报，account_id=%s", self.config.chat.ai_player_name)
            else:
                self.ctx.logger.warning(
                    "Host 未接受网关就绪上报。请确保在 MaiBot 的 config/bot_config.toml 中"
                    " 添加了 minecraft 平台的机器人账号，格式参考：\n"
                    "  [[bot_accounts]]\n"
                    "  platform = \"minecraft\"\n"
                    "  account_id = \"%s\"\n"
                    "  scope = \"primary\"",
                    self.config.chat.ai_player_name,
                )
        except Exception as e:
            self.ctx.logger.warning("上报网关就绪状态失败: %s", e)

    async def _report_gateway_offline(self) -> None:
        """向 Host 上报网关离线状态。"""
        try:
            await self.ctx.gateway.update_state(
                gateway_name=GATEWAY_NAME,
                ready=False,
                platform="minecraft",
                account_id=self.config.chat.ai_player_name,
                scope="primary",
            )
        except Exception as e:
            self.ctx.logger.warning("上报网关离线状态失败: %s", e)

    # ============================================================
    # 工具方法
    # ============================================================

    def _build_routed_message(
        self, *, msg_id: str, text: str, sender_id: str, sender_name: str
    ) -> dict[str, Any]:
        """构造符合 maim_message.MessageBase 结构的消息字典。

        group_info 位于 message_info 内部——这是 Host 识别群聊/私聊的依据。
        结构参照 https://github.com/MaiM-with-u/maim_message
        """
        chat_config = self.config.chat
        msg_info: dict[str, Any] = {
            "platform": "minecraft",
            "message_id": msg_id,
            "time": int(time.time()),
            "user_info": {
                "user_id": sender_id,
                "user_nickname": sender_name,
            },
        }
        if chat_config.is_group:
            msg_info["group_info"] = {
                "group_id": f"mc_{chat_config.group_name}",
                "group_name": chat_config.group_name,
            }

        return {
            "message_id": msg_id,
            "platform": "minecraft",
            "message_info": msg_info,
            "raw_message": [
                {"type": "text", "data": {"text": text}},
            ],
        }

    def _build_route_metadata(self) -> dict[str, Any]:
        """构造路由元数据，必须与 update_state 中的 account_id/scope 一致。"""
        return {
            "self_id": self.config.chat.ai_player_name,
            "connection_id": "primary",
        }


    @staticmethod
    def _extract_texts(message: Any) -> list[str]:
        """从 Host 发来的出站消息中提取 AI 回复文本段。

        实测确认 AI 回复位于 raw_message 数组中 type=="text" 的元素的 data 字段。
        """
        if isinstance(message, dict):
            raw = message.get("raw_message")
            if isinstance(raw, list):
                texts = [
                    item["data"]
                    for item in raw
                    if isinstance(item, dict)
                    and item.get("type") == "text"
                    and isinstance(item.get("data"), str)
                    and item["data"]
                ]
                if texts:
                    return texts
        return []


def create_plugin() -> MinecraftAdapterPlugin:
    return MinecraftAdapterPlugin()
