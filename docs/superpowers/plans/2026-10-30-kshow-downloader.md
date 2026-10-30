# kshow-downloader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Dockerised, phone-first web app that follows Korean seasonal TV shows on torrenttip-style sites, sends new episodes to an existing qBittorrent, and files finished downloads into the Plex TV folder.

**Architecture:** One FastAPI process holds the web UI (Jinja + htmx), the OIDC login, and an in-process scheduler. The scheduler runs two jobs: `check_all_shows` (search the sources, select releases, add torrents to qBittorrent) and `process_downloads` (poll qBittorrent, move finished files into `/tv`, notify). State lives in SQLite in `/config`. External systems (sources, TVDB, qBittorrent, Apprise, FlareSolverr) each sit behind one small client module, which tests mock with `respx`.

**Tech Stack:** Python 3.12, uv, FastAPI, uvicorn, Jinja2, htmx 2 (vendored), SQLAlchemy 2 + SQLite, httpx, selectolax (HTML parsing), APScheduler 3 (AsyncIOScheduler), authlib + itsdangerous (OIDC/session), pydantic-settings. Tests use pytest, pytest-asyncio, and respx.

**Spec:** `docs/superpowers/specs/2026-10-30-kshow-downloader-design.md`

## Global Constraints

- Container paths: `/config` (DB, secrets), `/tv` (Plex library), `/downloads` (qBittorrent downloads; same host folder as qBittorrent's `/downloads`).
- Honour `PUID`, `PGID`, and `TZ`. The image must build for `linux/arm64`, and also for `linux/amd64`.
- Secrets come from environment variables only: `QBIT_URL`, `QBIT_USERNAME`, `QBIT_PASSWORD`, `TVDB_API_KEY`, `TVDB_PIN` (optional), `FLARESOLVERR_URL` (optional), `APPRISE_URL` (optional), `APPRISE_TAG` (optional), `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_ALLOWED_USERS` (optional, comma-separated), `OIDC_ALLOWED_GROUPS` (optional, comma-separated), `APP_URL`, `SESSION_SECRET` (optional; generated and stored in `/config/session_secret` if unset).
- Defaults stored in SQLite and editable in the UI:
  - check interval: 20 minutes
  - qBittorrent category: `kshow`
  - qBittorrent save path: `/downloads/kshow`
  - preferred resolution: `720p`
  - stall timeout: 6 hours
- Only torrents in the app's own category may be read or changed in qBittorrent.
- Only bencoded torrent data is ever sent to qBittorrent: it must start with `b"d"` and contain `b"4:info"`.
- Files are moved, never copied. The torrent is then deleted from qBittorrent along with its leftover files. Files in `/tv` are never deleted or overwritten.
- File name: `<Folder> - S{season:02}E{episode:02}.<ext>`. New season folders are named `Season {season:02}`; an existing `Season N` or `Season 0N` folder is reused.
- English show names come only from TVDB, never from translation.
- UI copy is English. Layout is phone-first (single column, at least 44px tap targets).
- Scope: seasonal series only. Episode number = the release's `E` number. Release season = the number after the title, or 1 when absent.

## Review Focus

1. **A different season or spin-off of the same title shows up in results.** `신인감독 김연경.E09…` (season 1) or `신인감독 김연경 비하인드.E01…` while season 2 is followed: never grabbed. Test is in Task 7.
2. **The download host returns an HTML ad page instead of a torrent.** It is rejected, nothing is sent to qBittorrent, and the episode stays wanted. Test is in Task 7.
3. **The torrent contains extra files** (`.txt`, `.nfo`, a sample clip, a subfolder). Only the largest video file is moved. Test is in Task 8.
4. **The episode already exists in `/tv` under the raw release name** (manually filed). It is never re-grabbed, and the destination is never overwritten. Tests are in Task 3 and Task 8.
5. **qBittorrent or a source is unreachable mid-check.** The job logs and continues with other shows, episodes stay wanted, and nothing crashes the scheduler. Test is in Task 7.

---

## File Structure

```
pyproject.toml
Dockerfile
docker/entrypoint.sh
docker-compose.example.yml
README.md
kshow/
  __init__.py
  config.py          # Settings (env) — Task 1
  releases.py        # parse_release_name, parse_user_input, normalize_title — Task 1
  db.py              # engine/session, ORM models, settings get/set — Task 2
  library.py         # existing-episode scan, folder suggestion, target paths, safe move — Task 3
  sources/
    __init__.py      # build_sources(db) — Task 4
    http.py          # SiteClient: domain resolution + FlareSolverr fallback — Task 4
    torrenttip.py    # TorrenttipSource: search + fetch_torrent — Task 4
  tvdb.py            # TvdbClient — Task 5
  qbit.py            # QbitClient — Task 6
  notify.py          # Notifier (Apprise) — Task 6
  pipeline.py        # select_releases, check_show, check_all_shows — Task 7
  downloads.py       # process_downloads — Task 8
  scheduler.py       # start/stop jobs — Task 9
  auth.py            # OIDC routes + require_user dependency — Task 10
  main.py            # create_app() — Task 9
  web/
    routes.py        # UI routes — Task 11
    templates/*.html
    static/htmx.min.js, static/app.css
tests/
  fixtures/torrenttip_search.html, torrenttip_post.html, torrenttip_home.html
  test_*.py
```

---

### Task 1: Project scaffold, settings, release-name parsing

**Files:**
- Create: `pyproject.toml`, `kshow/__init__.py`, `kshow/config.py`, `kshow/releases.py`, `.gitignore`
- Test: `tests/test_releases.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `Settings` (pydantic-settings, the env vars from Global Constraints, plus `config_dir: Path = /config`, `tv_dir: Path = /tv`, `downloads_dir: Path = /downloads`, all overridable by env `CONFIG_DIR`/`TV_DIR`/`DOWNLOADS_DIR`).
  - `get_settings() -> Settings` (cached). `Settings.auth_enabled -> bool` is `bool(oidc_issuer)`.
  - `@dataclass(frozen=True) Release(title: str, season: int, episode: int, airdate: date | None, resolution: str | None, group: str | None, raw: str)`. `season` is 1 when the name has no season number.
  - `parse_release_name(name: str) -> Release | None`
  - `normalize_title(s: str) -> str`: lowercase, with all whitespace and punctuation removed.
  - `parse_user_input(text: str) -> tuple[str, int]`: returns (title, season).

- [ ] **Step 1: Scaffold.** Run `uv init --package --name kshow --python 3.12`, then add the dependencies from the Tech Stack (dev: pytest, pytest-asyncio, respx). Set `asyncio_mode = "auto"` in `[tool.pytest.ini_options]`. In `.gitignore`, ignore `.venv`, `__pycache__`, and `/config`.

- [ ] **Step 2: Write the failing tests** in `tests/test_releases.py`:

```python
from datetime import date
from kshow.releases import parse_release_name, parse_user_input, normalize_title

def test_parses_season_release():
    r = parse_release_name("신인감독 김연경 2.E03.261025.720p-NEXT")
    assert (r.title, r.season, r.episode) == ("신인감독 김연경", 2, 3)
    assert r.airdate == date(2026, 10, 25)
    assert (r.resolution, r.group) == ("720p", "NEXT")

def test_no_season_number_means_season_1():
    r = parse_release_name("신인감독 김연경.E09.251123.1080p-F1RST")
    assert (r.title, r.season, r.episode, r.resolution) == ("신인감독 김연경", 1, 9, "1080p")

def test_strips_extensions_and_torrent_suffix():
    r = parse_release_name("신인감독 김연경 2.E03.261025.720p-NEXT.mp4.torrent")
    assert r.episode == 3 and r.group == "NEXT"

def test_end_marker_and_no_group():
    r = parse_release_name("어떤 드라마.E16.END.261020.1080p")
    assert (r.episode, r.group, r.resolution) == (16, None, "1080p")

def test_unparseable_returns_none():
    assert parse_release_name("신인감독 김연경 2 전편 모음") is None
    assert parse_release_name("어떤 드라마.E01-E02.261020.720p-NEXT") is None  # multi-episode: ignored (ambiguous)

def test_user_input_season_variants():
    assert parse_user_input("신인감독 김연경 2") == ("신인감독 김연경", 2)
    assert parse_user_input("신인감독 김연경 시즌2") == ("신인감독 김연경", 2)
    assert parse_user_input("신인감독 김연경 시즌 3 ") == ("신인감독 김연경", 3)
    assert parse_user_input("신인감독 김연경") == ("신인감독 김연경", 1)

def test_normalize_title():
    assert normalize_title("신인감독  김연경!") == normalize_title("신인감독김연경")
```

And `tests/test_config.py`: with `OIDC_ISSUER` unset, `Settings().auth_enabled is False`; with it set, `True`.

- [ ] **Step 3: Run the tests.** `uv run pytest tests/test_releases.py tests/test_config.py -v` should fail (ImportError).

- [ ] **Step 4: Implement.** Before parsing, strip a trailing `.torrent`, then a video extension (`mp4|mkv|avi|ts`). Use this regex:

```
^(?P<title>.+?)(?:\s+(?P<season>\d{1,2}))?\.E(?P<ep>\d{1,4})(?:\.END)?\.(?P<date>\d{6})\.(?P<res>\d{3,4}p)(?:[-.](?P<group>[^.]+))?$
```

The date is `YYMMDD`, read as 20YY. If the date is invalid, set `airdate=None` and still return the release.

- [ ] **Step 5: Run the tests.** `uv run pytest -v` should pass.

- [ ] **Step 6: Commit.** `git commit -am "feat: scaffold, settings, release-name parsing"` (add the new files first).

---

### Task 2: Database models

**Files:**
- Create: `kshow/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces (SQLAlchemy 2 declarative):
  - `init_db(url: str) -> sessionmaker`. Creates the tables and seeds defaults: the torrenttip source and the settings defaults.
  - `Show(id, search_title: str, release_season: int, tvdb_id: int | None, tvdb_season: int, folder_name: str, season_folder: str, status: str = "following" | "finished" | "stopped", season_first_aired: date | None, added_at: datetime)`
  - `Episode(id, show_id, number: int, tvdb_airdate: date | None, state: str = "wanted" | "downloading" | "downloaded" | "failed", filed_path: str | None, overdue_notified: bool = False)`. Unique on `(show_id, number)`.
  - `Grab(id, episode_id, post_url: str, release_name: str, qbit_tag: str, state: str = "downloading" | "done" | "failed", progress: float = 0, last_progress_at: datetime, created_at: datetime)`
  - `SourceRow(id, name: str, kind: str = "torrenttip", domain_template: str, current_url: str, priority: int, enabled: bool, unreachable_since: datetime | None)`
  - `get_setting(session, key) -> str` and `set_setting(session, key, value)`. Keys: `check_interval_minutes`=`"20"`, `qbit_category`=`"kshow"`, `qbit_save_path`=`"/downloads/kshow"`, `preferred_resolution`=`"720p"`, `stall_hours`=`"6"`, `path_map`=`""` (format `from=to`).
  - Seed source: `name="torrenttip"`, `domain_template="https://torrenttip{N}.top"`, `current_url="https://torrenttip250.top"`, `priority=1`.

- [ ] **Step 1: Write the failing tests.** On `sqlite://`, `init_db` seeds exactly one source named `torrenttip` and `get_setting(s, "qbit_category") == "kshow"`. Adding two `Episode`s with the same `(show_id, number)` raises `IntegrityError`. Running `init_db` twice on the same file doesn't duplicate the seeds.
- [ ] **Step 2: Run the tests.** Expect FAIL.
- [ ] **Step 3: Implement `kshow/db.py`.**
- [ ] **Step 4: Run the tests.** Expect PASS.
- [ ] **Step 5: Commit.** `feat: database models and seed data`

---

### Task 3: Library scan and filing helpers

**Files:**
- Create: `kshow/library.py`
- Test: `tests/test_library.py`

**Interfaces:**
- Produces:
  - `suggest_folder(tv_dir: Path, english_name: str) -> str | None`. Returns the name of the existing folder whose `normalize_title` matches after removing a trailing `(YYYY)` from both. Returns None when there's no match.
  - `season_folder_name(show_dir: Path, season: int) -> str`. Returns the existing `Season N` or `Season 0N` folder, otherwise `f"Season {season:02}"`.
  - `existing_episodes(season_dir: Path, season: int) -> set[int]`. Finds episode numbers from video files named `SxxEyy` (case-insensitive, where xx == season) or from raw release names (via `parse_release_name`, ignoring the title).
  - `target_path(tv_dir, folder_name, season_folder, season, episode, ext) -> Path`
  - `move_into_library(src: Path, dest: Path) -> Path`. Creates parent folders. Raises `FileExistsError` if `dest` already exists. Uses `shutil.move`.
  - `VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".ts"}`

- [ ] **Step 1: Write the failing tests** (`tmp_path`):

```python
def test_recognises_manually_filed_release_names(tmp_path):
    d = tmp_path / "The Wonder Coach" / "Season 2"; d.mkdir(parents=True)
    for e in (1, 2, 3):
        (d / f"신인감독 김연경 2.E0{e}.2610{10+e}.720p-NEXT.mp4").touch()
    (d / "notes.txt").touch()
    assert existing_episodes(d, 2) == {1, 2, 3}

def test_recognises_sxxeyy(tmp_path):
    (tmp_path / "The Wonder Coach - S02E04.mkv").touch()
    (tmp_path / "Other - S01E05.mkv").touch()
    assert existing_episodes(tmp_path, 2) == {4}

def test_suggest_folder_ignores_year_and_case(tmp_path):
    (tmp_path / "The Wonder Coach").mkdir()
    assert suggest_folder(tmp_path, "the wonder coach (2025)") == "The Wonder Coach"
    assert suggest_folder(tmp_path, "Something Else") is None

def test_reuses_existing_season_folder(tmp_path):
    (tmp_path / "Season 2").mkdir()
    assert season_folder_name(tmp_path, 2) == "Season 2"
    assert season_folder_name(tmp_path, 3) == "Season 03"

def test_target_path(tmp_path):
    assert target_path(tmp_path, "The Wonder Coach", "Season 2", 2, 4, ".mp4") == \
        tmp_path / "The Wonder Coach" / "Season 2" / "The Wonder Coach - S02E04.mp4"

def test_move_never_overwrites(tmp_path):
    src = tmp_path / "a.mp4"; src.write_text("x")
    dest = tmp_path / "out" / "b.mp4"; dest.parent.mkdir(); dest.write_text("old")
    with pytest.raises(FileExistsError):
        move_into_library(src, dest)
    assert dest.read_text() == "old"
```

- [ ] **Step 2: Run the tests.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.** Expect PASS.
- [ ] **Step 5: Commit.** `feat: library scan and filing helpers`

---

### Task 4: Sources — domain resolution, FlareSolverr, torrenttip parser

**Files:**
- Create: `kshow/sources/__init__.py`, `kshow/sources/http.py`, `kshow/sources/torrenttip.py`
- Create fixtures:

  ```
  curl -sL -A "Mozilla/5.0" https://torrenttip250.top/ -o tests/fixtures/torrenttip_home.html
  curl -sL -A "Mozilla/5.0" --get --data-urlencode "q=신인감독 김연경" https://torrenttip250.top/search -o tests/fixtures/torrenttip_search.html
  curl -sL -A "Mozilla/5.0" https://torrenttip250.top/t/JYy7vk.html -o tests/fixtures/torrenttip_post.html
  ```

  If 250 no longer resolves, use the domain it redirects to. Then check that the search fixture contains `신인감독 김연경 2.E03.261025.720p-NEXT`.
- Test: `tests/test_sources.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Candidate(release: Release, post_url: str, posted: date | None, source: str)`
  - `class SourceUnreachable(Exception)`
  - `class SiteClient(row: SourceRow, http: httpx.AsyncClient, flaresolverr_url: str | None, on_url_change: Callable[[str], None])`
    - `async get(path_or_url: str, referer: str | None = None) -> tuple[str, bytes, str]`, returning `(final_url, body, content_type)`.
    - `async resolve() -> str`, the live base URL.
  - `class TorrenttipSource(client: SiteClient)`
    - `name`
    - `async search(query: str) -> list[Candidate]`
    - `async fetch_torrent(post_url: str) -> bytes`. Raises `ValueError` if the response isn't a torrent.
  - `is_torrent(data: bytes) -> bool`
  - `build_sources(session, http, settings) -> list[TorrenttipSource]`. Returns the enabled sources by priority. Each `on_url_change` persists `current_url`.

- [ ] **Step 1: Write the failing tests** (respx):
  - `test_search_parses_fixture`: the search returns candidates including one with `release.raw == "신인감독 김연경 2.E03.261025.720p-NEXT"`, `post_url` ending `/t/JYy7vk.html`, and `posted == date(2026, 10, 25)`. No candidate's `post_url` contains `/ad/`. Duplicate links from the highlight and sidebar lists collapse to one.
  - `test_fetch_torrent_follows_download_link`: the post fixture's download link (`https://dc.filesbest.xyz/download/XKogYv/1`) is requested with `Referer` = post URL. When it's mocked to return `b"d8:announce...4:infod...ee"`, those bytes come back.
  - `test_fetch_torrent_rejects_html`: the download link returns `<html>ad</html>` and the call raises `ValueError`.
  - `test_redirect_updates_current_url`: `https://torrenttip250.top/` → 301 → `https://torrenttip251.top/` → 200. `resolve()` returns `https://torrenttip251.top` and `on_url_change` is called with it.
  - `test_probes_next_numbers_when_dead`: 250 raises `ConnectError`, 251 and 252 error, 253 returns 200. `resolve()` returns 253. When all of N+1…N+10 fail, it raises `SourceUnreachable`.
  - `test_cloudflare_challenge_uses_flaresolverr`: a 403 with `cf-mitigated: challenge` header (or body containing `Just a moment...`) triggers a POST to `{FLARESOLVERR_URL}/v1` with `{"cmd":"request.get","url":...,"maxTimeout":60000}`. The `solution.response` HTML is returned, and the solution's cookies and user agent are reused on the next request.
- [ ] **Step 2: Run the tests.** Expect FAIL.
- [ ] **Step 3: Implement.**
  - Search: `GET {base}/search?q=`. Parse with selectolax: every `a[href*="/t/"][title]` gives a candidate, parsed with `parse_release_name(title)`. Skip unparseable ones and dedupe by `post_url`.
  - Posted date: the sibling `div.text-right` `YY/MM/DD` in the main results list, when present.
  - Torrent download link: the first `a[href*="/download/"]` on the post page.
  - Templates use `{N}`, matched from the current URL's host by the regex `(\d+)`.
- [ ] **Step 4: Run the tests.** Expect PASS.
- [ ] **Step 5: Commit.** `feat: torrenttip source with domain resolution and FlareSolverr`

---

### Task 5: TVDB client

**Files:**
- Create: `kshow/tvdb.py`
- Test: `tests/test_tvdb.py`

**Interfaces:**
- Produces:
  - `@dataclass TvdbMatch(id: int, name: str, english_name: str | None, year: str | None, network: str | None, image_url: str | None)`
  - `@dataclass TvdbEpisode(number: int, aired: date | None)`
  - `class TvdbClient(api_key: str, pin: str | None, http: httpx.AsyncClient)`, base URL `https://api4.thetvdb.com/v4`:
    - `async search(query: str) -> list[TvdbMatch]` (`GET /search?query=&type=series`; English name from `translations["eng"]`; top 8)
    - `async lookup(ref: str) -> TvdbMatch | None`. Accepts a numeric id, `https://thetvdb.com/series/<slug>` (`GET /series/slug/<slug>`), or a `…/series/<id>` URL.
    - `async english_name(series_id: int) -> str | None` (`GET /series/{id}/translations/eng` → `data.name`)
    - `async season_episodes(series_id: int, season: int) -> list[TvdbEpisode]` (`GET /series/{id}/episodes/default?season=N&page=0`, filtered to `seasonNumber == N`)
    - `async is_ended(series_id: int) -> bool` (`GET /series/{id}` → `data.status.name == "Ended"`)
  - Login: `POST /login {"apikey", "pin"?}` → bearer token, cached, with a single retry on 401.

- [ ] **Step 1: Write the failing tests** (respx, small JSON bodies):
  - Search maps `translations.eng` to `english_name`, and leaves it `None` when absent.
  - `lookup("https://thetvdb.com/series/the-wonder-coach")` calls `/series/slug/the-wonder-coach`.
  - `season_episodes` parses `aired: "2026-10-25"` and `aired: null`.
  - A 401 on a call triggers one re-login and a retry.
- [ ] **Step 2: Run the tests.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.** Expect PASS.
- [ ] **Step 5: Commit.** `feat: TVDB v4 client`

---

### Task 6: qBittorrent client and Apprise notifier

**Files:**
- Create: `kshow/qbit.py`, `kshow/notify.py`
- Test: `tests/test_qbit.py`, `tests/test_notify.py`

**Interfaces:**
- Produces:
  - `@dataclass QbitTorrent(hash: str, tags: list[str], progress: float, state: str, save_path: str)`
  - `@dataclass QbitFile(name: str, size: int)`
  - `class QbitError(Exception)`
  - `class QbitClient(url, username, password, http)`
    - `async add_torrent(data: bytes, filename: str, category: str, save_path: str, tag: str) -> None` (`POST /api/v2/torrents/add` multipart; a response body other than `Ok.` → `QbitError`)
    - `async torrents(category: str) -> list[QbitTorrent]` (`/api/v2/torrents/info?category=`)
    - `async files(hash: str) -> list[QbitFile]`
    - `async delete(hash: str, delete_files: bool) -> None`
    - Logs in with `POST /api/v2/auth/login`, keeps the SID cookie, and re-logs in once on 403. Any connection error raises `QbitError`.
  - `class Notifier(apprise_url: str | None, tag: str | None, http)`
    - `async send(title: str, body: str) -> None`: `POST apprise_url` with JSON `{"title","body","tag"?}`. A no-op when the URL is unset. Never raises; it logs failures.

- [ ] **Step 1: Write the failing tests.** Covers: login and add with `category`, `savepath`, and `tags` form fields; re-login on 403; `torrents()` passes `category=kshow`; a connection error becomes `QbitError`; Notifier posts the expected JSON; Notifier does nothing without a URL; Notifier swallows a 500.
- [ ] **Step 2: Run the tests.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.** Expect PASS.
- [ ] **Step 5: Commit.** `feat: qBittorrent client and Apprise notifier`

---

### Task 7: Pipeline — release selection and show checks

**Files:**
- Create: `kshow/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `Candidate`, `TorrenttipSource`, `SourceUnreachable` (Task 4); `TvdbClient` (Task 5); `QbitClient`, `QbitError`, `Notifier` (Task 6); `existing_episodes`, `season_folder_name` (Task 3); models (Task 2).
- Produces:
  - `select_releases(candidates: list[Candidate], search_title: str, release_season: int, wanted: set[int], season_first_aired: date | None, preferred_resolution: str) -> dict[int, Candidate]`
  - `@dataclass Deps(session_factory, sources: list, tvdb: TvdbClient | None, qbit: QbitClient, notifier: Notifier, settings: Settings)`
  - `async check_show(deps: Deps, show_id: int) -> None`
  - `async check_all_shows(deps: Deps) -> None`. Runs every show with `status == "following"`, catches and logs per-show exceptions, and continues with the next show.

`check_show` algorithm (the order matters):

1. If `tvdb_id` is set:
   - Upsert `Episode` rows from `tvdb.season_episodes(tvdb_id, tvdb_season)`, storing `tvdb_airdate`.
   - Set `season_first_aired` to the earliest non-null airdate.
2. Mark every episode in `existing_episodes(tv/folder/season_folder, tvdb_season)` as `downloaded`, creating rows if needed.
3. Search the sources in priority order with `search_title`.
   - `SourceUnreachable` on a source: notify once per outage (`SourceRow.unreachable_since` null → set it and notify; a success clears it), then try the next source.
4. Collect candidates. Wanted = episodes not `downloaded`/`downloading`, plus any release episode number not yet in the DB. This allows shows without TVDB or with incomplete TVDB data.
   - Exclude candidates whose `post_url` already has a `failed` `Grab` for that episode.
5. Run `select_releases`. For each selection:
   - Call `fetch_torrent`. On `ValueError`, log it and record a failed `Grab` so that `post_url` is skipped next time.
   - Create a `Grab` with `qbit_tag = f"kshow-{grab.id}"`, then `qbit.add_torrent(…, filename=f"{release.raw}.torrent")`, and set the episode to `downloading`.
   - On `QbitError`, roll back that grab and stop the loop. The episode stays `wanted`.
6. Overdue: for each `wanted` episode with `tvdb_airdate` at least 2 days before today and `overdue_notified` false, notify `"<Folder> E04 not found yet"` and set the flag.
7. Finished: if `tvdb_id` is set, every TVDB episode of the season is `downloaded`, and `tvdb.is_ended()` returns true, set `status = "finished"`.

`select_releases` rules:
- `normalize_title(c.release.title) == normalize_title(search_title)`
- `c.release.season == release_season`
- the episode is in `wanted`
- if `season_first_aired` and `c.release.airdate` are both set, the airdate must be no more than 7 days before `season_first_aired`

For each episode, prefer `resolution == preferred_resolution`, then the earliest `posted`, then the first in input order.

- [ ] **Step 1: Write the failing tests.** Build candidates with a helper `cand(raw, url, posted=None)` that calls `parse_release_name`:
  - `test_rejects_other_season_and_spinoff`: from `["신인감독 김연경 2.E04.261101.720p-NEXT", "신인감독 김연경.E04.251019.1080p-F1RST", "신인감독 김연경 비하인드 2.E04.261101.720p-NEXT"]` with season 2 and wanted `{4}`, only the first is selected.
  - `test_prefers_720p`: the 1080p and 720p releases of E04 → the 720p is chosen. With only 1080p → the 1080p is chosen.
  - `test_airdate_guard`: with `season_first_aired=date(2026,10,11)`, a release dated `250101` is rejected and one dated `261008` is accepted.
  - `test_check_show_skips_manually_filed`: E01–E03 exist under `Season 2` as raw release files, and the search returns E01–E04. Only E04 is added to qBittorrent (respx asserts a single `/torrents/add`), and E01–E03 are `downloaded`.
  - `test_html_instead_of_torrent_is_not_sent`: the download link returns HTML. There is no `/torrents/add` call, the episode is still `wanted`, and a failed `Grab` exists. On the next run the same `post_url` isn't tried again.
  - `test_qbit_down_keeps_episode_wanted`: `/api/v2/auth/login` raises `ConnectError`. `check_all_shows` doesn't raise and the episode stays `wanted`.
  - `test_source_unreachable_notifies_once`: two runs with the source dead → Apprise is POSTed once.
  - `test_overdue_notifies_once` and `test_marks_finished_when_ended_and_complete`.
- [ ] **Step 2: Run the tests.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.** Expect PASS.
- [ ] **Step 5: Commit.** `feat: release selection and show check pipeline`

---

### Task 8: Download processing and filing

**Files:**
- Create: `kshow/downloads.py`
- Test: `tests/test_downloads.py`

**Interfaces:**
- Consumes: `Deps` (Task 7), `QbitClient` (Task 6), `target_path`, `move_into_library`, `VIDEO_EXTS` (Task 3).
- Produces:
  - `map_path(qbit_path: str, path_map: str) -> Path`. `path_map` is `"from=to"` or empty, which means identity.
  - `async process_downloads(deps: Deps) -> None`

`process_downloads` works through every `Grab` in state `downloading`, finding its torrent by `qbit_tag` in `qbit.torrents(category)`:

- **Torrent missing:** mark the grab `failed` and the episode `wanted`.
- **Progress went up:** update `progress` and `last_progress_at`.
- **Progress not 1.0 and `now - last_progress_at > stall_hours`:**
  - `qbit.delete(hash, True)`
  - grab `failed`, episode `wanted`
  - notify `"Download failed: <Folder> S02E04"`
- **Progress 1.0:**
  - From `qbit.files(hash)`, pick the largest file whose suffix is in `VIDEO_EXTS`. The source is `map_path(save_path) / file.name`, and the destination is `target_path(...)`.
  - Call `move_into_library`. If the destination exists, raise `FileExistsError`: leave the torrent in place, mark the grab `failed`, and log. It never overwrites.
  - `qbit.delete(hash, True)`
  - episode `downloaded` with `filed_path`; grab `done`
  - notify `"<Folder> S02E04 is in Plex"`

Any `QbitError` aborts this run quietly; the next run retries.

- [ ] **Step 1: Write the failing tests.** Use `tmp_path` as both `/downloads` and `/tv`, with respx mocking qBittorrent:
  - `test_moves_largest_video_only`: the torrent files are `show/sample.mp4` (10), `show/ep.mp4` (1000), and `show/info.txt` (5). `ep.mp4` ends up at `…/The Wonder Coach/Season 2/The Wonder Coach - S02E04.mp4`, `delete` is called with `deleteFiles=true`, and the notification body contains `S02E04`.
  - `test_existing_destination_not_overwritten`: the destination already exists → it keeps its old content, the grab is `failed`, and there is no delete call.
  - `test_stalled_download_fails_after_timeout` (with `last_progress_at` 7h ago and progress unchanged) and `test_missing_torrent_marks_wanted`.
  - `test_map_path`: `map_path("/downloads/kshow/x", "/downloads=/data")` → `Path("/data/kshow/x")`, and the identity case when the map is empty.
- [ ] **Step 2: Run the tests.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.** Expect PASS.
- [ ] **Step 5: Commit.** `feat: process finished downloads into the library`

---

### Task 9: App factory and scheduler

**Files:**
- Create: `kshow/main.py`, `kshow/scheduler.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `create_app(settings: Settings | None = None) -> FastAPI`
    - Its lifespan runs `init_db(f"sqlite:///{config_dir}/kshow.db")`, builds `Deps` (shared `httpx.AsyncClient(timeout=30, follow_redirects=False)`), and stores it on `app.state.deps`.
    - It starts the scheduler unless `settings.disable_scheduler` is set. That flag comes from env `DISABLE_SCHEDULER` and is used in tests.
    - `/healthz` returns `{"ok": true}`.
  - `start_scheduler(deps) -> AsyncIOScheduler`:
    - job `check` runs `check_all_shows` every `check_interval_minutes`, with `max_instances=1` and `coalesce=True`
    - job `downloads` runs `process_downloads` every 60 seconds, also with `max_instances=1`
  - `reschedule_check(scheduler, minutes: int) -> None`
  - `trigger_check(app, show_id: int | None) -> None`: schedules an immediate one-off run.
- [ ] **Step 1: Write the failing tests.** With `TestClient(create_app(settings with tmp config_dir, DISABLE_SCHEDULER=1))`, `/healthz` returns 200 and `kshow.db` exists in the tmp dir. `start_scheduler` registers jobs `check` and `downloads`, and `reschedule_check(s, 5)` changes the interval to 5 minutes.
- [ ] **Step 2: Run the tests.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.** Expect PASS.
- [ ] **Step 5: Commit.** `feat: app factory and scheduler`

---

### Task 10: OIDC auth (Pocket ID)

**Files:**
- Create: `kshow/auth.py`
- Modify: `kshow/main.py` (add `SessionMiddleware` and the auth router)
- Test: `tests/test_auth.py`

**Interfaces:**
- Produces:
  - `router` with:
    - `GET /login`: a page with a single "Log in" button that links to `/auth/start`
    - `GET /auth/start`: redirects to the OIDC authorization URL (authlib starlette client, `server_metadata_url = OIDC_ISSUER + "/.well-known/openid-configuration"`, scope `openid profile email groups`, redirect `APP_URL + "/auth/callback"`)
    - `GET /auth/callback`
    - `POST /logout`
  - `require_user(request) -> dict` dependency:
    - when auth is disabled, it returns `{"name": "local"}`
    - otherwise it redirects to `/login` when there's no session user
  - `is_allowed(claims: dict, users: list[str], groups: list[str]) -> bool`:
    - true when both lists are empty
    - true when `preferred_username` or `email` is in `users`
    - true when any of `claims["groups"]` is in `groups`
  - The session secret comes from `SESSION_SECRET`, or else from `/config/session_secret`, generated with `secrets.token_urlsafe(32)` on first start.
  - Session cookie: `https_only` when `APP_URL` starts with `https`, 30-day max age.
- [ ] **Step 1: Write the failing tests:**
  - the `is_allowed` truth table
  - with auth disabled, `/` returns 200 without a session
  - with `OIDC_ISSUER` set, `/` returns a 303 to `/login`
  - the callback with mocked `authorize_access_token` returning claims of a disallowed user → 403, and an allowed user → session set and 303 to `/`
  - the session secret file is created once and reused
- [ ] **Step 2: Run the tests.** Expect FAIL.
- [ ] **Step 3: Implement.** Also log a warning at startup when auth is disabled.
- [ ] **Step 4: Run the tests.** Expect PASS.
- [ ] **Step 5: Commit.** `feat: OIDC login for Pocket ID`

---

### Task 11: Web UI

**Files:**
- Create: `kshow/web/routes.py`; templates `base.html`, `shows.html`, `add.html`, `add_results.html` (htmx partial), `confirm.html`, `show.html`, `settings.html`; `static/app.css`; `static/htmx.min.js` (vendored from `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js`)
- Modify: `kshow/main.py` (mount the router and static files; every UI route depends on `require_user`)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `parse_user_input` (Task 1); models (Task 2); `suggest_folder`, `season_folder_name` (Task 3); `TvdbClient.search/lookup/english_name` (Task 5); `trigger_check`, `reschedule_check` (Task 9).
- Routes:
  - `GET /`: the show list.
    - One card per show (following first, then finished, then stopped).
    - Each card shows the folder name, the Korean title, and a status line built by `status_line(show, episodes, grabs) -> str` in `routes.py`.
    - Status line examples: `"E03 downloaded · E04 downloading 42%"`, `"E04 not found yet, 2 days late"`, `"Finished"`.
    - A banner appears for any source with `unreachable_since`.
    - The list auto-refreshes every 30 seconds with `hx-trigger="every 30s"`.
  - `GET /add` shows the paste field. `POST /add/search` (htmx) runs `parse_user_input`, then TVDB search, and returns match cards with poster, English name, Korean name, year, and network.
    - It also shows an "English name or TVDB link" field for a second search through `lookup`/`search`.
    - It also has an "Add without TVDB" link.
  - `GET /add/confirm?tvdb_id=&title=&season=` has these editable fields:
    - folder name, pre-filled with `suggest_folder(...)` or the TVDB English name sanitised (no `/\:*?"<>|`), falling back to the original name
    - TVDB season
    - release season
  - `POST /add` creates the `Show` with `season_folder = season_folder_name(...)`, calls `trigger_check(app, show.id)`, and redirects to `/shows/{id}`.
  - `GET /shows/{id}` lists the episodes with their states. Its actions:
    - `POST /shows/{id}/search`
    - `POST /shows/{id}/episodes/{n}/retry`: marks the episode wanted and its failed grabs as retryable by deleting them
    - `POST /shows/{id}/stop`: sets status to stopped and never touches files
  - `GET /settings` and `POST /settings`:
    - edit the settings keys
    - sources: add, edit, enable, priority, `domain_template`, `current_url`
    - saving the interval calls `reschedule_check`
- [ ] **Step 1: Write the failing tests** (TestClient, scheduler disabled, TVDB mocked via respx):
  - `test_status_line_variants` covers the three examples above.
  - `test_add_flow`:
    - `POST /add/search` with `신인감독 김연경 2` shows "The Wonder Coach".
    - The confirm page with an existing `tv/The Wonder Coach/Season 2` pre-fills folder `The Wonder Coach`, and the TVDB season and release season are both `2`.
    - `POST /add` creates the show with `season_folder == "Season 2"` and `search_title == "신인감독 김연경"`.
  - `test_add_without_tvdb` creates a show with `tvdb_id is None`.
  - `test_stop_keeps_files`: after stop, the file under `/tv` still exists.
  - `test_settings_updates_interval`.
- [ ] **Step 2: Run the tests.** Expect FAIL.
- [ ] **Step 3: Implement.** Keep it to plain semantic HTML plus `app.css`:
  - system font, max width 640px, cards, buttons at least 44px tall
  - `<meta name="viewport" content="width=device-width, initial-scale=1">`
  - dark mode via `prefers-color-scheme`
- [ ] **Step 4: Run the tests.** Expect PASS.
- [ ] **Step 5: Manual check.** `DISABLE_SCHEDULER=1 CONFIG_DIR=./.dev TV_DIR=./.dev/tv DOWNLOADS_DIR=./.dev/dl uv run uvicorn kshow.main:create_app --factory`. Open it at phone width (390px), add a show without TVDB, and check that there's no horizontal scroll.
- [ ] **Step 6: Commit.** `feat: phone-first web UI`

---

### Task 12: Docker image, compose example, README, live smoke test

**Files:**
- Create: `Dockerfile`, `docker/entrypoint.sh`, `docker-compose.example.yml`, `README.md`, `.dockerignore`

**Interfaces:**
- Image:
  - Base `python:3.12-slim`; dependencies installed with uv from the lockfile. Exposes `8080`.
  - Entrypoint:
    - creates the group/user from `PGID`/`PUID` (defaults 1000)
    - `chown`s `/config` only
    - runs `setpriv --reuid --regid --init-groups uvicorn kshow.main:create_app --factory --host 0.0.0.0 --port 8080`
  - `HEALTHCHECK` on `/healthz`.
- The compose example matches the spec's volumes and env, with `PUID=1001`, `PGID=100`, `TZ=Europe/Amsterdam`, `QBIT_URL=http://<host>:8089`, `FLARESOLVERR_URL=http://<host>:8191`, and port `8080:8080`.
- README covers:
  - setup and env vars
  - the Pocket ID client setup (redirect URI `APP_URL/auth/callback`)
  - the sister sites (torrentsome, torrentrj, torrentbam, sometv) as known but not yet verified/supported
  - the fact that seeding stops after filing

- [ ] **Step 1: Build.** `docker buildx build --platform linux/arm64,linux/amd64 -t kshow-downloader:dev .` should succeed.
- [ ] **Step 2: Run locally.** `docker run --rm -p 8080:8080 -e DISABLE_SCHEDULER=1 -e PUID=1001 -e PGID=100 -v $PWD/.dev:/config kshow-downloader:dev`. Check that `curl localhost:8080/healthz` returns `{"ok":true}` and that `.dev/kshow.db` is owned by uid 1001.
- [ ] **Step 3: Live smoke test** (read-only against real services; ask the user before adding anything to their real qBittorrent):
  - Run `uv run python -c` against the real torrenttip with `TorrenttipSource.search("신인감독 김연경")` and check that S02E03 is among the results.
  - Run `TvdbClient.search("신인감독 김연경")` (needs the user's `TVDB_API_KEY`) and check that "The Wonder Coach" is among the results.
- [ ] **Step 4: Run the full test suite.** `uv run pytest -v`; all should pass.
- [ ] **Step 5: Commit.** `feat: Docker image, compose example, README`
