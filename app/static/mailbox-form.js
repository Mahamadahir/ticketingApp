/* The portal's mailbox forms (#309, #310, #311): the people pickers and the warning (the app's first modal, docs/ui.md "Modal"). Two forms
   use it: forwarding (the warning shows only when the copy is turned off) and giving access (data-warn="access": it ALWAYS shows).
   "Whose mailbox?" (#311) shows a second picker for the owner when "Someone else's" is chosen; the warning then names them.
   Without this script the form still posts: the server refuses until confirm_no_copy=1 / confirm_access=1 and shows the same
   warning as a block on the page. Choosing a person needs the search, which needs scripts. */
(function () {
  "use strict";
  var form = document.getElementById("mb-form");
  if (!form) { return; }

  // ---- The people pickers (the other person "mb", the mailbox owner "mo"): a result button stores the object id and name;
  // "Choose someone else" shows the search again. Each picker's elements share its prefix.
  function picker(prefix) {
    var oid = document.getElementById(prefix + "-oid");
    if (!oid) { return null; }
    return {
      oid: oid,
      nameField: document.getElementById(prefix + "-name"),
      chosen: document.getElementById(prefix + "-chosen"),
      chosenName: document.getElementById(prefix + "-chosen-name"),
      search: document.getElementById(prefix + "-search"),
      results: document.getElementById(prefix + "-results"),
      query: document.getElementById(prefix + "-q"),
      change: document.getElementById(prefix + "-change"),
      box: document.getElementById(prefix + "-picker")
    };
  }
  var target = picker("mb");
  var owner = picker("mo");

  form.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest(".mb-person") : null;
    if (!button) { return; }
    var which = [target, owner].filter(function (p) { return p && p.box && p.box.contains(button); })[0];
    if (!which) { return; }
    which.oid.value = button.getAttribute("data-oid");
    which.nameField.value = button.getAttribute("data-name");
    which.chosenName.textContent = button.getAttribute("data-detail") || button.getAttribute("data-name");
    which.chosen.hidden = false;
    which.search.hidden = true;
    which.results.innerHTML = "";
    var next = document.getElementById("mb-keep-copy") || document.getElementById("mb-full-access");
    if (next) { next.focus(); }
  });

  [target, owner].forEach(function (p) {
    if (!p) { return; }
    if (p.change) {
      p.change.addEventListener("click", function () {
        p.oid.value = "";
        p.nameField.value = "";
        p.chosen.hidden = true;
        p.search.hidden = false;
        p.query.value = "";
        p.results.innerHTML = "";
        p.query.focus();
      });
    }
    // Enter in the search box must not send the whole form.
    if (p.query) {
      p.query.addEventListener("keydown", function (event) {
        if (event.key === "Enter") { event.preventDefault(); }
      });
    }
  });

  // ---- Whose mailbox: "Someone else's" shows the owner's picker. For giving access it is always the person asking who gets the
  // access, so the "Give access to" picker goes; for forwarding the target picker stays (blank means the person asking).
  var access = form.getAttribute("data-warn") === "access";
  var ownerBlock = document.getElementById("mb-owner-block");
  var otherHint = document.getElementById("mb-other-target-hint");
  var whoseRadios = form.querySelectorAll("input[name=whose]");
  function isOther() {
    var checked = form.querySelector("input[name=whose]:checked");
    return !!checked && checked.value === "other";
  }
  function showWhose() {
    var other = isOther();
    if (ownerBlock) { ownerBlock.hidden = !other; }
    if (otherHint) { otherHint.hidden = !other; }
    if (access && target && target.box) { target.box.hidden = other; }
    var mine = document.getElementById("mb-mine-line");
    if (mine) { mine.hidden = other; }
  }
  Array.prototype.forEach.call(whoseRadios, function (radio) { radio.addEventListener("change", showWhose); });
  showWhose();

  // ---- The keep-a-copy warning.
  var dialog = document.getElementById("mb-dialog");
  var keepCopy = document.getElementById("mb-keep-copy");
  var confirmField = document.getElementById("mb-confirm-field");
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
    var other = isOther();
    var ownerName = owner && owner.nameField.value ? owner.nameField.value : "the mailbox owner";
    if (!access) {
      if (other) {
        return ownerName + "'s mail will not be kept. Mail sent to " + ownerName + " will go only to " + name +
          ". Nothing will arrive in their own mailbox until " + endText() + ", if the request is approved.";
      }
      return "Your mail will not be kept. Mail sent to you will go only to " + name +
        ". Nothing will arrive in your own mailbox until " + endText() + ".";
    }
    var parts = [];
    if (other) {
      if (form.elements.full_access.checked) { parts.push("You will be able to read all mail already in " + ownerName + "'s mailbox."); }
      if (form.elements.send_as.checked) { parts.push("You will be able to send email that appears to come from " + ownerName + "."); }
      return parts.join(" ") + " This lasts until " + endText() + ", if the request is approved.";
    }
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
    var chosenOid = isOther() ? (owner ? owner.oid.value : "") : (target ? target.oid.value : "");
    if (access && (!chosenOid || !form.elements.end_date.value)) { return; }
    if (!access && isOther() && !chosenOid) { return; }
    if (event.submitter && /^confirm_/.test(event.submitter.name)) { return; }
    event.preventDefault();
    var name = (target && target.nameField.value) || (isOther() ? (form.getAttribute("data-me") || "you") : "the person you chose");
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
