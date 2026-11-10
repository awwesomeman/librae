# A Streamlit console replaces the Grafana dashboards

Date: 2026-11-10
Status: Accepted

## Context

The monitoring UI was three generated Grafana dashboards plus a Grafana
service, datasource provisioning, an admin credential and a push script in the
reference deployment. Grafana cannot call Python, so signal forward returns
had a second implementation in SQL, kept equal to `compute_signal_outcomes()`
only by golden tests. A Python UI can call the engine's own functions and
ships as an optional extra instead of a separate service.

## Decision

- Librae ships `librae/console/`, a standalone read-only Streamlit app over
  the engine tables, behind the optional `console` extra. It publishes no
  page API for other applications; another repository's console links to it
  by URL at most.
- It connects with its own DSN (`LIBRAE_CONSOLE_DSN`) for the SELECT-only
  reference role and opens its session read-only.
- Status panels refresh as Streamlit fragments; history redraws on reload.
  Alerting stays with the notifier and external health checks.
- Signal outcomes call `compute_signal_outcomes()`, so the SQL copy is gone.
- Grafana is removed from the package, the reference Compose file and
  `cloud_deploy.sh`. `deploy/` does not start the console; it runs wherever
  the database is reachable, bound to `127.0.0.1` unless told otherwise.

## Consequences

- The read-only role keeps its name, `grafana_reader`: renaming it changes
  the bootstrap's roles on every existing database, which is a migration of
  its own.
- Existing Grafana containers and volumes are left for their operators to
  remove.
