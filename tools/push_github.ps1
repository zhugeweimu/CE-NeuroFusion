# 一键把 agent-neurofusion 推到 GitHub（需要先有 token）
# 用法（PowerShell）：
#   $env:GH_TOKEN='ghp_你的token'
#   .\push_github.ps1 -Repo agent-neurofusion
#
# token 权限只需要 repo（classic）或 Contents+Administration（fine-grained）
# 本脚本会：建仓库 → 加 remote → 推 main → 传两个 release 包

param(
  [string]$Repo = "agent-neurofusion",
  [string]$Owner = "",
  [string]$Desc = "CE: 让小模型（7B-14B）在 8K 上下文里干完本该 32K 才能干完的活（AGPL-3.0）",
  [switch]$Private
)

$ErrorActionPreference = "Stop"
$token = $env:GH_TOKEN
if (-not $token) { $token = $env:GITHUB_TOKEN }
if (-not $token) { Write-Host "缺少 token：先设置 `$env:GH_TOKEN" -ForegroundColor Red; exit 1 }

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $repoRoot

$hdr = @{ Authorization = "Bearer $token"; Accept = "application/vnd.github+json" }

# 1) 我是谁
if (-not $Owner) {
  $me = Invoke-RestMethod -Uri "https://api.github.com/user" -Headers $hdr
  $Owner = $me.login
}
Write-Host "账号: $Owner  仓库: $Repo"

# 2) 建仓库（已存在就跳过）
$body = @{ name = $Repo; description = $Desc; private = [bool]$Private; has_issues = $true } |
        ConvertTo-Json
try {
  $r = Invoke-RestMethod -Method Post -Uri "https://api.github.com/user/repos" -Headers $hdr `
        -Body ([Text.Encoding]::UTF8.GetBytes($body)) -ContentType "application/json"
  Write-Host "已创建: $($r.html_url)" -ForegroundColor Green
} catch {
  Write-Host "创建失败或已存在，继续尝试推送…" -ForegroundColor Yellow
}

# 3) remote + push
$url = "https://x-access-token:$token@github.com/$Owner/$Repo.git"
git remote remove origin 2>$null
git remote add origin $url
git -c credential.helper= push -u origin main --force

# 4) 传 release 包
if (Test-Path "dist") {
  $tag = "v1.0.0"
  $relBody = @{ tag_name = $tag; name = "v1.0.0"; body = "CE 单包与完整包（均为 AGPL-3.0）"; draft = $false } |
             ConvertTo-Json
  try {
    $rel = Invoke-RestMethod -Method Post -Uri "https://api.github.com/repos/$Owner/$Repo/releases" `
             -Headers $hdr -Body ([Text.Encoding]::UTF8.GetBytes($relBody)) -ContentType "application/json"
    foreach ($f in Get-ChildItem "dist" -File | Where-Object { $_.Name -like "*.zip" }) {
      $up = "https://uploads.github.com/repos/$Owner/$Repo/releases/$($rel.id)/assets?name=$($f.Name)"
      Invoke-RestMethod -Method Post -Uri $up -Headers $hdr `
        -Body ([IO.File]::ReadAllBytes($f.FullName)) -ContentType "application/zip" | Out-Null
      Write-Host "已上传 $($f.Name)" -ForegroundColor Green
    }
    Write-Host "Release: $($rel.html_url)" -ForegroundColor Green
  } catch {
    Write-Host "Release 上传失败（仓库已推上去，可稍后手动传包）: $_" -ForegroundColor Yellow
  }
}

# 5) 清掉带 token 的 remote，避免留在配置里
git remote set-url origin "https://github.com/$Owner/$Repo.git"
Write-Host "完成：https://github.com/$Owner/$Repo" -ForegroundColor Green
