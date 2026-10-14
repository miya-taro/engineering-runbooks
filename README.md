---
tags: [WSL, PostgreSQL, TimescaleDB, 検証]
date: 2026-10-14
status: active
source: "WSL内Dockerでのダミーデータ移行検証"
related: []
aliases: []
---

# WSL・TimescaleDBの構築と移行を確認する

PostgreSQLとTimescaleDBの版をそろえ、ダミーデータを移行して、Windows経由の接続まで確認するための手順。会社などでブラウザーから読み、必要なコマンドをコピーして使う。

これは検証環境を新規作成する手順。業務DBの接続情報や実データは含まない。

## 確認済みの範囲

2026年10月14日、WSL 2内のUbuntuとDockerで元のスクリプトを実行し、次を確認した。この掲載版では手順中の個人PCのパスとIPを変数へ置き換えている。

| 項目 | 結果 |
| --- | --- |
| DBの版 | 移行元・先ともPostgreSQL 16.11、TimescaleDB 2.25.2 |
| データ移行 | 機器5件・時系列1,440件・ハイパーテーブル1個。件数とハッシュ一致 |
| 制約 | 主キー・外部キーの違反が拒否されることを確認 |
| DB接続 | パスワード認証と読み書き成功 |
| コンテナ再起動 | データ保持・読み書き成功 |
| WindowsのLAN側IP経由 | 同一PC内からDB認証と読み書き成功 |
| 別の実端末 | 同じLANのスマホからDBまでTCP到達を確認 |

APTによる直接インストール、pgAdminの画面操作、Windows・WSL全体の再起動、別端末からのDB認証・SQL実行は未検証。Dockerイメージは再現用の固定版であり、最新の本番推奨版という意味ではない。

## 事前に用意するもの

- WindowsとWSL 2。以下ではディストリビューション名を `Ubuntu` とする。
- Ubuntu内で動くDocker EngineとPython 3。
- Windows側のPowerShell 7。`pwsh` で起動できる状態。
- 約1.5GB以上のコンテナ用メモリと、イメージ・ボリューム用の空き容量。
- このリポジトリの2本のスクリプトを同じWindowsフォルダに保存する。

[検証.py](検証.py) と [Windows接続設定.ps1](Windows接続設定.ps1) をダウンロードするか、GitHubの「Code → Download ZIP」で展開する。初期設定にはイメージ取得の通信と、Windowsの管理者権限が必要。

以降は各ブロックを順番に実行し、失敗したらその段階で止める。スクリプトは同名リソースを削除・上書きせず停止する。すでに作成済みなら「次回の再確認」へ進む。

## 1. WSLを起動し、スクリプトの場所を指定する

PowerShellから次を実行して、そのターミナルを開いたままにする。

```powershell
wsl -d Ubuntu
```

別のPowerShellを開く。先頭のパスをダウンロード先に変更する。`wslpath` はWindowsパスをWSL内のパスへ変換する。

```powershell
$labFolder = 'C:\作業フォルダ\engineering-runbooks'
$labLinuxFolder = (wsl -d Ubuntu --exec wslpath -u $labFolder).Trim()
$labScript = "$labLinuxFolder/検証.py"
```

## 2. 構築・ダミーデータ移行・照合

```powershell
wsl -d Ubuntu -u root --exec python3 $labScript setup
```

固定digestの公式イメージを取得し、専用ネットワーク・ボリューム・DB2台を作る。Custom形式でバックアップし、`timescaledb_pre_restore()` → `pg_restore` → `timescaledb_post_restore()` → `ANALYZE` の順に復元する。並列復元オプションは使わない。

成功時は「検証成功」と表示され、スクリプトと同じフォルダの `検証結果.json` に結果が保存される。移行先だけWSLのTCP15432で待ち受ける。

| リソース | 名前 |
| --- | --- |
| 移行元 | codex-tsdb-lab-20261014-source |
| 移行先 | codex-tsdb-lab-20261014-target |
| ネットワーク | codex-tsdb-lab-20261014-network |
| ボリューム | 各コンテナ名に `-data` を追加したもの |
| DB | lab |

既存リソースと競合したら自動削除しない。別名・別ポートで作る場合はPythonの `PREFIX` / `PORT` と、PowerShellの `$labPort` / `$labRule`、本文のコマンドも対応させる。

## 3. WindowsとWSLのIPを確認する

WindowsのPowerShellで実行する。

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceAlias,IPAddress,PrefixLength
Get-NetConnectionProfile | Select-Object InterfaceAlias,NetworkCategory
wsl -d Ubuntu --exec ip -4 addr show eth0
netsh interface portproxy show all
```

Windowsは接続に使うLAN側のIPv4、WSLは `eth0` のIPv4を選ぶ。Dockerの内部IPと混同しない。

## 4. Windowsの転送とファイアウォールを設定する

**PowerShell 7を管理者として開く。** 次の3つの値を実際の環境に置き換える。IPは説明用で、そのまま実行する値ではない。

```powershell
$labFolder = 'C:\作業フォルダ\engineering-runbooks'
$windowsIp = '192.0.2.10'
$wslIp = '192.0.2.20'
$networkProfile = 'Private'
& "$labFolder\Windows接続設定.ps1" -WindowsAddress $windowsIp -WslAddress $wslIp -NetworkProfile $networkProfile
```

`$networkProfile` は現在のネットワークに合わせ、`Public` / `Private` / `Domain` を選ぶ。確認結果が `DomainAuthenticated` の場合は `Domain` を渡す。

追加するのは、Windowsの指定IPのTCP15432 → WSLのTCP15432の転送と、その入口への受信許可。許可する接続元は `LocalSubnet`。別サブネットからの通信を試す場合は、その経路に合う受信範囲を別途設計する。

同名規則・同じ入口ポートが既にあれば停止する。既存設定の更新用ではない。途中で失敗した場合は、転送だけ追加されている可能性があるため、次のコマンドで状態を確認する。

```powershell
netsh interface portproxy show all
Get-NetFirewallRule -Name 'Codex-TimescaleDB-Lab-15432' | Select-Object Enabled,Direction,Action,Profile
Get-NetFirewallRule -Name 'Codex-TimescaleDB-Lab-15432' | Get-NetFirewallPortFilter
Get-NetFirewallRule -Name 'Codex-TimescaleDB-Lab-15432' | Get-NetFirewallAddressFilter
Test-NetConnection -ComputerName $windowsIp -Port 15432
```

`TcpTestSucceeded : True` を確認する。同一PC内の成功だけでは、別端末からの接続が通る証明にはならない。

## 5. Windows経由のDB認証・読み書きと再起動

同じPowerShell 7で実行する。`restart` が再起動するのは検証用の2コンテナだけで、保持確認用データを1件追加する。

```powershell
$labLinuxFolder = (wsl -d Ubuntu --exec wslpath -u $labFolder).Trim()
$labScript = "$labLinuxFolder/検証.py"
wsl -d Ubuntu -u root --exec python3 $labScript windows-route --host $windowsIp --port 15432
```

上の成功後に再起動を試す。

```powershell
wsl -d Ubuntu -u root --exec python3 $labScript restart
```

再起動の成功後、再度確認する。

```powershell
wsl -d Ubuntu -u root --exec python3 $labScript windows-route --host $windowsIp --port 15432
```

## 6. 別端末から確認する

同じLANの別のWindows PCで実行する。IPは接続先Windowsの実際のLAN側IPに置き換える。

```powershell
Test-NetConnection -ComputerName '192.0.2.10' -Port 15432
```

`True` はTCP到達の確認。DB認証・SQLまで試す場合はpsqlやpgAdminから接続する。接続先はWindowsのLAN側IP、ポート15432、DB名 `lab`、ユーザー `postgres`。

パスワードは構築時にランダム生成される。必要なときだけホストPCで確認し、共有資料やGitへ保存しない。

```powershell
wsl -d Ubuntu -u root --exec docker exec codex-tsdb-lab-20261014-target printenv POSTGRES_PASSWORD
```

スマホのブラウザーでDBポートを開くとWebページのエラーになる。エラー画面だけでは成功を判断できない。今回の実測では、Windowsの別端末IPからの接続記録とDB側の受信ログを照合してTCP到達を確認した。

## 次回の再確認

WSLを起動したままにし、手順1の変数を設定する。構築済み環境に `setup` を繰り返す必要はない。

```powershell
wsl -d Ubuntu -u root --exec docker start codex-tsdb-lab-20261014-source codex-tsdb-lab-20261014-target
```

DB起動後に実行する。

```powershell
wsl -d Ubuntu -u root --exec python3 $labScript verify
```

WSLのIPは変化することがある。接続が失敗したら手順3でIPと転送先を比較する。WindowsのLAN側IPやネットワーク種別が変わった場合も規則の見直しが必要。`unless-stopped` はDocker起動時のコンテナ再起動設定で、Windows起動時にWSLを起動する設定ではない。

## このリポジトリの更新

掲載対象はこの手順と2本のスクリプトに限定する。原本を更新したときは掲載版も見直す。Obsidian全体の自動同期は設定していない。

実行結果JSON・バックアップ・ログ・秘密情報はアップロードしない。`.gitignore` は補助であり、手順へ書き込んだ秘密情報までは検出しない。

## 公式資料

- [Microsoft：WSLのネットワーク](https://learn.microsoft.com/ja-jp/windows/wsl/networking)
- [PostgreSQL 16：pg_dump](https://www.postgresql.org/docs/16/app-pgdump.html)
- [TimescaleDB公式Dockerイメージ](https://github.com/timescale/timescaledb-docker)

