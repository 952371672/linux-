# CMCC 云电脑 Linux Docker 保活系统
## v1.5.0-dynamic-test 动态探针策略

- 每个账号独立探针 worker，不使用全局批量探测。
- 恢复后约 10 秒首次探测；稳定 1 分钟后约 30 秒；在线超过 15 分钟约 10 秒。
- 探测间隔应用约 ±10% 抖动；连续两次 suspect/need 才进入兜底恢复。
- 首次 suspect/need 后约 1 秒进行一次快速复核；复核恢复则取消重型恢复。
- `vmStatus=25` 表示云电脑已进入开机流程，保活动作已生效；进入 30 秒启动观察窗口，避免重复启动客户端。
- `vmStatus=1` 表示正常运行；`vmStatus=23` 表示关机，需经过确认后恢复。
- 缺少 SohoToken 时优先使用已保存的账号密码执行协议登录刷新，失败后才使用客户端兜底。
- 保留固定 6 个兜底槽位：slot0-slot5，对应 DISPLAY :100-:105、CDP 9223-9228。
- WebUI 计数按当前账号状态统计，实时日志中的探针汇总按同一账号集合统计。


> 当前版本：**v1.5.0-dynamic-test**（动态探针与快速异常复核版）。健康账号不会启动客户端；探针按账号在线阶段使用约 10/30/10 秒策略。

一个面向 Linux Docker 服务器的 CMCC 云电脑协议探针与客户端兜底保活系统。

## 核心工作方式

系统不是为每个账号长期运行一个客户端，而是采用轻量探针优先的资源节省架构：

```text
保存账号登录缓存 / Token
        ↓
按账号在线阶段以约 10/30/10 秒节奏进行协议/API 探针，读取真实云电脑状态
        ↓
状态正常：不启动客户端、不点击、不占用客户端槽位
        ↓
首次异常约 1 秒快速复核；连续确认疑似关机或需要启动
        ↓
占用受控客户端槽位
        ↓
SDK getFirmAuth / connectWorker 优先
        ↓
SDK 失败后使用官方客户端 + CDP 点击兜底
        ↓
观察真实状态恢复并记录结果
        ↓
完整回收客户端、CDP、profile 和槽位资源
```

## 主要功能

- 协议/API 实时探针，默认约每 10 秒检查一次；
- 正常账号跳过客户端，降低 CPU、内存和进程数量；
- SDK 优先保活，官方客户端/CDP 作为兜底；
- 固定 6 个独立客户端槽位，彼此独立（Xvfb/x11vnc 常驻，Electron 按需创建并清理）：
  - `slot0`：DISPLAY `:100`，CDP `9223`，VNC `5901`；
  - `slot1`：DISPLAY `:101`，CDP `9224`，VNC `5902`；
  - `slot2`：DISPLAY `:102`，CDP `9225`，VNC `5903`；
  - `slot3`：DISPLAY `:103`，CDP `9226`，VNC `5904`；
  - `slot4`：DISPLAY `:104`，CDP `9227`，VNC `5905`；
  - `slot5`：DISPLAY `:105`，CDP `9228`，VNC `5906`；
- 账号列表、状态、当前阶段和阶段日志 WebUI；
- 批量导入、批量导出、启动、停止、删除；
- WebUI Basic Auth 和在线修改密码；
- noVNC 观察实际正在运行的客户端槽位；槽位的 Xvfb/x11vnc 在容器启动时常驻，Electron/profile/CDP 仅在兜底任务期间按需创建并在任务结束后销毁；
- Docker 开机自启动和容器 `unless-stopped`；
- 事件日志尾部读取，避免日志不断增长导致内存占用增加；
- 实时阶段日志和协议探针汇总；事件日志按大小自动轮转，避免长期运行占满磁盘。

## 一键安装

### GitHub 安装（只访问 GitHub）

```bash
curl --http1.1 -fL --retry 5 --retry-all-errors --connect-timeout 30 --max-time 600 \
  https://raw.githubusercontent.com/952371672/linux-/main/install.sh | sudo bash
```

GitHub 安装脚本只从 GitHub Release 下载 `stable-latest/CMCC.Docker.zip`，不会访问 CNB 或读取 CNB Token。

### 已安装版本更新

更新前请确认服务器上的账号、Token、profile 和 WebUI 密码都在 `/opt/cmcc-linux-docker/data/` 或 `.env` 中。更新脚本会保留 `data/`、`.env` 和 `data/webui-auth.json`，不会删除账号运行数据。

```bash
curl --http1.1 -fL --retry 5 --retry-all-errors --connect-timeout 30 --max-time 600 \
  https://raw.githubusercontent.com/952371672/linux-/main/update.sh -o /tmp/cmcc-update.sh
sudo bash /tmp/cmcc-update.sh
```

也可以先下载完整包，再使用本地包更新（适合 GitHub 下载不稳定的服务器）：

```bash
scp CMCC.Docker.zip root@服务器IP:/tmp/CMCC.Docker.zip
ssh root@服务器IP 'CMCC_LOCAL_ARCHIVE=/tmp/CMCC.Docker.zip bash -s' < update.sh
```

更新脚本会自动校验 ZIP、停止旧容器、保留 `data/` 和 `.env`、重建镜像并启动新版本。更新完成后检查：

```bash
cd /opt/cmcc-linux-docker
docker compose -f novnc-compose.yml ps
curl -u 'admin:你的WebUI密码' http://127.0.0.1:8080/health
```

### CNB 安装/更新

CNB 使用独立的公开 OCI 制品渠道，不使用 GitHub 下载地址，也不需要填写 Token。CNB 网页的 `/-/raw/` 地址在未登录时可能返回 HTML 页面，不能直接管道给 `bash`，否则会出现 `curl: (23) Failure writing output to destination`。请先通过 Git 克隆脚本，再本地执行：

```bash
TMP_DIR="$(mktemp -d)"
git clone --depth 1 https://cnb.cool/952371672/cmcc-linux-docker.git "$TMP_DIR/cmcc-linux-docker"
sudo bash "$TMP_DIR/cmcc-linux-docker/install.sh"
rm -rf "$TMP_DIR"
```

在已有 CNB 安装上重复执行同一命令即可更新。脚本会保留 `/opt/cmcc-linux-docker/data/`、`.env` 和 WebUI 持久化密码，然后重新构建并启动容器。

如果服务器无法直接访问 CNB，可在能下载文件的电脑上下载 `CMCC.Docker.zip`，上传到服务器后执行：

```bash
sudo CMCC_LOCAL_ARCHIVE=/tmp/CMCC.Docker.zip bash install.sh
```

> GitHub 更新只使用 GitHub Release；CNB 安装/更新只使用 CNB OCI 制品。不要混用两个渠道的安装脚本。

## WebUI 首次登录密码（重要）

全新安装的默认 WebUI 登录凭据固定为：

```text
用户名：admin
密码：admin
```

登录地址：

```text
http://服务器IP:8080/
```

首次登录后，请立即点击页面顶部“修改密码”设置强密码。安装脚本只会在没有已有 `.env` 时创建以下配置：

```env
CMCC_WEBUI_USER=admin
CMCC_WEBUI_PASSWORD=admin
```

已有安装或更新时，脚本会保留原来的 `.env` 和 `data/webui-auth.json`，不会强制覆盖已有密码。如果之前通过页面修改过密码，运行时以以下文件为准：

```text
/opt/cmcc-linux-docker/data/webui-auth.json
```

忘记运行时密码时，可以删除运行时覆盖后重启，使服务重新使用 `.env`：

```bash
rm -f /opt/cmcc-linux-docker/data/webui-auth.json
docker compose -f /opt/cmcc-linux-docker/novnc-compose.yml up -d --force-recreate
```

如果 `.env` 也被删除，重新创建默认配置：

```bash
cat > /opt/cmcc-linux-docker/.env <<'EOF'
CMCC_WEBUI_USER=admin
CMCC_WEBUI_PASSWORD=admin
EOF
chmod 600 /opt/cmcc-linux-docker/.env
docker compose -f /opt/cmcc-linux-docker/novnc-compose.yml up -d --force-recreate
```

> `admin/admin` 仅用于首次登录，部署完成后必须立即修改。不要把修改后的真实密码提交到 GitHub、CNB、README、日志或聊天记录中。


账号列表中：

- **协议探针正常 / 已跳过客户端**：表示系统判断云电脑状态正常，没有启动官方客户端；
- **SDK 保活**：表示优先使用协议 SDK 完成保活；
- **点击兜底 / 客户端启动中 / 登录中**：表示正在使用受控客户端槽位；
- **slot0–slot5**：表示该账号当前占用的独立客户端槽位；
- **保活确认**：表示真实状态已经恢复。

### 批量导入格式

支持以下格式，每行一个账号：

```text
账号,密码
账号,密码,子账号登录
子账号登录,账号,密码
账号----密码
账号 密码
```

### 批量导出

批量导出受 WebUI Basic Auth 保护，生成 UTF-8 文本，格式与批量导入兼容。导出文件包含敏感信息，请妥善保管，不要上传到代码仓库或公开聊天。

## noVNC 实时页面

noVNC 用于观察**当前正在运行的官方客户端槽位**，不是每个账号永久保留的桌面。

正常账号不会创建客户端显示资源；只有进入SDK/客户端兜底时，系统才会为该账号临时创建对应的 Xvfb、CDP 和 x11vnc 资源，并在任务结束后自动销毁。

只有账号进入客户端兜底流程并占用 `slot0`–`slot5` 时，才有对应画面可查看。

如果服务器把 Docker 的 6080 端口转发到公网 22223 端口，请通过：

```text
http://服务器公网IP:22223/vnc.html
```

访问 noVNC。不要将 6080 直接暴露到公网；公网转发层应配置访问控制。

## 实时日志与探针汇总

页面底部“实时阶段日志”每秒刷新一次，显示最近的协议探针结果、正常账号汇总、异常探针以及 SDK/客户端兜底阶段。正常账号通常会显示为 `maybe_skip` /“协议探针正常”，这表示状态已确认，不会启动官方客户端。

事件日志文件位于 `/opt/cmcc-linux-docker/data/events.jsonl`。服务只读取最近一段日志供页面展示，并默认按以下策略轮转：单文件约 50 MiB，保留 5 个备份。不要手工删除正在使用的 `data/` 目录；如需调整上限，可通过环境变量 `CMCC_EVENTS_MAX_BYTES` 和 `CMCC_EVENTS_BACKUPS` 配置。

如果页面升级后仍显示旧的汇总或旧文案，请执行浏览器强制刷新：

```text
Ctrl+F5
```

页面显示“正常：0”时，先检查服务是否在线以及认证后的事件接口：

```bash
curl -u 'admin:你的WebUI密码' http://127.0.0.1:8080/events
```

接口应返回最近事件数组；若返回正常探针事件而浏览器仍显示旧内容，通常是浏览器缓存，应执行 `Ctrl+F5`。

## 运行检查

```bash
cd /opt/cmcc-linux-docker
docker compose -f novnc-compose.yml ps
docker ps --format '{{.Names}}|{{.Status}}|{{.Ports}}'
docker exec cmcc-linux-docker-cmcc-1 date -Is
```

WebUI 健康检查需要认证：

```bash
curl -u 'admin:你的WebUI密码' http://127.0.0.1:8080/health
```

noVNC 页面检查：

```bash
curl -I http://127.0.0.1:6080/vnc.html
```

## 安全注意事项

- 不要提交 `.env`、账号文件、Token、profile、事件日志或密码；
- 8080 WebUI 使用 Basic Auth；
- noVNC 独立于 WebUI 认证，公网转发时必须自行配置访问控制；
- 官方客户端目前按 amd64 环境验证；
- 不要使用全局 Docker prune 清理服务器；
- 更新后浏览器仍显示旧页面时，执行 `Ctrl+F5` 或 `Ctrl+Shift+R`。

## 目录和持久化数据

```text
/opt/cmcc-linux-docker/
├── data/                 # 必须保留：账号、Token、profile、事件数据
├── .env                  # 必须保留：基础配置
├── webui/                # WebUI 页面
├── service.py            # FastAPI 服务和保活调度器
├── novnc-compose.yml     # Docker Compose 配置
└── update.sh             # 更新脚本
```

项目仓库：

- GitHub：<https://github.com/952371672/linux->
- CNB：<https://cnb.cool/952371672/cmcc-linux-docker>

## 稳定性修复说明

当前版本包含以下修复：

- 已认证的 `#/home` 云电脑业务页即使暂时显示“暂无任何匹配结果”，也不会再被误判为登录页切换失败；
- 隐私确认后如果 Electron 替换 renderer，会重新发现当前页面并在有限次数内重试；
- CDP 遇到 `No such target id`、旧 WebSocket 失效或远程连接短暂断开时，会重新附着当前页面；
- 业务页状态会优先于登录表单判断，减少重复登录和客户端重启。
