// Settings → Link: poll /api/link every 3 s until the code is entered in RC Race HQ.
(function () {
  "use strict";
  var root = document.getElementById("link");
  if (!root) return;
  var api = root.getAttribute("data-api");
  var done = root.getAttribute("data-done");
  var message = document.getElementById("message");

  function tick() {
    fetch(api, { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        if (r.redirected || !r.ok) throw new Error("status " + r.status);
        return r.json();
      })
      .then(function (s) {
        if (s.state !== "waiting") {
          // expired / failed: show why on the reloaded page; linked: just reload.
          window.location.href = s.state === "linked" || !s.message ? done
            : done + "?error=" + encodeURIComponent(s.message);
          return;
        }
        message.textContent = s.message || "Waiting for the code to be entered…";  // textContent only
        setTimeout(tick, 3000);
      })
      .catch(function () {
        message.textContent = "Lost contact with the lap counter, retrying…";
        setTimeout(tick, 3000);
      });
  }
  setTimeout(tick, 3000);
})();
