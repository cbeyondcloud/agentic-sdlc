"""Dashboards (M2-37, M2-38): the SQL views Grafana reads, the gateway link they read usage
through, the tenant-bound reader roles, and the Grafana dashboards as data.

`views` builds the view DDL (run by migrations and by `link`); `link` points the usage
contract views at the gateway's tables through `postgres_fdw`; `readers` binds one Grafana
reader role to one tenant; `catalog` renders the dashboards' JSON."""
