# Engineering notes

Problems encountered while building this, what caused them, and what was done
about them. Kept because several of these are not obvious from reading the
finished code, and because the fixes are the reasoning behind decisions the
architecture document only states.

Each entry is something that actually happened, not a precaution copied from a
checklist. Where something was anticipated rather than hit, it says so.

---

## Security

### A superuser bypasses row level security even with FORCE

**Symptom.** With RLS policies in place and every table marked
`FORCE ROW LEVEL SECURITY`, an insert that named no tenant still succeeded.

**Cause.** `FORCE ROW LEVEL SECURITY` subjects a table's *owner* to its
policies. It does nothing to a superuser, who bypasses RLS unconditionally. The
connection under test was `postgres`.

**Why it matters.** An isolation test suite connected as that role passes every
assertion while protecting nothing. The policies look correct, the tests are
green, and the tenant boundary does not exist.

**Fix.** The application connects as `clinic_app`, a role that is neither a
superuser nor `BYPASSRLS`, created by `backend/scripts/bootstrap_roles.sql`. The
first test in `tests/test_tenant_isolation.py` asserts the privileges of the
connection under test before any visibility assertion runs:

```python
row = app_conn.execute(
    "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
).fetchone()
assert row == (False, False)
```

Pointed at a superuser, that test fails first and the rest follow.

### Neon's default role carries BYPASSRLS

**Symptom.** None — this was found by checking rather than by failing, which is
the point.

**Cause.** The connection string Neon hands you on project creation uses
`neondb_owner`. That role is not a superuser, but it is a member of
`neon_superuser`, and inherits `BYPASSRLS` from it:

```
rolsuper     = false
rolbypassrls = true
```

**Why it matters.** Using the provided connection string for the application —
the obvious thing to do — makes every policy in the schema inert. `FORCE ROW
LEVEL SECURITY` does not help, because `BYPASSRLS` takes precedence over it.
Managed Postgres hands you an over-privileged role by default and does not warn
you.

**Fix.** `clinic_app` is created explicitly and verified to inherit nothing
privileged. Verified against Neon (Postgres 18.6): the isolation suite passes
14/14 as `clinic_app`, and fails 9/14 as `neondb_owner`, the privilege guard
first, with `assert (False, True) == (False, False)`.

### Creating the first tenant is blocked by its own policy

**Symptom.** Seeding a new clinic failed with
`new row violates row-level security policy for table "clinics"`.

**Cause.** The policy on `clinics` is
`WITH CHECK (id = current_setting('app.clinic_id'))`. Provisioning happens
before any tenant scope exists, so the check has nothing to compare against.

**Fix.** Generate the tenant's UUID client-side, scope the transaction to it,
then insert:

```python
clinic_id = uuid.uuid4()
await _scope(session, clinic_id)      # set_config('app.clinic_id', <id>, true)
await session.execute(insert_clinic, {"id": clinic_id, ...})
```

The check then passes on its own terms. No policy exemption, no `SECURITY
DEFINER` escape hatch, no separate provisioning role. You may create exactly the
tenant you have already scoped yourself to.

Resolving a slug to an id has the same ordering problem and is solved
differently, with a narrow `SECURITY DEFINER` function that returns one id and
exposes nothing else — a deliberate hole of known shape rather than a gap in the
policy.

### Workload Identity Federation without an attribute condition trusts every repository

**Symptom.** Anticipated, not hit.

**Cause.** An OIDC provider created without `--attribute-condition` accepts any
valid token from the issuer. GitHub's issuer signs tokens for every repository
on GitHub, so any workflow anywhere could present one and impersonate the
deployer service account.

**Fix.** The provider is created with

```
--attribute-condition="assertion.repository == 'darren890311/clinic-chat-bot'"
```

and the `workloadIdentityUser` binding is scoped to
`attribute.repository/<repo>` rather than to the whole pool.

### Cloud Run defaults to an over-privileged identity

**Cause.** A service deployed without `--service-account` runs as the Compute
Engine default service account, which by default holds `roles/editor` on the
project — enough to read every secret, modify every resource, and deploy.

**Fix.** Two service accounts with disjoint permissions:

| Identity | May | May not |
|---|---|---|
| `github-deployer` | push images, deploy revisions | read secrets |
| `clinic-runtime` | read the seven runtime secrets | deploy, touch other resources |

`deploy.yml` pins both the service and the migration job to `clinic-runtime`.

### Secrets in environment variables are visible after deployment

Passing credentials with `--set-env-vars` puts them in the Cloud Run service
configuration, in `gcloud run services describe` output, and in the deployment
logs. They are read from Secret Manager with `--set-secrets` instead, and the
runtime identity holds `secretAccessor` on exactly those seven secrets.

---

## Correctness

### The turnaround buffer is policy; overlap is integrity

The database constraint forbids true overlap only:

```sql
EXCLUDE USING gist (
    practitioner_id WITH =,
    tstzrange(starts_at, ends_at, '[)') WITH &&
) WHERE (status IN ('held', 'confirmed'))
```

The 15-minute gap between appointments is applied by the scheduling engine, not
by the constraint. Two reasons: the buffer is per-clinic configuration that will
differ between tenants, and encoding it in the constraint would make a schema
migration out of a settings change. Intervals are half-open, so 09:00–10:00 and
10:00–11:00 do not collide at the database level; the engine is what keeps them
apart in practice.

### Holds and appointments share a table so one constraint covers both

A hold in a separate table could not participate in the same exclusion
constraint, and a cross-table overlap check is not expressible as a constraint
at all. They are one table with a `status`, and the constraint's `WHERE` clause
lists the states that occupy a practitioner's time.

Expiry is swept inside the booking transaction rather than by a background
worker:

```sql
UPDATE appointments SET status = 'expired'
WHERE status = 'held' AND hold_expires_at < now()
```

A constraint cannot reference `now()`, so an expired hold would otherwise block
its slot until something noticed. There is no job queue in this system by
design, and this is the reason it does not need one.

### Daylight saving moves the UTC offset, not the working day

Working hours are stored as local wall-clock times and projected onto real dates
in the clinic's timezone. Nine in the morning stays nine in the morning across a
transition; its UTC instant moves.

`tests/test_scheduling.py` asserts both sides of the 2026-03-08 transition:
Monday the 2nd opens at 14:00Z (EST) and Monday the 9th at 13:00Z (EDT), with
the same number of slots on each.

Slot alignment is computed on naive local minutes before converting, because
subtracting two aware datetimes across a transition yields elapsed absolute time
rather than wall-clock time, which would misalign the grid on that day.

### Free/busy is re-checked at confirm time

This deployment queries `freeBusy` live instead of subscribing to calendar
change notifications. The cost of that choice is a race: a practitioner can
accept a meeting in Outlook while the patient is still talking. `confirm`
therefore re-queries and re-validates the slot before writing, with the
exclusion constraint as the last line of defence behind it.

### Tests that verify nothing

After each scheduling rule was implemented, the behaviour was deliberately
broken to confirm a test caught it:

| Mutation | Tests that failed |
|---|---|
| `policy.buffer` → `timedelta(0)` | 2 |
| skip subtracting busy intervals | 5 |
| remove the final sort | 1 |

A green suite that survives its own subject being broken is not evidence of
anything.

---

## Tooling

### The isolation suite truncates tenant tables

**Symptom.** Demo data disappeared after running `make test`.

**Cause.** `tests/test_tenant_isolation.py` resets state with `TRUNCATE`, and
`TEST_DATABASE_URL` pointed at the development database.

**Why it matters.** The failure mode is running the suite shortly before a demo
and losing the data the demo depends on.

**Fix.** `make test` creates and migrates a separate `clinic_test` database and
points the suite at that. The same separation is used against Neon: the
isolation tests run in `clinic_test`, never in `neondb`.

### ALTER ROLE ... NOSUPERUSER aborts the bootstrap on managed Postgres

**Symptom.** `permission denied to alter role` on Neon, after which none of the
`GRANT` statements ran.

**Cause.** The script defensively set `NOBYPASSRLS NOCREATEDB NOCREATEROLE
NOSUPERUSER` on the new role. Changing the `SUPERUSER` attribute requires
superuser, which managed Postgres does not grant.

**Fix.** A freshly created role already lacks all of those, so the script now
asserts rather than alters, and raises if the platform granted more than was
asked for. That is the stronger check anyway — it is what would catch a platform
that auto-grants a privileged group to new roles.

### initdb under the C locale produces a SQL_ASCII cluster

**Symptom.** `TypeError: cannot use a string pattern on a bytes-like object`
from SQLAlchemy while parsing the server version.

**Cause.** Homebrew Postgres on macOS refuses to start unless the locale is
pinned (`postmaster became multithreaded during startup`), but `initdb` under
`LC_ALL=C` defaults the cluster to `SQL_ASCII`. libpq then hands the driver
bytes where it expects `str`.

**Fix.** The Makefile passes `--encoding=UTF8 --locale=C` explicitly, and scopes
`LC_ALL` to the `initdb`/`pg_ctl` invocations rather than exporting it — a
global export reintroduces the same failure from the client side.

Related: Unix socket paths are limited to 103 bytes, which a long project path
exceeds, so the local cluster runs TCP-only on port 55432.

### Use Neon's direct endpoint, not the pooled one

**Symptom.** Anticipated, not hit.

**Cause.** The pooled endpoint (`-pooler` in the hostname) routes through
pgbouncer in transaction pooling mode. asyncpg uses prepared statements by
default, which collide there —
`prepared statement "__asyncpg_stmt_1__" already exists`.

**Fix.** The direct endpoint is used throughout. Using the pooled one later
requires `statement_cache_size=0` on the connection.

### A block scalar ends at the first column-0 line

**Symptom.** `Invalid workflow file: You have an error in your yaml syntax on
line 63`, which appeared only after pushing.

**Cause.** Six continuation lines of a long `gcloud` argument were written
flush-left inside a `run: |` block to keep them short. A line at column 0
terminates the block and makes the rest of the file unparseable.

**Fix.** The argument is assembled into a shell variable on properly indented
lines. Workflow files are now validated locally before pushing:

```bash
ruby -ryaml -e "YAML.load_file('.github/workflows/deploy.yml')"
```

### B008 is a false positive on FastAPI

`ruff`'s "do not perform function call in argument defaults" fires on
`Depends()` and `Query()`, which is FastAPI's intended idiom rather than the
mutable-default bug the rule targets. Scoped to `app/api/*.py` in
`pyproject.toml` rather than disabled globally.

---

## Infrastructure

### A billing account caps how many projects it can fund

`gcloud billing projects link` failed with
`Cloud billing quota exceeded`. The limit is on the *number of linked projects*
(5 by default), not on spend — five idle projects exhaust it as readily as five
busy ones. Freed by unlinking a dormant project; the alternative is a quota
increase request, which is not same-day.

### Image tags accumulate

Every push creates a new Artifact Registry tag. Layers are shared, so the cost
does not multiply by the image size, but it does grow. A cleanup policy
retaining the most recent versions belongs here before this runs for long.

---

## Still open

- The four API-key secrets hold placeholder values and must be filled before the
  agent ships.
- No cleanup policy on the image repository yet.
- Calendar change notifications, a background job queue and full-duplex voice
  are deliberately out of scope; the architecture document records them as the
  scaling path rather than as omissions.
