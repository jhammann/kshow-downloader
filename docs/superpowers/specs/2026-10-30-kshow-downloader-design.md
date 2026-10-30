# kshow-downloader — Design

Date: 2026-10-30
Status: draft for review

## Intent

A lightweight, Sonarr-like app for Korean TV shows, run as a Docker container on the user's Raspberry Pi NAS.

Today the process is manual. The user's wife sends a Korean title such as `신인감독 김연경 2` (the trailing `2` is the season). The user then:

1. looks up the English name on TVDB
2. creates a folder under `/nas/Media/TV`
3. finds the torrent on whichever torrenttip domain is live that day
4. adds it to qBittorrent
5. moves the finished file into the show folder so Plex picks it up

Sonarr does this well for Western shows. For Korean shows the titles are in Hangul, the releases come from Korean torrent sites that keep changing domain, and the release names don't follow Western scene conventions.

The app lets the **wife** add a show herself from her **phone**. After that it grabs each new episode **as soon as it's available** and files it into Plex without anyone touching it.

## Goals

- The wife pastes a Korean title (with an optional season number), picks the correct TVDB match, and the show is followed from then on.
- Followed shows are checked on a schedule. New episodes are grabbed as soon as they appear: the first available release, with 720p preferred when more than one is present at the same moment.
- Episodes of the followed season that have already aired are grabbed when the show is added (backfill).
- Torrents go into the **existing** qBittorrent container. When a download finishes, the video file is moved into `/tv/<English name>/Season NN/` with a Plex-friendly name.
- Source sites whose domain number changes (`torrenttip250.top` → `torrenttip251.top`) keep working without manual edits.
- Sources are swappable entries, so a dead site can be replaced by another with the same layout without a code change.
- Phone-first English UI behind OIDC login (Pocket ID).
- Apprise notifications.

## Non-goals

- No second torrent client. No built-in VPN handling (qBittorrent already runs behind gluetun).
- No quality upgrades. Once an episode is downloaded it's never replaced by a better release.
- Movies, Western shows, and anything Sonarr already handles are out of scope.
- No integration with Sonarr or Prowlarr. This app runs next to them, independently.
- No subtitle handling (Bazarr is already present).
- No multi-tenant or role-based permissions. Every logged-in user can do everything.

## Anti-goals (failure even if it "works")

- **Grabbing the wrong show.** Korean search results include spin-offs, behind-the-scenes specials, and other seasons that share the title. A wrong file in Plex is worse than a missing one.
- **Touching other torrents in qBittorrent.** Sonarr/Radarr also use this qBittorrent. The app may only see and change torrents in its own category.
- **Duplicates or disk filling.** Never more than one release per episode.
- **Running anything downloaded.** The app accepts only responses that are real `.torrent` files (`application/x-bittorrent` / bencoded). Download hosts are ad-heavy.
- **A UI the wife can't use alone on her phone.**

## Environment (observed on the Pi, 2026-10-30)

- Host `hammann-nas`, Raspberry Pi (aarch64, Debian bookworm), reachable at `100.80.128.54` over Tailscale.
- **qBittorrent** (`linuxserver/qbittorrent`) runs in gluetun's network namespace. Its web UI is at `http://<host>:8089` (`WEBUI_PORT=8089`) and its volumes are `/nas/Downloads:/downloads` and `/nas/Config/qbittorrent:/config`.
- **Plex** mounts `/nas/Media/TV:/tv`.
- **Sonarr** volumes: `/nas/Config/sonarr:/config`, `/nas/Media/TV:/tv`, `/nas/Downloads:/downloads`.
- All linuxserver containers use `PUID=1001`, `PGID=100`, `TZ=Europe/Amsterdam`.
- Also present: **FlareSolverr** (`:8191`), **apprise-api**, nginx proxy manager, tinyauth.
- Existing TV folders use the plain English name, with a year where needed for disambiguation (`Scrubs (2026)`, `The Agency (2024)`, `Bluey`).

## Source site findings (torrenttip250.top, 2026-10-30)

- Search: `GET /search?q=<query>`. No token is needed for GET; the request succeeded without cookies or Cloudflare.
- Each result is an `<a href="https://torrenttip250.top/t/<id>.html" title="<release name>">`, with a category label such as `[ 예능·오락 ]` and a date `YY/MM/DD`.
- Release names seen:
  - `신인감독 김연경 2.E03.261025.720p-NEXT`
  - `신인감독 김연경.E09.251123.1080p-F1RST`

  The pattern is `<Korean title>[ <season>].E<ep>.<YYMMDD airdate>.<res>-<group>`. With no season number the release is season 1.
- The post page has **no magnet link**. It links to an external host (`https://dc.filesbest.xyz/download/<id>/1`) that returns a real `.torrent` file (`content-type: application/x-bittorrent`). It was fetched with a `Referer` of the post page and needed no captcha.
- The homepage links to sister sites with the same numbering pattern: `torrentsome242.com`, `torrentrj249.com`, `torrentbam227.top`, `sometv122.top`. The user reports they sit behind Cloudflare challenges. **Their HTML layout has not been verified.**
- Old domains redirect to the current one (the user's observation). Dead site families also happen (torrentqq, torrentzota).

## Decisions made with the user

- **Standalone app**, not a Sonarr/Prowlarr indexer.
- Reuse the **existing qBittorrent**. No second instance.
- Volumes follow the Sonarr style: `/config`, `/tv`, `/downloads`.
- Grab **as soon as available**. Prefer **720p**. No upgrades later.
- The wife uses it herself, **on her phone**, **in English**, pasting the Korean title.
- **OIDC login for Pocket ID**, configured through environment variables. The user sets up the subdomain and proxy in nginx proxy manager.
- **Apprise** notifications.
- **FlareSolverr** is used to get past Cloudflare on sources that need it.
- **Scope is season-based series** like 신인감독 김연경. Long-running weekly variety shows (런닝맨, 나 혼자 산다), whose releases count up forever and which TVDB files under year seasons, are **out of scope for v1**. Episode numbers come straight from the release (`2.E03` → `S02E03`).
- **English names come from TVDB, never from translating.** The official English title often differs from a literal translation (신인감독 김연경 is "The Wonder Coach" on TVDB). Match cards show both the Korean and the TVDB English name so the wife can recognise the right show.

## Behaviour

### Adding a show

1. The user pastes text such as `신인감독 김연경 2`. A trailing number (also `시즌2`/`시즌 2`) is parsed as the season; with no number, the season is 1.
2. The app searches TVDB (v4 API) with the Korean title and shows the top matches as cards with poster, English name, year, and network.
3. The user taps a match. The confirmation shows the target folder (`/tv/<English name>/Season 02`) and the season, which can be edited.
4. The show is saved as *followed* with:
   - **search title**: the Korean title as pasted, without the season
   - **release season**: the number used in release names, so empty for season 1
   - **TVDB id**
   - **TVDB season**
   - **folder name**

   Release season and TVDB season are stored separately because TVDB sometimes numbers Korean seasons differently. The confirmation screen lets them differ.
5. A search runs immediately, which backfills episodes that already aired.

**Existing library (Claude's call).**
- On the confirmation screen, if a folder under `/tv` matches the TVDB name loosely (ignoring case, punctuation, and a trailing `(year)`), it is suggested as the target. The folder name is always editable.
- When a show is added, and before each check, the app scans the season folder for files that already exist and treats those episodes as downloaded, so it never grabs them again. It must recognise both `SnnEmm` names and raw release names.
- Real example, found on the NAS on 2026-10-30: `/nas/Media/TV/The Wonder Coach/Season 2/` holds `신인감독 김연경 2.E01.261011.720p-NEXT.mp4` through `E03`, all filed by hand. Adding 신인감독 김연경 2 must suggest this folder and grab nothing until E04.
- Season folders: an existing `Season 2` or `Season 02` folder is reused as is. New ones are created as `Season 02` (Sonarr style). Plex reads both.

**TVDB fallbacks (Claude's call).**
- If the Korean search finds nothing, the user can search by English name or paste a TVDB URL or id.
- As a last resort the show can be added without TVDB: the user types the folder name and the season, and episode numbers come straight from releases. Overdue, next-expected, and Finished are then unavailable.
- If TVDB has no English name, the folder name field is pre-filled with the romanised or original name, and the user can edit it.
- `TVDB_API_KEY` is required. `TVDB_PIN` is optional and only needed for subscriber keys.

### Checking for episodes

- A scheduler runs every 20 minutes (configurable). For each followed show it searches the sources in priority order with the search title.
- **Release matching is strict.** Parse the release name. Normalise both titles by removing whitespace and punctuation and comparing case-insensitively. The normalised release title must **equal** the normalised search title, and the release's season must equal the show's release season. Anything that doesn't parse is ignored. Extra words such as `비하인드` or `스페셜` are rejected.
- **Airdate sanity check (Claude's call).** When TVDB knows the season's first air date, reject releases whose airdate falls more than 7 days before it. This stops old season-1 releases or an older show with the same title from slipping in. When TVDB has no dates for the season, skip the check.
- Pick a release for each episode not yet grabbed. When several releases of the same episode are seen in one check, prefer 720p, then other resolutions, then the earliest posted.
- Multi-episode releases (`E01-E02`), `END` markers, and other name variants: the builder should handle the ones that turn up in real results and ignore anything ambiguous.

### Downloading and filing

1. Fetch the post page, follow the download link with the post page as `Referer`, and check that the response is a bencoded torrent.
2. Add it to qBittorrent through its Web API in category **`kshow`** (configurable). The save path sits under `/downloads` (for example `/downloads/kshow`).
3. Poll qBittorrent for completion.
4. On completion, choose the largest video file in the torrent and **move** it to `/tv/<Folder>/Season NN/<Folder> - SNNEMM.<ext>`. Create folders as needed. Then remove the torrent from qBittorrent along with its leftover files. Seeding stops; this matches the current manual workflow.
5. Record the history and send an Apprise notification: "The Wonder Coach S02E03 is in Plex".

Path mapping: qBittorrent reports its own paths (`/downloads/...`). The app mounts the same host folder at the same container path, so no translation is needed by default. A configurable path map covers setups where they differ.

A download that has made no progress for a configurable time (default 6 hours) is marked failed and removed. The next matching release, if any, is tried on the next check.

### When a show stops being followed

- After every TVDB-listed episode of the season is downloaded and TVDB shows the season or series as ended, the show moves to "Finished".
- TVDB data for Korean variety shows is often incomplete, so the app **never auto-stops on silence**. The user can always stop following a show by hand.
- To get the next season, the wife adds `<title> 3` as a new entry.

### Statuses shown in the UI

For each show:

- last episode downloaded
- what's in progress (with download %)
- next expected episode (from TVDB air date, when known)
- warnings: "E04 not found yet, 2 days late", "Source torrenttip unreachable"

Manual actions:

- *Search now*
- *Retry episode*
- *Stop following*. This never deletes files from `/tv`.

### Sources

- Each source has a name, a **domain template** (`torrenttip{N}.top`), a last-known-good URL, a priority, and an enabled flag. Sources are editable on the settings page.
- **Resolving the live domain:**
  1. Request the last-known URL and follow redirects. Save where it lands.
  2. If that fails, probe `N+1 … N+10` with the template.
  3. If still nothing works, mark the source unreachable and send a notification.
- **Cloudflare:** if a response is a Cloudflare challenge, retry through FlareSolverr (`FLARESOLVERR_URL`) and reuse the cookies and user agent it returns for later requests.
- **v1 must fully support torrenttip.** The source code should be split so that a new site with a different HTML layout needs only a new parser. Add a sister site as a working source only after checking that its layout matches. If it doesn't, list it as a known-but-unsupported site in the README.

### Notifications (Apprise)

- `APPRISE_URL` points at the apprise-api endpoint (for example `http://apprise-api:8000/notify/<key>`), with an optional tag.
- Events:
  - episode filed into Plex
  - download failed
  - episode overdue (once per episode)
  - source unreachable (once per outage)

### Auth (OIDC / Pocket ID)

- Environment variables: `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `APP_URL` (to build the redirect URI `APP_URL/auth/callback`), and optionally `OIDC_ALLOWED_USERS` and/or `OIDC_ALLOWED_GROUPS`.
- Authorization-code flow with discovery and a session cookie. The UI shows a single "Log in" button.
- If `OIDC_ISSUER` is unset, auth is disabled and a warning is logged. This is for local use and testing.

## How it gets built (builder's calls, stated)

- **Stack** (Claude's call): Python 3.12, FastAPI, SQLite in `/config`, APScheduler or an equivalent in-process scheduler, and httpx. Server-rendered pages (Jinja + a small amount of htmx) styled for phones. No separate SPA build.
- **Docker** (Claude's call): one image, multi-arch (`linux/arm64` required, `linux/amd64` for development). It honours `PUID`/`PGID`/`TZ`. A `docker-compose.yml` example uses:

  ```yaml
  volumes:
    - /nas/Config/kshow:/config
    - /nas/Media/TV:/tv
    - /nas/Downloads:/downloads
  environment:
    - PUID=1001
    - PGID=100
    - TZ=Europe/Amsterdam
    - QBIT_URL=http://<host>:8089
    - QBIT_USERNAME=...
    - QBIT_PASSWORD=...
    - TVDB_API_KEY=...
    - FLARESOLVERR_URL=http://<host>:8191
    - APPRISE_URL=...
    - OIDC_ISSUER=...
    - OIDC_CLIENT_ID=...
    - OIDC_CLIENT_SECRET=...
    - APP_URL=https://kshow.example
  ```

- Secrets come from environment variables only. Sources, the check interval, the quality preference, and the qBittorrent category are editable on the settings page and stored in SQLite.
- Episode file names use `SNNEMM` only, with no episode title, because TVDB episode titles for Korean shows are often missing.
- The folder name is TVDB's English series name, sanitised for the filesystem. When TVDB's name already contains a year, it is kept as is.
- Plex is expected to pick up new files through its own library scanning. Triggering a Plex scan via the API is optional and left to the builder.
- Tests: release-name parsing and title matching need thorough unit tests, built from the real release names above. The source parser is tested against saved HTML fixtures from torrenttip.

## Left to the builder

- Exact DB schema, module layout, and UI visuals within "simple, phone-first, English".
- Handling of the less common release-name variants found in real data.
- Retry/backoff details and logging.
- Whether to trigger a Plex library scan.
