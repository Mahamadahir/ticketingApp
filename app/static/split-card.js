/* The Clear button of a part on the Split card (#259): empties that part's subject and details, which leaves the part out of
   the split (a part with no subject and no details is skipped by the server). Without this script a part is left out by
   emptying its boxes by hand. Defined once; it listens on the document, so it keeps working after the page swaps <main>. */
(function () {
  if (window.splitCard) { return; }
  window.splitCard = true;

  document.addEventListener("click", function (event) {
    var button = event.target && event.target.closest ? event.target.closest("[data-clear-part]") : null;
    if (!button) { return; }
    var part = button.closest(".tp-split-part");
    if (!part) { return; }
    var boxes = part.querySelectorAll("input[name=subject], textarea[name=details]");
    for (var i = 0; i < boxes.length; i++) { boxes[i].value = ""; }
  });
})();
