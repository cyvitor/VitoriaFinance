# Agente financeiro do Telegram

## Visão geral

A Vitoria recebe mensagens naturais pelo Telegram. O único comando tratado sem
IA é `/start CODIGO`, usado exclusivamente para associar o Telegram User ID a um
usuário autenticado do VitoriaFinance.

As demais mensagens passam pelo modelo configurado na DeepInfra. O modelo não
acessa o banco diretamente: ele escolhe uma ferramenta e fornece apenas filtros
de negócio, como período, nome de área, cartão ou financiamento.

## Isolamento de dados

Antes de executar qualquer ferramenta, o backend cria um contexto imutável com:

- usuário e conta do sistema;
- workspaces dos quais o usuário é membro;
- áreas financeiras visíveis;
- workspaces nos quais o papel permite escrita;
- área financeira padrão.

IDs internos de usuário, workspace e área não são aceitos como argumentos da IA.
Cada ferramenta reaplica o contexto na consulta SQL. Nomes mencionados na
conversa são resolvidos somente entre os registros já autorizados.

## Ferramentas disponíveis

- listar áreas financeiras;
- consultar contas e saldos estimados;
- consultar cartões, uso mensal e limite disponível;
- consultar gastos de cartão por período;
- consultar receitas e despesas por período, área e categoria;
- consultar resumo mensal;
- consultar financiamentos;
- preparar, confirmar ou cancelar uma despesa;
- preparar, confirmar ou cancelar uma amortização.

Resultados agregados são calculados pelo backend. A IA apenas interpreta a
solicitação e apresenta os dados retornados.

## Alterações financeiras

Despesas e amortizações seguem duas etapas. Primeiro o sistema cria um rascunho
ou ação pendente e apresenta um resumo. Somente uma confirmação natural posterior
efetiva a mudança.

Quando uma mensagem contém várias despesas, o backend exige que a IA extraia
todos os itens. A primeira despesa vira o rascunho ativo e as demais ficam em
uma fila persistente no banco. Depois de confirmar ou cancelar o item atual, o
bot abre automaticamente o próximo, preservando valor, data, categoria, forma
de pagamento e cartão já informados. A fila sobrevive à reinicialização do
worker e impede que parte de uma mensagem seja ignorada silenciosamente.

Para amortizações, o agente coleta:

- financiamento;
- novo saldo devedor oficial;
- redução de prazo, parcela ou ambos;
- parcelas restantes, quando o prazo muda;
- novo valor da parcela, quando a prestação muda;
- data, valor amortizado e observações, quando informados.

O sistema não deduz automaticamente o saldo bancário a partir do valor pago. O
saldo oficial deve ser informado pelo usuário, pois juros, seguros, correções e o
método de amortização podem alterar o cálculo.

Ao confirmar, o contrato e a recorrência mensal são atualizados e os valores
anteriores e novos são registrados em `financing_amortizations`.

## Falhas da IA

Se a DeepInfra falhar antes da execução de uma ferramenta, nenhuma operação é
realizada e o bot orienta o uso da interface web. Se uma escrita já tiver sido
confirmada e persistida, mas a IA falhar apenas ao formatar a resposta, o backend
envia uma confirmação determinística para não induzir o usuário a repetir a ação.

## Diagnóstico e logs

O worker possui os níveis `basic` e `detailed`. O nível básico registra somente
eventos operacionais relevantes e erros; o detalhado inclui mensagens, respostas
da DeepInfra, ferramentas, resultados e a resposta final. Como o modo detalhado
pode conter informações financeiras pessoais, ele deve ser usado apenas durante
investigações. A configuração completa, rotação e comandos de acompanhamento
estão em [Instalação e configuração](instalacao-e-configuracao.md#logs-do-worker-e-do-agente).
