# 在本地 Codex 桌面版继续开发

课题资料助手本身运行在用户电脑上。这里迁移的是源码、开发环境和开发会话；GitHub 继续保存源码、安装包和版本清单。

## Windows：最少操作

1. 下载源码：https://github.com/Taimai07/course-material-assistant/archive/refs/heads/main.zip 。解压到固定目录，例如 `D:\Projects\course-material-assistant`。不要把课程资料下载目录当作源码目录。
2. 在 Codex 桌面版中，把解压后的项目文件夹添加为本地项目。启动本地会话，检查其工作目录是电脑上的路径，而非云端 `/workspace`。
3. 把下面这段话发给本地会话：

> 请阅读 AGENTS.md 和 LOCAL-DEVELOPMENT.md，接手课题资料助手。先检查这台 Windows 电脑上的项目目录、Python 和 Git，配置项目自己的虚拟环境。源码来自 ZIP 时，请保留已有文件，先核对 GitHub 最新 main，再建立可同步的本地 Git 工作副本；不要覆盖本地改动。使用独立的 .local-state 目录验证开发环境，不要改动我已安装助手的课程、配对和设置。测试环境能启动后，告诉我如何继续核对学校账号的实际附件下载。已经完成的 0.7.0 功能与验证记录见交接文档。

如果已安装 GitHub Desktop，也可直接克隆 `https://github.com/Taimai07/course-material-assistant.git`，然后在 Codex 桌面版打开克隆目录；这样一开始就保留了 Git 历史。

不需要安装 VS Code；Codex 桌面版能编辑文件和运行命令。Python、Git 等运行工具由本地会话先检查，缺少时再配置。以下技术步骤供本地会话执行。

## 给接手会话的当前状态

- 当前公开版本：**0.7.0**。
- 源码仓库：https://github.com/Taimai07/course-material-assistant 。这是独立项目，不应克隆或公开其他私有工作区。
- 安装包：https://github.com/Taimai07/course-material-assistant/releases/tag/v0.7.0 。没有旧版时首次安装，有旧版时覆盖更新。
- 发行源码提交：`46eab436689ddf96e996e96d704b3ffef0dc9f22`。
- 0.7.0 EXE SHA-256：`027d25a206a1de9b8b78959d3aa7d9c6c496a2c65a21cdb9b6fc40b94740f23e`。
- 发布工作流：https://github.com/Taimai07/course-material-assistant/actions/runs/40570490632 。77 项 Windows 测试、原生 GUI 导航和资料迁移、运行时 DPI 感知、中文／日文安装器、重复启动及运行中覆盖更新保留资料均通过。
- 已使用实际更新客户端读取公开清单、下载正式安装包并验证 SHA-256；当前版本不会提示同版本更新。
- Claude 做过代码和设计静态复核并检查收尾修复，最后一次更新渠道复核任务为 `90acb9f0-73ce-4f97-93ef-8d0c629cf966`。静态复核不等于真实学校账号验证。
- 官方更新使用 GitHub Releases 和 `updates/windows/stable.json`。公开网站旧方案因匿名访问 403 已弃用；无需配置那个网站或申请其他云服务。

## 已完成功能与下一步

已修复 Chrome 大标签页编号被 `tab_id` 上限错误拒绝的问题；已实现本机原生课程 → 主题 → 课题 → 附件导航、蓝色本地文件链接、多选导出、集中设置、下载前选择位置、校验后迁移、可调大文件询问阈值、网盘客户端目录同步及应用内软件更新。

**优先核对学校账号的实际附件下载。** 0.7.0 的合成测试和正式安装验证均通过，但云端没有访问实际学校账号，不能据此宣称真实 PDF、图片、课件均已下载成功。用户曾提供 0.6.0 的 `无效的批处理参数：tab_id` 错误。先确认运行的是哪个程序版本、扩展版本及完整失败原因，再决定是否修代码。

后续还有 macOS 安装版及 1.0 发布准备；macOS 尚未交付。开发工作尽量使用脚本，资料采集不应调用模型。不要为了新会话重新实现已完成的功能。

## 开发环境与现有安装并存

- 使用 Python 3.12、Tk 和 Chrome。创建项目自己的 `.venv`；安装 `server/requirements.txt`，GUI 截图验证另需 Pillow。
- 源码入口是 `desktop.py`。`Run-Assistant.vbs` 和安装快捷方式可能启动已安装的 EXE，不能据此认为最新源码运行了。
- 服务固定监听 `127.0.0.1:47821`。运行源码前，通过现有助手的“退出助手”退出程序；点窗口右上角关闭只会最小化。不要同时运行两个接收服务。
- 开发验证使用 `COURSE_BRIDGE_DATA=<源码目录>\.local-state`，并保证测试和程序使用同一设置。此目录已被 Git 忽略；使用它不会自动带入已安装助手的配对和课程。
- 正常安装版的控制目录默认为用户主目录下的 `.course-material-bridge`；若设置了 `COURSE_BRIDGE_DATA` 则以其为准。课程材料的位置由该控制目录中的 `storage-location.json` 指向。不要把控制目录或材料目录复制进源码仓库。
- 若要诊断现有安装的实际资料，先停止开发实例，辨明当前控制目录和材料位置，保留原文件。不要自动迁移、重置数据库、清理凭证或替换配对码。
- 在本地聊天中直接使用资料工具，需要让本地会话检测并配置该客户端的本机 MCP 接入，保留已有其他服务。`show-mcp-config.py` 可生成使用当前 Python 真实路径的配置。仅打开项目不会自动授予浏览器或课程访问能力。

## 验证与发行

核心回归：`python -m unittest discover -s tests -p "test_*.py" -v`。
原生界面验证：`python tests/gui_check.py`；该脚本使用临时合成资料。
Chromium 页面测试：`tests/agent_check.py`，需要 Playwright 和 Chromium，Chrome API 为模拟实现。

本地完成开发和测试后，可按现有 GitHub Actions 流程构建 Windows 安装包。普通源码同步不发布新版；main 分支带 `[release]` 的提交或手动运行该工作流会尝试正式发行。发布代码新版本时先提高版本号，不要替换同版本的稳定安装包。仅文档交接提交使用 `[skip ci]`，无需重新打包应用。

云端聊天不会自动携带全部历史到本地新会话；本文件只保留开发交接信息。公开仓库中不保存课程内容、配对码、登录凭证或原始聊天记录。
