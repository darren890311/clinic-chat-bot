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

`backend/.env` is required and gitignored; `backend/.env.example` is the
template. Without `ANTHROPIC_API_KEY` the conversation endpoints return a
polite unavailable message rather than failing, so a missing key looks like a
working app with a broken model.

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

4. **Nothing is booked on speech alone.** Speech recognition misreads names,
   dates and digits, and a booking has consequences. The agent may place a hold
   from a spoken agreement, but the appointment is only confirmed when the
   patient acts on a confirmation card they can read. See the voice contract
   below.

5. **External calendars are read as well as written.** Free/busy is pulled into
   the engine so the bot respects commitments it did not create; confirmed
   appointments are pushed back out as events. Our database remains the single
   source of truth for appointments — the calendars are a busy source and a
   mirror, not a peer.

   The `CalendarProvider` port exposes no way to read event titles, attendees or
   descriptions, only opaque busy intervals. A booking assistant has no business
   knowing who a dentist is meeting, and untrusted calendar text never enters a
   model's context.

6. **The cached prompt prefix must stay byte-identical.** Providers cache by
   prefix, so one changing character in the system prompt discards the cached
   work for everything after it. Nothing fails when a cache misses — there is
   no error, only a larger bill and a slower reply — so this cannot be caught
   by running the app.

   Volatile per-turn material goes through the `context` parameter on
   `LLMProvider.complete`, which sits after the cache boundary. Never put a
   timestamp, a request id, or anything else that varies into `system`.
   `tests/test_agent.py` asserts the prompt is identical across two turns three
   hours apart; do not weaken it.

7. **A conversation's order is data.** Messages of one agent turn are written in
   a single transaction and share a `now()` timestamp, so they are ordered by
   `messages.seq`, an identity column. Ordering a transcript by `created_at`
   gives a scrambled conversation that providers reject.

## Voice interaction contract

Voice is **in-app push-to-talk**, not telephony. The brief asks for a working
web app and never mentions phone numbers; the agent is transport-agnostic, so
adding SIP later is a new channel rather than a rewrite.

Every voice turn must also be visible, because speech is lossy and cannot be
re-read:

- **Show what was heard.** The recognised text appears on screen so a patient
  catches a misreading immediately, rather than after it has been acted on.
- **Speak and show the reply.** Audio plus text, always both.
- **Let the patient stop the audio mid-sentence.** Listening to eight offered
  times when the second one was right is the fastest way to lose someone. This
  is distinct from mute, which is a session-level preference.
- **Confirm on a card, not by voice.** Before anything is booked, show service,
  practitioner, date and time, and require an explicit action. This maps onto
  the two-phase booking already implemented: spoken agreement creates the hold,
  the card confirms it.
- **Show the hold counting down.** The TTL is real; make it visible so the
  patient knows the slot is theirs and that it will not wait forever.
- **Type the phone number.** Digits are where recognition fails most and where
  a mistake is least recoverable — a wrong number means a patient who cannot be
  reached. The name is editable on the confirmation card for the same reason.

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
- **Assert the reason, not the status code.** This has caught three tests that
  passed for the wrong reason: a retried confirmation that was saved by the
  hold's status rather than the idempotency key, an expired OAuth state whose
  request failed later for unrelated reasons, and a stale hold. Every one was
  green with the feature deleted. `status_code == 400` is satisfied by any
  failure; the message text is satisfied by one.
- Commit messages containing shell metacharacters go through
  `git commit -F -` and a quoted heredoc. Backticks inside `-m "..."` are
  executed by the shell and their words vanish from the message.

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

The default model is `claude-opus-5`. `claude-sonnet-5` is roughly 2.5x cheaper
and the switch is one environment variable. Opus is kept because the model's
hardest job is recognising an emergency in a patient's own words, not
scheduling — the engine does the scheduling — and that is not where to save
$10 a month. See NOTES.md for the figures.

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
