#!/bin/bash
# 本机（桌面 Linux）一键安装脚本 —— 已从「服务器版」改造：
#   原版创建 aiagent 专用账号 / root 加固 / sudoers 白名单 / 停 Docker，均为云端服务器所需；
#   本地桌面单用户环境改为：当前用户 + venv + CUDA torch（NVIDIA 卡）+ 其余依赖。
# 用法：cd 到本项目目录 && bash install.sh
set -euo pipefail

BOT_DIR="${QQBOT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
echo "==> 项目目录：$BOT_DIR"

echo "==> 创建 venv"
python3 -m venv "$BOT_DIR/venv"

echo "==> 升级 pip"
"$BOT_DIR/venv/bin/pip" install --upgrade pip

echo "==> 安装 CUDA 版 torch（NVIDIA 卡；无独显时改装 CPU 版）"
# 国内直连 download.pytorch.org 常卡住，改从阿里云镜像按当前 Python 版本取 wheel；
# 海外或需换版本时，把下面 TORCH_WHEEL 换成官方源对应 wheel 即可。
PYV="$("$BOT_DIR/venv/bin/python" -c 'import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")')"
TORCH_WHEEL="https://mirrors.aliyun.com/pytorch-wheels/cu124/torch-2.6.0%2Bcu124-cp${PYV}-cp${PYV}-linux_x86_64.whl"
"$BOT_DIR/venv/bin/pip" install "$TORCH_WHEEL" -i https://pypi.tuna.tsinghua.edu.cn/simple

echo "==> 安装其余依赖（清华源）"
"$BOT_DIR/venv/bin/pip" install -r "$BOT_DIR/requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple

echo ""
echo "安装完成。前台运行："
echo "  cd '$BOT_DIR' && ./venv/bin/python bot.py"
echo "如需开机自启（systemd，需 sudo 密码）："
echo "  sudo cp '$BOT_DIR/qqbot.service' /etc/systemd/system/"
echo "  sudo systemctl daemon-reload && sudo systemctl enable --now qqbot"
