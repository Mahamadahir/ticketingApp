/* The End user picker on the agent ticket page (#279). The chevron after the End user's name opens an inline panel: a search box
   (htmx fetches GET /tickets/{number}/people as the agent types), the results as buttons, then the two tick boxes and Save / Cancel.
   A result button stores its directory object id (new_requester_oid); "Use {address}" stores a typed address (new_requester) for
   someone with no Entra account. The server checks either choice again. Escape or Cancel closes the panel and gives focus back to the
   chevron. Defined once; it listens on the document, so it keeps working after the page swaps <main>. */
(function () {
  "use strict";
  if (window.requesterPicker) { return; }
  window.requesterPicker = true;

  function byId(id) { return document.getElementById(id); }

  function reset() {
    var q = byId("rp-q");
    if (!q) { return; }
    byId("rp-oid").value = "";
    byId("rp-address").value = "";
    byId("rp-chosen").hidden = true;
    byId("rp-search").hidden = false;
    byId("rp-results").innerHTML = "";
    q.value = "";
  }

  function setOpen(open) {
    var toggle = byId("rp-toggle");
    var panel = byId("rp-panel");
    if (!toggle || !panel) { return; }
    panel.hidden = !open;
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      var q = byId("rp-q");
      if (q) { q.focus(); }
    } else {
      reset();
      toggle.focus();
    }
  }

  function isOpen() {
    var panel = byId("rp-panel");
    return !!panel && !panel.hidden;
  }

  document.addEventListener("click", function (event) {
    var target = event.target && event.target.closest ? event.target : null;
    if (!target) { return; }
    if (target.closest("[data-rp-toggle]")) { setOpen(!isOpen()); return; }
    if (target.closest("[data-rp-cancel]")) { setOpen(false); return; }
    var panel = byId("rp-panel");
    if (!panel || !panel.contains(target)) { return; }
    if (target.closest("#rp-change")) {
      reset();
      byId("rp-q").focus();
      return;
    }
    var button = target.closest(".mb-person");
    if (!button) { return; }
    var oid = button.getAttribute("data-oid");
    byId("rp-oid").value = oid || "";
    byId("rp-address").value = oid ? "" : (button.getAttribute("data-address") || "");
    byId("rp-chosen-name").textContent = button.getAttribute("data-detail") || button.getAttribute("data-name");
    byId("rp-chosen").hidden = false;
    byId("rp-search").hidden = true;
    byId("rp-results").innerHTML = "";
    var keep = panel.querySelector("input[name=keep_old]");
    if (keep) { keep.focus(); }
  });

  // A search answer that arrives after a choice was made (inside the 300 ms delay) must not bring the list back.
  document.addEventListener("htmx:beforeSwap", function (event) {
    var chosen = byId("rp-chosen");
    if (event.detail && event.detail.target && event.detail.target.id === "rp-results" && chosen && !chosen.hidden) {
      event.detail.shouldSwap = false;
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && isOpen()) {
      setOpen(false);
      return;
    }
    // Enter in the search box must not send the form with nothing chosen.
    if (event.key === "Enter" && event.target && event.target.id === "rp-q") { event.preventDefault(); }
  });
})();
