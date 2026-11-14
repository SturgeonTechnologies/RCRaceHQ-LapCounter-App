# Transponder learning

Admin → **Transponders** in the lap counter web app. A pure RC4 transponder
sends a scrambled code, not its number, so OpenStint has to learn it once and
keep a `.rc4` file for it. This page drives that and files the result under
the number printed on the label. RC3 and RC4 Hybrid transponders send their
own number; the same page just adds them to the list.

It replaces running `openstint-learn` over SSH (that helper, from the
`openstint_decoder` role in
[RCRaceHQ-LapCounter-Install](https://github.com/SturgeonTechnologies/RCRaceHQ-LapCounter-Install),
still works and follows the same rules).

## Using it

1. Sign in, open **Transponders** (`http://<pi>:8080/transponders`).
2. Type the **number on the label** and, optionally, the racer's name. Press
   **Learn**. This opens the Learn page for that number.
3. Turn on **only** that transponder and set the car still on the pad or the
   loop. Hands off. The page moves through the steps by itself:
   - **Place it**: waiting for the decoder to start learning.
   - **Hold still**: learning (about 12 s, 25 s for a Hybrid), with a progress
     bar and the decoder's live frame counts. If it moves, the page says so and
     the decoder retries on its own.
   - **Record**: learning finished.
4. Press **Record as <number>**. The decoder's file is renamed and the
   transponder appears in the list. The decoder picks up the new name within a
   second, no restart. Optional: pass it over the track loop and check the
   number on the live page.

**Cancel** closes the session. Starting a new Learn while another is open
cancels the open one (there is one pad).

### What the page can tell you

| Message | Meaning / what to do |
|---|---|
| Still holding its last learn | After a learn finishes, the decoder won't start another until it hears nothing for over 1 s. Lift the car off the loop (or switch the transponder off) for 2 s and set it back. Common right after **Delete** with the car still on the loop. |
| Already learned | `<number>.rc4` already exists. Press **Delete** next to it in the list to learn it again. |
| 1000-9999 are reserved | The decoder numbers new transponders 1000-9999, so labels in that range would clash. Check the label. |
| Already known as N | The decoder already has this transponder filed under another number N. Check the label, or delete `N.rc4` and learn again. |
| The decoder already sees this transponder as <number> | RC3 / Hybrid, or learned before. Record just adds it to the list (type "RC3 / Hybrid" if there is no file). |
| Nothing heard for 10 minutes | Session expired; cancel and start again. |
| Decoder files nobody recorded yet: 1000.rc4 … | Something was learned but never recorded (cancelled, or the car sat on the loop). Press **Delete** next to it, or rename it on the Pi (`mv 1000.rc4 <label>.rc4`). |
| No permission to write in /var/lib/openstint | The web container can't write the decoder's folder; see [Permissions](#permissions). |

## How it works

```
openstint ──L START / INTERRUPTED / DONE <auto_id> <n>──► lapd ──► learn_event (SQLite)
openstint ──P … <number> …──────────────────────────────► lapd ──► passing
                                                                       │
browser ◄── GET /api/learn/<id> (1 s poll) ◄── web: learning.describe() ◄┘
browser ──► POST /transponders/learn/<id>/record ──► rename <auto_id>.rc4 → <label>.rc4
                                                     + transponder row
```

- OpenStint learns by itself whenever an unknown RC4 transponder sits still;
  it writes `<auto_id>.rc4` (auto_id 1000-9999) and sends `L … DONE`. Nothing
  in the app talks to the radio.
- `lapd` logs every `L` line to `learn_event`. The web app works out the
  session's step from the events (and passings for the label number) since the
  session started, every time it's read, so there is no background job.
  Rules: first `DONE` after the start wins; `DONE` with an auto id → ready to
  record; with the label number, or a passing with the label number → already
  known; with any other number → conflict.
- **Record** renames the file (never overwrites an existing `<label>.rc4`),
  stores the file's SHA-256, and closes the session. A second click or another
  tab does nothing.

### Tables (migration 2)

| Table | Holds |
|---|---|
| `learn_event` | every `L` line: event, transponder, value, received time |
| `learn_session` | one row per Learn: label, racer name, `open` / `recorded` / `cancelled`, auto id, payloads |
| `transponder` | known transponders: number, `rc4` or `direct` (RC3/Hybrid), racer name, `.rc4` SHA-256, learned time |

### Endpoints (admin only, CSRF on POSTs)

| Method | Path | |
|---|---|---|
| GET | `/transponders` | list + start form; flags un-recorded auto files and list rows whose file is missing |
| POST | `/transponders/learn` | `label`, `racer_name` → new session, redirect to the Learn page |
| GET | `/transponders/learn/<id>` | Learn page |
| GET | `/api/learn/<id>` | session state JSON (polled every second) |
| POST | `/transponders/learn/<id>/record` | rename + record |
| POST | `/transponders/learn/<id>/cancel` | cancel |
| POST | `/transponders/<number>/delete` | delete `<number>.rc4` (if any) and the list row |

## Permissions

The decoder's folder is `/var/lib/openstint`, `root:users` mode `2775` (from
the openstint package). Recording renames files in it, so the **web**
container:

- mounts it read-write at the same path (`OPENSTINT_RC4_DIR`), and
- joins the folder's group (`group_add`, `users` = gid 100 on Debian/Ubuntu).

The `lapcounter_app` role in RCRaceHQ-LapCounter-Install sets both up; the
local `app/compose.yaml` does the same. `lapd` doesn't need the folder.

## Deleting a transponder

**Delete** (next to each row, and next to any stray `1000-9999.rc4`) removes
`<number>.rc4` from the decoder's folder and the row from the list, after a
confirm. The decoder drops a transponder whose file is gone on its next
folder scan (about a second; openstint `src/rc4.cpp`), so it can be learned
again straight away. Same permissions as Record: the app needs write access
to the folder.

## Not built yet

- The learning pad's RF relay (`learnd`): for now learn on the track loop or
  with the pad's coax plugged in by hand. Learning on the loop during a heat
  isn't blocked yet.
- Spoken "…, learned" announcement (`announcerd`).
- Uploading `.rc4` files to RC Race HQ (`syncd`).

## Missing transponders

The Transponders page lists every racer in the downloaded events whose
transponder this Pi doesn't know yet (none on the registration, or a number
that isn't recorded), grouped by event. The Events list and each event page
show the count and link here.

**Notify racers we are missing their transponder for the race** records one
request per racer still on that event's list, in the `transponder_notice` table,
and marks each row *notify requested* with the time. **No text is sent yet.**
Sending goes through RC Race HQ (which has the racers' phone numbers and SMS
opt-in) once the outbox syncs to it; `sent_at_ms` stays empty until then.
Pressing it again records another request.
