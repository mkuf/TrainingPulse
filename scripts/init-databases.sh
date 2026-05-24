#!/bin/bash
set -euo pipefail

# Primary database is created by the Postgres image from POSTGRES_DB.
for db in withings fddb_nutrition; do
  exists=$(psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -tAc \
    "SELECT 1 FROM pg_database WHERE datname = '${db}'")
  if [ "$exists" != "1" ]; then
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -c "CREATE DATABASE ${db}"
  fi
done
