#!/usr/bin/env bash
# データベース（Docker Compose の db サービス）のパスワードを新しいランダムな値に変更する。
#
# 使い方（ai-stock-intelligence ディレクトリで）:  ./scripts/rotate-db-password.sh
#
# 処理：
#   1. 新しいパスワードを生成し、.env の POSTGRES_PASSWORD を書き換える（旧ファイルは残さない）
#   2. db コンテナを新しい設定で起動し直す
#   3. コンテナ内のローカル接続で、データベース利用者のパスワードを変更する
#      （PostgreSQL はデータ領域の初回作成時にだけ POSTGRES_PASSWORD を使うため、この手順が必要）
#   4. 新しいパスワードでの接続が成功し、古いパスワードでの接続が失敗することを確認する
#   5. api / worker を起動し直す（DATABASE_URL は docker-compose.yml が .env から組み立てる）
#
# パスワードの値は画面にもログにも出さない。
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo ".env がありません（.env.example をコピーしてください）" >&2; exit 1; }
umask 077

# 1. 新しいパスワード（英数字とハイフン・アンダースコアのみ。URLにそのまま入れられる）
NEW_PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
OLD_PW="$(grep -E '^POSTGRES_PASSWORD=' .env | head -1 | cut -d= -f2-)"
TMP="$(mktemp .env.tmp.XXXXXX)"
NEW_PW="$NEW_PW" python3 - .env "$TMP" <<'PY'
import os, sys
src, dst = sys.argv[1], sys.argv[2]
lines = open(src, encoding="utf-8").read().splitlines()
out, done = [], False
for l in lines:
    if l.startswith("POSTGRES_PASSWORD="):
        out.append("POSTGRES_PASSWORD=" + os.environ["NEW_PW"]); done = True
    else:
        out.append(l)
if not done:
    out.append("POSTGRES_PASSWORD=" + os.environ["NEW_PW"])
open(dst, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
chmod 600 "$TMP"
mv "$TMP" .env
echo "[1/5] .env の POSTGRES_PASSWORD を更新しました"

# 2. db を新しい設定で起動し直す
docker compose up -d --no-build db >/dev/null
for i in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q db)")" = "healthy" ] && break
  sleep 1
done
echo "[2/5] db を起動しました"

# 3. コンテナ内のローカル接続（パスワード不要）で利用者のパスワードを変更する。
#    SQL は標準入力で渡し、コマンドライン引数やログに値を残さない
NEW_PW="$NEW_PW" python3 -c 'import os; pw=os.environ["NEW_PW"]; print("ALTER ROLE CURRENT_USER WITH PASSWORD \x27" + pw.replace("\x27","\x27\x27") + "\x27;")' \
  | docker compose exec -T db sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null
echo "[3/5] データベース利用者のパスワードを変更しました"

# 4. パスワード認証が必要な経路で確認する。
#    注意：公式イメージは 127.0.0.1 からの接続を trust（パスワード不要）にしているため、
#    コンテナのネットワーク上のアドレス（scram-sha-256 で認証される）に接続する。
#    コンテナ内の環境変数 POSTGRES_PASSWORD は新しい値になっている
docker compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$(hostname -i | cut -d" " -f1)" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select 1"' \
  | grep -q '^1$' && echo "[4/5] 新しいパスワードで接続できました" || { echo "新しいパスワードで接続できません" >&2; exit 1; }
if [ -n "$OLD_PW" ] && [ "$OLD_PW" != "$NEW_PW" ]; then
  if OLD_PW="$OLD_PW" docker compose exec -T -e OLD_PW db sh -c 'PGPASSWORD="$OLD_PW" psql -h "$(hostname -i | cut -d" " -f1)" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select 1"' >/dev/null 2>&1; then
    echo "警告：古いパスワードでも接続できます" >&2; exit 1
  fi
  echo "      古いパスワードでは接続できないことを確認しました"
fi

# 5. api / worker を新しい DATABASE_URL で起動し直す（起動中のものだけ）
for svc in api worker; do
  if [ -n "$(docker compose ps -q "$svc" 2>/dev/null)" ]; then
    docker compose up -d --no-build --force-recreate "$svc" >/dev/null && echo "[5/5] $svc を起動し直しました"
  fi
done
echo "完了"
