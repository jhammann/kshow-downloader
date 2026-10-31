# Restyle brief — "Cinema" with a poster grid (2026-10-31)

Approved mockup: `docs/design/2026-10-31-cinema-poster-grid.html` (open it in a browser). The user
picked direction A ("Cinema": dark, warm gold accent) combined with C's two-column poster grid.

## Goals

Apply that look to every screen; the app is used mainly on a phone by a non-technical person.

1. **Login and auth pages** (`/login`, "Login failed", "Not allowed") render through the app's
   templates and stylesheet instead of bare HTML from `kshow/auth.py`. Layout as in the mockup:
   centred logo `k<b>show</b>`, tagline "Korean shows, straight to Plex", one full-width gold
   button "Log in with {provider}", small line "Only invited accounts can sign in". No nav bar or
   log-out footer on these pages.
2. **Provider name setting:** new env var `OIDC_PROVIDER_NAME` (default `Pocket ID`) used in the
   button text. Document it in the README env table and the compose example
   (`OIDC_PROVIDER_NAME: Hammann ID`).
3. **Visual system** (`kshow/web/static/app.css`): dark only (`color-scheme: dark`), tokens on
   `:root` — background `#0f1115`, surface `#171a21`, border `#262a33`, text `#e8e6e3`, muted
   `#8a8f98`, accent `#f5b301` (text on accent `#111`), ok `#5fd38d`, late/error `#ff7a7a`.
   System font stack, logo weight 800. Buttons ≥44px, inputs styled to match, focus rings visible.
   No horizontal scroll at 390px. On wider screens the grid may grow to 3–5 columns; keep the
   content width capped (~960px).
4. **Show list** (`/`, and the `/shows/_list` partial): sections "Following", "Stopped" and
   "Finished" (omit empty sections); two-column poster grid (2:3 posters); on each poster a status
   pill bottom-left and, while downloading, a 3px gold progress bar along the bottom edge; below
   the poster the English/folder name (one line, ellipsis) and `Korean title · S{n}` muted.
   Finished/stopped posters greyed (grayscale + lower opacity). "+ Add" gold pill in the header.
   Source-unreachable banner stays, restyled as a red-tinted card. Empty state: friendly card
   with an "Add your first show" button.
5. **Status pills**: replace the free-text-only status with a structured status, e.g.
   `ShowStatus(kind, label, progress)` where kind ∈ downloading | ok | late | waiting | finished |
   stopped. Pill colours: downloading = solid gold with dark text; ok = dark glass with green text;
   late = dark glass with red text; waiting/finished/stopped = dark glass with muted text. Keep a
   `status_line()` (or equivalent) so the existing tests' meaning is preserved; update tests that
   assert exact strings only where the wording genuinely changes.
6. **Posters**: add a nullable `poster_url` column to `Show`. Set it at creation from the TVDB
   match image (only `https://` URLs, same check as the existing `_safe_image`). Backfill: during
   `check_show`, when `tvdb_id` is set and `poster_url` is empty, fetch the series image via the
   TVDB client (lookup by id) and store it; a TvdbError is ignored. Shows without a poster get a
   generated placeholder: a gradient chosen deterministically from the show id plus the first
   letters of the name. Images use `loading="lazy"` and `referrerpolicy="no-referrer"`.
7. **Schema migration**: the app is now deployed with an existing SQLite DB, and `create_all` does
   not add columns. Add a small, idempotent step in `init_db` that adds any missing **nullable**
   columns declared on the models to existing tables (`ALTER TABLE … ADD COLUMN`), so upgrading
   keeps the data. Test it against a DB file created with the old schema (without `poster_url`).
8. **Other screens** in the same style:
   - Show detail: poster + title block at the top (Korean + English name, season, folder path),
     action buttons (Search now, Stop following) and the episode list as rows with pills.
   - Add: big paste field with a gold "Search" button; results as poster cards showing both the
     Korean and English names, year and network; the "English name or TVDB link" and "Add without
     TVDB" options below, de-emphasised.
   - Confirm: poster + names, the full target path, the "existing folder" note, form fields styled.
   - Settings: grouped cards (General, qBittorrent, Sources) with consistent fields.
   - Error/400/403 pages of the app use the same card look.
9. **Header**: `k<b>show</b>` logo left, nav (`+ Add` pill, Settings) right; log out stays in the
   footer as a subtle link.

## Constraints

- Keep all behaviour, security checks (require_user, same-origin POST check, HX-Redirect,
  sanitisation, validation) and routes unchanged. Jinja autoescape stays on.
- No CDN or external assets besides the TVDB poster images; htmx stays vendored.
- Keep the 30s list polling via `/shows/_list`.
- Test suite stays green and warning-free; add tests for: provider name on the login page, login
  page uses the stylesheet, structured status kinds, poster_url set on add and backfilled, the
  migration adding `poster_url` to an old DB, placeholder poster when none.
- Verify visually: run the app locally with `DISABLE_SCHEDULER=1` and some seeded shows (one
  downloading, one in Plex, one late, one finished, one without poster) and take Playwright
  screenshots at 390px and 1280px of: login, show list, show detail, add (with mocked results if
  practical), confirm, settings. Save them under `docs/design/screens/` and check them against the
  mockup. Fix anything that looks off before reporting.
