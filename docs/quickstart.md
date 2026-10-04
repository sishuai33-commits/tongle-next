# Tongle 快速使用指南

**新用户先用引导：**macOS 独立包双击 `开始使用.command`，Windows x64 预览包双击 `开始使用.cmd`，或运行 `./tongle guide`。只需说明目标、拖入资料、连接模型。以后从同一个入口继续。源码包运行 `python3 -m tongle.product guide`。

以下是需要自定义路径或接入工具时的高级命令。源码运行要求 Python 3.11 及以上，当前验证环境为 macOS + Python 3.12；Linux 尚未单独验证。Windows x64 试用包未做代码签名，云端验收范围见随包记录；普通 Windows 11 桌面首次使用体验仍待实际试用。

所有命令都在解压后的包根目录执行，不需要安装全局命令，也不依赖原始仓库路径。示例中的 `/absolute/example/runtime`、`/absolute/example/vault`、`/absolute/example/file.md`、`/absolute/example/backup.zip`、`/absolute/example/restored-runtime`、`/absolute/example/restored-vault` 都是占位符，请替换成你自己的绝对路径。

## 目录与范围

- `tongle/`：Python 包与命令入口。
- `adapters/`：主机适配器，含可选的 Pi 连接器。
- `scripts/`：显式收录的运行时与维护脚本。
- `tests/`：选定测试，含 `.py` 与 `.mjs`。
- `examples/evaluation/status-boundary.json`：评测夹具；只有该夹具是合成内容，文档描述的是真实机制。

归档不含运行时数据、密钥、私有运维文档、缓存或证据目录。包内不捆绑任何模型或密钥；需要知识编译时必须自行配置模型。

## 产品入口

这是无界面的本地预览版。入口已做本地离线验收，实际模型账户和各宿主连接须分别验证。未配置 provider 时模型禁用，知识编译不会执行，只有显式的登记与取回等基础操作可用。

先预览配置：

```
python -m tongle.product setup --home /absolute/example/runtime --vault /absolute/example/vault --project demo --style product
```

确认无误后写入：

```
python -m tongle.product setup --home /absolute/example/runtime --vault /absolute/example/vault --project demo --style product --write
```

说明：

- 子命令写在 `--home` 之前。
- `--style` 必须选择一种：本指南统一使用 `product`，即产品式。若场景不同可改用 `consulting`（咨询式）或 `research`（研究式），不必混用。
- `setup` 默认只预览；确认后加 `--write` 才写入。
- 未提供 provider 时模型为禁用状态，无法进行知识编译；请先完成下面的可选模型配置。

登记一个已知文件：

```
python -m tongle.product add --home /absolute/example/runtime --path /absolute/example/file.md --project demo
```

取回上下文：

```
python -m tongle.product context --home /absolute/example/runtime --project demo --text "说明文本" --session s1
```

查看状态：

```
python -m tongle.product status --home /absolute/example/runtime
```

执行一次编译：

```
python -m tongle.product once --home /absolute/example/runtime
```

启动后台编译：

```
python -m tongle.product start --home /absolute/example/runtime
```

停止后台编译：

```
python -m tongle.product stop --home /absolute/example/runtime
```

`once` 与 `start` 需要已配置的模型才能进行知识编译；未配置时不会编译。本指南不描述具体的拒绝行为。

备份运行时数据到指定文件：

```
python -m tongle.product backup --home /absolute/example/runtime --to /absolute/example/backup.zip
```

从备份恢复到全新的独立运行时与知识库：

```
python -m tongle.product restore --home /absolute/example/restored-runtime --from /absolute/example/backup.zip --vault /absolute/example/restored-vault
```

打印连接片段（Pi）：

```
python -m tongle.product connect --home /absolute/example/runtime --host pi
```

打印连接片段（Codex）：

```
python -m tongle.product connect --home /absolute/example/runtime --host codex
```

`connect` 只在标准输出打印对应主机的连接片段，不修改任何全局配置。Pi 是可选连接器，不是运行时前置条件；直连 provider 的产品用法不依赖 Pi。`restore` 恢复到全新的独立目录，默认模型禁用。

如果打包时带有 `scripts/product_entry.py`，同一组产品子命令也可以从该入口执行，命令拼写与上面一致。

## 可选：配置模型

只有需要知识编译时才配置模型。在一条命令里给出完整的 chat URL、模型 ID，并用 `--key-env` 指向已存在的环境变量名；不要写入密钥值。

```
python -m tongle.product setup --home /absolute/example/model-runtime --vault /absolute/example/model-vault --project demo --style product --provider deepseek --endpoint FULL_CHAT_URL --model MODEL_ID --key-env MY_MODEL_KEY --write
```

占位符说明：

- `FULL_CHAT_URL`：provider 的完整 chat URL。
- `MODEL_ID`：要使用的模型 ID。
- `MY_MODEL_KEY`：环境中已存在的密钥变量名，不是密钥值本身。

该命令使用新的运行时路径，不覆盖前面的示例运行时。

## 源码与测试校验

在解压后的包根目录执行：

```
python -m unittest discover -s tests -p test_release_package.py -v
```

这会校验源码包的选择与搬迁行为。

## 合成回归说明

`scripts/daily.py` 需要已写入的 active.json，在首次运行前并不适用。`examples/evaluation/status-boundary.json` 是合成评测夹具；文档描述的是真实机制，并非所有内容都是合成的。

## 独立运行包

macOS Apple Silicon 包已带运行环境，在包目录把以上 `python -m tongle.product` 换成 `./tongle`。Windows x64 试用包在包目录把以上 `python -m tongle.product` 换成 `.\tongle.exe`。源代码和宿主连接仍是可选项。选择普通实体目录；当前路径检查拒绝用户目录中的符号链接，macOS 临时路径如需使用应先取 `/private/tmp` 等物理路径。恢复后模型保持关闭，外部文件登记、项目默认值与模型配置需重新核对；不得把恢复成功理解成连接自动恢复。

## 引导的模型连接

默认 DeepSeek 配置依据[官方 Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion/)，使用 `deepseek-flash`；也可选择自定义兼容服务并填写地址及模型名称。接口连接与使用效果是不同验收项，仍需使用者自己的账户验证。引导不会读取其他工具的密钥；密钥只留在本次进程环境，不持久保存。

## 首次打开与实验功能

本版未签名。请从本项目 GitHub Releases 下载，并核对 SHA-256。macOS 如阻止打开，可在系统设置 → 隐私与安全性中允许此次打开，不要关闭全局安全保护。Windows 可能触发 SmartScreen；不愿运行未签名包时可使用源码。Windows Server 自动测试不代表 Windows 11 桌面首次使用已验证。

增量主题综合与有界学习已包含在包中，默认关闭；分别按 [主题综合](topic-synthesis.md)、[学习说明](learning.md) 配置项目范围。主引导不会自动开启这两个实验功能。

完整来源账本与版本状态存于本地；启用模型后相关内容会发送到你配置的服务。知识已更新不保证模型一定正确使用，详见[验证范围](release-validation.md)。
