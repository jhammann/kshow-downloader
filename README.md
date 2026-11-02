# kshow-downloader

A small, Sonarr-like web app for Korean TV shows. You add a show (looked up on TVDB), and it
searches the Korean torrent index torrenttip for new episodes, sends the torrent to qBittorrent,
and, once the download finishes, files the episode into your TV library with a Sonarr-style
name. It has a phone-first web UI, optional Pocket ID (OIDC) login, and optional Apprise
notifications. State lives in a SQLite database in `/config`.

## Run it

Copy `docker-compose.example.yml`, adjust it, and start it:

```sh
docker compose -f docker-compose.example.yml up -d
```

The image comes from `registry.hammann.dev/kshow-downloader`. Releases are automatic: every push to `master` runs semantic-release (GitHub Actions), which tags `vX.Y.Z` from the commit messages (`feat:` → minor, `fix:`/`ci:` → patch). Woodpecker builds the `linux/arm64` image for each `v*` tag, pushes it as `latest` and the version tag, and keeps the 5 most recent version tags. To build locally instead, add `build: .` under the service.

The example matches this layout:

| Container path | Host path | Purpose |
| --- | --- | --- |
| `/config` | `/nas/Config/kshow` | SQLite database (`kshow.db`) and `session_secret` |
| `/tv` | `/nas/Media/TV` | Your TV library (files are moved here) |
| `/downloads` | `/nas/Downloads` | qBittorrent's download folder, as seen by this container |

The container runs as `PUID`/`PGID` (default 1000). Only `/config` is chowned; `/tv` and
`/downloads` are never touched, so that user needs write access to them. The image is
multi-arch (amd64 and arm64) and exposes port 8080; `/healthz` is the health check.

## Environment variables

| Variable | Meaning |
| --- | --- |
| `PUID`, `PGID` | User and group the app runs as (default 1000). |
| `TZ` | Timezone, for example `Europe/Amsterdam`. |
| `APP_URL` | Public URL of the app, for example `https://kshow.example.com`. Required when auth is on. |
| `QBIT_URL` | qBittorrent Web UI URL, for example `http://<host>:8089`. |
| `QBIT_USERNAME`, `QBIT_PASSWORD` | qBittorrent Web UI credentials. |
| `TVDB_API_KEY` | TVDB v4 API key, used to look up shows and episodes. |
| `TVDB_PIN` | TVDB subscriber PIN, if your key needs one. |
| `FLARESOLVERR_URL` | FlareSolverr URL, for example `http://<host>:8191`, used when the site sits behind a Cloudflare challenge. |
| `APPRISE_URL` | Apprise API notify endpoint, for example `http://apprise-api:8000/notify/kshow`. See [Notifications](#notifications-apprise). |
| `APPRISE_TAG` | Only notify the channels with this tag. Leave unset unless your channels are tagged. |
| `OIDC_ISSUER` | Pocket ID base URL. Setting it turns authentication on; leave it unset to run without login (LAN only). |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | The OIDC client from Pocket ID. |
| `OIDC_ALLOWED_GROUPS` | Comma-separated Pocket ID groups allowed to log in. |
| `OIDC_ALLOWED_USERS` | Comma-separated allow-list of user `sub` values or verified emails. |
| `OIDC_PROVIDER_NAME` | Name on the login button, as in "Log in with Pocket ID" (default `Pocket ID`). |
| `SESSION_SECRET` | Session signing key (at least 32 characters). If unset, one is generated and stored in `/config/session_secret`. |
| `CONFIG_DIR`, `TV_DIR`, `DOWNLOADS_DIR` | Override the in-container paths (defaults `/config`, `/tv`, `/downloads`). |
| `DISABLE_SCHEDULER` | Set to `1` to disable the background polling (useful for testing). |

## Pocket ID setup

1. In Pocket ID create an OIDC client with the redirect URI `APP_URL/auth/callback`
   (for example `https://kshow.example.com/auth/callback`).
2. Set `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` and `APP_URL`.
3. Restrict access. Using `OIDC_ALLOWED_GROUPS` is recommended: put the people who may use the
   app in a Pocket ID group. `OIDC_ALLOWED_USERS` matches a user's `sub` or a verified email
   only. Usernames are deliberately not matched, because Pocket ID users can edit their own.

Sessions last 30 days. To log everyone out, delete `/config/session_secret` and restart the
container (this does not apply if you set `SESSION_SECRET` yourself; change it instead).

Behind a reverse proxy: when `APP_URL` starts with `https`, the session cookie is `Secure`, so
opening the app over plain `http` on the LAN will not keep a session. Use the https URL.

## Networking

The example joins the existing `npm_default` network (the one nginx proxy manager uses), so:

- nginx proxy manager reaches the app as `kshow:8080`. Point your proxy host there; publishing
  port 8080 on the host is then optional.
- FlareSolverr and apprise-api are reached by container name (`flaresolverr:8191`,
  `apprise-api:8000`), because they are on the same network.
- qBittorrent runs inside gluetun's network, which kshow can't join, so it is reached through
  the port gluetun publishes on the host: `http://<host>:8089`.

## Seasons and what kshow fetches by itself

Each show has its seasons listed on its page. What kshow downloads without you asking:

- After you add a show, only episodes airing from that day on are fetched (the day is the
  Korean date, since air dates are Korean broadcast dates). Older episodes are never fetched
  by themselves; use Search for those.
- Seasons from the one you picked onwards are followed. Earlier seasons are listed but not
  followed.
- A season that TVDB announces later is followed automatically, including episodes that
  aired up to 14 days before kshow noticed it (TVDB often adds a season a little late). You
  get a "New season" notification.
- A finished show comes back to following by itself when TVDB announces a new season.
- Episodes you deleted are never fetched again by themselves.

Each season has a **Following new episodes** switch. Turn it off to stop automatic downloads
for that season; Search still works. A stopped or finished show has a **Follow again** button.

## Searching by hand

Search works on every show and season, whether it is followed, stopped or finished.

- **Search** (next to an episode) looks for that one episode. **Search season** looks for the
  whole season.
- Both are one-shot: kshow reads up to 5 result pages, picks the best release automatically
  and sends it to qBittorrent. Searching never turns on automatic fetching for the episode.
- Search season skips episodes you already had (shown as Missing, for example because you
  deleted them) and episodes that haven't aired yet. A deleted episode is only fetched with its
  own Search button, so one tap can't bring back everything you watched and deleted.
- If a download you asked for fails later, kshow notifies you and does not try again. The
  episode goes back to Not downloaded.

### Release names use season

This setting, per season, is the number after the title in torrent names; no number means 1.
Both `Title 2` and `Title 시즌2` forms are recognised. Change it when a season's releases
use a different number than TVDB does.

### Episode labels

| Label | Meaning |
| --- | --- |
| In Plex | The episode is in your library. |
| Missing | kshow filed it before, but the file is gone from the library. |
| Downloading NN% | The torrent is downloading. |
| Wanted | kshow will fetch it by itself (followed season, not downloaded yet). |
| Not downloaded | Nothing is fetched by itself; use Search if you want it. |
| Not aired yet | The episode airs later. |
| Failed | The download failed. Use Search to try again. |

## Upgrading

The first start of this version converts the database automatically. It first writes a backup
next to it, `kshow.db.bak-<timestamp>` in `/config`. Delete the backup once everything looks
right.

## Notifications (Apprise)

kshow posts each notification to `APPRISE_URL`. With apprise-api's stateful endpoint
(`/notify/<key>`), the actual channels (Telegram, e-mail, …) are stored in apprise-api under that
key, not in kshow. To set up the `kshow` key with a Telegram channel, run on the Docker host:

```sh
docker exec apprise-api wget -qO- --post-data 'urls=tgram://BOT_TOKEN/CHAT_ID' \
  http://127.0.0.1:8000/add/kshow
```

Send a test message:

```sh
docker exec apprise-api wget -qO- --header 'Content-Type: application/json' \
  --post-data '{"title":"test","body":"hello from kshow"}' http://127.0.0.1:8000/notify/kshow
```

Leave `APPRISE_TAG` unset unless you store the channel with a tag (`urls=kshow=tgram://…`).
With a tag set, apprise-api only notifies channels carrying that tag, so an untagged channel
gets nothing.

kshow notifies when:

- a download starts (show, episode, resolution and release group);
- an episode is added to the library;
- a download fails, stalls or can't be moved into the library;
- a download link doesn't return a torrent (kshow then tries another release);
- an episode is overdue;
- a search finds no release ("No release found");
- a search can't run because no source is reachable ("Search failed");
- TVDB announces a new season and kshow starts following it ("New season");
- qBittorrent can't be reached or logged in to (once per outage), and when it works again;
- a source becomes unreachable, and when it is reachable again.

## Leaving `OIDC_ISSUER` unset

Without `OIDC_ISSUER` there is no login at all: anyone who can reach port 8080 can add and
remove shows and change settings. Only do this when the port is reachable from a trusted LAN,
never when it is published to the internet or exposed through a reverse proxy.

## qBittorrent paths

kshow tells qBittorrent to save into the "qBittorrent save path" setting (default
`/downloads/kshow`), a path as **qBittorrent** sees it. kshow then reads the finished file from
the same path inside its own container. So qBittorrent's `/downloads` and kshow's `/downloads`
must be the same host folder. In the example both containers have the volume
`/nas/Downloads:/downloads`, so qBittorrent writes to `/nas/Downloads/kshow` on the host and
kshow reads it from `/downloads/kshow`.

If qBittorrent mounts that folder somewhere else, for example `/nas/Downloads:/data`, set the
save path to `/data/kshow` and the "Path map" setting to `/data=/downloads`. The path map is a
single `qbit-prefix=kshow-prefix` rewrite applied to qBittorrent's paths before kshow reads
them; leave it empty when both containers use the same paths.

## How downloads are filed

- With `/downloads` and `/tv` as separate mounts, filing is a copy followed by removing the
  source (the copy runs in the background, but a multi-GB episode still takes a while). The
  destination filesystem needs hard-link support (the copy is hard-linked into place to avoid
  overwriting existing files).
- To make filing an instant rename instead, mount the common parent once and point both
  directories into it, for example `/nas:/nas` with `TV_DIR=/nas/Media/TV` and
  `DOWNLOADS_DIR=/nas/Downloads`. qBittorrent's save path then has to be a path under
  `/nas/Downloads` as qBittorrent sees it; use the path map if its mount differs (see above).
- Seeding stops after filing: once an episode is in the library, its torrent is removed from
  qBittorrent.

## Sources

Only `torrenttip` is supported. The sister sites torrentsome, torrentrj, torrentbam and sometv
are known but not yet verified or supported.

## Development

```sh
uv sync
uv run pytest
```
