#!/usr/bin/env bash
# ============================================================
#  Yuki (QQ 群机器人) 一键安装脚本
#
#  功能：交互式安装 + 自动配置 + 可选 systemd 自启动
#  用法：
#      sudo bash install.sh        （推荐，安装到 /opt/yuki 并可选 systemd）
#      bash install.sh             （无 root 时仅支持本地目录安装）
#
#  可重复执行 = 升级：检测到此前安装时自动跳过已完成步骤，
#  config.json / data/ 不会被覆盖；配置项会带出旧值作为默认。
# ============================================================
set -e

# ---------- 输出辅助 ----------
if [ -t 1 ]; then
    C_G="\033[32m"; C_Y="\033[33m"; C_R="\033[31m"; C_B="\033[36m"; C_0="\033[0m"
else
    C_G=""; C_Y=""; C_R=""; C_B=""; C_0=""
fi
info() { echo -e "${C_B}[INFO]${C_0} $*"; }
ok()   { echo -e "${C_G}[ OK ]${C_0} $*"; }
warn() { echo -e "${C_Y}[WARN]${C_0} $*"; }
err()  { echo -e "${C_R}[FAIL]${C_0} $*"; }

# 交互提问：ask "提示" "默认值" → 结果写入 REPLY（直接回车取默认值）
ask() {
    local prompt="$1" def="$2" input
    read -r -p "$prompt [$def]: " input || input=""
    REPLY="${input:-$def}"
}
# 是/否提问：ask_yn "提示" "y|n(默认)" → 返回 0=yes 1=no
ask_yn() {
    local prompt="$1" def="$2" input
    while :; do
        read -r -p "$prompt [$def/n]: " input || input="$def"
        input="$(echo "$input" | tr 'A-Z' 'a-z')"
        [ -z "$input" ] && input="$def"
        case "$input" in
            y|yes) return 0 ;;
            n|no)  return 1 ;;
            *) echo "  请输入 y 或 n" ;;
        esac
    done
}

echo "=========================================="
echo "       Yuki QQ 群机器人 一键安装"
echo "=========================================="

# ---------- [1/7] 环境检查 ----------
info "步骤 1/7：环境检查"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

IS_ROOT=0
if [ "$(id -u)" -eq 0 ]; then IS_ROOT=1; fi

if [ "$(uname -s)" != "Linux" ]; then
    warn "当前不是 Linux 系统（$(uname -s)）。本脚本主要面向 Linux 服务器部署，可继续但 systemd 相关步骤会跳过。"
fi

if ! command -v python3 >/dev/null 2>&1; then
    err "未找到 python3，请先安装 Python 3.9+（Ubuntu/Debian: apt install python3 python3-venv python3-pip）"
    exit 1
fi
PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 9) else 0)')"
if [ "$PY_OK" != "1" ]; then
    err "Python 版本过低（当前 $PY_VER，需要 >= 3.9）"
    exit 1
fi
ok "Python $PY_VER"

HAS_SYSTEMD=0
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    HAS_SYSTEMD=1
    ok "检测到 systemd（可配置自启动）"
else
    warn "未检测到 systemd（如 1panel/宝塔 supervisor 部署），自启动步骤将跳过"
fi

if [ "$IS_ROOT" -ne 1 ]; then
    warn "当前不是 root。安装到系统目录（如 /opt/yuki）或配置 systemd 需要 root 权限。"
    if ! ask_yn "仍要继续吗（仅支持安装到你有写权限的目录）？" "n"; then
        echo "请改用：sudo bash install.sh"
        exit 1
    fi
fi

# ---------- [2/7] 安装目录 ----------
info "步骤 2/7：选择安装目录"
if [ "$IS_ROOT" -eq 1 ]; then
    DEF_DIR="/opt/yuki"
else
    DEF_DIR="$SCRIPT_DIR"
fi
ask "安装目录（源码将复制到这里）" "$DEF_DIR"
TARGET_DIR="${REPLY%/}"
if [ "$TARGET_DIR" != "$SCRIPT_DIR" ]; then
    mkdir -p "$TARGET_DIR"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --exclude .git --exclude .venv --exclude data "$SCRIPT_DIR/" "$TARGET_DIR/"
    else
        cp -R "$SCRIPT_DIR/." "$TARGET_DIR/"
        rm -rf "$TARGET_DIR/.git" "$TARGET_DIR/.venv"
    fi
    ok "源码已复制到 $TARGET_DIR"
else
    ok "使用当前目录 $TARGET_DIR"
fi
cd "$TARGET_DIR"

# 检测此前安装：.install.env 由本脚本生成，存在则已完成步骤默认跳过、配置带出旧值
PREV_INSTALLED=0
PREV_ADMIN=""; PREV_HOST=""; PREV_PORT=""; PREV_TOKEN=""
DEPS_DONE=0; CHROMIUM_DONE=0
if [ -f ".install.env" ]; then
    PREV_INSTALLED=1
    # shellcheck disable=SC1091
    . ./.install.env 2>/dev/null || true
    PREV_ADMIN="$ADMIN_QQ"; PREV_HOST="$BOT_HOST"; PREV_PORT="$BOT_PORT"; PREV_TOKEN="$ACCESS_TOKEN"
    DEPS_DONE="${DEPS_DONE:-0}"; CHROMIUM_DONE="${CHROMIUM_DONE:-0}"
    ok "检测到此前安装记录，已完成步骤将默认跳过（配置项回车即保留旧值）"
fi

# ---------- [3/7] Python 虚拟环境 ----------
info "步骤 3/7：创建 Python 虚拟环境"
if [ ! -x ".venv/bin/python" ]; then
    if ! python3 -m venv .venv 2>/dev/null; then
        warn "venv 创建失败，尝试安装 python3-venv ..."
        if command -v apt-get >/dev/null 2>&1 && [ "$IS_ROOT" -eq 1 ]; then
            apt-get update -qq && apt-get install -y -qq python3-venv python3-pip
            python3 -m venv .venv
        else
            err "python3 -m venv 失败。请手动安装：apt install python3-venv python3-pip 后重试"
            exit 1
        fi
    fi
fi
VENV_PY="$TARGET_DIR/.venv/bin/python"
ok "虚拟环境就绪：$TARGET_DIR/.venv"

# ---------- [4/7] 安装依赖 ----------
info "步骤 4/7：安装 Python 依赖"
PIP_INDEX=""
if ask_yn "使用清华 PyPI 镜像加速（国内服务器建议）？" "y"; then
    PIP_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
fi
PIP_ARGS=""
[ -n "$PIP_INDEX" ] && PIP_ARGS="-i $PIP_INDEX"

# 此前已装过 → 默认跳过（回车即跳过；输入 y 可重装升级依赖）
DEPS_DEF="y"
if [ "$PREV_INSTALLED" -eq 1 ] && [ "$DEPS_DONE" = "1" ]; then DEPS_DEF="n"; fi
if ask_yn "立即安装/更新依赖（aiohttp/aiofiles/playwright/jinja2/Pillow）？" "$DEPS_DEF"; then
    # shellcheck disable=SC2086
    "$VENV_PY" -m pip install -q --upgrade pip $PIP_ARGS
    # shellcheck disable=SC2086
    "$VENV_PY" -m pip install -q -r requirements.txt $PIP_ARGS
    DEPS_DONE=1
    ok "Python 依赖安装完成"
else
    DEPS_DONE=1
    info "跳过依赖安装（沿用已有环境；需要时可手动：$VENV_PY -m pip install -r requirements.txt）"
fi

CHROME_DEF="y"
if [ "$PREV_INSTALLED" -eq 1 ] && [ "$CHROMIUM_DONE" = "1" ]; then CHROME_DEF="n"; fi
if ask_yn "安装 Playwright Chromium 内核（渲染图片必需，约 150MB）？" "$CHROME_DEF"; then
    if [ "$IS_ROOT" -eq 1 ] && ask_yn "先安装 Chromium 系统依赖库（apt 环境，首次部署建议）？" "y"; then
        "$VENV_PY" -m playwright install-deps chromium || warn "系统依赖安装失败，若启动报错请手动执行 playwright install-deps"
    fi
    "$VENV_PY" -m playwright install chromium
    CHROMIUM_DONE=1
    ok "Chromium 内核安装完成"
else
    CHROMIUM_DONE=1
    info "跳过 Chromium 安装（沿用已有环境）"
fi

# ---------- [5/7] 基础配置 ----------
info "步骤 5/7：基础配置"

if [ ! -f "config.json" ]; then
    cp config.example.json config.json
    ok "已从 config.example.json 生成 config.json"
else
    ok "已存在 config.json，保留不覆盖"
fi

DEF_ADMIN="$PREV_ADMIN"; [ -z "$DEF_ADMIN" ] && DEF_ADMIN="0"
echo ""
echo "管理员 QQ 用于 #yukireboot / #log / /统计发言 等管理员指令的权限校验。"
echo "可配置多个，用英文逗号分隔（如 123,456）；填 0 = 禁用全部管理员指令。"
ask "BOT_ADMIN_QQ（管理员 QQ，多个逗号分隔）" "$DEF_ADMIN"
ADMIN_QQ="$REPLY"

DEF_HOST="$PREV_HOST"; [ -z "$DEF_HOST" ] && DEF_HOST="127.0.0.1"
DEF_PORT="$PREV_PORT"; [ -z "$DEF_PORT" ] && DEF_PORT="8082"
echo ""
echo "监听地址：NapCat 与 Yuki 在同一台机器用 127.0.0.1；"
echo "NapCat 在其他机器（反向 WS 连入）用 0.0.0.0，并记得在防火墙放行端口。"
ask "监听地址 BOT_HOST" "$DEF_HOST"
BOT_HOST="$REPLY"
ask "监听端口 BOT_PORT" "$DEF_PORT"
BOT_PORT="$REPLY"
if [ "$BOT_HOST" = "0.0.0.0" ]; then
    warn "已选择对外监听，请确认在防火墙/安全组放行 $BOT_PORT 端口！"
fi

echo ""
echo "鉴权 token：设置后 NapCat 反向 WS 必须携带相同 access_token 才能连入"
echo "（OneBot v11 标准：Authorization 头或 ?access_token= 参数均可）。"
echo "留空 = 不鉴权（任何能连到端口的客户端都可接入，公网部署建议设置）。"
ask "BOT_ACCESS_TOKEN（鉴权 token，默认留空禁用）" "$PREV_TOKEN"
ACCESS_TOKEN="$REPLY"

# 保存本次配置（systemd unit 与结尾摘要共用；含步骤完成标记供下次跳过）
cat > "$TARGET_DIR/.install.env" <<EOF
INSTALL_DIR="$TARGET_DIR"
VENV_PY="$VENV_PY"
ADMIN_QQ="$ADMIN_QQ"
BOT_HOST="$BOT_HOST"
BOT_PORT="$BOT_PORT"
ACCESS_TOKEN="$ACCESS_TOKEN"
DEPS_DONE=$DEPS_DONE
CHROMIUM_DONE=$CHROMIUM_DONE
EOF
chmod 600 "$TARGET_DIR/.install.env"
ok "配置已保存到 .install.env"

# ---------- [6/7] systemd 自启动（可选） ----------
info "步骤 6/7：systemd 自启动配置"
SYSTEMD_ENABLED=0
if [ "$HAS_SYSTEMD" -eq 1 ] && [ "$IS_ROOT" -eq 1 ]; then
    if ask_yn "配置 systemd 开机自启动 + 崩溃自动拉起？" "y"; then
        cat > /etc/systemd/system/yuki.service <<EOF
[Unit]
Description=Yuki (OneBot v11 WebSocket Server)
After=network.target

[Service]
Type=simple
WorkingDirectory=$TARGET_DIR
ExecStart=$VENV_PY $TARGET_DIR/main.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=BOT_ADMIN_QQ=$ADMIN_QQ
Environment=BOT_HOST=$BOT_HOST
Environment=BOT_PORT=$BOT_PORT
Environment=BOT_ACCESS_TOKEN=$ACCESS_TOKEN

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        systemctl enable yuki >/dev/null 2>&1
        systemctl restart yuki
        SYSTEMD_ENABLED=1
        ok "yuki.service 已安装并启动（开机自启 + 崩溃自动重启）"
        echo ""
        systemctl --no-pager -l status yuki | head -12 || true
    else
        info "跳过 systemd 配置（可用仓库里的 yuki.service 模板手动配置）"
    fi
else
    info "跳过 systemd 配置（无 systemd 或无 root）"
fi

# ---------- [7/7] 完成摘要 ----------
info "步骤 7/7：完成"
echo ""
echo "=========================================="
echo "               安装完成 🎉"
echo "=========================================="
echo "  安装目录   : $TARGET_DIR"
echo "  虚拟环境   : $VENV_PY"
echo "  配置文件   : $TARGET_DIR/config.json（功能开关 / #log 日志源）"
ADMIN_NOTE=""
if [ "$ADMIN_QQ" = "0" ]; then ADMIN_NOTE="（当前为 0，管理员指令未启用！）"; fi
echo "  管理员 QQ  : $ADMIN_QQ $ADMIN_NOTE"
echo "  监听地址   : $BOT_HOST:$BOT_PORT"
TOKEN_NOTE="未设置（不鉴权：任何能连到端口的客户端都可接入）"
if [ -n "$ACCESS_TOKEN" ]; then TOKEN_NOTE="已设置（内容不回显）"; fi
echo "  鉴权 token : $TOKEN_NOTE"
echo "  自启动     : $([ "$SYSTEMD_ENABLED" -eq 1 ] && echo 'systemd（yuki.service）' || echo '未配置，请用你的守护进程方式启动')"
echo ""
echo "  下一步：在 NapCat 配置反向 WebSocket 连到"
echo "      ws://<本机IP>:$BOT_PORT/"
echo "  （同机用 127.0.0.1:${BOT_PORT}，跨机用服务器 IP 并放行端口）"
if [ -n "$ACCESS_TOKEN" ]; then
    echo "  ⚠️ 已启用鉴权：NapCat 的 access_token 必须填相同的值，否则连不上"
fi
echo ""
if [ "$SYSTEMD_ENABLED" -eq 1 ]; then
    echo "  常用命令："
    echo "      systemctl status yuki      # 查看状态"
    echo "      journalctl -u yuki -f      # 实时日志"
    echo "      systemctl restart yuki     # 重启"
else
    echo "  手动启动（或交给 1panel/supervisor 守护）："
    echo "      cd $TARGET_DIR && $VENV_PY main.py"
    echo "  守护进程环境变量："
    echo "      BOT_ADMIN_QQ=$ADMIN_QQ  BOT_HOST=$BOT_HOST  BOT_PORT=$BOT_PORT"
    if [ -n "$ACCESS_TOKEN" ]; then
        echo "      BOT_ACCESS_TOKEN=$ACCESS_TOKEN   # NapCat 侧 access_token 必须一致"
    fi
    echo "  日志文件路径（1panel supervisor 默认）：/opt/1panel/data/supervisor/log/<程序名>-stdout.log"
    echo "  （填入 config.json 的 log.files 后即可在群里用 #log 查日志）"
fi
echo ""
echo "  升级方式：重新运行本脚本（config.json 与 data/ 不会被覆盖）"
echo "  卸载方式：systemctl disable --now yuki; rm /etc/systemd/system/yuki.service"
echo "            rm -rf $TARGET_DIR"
echo "=========================================="
