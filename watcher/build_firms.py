"""Build data/firms.json, the firm database, from the Trackr US Finance lists.

Each firm gets one or more "sources", meaning the job site we can read for it
(Workday, Greenhouse, Oracle and so on). data/overrides.json lets us add or
replace sources for firms by hand.
"""
import csv
import json
import os
import re
import sys
from urllib.parse import urlparse, parse_qs

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
API = "https://api.the-trackr.com/programmes"
LISTS = [
    ("2028", "summer-internships"),
    ("2027", "full-time-programmes"),
    ("2027", "summer-internships"),
]
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def oleeo_listing(u):
    """Turn any Oleeo (tal.net) link into its public vacancy-list page."""
    path = re.sub(r"/(user-\d+|xf-[0-9a-f]+|wid-\d+)(?=/)", "", u.path)
    m = re.search(r"^(.*?/candidate/)", path)
    if not m:
        return f"https://{u.hostname}{path}"
    prefix = m.group(1)
    board = re.search(r"/jobboard/vacancy/(\d+)/", path) or re.search(r"/pl/(\d+)/", path)
    n = board.group(1) if board else "1"
    return f"https://{u.hostname}{prefix}jobboard/vacancy/{n}/adv/"


def classify(url):
    """Return (kind, key) for a job-site URL, or None if it is not usable."""
    try:
        u = urlparse(url)
    except ValueError:
        return None
    h = (u.hostname or "").lower()
    seg = [s for s in u.path.split("/") if s]
    q = parse_qs(u.query)
    if not h:
        return None
    if h.endswith("myworkdayjobs.com"):
        tenant = h.split(".")[0]
        site = seg[1] if seg and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", seg[0]) and len(seg) > 1 else (seg[0] if seg else None)
        return ("workday", f"{h}|{tenant}|{site}") if site else None
    if h.endswith("myworkdaysite.com"):
        if "recruiting" in seg:
            i = seg.index("recruiting")
            if len(seg) > i + 2:
                return ("workday", f"{h}|{seg[i+1]}|{seg[i+2]}")
        return None
    if h.endswith("oraclecloud.com"):
        if "sites" in seg and len(seg) > seg.index("sites") + 1:
            return ("oracle", f"{h}|{seg[seg.index('sites') + 1]}")
        return None
    if h.endswith("greenhouse.io"):
        if seg and seg[0] == "embed":
            board = (q.get("for") or [None])[0]
            return ("greenhouse", board) if board else None
        return ("greenhouse", seg[0]) if seg else None
    if h == "jobs.lever.co":
        return ("lever", seg[0]) if seg else None
    if h == "jobs.ashbyhq.com":
        return ("ashby", seg[0]) if seg else None
    if h.endswith("smartrecruiters.com"):
        return ("smartrecruiters", seg[0]) if seg else None
    if h.endswith(".tal.net"):
        return ("oleeo", oleeo_listing(u))
    if h.endswith(".icims.com"):
        return ("icims", h)
    if h.endswith(".avature.net") or h == "careers.twosigma.com":
        if seg and re.fullmatch(r"[a-z]{2}_[A-Z]{2}", seg[0]):
            return ("avature", f"{h}|/{seg[0]}/{seg[1] if len(seg) > 1 else 'careers'}")
        return ("avature", f"{h}|/{seg[0] if seg else 'careers'}")
    if re.search(r"/careers/(JobDetail|SearchJobs)", u.path):
        i = seg.index("careers")
        return ("avature", f"{h}|/{'/'.join(seg[:i + 1])}")
    if h == "jobs.jobvite.com" and seg:
        return ("site", f"https://jobs.jobvite.com/{seg[0]}/jobs")
    if h.endswith("teamtailor.com"):
        return ("site", f"https://{h}/jobs")
    if h.endswith("applytojob.com"):
        return ("site", f"https://{h}/apply")
    if h.endswith("applicantpro.com"):
        return ("site", f"https://{h}/jobs/")
    if h.endswith("hiringthing.com"):
        return ("site", f"https://{h}/")
    if h.endswith("breezy.hr"):
        return ("breezy", h)
    if h.endswith("bamboohr.com"):
        return ("bamboo", h)
    if h == "apply.workable.com" or h.endswith(".workable.com"):
        return ("workable", seg[0]) if seg else None
    if h.endswith("paylocity.com"):
        return ("paylocity", url.split("?")[0])
    if (h.endswith("ultipro.com") or h.endswith("ukg.net")) and len(seg) >= 3 and seg[1].lower() == "jobboard":
        return ("ukg", f"{h}|{seg[0]}|{seg[2]}")
    if "workforcenow" in h and "adp.com" in h:
        cid = (q.get("cid") or [None])[0]
        return ("adp", cid) if cid else None
    if h.endswith("linkedin.com") or h == "bit.ly":
        return None
    return ("site", url.split("#")[0])


def fetch_lists():
    out = []
    for season, typ in LISTS:
        r = requests.get(API, params={"region": "US", "industry": "Finance", "season": season, "type": typ},
                         headers=UA, timeout=60)
        r.raise_for_status()
        for p in r.json().get("programmes", []):
            p["_list"] = f"{season}/{typ}"
            out.append(p)
    return out


def build(programmes, overrides):
    firms = {}
    for p in programmes:
        c = p.get("company") or {}
        fid = c.get("id") or p.get("companyId")
        if not fid:
            continue
        f = firms.setdefault(fid, {
            "id": fid, "name": c.get("name", fid), "categories": set(),
            "careers_site": c.get("careersSite") or "", "programs": [], "links": set(),
        })
        f["categories"].update(p.get("categories") or [])
        f["programs"].append({"list": p["_list"], "name": p.get("name", ""), "url": p.get("url") or "",
                              "opening": p.get("openingDate"), "closing": p.get("closingDate")})
        if p.get("url"):
            f["links"].add(p["url"])

    result = []
    for fid, f in firms.items():
        srcs = {}
        for link in f["links"]:
            c = classify(link)
            if c:
                srcs.setdefault(c, 0)
                srcs[c] += 1
        ats = [k for k in srcs if k[0] != "site"]
        sources = [{"kind": k, "key": v} for k, v in ats]
        if not sources:
            sites = [v for k, v in srcs if k == "site"][:2]
            if f["careers_site"]:
                sites.append(f["careers_site"])
            sources = [{"kind": "site", "key": s} for s in dict.fromkeys(sites)]
        ov = overrides.get(fid)
        if ov is not None:
            if ov.get("replace"):
                sources = ov.get("sources", [])
            else:
                sources = sources + [s for s in ov.get("sources", []) if s not in sources]
        result.append({
            "id": fid, "name": f["name"], "categories": sorted(f["categories"]),
            "careers_site": f["careers_site"], "sources": sources,
            "programs": f["programs"],
        })
    result.sort(key=lambda x: x["name"].lower())
    return result


def main():
    ov_path = os.path.join(DATA, "overrides.json")
    overrides = json.load(open(ov_path)) if os.path.exists(ov_path) else {}
    firms = build(fetch_lists(), overrides)
    json.dump(firms, open(os.path.join(DATA, "firms.json"), "w"), indent=1)
    with open(os.path.join(DATA, "firms.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["firm", "categories", "careers_site", "job_site_type", "job_site", "programs"])
        for f in firms:
            w.writerow([f["name"], "/".join(f["categories"]), f["careers_site"],
                        " ; ".join(s["kind"] for s in f["sources"]),
                        " ; ".join(s["key"] for s in f["sources"]),
                        " | ".join(sorted({p["name"] for p in f["programs"] if p["name"]}))])
    kinds = {}
    for f in firms:
        k = f["sources"][0]["kind"] if f["sources"] else "none"
        kinds[k] = kinds.get(k, 0) + 1
    print(f"{len(firms)} firms", json.dumps(kinds))


if __name__ == "__main__":
    sys.exit(main())
