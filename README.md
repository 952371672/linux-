# CMCC 云电脑 Linux Docker 保活系统

一个面向 Linux Docker 服务器的 CMCC 云电脑协议探针与客户端兜底保活系统。

## 核心工作方式

系统不是为每个账号长期运行一个客户端，而是采用轻量探针优先的资源节省架构：

```text
保存账号登录缓存 / Token
        ↓
每 10 秒协议/API 探针读取真实云电脑状态
        ↓
状态正常：不启动客户端、不点击、不占用客户端槽位
        ↓
连续确认疑似关机或需要启动
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
- 6 个独立客户端槽位，彼此独立：
  - `slot0`：DISPLAY `:100`，CDP `9223`，VNC `5901`；
  - `slot1`：DISPLAY `:101`，CDP `9224`，VNC `5902`；
  - `slot2`：DISPLAY `:102`，CDP `9225`，VNC `5903`；
  - `slot3`：DISPLAY `:103`，CDP `9226`，VNC `5904`；
  - `slot4`：DISPLAY `:104`，CDP `9227`，VNC `5905`；
  - `slot5`：DISPLAY `:105`，CDP `9228`，VNC `5906`；
- 账号列表、状态、当前阶段和阶段日志 WebUI；
- 批量导入、批量导出、启动、停止、删除；
- WebUI Basic Auth 和在线修改密码；
- noVNC 观察实际正在运行的客户端槽位；
- Docker 开机自启动和容器 `unless-stopped`；
- 事件日志尾部读取，避免日志不断增长导致内存占用增加。

## 一键安装

### GitHub 安装

```bash
curl --http1.1 -fL --retry 5 --retry-all-errors --retry-delay 2 \
  'https://raw.githubusercontent.com/952371672/linux-/main/install.sh' | sudo bash
```

### CNB 安装

```bash
curl --http1.1 -fL --retry 5 --retry-all-errors --retry-delay 2 \
  'https://cnb.cool/952371672/cmcc-linux-docker/-/raw/main/install.sh' | sudo bash
```

安装脚本会自动完成：

1. 下载固定发布资产 `stable-latest/CMCC.Docker.zip`；
2. 解压项目文件；
3. 保留已有 `data/`、`.env` 和 WebUI 认证配置；
4. 构建 amd64 Docker 镜像；
5. 启动 `cmcc-linux-docker-cmcc-1`；
6. 检查容器和健康状态。

如果服务器访问 GitHub 受限，也可以先将 `CMCC.Docker.zip` 放在当前目录，安装脚本会优先使用本地压缩包。

## 更新已有安装

推荐使用固定更新脚本。更新前不要删除 `data/`，其中包含账号、密码加密数据、Token、登录缓存、profile 和运行数据。

### GitHub 更新

```bash
curl --http1.1 -fL --retry 5 --retry-all-errors --retry-delay 2 \
  'https://raw.githubusercontent.com/952371672/linux-/main/update.sh' | sudo bash
```

### CNB 更新

```bash
curl --http1.1 -fL --retry 5 --retry-all-errors --retry-delay 2 \
  'https://cnb.cool/952371672/cmcc-linux-docker/-/raw/main/update.sh' | sudo bash
```

更新脚本具有版本标记判断：如果当前服务器已经是同一个 `stable-latest` 资产，不会重复下载、停止容器或重建镜像。

更新时只清理当前 Compose 项目的旧资源，不使用全局 Docker 清理命令。不会执行：

```bash
docker system prune -a
docker image prune -a
```

## WebUI 使用

默认地址：

```text
http://服务器IP:8080/
```

WebUI 使用 Basic Auth。首次安装后请使用页面顶部“修改密码”功能设置新的 WebUI 密码。

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

如果账号当前处于协议探针正常状态，系统可能没有启动客户端，也没有可查看的 VNC 桌面。此时实时页面显示无法连接是预期现象，不代表保活失败。

只有账号进入客户端兜底流程并占用 `slot0`–`slot5` 时，才有对应画面可查看。

如果服务器把 Docker 的 6080 端口转发到公网 22223 端口，请通过：

```text
http://服务器公网IP:22223/vnc.html
```

访问 noVNC。不要将 6080 直接暴露到公网；公网转发层应配置访问控制。

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
