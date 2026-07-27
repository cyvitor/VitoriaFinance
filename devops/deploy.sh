#!/usr/bin/env sh

set -eu

ROOT_DIR=${VITORIAFINANCE_ROOT:-/opt/vitoriafinance}
APP_DIR="$ROOT_DIR/app"
CONFIG_DIR="$ROOT_DIR/config"
ENV_FILE="$CONFIG_DIR/vitoriafinance.env"
VENV_DIR="$ROOT_DIR/venv"
BACKUP_DIR="$ROOT_DIR/backups/banco"
LOG_DIR="$ROOT_DIR/logs"
DEPLOY_LOG_DIR="$LOG_DIR/deploy"
STATE_DIR="$ROOT_DIR/state"
RUN_DIR="$ROOT_DIR/run"
DEPLOY_LOCK="$RUN_DIR/deploy.lock"
DEPLOY_STATE="$STATE_DIR/deployed-version"
DEPLOY_HISTORY="$STATE_DIR/deploy-history.log"

SERVICE_USER=${VITORIAFINANCE_USER:-vitoriafinance}
SERVICE_GROUP=${VITORIAFINANCE_GROUP:-vitoriafinance}
WEB_SERVICE=vitoriafinance-web.service
BOT_SERVICE=vitoriafinance-bot.service
PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"
REPOSITORY_URL=${VITORIAFINANCE_REPOSITORY_URL:-https://github.com/cyvitor/VitoriaFinance.git}
HOMOLOGATION_BRANCH=${VITORIAFINANCE_HOMOLOGATION_BRANCH:-main}
HEALTH_TIMEOUT=${VITORIAFINANCE_HEALTH_TIMEOUT:-45}
BACKUP_RETENTION=${VITORIAFINANCE_BACKUP_RETENTION:-30}

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

info() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

die() {
    printf '[%s] Erro: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Uso: ./devops/deploy.sh [comando] [referencia]

  auto                Verifica o Git e publica quando houver versao elegivel
                      Este e o comando padrao quando nenhum argumento e informado
  check               Valida sistema, repositorio e instalacao
  install             Prepara uma VPS nova e cria os servicos systemd
  deploy [referencia] Atualiza a aplicacao
                       homologation: usa apenas main
                       production: exige uma tag, por exemplo v1.0.0
  version             Mostra a versao implantada e a versao do codigo
  status              Mostra o estado dos servicos
  logs [web|bot|deploy]
                      Acompanha logs dos servicos ou do ultimo deploy
EOF
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "Execute este comando com sudo."
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "Comando obrigatorio ausente: $1"
}

validate_runtime() {
    for command_name in git python3 curl systemctl flock gzip; do
        require_command "$command_name"
    done

    python3 -c '
import sys
if sys.version_info < (3, 12):
    raise SystemExit("Python 3.12 ou superior e obrigatorio.")
print(f"Python encontrado: {sys.version.split()[0]}")
'

    [ -d /run/systemd/system ] || die "systemd nao esta ativo nesta maquina."
}

validate_repository() {
    [ -d "$REPOSITORY_ROOT/.git" ] || die "Execute o script dentro de um clone Git do VitoriaFinance."
    origin=$(git -C "$REPOSITORY_ROOT" remote get-url origin)
    case "$origin" in
        "$REPOSITORY_URL"|git@github.com:cyvitor/VitoriaFinance.git)
            ;;
        *)
            die "Origin inesperado: $origin"
            ;;
    esac
}

create_service_user() {
    if id "$SERVICE_USER" >/dev/null 2>&1; then
        info "Usuario de servico ja existe: $SERVICE_USER"
        return
    fi

    info "Criando usuario de servico: $SERVICE_USER"
    useradd \
        --system \
        --home-dir "$ROOT_DIR" \
        --shell /usr/sbin/nologin \
        --user-group \
        "$SERVICE_USER"
}

prepare_directories() {
    mkdir -p \
        "$CONFIG_DIR" \
        "$BACKUP_DIR" \
        "$LOG_DIR/bot" \
        "$DEPLOY_LOG_DIR" \
        "$STATE_DIR" \
        "$RUN_DIR"

    chmod 750 "$ROOT_DIR" "$CONFIG_DIR" "$BACKUP_DIR" "$LOG_DIR" "$STATE_DIR" "$RUN_DIR"
    chown root:"$SERVICE_GROUP" "$ROOT_DIR" "$CONFIG_DIR" "$STATE_DIR" "$RUN_DIR"
    chmod 750 "$LOG_DIR/bot" "$DEPLOY_LOG_DIR"
    chown root:"$SERVICE_GROUP" "$LOG_DIR" "$DEPLOY_LOG_DIR"
    chown -R "$SERVICE_USER":"$SERVICE_GROUP" "$LOG_DIR/bot"
    chown root:"$SERVICE_GROUP" "$BACKUP_DIR"
}

start_deploy_log() {
    action=$1
    [ "$(id -u)" -eq 0 ] || return 0

    mkdir -p "$DEPLOY_LOG_DIR"
    timestamp=$(date '+%Y%m%d-%H%M%S')
    if [ "$action" = "auto" ]; then
        DEPLOY_LOG_FILE="$DEPLOY_LOG_DIR/$(date '+%Y%m%d')_auto.log"
    else
        DEPLOY_LOG_FILE="$DEPLOY_LOG_DIR/${timestamp}_${action}.log"
    fi
    touch "$DEPLOY_LOG_FILE"
    chmod 640 "$DEPLOY_LOG_FILE"
    printf 'Log desta execucao: %s\n' "$DEPLOY_LOG_FILE"
    exec >>"$DEPLOY_LOG_FILE" 2>&1
    info "Inicio da execucao: $action"
}

prepare_environment_file() {
    if [ -f "$ENV_FILE" ]; then
        chown root:"$SERVICE_GROUP" "$ENV_FILE"
        chmod 640 "$ENV_FILE"
        return
    fi

    cp "$REPOSITORY_ROOT/.env.example" "$ENV_FILE"
    chown root:"$SERVICE_GROUP" "$ENV_FILE"
    chmod 640 "$ENV_FILE"
    cat <<EOF

O arquivo de configuracao foi criado:
  $ENV_FILE

Edite os valores da maquina e execute novamente:
  sudo $APP_DIR/devops/deploy.sh install

Campos importantes:
  APP_ENV=homologation ou APP_ENV=production
  APP_DEBUG=false
  HTTP_PORT=8000
  SECRET_KEY=<chave longa e exclusiva>
  DB_HOST, DB_PORT, DB_NAME, DB_USER e DB_PASSWORD
EOF
    exit 2
}

create_virtualenv() {
    if [ ! -x "$PYTHON" ]; then
        info "Criando ambiente virtual em $VENV_DIR"
        python3 -m venv "$VENV_DIR" || die "Falha ao criar o ambiente virtual. Instale python3-venv."
    fi

    info "Instalando dependencias Python."
    "$PYTHON" -m pip install --upgrade pip
    "$PIP" install -r "$APP_DIR/requirements.txt"
}

env_value() {
    key=$1
    "$PYTHON" - "$ENV_FILE" "$key" <<'PY'
import sys
from dotenv import dotenv_values

value = dotenv_values(sys.argv[1]).get(sys.argv[2])
if value is not None:
    print(value, end="")
PY
}

run_with_app_env() {
    "$PYTHON" - "$ENV_FILE" "$@" <<'PY'
import os
import sys
from dotenv import dotenv_values

env = os.environ.copy()
env.update({key: value for key, value in dotenv_values(sys.argv[1]).items() if value is not None})
command = sys.argv[2:]
if not command:
    raise SystemExit("Comando ausente.")
os.execvpe(command[0], command, env)
PY
}

require_env_value() {
    key=$1
    value=$(env_value "$key")
    [ -n "$value" ] || die "Variavel obrigatoria ausente no arquivo de ambiente: $key"
}

validate_environment() {
    for key in APP_ENV APP_DEBUG HTTP_PORT SECRET_KEY DB_DRIVER DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD; do
        require_env_value "$key"
    done

    app_env=$(env_value APP_ENV)
    case "$app_env" in
        homologation|production)
            ;;
        *)
            die "APP_ENV deve ser homologation ou production no servidor. Valor atual: $app_env"
            ;;
    esac

    app_debug=$(env_value APP_DEBUG)
    [ "$app_debug" = "false" ] || die "APP_DEBUG deve ser false em homologacao e producao."

    if [ "$app_env" = "production" ]; then
        session_https_only=$(env_value SESSION_HTTPS_ONLY)
        [ "$session_https_only" = "true" ] \
            || die "SESSION_HTTPS_ONLY deve ser true em producao."
    fi

    http_port=$(env_value HTTP_PORT)
    case "$http_port" in
        ''|*[!0-9]*)
            die "HTTP_PORT deve ser numerica."
            ;;
    esac
    [ "$http_port" -ge 1 ] && [ "$http_port" -le 65535 ] \
        || die "HTTP_PORT deve estar entre 1 e 65535."

    secret_key=$(env_value SECRET_KEY)
    case "$secret_key" in
        change-me|troque-*|dev-local-*)
            die "SECRET_KEY ainda contem um valor de exemplo."
            ;;
    esac
    [ "${#secret_key}" -ge 32 ] || die "SECRET_KEY deve possuir pelo menos 32 caracteres."

    db_password=$(env_value DB_PASSWORD)
    case "$db_password" in
        troque-*|vitoria)
            die "DB_PASSWORD ainda contem um valor de exemplo."
            ;;
    esac
}

validate_database_connection() {
    info "Validando conexao com o banco."
    (
        cd "$APP_DIR"
        run_with_app_env "$PYTHON" -c '
from sqlalchemy import text
from app.database import engine
with engine.connect() as connection:
    connection.execute(text("SELECT 1"))
print("Conexao com o banco validada.")
'
    )
}

run_migrations() {
    info "Aplicando migrations Alembic."
    (
        cd "$APP_DIR"
        run_with_app_env "$PYTHON" -m alembic upgrade head
    )
}

run_tests() {
    info "Executando testes automatizados antes do deploy."
    (
        cd "$APP_DIR"
        run_with_app_env "$PYTHON" -m pytest -q
    )
}

write_systemd_services() {
    info "Criando servicos systemd."

    cat >"/etc/systemd/system/$WEB_SERVICE" <<EOF
[Unit]
Description=VitoriaFinance Web
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$PYTHON $APP_DIR/main.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

    cat >"/etc/systemd/system/$BOT_SERVICE" <<EOF
[Unit]
Description=VitoriaFinance Telegram Bot
After=network-online.target $WEB_SERVICE
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$PYTHON -m app.automation.runner
Restart=on-failure
RestartSec=10
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

    chmod 644 "/etc/systemd/system/$WEB_SERVICE" "/etc/systemd/system/$BOT_SERVICE"
    systemctl daemon-reload
    systemctl enable "$WEB_SERVICE"

    bot_enabled=$(env_value BOT_SERVICE_ENABLED)
    if [ "$bot_enabled" = "true" ]; then
        systemctl enable "$BOT_SERVICE"
    else
        systemctl disable "$BOT_SERVICE" >/dev/null 2>&1 || true
        info "Bot criado, mas desabilitado. Use BOT_SERVICE_ENABLED=true depois de configurar o Telegram."
    fi
}

start_services() {
    systemctl restart "$WEB_SERVICE"

    bot_enabled=$(env_value BOT_SERVICE_ENABLED)
    if [ "$bot_enabled" = "true" ]; then
        systemctl restart "$BOT_SERVICE"
    else
        systemctl stop "$BOT_SERVICE" >/dev/null 2>&1 || true
    fi
}

stop_services() {
    systemctl stop "$BOT_SERVICE" >/dev/null 2>&1 || true
    systemctl stop "$WEB_SERVICE" >/dev/null 2>&1 || true
}

health_check() {
    http_port=$(env_value HTTP_PORT)
    url="http://127.0.0.1:${http_port}/health"
    elapsed=0

    info "Aguardando health check: $url"
    while [ "$elapsed" -lt "$HEALTH_TIMEOUT" ]; do
        if curl --fail --silent --show-error --max-time 5 "$url" >/dev/null 2>&1; then
            info "Health check concluido com sucesso."
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done

    systemctl status "$WEB_SERVICE" --no-pager >&2 || true
    journalctl -u "$WEB_SERVICE" -n 100 --no-pager >&2 || true
    return 1
}

acquire_deploy_lock() {
    mkdir -p "$RUN_DIR"
    eval "exec 9>\"$DEPLOY_LOCK\""
    flock -n 9 || die "Ja existe uma instalacao ou deploy em andamento."
}

sanitize_name() {
    printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_'
}

create_database_backup() {
    require_command mysqldump

    db_host=$(env_value DB_HOST)
    db_port=$(env_value DB_PORT)
    db_name=$(env_value DB_NAME)
    db_user=$(env_value DB_USER)
    db_password=$(env_value DB_PASSWORD)
    db_charset=$(env_value DB_CHARSET)
    [ -n "$db_charset" ] || db_charset=utf8mb4

    version_name=$(sanitize_name "$1")
    timestamp=$(date '+%Y%m%d-%H%M%S')
    sql_file="$BACKUP_DIR/${timestamp}_${version_name}.sql"
    archive_file="${sql_file}.gz"
    credentials_file="$RUN_DIR/mysql-backup.cnf.$$"

    umask 077
    {
        printf '[client]\n'
        printf 'host=%s\n' "$db_host"
        printf 'port=%s\n' "$db_port"
        printf 'user=%s\n' "$db_user"
        printf 'password=%s\n' "$db_password"
        printf 'default-character-set=%s\n' "$db_charset"
    } >"$credentials_file"
    trap 'rm -f "$credentials_file" "$sql_file"' EXIT INT TERM

    info "Criando backup pre-deploy: $archive_file"
    mysqldump \
        --defaults-extra-file="$credentials_file" \
        --single-transaction \
        --routines \
        --triggers \
        --databases "$db_name" \
        --result-file="$sql_file"

    [ -s "$sql_file" ] || die "O backup foi criado vazio."
    gzip -9 "$sql_file"
    [ -s "$archive_file" ] || die "Falha ao compactar o backup."
    chmod 600 "$archive_file"
    rm -f "$credentials_file"
    trap - EXIT INT TERM

    find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.sql.gz' -mtime +"$BACKUP_RETENTION" -delete
    info "Backup validado: $archive_file"
}

ensure_clean_repository() {
    [ -z "$(git -C "$APP_DIR" status --porcelain)" ] \
        || die "Existem alteracoes locais em $APP_DIR. Deploy interrompido."
}

resolve_deploy_target() {
    requested_ref=${1:-}
    app_env=$(env_value APP_ENV)

    git -C "$APP_DIR" fetch origin --prune --tags

    case "$app_env" in
        homologation)
            [ -z "$requested_ref" ] || [ "$requested_ref" = "$HOMOLOGATION_BRANCH" ] \
                || die "Homologacao aceita apenas a branch $HOMOLOGATION_BRANCH."
            DEPLOY_REF=$HOMOLOGATION_BRANCH
            DEPLOY_TARGET="refs/remotes/origin/$HOMOLOGATION_BRANCH"
            git -C "$APP_DIR" show-ref --verify --quiet "$DEPLOY_TARGET" \
                || die "Branch remota nao encontrada: $HOMOLOGATION_BRANCH"
            ;;
        production)
            [ -n "$requested_ref" ] || die "Producao exige uma tag, por exemplo: deploy v1.0.0"
            case "$requested_ref" in
                v[0-9]*)
                    ;;
                *)
                    die "A tag de producao deve comecar com v, por exemplo v1.0.0."
                    ;;
            esac
            DEPLOY_REF=$requested_ref
            DEPLOY_TARGET="refs/tags/$requested_ref"
            git -C "$APP_DIR" show-ref --verify --quiet "$DEPLOY_TARGET" \
                || die "Tag nao encontrada: $requested_ref"
            main_target="refs/remotes/origin/$HOMOLOGATION_BRANCH"
            git -C "$APP_DIR" show-ref --verify --quiet "$main_target" \
                || die "Branch remota nao encontrada: $HOMOLOGATION_BRANCH"
            ;;
        *)
            die "APP_ENV invalido para deploy: $app_env"
            ;;
    esac

    DEPLOY_SHA=$(git -C "$APP_DIR" rev-parse "${DEPLOY_TARGET}^{commit}")
    if [ "$app_env" = "production" ]; then
        git -C "$APP_DIR" merge-base --is-ancestor "$DEPLOY_SHA" "$main_target" \
            || die "A tag $DEPLOY_REF nao aponta para um commit pertencente a $HOMOLOGATION_BRANCH."
    fi
}

validate_install_ref() {
    app_env=$(env_value APP_ENV)
    current_sha=$(git -C "$APP_DIR" rev-parse HEAD)

    case "$app_env" in
        homologation)
            current_branch=$(git -C "$APP_DIR" branch --show-current)
            [ "$current_branch" = "$HOMOLOGATION_BRANCH" ] \
                || die "A instalacao de homologacao deve iniciar na branch $HOMOLOGATION_BRANCH."
            ;;
        production)
            git -C "$APP_DIR" fetch origin --prune --tags
            current_tag=$(git -C "$APP_DIR" describe --tags --exact-match "$current_sha" 2>/dev/null || true)
            case "$current_tag" in
                v[0-9]*)
                    ;;
                *)
                    die "A instalacao de producao exige checkout de uma tag v*, por exemplo v1.0.0."
                    ;;
            esac
            main_target="refs/remotes/origin/$HOMOLOGATION_BRANCH"
            git -C "$APP_DIR" merge-base --is-ancestor "$current_sha" "$main_target" \
                || die "A tag $current_tag nao aponta para um commit pertencente a $HOMOLOGATION_BRANCH."
            ;;
    esac
}

record_deploy() {
    status=$1
    ref=$2
    sha=$3
    timestamp=$(date --iso-8601=seconds)

    printf '%s status=%s env=%s ref=%s sha=%s\n' \
        "$timestamp" "$status" "$(env_value APP_ENV)" "$ref" "$sha" >>"$DEPLOY_HISTORY"
}

write_deploy_state() {
    temporary="$DEPLOY_STATE.tmp.$$"
    {
        printf 'APP_ENV=%s\n' "$(env_value APP_ENV)"
        printf 'REF=%s\n' "$DEPLOY_REF"
        printf 'SHA=%s\n' "$DEPLOY_SHA"
        printf 'DEPLOYED_AT=%s\n' "$(date --iso-8601=seconds)"
    } >"$temporary"
    chmod 640 "$temporary"
    mv "$temporary" "$DEPLOY_STATE"
}

deployed_sha() {
    [ -f "$DEPLOY_STATE" ] || return 0
    sed -n 's/^SHA=//p' "$DEPLOY_STATE" | head -n 1
}

install_application() {
    require_root
    validate_runtime
    validate_repository
    acquire_deploy_lock

    [ "$REPOSITORY_ROOT" = "$APP_DIR" ] \
        || die "O clone deve estar em $APP_DIR. Local atual: $REPOSITORY_ROOT"

    create_service_user
    prepare_directories
    prepare_environment_file
    create_virtualenv
    validate_environment
    ensure_clean_repository
    validate_install_ref
    validate_database_connection
    run_migrations
    write_systemd_services
    start_services
    health_check || die "A aplicacao nao respondeu ao health check."

    current_sha=$(git -C "$APP_DIR" rev-parse HEAD)
    current_ref=$(git -C "$APP_DIR" describe --tags --exact-match 2>/dev/null \
        || git -C "$APP_DIR" branch --show-current)
    [ -n "$current_ref" ] || current_ref=detached
    DEPLOY_REF=$current_ref
    DEPLOY_SHA=$current_sha
    write_deploy_state
    record_deploy installed "$current_ref" "$current_sha"

    info "Instalacao concluida."
    info "Aplicacao: http://127.0.0.1:$(env_value HTTP_PORT)"
}

deploy_application() {
    require_root
    validate_runtime
    validate_repository
    acquire_deploy_lock

    [ -f "$ENV_FILE" ] || die "Instalacao ausente. Execute install primeiro."
    [ -x "$PYTHON" ] || die "Ambiente virtual ausente. Execute install primeiro."
    create_virtualenv
    validate_environment
    ensure_clean_repository
    resolve_deploy_target "${1:-}"

    installed_sha=$(deployed_sha)
    if [ -n "$installed_sha" ] && [ "$installed_sha" = "$DEPLOY_SHA" ]; then
        info "A versao solicitada ja esta instalada: $DEPLOY_REF ($DEPLOY_SHA)"
        return
    fi
    old_sha=$installed_sha
    [ -n "$old_sha" ] || old_sha=$(git -C "$APP_DIR" rev-parse HEAD)

    info "Preparando deploy de $DEPLOY_REF ($DEPLOY_SHA)."
    git -C "$APP_DIR" checkout --detach "$DEPLOY_SHA"
    create_virtualenv
    if ! run_tests; then
        record_deploy failed-tests "$DEPLOY_REF" "$DEPLOY_SHA"
        git -C "$APP_DIR" checkout --detach "$old_sha" || true
        create_virtualenv || true
        die "Os testes falharam. A versao em execucao foi preservada."
    fi
    validate_database_connection
    create_database_backup "$DEPLOY_REF"

    stop_services
    if ! run_migrations; then
        record_deploy failed-migration "$DEPLOY_REF" "$DEPLOY_SHA"
        git -C "$APP_DIR" checkout --detach "$old_sha" || true
        create_virtualenv || true
        start_services || true
        die "Migration falhou. O banco nao foi restaurado automaticamente."
    fi

    write_systemd_services
    start_services
    if ! health_check; then
        record_deploy failed-health "$DEPLOY_REF" "$DEPLOY_SHA"
        die "Deploy falhou no health check. Consulte os logs antes de restaurar o banco."
    fi

    write_deploy_state
    record_deploy success "$DEPLOY_REF" "$DEPLOY_SHA"
    info "Deploy concluido: $DEPLOY_REF ($DEPLOY_SHA)"
}

auto_deploy() {
    require_root
    validate_runtime
    validate_repository

    [ -f "$ENV_FILE" ] || die "Instalacao ausente. Execute install primeiro."
    [ -x "$PYTHON" ] || die "Ambiente virtual ausente. Execute install primeiro."
    validate_environment

    app_env=$(env_value APP_ENV)
    git -C "$APP_DIR" fetch origin --prune --tags

    case "$app_env" in
        homologation)
            info "Homologacao: verificando a branch $HOMOLOGATION_BRANCH."
            deploy_application "$HOMOLOGATION_BRANCH"
            ;;
        production)
            main_target="refs/remotes/origin/$HOMOLOGATION_BRANCH"
            git -C "$APP_DIR" show-ref --verify --quiet "$main_target" \
                || die "Branch remota nao encontrada: $HOMOLOGATION_BRANCH"
            main_sha=$(git -C "$APP_DIR" rev-parse "${main_target}^{commit}")
            production_tag=$(git -C "$APP_DIR" tag \
                --points-at "$main_sha" \
                --list 'v*' \
                --sort=-v:refname | head -n 1)

            if [ -z "$production_tag" ]; then
                info "Producao: o commit atual da main ($main_sha) nao possui tag v*. Nenhum deploy."
                return
            fi

            info "Producao: a main aponta para $main_sha com a tag $production_tag."
            deploy_application "$production_tag"
            ;;
        *)
            die "APP_ENV invalido para deploy automatico: $app_env"
            ;;
    esac
}

check_all() {
    validate_runtime
    validate_repository
    info "Repositorio: $REPOSITORY_ROOT"
    info "Commit: $(git -C "$REPOSITORY_ROOT" rev-parse HEAD)"

    if [ -f "$ENV_FILE" ] && [ -x "$PYTHON" ]; then
        validate_environment
        validate_database_connection
        info "Configuracao instalada validada."
    else
        info "A instalacao ainda nao foi concluida em $ROOT_DIR."
    fi
}

show_version() {
    if [ -f "$DEPLOY_STATE" ]; then
        cat "$DEPLOY_STATE"
    else
        info "Estado de deploy ainda nao registrado."
    fi
    printf 'CODE_SHA=%s\n' "$(git -C "$REPOSITORY_ROOT" rev-parse HEAD)"
}

show_status() {
    systemctl status "$WEB_SERVICE" "$BOT_SERVICE" --no-pager || true
}

show_logs() {
    case "${1:-web}" in
        web)
            exec journalctl -fu "$WEB_SERVICE"
            ;;
        bot)
            exec journalctl -fu "$BOT_SERVICE"
            ;;
        deploy)
            [ -d "$DEPLOY_LOG_DIR" ] || die "Diretorio de logs de deploy ainda nao existe."
            latest_log=$(find "$DEPLOY_LOG_DIR" -maxdepth 1 -type f -name '*.log' \
                -printf '%T@ %p\n' | sort -nr | head -n 1 | cut -d' ' -f2-)
            [ -n "$latest_log" ] || die "Nenhum log de deploy encontrado."
            printf 'Acompanhando: %s\n' "$latest_log"
            exec tail -f "$latest_log"
            ;;
        *)
            die "Use logs web, logs bot ou logs deploy."
            ;;
    esac
}

command_name=${1:-auto}
case "$command_name" in
    auto)
        start_deploy_log auto
        auto_deploy
        info "Verificacao automatica concluida."
        ;;
    check)
        check_all
        ;;
    install)
        start_deploy_log install
        install_application
        info "Comando install concluido."
        ;;
    deploy)
        start_deploy_log deploy
        deploy_application "${2:-}"
        info "Comando deploy concluido."
        ;;
    version)
        show_version
        ;;
    status)
        show_status
        ;;
    logs)
        show_logs "${2:-web}"
        ;;
    help|-h|--help)
        usage
        ;;
    *)
        usage >&2
        exit 1
        ;;
esac
