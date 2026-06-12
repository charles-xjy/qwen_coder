[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:OPENAI_API_KEY = "sk-dummy"
$env:OPENAI_BASE_URL = "http://10.129.107.145:8001/v1"
$env:MODEL_NAME = "Qwen_agent"
$env:PYTHONIOENCODING = "utf-8"
$env:PATH += ";C:\Users\charl\.bun\bin"
$env:ANTHROPIC_AUTH_TOKEN = "sk-dummy"
$env:CALLER_DIR = $PSScriptRoot

$server = Start-Process python -ArgumentList "-m","interfaces.server" -PassThru -NoNewWindow `
    -WorkingDirectory $PSScriptRoot `
    -RedirectStandardOutput "$PSScriptRoot\server.log" `
    -RedirectStandardError "$PSScriptRoot\server.err"
Write-Host "Python server started (PID $($server.Id))"
Start-Sleep -Seconds 3

Set-Location "$PSScriptRoot\frontend"
bun run .\src\entrypoints\cli.tsx $args

Write-Host "Stopping Python server..."
Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
Set-Location $PSScriptRoot
