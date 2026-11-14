// Live view: subscribe to /api/live/stream (SSE) and render passings + decoder state.
(function () {
  "use strict";
  var tbody = document.querySelector("#passings tbody");
  var status = document.getElementById("decoder");
  var MAX_ROWS = 50;

  function fmtTime(ms) {
    return new Date(ms).toLocaleTimeString([], { hour12: false });
  }

  function cell(text) {
    var td = document.createElement("td");
    td.textContent = text;  // textContent only: decoder data never becomes HTML
    return td;
  }

  function addPassing(p) {
    var empty = tbody.querySelector(".empty");
    if (empty) empty.remove();
    var tr = document.createElement("tr");
    tr.appendChild(cell(fmtTime(p.received_at_ms)));
    tr.appendChild(cell(String(p.transponder)));
    tr.appendChild(cell(Number(p.rssi).toFixed(1) + " dBFS"));
    tbody.insertBefore(tr, tbody.firstChild);
    while (tbody.rows.length > MAX_ROWS) tbody.deleteRow(-1);
  }

  function setDecoder(d) {
    status.className = "status " + (d.online ? "ok" : "bad");
    if (d.online) {
      status.textContent = "Decoder online: " + d.received + " frames/s, noise " +
        Number(d.noise).toFixed(1) + " dBFS";
    } else {
      status.textContent = d.seen ? "Decoder offline" : "Waiting for the decoder";
    }
  }

  var src = new EventSource("/api/live/stream");
  src.addEventListener("passing", function (e) { addPassing(JSON.parse(e.data)); });
  src.addEventListener("decoder", function (e) { setDecoder(JSON.parse(e.data)); });
  src.onerror = function () {
    status.className = "status unknown";
    status.textContent = "Reconnecting…";
  };
})();
