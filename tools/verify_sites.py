"""Check every firm's career sites the way a person would, then fix config/firms.yaml.

For each firm:
1. Every job-system board in the config (Workday, Greenhouse, Oracle...) is read with the
   watcher's own reader to confirm it answers and lists postings.
2. The firm's careers pages are opened in a real browser (Chromium). The script collects
   every job-system board the page links to, embeds, or loads jobs from, and follows up
   to two "search jobs / view openings" links when the page itself shows none.
3. A board found on the firm's own pages is accepted only if it looks like the firm's
   (its name matches) and the watcher can read postings from it.

Changes applied:
- Plain careers pages are replaced by the job-system board(s) they lead to.
- Firms already on a job system gain any extra verified board their careers page links to.
- A board that no longer exists is dropped when a verified replacement was found.
- Every job-system address is written in its standard form (Oracle ends in /jobs).
Findings go to archive/verify/<date>.md and .json.

Run:  python tools/verify_sites.py
"""
import asyncio
import datetime as dt
import difflib
import json
import os
import re
import sys
from urllib.parse import parse_qs, urljoin, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "watcher"))
import config_io  # noqa: E402
import watch  # noqa: E402  (the watcher's readers)
from sites import board_url, classify, clean_page, is_student_board  # noqa: E402

TODAY = dt.date.today().isoformat()
OUT = os.path.join(ROOT, "archive", "verify")
PARALLEL_PAGES = 8
HOP_TEXT = re.compile(r"(search|view|see|browse|explore|find|current|all|open)\b.{0,25}\b(jobs?|openings?|positions?|"
                      r"opportunit|roles?|vacanc)|job search|apply now|apply here|open positions|job openings|"
                      r"career opportunities|join (us|our team)", re.I)
OTHER_SYSTEMS = re.compile(r"taleo\.net|successfactors\.(com|eu)|dayforcehcm\.com|recsolu\.com|TGnewUI|eightfold\.ai|"
                           r"jobvite\.com|phenompeople|rippling\.com|hibob\.com|pinpointhq|teamtailor|recruitee|"
                           r"jazzhr|applytojob|myworkdaysite", re.I)
GENERIC = {"capital", "partners", "group", "management", "advisors", "advisory", "investments", "investment", "bank",
           "banking", "financial", "securities", "company", "associates", "holdings", "global", "asset", "the", "and",
           "llc", "inc", "fund", "funds", "markets", "international", "trading", "corporation", "corp", "co", "of"}
HOST_NOISE = {"www", "careers", "career", "jobs", "job", "search", "apply", "com", "net", "org", "io", "co", "uk", "us",
              "hr", "recruiting", "talent", "en", "global"}


# ------------------------------------------------------------------ job systems seen in a URL
def ats_of(url):
    """(kind, key) when a URL belongs to a job system the watcher can read, else None."""
    try:
        u = urlparse(url)
    except ValueError:
        return None
    h, seg = (u.hostname or "").lower(), [s for s in u.path.split("/") if s]
    q = parse_qs(u.query)
    if h == "boards-api.greenhouse.io" and len(seg) >= 3 and seg[1] == "boards":
        return ("greenhouse", seg[2])
    if h.endswith("greenhouse.io") and "for" in q:
        return ("greenhouse", q["for"][0])
    if h == "api.lever.co" and len(seg) >= 3:
        return ("lever", seg[2])
    if h == "api.ashbyhq.com" and len(seg) >= 3:
        return ("ashby", seg[-1])
    if h.endswith("myworkdayjobs.com") and len(seg) >= 4 and seg[0] == "wday" and seg[1] == "cxs":
        return ("workday", f"{h}|{seg[2]}|{seg[3]}")
    if h == "api.smartrecruiters.com" and len(seg) >= 3 and seg[1] == "companies":
        return ("smartrecruiters", seg[2])
    if h == "apply.workable.com" and len(seg) >= 4 and seg[0] == "api":
        return ("workable", seg[3])
    if h.endswith("oraclecloud.com") and "siteNumber=" in url:
        m = re.search(r"siteNumber=(CX_\d+)", url)
        return ("oracle", f"{h}|{m.group(1)}") if m else None
    c = classify(url)
    if not c or c[0] in ("site", "goldman", "janestreet"):
        return None
    if c[0] == "greenhouse" and c[1] in ("embed", "v1", "jobs", "boards"):
        return None
    if c[0] == "workday" and c[1].split("|")[2] in ("wday", "en-US", "login", "userHome"):
        return None
    return c


def ident(kind, key):
    """The part of a board's key that should resemble the firm's name, or None when it can't."""
    if kind == "workday":
        return key.split("|")[1]
    if kind in ("greenhouse", "lever", "ashby", "smartrecruiters", "workable"):
        return key
    if kind in ("icims", "bamboo", "breezy"):
        return re.sub(r"^(careers|jobs|uscareers|external|internal)-", "", key.split(".")[0])
    if kind in ("avature", "oleeo"):
        return (urlparse(key if kind == "oleeo" else "https://" + key.split("|")[0]).hostname or "").split(".")[0]
    return None  # oracle, ukg, adp, paylocity: keys don't carry a name


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def owned(firm, kind, key):
    tag = norm(ident(kind, key))
    if not tag:
        return None  # can't tell from the name
    words = [w for w in re.findall(r"[a-z0-9]+", firm["name"].lower()) if w not in GENERIC and len(w) >= 3]
    host = urlparse(firm.get("homepage") or "").hostname or ""
    labels = [norm(x) for x in host.split(".") if x and x not in HOST_NOISE and len(x) >= 3]
    names = [norm("".join(words))] + [norm(w) for w in words] + labels
    for n in filter(None, names):
        if len(n) >= 3 and (n in tag or (len(tag) >= 3 and tag in n)):
            return True
        if difflib.SequenceMatcher(None, n, tag).ratio() >= 0.75:
            return True
    initials = "".join(w[0] for w in re.findall(r"[a-z0-9]+", firm["name"].lower()) if w not in ("the", "and", "of"))
    return len(initials) >= 2 and tag.startswith(initials)


# ------------------------------------------------------------------ reading boards (watcher's readers)
def read_board(kind, key):
    try:
        jobs = watch.ADAPTERS[kind](key)
        return {"ok": True, "count": len(jobs)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "count": 0, "error": f"{type(e).__name__}: {str(e)[:120]}"}


# ------------------------------------------------------------------ browser
async def render(ctx, url, sem):
    async with sem:
        page = await ctx.new_page()
        reqs = []
        page.on("request", lambda r: reqs.append(r.url))
        try:
            resp = await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            links = await page.eval_on_selector_all(
                "a[href]", "els => els.map(e => [e.href, (e.innerText || e.getAttribute('aria-label') || '').trim().slice(0, 120)])")
            return {"url": url, "status": resp.status if resp else None, "final": page.url, "links": links,
                    "frames": [f.url for f in page.frames], "requests": reqs}
        except Exception as e:  # noqa: BLE001
            return {"url": url, "error": f"{type(e).__name__}: {str(e)[:120]}", "links": [], "frames": [], "requests": reqs}
        finally:
            await page.close()


def boards_in(pages):
    found, other = {}, set()
    for p in pages:
        for u in [p.get("final") or ""] + [x[0] for x in p["links"]] + p["frames"] + p["requests"]:
            c = ats_of(u)
            if c:
                found.setdefault(c, u)
            elif OTHER_SYSTEMS.search(u or ""):
                h = urlparse(u).hostname or ""
                if h and "linkedin" not in h:
                    other.add(h)
    return found, sorted(other)


def hop_links(page):
    base = urlparse(page.get("final") or page["url"]).hostname or ""
    out = []
    for href, text in page["links"]:
        h = urlparse(href).hostname or ""
        if not href.startswith("http") or "linkedin" in h:
            continue
        if HOP_TEXT.search(text or "") or re.search(r"/(jobs|openings|positions|search-jobs|job-search|opportunities|vacancies)\b",
                                                     urlparse(href).path, re.I):
            if h == base or h.endswith("." + ".".join(base.split(".")[-2:])):
                out.append(href.split("#")[0])
    return list(dict.fromkeys(out))[:2]


# ------------------------------------------------------------------ per firm
async def check_firm(ctx, sem, firm):
    rec = {"id": firm["id"], "name": firm["name"], "before": list(firm.get("career_sites") or [])}
    sites = [str(s) for s in rec["before"]]
    current = [(s, classify(s)) for s in sites]
    ats_now = [(s, c) for s, c in current if c and c[0] != "site"]
    pages_now = [s for s, c in current if c and c[0] == "site"]
    home = firm.get("homepage") or ""
    scan = list(dict.fromkeys([u for u in pages_now + ([home] if home else []) if "linkedin.com" not in u]))

    loop = asyncio.get_running_loop()
    rec["boards"] = {}
    for s, c in ats_now:
        rec["boards"][board_url(*c)] = await loop.run_in_executor(None, read_board, *c)

    pages = list(await asyncio.gather(*(render(ctx, u, sem) for u in scan)))
    found, other = boards_in(pages)
    have = {c for _, c in ats_now}
    mine = [c for c in found if c not in have and owned(firm, *c) is not False]
    if not mine and not ats_now:
        hops = [h for p in pages for h in hop_links(p)][:2]
        if hops:
            hop_pages = await asyncio.gather(*(render(ctx, u, sem) for u in hops))
            pages += hop_pages
            f2, o2 = boards_in(hop_pages)
            found.update(f2)
            other = sorted(set(other) | set(o2))
    rec["pages"] = [{"url": p["url"], "status": p.get("status"), "final": p.get("final"), "error": p.get("error")}
                    for p in pages]
    rec["other_systems"] = other

    have = {c for _, c in ats_now}
    rec["candidates"] = {}
    for c, seen_at in found.items():
        if c in have:
            continue
        own = owned(firm, *c)
        n_kind = sum(1 for k in found if k[0] == c[0])
        if own is False or (own is None and n_kind > 2):
            rec["candidates"][board_url(*c)] = {"skipped": "doesn't look like this firm's board", "seen_at": seen_at}
            continue
        r = await loop.run_in_executor(None, read_board, *c)
        r["seen_at"] = seen_at
        rec["candidates"][board_url(*c)] = r
    return rec


def decide(firm, rec):
    """New career_sites list and the reasons."""
    before = [str(s) for s in firm.get("career_sites") or []]
    norm_before = []
    for s in before:
        c = classify(s)
        norm_before.append(board_url(*c) if c and c[0] != "site" else s)
    good_new = [u for u, r in rec["candidates"].items() if r.get("ok") and r.get("count", 0) > 0]
    good_new.sort(key=lambda u: (is_student_board(*classify(u)), -rec["candidates"][u]["count"]))
    why = []
    ats = [s for s in norm_before if (classify(s) or ("site",))[0] != "site"]
    plain = [s for s in norm_before if (classify(s) or ("site",))[0] == "site"]
    if not ats:
        if good_new:
            after = good_new[:3]
            why.append("careers page leads to " + ", ".join(after) + "; replaced plain pages")
        else:
            after = list(plain)
            for p in rec["pages"]:
                if p["url"] in after and p.get("final") and not p.get("error") and (p.get("status") or 0) < 400:
                    fin = clean_page(p["final"])
                    if (urlparse(fin).hostname, urlparse(fin).path.rstrip("/")) != \
                            (urlparse(p["url"]).hostname, urlparse(p["url"]).path.rstrip("/")) and \
                            not re.search(r"login|signin|sign-in|auth|sso|captcha|challenge", fin, re.I):
                        after[after.index(p["url"])] = fin
                        why.append(f"{p['url']} now redirects to {fin}")
            after = list(dict.fromkeys(after))
    else:
        broken = [u for u in ats if not rec["boards"].get(u, {}).get("ok") and
                  re.search(r"404|410|422|not found", rec["boards"].get(u, {}).get("error", ""), re.I)]
        after = [u for u in ats if not (broken and good_new and u in broken)]
        if broken and good_new:
            why.append("dropped broken " + ", ".join(broken))
        extra = [u for u in good_new if u not in after][: max(0, 4 - len(after))]
        if extra:
            after += extra
            why.append("careers page also links to " + ", ".join(extra))
        if any(s != n for s, n in zip(before, norm_before)):
            why.append("standard address form")
    return after, why


async def main_async():
    from playwright.async_api import async_playwright
    entries = config_io.load()
    head = config_io.header()
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(user_agent=watch.UA, locale="en-US", viewport={"width": 1366, "height": 900})

        async def block(route):
            if route.request.resource_type in ("image", "media", "font"):
                await route.abort()
            else:
                await route.continue_()
        await ctx.route("**/*", block)
        sem = asyncio.Semaphore(PARALLEL_PAGES)
        firms = [e for e in entries if e.get("watch") is not False]
        recs, done = [], 0
        for i in range(0, len(firms), 16):
            batch = firms[i:i + 16]
            recs += await asyncio.gather(*(check_firm(ctx, sem, f) for f in batch))
            done += len(batch)
            print(f"{done}/{len(firms)} firms checked", flush=True)
        await browser.close()
    by_id = {r["id"]: r for r in recs}
    changes = []
    for e in entries:
        r = by_id.get(e["id"])
        if not r:
            continue
        after, why = decide(e, r)
        r["after"], r["why"] = after, why
        if after != [str(s) for s in e.get("career_sites") or []]:
            changes.append(r)
            e["career_sites"] = after
    config_io.save(entries, head=head)
    write_report(recs, changes)
    if changes:
        open(os.path.join(ROOT, "data", "quiet_next_run"), "w").close()
    print(f"{len(changes)} firms changed")


def write_report(recs, changes):
    os.makedirs(OUT, exist_ok=True)
    json.dump(recs, open(os.path.join(OUT, f"{TODAY}.json"), "w"), indent=1)
    lines = [f"# Career-site check, {TODAY}", "",
             f"{len(recs)} firms checked in a browser. {len(changes)} had their career sites changed.", ""]
    real = [r for r in changes if any("standard address form" != w for w in r["why"])]
    lines += ["## Changed", "", "| Firm | Before | After | Why |", "| --- | --- | --- | --- |"]
    for r in sorted(real, key=lambda r: r["name"].lower()):
        lines.append(f"| {r['name']} | {'<br>'.join(r['before'])} | {'<br>'.join(r['after'])} | {'; '.join(r['why'])} |")
    dead = [r for r in recs if any(not b.get("ok") for b in r["boards"].values())]
    lines += ["", "## Job-system boards that didn't answer", "", "| Firm | Board | Error |", "| --- | --- | --- |"]
    for r in sorted(dead, key=lambda r: r["name"].lower()):
        for u, b in r["boards"].items():
            if not b.get("ok"):
                lines.append(f"| {r['name']} | {u} | {b.get('error', '')} |")
    plain = [r for r in recs if r.get("after") and all((classify(s) or ("site",))[0] == "site" for s in r["after"])]
    lines += ["", "## Still plain web pages (no readable job system found)", "",
              "| Firm | Pages | Job systems seen but not readable |", "| --- | --- | --- |"]
    for r in sorted(plain, key=lambda r: r["name"].lower()):
        lines.append(f"| {r['name']} | {'<br>'.join(r['after'])} | {', '.join(r['other_systems']) or ''} |")
    skipped = [(r, u, c) for r in recs for u, c in r["candidates"].items() if c.get("skipped")]
    lines += ["", "## Boards found on a firm's pages but not added (name didn't match)", "",
              "| Firm | Board | Found on |", "| --- | --- | --- |"]
    for r, u, c in sorted(skipped, key=lambda x: x[0]["name"].lower()):
        lines.append(f"| {r['name']} | {u} | {c['seen_at'][:100]} |")
    open(os.path.join(OUT, f"{TODAY}.md"), "w").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    asyncio.run(main_async())
