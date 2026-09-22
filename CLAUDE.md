# CLAUDE.md

Working notes for this repository. Read before making changes.

## What this is

A chat and voice appointment assistant for a three-dentist clinic, booking into
the practitioners' own Google and Outlook calendars. Built as an interview
take-home; the deliverable is a working web app plus two documents (internal
architecture/cost/security, external product/installation guide).

## Commits

- **Say what a commit will contain before making it**, including anything added
  automatically that the user did not ask for. Do not surface it afterwards.
- Do not commit or push unless asked.

## Commands

```bash
make setup    # start local Postgres, create roles, migrate, seed. Run this first.
make test     # full suite (40 tests) against a separate clinic_test database
make lint     # ruff check + format check; CI enforces both
make api      # FastAPI on :8000
make web      # Vite dev server on :5173, proxies /api to :8000
make help     # everything else
```

## Architecture invariants

Do not break these without a deliberate decision; they are the substance of the
submission.

1. **The language model never decides when an appointment happens.** It extracts
   intent and calls tools. All scheduling — which slots exist, whether a slot is
   free, who is qualified — is answered by `app/domain/scheduling.py`, which is
   pure: no database, no network, and no clock read except the `now` passed in.
   Keep it that way; it is what makes the rules exhaustively testable and what
   makes a model swap unable to change booking behaviour.

2. **Postgres enforces what application code must not be trusted with.**
   - Overlapping bookings: GiST exclusion constraint on
     `(practitioner_id, [starts_at, ends_at))`, partial on occupying statuses.
   - Tenant isolation: row level security keyed on the `app.clinic_id`
     transaction setting.

3. **Provider choices are configuration, not code.** LLM, speech and calendar
   backends sit behind ports. Adding a calendar system means one class satisfying
   `CalendarProvider`; nothing in the engine, agent or API changes.

## Row level security: the trap

RLS policies are **silently inert** for superusers and for roles with
`BYPASSRLS`. `FORCE ROW LEVEL SECURITY` subjects a table's *owner* to its
policies but does **not** constrain a superuser. On managed Postgres the role you
are given is usually the owner and often a superuser, so an isolation suite that
connects as that role passes every assertion while protecting nothing.

Therefore:

- The application connects as `clinic_app`, a non-owner role created by
  `backend/scripts/bootstrap_roles.sql`. It is `NOBYPASSRLS` and non-superuser.
- Every table is marked `FORCE ROW LEVEL SECURITY`.
- `tests/test_tenant_isolation.py` asserts the connection under test is neither a
  superuser nor `BYPASSRLS` **before** any visibility assertion. Point the suite
  at a superuser and nine tests fail, that one first. Do not weaken it.
- `bootstrap_roles.sql` must run **before** migrations; the migration grants
  privileges to the role and skips silently if it does not exist.

Tenant scope is set with `set_config('app.clinic_id', ..., true)` — the `true`
makes it transaction-local. The session-scoped form would survive the
connection's return to the pool and leak the previous request's tenant.

## Testing

- `tests/test_intervals.py` and `tests/test_scheduling.py` need no database.
- `tests/test_tenant_isolation.py` needs a real one and **truncates tenant
  tables**. It runs against `clinic_test`, never the development database. Do not
  point `TEST_DATABASE_URL` at `clinic`; `make test` handles this.
- When changing the engine, check the tests actually fail for the right reason —
  mutate the behaviour and confirm a test catches it.

## Local Postgres quirks (macOS / Homebrew)

- The server refuses to start unless the locale is pinned (`LC_ALL=C`), but
  exporting `LC_ALL` globally makes libpq hand psycopg bytes it cannot parse.
  The Makefile scopes it to the cluster commands only.
- `initdb` under the C locale defaults the cluster to `SQL_ASCII`, which causes
  the same bytes-vs-str failure. The Makefile passes `--encoding=UTF8`
  explicitly.
- Socket paths under long directories exceed the 103-byte limit, so the cluster
  runs TCP-only on port 55432.

## Stack, and what is deliberately not being built

Python 3.12 + FastAPI (async throughout), SQLAlchemy 2.0 + Alembic, Postgres,
Vue 3 + Vite, deployed as a **single container** to Cloud Run — FastAPI serves
the built frontend from `backend/static`, so production has one deploy target,
one origin and no CORS configuration.

Chosen for familiarity over novelty: the scarce resource is time to get the
scheduling and security right, not time to learn a framework.

Out of scope, by decision rather than oversight — record these in the internal
document as the scaling path rather than silently dropping them:

- Calendar change notifications (Google watch channels, Graph subscriptions).
  Free/busy is queried live per request instead, and re-queried at confirm time.
- Background job queue. Holds are swept inside the booking transaction; OAuth
  tokens refresh lazily on use.
- Full-duplex voice (WebRTC, VAD, barge-in). Voice is push-to-talk through
  swappable STT/TTS ports; LiveKit is the documented upgrade path.
- Multi-clinic administration UI. The data layer is multi-tenant; the UI is not.
- Payments, SMS/email reminders, any clinical records.

## Patient data

Contact details only — name, phone, email. **No clinical information**, which
keeps the system clear of HIPAA-regulated protected health information. Service
codes on an appointment are not diagnoses. Do not add fields that change this
without saying so explicitly.
