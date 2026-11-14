# Walk-up check-in

Racers at the track join the hotspot, land on the live page (the captive
portal sends them to `/`), tap **Racing today? Check in here**, type their
phone number and press **I'm here, check me in**. On their own phone they can instead press **Just show what I still need** to see the checklist without checking in (handy from the parking lot); the list then has an **I'm here** button. The desk kiosk always checks in.

- **Open only on race day:** the page works once an event is checked out on
  this Pi (Events → event → Check out). Before that it says check-in isn't open.
- **Which number:** the phone on the racer's RC Race HQ account, or on the
  account that registered them (a parent). One number checks in every
  registration it matches in that event.
- **What they see:** their first name, their classes and a to-do list. Nothing
  else from the registration.
  - "Bring your car to the learning table" when the transponder is missing or
    not learned on this Pi.
  - The staff note from the event page (for example "Tech inspection at the
    tower"). One note per Pi, shown for every racer.
- **Checklist:** after the number matches, each registration gets three
  items with a check mark or a hint: **Checked in**, **Entry paid** (from the
  pack's status; "Pay at the race desk" until RC Race HQ says paid and the
  pack is downloaded again) and **Transponder learned** (in this Pi's list or
  `.rc4` folder). The staff note is shown last and never blocks.
- **Ready to race!:** the button turns on once every item is ticked.
  **Check again** re-reads the list after the car has been to the learning
  table, without typing the number again (the match is kept in the browser's
  signed session for 15 minutes). Ready is recorded on the Pi only
  (`entry_checkin.ready_at_ms`); it isn't sent to RC Race HQ yet.
- **Kiosk (`/kiosk`):** the same flow for a shared tablet at the desk. No
  menu, big text and buttons, no phone autofill. The screen goes back to the
  start 45 s after a result (6 s after Ready), and a fresh start screen
  forgets the last racer, so the next person can't pull up their list.
  Put a tablet's browser in full-screen on `http://<pi>/kiosk`.
- **Staff view:** the event page shows how many registrations are checked in,
  how many are ready to race, and who isn't here yet.
- **RC Race HQ:** each new check-in queues `entry.checkin`
  (`{registrationId, atMs}`); RC Race HQ stores `checkedInAt` on the
  registration. A second check-in queues nothing.

## Privacy

The Pi never stores phone numbers. Each pack entry carries `firstName` and
`phoneTags`: `sha256("rchq-checkin:<eventId>:<E.164>")`, first 32 hex
characters, for the racer's and the purchaser's account phones. The typed
number is normalized the same way as RC Race HQ (`lib/phone.ts`) and hashed.

A phone number has few enough possibilities that a tag can be brute-forced by
someone who has the pack, so this keeps numbers out of the pack and off the
SD card, not out of reach of a determined attacker with the Pi.

The hotspot is open by default (Settings → Advanced can switch it to WPA2; see
below), so anyone on it can type any number. Tries are limited to
6 a minute per device and 60 a minute overall, and a match shows only a first
name and classes. A texted code can be added once SMS is live.

## Hotspot Wi-Fi (Settings → Advanced)

Default is open, so racers can check in and see race status. An admin can set
the network name and switch to WPA2. The web container writes
`wifi.json` (`{"security": "open"|"wpa2", "ssid": "...", "passphrase": "..."}`)
into `LAPCOUNTER_WIFI_DIR` (`/etc/lapcounter`), which compose bind-mounts from
the host (`LAPCOUNTER_WIFI_HOST_DIR`). The deploy (`lapcounter_app` role in
RCRaceHQ-LapCounter-Install) must mount that directory and apply the file to the
hotspot's NetworkManager profile when it changes. The file is 0640 and holds the
passphrase in clear text.
