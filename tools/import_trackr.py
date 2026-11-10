"""One-time import from Trackr. Not run on any schedule.

1. Fetches Trackr's three US Finance lists (2028 summer, 2027 full-time, 2027 summer).
2. Archives every raw Trackr link, untouched, in archive/trackr/ (the watcher never reads it).
3. Writes config/firms.yaml: one entry per firm with clean board addresses only,
   e.g. https://pjtpartners.wd1.myworkdayjobs.com/Careers instead of the Trackr link
   .../Students?Job_Profile=...&utm_source=Trackr...
4. For firms whose Trackr link points at a student-only board (Campus, Students,
   EarlyCareers...), finds the firm's main board on the same job system. The student
   board is kept as a second site only when the main board doesn't carry its postings.
   Findings go to archive/trackr/student-boards.json.

Run:  python tools/import_trackr.py
"""
import csv
import datetime as dt
import difflib
import json
import os
import re
import sys
import time

import requests
import yaml
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "watcher"))
from sites import STUDENT, board_url, classify, clean_page, is_student_board, listing_page  # noqa: E402

API = "https://api.the-trackr.com/programmes"
LISTS = [("2028", "summer-internships"), ("2027", "full-time-programmes"), ("2027", "summer-internships")]
ARCHIVE = os.path.join(ROOT, "archive", "trackr")
CONFIG = os.path.join(ROOT, "config", "firms.yaml")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
TODAY = dt.date.today().isoformat()

# Hand-checked sources that Trackr's links don't lead to (formerly data/overrides.json).
REPLACE = {
    "goldman-sachs": ["https://higher.gs.com/"],
    "jane-street": ["https://www.janestreet.com/join-jane-street/open-roles/"],
}


def fetch_lists():
    raw, progs = {}, []
    for season, typ in LISTS:
        r = S.get(API, params={"region": "US", "industry": "Finance", "season": season, "type": typ}, timeout=60)
        r.raise_for_status()
        data = r.json()
        raw[f"{season}/{typ}"] = data
        for p in data.get("programmes", []):
            p["_list"] = f"{season}/{typ}"
            progs.append(p)
    return raw, progs


# ------------------------------------------------------------------ probes (network)
def try_json(method, url, **kw):
    try:
        r = S.request(method, url, timeout=25, **kw)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:  # noqa: BLE001
        return None


def wd_page(host, tenant, site, search="", offset=0, limit=20):
    return try_json("POST", f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                    json={"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": search},
                    headers={"Accept": "application/json"})


def wd_all(host, tenant, site, cap=300):
    out, off = [], 0
    while off < cap:
        d = wd_page(host, tenant, site, offset=off)
        posts = (d or {}).get("jobPostings") or []
        out += posts
        if len(posts) < 20:
            break
        off += 20
    return out


def req_id(path):
    return (path or "").rstrip("/").split("_")[-1].lower()


def homepage_links(homepage, pattern):
    """Job-board links on the firm's own careers page."""
    if not homepage:
        return []
    try:
        page = S.get(homepage, timeout=25).text
    except Exception:  # noqa: BLE001
        return []
    return list(dict.fromkeys(re.findall(pattern, page)))


def strip_student(name):
    s = re.sub(r"[-_ ]?(campus|students?|early|graduates?|university|college|interns?(hips?)?|grads?)", "", name, flags=re.I)
    return s.strip("-_ ")


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def same_company(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    return a == b or (len(a) >= 5 and b.startswith(a)) or (len(b) >= 5 and a.startswith(b)) or \
        difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def main_workday(firm, key):
    host, tenant, site = key.split("|")
    cands = []
    for u in homepage_links(firm["homepage"], rf"https?://{re.escape(host)}/(?:[a-z]{{2}}-[A-Z]{{2}}/)?([A-Za-z0-9_\-]+)"):
        cands.append(u)
    try:
        r = S.get(f"https://{host}/", timeout=25, allow_redirects=True)
        seg = [s for s in r.url.split(host, 1)[-1].split("?")[0].split("/") if s]
        seg = [s for s in seg if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", s)]
        if seg:
            cands.append(seg[0])
    except Exception:  # noqa: BLE001
        pass
    base = strip_student(site)
    if base:
        cands.append(base)
    for t in (tenant, tenant.upper(), tenant.capitalize()):
        cands += [f"{t}_Careers", f"{t}Careers", f"{t}_External", t]
    cands += ["Careers", "External", "External_Careers", "External_Career_Site", "careers", "Search"]
    best = None
    for c in dict.fromkeys(cands):
        if c == site or STUDENT.search(c) or re.search(r"privileg|confidential|private|internal", c, re.I):
            continue
        d = wd_page(host, tenant, c, limit=1)
        total = (d or {}).get("total") or 0
        if total and (best is None or total > best[1]):
            best = (c, total)
    if not best:
        return None
    main_site, main_total = best
    student = wd_all(host, tenant, site)
    found = 0
    for p in student[:60]:
        hits = (wd_page(host, tenant, main_site, search=p.get("title") or "") or {}).get("jobPostings") or []
        if any(req_id(h.get("externalPath")) == req_id(p.get("externalPath")) for h in hits):
            found += 1
    checked = min(len(student), 60)
    return {"main": f"{host}|{tenant}|{main_site}", "main_total": main_total, "student_total": len(student),
            "student_checked": checked, "also_on_main": found}


def gh_board(token):
    meta = try_json("GET", f"https://boards-api.greenhouse.io/v1/boards/{token}")
    if not meta:
        return None, []
    jobs = (try_json("GET", f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs") or {}).get("jobs") or []
    return meta.get("name"), jobs


def main_greenhouse(firm, key):
    words = re.findall(r"[a-z0-9]+", firm["name"].lower())
    cands = homepage_links(firm["homepage"], r"greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_\-]+)")
    cands += [strip_student(key), re.sub(r"(recruiting|board|external|-)", "", strip_student(key)),
              "".join(words), words[0] if words else "", "".join(words[:2])]
    _, student = gh_board(key)
    for c in dict.fromkeys(x for x in cands if x and x != key and not STUDENT.search(x)):
        name, jobs = gh_board(c)
        if name and jobs and same_company(name, firm["name"]):
            titles = {norm(j.get("title")) for j in jobs}
            return {"main": c, "main_total": len(jobs), "student_total": len(student),
                    "student_checked": len(student),
                    "also_on_main": sum(1 for j in student if norm(j.get("title")) in titles)}
    return None


def oracle_site_number(host, site):
    try:
        page = S.get(f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/jobs", timeout=25).text
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"siteNumber\W{1,6}(CX_\d+)", page)
    return m.group(1) if m else None


def oracle_ids(host, num):
    finder = f"findReqs;siteNumber={num},limit=200,sortBy=POSTING_DATES_DESC"
    d = try_json("GET", f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                        f"?onlyData=true&expand=requisitionList&finder={requests.utils.quote(finder, safe=';=,')}",
                 headers={"Accept": "application/json"}) or {}
    items = d.get("items") or [{}]
    return {str(r.get("Id")) for r in (items[0].get("requisitionList") or [])}


def main_oracle(firm, key):
    host, site = key.split("|")
    student_num = oracle_site_number(host, site)
    student = oracle_ids(host, student_num) if student_num else set()
    for c in dict.fromkeys([strip_student(site), "CX_1", "CX"]):
        if not c or c == site:
            continue
        num = oracle_site_number(host, c)
        if num and num != student_num:
            main = oracle_ids(host, num)
            if main:
                return {"main": f"{host}|{c}", "main_total": len(main), "student_total": len(student),
                        "student_checked": len(student), "also_on_main": len(student & main)}
    return None


def paylocity_board(url):
    """A Paylocity single-job page links to the company's full job list."""
    if "/details/" not in url.lower():
        return url
    try:
        m = re.search(r"/Recruiting/Jobs/All/([0-9a-fA-F\-]{36})", S.get(url, timeout=25).text, re.I)
    except Exception:  # noqa: BLE001
        return url
    return f"https://recruiting.paylocity.com/recruiting/jobs/All/{m.group(1)}" if m else url


SHORTENERS = ("bit.ly", "tinyurl.com", "lnkd.in", "ow.ly", "t.co", "rebrand.ly", "buff.ly")


def resolve(url):
    """Follow short links (bit.ly...) to the real page."""
    host = (urlparse(url).hostname or "").lower()
    if host not in SHORTENERS:
        return url
    try:
        return S.get(url, timeout=25, allow_redirects=True).url
    except Exception:  # noqa: BLE001
        return url


def fix_source(src):
    kind, key = src
    if kind == "site":
        return (kind, listing_page(key))
    if kind == "paylocity":
        return (kind, paylocity_board(key))
    return src


MAIN_FINDERS = {"workday": main_workday, "greenhouse": main_greenhouse, "oracle": main_oracle}


# ------------------------------------------------------------------ build
def build(progs):
    firms = {}
    for p in progs:
        c = p.get("company") or {}
        fid = c.get("id") or p.get("companyId")
        if not fid:
            continue
        if fid not in firms:
            firms[fid] = {"id": fid, "name": c.get("name", fid), "categories": set(), "links": [],
                          "homepage": clean_page(resolve(c.get("careersSite") or ""))}
        f = firms[fid]
        f["categories"].update(p.get("categories") or [])
        if p.get("url"):
            f["links"].append(resolve(p["url"]))

    review, config = [], []
    for fid, f in sorted(firms.items(), key=lambda x: x[1]["name"].lower()):
        srcs = list(dict.fromkeys(fix_source(c) for c in (classify(u) for u in f["links"]) if c))
        ats = [s for s in srcs if s[0] != "site"]
        sites = []
        if fid in REPLACE:
            sites = list(REPLACE[fid])
        elif ats:
            for kind, key in ats:
                if is_student_board(kind, key) and kind in MAIN_FINDERS:
                    t0 = time.time()
                    try:
                        found = MAIN_FINDERS[kind](f, key)
                    except Exception as e:  # noqa: BLE001
                        found = {"error": f"{type(e).__name__}: {e}"}
                    entry = {"firm": f["name"], "system": kind, "student_board": board_url(kind, key),
                             "seconds": round(time.time() - t0, 1)}
                    if found and "main" in found:
                        main_url = board_url(kind, found["main"])
                        mirrored = found["student_checked"] > 0 and found["also_on_main"] >= 0.8 * found["student_checked"]
                        entry.update(found, main_board=main_url)
                        entry["decision"] = ("main board only (it carries the student postings)" if mirrored else
                                             "main board plus student board (student postings aren't all on main)")
                        sites.append(main_url)
                        if not mirrored:
                            sites.append(board_url(kind, key))
                    else:
                        entry.update(found or {}, decision="student board only (no main board found)")
                        sites.append(board_url(kind, key))
                    review.append(entry)
                else:
                    sites.append(board_url(kind, key))
        else:
            sites = [s[1] for s in srcs][:2]
            home = classify(f["homepage"]) if f["homepage"] else None
            if home:  # not LinkedIn
                sites.append(listing_page(home[1]) if home[0] == "site" else board_url(*home))
        entry = {"id": fid, "name": f["name"], "categories": sorted(f["categories"])}
        if f["homepage"]:
            entry["homepage"] = f["homepage"]
        entry["career_sites"] = list(dict.fromkeys(sites))
        config.append(entry)
    return config, review


def write_config(config):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    head = (f"# Firms the job watcher checks. Built once from Trackr's US Finance lists on {TODAY};\n"
            "# the watcher no longer reads Trackr. The raw Trackr links live in archive/trackr/.\n"
            "#\n"
            "# career_sites  the job boards the watcher reads. Plain addresses only, no tracking tags or filters.\n"
            "#               The watcher works out the job system (Workday, Greenhouse...) from the address.\n"
            "# homepage      the firm's careers page, for you. Read only when a firm has no job board listed.\n"
            "# watch: false  add this line to stop checking a firm without deleting it.\n"
            "#\n"
            "# To add a firm, copy any entry and give it a new id.\n\n")
    def scalar(v):  # block style: URLs stay unquoted
        return yaml.safe_dump({"x": v}, allow_unicode=True, width=1000, default_flow_style=False).strip()[3:]

    def flow(v):  # inside [ ]
        return yaml.safe_dump({"x": v}, allow_unicode=True, width=1000, default_flow_style=True).strip()[4:-1]

    with open(CONFIG, "w") as fh:
        fh.write(head)
        for e in config:
            fh.write(f"- id: {scalar(e['id'])}\n  name: {scalar(e['name'])}\n")
            fh.write(f"  categories: [{', '.join(flow(c) for c in e['categories'])}]\n")
            if e.get("homepage"):
                fh.write(f"  homepage: {scalar(e['homepage'])}\n")
            if e["career_sites"]:
                fh.write("  career_sites:\n" + "".join(f"    - {scalar(s)}\n" for s in e["career_sites"]) + "\n")
            else:
                fh.write("  career_sites: []\n\n")
    yaml.safe_load(open(CONFIG))  # fail loudly if the file isn't valid YAML


def write_archive(raw, progs, config):
    os.makedirs(ARCHIVE, exist_ok=True)
    json.dump({"retrieved": TODAY, "source": API, "lists": raw}, open(os.path.join(ARCHIVE, "programmes.json"), "w"),
              indent=1)
    with open(os.path.join(ARCHIVE, "links.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["list", "firm_id", "firm", "program", "opening", "closing", "trackr_link", "clean_board"])
        for p in sorted(progs, key=lambda p: ((p.get("company") or {}).get("name") or "", p["_list"])):
            c = p.get("company") or {}
            k = classify(p.get("url") or "")
            w.writerow([p["_list"], c.get("id") or "", c.get("name") or "", p.get("name") or "",
                        (p.get("openingDate") or "")[:10], (p.get("closingDate") or "")[:10], p.get("url") or "",
                        board_url(*k) if k else ""])
        for c in {(p.get("company") or {}).get("id"): p.get("company") or {} for p in progs}.values():
            if c.get("careersSite"):
                w.writerow(["company careers page", c.get("id"), c.get("name"), "", "", "", c["careersSite"],
                            clean_page(c["careersSite"])])


def main():
    raw, progs = fetch_lists()
    config, review = build(progs)
    write_archive(raw, progs, config)
    json.dump(review, open(os.path.join(ARCHIVE, "student-boards.json"), "w"), indent=1)
    write_config(config)
    n_sites = sum(len(e["career_sites"]) for e in config)
    print(f"{len(progs)} programs, {len(config)} firms, {n_sites} career sites, {len(review)} student boards checked")
    for r in review:
        print(f"- {r['firm']}: {r['decision']} ({r.get('also_on_main', '?')}/{r.get('student_checked', '?')} on main)")


if __name__ == "__main__":
    main()
