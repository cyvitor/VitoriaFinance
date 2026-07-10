# Roadmap

Este documento diferencia o que está disponível no código atual do que continua planejado.

## Entregue no núcleo web

- Autenticação, sessão e alteração de senha.
- Contas do sistema, workspace principal e usuários por conta.
- Administração global, super contas e desativação de contas.
- Áreas financeiras pessoais ou compartilhadas e acesso por usuário.
- Contas bancárias, cartões e categorias.
- Receitas, despesas e transferências.
- Gastos no cartão em crédito ou débito e parcelamento.
- Receitas recorrentes e despesas fixas com confirmação mensal.
- Visão por competência, fechamento e reabertura do mês.
- Dashboard e análise financeira com projeção de saldo.
- Configuração e teste de conexão com DeepInfra e Telegram.
- Migrações Alembic, seed inicial e testes funcionais automatizados.

## Próxima etapa: assistente e Telegram

- Executar o bot por long polling ou por uma arquitetura equivalente.
- Vincular com segurança o identificador do Telegram ao usuário autenticado.
- Interpretar mensagens em linguagem natural.
- Registrar receitas e despesas após confirmação do usuário.
- Responder a consultas como saldo, gastos, pendências e projeções.
- Aplicar as mesmas permissões de workspace e área financeira usadas pela interface web.
- Criar trilha de auditoria das ações realizadas pela IA.

## Planejamento e relatórios

- Metas financeiras e cálculo de economia mensal necessária.
- Planejamento de viagens, compras e prestações.
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
