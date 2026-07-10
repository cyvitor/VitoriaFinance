# Arquitetura e modelo de dados

## Estrutura da aplicação

O projeto é um monólito web em Python. Um único processo Uvicorn executa o FastAPI, renderiza páginas Jinja2 e atende os arquivos estáticos locais.

```text
Navegador
   |
FastAPI + sessão por cookie
   |
Rotas web e regras de negócio
   |
SQLAlchemy
   |
MySQL
```

Bootstrap, Bootstrap Icons, HTMX e Chart.js são carregados por CDN. A interface atual usa principalmente formulários HTML tradicionais e redirecionamentos após operações de escrita.

## Organização do código

```text
app/
  config.py          configuração por ambiente
  database.py        engine, sessão e base SQLAlchemy
  dependencies.py    usuário atual, workspace e permissões
  models.py          enums e entidades persistidas
  routers/web.py     páginas, operações e regras de negócio
  security.py        hash e verificação de senhas
  templates/         páginas Jinja2
  static/            CSS, JavaScript, ícones e imagens
alembic/              migrações do banco
scripts/              seed e rotinas de manutenção
tests/                testes funcionais da aplicação web
main.py               ponto de entrada local
```

## Isolamento dos dados

A separação ocorre em camadas:

1. `SystemAccount` representa a conta do cliente ou grupo.
2. Cada `Workspace` pertence a uma conta do sistema.
3. `WorkspaceMember` liga usuários aos workspaces.
4. As entidades financeiras carregam `workspace_id`.
5. Para membros comuns, `UserPersonAccess` limita as áreas financeiras visíveis.

As consultas financeiras filtram o workspace da sessão e, quando necessário, os identificadores das áreas permitidas. Contas do sistema desativadas bloqueiam seus usuários e invalidam novas sessões.

## Entidades principais

| Entidade | Responsabilidade |
| --- | --- |
| `SystemAccount` | Conta isolada e seu estado; pode conceder administração global a seus administradores |
| `User` | Login, senha, perfil administrativo e área padrão |
| `Workspace` | Contêiner dos dados financeiros |
| `WorkspaceMember` | Associação e papel do usuário no workspace |
| `Person` | Área financeira pessoal ou compartilhada |
| `UserPersonAccess` | Áreas liberadas a um membro |
| `Account` | Conta bancária, carteira ou outro saldo inicial |
| `Card` | Cartão vinculado opcionalmente a uma conta |
| `Category` | Categoria de receita ou despesa, com agrupamento por setor |
| `Transaction` | Receita, despesa ou transferência e sua competência |
| `RecurrenceRule` | Modelo mensal de receita recorrente ou despesa fixa |
| `RecurrenceOccurrence` | Decisão de confirmar ou ignorar uma recorrência em uma competência |
| `AccountingPeriod` | Estado aberto ou fechado de um mês |
| `SystemSetting` | Configurações globais, inclusive credenciais de serviços externos |

## Cálculo financeiro

O saldo parte da soma dos saldos iniciais das contas visíveis. Receitas confirmadas são adicionadas e despesas confirmadas são subtraídas. Transferências não alteram o total consolidado.

O dashboard e a visão mensal priorizam a competência. A análise histórica usa as datas dos lançamentos confirmados dentro do intervalo solicitado. A projeção calcula médias de meses completos, separa a parcela histórica ligada a recorrências e combina o restante variável com as regras recorrentes atualmente ativas.

## Sessão e segurança

- A autenticação usa senha com hash bcrypt.
- A sessão permanece em cookie assinado por até 12 horas.
- O cookie usa `SameSite=Lax` e pode ser limitado a HTTPS por variável de ambiente.
- Rotas protegidas validam usuário ativo e conta do sistema ativa.
- As configurações de DeepInfra e Telegram são marcadas como secretas no banco, mas atualmente são armazenadas no campo textual da configuração; a proteção do banco e dos backups é, portanto, essencial.

## Integrações externas atuais

- **DeepInfra:** consulta o catálogo de modelos e testa uma chamada compatível com a API de chat da OpenAI.
- **Telegram:** testa o token por meio do endpoint `getMe`.

Não existe ainda um worker, scheduler, serviço de bot ou camada de ferramentas de IA. Essas peças permanecem como evolução planejada.
