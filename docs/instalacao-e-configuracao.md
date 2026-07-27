# Instalação e configuração

## Requisitos

- Python 3.12 ou superior
- MySQL 8 ou superior
- Acesso à internet no navegador para carregar Bootstrap, Bootstrap Icons, HTMX e Chart.js pelos CDNs usados na interface

## Banco de dados

Crie o banco e um usuário dedicado. Troque a senha do exemplo antes de usar fora de um ambiente local.

```sql
CREATE DATABASE vitoria_finance CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'vitoria'@'localhost' IDENTIFIED BY 'uma-senha-segura';
GRANT ALL PRIVILEGES ON vitoria_finance.* TO 'vitoria'@'localhost';
FLUSH PRIVILEGES;
```

## Variáveis de ambiente

Copie `.env.example` para `.env` e configure:

| Variável | Finalidade | Exemplo |
| --- | --- | --- |
| `APP_NAME` | Nome exibido pela aplicação | `VitoriaFinance` |
| `APP_ENV` | Identificação do ambiente | `development` |
| `APP_DEBUG` | Ativa o modo de depuração | `false` |
| `HTTP_PORT` | Porta HTTP em que a aplicação escuta | `8000` |
| `SECRET_KEY` | Assina o cookie de sessão | valor longo e aleatório |
| `DB_DRIVER` | Driver SQLAlchemy | `mysql+pymysql` |
| `DB_HOST` | Host do MySQL | `127.0.0.1` |
| `DB_PORT` | Porta do MySQL | `3306` |
| `DB_NAME` | Nome do banco | `vitoria_finance` |
| `DB_USER` | Usuário do banco | `vitoria` |
| `DB_PASSWORD` | Senha do banco | senha definida no MySQL |
| `DB_CHARSET` | Codificação da conexão | `utf8mb4` |
| `SESSION_HTTPS_ONLY` | Restringe o cookie a HTTPS | `true` em produção |
| `BOT_LOG_DIR` | Diretório dos logs do worker, relativo à raiz do projeto ou absoluto | `logs` |
| `BOT_LOG_LEVEL` | Nível de detalhes do bot: `basic` ou `detailed` | `basic` |
| `BOT_LOG_MAX_BYTES` | Tamanho máximo de cada arquivo antes da rotação | `10485760` |
| `BOT_LOG_BACKUP_COUNT` | Quantidade de arquivos antigos preservados | `7` |
| `BOT_SERVICE_ENABLED` | Habilita o serviço systemd do bot | `false` |

A variável `DATABASE_URL` também é aceita como substituição da configuração separada do banco. Ela é usada principalmente por testes e ambientes gerenciados.

## Ambiente Python e execução

No PowerShell:

```powershell
Copy-Item .env.example .env
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m alembic upgrade head
python main.py
```

A aplicação fica disponível na porta definida por `HTTP_PORT` (por padrão, [http://localhost:8000](http://localhost:8000)). O endpoint `/health` retorna um JSON simples para verificação de disponibilidade.

## Worker do Telegram

No painel **Configurações globais**, informe e teste a chave da DeepInfra, o modelo e o token do bot. Depois execute o worker em outro processo:

```powershell
python -m app.automation.runner
```

O processo web continua responsável pela interface e pelos códigos de associação; o worker usa long polling para receber as mensagens. Em produção, mantenha ambos os processos supervisionados e reinicie o worker quando alterar suas configurações operacionais.

### Logs do worker e do agente

Por padrão, o worker grava em `logs/vitoria-bot.log`. A pasta é criada
automaticamente quando o processo inicia e está no `.gitignore`, portanto as
conversas e informações operacionais não são enviadas ao repositório.

O arquivo usa rotação por tamanho. Com os valores padrão, cada arquivo pode ter
até 10 MB e sete versões anteriores são mantidas:

```env
BOT_LOG_DIR=logs
BOT_LOG_MAX_BYTES=10485760
BOT_LOG_BACKUP_COUNT=7
```

O nível básico é recomendado para uso normal e produção:

```env
BOT_LOG_LEVEL=basic
```

Ele registra inicialização e encerramento do worker, operações financeiras de
escrita, repetições causadas por respostas inválidas, indisponibilidade da
DeepInfra, erros de ferramentas e exceções inesperadas. As mensagens completas
do usuário não são registradas nesse nível.

Para investigar o comportamento do agente, ative temporariamente:

```env
BOT_LOG_LEVEL=detailed
```

Além dos eventos básicos, esse nível registra:

- texto recebido do usuário;
- resposta bruta da DeepInfra;
- decisão estruturada do agente;
- ferramenta selecionada e seus argumentos;
- resultado devolvido pela ferramenta;
- resposta enviada ao Telegram;
- quantidade de memórias candidatas extraídas.

Todos os registros possuem data, hora, fuso e nível. O modo `detailed` pode
conter descrições, valores e outras informações financeiras pessoais. Use-o
somente durante diagnósticos, restrinja o acesso à pasta de logs e retorne ao
nível `basic` quando terminar.

Depois de alterar qualquer opção de log, reinicie o worker:

```powershell
python -m app.automation.runner
```

Para acompanhar as últimas linhas no PowerShell:

```powershell
Get-Content .\logs\vitoria-bot.log -Tail 100 -Wait
```

## Instalação em servidor

Para instalar em uma VPS com Git, ambiente virtual Python, serviços systemd,
backup pré-deploy e atualização por branch ou tag, consulte o
[guia de instalação e deploy DevOps](deploy-devops.md).

## Migrações e seed

As migrations versionadas ficam em `alembic/versions`. O comando
`alembic upgrade head` cria um banco novo ou aplica apenas as revisões ainda
pendentes em uma instalação existente.

Credenciais iniciais:

- usuário: `vh`
- senha: `123456`

Troque a senha imediatamente em **Meu perfil**. O seed não cria áreas financeiras automaticamente; o administrador deve cadastrá-las na interface.

Para atualizar uma instalação existente após mudanças de esquema:

```powershell
python -m alembic upgrade head
```

## Testes

```powershell
python -m pytest -q
```

A suíte troca a conexão por um SQLite temporário e executa o seed de teste. O MySQL do ambiente não é modificado.

## Limpeza dos dados financeiros

```powershell
python -m scripts.clear_transactions --confirm
```

O comando remove lançamentos, ocorrências e regras recorrentes, além dos fechamentos mensais. Contas do sistema, usuários, áreas, contas bancárias, cartões e categorias são preservados.

## Recomendações para produção

- Use uma `SECRET_KEY` exclusiva, longa e aleatória.
- Sirva a aplicação atrás de HTTPS e defina `SESSION_HTTPS_ONLY=true`.
- Não mantenha as credenciais iniciais.
- Proteja o arquivo `.env` e faça backup periódico do MySQL.
- Restrinja o usuário do banco ao banco da aplicação.
- Não exponha as configurações globais a contas que não devam administrar IA e Telegram.
- Execute o processo web e o worker do Telegram com supervisão e logs.
- Nunca versione chaves da DeepInfra, tokens do Telegram, dumps ou backups com dados reais.
