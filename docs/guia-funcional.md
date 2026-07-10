# Guia funcional

## Visão geral

O VitoriaFinance organiza as finanças de uma ou mais pessoas sem exigir que todos compartilhem o mesmo login. Cada conta do sistema possui um workspace principal, usuários próprios e áreas financeiras que delimitam o que cada membro pode visualizar.

## Conceitos

- **Conta do sistema:** organização de nível superior. Pode ser uma família, um casal ou outro grupo independente.
- **Workspace:** ambiente que contém os dados financeiros de uma conta do sistema.
- **Área financeira:** representa uma pessoa ou um contexto compartilhado, como `Vitor`, `Esposa` ou `Casal`.
- **Usuário:** identidade usada para entrar no sistema.
- **Competência:** mês e ano aos quais uma movimentação pertence, independentemente do dia em que foi efetivamente confirmada.
- **Lançamento:** receita, despesa ou transferência registrada no sistema.

## Perfis e permissões

### Superadministrador global

Pode gerenciar todas as contas do sistema, criar contas normais ou super contas, acessar as configurações globais e desativar contas. A própria conta do administrador global não pode ser desativada por ele.

### Administrador da conta

Gerencia usuários e áreas financeiras de sua conta. Administradores enxergam todas as áreas do workspace.

### Membro

Enxerga apenas as áreas liberadas pelo administrador. Essa restrição também é aplicada às contas bancárias e aos lançamentos associados às áreas.

## Dashboard

A página inicial apresenta a competência atual e consolida:

- saldo calculado a partir dos saldos iniciais e lançamentos confirmados;
- receitas e despesas confirmadas no mês;
- compromissos pendentes;
- saldo livre;
- lançamentos recentes;
- despesas por categoria;
- atalhos para nova receita, despesa e gasto no cartão.

## Visão mês

A visão mensal é o centro do acompanhamento financeiro. Ela permite navegar pelo ano e consultar, por competência:

- saldo inicial, realizado, previsto e saldo final;
- receitas e despesas confirmadas ou pendentes;
- receitas recorrentes e despesas fixas aguardando decisão;
- gastos de cartão agrupados por cartão, modalidade e situação;
- meses com movimentação dentro do ano selecionado.

O mês pode ser fechado e reaberto. Se uma confirmação for feita para uma competência fechada, o lançamento será direcionado ao próximo mês aberto.

## Lançamentos

O sistema aceita três tipos:

- **Receita:** entrada de dinheiro vinculada a uma área, conta e, opcionalmente, categoria.
- **Despesa:** saída de dinheiro com área, conta, categoria, forma de pagamento e cartão opcionais.
- **Transferência:** movimentação entre duas contas diferentes. Não é somada como receita nem despesa no cálculo consolidado.

Os lançamentos possuem situação pendente, paga ou cancelada. Itens pendentes podem ser confirmados posteriormente, respeitando o fechamento das competências.

## Contas bancárias e cartões

As contas armazenam nome, banco, tipo, saldo inicial, cor e área financeira. Os tipos disponíveis são conta corrente, poupança, investimento, carteira digital, conta internacional, cripto e dinheiro em espécie.

Os cartões podem ser vinculados a uma conta e armazenam bandeira, limite, dia de fechamento, dia de vencimento e cor. Para lançar um gasto pelo atalho de cartões, o cartão precisa estar ligado a uma conta que tenha uma área financeira.

### Regras dos gastos no cartão

- Débito entra como despesa confirmada.
- Crédito entra como despesa pendente até a confirmação da fatura.
- Uma compra no crédito pode ser parcelada de 1 a 120 vezes.
- As parcelas são distribuídas pelas competências seguintes.
- Eventuais centavos restantes ficam na última parcela para preservar exatamente o valor total.
- O extrato permite consultar os gastos do cartão, confirmar a fatura da competência e remover um gasto, tratado na interface como estorno.

## Receitas recorrentes e despesas fixas

Uma recorrência é um modelo mensal, não um lançamento criado antecipadamente. A cada competência ativa, ela aparece na visão mensal e pode ser:

- confirmada com o valor original ou um valor ajustado apenas para aquele mês;
- ignorada somente naquela competência;
- desativada para deixar de aparecer nos meses futuros.

Confirmar cria um lançamento real e preserva o modelo para os próximos meses. Desativar uma regra não apaga o histórico que já foi confirmado.

## Análise financeira

A análise pode considerar todas as áreas visíveis ou uma área específica. O histórico pode abranger 3, 6, 12 ou 24 meses, e a projeção pode cobrir 3, 6, 12, 24, 36 ou 60 meses.

A tela apresenta receitas, despesas, gastos no crédito, saldo atual, taxa de economia, médias mensais, categorias, setores e evolução mensal. A projeção combina:

- médias dos meses completos do período escolhido;
- receitas recorrentes ativas;
- despesas fixas ativas;
- saldo inicial das contas e fluxo já confirmado.

A projeção é uma estimativa baseada no histórico e nas recorrências cadastradas, não uma garantia de saldo futuro.

## Configurações de IA e Telegram

O superadministrador pode salvar a chave da DeepInfra, carregar o catálogo de modelos, escolher um modelo e testar uma chamada curta. Também pode salvar um token do Telegram e validá-lo por meio do método `getMe`.

Essas telas validam a conectividade, mas o assistente de IA e o bot operacional ainda fazem parte do roadmap. Atualmente não há interpretação de mensagens, vínculo de usuários do Telegram nem criação de lançamentos pela IA.
