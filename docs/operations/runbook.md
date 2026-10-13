# 配置与部署

本说明针对全新技术部署。代码库不包含原部署配置或状态。已有部署应保留原持久路径、卷名及账号盐，先备份再按需指定环境变量；不要删除卷或重置所有会话。

## 环境准备

使用 Linux Docker Engine 与 Compose v2；固定面板归档面向 Linux x86_64。三个栈统一由 `scripts/compose.sh` 选择，默认读取仓库根目录被忽略的 `.env`，也可用 `DEPLOY_ENV_FILE` 指定绝对路径。所有操作从仓库根目录执行。

```sh
cp config/deploy.env.example .env
cat config/bot.env.example >> .env
chmod 600 .env
```

编辑 `.env`：选择受保护的绝对状态路径，填写自己的模型 HTTPS 地址、模型 ID、QQ 账号及允许列表。示例地址 `api.example.com` 与 `your-chat-model` 仅是占位符。默认关闭记忆、面板和空间发布，按需要显式开启。

首次仅创建缺失的机器人凭据与 NapCat 配置：

```sh
BOT_STATE_DIR=/srv/qqbot/bot python3 tools/provision/prepare.py
```

该工具不覆盖已存在凭据。将自己的模型 API key 写到 `BOT_STATE_DIR/secrets/deepseek_api_key`，权限设为 0600。不要将 key 写入源码或命令行参数。`archive_salt` 是账号稳定标识的一部分，部署后不得重新生成。

同时创建 `MEMORY_STATE_DIR/secrets/memory_token` 与 `PANEL_STATE_DIR/secrets/bridge_token`，分别使用至少32字节的随机值、权限0600；这些 secret 文件即使可选能力关闭，也需存在，因为 Compose 声明了挂载。父目录设为0700。可以在受保护目录中运行以下仅创建缺失文件的片段：

```sh
python3 - <<'PY'
import os, secrets
from pathlib import Path
for root, name in (("/srv/qqbot/memory", "memory_token"), ("/srv/qqbot/panel", "bridge_token")):
    folder = Path(root) / "secrets"
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    path = folder / name
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
PY
```

按实际目录调整以上路径。机器人与记忆容器使用 UID/GID 1000，给相应存档、模型和 secret 文件配置可访问的所有权，密钥仍保持0600；不要通过放宽到全员可读解决权限问题。NapCat 的 UID/GID 见清单环境变量。

初次运行需创建 Compose 清单声明的外部资源：

```sh
docker network create --internal qq_memory_link
docker network create --internal qq_yunzai_link
docker volume create qq_memory_outbox
docker volume create qq-memory-addon_graph_data
docker volume create qq-memory-addon_graph_logs
docker volume create qq-memory-addon_memory_state
```

已有资源时跳过相应创建。面板使用的六个卷名见 `deploy/panel.compose.yaml`，启用前逐一创建。名称保留为稳定运行标识，不含任何用户信息。

## 机器人

```sh
scripts/compose.sh bot config --quiet
scripts/compose.sh bot build bot
scripts/compose.sh bot up -d
```

通过本机或受保护隧道访问 NapCat WebUI 完成自己的账号登录。首次登录工具 `tools/provision/login.py` 默认使用 `127.0.0.1:6099`，密码只在当前进程使用。服务不会在导入或检查时自动登录。私有自定义提示词使用 `.env` 中的 `SYSTEM_PROMPT`；直接 Python 运行也支持指向库外文件的 `PERSONA_FILE`。提示词、对话和报告不可提交到仓库。

## 可选图记忆

复制 `config/memory.env.example` 到 `MEMORY_STATE_DIR/memory.env` 并设置私有权限，填写实际模型配置。在独立模型准备环境安装 `deploy/requirements.memory.txt` 与 CPU torch 2.8.0，再设置 `EMBEDDING_PATH` 运行 `tools/models/fetch_model.py`，设置 `MODEL_DIR` 运行 `tools/models/train.py`。训练仅使用源码内人为编写的合成数据。把验证后的 embedding、provenance、R-GCN 权重及 metadata 放到 `MEMORY_STATE_DIR/models`；不要提交这些文件。

```sh
scripts/compose.sh memory config --quiet
scripts/compose.sh memory build memory-service
scripts/compose.sh memory up -d
```

然后在 `.env` 启用 `MEMORY_ENABLED=1`，仅重建机器人容器。图端口不对主机发布，模型目录只读挂载。

## 可选公开面板

```sh
python3 tools/provision/fetch_panel.py
scripts/compose.sh panel build yunzai-bridge
scripts/compose.sh panel up -d
```

获取工具按 `bridge/yunzai/provenance.json` 下载固定归档并校验，源码留在被忽略的 `third_party/yunzai/vendor`，不修改上游代码。创建清单中的外部卷并启动 Redis 与桥接后，在 `.env` 启用 `YUNZAI_ENABLED=1`，仅更新机器人容器。上游资源和许可证见第三方说明。

## 检查与更新

```sh
scripts/verify.sh
```

图检查使用固定虚构 actor 并清理测试图，宜使用独立测试环境。实际模型联调要将私有机器人环境文件配置为 `BOT_ENV_FILE`；`scripts/smoke.sh boundary`、`search`、`conversation`、`memory-quality`、`addressing`、`continuity`、`voice`、`social` 都捕获回复，不发送给QQ联系人。面板联调另需 `SMOKE_PANEL_UID` 与 `SMOKE_PANEL_CHARACTER`；模型比较另需 `SMOKE_COMPARISON_MODEL`。这些环境变量只在本机使用。`smoke.sh` 与 `verify.sh` 读取当前进程环境；自定义路径和镜像时，应显式导出相应变量，不能只依赖 Compose 的 `.env`。公开版默认通用提示词，旧角色风格的实验结论不适用。

`social-read` 只读实际空间；`social-self` 会写入并删除仅自己可见的测试动态，属于单独的显式写入验证，不是常规发布检查。本次代码发布不执行这些线上操作。所有联调报告、截图与服务状态保存在被忽略的 `evidence/`。

更新前备份源码、私有配置和持久数据，验证后按清单 `up -d --no-deps 服务名`。不要使用 `down -v`、全体会话重置或重新生成账号盐。只检查容器健康不能证明模型可用。

`tools/data_cli.py` 用于人工审核和导出；不通过 QQ 或模型工具调用。只有本人允许且人工批准的成功普通聊天可导出，脚本不提交训练任务。空间功能默认关闭，启用时将 `SOCIAL_ADMINS` 配置在私有 `.env`，不能将真实账号写入公共文档。
