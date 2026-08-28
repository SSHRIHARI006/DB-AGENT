# Deploying the DB-Agent demo (VPS + Caddy)

How the public demo at `https://dbagent.shrihari.dev` is wired up on the Contabo VPS.

## Architecture

```text
                         INTERNET
                            │
                            │ HTTPS :443
                            ▼
                 ┌──────────────────────┐
                 │ Caddy                │
                 │ container:           │
                 │   production_proxy   │
                 └──────────┬───────────┘
                            │
                     Docker network
                       web_network
                            │
                            ▼
                 ┌──────────────────────┐
                 │ db-agent-web         │
                 │ Streamlit :8501      │
                 └──────────────────────┘
```

- Caddy (already running in Docker as `production_proxy`) terminates TLS and
  reverse-proxies to the container by Docker DNS name — no nginx, no Certbot.
- The DB-Agent container publishes **no host ports**. Streamlit binds
  `0.0.0.0:8501` *inside* the container; only Caddy can reach it, over
  `web_network`. The app is never directly reachable on the VPS IP.
- Langfuse tracing and LLM calls go outbound from the container.

## Repository state

Deploying from `~/apps/db-agent` (clone of `SSHRIHARI006/DB-AGENT`, branch `main`).
`pyproject.toml` pins setuptools package discovery to `db_agent*` so the `eval/`
script directory is not mistaken for a second top-level package — the build
fails without this.

## Before first deploy

1. Copy `.env.example` to `.env` and set values. Provider keys should be
   limited to the provider the demo actually uses; unused credentials are a
   larger blast radius, not a feature.
2. Langfuse variables are `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`,
   `LANGFUSE_HOST` (the code reads `LANGFUSE_HOST`; `LANGFUSE_BASE_URL` is
   ignored). For the US cloud region:

   ```text
   LANGFUSE_HOST=https://us.cloud.langfuse.com
   ```

3. Make sure `.env` is `chmod 600` and untracked by Git (it is gitignored).

## One-time VPS checks

- An old native Streamlit process may already be listening on `0.0.0.0:8501`.
  It is **not** the Docker container. Identify it before touching anything:

  ```bash
  sudo ss -ltnp | grep :8501
  ps -fp <PID>
  sudo readlink -f /proc/<PID>/cwd
  sudo tr '\0' ' ' < /proc/<PID>/cmdline; echo
  ```

  Only stop it once confirmed obsolete. A stray listener on `0.0.0.0:8501`
  would otherwise serve the public port directly (bad) and shadow the
  container's health endpoint (confusing).
- Confirm the shared network exists: `docker network inspect web_network`.
- Verify `dbagent.shrihari.dev` resolves to the VPS public IP.

## Deploy / update

```bash
cd ~/apps/db-agent
git pull
docker compose down
docker compose up -d --build
docker compose ps          # expect: Up (healthy)
```

Check health from inside the container:

```bash
docker compose exec db-agent \
  python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=5).read())"
```

Expected: `b'ok'`. Then verify Caddy can reach the app over `web_network`
(it can if the healthcheck passes, since both use the same container DNS).

## Caddy

Add a site block to the existing Caddy config (next to the other
`*.shrihari.dev` sites):

```caddy
dbagent.shrihari.dev {
    reverse_proxy db-agent:8501
}
```

Reload Caddy (`docker exec production_proxy caddy reload --config /etc/caddy/Caddyfile`).
Caddy obtains the TLS certificate automatically; WebSocket upgrades are
handled transparently by `reverse_proxy`.

## Verify the live demo

- [ ] Streamlit loads at `https://dbagent.shrihari.dev`
- [ ] WebSockets work (interactive queries, no reconnect loops)
- [ ] A query runs and returns rows
- [ ] A risky mutation (UPDATE/DELETE without WHERE) stops at the approval gate
- [ ] Injection attempts are blocked
- [ ] The reset button works
- [ ] Langfuse shows one trace per query:
      `orchestrator.plan_dag` → `worker.execute_task` → `gate.decision` → tool result

## Troubleshooting

- **Container exits / restarts**: check `docker compose logs db-agent`. The
  most common cause was the packaging bug (now fixed in `pyproject.toml`).
- **Healthcheck never turns green**: the healthcheck uses `python -c
  urllib.request` because the slim base image has no `curl`/`wget`.
- **`curl localhost:8501` from the VPS answers but the container is down**:
  you are talking to the stray native Streamlit process, not the container.
- **Caddy 502**: the container is not up or not on `web_network`; verify with
  `docker compose ps` and `docker network inspect web_network`.
