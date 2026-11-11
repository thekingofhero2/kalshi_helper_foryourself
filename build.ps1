# 一键打包：生成 dist\KalshiDesk.exe（Windows 单文件）
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

python -m pip install -r requirements.txt
python -m pip install "pyinstaller>=6.6"

python -m PyInstaller --noconfirm --clean KalshiDesk.spec

Write-Host ""
Write-Host "打包完成：$PSScriptRoot\dist\KalshiDesk.exe" -ForegroundColor Green
Write-Host "首次运行会在 exe 同级生成 data\ 目录，用于保存 PEM 私钥与登录会话。"
