# Seasons and Search-on-Demand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One show page with every TVDB season; only episodes airing after the add (and new seasons) are fetched automatically; older episodes/seasons are fetched with one-shot Search buttons.

**Architecture:** A new `seasons` table sits between `shows` and `episodes`; episodes get a `monitored` flag. The scheduled check syncs all seasons from TVDB, applies the monitoring rules, and grabs only monitored episodes. A new `kshow/manual.py` runs one-shot searches under the existing check lock. A versioned SQLite migration (PRAGMA `user_version`) converts the deployed database, after a file backup.

**Tech Stack:** unchanged (Python 3.12, FastAPI, SQLAlchemy 2 + SQLite, httpx, respx, pytest).

**Spec:** `docs/superpowers/specs/2026-11-02-seasons-and-monitoring-design.md` (read the "Rules settled after the builder check" section — those rules are binding).

## Global Constraints

- The deployed DB has one show (The Wonder Coach, TVDB 468944, tvdb_season 2, release_season 2, season_folder `Season 2`, episodes 1–12, grabs history). The migration must keep every row and all grab ids (grab ids are qBittorrent tags `kshow-{id}`).
- Back up the DB file to `kshow.db.bak-<YYYYmmddHHMMSS>` (same directory) before migrating; never migrate an in-memory DB with a backup step.
- "Today" for monitoring = date in `Asia/Seoul` (`kshow/clock.py: today_kst() -> date`). Timestamps stay naive UTC (`db.utcnow`).
- Monitoring rule: an episode in a followed season is monitored iff `aired is None or aired >= season.followed_since`. New seasons detected later get `followed_since = today_kst() - 14 days`. Seasons followed at add get `followed_since = today_kst()`.
- "New season" = TVDB season number > `show.max_known_season`. Never auto-follow a lower season.
- Release season default = TVDB season number (the parser already maps "no number" to 1).
- Automatic grabs only for monitored, not-done episodes of followed seasons of following shows. Releases for episodes TVDB doesn't list: auto-grab only when the release air date ≥ `season.followed_since`.
- Manual search never sets `monitored = True`. Missing (previously downloaded, file gone) episodes get `monitored = False` when searched manually.
- Season 0 (specials) is ignored everywhere.
- Keep: require_user + same-origin on all UI routes; qBittorrent category isolation; never overwrite or delete in /tv; notifications never raise.
- Conventional commits (`feat:`/`fix:`), no AI attribution. Suite green and warning-free (`uv run pytest -q -W error`).

## Review Focus

1. **Migration on the real DB shape** (old `shows`/`episodes`, grabs pointing at episode ids, settings): no row lost, grab→episode links intact, SQLite schema text of `grabs` still references `episodes` (not a renamed temp table). Test in Task 3.
2. **First check after migration sees TVDB season 1** of The Wonder Coach: it must be created unfollowed with zero monitored episodes and nothing grabbed. Test in Task 4.
3. **Season 1 releases have no number; season 2 releases have " 2" or "시즌2"**: a season-1 search must never pick a season-2 release and vice versa. Tests in Tasks 1 and 5.
4. **Search season with a deleted (Missing) episode and an unaired episode**: neither is searched; only never-downloaded aired episodes are. Test in Task 5.
5. **A manual search running while a scheduled check runs**: they never overlap (same lock) and never grab the same episode twice. Test in Task 5.

---

## File Structure

```
kshow/clock.py          today_kst()                                   Task 3
kshow/releases.py       "시즌N" season form                             Task 1
kshow/tvdb.py           all_episodes(); TvdbEpisode.season              Task 2
kshow/db.py             Show/Season/Episode models                      Task 3
kshow/migrations.py     migrate(engine, db_path)                        Task 3
kshow/pipeline.py       multi-season check, monitoring, new seasons     Task 4
kshow/downloads.py      season-aware filing paths                       Task 4
kshow/sources/torrenttip.py  search(query, page=1)                      Task 5
kshow/manual.py         search_episodes(...)                            Task 5
kshow/scheduler.py      trigger_search(...)                             Task 5
kshow/web/routes.py, templates/show.html, confirm.html, _show_list.html Task 6
README.md                                                               Task 7
```

---

### Task 1: Parse the "시즌N" season form

**Files:** Modify `kshow/releases.py`; Test `tests/test_releases.py`

**Interfaces:** Produces unchanged `parse_release_name(name) -> Release | None`, now also accepting `<title> 시즌N.E..` and `<title>시즌N.E..` (optional space, `시즌 N` allowed).

- [ ] **Step 1: failing tests**

```python
def test_sijeun_season_form():
    r = parse_release_name("우리들의 발라드 시즌2.E04.261026.720p-NEXT")
    assert (r.title, r.season, r.episode) == ("우리들의 발라드", 2, 4)
    r = parse_release_name("신인감독 김연경 시즌2.E04.261101.720p-NEXT.mp4")
    assert (r.title, r.season, r.episode, r.group) == ("신인감독 김연경", 2, 4, "NEXT")
    assert parse_release_name("심야괴담회 시즌 6.E18.261026.720p-NEXT").season == 6

def test_number_inside_title_is_not_a_season():
    assert parse_release_name("신인감독 김연경.E09.251123.1080p-F1RST").season == 1
```

- [ ] **Step 2:** run, expect FAIL. **Step 3:** extend the regex season group to `(?:\s*시즌\s*(?P<season>\d{1,2})|\s+(?P<season2>\d{1,2}))?` (or equivalent) before `\.E`. **Step 4:** full suite passes. **Step 5:** commit `feat: recognise the 시즌N season form in release names`.

---

### Task 2: TVDB — all episodes of a series

**Files:** Modify `kshow/tvdb.py`; Test `tests/test_tvdb.py`

**Interfaces:** Produces `TvdbEpisode(number: int, aired: date | None, season: int = 0)` and `TvdbClient.all_episodes(series_id: int) -> list[TvdbEpisode]`: `GET /series/{id}/episodes/default?page=N` starting at 0, following while `links.next` is non-null (cap 20 pages), skipping `seasonNumber == 0`, same shape validation → `TvdbError` as the other methods. Keep `season_episodes` (used by nothing after Task 4; remove it in Task 4 if unused).

- [ ] Tests: two pages (page 0 with `links.next` set, page 1 with `links.next: null`) → episodes from both, season 0 dropped, `season` set; malformed `episodes` → `TvdbError`; 404 → `TvdbError`.
- [ ] Implement, full suite, commit `feat: fetch every season's episodes from TVDB`.

---

### Task 3: Schema v2 and migration of the deployed database

**Files:** Create `kshow/clock.py`, `kshow/migrations.py`; Modify `kshow/db.py`; Test `tests/test_migrations.py`, `tests/test_db.py`

**Interfaces — Produces (later tasks use exactly these):**

```python
class Show:      # shows
    id; search_title: str; tvdb_id: int | None; folder_name: str
    status: str = "following"          # following | stopped | finished
    poster_url: str | None; added_at: datetime
    max_known_season: int = 0          # highest season known at add/migration/sync
    seasons: relationship -> list[Season], order_by Season.number

class Season:    # seasons, unique (show_id, number)
    id; show_id: FK shows.id; number: int; release_season: int
    folder: str                        # e.g. "Season 2" / "Season 01"
    followed: bool = False; followed_since: date | None; first_aired: date | None
    show: relationship -> Show

class Episode:   # episodes, unique (season_id, number)
    id; season_id: FK seasons.id; number: int; tvdb_airdate: date | None
    state: str = "wanted"; filed_path: str | None; overdue_notified: bool = False
    monitored: bool = False
    season: relationship -> Season
```

`Grab` unchanged. `kshow.clock.today_kst() -> date` (zoneinfo `Asia/Seoul`). `kshow.migrations.migrate(engine, db_path: Path | None) -> None`; `init_db` calls it before `create_all` / seeding.

**Migration algorithm (`PRAGMA user_version`):**
- No `shows` table → fresh DB: `create_all`, set `user_version = 2`.
- `shows` exists and `user_version < 2`:
  1. If `db_path` given: copy the file to `kshow.db.bak-<timestamp>` (before opening a write transaction).
  2. In one transaction with `PRAGMA foreign_keys=OFF` and `PRAGMA legacy_alter_table=ON` (so `grabs`' FK text keeps saying `episodes`): rename `shows`→`shows_v1`, `episodes`→`episodes_v1`; `create_all` (creates new `shows`, `seasons`, `episodes`); copy:
     - shows: id, search_title, tvdb_id, folder_name, status, poster_url, added_at; `max_known_season = tvdb_season`.
     - seasons: one per old show: number = tvdb_season, release_season, folder = season_folder, followed = (status == "following"), followed_since = date(added_at), first_aired = season_first_aired.
     - episodes: same ids, season_id = that season, monitored = state != "downloaded" (wanted/failed/downloading stay monitored), other columns copied.
  3. Drop `*_v1`; set `user_version = 2`; commit. Any error → rollback, re-raise (startup fails loudly; the backup remains).
- `user_version >= 2`: nothing (the existing "add missing nullable columns" step still runs afterwards).

- [ ] **Step 1: failing tests** in `tests/test_migrations.py`, building an **old-schema** SQLite file by hand (the v1 DDL: shows with search_title/release_season/tvdb_id/tvdb_season/folder_name/season_folder/status/season_first_aired/poster_url/added_at; episodes with show_id/number/... unique(show_id, number); grabs with episode_id FK; settings; sources) and inserting The Wonder Coach rows (E1–E4 downloaded, E5–E12 wanted, one grab `kshow-7` on E4 `add_failed`):
  - `test_migrates_wonder_coach_losslessly`: after `init_db(f"sqlite:///{path}")`: one show, one season (number 2, release_season 2, folder "Season 2", followed True), 12 episodes with the same ids, E1–4 downloaded & unmonitored, E5–12 wanted & monitored, the grab still points at E4's id with tag `kshow-7`, `max_known_season == 2`, `PRAGMA user_version == 2`.
  - `test_backup_file_written`: a `kshow.db.bak-*` file exists next to the DB with the old schema.
  - `test_grabs_fk_still_references_episodes`: `sqlite_master.sql` for `grabs` contains `REFERENCES episodes` and not `episodes_v1`.
  - `test_migration_is_idempotent`: running `init_db` twice changes nothing and writes no second backup.
  - `test_fresh_db_is_v2`: a new file gets the v2 tables and `user_version == 2`, no backup.
- [ ] **Step 2:** run, FAIL. **Step 3:** implement models, `clock.py`, `migrations.py`, wire into `init_db`; update every existing reference to `Show.tvdb_season/release_season/season_folder/season_first_aired` and `Episode.show_id` **only as far as needed to keep the suite importable** — tests for pipeline/downloads/web that depend on the old shape may be marked `xfail(strict=False, reason="seasons rework, Task 4-6")` and must be un-xfailed by the task that reworks that module. **Step 4:** suite passes (with those xfails). **Step 5:** commit `feat: seasons table, monitored episodes and a migration of the existing database`.

---

### Task 4: Multi-season automatic check, monitoring and new seasons

**Files:** Modify `kshow/pipeline.py`, `kshow/downloads.py`; Test `tests/test_pipeline.py`, `tests/test_downloads.py` (un-xfail and adapt)

**Interfaces:**
- Consumes Task 2 `all_episodes`, Task 3 models and `today_kst`.
- Produces:
  - `select_releases(...)` unchanged signature (called per season with `season.release_season`).
  - `async grab_selected(session, deps, show: Show, season: Season, sources: dict[str, TorrenttipSource], selected: dict[int, Candidate]) -> None` (the current `_grab`, made public and season-aware: label `"{folder} S{season.number:02}E{n:02}"`).
  - `async def search_candidates(session, deps, sources, query: str, pages: int = 1) -> list[Candidate]` (current `_search`, plus pages support once Task 5 adds paging; until then pages=1).
  - `async def sync_seasons(session, deps, show: Show) -> bool` (returns whether TVDB answered).
  - `check_show(deps, show_id)` / `check_all_shows(deps)` unchanged signatures.
- `downloads.py`: target path uses `ep.season.folder` and `ep.season.number`; label uses the season number.

`check_show` order: 1 `sync_seasons` (create seasons: followed iff number > max_known_season → followed_since = today_kst() − 14 days, notify "New season" / "<Folder> season N is announced; kshow will follow it."; update max_known_season; upsert episodes: new rows `monitored = season.followed and (aired is None or aired >= followed_since)`, existing rows only get `tvdb_airdate` updated) → poster backfill as today → 2 library scan per season folder → 3 one search with `show.search_title` → 4 per followed season: wanted = monitored and state not in (downloaded, downloading); unlisted release episodes with `release.airdate >= followed_since` are created monitored and wanted; drop failed posts → 5 `select_releases` + `grab_selected` per season → 6 overdue: monitored + wanted only → 7 finished: TVDB ended and every monitored episode downloaded.

- [ ] **Tests (new or adapted):**
  - `test_first_check_after_migration_does_not_backfill_season_1`: show with max_known_season 2 and season 2 followed; TVDB returns season 1 (E1–E9 aired 2025) and season 2; search returns all season-1 and season-2 releases → season 1 created unfollowed, 0 monitored episodes, no `/torrents/add` for season 1.
  - `test_add_time_monitoring_only_future`: season followed_since = 2026-11-02 (today_kst monkeypatched); TVDB E3 aired 2026-10-25, E5 aired 2026-11-08, E6 aired None → only E5, E6 monitored; a released E3 is not grabbed.
  - `test_episode_airing_today_is_monitored`.
  - `test_new_season_is_followed_with_14_day_grace_and_notifies_once`: TVDB adds season 3 (E1 aired 20 days ago, E2 aired 10 days ago, E3 not aired) → season 3 followed, E2 and E3 monitored, E1 not; "New season" notification once across two checks.
  - `test_unlisted_release_after_followed_since_is_grabbed` and `..._before_is_not`.
  - `test_season_1_release_never_fills_season_2` (title match + release_season).
  - `test_overdue_only_for_monitored`.
  - `test_finished_requires_all_monitored_downloaded`.
  - downloads: `test_files_into_the_episodes_season_folder` (season 1 episode → `Season 01` or existing `Season 1`).
  - All previously passing behaviour tests (rejections, outages, adoption, notifications) un-xfailed and green.
- [ ] Implement, full suite green with **no** remaining xfails in pipeline/downloads, commit `feat: follow only new episodes and new seasons automatically`.

---

### Task 5: One-shot manual search

**Files:** Create `kshow/manual.py`; Modify `kshow/sources/torrenttip.py`, `kshow/scheduler.py`, `kshow/pipeline.py` (pages in `search_candidates`); Test `tests/test_manual.py`, `tests/test_sources.py`, `tests/test_app.py`

**Interfaces:**
- `TorrenttipSource.search(query: str, page: int = 1) -> list[Candidate]` (page > 1 adds `&page=N`).
- `search_candidates(..., pages: int)`: fetches pages 1..pages per source, stopping early when a page has no candidate whose normalized title equals the query's.
- `kshow.manual.episodes_to_search(session, season: Season, numbers: list[int] | None) -> list[Episode]`: for a single episode (`numbers=[n]`): that episode if not downloaded-and-present and not downloading (Missing episodes allowed; they get `monitored=False`, state wanted, old done/failed/add_failed grabs deleted — the current retry logic). For a season (`numbers=None`): episodes with state not in (downloaded, downloading), `filed_path is None`, no `done` grab, and `tvdb_airdate is None or <= today_kst()`; never Missing ones.
- `async kshow.manual.search_episodes(deps, show_id: int, season_number: int, numbers: list[int] | None) -> None`: compute targets; nothing → return; search with `pages=5`; `select_releases` for the targets (skipping failed posts); `grab_selected`; for every target not selected → notify `"No release found"`, body `"No release found for <Folder> S01E03"`. Never sets monitored True.
- `kshow.scheduler.trigger_search(app, show_id: int, season_number: int, numbers: list[int] | None) -> None`: like `trigger_check`, under `app.state.check_lock`.

- [ ] **Tests:**
  - `test_season_search_skips_missing_and_unaired`: season 1 with E1 downloaded+present, E2 Missing (downloaded, file gone), E3 never downloaded aired, E10 unaired → only E3 grabbed.
  - `test_episode_search_on_missing_unmonitors_and_grabs`.
  - `test_not_found_notifies_once_per_episode` and that nothing is retried by a following `check_show` (still unmonitored).
  - `test_manual_search_follows_pages_until_found` (E1 only on page 2) and `test_pages_stop_when_no_title_match`.
  - `test_season_1_search_ignores_season_2_releases`.
  - `test_manual_failure_does_not_auto_retry`: a manually grabbed download that stalls → episode back to wanted, unmonitored, the next `check_show` grabs nothing.
  - `test_trigger_search_runs_under_check_lock` (same pattern as the existing trigger_check test).
- [ ] Implement, suite green, commit `feat: one-shot search for older episodes and seasons`.

---

### Task 6: Show page with seasons, add flow, list card

**Files:** Modify `kshow/web/routes.py`, `kshow/web/templates/show.html`, `confirm.html`, `_show_list.html`, `kshow/web/static/app.css` (only additions in the existing Cinema style); Test `tests/test_web.py`

**Routes (all under the existing router with require_user + same-origin):**
- `GET /shows/{id}?season=N` — season selector (links styled as tabs; default = latest followed season, else latest season); per season: follow toggle, release-name season field, "Search season" button, episode rows with pills: In Plex (downloaded+present), Missing, Downloading x%, Wanted (monitored), Not downloaded (unmonitored, aired), Not aired yet, Failed; a "Search" button on Missing / Not downloaded / Failed / Wanted rows.
- `POST /shows/{id}/seasons/{n}/search` → `trigger_search(app, id, n, None)`; redirect to the season.
- `POST /shows/{id}/seasons/{n}/episodes/{e}/search` → validation via `episodes_to_search`, then `trigger_search(app, id, n, [e])`. Replaces `/episodes/{n}/retry` (remove the old route and its template use).
- `POST /shows/{id}/seasons/{n}/follow` (form `followed=1|0`): turning on sets `followed_since = today_kst()` and marks not-done episodes with `aired is None or aired >= today` monitored; turning off unmonitors nothing done, just stops automatic grabs.
- `POST /shows/{id}/seasons/{n}/release-season` (int 1..99).
- Existing `/shows/{id}/search` ("Search now" = run the automatic check) and `/stop` stay.
- **Add flow:** confirm page lists TVDB seasons and which will be followed ("Season 2 and later: new episodes only"); `add_show` creates the Show (`max_known_season` = highest TVDB season) and Seasons (picked and later: followed, followed_since today_kst; earlier: unfollowed; folder via `season_folder_name`; release_season = number). Without TVDB: one season with the typed number, followed. Then `trigger_check`.
- **List card:** subtitle `Korean title · S{latest followed season}`; status computed from that season.

- [ ] **Tests:** season tabs render and switch; pills per state (incl. Missing, Not downloaded, Not aired yet); Search buttons present only where valid; each POST route calls the recorder with the right args and rejects cross-origin; follow toggle sets followed_since and monitored correctly; release-season validation; add flow creates the expected seasons for a TVDB show with seasons 1–2 picked at 2; no-TVDB add; old `/retry` route gone (404/405).
- [ ] Phone screenshots (390px) of the show page with two seasons into `docs/design/screens/seasons-*.png` (not committed) and check them; suite green; commit `feat: show page with every season and search buttons`.

---

### Task 7: Docs and wrap-up

**Files:** `README.md`

- [ ] Describe monitoring (new episodes only, new seasons followed, deleted never re-fetched), Search / Search season, the new notifications ("No release found", "New season"), and the automatic DB backup on upgrade. Commit `docs: seasons, monitoring and manual search`.
