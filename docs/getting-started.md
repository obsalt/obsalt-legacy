# Getting started

```bash
pip install obsalt obsalt-vapi obsalt-retell
docker compose up -d
obsalt init
obsalt doctor
obsalt serve
```

Create a Vapi connection (hashed ingest key, envelope-encrypted secret) via `POST /v1/connections` or the settings UI. Point Vapi at:

```
POST /v1/ingest/vapi/{ingest_key}
```

Authentication is fail-closed. An empty secret is not "skip verification."

Then open `http://localhost:8080/v1/ui`.

`obsalt demo` is the same compose stack with a banner: **not for production, data is not durable.**
