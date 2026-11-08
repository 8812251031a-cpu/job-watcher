# Job watcher

Checks the job sites of every firm on Trackr's US Finance lists (2028 summer, 2027 full-time and 2027 summer). When a new posting's title says **2027**, or reads like a **full-time analyst** role, it alerts you.

- **Phone:** a push through the ntfy app. The channel name lives in the `NTFY_TOPIC` repo secret.
- **Email:** a GitHub issue in this repo, which GitHub emails to you.

Edit the list on the [Firm Watchlist page](https://claude.ai/artifact/9fYTJzRSq7rChv36B4tnkY), then ask Claude to sync it.

## How it runs
| Workflow | When | What |
|---|---|---|
| Watch job sites | every 10 min | Reads every firm's job site and alerts on new matches |
| Refresh firm list | Mondays | Rebuilds `data/firms.json` from Trackr and writes a health report |

## Files
- `data/firms.json` / `data/firms.csv`: the firm database (firm, category, careers site, job-site type, programs)
- `data/overrides.json`: hand-checked job sources that replace or add to what Trackr links to
- `data/seen.json`: postings already alerted on
- `data/report.json`: which firms' sites could be read last time, and how many postings
- `watcher/watch.py`: the title filter (`is_match`) and one reader per job-site type

To change what counts as a match, edit `is_match` in `watcher/watch.py`.
