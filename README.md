# YUNO AI —— QQ 官方机器人（DeepSeek 驱动）

一个跑在 Linux 服务器上的 QQ 官方群聊机器人：群里 @ 她，她会以设定的人设（默认：BanG Dream! 梦限大 MewType 的 DJ 千石由乃）调用 DeepSeek 回答。

采用**插件架构 + 专用账号 + 白名单权限**：核心只负责连接和路由，所有功能以插件形式放在 `plugins/` 目录，扩展功能不用改核心。

## 功能一览

| 分类 | 功能 |
|---|---|
| 聊天 | 角色人设对话、情绪系统、自动记忆（按场景隔离） |
| 查询 | `/状态`（负载/CPU/内存/磁盘/运行时间）、`/天气`、`/余额` |
| 记忆 | 自动提取关键信息、`/记住`、`/我的记忆`、`/群记忆`、`/清除记忆` |
| 身份 | `/绑定`（群 ↔ 私聊同一个人）、`/解绑`、`/昵称` |
| 游戏 | `/成语` 成语接龙（支持外部大词库）、`/答题`、`/排名` |
| 管理 | `/容器` 启停（白名单）、`/写文件`、`/读文件`、`/命令` AI 执行、`/报告` |
| 后台 | 定时日报邮件、异常告警邮件、低余额提醒、随机主动消息（默认关闭） |

## 架构

```
bot.py                  核心：QQ 连接、消息路由、插件加载、分段回复
plugins/
  _shared.py            共享基础层（配置、AI 调用、容器/文件/邮件等）
  _db.py                SQLite 数据层（记忆/分数/昵称/绑定/状态）
  info.py               查询监控插件
  manage.py             服务器管理插件
  memory.py             记忆与身份插件
  games.py              娱乐互动插件
qqbot-ctl               宿主机白名单脚本（root 校验后操作 Docker）
install.sh              一键安装（创建 aiagent 账号 + systemd + 依赖）
qqbot.service           systemd 服务单元
logrotate-qqbot.conf    日志轮转（每日、保留 7 天）
```

安全模型：机器人以专用账号 `aiagent` 跑在宿主机（无 docker.sock）；容器操作经 `sudo → qqbot-ctl → config.json 白名单` 校验；文件读写限定在 data 目录；敏感指令仅管理员（`ADMIN_OPENIDS`）可用。

## 快速开始

### 前置条件

1. [q.qq.com](https://q.qq.com) 完成个人/企业认证并创建机器人，拿到 `AppID`、`AppSecret`，把服务器公网 IP 加入后台 IP 白名单。
2. [platform.deepseek.com](https://platform.deepseek.com) 创建 API Key。
3. 一台 Linux 服务器（示例路径 `/home/ubuntu/qq-bot`，可用环境变量 `QQBOT_DIR` 覆盖）。

### 部署

```bash
# 1. 上传本项目到服务器（示例）
scp -r qq-bot-github ubuntu@<你的服务器IP>:~
mv ~/qq-bot-github ~/qq-bot && cd ~/qq-bot

# 2. 配置密钥
cp .env.example .env
nano .env          # 填 APPID / SECRET / DEEPSEEK_API_KEY / ADMIN_OPENIDS

# 3. 一键安装（自动创建 aiagent 账号、装依赖、装 systemd 服务）
bash install.sh

# 4. 验证
systemctl status qqbot
sudo tail -n 20 ~/qq-bot/data/bot.log    # 应看到 4 个插件加载 + 登录成功 + 心跳
```

> 若服务器报 `Permission denied`，说明家目录权限太紧：
> `sudo setfacl -m u:aiagent:x /home/ubuntu 2>/dev/null || sudo chmod o+x /home/ubuntu`
>
> 若报 `ensurepip is not available`：`sudo apt install -y python3-venv`，再重跑 `bash install.sh`。

### 配置管理员

私聊机器人发 `/我的ID`，把返回的 `user_openid` 填入 `.env` 的 `ADMIN_OPENIDS=`，然后 `sudo systemctl restart qqbot`。

## 配置说明

### .env（密钥与基础设置）

| 变量 | 说明 |
|---|---|
| `APPID` / `SECRET` | q.qq.com 开发设置中的机器人凭证 |
| `DEEPSEEK_API_KEY` | DeepSeek API Key |
| `SYSTEM_PROMPT` | 机器人人设（可自定义） |
| `ADMIN_OPENIDS` | 管理员 openid，多个用英文逗号分隔 |
| `SMTP_*` / `MAIL_TO` | 邮件日报（可选，`SMTP_PASS` 填邮箱授权码） |

### config.json（权限与后台开关）

| 段 | 说明 |
|---|---|
| `allowed_paths` | 允许读写文件的路径（默认 data 目录） |
| `containers` | 允许管理的容器白名单（关键词/路径/允许操作） |
| `report` | 定时日报邮件（`enabled`/`hour`/`minute`/`anomaly_immediate`） |
| `random_events` | 随机主动消息（`enabled`/`group_openid`/间隔） |
| `balance_alert` | 低余额提醒（`enabled`/`threshold`/`target_type`/`target`） |

配置在服务器上修改（改完最多 5 分钟自动生效，或重启服务立即生效）：

```bash
# 推荐：走白名单脚本（带校验、原子写）
sudo qqbot-ctl config-set report enabled true
sudo qqbot-ctl config-set balance_alert target_type c2c

# 或手动编辑
sudo nano ~/qq-bot/config.json
```

添加要管理的容器：

```bash
/容器 添加 关键词 /home/ubuntu/项目目录   # QQ 私聊（管理员）
```

## 指令参考

**所有人可用（群聊需 @ 机器人）：**

| 指令 | 说明 |
|---|---|
| 直接聊天 | DeepSeek 按人设回复（带记忆与心情） |
| `/帮助` | 指令菜单 |
| `/状态` | 服务器负载、CPU、内存、磁盘、运行时间 |
| `/天气 城市` | 查天气（默认上海） |
| `/记住 内容` | 手动补充记忆 |
| `/我的记忆` / `/群记忆` | 查看当前场景/本群记忆 |
| `/清除记忆` | 清除当前场景记忆 |
| `/昵称 名字` | 设置排行榜昵称（绑定后跨场景同步） |
| `/绑定` / `/解绑` | 群 ↔ 私聊身份对应（绑定成功群里自动公告） |
| `/成语` / `/答题` / `/排名` | 游戏与排行榜（游戏中发「结束」退出） |
| `/我的ID` | 查看自己的 openid |

**仅管理员：**

| 指令 | 说明 |
|---|---|
| `/容器 列表/启动/停止/重启/添加/删除` | 白名单容器管理 |
| `/写文件 文件名 内容` / `/读文件 文件名` | data 目录内文件读写 |
| `/命令 你的要求` | AI 按自然语言执行白名单动作 |
| `/余额` / `/余额 测试` | 查余额 / 测试低余额提醒通道 |
| `/报告 现在` / `/报告 测试` | 立即生成日报 / 发测试邮件 |
| `/重启` | 重启机器人服务 |

## 数据与存储

所有运行时数据在 `data/` 目录，存储在 SQLite（`bot.db`）：记忆、绑定关系、分数、昵称、心情、群列表。首次启动会自动把旧版 JSON 数据迁移进来。删除 `bot.db` 即重置全部数据（注意同时删除旧 JSON 文件，否则会再次迁移）。

日志写入 `data/bot.log`，每日轮转、保留 7 天。

## 开发新插件

在 `plugins/` 放一个 `.py` 文件，核心自动加载。插件协议：

```python
from plugins import _shared

NAME = "我的插件"
HELP = "/新指令 —— 说明"

COMMANDS = {"/新指令": handler}   # 处理函数可返回 str/None 或协程

def handler(text, ctx):
    # ctx: is_admin / chat_key / user_key / scene / api / config ...
    return "hello"

def chat_context(ctx): ...        # 可选：注入聊天上下文
def game_try(ctx, text): ...      # 可选：非指令消息交给游戏
async def after_chat(ctx, text, reply): ...  # 可选：AI 回复后钩子
def loops(make_ctx): ...          # 可选：后台协程
```

## 常见问题

- **连不上 / 401**：检查 `.env` 的 AppID/AppSecret、后台 IP 白名单。
- **群聊不响应**：官方机器人只接收被 @ 的消息；上线前仅沙箱环境可用（个人开发者沙箱群可能受限，可先用私聊测试）。
- **pip 安装失败**：install.sh 已使用清华源；仍失败可手动 `sudo -u aiagent ./venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt`。
- **权限不足 / 上传失败**：`~/qq-bot` 归 aiagent 所有，更新文件先 `sudo chown -R ubuntu:ubuntu ~/qq-bot`，传完恢复 `sudo chown -R aiagent:aiagent ~/qq-bot` 并重置 config.json 为 `root:aiagent 640`。

## 免责声明

本项目仅供学习交流。请遵守 QQ 机器人开放平台与 DeepSeek 服务条款；审核资料中请勿填写与 AI/大模型相关的描述。
