# Windows PowerShell 5.1 compatible; UTF-8 BOM preserves Chinese in source ZIPs.
param([Parameter(Position=0, ValueFromRemainingArguments=$true)][string[]]$ManagerArgs)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$runtime = Join-Path $root 'runtime'
$python = Join-Path $runtime 'python.exe'
$checker = Join-Path $PSScriptRoot 'runtime_setup.py'
$settings = $env:ARB_SETTINGS_FILE
if (-not $settings) { $settings = Join-Path $env:LOCALAPPDATA 'ArbitrageDesign\launcher.json' }
$setupLock = $null
$stage = $null
$result = 1

function Read-DownloadProxy {
    while ($true) {
        $value = Read-Host '自动安装需要联网。本机 HTTP 代理端口 [7890]，0=不使用代理'
        if (-not $value) { $value = '7890' }
        if ($value -eq '0' -or $value -eq 'none') { return '' }
        if ($value -match '^\d{1,5}$' -and [int]$value -ge 1 -and [int]$value -le 65535) {
            return "http://127.0.0.1:$value"
        }
        Write-Host '请输入 1 到 65535 的端口，或输入 0 直连。'
    }
}

function Test-Runtime {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $python
    $info.Arguments = '-X utf8 "' + $checker + '" check "' + $runtime + '"'
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        $null = $process.Start()
        $null = $process.StandardOutput.ReadToEnd()
        $null = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return $process.ExitCode -eq 0
    } finally { $process.Dispose() }
}

function Download-Verified($spec, [string]$target) {
    $arguments = @{ Uri=$spec.url; OutFile=$target; UseBasicParsing=$true; TimeoutSec=120 }
    if ($env:ARB_SETUP_PROXY) { $arguments.Proxy = $env:ARB_SETUP_PROXY }
    Invoke-WebRequest @arguments
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($target)
    try { $digest = [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $algorithm.Dispose() }
    if ($digest -ne $spec.sha256) {
        throw "下载校验失败：$target"
    }
}

function Expand-Checked([string]$archive, [string]$target) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($archive)
    try {
        foreach ($entry in $zip.Entries) {
            if ($entry.FullName -match '(^/|\\|:|(^|/)\.\.(/|$))' -or
                (($entry.ExternalAttributes -shr 16) -band 61440) -eq 40960) {
                throw 'Unsafe runtime archive entry'
            }
        }
    } finally { $zip.Dispose() }
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $target)
}

try {
    if (-not $ManagerArgs -or $ManagerArgs.Count -eq 0) { $ManagerArgs = @('launch') }
    $readOnlyOrStop = $ManagerArgs[0] -in @('stop', 'status', 'open')
    if ($ManagerArgs[0] -eq 'stop' -and -not (Test-Path -LiteralPath $settings)) {
        Write-Host '尚未配置启动，无需停止。'
        exit 0
    }
    if ((Test-Path -LiteralPath $runtime) -and
        ((Get-Item -LiteralPath $runtime -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Runtime must not be a junction or symbolic link.'
    }
    if (-not $readOnlyOrStop) {
        # Serializes setup and startup. Stops remain available while setup runs.
        $setupLock = [IO.File]::Open((Join-Path $root '.runtime-setup.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
        $healthy = $false
        if (Test-Path -LiteralPath $python) {
            $healthy = Test-Runtime
        }
        if (-not $healthy) {
            Write-Host '正在自动准备 Python、Node 和依赖。需要联网，无需管理员权限，不修改系统环境。'
            if (Test-Path -LiteralPath $settings) {
                $env:ARB_SETUP_PROXY = [string](Get-Content -LiteralPath $settings -Raw -Encoding UTF8 | ConvertFrom-Json).proxy
            } elseif (-not (Test-Path Env:ARB_SETUP_PROXY)) {
                $env:ARB_SETUP_PROXY = Read-DownloadProxy
            }
            if ($env:ARB_SETUP_PROXY -and $env:ARB_SETUP_PROXY -notmatch '^https?://') {
                Write-Host '安装下载请使用 HTTP 代理端口，或选择直连。'
                $env:ARB_SETUP_PROXY = Read-DownloadProxy
            }
            $env:ARB_SETUP_PROXY_SET = '1'
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $spec = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime-spec.json') -Raw | ConvertFrom-Json
            $stage = Join-Path $root ('.runtime-setup-' + [Guid]::NewGuid().ToString('N'))
            $null = New-Item -ItemType Directory -Path $stage
            $stagedRuntime = Join-Path $stage 'runtime'
            Download-Verified $spec.python (Join-Path $stage 'python.zip')
            Expand-Checked (Join-Path $stage 'python.zip') $stagedRuntime
            Download-Verified $spec.pip (Join-Path $stage 'pip.zip')
            Expand-Checked (Join-Path $stage 'pip.zip') (Join-Path $stagedRuntime 'Lib\site-packages')
            Download-Verified $spec.node (Join-Path $stage 'node.zip')
            Expand-Checked (Join-Path $stage 'node.zip') (Join-Path $stage 'node')
            $nodeTarget = Join-Path $stagedRuntime 'node'
            $null = New-Item -ItemType Directory -Path $nodeTarget
            foreach ($name in @('node.exe', 'LICENSE')) {
                Move-Item -LiteralPath (Join-Path (Join-Path $stage 'node') ($spec.node.prefix + $name)) -Destination (Join-Path $nodeTarget $name)
            }
            $pth = "python312.zip`n.`nLib/site-packages`n../arbitrage-research/src`n../arbitrage-live/src`n../arbitrage-realtime/src`nimport site`n"
            [IO.File]::WriteAllText((Join-Path $stagedRuntime 'python312._pth'), $pth, (New-Object Text.UTF8Encoding($false)))
            $stagedPython = Join-Path $stagedRuntime 'python.exe'
            & $stagedPython -X utf8 -u $checker install $stagedRuntime $stage
            if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，已有运行环境和数据已保留。' }
            & $stagedPython -X utf8 $checker idle $stagedRuntime
            if ($LASTEXITCODE -ne 0) { throw '请先点击停止，再修复运行环境。' }
            if (Test-Path -LiteralPath $runtime) {
                $backup = Join-Path $root ('runtime-previous-' + [Guid]::NewGuid().ToString('N'))
                Move-Item -LiteralPath $runtime -Destination $backup
                Write-Host "旧运行环境已保留：$backup"
            }
            Move-Item -LiteralPath $stagedRuntime -Destination $runtime
            Write-Host '运行环境已就绪。下次开启将直接复用，无需重新下载。'
        }
    }
    if (-not (Test-Path -LiteralPath $python)) { throw '运行环境缺失，请先点击开启；停止操作不会安装软件。' }
    & $python -X utf8 -u (Join-Path $PSScriptRoot 'manager.py') @ManagerArgs
    $result = $LASTEXITCODE
} catch {
    Write-Host "操作失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host '请检查代理、网络和目录写入权限，再点击开启重试。已有数据已保留。'
} finally {
    if ($stage -and (Test-Path -LiteralPath $stage)) {
        # Only our freshly created staging directory; reject any reparse points.
        $item = Get-Item -LiteralPath $stage -Force
        if ($item.Parent.FullName -ne $root -or $item.Name -notmatch '^\.runtime-setup-[0-9a-f]{32}$') { throw 'Unexpected setup cleanup path' }
        $entries = @(Get-ChildItem -LiteralPath $stage -Force -Recurse)
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
            @($entries | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) { throw 'Refusing cleanup through a link' }
        Write-Host "正在清理 $($entries.Count) 个临时安装文件：$stage"
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction Stop
    }
    if ($setupLock) { $setupLock.Dispose() }
}
exit $result
