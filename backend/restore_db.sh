#!/bin/bash
set -e

echo "Restoring Metabase application database from metabase_envdata.dump..."
pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-acl /docker-entrypoint-initdb.d/metabase_envdata.dump || true
echo "Database restoration completed."
