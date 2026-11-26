/* The portal's mailbox forms (#309, #310): the people picker and the warning (the app's first modal, docs/ui.md "Modal"). Two forms
   use it: forwarding (the warning shows only when the copy is turned off) and giving access (data-warn="access": it ALWAYS shows).
   Without this script the form still posts: the server refuses until confirm_no_copy=1 / confirm_access=1 and shows the same
   warning as a block on the page. Choosing a person needs the search, which needs scripts. */
(function () {
  "use strict";
  var form = document.getElementById("mb-form");
  if (!form) { return; }

  // ---- The people picker: a result button stores the object id and name; "Choose someone else" shows the search again.
  var oid = document.getElementById("mb-oid");
  var nameField = document.getElementById("mb-name");
  var chosen = document.getElementById("mb-chosen");
  var chosenName = document.getElementById("mb-chosen-name");
  var search = document.getElementById("mb-search");
  var results = document.getElementById("mb-results");
  var query = document.getElementById("mb-q");

  form.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest(".mb-person") : null;
    if (!button) { return; }
    oid.value = button.getAttribute("data-oid");
    nameField.value = button.getAttribute("data-name");
    chosenName.textContent = button.getAttribute("data-detail") || button.getAttribute("data-name");
    chosen.hidden = false;
    search.hidden = true;
    results.innerHTML = "";
    var next = document.getElementById("mb-keep-copy") || document.getElementById("mb-full-access");
    if (next) { next.focus(); }
  });

  var change = document.getElementById("mb-change");
  if (change) {
    change.addEventListener("click", function () {
      oid.value = "";
      nameField.value = "";
      chosen.hidden = true;
      search.hidden = false;
      query.value = "";
      results.innerHTML = "";
      query.focus();
    });
  }
  // Enter in the search box must not send the whole form.
  if (query) {
    query.addEventListener("keydown", function (event) {
      if (event.key === "Enter") { event.preventDefault(); }
    });
  }

  // ---- The keep-a-copy warning.
  var dialog = document.getElementById("mb-dialog");
  var keepCopy = document.getElementById("mb-keep-copy");
  var confirmField = document.getElementById("mb-confirm-field");
  var access = form.getAttribute("data-warn") === "access";
  if (!dialog || typeof dialog.showModal !== "function" || !confirmField || (!access && !keepCopy)) { return; }

  var text = document.getElementById("mb-dialog-text");
  var countdown = document.getElementById("mb-countdown");
  var cancel = document.getElementById("mb-dialog-cancel");
  var confirm = document.getElementById("mb-dialog-confirm");
  var WAIT_SECONDS = 5;
  var timer = null;

  function stopTimer() {
    if (timer !== null) { window.clearInterval(timer); timer = null; }
  }

  function endText() {
    var date = form.elements.end_date.value;
    var time = form.elements.end_time.value;
    var parsed = new Date(date + "T" + time + ":00");
    if (isNaN(parsed.getTime())) { return date + " " + time; }
    return parsed.toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" }) + " at " + time;
  }

  var action = access ? "give access" : "turn off your copy";

  function waitText(left) {
    return "You can " + action + " in " + left + (left === 1 ? " second." : " seconds.");
  }

  // The same words as the server's (app/portal_mailbox.py: access_warning, copy_off_warning).
  function warningText(name) {
    if (!access) {
      return "Your mail will not be kept. Mail sent to you will go only to " + name +
        ". Nothing will arrive in your own mailbox until " + endText() + ".";
    }
    var parts = [];
    if (form.elements.full_access.checked) { parts.push(name + " will be able to read all mail already in your mailbox."); }
    if (form.elements.send_as.checked) { parts.push(name + " will be able to send email that appears to come from you."); }
    return parts.join(" ") + " This lasts until " + endText() + ".";
  }

  form.addEventListener("submit", function (event) {
    if (confirmField.value === "1") { return; }
    if (!access && keepCopy.checked) { return; }
    // Nothing chosen is the server's refusal to word, not a warning about nothing.
    if (access && !form.elements.full_access.checked && !form.elements.send_as.checked) { return; }
    // Likewise no person or no end yet: the server says so first, and the warning comes once the rest is in order.
    if (access && (!oid.value || !form.elements.end_date.value)) { return; }
    if (event.submitter && /^confirm_/.test(event.submitter.name)) { return; }
    event.preventDefault();
    var name = nameField.value || "the person you chose";
    text.textContent = warningText(name);
    var left = WAIT_SECONDS;
    confirm.disabled = true;
    countdown.textContent = waitText(left);
    dialog.showModal();
    cancel.focus();
    stopTimer();
    timer = window.setInterval(function () {
      left -= 1;
      if (left > 0) {
        countdown.textContent = waitText(left);
      } else {
        stopTimer();
        confirm.disabled = false;
        countdown.textContent = "You can now " + action + ".";
      }
    }, 1000);
  });

  cancel.addEventListener("click", function () { dialog.close(); });
  // Escape closes a modal dialog by itself; either way the countdown stops and the send button gets focus back.
  dialog.addEventListener("close", function () {
    stopTimer();
    confirm.disabled = true;
  });
  confirm.addEventListener("click", function () {
    if (confirm.disabled) { return; }
    confirmField.value = "1";
    stopTimer();
    dialog.close();
    form.submit();
  });
})();
