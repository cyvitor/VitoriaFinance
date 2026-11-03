# API para o aplicativo móvel

A API JSON é servida pelo mesmo FastAPI das telas web, sob `/api/v1`. As rotas financeiras reutilizam as entidades e regras de acesso a áreas do serviço. Nenhum endpoint recebe `user_id` ou `workspace_id` arbitrário: a identidade vem do token e o workspace é validado pela associação do usuário.

## Autenticação

- `POST /api/v1/auth/login`: JSON `{ "username", "password", "device_name?" }`; devolve `access_token`, validade e perfil.
- `GET /api/v1/auth/me`: perfil, workspace, áreas permitidas e indicação de administrador.
- `POST /api/v1/auth/logout`: revoga o token do dispositivo.
- Demais rotas recebem `Authorization: Bearer <access_token>`.

O token é aleatório, válido por 30 dias e armazenado no banco somente como SHA-256. Revogação e expiração são verificadas a cada chamada autenticada. O app guarda o valor original no Keychain (iOS) ou Keystore (Android).

## Dados e operações

- `GET /api/v1/bootstrap?area_id=`: áreas, contas, cartões, categorias e contexto inicial.
- `GET /api/v1/areas`, `/accounts?area_id=`, `/categories?kind=`, `/cards?area_id=`: listas para telas e formulários nativos.
- `GET /api/v1/dashboard?area_id=`: saldo confirmado, totais do mês e movimentações recentes.
- `GET /api/v1/months/{ano}/{mês}?area_id=`: totais, status de fechamento e lançamentos da competência.
- `GET /api/v1/transactions?year=&month=&area_id=&limit=&offset=`: lista paginada de lançamentos.
- `POST /api/v1/transactions`: receita, despesa ou transferência. Valida valor, categoria, conta, cartão, área e permissão de escrita. Compras no crédito ficam pendentes; compras no débito ficam pagas. Competências fechadas são deslocadas para a próxima competência aberta.

Valores monetários são strings decimais com duas casas para evitar perda de precisão. Datas usam ISO `YYYY-MM-DD`. Os erros de validação seguem o formato padrão do FastAPI.

## Instalação e testes

Aplicar a migração antes de iniciar a versão com a API:

```powershell
python -m alembic upgrade head
```

Os testes da API ficam em `tests/test_mobile_api.py` e usam o SQLite temporário do `tests/conftest.py`:

```powershell
python -m pytest tests/test_mobile_api.py -q
```
