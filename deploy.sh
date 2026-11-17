#!/bin/bash
set -e

echo "Starting Podman machine (if not already running)..."
podman machine start 2>/dev/null || true

echo "Creating persistent volume and pod..."
podman volume create mysql_subscriber_data >/dev/null
podman pod exists subscriber-pod || podman pod create --name subscriber-pod -p 5050:5000 -p 3306:3306

echo "Launching MySQL container..."
podman rm -f subscriber-db 2>/dev/null || true
podman run -d \
  --name subscriber-db \
  --pod subscriber-pod \
  --env-file .env \
  -v mysql_subscriber_data:/var/lib/mysql \
  -v ./init.sql:/docker-entrypoint-initdb.d/init.sql:Z \
  mysql:8.4

echo "Waiting for MySQL to initialize..."
until podman exec subscriber-db mysqladmin ping -h 127.0.0.1 --silent; do
  sleep 2
done

echo "Building and launching Web UI container..."
podman build -t subscriber-manager .
podman rm -f subscriber-web 2>/dev/null || true
podman run -d \
  --name subscriber-web \
  --pod subscriber-pod \
  --env-file .env \
  subscriber-manager

echo "Done! Open http://localhost:5050 in your browser."
