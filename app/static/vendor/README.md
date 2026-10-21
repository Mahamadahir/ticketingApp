# Vendored front-end files

Served from `/static/vendor/`. The version is in the file name, so a new version is a
new URL and the files are cached for a year. Nothing is loaded from a third-party CDN.

| File | Source | SHA-256 |
|---|---|---|
| `htmx-2.0.11.min.js` | `dist/htmx.min.js` from the `htmx.org@2.0.11` npm package (BSD 0-Clause, `htmx-LICENSE.txt`) | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` |

To upgrade htmx: `npm pack htmx.org@<version>`, copy `package/dist/htmx.min.js` to
`htmx-<version>.min.js`, update the table and the `<script>` in `app/templates/base.html`,
delete the old file, and re-check every `hx-*` attribute in `app/templates` against the
htmx upgrade notes. `tests/test_static.py` fails if the hash here and the file disagree.
The `.js` files are marked `-text` in `.gitattributes` so line-ending conversion cannot change them.
