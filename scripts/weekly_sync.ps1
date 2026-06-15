<#
.SYNOPSIS
  每周教学计划自动同步脚本 — classWeekChildPlan
.DESCRIPTION
  Loop Engineer 模式的自动化实践。
  检查环境 → 启动服务 → 扫描 PDF → 调用 DeepSeek API → 归档

  经验沉淀记录（来自 2026-06-15 实战）：
    v1: 发现 WindowsApps 下的 python.exe 是 0 字节占位符，不是真 Python
    v2: 改用 $env:LOCALAPPDATA\Programs\Python\Python312\python.exe
    v3: PS5.1 没有 Invoke-WebRequest -Form 参数，改用 curl.exe
    v4: exec_shell 后台进程跨回合被清理，跨会话运行需独立进程

.PARAMETER Port
  后端服务端口，默认 8000
.PARAMETER InboxDir
  PDF 收件箱目录，默认 .whale/inbox
.PARAMETER OutputDir
  计划输出目录，默认 .whale/output/weekly_plans
#>

param(
    [int]    $Port       = 8000,
    [string] $InboxDir   = "$PSScriptRoot/../.whale/inbox",
    [string] $OutputDir  = "$PSScriptRoot/../.whale/output/weekly_plans"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path "$PSScriptRoot/.."

Write-Host "╔══════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║    classWeekChildPlan — 每周计划自动同步     ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# ── 经验：Python 不在 PATH 中（WindowsApps 占位符），需要手动定位 ──
$pythonCandidates = @(
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
    "C:\Program Files\Python312\python.exe",
    "C:\Python312\python.exe"
)
$py = $null
foreach ($candidate in $pythonCandidates) {
    if (Test-Path $candidate) { $py = $candidate; break }
}
if (-not $py) {
    Write-Host "  ❌ 未找到 Python 安装" -ForegroundColor Red
    Write-Host "  请通过 winget install Python.Python.3.12 安装" -ForegroundColor Yellow
    exit 1
}

# ── Step 1: 环境检查 ──────────────────────────────────────────────────────
Write-Host "▸ Step 1/4 — 环境检查" -ForegroundColor Yellow
& $py --version 2>&1 | ForEach-Object { Write-Host "  $_" }

# 检查依赖
Write-Host "  检查依赖..." -NoNewline
$depsOk = & $py -m pip list 2>&1 | Select-String -Pattern "fastapi|openai|uvicorn|PyPDF2"
if ($depsOk.Count -ge 4) { Write-Host " ✅" -ForegroundColor Green }
else {
    Write-Host " ⚠️ 安装依赖..." -ForegroundColor Yellow
    & $py -m pip install -r "$ProjectRoot/backend/requirements.txt" --quiet 2>&1
    Write-Host "  完成"
}

# API Key
$envFile = "$ProjectRoot/.env"
if (-not (Test-Path $envFile)) {
    Copy-Item "$ProjectRoot/.env.example" $envFile -ErrorAction SilentlyContinue
}
$envContent = Get-Content $envFile -ErrorAction SilentlyContinue
if ($envContent -notmatch "DEEPSEEK_API_KEY=.+") {
    Write-Host "  ❌ DEEPSEEK_API_KEY 未配置" -ForegroundColor Red
    exit 1
}
Write-Host "  ✅ 环境就绪" -ForegroundColor Green

# ── Step 2: 创建目录 ──────────────────────────────────────────────────────
Write-Host "▸ Step 2/4 — 准备目录" -ForegroundColor Yellow
@($InboxDir, $OutputDir, "$InboxDir/_done") | ForEach-Object {
    if (-not (Test-Path $_)) {
        New-Item -ItemType Directory -Path $_ -Force | Out-Null
        Write-Host "  📁 创建: $_"
    }
}
Write-Host "  ✅ 目录就绪" -ForegroundColor Green

# ── Step 3: 启动后端 ──────────────────────────────────────────────────────
Write-Host "▸ Step 3/4 — 启动后端服务" -ForegroundColor Yellow
$healthUrl = "http://localhost:$Port/api/health"
$serviceRunning = $false

try {
    $resp = Invoke-WebRequest -Uri $healthUrl -TimeoutSec 5 -UseBasicParsing
    if ($resp.StatusCode -eq 200) { $serviceRunning = $true }
} catch {}

if (-not $serviceRunning) {
    $timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    # 经验：Start-Process 不允许 stdout/stderr 指向同一文件（PowerShell 限制）
    $outLog = "$ProjectRoot/.whale/output/server_${timestamp}_out.log"
    $errLog = "$ProjectRoot/.whale/output/server_${timestamp}_err.log"
    # 经验：uvicorn 是可执行脚本不在 PATH 中，需用 python -m uvicorn
    $proc = Start-Process -NoNewWindow -PassThru -FilePath $py -ArgumentList @(
        "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "$Port"
    ) -WorkingDirectory "$ProjectRoot/backend" -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    Write-Host "  🚀 服务已启动 (PID: $($proc.Id))"

    # 等待就绪
    $ready = $false
    for ($i = 0; $i -lt 12; $i++) {
        Start-Sleep -Seconds 2
        try {
            $r = Invoke-WebRequest -Uri $healthUrl -TimeoutSec 3 -UseBasicParsing
            if ($r.StatusCode -eq 200) { $ready = $true; break }
        } catch {}
    }
    if (-not $ready) {
        Write-Host "  ⚠️  服务启动超时" -ForegroundColor Yellow
        Get-Content $errLog -Tail 5 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "    $_" }
    }
} else {
    Write-Host "  ✅ 服务已在运行" -ForegroundColor Green
}

# ── Step 4: 处理 PDF ──────────────────────────────────────────────────────
Write-Host "▸ Step 4/4 — PDF 同步处理" -ForegroundColor Yellow
$pdfFiles = Get-ChildItem -Path $InboxDir -Filter "*.pdf"
$count = $pdfFiles.Count

if ($count -eq 0) {
    Write-Host "  📭 收件箱为空，无需处理" -ForegroundColor Yellow
    Write-Host "  💡 将每周计划 PDF 放入: $InboxDir"
    exit 0
}

Write-Host "  发现 $count 个待处理文件" -ForegroundColor Green
$success = 0

foreach ($pdf in $pdfFiles) {
    Write-Host "  ── $($pdf.Name) ──" -ForegroundColor Magenta

    # 经验：PowerShell 5.1 没有 -Form 参数，改用 curl.exe（Windows 内置）
    $response = curl.exe -s -X POST "http://localhost:$Port/api/generate-plan" `
        -F "file=@$($pdf.FullName)" --max-time 180 2>&1

    if ($LASTEXITCODE -eq 0) {
        $data = $response | ConvertFrom-Json
        $plan = $data.plan
        $timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
        $out = "$OutputDir/plan_$($pdf.BaseName)_$timestamp.md"
        $plan | Out-File -FilePath $out -Encoding UTF8
        Write-Host "  ✅ 已保存: $out" -ForegroundColor Green
        # 经验：处理完必须移入 _done，避免下次重复处理
        Move-Item $pdf.FullName "$InboxDir/_done/$($pdf.Name)" -Force
        $success++
        Write-Host "  📋 预览: $(($plan -split "`n")[0])" -ForegroundColor Gray
    } else {
        Write-Host "  ❌ curl 失败 (exit: $LASTEXITCODE)" -ForegroundColor Red
        # VERIFY: 上传失败时检查服务是否还在
        try {
            $h = Invoke-WebRequest $healthUrl -TimeoutSec 3 -UseBasicParsing
            Write-Host "  但后端服务正常，可能是超时。重试请手动执行 curl 命令。" -ForegroundColor Yellow
        } catch {
            Write-Host "  ❌ 后端服务也异常" -ForegroundColor Red
        }
    }
}

# ── 报告 ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "╔══════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║  同步完成: $success / $count 成功"
Write-Host "║  归档: $OutputDir"
Write-Host "║  收件箱: $InboxDir"
Write-Host "║  累积计划: $( (Get-ChildItem $OutputDir -Filter '*.md').Count ) 份"
Write-Host "╚══════════════════════════════════════════════╝" -ForegroundColor Cyan
