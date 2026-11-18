"""Keep the application's tables out of Supabase's Data API, and index usage by time.

When the database is a Supabase project, Supabase publishes the `public` schema through its
Data API (PostgREST), reachable with the project's *anon* key, which is public by design: it
is compiled into the web app. Supabase's default privileges grant the `anon` and
`authenticated` roles access to every new table in `public`. Without this migration, anyone
could read every tenant's documents, chunks and messages through that API.

The application never uses the Data API: only the API server reads these tables, connected
as the table owner. So on Supabase this migration:

1. revokes every privilege of `anon` and `authenticated` on the application's tables and on
   the sequences those tables own. Nothing else in the project is touched;
2. enables row-level security on every application table, with no policies, as a second
   barrier. The owner (the API's database role) is not subject to RLS unless it is forced,
   which it is not, so the application is unaffected.

On PostgreSQL without those roles (local development, tests, other hosts) steps 1 and 2 do
nothing.

A table added later must be locked down by its own migration in the same way;
`tests/test_free_deployment.py` fails until it is listed in `APP_TABLES` here.

It also adds an index on `usage_events.created_at` for the deployment-wide budget check.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_TABLES = (
    "alembic_version",
    "audit_events",
    "conversations",
    "document_chunks",
    "document_segments",
    "documents",
    "ingestion_jobs",
    "messages",
    "rate_limit_buckets",
    "usage_alerts",
    "usage_events",
    "workspace_member_quotas",
    "workspace_members",
    "workspace_quota_policies",
    "workspaces",
)

# Runs only where Supabase's API roles exist. Tables that do not exist are skipped.
LOCKDOWN = """
DO $$
DECLARE
    app_tables text[] := string_to_array(:'tables', ',');
    api_role text;
    table_name text;
    sequence_name text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN ('anon', 'authenticated')) THEN
        RETURN;  -- not Supabase: nothing to do
    END IF;
    FOREACH table_name IN ARRAY app_tables LOOP
        IF to_regclass('public.' || quote_ident(table_name)) IS NULL THEN
            CONTINUE;
        END IF;
        FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
                EXECUTE format('REVOKE ALL ON TABLE public.%I FROM %I', table_name, api_role);
                -- Only sequences owned by this table's columns, nothing else in the schema.
                FOR sequence_name IN
                    SELECT s.relname FROM pg_class s
                    JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a', 'i')
                    WHERE s.relkind = 'S'
                      AND d.refobjid = ('public.' || quote_ident(table_name))::regclass
                LOOP
                    EXECUTE format(
                        'REVOKE ALL ON SEQUENCE public.%I FROM %I', sequence_name, api_role
                    );
                END LOOP;
            END IF;
        END LOOP;
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name);
    END LOOP;
END
$$;
"""


def upgrade() -> None:
    # psql-style :'tables' is not available here; substitute the literal list safely (the
    # names are fixed identifiers defined above, not input).
    op.execute(LOCKDOWN.replace(":'tables'", "'" + ",".join(APP_TABLES) + "'"))
    op.create_index("ix_usage_events_created", "usage_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_usage_events_created", table_name="usage_events")
    # The privilege lockdown is deliberately not undone: re-granting the Data API roles
    # access would expose every tenant's data again.
