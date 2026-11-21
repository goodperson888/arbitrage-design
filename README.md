# Arbitrage Design

预测市场研究、数据采集、回测、纸上验证和实时辅助工具。策略代码及研究文档公开保存在本仓库；历史行情、运行账本和密钥保存在各机器本地。

## 项目入口

- [研究监控与纸上交易](arbitrage-research/README.md)：数据采集、历史回测、候选策略和监控页面。
- [实盘执行器](arbitrage-live/README.md)：独立的交易执行与风控程序。
- [实时辅助指标](arbitrage-realtime/README.md)：独立的实时指标网页。
- [总体设计](00-总设计文档.md)。

同目录的 `mexc-polymarket-arb` 是已有独立 Git 仓库的桌面项目，不包含在本仓库中。

## 代码与数据

Git 保存源代码、参数模板、测试和研究文档；`data/`、`runs/`、本地日志、虚拟环境和密钥文件不上传。已生成结果目录只保留 Markdown 报告，报告引用的本地数据和图表需要在原数据机器上查看或重新生成。

克隆或更新代码不会自动复制已有采集数据。迁移监控时，先正常停止原机器上的写入进程，再单独备份、传输数据与账本，并确认新机器从相同数据目录继续运行。

## Windows 与打包状态

每次推送 `main`，GitHub Actions 的 **Windows portable package** 自动在 Windows 上测试、构建并公开发布带 Python、Node 和运行依赖的便携 ZIP。

[直接下载最新版 Windows 完整包](https://github.com/goodperson888/arbitrage-design/releases/latest/download/ArbitrageDesign-Windows-x64.zip)（无需登录 GitHub）。历史版本和校验文件见 [Releases](https://github.com/goodperson888/arbitrage-design/releases)。解压程序包后只需两个入口：

1. `开启.cmd`：自动检查环境，首次提示数据目录和代理端口（默认 `7890`，输入 `0` 直连），保存后开启纸上监控、5/15分钟采集和 Gate，并打开页面。后续直接复用设置。
2. `停止.cmd`：正常关闭全部服务，保留数据。

完整包自带环境；缺失或损坏时自动从官方源下载、校验并安装到程序目录，无需管理员权限。实时辅助网页、实盘管理页和单独服务控制放在高级命令中。配置和数据放在程序目录之外，更新代码不覆盖账本。启动采集不会自动开启真实交易。

完整下载、迁移、更新、密钥保存和单独控制步骤见 [Windows部署与数据迁移](docs/Windows部署与数据迁移.md)。GitHub 的 **Code → Download ZIP** 是源码，首次开启需要联网自动准备环境；完整便携包则无需这次下载。Mac 虚拟环境不能复制到 Windows 使用。

## 本地 Git 同步

在仓库根目录执行：

```bash
git status
git add .
git diff --cached --stat
git commit -m "Describe the change"
git push
```

提交前检查暂存文件，确保新增的本地账户配置、数据文件和密钥没有进入提交。GitHub 的源码 ZIP 仅包含已提交内容，不包含被排除的数据，也不等于 Windows 可运行程序。
