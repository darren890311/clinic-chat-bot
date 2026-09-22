-- Run ONCE per database, as the owner/superuser, before the first migration.
-- Roles and passwords are deliberately not in migrations: they are cluster-level
-- objects and the password must never land in version control.
--
--   psql "$MIGRATION_DATABASE_URL" -v app_password="$(openssl rand -base64 24)" \
--        -f scripts/bootstrap_roles.sql
--
-- The application connects as clinic_app. It must NOT own any table, because a
-- table owner bypasses row level security. The migration additionally marks every
-- table FORCE ROW LEVEL SECURITY so even the owner is subject to the policies.

\set ON_ERROR_STOP on

SELECT format('CREATE ROLE clinic_app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'clinic_app')
\gexec

SELECT format('ALTER ROLE clinic_app PASSWORD %L', :'app_password')
\gexec

-- A freshly created role already lacks SUPERUSER, BYPASSRLS, CREATEDB and
-- CREATEROLE, so we verify rather than ALTER. Two reasons:
--
--   * Changing the SUPERUSER attribute requires superuser, which managed
--     Postgres does not give you. On Neon the ALTER aborts the whole script.
--   * A platform may grant more than you asked for. Neon's own neondb_owner
--     carries BYPASSRLS via the neon_superuser group; a role that inherited
--     that would make every policy in the next migration silently inert.
--
-- Failing here is the point: better a refused bootstrap than a deployment that
-- looks isolated and is not.
DO $$
DECLARE attrs record;
BEGIN
    SELECT rolsuper, rolbypassrls INTO attrs FROM pg_roles WHERE rolname = 'clinic_app';

    IF attrs.rolsuper OR attrs.rolbypassrls THEN
        RAISE EXCEPTION
            'clinic_app has SUPERUSER or BYPASSRLS; row level security would not apply to it';
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_auth_members m
        JOIN pg_roles g ON g.oid = m.roleid
        WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = 'clinic_app')
          AND (g.rolsuper OR g.rolbypassrls)
    ) THEN
        RAISE EXCEPTION
            'clinic_app inherits a group with SUPERUSER or BYPASSRLS; isolation would be inert';
    END IF;
END
$$;

REVOKE CREATE ON SCHEMA public FROM clinic_app;

-- Tables created by future migrations should be reachable without another
-- bootstrap run. Owner-scoped so it applies to what the migration role creates.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO clinic_app;

SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'clinic_app';
