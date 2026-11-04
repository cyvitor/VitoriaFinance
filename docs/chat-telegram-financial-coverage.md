# Cobertura do chat e Telegram

## Resultado do levantamento

O chat do app e o Telegram executam o mesmo agente (`run_financial_agent`) e compartilham `execute_tool`. Portanto, novas ferramentas nesse serviço passam a funcionar nos dois canais sem duplicar regras de negócio.

Antes desta branch, o agente já consultava receitas, despesas, contas, cartões, orçamento, saldo/projeção e financiamentos; registrava/corrigia lançamentos; preparava receitas; fazia amortizações; registrava abastecimentos associados a despesas; e pagava/consultava contas fixas pendentes. Faltava principalmente administrar os cadastros e os parâmetros que alimentam essas consultas.

## Cobertura acrescentada

| Área do sistema web | Chat/Telegram nesta branch |
| --- | --- |
| Orçamentos | Criar/ajustar, desativar, liberar a reserva do mês e reativá-la; consulta existente permanece |
| Veículos e abastecimentos | Consultar veículos e histórico; cadastrar, editar e desativar veículos; cadastro de abastecimento continua ligado ao fluxo de despesas |
| Receitas recorrentes | Listar, cadastrar, editar, desativar e escolher conta de destino/categoria/dia |
| Contas fixas | Listar todas; cadastrar, editar, desativar; pagar e consultar pendências pela integração existente |
| Financiamentos | Listar, cadastrar parcela recorrente, editar dados, desativar e amortizar |
| Contas e cartões | Consultar e cadastrar conta/cartão com área, conta vinculada, limite e vencimento |
| Categorias | Consultar e cadastrar categorias de receita/despesa |
| Lançamentos | Consultar, criar e corrigir pelo conjunto existente de ferramentas |

## Fluxo de segurança e conversa

- Toda escrita de cadastro cria uma ação pendente. O agente apresenta um resumo e aguarda confirmação no texto; o Telegram também oferece os botões Confirmar/Cancelar.
- Se faltarem campos, o agente pergunta apenas o que falta e retoma a mesma ação no turno seguinte.
- O agente e o Telegram usam sempre a área financeira padrão do usuário logado, mesmo que o app/web permitam trocar de área. As ferramentas mantêm a validação de permissão de leitura/escrita sobre essa área.
- Uma ação pendente diferente ou um lançamento ainda aguardando confirmação impede iniciar outro cadastro, para não sobrescrever nem perder a confirmação anterior.
- Configurações administrativas do provedor de IA, tokens do Telegram, senhas e gerenciamento de usuários permanecem fora das ferramentas financeiras conversacionais.

## Limites

- Histórico de abastecimento retorna até 100 itens por consulta.
- A análise e a alteração dependem do usuário informar nomes suficientemente claros quando há cadastros homônimos; o agente deve perguntar pela categoria/conta para desambiguar.
