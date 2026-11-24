# Windows 便携版运行、更新与数据迁移

Mac 用于开发；Windows 用于长期监控和记录。便携包包含 Windows x64 Python、Node.js 和运行依赖，目标机器不需要安装 Python、Node、Git，也不需要 Codex 保持打开。

## 获取可运行包

每次推送 `main`，GitHub Actions 自动运行 **Windows portable package**：先运行 Windows 回归测试，再构建便携 ZIP，最后在 Windows 上测试六个服务的启动、页面访问、重复启动保护、正常关闭、gzip 完整性和账本续写。通过后才上传下载文件，并发布到公开的 GitHub Releases。

[直接下载最新版完整包](https://github.com/goodperson888/arbitrage-design/releases/latest/download/ArbitrageDesign-Windows-x64.zip)，无需登录 GitHub，可在其他电脑直接使用这个固定地址。下载后解压到固定目录，例如 `D:\Apps\ArbitrageDesign`。不要直接在 ZIP 预览里双击脚本。

[Releases 页面](https://github.com/goodperson888/arbitrage-design/releases)保留各次公开发布及 `SHA256SUMS.txt`。请选择 `ArbitrageDesign-Windows-x64.zip`，GitHub 自动生成的 Source code ZIP 只是源码。

GitHub 的绿色 **Code → Download ZIP** 下载的是源码，不包含便携运行时；也可以解压后点击 `开启.cmd`，首次会联网自动准备环境。推荐完整便携包，省去目标机器上的首次安装下载。Actions 的 Artifacts 仍作为备用下载，需要登录且保留 30 天；公开 Release 下载没有这项登录要求，也不受 Artifacts 的 30 天保留期限制。

包里的 `build-info.json` 记录对应代码提交、运行时版本及校验值；`dependency-lock.json` 记录本次包内的全部依赖版本及各 wheel 的 SHA-256。运行依赖安装后不需要联网下载，行情采集本身仍需网络连接。

便携包使用采集运行依赖：保留 PyArrow、数据压缩、盘口监控、账本与页面所需模块；不携带离线回测使用的 NumPy、DuckDB、Polars，也不携带项目测试和依赖测试素材、Arrow 编译头文件。依赖的运行数据、原生动态库、许可证和版本元数据保留。`runtime/dependency-layout.json` 记录精简文件数量和字节数；完整包和自动安装使用相同的精简规则。历史数据格式和策略规则不变，数据仍可同步回 Mac 回测。

如果需要在 Windows 开发环境运行离线分析，请单独创建 Python 虚拟环境，安装 `tools/windows/requirements-test.txt` 和相应项目；不要向便携包的 `runtime` 安装分析或开发依赖。

客户便携包按 `tools/windows/distribution.py` 中的发布清单生成：只复制三个项目的程序源码与页面资源、明确列出的采集入口、配置及 Windows 启动工具。开发设计文档、研究报告、离线分析脚本、测试和构建工具不随包发布。客户只看到独立编写的 `使用说明.md`；`application-manifest.json` 记录发布的项目文件清单。第三方运行依赖的许可证与必要资源继续保留。开发仓库中的原文档不删除，公开仓库的可见范围也不因精简客户包而改变。

## 第一次启动

根目录只保留两个操作入口：

1. 双击 `开启.cmd`。首次提示数据目录，回车使用默认目录，或填写 `D:\ArbData` 等程序目录以外的路径。代理只需输入本机端口，例如 `7890`、`10808`；默认 `7890`，输入 `0` 表示直连。也接受完整 HTTP/SOCKS5 URL。设置保存后，下次不再重复询问。
2. 自动检查依赖、时区和运行路径，启动纸上监控、5 分钟跨市场采集、15 分钟采集和 Gate 辅助监控，并自动打开网页（首选 `http://127.0.0.1:8765/`，端口被占用时自动选择空闲端口）。控制台同时显示实际地址、各项状态和日志路径。
3. 不采集时双击 `停止.cmd`，正常关闭全部服务并保留数据。

完整便携包不需要安装环境。如果运行环境缺失或检查未通过，开启脚本会自动下载带固定 SHA-256 的官方 Python、Node 和 pip，再安装项目依赖，仅写入程序自己的 `runtime` 目录，无需管理员权限，也不修改系统 Python。程序目录应放在当前用户可写的位置。源码包首次准备环境时先问下载代理端口；随后配置数据目录，复用刚填写的代理。安装下载需要 HTTP 代理端口或可用直连网络。

安装先在临时目录完成，检查通过才启用；下载或安装失败会显示原因，可修正网络后重试。已有服务运行时不替换其运行环境；修复前先点击停止。被替换的旧运行环境保留为 `runtime-previous-随机编号`，历史数据始终保留在外部数据目录。

开启前会检查本机代理端口是否可连接，不通时提示开启代理软件或重新填写端口；不会自行切成直连。若已有服务运行，先停止再改代理，避免新旧进程使用不同设置。端口能连通只代表本机代理监听正常，平台联网状态仍需在页面确认。

配置文件默认在 `%LOCALAPPDATA%\ArbitrageDesign\launcher.json`，升级程序后继续使用。后台任务独立于启动窗口运行，关闭 CMD 窗口或浏览器不会关闭采集；请使用停止脚本。Windows 休眠、关机、网络断开仍会中断采集，应在系统设置中关闭采集时段的自动休眠。当前不是开机自动启动服务，重启 Windows 后需要再次点击启动。

实时辅助网页 `http://127.0.0.1:8787/` 和实盘管理页面 `http://127.0.0.1:8766/` 使用下方高级命令单独启动。后者只启动管理页面，交易运行和模式仍在原页面控制；一键启动采集不会开启实盘。

Gate 保留原来的直连 WebSocket 方式；启动配置中的代理用于 Python 采集链路。若 Gate 连接失败，可单独停止 Gate，其余采集继续工作。

## 单独控制与查看日志

在程序目录的终端中使用：

```bat
tools\windows\Arbitrage.cmd start paper
tools\windows\Arbitrage.cmd start cross
tools\windows\Arbitrage.cmd start cross15
tools\windows\Arbitrage.cmd start gate
tools\windows\Arbitrage.cmd start realtime
tools\windows\Arbitrage.cmd start live
tools\windows\Arbitrage.cmd status
tools\windows\Arbitrage.cmd configure
tools\windows\Arbitrage.cmd stop gate
tools\windows\Arbitrage.cmd stop paper
tools\windows\Arbitrage.cmd stop all
```

`start all` 只启动四个研究采集服务；`stop all` 包括采集、辅助网页和实盘管理页面。重复点击启动会检查当前数据目录的进程锁，不会重复创建写入进程；状态判断不依赖可能被系统复用的 PID。停止通过请求文件通知服务正常收尾，不按进程名批量杀进程；等待超时会明确显示仍在收尾，不能据此当作已经停止。

日志保存在 `<数据目录>\launcher_logs`，每次启动独立文件。数据目录内部 `.control` 仅保存本机启动控制信息，不是历史研究数据；请不要手工删除正在运行服务的锁文件。

修改已保存的代理或数据目录时，先停止，再运行 `tools\windows\Arbitrage.cmd configure`。8765、8766、8787 是网页首选端口；被占用时，程序让系统分配空闲端口，不关闭占用者。启动器读取网页实际绑定的端口，自动打开对应地址；`status` 和 `open paper|live|realtime` 也使用实际地址。不要根据旧书签判断是否启动成功，换端口后以启动器显示为准。代理端口仍需填写代理软件真实监听的端口，不会自动改成别的端口。

## 时钟偏差与行情延迟

纸上监控、候选账户和5/15分钟跨市场采集使用进程内部的 Binance 服务器时钟。启动时查询公开 `/api/v3/time`，成功后每60秒重采样；校时网络客户端由服务持有，启动及后续刷新复用同一连接池，保持连接的客户端空闲上限为120秒。每个接口最多连续尝试3个样本，选择其中往返最快的有效样本；主接口没有有效样本才尝试备用接口。只采用往返不超过1秒且服务器时间格式有效的样本，按请求中点估计时间，并以单调时钟向前计时。整轮失败后等待5秒重试，失败不更新上次成功样本的时间，也不清除仍在有效期内的校时结果。校时不会修改 Windows 系统时间，也不把“刚收到的成交时间”当成当前时间，所以延迟到达的历史成交仍会被拒绝。180秒未取得可用样本时，候选账户暂停新开仓，已有持仓继续等待结算；原纸上策略也保留校时检查。行情真正超过2秒的保护不取消，校时误差范围内的轻微未来来源时间才作有限容忍。

候选页面分别显示校时后来源延迟、接收后经过时间、本机快/慢多少毫秒、校时请求往返耗时、上次成功校时距今多久及下一次校时或重试倒计时。前两项用来区分网络/缓冲积压与本机时钟偏差。最近一次失败而已有校时仍有效时，页面会同时说明失败原因和现有校时仍有效；没有成功记录或已过期时明确暂停新开仓。应检查代理、Binance连接和 Windows 的“设置 → 时间和语言 → 日期和时间 → 立即同步”；系统时间严重偏差还可能影响 TLS，程序不会自行修改管理员级系统设置。

校时与状态信息写入新数据和候选健康记录，已有历史数据不回写。单凭延迟截图不能确认是哪台电脑的时钟有误，应以该电脑新版页面的校时状态为准。实盘执行器和独立实时概率工具的时间逻辑不在本次纸上监控校时范围内。

看不到页面时，先查高级 `status` 命令和对应启动日志；页面能打开也不代表外部行情连接正常，还需确认状态为 live、数据时间持续更新。

## 从 Mac 迁移已有数据

先下载并检查 Windows 包，再做实际切换。部署代码不会自动停止 Mac 上现有服务。

1. 在 Mac 的 `arbitrage-research` 目录执行 `./arb-monitor stop all`，确认采集已经正常退出。若同机还开着实盘执行器或实时辅助网页，也先在各自页面/终端正常停止。
2. 备份 Mac 上整个 `arbitrage-research/data/`。将其**内容**复制到 Windows 配置的数据目录，例如 `D:\ArbData`，保持目录层级不变。不要只复制 `latest.json`。
3. 若迁移实盘历史，再把 `arbitrage-live/data/live_trading/` 复制到 `D:\ArbData\live_trading\`。若迁移实时辅助的稀疏记录，将原记录文件放在 `D:\ArbData\realtime\facts.ndjson`。
4. 核对文件数量和总字节数，重要的 `accounts.ndjson`、订单、成交、官方结算账本再核对 SHA-256。先保留 Mac 原始备份，确认 Windows 中账户笔数、累计收益和未结算仓位相符后继续观察。
5. Windows 点击启动，在页面检查行情更新时间、候选十二账户的原有统计和最新追加记录。不要同时让两台机器对同一份共享数据目录写入。

数据目录结构示例：

```text
D:\ArbData\
  live_monitor\                       5分钟原纸上监控与候选账户
    candidates\v1\accounts.ndjson     候选账本，需要保留
    candidates\v1\observations\       候选按秒观察
  cross_market_validation\            5分钟跨市场特征
  cross_market_validation_15m\        15分钟数据
  gate_monitor\                       Gate记录
  live_trading\                       实盘记录、页面设置
  realtime\facts.ndjson               实时辅助稀疏记录
  launcher_logs\                      Windows启动日志
  .control\                           Windows本机进程控制
```

Windows 使用相同的账本和数据格式，不重置策略统计、不改变策略指纹或开仓阈值。历史报告和离线分析仍可在 Mac 做；Windows 数据同步回 Mac 后，运行回测时选择实际数据路径。历史脚本若使用固定的 `data/` 输入路径，需要按原目录层级摆放数据或显式传入路径。

## 密钥与实盘设置

Mac 的钥匙串不会随数据文件迁移到 Windows。Windows 版本改用当前用户的 **Windows Credential Manager** 保存私钥及派生 CLOB 凭据，需要在 Windows 管理页面重新填写/识别账户；不把密钥写入 Git 或程序 ZIP。已有非敏感页面设置随 `live_trading` 数据目录迁移。

迁移已有实盘设置后，先检查方向、金额、模式和订单开关，再决定是否启动交易运行器。自动采集脚本不启动交易运行器，也不会替用户切换到实盘。

## 更新代码，保留数据

1. 使用旧版本 `停止.cmd`（更早版本为 `04-Stop-All.cmd`），确认六项服务均停止。
2. 下载新的成功构建包，解压到一个新目录。
3. 双击新目录里的 `开启.cmd`，自动检查并启动。启动器继续读取同一个 `%LOCALAPPDATA%` 配置和外部数据目录。
4. 确认账本接续后再处理旧程序目录；原数据目录保持独立。回退到旧代码前，先检查新旧版本的账本兼容性，不要用旧版本强行改写它不识别的策略指纹。

Windows 压缩工具仍提供逐文件校验和活跃写入锁保护。NTFS 上文件内容会在提交前 flush/fsync；Windows 不提供 POSIX 目录 fsync，同名目录同步操作因此跳过。采集盘建议使用本地 NTFS，不使用网络盘或云同步目录承载正在写入的账本。

## 开发与重新打包

Mac 开发并 `git push` 后由 Actions 自动打包。也可在已安装 Python 3.12 x64 的 Windows 开发环境中运行：

```bat
python tools\windows\build.py --output dist\manual-build
```

输出目录必须不存在，构建器不会清理或覆盖已有目录。构建只从 Git 跟踪的文件中选择发布清单允许的文件，缺少必需文件会中止；下载官方 Python/Node 运行时后验证固定 SHA-256，依赖只安装 Windows 二进制 wheel。最终包仍通过独立的 `tools/windows/smoke_test.py` 验证，并检查没有混入项目设计文档、研究报告或离线分析脚本。
