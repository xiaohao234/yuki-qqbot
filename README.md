# Yuki

独立运行的 OneBot v11 WebSocket 服务端程序（不依赖 NoneBot2）。

> Author: [xiaohao234](https://github.com/xiaohao234) · QQ Bot「Yuki」

通信架构：**本程序作为 WebSocket Server**，监听 `ws://127.0.0.1:8082`；
**NapCat 作为 WebSocket Client**，通过反向 WebSocket 连接进来，推送 OneBot v11 协议 JSON。
本程序解析群消息指令，再通过原 WS 连接回发 `send_group_msg` 等 Action 发消息/图片。

## 功能

- 被动统计群发言 → `/发言排行` 或 `/发言榜` 渲染今日 Top10 排行榜图片（金银铜高亮，实时更新）。
- `/昨日发言` 或 `/昨日发言榜` → 查看昨日已定格的发言排行。
- `/发言速` / `/水群速` / `/水群` [时间] → 最近一段时间的发言速度 + 水群排行（时间支持 30m/30分/三十分钟/1h/一小时，默认 30 分钟，最长 2 小时）。
- 发言统计按日期隔离，每天 0 点自动刷新；数据只保留 2 天（今天 + 昨天），旧数据惰性清理。
- `/签到`、`/运势` → 每日每人每群一次，随机大吉/中吉/小吉/凶 + 宜忌 + 幸运数字。
- **三人复读**：同群连续 3 个不同成员说同一句纯文本时自动复读一次，**同一句只复读一次**（后续跟团不再重复）。
- 头像通过 aiohttp 拉取 QQ 头像接口，内嵌为 data URI，再用 Playwright 渲染整页 PNG。

> 复读只对纯文本生效：命令（`/` 开头）、含 CQ 码（图片 / @ / 表情）的消息不参与复读。
> 机器人自身消息一律跳过（不计统计、不复读）。
> **@bot 已下线**：@ 消息交给 astrbot 处理 AI 聊天，本程序不再回复 @。

## arm64 / Debian 12 适配说明

| 组件 | arm64 兼容性 | 备注 |
|------|------------|------|
| Python | ✅ | Debian 12 自带 3.11，满足 ≥3.10 |
| aiohttp / aiofiles / jinja2 | ✅ | PyPI 有 aarch64 wheel 或纯 Python |
| Playwright + Chromium | ✅ | Playwright ≥1.28 支持 arm64 Linux |

**代码逻辑无需为 arm64 改动**。但干净的 Debian 12 上有两个必须处理的实际问题：

1. **必须安装中文字体**，否则 Playwright 渲染的图片里中文会显示为方块（豆腐块）——Chromium 不自带 CJK 字体，依赖系统 fontconfig。
2. **必须安装 Playwright 系统依赖库**（`playwright install-deps`）。
3. **不要用宝塔面板自带的 Python**（通常是面板专用、版本较低），请用系统 python3.11 + venv。

## 项目结构

```
qqbot/
├── main.py              # WS Server 入口 + 生命周期管理
├── onebot.py            # WS 连接管理 + OneBot v11 事件解析 + Action 封装
├── handler.py           # 指令处理（统计/签到/复读）+ 按日期隔离统计 + 异步文件 IO + 头像拉取
├── repeat.py            # 三人复读检测器（同一句只复读一次）
├── renderer.py          # Playwright + Jinja2 渲染封装
├── templates/
│   ├── stats.html       # 发言排行模板（金银铜高亮，粉紫渐变）
│   └── fortune.html     # 签到运势模板
├── data/                # 运行时生成：msg_stats.json / sign_in.json
├── yuki.service   # systemd 服务单元（可选，开机自启用）
├── requirements.txt
└── README.md
```

## 部署步骤（Debian 12 arm64 + 宝塔面板）

以下命令在服务器 SSH 终端执行（root 或 sudo 用户）。

### 1. 安装系统依赖

```bash
apt update
# CJK 字体（渲染中文必需，二选一即可，推荐 noto）
apt install -y fonts-noto-cjk
# 可选轻量替代：apt install -y fonts-wqy-zenhei fonts-wqy-microhei

# Playwright 编译/运行所需的系统库 + 构建工具
apt install -y python3 python3-venv python3-pip build-essential python3-dev \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2
```

### 2. 拉取代码并创建虚拟环境

```bash
# 把项目放到 /opt/yuki（也可自选目录，systemd 里同步改即可）
mkdir -p /opt && cd /opt
# 若用 git：git clone <你的仓库> yuki
# 或把本地代码上传到 /opt/yuki

cd /opt/yuki

# 用系统 python3.11 创建虚拟环境（不要用宝塔的 python）
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

### 3. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 4. 安装 Playwright 浏览器（arm64）

```bash
# 下载 arm64 版 Chromium
playwright install chromium

# 自动安装剩余系统依赖（需 root；若已在上一步装好可跳过）
playwright install-deps chromium
```

### 5. 运行

```bash
python main.py
```

启动后日志会出现 `监听 ws://127.0.0.1:8082 （等待 NapCat 反向连接）`。

可选环境变量：

| 变量          | 默认值       | 说明                                          |
|---------------|-------------|-----------------------------------------------|
| `BOT_HOST`    | `127.0.0.1` | 监听地址（NapCat 在本机用 127.0.0.1 最安全）    |
| `BOT_PORT`    | `8082`      | 监听端口                                       |
| `BOT_DATA_DIR`| `<项目>/data` | 数据目录（可指向持久卷；测试用临时目录避免污染） |
| `BOT_ADMIN_QQ`| `0`         | 管理员 QQ（使用 /统计发言 /删除统计 需设置）    |
| `BOT_CONFIG_PATH`| `<项目>/config.json` | 功能开关配置文件路径                     |

### 功能开关（config.json）

复制 `config.example.json` 为 `config.json`，把不想用的功能改成 `false` 即可：

```json
{
  "features": {
    "stats": true,        // 发言排行（/发言排行 /发言榜 /今日发言榜 /昨日发言 /昨日发言榜）
    "user_stats": true,   // 个人查询（/查发言 /发言）
    "trend": true,        // 发言趋势（/发言趋势 /趋势）
    "phrase_stats": true, // 特定发言统计（/统计发言 /删除统计 /昨日数据 /<短语>）
    "sign": true,         // 签到运势（/签到 /运势）
    "repeat": true,       // 三人复读
    "help": true          // 帮助菜单（/yukihelp //help）
  }
}
```

- 停用的指令静默不响应；帮助菜单图片中对应功能会置灰并标注「已停用」
- 启动日志会打印所有开关状态：`功能开关：stats=on user_stats=on ...`
- 文件不存在 / JSON 损坏 / 含未知开关 → 全部功能按默认（启用）运行，不影响使用
- 修改后需重启生效

### 功能开关热切换（管理员，无需重启）

管理员可直接在群里用指令实时启停功能，改动立即生效并自动写回 `config.json`（重启后保持）：

| 指令 | 说明 |
|---|---|
| `/stop` | 查看全部功能当前开关状态 |
| `/stop <功能>` | 停用指定功能，如 `/stop 签到`、`/stop repeat`（中英文均可） |
| `/start` | 同 `/stop`，查看状态 |
| `/start <功能>` | 启用指定功能，如 `/start 签到` |

- `/stop` `/start` 本身不受任何开关控制——即使把帮助菜单停用了，`/start help` 仍可恢复
- 切换记录会写入日志：`功能开关热切换：sign=False`

> 若 NapCat 部署在**另一台机器**：`BOT_HOST=0.0.0.0 python main.py`，
> 并在宝塔「安全」里放行 8082 端口。

### 6.（可选）配置为 systemd 服务（开机自启 + 崩溃重启）

```bash
# 复制单元文件到 systemd 目录
cp /opt/yuki/yuki.service /etc/systemd/system/

# 如目录不是 /opt/yuki，编辑该文件修改 WorkingDirectory / ExecStart 路径
systemctl daemon-reload
systemctl enable --now yuki
systemctl status yuki        # 查看状态
journalctl -u yuki -f        # 查看实时日志
```

## NapCat 反向 WebSocket 配置

打开 NapCat WebUI（默认 `http://127.0.0.1:6099`）：

1. 进入 **网络配置** → 新建 **反向 WebSocket 客户端（Reverse WebSocket）**。
2. **地址 / URL** 填：

   ```
   ws://127.0.0.1:8082
   ```

   （NapCat 与本程序同机时用 127.0.0.1；分机部署则填本程序机器的 IP，并把本程序 `BOT_HOST` 设为 `0.0.0.0`、宝塔放行端口。）
3. 保存启用。NapCat 会自动连接到本程序的 WS Server。
4. 在群里发送 `/发言排行`、`/签到`、`/运势` 即可触发对应功能。

> NapCat 断线会自动重连；本程序侧也会自动替换旧连接。

## 宝塔面板注意事项

- **不要用宝塔自带 Python**：宝塔面板自身的 Python 是给面板用的、版本偏低。本项目请用系统 `python3.11` 配 venv（见步骤 2）。
- 宝塔「Python 项目管理器」插件可以管理本项目的 venv，但**仅当它用的是系统 python3.11** 才建议用；否则优先手动 venv。
- **防火墙**：NapCat 与本程序同机时无需放行端口（走 127.0.0.1）；分机部署时在宝塔「安全」放行 8082（TCP）。
- 若用宝塔 Nginx 反代，注意 WebSocket 需要Upgrade头；但反向 WS 场景一般直连 127.0.0.1:8082 即可，无需反代。

## 数据文件

- `data/msg_stats.json`：`{"群号": {"用户ID": {"nickname": "...", "count": N}}}`
- `data/sign_in.json`：`{"群号_用户ID": {"date": "YYYY-MM-DD", "streak": N}}`

## 并发与连接状态说明

- aiohttp 的 `ws_handler` 在每条反向 WS 连接建立时被调用，连接对象 `ws` 保存在
  `OneBotConnection` 单例中（`set_connection`）。NapCat 重连时新连接会替换并关闭旧连接，
  避免向已失效的 ws 发送。
- `async for msg in ws` 阻塞读取该连接的上报；每收到一条 TEXT 事件，用
  `asyncio.create_task(...)` 派发独立任务处理，**慢操作（拉头像、Playwright 渲染）不会
  阻塞后续消息接收**，多群/多指令可并发执行。
- 共享资源（`msg_stats.json`、`sign_in.json`）用各自的 `asyncio.Lock` 串行化读写，
  防止并发竞态；写文件采用临时文件 + `os.replace` 原子替换。
- Playwright 浏览器在 `on_startup` 启动一次（已加 `--no-sandbox`，适配服务器/root 环境），
  渲染时 `new_page()` 复用浏览器、用完即关；`on_cleanup` 统一关闭浏览器与 HTTP 会话。
- 指令处理全程 try/except，出错向群发文本提示，不让进程崩溃。
