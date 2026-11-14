# Lap counter app

Python package `lapcounter`: the services that run next to OpenStint on the Pi.
See [../docs/design.md](../docs/design.md) for the full design. Built so far:
lapd + SQLite + live page + local admin (step 1), transponder learning
([../docs/learning.md](../docs/learning.md)) and linking to RC Race HQ
([../docs/linking.md](../docs/linking.md)).

| Service | Command | What it does now |
|---|---|---|
| `web` | `gunicorn … 'lapcounter.web:create_app()'` | First-run admin setup, admin sign-in, public live view (SSE + current heat), Admin → Transponders (learn + record), Events (download, start / end heats, qualifying), Settings → Link to RC Race HQ |
| `lapd` | `python -m lapcounter.lapd` | Subscribes to OpenStint's ZeroMQ feed (`:5556`), stores passings, decoder stats and every learning (`L`) line in SQLite |

Both share `/var/lib/lapcounter/lapcounter.db` (WAL). Heats are scored by `web`
from lapd's raw passings ([../docs/racing.md](../docs/racing.md)). Not built yet:
lineup editing on the Pi, results upload to RC Race HQ (outbox), the learning pad's RF relay, syncd, announcer, captive-portal grants
(the placeholder `race-portal` from `race_hotspot` still owns port 80).

## Run

```
docker compose up --build        # web on :8080, lapd talks to 127.0.0.1:5556
```

Without Docker:

```
pip install -e '.[test]'
LAPCOUNTER_DB=./dev.db python -m lapcounter.lapd &
LAPCOUNTER_DB=./dev.db flask --app 'lapcounter.web:create_app()' run -p 8080
```

Open `http://<pi>:8080/`. The first visit goes to **Set up the lap counter**.
Transponders are at `/transponders`, events and heats at `/events` and the RC Race HQ link at `/settings`
once you're signed in.

## Environment

| Variable | Default |
|---|---|
| `LAPCOUNTER_DB` | `/var/lib/lapcounter/lapcounter.db` |
| `OPENSTINT_ZMQ` | `tcp://127.0.0.1:5556` |
| `LAPCOUNTER_DECODER_STALE_S` | `5` (seconds without an `S` line before the decoder shows offline) |
| `OPENSTINT_RC4_DIR` | `/var/lib/openstint` (OpenStint's `.rc4` folder; the web container needs it mounted read-write, see [learning](../docs/learning.md#permissions)) |
| `RCRACEHQ_API_URL` | empty (RC Race HQ API base URL; empty turns Link off, see [linking](../docs/linking.md#configure)) |
| `LAPCOUNTER_CLOUD_DIR` | `/var/lib/lapcounter/cloud` (link key + certificate) |

## Test

```
pip install -e '.[test]' && pytest
```

## Image and deploy

CI builds a `linux/arm64` image and pushes it to GHCR
(`ghcr.io/sturgeontechnologies/rcracehq-lapcounter`,
`:sha-<7>` from `main`, `:vX.Y.Z` from tags): [../docs/build.md](../docs/build.md).
The Pi runs it through `deploy_lapcounter_app.yml` in
[RCRaceHQ-LapCounter-Install](https://github.com/SturgeonTechnologies/RCRaceHQ-LapCounter-Install).
