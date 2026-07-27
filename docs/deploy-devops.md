# Instalação e deploy com o script DevOps

O script `devops/deploy.sh` prepara uma instalação Linux sem Docker, usando Git,
ambiente virtual Python, Alembic e serviços systemd. Ele deve ser executado como
`root` por meio de `sudo`, mas a aplicação é executada pelo usuário restrito
`vitoriafinance`.

## Estrutura no servidor

```text
/opt/vitoriafinance/
  app/                 clone do repositório
  config/
    vitoriafinance.env configuração externa da máquina
  venv/                ambiente virtual Python
  backups/banco/       backups pré-deploy do MySQL
  logs/bot/            logs persistentes do bot
  logs/deploy/         logs completos das verificações e deploys
  state/               versão instalada e histórico
  run/                 lock e arquivos temporários
```

O arquivo de ambiente, os backups, os logs e o estado ficam fora do clone Git.
Uma atualização do código não sobrescreve esses dados.

## Requisitos da VPS

- Linux com systemd;
- Python 3.12 ou superior;
- Git;
- `python3-venv`;
- `curl`;
- cliente MySQL, incluindo `mysqldump`;
- `flock`, fornecido normalmente pelo pacote `util-linux`;
- `gzip`;
- acesso ao repositório privado no GitHub.

Em uma distribuição Ubuntu compatível, os pacotes básicos podem ser instalados
com:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv curl default-mysql-client util-linux gzip
```

O banco e o usuário MySQL devem ser criados antes da instalação. O script aplica
o esquema, mas não cria o servidor MySQL nem concede permissões ao usuário.

## Primeira instalação em homologação

Clone a branch `main` no local definitivo:

```bash
sudo mkdir -p /opt/vitoriafinance
sudo git clone git@github.com:cyvitor/VitoriaFinance.git \
  /opt/vitoriafinance/app
cd /opt/vitoriafinance/app
sudo sh ./devops/deploy.sh check
sudo sh ./devops/deploy.sh install
```

Na primeira execução, `install` cria:

```text
/opt/vitoriafinance/config/vitoriafinance.env
```

O script encerra antes de acessar o banco ou criar os serviços. Edite o arquivo:

```bash
sudo nano /opt/vitoriafinance/config/vitoriafinance.env
```

Configuração mínima de homologação:

```env
APP_NAME="VitoriaFinance Homologacao"
APP_ENV=homologation
APP_DEBUG=false
HTTP_PORT=8000
SECRET_KEY=uma-chave-exclusiva-com-pelo-menos-32-caracteres

DB_DRIVER=mysql+pymysql
DB_HOST=host-do-mysql
DB_PORT=3306
DB_NAME=vitoriafinance_homologacao
DB_USER=vitoriafinance_homologacao
DB_PASSWORD=senha-real
DB_CHARSET=utf8mb4

SESSION_HTTPS_ONLY=false
BOT_LOG_DIR=/opt/vitoriafinance/logs/bot
BOT_LOG_LEVEL=basic
BOT_LOG_MAX_BYTES=10485760
BOT_LOG_BACKUP_COUNT=7
BOT_SERVICE_ENABLED=false
```

Execute novamente:

```bash
cd /opt/vitoriafinance/app
sudo sh ./devops/deploy.sh install
```

A segunda execução:

1. valida o sistema, o repositório e o ambiente;
2. cria o usuário Linux `vitoriafinance`;
3. cria o ambiente virtual;
4. instala as dependências;
5. testa a conexão com o MySQL;
6. executa `alembic upgrade head`;
7. cria os serviços systemd;
8. inicia o serviço web;
9. consulta `http://127.0.0.1:${HTTP_PORT}/health`;
10. registra o commit instalado.

## Primeira instalação em produção

Produção deve ser instalada a partir de uma tag:

```bash
sudo mkdir -p /opt/vitoriafinance
sudo git clone git@github.com:cyvitor/VitoriaFinance.git \
  /opt/vitoriafinance/app
cd /opt/vitoriafinance/app
sudo git fetch --tags
sudo git checkout --detach v1.0.0
sudo sh ./devops/deploy.sh install
```

Configure o arquivo externo com:

```env
APP_ENV=production
APP_DEBUG=false
SESSION_HTTPS_ONLY=true
```

Depois execute `install` novamente. O script recusa uma instalação de produção
se o `HEAD` não estiver exatamente em uma tag iniciada por `v`.

## Serviços

O script cria:

```text
vitoriafinance-web.service
vitoriafinance-bot.service
```

Comandos úteis:

```bash
sudo systemctl status vitoriafinance-web
sudo journalctl -fu vitoriafinance-web
sudo journalctl -fu vitoriafinance-bot
```

O bot é habilitado somente quando:

```env
BOT_SERVICE_ENABLED=true
```

Antes disso, configure e teste o token do Telegram e a DeepInfra na interface
web. Depois altere a variável e execute `install` novamente para atualizar os
serviços.

Também é possível usar os atalhos:

```bash
sudo sh ./devops/deploy.sh
sudo sh ./devops/deploy.sh status
sudo sh ./devops/deploy.sh logs web
sudo sh ./devops/deploy.sh logs bot
sudo sh ./devops/deploy.sh logs deploy
sudo sh ./devops/deploy.sh version
```

Quando nenhum comando é informado, o script executa a verificação automática
adequada ao `APP_ENV`.

## Atualização de homologação

Quando `APP_ENV=homologation`, o script aceita somente a branch `main`:

```bash
cd /opt/vitoriafinance/app
sudo sh ./devops/deploy.sh deploy main
```

Se a referência for omitida, `main` é utilizada:

```bash
sudo sh ./devops/deploy.sh deploy
```

## Atualização de produção

Quando `APP_ENV=production`, o script exige uma tag iniciada por `v`:

```bash
cd /opt/vitoriafinance/app
sudo sh ./devops/deploy.sh deploy v1.1.0
```

Uma tentativa de implantar `main` diretamente em produção é recusada.

## Etapas de uma atualização

O comando `deploy`:

1. impede dois deploys simultâneos com `flock`;
2. recusa alterações locais no clone;
3. busca branches e tags no GitHub;
4. valida a referência conforme `APP_ENV`;
5. faz checkout do commit exato;
6. atualiza as dependências Python;
7. executa toda a suíte Pytest;
8. testa a conexão com o banco;
9. cria e compacta um backup MySQL;
10. interrompe web e bot;
11. executa `alembic upgrade head`;
12. atualiza e reinicia os serviços;
13. executa o health check;
14. registra referência, SHA, ambiente, data e resultado.

Os backups são gravados como:

```text
/opt/vitoriafinance/backups/banco/20260726-153000_v1.1.0.sql.gz
```

Por padrão, dumps com mais de 30 dias são removidos. A retenção pode ser
alterada ao executar o script com `VITORIAFINANCE_BACKUP_RETENTION`.

Se uma migration falhar, o deploy registra a falha e não restaura o banco
automaticamente. A restauração deve ser uma decisão consciente, pois pode
eliminar dados posteriores ao backup.

## Verificação automática por cron

O script pode ser executado sem argumentos:

```bash
cd /opt/vitoriafinance/app
sudo sh ./devops/deploy.sh
```

Ele lê `APP_ENV` e decide:

```text
APP_ENV=homologation
  -> verifica origin/main
  -> se o SHA mudou, testa e implanta

APP_ENV=production
  -> verifica o SHA atual de origin/main
  -> procura uma tag v* apontando exatamente para esse SHA
  -> sem tag: não altera a produção
  -> com tag nova: testa e implanta
```

Para verificar a cada cinco minutos, edite o crontab do `root`:

```bash
sudo crontab -e
```

Adicione:

```cron
*/5 * * * * /usr/bin/sh /opt/vitoriafinance/app/devops/deploy.sh
```

O repositório é privado, portanto o usuário `root` precisa possuir uma chave SSH
com acesso de leitura ao GitHub para que `git fetch` funcione no cron.

## Logs do deploy

Toda execução de `install`, `deploy` ou do modo automático redireciona sua saída
completa para:

```text
/opt/vitoriafinance/logs/deploy/
```

As verificações automáticas do mesmo dia são agrupadas:

```text
20260726_auto.log
```

Instalações e deploys manuais recebem data e hora no nome:

```text
20260726-153000_install.log
20260726-181500_deploy.log
```

Os passos controlados pelo script também imprimem data e hora em cada mensagem.
Para acompanhar o arquivo mais recente:

```bash
sudo sh /opt/vitoriafinance/app/devops/deploy.sh logs deploy
```

## Papel do GitHub Actions

GitHub Actions pode apenas testar ou também realizar deploy. As duas estratégias
são válidas:

```text
Estratégia com cron:
  GitHub Actions testa
  cada servidor consulta o Git periodicamente e decide se deve implantar

Estratégia orientada pelo GitHub:
  GitHub Actions testa
  se aprovado, conecta ao servidor por SSH e chama o deploy
```

Este projeto usa inicialmente a estratégia com cron. Para evitar que o cron
implante um commit enquanto o runner ainda está testando, o próprio
`deploy.sh` executa novamente o Pytest antes de backup, migration ou reinício.
Se os testes falharem, a versão em execução é preservada.

No futuro, o cron poderá ser substituído por um workflow de deploy via SSH. A
vantagem seria iniciar imediatamente após os testes do runner e mostrar todo o
resultado na interface do GitHub.
