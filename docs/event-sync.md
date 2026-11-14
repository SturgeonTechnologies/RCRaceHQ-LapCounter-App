# Event download (RC Race HQ → lap counter)

How a linked lap counter downloads its club's events: **Events → Download
events from RC Race HQ**. This is the download half of
[club-sync.md](club-sync.md) PR 3/4; checking an event out, running heats and
sending results back come later.

Code: [`app/lapcounter/eventsync.py`](../app/lapcounter/eventsync.py) (the
download), [`sigv4.py`](../app/lapcounter/sigv4.py) (S3 request signing),
[`web/events.py`](../app/lapcounter/web/events.py) (pages).

## How it works

```
 Pi (web container)                         AWS
 ──────────────────                         ───
 1. MQTT over TLS to <iotEndpoint>:8883, client ID = thing name,
    PUBLISH QoS 1 <topicPrefix>/events/get {"requestId": "<uuid>"},
    device cert as TLS client cert ───────────────────────────────────► IoT rule → Lambda
                                                                          │ writes packs/<thing>/<eventId>.json
                                                                          │ then packs/<thing>/index.json
 2. GET packs/<thing>/index.json every 1.5 s (≤ 25 s)   ◄────────────────┘
    until index.requestId == ours
 3. GET each pack whose sha256 changed, check the hash, store it
 4. Delete local events that aren't in the index any more
```

- **Who's asking** comes from AWS, not the Pi: the IoT policy only lets the
  certificate publish under its own `rchq/<stage>/lc/<thing>/`, and the S3 keys
  from the credentials provider can only read `packs/<thing>/`.
- **No MQTT library and no boto3** on the Pi. Step 1 is a one-shot MQTT 3.1.1
  CONNECT → PUBLISH → PUBACK → DISCONNECT written in `eventsync.py`. It has to be
  MQTT, not AWS IoT's HTTPS publish: the IoT policy scopes topics with
  `${iot:Connection.Thing.ThingName}`, which AWS only resolves when an MQTT
  client ID equals the thing name. HTTPS has no client ID, so AWS answered 403.
  S3 reads use short-lived keys from the credentials provider (the same call as
  Settings → Check connection), cached until 5 minutes before they expire, and
  signed with a small SigV4 implementation tested against AWS's own example.
- The web container only needs outbound 443 (S3 and the credentials provider)
  and **8883** (MQTT to the IoT data endpoint). Nothing new in the deploy repo.

## Contract

**index.json** (`schema: 1`):

```json
{
  "schema": 1,
  "requestId": "…",
  "generatedAt": "2026-10-31T17:00:00Z",
  "lapCounter": { "lapCounterId": "…", "name": "…", "clubId": "…", "clubName": "…",
                  "trackId": "…", "trackName": "…" },
  "events": [
    { "eventId": "…", "title": "Test Race", "dateISO": "…", "endDateISO": null,
      "location": "…", "packKey": "packs/<thing>/<eventId>.json",
      "packSha256": "<hex of the pack's bytes>", "entryCount": 6, "heatCount": 4 }
  ]
}
```

If the lap counter isn't active (unlinked, replaced), the index has
`"error": "<message>"` and no events; the Pi shows the message and keeps what
it already has.

**Pack** (`packs/<thing>/<eventId>.json`, `schema: 1`):

```json
{
  "schema": 1, "eventId": "…", "clubId": "…", "title": "…", "description": "…",
  "dateISO": "…", "endDateISO": null, "location": "…", "days": ["2026-11-07"],
  "classes": [{ "name": "2WD Buggy", "qualifyingRounds": 3, "mains": 1 }],
  "entries": [{ "registrationId": "…", "racerUserId": null, "racerName": "…",
                "racerClass": "2WD Buggy", "status": "paid", "transponder": "8954097",
                "vehicle": "Associated B6 (2WD Buggy)" }],
  "heats": [{ "heatId": "…", "className": "2WD Buggy", "roundType": "qualifier",
              "roundNumber": 1, "dayISO": "2026-11-07", "order": 1, "entries": [] }],
  "generatedAt": "…"
}
```

The Pi refuses:
- a schema other than 1 (update the lap counter app);
- a `packKey` outside `packs/<its thing>/`, or an `eventId` that isn't
  `[A-Za-z0-9_-]{1,64}`;
- a pack whose sha256 doesn't match the index;
- anything over 5 MB.

Unknown fields are ignored, so the backend can add fields without breaking older Pis.

RC Race HQ heats don't have racers assigned yet, so `heats[].entries` is empty;
the event page shows each heat's class roster ("Racers in this class") instead.

## On the Pi

- **Events** (admin): linked club/track, last download, the Download button and
  the downloaded events. Each event shows its racer count and how many
  transponders are missing, linking to the Transponders page.
- **Event page**: a **Racers** summary ("12 racers · 3 missing transponders",
  linking to the Transponders page), classes (rounds, mains, racers, missing per
  class) and heats grouped by day in run order with *learned / not learned*
  badges on each grid.
- **Transponders page → Transponders we don't know yet**: every racer across the
  downloaded events with no transponder on the registration, or whose number
  isn't recorded on this Pi (not in Known transponders and no `<number>.rc4`),
  grouped by event. **Learn** fills in the form with the number and racer name.
  Each event has a **Notify racers we are missing their transponder for the
  race** button ([learning.md](learning.md#missing-transponders)).

Storage: SQLite tables `event_pack` (one row per event, the whole pack as JSON)
and `event_entry` (entries, for the to-learn query). Schema version 3
(5 with notify requests, `transponder_notice`).

## Troubleshooting

| Message | Meaning / fix |
|---|---|
| "This Pi isn't linked to RC Race HQ yet" | Settings → Link first. |
| "AWS IoT refused the certificate" (connection closed on CONNECT or PUBLISH) | The lap counter was unlinked or replaced in RC Race HQ. Link it again. If Settings → Check connection says OK, the certificate is fine and the topic was refused: check `topicPrefix` matches the IoT policy. |
| "Can't reach AWS IoT" | Outbound TCP 8883 is blocked on the Pi's uplink. |
| "S3 refused … (403)" on a pack | Credentials provider keys rejected, or the device role doesn't cover the key. Settings → Check connection; relink if it fails. (A 403 on `index.json` while waiting is normal: S3 reports a not-yet-written key as 403 to this role, so the Pi keeps polling.) |
| "RC Race HQ didn't answer … within 25 s" | The request got to AWS IoT but no index came back: the IoT rule / event pack Lambda isn't deployed on that stage yet, or it failed (check its CloudWatch logs). |
| "RC Race HQ: …" | The backend's own error (e.g. lap counter not active). |
| "… schema N …" | The backend sends a newer format. Deploy a newer lap counter image. |
| "didn't match its checksum" | The pack changed while downloading. Download again. |

Logs: `cd /opt/lapcounter && sudo docker compose logs web | grep 'event download'`.
