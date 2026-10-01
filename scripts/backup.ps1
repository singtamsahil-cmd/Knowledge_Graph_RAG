# Neo4j volume backup (Windows PowerShell). Usage: .\scripts\backup.ps1
# Override volume with $env:VOLUME if your compose project name differs.
$Dir = if ($env:BACKUP_DIR) { $env:BACKUP_DIR } else { ".\backups" }
$Volume = if ($env:VOLUME) { $env:VOLUME } else { "graph_neo4j_data" }
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$Stamp = Get-Date -Format "yyyy-MM-dd-HHmm"
docker run --rm -v "${Volume}:/data" -v "${PWD}/${Dir}:/backups" alpine tar czf "/backups/neo4j-$Stamp.tgz" /data
Write-Output "Backup written to $Dir/neo4j-$Stamp.tgz (volume $Volume)"
