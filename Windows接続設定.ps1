# 検証用TCPポートの転送と受信規則を追加する。管理者PowerShellで実行する。
param(
    [Parameter(Mandatory=$true)][System.Net.IPAddress]$WindowsAddress,
    [Parameter(Mandatory=$true)][System.Net.IPAddress]$WslAddress,
    [ValidateSet('Public','Private','Domain')][string]$NetworkProfile = 'Public'
)
$ErrorActionPreference = 'Stop'
$labPort = 15432
$labRule = 'Codex-TimescaleDB-Lab-15432'
$labResultPath = Join-Path $PSScriptRoot '検証結果.json'
$outcome = [ordered]@{ 成功 = $false; 確認日時 = (Get-Date -Format o) }
try {
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw '管理者権限が必要です。'
    }
    if (-not (Test-Path -LiteralPath $labResultPath)) {
        throw 'DB側の検証結果がないため停止しました。'
    }
    if (-not (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -eq $WindowsAddress.ToString() })) {
        throw '指定されたWindowsのIPは、このPCにありません。'
    }
    if (Get-NetFirewallRule -Name $labRule -ErrorAction SilentlyContinue) {
        throw '同名のファイアウォール規則があります。既存設定を上書きせず停止しました。'
    }
    $proxyText = netsh interface portproxy show v4tov4 | Out-String
    if ($proxyText -match "(?m)^\s*\S+\s+$labPort\s+") {
        throw '同じ入口ポートの転送設定があります。既存設定を上書きせず停止しました。'
    }
    if (Get-NetTCPConnection -State Listen -LocalPort $labPort -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -in @($WindowsAddress.ToString(), '0.0.0.0', '::') }) {
        throw 'Windowsの入口ポートが使用中です。'
    }
    $direct = [Net.Sockets.TcpClient]::new()
    try {
        $direct.ConnectAsync($WslAddress, $labPort).Wait(5000) | Out-Null
        if (-not $direct.Connected) { throw 'WSL側の公開ポートに接続できません。' }
    } finally { $direct.Dispose() }
    if ((Get-Service iphlpsvc).Status -ne 'Running') { Start-Service iphlpsvc }
    netsh interface portproxy add v4tov4 "listenaddress=$WindowsAddress" "listenport=$labPort" "connectaddress=$WslAddress" "connectport=$labPort"
    if ($LASTEXITCODE -ne 0) { throw 'ポート転送の追加が失敗しました。' }
    New-NetFirewallRule -Name $labRule -DisplayName 'TimescaleDB検証 TCP15432 同一サブネット' -Direction Inbound -Action Allow -Protocol TCP -LocalAddress $WindowsAddress.ToString() -LocalPort $labPort -RemoteAddress LocalSubnet -Profile $NetworkProfile | Out-Null
    $outcome.成功 = $true
    $outcome.Windows接続先 = "$($WindowsAddress):$labPort"
    $outcome.WSL転送先 = "$($WslAddress):$labPort"
    $outcome.受信規則 = $labRule
    $outcome.プロファイル = $NetworkProfile
    $outcome.接続元範囲 = 'LocalSubnet'
} catch {
    $outcome.エラー = $_.Exception.Message
} finally {
    if (Test-Path -LiteralPath $labResultPath) {
        $labResult = Get-Content -LiteralPath $labResultPath -Raw -Encoding utf8 | ConvertFrom-Json -AsHashtable
        $labResult['Windows設定'] = $outcome
        $labResult | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $labResultPath -Encoding utf8
    }
}
if (-not $outcome.成功) { Write-Error $outcome.エラー; exit 1 }
Write-Output '検証用のポート転送と受信規則を追加しました。'
