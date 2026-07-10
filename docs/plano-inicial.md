# VitoriaFinance — Planejamento inicial

> Documento histórico que registra a visão original do projeto. Para conhecer o que está implementado hoje, consulte o [guia funcional](guia-funcional.md). Para acompanhar as próximas etapas, consulte o [roadmap](roadmap.md).

## Visao Geral

O VitoriaFinance sera um sistema web de gerenciamento financeiro pessoal, familiar e compartilhado, desenvolvido inicialmente para uso proprio, mas projetado desde o inicio para suportar multiplos usuarios.

O objetivo nao e ser apenas um controle financeiro, mas um copiloto financeiro inteligente, usando IA para auxiliar no controle, planejamento e tomada de decisoes.

## Conceitos Principais

- **VitoriaFinance:** nome do sistema.
- **Vitoria:** assistente financeira com IA.
- **VitoriaBot:** bot do Telegram.
- **Workspace:** ambiente financeiro isolado.
- **Pessoa financeira:** entidade usada para classificar lancamentos dentro de um workspace.
- **Usuario:** pessoa com login proprio no sistema.
- **Membro do workspace:** vinculo entre usuario e workspace com permissao.

## Publico-Alvo

- Uso pessoal
- Casais
- Familias
- Amigos
- Pequenos grupos

## Filosofia do Produto

O sistema deve ser simples para registrar movimentacoes e poderoso para consultas, analises e planejamento.

Sempre que possivel, a IA deve reduzir a quantidade de cliques necessarios para registrar e consultar informacoes financeiras.

## Arquitetura Inicial

Na primeira versao, a aplicacao deve rodar em um unico processo Python:

```bash
python main.py
```

Componentes iniciais:

- Interface web
- API
- Scheduler
- Bot Telegram

Nao ha necessidade inicial de Docker, microservicos ou filas, mas a arquitetura deve permitir evolucao futura.

## Stack Tecnologica

### Backend

- Python
- FastAPI

### Frontend

- Jinja2
- HTMX
- Bootstrap
- Chart.js

### Banco de Dados

- MySQL
- SQLAlchemy
- Alembic

## Workspaces

O sistema sera baseado em workspaces. Cada workspace representa um ambiente financeiro isolado.

Exemplos:

- Familia
- Casal
- Empresa
- Projeto

Cada workspace podera possuir:

- Pessoas
- Contas
- Cartoes
- Receitas
- Despesas
- Transferencias
- Emprestimos
- Patrimonio
- Planejamento
- Metas

## Usuarios e Permissoes

Cada usuario possui login proprio e pode participar de varios workspaces.

Exemplo:

- Vitor participa do workspace Familia e do workspace Empresa.
- Esposa participa do workspace Familia.
- Contador participa do workspace Empresa com acesso somente leitura.

Papeis iniciais:

- **Administrador:** controle total.
- **Editor:** pode cadastrar e editar movimentacoes.
- **Leitor:** apenas consulta.
- **Consultor:** acesso focado em relatorios.

## Conta do Casal

Na conta do casal, cada pessoa possui seu proprio login. Nao e necessario compartilhar usuario e senha.

Exemplo:

- Vitor tem login proprio.
- Esposa tem login proprio.
- Ambos pertencem ao mesmo workspace Familia.
- Cada um pode vincular seu proprio Telegram.
- Lancamentos podem pertencer a Vitor, Esposa ou ao workspace como despesa compartilhada.

## Bancos, Contas e Cartoes

O usuario podera cadastrar diversos bancos e contas.

Tipos de conta previstos:

- Conta corrente
- Conta poupanca
- Conta investimento
- Carteira digital
- Conta internacional
- Cripto
- Dinheiro em especie

Cartoes podem ter:

- Nome
- Banco
- Bandeira
- Limite
- Dia de fechamento
- Dia de vencimento
- Cor

## Receitas

Campos previstos:

- Valor
- Data
- Pessoa
- Workspace
- Banco ou conta de entrada
- Fonte
- Categoria
- Observacao
- Recorrencia

Fontes iniciais:

- Salario
- Freelance
- Dividendos
- Venda
- PIX
- Cashback
- Reembolso
- Aluguel
- Outros

## Despesas

Campos previstos:

- Valor
- Data
- Pessoa
- Workspace
- Banco ou conta
- Cartao, quando aplicavel
- Categoria
- Observacao
- Forma de pagamento
- Status
- Parcelamento
- Recorrencia

Formas de pagamento:

- Debito
- Credito
- PIX
- Dinheiro
- Transferencia

Status:

- Pendente
- Pago
- Cancelado

## Transferencias

Transferencias entre contas nao devem ser tratadas como receita nem despesa.

Exemplo:

Mover R$ 500 do Nubank para o Itau altera o saldo das contas, mas nao altera o patrimonio total.

Por isso, transferencia deve ser um tipo proprio de movimentacao.

## Parcelamentos

Compras parceladas devem gerar automaticamente as parcelas futuras.

Exemplo:

Notebook em 12 parcelas:

- 1/12
- 2/12
- 3/12
- ...
- 12/12

Cada parcela sera um lancamento independente, ligado a um grupo de parcelamento.

## Recorrencias

Receitas e despesas recorrentes devem gerar lancamentos automaticamente conforme a regra configurada.

Exemplos:

- Salario
- Internet
- Aluguel
- Streaming
- Academia
- Plano de saude

## Emprestimos

Cadastro previsto:

- Valor
- Juros
- Parcelas
- Banco
- Data inicial
- Parcelas pagas
- Parcelas restantes

## Categorias Iniciais

### Moradia

- Aluguel
- Condominio
- Energia
- Agua
- Internet
- IPTU
- Reformas
- Moveis
- Decoracao

### Alimentacao

- Mercado
- Restaurante
- Delivery
- Padaria
- Cafe

### Transporte

- Combustivel
- Uber
- Taxi
- Onibus
- Pedagio
- Seguro
- IPVA
- Oficina
- Estacionamento

### Saude

- Plano de saude
- Consulta
- Exames
- Medicamentos
- Odontologia
- Psicologo
- Academia

### Educacao

- Cursos
- Faculdade
- Livros
- Idiomas

### Trabalho

- Equipamentos
- Software
- Ferramentas

### Lazer

- Cinema
- Jogos
- Streaming
- Passeios
- Viagens

### Familia

- Escola
- Filhos
- Presentes
- Mesada

### Cuidados Pessoais

- Beleza
- Vestuario
- Barbearia
- Cabelereiro
- Higiene pessoal

### Pets

- Veterinario
- Banho
- Racao
- Medicamentos

### Financeiro

- Juros
- Tarifas
- IOF
- Financiamentos
- Dividas
- Seguros

### Planejamento

- Reserva de emergencia
- Viagem
- Casa
- Carro
- Casamento
- Notebook
- Eventos

### Outros

- Doacoes
- Impostos
- Emergencias
- Reembolsos
- Outros

## Dashboard

O dashboard deve funcionar bem em desktop e dispositivos moveis.

Indicadores previstos:

- Patrimonio
- Saldo atual
- Entradas
- Saidas
- Cartoes
- Faturas
- Contas pendentes
- Contas vencidas
- Proximos vencimentos

Visao principal:

```text
Saldo atual
- Compromissos ate o final do mes
= Saldo livre
```

## Fluxo de Caixa

Visualizacoes:

- Diario
- Semanal
- Mensal
- Anual

O usuario tambem deve poder visualizar meses futuros, com receitas previstas, despesas previstas e saldo projetado.

## Planejamento Financeiro

O usuario podera criar metas.

Exemplos:

- Viagem
- Carro
- Casa
- Notebook
- Casamento
- Reserva

Campos previstos:

- Valor desejado
- Valor atual
- Data limite
- Percentual concluido

O sistema deve calcular automaticamente quanto precisa ser economizado por mes para atingir a meta.

## Patrimonio

Cadastro de bens e dividas para calculo de patrimonio liquido.

Exemplos de bens:

- Casa
- Apartamento
- Carro
- Moto
- Notebook
- Bitcoin
- Ethereum
- Ouro

## Investimentos

Modulo previsto para evolucao futura.

Suporte planejado:

- Tesouro Direto
- CDB
- Acoes
- ETFs
- FIIs
- Bitcoin
- Ethereum

## Relatorios

Relatorios previstos:

- Fluxo mensal
- Gastos por categoria
- Gastos por pessoa
- Evolucao patrimonial
- Receitas x despesas
- Cartoes
- Investimentos

Exportacoes futuras:

- PDF
- Excel
- CSV

## IA Vitoria

A IA deve auxiliar em:

- Cadastro de movimentacoes
- Consultas
- Planejamento
- Alertas
- Recomendacoes

Exemplos de perguntas:

- Quanto gastei esse mes?
- Quanto falta para minha viagem?
- Quanto sobrou?
- Quanto gastei com alimentacao?
- Qual sera meu saldo em setembro?

## Bot Telegram

O Telegram sera uma interface adicional do VitoriaFinance.

Toda a logica deve permanecer no sistema principal. O bot apenas recebe mensagens, identifica o usuario e chama os servicos internos.

### Arquitetura MVP

O bot utilizara long polling no MVP.

Webhook podera ser implementado futuramente.

### Configuracao do Bot

A configuracao sera feita pela interface web.

Campos previstos:

- Habilitar bot
- Bot token
- Intervalo do polling
- Timeout
- Modo debug

Apos salvar, o sistema deve testar automaticamente a conexao com o Telegram.

Caso o token seja alterado, o bot deve ser reiniciado sem reiniciar toda a aplicacao.

## Autenticacao do Telegram

O sistema nao deve usar username do Telegram como identificador principal.

Fluxo recomendado:

1. Usuario faz login na interface web.
2. Solicita conectar Telegram.
3. Sistema gera um codigo temporario.
4. Usuario envia o codigo ao bot.
5. Sistema vincula o Telegram User ID ao usuario do VitoriaFinance.

A partir dai, todas as mensagens daquele Telegram sao associadas ao usuario correto.

## Configuracao da IA

A configuracao sera feita pela interface web.

Campos previstos:

- Provedor
- API key
- Modelo
- Temperatura
- Timeout
- Maximo de tokens

Provedores previstos:

- DeepInfra
- OpenAI
- OpenRouter
- Ollama

A API key deve ser armazenada de forma segura.

## Painel Administrativo

Funcoes previstas:

- Cadastro de usuarios
- Cadastro de workspaces
- Configuracao do Telegram
- Configuracao da IA
- Logs
- Auditoria
- Configuracoes globais

## Modelo de Dados Inicial

Tabelas candidatas:

- `users`
- `workspaces`
- `workspace_members`
- `people`
- `accounts`
- `cards`
- `categories`
- `transactions`
- `installment_groups`
- `recurrence_rules`
- `goals`
- `loans`
- `app_settings`
- `telegram_links`
- `ai_settings`
- `audit_logs`

## Roadmap Sugerido

### MVP 1: Controle financeiro usavel

- Login
- Workspaces
- Pessoas
- Bancos, contas e cartoes
- Receitas
- Despesas
- Transferencias
- Parcelamentos
- Recorrencias simples
- Dashboard
- Fluxo de caixa mensal e futuro

### MVP 2: IA e Telegram

- Bot Telegram via long polling
- Vinculacao por codigo temporario
- Cadastro de movimentacoes por mensagem
- Comandos basicos
- Interpretacao de linguagem natural
- Painel admin para bot e IA

### MVP 3: Planejamento e relatorios

- Metas
- Planejamento de viagens, compras e prestacoes
- Relatorios por categoria, pessoa, banco e cartao
- Exportacao CSV/Excel
- Alertas financeiros

### MVP 4: Evolucao SaaS

- Convites para usuarios
- Permissoes refinadas
- Auditoria completa
- Logs administrativos
- Configuracoes por workspace
- Importacao OFX/CSV
- Open Finance
- OCR de comprovantes

## Observacoes

Este documento representa a consolidacao do planejamento inicial. A modelagem final, APIs, telas, regras de negocio e estrutura interna ainda devem ser refinadas durante a fase de desenvolvimento.
