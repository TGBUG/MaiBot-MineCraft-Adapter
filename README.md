# MaiBot Minecraft 适配器

通过 WebSocket 将 Minecraft 服务器接入 MaiBot，使 AI 能够感知游戏内聊天并执行服务器命令

*Powered by AI*

*本项目处于早期阶段，如果遇到问题，欢迎提交issue反馈*

## 功能

- **双向聊天同步** — MC 玩家聊天消息实时推送到 MaiBot 群聊流，AI 回复自动发送回游戏
- **命令执行 Tool** — AI 可调用 `execute_mc_command` 在服务器上执行任意命令（如 `say`、`list`、`give` 等）
- **玩家进出通知** — 可选将玩家加入/离开事件同步到 MaiBot
- **自动重连** — 连接断开后按固定间隔自动重连，鉴权失败则停止重连
- **心跳保活** — 可配置心跳间隔，防止空闲断连

## 安装

1. 将本插件放入 MaiBot 的 `plugins/` 目录
2. 安装依赖：
   ```bash
   pip install aiohttp
   ```
   > 通常无需此步
3. 在 MaiBot 的 `config/bot_config.toml` 中添加机器人账号：
   ```toml
   [[bot_accounts]]
   platform = "minecraft"
   account_id = "MaiBot"
   scope = "primary"
   ```
   > `account_id` 需与插件配置中的 `chat.ai_player_name` 一致

## 配置

插件加载后 MaiBot 会自动生成默认配置文件，包含以下节：

### `[plugin]` — 插件基础设置

| 字段               | 默认值       | 说明               |
|------------------|-----------|------------------|
| `config_version` | `"1.0.0"` | 配置版本，由 Runner 管理 |
| `enabled`        | `true`    | 是否启用插件           |

### `[connection]` — WebSocket 连接设置

| 字段                   | 默认值                             | 说明                  |
|----------------------|---------------------------------|---------------------|
| `ws_url`             | `ws://localhost:8080/minecraft` | MC 服务器 WebSocket 地址 |
| `auth_token`         | `""`                            | 鉴权令牌，留空则不鉴权         |
| `auto_reconnect`     | `true`                          | 断开后是否自动重连           |
| `reconnect_interval` | `10`                            | 重连间隔（秒）             |
| `heartbeat_interval` | `30`                            | 心跳间隔（秒）。同时用于 aiohttp 内置 WebSocket ping 和应用层 JSON ping/pong |

### `[chat]` — 聊天同步设置

| 字段                       | 默认值                         | 说明                |
|--------------------------|-----------------------------|-------------------|
| `enabled`                | `true`                      | 是否启用聊天同步          |
| `show_player_join_leave` | `false`                     | 是否显示玩家加入/离开消息     |
| `chat_format`            | `[{player_name}] {message}` | 聊天消息格式            |
| `ai_player_name`         | `MaiBot`                    | AI 在游戏内显示的名称      |
| `group_name`             | `Minecraft 服务器`             | 在 MaiBot 中显示的群聊名称 |
| `is_group`               | `true`                      | 是否注册为群聊           |

## WebSocket 协议规范

插件作为 WS 客户端连接 MC 服务器端 mod/plugin 提供的 WS 服务。所有消息均为 JSON 格式

### 客户端 → 服务器 （本插件 → MC 服务器端实现）

#### `auth` — 鉴权

连接建立后立即发送（若配置了 `auth_token`）

```json
{
  "type": "auth",
  "token": "<auth_token>"
}
```

#### `command` — 执行命令

```json
{
  "type": "command",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "command": "say Hello everyone"
}
```

| 字段           | 类型     | 说明             |
|--------------|--------|----------------|
| `request_id` | string | 唯一请求 ID，用于关联响应 |
| `command`    | string | 要执行的命令（不含 `/`） |

#### `chat` — 发送聊天

```json
{
  "type": "chat",
  "player_name": "MaiBot",
  "message": "你好！"
}
```

| 字段            | 类型     | 说明    |
|---------------|--------|-------|
| `player_name` | string | 发送者名称 |
| `message`     | string | 消息内容。适配器从 Host 出站消息的 `raw_message` 数组中筛选 `type: "text"` 的元素，提取其 `data` 字段作为纯文本。MaiBot 若将回复拆为多个 `text` 段，适配器对每段分别发送一条独立的 `chat` 消息，保留分段语义 |

#### `ping` — 心跳

```json
{
  "type": "ping"
}
```

客户端按 `heartbeat_interval` 周期发送

---

### 服务器 → 客户端 （MC 服务器端实现 → 本插件）

#### `auth_ok` / `auth_fail` — 鉴权结果

```json
{ "type": "auth_ok" }
```

```json
{
  "type": "auth_fail",
  "reason": "令牌无效"
}
```

收到 `auth_fail` 后插件将停止重连

#### `command_result` — 命令执行结果

```json
{
  "type": "command_result",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "success": true,
  "output": "已执行命令: say Hello everyone"
}
```

| 字段           | 类型     | 说明       |
|--------------|--------|----------|
| `request_id` | string | 对应请求的 ID |
| `success`    | bool   | 是否执行成功   |
| `output`     | string | 命令输出文本   |

#### `chat` — 聊天消息

MC 玩家在游戏内发送的原始聊天文本。

```json
{
  "type": "chat",
  "player_name": "Steve",
  "message": "有人在吗",
  "timestamp": 1719000000
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `player_name` | string | 发言人玩家名 |
| `message` | string | 玩家发送的原始聊天文本（未经格式化） |
| `timestamp` | number | Unix 时间戳（秒） |

#### `player_join` / `player_leave` — 玩家进出

```json
{
  "type": "player_join",
  "player_name": "Alex"
}
```

```json
{
  "type": "player_leave",
  "player_name": "Alex"
}
```

#### `server_start` / `server_stop` — 服务器状态

```json
{ "type": "server_start" }
```

```json
{ "type": "server_stop" }
```

仅记录日志，不路由到 MaiBot

#### `pong` — 心跳响应

```json
{ "type": "pong" }
```

#### `error` — 错误

```json
{
  "type": "error",
  "message": "未知的消息类型"
}
```

---

### 连接流程

```
客户端                                服务器
  │                                    │
  │──── ws 连接 ───────────────────────→│
  │                                    │
  │──── {type:"auth", token:"..."} ───→│  (若有 token)
  │←─── {type:"auth_ok"} ──────────────│
  │                                    │
  │←─── {type:"chat", ...} ────────────│  (实时推送)
  │──── {type:"chat", ...} ───────────→│
  │                                    │
  │──── {type:"ping"} ────────────────→│  (每 heartbeat_interval 秒)
  │←─── {type:"pong"} ─────────────────│
  │                                    │
  │──── {type:"command", ...} ────────→│
  │←─── {type:"command_result", ...} ──│
```

## 消息转换

插件收到 MC WS 消息后，按 `[maim_message](https://github.com/MaiM-with-u/maim_message)` 的 `MessageBase` 结构构造消息字典，通过 `route_message` 注入 MaiBot。

### 示例：玩家 Alex 在游戏中说 "123"

**第一步：MC 服务器通过 WS 推送原始聊天**

```json
{
  "type": "chat",
  "player_name": "Alex",
  "message": "123",
  "timestamp": 1719000000
}
```

**第二步：插件按 `chat_format` 格式化后，构造 `MessageBase` 注入 Host**

```json
{
  "message_id": "mc_msg_1719000000_Alex",
  "platform": "minecraft",
  "message_info": {
    "platform": "minecraft",
    "message_id": "mc_msg_1719000000_Alex",
    "time": 1719000000,
    "user_info": {
      "user_id": "mc_Alex",
      "user_nickname": "Alex"
    },
    "group_info": {
      "group_id": "mc_Minecraft 服务器",
      "group_name": "Minecraft 服务器"
    }
  },
  "raw_message": [
    { "type": "text", "data": { "text": "[Alex] 123" } }
  ]
}
```

### 字段映射

| MC WS 字段 | MessageBase 位置 | 说明 |
|-----------|-----------------|------|
| `player_name` | `message_info.user_info.user_nickname` | 发言者名称 |
| `player_name` | `message_info.user_info.user_id` | 构造为 `mc_{player_name}` |
| `message` | `raw_message[0].data.text` | 经 `chat_format` 格式化后的文本 |
| `timestamp` | `message_info.message_id` | 参与构造 message_id（格式 `mc_msg_{timestamp}_{player_name}`），若缺失则对应位置为空 |
| — | `message_info.time` | 当前时间戳（`int(time.time())`），非 MC 原始时间戳 |
| — | `message_info.group_info.group_id` | 构造为 `mc_{group_name}` |
| — | `message_info.group_info.group_name` | 来自 `chat.group_name` 配置 |

> `is_group = false` 时，`group_info` 不出现在 `message_info` 中，Host 将其识别为私聊。

## 测试

项目包含模拟 MC 端 WS 服务，可用于独立测试：

```bash
python test_mc_ws_server.py --port 8765
```

完整参数：

```bash
python test_mc_ws_server.py --port 8765 --host 0.0.0.0 --token mytoken --path /minecraft
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--port` | `8765` | 监听端口 |
| `--host` | `0.0.0.0` | 监听地址 |
| `--token` | `""` | 鉴权令牌，留空则不要求鉴权 |
| `--path` | `/minecraft` | WS 路径 |

启动后在终端输入命令模拟 MC 行为：

```
chat Steve 你好     # 模拟玩家聊天
join Alex           # 模拟玩家加入
leave Alex          # 模拟玩家离开
```

插件配置中将 `ws_url` 设为 `ws://localhost:8765/minecraft` 即可连接测试

## 开发注意事项

### MC 端 WS 服务实现要点

开发 Fabric 模组或 Paper 插件提供 WS 服务时需注意：

1. **命令不含 `/`**：插件发送的 `command` 消息中的命令不包含前导 `/`。MC 端需要自行处理（原版 `execute` 需要 `/`，Rcon 不需要，具体取决于底层实现）。

2. **`message_id` 构造依赖 `timestamp`**：插件使用 `mc_msg_{timestamp}_{player_name}` 作为消息去重键。若 MC 端不发送 `timestamp` 字段，message_id 会变为 `mc_msg__player_name`（时间戳位置为空）。

3. **鉴权时序**：连接建立后插件会立即发送 `auth`（若配置了 token），并等待 10 秒。超时视为连接失败，将触发重连。

4. **命令超时**：`command` 消息的响应超时为 10 秒。MC 端需在此时间内返回 `command_result`。

5. **`server_stop` 不改变连接**：收到 `server_stop` 后插件仅记录日志，不会主动断开 WS 或上报离线。若 MC 端会在 stop 时关闭 WS，则由重连逻辑处理。

6. **AI 回复提取**：`_extract_texts` 从 Host 出站消息的 `raw_message` 数组中筛选 `type == "text"` 的元素，提取其 `data` 字段（纯字符串）。每个 `text` 段作为一条独立的 `chat` WS 消息发送，保留 MaiBot 的分段回复语义。非 `text` 类型元素自动跳过。

### 分段回复与协议设计

当前 WS 协议对 AI→MC 方向使用单条 `chat` 消息（一个 `message` 字段）。适配器通过**逐段发送多条 `chat` 消息**来承载 MaiBot 的分段回复，无需修改协议。MC 端实现只需正常处理每条 `chat` 消息即可。

若未来需要批量分段语义，可扩展 `chat` 消息增加可选的 `messages` 数组字段：

```json
{
  "type": "chat",
  "player_name": "MaiBot",
  "message": "单条文本（向后兼容）",
  "messages": ["段落一", "段落二", "段落三"]
}
```

当 `messages` 存在时 MC 端可忽略 `message` 字段，按数组顺序逐条发送到游戏内聊天。此扩展为**可选增强**，当前版本不需要实现。

### route_metadata 一致性

插件内部通过 `route_metadata` 标识消息来源，其结构为：
```json
{
  "self_id": "<ai_player_name>",
  "connection_id": "primary"
}
```
这必须与 `update_state` 上报的 `account_id`、`scope` 一致，否则 Host 无法正确路由消息。`connection_id` 始终为 `"primary"`（单连接模式）。

### i18n 目录

`_manifest.json` 中声明了 `"locales_path": "i18n"` 和 `"supported_locales": ["zh-CN", "en-US"]`。如需国际化支持，需在插件目录下创建 `i18n/` 目录并放入对应语言文件。

## TODO

- [x] 写对接fabric模组
- [ ] 写对接paper插件
- [ ] 实现对接多个服务器
- [ ] 考虑兼容客户端MC