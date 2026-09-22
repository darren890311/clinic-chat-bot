"""Integration tests for the two guarantees Postgres enforces for us.

These run against a real database because they test database behaviour; an
in-memory fake would prove nothing. Skipped when TEST_DATABASE_URL is unset.

The first test in this file is the most important one in the repository. Row
level security policies are silently inert for superusers and for roles with
BYPASSRLS, and on managed Postgres the role you are given is usually one of
those. A suite that connects as that role will pass while protecting nothing,
so we assert the connection's privileges before asserting on its visibility.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

OWNER_URL = os.environ.get("TEST_DATABASE_URL")
APP_URL = os.environ.get("TEST_APP_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not (OWNER_URL and APP_URL),
    reason="set TEST_DATABASE_URL and TEST_APP_DATABASE_URL to run database tests",
)

ALPHA = uuid.UUID("11111111-1111-1111-1111-111111111111")
BETA = uuid.UUID("22222222-2222-2222-2222-222222222222")
ALPHA_DOC = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
BETA_DOC = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")

MON_9AM = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)  # 09:00 America/New_York


@pytest.fixture
def seeded():
    """Reset to two clinics, one practitioner and one appointment each."""
    with psycopg.connect(OWNER_URL, autocommit=True) as conn:
        conn.execute("TRUNCATE appointments, practitioners, clinics, audit_log CASCADE")
        for cid, slug in ((ALPHA, "alpha"), (BETA, "beta")):
            conn.execute(
                "INSERT INTO clinics (id, slug, name, timezone, scheduling_policy)"
                " VALUES (%s, %s, %s, 'America/New_York', '{}')",
                (cid, slug, slug.title() + " Dental"),
            )
        for cid, did, slug in ((ALPHA, ALPHA_DOC, "dr-hale"), (BETA, BETA_DOC, "dr-beta")):
            conn.execute(
                "INSERT INTO practitioners"
                " (id, clinic_id, slug, name, title, seniority, service_codes,"
                "  working_windows, is_active)"
                " VALUES (%s, %s, %s, %s, 'Dentist', 'senior', '{A,B,C,D,E}', '[]', true)",
                (did, cid, slug, slug.replace("-", " ").title()),
            )
            conn.execute(
                "INSERT INTO appointments"
                " (id, clinic_id, practitioner_id, service_code, starts_at, ends_at, status)"
                " VALUES (%s, %s, %s, 'A', %s, %s, 'confirmed')",
                (uuid.uuid4(), cid, did, MON_9AM, MON_9AM + timedelta(hours=1)),
            )
        yield


@pytest.fixture
def app_conn():
    with psycopg.connect(APP_URL) as conn:
        yield conn
        conn.rollback()


def scope(conn, clinic_id: uuid.UUID) -> None:
    """Mirror what tenant_session() does per request."""
    conn.execute("SELECT set_config('app.clinic_id', %s, true)", (str(clinic_id),))


def one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


# --- the guard against a false green ---------------------------------------


def test_application_role_can_actually_be_constrained_by_rls(app_conn) -> None:
    row = app_conn.execute(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    assert row == (False, False), (
        "the application role is a superuser or has BYPASSRLS; every isolation "
        "test below would pass without any policy being enforced"
    )


def test_every_tenant_table_has_rls_enabled_and_forced(app_conn) -> None:
    rows = app_conn.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class"
        " WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'"
        "   AND relname <> 'alembic_version'"
    ).fetchall()
    assert rows, "no tables found"
    unprotected = [name for name, enabled, forced in rows if not (enabled and forced)]
    assert unprotected == []


# --- isolation --------------------------------------------------------------


def test_a_session_with_no_tenant_scope_sees_nothing(seeded, app_conn) -> None:
    assert one(app_conn, "SELECT count(*) FROM appointments") == 0
    assert one(app_conn, "SELECT count(*) FROM clinics") == 0
    assert one(app_conn, "SELECT count(*) FROM practitioners") == 0


def test_a_scoped_session_sees_only_its_own_tenant(seeded, app_conn) -> None:
    scope(app_conn, ALPHA)
    assert one(app_conn, "SELECT count(*) FROM appointments") == 1
    assert one(app_conn, "SELECT clinic_id FROM appointments") == ALPHA
    assert one(app_conn, "SELECT slug FROM clinics") == "alpha"


def test_naming_another_tenant_explicitly_still_returns_nothing(seeded, app_conn) -> None:
    """The dangerous case: application code that forgets to filter, or is tricked."""
    scope(app_conn, ALPHA)
    assert one(app_conn, "SELECT count(*) FROM appointments WHERE clinic_id = %s", (BETA,)) == 0
    assert one(app_conn, "SELECT count(*) FROM practitioners WHERE id = %s", (BETA_DOC,)) == 0


def test_writing_a_row_belonging_to_another_tenant_is_rejected(seeded, app_conn) -> None:
    scope(app_conn, ALPHA)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(
            "INSERT INTO appointments"
            " (id, clinic_id, practitioner_id, service_code, starts_at, ends_at, status)"
            " VALUES (%s, %s, %s, 'A', %s, %s, 'confirmed')",
            (uuid.uuid4(), BETA, BETA_DOC, MON_9AM, MON_9AM + timedelta(hours=1)),
        )


def test_moving_an_own_row_to_another_tenant_is_rejected(seeded, app_conn) -> None:
    scope(app_conn, ALPHA)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("UPDATE appointments SET clinic_id = %s", (BETA,))


def test_the_audit_trail_is_append_only_for_the_application(seeded, app_conn) -> None:
    scope(app_conn, ALPHA)
    app_conn.execute(
        "INSERT INTO audit_log (id, clinic_id, actor, action, entity_type, detail)"
        " VALUES (%s, %s, 'bot', 'appointment.confirmed', 'appointment', '{}')",
        (uuid.uuid4(), ALPHA),
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("UPDATE audit_log SET action = 'tampered'")
    app_conn.rollback()
    scope(app_conn, ALPHA)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("DELETE FROM audit_log")


# --- overlap prevention -----------------------------------------------------


def book(conn, clinic, doc, start, end, status="confirmed"):
    return conn.execute(
        "INSERT INTO appointments"
        " (id, clinic_id, practitioner_id, service_code, starts_at, ends_at, status,"
        "  hold_expires_at)"
        " VALUES (%s, %s, %s, 'A', %s, %s, %s, %s)",
        (
            uuid.uuid4(),
            clinic,
            doc,
            start,
            end,
            status,
            start if status == "held" else None,
        ),
    )


def test_an_overlapping_booking_is_rejected_by_the_database(seeded, app_conn) -> None:
    scope(app_conn, ALPHA)
    with pytest.raises(psycopg.errors.ExclusionViolation):
        book(
            app_conn,
            ALPHA,
            ALPHA_DOC,
            MON_9AM + timedelta(minutes=30),
            MON_9AM + timedelta(minutes=90),
        )


def test_back_to_back_bookings_are_allowed(seeded, app_conn) -> None:
    """Intervals are half-open, so 09:00-10:00 and 10:00-11:00 do not collide.

    The 15-minute turnaround buffer is a clinic policy applied by the scheduling
    engine, not an integrity rule; the database only forbids true overlap.
    """
    scope(app_conn, ALPHA)
    book(app_conn, ALPHA, ALPHA_DOC, MON_9AM + timedelta(hours=1), MON_9AM + timedelta(hours=2))
    assert one(app_conn, "SELECT count(*) FROM appointments") == 2


def test_a_cancelled_appointment_releases_its_slot(seeded, app_conn) -> None:
    scope(app_conn, ALPHA)
    app_conn.execute("UPDATE appointments SET status = 'cancelled'")
    book(app_conn, ALPHA, ALPHA_DOC, MON_9AM, MON_9AM + timedelta(hours=1))
    assert one(app_conn, "SELECT count(*) FROM appointments WHERE status='confirmed'") == 1


def test_an_active_hold_blocks_the_slot(seeded, app_conn) -> None:
    """A hold is the same row shape as an appointment, so one constraint covers both."""
    scope(app_conn, ALPHA)
    app_conn.execute("UPDATE appointments SET status = 'cancelled'")
    book(app_conn, ALPHA, ALPHA_DOC, MON_9AM, MON_9AM + timedelta(hours=1), status="held")
    with pytest.raises(psycopg.errors.ExclusionViolation):
        book(app_conn, ALPHA, ALPHA_DOC, MON_9AM, MON_9AM + timedelta(hours=1))


def test_an_expired_hold_stops_blocking_once_swept(seeded, app_conn) -> None:
    """We have no background worker, so holds are swept inside the booking
    transaction itself. This is that sweep, and the slot must free up."""
    scope(app_conn, ALPHA)
    app_conn.execute("UPDATE appointments SET status = 'cancelled'")
    app_conn.execute(
        "INSERT INTO appointments"
        " (id, clinic_id, practitioner_id, service_code, starts_at, ends_at, status,"
        "  hold_expires_at)"
        " VALUES (%s, %s, %s, 'A', %s, %s, 'held', now() - interval '1 minute')",
        (uuid.uuid4(), ALPHA, ALPHA_DOC, MON_9AM, MON_9AM + timedelta(hours=1)),
    )
    app_conn.execute(
        "UPDATE appointments SET status = 'expired'"
        " WHERE status = 'held' AND hold_expires_at < now()"
    )
    book(app_conn, ALPHA, ALPHA_DOC, MON_9AM, MON_9AM + timedelta(hours=1))
    assert one(app_conn, "SELECT count(*) FROM appointments WHERE status='confirmed'") == 1


def test_two_clinics_may_book_the_same_wall_clock_time(seeded, app_conn) -> None:
    """The constraint is per practitioner, not global; tenants never collide."""
    scope(app_conn, ALPHA)
    assert one(app_conn, "SELECT count(*) FROM appointments WHERE status='confirmed'") == 1
    with psycopg.connect(APP_URL) as beta_conn:
        scope(beta_conn, BETA)
        assert one(beta_conn, "SELECT count(*) FROM appointments WHERE status='confirmed'") == 1
