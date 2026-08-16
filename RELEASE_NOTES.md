# CMCC Docker 1.5.6-efficient-observe

## 核心更新

- 修复重型恢复后补跑过期探针导致的重复触发和队列压力；
- 保留六个固定客户端槽位与 `fallback_concurrency=6`；
- 保留 `vmStatus=23` 优先和账号级 single-flight；
- SDK动作完成立即释放客户端槽位，由独立SOHO探针异步确认 `vmStatus=25/1`；
- `sdk_action_done`、`click_dispatched` 不再作为云端成功；
- 每个恢复周期只记录一次 `recovery_confirmed`；
- 失败冷却固定60秒，不再指数增长；
- 同优先级队列改为单调FIFO序号。

## 保活证据

只有新鲜的：

```text
vmStatus=25（启动中）
vmStatus=1（运行中）
```

可作为云端恢复证据。HTTP 200、MQTT PINGRESP、SDK Promise完成、点击动作和容器运行不等于云端恢复。

## 文件

- `CMCC.Docker.zip`：包含代码、WebUI、Docker配置、安装/更新脚本和官方Linux客户端的完整包；
- 仓库源码不包含 `.env`、账号、密码、Token、Profile、事件日志和服务器运行数据。

## 安装

GitHub：

```bash
curl --http1.1 -fL --retry 5 --retry-all-errors --retry-delay 2 \
  'https://raw.githubusercontent.com/952371672/linux-/main/install.sh' | sudo bash
```

CNB：

```bash
curl --http1.1 -fL --retry 5 --retry-all-errors --retry-delay 2 \
  'https://cnb.cool/952371672/cmcc-linux-docker/-/raw/main/install-cnb.sh' | sudo bash
```

详细配置、更新和故障检查见仓库 `README.md`。
