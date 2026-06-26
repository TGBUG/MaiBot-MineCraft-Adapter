"""
模拟 Minecraft 端 WebSocket 服务，用于独立测试 Minecraft 适配器插件。

启动后插件可连接到此服务进行功能验证。

用法：
    python test_mc_ws_server.py [--port PORT] [--token TOKEN]

交互命令（在终端输入）：
    chat <玩家名> <消息>   — 模拟玩家发送聊天消息
    join <玩家名>           — 模拟玩家加入
    leave <玩家名>          — 模拟玩家离开
    start                   — 模拟服务器启动
    stop                    — 模拟服务器停止
    error <消息>            — 发送错误消息
    list                    — 列出已连接的客户端
    help                    — 显示帮助
"""

import argparse
import asyncio
import json
import time
import uuid
from typing import Any

import aiohttp
from aiohttp import web


class MockMinecraftWSServer:
    """模拟 MC 端 WebSocket 服务端。"""

    def __init__(self, expected_token: str = "") -> None:
        self._expected_token = expected_token
        self._clients: set[web.WebSocketResponse] = set()

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        peer = request.remote
        print(f"[连接] 新客户端: {peer} (当前 {len(self._clients)} 个连接)")

        try:
            await self._client_loop(ws)
        finally:
            self._clients.discard(ws)
            print(f"[断开] 客户端: {peer} (剩余 {len(self._clients)} 个连接)")

        return ws

    async def _client_loop(self, ws: web.WebSocketResponse) -> None:
        """处理单个客户端消息循环。"""
        async for raw in ws:
            if raw.type == aiohttp.WSMsgType.TEXT:
                try:
                    msg = json.loads(raw.data)
                    await self._handle_message(ws, msg)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "无效的 JSON 格式"})
            elif raw.type == aiohttp.WSMsgType.ERROR:
                print(f"[错误] WS 错误: {ws.exception()}")
                break

    async def _handle_message(self, ws: web.WebSocketResponse, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type", "")
        print(f"[收到] {msg_type}: {json.dumps(msg, ensure_ascii=False)}")

        if msg_type == "auth":
            await self._handle_auth(ws, msg)
        elif msg_type == "ping":
            await ws.send_json({"type": "pong"})
        elif msg_type == "command":
            await self._handle_command(ws, msg)
        elif msg_type == "chat":
            print(f"  [聊天] {msg.get('player_name')}: {msg.get('message')}")

    async def _handle_auth(self, ws: web.WebSocketResponse, msg: dict[str, Any]) -> None:
        if not self._expected_token:
            print("  [鉴权] 无 token 要求，直接通过")
            await ws.send_json({"type": "auth_ok"})
            return

        token = msg.get("token", "")
        if token == self._expected_token:
            print("  [鉴权] 成功")
            await ws.send_json({"type": "auth_ok"})
        else:
            print(f"  [鉴权] 失败: token={token!r}")
            await ws.send_json({"type": "auth_fail", "reason": "无效的鉴权令牌"})

    async def _handle_command(self, ws: web.WebSocketResponse, msg: dict[str, Any]) -> None:
        command = msg.get("command", "")
        request_id = msg.get("request_id", str(uuid.uuid4()))
        print(f"  [命令] 执行: /{command}")

        # 模拟命令执行
        await asyncio.sleep(0.3)
        await ws.send_json({
            "type": "command_result",
            "request_id": request_id,
            "success": True,
            "output": f"已执行命令: /{command}",
        })

    # ---- 主动推送方法 ----

    async def broadcast(self, data: dict[str, Any]) -> None:
        """向所有客户端广播消息。"""
        for ws in list(self._clients):
            try:
                await ws.send_json(data)
            except Exception:
                pass

    async def send_chat(self, player_name: str, message: str) -> None:
        await self.broadcast({
            "type": "chat",
            "player_name": player_name,
            "message": message,
            "timestamp": int(time.time()),
        })
        print(f"[广播] 聊天: {player_name}: {message}")

    async def send_player_join(self, player_name: str) -> None:
        await self.broadcast({
            "type": "player_join",
            "player_name": player_name,
        })
        print(f"[广播] 加入: {player_name}")

    async def send_player_leave(self, player_name: str) -> None:
        await self.broadcast({
            "type": "player_leave",
            "player_name": player_name,
        })
        print(f"[广播] 离开: {player_name}")

    async def send_server_start(self) -> None:
        await self.broadcast({"type": "server_start"})
        print("[广播] 服务器启动")

    async def send_server_stop(self) -> None:
        await self.broadcast({"type": "server_stop"})
        print("[广播] 服务器停止")

    async def send_error(self, message: str) -> None:
        await self.broadcast({"type": "error", "message": message})
        print(f"[广播] 错误: {message}")


# ---- 命令行交互 ----


def print_help() -> None:
    print("\n可用命令:")
    print("  chat <玩家名> <消息>  — 模拟玩家聊天")
    print("  join <玩家名>          — 模拟玩家加入")
    print("  leave <玩家名>         — 模拟玩家离开")
    print("  start                  — 模拟服务器启动")
    print("  stop                   — 模拟服务器停止")
    print("  error <消息>           — 发送错误消息")
    print("  list                   — 列出已连接的客户端")
    print("  help                   — 显示此帮助")
    print("  quit                   — 退出\n")


async def interactive_loop(server: MockMinecraftWSServer) -> None:
    """终端交互循环，用于手动推送测试事件。"""
    print_help()

    loop = asyncio.get_running_loop()

    while True:
        try:
            line = await loop.run_in_executor(None, input, "> ")
        except (EOFError, KeyboardInterrupt):
            print("\n正在退出...")
            break

        line = line.strip()
        if not line:
            continue

        parts = line.split(maxsplit=2)
        cmd = parts[0].lower()

        if cmd == "chat" and len(parts) >= 3:
            await server.send_chat(parts[1], parts[2])
        elif cmd == "join" and len(parts) >= 2:
            await server.send_player_join(parts[1])
        elif cmd == "leave" and len(parts) >= 2:
            await server.send_player_leave(parts[1])
        elif cmd == "start":
            await server.send_server_start()
        elif cmd == "stop":
            await server.send_server_stop()
        elif cmd == "error" and len(parts) >= 2:
            await server.send_error(parts[1])
        elif cmd == "list":
            count = len(server._clients)
            print(f"已连接客户端: {count} 个")
        elif cmd == "help":
            print_help()
        elif cmd == "quit":
            break
        else:
            print(f"未知命令: {cmd}，输入 help 查看帮助")


async def main() -> None:
    parser = argparse.ArgumentParser(description="模拟 MC 端 WebSocket 服务")
    parser.add_argument("--port", type=int, default=8765, help="监听端口 (默认 8765)")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--token", default="", help="鉴权令牌，留空则不要求鉴权")
    parser.add_argument("--path", default="/minecraft", help="WS 路径 (默认 /minecraft)")
    args = parser.parse_args()

    server = MockMinecraftWSServer(expected_token=args.token)

    app = web.Application()
    app.router.add_get(args.path, server.handle_ws)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()

    print(f"模拟 MC WebSocket 服务已启动:")
    print(f"  地址: ws://{args.host}:{args.port}{args.path}")
    print(f"  鉴权: {'token=' + args.token if args.token else '无'}")
    print(f"  请在插件配置中将 ws_url 设为上述地址\n")

    try:
        await interactive_loop(server)
    finally:
        await runner.cleanup()
        print("服务已停止")


if __name__ == "__main__":
    asyncio.run(main())
