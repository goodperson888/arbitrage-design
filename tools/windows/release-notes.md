Windows x64 完整便携包，内含 Python、Node.js 和运行依赖。无需登录 GitHub 即可下载下方的 `ArbitrageDesign-Windows-x64.zip`。

解压后双击 `开启.cmd`；首次填写数据目录和代理端口（默认 7890，输入 0 直连），保存后下次直接开启。双击 `停止.cmd` 正常关闭全部服务并保留数据。

完整包发布前已通过 Windows 回归测试、六个服务启动/关闭测试、重复启动保护、账本续写和缺少环境时的自动安装验证。`SHA256SUMS.txt` 可用于核对下载文件。

本版精简了离线分析专用依赖和测试素材，保留采集、页面、账本和 Parquet 读写依赖；两种安装方式均验证原生 Parquet 读写，采集格式与策略规则保持一致。离线回测继续在开发环境进行。

程序包不包含本地历史行情、交易账本或密钥；已有数据按部署文档另行迁移。GitHub 自动附加的 Source code 文件是源码，不是完整便携包。

固定最新版下载地址：
https://github.com/goodperson888/arbitrage-design/releases/latest/download/ArbitrageDesign-Windows-x64.zip
