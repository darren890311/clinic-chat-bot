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

### A transaction timestamp cannot order a conversation

**Symptom.** A test replaying a two-turn transcript got the messages back in
the wrong order.

**Cause.** One agent turn writes the patient's line, the assistant's, and every
tool result inside a single transaction. `now()` returns the transaction start
time, so all of them shared a `created_at` and `ORDER BY created_at, id` fell
through to a random UUID.

**Why it matters.** A scrambled transcript hands the model tool results before
the calls that produced them, which providers reject outright. It is not a
display problem: the order *is* the conversation.

**Fix.** `messages.seq`, a `BIGINT GENERATED ALWAYS AS IDENTITY`. Identity is
per-insert rather than per-transaction, so it orders correctly no matter how
many rows one turn writes. `clock_timestamp()` — used for `audit_log` — would
have worked for ordering too, but a sequence cannot collide.

### An idempotency key that was both too long and wrong

**Symptom.** `value too long for type character varying(64)` on the first
confirmation that went through the agent.

**Cause.** The key was `conversation:<uuid>:<uuid>`, 86 characters against a
64-character column. Every real booking would have failed.

The shortening surfaced a second problem. Including the conversation *and* the
hold meant a retry that created a fresh hold produced a different key, so the
deduplication it existed for did not apply; including only the conversation
would have merged a patient's second, deliberate appointment into their first.

**Fix.** Keyed on the hold alone. Re-confirming one hold is a retry;
confirming a second hold is a patient booking twice, and those are different
events. HTTP clients retrying a dropped response still supply their own
`Idempotency-Key` header, which is a separate concern at a separate layer.

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

### Assert the reason, not the status code

Three times now a test has passed for the wrong reason, and the shape was
identical each time: the assertion was too coarse to distinguish the path under
test from a different path with the same outcome.

| Test | Passed because | Not because |
|---|---|---|
| Retried confirmation returns one appointment | the hold was already `confirmed` | the idempotency key matched |
| Expired OAuth state is rejected | the token exchange failed for its own reasons | the state had expired |
| Stale hold is refused | — caught by mutation before it shipped | |

In each case removing the feature left the test green. The fix is the same
every time: assert the *reason*. `response.status_code == 400` is satisfied by
any failure; `"invalid or has expired" in response.text` is satisfied by one.

This is what mutation testing is for, and it is why every behavioural change in
this repository is followed by deliberately breaking it.

### Estimating tokens by character count was 57% low

The fixed prompt overhead was estimated at 1550 tokens from a
characters-divided-by-four rule. Measured against the API it is **2437**.

The direction matters: the estimate understated the waste, so it understated
what caching was worth. Cost figures in the architecture document come from
`usage` on real responses, never from a character count.

### Backticks in a double-quoted commit message are executed

`git commit -m "... the `context` parameter ..."` runs `context` as a shell
command and substitutes its output — nothing — into the message. The commit
succeeded with words silently missing.

Commit messages with any shell metacharacter go through `git commit -F -` and a
quoted heredoc.

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

## Cost and latency

### The standing prompt is resent on every call, and it is not small

The API is stateless: each call carries the system prompt, all seven tool
definitions, and the whole conversation so far. The first two are byte-identical
every time and measure **2437 tokens**. A booking conversation makes roughly
nine calls, so without caching that is about 22,000 tokens of repeated input.

Marking it cacheable, measured on three consecutive calls:

| Call | Fresh input | Cache write | Cache read |
|---|---|---|---|
| 1 | 109 | 2437 | 0 |
| 2 | 112 | 0 | 2437 |
| 3 | 115 | 0 | 2437 |

For a nine-call conversation on Claude Opus 5 that is roughly $0.117 of input
down to $0.032 — about 73% of the input cost, and 59% of the total once output
is counted. Output is not cacheable and is the larger share at this model tier,
which is why the headline saving is not the tenfold figure the per-token rates
suggest.

The latency matters more than the money. Cached tokens are not re-read, so the
prefill disappears from every turn after the first. On a voice call that is the
pause between the patient finishing their sentence and the assistant starting
its reply.

### The cache was impossible before it was enabled

The current time was appended to the system prompt, so the cached prefix changed
every minute.

Nothing fails when a cache misses. There is no error, no warning, and no
degraded behaviour — only a larger bill and a slower reply, neither of which is
visible from inside the request. A cache that silently never works is the
default outcome of writing this code without measuring it.

Volatile per-turn material now goes through an explicit `context` parameter on
the provider port, placed after the cache boundary, and the docstring says why.
A test asserts the system prompt is byte-identical across two turns three hours
apart and that the timestamp appears only in the uncached half.

*Related trap, same shape:* adding that parameter, it was named `context` —
which the existing `ToolContext` variable in the same scope then shadowed,
sending a `ToolContext` object to the API as the prompt.

### What a month costs, and where the volume number comes from

A per-conversation figure is only useful with a volume to multiply it by, and a
volume figure is only useful if it can be defended. This one is derived from the
clinic's own configuration rather than assumed.

**Capacity, from the working hours in the seed data.** Mon-Fri 09:00-18:00 plus
Sat 09:00-13:00 is 49 hours a week per practitioner, 147 across the three. With
the 15-minute turnaround, a one-hour treatment occupies 75 minutes, so one
practitioner fits 39 a week and the practice fits 117 — about **507 a month at
full occupancy**.

That ceiling is generous on purpose: it assumes every appointment is the
shortest treatment offered. A Full Mouth Restoration occupies 375 minutes, and
a week of those is 14 appointments for the whole practice. A realistic mix sits
well below 507.

**Utilisation is an assumption, and is stated as one.** No dental practice runs
at full occupancy. Rather than pick a number and defend it, the cost is a curve:

| Occupancy | Bookings/month | LLM cost/month |
|---|---|---|
| 40% | 203 | $12 |
| 55% | 279 | $17 |
| 70% | 355 | $21 |
| 85% | 431 | $26 |

At $0.06 per conversation on Claude Opus 5 with caching, the model is between
**$12 and $26 a month** across the plausible range. Switching to Claude Sonnet 5
roughly halves it.

**What this figure does not include.** Infrastructure — Cloud Run at
`min-instances 0`, Neon's free tier, Artifact Registry, seven secrets — measures
under $1 a month at this scale. Speech recognition and synthesis are not built
yet and will be charged per minute of audio, not per conversation; that is the
line item most likely to exceed the model itself once voice ships.

**And the unit is wrong in a way worth saying aloud.** The table counts
*bookings*, but the bill is per *conversation*. A patient who asks about opening
hours, cancels an appointment, or abandons halfway still costs a conversation
and produces no booking. Conversations therefore exceed bookings by some factor
this system cannot know until it has run for a month — which is why the
`conversations` table records provider, model and channel from the first day.

### Choosing the model

Default is `claude-opus-5`, at roughly $0.06 per booking conversation with
caching. `claude-sonnet-5` is about 2.5x cheaper and switching is one
environment variable, no redeploy of the image.

Opus was kept for one reason. The model's hardest job here is not scheduling —
the engine does that — it is recognising an emergency in a patient's own words:
*"my back tooth aches a bit and my face feels puffy"* must escalate, not book.
At a few hundred bookings a month the difference between the two models is
$10-20; the cost of missing that sentence is somebody's health. That is not
where to economise.

The switch is deliberately trivial so the clinic can make the opposite call.

---

## Reading the brief

Two requirements are ambiguous enough that the reading is a decision, and a
decision is worth recording.

### "Voice" means in the app, not on the phone

The brief asks for "chat and voice AI" and, under In Scope, "create the working
web app". It never mentions a phone number, telephony, SIP or PSTN.

Read as in-app push-to-talk. A phone channel would need a carrier, a number and
a telephony provider, none of which is a web app feature, and it is a large
enough requirement that it would not be carried by two words.

The agent is transport-agnostic — it holds the tools, the scope guard and the
conversation, and knows nothing about how audio reaches it — so a phone channel
is an additional front end rather than a rewrite. Recorded as the scaling path,
not built.

### "Updated in Outlook and Google Calendar" is read as both directions

Requirement 2 is phrased as a write: the schedule "should be updated in" the
calendars. Requirement 3 asks the bot to be "aware of the busy / available
schedule of the professionals".

If the calendars were only ever written to, our own database would already be
the complete picture and requirement 3 would say nothing that requirement 4
does not. Read together, the natural meaning is that a practitioner's real
diary — the surgery they scheduled themselves, the supplier meeting, the
afternoon off — is part of what "busy" means.

Built bidirectional, for three reasons:

* Writing events already requires the full OAuth flow, token storage and an API
  client. Reading free/busy is one more endpoint on a connection that has to
  exist anyway.
* Bidirectional is a superset. If the brief meant write-only, nothing is lost;
  if it meant both and we built one, a requirement is missing.
* It demonstrates better. An interviewer can put "Lunch, 12:00" in a test
  calendar and watch the bot decline that slot.

The port deliberately cannot read event titles, attendees or descriptions, only
opaque busy intervals. A booking assistant has no business knowing who a dentist
is meeting, and it means no attacker-controlled calendar text ever reaches the
model's context.

*Implication for the demo:* seed the test calendars with ordinary commitments
beforehand. With empty calendars the read path is invisible and the system looks
write-only regardless of what it does.

### Two ways the assistant told patients a free slot was taken

Both were found by using the thing rather than by a failing test, and neither
raised an error. The assistant produced a fluent, confident, wrong answer.

**A truncated tool result read as a complete one.** `find_availability`
returned the earliest six slots. Asked for 11:45, the model received a list
ending at 10:15, concluded the time was unavailable, and said so. Nothing in
the result indicated it had been cut off, so there was nothing for the model to
be suspicious of.

It now returns every start time in the range, grouped by day. A day of
quarter-hour starts is one short line of text; being able to answer "is 11:45
free?" without another round trip is worth far more than the tokens.

**Clinic-local display times beside a UTC example.** The result listed times as
`11:45 AM` and then said "pass the exact start time as an ISO datetime, for
example 2026-09-23T13:00:00+00:00". The model did the obvious thing and
combined the two: it sent `2026-09-24T11:45:00+00:00`, which is 07:45 at the
practice, before opening. The engine refused correctly, and the assistant
relayed the refusal to the patient as a fact about availability.

The instruction now matches the display — clinic local time, no offset, with a
local-format example — and the refusal message names the time it actually
understood, so a timezone mistake reads as one instead of hiding behind a
plausible business answer.

The shared lesson is about the boundary rather than either bug: **a tool result
is a prompt.** Anything ambiguous in it will be resolved by a model that has no
way to check, and the result will be delivered to a patient in a confident
voice.

### A patient's clock is not the clinic's clock

The assistant said 11:45 AM and the confirmation beneath it said 11:45 PM, for
the same appointment. The server formats in the practice's timezone; the
browser was formatting in the viewer's, which during development was Taipei.

Times are now rendered with the clinic's timezone, fetched from `/api/clinic`.
A patient is walking into a building, and the building has one clock.

### The assistant could not see what the card had done

Booking moved out of the model's reach, which left it unable to tell whether
the patient had completed the form. A patient who had just booked was told to
complete the form, because from the model's side nothing had happened.

Confirmed appointments and any live hold for the conversation are now part of
the per-turn context — the uncached channel that already carried the current
time — read from the database rather than announced by the client.

The first attempt was worse: the client sent a synthetic "Thanks, that is
booked" as though the patient had typed it. That put words in their mouth,
spent a model call to say something the server already knew, and still left the
model guessing. State the system owns belongs in the context, not in a fake
turn.

### The threat model, written out because nothing authenticates a patient

Nothing in this system asks a patient to prove who they are. There is no
login, no code sent to a phone, no secret only they know. That is a reasonable
place to land for a booking assistant a clinic puts on its website, but it has
to be said out loud, because every access decision in the system is really a
question about one of two identifiers.

**`conversation_id` is a bearer token.** The client sends it with each turn and
the server accepts it; row level security checks the clinic and nothing checks
the holder. Whoever has it can continue that conversation, and
`GET /api/conversations/{id}/bookings` returns the patient's name and phone
number to anyone who asks with it. It is a v4 UUID that is never displayed, so
the practical exposure is wherever a URL or a log line could carry it. It is
not persisted in the browser, which is why a refresh starts a new conversation
and the patient has to identify themselves again.

**A phone number is not a secret.** Whoever can say a number gets that
number's appointments — when, with which dentist — and can cancel them. There
is no second factor and no second question.

The risk is targeted, not mass. An earlier draft of this section said the
number space could be enumerated, which does not survive arithmetic: a mobile
number here is 09 plus eight digits, and at roughly a cent per probe — a
booking conversation costs about six — walking the space costs the practice
seven figures to find a few hundred patients. Nobody does that. Somebody who
already has one specific number, on the other hand, spends one cent and gets
everything the system knows about that patient.

The absence of rate limiting is still a hole; it is just a different one. Each
probe is a model call the practice pays for, so anyone can spend the clinic's
money at will. That is a cost attack, not a disclosure, and it is the cheaper
reason to add throttling.

**This risk is not new, and that is the argument.** Ring any dental practice,
give a name and a number, and the receptionist tells you the appointment and
cancels it if asked. The threat model is the one the clinic already lives
with. What changes is scale and attention: a receptionist can be talked round
once per phone call and may notice something odd about the third attempt; an
endpoint answers in parallel and notices nothing.

Two fixes at very different prices, and the cheap one is not the real one.
Throttling — per IP and per number, with a cap on consecutive misses — is a
middleware, and it addresses the cost attack and makes probing tedious. A
one-time code sent to the number before anything is disclosed is the actual
answer.

Which is what makes SMS being out of scope a security decision and not a
feature decision. Listed among the things not built, it reads as missing
reminders. But SMS is also the only channel here that can verify a phone
number, so dropping it decides that nothing authenticates a patient at all.
The honest sentence is not "we did not build SMS". It is "read and cancel
access rests on an unverified identifier, the exposure is that a named person
has a dental appointment on Thursday, the practice's phone line has the same
property today, and closing it needs a one-time code".

What the system does do is limit what there is to take. Patients are contact
details only — name, phone, email, no clinical information of any kind — so
the worst disclosure is that a named person has a dental appointment on
Thursday. The `CalendarProvider` port cannot read event titles or attendees,
so a practitioner's other commitments never enter any of this either.

### An id that cannot be guessed is not an id that has been checked

Testing what happens after a browser refresh turned up the honest answer —
the patient gives the phone number on the booking, the assistant looks it up
and moves it — and, underneath, a gap. Three tools take an appointment id:
`cancel_appointment`, and both halves of a move (`moving_appointment_id` on the
search, `replaces_appointment_id` on the hold). None of them checked whose
appointment it was. Row level security keeps an id inside its own clinic, and
within one clinic that was the only thing standing between a conversation and
any other patient's booking.

It is worth being exact about what that was worth, because the first two
accounts of it were inflated. An appointment id is a v4 UUID that never
appears in the interface — the confirmation card shows the treatment, the
practitioner, the time and the contact details, never the id — so a patient
has no way to see one and is never asked for one. An id typed by a stranger
is not a threat that exists. Nor is a model mis-copying one: a transposed
character names nothing, and the guard changes nothing about it. Nor is a
model picking the wrong appointment out of the two in front of it, because
both are in scope either way.

One concrete case survives. A patient gives a wrong number, the lookup
answers with somebody else's bookings, and their ids are now in the
conversation. The patient corrects the number; the stale ids are still in
context and were, until this, still actionable. `identified_phone` holds only
the most recent number a lookup answered on, so they go out of scope the
moment the correction lands.

The larger thing is that the rule now exists. Before, the answer to "who may
cancel this appointment?" was "whoever can produce the id", and nobody had
written that down or decided it — it was the emergent consequence of a tool
signature. It is now a named function with tests, which is the difference
between a property the system happens to have and one it is committed to.

There are exactly two honest ways for a conversation to hold an id: it booked
the appointment, which `appointments.conversation_id` already recorded, or the
patient gave the phone number it was booked under and the lookup returned it,
which was recorded nowhere. `conversations.identified_phone` records the
second, and `_in_scope` requires one of the two.

Three things worth saying about it.

**It is not authentication, and the column is not called `verified_phone`.**
Nothing sends a code to the number; the patient says it and the lookup
answers. Anyone who knows your number can still list and cancel your
appointments. Called `verified_phone`, this would read to the next person as
though something had checked it.

**The refusal is the same sentence as "no such appointment".** A distinct "that
one is not yours" answers the question of whether the id is real, which is the
only thing someone probing with ids wants to know.

**The phone is stored, not re-derived.** It could have been recovered by
reading back the `find_my_appointments` arguments from the stored tool calls.
An authorisation decision that depends on parsing message JSON is one that
breaks silently the day the message format moves.

### A true sentence that means something false

Asked for a root canal, the assistant offered "today 12:30 PM with Dr. Hale,
today 2:00 PM with Dr. Okafor, tomorrow 9:00 AM with Dr. Hale". Challenged on
it — *I thought you said 2pm is Dr. Okafor* — it explained that both were free
at 2 PM and it had held Dr. Hale for continuity. That was sound reception
instinct. But the patient had already been told, in a sentence with no false
words in it, that 2 PM was Dr. Okafor's; they reasonably read it as Dr. Hale
being busy, and only found out otherwise because they queried it.

The cause was the shape of the tool result, not the wording of the reply.
Availability came back grouped by practitioner:

    - Wednesday 23 September, Dr. Hale (dr-hale): 9:00 AM, 9:15 AM, … 3:30 PM
    - Wednesday 23 September, Dr. Okafor (dr-okafor): 9:00 AM, 9:15 AM, … 3:30 PM

Both lists were identical — every time offered was available with either
dentist — but establishing that needs a cross-reference of sixty entries. The
model did what the data made easy: it took a time off one line and attributed
it to that line's name. The question a patient actually asks is *who can see me
at 2?*, and the data was organised around the other one.

It is grouped by time now, and each day's times are grouped by the set of
people free at them, so a time appears exactly once under everyone who can take
it:

    - Wednesday 23 September:
        dr-hale, dr-okafor: 9:00 AM, 9:15 AM, … 11:15 AM
        dr-okafor: 11:30 AM, 11:45 AM, … 3:30 PM

The sets are disjoint by construction, so "who is free at 2 PM" is a lookup.
The reply became, unprompted, "9:00, 10:00 or 11:00 AM — Dr. Hale or Dr. Okafor
are free at all three."

Two decisions inside that. Names are written once per group rather than once
per time: annotating all forty quarter-hour starts said the same thing and cost
about twice the tokens (4,400 characters against 1,788 for the same diary).
But every start time is still written out literally, never collapsed into a
range — the earlier bug where the assistant told a patient 11:45 was
unavailable came from a truncated list, and a model that once stapled a UTC
example onto a clinic-local time is not one to hand arithmetic to.

The brief carries the rule as well, because the shape can only make the honest
answer easy; it cannot stop the model picking one name out of two and sounding
certain. And for a move it now says it is keeping the patient with their
existing practitioner, rather than doing it silently.

A smaller thing surfaced alongside: the result was capped at eight
day-and-practitioner groups, which with three dentists is under three days, and
the cap was silent. Grouping by day made the same cap eight days, and it says
how many more it is not showing.

### The engine could be told to ignore an appointment; the constraint could not

Moving an appointment to an adjacent time — 09:00 to 09:30 — failed. The
scheduling engine was given the id of the appointment being given up and
correctly excluded it, so it offered 09:30. Postgres then rejected the insert:
to an exclusion constraint, 09:00–10:00 and 09:30–10:30 simply overlap, and it
has no idea one is replacing the other.

The assistant relayed that as *"the system won't release your 9:00 to free up
9:30"*, which is an accurate description of a design flaw and no use to a
patient.

A receptionist moving an appointment does not cancel and rebook; she changes
the time. The equivalent here is a `superseded` state: the old appointment
steps aside in the same transaction that creates the new hold, so it no longer
occupies the slot, and the hold sweep puts it back if the move is never
confirmed. A patient who walks away mid-move still has what they arrived with.

Three smaller things surfaced underneath it.

**A rejected insert stays pending.** Translating the constraint violation into
`SlotUnavailable` left the failed row in the session's unit of work, so the
next flush retried it and the request died several steps later with an error
that named neither the slot nor the constraint. The write is now inside a
savepoint and the rejected row is expunged.

**SQLAlchemy batches UPDATEs and does not order them for you.** Restoring the
superseded appointment while the expiring hold was still marked `held` put both
updates in one flush, and the restore was issued first — straight into the slot
the hold had not yet released. The expiry is flushed before anything moves back.

**The client was keeping its own copy of the truth.** After a reschedule it
showed the replaced appointment beside its replacement, and after a name
correction it kept showing the name that had been corrected, because the
booking cards were assembled from what the browser remembered submitting. They
are read from the server now, patient details included.

### The interface promised something the assistant could not do

The confirmation card ends with *"If anything here is wrong, tell me below and
I will sort it out."* A patient who noticed their name was mistyped said so,
and the assistant escalated: *"Someone from the practice will follow up with
you to fix the name."* Then the composer locked, for a typo.

Two separate mistakes met here.

**A promise written in the UI with no capability behind it.** There was no tool
that could change a patient's contact details, so the model did the only thing
left and handed it to a human. Correcting a name is ordinary reception work on
data the patient typed thirty seconds earlier; it now has a tool, scoped to
bookings made in this conversation so that a typo fix cannot become a way to
overwrite somebody else's record.

**Escalation treated as one thing when it is three.** Being told to go to
hospital, and asking to speak to a person, genuinely hand the conversation
over — a bot answering in parallel is how a patient gets two different
answers. Declining to discuss insurance does not. The lock now applies only to
the reasons that are a handover, so a patient who asks something out of scope
can still book afterwards.

The first mistake is the one worth carrying: **UI copy is a specification.**
"I will sort it out" is a claim about the system, and nothing checks it.

### Moving an appointment is one operation, not two

Rescheduling was possible by cancelling and rebooking, which leaves a patient
holding both appointments or neither depending on where they stop. Booking
first and cancelling after risks a double booking if they walk away; cancelling
first risks losing the slot and getting nothing.

Worse, the assistant could not do it cleanly anyway. The per-turn context
described booked appointments in prose — *"Routine Cleaning with Dr. Hale on
Thursday at 2:00 PM"* — with no id, so it had no way to name the one to cancel
and fell back to asking the patient for the phone number on a booking it had
made itself a minute earlier.

A hold now records the appointment it replaces, and confirming it cancels that
one in the same transaction. The exchange happens or it does not. The context
carries ids, and the assistant is told not to cancel anything itself.

### Caution that refused the job

The first version of the urgent-symptoms rule listed "severe pain" as an
emergency. A patient who said *"my tooth hurts"* and then *"pain severe"* was
told to ring the practice, go to an emergency department if they could not, and
the conversation was locked so they could not book.

Severe toothache is the single most common reason a person contacts a dentist.
The assistant was refusing its own job at exactly the moment it was most
needed, and a practice that sent every toothache to A&E would not be a practice
for long.

The mistake was conflating **urgent** with **beyond this system**. Severe pain
is urgent — it wants the earliest appointment there is — and it is ordinary
dental work. The rule is now three ordered tiers with no overlap:

| | Example | Response |
|---|---|---|
| Hospital | swelling spreading to eye or neck, bleeding that will not stop, difficulty breathing or swallowing, blow to the head | stop, call now, escalate |
| Dentist, immediately | knocked-out adult tooth | ring the practice **and** book the earliest slot |
| Dentist, soon | severe pain, broken tooth, lost filling, localised swelling | search from today, offer the earliest |

The knocked-out tooth is the case that shows the shape of the error. The
routing question originally asked whether the injury followed an accident,
which swept up every sports knock and escalated it away from the one place that
could treat it — while re-implantation is measured in minutes.

There is a general lesson underneath. **Safety rules written without the
domain in front of you default to caution, and caution that refuses the task is
not safe, it is useless.** The list read like a first-aid leaflet rather than
something a dental receptionist would recognise, and it took using the product
to notice.

### Silence does not override the transcript

After booking moved to the card, the assistant kept insisting the appointment
was not made. The per-turn context said plainly that it was:

> *This patient has already completed the form and these appointments are
> booked: Routine Cleaning on Wednesday 23 September at 10:00 AM.*

The model read that and still answered *"Not yet — the hold isn't a booking
until you submit the form"*, then offered to cancel the booking it had just
made, as a duplicate of itself.

Two things were competing with the context, and both were louder.

**The system prompt.** It said, emphatically and in the cached prefix, that a
hold is not a booking and that "yes, book it" means pointing the patient back
at the form. It never acknowledged that the form does eventually get submitted.
The prompt now says what happens next, and that the held slot *becomes* that
appointment rather than sitting beside it.

**The assistant's own earlier words.** The transcript still contained its
`hold_slot` result — *"Held. Reserved for about 5 minutes — ask them to
confirm."* That sentence stays in the conversation forever, and omitting the
hold from the context did nothing to contradict it. The context now states the
absence outright:

> *No slot is currently held, and no confirmation form is on the patient's
> screen. Any hold you placed earlier has either become one of the booked
> appointments above or expired.*

The general point is worth more than the fix. **A model reasons over
everything in front of it, and stale text does not stop being text.** Removing
a fact from the context does not remove it from the conversation; only a newer,
explicit statement does. Where state can change behind the model's back, say
what the state *is* on every turn — including when it is nothing.

### A guardrail in the prompt is not a guardrail

The voice contract says nothing is booked without the patient acting on a
confirmation card. The agent was nonetheless given a `confirm_booking` tool, so
that rule lived only in the system prompt — a sentence the model was asked to
respect rather than a boundary it could not cross.

Removing the tool made the guarantee structural. The assistant can offer times
and hold one; `POST /api/appointments` from the card is what books, with the
name and phone number the patient typed.

**What broke, and why it is the interesting part.** Removing the tool without
updating the prompt left the model asked to obtain a confirmation it had no way
to obtain. Faced with an instruction it could not satisfy, it improvised: asked
to confirm, it called `escalate`, told the patient the practice would follow
up, and marked the conversation as needing a human. Every booking would have
ended in the practice's inbox, and the patient would have been waiting for a
call that was never scheduled.

Nothing errored. The tool call succeeded, the reply was fluent, and the
behaviour was completely wrong.

The prompt now states the constraint positively — you cannot book, a form
appears, point the patient at it — and the model says so plainly:

> *"I can't book it from my side — the appointment is only made once you
> complete the form on your screen. It's held for about five more minutes."*

The lesson is not about this tool. **The prompt and the tool list are one
artefact and have to be changed together.** A model given an instruction it
cannot carry out does not report the contradiction; it finds something else to
do.

### Nothing is booked on speech alone

Speech recognition misreads names, dates and digits, and it offers the patient
no way to re-read what was understood. A booking has consequences — someone
takes time off work for it.

So the spoken agreement places a *hold*, and the appointment is confirmed only
when the patient acts on a card showing service, practitioner, date and time.
That falls out of the two-phase booking already built for a different reason:
the hold exists because a patient needs time to decide, and it turns out to be
exactly the window in which they read the card.

The phone number is typed rather than spoken. Digits are where recognition fails
most often and where a mistake is least recoverable: a wrong number is a patient
nobody can reach.

---

## Still open

- The four API-key secrets hold placeholder values and must be filled before the
  agent ships.
- No cleanup policy on the image repository yet.
- Calendar change notifications, a background job queue and full-duplex voice
  are deliberately out of scope; the architecture document records them as the
  scaling path rather than as omissions.
