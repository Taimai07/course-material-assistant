# 课题资料助手

一个独立、非官方的 Google Classroom / Google Drive 本地资料整理工具。
采集和整理由脚本执行，不需要 GPT 会员、模型 API 或 AI 额度。

## 使用

Windows 用户安装发布的更新安装器。首次打开时选择资料保存位置，再到「设置 → 浏览器与聊天」安装 Chrome 扩展并连接已登录学校账号的浏览器。

左侧选择课程，右侧逐级进入主题、课题和附件。已下载附件以蓝色文件名显示，可用本机程序打开。
可勾选多个课程／课题导出，手动交给任意 AI 分析，也可以不使用 AI。

- 应用启动时检查软件更新，发现新版先询问，确认后自动下载、校验并打开安装向导。
- 在设置中更改资料位置，迁移时校验文件；账号配对和扩展仍在原配置目录。
- 大文件使用可调的询问阈值，不将该数值当成下载上限。
- 可选 Google Drive、OneDrive、Dropbox 本机同步目录，由相应客户端负责上传。
- 所有偏好集中在设置窗口。课程自动同步和软件版本更新是两个独立选项。

应用下载页： https://course-material-updates-20261028.daringkiwi32.chatgpt.site

## 从源码运行

需要 Python 3.12、Tk 和 Chrome。PDF 文字提取使用可选 pypdf：

```sh
python -m pip install -r server/requirements.txt
python desktop.py
```

首次打开应用会引导选择位置。扩展源码在 extension/。源码可供其他平台研究使用；当前打包和覆盖更新验收以 Windows 为主，不声称已经提供 Mac 安装包。

运行不访问真实账号的回归测试：

```sh
python -m unittest discover -s tests -p "test_*.py" -v
python tests/gui_check.py
node packaging/update_service/check.mjs
```

tests/agent_check.py 使用合成页面和模拟 Chrome API，需要额外安装 Playwright 和 Chromium。
合成测试和代码复核不等于任何学校账号已完成真实端到端验收。

## 构建与更新服务

Windows 构建使用 PyInstaller 和 Inno Setup；.github/workflows/build.yml 提供不含发布凭据的源码构建流程。
packaging/collect_licenses.py 将实际运行时和依赖许可一起放入安装目录。

官方更新通道通过 HTTPS 固定发布源和 SHA-256 进行来源范围／下载完整性检查；这不等于受信任的 Windows 发布者签名。
更新服务源码在 packaging/update_service/，只存软件安装包和版本清单。发布接口验证 GitHub Actions OIDC 的签名、仓库、分支、工作流和有效期，不在客户端内嵌发布凭据。
自行分发修改版时应修改更新服务地址和发布身份配置，建立自己的发行通道。

## 资料与使用权限

- 本项目不包含课件、课程数据库、学生信息、登录 Cookie、配对码或私人聊天连接配置。
- 界面由本项目自行实现，不分发 Google 前端源码、标志或界面图片；Google Classroom / Drive 名称仅用于说明兼容的服务。
- 仅使用本人有权访问和下载的资料，并遵守学校、文件权利人及服务的使用限制。
- 能下载课件不代表可以公开传播课件；本项目的软件许可证不授予课程内容的版权。
- 学校 DOM、权限、分页及虚拟滚动可能造成缺失，应用会尽量报告失败和覆盖范围，不能保证整门课程必定读取完整。
- 提交问题前请脱敏，不要在公开 issue 上传学校课件、私有课程链接、个人信息或账号凭据。

## 许可

本项目原创代码采用 MIT，见 LICENSE。第三方组件和翻译文件保留各自许可及版权声明，见 THIRD_PARTY_NOTICES.md 和 packaging/languages/。
