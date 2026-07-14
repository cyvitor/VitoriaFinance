# VitoriaFinance

O VitoriaFinance é um sistema web de gestão financeira pessoal e compartilhada. Ele reúne contas, cartões, receitas, despesas, transferências e recorrências em uma visão mensal inspirada em planilhas, com controle de acesso por área financeira e projeção de saldo.

## Por que este projeto existe

Ao conhecer os sistemas financeiros disponíveis no mercado, não encontrei um que mostrasse minhas finanças da mesma forma que a planilha de Excel que eu já usava. Com a evolução da inteligência artificial e a possibilidade de contar com ela durante o desenvolvimento, decidi transformar aquela experiência em um sistema próprio.

O VitoriaFinance nasceu dessa necessidade: manter uma visão financeira clara e familiar, mas com espaço para evoluir. A ideia é que, no futuro, a **Vitoria** seja uma assistente financeira com IA disponível também pelo Telegram, ajudando a registrar movimentações, consultar informações e planejar melhor o uso do dinheiro.

## O que já está disponível

- Dashboard com saldo, entradas, saídas, compromissos, saldo livre e despesas por categoria.
- Visão mensal e anual por competência, com valores realizados e previstos.
- Receitas, despesas e transferências entre contas.
- Contas bancárias, cartões, categorias e áreas financeiras.
- Gastos no crédito e no débito, extrato do cartão, confirmação de fatura e parcelamento em até 120 vezes.
- Receitas recorrentes e despesas fixas, confirmadas ou ignoradas individualmente em cada mês.
- Fechamento e reabertura de competências mensais.
- Análise financeira por período e área, com evolução mensal, categorias, setores e projeção de saldo de 3 a 60 meses.
- Contas do sistema, usuários, administradores e permissões por área financeira.
- Configuração global e integração operacional com DeepInfra e Telegram.
- Assistente financeira conversacional com consultas protegidas por área, registro confirmado de despesas e atualização confirmada de amortizações.

## Tecnologias

- Python 3.12+
- FastAPI e Uvicorn
- SQLAlchemy e Alembic
- MySQL 8+
- Jinja2, HTMX, Bootstrap e Chart.js
- Pytest

## Como executar

### 1. Prepare o banco MySQL

```sql
CREATE DATABASE vitoria_finance CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'vitoria'@'localhost' IDENTIFIED BY 'uma-senha-segura';
GRANT ALL PRIVILEGES ON vitoria_finance.* TO 'vitoria'@'localhost';
FLUSH PRIVILEGES;
```

### 2. Configure a aplicação

Copie `.env.example` para `.env` e ajuste, no mínimo, os dados de conexão e a `SECRET_KEY`.

```powershell
Copy-Item .env.example .env
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m alembic upgrade head
python main.py
```

Para iniciar o worker de automacao e o bot Telegram em outro terminal, configure
primeiro o token em **Configuracoes globais** e execute:

```bash
python -m app.automation.runner
```

Cada usuario associa o proprio Telegram em **Meu perfil**. O sistema gera um
codigo de oito digitos, de uso unico e valido por dez minutos, que deve ser
enviado ao bot no formato `/start CODIGO`. A vinculacao usa o Telegram User ID,
nao o username.

Depois da vinculacao, o usuario conversa naturalmente com a Vitoria, por exemplo:
`gastei 85,90 no mercado`. O modelo ativo da DeepInfra interpreta a intencao e o
bot cria um rascunho valido por 30 minutos. Respostas naturais como `sim, pode
registrar` ou `nao, cancela` confirmam ou descartam o rascunho. Se a IA estiver
indisponivel, nenhum lancamento e criado e o bot orienta o uso da interface web.

A Vitoria também consulta contas, cartões, receitas, despesas, gastos de cartão,
resumos mensais e financiamentos. O backend injeta o escopo do usuário associado
e reaplica as permissões em cada ferramenta; a IA nunca escolhe IDs de usuário,
workspace ou áreas fora das permitidas. Amortizações são coletadas em conversa,
apresentadas para conferência e somente alteram o contrato após confirmação clara.

Acesse [http://localhost:8000](http://localhost:8000).

O seed inicial cria o acesso abaixo somente para o ambiente recém-instalado:

- Usuário: `vh`
- Senha: `123456`

Altere essa senha em **Meu perfil** logo após o primeiro acesso. Em produção, use HTTPS, defina `SESSION_HTTPS_ONLY=true` e gere uma `SECRET_KEY` longa e aleatória.

## Testes

```powershell
python -m pytest -q
```

Os testes usam um banco SQLite temporário e não alteram o MySQL configurado no `.env`.

## Utilitários

Para apagar lançamentos, previsões, recorrências e fechamentos mensais, preservando usuários e cadastros:

```powershell
python -m scripts.clear_transactions --confirm
```

Essa operação é destrutiva para os dados financeiros e deve ser usada com cuidado.

## Documentação

- [Índice da documentação](docs/README.md)
- [Guia funcional](docs/guia-funcional.md)
- [Instalação e configuração](docs/instalacao-e-configuracao.md)
- [Arquitetura e modelo de dados](docs/arquitetura.md)
- [Agente financeiro do Telegram](docs/agente-telegram.md)
- [Roadmap](docs/roadmap.md)
- [Planejamento inicial](docs/plano-inicial.md)

## Status

O núcleo web está funcional e em evolução. As próximas etapas concentram-se no assistente financeiro com IA, na integração operacional com o Telegram e em novos recursos de planejamento e relatórios.
