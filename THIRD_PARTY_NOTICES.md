# 第三方组件

本仓库的 MIT 许可证仅适用于自主维护代码。第三方源码、游戏图像、模型、论文及下载包不包含在发布树中，也不被重新授权。

| 组件 | 固定版本或来源 | 用途 |
| --- | --- | --- |
| [Yunzai](https://github.com/TimeRainStarSky/Yunzai) | `79a79c3defd9111429cd1da2acd120f22e68aa29` | 面板运行时 |
| [miao-plugin](https://github.com/yoimiya-kokomi/miao-plugin) | `0771a0e827bc51dfb6c8df50d797447e1aae7ba7` | 公开展柜解析与渲染 |
| [Node.js](https://nodejs.org/) | 24.8.0，构建归档 SHA-256 见 provenance | 面板运行环境 |
| [Qwen3 Embedding](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) | 获取时保存来源、修订与校验清单 | 本地文本编码 |
| [NapCat](https://github.com/NapNeko/NapCatQQ) | 由部署者固定验证后的镜像 digest | OneBot 接入 |
| Neo4j、Redis、PyTorch、Transformers、aiohttp | 见清单与 requirements | 数据库及运行依赖 |

面板归档地址、commit 和 SHA-256 保存在 `bridge/yunzai/provenance.json`；解析后的依赖锁文件用于复现安装。获取工具保留上游许可证和源文件。使用或再分发组合镜像时，应遵循相应上游 LICENSE 及资源权利要求。

研究思路包括证据关联、时序撤销、受众过滤和关系图排序；本发布不附论文全文、第三方研究仓库副本或个人研究笔记。
