# JSSG API – Centralized Jain Community Digital Platform

Backend for the **Jain Social Group** federated platform: a centralized directory of
Jain communities — **Federation → Region → Social Group → Trustees → Members** — with
events, news, notifications, media, advertisements, federation fees and payments.

Built to be consumed by a **Flutter mobile app** and a **React/Next.js admin panel**.
Content is primarily Hindi; all models carry `*_hi` fields so multilingual support can
be added later without schema redesign.

## Architecture

```
Flutter / Admin Panel
        │  HTTPS / JSON
        ▼
   Cloudflare (DNS, CDN, SSL, WAF)
        ▼
      Fly.io → FastAPI (Uvicorn, async)
                 │
        ┌────────┼──────────┐
        ▼        ▼          ▼
   PostgreSQL   Redis   Cloudflare R2
   (SQLAlchemy,       (cache,     (photos / videos / media)
    Alembic)           jobs, rate limit)
        |
   Firebase Cloud Messaging (push to Flutter)
```

Design conventions

- **Routers** validate input and delegate; no business logic.
- **Services** contain business logic.
- **Repositories** perform database operations.
- **Integrations** wrap external providers (R2, Firebase, email, payment gateways).
- **Workers** handle background jobs (notifications, payments, media).
- All responses use a consistent envelope (see "API conventions").

## Tech stack

| Layer       | Choice                                             |
|-------------|----------------------------------------------------|
| Framework   | FastAPI (async)                                    |
| ORM         | SQLAlchemy 2.x (typed `Mapped`/`mapped_column`)    |
| Migrations  | Alembic                                            |
| Validation  | Pydantic v2                                        |
| Auth        | JWT (access + refresh), bcrypt                     |
| Database    | PostgreSQL 16                                      |
| Cache/Jobs  | Redis                                              |
| Media       | Cloudflare R2                                      |
| Push        | Firebase Cloud Messaging                           |
| Deploy      | Fly.io + Docker, Cloudflare in front               |

## Repository layout

```
app/
├── main.py                 # FastAPI app + lifespan + global handlers
├── config.py               # Pydantic-settings based configuration
├── database.py             # Async engine + session factory
├── dependencies.py         # get_db, get_current_user, etc.
├── core/                   # security, jwt, permissions, exceptions, constants, responses
├── models/                 # SQLAlchemy models (UUID PKs, timestamps, soft delete)
├── schemas/                # Pydantic request/response schemas
├── api/v1/                 # versioned routers
├── services/               # business logic
├── repositories/           # data-access layer
├── integrations/           # R2, Firebase, email, payment gateway adapters
├── workers/                # background job entry points
├── utils/                  # pagination, validators, file_upload, helpers
└── data/                   # bootstrap seed data (roles, permissions)
migrations/                 # Alembic migrations
tests/                      # pytest suite
```

## Local development

### Prerequisites

- Python 3.11+
- PostgreSQL 16
- Redis 7 (optional locally; only needed for cache/rate-limit/jobs)
- Docker (optional; `docker compose` path below)

### 1. Environment

```bash
cp .env.example .env
# then edit .env with your local values
```

### 2. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt   # dev tooling
```

### 3. Database setup

```sql
CREATE ROLE jssg WITH LOGIN PASSWORD 'jssg_secret';
CREATE DATABASE jssg_db OWNER jssg;
```

Point `DATABASE_URL` in `.env` at it, e.g.:

```
DATABASE_URL=postgresql+asyncpg://jssg:jssg_secret@localhost:5432/jssg_db
```

### 4. Run migrations

```bash
alembic upgrade head
```

On every model change:

```bash
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

Rollback one step:

```bash
alembic downgrade -1
```

> `DATABASE_URL` configured as `postgresql+asyncpg://` is used as-is by the app;
> Alembic automatically resolves the sync `psycopg2` driver.
>
> Hosted Postgres providers (e.g. Neon via Vercel) often supply a plain
> `postgresql://` connection string. The app automatically rewrites it to
> `postgresql+asyncpg://` so the async engine always uses asyncpg; set
> `DATABASE_URL` in Vercel to your Neon connection string (either form works).

### 5. Run the API

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- Interactive docs: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>
- OpenAPI JSON: <http://localhost:8000/openapi.json>
- Health: <http://localhost:8000/health>

On first boot the app seeds the five roles, the full permission set, and (in
non-production environments) a default super admin:

- email: `superadmin@jssg.local`
- password: `ChangeMe123!`

**Change or remove it immediately in any shared environment.**

### 6. Run tests

```bash
pytest
```

Use an isolated/stem database for tests; tests must never touch production services.

## Docker Compose (local)

```bash
cp .env.example .env          # defaults match docker-compose.yml
docker compose up --build
```

Starts three services:

- `db` – PostgreSQL 16 on `5432`
- `redis` – Redis 7 on `6379`
- `api` – FastAPI on `8000`, runs `alembic upgrade head` at boot

## API conventions

### Authentication

All authenticated endpoints require:

```
Authorization: Bearer <access_token>
```

- Access tokens expire after `ACCESS_TOKEN_EXPIRE_MINUTES` (default 30 min).
- Refresh tokens expire after `REFRESH_TOKEN_EXPIRE_DAYS` (default 7 days).
- Refresh tokens are stored hashed server-side and revocable.

### Response envelope

Success:

```json
{
  "success": true,
  "message": "Member fetched successfully",
  "data": { }
}
```

Paginated:

```json
{
  "success": true,
  "data": [],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total": 100,
    "total_pages": 5
  }
}
```

Error:

```json
{
  "success": false,
  "message": "Member not found",
  "error_code": "MEMBER_NOT_FOUND"
}
```

Standard error codes: `UNAUTHORIZED`, `FORBIDDEN`, `NOT_FOUND`,
`ALREADY_EXISTS`, `VALIDATION_ERROR`, `BAD_REQUEST`.

### Versioning

Endpoints are versioned: `/api/v1/<resource>`. Client + server agree on the version;
breaking changes ship new versions, existing structures stay stable for Flutter.

## Environment variables

See [`.env.example`](.env.example) for the full list, including:

- `APP_NAME`, `APP_ENV`, `APP_DEBUG`, `APP_HOST`, `APP_PORT`
- `DATABASE_URL`
- `JWT_SECRET`, `JWT_ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS`
- `REDIS_URL`
- `CLOUDFLARE_R2_*` (endpoint, access key, secret key, bucket, public URL)
- `FIREBASE_PROJECT_ID`, `FIREBASE_PRIVATE_KEY`, `FIREBASE_CLIENT_EMAIL`
- `PAYMENT_PROVIDER`, `PAYMENT_KEY`, `PAYMENT_SECRET`, `PAYMENT_WEBHOOK_SECRET`
- `CORS_ORIGINS`

**Never commit `.env` or any real secret.** `.env` is gitignored.

## Security

- bcrypt password hashing (never plain text).
- JWT access/refresh tokens; refresh tokens stored hashed and revocable.
- RBAC with separately modeled permissions (extensible).
- Authorization enforced server-side on every admin endpoint.
- File uploads validated by MIME type and size, never by extension alone.
- ORM everywhere (parameterized queries → no raw SQL injection surface).
- CORS restricted to `CORS_ORIGINS`.
- Logging never includes passwords, tokens, or secrets.
- Webhooks verify gateway signatures before trust.

## Cloudflare R2 (media)

Media files are stored in R2; PostgreSQL stores only metadata (owner, name, type,
size, object key, URL). The `MediaService`/`CloudflareR2Client` abstraction lets the
storage provider be swapped later without touching business logic.

Set:

```
CLOUDFLARE_R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
CLOUDFLARE_R2_ACCESS_KEY=...
CLOUDFLARE_R2_SECRET_KEY=...
CLOUDFLARE_R2_BUCKET=jssg-media
CLOUDFLARE_R2_PUBLIC_URL=https://media.yourdomain.com
```

Wire your domain to the bucket in the Cloudflare dashboard and add a CNAME to the R2
bucket for a clean CDN-enabled public URL.

## Firebase Cloud Messaging

Notifications flow: FastAPI → FCM → Flutter.

- App registers a device token via `POST /api/v1/notifications/device-token`.
- Broadcasts to large audiences run through background workers.
- Notification history (`Notification`) is stored in PostgreSQL.

Requires `FIREBASE_PROJECT_ID`, `FIREBASE_PRIVATE_KEY`, `FIREBASE_CLIENT_EMAIL`
(or a service-account file for local dev).

## Fly.io deployment

1. Create the app and Postgres:

   ```bash
   fly launch
   fly postgres create --name jssg-db
   fly postgres attach jssg-db --app jssg-api
   ```

2. Set secrets (never in `fly.toml`):

   ```bash
   fly secrets set \
     JWT_SECRET=<long-random> \
     DATABASE_PASSWORD=<postgres-password> \
     CLOUDFLARE_R2_ACCESS_KEY=... \
     CLOUDFLARE_R2_SECRET_KEY=... \
     FIREBASE_PRIVATE_KEY='...' \
     PAYMENT_SECRET=... \
     PAYMENT_WEBHOOK_SECRET=...
   ```

3. Deploy:

   ```bash
   fly deploy
   ```

The app listens on `0.0.0.0:8000` with a `/health` HTTP check. Point Cloudflare at
the Fly.io hostname (CNAME) and enable proxy for SSL/CDN/WAF.

## Documentation / roadmap

Phases still in progress (implemented incrementally, each verified before moving on):

- [x] Phase 1 – Project setup, FastAPI, SQLAlchemy 2.x, Alembic, Docker, config
- [ ] Phase 2 – User, authentication, JWT, roles, permissions
- [ ] Phase 3 – Locations, groups, members, trustees
- [ ] Phase 4 – Family, professional info, search
- [ ] Phase 5 – Events, news, notifications
- [ ] Phase 6 – Media + Cloudflare R2
- [ ] Phase 7 – Advertisements
- [ ] Phase 8 – Fees + payments
- [ ] Phase 9 – Reports, audit logs (foundations seeded)
- [ ] Phase 10 – Tests, security review, docs
- [ ] Phase 11 – Production Docker, Fly.io, Cloudflare config