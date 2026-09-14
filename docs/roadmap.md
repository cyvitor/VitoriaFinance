# Roadmap

Este documento diferencia o que está disponível no código atual do que continua planejado.

## Entregue no núcleo web

- Autenticação, sessão e alteração de senha.
- Contas do sistema, workspace principal e usuários por conta.
- Administração global, super contas e desativação de contas.
- Áreas financeiras pessoais ou compartilhadas e acesso por usuário.
- Contas bancárias, cartões e categorias.
- Receitas, despesas e transferências.
- Gastos no cartão em crédito ou débito, categoria obrigatória, edição pelo extrato e parcelamento.
- Receitas recorrentes e despesas fixas com confirmação mensal.
- Visão por competência, fechamento e reabertura do mês.
- Dashboard, análise mensal e análise financeira com projeção de saldo.
- Financiamentos e histórico confirmado de amortizações.
- Configuração e teste de conexão com DeepInfra e Telegram.
- Migrações Alembic, seed inicial e testes funcionais automatizados.

## Assistente e Telegram

- [x] Executar o bot por long polling.
- [x] Vincular com segurança o identificador do Telegram ao usuário autenticado.
- [x] Interpretar mensagens em linguagem natural com DeepInfra.
- [x] Registrar despesas após confirmação natural do usuário.
- [x] Consultar contas, cartões, receitas, despesas, gastos de cartão, resumo mensal e financiamentos.
- [x] Aplicar no backend as permissões de workspace, área financeira e escrita.
- [x] Coletar e confirmar atualizações de amortização, preservando histórico.
- [x] Persistir histórico curto de conversa e atualizações processadas.
- [x] Consultar saldo livre com simulação de um gasto planejado.
- [x] Repetir decisões inválidas e exigir ferramenta em consultas financeiras.
- [x] Registrar logs básicos ou detalhados do worker e do agente.
- [ ] Registrar receitas e transferências por conversa.
- [ ] Adicionar consultas de pendências e projeções detalhadas.
- [ ] Implementar o **Orientador Financeiro — Compra Consciente**, com simulação de impacto no cartão, projeção futura, comparação de cenários e planos de compra; acompanhar o checklist em [Orientador Financeiro](orientador-financeiro.md).
- [ ] Permitir que o bot localize e confirme gastos fixos/recorrentes já previstos, com ajuste de valor e cartão, sem duplicar o lançamento mensal.
- [ ] Expor a trilha de ferramentas e ações em uma tela administrativa.
- [ ] Evoluir para um agente multi-turno de ferramentas, permitindo que o modelo execute várias consultas sequenciais no mesmo pedido, como nos fluxos de agentes do n8n, com limite de passos, orçamento, auditoria e prevenção de ciclos.

## Planejamento e relatórios

- Metas financeiras e cálculo de economia mensal necessária.
- Planejamento de viagens, compras e prestações, integrado ao Orientador Financeiro.
- Relatórios por pessoa, conta, cartão, categoria e período.
- Exportação para CSV e Excel e, quando útil, PDF.
- Alertas de vencimento, limite e comportamento financeiro.

## Evoluções futuras

- Convites e permissões mais refinadas.
- Auditoria administrativa completa.
- Importação OFX e CSV.
- Open Finance.
- OCR de comprovantes.
- Patrimônio, empréstimos e investimentos.

As prioridades podem mudar conforme o uso real do sistema. O [planejamento inicial](plano-inicial.md) registra a visão original e inclui ideias que ainda não fazem parte do produto atual.
