# Orientador Financeiro — módulo Compra Consciente

## Objetivo

O **Orientador Financeiro** ajuda o usuário a avaliar decisões usando os dados reais do VitoriaFinance. Seu primeiro módulo será o **Compra Consciente**, voltado a simular compras antes do lançamento, mostrar o impacto no mês e nos meses seguintes e oferecer alternativas que reduzam decisões por impulso.

O recurso oferece orientação educativa e apoio à decisão. Ele não substitui aconselhamento financeiro profissional, não promete resultados e não impede o usuário de realizar uma compra.

## Princípios

- O backend é responsável por cálculos, permissões, projeções e validações.
- A IA coleta o contexto, escolhe ferramentas e explica o resultado em linguagem natural.
- A existência de limite no cartão não significa que a compra cabe no orçamento.
- Simulações nunca criam lançamentos financeiros.
- Qualquer plano, reserva ou lançamento exige solicitação e confirmação explícitas.
- A orientação deve ser objetiva, respeitosa e sem julgamento moral.
- O usuário pode mudar de assunto naturalmente durante a conversa.

## Fluxo conversacional esperado

Exemplo inicial:

> Estou querendo fazer uma compra de R$ 200 no cartão.

O agente identifica e pergunta apenas os dados ausentes que alterem o cálculo:

- cartão;
- pagamento à vista ou parcelado;
- quantidade de parcelas;
- categoria;
- data aproximada da compra;
- existência de juros e custo total, quando aplicável.

Com os dados suficientes, o backend calcula o cenário e a IA apresenta:

- competência da primeira fatura;
- valor da fatura antes e depois da compra;
- limite disponível depois da compra;
- orçamento restante na categoria;
- saldo projetado antes e depois;
- parcelas já contratadas em cada mês;
- impacto nos próximos 3, 6 ou 12 meses;
- pior saldo projetado do período e o mês em que ocorre;
- riscos e premissas utilizados.

Depois dos números, o agente pode perguntar se a compra precisa ser feita agora ou se pode esperar. Quando fizer sentido, deve comparar compra imediata, compra após o fechamento, parcelamento e formação prévia de uma reserva.

## Classificação do cenário

| Nível | Regra geral |
|---|---|
| Confortável | Os meses analisados permanecem positivos e preservam a margem configurada. |
| Atenção | A compra cabe, mas reduz significativamente orçamento, reserva ou saldo livre. |
| Arriscado | Algum mês fica negativo, obrigações ficam descobertas ou o limite é insuficiente. |
| Incompleto | Faltam dados necessários para uma simulação confiável. |

Os limites de margem e comprometimento deverão ser configuráveis. A classificação é produzida pelo backend; a IA apenas a explica.

## Ferramentas planejadas para o agente

### `simular_compra_cartao`

Entrada prevista:

```json
{
  "amount": 200,
  "card": "C6",
  "installments": 1,
  "category": "Lazer",
  "purchase_date": "2026-09-14",
  "horizon_months": 6
}
```

O retorno deve incluir fatura, limite, orçamento da categoria, projeções mensais, parcelas existentes, pior saldo e indicadores de risco. A ferramenta é somente leitura.

### `comparar_cenarios_compra`

Compara alternativas como pagamento em uma vez, parcelamento, compra após o fechamento e adiamento para outro mês. Deve considerar juros e custo total quando informados.

### `planejar_compra`

Cria um plano somente depois da confirmação do usuário. O plano poderá guardar descrição, valor-alvo, categoria, prioridade, data-alvo, valor reservado, aporte mensal, cartão pretendido e situação.

## Planos de compra

Um plano de compra deverá possuir, no mínimo:

- descrição e valor desejado;
- prioridade e categoria;
- data-alvo;
- valor já reservado;
- aporte mensal planejado;
- cartão pretendido, quando houver;
- situação: planejando, pronto para comprar, comprado ou cancelado;
- observações e histórico de alterações.

Valores reservados para planos podem participar da projeção, mas não alteram o saldo bancário real. O usuário deverá poder liberar ou cancelar a reserva apenas para uma competência.

## Fricção contra compra por impulso

Quando a compra reduzir significativamente a margem financeira, o agente pode:

- perguntar se é necessidade, oportunidade ou desejo;
- perguntar se precisa acontecer agora;
- mostrar quanto da margem livre será consumido;
- mostrar quantos meses serão afetados;
- sugerir aguardar 24 ou 48 horas;
- sugerir uma data financeiramente melhor;
- oferecer a criação de um plano de compra.

O usuário continua livre para prosseguir, mediante confirmação consciente.

## Segurança e auditoria

- O simulador respeita conta, workspace, áreas permitidas e cartões acessíveis.
- Resultados financeiros vêm exclusivamente dos serviços internos, nunca da memória do modelo.
- Cada simulação registra premissas e data de referência para diagnóstico, sem criar transação.
- Escritas são idempotentes e exigem confirmação posterior.
- O agente não deve reutilizar uma simulação antiga se faturas, saldos ou lançamentos tiverem mudado.

## Checklist de implementação

### Etapa 1 — Simulação determinística

- [x] Definir os indicadores, margens e níveis de risco.
- [x] Criar o serviço de simulação sem persistência de lançamentos.
- [x] Calcular a competência pelo fechamento real do cartão.
- [x] Considerar fatura atual, limite e parcelas futuras existentes.
- [x] Considerar receitas, despesas fixas, financiamentos, orçamentos e saldo transportado.
- [x] Projetar o efeito da compra por 3, 6 ou 12 meses, com 6 meses como padrão.
- [x] Criar testes para mês aberto/fechado, parcelas, orçamento excedido e saldo negativo.

### Etapa 2 — Ferramenta do Telegram

- [x] Expor `simular_compra_cartao` ao agente.
- [x] Ensinar a IA a coletar somente os dados ausentes.
- [x] Garantir que a simulação nunca crie uma despesa.
- [x] Apresentar diferença entre limite disponível e capacidade financeira.
- [x] Perguntar sobre urgência somente depois de apresentar os números.
- [x] Testar mudança natural de assunto durante a análise.

O MVP usa três regras transparentes: fica **arriscado** se o limite for insuficiente ou algum saldo projetado ficar negativo; fica em **atenção** se ultrapassar o orçamento ou consumir pelo menos metade da margem projetada de algum mês; nos demais casos, fica **confortável**. Esses limites poderão se tornar configuráveis em uma etapa posterior.

### Etapa 3 — Comparação de cenários

- [ ] Criar `comparar_cenarios_compra`.
- [ ] Comparar compra agora e depois do fechamento da fatura.
- [ ] Comparar pagamento em uma vez e parcelamentos.
- [ ] Considerar juros, custo total e comprometimento futuro.
- [ ] Indicar a alternativa mais segura e explicar o motivo.

### Etapa 4 — Planos de compra

- [ ] Criar tabelas e migrations dos planos e histórico.
- [ ] Implementar criação, edição, conclusão e cancelamento.
- [ ] Calcular aporte mensal necessário para a data-alvo.
- [ ] Permitir reservar valores na projeção sem alterar o saldo real.
- [ ] Expor `planejar_compra` e consultas ao agente.

### Etapa 5 — Interface web

- [ ] Criar simulador visual de compra.
- [ ] Exibir impacto mensal e comparação entre alternativas.
- [ ] Criar tela de planos de compra e progresso das reservas.
- [ ] Adaptar gráficos para celular e garantir acessibilidade.

### Etapa 6 — Lembretes e acompanhamento

- [ ] Permitir lembrete opcional para revisar uma compra depois de 24 ou 48 horas.
- [ ] Avisar quando um plano estiver financeiramente pronto para compra.
- [ ] Permitir adiar, concluir ou cancelar pelo Telegram.
- [ ] Medir quantas simulações viraram plano ou compra, sem julgamento do usuário.

### Etapa 7 — Homologação e documentação

- [ ] Validar cenários com dados anonimizados próximos do uso real.
- [ ] Revisar textos para evitar tom moralista ou promessa financeira.
- [ ] Documentar configuração, regras e limitações no guia funcional.
- [ ] Executar a suíte completa e homologar web e Telegram.

## Evolução relacionada: conciliação de gastos previstos

O bot também deverá confirmar cobranças que já estão programadas, como GeForce NOW, Google ou YouTube Premium.

Exemplo:

> Confirme o GeForce NOW deste mês por R$ 63,90 no C6.

O agente deverá procurar uma despesa fixa ou recorrência pendente compatível, conferir competência, área, categoria e cartão, apresentar um resumo e, após confirmação, gerar ou confirmar o lançamento real. Ele nunca deve criar uma segunda cobrança quando já existir uma ocorrência confirmada.

Essa evolução deve incluir busca tolerante por nome, tratamento de valores variáveis, escolha do cartão/fatura e proteção contra duplicidade.
