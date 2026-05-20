// admin.js — lightweight sortable table for GC Tracker admin pages
// Attach data-col="N" to <th> elements (use data-col="-1" to disable sort on a column).
// Attach data-value="raw-sort-key" to <td> elements where the display text shouldn't
// be used for sorting (e.g. formatted dates — store the ISO string in data-value).
(function () {
  "use strict";

  function parseVal(cell) {
    var v = (cell.dataset.value !== undefined && cell.dataset.value !== "")
      ? cell.dataset.value
      : cell.textContent.trim();
    if (v === "—" || v === "") return null; // em-dash or empty → sort to bottom
    var n = parseFloat(v);
    return isNaN(n) ? v.toLowerCase() : n;
  }

  function sortTable(th) {
    var col   = parseInt(th.dataset.col, 10);
    var table = th.closest("table");
    var tbody = table.tBodies[0] || table;
    var rows  = Array.from(tbody.rows).filter(function (r) {
      return !r.querySelector("th"); // skip any header rows inside tbody
    });

    var asc = th.dataset.dir !== "asc";

    // Reset all header indicators
    th.closest("tr").querySelectorAll("th[data-col]").forEach(function (t) {
      t.dataset.dir = "";
      t.classList.remove("sort-asc", "sort-desc");
    });
    th.dataset.dir = asc ? "asc" : "desc";
    th.classList.add(asc ? "sort-asc" : "sort-desc");

    rows.sort(function (a, b) {
      var cellsA = a.cells;
      var cellsB = b.cells;
      if (col >= cellsA.length || col >= cellsB.length) return 0;
      var va = parseVal(cellsA[col]);
      var vb = parseVal(cellsB[col]);
      // Nulls always sink to the bottom regardless of sort direction
      if (va === null && vb === null) return 0;
      if (va === null) return 1;
      if (vb === null) return -1;
      if (va < vb) return asc ? -1 : 1;
      if (va > vb) return asc ? 1 : -1;
      return 0;
    });

    rows.forEach(function (r) { tbody.appendChild(r); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("th[data-col]").forEach(function (th) {
      if (th.dataset.col === "-1") return;
      th.addEventListener("click", function () { sortTable(th); });
    });
  });
}());
