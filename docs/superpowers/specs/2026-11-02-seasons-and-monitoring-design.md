# Seasons on one show page, and search on demand — Design

Date: 2026-11-02
Status: draft for review
Builds on: `docs/superpowers/specs/2026-10-30-kshow-downloader-design.md` (deployed, v1.3.0)

## Intent

kshow is deployed and works. Two behaviours don't match how the user wants to use it, and both
mirror Sonarr:

1. **Only future episodes are fetched automatically.** Today adding a show backfills every aired
   episode of the season. The user wants: after adding, only episodes that air from then on are
   grabbed by themselves. Older episodes are fetched only when the user asks.
2. **Older episodes and older seasons on demand, on one show page.** Today a "show" in kshow is
   exactly one season; season 1 would be a separate entry. The user wants one page per show with
   every season on it (like Sonarr), a "Search" button per episode and a "Search season" button,
   with kshow picking the best release automatically (same rules as for new episodes).

Unchanged and important: a deleted episode is never re-fetched automatically (current
"Download again" behaviour stays manual).

## Decisions made with the user

- Manual search picks the best release automatically (no interactive release list).
- One show page with all seasons, Sonarr-style; the existing show is converted automatically.
- When TVDB announces a new season of a followed show, kshow starts following it automatically.
- (Confirmed playback) Manual searches are one-shot: if nothing is found, notify
  "No release found for <Folder> S01E03" instead of searching in the background.

## Goals

- A show = one TVDB series with many seasons. Season 0 (specials) is ignored.
- Each episode has a **monitored** flag. Automatic checks only grab **monitored** episodes that
  are not downloaded/downloading. Overdue alerts only for monitored episodes.
- **On add:** the season the user picked and any later seasons are followed; within them only
  episodes that air **after the add** (air date > today, or no air date yet) are monitored.
  Already-aired episodes are listed, unmonitored. Earlier seasons are listed, unmonitored.
- **New season on TVDB** for a followed show: create it; its episodes are monitored (they air in
  the future). Detected during the normal check.
- **Manual search:** per episode ("Search") and per season ("Search season" = every episode of
  that season that is not in the library and not downloading). One-shot, best release picked by
  the existing `select_releases` rules (exact title, season match, 720p preference). Results
  feed the existing grab → qBittorrent → filing pipeline. One notification per episode not found.
- **Deleted episodes:** still shown as "Missing"; the button becomes the same one-shot "Search".
  A missing episode is never monitored automatically again (set monitored = false when it is
  detected missing or searched manually after deletion — see "Left to the builder").
- **Show page:** season selector (tabs or a select) defaulting to the latest followed season;
  per season: Search season button, episode rows with pills (In Plex / Missing / Not downloaded /
  Wanted / Downloading x% / Failed) and a Search button where relevant. The show list card shows
  the latest followed season.
- **Add flow:** pick the show on TVDB once (the season number typed after the title still selects
  which season is "current"; default = latest season on TVDB that has episodes). The confirm page
  shows the folder and which seasons will be followed.

## Non-goals

- Interactive release picking, quality upgrades, per-episode monitor toggles beyond what the
  above needs (the builder may add an "unmonitor" control only if it falls out naturally).
- Long-running weekly variety shows with year-seasons (still out of scope).
- Changing the torrent sources, qBittorrent handling, notifications or auth beyond what's needed.

## Anti-goals

- Re-downloading something the user deleted without being asked.
- Backfilling a whole series on add (disk filling).
- Losing or duplicating anything during the migration of the existing database (one show,
  The Wonder Coach season 2, with E01–E04 in Plex and grabs history).
- Grabbing a release of the wrong season (season 1 releases have no number; season 2 has " 2").

## Facts (verified 2026-11-02)

- Torrenttip search "신인감독 김연경" returns season 1 (`신인감독 김연경.E01…E09.25xxxx.1080p-F1RST`,
  no number) and season 2 (`신인감독 김연경 2.E01…E04`) on the first page, plus sidebar noise
  and other shows. Search results are paginated (`&page=2…7`); later pages contain mostly
  unrelated matches. Older episodes of long shows may only appear on later pages.
- Some releases spell the season as `시즌N` attached to the title, e.g.
  `우리들의 발라드 시즌2.E04.261026.720p-NEXT`, `심야괴담회 시즌6.E18…`, and the user's own manual
  E04 download was `신인감독 김연경 시즌2.E04.261101.720p-NEXT.mp4`. Today's parser does not
  recognise this form (the title would include "시즌2"). It should: `<title> 시즌N.E..` and
  `<title> N.E..` both mean season N.
- Today's data model (kshow/db.py): `Show` is one season (search_title, release_season, tvdb_id,
  tvdb_season, folder_name, season_folder, status, season_first_aired, poster_url…);
  `Episode(show_id, number, …)` unique on (show_id, number); `Grab(episode_id, …)`.
  `init_db` only adds missing nullable columns; it does not migrate data.
- TVDB: `GET /series/{id}/episodes/default?page=N` without `season` returns all episodes with
  `seasonNumber` (paginated). The current client fetches one season.

## Behaviour details

### Release season per season
Default mapping: TVDB season 1 ↔ releases without a number; season N ≥ 2 ↔ releases with " N" or
"시즌N". Stored per season and editable on the show page (a small "Release name uses season …"
field), because some shows number differently. The existing show keeps its current values.

### Automatic check (scheduler)
Per followed show: sync seasons and episodes from TVDB (all seasons, skip 0); create new
seasons (followed, episodes monitored); scan each season folder for files already present (as
today); search the sources once with the show's search title; select releases for **monitored,
wanted** episodes of followed seasons; grab as today. Overdue/finished logic uses monitored
episodes only. A show is "finished" when TVDB says ended and every monitored episode is in.

### Manual search
Runs immediately under the existing check lock (so it never overlaps a scheduled check).
Search the sources with the show's search title; if the wanted episodes are not all found on
page 1, follow result pages (up to a cap, e.g. 5 pages, stopping early when a page has no
release matching the show title). Pick per episode with `select_releases`. Grab found ones;
notify once per episode not found: "No release found for The Wonder Coach S01E03".
Manual search ignores the "failed post" blacklist? — No: keep skipping posts that failed before
(a different release may be picked); if nothing else exists, it's "not found".

### Folder and files
Same show folder; season folder per season via `season_folder_name` (reuses `Season 1` /
`Season 01` if present). File names unchanged (`<Folder> - SxxEyy.ext`).

### Migration of the existing database (must be automatic and lossless)
On startup, convert each existing `Show` (one season) into a show + one season with that
season's tvdb_season, release_season and season_folder; keep its episodes and grabs attached;
existing episodes: downloaded stay downloaded; wanted/failed episodes stay monitored (they were
being followed). Idempotent, runs inside one transaction, tested against a copy of the old
schema with realistic rows. Back up the SQLite file (`kshow.db.bak-<timestamp>`) before
migrating.

### Notifications
Existing ones unchanged. New: "No release found for <Folder> SxxEyy" (manual search only);
"New season" — "<Folder> season 3 is announced; kshow will follow it." when a new season is
created.

## Rules settled after the builder check (Claude's calls)

1. **"After the add" uses the date, not the time:** an episode is monitored when its air date is
   on or after the add date (so an episode airing tonight counts). "Today" is the date in
   Korea (`Asia/Seoul`), since air dates are Korean broadcast dates.
2. **Releases for episodes TVDB doesn't list yet** (TVDB often lags for Korean shows): still
   grabbed automatically for a followed season, but only when the release's own air date
   (YYMMDD in the name) is on or after the date the season started being followed. Such an
   episode row is created monitored. Without a parsable air date it is not auto-grabbed.
3. **What counts as a "new season":** a season number higher than every season known when the
   show was added (or migrated). Seasons below that (e.g. The Wonder Coach season 1, which the
   first check after migration will see on TVDB) are created unfollowed and unmonitored. For a
   new season, episodes airing on or after 14 days before it was detected are monitored (TVDB
   often adds a season a little late; anything older than that is left for manual search).
4. **Shows without TVDB** stay supported as a single manually defined season (number typed at
   add time). Episodes come from releases; monitoring uses rule 2 (release air date ≥ add date).
   The deployed show (The Wonder Coach) has TVDB id 468944.
5. **Manual search stays one-shot all the way:** pressing Search never makes an episode
   monitored. If a manually searched download later fails (stall, gone, no video, not a
   torrent), kshow notifies ("Download failed …", as today) and the episode returns to
   "Not downloaded" without an automatic retry. Monitored episodes keep today's automatic retry.
6. **"Search season" skips episodes that were downloaded before** (shown as "Missing") and
   episodes that haven't aired yet. A deleted episode is only fetched with its own Search
   button, so one tap can't re-download everything the user already watched and deleted.
7. **Manual search works on every show and season,** whether following, stopped or finished.
   "Stopped" / unfollowed only means "fetch nothing automatically".

## Left to the builder

- Exact schema (e.g. a `seasons` table vs. columns on episodes) and the migration mechanics,
  within the constraints above.
- Season selector UI details within the existing Cinema style; poster per season is optional.
- Whether "Stop following" applies to the show or per season (a per-season follow toggle is fine
  and probably natural).
- Exactly when a missing episode flips to unmonitored (at detection or at manual search) — either
  satisfies "never re-fetched automatically".
- Pagination cap and stop rule details.
