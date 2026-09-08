#!/usr/bin/env bash
# 域名监控 & 自动抢注 —— 一键安装
#
#   ./install.sh              交互式安装（推荐）
#   ./install.sh --yes        全部用默认值，不提问
#   ./install.sh --systemd    装完顺便配置 systemd 开机自启（需要 root）
#
# 脚本是幂等的：已存在的配置和密钥不会被覆盖，重复运行安全。

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$REPO_DIR/.venv"
CONFIG_FILE="$REPO_DIR/config.yaml"
ENV_FILE="$REPO_DIR/.env"
MIN_MAJOR=3
MIN_MINOR=10

ASSUME_YES=0
SETUP_SYSTEMD=0

# 非交互场景（CI、docker exec 不带 -t、AI 代理的 shell）下的配置入口，
# 免得脚本因为问不出话来就装出一个空壳还不吭声。
DM_DOMAINS="${DM_DOMAINS:-}"
DM_TG_TOKEN="${DM_TG_TOKEN:-}"
DM_TG_CHAT="${DM_TG_CHAT:-}"
for arg in "$@"; do
  case "$arg" in
    --yes|-y)   ASSUME_YES=1 ;;
    --systemd)  SETUP_SYSTEMD=1 ;;
    --help|-h)
      sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数: $arg（用 --help 查看用法）" >&2; exit 2 ;;
  esac
done

info()  { printf '\033[36m▸\033[0m %s\n' "$*"; }
ok()    { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '\033[33m!\033[0m %s\n' "$*"; }
fail()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# 没有终端就没法提问。这种情况下必须明确切成非交互，
# 否则每个 read 都静默失败、用默认值走完，用户以为装好了其实什么都没配。
HAS_TTY=0
[ -r /dev/tty ] && [ -t 1 ] && HAS_TTY=1

ask() {  # ask <提示> <默认值>
  local prompt="$1" default="${2:-}" reply
  if [ "$ASSUME_YES" = 1 ] || [ "$HAS_TTY" = 0 ]; then echo "$default"; return; fi
  if [ -n "$default" ]; then
    read -r -p "$prompt [$default]: " reply </dev/tty || reply=""
  else
    read -r -p "$prompt: " reply </dev/tty || reply=""
  fi
  echo "${reply:-$default}"
}

ask_secret() {  # ask_secret <提示> <默认值>；输入不回显
  local prompt="$1" default="${2:-}" reply
  if [ "$ASSUME_YES" = 1 ] || [ "$HAS_TTY" = 0 ]; then echo "$default"; return; fi
  read -r -s -p "$prompt: " reply </dev/tty || reply=""
  echo >/dev/tty
  echo "${reply:-$default}"
}

# ---------------------------------------------------------------- 1. 找 Python
if [ "$HAS_TTY" = 0 ] && [ "$ASSUME_YES" = 0 ]; then
  warn "检测不到终端，按非交互模式安装（不会提问）。"
  warn "要在这种环境下预填配置，用环境变量："
  warn "  DM_DOMAINS='a.com b.com' DM_TG_TOKEN=xxx DM_TG_CHAT=123 ./install.sh"
fi

info "检查 Python 版本（需要 ${MIN_MAJOR}.${MIN_MINOR}+）"
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
  command -v "$candidate" >/dev/null 2>&1 || continue
  if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_MAJOR,$MIN_MINOR) else 1)" 2>/dev/null; then
    PYTHON="$candidate"; break
  fi
done
[ -n "$PYTHON" ] || fail "没找到 Python ${MIN_MAJOR}.${MIN_MINOR}+。
  Ubuntu/Debian: sudo apt update && sudo apt install -y python3.11 python3.11-venv
  CentOS/RHEL:   sudo dnf install -y python3.11
  macOS:         brew install python@3.11"
ok "使用 $($PYTHON --version) （$(command -v "$PYTHON")）"

# ------------------------------------------------------------- 2. 虚拟环境
if [ -d "$VENV_DIR" ]; then
  ok "虚拟环境已存在，跳过创建"
else
  info "创建虚拟环境 .venv"
  "$PYTHON" -m venv "$VENV_DIR" 2>/dev/null || fail "创建虚拟环境失败。
  Debian/Ubuntu 上通常是缺 venv 模块：sudo apt install -y python3-venv"
  ok "虚拟环境已创建"
fi
VPY="$VENV_DIR/bin/python"
[ -x "$VPY" ] || fail "虚拟环境损坏，请删除 .venv 后重跑：rm -rf '$VENV_DIR'"

# ------------------------------------------------------------- 3. 装依赖
info "安装依赖（httpx / PyYAML / dnspython）"
"$VPY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || warn "pip 自升级失败，继续"
if ! "$VPY" -m pip install --quiet -r "$REPO_DIR/requirements.txt"; then
  warn "直接安装失败，改用清华镜像重试"
  "$VPY" -m pip install --quiet -i https://pypi.tuna.tsinghua.edu.cn/simple \
    -r "$REPO_DIR/requirements.txt" || fail "依赖安装失败，请检查网络"
fi
ok "依赖安装完成"

# ------------------------------------------------------------- 4. 配置文件
if [ -f "$CONFIG_FILE" ]; then
  ok "config.yaml 已存在，保持不动"
else
  info "生成 config.yaml"
  cp "$REPO_DIR/config.example.yaml" "$CONFIG_FILE"
  # 模板里的 example.com 之类只是示例，清掉，否则新装用户会在监控一堆无关域名
  "$VPY" "$REPO_DIR/domain_monitor/config_edit.py" "$CONFIG_FILE" domains
  ok "已从模板生成 config.yaml（示例域名已清空）"
fi

# ------------------------------------------------------------- 5. 要监控的域名
if [ "$ASSUME_YES" = 0 ] || [ -n "$DM_DOMAINS" ]; then
  echo
  info "要监控哪些域名？（空格分隔，直接回车跳过，之后也能在 Telegram 里加）"
  DOMAINS="$(ask '  域名' "$DM_DOMAINS")"
  if [ -n "$DOMAINS" ]; then
    # shellcheck disable=SC2086
    "$VPY" "$REPO_DIR/domain_monitor/config_edit.py" "$CONFIG_FILE" domains $DOMAINS
    ok "已写入监控域名"
  fi
fi

# ------------------------------------------------------------- 6. Telegram
if [ -f "$ENV_FILE" ]; then
  ok ".env 已存在，不覆盖已有密钥"
else
  echo
  info "配置 Telegram（可跳过，之后再填也行）"
  echo "  1) 找 @BotFather 发 /newbot 拿 token"
  echo "  2) 找 @userinfobot 拿你的数字 user id"
  echo "  3) 记得主动给你的机器人发一句 /start，否则它没法给你发消息"
  TG_TOKEN="$(ask_secret '  Bot Token（输入不回显，回车跳过）' "$DM_TG_TOKEN")"
  TG_CHAT=""
  [ -n "$TG_TOKEN" ] && TG_CHAT="$(ask '  你的 user id' "$DM_TG_CHAT")"

  umask 077
  {
    echo "# 域名监控的密钥。这个文件不进 git（.gitignore 已排除）。"
    echo "# 改完后重启服务生效。"
    echo "TG_BOT_TOKEN=${TG_TOKEN}"
    echo "TG_CHAT_ID=${TG_CHAT}"
    echo
    echo "# 注册商 API（要开启自动抢注时再填，先留空）"
    echo "NAMESILO_API_KEY="
    echo "DYNADOT_API_KEY="
    echo "ALIYUN_AK="
    echo "ALIYUN_SK="
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ok "密钥已写入 .env（权限 600，仅本人可读）"

  if [ -n "$TG_TOKEN" ]; then
    "$VPY" "$REPO_DIR/domain_monitor/config_edit.py" "$CONFIG_FILE" telegram-on
    ok "已在 config.yaml 里打开 Telegram"
  fi
fi

# ------------------------------------------------------------- 7. 自检
echo
info "运行自检"
set +e
# shellcheck disable=SC1090
set -a; [ -f "$ENV_FILE" ] && . "$ENV_FILE"; set +a
"$VPY" -m domain_monitor -c "$CONFIG_FILE" test
TEST_RC=$?
set -e
[ $TEST_RC -eq 0 ] && ok "自检全部通过" || warn "自检有未通过项（见上文）。网络不通或密钥没填都会这样，不影响已装好的部分。"

# ------------------------------------------------------------- 8. systemd
if [ "$SETUP_SYSTEMD" = 1 ]; then
  echo
  [ "$(id -u)" -eq 0 ] || fail "--systemd 需要 root：sudo ./install.sh --systemd"
  info "配置 systemd 开机自启"
  UNIT=/etc/systemd/system/domain-monitor.service
  sed -e "s|/opt/domain-monitor/.venv/bin/python|$VPY|" \
      -e "s|/opt/domain-monitor/config.yaml|$CONFIG_FILE|" \
      -e "s|WorkingDirectory=.*|WorkingDirectory=$REPO_DIR|" \
      -e "s|EnvironmentFile=.*|EnvironmentFile=-$ENV_FILE|" \
      -e "s|^User=.*|User=$(stat -c '%U' "$REPO_DIR")|" \
      -e "s|^Group=.*|Group=$(stat -c '%G' "$REPO_DIR")|" \
      -e "s|ReadWritePaths=.*|ReadWritePaths=$REPO_DIR|" \
      "$REPO_DIR/deploy/domain-monitor.service" > "$UNIT"
  systemctl daemon-reload
  systemctl enable --now domain-monitor
  ok "服务已启动：systemctl status domain-monitor"
fi

# ------------------------------------------------------------- 完成
cat <<EOF

$(ok "安装完成")

接下来：

  查一个域名现在什么状态
    $VPY -m domain_monitor check example.com

  看监控列表
    $VPY -m domain_monitor list

  跑起来（前台，Ctrl-C 退出）
    $VPY -m domain_monitor run
    （.env 会被自动读取，不用手动 source）

  常驻后台（开机自启）
    sudo ./install.sh --systemd

配置文件： $CONFIG_FILE
密钥文件： $ENV_FILE  （权限 600）

⚠️  默认是【只监控不下单】。要开启自动抢注，先读 README 的
   「抢注要准备什么」一节，把注册商开户、充值、API 都配好，
   先用 dry_run 演练几天再改成真实下单。
EOF
