/* Ticket presence (#246): what the agent is doing on this ticket page, for the strip's check-in (app/ticket_presence.py).
   Loaded by ticket_detail.html. The page swaps <main> after every action, so everything here is defined once and reads the page
   when asked, never keeps its own copy of it. */
(function () {
  if (window.ticketPresenceVals) { return; }

  // The panel an open action names itself by: the nested menu entry if one is open, else the action. Each panel says what it is
  // doing in its own data-doing attribute; one that does not is called "using <its button>".
  function openPanel() {
    var action = document.querySelector("details.tp-action[open]");
    if (!action) { return null; }
    return action.querySelector("details.tp-item[open]") || action;
  }

  function doingOf(panel) {
    var said = panel.getAttribute("data-doing");
    if (said) { return said; }
    var summary = panel.querySelector("summary");
    return "using " + (summary ? summary.textContent.trim() : "an action");
  }

  function composing(panelSelector, radioId) {
    var panel = document.querySelector(panelSelector);
    if (!panel) { return false; }
    if (panel.contains(document.activeElement)) { return true; }
    var radio = document.getElementById(radioId);
    var box = panel.querySelector("textarea[name=body]");
    // A draft in the open tab counts as writing, even while they look at something else.
    return !!(radio && radio.checked && box && box.value.trim());
  }

  // What the check-in sends: the activity, the action's name, and the fingerprint of the strip now on the page.
  window.ticketPresenceVals = function () {
    var shown = document.querySelector("#presence .tp-presence-body");
    var vals = { activity: "viewing", doing: "", shown: shown ? shown.getAttribute("data-shown") || "" : "" };
    var panel = openPanel();
    if (panel) {
      vals.activity = "action";
      vals.doing = doingOf(panel);
    } else if (composing(".tp-compose-email", "mode-email")) {
      vals.activity = "email";
    } else if (composing(".tp-compose-note", "mode-note")) {
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
    if (event.target.closest && event.target.closest(".tp-composer")) { checkInNow(); }
  });
  document.addEventListener("toggle", function (event) {
    if (event.target.matches && event.target.matches("details.tp-action, details.tp-item")) { checkInNow(); }
  }, true);
})();
