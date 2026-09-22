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

-- No schema-creation rights, no ability to opt out of RLS.
ALTER ROLE clinic_app NOBYPASSRLS NOCREATEDB NOCREATEROLE NOSUPERUSER;
REVOKE CREATE ON SCHEMA public FROM clinic_app;

-- Tables created by future migrations should be reachable without another
-- bootstrap run. Owner-scoped so it applies to what the migration role creates.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO clinic_app;

SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'clinic_app';
