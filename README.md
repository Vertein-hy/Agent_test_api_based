# Agent_test_api_based

基于模型 API 的模块化 QQ 对话与记忆系统。通过 NapCat OneBot 接收消息，支持有预算的工具调用、独立公开面板、按账号和场景隔离的长期记忆，以及管理员明确指令下的空间动态任务。

这是脱敏的技术发布版：不含私人角色卡、原始提示词、实际 API 网关配置、密钥、QQ 账号、真实 UID 样例、聊天记录、记忆数据库、模型权重或生产运维证据。运行所需指令已重新编写为通用技术模板；默认提示词是中性示例，可在私有环境中自行配置。

## 技术组成

| 目录 | 职责 |
| --- | --- |
| `src/qqbot` | 消息准入、会话编排、SSE/JSON 模型边界、唯一发送路径、存档、偏好、记忆协调与空间任务 |
| `src/qqmemory` | 图记忆 API、证据抽取、时序更新、受众过滤、离线编码、冻结 R-GCN 排序与有限反馈调整 |
| `bridge/yunzai` | 自主维护的只读公开展柜桥接、协议、上游版本与依赖锁定 |
| `config`、`deploy` | 无凭据的配置示例、服务清单与镜像定义 |
| `scripts`、`tests` | 统一检查、部署清单选择、捕获式联调与虚构测试数据 |
| `tools` | 首次配置、人工审核导出、固定上游获取与离线合成训练 |

每类服务一个入口：`python -m qqbot`、`python -m qqbot.tools.server`、`python -m qqmemory.app`；面板镜像使用 `node addon/bridge.mjs`。

## 本地检查

需要 Python 3.11+。Node 协议检查使用 Node 24；内存与图服务另有依赖。

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python scripts/check.py bot --python-only
node --test tests/unit/test_protocol.mjs
.venv/bin/python scripts/audit_public.py
```

`scripts/verify.sh` 是容器检查入口，分别使用机器人、面板及记忆镜像。需要先构建镜像并启动用于测试的图服务。真实模型检查必须显式运行 `scripts/smoke.sh`；这些检查使用虚构账号、临时数据库及回复捕获，不向 QQ 联系人发送测试。

## 配置与部署

参见 [部署说明](docs/operations/runbook.md)。模型需支持聊天接口、结构化 tool calls、SSE 或 JSON，以及当前客户端使用的 thinking 参数；部署前确认服务商兼容性。环境变量名 `DEEPSEEK_*` 为客户端配置接口，并不包含任何凭据。

私聊直接提问；群聊使用 @、经校验的机器人消息引用或 `/ai`。`/help` 查看命令。保留记录同意、训练导出同意、语气学习开关及记忆撤销命令。真实聊天不会自动训练模型；训练导出需人工审核。

## 数据与权限边界

- 身份仅由校验后的 QQ 账号确定，显示名、引用、记忆和模型输出不能赋予权限。
- 同账号称呼与适用偏好可以延续，私聊资料不得进入其他群，群聊经历按场景过滤。
- 只有成功发送的合规非面板回答才进入长期记忆；失败发送不提交短会话。
- 工具清单来自可信安装，执行次数、轮数、参数、响应大小和超时有上限。
- UID 只接受真实会话的用户输入；图片只在当前发送动作使用，不进入模型、存档或记忆。
- 删除、暂停和修订遵守 generation、actor lock 与 tombstone；撤销同步失败时阻断召回和 intake。

详见 [架构说明](docs/architecture/overview.md)、[提示词与公开发布边界](docs/public-release.md)、[第三方组件说明](THIRD_PARTY_NOTICES.md)。

## 许可证

自主维护代码使用 [MIT](LICENSE)。上游项目、依赖、模型及游戏资源使用各自许可证，本仓库不重新授权这些内容。
