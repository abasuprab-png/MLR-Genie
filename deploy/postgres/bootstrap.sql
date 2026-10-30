-- Run as an authorized database administrator connected to the mlr_genie DB.
-- Supply the IAM database role with: psql -v runtime_role='service@project.iam' ...
\set ON_ERROR_STOP on

\if :{?runtime_role}
\else
  DO $$ BEGIN
    RAISE EXCEPTION 'Missing runtime_role. Pass the terraform database_iam_role output with -v runtime_role=...';
  END $$;
\endif

DO $$ BEGIN
  IF current_database() <> 'mlr_genie' THEN
    RAISE EXCEPTION 'Refusing to bootstrap outside the mlr_genie database.';
  END IF;
END $$;

BEGIN;
GRANT CONNECT ON DATABASE mlr_genie TO :"runtime_role";
CREATE SCHEMA IF NOT EXISTS mlr_genie AUTHORIZATION :"runtime_role";
GRANT USAGE, CREATE ON SCHEMA mlr_genie TO :"runtime_role";
COMMIT;
