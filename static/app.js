// Day log — progressive enhancements. The app works fully without JS; these
// only sharpen feedback under deadline.
(function () {
  "use strict";

  // 1. Loading feedback. On submit, reflect a busy state on the submit button
  //    so a slow Pull/Mark/Retract can't be double-clicked. Deferred to a
  //    macrotask so the form still submits before the button is disabled.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var btn = form.querySelector("button[type=submit], button:not([type])");
    if (!btn || btn.disabled) return;
    var busy = btn.getAttribute("data-busy");
    setTimeout(function () {
      if (busy) btn.textContent = busy;
      btn.setAttribute("aria-busy", "true");
      btn.disabled = true;
    }, 0);
  });

  // 2. Candidate filter. Find a story fast on a heavy day.
  var input = document.getElementById("candidate-filter");
  if (input) {
    var grid = document.getElementById("candidate-grid");
    var cards = grid ? Array.prototype.slice.call(grid.querySelectorAll(".filterable")) : [];
    var count = document.getElementById("candidate-count");
    var empty = document.getElementById("candidate-empty");
    var apply = function () {
      var q = input.value.trim().toLowerCase();
      var shown = 0;
      cards.forEach(function (c) {
        var match = !q || (c.getAttribute("data-text") || "").indexOf(q) !== -1;
        c.hidden = !match;
        if (match) shown++;
      });
      if (count) count.textContent = q ? shown + " of " + cards.length : "";
      if (empty) empty.hidden = shown !== 0;
    };
    input.addEventListener("input", apply);
  }
})();
