<div align="center">

# Aqua - Backend

Backend Django/DRF com monitoração de enchentes via câmeras, previsão do tempo, ocorrências, cadastro de pontos de alagamento, upload e gerenciamento de usuários. Orquestrado com Celery + Redis e Postgres via Docker Compose.

</div>

> Consulte o [guia de operação de containers e demo](docs/OPERACAO_CONTAINERS_E_DEMO.md)
> para comandos, variáveis, portas, saúde dos serviços e o plano de isolamento
> opcional de Flood Monitoring.

---

## Arquitetura Dual-Instance (Dev + Prod)

O projeto suporta duas instâncias independentes rodando no mesmo computador sem conflitos:

| Aspecto | Desenvolvimento | Produção |
|---|---|---|
| **Diretório** | `~/apps/aqua-dev/` | `~/apps/aqua-prod/` |
| **Projeto Docker** | `aqua-dev` | `aqua-prod` |
| **Porta Web** | `8001` | `8000` |
| **Porta DB** | `5434` | `5433` |
| **Volumes** | `aqua-dev_pgdata`, `aqua-dev_media` | `aqua-prod_pgdata`, `aqua-prod_media` |
| **Rede** | `aqua-dev_default` | `aqua-prod_default` |
| **Banco** | `aqua_dev` | `aqua_prod` |
| **Compose** | `docker compose up` | `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d` |

Cada instância é um clone independente do repositório, com seu próprio `.env`, volumes e containers.

---

## Início Rápido

### 1. Clonar o repositório (duas cópias)

```bash
# Produção
git clone <repo-url> ~/apps/aqua-prod
cd ~/apps/aqua-prod
git checkout main

# Desenvolvimento
git clone <repo-url> ~/apps/aqua-dev
cd ~/apps/aqua-dev
git checkout develop
```

### 2. Configurar ambiente

```bash
cd ~/apps/aqua-dev
cp .env.sample.dev .env
# Editar .env com suas credenciais de dev

cd ~/apps/aqua-prod
cp .env.sample.prod .env
# Editar .env com credenciais reais de produção
```

### 3. Subir os serviços

```bash
# Desenvolvimento (override carregado automaticamente)
cd ~/apps/aqua-dev

# Base: API e worker geral, sem câmeras e sem demo
docker compose -p aqua-dev up --build

# Apenas câmeras
docker compose -p aqua-dev --profile flood up --build

# Apenas demo
# Requer três vídeos enviados para /api/upload/videos/ e as chaves
# DEMO_NORMAL_VIDEO_ATTACHMENT_KEY, DEMO_FLOODED_VIDEO_ATTACHMENT_KEY e
# DEMO_DYNAMIC_VIDEO_ATTACHMENT_KEY no .env.
docker compose -p aqua-dev --profile demo up --build

# Câmeras e demo
docker compose -p aqua-dev --profile flood --profile demo up --build

# Produção (sempre rodando)
cd ~/apps/aqua-prod
docker compose -p aqua-prod -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Na produção, Flood Monitoring, beat e demo-stream sobem por padrão. Em
desenvolvimento, os perfis tornam câmeras e demo opcionais.

### 4. Migrações e superusuário

```bash
docker compose -p aqua-dev exec web python manage.py migrate
docker compose -p aqua-dev exec web python manage.py createsuperuser
```

---

## Mapeamento de Portas

| Serviço | Container | Dev | Prod |
|---|---|---|---|
| **Django API** | `8090` | `8001` | `8000` |
| **PostgreSQL** | `5432` | `5434` | `5433` |
| **Redis** | `6379` | _(não exposta)_ | _(não exposta)_ |
| **Worker/Beat** | — | _(sem porta)_ | _(sem porta)_ |

---

## Arquivos de Composição

| Arquivo | Função |
|---|---|
| `docker-compose.yml` | Base compartilhada (build, depends_on, volumes nomeados) |
| `docker-compose.override.yml` | Override dev (portas interpoladas, bind mount para hot reload) — **auto-load** |
| `docker-compose.prod.yml` | Override prod (portas fixas, restart policies, volumes nomeados) |

### Uso

```bash
# Dev (override auto-load)
docker compose -p aqua-dev up

# Prod (override explícito)
docker compose -p aqua-prod -f docker-compose.yml -f docker-compose.prod.yml up -d
```

---

## Comandos Comuns

```bash
# Ver logs
docker compose -p aqua-dev logs -f web
docker compose -p aqua-dev logs -f worker

# Executar comandos no container
docker compose -p aqua-dev exec web python manage.py migrate
docker compose -p aqua-dev exec web python manage.py createsuperuser

# Parar
docker compose -p aqua-dev down

# Reconstruir imagem
docker compose -p aqua-dev up --build

# Ver volumes
docker volume ls | grep aqua
```

---

## Variáveis de Ambiente

Cada clone tem seu próprio `.env`. Use os samples como template:

| Sample | Ambiente | Portas |
|---|---|---|
| `.env.sample.dev` | Desenvolvimento | `WEB_PORT=8001`, `DB_PORT=5434` |
| `.env.sample.prod` | Produção | `WEB_PORT=8000`, `DB_PORT=5433` |

Principais variáveis:

| Variável | Descrição |
|---|---|
| `DEBUG` | `1` para dev, `0` para produção |
| `DJANGO_SECRET_KEY` | Chave secreta (gerar com `python -c "import secrets; print(secrets.token_urlsafe(64))"`) |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | Credenciais do banco |
| `CELERY_BROKER_URL` | Redis (default: `redis://redis:6379/0`) |
| `WEB_PORT` | Porta host para o Django |
| `DB_PORT` | Porta host para o PostgreSQL |
| `ACCESS_TOKEN` | Token Mercado Pago |
| `FLOOD_MODEL_DRIVE_ID` | ID do modelo ML no Google Drive |
| `API_URL` | URL da API para sync remoto |
| `INTERNAL_METRICS_ENABLED` | Habilita o endpoint privado `/internal/metrics` somente quando vale `1` |
| `INTERNAL_METRICS_TOKEN` | Bearer token exclusivo exigido pelo endpoint de métricas |

### Métricas privadas para o Aqua Ops

O endpoint `/internal/metrics` permanece desativado quando a flag não está
configurada. Para permitir que o Prometheus privado do Aqua Ops faça a coleta,
configure no ambiente do backend:

```dotenv
INTERNAL_METRICS_ENABLED=1
INTERNAL_METRICS_TOKEN=<mesmo-conteudo-do-secret-usado-pelo-prometheus>
```

O valor de `INTERNAL_METRICS_TOKEN` deve ser exatamente o conteúdo do secret
entregue ao Prometheus. Não reutilize credenciais de usuário, JWT, token do
callback de CI ou segredo do Django, e não registre o valor real no
repositório.

---

## Estrutura do Projeto

```
├── docker-compose.yml              # Docker Compose base
├── docker-compose.override.yml     # Dev overrides (auto-load)
├── docker-compose.prod.yml         # Prod overrides
├── Dockerfile               # Multi-stage production (Gunicorn — usado pelo Dokku)
├── Dockerfile.slim          # Slim image (usada pelo Docker Compose local)
├── requirements.runtime.txt # Dependências para a slim image
├── .dockerignore            # Regras de exclusão do build Docker
├── requirements.txt         # Dependências pinadas
├── pyproject.toml           # Metadados e scripts PDM
├── Procfile                 # Perfil Heroku/Dokku
├── config/                  # Django settings, urls, wsgi, celery
├── core/                    # Apps: users, weather, forecast, occurrences, etc.
├── .env.sample.dev          # Template para dev
└── .env.sample.prod         # Template para produção
```

---

## Deploy (Dokku)

O deploy real de produção é feito via GitHub Actions para Dokku:

```yaml
# .github/workflows/deploy.yml
# Push na branch main → ssh para dokku@app2.fabricadesoftware.ifc.edu.br
```

O `Dockerfile` da raiz (multi-stage com Gunicorn) é usado pelo Dokku, não pelo Compose local.

---

## Solução de Problemas

- **Porta ocupada**: Ajuste `WEB_PORT` e `DB_PORT` no `.env`
- **Conflito de volumes**: Use `-p aqua-prod` / `-p aqua-dev` para isolar nomes
- **Healthcheck DB falhando**: Verifique se `POSTGRES_USER` no `.env` corresponde ao usuário do healthcheck
- **Migrações pendentes**: `docker compose exec web python manage.py migrate`
