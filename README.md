# Job watcher

Checks the job boards of every firm in `config/firms.yaml`. When a new posting's title says **2027**, or reads like a **full-time analyst** role, in the US, mainland China or Hong Kong and posted in the last 14 days, it alerts you.

- **Phone:** a push through the ntfy app. The channel name lives in the `NTFY_TOPIC` repo secret.
- **Email:** a GitHub issue in this repo, which GitHub emails to you.

The firm list started as Trackr's US Finance lists (2028 summer, 2027 full-time, 2027 summer), imported once on Nov 9, 2026. Trackr is no longer read.

## The config
`config/firms.yaml` has one entry per firm:

```yaml
- id: pjt-partners
  name: PJT Partners
  categories: [Elite Boutique]
  homepage: https://pjtpartners.com/careers
  career_sites:
    - https://pjtpartners.wd1.myworkdayjobs.com/Careers
```

- `career_sites` are plain board addresses, with no tracking tags or filters. The watcher works out the job system (Workday, Greenhouse, Oracle and so on) from the address.
- `homepage` is the firm's careers page. It is read only when a firm has no job board listed.
- Add `watch: false` to pause a firm. Copy an entry to add one.

Edit the file on GitHub and commit. The health report reruns on every config change.

## The Trackr archive
`archive/trackr/` keeps everything Trackr gave us, untouched. The watcher never reads it.

- `links.csv`: every program with its raw Trackr link (tracking tags, filters) next to the clean board it became
- `programmes.json`: the full API responses
- `student-boards.json`: for firms whose Trackr link was a student-only board, the main board found and whether it carries the student postings

## How it runs
| Workflow | When | What |
|---|---|---|
| Watch job sites | every 10 min (about hourly in practice) | Reads every board and alerts on new matches |
| Weekly health report | Mondays, and on config changes | Writes `data/report.json`: which boards could be read |

## Files
- `config/firms.yaml`: the firm list (edit this)
- `data/seen.json`: postings already alerted on
- `data/open_now.md`: matches open right now, plus firms to check by hand
- `data/report.json`, `data/status.md`: health
- `watcher/watch.py`: the filters (`is_match`, `location_ok`, `fresh`) and one reader per job system
- `watcher/sites.py`: turns any job-board address into the job system and board it belongs to

To change what counts as a match, edit `is_match` in `watcher/watch.py`. To silence alerts for one run after a big config change, add an empty file `data/quiet_next_run`.
