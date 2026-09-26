# AGENTS.md — yuki-qqbot 开发指南

本文件面向 AI 编码助手与后续贡献者，说明项目结构、开发约定与测试要求。

## 项目简介

QQ 群机器人（OneBot v11 协议）：发言统计/排行/趋势/特定短语统计/水群速度/签到运势/三人复读，
统计结果用 Playwright + Chromium 渲染成图片发回群内。
本程序作为 WebSocket **Server** 运行（默认 `ws://127.0.0.1:8082`），NapCat 作为 Client 反向接入。

## 文件职责

| 文件 | 职责 |
|---|---|
| `main.py` | 入口：aiohttp WS 服务、全局资源生命周期、启动通知钩子、反向 WS 鉴权 |
| `onebot.py` | OneBot v11 协议：连接管理、Action 发送、事件解析 |
| `handler.py` | 全部指令分发与业务逻辑（最大的文件，改指令都在这里） |
| `renderer.py` | Jinja2 + Playwright 渲染：懒启动、闲置回收、并发闸门 |
| `features.py` | config.json 功能开关加载 |
| `repeat.py` | 三人复读状态机 |
| `cqtext.py` | CQ 码归一化（QQ 表情/商城大表情 → 短标记） |
| `templates/` | 图片模板（Jinja2，配色为浅蓝渐变） |
| `ptest/render_samples.py` | 用真实 Chromium 渲染全部模板样例图 |
| `install.sh` | 交互式一键安装（依赖 + 配置 + 可选 systemd） |

测试：`test_admin.py` / `test_cq.py` / `test_avatar.py` / `test_repeat.py`（离线）
与 `test_all.py`（端到端，需网络 + Chromium）。

## 常用命令

```bash
pip install -r requirements.txt && playwright install chromium   # 初始化环境
python3 main.py                     # 运行
python3 test_admin.py               # 各离线测试（见上）
python3 ptest/render_samples.py     # 渲染全部模板样例图到 ptest/
bash install.sh                     # 一键安装
```

## 必须遵守的约定

1. **所有 IO 异步**：JSON 文件读写用 `_load_json`/`_save_json`（原子写），
   新增文件 IO 必须配套 `asyncio.Lock`。
2. **内存有界原则**：任何缓存/运行时状态必须有上限或过期机制（现有范例：
   `_msg_times` 滚动窗口惰性清理、`_avatar_cache` LRU+TTL、`_pending_reboot` 单槽位）。
   禁止引入无界增长的 dict/list。
3. **消息匹配一律先过 `cqtext.normalize()`**：QQ 商城大表情的 CQ 码带大段转义
   JSON，直接拿原文匹配会出错；复读发送时用原始消息（表情原样复现）。
4. **管理员指令必须经 `ADMIN_QQS` 校验**（环境变量 `BOT_ADMIN_QQ`，逗号分隔多个）。
5. **新增指令**：常量定义在 `handler.py` 顶部 → `handle_event` 分发 → 受功能开关
   控制（管理员/系统类指令除外）→ 更新帮助菜单（用户版与管理员版）→ 配套测试。
6. **渲染**：视口宽 760（680px 卡片水平居中，改小会右偏）；不得绕过并发闸门与
   懒启动生命周期；头像一律经 `_fetch_avatar_b64`（压缩 + 缓存），不要内嵌原图。
7. **日志**：级别颜色只在终端生效（`_ColorFormatter`），重定向到文件时必须纯文本。
8. **子进程**：一律 `asyncio.create_subprocess_exec` 数组调用（禁止 shell 拼接），
   必须设超时，超时后 `kill()` + `wait()` 回收。

## 测试要求

任何改动后运行全部离线测试并保持全绿：

```bash
python3 test_repeat.py && python3 test_cq.py && python3 test_avatar.py && python3 test_admin.py
```

涉及端到端行为再跑 `python3 test_all.py`（需要网络与 Chromium）。
新功能必须附带离线测试（不依赖网络/浏览器），打桩用 FakeWS/FakeRenderer 模式。

## 配置与环境变量

- `BOT_ADMIN_QQ`：管理员 QQ，逗号分隔多个；`0` 或留空禁用全部管理员指令
- `BOT_HOST` / `BOT_PORT`：监听地址（跨机部署用 `0.0.0.0` 并放行端口）
- `BOT_DATA_DIR` / `BOT_CONFIG_PATH`：数据目录与配置文件路径覆盖
- `BOT_BROWSER_IDLE_SEC`：Chromium 闲置回收秒数（默认 900，0 = 不回收）
- `BOT_ACCESS_TOKEN`：反向 WS 鉴权 token（OneBot v11 标准，Bearer 头或 ?access_token= 均可）；留空 = 不鉴权
- `config.json`：`features` 功能开关；`log` 块为 `#log` 日志源
  （`mode`: file/journal/auto，supervisor 文件日志用 file，systemd 用 journal）

## 部署

`bash install.sh` 交互式安装（依赖 + 配置 + 可选 systemd 自启动），
或用任意守护进程（supervisor 等）拉起 `main.py` 并注入上述环境变量。
NapCat 侧配置反向 WebSocket 连接 `ws://<本机IP>:8082/`。
