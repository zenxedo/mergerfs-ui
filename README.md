# mergerfs-ui

A small web dashboard for a [mergerfs](https://github.com/trapexit/mergerfs)
pool. See the pool and its member drives at a glance — capacity, per-drive
model/serial, live read/write, and health — and run the
[mergerfs-tools](https://github.com/trapexit/mergerfs-tools) (`balance`,
`consolidate`, `ctl`, `dedup`, `dup`, `fsck`) from a form with **live streaming
output** and a stop button.

It is a single-file FastAPI app that shells out to the mergerfs-tools scripts.
No database, no agents — it reads `df` / `/proc` / `/sys` and runs the tools
against paths you mount.

## Features

- Pool capacity, free space, and usage.
- Member-drive inventory: mount, device, model, serial, size, free, status,
  live read/write, and utilization.
- Host metrics: uptime, CPU load, memory, pending OS updates.
- Per-tool forms generated from each tool's options, with a **live command
  preview**, a manual-override box, streaming console output, and a stop button.
- Background-run awareness: re-attaching to a running tool shows a badge and its
  captured log.

## Quick start

```sh
docker run -d \
  --name mergerfs-ui \
  --restart unless-stopped \
  -p 8480:8480 \
  -e MERGERFS_POOL_MOUNT=/mnt/pool \
  -v /mnt:/mnt \
  -v /dev:/dev:ro \
  ghcr.io/zenxedo/mergerfs-ui:latest
```

Then open <http://localhost:8480/>.

### Docker Compose

```yaml
services:
  mergerfs-ui:
    image: ghcr.io/zenxedo/mergerfs-ui:latest
    container_name: mergerfs-ui
    restart: unless-stopped
    ports:
      - "8480:8480"
    environment:
      # Mount path of the mergerfs pool *inside the container*.
      MERGERFS_POOL_MOUNT: /mnt/pool
    volumes:
      # The pool and its member drives must be visible to the container.
      - /mnt:/mnt
      # Read-only is enough for per-device info.
      - /dev:/dev:ro
```

## Image tags

| Tag | What it is |
|---|---|
| `latest` | newest tagged release (stable). `0.1.0`, `0.1`, `0` are pinned equivalents. |
| `edge` | built from the tip of `main` — newest, possibly unreleased. |
| `sha-<short>` | the exact commit an `edge` build came from. |

Pin `:0.1.0` (or a newer release) for production; use `:edge` to track `main`.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `MERGERFS_POOL_MOUNT` | *(unset)* | Path where the mergerfs pool is mounted inside the container. When set, a mount at/under this path is shown as the pool. When unset, a filesystem whose type contains `mergerfs` is used. |
| `MERGERFS_POOL_LABEL` | *(unset)* | Caption under "Total Pool Capacity". Defaults to a detected drive count. |
| `MERGERFS_TOOLS_DIR` | `/app/tools/src` | Where the mergerfs-tools scripts live in the image. |
| `PORT` | `8480` | HTTP port to listen on (the healthcheck follows it). Change the published `ports:` mapping to match. |

## Mounts and privileges

- **`/mnt`** (or wherever your pool lives) must be mounted so the app can read
  drive stats and run the tools against it. Mount it read-only if you only want
  monitoring; the read-only tools still work, but `--execute` / `--fix` actions
  will fail.
- **`/dev`** is used to read device information; read-only is enough for that.
- The container runs as **root**. Ownership-fixing tools (`mergerfs.fsck --fix`)
  and file operations need it; a non-root container cannot repair a pool whose
  files are owned by other users.

## Security

**This app ships with no authentication.** Anyone who can reach the port can
read pool stats, **and run the mergerfs tools — including destructive ones**
(`mergerfs.dedup --execute`, `mergerfs.consolidate --execute`,
`mergerfs.balance`, `mergerfs.fsck --fix`). It also runs as root. Treat it as a
trusted-LAN tool:

- Do **not** expose it directly to the internet.
- Put it behind a reverse proxy that provides auth (Authelia, oauth2-proxy,
  Cloudflare Access, basic auth), or reach it over a VPN/Tailscale.
- Mount only the paths the app actually needs.
- The dashboard and `/api/metrics` also expose drive model/serial numbers and
  host metrics (uptime, memory) unauthenticated — a stranger should know that
  before exposing even behind a team proxy.
- Cross-origin form posts are rejected, but that is not a substitute for auth.

## Destructive tools and hardlinks

The mergerfs-tools that move files — `dedup`, `consolidate`, and `balance` —
**can break hardlinks** between a pool and other copies of the same file (for
example a torrent client's files that are hardlinked into a media library).
Removing what looks like a duplicate can unlink that shared inode or move a file
across filesystems.

- Prefer a dry run first (omit `--execute`) and read the commands it prints.
- Know which files are hardlinked before you let a tool move them.
- `--fix` and `--execute` act on real data; there is no undo.
- `mergerfs.dup --prune` deletes extra copies, and `mergerfs.ctl add/remove/set`
  reconfigures the live pool. Treat them as destructive too.

## Image

Built from `python:3.12-slim` with `rsync` added, and vendoring
`mergerfs-tools` at a pinned commit (SHA256-verified at build time). The image
is rebuilt on each release; OS packages may carry Debian CVEs that have no
upstream fix yet, so rebuild to pick up security updates as they land.

## License

MIT — see [LICENSE](LICENSE).
