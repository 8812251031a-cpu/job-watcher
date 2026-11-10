"""Commit this run's data/ files and push, even if another run pushed in the meantime.

seen.json is merged (every key from both runs, earliest record kept). The other files
this run rewrote (open_now.md, status.md, report.json) are taken from this run.
Usage: python watcher/save_state.py "commit message"
"""
import json
import os
import subprocess
import sys

SEEN = "data/seen.json"
FLAG = "data/quiet_next_run"


def git(*args, check=True):
    return subprocess.run(["git", *args], check=check, capture_output=True, text=True)


def main():
    msg = sys.argv[1] if len(sys.argv) > 1 else "Update seen postings"
    changed = set(git("diff", "--name-only", "HEAD", "--", "data").stdout.split())
    changed |= set(git("ls-files", "--others", "--exclude-standard", "data").stdout.split())
    if not changed:
        print("Nothing to save.")
        return
    had_flag = git("cat-file", "-e", f"HEAD:{FLAG}", check=False).returncode == 0
    consumed_flag = had_flag and not os.path.exists(FLAG)
    ours = {f: open(f, "rb").read() for f in changed if os.path.exists(f)}
    for attempt in range(5):
        git("fetch", "-q", "origin", "main")
        git("reset", "-q", "--hard", "origin/main")
        theirs_seen = json.load(open(SEEN)) if os.path.exists(SEEN) else {}
        for f, data in ours.items():
            os.makedirs(os.path.dirname(f), exist_ok=True)
            open(f, "wb").write(data)
        if SEEN in ours:
            merged = json.loads(ours[SEEN])
            for k, v in theirs_seen.items():
                if k not in merged or v.get("first_seen", "") < merged[k].get("first_seen", ""):
                    merged[k] = v
            json.dump(merged, open(SEEN, "w"), indent=1, sort_keys=True)
        if consumed_flag and os.path.exists(FLAG):
            os.remove(FLAG)
        git("add", "-A", "data")
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            print("Nothing new to save.")
            return
        git("commit", "-q", "-m", msg)
        if git("push", "-q", "origin", "HEAD:main", check=False).returncode == 0:
            print(f"Saved on attempt {attempt + 1}.")
            return
    sys.exit("Could not push after 5 attempts.")


if __name__ == "__main__":
    main()
