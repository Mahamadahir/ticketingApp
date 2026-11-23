/* The Split card (#259, #285). Clear empties a part's subject and details, which leaves the part out of the split (a part
   with neither is skipped by the server). "Add another part" shows the next part the server rendered hidden (it renders as
   many as the ticket can still take); when none are left it hides itself and says that is the most. Without this script
   the card shows its first parts only, and a part is left out by emptying its boxes by hand. Defined once; it listens on
   the document, so it keeps working after the page swaps <main>. */
(function () {
  if (window.splitCard) { return; }
  window.splitCard = true;

  // The Add button is rendered hidden (it does nothing without this script); show it once the script is running.
  function showAddButtons() {
    var buttons = document.querySelectorAll("[data-add-part]");
    for (var i = 0; i < buttons.length; i++) { buttons[i].hidden = false; }
    var notes = document.querySelectorAll("[data-split-nojs]");
    for (var j = 0; j < notes.length; j++) { notes[j].hidden = true; }
  }
  showAddButtons();
  document.addEventListener("DOMContentLoaded", showAddButtons);
  document.addEventListener("htmx:afterSwap", showAddButtons);

  document.addEventListener("click", function (event) {
    var target = event.target && event.target.closest ? event.target : null;
    if (!target) { return; }
    var clear = target.closest("[data-clear-part]");
    if (clear) {
      var part = clear.closest(".tp-split-part");
      if (!part) { return; }
      var boxes = part.querySelectorAll("input[name=subject], textarea[name=details]");
      for (var i = 0; i < boxes.length; i++) { boxes[i].value = ""; }
      return;
    }
    var add = target.closest("[data-add-part]");
    if (add) {
      var form = add.closest("form");
      var hiddenParts = form ? form.querySelectorAll(".tp-split-part[hidden]") : [];
      if (hiddenParts.length) {
        hiddenParts[0].hidden = false;
        var field = hiddenParts[0].querySelector("input[name=subject]");
        if (field) { field.focus(); }
      }
      if (hiddenParts.length <= 1) {
        add.hidden = true;
        var most = form.querySelector("[data-split-most]");
        if (most) { most.hidden = false; }
      }
    }
  });
})();
