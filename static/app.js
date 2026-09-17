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

  // 2. Section strip cap. Past a handful of sections the strip is trimmed to
  //    the heaviest N with a "+N more" reveal. Without JS all chips show.
  var more = document.getElementById("more-sections");
  if (more) {
    var cap = parseInt(more.getAttribute("data-cap"), 10) || 10;
    var secs = Array.prototype.slice.call(more.parentNode.querySelectorAll(".chip.sec"));
    var hidden = secs.slice(cap).filter(function (c) { return !c.classList.contains("is-on"); });
    if (hidden.length) {
      hidden.forEach(function (c) { c.hidden = true; });
      more.textContent = "+" + hidden.length + " more";
      more.hidden = false;
      more.addEventListener("click", function () {
        hidden.forEach(function (c) { c.hidden = false; });
        more.setAttribute("aria-expanded", "true");
        more.hidden = true;
        if (hidden[0]) hidden[0].focus();
      });
    }
  }

  // 3. Find-as-you-type over any list. An input declares its list with
  //    data-filter-target, and optionally where to write the count and which
  //    empty-state to reveal. Works for the marked rows and the candidates.
  var inputs = document.querySelectorAll("input[data-filter-target]");
  Array.prototype.forEach.call(inputs, function (input) {
    var target = document.querySelector(input.getAttribute("data-filter-target"));
    if (!target) return;
    var items = Array.prototype.slice.call(target.querySelectorAll(".filterable"));
    var dividers = Array.prototype.slice.call(target.querySelectorAll(".hour-divider"));
    var count = input.hasAttribute("data-filter-count") ? document.querySelector(input.getAttribute("data-filter-count")) : null;
    var empty = input.hasAttribute("data-filter-empty") ? document.querySelector(input.getAttribute("data-filter-empty")) : null;
    var apply = function () {
      var q = input.value.trim().toLowerCase();
      var shown = 0;
      items.forEach(function (el) {
        var match = !q || (el.getAttribute("data-text") || "").indexOf(q) !== -1;
        el.hidden = !match;
        if (match) shown++;
      });
      // Hour landmarks only make sense for the full, unfiltered list.
      dividers.forEach(function (d) { d.hidden = !!q; });
      if (count) count.textContent = q ? shown + " of " + items.length : "";
      if (empty) empty.hidden = shown !== 0;
    };
    input.addEventListener("input", apply);
  });
})();
