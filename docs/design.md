# Design

How the IT support ticketing app looks and reads, and the rules a new page or mockup follows. It describes the design
as built (Phase 9, on the 9A tokens). `docs/ui.md` is the implementation reference: which class to use, which template
draws what, and which module decides it. When the two disagree, the code wins and both documents are fixed.

## Who it is for

Two audiences, two frames.

- **Agents** (the IT team) live in the console all day. They scan queues, triage, reply and move tickets. The console
  is dense, calm and fast to read: small type, tables, chips, everything one click from the queue.
- **Requesters** (everyone else at the firm) mostly use email and Teams and visit the portal now and then. The portal is
  roomier and plainer: bigger type, one column of their own requests, the status in their words, never IT's.

Admins use the console with a second menu for the admin pages.

## Principles

1. **Plain and functional.** Semantic HTML first, CSS second. No decoration, gradients, illustrations or animation.
   A page is headings, cards, tables and forms.
2. **Tokens only.** Every colour, corner and font comes from `app/static/tokens.css`. A template or a page's CSS never
   holds a raw colour.
3. **Meaning is never colour alone.** A chip always carries the words ("Breached 2h ago", "P1", "Waiting for your
   reply"). The current nav item has `aria-current`, not just a fill.
4. **Light and dark are equal.** Every page is checked in both. Each text and fill pair is 4.5:1 or better in both
   themes, and a test enforces it.
5. **Server-rendered.** Jinja2 and htmx; no SPA. Menus and pop-overs are `<details>`, so they work without script.
6. **The server decides, the template draws.** Who sees a nav item, the wording of a status and the tone of an SLA chip
   come from Python (`app/navigation.py`, `app/messages.py`, `app/sla.py`). A template never chooses them.

## Colour

Light is the default (`:root`); dark is `.dark` on `<html>`; System (no stored choice) follows the browser's
`prefers-color-scheme`. Each person picks System, Light or Dark.

### Surfaces and text

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--bg` | `#F4F5F7` | `#0E1116` | Page background |
| `--surface` | `#FFFFFF` | `#161B22` | Cards, tables, the sidebar, the top bar, form fields |
| `--raised` | `#F8F9FB` | `#1C2230` | Hover rows, the search box, the admin menu column, IT's messages in the portal |
| `--border` | `#E1E4E8` | `#2A313C` | Every 1px line |
| `--text` | `#1B1F24` | `#E6EAF0` | Body text |
| `--muted` | `#57606A` | `#9AA4B2` | Secondary text, labels, table headers, timestamps |
| `--accent` | `#2F5BD3` | `#7AA2FF` | Links, primary buttons, the current item, the focus ring |
| `--accent-text` | `#FFFFFF` | `#0E1116` | Text on an accent fill |
| `--accent-soft` | `#E6EEFB` | `#1B2740` | The fill behind the current nav item, tab or segment, and the selected table row |
| `--note-bg` | `#FFF8EC` | `#221C12` | An internal note and the note composer |
| `--note-border` | `#F0D9B5` | `#4A3A1F` | Its border |
| `--focus` | `#2F5BD3` | `#7AA2FF` | The 2px focus ring |

### Tones

Five tone pairs, each a text colour on its own fill. They are used for chips, notices and the progress bar.

| Tone | Light text / fill | Dark text / fill | Means |
| --- | --- | --- | --- |
| Gray | `#3D444D` / `#EEF0F3` | `#C3CAD4` / `#232A35` | Neutral: counts, Closed, Scheduled, No SLA, P3 and P4, Never run, Waiting (outbound mail) |
| Blue | `#1F4FB8` / `#E6EEFB` | `#8FB2FF` / `#182440` | Information or in hand: Needs triage, Being worked on, Waiting for approval, SLA paused |
| Amber | `#8A4B00` / `#FFF1DC` | `#F5B95C` / `#2E2414` | Needs attention soon: At risk, P2, Waiting for your reply, Changed (a setting), Retrying, outage mode |
| Red | `#B42318` / `#FDECEA` | `#FF8A80` / `#3A1A1A` | Gone wrong or urgent: Breached, Missed, P1, Escalated, Failing, Given up, destructive actions |
| Green | `#1E6B3A` / `#E3F4E8` | `#6FD38F` / `#15291D` | Done well: Resolved, Met, Sent, Worked, Saved |

Pick the tone by meaning, from this table. Do not add a sixth tone or tint a tone for variety. Amber is also the
internal note's left bar, so a note never looks like something the requester can see.

## Type

Inter 4.0 variable, self-hosted (`app/static/fonts/`), falling back to `system-ui`. One family, no italics for
emphasis, no second display face.

| Role | Size / weight | Notes |
| --- | --- | --- |
| Body | 14px / 400, line height 1.5 | The console default |
| Page title `h1` | 24px / 700 | Portal: 28px on a page, 30px on the home greeting |
| Section `h2` | 20px / 700 | 16 to 18px inside a card or panel |
| `h3` | 16px / 600 | |
| Card section label | 13px / 700, uppercase, 0.05em tracking, muted | Side cards on the ticket page, panel sections |
| Table header | 12px / 600, uppercase, 0.04em tracking, muted | |
| Admin menu group title | 11px / 700, uppercase, 0.06em tracking, muted | |
| Field label | 12px / 600, muted | Above the control; the control's text is 14px / 500 |
| Chip | 12px / 600 | 13px in the portal's request list |
| Small and meta | 12 to 13px, muted | Timestamps, references, hints |
| Big figure | 20 to 22px / 700 | Admin home stats, mail intake figures |

Ticket references and times use tabular numerals (`font-variant-numeric: tabular-nums`). Headings are sentence case.
Line height 1.25 for headings, 1.5 to 1.6 for message bodies.

## Shape, space and depth

- **Corners:** 8px (`--radius`) for controls, buttons and small boxes; 12px (`--radius-lg`) for cards, tables, panels
  and messages; fully round for chips, avatars and count badges; 6px for items inside a segmented control or a side
  menu; 10px for the portal's extra-large button, notices and attention rows.
- **Lines:** 1px `--border` everywhere. A 2px line marks the current tab (accent underline), the chosen request kind
  (accent border) and the outage banner (amber). A 4px amber left bar marks an internal note.
- **Spacing:** steps of 2px, mostly 4, 8, 12, 16, 20 and 24. Gaps between cards are 12 to 16px; card padding is 16px by
  20px (20 to 24px for a form card). Page gutters are 28px in the console and 24px in the portal, 16px under 720px.
- **Depth:** flat. Only floating panels (the person menu, the bell, the ticket page's action panels) have a shadow,
  `0 8px 24px rgba(0, 0, 0, 0.12)` (0.18 for the action panels). Nothing else is raised by shadow; use `--raised` and a
  border instead.
- **Control heights:** 36px default, 32px compact, 40px in the top bar and for icon buttons, 44px for the portal's main
  button and for touch rows in the bell. Nothing clickable is under 32px.

## Layout

`app/templates/base.html` draws one of four frames.

- **Console:** a left sidebar (232px, sticky to the viewport; the app mark, navigation with counts, the person row at the
  foot) beside a top bar (search, theme, bell, Raise ticket) over the page. Content is at most 1360px wide.
- **Admin frame:** the console plus a 216px admin menu column (grouped links on `--raised`) beside the page.
- **Portal:** no sidebar. A top bar with the app mark, My requests, New request, Approvals, Settings, theme, bell and the
  initials menu, over a centred column at most 1120px wide.
- **Bare:** the app mark only, for sign-in pages.

Page patterns to reuse rather than invent:

| Pattern | Shape | Used on |
| --- | --- | --- |
| Queue | Header with count chips, tabs, a filter bar, one wide table | Agent queue, logs, outbound mail |
| Detail with side cards | A main column (flex 999, min 520px) beside a side column of cards (300 to 380px) | Agent ticket page, portal request page |
| List and panel | A table on the left, the chosen row's edit panel on the right (`tr.sel` highlights it); chosen by a link such as `?team=` | Users, Teams, Approval groups, Categories, Canned responses |
| Full-page editor | One card of sections, an `h3` each, with a sticky preview beside it when there is one | Request types, Email templates |
| Grouped settings | A list of groups beside a form card of setting rows (text left, control right) | Settings |
| Card grid | `repeat(auto-fit, minmax(240 to 320px, 1fr))` of status cards | Admin home, Mail intake, Outage mode, Data protection |

Everything wraps rather than scrolls sideways. Breakpoints, widest first: 1280px (the ticket page's action panels open
in the flow), 1000px (the admin menu moves above the page; edit panels go full width under the list), 800px (the
sidebar stacks above the page), 720px (16px gutters; pop-overs anchor to the bar), 600px (setting rows stack). Mobile
is not designed for yet; pages must stay usable on a narrow screen, not polished for it.

## Components

The full list, with the class names and when to use each, is in `docs/ui.md` under "Components". In design terms:

- **Chip:** a short pill of tone text on tone fill. Status, priority, SLA, counts, Changed. Words always inside.
- **Buttons:** default (surface, bordered), primary (accent fill, one per form or page), danger (red text on red fill
  with a red border). Primary is on the right of a button row. Disabled is 55% opacity.
- **Icon button:** a 40px bordered square with an `aria-label`; a red count badge on its corner for the bell.
- **Card:** surface, 1px border, 12px corners. An optional header row holds the title and its actions.
- **Table:** inside a bordered box that scrolls sideways when it must; uppercase muted headers; row hover `--raised`;
  no zebra stripes.
- **Tabs:** muted text, the current one in body text with a 2px accent underline. Counts sit in gray chips.
- **Segmented control:** two to four exclusive choices in a bordered strip; the current one on `--accent-soft`.
- **Form:** label above control, hint under it in muted text, error under it in red. Fields sit in a responsive grid
  (min 220px a column). Tick boxes sit beside their text.
- **Pop-over:** a `<details>` whose panel is a card with the floating shadow. One open at a time; Escape and a click
  outside close it.
- **Empty state:** a dashed bordered box, a bold line, then one sentence saying what to do.
- **Message:** a card with an avatar, the name, the time on the right and the body. An internal note is the note tint
  with the amber left bar. In the portal, IT's messages sit on `--raised` with an accent avatar.
- **Progress steps:** the portal request page's bar of steps (Received, Being worked on, Waiting for you, Resolved).
  Done and current steps are accent (amber while waiting for the requester, green when resolved); steps to come are
  `--border`.

Icons are 24px-grid line icons, 1.8px stroke, round caps and joins, drawn in `--accent` or `currentColor`, always
`aria-hidden` beside a text label. The request kinds use a fixed set (`app/request_icons.py`).

## Interaction states

| State | Look |
| --- | --- |
| Hover | Rows, nav items and default buttons take `--raised`; links turn `--text`; cards and tiles get an accent border; a primary button brightens slightly |
| Focus | A 2px `--focus` outline, 2px offset, on `:focus-visible` only |
| Current | `--accent-soft` fill with accent text and weight 600, plus `aria-current` |
| Open | An open menu's trigger has the accent outline; an open action button turns accent-soft with an accent border |
| Selected row | `--accent-soft` |
| Disabled | 55% opacity, `not-allowed` cursor |
| Unread | Bold text and an accent dot, plus "Unread:" for screen readers |

Motion is off. Nothing animates, and `prefers-reduced-motion` turns off anything a browser adds.

## Words

The interface is written in plain British English, sentence case, no exclamation marks.

- **Buttons say what happens:** Email user, Add note, Assign to me, Resolve, Save, Reset to default. Not OK, Submit or
  Confirm.
- **Requesters see their own wording of a status,** from `messages.requester_status`: Received, Being worked on,
  Waiting for your reply, Waiting for approval, Scheduled for Thursday 12 November, Resolved, Closed. With MSP and With supplier
  read as Being worked on. Never show an internal status name in the portal.
- **Agents see the real status** and the SLA as a sentence: "Breached 2h ago", "Paused · waiting on requester",
  "Met · resolved in time", "No SLA until triaged". The middle dot joins a state to its reason.
- **Empty states** say what is missing and what to do next, in one sentence.
- **Dates and times** are in the viewer's own time zone. Activity is grouped under Today, Yesterday, then the date.

## Do and don't

- Do reuse a component or page pattern above before adding a class. A new page-specific class goes in its own block at
  the end of `styles.css`, prefixed for the page (`.tp-*`, `.rq-*`, `.mail-*`).
- Do check every new page in light and dark, and at 720px wide.
- Don't use a raw colour, a new font, a shadow on a static element, or animation.
- Don't put a second primary button in a form, or use colour as the only signal.
- Don't restyle a page outside its own issue.

## Source files

`app/static/tokens.css` (the tokens), `app/static/styles.css` (the shell, components and page blocks),
`app/templates/base.html` (the frames), `app/navigation.py` (what the navigation holds), `docs/ui.md` (the class-level
reference), `tests/test_shell.py` (the contrast checks).
