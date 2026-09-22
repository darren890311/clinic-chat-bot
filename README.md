# Clinic Appointment Assistant

A chat and voice assistant that books appointments for a three-dentist practice,
writing to the practitioners' own Google and Outlook calendars.

## The one architectural decision everything else follows from

**The language model never decides when an appointment happens.**

The model handles conversation: understanding what the patient wants, which
service that maps to, roughly when they are free, and whether the request is
something this assistant should handle at all. Every scheduling question —
which slots exist, whether a slot is still free, whether a practitioner is
qualified — is answered by [`app/domain/scheduling.py`](backend/app/domain/scheduling.py),
which is pure, deterministic and has no network or database access.

That split is what makes the system testable and what makes it safe to swap
model providers: changing the model cannot change booking behaviour.

Two further guarantees are enforced by Postgres rather than application code,
because application code is the thing most likely to contain a bug:

| Guarantee | Mechanism |
|---|---|
| A practitioner can never hold two overlapping bookings | GiST exclusion constraint over `(practitioner_id, [starts_at, ends_at))` |
| No request can read or write another clinic's rows | Row level security keyed on the `app.clinic_id` transaction setting |

## Services and competencies

| Code | Service | Duration | Who can perform it |
|---|---|---|---|
| A | Routine Cleaning | 1h | All three |
| B | Dental Examination | 1h | All three |
| C | Root Canal Treatment | 2.5h | Seniors only |
| D | Crown Fitting | 2h | Seniors only |
| E | Full Mouth Restoration | 6h | Seniors only |

Opening hours are Mon–Fri 09:00–18:00 and Sat 09:00–13:00. Note that service E
simply cannot be booked on a Saturday: the engine derives that from the duration
and the window rather than encoding it as a special case.

## Running it locally

Requires Python 3.12+, Node 22+, and PostgreSQL 16+ on your `PATH`.

```bash
python3 -m venv backend/.venv && backend/.venv/bin/pip install -e "backend[dev]"
cd frontend && npm install && cd ..

make setup     # start Postgres, create roles, migrate, seed
make test      # 40 tests, including the database isolation suite
make api       # http://localhost:8000
make web       # http://localhost:5173 (proxies /api to :8000)
```

`make help` lists everything.

## A trap worth knowing about

Row level security policies are **silently inert** for superusers and for roles
with `BYPASSRLS`, and `FORCE ROW LEVEL SECURITY` only subjects a table's *owner*
— not a superuser. On managed Postgres the role you are handed is usually the
owner, and often a superuser. An isolation test suite that connects as that role
passes every assertion while protecting nothing.

So the application connects as `clinic_app`, a separate non-owner role created by
[`scripts/bootstrap_roles.sql`](backend/scripts/bootstrap_roles.sql), every table
is marked `FORCE ROW LEVEL SECURITY`, and the first test in
[`test_tenant_isolation.py`](backend/tests/test_tenant_isolation.py) asserts that
the connection under test is neither a superuser nor `BYPASSRLS` before any
visibility assertion runs. Point that suite at a superuser and nine tests fail
immediately, starting with that one.

## Multi-tenancy

This deployment serves one clinic. The data layer is built for many: every table
carries `clinic_id`, every policy keys off it, and the isolation tests run two
tenants against each other. What is deliberately *not* built is the multi-clinic
administration UI. Adding a second clinic is a row in `clinics` plus an OAuth
connection per practitioner; it needs no schema change.

## Layout

```
backend/
  app/domain/      pure scheduling logic — no I/O, no framework
  app/db/          models, migrations, tenant-scoped sessions
  app/providers/   swappable ports: calendar, LLM, speech
  app/api/         HTTP surface
  tests/           engine tests (no DB) + isolation tests (real DB)
frontend/          Vue 3 + Vite; built into backend/static
docs/              internal and external documentation
```

## Documentation

- [`NOTES.md`](NOTES.md) — problems hit while building this and what was done
  about them, including why the application does not connect as the database
  role the provider hands you.
- `docs/internal.md` — architecture, cost of building and running in production,
  decisions made along the way, security features.
- `docs/external.md` — what the product does, and how to install and use it,
  including the steps outside the app itself.
