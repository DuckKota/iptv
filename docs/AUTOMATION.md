# Automation

Once set up, the repository maintains itself. Nothing below needs a human on a
normal day.

## Daily schedule (UTC)

| Time | Workflow | What happens |
| --- | --- | --- |
| 00:15 / 06:15 / 12:15 / 18:15 | **Sync & Deploy** | Re-reads upstream + all sources, rebuilds the API, playlists, guide and site, deploys to Pages |
| 01:00 | **EPG Grab** | Scrapes each site in `config/epg-grab.yml`, one job per site, publishes XMLTV to the `state` branch |
| 02:00 | **Discover Streams** | Scans configured playlists for streams we lack, probes each, commits the survivors to `config/discovered.m3u` |
| 03:00 / 15:00 | **Health Scan** | Probes every stream in 6 parallel shards, updates rolling scores, retires dead links |
| 04:00 / 16:00 | **livetvbuilder Source** | Fetches the third-party bundles, merges them, publishes m3u + XMLTV to the `state` branch |

Plus **CI** on every push and pull request, and **Dependabot** monthly.

## Where the schedule comes from

Not from GitHub. The workflows have no `schedule:` trigger on purpose: from
2026-10-14 GitHub's cron stopped delivering runs for this account entirely.
`workflow_dispatch` kept working, and a throwaway repository with a `*/5` cron
never received a single schedule event, so the fault is account-level, not in
these files. GitHub Support has to clear it; nothing in the repository can.

Until then, [cron-job.org](https://cron-job.org) dispatches each workflow over
the API at the times above. Five jobs, all UTC:

| Job | Workflow | Times |
| --- | --- | --- |
| `IPTV sync` | `sync.yml` | 00:15, 06:15, 12:15, 18:15 |
| `IPTV epg` | `epg.yml` | 01:00 |
| `IPTV discover` | `discover.yml` | 02:00 |
| `IPTV health` | `health.yml` | 03:00, 15:00 |
| `IPTV livetvbuilder` | `livetvbuilder.yml` | 04:00, 16:00 |

Each job POSTs `{"ref":"main"}` to
`https://api.github.com/repos/DuckKota/iptv/actions/workflows/<file>/dispatches`
with a GitHub token in the `Authorization` header. **That token is the weak
point:** when it expires, every job starts failing (cron-job.org emails on the
first failure). Rotate it by editing the header on all five jobs:

1. Create a fine-grained token scoped to this repository with **Actions: read
   and write**. Give it the longest expiry available, not the default 30 days.
2. For each job in the cron-job.org console, replace the `Authorization` header
   value with `Bearer <new token>`.

If GitHub restores schedule delivery, delete the cron-job.org jobs and put the
`on: schedule:` blocks back — otherwise every slot runs twice.

The timetable is a timetable, not a dependency graph: sync at 06:15 assumes the
04:00 livetvbuilder run finished (it takes ~45 minutes). If a run overruns, the
next sync publishes whatever state existed when it started.

## What updates without you

- **New channels and streams from upstream.** iptv-org changes constantly; every
  sync picks that up. This is the main source of new content.
- **New streams from discovery.** Anything matching a channel we already index,
  from a source already listed in `config/discovery.yml`, that answers a live
  probe.
- **Dead links.** Streams that fail repeatedly lose score and are dropped.
- **Quality metadata.** Resolution, codec, bitrate and latency are re-measured
  on every scan, so a channel that silently drops from 1080p to 480p shows it.
- **The guide.** Rebuilt nightly and re-matched to channel ids.
- **`PLAYLISTS.md` and the README table.** Regenerated and committed on sync.

## What still needs you

Exactly one thing: **adding a new source**.

`config/sources.yml`, `config/discovery.yml` and `config/epg-grab.yml` are the
trust boundary. Everything downstream of them is automatic; deciding that a
particular list is appropriate to index is not, and should not be. That single
human decision is what separates a curated index from an unattended scraper —
see [`LEGAL.md`](LEGAL.md).

## Running the bot under its own identity

By default automated commits appear as `github-actions[bot]`. That works, with
one real limitation: commits made with the default `GITHUB_TOKEN` **do not
trigger other workflows**, so a discovery commit will not kick off a sync — it
waits for the next scheduled one.

Giving the automation its own GitHub App fixes that and puts a recognisable
name on its commits, the way `iptv-bot` does for iptv-org.

You have to create the App yourself — it involves generating a private key,
which should never pass through anyone else's hands:

1. **github.com → Settings → Developer settings → GitHub Apps → New GitHub App**
2. Name it (e.g. `iptv-nexus-bot`), set Homepage URL to your repository, and
   **uncheck Webhook → Active**.
3. Under **Repository permissions** grant:
   - Contents: **Read and write**
   - Pull requests: **Read and write**
   - Metadata: Read-only (automatic)
4. Create the App, then **Generate a private key** and download the `.pem`.
5. **Install App** → select only this repository.
6. In the repository: **Settings → Secrets and variables → Actions**
   - Variable `BOT_APP_ID` = the App ID shown on the App's page
   - Secret `BOT_PRIVATE_KEY` = the entire contents of the `.pem` file

The workflows already look for these and fall back to `GITHUB_TOKEN` when they
are absent, so nothing breaks if you skip this.

## Manual controls

Every workflow has a **Run workflow** button with useful inputs:

| Workflow | Inputs |
| --- | --- |
| EPG Grab | `sites` (comma-separated subset), `days` |
| Discover Streams | `max_probes`, `apply` (off = open a PR instead of committing) |
| Health Scan | `limit` (streams per shard, 0 = all) |
| Sync & Deploy | `skip_epg` |

## Cost

All of it runs on GitHub's free tier for public repositories: Actions minutes
are unlimited for public repos, and Pages serves the output. The heaviest job
is the health scan at roughly 6 × 5 minutes per run.
