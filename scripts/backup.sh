#!/usr/bin/env bash
# Neo4j volume backup. Usage: ./scripts/backup.sh [backup_dir]
# Volume defaults to <project>_neo4j_data; override with VOLUME=xxx ./scripts/backup.sh
set -euo pipefail
DIR="${1:-${BACKUP_DIR:-./backups}}"
VOLUME="${VOLUME:-graph_neo4j_data}"
mkdir -p "$DIR"
STAMP="$(date +%F-%H%M)"
docker run --rm -v "$VOLUME:/data" -v "$PWD/$DIR:/backups" alpine \
  tar czf "/backups/neo4j-$STAMP.tgz" /data
echo "Backup written to $DIR/neo4j-$STAMP.tgz (volume $VOLUME)"
