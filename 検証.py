#!/usr/bin/env python3
"""専用Docker環境によるTimescaleDB移行検証。既存リソースの削除はしない。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
RESULT = ROOT / "検証結果.json"
IMAGE = "timescale/timescaledb@sha256:0af03ecf697825f6ddae76fd275d16bf46007bed6d00eb3d754779cb7db96fa6"
PREFIX = "codex-tsdb-lab-20261014"
SRC, DST = PREFIX + "-source", PREFIX + "-target"
NETWORK = PREFIX + "-network"
LABEL = "jp.local.codex.lab=timescaledb-20261014"
PORT = 15432

def run(args, data=None, env=None, check=True, timeout=180):
    result = subprocess.run(args, input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"処理失敗: {args[0]} {args[1] if len(args)>1 else ''}\n"
                           + result.stderr.decode(errors="replace")[-4000:])
    return result

def docker(*args, **kwargs):
    return run(["docker", *args], **kwargs)

def sql(container, statement, database="lab", check=True):
    return docker("exec", "-i", container, "psql", "-X", "-U", "postgres", "-d", database,
                  "-v", "ON_ERROR_STOP=1", "-A", "-t", data=statement.encode(), check=check)

def value(container, statement):
    return sql(container, statement).stdout.decode().strip()

def announce(message):
    print(message, flush=True)

def wait_ready(container):
    for attempt in range(60):
        process = docker("exec", container, "cat", "/proc/1/comm", check=False)
        if process.stdout.strip() != b"postgres":
            time.sleep(1)
            continue
        ready = docker("exec", container, "pg_isready", "-U", "postgres", check=False)
        if ready.returncode == 0:
            try:
                sql(container, "SELECT 1;", "postgres")
                return
            except RuntimeError:
                pass
        time.sleep(1)
    raise RuntimeError("DBが起動しませんでした: " + container)

def check_ownership():
    for name in (SRC, DST):
        info = json.loads(docker("inspect", name).stdout)[0]
        if info["Config"]["Labels"].get("jp.local.codex.lab") != "timescaledb-20261014":
            raise RuntimeError("検証用ラベルと一致しません: " + name)

def container_password(container):
    info = json.loads(docker("inspect", container).stdout)[0]
    return next(s.split("=", 1)[1] for s in info["Config"]["Env"]
                if s.startswith("POSTGRES_PASSWORD="))

def tcp_query(statement, host="127.0.0.1", port=5432):
    # パスワードはコマンド引数や結果ファイルに残さない。
    env = os.environ.copy()
    env["PGPASSWORD"] = container_password(DST)
    return docker("exec", "-i", "-e", "PGPASSWORD", DST,
                  "psql", "-X", "-h", host, "-p", str(port), "-U", "postgres", "-d", "lab",
                  "-v", "ON_ERROR_STOP=1", "-A", "-t", data=statement.encode(), env=env)

def setup():
    # 名前の再利用・既存ボリュームへの接続を拒否する。
    for kind, names in (("container", (SRC, DST)), ("volume", (SRC + "-data", DST + "-data")),
                        ("network", (NETWORK,))):
        for name in names:
            if docker(kind, "inspect", name, check=False).returncode == 0:
                raise RuntimeError("既存リソースがあるため新規作成を停止: " + name)
    with socket.socket() as probe:
        probe.bind(("0.0.0.0", PORT))
    announce("固定digestの公式イメージを取得しています。")
    pull = subprocess.run(["docker", "pull", IMAGE], timeout=900)
    if pull.returncode:
        raise RuntimeError("イメージ取得に失敗しました。")
    docker("network", "create", "--label", LABEL, NETWORK)
    for name in (SRC, DST):
        docker("volume", "create", "--label", LABEL, name + "-data")
        env = os.environ.copy()
        env["POSTGRES_PASSWORD"] = secrets.token_urlsafe(32)
        args = ["run", "-d", "--name", name, "--label", LABEL, "--restart", "unless-stopped",
                "--network", NETWORK, "--memory", "768m", "--cpus", "1",
                "-v", name + "-data:/var/lib/postgresql/data",
                "-e", "POSTGRES_PASSWORD", "-e", "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256",
                "-e", "TIMESCALEDB_TELEMETRY=off",
                "-e", "TS_TUNE_MEMORY=512MB", "-e", "TS_TUNE_NUM_CPUS=1"]
        if name == DST:
            args += ["-p", f"0.0.0.0:{PORT}:5432"]
        docker(*args, IMAGE, env=env)
        wait_ready(name)
        sql(name, "CREATE DATABASE lab TEMPLATE template0;", "postgres")
        sql(name, "CREATE EXTENSION timescaledb VERSION '2.25.2';")
    announce("同一版のDBを2台作成しました。テストデータを作ります。")
    sql(SRC, """
CREATE TABLE devices (id integer PRIMARY KEY, name text NOT NULL);
INSERT INTO devices SELECT i, '検証機器' || i FROM generate_series(1,5) AS i;
CREATE TABLE readings (
  time timestamptz NOT NULL, device_id integer NOT NULL REFERENCES devices(id),
  value numeric(10,2) NOT NULL, PRIMARY KEY(time, device_id));
SELECT create_hypertable('readings', by_range('time'));
INSERT INTO readings
SELECT '2026-10-01 00:00:00+00'::timestamptz + i * interval '5 minutes', d,
       ((i % 97) * 0.25 + d)::numeric(10,2)
FROM generate_series(0,287) i CROSS JOIN generate_series(1,5) d;
CREATE TABLE write_probe (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, note text NOT NULL);
""")
    docker("exec", SRC, "pg_dump", "-U", "postgres", "-d", "lab", "-Fc", "-f", "/tmp/lab.dump")
    backup = docker("exec", SRC, "cat", "/tmp/lab.dump").stdout
    sql(DST, "SELECT timescaledb_pre_restore();")
    restored = docker("exec", "-i", DST, "pg_restore", "-U", "postgres", "-d", "lab",
                      "--exit-on-error", data=backup, check=False)
    # 失敗時も復元モードを終了し、失敗内容は上書きせず報告する。
    finished = sql(DST, "SELECT timescaledb_post_restore();", check=False)
    if restored.returncode or finished.returncode:
        raise RuntimeError("復元失敗\n" + restored.stderr.decode(errors="replace")
                           + finished.stderr.decode(errors="replace"))
    sql(DST, "ANALYZE;")
    announce("復元が完了しました。内容を照合します。")
    return {"バックアップSHA256": hashlib.sha256(backup).hexdigest(),
            "バックアップバイト数": len(backup), "復元ログ": restored.stderr.decode(errors="replace")}

def verify():
    check_ownership()
    versions = {}
    signatures = {}
    for name in (SRC, DST):
        versions[name] = {"PostgreSQL": value(name, "SHOW server_version;"),
                          "TimescaleDB": value(name, "SELECT extversion FROM pg_extension WHERE extname='timescaledb';")}
        if not versions[name]["PostgreSQL"].startswith("16.") or versions[name]["TimescaleDB"] != "2.25.2":
            raise RuntimeError("想定バージョンと一致しません。")
        signatures[name] = {
            "機器件数": value(name, "SELECT count(*) FROM devices;"),
            "時系列件数": value(name, "SELECT count(*) FROM readings;"),
            "時系列ハッシュ": value(name, "SELECT md5(string_agg(time::text || ',' || device_id || ',' || value, E'\\n' ORDER BY time, device_id)) FROM readings;"),
            "機器ハッシュ": value(name, "SELECT md5(string_agg(id || ',' || name, E'\\n' ORDER BY id)) FROM devices;"),
            "ハイパーテーブル数": value(name, "SELECT count(*) FROM timescaledb_information.hypertables WHERE hypertable_schema='public';")}
    if signatures[SRC] != signatures[DST] or signatures[DST]["時系列件数"] != "1440" or signatures[DST]["ハイパーテーブル数"] != "1":
        raise RuntimeError("移行元と復元先のデータが一致しません。")
    duplicate = sql(DST, "BEGIN; INSERT INTO devices VALUES (1,'重複テスト'); ROLLBACK;", check=False)
    if duplicate.returncode == 0 or b"duplicate key" not in duplicate.stderr:
        raise RuntimeError("主キーの制約確認に失敗しました。")
    foreign = sql(DST, "BEGIN; INSERT INTO readings VALUES ('2026-12-01',999,1); ROLLBACK;", check=False)
    if foreign.returncode == 0 or b"foreign key" not in foreign.stderr:
        raise RuntimeError("外部キーの制約確認に失敗しました。")
    readwrite = tcp_query("BEGIN; INSERT INTO write_probe(note) VALUES ('TCP読み書き確認') RETURNING note; ROLLBACK;").stdout.decode()
    if "TCP読み書き確認" not in readwrite:
        raise RuntimeError("TCPでの読み書き確認に失敗しました。")
    return {"バージョン": versions, "照合結果": signatures,
            "主キー検証": "成功", "外部キー検証": "成功", "コンテナ内TCP認証と読み書き": "成功"}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["setup", "verify", "restart", "windows-route"])
    parser.add_argument("--host")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    previous = json.loads(RESULT.read_text()) if RESULT.exists() else {}
    if args.mode == "setup":
        previous.update(setup())
        previous.update(verify())
    elif args.mode == "verify":
        previous.update(verify())
    elif args.mode == "restart":
        check_ownership()
        docker("start", SRC, DST)
        wait_ready(SRC)
        wait_ready(DST)
        marker = "再起動保持確認-" + secrets.token_hex(8)
        sql(DST, f"INSERT INTO write_probe(note) VALUES ('{marker}');")
        docker("restart", SRC, DST)
        wait_ready(SRC)
        wait_ready(DST)
        if value(DST, f"SELECT count(*) FROM write_probe WHERE note='{marker}';") != "1":
            raise RuntimeError("再起動後のデータ保持確認に失敗しました。")
        previous.update(verify())
        previous["検証用コンテナ再起動後の保持"] = "成功"
    elif args.mode == "windows-route":
        check_ownership()
        if not args.host:
            raise RuntimeError("--host にWindows側の接続先を指定してください。")
        output = tcp_query("BEGIN; SELECT count(*) FROM readings; INSERT INTO write_probe(note) VALUES ('Windows経由読み書き') RETURNING note; ROLLBACK;", args.host, args.port).stdout.decode()
        if "1440" not in output or "Windows経由読み書き" not in output:
            raise RuntimeError("Windows経由でのデータ確認に失敗しました。")
        previous["同一PC内からWindowsのLANアドレス経由"] = "成功"
        previous["Windows接続先"] = f"{args.host}:{args.port}"
    previous.update({"確認日時UTC": datetime.now(timezone.utc).isoformat(), "固定イメージ": IMAGE,
                     "コンテナ": [SRC,DST], "WSL公開ポート": PORT})
    for key, default in {"別の実端末からの確認": "未実施", "APT直接導入": "未検証", "pgAdmin画面操作": "未検証"}.items():
        previous.setdefault(key, default)
    RESULT.write_text(json.dumps(previous, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    announce("検証成功。結果を検証結果.jsonへ保存しました。")
    print(json.dumps({"バージョン": previous.get("バージョン"), "再起動": previous.get("検証用コンテナ再起動後の保持"),
                      "Windows経由": previous.get("同一PC内からWindowsのLANアドレス経由")}, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    main()
