// Show stored epoch-ms timestamps in the browser's local time.
(function () {
  "use strict";
  document.querySelectorAll(".ts[data-ms]").forEach(function (el) {
    var ms = Number(el.getAttribute("data-ms"));
    if (ms) el.textContent = new Date(ms).toLocaleString();
  });
})();
