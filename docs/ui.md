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
(Appearance) and with the button in the top bar, which steps System, Light, Dark and returns to the same page
(`POST /settings/theme`, `return_to` accepted only as a path on this site). The server writes the class on `<html>`, so
the page never flashes the wrong theme. For System no script is needed: the media query decides before first paint
(the app has no content security policy nonce, so the media query is the approach). `<meta name="color-scheme">` follows
the theme so form controls and scrollbars match.

## The shell

`base.html` draws one of three frames and puts the page into `<main id="main-content">` (an htmx form still swaps
`main`, so the frame stays put; an htmx partial carries no frame at all).

- **Console** (agents and super admins): a left sidebar (the app mark, the navigation with counts, the person and
  Sign out), a top bar (search, theme button, bell, Raise ticket) and the outage banner above both.
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
| `.field` around a label text and its control | A form field with a visible label above the control. Inputs, selects and textareas are styled by element |
| `.segmented` | Two to four exclusive choices that change a view or a mode (links or buttons; mark the current one with `.on`, `aria-selected="true"` or `aria-current="page"`) |
| `.tabs` with `.tab` | Page sections on one screen or sibling pages; mark the current tab with `.on`, `aria-selected` or `aria-current` |
| `dl.dl` | A definition list: label and value pairs |
| `.empty-state` | Nothing to show yet: a bold line, then one sentence on what to do |
| `.pagination` (via `_pagination.html`) | Previous, page of total, next |
| `.sr-only` | Text only screen readers get |
| `.skip-link` | The first link on every page (already in `base.html`) |
| `.count-badge` | The red number on an icon button |
| `.navlink` | A navigation item (drawn by the `nav_item` macro in `base.html`) |

Older classes still used by page bodies (`.badge`, `.status-*`, `.filters`, `.admin-tabs`, `.message`, `.message-internal`,
`.note`, `.error`, `.button-*`, `.team-card`, `.ticket-card`, `.sla-*`, `.outage-banner`) are kept and now read from the
tokens, so every page looks right in dark mode. New pages use the components above; the old classes go as each page is
restyled.

## Font

Inter 4.0 variable (`app/static/fonts/Inter-4.0-variable.woff2`), SIL Open Font License 1.1, licence in
`Inter-OFL-LICENSE.txt` beside it. Self-hosted: no request to a font service, so it works offline. The fallback is
`system-ui`. The file is served with a long cache, so a new version needs a new file name.
