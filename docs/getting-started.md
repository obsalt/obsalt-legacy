# Getting started

```bash
pip install -r requirements-dev.txt
docker compose up
obsalt doctor
```

Create a connection (`POST /v1/connections`) to receive a per-tenant
`ingest_key`. Point the provider webhook at:

```
https://your-host/v1/ingest/{provider}/{ingest_key}
```

`obsalt demo` starts an in-process stack for local exploration. It is **not
for production** and data is not durable.

See [rewrite-plan.md](rewrite-plan.md) for the architecture.
