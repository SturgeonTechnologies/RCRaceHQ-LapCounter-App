# Club sync: RC Race HQ ↔ lap counter

How a lap counter linked to a club's track downloads that club's events, runs the
race day locally, and sends results and live timing back to RC Race HQ.
[design.md](design.md) covers the Pi itself; this doc covers the club side and
supersedes design.md's *Pairing*, *Cloud side* and *Offline-first sync* sections
where they disagree.

Status: design, agreed 2026-10-29; heat mirroring and live monitoring added
2026-10-31. Built so far: pairing (PR 1, 2) and the event download on the Pi
([event-sync.md](event-sync.md)); mirroring, checkout, outbox and the rest are not.

## Decisions

| # | Question | Decision |
|---|---|---|
| 1 | Qualifying | Rank by **best complete lap** across all qualifying rounds. Scoring is one function so it can change later (see [Scoring](#scoring)). |
| 2 | Where heats are built | **In RC Race HQ, ahead of time.** That is the starting point; once the Pi checks the event out, the race is run and edited **locally**. |
| 3 | Walk-ups | Staff can add a racer on the Pi; it syncs back as an on-site registration. The event's registration page shows a **"cars to learn"** list. |
| 4 | Pis per track | **One per track.** A spare Pi can take over a checked-out event ([Hardware failure](#hardware-failure-takeover)). |
| 5 | Transponder check-in | Racer drops the car on the learning pad at check-in. Swapping transponders between cars is fine; what matters is **transponder → racer for this event**. |
| 6 | Planning from either side | Event, classes and heats can be edited in RC Race HQ **or** on the Pi and are mirrored ([Heat mirroring](#heat-mirroring)). |
| 7 | Conflicts | **The on-site lap counter wins**, always, and especially at race time. RC Race HQ keeps the version it lost so nothing disappears silently. |
| 8 | Race time | RC Race HQ **subscribes** to the lap counter: heat state and every crossing are monitored and **recorded** in RC Race HQ as they happen, not just at the end ([Live monitoring](#live-monitoring-and-recording)). |

## Overview

```
 RC Race HQ (AWS)                                        Lap counter (Pi, per track)
 ┌──────────────────────────────┐                        ┌─────────────────────────┐
 │ Manage → club → Lap counters │ ◄─ pairing code ─────► │ Settings → Link         │
 │ events / registrations/heats │                        │                         │
 │   │ eventPack Lambda ─► S3 packs/<thing>/<event>.json ─┼─► syncd (download)      │
 │   │                          │  ◄── MQTT events/get ──┤                         │
 │ ingestLapCounter Lambda ◄────┼──── MQTT outbox ───────┤ lapd/web → outbox       │
 │   ├─► laps, heat results     │  ──── MQTT ack ───────►│                         │
 │   └─► live/<eventId> topic   │                        └─────────────────────────┘
 │ event page (spectators) ◄── live/<eventId> (read-only WebSocket, or 2 s poll v1)
 └──────────────────────────────┘
```

- **MQTT** carries small, ordered messages: download requests, the results/laps
  outbox, acks, live timing.
- **S3** carries the event pack (it can exceed MQTT's 128 KB limit) and `.rc4` files.
- No password or long-lived AWS key is ever stored on the Pi. Its only credential is
  the IoT certificate whose private key it generated itself.

## Device identity (the open question from the first proposal)

How does the backend know *which Pi* is calling? Answer: never trust a value the Pi
sends; get the thing name from something IoT enforces.

- **MQTT:** the IoT policy only lets a certificate publish under
  `rchq/<stage>/lc/${iot:Connection.Thing.ThingName}/...`. The rule passes
  `topic(4)` to the Lambda, so the thing name in the topic is authenticated.
- **S3 (event packs, `.rc4`):** the Pi gets short-lived credentials from the IoT
  credentials provider (`x-amzn-iot-thingname` header, role alias
  `rcracehq-<stage>-lapcounter`). The role's policy uses the documented
  `${credentials-iot:ThingName}` variable, e.g. `packs/${credentials-iot:ThingName}/*`.
  AWS rejects the header with 403 if it doesn't match the thing attached to the cert.

This avoids a REST API that would have to work out the caller from the STS session
name, which AWS doesn't document. **Spike in PR 1:** pair a test thing, confirm the
Pi can read its own `packs/<thing>/` prefix and gets 403 on another thing's.

## Pairing (one Pi per track)

Same flow as design.md: Pi makes key + CSR, shows code `K7QX-4M2P` (10 min,
single-use), a club manager enters it under **Manage → club → Lap counters → Add**
and picks the track. Backend creates thing `lc-<ULID>`, signs the CSR, stores:

```
rcracehq-<stage>-lapcounters
  lapCounterId (pk) | clubId (GSI ClubIndex) | trackId | thingName | name
  status: pending|active|revoked | certificateId | pairedAt | lastSeenAt
  checkedOutEventId?
```

One **active** lap counter per track. Pairing a new one to a track that already has
one asks the manager to confirm and revokes the old one (that is also the hardware
replacement path).

## Event download

As built, see [event-sync.md](event-sync.md): the Pi publishes one MQTT
message (no persistent connection) and polls an `index.json` instead of subscribing to a reply topic.
The original plan:

1. Pi publishes `.../events/get` (`{ "since": <iso> }`).
2. `lapCounterEvents` Lambda finds the club's published events for the next 30 days
   (filtered by `trackId` if the event has one), writes one pack per event to
   `s3://rcracehq-<stage>-lapcounter/packs/<thing>/<eventId>.json`, and replies on
   `.../events/accepted` with `[{eventId, title, dateISO, packKey, packEtag}]`.
3. `syncd` downloads changed packs.

**Event pack** (versioned, `schema: 1`):

```json
{
  "schema": 1, "eventId": "...", "clubId": "...", "trackId": "...",
  "title": "...", "days": ["2026-11-07"],
  "classes": [{ "name": "2WD Buggy", "qualifyingRounds": 3, "mains": 1 }],
  "entries": [{ "registrationId": "...", "racerUserId": "...", "racerName": "...",
               "racerClass": "2WD Buggy", "status": "paid",
               "vehicleId": "...", "transponder": "8954097" }],
  "heats": [{ "heatId": "...", "className": "2WD Buggy", "roundType": "qualifier",
              "roundNumber": 1, "dayISO": "2026-11-07", "order": 1,
              "entries": [{ "registrationId": "...", "grid": 1 }] }],
  "generatedAt": "..."
}
```

Before checkout, re-downloading replaces the Pi's copy. After checkout, the Pi's copy
is the truth.

## Heat mirroring

Before race day, a club can plan the event from either side. Both sides hold a copy
of the event, its classes and its heats (including **which racers are in each
heat**), and every edit is copied to the other side within seconds when online.

**What is mirrored:** event (name, days, track), classes (qualifying rounds, mains,
racers per heat), heats (class, round, day, run order, racers and grid).
Registrations stay owned by RC Race HQ; walk-ups come from the Pi.

**Directions:**
- **RC Race HQ → Pi:** after any change to an event that a linked lap counter
  covers, the backend publishes `rchq/<stage>/lc/<thing>/events/changed`
  (`{eventId, version}`). The Pi re-downloads that pack. This needs the Pi to keep
  an MQTT connection open (today it connects, publishes once and disconnects).
- **Pi → RC Race HQ:** every local edit is an outbox record (`event.upsert`,
  `heat.upsert`, `heat.delete`) applied by `ingestLapCounter`. Events created on
  the Pi get a ULID on the Pi and appear in RC Race HQ like any other event.

**Conflicts: the lap counter wins.** Each mirrored record carries `version` and
`updatedBy` (`web` or `lapcounter`).
- A Pi edit is always applied, even if the web copy changed since the Pi last saw
  it. The web copy it replaces is saved to the record's `history` and Manage shows
  *"Heat 3 was changed on the lap counter; your edit was replaced"* with a
  **View previous** link.
- A web edit to a record the Pi has changed but not yet synced is applied in the
  cloud, then overwritten when the Pi's record arrives (same notice).
- After checkout (below) web edits to heats are blocked entirely, so at race time
  there is nothing to conflict.

**Heat fill.** Both sides get a racer picker and **Auto-fill from registrations**.
The fill rule (per class, `racersPerHeat`, round 1 in registration order, later
rounds re-seeded from qualifying) is written once in TypeScript and once in Python
with a shared JSON test-case file, so both sides build the same heats.

## Live monitoring and recording

At race time RC Race HQ follows the lap counter and **records** what happens, so
the cloud has the race as it runs, not only the final results.

- **Presence:** the Pi stays connected to AWS IoT. IoT lifecycle events
  (`$aws/events/presence/connected|disconnected/<thing>`) update `lastSeenAt` and
  `online` on the lap counter row; Manage → Lap counters and the event page show
  *Online / Offline since 14:02*.
- **Heat state** (`heat.state`, outbox, stored): `staged → running → finished`
  (or `aborted`), with start time, duration and the heat's racers and transponders.
- **Crossings** (`heat.lap`, outbox, stored): one record per crossing
  (`heatId, transponder, registrationId, tsMs, lapMs, lapNo`). Stored in the `laps`
  table as they arrive, ordered by the outbox `seq`.
- **Final result** (`heat.result`, outbox, stored): the Pi's computed standings,
  which are authoritative and replace anything the cloud derived from the laps.
- **Fan-out:** `ingestLapCounter` republishes each state change and crossing to
  `live/<eventId>` for spectators. The live view reads from the stored records on
  page load, then follows the topic, so a late joiner sees the whole heat so far.

If the Pi loses internet mid-heat, the race keeps running locally; the outbox
replays the crossings in order when it reconnects and RC Race HQ fills in the gap
(the live view shows *Lap counter offline, catching up*).

## Checkout, local race management, check-in

- **Checkout:** the admin picks the event on the Pi → outbox record
  `event.checkout`. RC Race HQ marks the event *Running on <lap counter>* and makes
  its heats, entries and results **read-only** in Manage. Registrations stay open
  (late online sign-ups still flow down on the next `events/get`).
- **Locally:** heats can be edited (move racers, add a heat, reorder), racers checked
  in, walk-ups added, heats run. Every change appends an outbox record.
- **Check-in:** **Finish event** sends `event.finish`; when the outbox has drained,
  the backend releases the lock and RC Race HQ shows the final results.

### Outbox record types

| type | payload | backend effect |
|---|---|---|
| `event.checkout` / `event.finish` | `{}` | set / clear `checkedOutBy` |
| `event.upsert` | event + classes | create/update event (Pi-made events get a ULID on the Pi); Pi wins |
| `heat.upsert` | heat incl. entries | upsert heat (Pi-made heats get a ULID on the Pi); Pi wins, replaced web copy goes to `history` |
| `heat.delete` | `{heatId}` | delete heat |
| `entry.walkup` | racer name, class, optional userId, transponder | registration `status: "onsite"`, `source: "lapcounter"`, `amountCents` 0 |
| `entry.transponder` | `{registrationId, transponder}` | set the registration's transponder for this event |
| `heat.state` | `{heatId, state, startedAt, durationS, entries}` | store on the heat, republish to `live/<eventId>` |
| `heat.lap` | one crossing | write to `laps`, republish to `live/<eventId>` |
| `heat.result` | final standings | fill `HeatRow.results` (authoritative) |
| `entry.checkin` | `{registrationId, atMs}` | set `checkedInAt` / `checkedInBy` on the registration (first one wins); see [checkin.md](checkin.md) |

All idempotent on the record `uuid` and applied in `seq` order. Nothing is
live-only: everything the live view shows is also stored.

## Scoring

```python
def rank_qualifying(laps) -> list[Standing]:
    """Best complete lap across all qualifier heats in the class, fastest first.
    Ties: the lap set earlier wins. Racers with no complete lap go last, by name."""
```

- A *complete lap* is crossing to crossing within one heat, after the first
  (start-line) crossing and not shorter than `min_lap_ms`.
- Lives in one module (`app/lapcounter/scoring.py`) with tests, selected per class
  by `scoring: "best_lap"`, so laps-then-time or points can be added later without
  touching anything else.
- Mains are seeded from this ranking (TQ = pole of the A-main).

## Registration and transponders

RC Race HQ changes:
- **Car picker at registration:** the racer picks one of their Vehicles (or adds one);
  the registration stores `vehicleId` and copies `transponder`.
- **Cars to learn** (Manage → event → Registrations): lists entries whose transponder
  is missing, or is an RC4 number with no `.rc4` file in S3 yet. The Pi shows the
  same list on its Check-in page.

Check-in on the Pi:
1. Staff picks the racer; the racer drops the car on the pad (until the pad and RF
   relay are built, the car sits on the track loop instead).
2. The pad reads the transponder. RC3/Hybrid send their number; a known RC4 is
   matched against its `.rc4`; an unknown RC4 starts **Learn** with the number
   from the registration pre-filled.
3. The Pi records `entry.transponder` for **this event**. If the racer is running a
   different transponder than their registration says (borrowed or swapped), that's
   fine; the event uses the one read at check-in, and the Vehicle record is not
   changed.
4. The same transponder checked in for two racers in one class is refused.

## Hardware failure (takeover)

If the track's Pi dies mid-event:
1. Plug in the spare, run setup, **Link** it with a new code. When the manager picks
   the same track, RC Race HQ warns that `<old>` has `<event>` checked out and
   offers **Take over**.
2. Take over revokes the old certificate and moves the checkout to the new Pi.
3. The new Pi downloads a pack built from **what reached the cloud**: heats, results,
   walk-ups and check-in transponders the old Pi had already synced.
4. The new Pi's **Recovery** page lists what may be missing (the heat that was
   running, entries with no transponder) and walks staff through re-checking-in those
   racers on the pad.

Anything still in the dead Pi's outbox is lost unless its SD card is recovered; the
outbox keeps that window to seconds whenever there is internet. `.rc4` files come
back from S3, so learned transponders are not lost.

## Still open

Defaults below are what gets built unless changed:

- Pi-created events: **draft** in RC Race HQ until a manager publishes them.
- Auto-fill: **8** racers per heat; round 1 in **registration order**.
- Mains: **auto-filled** from qualifying (top N to the A-main), editable by hand.

- Payment for walk-ups (`onsite` registrations are unpaid; collect cash, or a Stripe
  link later).
- Whether a club with several tracks needs `trackId` required on events.
