#!/usr/bin/env bash
# factory.sh — Script de gestión de la Content Factory
# Uso: ./factory.sh [comando]
# Comandos: start, stop, restart, status, logs, backup, test, reset-failed

set -euo pipefail

COMPOSE_FILE="docker/docker-compose.yml"
ENV_FILE="docker/.env"
BACKUP_DIR="/opt/factory/backups"
POSTGRES_CONTAINER="cf_postgres"
DB_USER="factory"
DB_NAME="factory"

# ── Colores ────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }
info() { echo -e "${BLUE}[i]${NC} $*"; }

# ── Verificar prerequisitos ───────────────────────────────────
check_deps() {
  command -v docker   >/dev/null 2>&1 || { err "Docker no encontrado"; exit 1; }
  command -v docker   >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 \
    || { err "Docker Compose v2 no encontrado"; exit 1; }
  [[ -f "$ENV_FILE" ]] || { err ".env no encontrado en $ENV_FILE — copiar de .env.example"; exit 1; }
}

dc() {
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

# ── COMANDOS ──────────────────────────────────────────────────

cmd_start() {
  log "Iniciando Content Factory..."
  
  # Crear directorios necesarios
  mkdir -p /opt/factory/{minio,logs,backups}
  
  # Infraestructura base primero
  info "Levantando infraestructura base..."
  dc up -d postgres redis minio
  
  info "Esperando health checks (30s)..."
  sleep 30
  
  # Verificar salud
  dc ps | grep -E "(healthy|running)" | wc -l | xargs -I{} info "{} servicios saludables"
  
  # Inicializar MinIO buckets si no existen
  dc up minio_init --no-deps 2>/dev/null || true
  
  # Levantar el resto
  info "Levantando workers y orquestador..."
  dc up -d n8n whisper tts_worker render_worker watchtower
  
  sleep 15
  cmd_status
}

cmd_stop() {
  warn "Deteniendo todos los servicios..."
  dc down
  log "Servicios detenidos"
}

cmd_restart() {
  local service="${1:-}"
  if [[ -n "$service" ]]; then
    info "Reiniciando: $service"
    dc restart "$service"
  else
    warn "Reiniciando todos los servicios..."
    dc restart
  fi
  log "Reinicio completado"
}

cmd_status() {
  echo ""
  info "=== Estado de la Content Factory ==="
  dc ps
  
  echo ""
  info "=== Health Checks ==="
  
  check_service() {
    local name="$1" url="$2"
    if curl -sf "$url" >/dev/null 2>&1; then
      log "$name: OK ($url)"
    else
      err "$name: NO RESPONDE ($url)"
    fi
  }
  
  check_service "TTS Worker"    "http://localhost:8081/health"
  check_service "Render Worker" "http://localhost:8080/health"
  check_service "MinIO"         "http://localhost:9000/minio/health/live"
  check_service "n8n"           "http://localhost:5678/healthz"
  
  echo ""
  info "=== Pipeline Stats (últimas 24h) ==="
  docker exec "$POSTGRES_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c \
    "SELECT topic_status, COUNT(*) FROM factory.v_pipeline_status GROUP BY topic_status;" \
    2>/dev/null || warn "No se pudo conectar a PostgreSQL"
  
  echo ""
  info "=== Errores recientes ==="
  docker exec "$POSTGRES_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c \
    "SELECT service, error_type, LEFT(message,80) as msg, created_at FROM factory.errors WHERE resolved = FALSE ORDER BY created_at DESC LIMIT 5;" \
    2>/dev/null || true
}

cmd_logs() {
  local service="${1:-}"
  if [[ -n "$service" ]]; then
    dc logs -f --tail=100 "$service"
  else
    dc logs -f --tail=50 tts_worker render_worker n8n
  fi
}

cmd_backup() {
  local timestamp
  timestamp=$(date +%Y%m%d_%H%M%S)
  local backup_file="$BACKUP_DIR/factory_${timestamp}.sql.gz"
  
  info "Creando backup: $backup_file"
  mkdir -p "$BACKUP_DIR"
  
  docker exec "$POSTGRES_CONTAINER" \
    pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$backup_file"
  
  local size
  size=$(du -sh "$backup_file" | cut -f1)
  log "Backup completado: $backup_file ($size)"
  
  # Retener solo los últimos 7 backups
  ls -t "$BACKUP_DIR"/factory_*.sql.gz | tail -n +8 | xargs -r rm
  info "Backups retenidos: $(ls "$BACKUP_DIR"/factory_*.sql.gz | wc -l)"
}

cmd_test() {
  info "=== Test de integración ==="
  
  echo ""
  info "Test 1: TTS Worker"
  RESULT=$(curl -sf -X POST http://localhost:8081/synthesize \
    -H "Content-Type: application/json" \
    -d '{"text":"Prueba de integración de la fábrica de contenido.","voice":"af_heart","script_id":"00000000-0000-0000-0000-000000000001","lang":"e"}' 2>&1 || true)
  
  if echo "$RESULT" | grep -q '"success":true'; then
    log "TTS Worker: PASSED"
    AUDIO_URL=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('storage_url',''))" 2>/dev/null || true)
    info "Audio URL: $AUDIO_URL"
  else
    err "TTS Worker: FAILED"
    echo "$RESULT" | head -5
  fi
  
  echo ""
  info "Test 2: Render Worker health"
  RESULT=$(curl -sf http://localhost:8080/health 2>&1 || true)
  if echo "$RESULT" | grep -q '"ffmpeg":true'; then
    log "Render Worker + FFmpeg: PASSED"
  else
    err "Render Worker: FAILED"
    echo "$RESULT"
  fi
  
  echo ""
  info "Test 3: PostgreSQL schema"
  TABLES=$(docker exec "$POSTGRES_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAc \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'factory';" 2>/dev/null || echo "0")
  
  if [[ "$TABLES" -ge 10 ]]; then
    log "PostgreSQL schema: PASSED ($TABLES tablas)"
  else
    err "PostgreSQL schema: FAILED (solo $TABLES tablas, esperadas ≥10)"
  fi
  
  echo ""
  info "Test 4: MinIO buckets"
  BUCKETS=$(docker exec cf_minio mc ls local 2>/dev/null | wc -l || echo "0")
  if [[ "$BUCKETS" -ge 4 ]]; then
    log "MinIO buckets: PASSED ($BUCKETS buckets)"
  else
    err "MinIO buckets: FAILED (solo $BUCKETS buckets)"
  fi
  
  echo ""
  info "=== Tests completados ==="
}

cmd_reset_failed() {
  warn "Reseteando tópicos fallidos a 'pending'..."
  docker exec "$POSTGRES_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c \
    "UPDATE factory.topics SET status = 'pending', updated_at = NOW() WHERE status = 'failed';"
  log "Tópicos fallidos reseteados"
}

cmd_rebuild_workers() {
  warn "Reconstruyendo imágenes de workers (puede tardar 5-15 min)..."
  dc build --no-cache tts_worker render_worker
  dc up -d tts_worker render_worker
  log "Workers reconstruidos"
}

cmd_disk_usage() {
  info "=== Uso de disco ==="
  
  echo ""
  info "Volúmenes Docker:"
  docker system df -v 2>/dev/null | grep -A20 "VOLUME NAME" | head -20
  
  echo ""
  info "Datos de MinIO:"
  du -sh /opt/factory/minio 2>/dev/null || warn "No se puede acceder a /opt/factory/minio"
  
  echo ""
  info "Backups:"
  ls -lh "$BACKUP_DIR"/*.sql.gz 2>/dev/null || info "Sin backups todavía"
}

cmd_help() {
  echo ""
  echo "🏭 Content Factory — Script de gestión"
  echo ""
  echo "Uso: $0 [comando] [opciones]"
  echo ""
  echo "Comandos disponibles:"
  echo "  start              Iniciar todos los servicios"
  echo "  stop               Detener todos los servicios"
  echo "  restart [service]  Reiniciar todos o un servicio específico"
  echo "  status             Ver estado, health checks y stats del pipeline"
  echo "  logs [service]     Ver logs (todos o de un servicio específico)"
  echo "  backup             Crear backup de PostgreSQL"
  echo "  test               Ejecutar tests de integración"
  echo "  reset-failed       Resetear tópicos fallidos a 'pending'"
  echo "  rebuild            Reconstruir imágenes de workers"
  echo "  disk               Ver uso de disco"
  echo "  help               Mostrar esta ayuda"
  echo ""
  echo "Servicios disponibles:"
  echo "  postgres, redis, minio, n8n, whisper, tts_worker, render_worker, watchtower"
  echo ""
}

# ── Main ──────────────────────────────────────────────────────
check_deps

case "${1:-help}" in
  start)         cmd_start ;;
  stop)          cmd_stop ;;
  restart)       cmd_restart "${2:-}" ;;
  status)        cmd_status ;;
  logs)          cmd_logs "${2:-}" ;;
  backup)        cmd_backup ;;
  test)          cmd_test ;;
  reset-failed)  cmd_reset_failed ;;
  rebuild)       cmd_rebuild_workers ;;
  disk)          cmd_disk_usage ;;
  help|--help|-h) cmd_help ;;
  *)
    err "Comando desconocido: $1"
    cmd_help
    exit 1
    ;;
esac
