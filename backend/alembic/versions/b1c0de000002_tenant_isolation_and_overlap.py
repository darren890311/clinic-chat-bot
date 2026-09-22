"""tenant isolation and overlap prevention

Two database-level guarantees that the application cannot accidentally bypass:

1. A practitioner can never hold two overlapping bookings. Enforced with a GiST
   exclusion constraint over (practitioner_id, [starts_at, ends_at)), restricted
   to rows that actually occupy the slot. This is the last line of defence behind
   the scheduling engine's own checks, and it is what makes the read-then-write
   race between two concurrent conversations safe without table locks.

   Note the constraint deliberately does NOT include the turnaround buffer.
   Overlap is an integrity concern and belongs here; buffer is a clinic policy
   that changes per tenant and belongs in the scheduling engine.

2. No session can read or write another clinic's rows. Enforced with row level
   security keyed on the `app.clinic_id` transaction setting.

   FORCE ROW LEVEL SECURITY is essential: a table's owner bypasses RLS by
   default, and on managed Postgres the role you are handed is usually the owner.
   Without FORCE, the policies below would pass their own tests and protect
   nothing. The application additionally connects as a separate non-owner role.

Revision ID: b1c0de000002
Revises: 
Create Date: 2026-09-22
"""
from __future__ import annotations

from alembic import op

revision = "b1c0de000002"
down_revision = "75d4024f5896"
branch_labels = None
depends_on = None

APP_ROLE = "clinic_app"

TENANT_TABLES = (
    "services",
    "practitioners",
    "calendar_accounts",
    "patients",
    "appointments",
    "conversations",
    "messages",
    "audit_log",
)

# Rows in these states occupy the practitioner's time. An expired hold or a
# cancelled appointment must stop blocking the slot immediately.
OCCUPYING_STATES = "'held', 'confirmed'"

TENANT_PREDICATE = "clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.execute(
        f"""
        ALTER TABLE appointments
        ADD CONSTRAINT appointments_no_overlap
        EXCLUDE USING gist (
            practitioner_id WITH =,
            tstzrange(starts_at, ends_at, '[)') WITH &&
        ) WHERE (status IN ({OCCUPYING_STATES}))
        """
    )

    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING ({TENANT_PREDICATE})
            WITH CHECK ({TENANT_PREDICATE})
            """
        )

    # The tenant table itself: a session may only see the clinic it is scoped to.
    op.execute("ALTER TABLE clinics ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE clinics FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON clinics
        USING (id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
        WITH CHECK (id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
        """
    )

    # Resolving slug -> id necessarily happens before a tenant is scoped, so it
    # goes through a narrow SECURITY DEFINER function rather than a hole in the
    # policy. It returns one id and exposes nothing else.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.resolve_clinic(p_slug text)
        RETURNS uuid
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$ SELECT id FROM public.clinics WHERE slug = p_slug AND deactivated_at IS NULL $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.resolve_clinic(text) FROM PUBLIC")

    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                GRANT USAGE ON SCHEMA public TO {APP_ROLE};
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON ALL TABLES IN SCHEMA public TO {APP_ROLE};
                GRANT EXECUTE ON FUNCTION public.resolve_clinic(text) TO {APP_ROLE};
                -- The audit trail is append-only for the application.
                REVOKE UPDATE, DELETE ON audit_log FROM {APP_ROLE};
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.resolve_clinic(text)")
    for table in (*TENANT_TABLES, "clinics"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_no_overlap")
