# Tongle 快速开始

把压缩包完整解压到一个之后会保留的目录。Apple 芯片 Mac 打开 `Start Tongle.command`，Windows x64 打开 `Start Tongle.cmd`。请从项目的 GitHub Releases 下载对应系统的包，并对照同一版本的校验值。包自带 Python，基础功能可离线使用。

1. 看到目录提示后按回车使用默认目录，也可以粘贴一个新的空目录路径。
2. 选 **1 体验本地示例**。看到观星活动的集合地点和时间，表示 Tongle 已完成一次资料导入、检索和原文核对。示例单独存放。
3. 选 **2 添加资料**，粘贴你想使用的 Markdown、文本或 DOCX 文件的完整路径，每个文件最多 5 MB。选 **3 查找资料**，输入一段关键词，再查看检索到的原文。
4. 下次从同一个启动文件进入，继续选择原来的数据目录。资料修改后，可以在同一路径再次添加，Tongle 会保留来源的版本关系。

数据目录包含 `instance`（数据库、配置）和 `vault`（可阅读的 Markdown 知识库）。初次试用可以选择一份公开或自写的资料。第一次运行前把解压目录放到长期使用的位置。运行后的数据目录与这份程序绑定，后续版本迁移需要单独核验。

## 接入已经在用的 AI 工具

本地检索不用 AI 账户。如果想在 AI 对话中使用 Tongle，先确认自己的 Pi 或 Codex 能正常对话。AI 服务会处理你在对话中发送的内容，也可能收到这轮用到的资料片段。先用一条你愿意发送给该服务的资料验证。

- **Pi**。选 **4 接入已有 AI 工具**，同意打开已有的 Pi，再开启新对话。试问“用 tongle_memory 查找这份资料的原文，并告诉我它记录的时间”。核对回答是否真的来自你的资料。已有的模型登录仍由 Pi 管理。
- **Codex（Mac）**。以下命令示例从解压目录执行。把尖括号内的路径换成自己的 `instance` 路径和实际使用的 Codex 配置目录。第一条预览，核对配置位置后用第二条应用。按 Codex 的提示启用对应的 hooks，重新开对话验证实际捕获和检索。这个接入步骤需要已有 Codex，Windows 包的基础使用不依赖 Codex。

```sh
./runtime/bin/python3 scripts/tongle.py install --home "<数据目录>/instance" --host codex --config-dir "<Codex 配置目录>"
./runtime/bin/python3 scripts/tongle.py install --home "<数据目录>/instance" --host codex --config-dir "<Codex 配置目录>" --write
```

基础安装默认使用本地关键词检索。安装成功、资料读回和 AI 对话接入需要分别验证。本地示例展示的是资料读回。

## 常见情况

- **系统无法打开启动文件**。先确认包来自项目发行页且版本校验一致。仍无法打开时，请提供系统、芯片类型和报错文字。
- **找不到资料**。确认添加成功，并用资料中的专有名词再查一次。查找菜单展示原文开头，长文可从原始文件继续阅读。
- **忘了用过哪个目录**。首次运行时，程序会显示实际数据目录。默认在 Mac 的 `~/Library/Application Support/Tongle`，Windows 的 `%LOCALAPPDATA%\Tongle`。自定义目录要在下次打开时重新选择。
- **停止使用**。退出 Tongle 和通过它打开的 Pi。从原解压目录使用下方示例里的包内 Python 执行 `scripts/tongle.py disable --home <instance路径> --write`，停止后续维护并清理该实例安装的接入配置。正在运行的维护可能需要完成当前周期。移除程序前保留你要继续使用的 `instance`、`vault` 及源资料。

## 导出备份

在解压目录的终端中执行对应系统的命令，把示例路径换成自己的实例目录和一份尚不存在的备份文件。

```sh
# Mac
./runtime/bin/python3 -m tongle --home "/完整路径/instance" export --to "/完整路径/tongle-backup.zip"
```

```powershell
# Windows
.\runtime\python.exe -X utf8 -m tongle --home "C:\完整路径\instance" export --to "C:\完整路径\tongle-backup.zip"
```
