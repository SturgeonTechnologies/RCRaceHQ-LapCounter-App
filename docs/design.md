# Lap counter design (v1)

Status: draft for review. Nothing here is built yet.

## Goals

- Run a full race day with **no internet**. The Pi is the source of truth for any
  event it has checked out.
- When online, push every lap and result to RC Race HQ so people can follow live.
- Zero-config first boot: power on, join the Wi-Fi, create an admin, pair to a track.
- Learning a pure RC4 transponder is a one-time, hands-off step on a pad that ships
  with the kit. The learned file follows the transponder to every future event.

## Hardware (per kit)

| Part | Role |
|---|---|
| Raspberry Pi 4 (kit baseline) | Runs everything below |
| RTL-SDR Blog V3 | 5 MHz receiver (OpenStint, Q-branch direct sampling) |
| Track loop | Split speaker wire across the track, 470 Ω termination, 1:9 balun, coax |
| Learning pad | Cradle with a fixed coil under it (see [Learning pad](#learning-pad)) |
| RF relay (SPDT, GPIO driven) | Selects track loop or pad into the one SDR |
| Optional USB Wi-Fi dongle | Secondary internet uplink (`wlan1`) |
| Speaker / PA via 3.5 mm jack, plus HDMI | Announcer output (same audio on both, see [Announcer](#announcer)) |

## Networking

| Interface | Default role | Admin setting |
|---|---|---|
| `eth0` | **Internet uplink (required by default)** | Can be disabled |
| `wlan0` (built-in) | Hotspot / captive portal, open SSID `RC Race Event` | SSID and optional WPA2 passphrase |
| `wlan1` (USB dongle) | Off | Secondary uplink: join a track Wi-Fi network |

- `wlan0` is always the hotspot. A Pi 3 has a single radio, so it can't also be the uplink.
- If both `eth0` and `wlan1` are enabled, `eth0` wins (lower route metric) and `wlan1`
  is a failover.
- Managed with NetworkManager profiles; the web UI writes profiles through `nmcli`
  via a small privileged helper (no root in the web process).
- Captive portal: dnsmasq resolves everything to the Pi while there is no admin yet,
  and answers the OS connectivity checks (`/generate_204`, `/hotspot-detect.html`,
  `/connecttest.txt`) with a redirect so phones pop the portal page. After setup the
  connectivity checks are answered normally and the portal only appears for `http://`
  requests to unknown hosts.

## Services on the Pi

```
 openstint.service ──ZeroMQ :5556──► lapd ──► SQLite (WAL) ◄── web (Flask/gunicorn)
                                              ▲    │
                                              │    ▼
                                         learnd  syncd ──MQTT/TLS──► AWS IoT Core
                                              │
                                              ▼
                                         announcerd ──► 3.5 mm jack + HDMI
```

| Service | Responsibility |
|---|---|
| `openstint` | Decoder, unchanged upstream package. `.rc4` files in `/var/lib/openstint` |
| `lapd` | Subscribes to passings, assigns them to the live heat, computes laps, writes SQLite. Independent of the web app so a web crash never loses a lap |
| `web` | Flask under gunicorn: setup wizard, live view (Server-Sent Events), admin pages |
| `syncd` | Pulls events/lineups, pushes the outbox, uploads/downloads `.rc4` files |
| `learnd` | Drives the RF relay and the learning state machine (see below) |
| `announcerd` | Start countdown, tones and spoken calls (see [Announcer](#announcer)). Read-only on SQLite; never blocks timing |

All services share one SQLite file. Writers: `lapd`, `learnd`, `syncd`, `web`; WAL mode
plus short transactions are enough at this write rate (a few passings per second).

## First boot

1. Pi brings up hotspot `RC Race Event`. Any page redirects to **Create admin**.
2. Admin account is stored locally (argon2 hash). Local admin is independent of
   RC Race HQ so the counter works with no internet and no account.
3. After creation: the public **live view** (current heat, running order, upcoming
   heats) is open to anyone on the hotspot. Admin pages need the local login.

## Pairing to RC Race HQ

See [club-sync.md](club-sync.md) for the current club sync design (pairing, event
download, check-in, scoring, takeover); it supersedes this section and the next two
where they differ.

No RC Race HQ username/password is entered on the Pi.

1. Admin → Settings → **Link to RC Race HQ**. Requires internet.
2. Pi generates an EC P-256 key and a CSR **locally** (the private key never leaves
   the Pi), calls `POST /api/lapcounters/pair` with the CSR, and gets back a
   short code like `K7QX-4M2P` (10 min TTL) plus a polling token.
3. Admin signs in to rcracehq.com on their phone → Manage → *club* →
   **Lap counters → Add**, enters the code, picks the **track** (from the club's
   Manage → Tracks list).
4. Backend: creates IoT thing `lc-<id>`, `CreateCertificateFromCsr`, attaches the
   lap-counter IoT policy, stores `{lapCounterId, clubId, trackId, thingName}`.
5. Pi's poll returns the certificate PEM and the IoT endpoint. Pi stores them in
   `/etc/lapcounter/` (0600, owned by the `syncd` user).
6. Revoking in RC Race HQ deactivates the certificate; the Pi shows "Unlinked".

The Pi admin page then shows the linked club/track and the club's upcoming events.
Without internet it shows, in red: **"No Internet access, can't import events."**

## Cloud side: AWS IoT Core

### Topics

`rchq/<stage>/lc/<thingName>/...`

| Topic | Direction | QoS | Payload |
|---|---|---|---|
| `.../outbox` | Pi → cloud | 1 | Batch of outbox records (below) |
| `.../events/get` | Pi → cloud | 1 | Request upcoming events for the linked track |
| `.../events/accepted` | cloud → Pi | 1 | Events + heats + registrations |
| `.../ack` | cloud → Pi | 1 | Highest outbox sequence persisted |
| `$aws/things/<thingName>/shadow/...` | both | 1 | Settings + health (reported), track link (desired) |

The IoT policy scopes every resource to `${iot:Connection.Thing.ThingName}`, so a
certificate can only publish/subscribe under its own thing.

### Rules

- `.../outbox` → Lambda `ingestLapCounter`: validates the thing is linked to the
  club that owns the event, writes laps/results idempotently by record UUID,
  publishes `.../ack`.
- `.../events/get` → Lambda `lapCounterEvents`: returns events for the linked track.

### Credentials for S3

Pi uses the **IoT credentials provider** (mTLS with the same cert) with a role alias
to get short-lived AWS credentials, limited to the transponder bucket prefix. No
long-lived AWS keys on the Pi.

### Live view on rcracehq.com

v1: `ingestLapCounter` writes the latest heat state to DynamoDB; the event page polls
every 2 s while a heat is live. v2 (if needed): API Gateway WebSocket fan-out.

## Offline-first sync

- **Checkout:** when the admin picks an event to run, the Pi records a checkout
  (`events/get` with `checkout: true`). RC Race HQ marks the event as
  *Running on lap counter <name>* and makes its heats read-only until check-in.
- **Outbox:** every write that must reach the cloud appends a row:
  `{seq, uuid, type, eventId, payload, createdAt}`. `syncd` publishes in `seq` order
  in batches of ≤ 50, and deletes up to the acked `seq`. IoT Core does not queue
  messages published while the device is offline, so the outbox is the buffer.
- Server writes are idempotent on `uuid`, so a resend after a dropped connection
  never duplicates a lap.
- **Check-in:** after the last heat, admin presses **Finish event**; once the outbox
  drains the server releases the lock.

## Data model (Pi SQLite)

Mirrors RC Race HQ's `HeatRow` (`backend/src/lib/db.ts`) so a lineup built on either
side is the same data.

```
admin(id, username, pw_hash, created_at)
settings(key, value)                         -- network, link, hotspot
event(id, club_id, name, days_json, checked_out_at, finished_at)
heat(id, event_id, class_name, round_type,   -- 'qualifier' | 'main'
     round_number, day_iso, run_order, duration_s, status)  -- pending|staged|live|done
heat_entry(heat_id, racer_name, user_id, transponder, grid)
passing(id, heat_id, transponder, ts_ms, rssi, raw_line)
lap(id, heat_id, transponder, lap_no, lap_ms, passing_id)
result(heat_id, transponder, place, laps, total_ms, best_lap_ms)
transponder(number, kind, rc4_sha256, learned_at, source)  -- kind: rc3|hybrid|rc4
outbox(seq, uuid, type, event_id, payload_json, created_at)
```

RC Race HQ changes needed:
- `HeatResult` gains `userId?`, `transponder?`, `totalMs?`, `bestLapMs?`; laps go to
  a new `rcracehq-<stage>-laps` table (pk `heatId`, sk `transponder#lapNo`).
- Vehicles already carry `transponderNumber` (`VehicleRow`); registrations should
  record which vehicle (and so which transponder) a racer is running.

## Lap logic (`lapd`)

- The admin presses **Start**. That begins a **15 s countdown** that can be cancelled
  from the admin page until it reaches zero; cancelling returns the heat to *staged*.
- At zero the heat goes *live* and `lapd` records the start time from the Pi clock at
  that instant. Audio is cosmetic: the official start is never taken from when a
  sound is played or heard, so speaker or HDMI delay can't skew timing.
- The first passing per transponder after the start is the start-line crossing
  (lap 0), unless the heat uses a timed start.
- Ignore passings for the same transponder closer than `min_lap_ms` (default 4 s).
- Unknown transponders during a heat are recorded and shown to the admin to assign.
- Heat ends at `duration_s`; each car's next crossing finishes it (standard
  "time + finish lap").

## Admin UI

- **Live**: current heat, positions, last lap, best lap; upcoming heats in run order.
- **Events**: upcoming events for the linked track; select one to manage.
- **Event → Lineup**: days → heats in run order (class, Qualifier/Main N), drag to
  reorder, same structure as RC Race HQ Manage → event. Empty event = build it here.
- **Transponders**: learn, list, check which entries are missing an `.rc4`.
- **Settings**: network (eth0 uplink on/off, `wlan1` uplink SSID, hotspot SSID),
  RC Race HQ link, min lap time, software version.

## Learning pad

**Why a pad:** learning needs ~12 s (RC4) or ~25 s (Hybrid) of steady signal within
±3 dB and about 8,196 clean frames (`openstint/src/rc4.cpp`). Hand-held setups kept
interrupting; a fixed cradle makes coupling repeatable so it passes first time.

**Pad:** a car-sized tray (3D printed) with a small fixed coil a few cm under the
surface where a 1/10 transponder normally sits, an RGB LED, and a coax lead.

**One SDR, two antennas:** `learnd` switches a GPIO-driven SPDT RF relay between
the track loop and the pad. A second SDR on a Pi 3 would exceed its CPU budget.
Consequence: **learning is blocked while a heat is live**; it runs between heats.

**Flow:**
1. Admin → Transponders → **Learn**, types the number on the label.
2. If the number is already known locally or in S3 → done, nothing to learn.
3. Relay → pad. Screen and LED show: *Place car* → *Hold still* (progress bar from
   OpenStint's frame count) → *Done* or *Moved, try again*.
4. On `L … DONE <n>` the file is renamed to `<label>.rc4`, the relay switches back
   to the loop, and an upload is queued.
5. Admin confirms with one pass over the track loop (optional).

RC3 and RC4 Hybrid transponders send their real ID and skip learning.

**Storage:** `s3://rcracehq-transponders-<stage>/rc4/<number>.rc4` plus metadata
(`sha256`, `learnedBy` thing, `clubId`, timestamp). On event checkout `syncd`
downloads the files for every registered transponder so late arrivals are already
known. A transponder's RC4 code is a property of the hardware, so files are shared
across clubs.

**Abuse guard:** a number that already has a file is never overwritten by a Pi;
replacement needs an RC Race HQ admin action. Every upload records the thing that
made it.

## Announcer

`announcerd` reacts to heat and learning events that `lapd`/`learnd` write to SQLite.
It runs as its own service, so a stuck sound never delays a lap.

**Output:** the Pi 4's built-in 3.5 mm jack and HDMI play the same audio at once.
Each clip is opened on both ALSA devices (`plughw:CARD=Headphones` and
`hdmi:CARD=vc4hdmi0`) together; the two outputs stay within a few tens of ms of each
other, which is fine for calls. A USB sound card is a drop-in upgrade if the jack's
hiss is a problem on a PA.

**Sounds:**
- **Countdown:** spoken "Heat starts in 15 seconds", "10", then 5-4-3-2-1 beeps and a
  start horn at zero. Cancelling plays "Start cancelled" and stops the queue.
- **Heat end / finish:** a horn when time is up, then "Finish" as each car crosses.
- **Lap calls** (optional, per admin setting: off, every lap, best laps only, last
  minute only): "Paul, lap 5, 14.2".
- **Learning:** when `learnd` finishes, "Paul Smith, transponder 8954097, learned."
  Failure plays "Moved, try again."

**Racer names for learning:** looked up in this order:
1. A racer in the checked-out event whose vehicle has that transponder number
   (`heat_entry` / RC Race HQ `VehicleRow.transponderNumber`).
2. A name the admin types on the Learn screen (optional field next to the number).
3. Otherwise just the number: "Transponder 8954097, learned."

**Engine:** WAV clips for beeps and horns (`aplay`, near instant). Speech uses Piper
(neural TTS, faster than real time on a Pi 4) with `espeak-ng` as fallback; common
phrases and each racer's name are rendered once and cached as WAV.

**Queue:** start, cancel and finish sounds pre-empt everything. Lap calls are dropped
if they are more than 3 s old, so the announcer never falls behind the race.

**Settings:** volume, which outputs are enabled (jack / HDMI), lap-call mode, voice.

## Security notes

- Hotspot is open by default (spectators); admin pages require local login and are
  CSRF-protected; sessions are cookie-based with `SameSite=Strict`.
- Device credentials: IoT X.509 cert only, private key generated on the Pi.
- Pairing endpoint is rate-limited; codes are single-use with a 10 min TTL.

## Build order

1. `lapd` + SQLite + minimal live page against a running `openstint` (driveway test).
2. Setup wizard, local admin, lineup builder (offline only).
3. Learning flow with the pad and relay (local files only). The Learn page and
   Record step are built ([learning.md](learning.md)); the pad's RF relay is not.
   `announcerd` (countdown, start/finish, learn announcement) lands with steps 1 and 3.
4. RC Race HQ side: pairing API, IoT thing/policy, `ingestLapCounter`, laps table.
5. `syncd`: pairing, event import, outbox, S3 `.rc4` sync.
6. Captive portal + network settings; the Ansible in [RCRaceHQ-LapCounter-Install](https://github.com/SturgeonTechnologies/RCRaceHQ-LapCounter-Install) installs it all.

## Open questions

- Timed start vs first-crossing start as the default.
- Whether spectators on the hotspot should see the live view or be pointed at
  rcracehq.com once online.
