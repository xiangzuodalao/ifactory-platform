#!/bin/sh
set -eu

read_secret() {
  secret_path="$1"
  if [ ! -f "$secret_path" ]; then
    echo "required runtime secret is unavailable" >&2
    exit 78
  fi
  value="$(tr -d '\r\n' < "$secret_path")"
  if [ -z "$value" ]; then
    echo "required runtime secret is empty" >&2
    exit 78
  fi
  printf '%s' "$value"
}

DB_PWD="$(read_secret /run/secrets/cmms_postgres_password)"
JWT_SECRET_KEY="$(read_secret /run/secrets/cmms_jwt_secret)"
MINIO_ACCESS_KEY="$(read_secret /run/secrets/minio_root_user)"
MINIO_SECRET_KEY="$(read_secret /run/secrets/minio_root_password)"
export DB_PWD JWT_SECRET_KEY MINIO_ACCESS_KEY MINIO_SECRET_KEY

exec java --add-opens=java.base/java.lang=ALL-UNNAMED -jar /app/my-spring-boot-app.jar
