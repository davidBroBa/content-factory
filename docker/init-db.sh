#!/bin/bash
# Crea múltiples bases de datos en PostgreSQL
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
  CREATE DATABASE n8n;
  CREATE DATABASE factory;
  GRANT ALL PRIVILEGES ON DATABASE factory TO $POSTGRES_USER;
  GRANT ALL PRIVILEGES ON DATABASE n8n TO $POSTGRES_USER;
EOSQL

echo "Databases 'factory' and 'n8n' created successfully"
