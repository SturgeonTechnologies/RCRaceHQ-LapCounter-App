// Learn page: poll /api/learn/<id> once a second and move through the steps.
(function () {
  "use strict";
  var root = document.getElementById("learn");
  if (!root) return;
  var api = root.getAttribute("data-api");
  var message = document.getElementById("message");
  var progress = document.getElementById("progress");
  var signal = document.getElementById("signal");
  var record = document.getElementById("record");
  var STEP = { waiting: 0, lift: 0, expired: 0, learning: 1, interrupted: 1, ready: 2, known: 2, conflict: 1 };
  var CLASS = { ready: "ok", known: "ok", conflict: "bad", expired: "bad", interrupted: "bad", lift: "bad" };

  function render(s) {
    if (s.closed) { window.location.reload(); return; }
    message.textContent = s.message;  // textContent only
    message.className = "status " + (CLASS[s.state] || "unknown");
    progress.value = s.progress;
    progress.textContent = s.progress + "%";
    record.disabled = !s.can_record;
    var at = STEP[s.state];
    root.querySelectorAll(".steps li").forEach(function (li, i) {
      li.className = i < at ? "done" : (i === at ? "current" : "");
    });
    var d = s.decoder;
    if (d.online) {
      signal.textContent = "Decoder: " + d.received + " frames/s, " + d.processed +
        " clean, noise " + Number(d.noise).toFixed(1) + " dBFS" +
        (s.state === "learning" ? " · " + s.elapsed_s.toFixed(0) + " s" : "");
    } else {
      signal.textContent = d.seen ? "Decoder offline. Check systemctl status openstint."
                                  : "Waiting for the decoder…";
    }
  }

  function tick() {
    fetch(api, { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        if (r.redirected || !r.ok) throw new Error("status " + r.status);
        return r.json();
      })
      .then(render)
      .catch(function () { signal.textContent = "Lost contact with the lap counter, retrying…"; })
      .finally(function () { setTimeout(tick, 1000); });
  }
  tick();
})();
