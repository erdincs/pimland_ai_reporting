#!/bin/bash
# deploy.sh — EC2 üzerinde production deployment
set -e

APP_DIR="/opt/pimland-reporting"
COMPOSE="docker compose -f docker-compose.prod.yml"

echo "=== Pimland AI Reporting — Deployment ==="

# 1. Bağımlılıkları kontrol et
if ! command -v docker &>/dev/null; then
  echo "Docker kuruluyor..."
  curl -fsSL https://get.docker.com | sh
  usermod -aG docker $USER
  systemctl enable docker
  systemctl start docker
fi

# 2. Uygulama dizinine git
cd $APP_DIR

# 3. Imajı build et
echo "Docker imajı build ediliyor..."
docker build -t pimland-api:latest .

# 4. Servisleri başlat
echo "Servisler başlatılıyor..."
$COMPOSE down --remove-orphans 2>/dev/null || true
$COMPOSE up -d

# 5. DB hazır olana kadar bekle
echo "PostgreSQL bekleniyor..."
until $COMPOSE exec db pg_isready -U pimland &>/dev/null; do sleep 2; done

# 6. DB kullanıcı ve yetkileri ayarla
echo "Veritabanı yapılandırılıyor..."
$COMPOSE exec db psql -U pimland -d pimland_reporting -c "
  DO \$\$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='pimland_ro') THEN
      CREATE USER pimland_ro WITH PASSWORD 'PimlandRo2026!';
    END IF;
  END \$\$;
  GRANT CONNECT ON DATABASE pimland_reporting TO pimland_ro;
  GRANT USAGE ON SCHEMA public TO pimland_ro;
  ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO pimland_ro;
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO pimland_ro;
" 2>/dev/null || true

# 7. Alembic migration
echo "Migration çalıştırılıyor..."
$COMPOSE exec api alembic upgrade head 2>/dev/null || true

# 8. Durum kontrolü
sleep 5
$COMPOSE ps

echo ""
echo "=== Deployment tamamlandı! ==="
PUBLIC_IP=$(curl -sf http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "IP alınamadı")
echo "Portal: http://$PUBLIC_IP/api/v1/portal"
echo ""
echo "Sync başlatmak için:"
echo "  curl -X POST http://$PUBLIC_IP/api/v1/connectors/incorta_ecommerce_sales/sync"
