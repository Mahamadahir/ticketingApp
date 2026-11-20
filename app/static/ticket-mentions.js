/* The @ list of the Add note composer (#254). Typing @ and a few letters asks the page for the agents the note could mention
   (GET /tickets/<n>/mentions?q=, which lists only agents who can open the ticket); choosing one writes the mention into the note.
   What the server reads back is unchanged: "@" followed by the part of the address before the @ (or the whole address when two
   agents share it), so a note typed by hand still works exactly as before. Without this script the box is a plain textarea.
   Defined once; it listens on the document, so it keeps working after the page swaps <main>. */
(function () {
  if (window.ticketMentions) { return; }
  window.ticketMentions = true;

  var timer = null;
  var asked = 0;
  var found = [];
  var active = -1;

  function areaOf(node) {
    return node && node.closest ? node.closest(".tp-compose-note") : null;
  }

  function textareaOf(area) {
    return area.querySelector("textarea[name=body]");
  }

  function listOf(area) {
    return area.querySelector(".tp-mentions");
  }

  // The @word the caret is in: an @ that does not follow a letter, a dot or another @ (the server's own rule), and what is typed after it.
  function tokenAt(textarea) {
    var caret = textarea.selectionStart;
    var before = textarea.value.slice(0, caret);
    var match = /(^|[^\w.@])@([A-Za-z0-9._-]*)$/.exec(before);
    if (!match) { return null; }
    return { start: caret - match[2].length - 1, caret: caret, query: match[2] };
  }

  function hide(area) {
    var list = listOf(area);
    var textarea = textareaOf(area);
    found = [];
    active = -1;
    if (list) { list.hidden = true; list.textContent = ""; }
    if (textarea) { textarea.removeAttribute("aria-activedescendant"); }
  }

  function mark(area) {
    var list = listOf(area);
    var textarea = textareaOf(area);
    Array.prototype.forEach.call(list.children, function (item, index) {
      item.setAttribute("aria-selected", index === active ? "true" : "false");
      if (index === active) { textarea.setAttribute("aria-activedescendant", item.id); }
    });
  }

  function show(area, agents) {
    var list = listOf(area);
    found = agents;
    active = agents.length ? 0 : -1;
    list.textContent = "";
    if (!agents.length) { list.hidden = true; return; }
    agents.forEach(function (agent, index) {
      var item = document.createElement("li");
      item.id = "mention-option-" + index;
      item.setAttribute("role", "option");
      item.className = "tp-mention";
      var name = document.createElement("b");
      name.textContent = agent.name;
      var email = document.createElement("span");
      email.className = "muted small";
      email.textContent = " " + agent.email;
      item.appendChild(name);
      item.appendChild(email);
      // mousedown, not click: the textarea would lose focus (and the list close) before a click landed.
      item.addEventListener("mousedown", function (event) { event.preventDefault(); choose(area, index); });
      list.appendChild(item);
    });
    list.hidden = false;
    mark(area);
  }

  function choose(area, index) {
    var textarea = textareaOf(area);
    var token = tokenAt(textarea);
    var agent = found[index];
    if (!token || !agent) { hide(area); return; }
    var text = "@" + agent.insert + " ";
    textarea.value = textarea.value.slice(0, token.start) + text + textarea.value.slice(token.caret);
    var caret = token.start + text.length;
    textarea.setSelectionRange(caret, caret);
    hide(area);
    textarea.focus();
  }

  function ask(area) {
    var textarea = textareaOf(area);
    var token = tokenAt(textarea);
    ++asked;  // any answer still on its way is stale from here on
    if (!token) { hide(area); return; }
    var url = area.getAttribute("data-mention-url");
    if (!url || !window.fetch) { return; }
    var mine = asked;
    fetch(url + "?q=" + encodeURIComponent(token.query), { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) { return response.ok ? response.json() : { agents: [] }; })
      .then(function (data) { if (mine === asked && document.activeElement === textarea) { show(area, data.agents || []); } })
      .catch(function () { hide(area); });
  }

  document.addEventListener("input", function (event) {
    var area = areaOf(event.target);
    if (!area || event.target.name !== "body") { return; }
    clearTimeout(timer);
    timer = setTimeout(function () { ask(area); }, 120);
  });

  document.addEventListener("keydown", function (event) {
    var area = areaOf(event.target);
    if (!area || event.target.name !== "body" || !found.length) { return; }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      active = (active + (event.key === "ArrowDown" ? 1 : found.length - 1)) % found.length;
      mark(area);
    } else if (event.key === "Enter" || event.key === "Tab") {
      event.preventDefault();
      choose(area, active);
    } else if (event.key === "Escape") {
      event.preventDefault();
      hide(area);
    }
  });

  document.addEventListener("focusout", function (event) {
    var area = areaOf(event.target);
    if (area && event.target.name === "body") { hide(area); }
  });
})();
