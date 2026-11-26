/* The portal's mailbox forms (#309): the people picker and the keep-a-copy warning (the app's first modal, docs/ui.md "Modal").
   Without this script the form still posts: the server refuses a copy-off request without confirm_no_copy=1 and shows the same
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
    var next = document.getElementById("mb-keep-copy");
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
  if (!dialog || typeof dialog.showModal !== "function" || !keepCopy || !confirmField) { return; }

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

  function waitText(left) {
    return "You can turn off your copy in " + left + (left === 1 ? " second." : " seconds.");
  }

  form.addEventListener("submit", function (event) {
    if (keepCopy.checked || confirmField.value === "1") { return; }
    if (event.submitter && event.submitter.name === "confirm_no_copy") { return; }
    event.preventDefault();
    var name = nameField.value || "the person you chose";
    text.textContent = "Your mail will not be kept. Mail sent to you will go only to " + name +
      ". Nothing will arrive in your own mailbox until " + endText() + ".";
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
        countdown.textContent = "You can now turn off your copy.";
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
