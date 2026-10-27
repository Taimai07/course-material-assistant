# Windows 发布签名状态

当前 0.5.3 安装程序和应用 EXE 尚未使用受信任发布者的 Authenticode 签名。安装器语言本地化不会改变这个状态。

正式签名需要发布者申请代码签名证书或云签名服务、完成身份验证，并安全配置 CI 的签名授权。用户尚未配置或选择该服务；没有生成自签名证书、安装受信任根证书或更改 Windows 防护设置。

办理后应签署应用 EXE，再打包安装器；安装器及卸载器也应使用同一核验身份签名并加可信时间戳。签名后验证签名有效性和发布者，且不得再改动已签署文件。证书私钥和服务授权不得提交到 Git。

可信签名验证发布者与完整性，SmartScreen 还会评估信誉；新签名并不保证第一次下载不提示。[Microsoft 官方说明](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation)
