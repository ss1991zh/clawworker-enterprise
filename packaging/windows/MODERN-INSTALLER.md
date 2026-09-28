# 现代安装器（用户端与管理端）

输出文件以 `-modern.exe` 结尾，产品版本仍为 1.7.0。旧安装包不覆盖。

- 无标题栏整页展示：蓝色波浪渐变约 4 秒循环，由左上向右下流动；预加载背景帧并合成绘制，不再使用圆球。原品牌 Logo 居中，下方目录与主按钮，蓝色无边框进度条显示真实文件进度。依赖配置阶段的蓝条仅从左向右循环，不往返。
- 完成页使用透明底启动选项，与真实启动状态联动；移除首次使用提示。依赖配置仍显示不定进度，不伪造耗时或百分比。
- 背景及说明文字可拖拽移动窗口，操作控件保留点击行为。安装前关闭不确认，安装中禁止关闭；原生取消按钮保留可见属性但位于视区外，以满足 Inno 的 CanFocus 退出检查。
- 复制文件阶段显示真实文件进度；依赖配置阶段使用不定进度条和实际阶段提示，不伪造总百分比。
- 配置脚本退出码必须为零才允许显示成功并启动应用。
- 配置失败提供重试，日志位于安装目录 `packaging/windows/install-client.log` 或 `install-admin.log`。
- 保留原角色 AppId，升级沿用已有目录，旧共享 Clawworker 根目录迁移至角色子目录。
- 保留会话、用户配置、私钥与字典；不捆绑用户密钥，不自动开启自启动。
- 仍包含离线 Python、角色 wheels、用户端 WebView2/HE 库、管理端数据库驱动。

构建：运行 `build_installers.ps1`。可通过 `CLAWWORKER_BUILD_PY` 指定用于依赖检查的 Python。
构建 Python 需先安装 `requirements-artwork.txt`；背景帧由脚本重新生成，不上传 Git。
构建会验证离线依赖并生成两端安装包及 SHA256 校验文件。

实现参考：[Inno Setup 进度事件](https://jrsoftware.org/ishelp/topic_scriptevents.htm)、
[子进程输出与退出码](https://jrsoftware.org/ishelp/topic_isxfunc_execandlogoutput.htm)。

发布验证边界：编译、离线依赖解析、单元测试和界面预览不替代干净 Windows 虚拟机上的安装、升级、卸载测试。
正式对外发布前还应完成低配置机器与管理员/普通用户两种权限场景验收，并配置企业代码签名。
