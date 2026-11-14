// Heat view: polls the race JSON once a second and ticks the clock in between.
// Used by the race director page (data-admin) and the public live page.
(function () {
  "use strict";
  var root = document.getElementById("race");
  if (!root) return;
  var src = root.getAttribute("data-src");
  var admin = root.getAttribute("data-admin") === "1";
  var phaseEl = document.getElementById("race-phase");
  var clockEl = document.getElementById("race-clock");
  var titleEl = document.getElementById("race-title");
  var tbody = root.querySelector("#standings tbody");
  var unknown = document.getElementById("unknown");
  var state = null;
  var offset = 0; // server clock minus this device's clock

  var LABELS = {
    countdown: "Starting", running: "Racing", finishing: "Time's up: finish lap",
    finished: "Finished", cancelled: "Start cancelled", aborted: "Aborted"
  };

  function secs(ms) {
    if (ms === null || ms === undefined) return "";
    return (ms / 1000).toFixed(3);
  }

  function mmss(s) {
    s = Math.max(0, Math.floor(s));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }

  function cell(text) {
    var td = document.createElement("td");
    td.textContent = text; // textContent only: names never become HTML
    return td;
  }

  function tick() {
    if (!state || !state.phase) { clockEl.textContent = ""; return; }
    var now = Date.now() + offset;
    if (state.phase === "countdown") {
      clockEl.textContent = Math.max(0, Math.ceil((state.goAtMs - now) / 1000)) + " s";
    } else if (state.phase === "running") {
      clockEl.textContent = mmss((state.timeUpAtMs - now) / 1000) + " left";
    } else {
      clockEl.textContent = "";
    }
  }

  function render(v) {
    state = v;
    offset = v.nowMs - Date.now();
    root.hidden = false;
    if (titleEl) titleEl.textContent = v.title || "No heat on track";
    phaseEl.textContent = v.phase ? (LABELS[v.phase] || v.phase) : "";
    phaseEl.className = "status " + (v.phase === "running" || v.phase === "countdown" ? "ok" : "unknown");
    document.querySelectorAll("[data-phases]").forEach(function (el) {
      el.hidden = el.getAttribute("data-phases").split(" ").indexOf(v.phase) < 0;
    });
    tbody.textContent = "";
    (v.standings || []).forEach(function (s) {
      var tr = document.createElement("tr");
      tr.appendChild(cell(s.place ? String(s.place) : ""));
      var who = s.racerName + (s.transponder ? "" : " (no transponder)");
      if (s.finished) who += " ✓";
      tr.appendChild(cell(who));
      tr.appendChild(cell(s.started ? String(s.laps) : ""));
      tr.appendChild(cell(s.totalMs !== null && s.started ? mmss(s.totalMs / 1000) : ""));
      tr.appendChild(cell(secs(s.bestLapMs)));
      tr.appendChild(cell(secs(s.lastLapMs)));
      tbody.appendChild(tr);
    });
    if (admin && unknown) {
      var list = unknown.querySelector("ul");
      list.textContent = "";
      (v.unknown || []).forEach(function (u) {
        var li = document.createElement("li");
        li.textContent = u.transponder + " (" + u.crossings + " crossings)";
        list.appendChild(li);
      });
      unknown.hidden = !(v.unknown && v.unknown.length);
    }
    tick();
  }

  function poll() {
    fetch(src, { cache: "no-store", credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (v) { if (v) render(v); })
      .catch(function () { /* keep the last view; the next poll retries */ });
  }

  var initial = root.getAttribute("data-initial");
  if (initial) render(JSON.parse(initial));
  poll();
  setInterval(poll, 1000);
  setInterval(tick, 250);
})();
