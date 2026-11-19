/* Ticket presence (#246): what the agent is doing on this ticket page, for the strip's check-in (app/ticket_presence.py).
   Loaded by ticket_detail.html. The page swaps <main> after every action, so everything here is defined once and reads the page
   when asked, never keeps its own copy of it. */
(function () {
  if (window.ticketPresenceVals) { return; }

  // What the person has open: an action's card (#tp-card) or a box row being edited, each of which says what it is doing in its own
  // data-doing attribute (the words after the name in the strip). The composers have none: they count only once something is typed.
  function openPanel() {
    return document.querySelector("#tp-card [data-doing], #tp-card[data-doing], .tp-box [data-doing]");
  }

  function doingOf(panel) {
    return panel.getAttribute("data-doing") || "using an action";
  }

  function composing(kind) {
    var card = document.querySelector('[data-compose="' + kind + '"]');
    if (!card) { return false; }
    if (card.contains(document.activeElement)) { return true; }
    var box = card.querySelector("textarea[name=body]");
    // A draft in the open tab counts as writing, even while they look at something else.
    return !!(box && box.value.trim());
  }

  // What the check-in sends: the activity, the action's name, and the fingerprint of the strip now on the page.
  window.ticketPresenceVals = function () {
    var shown = document.querySelector("#presence .tp-presence-body");
    var vals = { activity: "viewing", doing: "", shown: shown ? shown.getAttribute("data-shown") || "" : "" };
    var panel = openPanel();
    if (panel) {
      vals.activity = "action";
      vals.doing = doingOf(panel);
    } else if (composing("email")) {
      vals.activity = "email";
    } else if (composing("note")) {
      vals.activity = "note";
    }
    return vals;
  };

  function checkInNow() {
    var strip = document.getElementById("presence");
    if (strip && window.htmx) { window.htmx.trigger(strip, "presence-now"); }
  }

  // Leaving the page clears the row at once. sendBeacon is a POST that survives the page going away.
  window.addEventListener("pagehide", function () {
    var strip = document.getElementById("presence");
    var leave = strip && strip.getAttribute("data-leave");
    if (leave && navigator.sendBeacon) { navigator.sendBeacon(leave); }
  });
  // Coming back from the back/forward cache: the page was not reloaded, so say so.
  window.addEventListener("pageshow", function (event) { if (event.persisted) { checkInNow(); } });
  // A change of what they are doing shows sooner than the next interval: focus moving into the composer, a panel opening or closing.
  document.addEventListener("focusin", function (event) {
    var composer = event.target.closest && event.target.closest(".tp-composer");
    // Only on moving into the composer, not on every field inside it.
    if (composer && !(event.relatedTarget && composer.contains(event.relatedTarget))) { checkInNow(); }
  });
  // Opening or cancelling a card, or a box row's edit form, fetches the ticket page itself; say so at once.
  document.body.addEventListener("htmx:afterSettle", function (event) {
    var path = (event.detail && event.detail.requestConfig && event.detail.requestConfig.path) || "";
    if (/^\/tickets\/\d+(\?|$)/.test(path)) { checkInNow(); }
  });
})();
