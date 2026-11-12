# UI foundation (Phase 9A)

The look of the app: design tokens, light and dark mode, the shell around every page and the shared component classes.
Plain CSS and server-rendered Jinja2; no framework. The values come from the owner-approved mockups (agent queue and
ticket page, light and dark). A page body is still the markup it had before; 9B (queue), 9C (ticket page) and later
issues move each page onto the classes below.

Files: `app/static/tokens.css` (tokens, the Inter font face), `app/static/styles.css` (shell, components, older page
classes), `app/templates/base.html` (the shell), `app/navigation.py` (what the navigation holds), `app/theme.py`
(the theme setting), `app/static/fonts/` (Inter and its licence).

## Tokens

CSS custom properties on `:root`. Never write a colour in a template or a component; use a token.

| Token | Use |
| --- | --- |
| `--bg` | Page background |
| `--surface` | Cards, tables, the sidebar, the top bar, form fields |
| `--raised` | Hover rows, the search box, anything one step above `--surface` |
| `--border` | Every 1px line |
| `--text`, `--muted` | Body text; secondary text (still 4.5:1 or better on `--bg`, `--surface` and `--raised`) |
| `--accent`, `--accent-text`, `--accent-soft` | Links, primary buttons and the current nav item; text on an accent fill; the soft fill behind the current item |
| `--gray`/`--gray-bg`, `--blue`/`--blue-bg`, `--amber`/`--amber-bg`, `--red`/`--red-bg`, `--green`/`--green-bg` | The five tone pairs: text colour and its fill |
| `--note-bg`, `--note-border` | The tint of an internal note and of the note composer |
| `--focus` | The focus ring |
| `--font`, `--radius` (8px), `--radius-lg` (12px) | Type and corners |

Light is `:root`. Dark is `.dark` on `<html>`. System (no stored choice) is `.system` on `<html>`, which repeats the dark
values inside `@media (prefers-color-scheme: dark)`; a test keeps the two copies identical. Every text and fill pair
in the table above is checked at 4.5:1 or better in both themes by `tests/test_shell.py`.

## Light and dark

A per-person setting: System (the default, stored as no value), Light or Dark (`User.theme`). It is changed on My settings
(Appearance) and with the button in the top bar, which steps System, Light, Dark and returns to the same page (its path; a search or filter in the address is not kept)
(`POST /settings/theme`, `return_to` accepted only as a path on this site). The server writes the class on `<html>`, so
the page never flashes the wrong theme. For System no script is needed: the media query decides before first paint
(the app has no content security policy nonce, so the media query is the approach). `<meta name="color-scheme">` follows
the theme so form controls and scrollbars match.

## The shell

`base.html` draws one of three frames and puts the page into `<main id="main-content">` (an htmx form still swaps
`main`, so the frame stays put; an htmx partial carries no frame at all).

- **Console** (agents and super admins): a left sidebar fixed to the viewport (the app mark, the navigation with counts, which scrolls on its own when it is taller than the screen, and the person row, always in view; below 800px it stacks above the page and scrolls with it), a top bar (search, theme button, bell, Raise ticket) and the outage banner above both.
- **Admin frame** (9H, every `/admin` page in the console): `.admin-frame` puts `nav.admin-nav[aria-label="Admin"]` beside `<main>`: the Admin home link, then a `.admin-group` per group (a `p.admin-group-title` over a `ul` of `a.admin-link`, the current one `aria-current="page"`). The groups and who gets each page are `ADMIN_GROUPS` and `allowed_admin_pages` in `app/navigation.py`, carried on `Shell.admin_home` and `Shell.admin_menu`. Below 1000px the menu sits above the page, its groups side by side.
- **Portal** (requesters on every page, and anyone on a `/portal` page): a top bar with the app mark, My requests, New
  request, Approvals, Settings (and Agent console for an agent), the theme button, the bell and Sign out. No sidebar.
- **Bare** (sign-in pages, nobody signed in): the app mark only.

The navigation is one definition in `app/navigation.py`. It decides which items a person gets from their role and the
permissions in `app/permissions.py` (Reports for `view_reports`; Admin for a super admin, or for the page an extra
permission opens; Canned responses for a super admin) and counts what they can see through `app/visibility.py`. A template
only draws what it is given. The routes still enforce access themselves.

The bell keeps its contract: the link polls `/notifications/count` every 30 seconds (`data-poll`) into a screen-reader
label ("Notifications (3)"), and the red number on the icon follows that text.

Accessibility built into the shell (WCAG 2.2 AA): a skip link to `#main-content`; landmarks (`header`, `nav`, `main`,
`role="search"`); `aria-current="page"` on the current item (the fill is not the only signal); `:focus-visible` rings;
every icon button has an `aria-label` or a screen-reader label; icons are `aria-hidden`; controls are at least 32px
high (the icon buttons 40px).

## Components

| Class | When to use it |
| --- | --- |
| `.chip` with `.t-gray`, `.t-blue`, `.t-amber`, `.t-red`, `.t-green` | A short status, priority, count or SLA label. The tone sets the colours; put the meaning in the text too, never colour alone |
| `.btn` | A default button or a link that looks like one |
| `.btn.btn-primary` | The one main action of a page or form |
| `.btn.btn-danger` | An action that loses something |
| `.btn-lg` | A 40px button (the top bar) |
| `.icon-btn` | A square icon-only button or link. It needs an `aria-label` or a `.sr-only` label |
| `.card`, `.card-header` | A bordered panel; the header holds a heading and, optionally, actions |
| `.table-box` around a `table.table` | A table: uppercase header row, row hover, and a box that scrolls sideways on a narrow screen |
| `.field` around a label text and its control | A form field with a visible label above the control. Inputs, selects and textareas are styled by element. See "Forms" below for the layout |
| `.segmented` | Two to four exclusive choices that change a view or a mode (links or buttons; mark the current one with `.on`, `aria-selected="true"` or `aria-current="page"`) |
| `.tabs` with `.tab` | Page sections on one screen or sibling pages; mark the current tab with `.on`, `aria-selected` or `aria-current` |
| `dl.dl` | A definition list: label and value pairs |
| `.empty-state` | Nothing to show yet: a bold line, then one sentence on what to do |
| `.pagination` (via `_pagination.html`) | Previous, page of total, next |
| `.sr-only` | Text only screen readers get |
| `.skip-link` | The first link on every page (already in `base.html`) |
| `.count-badge` | The red number on an icon button |
| `.navlink` | A navigation item (drawn by the `nav_item` macro in `base.html`) |

Older classes still used by page bodies (`.badge`, `.status-*`, `.filters`, `.message`, `.message-internal`,
`.note`, `.error`, `.button-*`, `.team-card`, `.ticket-card`, `.sla-*`, `.outage-banner`) are kept and now read from the
tokens, so every page looks right in dark mode. New pages use the components above; the old classes go as each page is
restyled.

## The person menu

One component for both frames (the `person_menu` macro in `base.html`): `details.person-menu` whose `summary` is the person row (console, `.person-menu-up`: the panel opens upward across the row) or the initials (portal: the panel opens downward at the right). The panel holds the items from `Shell.person_menu` in `app/navigation.py` (agents: My requests, My feedback, Settings, Appearance; the portal: Settings, Appearance) in a `nav[aria-label="Account"]`, then Sign out (a POST form to `/auth/logout`). A script at the foot of `base.html` closes it on Escape (focus returns to the summary) or a click outside. A template never decides which items a person gets.

## Forms

One layout, replacing runs of inline `label + input`.

- `.form-grid`: a responsive grid (`repeat(auto-fit, minmax(220px, 1fr))`, 16px gap). Each child is a `label.field` (label text above the control; add a `.field-hint` span for help and a `.field-error` for a message under the control). `.field-wide` spans the full row; `.field-narrow` caps a short field at 260px. Inputs, selects and textareas fill their field. A tick box is a `label.field-check` (the box beside its text), with `.field-wide` when it should take a row.
- `.form-actions`: the buttons row under the grid, shown right-aligned and wrapping on a narrow screen. It is `row-reverse`, so write the primary button first in the markup (Enter in a field submits the first button) and it shows last, on the right.
- `.filter-bar`: an inline form of `.field` selects plus its button(s), wrapping; each field takes at least 200px.

Field names, ids and validation belong to the page and do not change when a form moves onto this. First users: Admin > Users and My settings; the other admin pages move with the admin overhaul.

## Font

Inter 4.0 variable (`app/static/fonts/Inter-4.0-variable.woff2`), SIL Open Font License 1.1, licence in
`Inter-OFL-LICENSE.txt` beside it. Self-hosted: no request to a font service, so it works offline. The fallback is
`system-ui`. The file is served with a long cache, so a new version needs a new file name.

## Pages on the components

- **Admin home** (9H, `admin_home.html`, data from `app/admin_home.py`): `.admin-head` (`h1`, one muted line), `.card.admin-attention` with `ul.attention-list` of `li.attention` (a tone `.chip`, `.attention-text`, the link) or `p.attention-clear`, then `.admin-areas`, a grid of `section.card.admin-area` (`h2`, `dl.admin-stats` whose `div`s show the value above its label, `ul.admin-quick` of links).
- **Admin Users** (9I, `admin_users.html`): `.users-head` (`.admin-head`, `a.btn-primary.users-new` to `?new=1`), the filter form `.users-filters` (a `.form-grid` of `.field`s, `.form-actions`), `.users-cols` of `.users-list` (`.table-box` with `table.users-table`: `a.person-cell` with `.person-name` and `.person-email`, role and status `.chip`s, `tr.sel` for the open person; `_pagination.html`) beside `aside.card.users-panel` (`.up-person` with `.up-avatar`, `.up-chips`, then a `section.up-section` with an `h3` per job: each form is `.up-inline` or `.up-perms`, `.up-team` per team, `.up-actions` for the sign-in buttons). The panel goes below the list when the page is too narrow for both (flex wrap, under 1000px full width). The link review is `section.users-links#linked-identities` under them.
- **Admin Settings** (9J, `admin_settings.html`, `_setting_row.html`, `admin_settings_reset.html`): `.admin-head`, a `.filter-bar.settings-search` GET form (Find a setting), then `.settings-layout`: `nav.settings-groups` of `a.grp` (the current one `.on` with `aria-current="page"`, a plain link so no script) beside `form.card.settings-panel` (the group's `h2` and `.note`, a `.setting` per setting, then `.form-actions` with the primary Save and the Reset link). A `.setting` is `.setting-text` (`.setting-label` with the amber `Changed` chip, `.setting-help`, `.setting-default`) beside `.setting-input` (`.setting-control`: the input and `.setting-unit`; the message is a `.field-error` under it). Below about 600px the grid stacks the text above the input and the group list wraps above the form. Which group a setting is in comes from `app/settings_store.py`, never the template.
- **Agent queue** (9B, `tickets_list.html`): the first page moved over. Header with count chips, `.tabs` of links (current tab `aria-current="true"`, plus `.on`), a filter form of `.field` selects, `.table-box` with `table.table.queue-table`, `.empty-state` per view. The time-to-breach chip is `queue_sla` in `app/sla.py`. Page-specific classes are the `.queue-*` and `.ticket-*` ones at the end of the components section of `styles.css`.
- **Portal home** (9D, `portal_tickets_list.html`): the portal frame is the top bar of `base.html` (`.portal-bar`: brand, `.navlink` items, theme button, bell, and `details.person-menu` whose `summary.avatar` opens Settings and Sign out) over a centred `.content` of at most 1120px. The page is `.home-hero` (greeting, `.btn-xl` New request, `.tiles` of `a.tile`), then `.home-requests`: `.tabs.home-tabs` of links and `ul.req-list` of `li.req` (`a.req-main` with `.req-ref`, `.req-subject`, `.req-last`; `.req-side` with the status `.chip` and Reopen). Status words, tones and the last-update line come from `messages.requester_status` and `messages.requester_last_update`; every portal page uses them, never the internal status name.
- **Portal New request** (9F, `portal_new_ticket.html`): `div.nr` of `.nr-head`, then `.nr-cols`: `nav.nr-kinds` (step 1: `a.kind` cards, the chosen one `.on` with `aria-current="true"`, drawn with `kind_icon` from `_icons.html`; icons are the fixed set in `app/request_icons.py`) beside `form.nr-form.card` (step 2: `.nr-notice` blue approval notice, `label` fields, `.nr-hint`, `details.nr-agent` for agents, `.nr-actions` with the attach control and `.nr-send`). The My requests tiles use the same icons. **Portal approvals** (`portal_approvals.html`): `.ap` of `article.card.ap-card` (`.ap-head`, `dl.dl.ap-answers`, `form.ap-form`).
- **Agent ticket page** (9C, `ticket_detail.html`): header (`.tp-head`: chips, `h1`, one line, the action bar `.tp-actions`), then `.tp-main` (triage card, resolution, the composer, the timeline `ol.tp-thread`) beside `.tp-side` (cards: Service level, Details with `dl.dl.tp-dl`, Followers, Linked tickets, Approvals, MSP). An action is a `details.tp-action` whose `summary.btn` opens a `.tp-panel` (the `action` macro at the top of the template); a menu entry is a nested `details.tp-item`. The attach control (`_attach_control.html`: a visually hidden `input[type=file]` before a `label.btn` with the paperclip, the chosen names in `.tp-attach-names`) sits above the composer row on both forms. The composer is two `input.tp-mode` radios plus `.tp-compose-email` / `.tp-compose-note` panels, switched in CSS by `:checked`. A message is `article.tp-msg` (`.tp-msg-note` for an internal note: `--note-bg`, `--note-border`, an amber left bar), an event `div.tp-event`. Forms inside a card or panel use `.tp-form`, which stacks the labels above their controls. Wording of the SLA chips is `queue_sla` and `sla_rows` in `app/sla.py`.
- **Portal request page** (9E, `portal_ticket_detail.html`): `div.rq` of the header (`.rq-line` with the reference, kind and the status `.chip`; `h1` subject), `ol.rq-steps` (a `li.rq-step` per step with `.rq-step-done|current|todo` and `.rq-tone-*`; the current one has `aria-current="step"`, and each label ends in `.sr-only` text, from `messages.requester_steps`), then `.rq-cols`: `.rq-main` (the reply `.card.rq-reply`, `.rq-reply-wanted` while Awaiting user, with `_attach_control.html` labelled Add a file; `ol.rq-thread` of `article.rq-msg`, `.rq-msg-it` for IT) beside `.rq-side` (`.card.rq-card`, a small uppercase `h2`, `dl.dl`). Names in the thread come from `portal._conversation`; the status chip, steps and last update only from `messages.py`.
- **Admin Mail pages** (9L part 1): each opens with `.admin-head`. **Email templates** (`admin_email_templates.html`): a `section.mail-section` per audience (`AUDIENCES`) holding a `.table-box` of Template (link), What it is for, a Changed or Default `.chip`, and an Edit button; the editor (`admin_email_template_edit.html`, `/admin/email-templates/<key>`) is `.tpl-editor`: `form.card.tpl-form` (the `.form-grid` of Subject and Text, `.form-actions` with the primary Save, Reset to default, Back, then `.tpl-placeholders`) beside `aside.card.tpl-preview` (the htmx preview; it wraps below the form when the page is too narrow for both, and is sticky from 1100px). **Never follow** and **Thanks rules**: `.table-box`, `.empty-state`, the add forms as `form.card.mail-form` with a `.form-grid`; a rule's state is a `.chip` and its Edit is a `details.rule-edit` whose panel is the rule's own form (`.rule-panel`); the review queue's per-row form is `.candidate-form`. **Mail intake**: `.intake-cards` grid of `section.card.intake-card` (Subscription and Catch-up with a state chip and a `dl.dl`, Last mail received and Received not yet processed with `.intake-big`), `.intake-warnings` or `.attention-clear`, then the table of mail set aside. **Outbound mail**: a `.filter-bar` with the Waiting only `.field-check`, a `.table-box` of `table.mail-table` with a state `.chip` per email (gray Waiting, amber Retrying, red Given up, green Sent). Classes are the `.mail-*`, `.tpl-*`, `.rule-*` and `.intake-*` block at the end of `styles.css`.
- **Admin System and Logs pages** (9L part 2): **Scheduled jobs** (`admin_scheduled_jobs.html`): `p.job-status`, then a `.table-box` of `table.job-table` with a Last result `.chip` (red Failing, amber Did not finish, green Worked, gray Never run). **Outage mode** (`admin_outage.html`): `.outage-cards` of `section.card.outage-card` (Status with an On or Off chip, `dl.dl` and the action in `.form-actions`; Who is ready with a chip and `ul.outage-without`). **Data protection** (`admin_data_protection.html`): `.dp-cards` of two `section.card.dp-card` (each a form in the 9G layout), then `section.mail-section` with the waiting deletes in a `.table-box` or an `.empty-state`; the anonymise page (`admin_anonymise.html`) is `.dp-card`s, the confirmation `form.card.dp-card.dp-confirm`. **Audit log, Activity log and Feedback**: `.filter-bar` (selects and inputs as `.field`s, a `.field-check`, Filter and Clear), a `.table-box` of `table.log-table` (chips for the action, the result code and the rating), `.empty-state`, `_pagination.html`. Classes are the `.job-*`, `.log-table`, `.outage-*` and `.dp-*` block at the end of `styles.css`.
