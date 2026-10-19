# Deployment

Whoosh's server only does signaling, so it needs very little: one small
VPS (1 vCPU, 512 MB–1 GB RAM) running Docker. File data never touches it.

The stack is two containers, defined in `docker/compose.yaml`:

* **caddy**: terminates HTTPS (certificates obtained and renewed
  automatically) and proxies HTTP and WebSockets to the app.
* **whoosh**: the signaling server, which also serves the web app at
  `/`. Its port is never published; Caddy is the only way in (see
  [decisions](decisions.md) D30).

## Prerequisites

* A Linux VPS with Docker Engine and the Compose plugin.
* A domain name, with an **A** (and optionally **AAAA**) record pointing
  at the VPS.
* Inbound ports open: **22** (SSH), **80** and **443** TCP, **443** UDP
  (HTTP/3). Nothing else.

## First deploy

```bash
git clone https://github.com/sahil3105kumar/Whoosh.git
cd Whoosh
cp docker/.env.example docker/.env
# edit docker/.env: set WHOOSH_DOMAIN=your.domain
docker compose -f docker/compose.yaml up -d --build
```

DNS must already resolve to the server before the first start, or Caddy
can't complete the certificate challenge. It retries, but fixing DNS
first is faster.

## Verify

From your own machine:

```bash
curl https://your.domain/healthz
uv run python scripts/smoke_test.py https://your.domain
```

Open `https://your.domain` in a browser: you should see the Whoosh
home page. Then a real transfer between two machines (CLI, browser, or
one of each):

```bash
export WHOOSH_SERVER=https://your.domain
whoosh send somefile.zip           # machine A
whoosh receive <room id>           # machine B
```

## Installing the CLI

```bash
uv tool install "whoosh[cli] @ git+https://github.com/sahil3105kumar/Whoosh"
```

(`pipx install "whoosh[cli] @ git+https://..."` works too.) Set
`WHOOSH_SERVER` in your shell profile so `--server` isn't needed.

## Configuration

Set in `docker/.env`; see `docker/.env.example` and
`apps/server/config.py` for the full list.

| Variable | Default | |
|---|---|---|
| `WHOOSH_DOMAIN` | — (required) | Public hostname |
| `WHOOSH_ALLOWED_ORIGINS` | `https://$WHOOSH_DOMAIN` | Browser origins allowed (web client) |
| `WHOOSH_ROOM_CREATE_PER_MINUTE` | `10` | Per client IP |
| `WHOOSH_LOG_LEVEL` | `INFO` | |

## TURN relay (optional)

Some networks (symmetric NATs, strict corporate firewalls) block direct
peer-to-peer connections; Whoosh then reports "Direct connection
failed". A TURN relay fixes that by forwarding the (still encrypted)
traffic.

1. In `docker/.env` set a long random secret and the relay's URLs:

   ```bash
   WHOOSH_TURN_SECRET=<output of: openssl rand -hex 32>
   WHOOSH_TURN_URLS=turn:your.domain:3478?transport=udp,turn:your.domain:3478?transport=tcp
   ```

2. Open inbound **3478** UDP and TCP, and UDP **49160–49200** (the
   relay ports) on the VPS.
3. Start the stack with the `turn` profile:

   ```bash
   docker compose -f docker/compose.yaml --profile turn up -d --build
   ```

Credentials handed to clients expire after an hour; the secret itself
never leaves the server. To also hide peers' IP addresses from each
other (at the cost of relaying everything), set
`WHOOSH_ICE_TRANSPORT_POLICY=relay`.

## Operating

```bash
docker compose -f docker/compose.yaml logs -f whoosh   # app logs
docker compose -f docker/compose.yaml ps               # health
git pull && docker compose -f docker/compose.yaml up -d --build   # update
```

**Restarts drop rooms.** Room state is in memory (D9). Peers who are
already connected peer-to-peer are unaffected; rooms still waiting for
someone to join are lost, and those users need to create a new one.

Certificates live in the `caddy_data` volume. Don't delete it, or Caddy
re-requests certificates and can hit Let's Encrypt rate limits.

## Troubleshooting

* **Certificate errors / Caddy logs ACME failures**: DNS doesn't point
  at this server yet, or ports 80/443 are blocked.
* **"Direct connection failed"**: signaling worked but the peers can't
  reach each other. Enable the TURN relay (above), and check its ports
  are open.
* **Rate limit hits everyone at once**: the app is seeing Caddy's IP
  instead of clients'. Check the app port isn't being reached some other
  way and that uvicorn still runs with `--proxy-headers` (Dockerfile).
