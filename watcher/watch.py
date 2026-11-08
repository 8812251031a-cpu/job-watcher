"""Check every firm's job site and alert on new postings that look like
2027 or full-time analyst roles.

Run:  python watcher/watch.py            (normal run: alert on new matches)
      python watcher/watch.py --report   (also write a per-firm health report)
"""
import concurrent.futures as cf
import datetime as dt
import html
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse, quote

import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 25

# ---------------------------------------------------------------- the filter
YEAR = re.compile(r"\b2027\b")
LATER = re.compile(r"\b20(28|29|30)\b")
FULLTIME = re.compile(r"full[\s\-]?time|new[\s\-]?grad|graduate (program|programme|analyst|scheme)|campus hire|"
                      r"analyst (program|programme|class)|rotational (program|analyst)|entry[\s\-]level analyst", re.I)
ROLE = re.compile(r"analyst|associate|trader|researcher", re.I)
# Internships, non-English student schemes and senior roles never count, even with 2027 in the title.
EXCLUDE = re.compile(r"summer|\bintern\b|\binterns\b|internship|co-?op|apprentic|\bstage\b|off[\s\-]?cycle|insight|"
                     r"spring week|winter|ausbildung|werkstudent|working student|praktikum|stagiaire|alternance|"
                     r"duales|studium|bachelor of science|test req|experienced|lateral|senior|vice president|\bvp\b|"
                     r"director|manager|principal|\blead\b|head of|managing", re.I)


def is_match(title):
    """True for titles that say 2027, or that read like a full-time analyst program.
    Internships and senior roles are always left out."""
    t = " ".join(title.split())
    if EXCLUDE.search(t):
        return False
    if YEAR.search(t):
        return True
    if LATER.search(t):
        return False
    return bool(FULLTIME.search(t) and ROLE.search(t))


# ---------------------------------------------------------------- http helpers
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})


def get(url, **kw):
    r = S.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def post(url, **kw):
    r = S.post(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def job(title, url, jid=None, loc=""):
    title = " ".join(html.unescape(title or "").split())
    return {"title": title, "url": url, "id": str(jid or url), "location": loc or ""}


# ---------------------------------------------------------------- adapters
def a_workday(key):
    host, tenant, site = key.split("|")
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    jobs, seen = [], set()
    for q, pages in (("2027", 5), ("full time analyst", 2), ("analyst program", 1), ("", 1)):
        for page in range(pages):
            body = {"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": q}
            data = post(api, json=body, headers={"Accept": "application/json"}).json()
            posts = data.get("jobPostings", [])
            for p in posts:
                path = p.get("externalPath", "")
                if path in seen:
                    continue
                seen.add(path)
                jobs.append(job(p.get("title"), f"https://{host}/en-US/{site}{path}", path, p.get("locationsText")))
            if len(posts) < 20 or (page + 1) * 20 >= (data.get("total") or 0):
                break
    return jobs


_oracle_site = {}


def a_oracle(key):
    host, site = key.split("|")
    num = site if re.fullmatch(r"CX(_\d+)?", site) else _oracle_site.get(key)
    if not num:
        page = get(f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/jobs").text
        m = re.search(r"siteNumber\W{1,6}(CX_\d+)", page) or re.search(r"\b(CX_\d+)\b", page)
        if not m:
            raise RuntimeError("oracle siteNumber not found")
        num = _oracle_site[key] = m.group(1)
    jobs = []
    for kw in ("", "2027"):
        finder = f"findReqs;siteNumber={num},limit=100,sortBy=POSTING_DATES_DESC"
        if kw:
            finder += f",keyword=\"{kw}\""
        url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
               f"?onlyData=true&expand=requisitionList&finder={quote(finder, safe=';=,')}")
        items = get(url, headers={"Accept": "application/json"}).json().get("items", [])
        for it in items:
            for r in it.get("requisitionList", []) or []:
                jobs.append(job(r.get("Title"), f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r.get('Id')}",
                                r.get("Id"), r.get("PrimaryLocation")))
    return jobs


def a_greenhouse(board):
    data = get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs").json()
    return [job(j["title"], j.get("absolute_url"), j.get("id"), (j.get("location") or {}).get("name"))
            for j in data.get("jobs", [])]


def a_lever(co):
    data = get(f"https://api.lever.co/v0/postings/{co}?mode=json").json()
    return [job(j.get("text"), j.get("hostedUrl"), j.get("id"), (j.get("categories") or {}).get("location"))
            for j in data]


def a_ashby(org):
    data = get(f"https://api.ashbyhq.com/posting-api/job-board/{org}").json()
    return [job(j.get("title"), j.get("jobUrl"), j.get("id"), j.get("location")) for j in data.get("jobs", [])]


def a_smartrecruiters(co):
    data = get(f"https://api.smartrecruiters.com/v1/companies/{co}/postings?limit=100").json()
    return [job(j.get("name"), f"https://jobs.smartrecruiters.com/{co}/{j.get('id')}", j.get("id"),
                (j.get("location") or {}).get("city")) for j in data.get("content", [])]


def a_workable(co):
    data = post(f"https://apply.workable.com/api/v3/accounts/{co}/jobs",
                json={"query": "", "location": [], "department": [], "worktype": [], "remote": []}).json()
    return [job(j.get("title"), f"https://apply.workable.com/{co}/j/{j.get('shortcode')}", j.get("shortcode"))
            for j in data.get("results", [])]


def a_bamboo(host):
    data = get(f"https://{host}/careers/list", headers={"Accept": "application/json"}).json()
    return [job(j.get("jobOpeningName"), f"https://{host}/careers/{j.get('id')}", j.get("id"))
            for j in data.get("result", [])]


def a_breezy(host):
    data = get(f"https://{host}/json").json()
    return [job(j.get("name"), j.get("url"), j.get("id")) for j in data]


def a_ukg(key):
    host, tenant, board = key.split("|")
    base = f"https://{host}/{tenant}/JobBoard/{board}"
    body = {"opportunitySearch": {"Top": 100, "Skip": 0, "QueryString": "",
                                  "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}],
                                  "Filters": []}, "matchCriteria": {"PreferredJobs": [], "Educations": [],
                                                                    "LicenseAndCertifications": [], "Skills": [],
                                                                    "hasNoLicenses": False, "SkippedSkills": []}}
    data = post(f"{base}/JobBoardView/LoadSearchResults", json=body).json()
    return [job(o.get("Title"), f"{base}/OpportunityDetail?opportunityId={o.get('Id')}", o.get("Id"))
            for o in data.get("opportunities", [])]


def a_adp(cid):
    url = ("https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
           f"?cid={cid}&lang=en_US&locale=en_US&$top=100")
    data = get(url, headers={"Accept": "application/json"}).json()
    out = []
    for r in data.get("jobRequisitions", []):
        jid = r.get("itemID")
        out.append(job(r.get("requisitionTitle"),
                       f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}&jobId={jid}",
                       jid))
    return out


def a_paylocity(url):
    page = get(url).text
    m = re.search(r"window\.pageData\s*=\s*", page)
    if not m:
        raise RuntimeError("Paylocity page data not found")
    data, _ = json.JSONDecoder().raw_decode(page, m.end())
    return [job(j.get("JobTitle"), f"https://recruiting.paylocity.com/Recruiting/Jobs/Details/{j.get('JobId')}", j.get("JobId"),
                (j.get("LocationName") or "")) for j in data.get("Jobs", [])]


def anchors(page, base, href_re=None):
    soup = BeautifulSoup(page, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href_re and not re.search(href_re, href):
            continue
        text = a.get("title") or a.get_text(" ", strip=True)
        text = re.sub(r"^\d+\s*-\s*", "", text or "")
        if text and 3 < len(text) < 200:
            out.append(job(text, urljoin(base, href)))
    return out


def a_icims(host):
    jobs = []
    for kw in ("", "2027", "analyst"):
        url = f"https://{host}/jobs/search?ss=1&in_iframe=1&searchKeyword={quote(kw)}"
        jobs += anchors(get(url).text, url, r"/jobs/\d+/")
    return jobs


def a_avature(key):
    host, base = key.split("|")
    url = f"https://{host}{base}/SearchJobs?jobRecordsPerPage=100"
    return anchors(get(url).text, url, r"/JobDetail/")


def a_oleeo(url):
    """url is the vacancy-list page. If it lists nothing, try the other board number."""
    targets = [url]
    m = re.search(r"/jobboard/vacancy/(\d+)/adv/$", url)
    if m:
        other = "2" if m.group(1) == "1" else "1"
        targets.append(url[: m.start(1)] + other + "/adv/")
    jobs = []
    for target in targets:
        try:
            jobs += anchors(get(target).text, target, r"/opp/")
        except Exception:
            continue
        if jobs:
            break
    if not jobs:
        raise RuntimeError("no Oleeo vacancies found")
    return jobs


def phenom_jobs(page, base):
    """Phenom career sites embed search results as JSON under eagerLoadRefineSearch."""
    k = '"eagerLoadRefineSearch":'
    i = page.find(k)
    if i < 0:
        return []
    try:
        obj, _ = json.JSONDecoder().raw_decode(page, i + len(k))
    except ValueError:
        return []
    out = []
    for j in ((obj.get("data") or {}).get("jobs") or []):
        jid = j.get("jobId") or j.get("reqId") or j.get("jobSeqNo")
        out.append(job(j.get("title"), f"{base}/job/{jid}", j.get("jobSeqNo") or jid, j.get("location")))
    return out


def a_site(url):
    """Generic: read the page, follow Phenom / SuccessFactors search if detected,
    otherwise use link and heading text."""
    r = get(url)
    page, final = r.text, r.url
    u = urlparse(final)
    jobs = []
    if "phApp" in page or "eagerLoadRefineSearch" in page:
        m = re.match(r"^/([a-z]{2,6})/([a-z]{2})(/|$)", u.path)
        base = f"{u.scheme}://{u.netloc}" + (f"/{m.group(1)}/{m.group(2)}" if m else "")
        for kw in ("2027", "full time analyst", "analyst program"):
            try:
                jobs += phenom_jobs(get(f"{base}/search-results?keywords={quote(kw)}").text, base)
            except Exception:
                pass
        jobs += phenom_jobs(page, base)
        if jobs:
            return jobs
    if "jobTitle-link" in page or "/search/?q=" in page:
        base = f"{u.scheme}://{u.netloc}"
        for kw in ("2027", "analyst"):
            try:
                jobs += anchors(get(f"{base}/search/?q={quote(kw)}&sortColumn=referencedate&sortDirection=desc").text,
                                base, r"/job/")
            except Exception:
                pass
        if jobs:
            return jobs
    jobs = anchors(page, final)
    soup = BeautifulSoup(page, "html.parser")
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "li"]):
        t = tag.get_text(" ", strip=True)
        if t and 3 < len(t) < 160:
            jobs.append(job(t, final, "text:" + t))
    return jobs


GS_QUERY = ("query GetRoles($searchQueryInput: RoleSearchQueryInput!) { roleSearch(searchQueryInput: $searchQueryInput) "
            "{ totalCount items { roleId corporateTitle jobTitle locations { primary city country } } } }")


def a_goldman(_key):
    jobs = []
    for term in ("2027", "analyst program", "new analyst", "full time analyst"):
        body = {"operationName": "GetRoles", "query": GS_QUERY, "variables": {"searchQueryInput": {
            "page": {"pageSize": 40, "pageNumber": 0}, "sort": {"sortStrategy": "POSTED_DATE", "sortOrder": "DESC"},
            "filters": [], "experiences": ["EARLY_CAREER", "PROFESSIONAL"], "searchTerm": term}}}
        data = post("https://api-higher.gs.com/gateway/api/v1/graphql", json=body,
                    headers={"Origin": "https://higher.gs.com", "Referer": "https://higher.gs.com/"}).json()
        for it in ((data.get("data") or {}).get("roleSearch") or {}).get("items") or []:
            rid = str(it.get("roleId", ""))
            loc = ", ".join(l.get("city") or "" for l in it.get("locations") or [] if l.get("primary"))
            jobs.append(job(f"{it.get('jobTitle')} ({it.get('corporateTitle')})",
                            f"https://higher.gs.com/roles/{rid.split('_')[0]}", rid, loc))
    return jobs


def a_janestreet(_key):
    data = get("https://www.janestreet.com/jobs/main.json").json()
    return [job(f"{j.get('position')} ({j.get('availability')})",
                f"https://www.janestreet.com/join-jane-street/position/{j.get('id')}/", j.get("id"), j.get("city"))
            for j in data]


ADAPTERS = {
    "goldman": a_goldman, "janestreet": a_janestreet,
    "workday": a_workday, "oracle": a_oracle, "greenhouse": a_greenhouse, "lever": a_lever,
    "ashby": a_ashby, "smartrecruiters": a_smartrecruiters, "workable": a_workable, "bamboo": a_bamboo,
    "breezy": a_breezy, "ukg": a_ukg, "adp": a_adp, "paylocity": a_paylocity, "icims": a_icims,
    "avature": a_avature, "oleeo": a_oleeo, "site": a_site,
}


def check_firm(firm):
    res = {"id": firm["id"], "name": firm["name"], "sources": [], "matches": []}
    for src in firm["sources"]:
        fn = ADAPTERS.get(src["kind"])
        entry = {"kind": src["kind"], "key": src["key"], "ok": False, "count": 0, "error": ""}
        if not fn:
            entry["error"] = "no adapter"
            res["sources"].append(entry)
            continue
        try:
            jobs = fn(src["key"])
            entry["ok"], entry["count"] = True, len(jobs)
            for j in jobs:
                if j["title"] and is_match(j["title"]):
                    j["key"] = f"{firm['id']}|{src['kind']}|{j['id'] if not j['id'].startswith('text:') else j['title'].lower()}"
                    res["matches"].append(j)
        except Exception as e:  # noqa: BLE001
            entry["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        res["sources"].append(entry)
    uniq = {}
    for m in res["matches"]:
        uniq.setdefault(m["key"], m)
    res["matches"] = list(uniq.values())
    return res


# ---------------------------------------------------------------- alerts
def ntfy(topic, title, body, click=None, priority="default"):
    if not topic:
        return
    headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": "briefcase"}
    if click:
        headers["Click"] = click
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers, timeout=20)
    except Exception as e:  # noqa: BLE001
        print("ntfy failed:", e)


def github_issue(title, body):
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return
    owner = repo.split("/")[0]
    try:
        requests.post(f"https://api.github.com/repos/{repo}/issues",
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                      json={"title": title, "body": body + f"\n\ncc @{owner}"}, timeout=20)
    except Exception as e:  # noqa: BLE001
        print("issue failed:", e)


# ---------------------------------------------------------------- main
def main():
    report_mode = "--report" in sys.argv
    if not os.path.exists(os.path.join(DATA, "firms.json")):
        print("No firm list yet. Run the 'Refresh firm list' workflow first.")
        return
    firms = json.load(open(os.path.join(DATA, "firms.json")))
    seen_path = os.path.join(DATA, "seen.json")
    first_run = not os.path.exists(seen_path)
    seen = {} if first_run else json.load(open(seen_path))
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(check_firm, firms))
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    new = []
    for r in results:
        for m in r["matches"]:
            if m["key"] not in seen:
                seen[m["key"]] = {"firm": r["name"], "title": m["title"], "url": m["url"], "first_seen": now}
                new.append((r["name"], m))

    topic = os.environ.get("NTFY_TOPIC", "")
    if first_run:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        groups = {}
        for f, m in new:
            groups.setdefault(f, []).append(m)
        with open(os.path.join(DATA, "open_now.md"), "w") as fh:
            fh.write(f"# Open right now: {len(new)} postings\n\nTitles with 2027 or a full-time analyst sign, as of {now}.\n\n")
            for f in sorted(groups):
                fh.write(f"## {f}\n" + "".join(f"- [{m['title']}]({m['url']})\n" for m in groups[f]) + "\n")
        link = f"https://github.com/{repo}/blob/main/data/open_now.md" if repo else "data/open_now.md"
        top = sorted(groups.items(), key=lambda x: -len(x[1]))
        github_issue(f"Job watcher is live: {len(new)} matching postings open right now",
                     f"Full list by firm: {link}\n\nFrom now on you'll only get alerts for new postings.\n\n"
                     + "\n".join(f"- **{f}**: {len(ms)}" for f, ms in top))
        ntfy(topic, "Job watcher is live",
             f"{len(new)} matching postings are open across {len(groups)} firms. Tap for the full list. "
             "From now on you'll get a push for each new one.", click=link)
    elif new:
        for firm, m in new[:25]:
            ntfy(topic, f"{firm}: new posting", m["title"], click=m["url"], priority="high")
        if len(new) > 25:
            ntfy(topic, "More new postings", f"{len(new) - 25} more. See the GitHub email.")
        github_issue(f"{len(new)} new posting(s): " + ", ".join(sorted({f for f, _ in new}))[:200],
                     "\n".join(f"- **{f}**: [{m['title']}]({m['url']})" for f, m in new))

    json.dump(seen, open(seen_path, "w"), indent=1, sort_keys=True)

    health = []
    for r in results:
        ok = any(s["ok"] for s in r["sources"])
        total = sum(s["count"] for s in r["sources"])
        health.append({"firm": r["name"], "id": r["id"], "readable": ok and total > 0, "postings_read": total,
                       "matches": len(r["matches"]), "sources": r["sources"],
                       "sample_matches": [m["title"] for m in r["matches"][:5]]})
    if report_mode or first_run:
        json.dump(health, open(os.path.join(DATA, "report.json"), "w"), indent=1)
    good = sum(1 for h in health if h["readable"])
    with open(os.path.join(DATA, "status.md"), "w") as fh:
        fh.write(f"Last run {now}. {good}/{len(health)} firms readable. "
                 f"{len(new)} new matches this run. Took {time.time() - t0:.0f}s.\n")
    print(open(os.path.join(DATA, "status.md")).read())


if __name__ == "__main__":
    main()
