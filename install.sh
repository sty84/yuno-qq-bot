#!/bin/bash
# 方案二一键安装：专用账号 aiagent + systemd + 白名单脚本
# 在服务器上执行：cd ~/qq-bot && bash install.sh
set -euo pipefail

BOT_DIR="${QQBOT_DIR:-/home/ubuntu/qq-bot}"

echo "==> 创建专用账号 aiagent（不能登录）"
sudo useradd -r -M -s /usr/sbin/nologin aiagent 2>/dev/null || echo "aiagent 已存在，跳过创建"

echo "==> 准备目录（机器人文件和数据目录归 aiagent）"
sudo mkdir -p "$BOT_DIR/data"
sudo chown -R aiagent:aiagent "$BOT_DIR"

echo "==> 加固：config.json 改为 root 所有、aiagent 只读"
sudo chown root:aiagent "$BOT_DIR/config.json"
sudo chmod 640 "$BOT_DIR/config.json"

echo "==> 保证 aiagent 能进入 BOT_DIR 的父目录（home 目录为 700 时 sudo -u aiagent 会报 Permission denied）"
sudo chmod o+x "$(dirname "$BOT_DIR")" 2>/dev/null || true

echo "==> 安装 Python 依赖（清华源）"
sudo -u aiagent python3 -m venv "$BOT_DIR/venv"
sudo -u aiagent "$BOT_DIR/venv/bin/pip" install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r "$BOT_DIR/requirements.txt"

echo "==> 安装白名单脚本 qqbot-ctl"
sudo install -o root -g root -m 755 qqbot-ctl /usr/local/bin/qqbot-ctl

echo "==> 配置日志轮转（bot.log 保留 7 天）"
sudo install -o root -g root -m 644 logrotate-qqbot.conf /etc/logrotate.d/qqbot

echo "==> 配置 sudoers（aiagent 只能运行白名单脚本和重启机器人服务）"
echo 'aiagent ALL=(root) NOPASSWD: /usr/local/bin/qqbot-ctl *' | sudo tee /etc/sudoers.d/qqbot-ctl >/dev/null
echo 'aiagent ALL=(root) NOPASSWD: /usr/bin/systemctl restart qqbot' | sudo tee /etc/sudoers.d/qqbot-restart >/dev/null
sudo chmod 440 /etc/sudoers.d/qqbot-ctl /etc/sudoers.d/qqbot-restart

echo "==> 安装并启动 systemd 服务"
sudo install -o root -g root -m 644 qqbot.service /etc/systemd/system/qqbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now qqbot

echo "==> 停用旧的 Docker 版机器人（如有）"
cd "$BOT_DIR" && docker compose down 2>/dev/null || true

echo ""
echo "安装完成。常用命令："
echo "  查看状态：systemctl status qqbot"
echo "  查看日志：journalctl -u qqbot -f"
