// Progressive enhancement only. Every page works with JS off: the positions
// table is rendered server-side, forms are real POSTs, and the preview falls
// back to opening in a new tab.
(function () {
  "use strict";

  // ---------------------------------------------------------------- nav
  var toggle = document.querySelector("[data-nav-toggle]");
  var nav = document.querySelector("[data-nav]");
  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      var open = nav.hasAttribute("hidden");
      if (open) { nav.removeAttribute("hidden"); } else { nav.setAttribute("hidden", ""); }
      toggle.setAttribute("aria-expanded", String(open));
    });
  }

  // ------------------------------------------------------- confirmations
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (ev) {
      if (!window.confirm(form.getAttribute("data-confirm"))) { ev.preventDefault(); }
    });
  });

  // -------------------------------------------------------------- preview
  // The Preview button posts the form into a same-named iframe, so the editor
  // keeps the unsaved textareas. That means nothing navigates away and the pane
  // has to be opened and sized from here.
  var pane = document.getElementById("preview-pane");
  var frame = document.getElementById("preview-frame");
  if (pane && frame) {
    var editorForm = document.getElementById("editor");
    var previewing = false;

    function size() {
      try {
        var doc = frame.contentDocument;
        if (doc && doc.documentElement) {
          frame.style.height = Math.max(doc.documentElement.scrollHeight, 400) + "px";
        }
      } catch (e) { /* not same-origin: the CSS height has to do */ }
    }

    // preview.html reports the height of the email it is showing. Re-measure on
    // the message instead of trusting it: the number is the inner frame's
    // height, and what this pane wants is the whole document's.
    window.addEventListener("message", function (ev) {
      if (ev.data && ev.data.type === "preview-height") {
        size();
        setTimeout(size, 250);
      }
    });

    frame.addEventListener("load", function () {
      size();
      setTimeout(size, 250);
    });

    if (editorForm) {
      // ev.submitter is the direct route; the click flag covers older browsers.
      editorForm.addEventListener("click", function (ev) {
        var btn = ev.target.closest ? ev.target.closest("[formtarget]") : null;
        if (btn && btn.getAttribute("formtarget") === "preview-frame") {
          previewing = true;
          pane.removeAttribute("hidden");
        }
      });
      editorForm.addEventListener("submit", function (ev) {
        var btn = ev.submitter;
        var targets = btn && btn.getAttribute &&
                      btn.getAttribute("formtarget") === "preview-frame";
        if (targets || previewing) {
          previewing = false;
          pane.removeAttribute("hidden");
          size();
        }
      });
    }

    document.querySelectorAll("[data-close-preview]").forEach(function (el) {
      el.addEventListener("click", function () {
        pane.setAttribute("hidden", "");
        frame.removeAttribute("src");
      });
    });
  }

  // ------------------------------------------------------------ positions
  var table = document.getElementById("positions");
  var search = document.getElementById("q");
  if (!table || !search) { return; }

  var body = table.tBodies[0];
  var costCell = document.querySelector("[data-cost]");
  var qtyCell = document.querySelector("[data-qty]");
  var countCell = document.querySelector("[data-count]");

  function money(n) {
    return "$" + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function num(n) {
    return n.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }
  function cell(row, name) {
    var td = row.querySelector("td:nth-child(" + name + ")");
    return td ? td.textContent.trim() : "";
  }

  // Sort and filter without a round trip. The whole table is only ever a few
  // dozen rows, so filtering the DOM is simpler than any pagination.
  function apply() {
    var q = search.value.trim().toLowerCase();
    var sort = table.getAttribute("data-sort") || "cost";
    var dir = table.getAttribute("data-dir") === "desc" ? -1 : 1;
    var index = { symbol: 1, description: 2, qty: 3, avg_cost: 4, cost: 5, lots: 6, first_acquired: 7 }[sort];
    var numeric = sort === "qty" || sort === "avg_cost" || sort === "cost" || sort === "lots";

    var rows = Array.prototype.slice.call(body.rows).filter(function (row) {
      if (!q) { return true; }
      return row.getAttribute("data-symbol").toLowerCase().indexOf(q) !== -1 ||
             (row.getAttribute("data-description") || "").toLowerCase().indexOf(q) !== -1;
    });

    rows.sort(function (a, b) {
      var av = cell(a, index), bv = cell(b, index);
      var an = numeric ? (parseFloat(av.replace(/[^0-9.-]/g, "")) || 0) : av.toLowerCase();
      var bn = numeric ? (parseFloat(bv.replace(/[^0-9.-]/g, "")) || 0) : bv.toLowerCase();
      if (an < bn) { return -1 * dir; }
      if (an > bn) { return 1 * dir; }
      return 0;
    });

    rows.forEach(function (row) { body.appendChild(row); });

    var cost = 0, qty = 0;
    rows.forEach(function (row) {
      cost += parseFloat(cell(row, 5).replace(/[^0-9.-]/g, "")) || 0;
      qty += parseFloat(cell(row, 3).replace(/[^0-9.-]/g, "")) || 0;
    });
    if (costCell) { costCell.textContent = money(cost); }
    if (qtyCell) { qtyCell.textContent = num(qty); }
    if (countCell) { countCell.textContent = String(rows.length); }

    var empty = document.querySelector(".empty");
    if (empty) { empty.hidden = rows.length > 0; }
  }

  search.addEventListener("input", apply);

  table.querySelectorAll("th[data-sort]").forEach(function (th) {
    th.setAttribute("tabindex", "0");
    function go() {
      var key = th.getAttribute("data-col");
      if (table.getAttribute("data-sort") === key) {
        table.setAttribute("data-dir", table.getAttribute("data-dir") === "desc" ? "asc" : "desc");
      } else {
        table.setAttribute("data-sort", key);
        table.setAttribute("data-dir", "desc");
      }
      // data-col is which column; the arrow is a separate class, so the two
      // never get confused for each other.
      table.querySelectorAll("th[data-col]").forEach(function (o) {
        o.classList.remove("asc", "desc");
      });
      th.classList.add(table.getAttribute("data-dir"));
      apply();
    }
    th.addEventListener("click", go);
    th.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(); }
    });
  });

  apply();
})();
