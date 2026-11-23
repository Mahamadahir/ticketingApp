/* The Split card (#259, #285, #286). The red bin on part 3 onwards removes an added part (empties it, hides it again, renumbers).
   Clear empties a part's subject and details, which leaves the part out of the split (a part
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
    var bins = document.querySelectorAll("[data-remove-part]");
    for (var b = 0; b < bins.length; b++) { bins[b].hidden = false; }
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
    var remove = target.closest("[data-remove-part]");
    if (remove) {
      var gone = remove.closest(".tp-split-part");
      var card = gone ? gone.closest("form") : null;
      if (!card) { return; }
      var goneBoxes = gone.querySelectorAll("input[name=subject], textarea[name=details]");
      for (var g = 0; g < goneBoxes.length; g++) { goneBoxes[g].value = ""; }
      gone.hidden = true;
      // Move it to the end of the parts so the shown parts stay numbered 1, 2, 3 with no gaps; then renumber.
      var all = card.querySelectorAll(".tp-split-part");
      all[all.length - 1].insertAdjacentElement("afterend", gone);
      all = card.querySelectorAll(".tp-split-part");
      for (var n = 0; n < all.length; n++) {
        var label = all[n].querySelector("[data-part-label]");
        if (label) { label.textContent = "Part " + (n + 1) + " subject"; }
        var bin = all[n].querySelector("[data-remove-part]");
        if (bin) { bin.setAttribute("aria-label", "Remove part " + (n + 1)); }
      }
      var addBtn = card.querySelector("[data-add-part]");
      var mostNote = card.querySelector("[data-split-most]");
      if (mostNote) { mostNote.hidden = true; }
      if (addBtn) {
        addBtn.hidden = false;
        addBtn.focus();
      } else {
        // No Add button on this card (every part was already shown): focus the last shown part's subject.
        var shown = card.querySelectorAll(".tp-split-part:not([hidden]) input[name=subject]");
        if (shown.length) { shown[shown.length - 1].focus(); }
      }
      return;
    }
    var add = target.closest("[data-add-part]");
    if (add) {
      var form = add.closest("form");
      if (!form) { return; }
      var hiddenParts = form.querySelectorAll(".tp-split-part[hidden]");
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
