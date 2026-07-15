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
| `SECRET_KEY` | Assina o cookie de sessão | valor longo e aleatório |
| `DB_DRIVER` | Driver SQLAlchemy | `mysql+pymysql` |
| `DB_HOST` | Host do MySQL | `127.0.0.1` |
| `DB_PORT` | Porta do MySQL | `3306` |
| `DB_NAME` | Nome do banco | `vitoria_finance` |
| `DB_USER` | Usuário do banco | `vitoria` |
| `DB_PASSWORD` | Senha do banco | senha definida no MySQL |
| `DB_CHARSET` | Codificação da conexão | `utf8mb4` |
| `SESSION_HTTPS_ONLY` | Restringe o cookie a HTTPS | `true` em produção |

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

A aplicação fica disponível em [http://localhost:8000](http://localhost:8000). O endpoint `/health` retorna um JSON simples para verificação de disponibilidade.

## Worker do Telegram

No painel **Configurações globais**, informe e teste a chave da DeepInfra, o modelo e o token do bot. Depois execute o worker em outro processo:

```powershell
python -m app.automation.runner
```

O processo web continua responsável pela interface e pelos códigos de associação; o worker usa long polling para receber as mensagens. Em produção, mantenha ambos os processos supervisionados e reinicie o worker quando alterar suas configurações operacionais.

## Migrações e seed

O histórico consolidado contém duas migrações:

- `0001_schema`: cria as tabelas e restrições;
- `0002_seed`: cria a conta inicial `VH`, o usuário global `vh` e as categorias padrão.

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
