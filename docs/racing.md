# Running a heat

How the race director starts, watches and ends a heat on the lap counter, and how
laps and results are worked out. Timing rules are in
[`app/lapcounter/race.py`](../app/lapcounter/race.py); qualifying order in
[`app/lapcounter/scoring.py`](../app/lapcounter/scoring.py); pages in
[`app/lapcounter/web/race.py`](../app/lapcounter/web/race.py).

Results are kept on the Pi and, when it's paired, sent to RC Race HQ through
the outbox ([club-sync.md](club-sync.md#checkout-local-race-management-check-in)).

## Using it

1. **Events → the event.** Download it first (Events → Download) so the heats and
   grids from RC Race HQ are on the Pi.
2. Under **Heats (run order)**, each heat has **Length** (minutes, default 5) and
   **Min lap** (seconds, default 4), and a **Start** button.
3. **Start** copies the heat's grid onto the run and begins a **15 s countdown**.
   The page moves to the race view.
   - **Cancel start** works until the countdown reaches zero. The heat goes back
     to not started.
4. At zero the heat is **Racing**. The clock counts down the heat length. The
   standings table updates every second.
5. At time-up the page shows **Time's up: finish lap**. Each car's next crossing
   is its finish (✓ next to the name). Once every car that started has finished,
   the heat finishes by itself. If a car has stopped on track, it finishes 60 s
   after time-up anyway, or press **End heat**.
6. **End heat** finishes now and keeps the results as they stand.
   **Abort** throws the run away (no results); the heat can be started again.
7. A finished heat shows **finished · results** on the event page and a
   **Re-run** button. A re-run's results replace the earlier ones.

The public live page (`/`, no sign-in, what the hotspot shows) shows the heat on
track, or the last finished one, with the same standings.

Only one heat can be on track at a time. While one is, Learn is refused, and
starting a heat cancels any open learn session; the loop belongs to the race.

## Who is in the heat

- The heat's **picked racers** from RC Race HQ, in grid order.
- If none were picked, **every racer in the class**, by name (same as the event
  page shows).
- A racer's transponder is the one on their registration's car in RC Race HQ.
  A racer with no transponder is listed but can't be timed.
- The grid is copied when the heat starts. Re-downloading the event later
  doesn't change a run that already happened.

## Timing rules

| Rule | Detail |
|---|---|
| Start | The Pi clock at the end of the countdown. Never from a sound. |
| Lap 0 | Each car's first crossing after the start is its start-line crossing. Laps count from there. |
| Min lap | A crossing closer than **Min lap** to the car's previous counted crossing is ignored (double reads, a car parked on the loop). |
| Before the start | Crossings before the countdown ends are ignored. |
| Time-up | Start + heat length. The first crossing at or after it is the car's finish; anything after is ignored. A car whose first crossing is after time-up didn't race. |
| Lap time | From the decoder's own clock when it agrees with the Pi clock within 2 s, so network delay can't skew a lap. Otherwise from the Pi clock (after a decoder restart). |
| Order | Most laps, then least total time (start to the car's last counted crossing). Cars that never crossed go last, in grid order, with no place. |
| Unknown | Transponders that cross during the heat but aren't on its grid are listed on the race director page (not the public one). |

## Qualifying

The event page's **Qualifying (best lap)** table ranks each class by its best
complete lap across its finished qualifier heats (the latest run of each heat).
Fastest first; a tie goes to the lap set earlier; racers with no complete lap go
last by name. This is the rule agreed in [club-sync.md](club-sync.md#scoring) and
is expected to change; it is one function.

## Storage

`heat_run` in the Pi's SQLite (migration 4):

| Column | What |
|---|---|
| `event_id`, `heat_id` | The RC Race HQ event and heat |
| `title`, `class_name`, `round_type`, `round_number` | Copied from the heat |
| `entries_json` | The grid at start: `registrationId`, `racerName`, `transponder`, `grid` |
| `duration_s`, `min_lap_ms` | Settings chosen at Start |
| `state` | `started` (countdown / racing / finishing, by the clock), `cancelled`, `finished`, `aborted` |
| `go_at_ms`, `ended_at_ms` | Official start; when it was cancelled, finished or aborted |
| `results_json` | Frozen standings and unknown transponders, set when it finishes |

Passings stay raw in `passing`; when a heat finishes its passings are tagged
with the run id. Standings for a running heat are worked out from the passings
on each request, and frozen at finish.

## Endpoints

| Method | Path | Who | What |
|---|---|---|---|
| POST | `/events/<event>/heats/<heat>/start` | admin | Start the countdown (`duration_min`, `min_lap_s`) |
| GET | `/race/<run>` | admin | Race director page |
| GET | `/api/race/<run>` | admin | The run as JSON (polled every second) |
| POST | `/race/<run>/cancel` · `/end` · `/abort` | admin | Cancel the countdown · finish now · throw away |
| GET | `/api/race/current` | anyone | Heat on track, else the last finished one (no unknown transponders) |

## Not built yet

- Announcer (countdown calls, horn, finish calls).
- Timed start (staggered) as an option; today every car's first crossing is lap 0.
- Assigning an unknown transponder to a racer during a heat.
- Mains seeded from qualifying.
- Uploading heat state, laps and results to RC Race HQ (M4).
