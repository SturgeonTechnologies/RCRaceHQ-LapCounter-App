# RC Race HQ Lap Counter

Offline-first RC lap counter that runs on a Raspberry Pi 4 at the track and
works with [RC Race HQ](https://www.rcracehq.com). Transponder decoding is
done by [OpenStint](https://github.com/zsellera/openstint) with an RTL-SDR;
this repo is the app around it: live timing, heats and qualifying, racer
check-in and a desk kiosk, transponder learning, and sync with RC Race HQ.

**Needs an RC Race HQ club for the connected parts.** The Pi times races on
its own. Event download, result uploads, check-in from registrations and the
shared transponder library work once the Pi is paired to a club
(Settings → Link, [docs/linking.md](docs/linking.md)).

## Install

Use [RCRaceHQ-LapCounter-Install](https://github.com/SturgeonTechnologies/RCRaceHQ-LapCounter-Install).
It sets up OpenStint, the event Wi-Fi (`http://race/`) and this app from the
public image `ghcr.io/sturgeontechnologies/rcracehq-lapcounter`.

The optional RF front-end board is
[RCRaceHQ-LapCounter-HAT](https://github.com/SturgeonTechnologies/RCRaceHQ-LapCounter-HAT).

## Docs

| Doc | What |
|---|---|
| [docs/design.md](docs/design.md) | Architecture |
| [docs/club-sync.md](docs/club-sync.md) | How the Pi and RC Race HQ work together: pairing, event download, check-in, scoring, takeover |
| [docs/linking.md](docs/linking.md) | Pairing a Pi to a club's track (Settings → Link) |
| [docs/event-sync.md](docs/event-sync.md) | Downloading a club's events and the transponders to learn |
| [docs/racing.md](docs/racing.md) | Running a heat: start, countdown, laps, finish, results, qualifying |
| [docs/checkin.md](docs/checkin.md) | Racer check-in and the desk kiosk |
| [docs/learning.md](docs/learning.md) | Learning transponders |
| [docs/build.md](docs/build.md) | Image tags, building your own |

## Develop

```bash
cd app
pip install -e '.[test]' && pytest -q
docker compose up --build        # web on :8080, lapd → 127.0.0.1:5556
```

Issues and pull requests are welcome.

## License

Source-available under [FSL-1.1-ALv2](LICENSE): run it at your club, change
it, share fixes; don't sell it or run a competing service with it. Each
version becomes Apache-2.0 two years after its release. Plain-language
summary: [LICENSING.md](LICENSING.md). OpenStint is a separate program under
GPL-3.0 and isn't included here. "RC Race HQ" is a trademark of Sturgeon
Technologies LLC.
