// admin-task.js — shared "Run Now" + live SSE progress log for long-running admin
// task pages (Build Coords, Validate Stores, PG Backfill), rendered by
// _admin_task_page() in gc_tracker_app.py.
//
// This used to be an inline <script> with onclick="run()" baked into the template
// itself. CSP's script-src is 'self' only (no 'unsafe-inline', no nonce) — so that
// inline script and inline onclick handler were BOTH silently blocked by the browser.
// The button looked normal but clicking it did nothing at all; no console error is
// shown for a CSP-blocked inline handler failing to attach, only for a blocked
// <script> tag's content (which is why this went unnoticed — see the v2.16.19
// HANDOFF.md entry for the full story). Moving the logic to this external,
// same-origin file makes it load fine under 'self', matching how every other
// onclick="..." in this app was already replaced back in v2.10.18.
(function () {
  "use strict";

  function run() {
    var btn = document.getElementById("run-btn");
    var log = document.getElementById("log");
    if (!btn || !log) return;
    var apiPath = btn.dataset.apiPath;
    if (!apiPath) return;

    btn.disabled = true;
    btn.textContent = "⏳ Running…";
    log.textContent = "Starting…\n";

    // The only existing "extra option" across these pages is Build Coords' force
    // re-geocode checkbox — always id="force-cb" when present (see _admin_task_page's
    // options_html docstring in gc_tracker_app.py).
    var body = {};
    var forceCb = document.getElementById("force-cb");
    if (forceCb) body.force = forceCb.checked;

    fetch(apiPath, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (resp) {
      if (!resp.ok) {
        return resp.json().catch(function () { return {}; }).then(function (e) {
          log.textContent += "❌ Error: " + (e.error || resp.statusText) + "\n";
          btn.disabled = false;
          btn.textContent = "▶ Run Now";
        });
      }
      var es = new EventSource("/api/progress");
      es.onmessage = function (e) {
        var msg = JSON.parse(e.data);
        if (msg.type === "ping") return;
        if (msg.type === "progress") {
          log.textContent += (msg.msg || "") + "\n";
          log.scrollTop = log.scrollHeight;
          return;
        }
        if (msg.type === "done") {
          es.close();
          if (msg.error) {
            log.innerHTML += '<span class="err">\n❌ ' + msg.error + "</span>";
          } else {
            log.innerHTML += '<span class="done">\n✓ Done.</span>';
          }
          btn.disabled = false;
          btn.textContent = "▶ Run Again";
        }
      };
      es.onerror = function () {
        es.close();
        log.innerHTML += '<span class="err">\nConnection lost.</span>';
        btn.disabled = false;
        btn.textContent = "▶ Run Now";
      };
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("run-btn");
    if (btn) btn.addEventListener("click", run);
  });
}());
