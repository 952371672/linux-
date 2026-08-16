# CMCC 云电脑 Linux Docker 保活系统

当前发布版本：**1.5.6-efficient-observe**  
生产源码 SHA256：`3d0358d239309bfbc07b37f51c96e11284ac576cec9b91722b2f65a99ab4f4ee`

本项目使用轻量 SOHO 状态探针配合官方 CMCC Linux 客户端、SDK 和 CDP 点击兜底，对多账号云电脑进行实时状态监测与恢复。

> 完整包和仓库源码不包含账号、密码、Token、Profile、`.env`、事件日志或服务器运行数据。

## 当前保活机制

```text
每账号轻量 SOHO 探针
        ↓
vmStatus=1：运行中，跳过客户端
vmStatus=21：关机过渡，只观察
vmStatus=25：启动中，停止重复恢复
vmStatus=23：连续确认后进入恢复队列，优先级20
        ↓
账号级 single-flight 去重
        ↓
六个固定官方客户端槽位
        ↓
官方 getFirmAuth → mainApi.connectWorker
        ↓
SDK动作完成后立即释放客户端槽位
        ↓
原账号轻量探针异步确认 vmStatus=25/1
        ↓
SDK明确报错时才尝试一次客户端/CDP点击兜底
```

### 证据语义

- `sdk_action_done`：SDK动作完成，不代表云端已经恢复；
- `click_dispatched`：点击动作已发送，不代表云端已经恢复；
- `recovery_confirmed`：独立探针读取到新鲜的 `vmStatus=25/1`，才是云端恢复证据；
- `recovery_failed`：完整动作链明确失败，当前账号固定冷却60秒后可重新进入队列。

HTTP 200、MQTT PINGRESP、`connectWorker done=True`、按钮点击和容器运行都不能单独算作保活成功。

## 1.5.6 更新内容

- 保留六个固定客户端槽位和 `fallback_concurrency=6`；
- 保留 `vmStatus=23` 优先恢复；
- 保留账号级 `QUEUED/RUNNING/COOLDOWN` 去重；
- 修复重型恢复后连续补跑过期探针节拍的问题；
- 成功动作观察窗口内不再被单次正常探针提前清除；
- 同优先级队列改用单调序号，保持严格 FIFO；
- SDK动作和云端恢复确认分离；
- SDK动作后立即清理客户端并释放重型槽位，由轻量探针异步确认；
- 失败冷却固定60秒，不再指数增长；
- 同一恢复周期只记录一次 `recovery_confirmed`；
- 不包含已经验证会降低效率的严格云卡片门控、强制清认证、持槽同步探针和MQTT/HTTP伪成功逻辑。

## 固定客户端槽位

| 槽位 | DISPLAY | CDP | VNC |
|---|---:|---:|---:|
| slot0 | :100 | 9223 | 5901 |
| slot1 | :101 | 9224 | 5902 |
| slot2 | :102 | 9225 | 5903 |
| slot3 | :103 | 9226 | 5904 |
| slot4 | :104 | 9227 | 5905 |
| slot5 | :105 | 9228 | 5906 |

六套 Xvfb/x11vnc 常驻，官方客户端只在账号进入重型恢复时启动，动作完成后清理并释放槽位。

## 支持环境

- Linux amd64；
- Docker Engine；
- Docker Compose v2；
- 建议至少 4 核 CPU、8 GiB 内存；
- 默认 WebUI 端口 `8080`；
- 默认 noVNC 端口 `6080`。

## 一键安装

### GitHub

```bash
curl --http1.1 -fL --retry 5 --retry-all-errors --retry-delay 2 \
  'https://raw.githubusercontent.com/952371672/linux-/main/install.sh' | sudo bash
```

### CNB

```bash
curl --http1.1 -fL --retry 5 --retry-all-errors --retry-delay 2 \
  'https://cnb.cool/952371672/cmcc-linux-docker/-/raw/main/install-cnb.sh' | sudo bash
```

安装脚本会安装依赖、下载完整包、保留已有 `data/` 和 `.env`、构建镜像并启动服务。服务器网络受限时，可以先把 `CMCC.Docker.zip` 放在当前目录，安装脚本会优先使用本地文件。

## 更新

### GitHub

```bash
curl --http1.1 -fL --retry 5 --retry-all-errors --retry-delay 2 \
  'https://raw.githubusercontent.com/952371672/linux-/main/update.sh' | sudo bash
```

### CNB

```bash
curl --http1.1 -fL --retry 5 --retry-all-errors --retry-delay 2 \
  'https://cnb.cool/952371672/cmcc-linux-docker/-/raw/main/update-cnb.sh' | sudo bash
```

更新时必须保留：

```text
/opt/cmcc-linux-docker/data/
/opt/cmcc-linux-docker/.env
```

它们包含账号配置、加密密码、Token、Profile、WebUI密码覆盖和运行状态。安装/更新脚本不会执行全局 Docker prune。

## 首次登录

默认地址：

```text
http://服务器IP:8080/
```

全新安装默认凭据：

```text
用户名：admin
密码：admin
```

首次登录后必须立即在WebUI中修改密码。已有安装会保留 `.env` 和 `data/webui-auth.json`。

## 批量导入

主账号：

```text
账号,密码
```

子账号：

```text
子账号,密码,子账号登录
```

账号、密码和Token不得提交到仓库或打进发布包。

## 常用检查

```bash
cd /opt/cmcc-linux-docker
docker compose -f novnc-compose.yml ps
docker inspect -f 'STATUS={{.State.Status}} RESTART={{.RestartCount}} OOM={{.State.OOMKilled}}' cmcc-linux-docker-cmcc-1
docker exec cmcc-linux-docker-cmcc-1 python3 -m py_compile /opt/cmcc-app/service.py
```

认证后的健康检查：

```bash
curl -u 'admin:你的WebUI密码' http://127.0.0.1:8080/health
```

noVNC：

```text
http://服务器IP:6080/vnc.html
```

noVNC与WebUI认证相互独立，不应将6080直接暴露到公网；如需远程访问，应在反向代理/NAT层增加认证和访问控制。

## 项目目录

```text
CMCC.Docker/
├── service.py
├── novnc-Dockerfile
├── novnc-compose.yml
├── novnc-entrypoint.sh
├── fallback-start-client.sh
├── stable-requirements.txt
├── install.sh
├── update.sh
├── webui/
│   ├── index.html
│   └── live.html
├── CMCC-JTYDN-UOSx86-2.23.1.deb
└── README.md
```

不在仓库/发布包中的运行数据：

```text
.env
data/
profiles/
accounts.json
events.jsonl
webui-auth.json
Token/Cookie/密码文件
```

## 项目地址

- GitHub：<https://github.com/952371672/linux->
- CNB：<https://cnb.cool/952371672/cmcc-linux-docker>
