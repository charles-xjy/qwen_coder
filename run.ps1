[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:OPENAI_API_KEY = "sk-dummy"
$env:OPENAI_BASE_URL = "http://10.129.107.145:8001/v1"
$env:MODEL_NAME = "Qwen_agent"
$env:PYTHONIOENCODING = "utf-8"
$env:PATH += ";C:\Users\charl\.bun\bin"
$env:ANTHROPIC_AUTH_TOKEN = "sk-dummy"
$env:CALLER_DIR = $PSScriptRoot

# 经 cmd /c 在 OS 层把 stderr 并入 stdout（Start-Process 不允许两路重定向到同一文件）
$server = Start-Process cmd -ArgumentList '/c','python -m interfaces.server > server.log 2>&1' `
    -PassThru -NoNewWindow -WorkingDirectory $PSScriptRoot
Write-Host "Python server started (PID $($server.Id))"
Start-Sleep -Seconds 3

Set-Location "$PSScriptRoot\frontend"
bun run .\src\entrypoints\cli.tsx $args

Write-Host "Stopping Python server..."
# /T 连同子进程（cmd 下的 python）一起结束，避免留下孤儿进程
taskkill /PID $server.Id /T /F 2>$null | Out-Null
Set-Location $PSScriptRoot
