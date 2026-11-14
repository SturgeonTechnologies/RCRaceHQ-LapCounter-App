# Linking a lap counter to RC Race HQ

Club sync PR 2 ([club-sync.md](club-sync.md#pairing-one-pi-per-track)). Links
one Pi to one track of a club in RC Race HQ, so later steps can download that
club's events and send results back. This page covers using it, what it stores
and how to check it.

## Use it

1. On the Pi: **Settings → Link to RC Race HQ**. A code like `K7QX-4M2P` shows.
2. In RC Race HQ (signed in as a club manager): **Manage → your club → Lap
   counters**, type the code, pick the track, **Link**.
3. Within a few seconds the Pi's page shows **Linked to <club> / <track>**.
4. **Test connection** asks AWS IoT for short-lived keys with the new
   certificate. "Connection OK" means the link works end to end.

The code works once and expires after 10 minutes. **Cancel** throws it away.

| Button | What it does |
|---|---|
| Link to another track | New key + code. The current link keeps working until the new code is entered; cancelling leaves it as it was |
| Test connection | Certificate → IoT credentials provider. A refusal means it was unlinked in RC Race HQ |
| Remove link | Deletes the key, certificate and link settings on the Pi. **Also Unlink it in RC Race HQ**, which revokes the certificate |

Replacing a broken Pi: link the new one to the same track. RC Race HQ asks to
replace the old one and revokes its certificate.

## Configure

The Pi needs the RC Race HQ API URL in `RCRACEHQ_API_URL`. It defaults to
`https://www.rcracehq.com/api` (in `app/compose.yaml` and the Install repo's
`lapcounter_app_rcracehq_api_url`); without it the Settings page says Link
isn't configured.

## What happens

```
Pi (web)                                 RC Race HQ                         AWS IoT
--------                                 ----------                         -------
P-256 key + CSR (key stays on the Pi)
POST /lapcounters/pair {csr}      ─────▶ code + pollToken (10 min)
show code
POST /lapcounters/pair/status     ─────▶ waiting …
(every 3 s while the page is open)       manager enters code + track ──────▶ thing lc-<stage>-<id>,
                                                                             cert signed from the CSR,
                                                                             IoT policy attached
                                  ◀───── paired: certificate, thing name,
                                         club/track, endpoints, role alias,
                                         topic prefix, bucket
save cert, promote key, save link
Test connection: GET https://<credentialsEndpoint>/role-aliases/<alias>/credentials
                 (client cert, x-amzn-iot-thingname) ─────────────────────▶ short-lived AWS keys
```

Polling is done by the web app while the Settings page is open (3 s in the
browser, at most one call per 2.5 s to RC Race HQ however many tabs). Closing
the page pauses it; reopening resumes until the code expires. syncd (a later
PR) uses the stored link; it doesn't do pairing.

## What it stores

In the app's data volume (`/var/lib/lapcounter` on the Pi):

| Where | What |
|---|---|
| `cloud/device.key` | Private key (PKCS#8 P-256), `0600`, folder `0700`. Never sent anywhere |
| `cloud/device.crt` | Certificate RC Race HQ signed, `0600` |
| `cloud/pending.key` | Key of a pairing still waiting for its code; becomes `device.key` when linked |
| `settings` `cloud.link` | JSON: `lapCounterId`, `thingName`, `club`, `track`, `stage`, `region`, `iotEndpoint`, `credentialsEndpoint`, `roleAlias`, `topicPrefix`, `bucket`, `linkedAt` |
| `settings` `cloud.pairing` | JSON: `code`, `pollToken`, `expiresAt` of the open pairing |

Back up `/var/lib/lapcounter` with the database and the link survives an SD
card swap. Lose it and the Pi is linked again with a new code.

## Check it from a shell

```bash
cd /opt/lapcounter
sudo docker compose exec web sh -c 'ls -la /var/lib/lapcounter/cloud; env | grep RCRACEHQ'
sudo docker compose exec web python -c "
from lapcounter import db, cloud
print(cloud.current_link(db.connect('/var/lib/lapcounter/lapcounter.db')))"
```

## Messages

| Message | Meaning |
|---|---|
| RC Race HQ isn't configured on this Pi | `RCRACEHQ_API_URL` is empty; set `lapcounter_app_rcracehq_api_url` and redeploy |
| Can't reach RC Race HQ | No internet on the Pi. While a code is showing, the page keeps retrying |
| RC Race HQ refused the link request (HTTP 429) | The public pairing route is rate-limited (1/s); wait and try again |
| The code expired … Start again | Not entered within 10 minutes, or already used |
| RC Race HQ couldn't link it: … | RC Race HQ failed creating the thing or signing the certificate; the text is its error |
| AWS refused the certificate | Unlinked or replaced in RC Race HQ. Link again |
| The key or certificate is missing | Files under `cloud/` were deleted; Remove link, then Link again |
