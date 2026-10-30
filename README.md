# kshow-downloader

A small, Sonarr-like web app for Korean TV shows. You add a show (looked up on TVDB), and it
searches the Korean torrent index torrenttip for new episodes, sends the torrent to qBittorrent,
and, once the download finishes, files the episode into your TV library with a Sonarr-style
name. It has a phone-first web UI, optional Pocket ID (OIDC) login, and optional Apprise
notifications. State lives in a SQLite database in `/config`.

## Run it

Copy `docker-compose.example.yml`, adjust it, and start it:

```sh
docker compose -f docker-compose.example.yml up -d --build
```

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
| `APPRISE_URL` | Apprise API notify endpoint for notifications. |
| `APPRISE_TAG` | Apprise tag to notify. |
| `OIDC_ISSUER` | Pocket ID base URL. Setting it turns authentication on; leave it unset to run without login (LAN only). |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | The OIDC client from Pocket ID. |
| `OIDC_ALLOWED_GROUPS` | Comma-separated Pocket ID groups allowed to log in. |
| `OIDC_ALLOWED_USERS` | Comma-separated allow-list of user `sub` values or verified emails. |
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

## Leaving `OIDC_ISSUER` unset

Without `OIDC_ISSUER` there is no login at all: anyone who can reach port 8080 can add and
remove shows and change settings. Only do this when the port is reachable from a trusted LAN,
never when it is published to the internet or exposed through a reverse proxy.

## qBittorrent paths

kshow tells qBittorrent to save into the "qBittorrent save path" setting (default
`/downloads/kshow`), a path as **qBittorrent** sees it. kshow then reads the finished file from
the same path inside its own container. So qBittorrent's `/downloads` and kshow's `/downloads`
must be the same host folder. In the example both map `/nas/Downloads`:

```yaml
# qBittorrent           # kshow
- /nas/Downloads:/downloads   - /nas/Downloads:/downloads
```

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
