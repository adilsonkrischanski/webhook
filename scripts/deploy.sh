#!/bin/sh
# Executado no mesmo host/daemon Docker usado pelo proxy de produção.
set -eu

: "${APP_IMAGE:?}"
: "${IMAGE_TAG:?}"
: "${APP_CONTAINER:?}"
: "${DOCKER_NETWORK:?}"
: "${DATA_VOLUME:?}"
: "${ENV_FILE:?}"
: "${HOST_PORT:=8089}"
: "${CONTAINER_PORT:=8000}"
: "${HEALTH_ATTEMPTS:=40}"
: "${HEALTH_INTERVAL:=3}"

image="$APP_IMAGE:$IMAGE_TAG"
previous="$APP_CONTAINER-previous"
backup="/data/.deploy-backup.sqlite"
old_present=false
old_running=false
backup_ready=false
candidate_created=false
switched=false
committed=false

docker network inspect "$DOCKER_NETWORK" >/dev/null 2>&1 || docker network create "$DOCKER_NETWORK" >/dev/null
docker volume inspect "$DATA_VOLUME" >/dev/null 2>&1 || docker volume create "$DATA_VOLUME" >/dev/null

# Valida o ambiente sem imprimir segredos nem abrir o banco de produção.
docker run --rm --network none --env-file "$ENV_FILE" \
    -e DATABASE_URL=sqlite:///:memory: \
    "$image" python -c 'from app import create_app; create_app(); print("Configuração validada.")'

if docker container inspect "$previous" >/dev/null 2>&1; then
    echo "ERRO: $previous já existe. Verifique o deploy anterior antes de continuar."
    exit 1
fi

if docker container inspect "$APP_CONTAINER" >/dev/null 2>&1; then
    old_present=true
    old_running=$(docker inspect -f '{{.State.Running}}' "$APP_CONTAINER")
    mounted_volume=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$APP_CONTAINER")
    if [ "$mounted_volume" != "$DATA_VOLUME" ]; then
        echo "ERRO: container atual não usa o volume $DATA_VOLUME em /data."
        echo "Migre os dados existentes antes de trocar o container; ele foi preservado."
        exit 1
    fi
fi

restore() {
    result=$?
    trap - EXIT HUP INT TERM
    if [ "$committed" = true ]; then
        exit "$result"
    fi
    if [ "$candidate_created" = true ]; then
        docker logs --tail=100 "$APP_CONTAINER" 2>/dev/null || true
        if docker container inspect "$APP_CONTAINER" >/dev/null 2>&1 && ! docker rm -f "$APP_CONTAINER" >/dev/null; then
            echo "ERRO: não foi possível parar a nova versão; banco não será restaurado com ela em execução."
            exit 1
        fi
    fi
    if [ "$backup_ready" = true ]; then
        echo "Restaurando banco anterior ao deploy..."
        if ! docker run --rm --network none -v "$DATA_VOLUME:/data" "$image" python -c '
from pathlib import Path
import os
root = Path("/data")
for suffix in ("-wal", "-shm"):
    (root / ("webhooks.db" + suffix)).unlink(missing_ok=True)
backup = root / ".deploy-backup.sqlite"
if backup.exists():
    os.replace(backup, root / "webhooks.db")
else:
    (root / "webhooks.db").unlink(missing_ok=True)
'; then
            echo "ERRO: restauração do banco falhou. Versão anterior permanece parada; backup preservado em $backup."
            exit 1
        fi
    fi
    if [ "$switched" = true ]; then
        docker rename "$previous" "$APP_CONTAINER" || exit 1
    fi
    if [ "$old_present" = true ] && [ "$old_running" = true ]; then
        docker start "$APP_CONTAINER" >/dev/null || exit 1
        echo "Versão anterior reiniciada."
    fi
    # Uma falha de deploy nunca deve ser reportada como sucesso.
    if [ "$result" -eq 0 ]; then result=1; fi
    exit "$result"
}
trap restore EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM

if [ "$old_present" = true ]; then
    docker stop --time 30 "$APP_CONTAINER" >/dev/null
fi

# Backup consistente e offline, antes de executar qualquer migração nova.
docker run --rm --network none -v "$DATA_VOLUME:/data" "$image" python -c '
from pathlib import Path
import sqlite3
root = Path("/data")
backup = root / ".deploy-backup.sqlite"
if backup.exists():
    raise SystemExit("Backup de deploy pendente. Inspecione /data/.deploy-backup.sqlite antes de continuar.")
source = root / "webhooks.db"
if source.exists():
    with sqlite3.connect(str(source)) as src, sqlite3.connect(str(backup)) as dst:
        src.backup(dst)
print("Backup pré-migração preparado.")
'
backup_ready=true

if [ "$old_present" = true ]; then
    docker rename "$APP_CONTAINER" "$previous"
    switched=true
fi

# Marca antes do run para também remover um container criado que não conseguiu iniciar.
candidate_created=true
docker run -d \
    --name "$APP_CONTAINER" \
    --restart unless-stopped \
    --network "$DOCKER_NETWORK" \
    --env-file "$ENV_FILE" \
    -e DATABASE_URL=sqlite:////data/webhooks.db \
    -e PUBLIC_BASE_URL=https://webhook.krischanski.dev \
    -e COOKIE_SECURE=true \
    -e TRUST_PROXY=true \
    --mount "type=volume,src=$DATA_VOLUME,dst=/data" \
    --read-only --tmpfs /tmp \
    --cap-drop ALL --security-opt no-new-privileges:true \
    -p "127.0.0.1:$HOST_PORT:$CONTAINER_PORT" \
    "$image" >/dev/null

attempt=1
healthy=false
while [ "$attempt" -le "$HEALTH_ATTEMPTS" ]; do
    if [ "$(docker inspect -f '{{.State.Running}}' "$APP_CONTAINER")" != true ]; then
        echo "ERRO: nova versão não permaneceu em execução."
        exit 1
    fi
    # O healthcheck Docker verifica a resposta diretamente no novo container.
    # A chamada externa confirma também o mapeamento da porta no host.
    if [ "$(docker inspect -f '{{.State.Health.Status}}' "$APP_CONTAINER")" = healthy ] && \
        curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
            "http://127.0.0.1:$HOST_PORT/healthz" >/dev/null 2>&1; then
        healthy=true
        break
    fi
    sleep "$HEALTH_INTERVAL"
    attempt=$((attempt + 1))
done

if [ "$healthy" != true ]; then
    echo "ERRO: nova versão não passou no healthcheck."
    exit 1
fi

committed=true
trap - EXIT HUP INT TERM
# A versão já está saudável: falhas de limpeza não devem reverter o deploy.
if [ "$old_present" = true ]; then
    docker rm "$previous" >/dev/null || echo "AVISO: remova $previous antes do próximo deploy."
fi
docker run --rm --network none -v "$DATA_VOLUME:/data" "$image" \
    python -c 'from pathlib import Path; Path("/data/.deploy-backup.sqlite").unlink(missing_ok=True)' \
    || echo "AVISO: remova o backup de deploy pendente antes do próximo deploy."
echo "Deploy saudável: $image em 127.0.0.1:$HOST_PORT."
