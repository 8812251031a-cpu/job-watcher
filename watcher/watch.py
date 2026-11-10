"""Check every firm's job site and alert on new postings that look like
2027 or full-time analyst roles.

Reads the firm list from config/firms.yaml (no Trackr).

Run:  python watcher/watch.py            (normal run: alert on new matches)
      python watcher/watch.py --report   (also write a per-firm health report)
      python watcher/watch.py --quiet    (record new matches without alerting)
A file named data/quiet_next_run makes the next run quiet once, then is removed.
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
import yaml
from bs4 import BeautifulSoup

from sites import board_url, classify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CONFIG = os.path.join(ROOT, "config", "firms.yaml")
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


# ---------------------------------------------------------------- location filter
# Keep: United States, mainland China, Hong Kong. A posting with no location
# information is kept, since we can't tell where it is.
US_STATES = ("Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|"
             "Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|"
             "Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|"
             "North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|"
             "Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia")
US_CODES = ("AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|"
            "OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC")
US_CITIES = ("New York|NYC|San Francisco|Chicago|Boston|Houston|Dallas|Charlotte|Los Angeles|Miami|Atlanta|Philadelphia|"
             "Minneapolis|Denver|Seattle|Austin|Pittsburgh|Buffalo|Stamford|Greenwich|Jersey City|Menlo Park|Palo Alto|"
             "Salt Lake City|Columbus|Cleveland|Detroit|Nashville|Richmond|Baltimore|St\\. Louis|Kansas City|Phoenix|"
             "San Diego|Wilmington|Princeton|Newport Beach|Cincinnati|Milwaukee|Omaha|Tampa|Raleigh|Indianapolis|"
             "Little Rock|Des Moines|Hartford|Irvine|Plano|Fort Worth|San Antonio|Tulsa|Broomfield|Akron|Silicon Valley")
ALLOWED = re.compile(
    rf"united states|americas|\b({US_STATES})\b|\b({US_CITIES})\b|"
    r"china|shanghai|beijing|shenzhen|guangzhou|hangzhou|chengdu|suzhou|tianjin|nanjing|wuhan|xi'?an|"
    r"hong kong|kowloon", re.I)
ALLOWED_SHORT = re.compile(r"\b(US|USA|U\.S\.A?|PRC|HK|HKG|SHA|NYC)\b")  # case-sensitive
ALLOWED_CODES = re.compile(rf"(?:,\s*|[\s\-/])({US_CODES})(?=$|[\s\-/,)])")
ELSEWHERE = re.compile(
    r"london|united kingdom|\bUK\b|england|scotland|edinburgh|glasgow|ireland|dublin|cork|frankfurt|germany|munich|"
    r"m(?:ü|ue|u)nchen|berlin|hamburg|d(?:ü|ue|u)sseldorf|stuttgart|paris|france|milan|italy|madrid|spain|amsterdam|"
    r"netherlands|z(?:ü|ue|u)rich|geneva|switzerland|luxembourg|brussels|belgium|stockholm|sweden|oslo|norway|"
    r"copenhagen|denmark|helsinki|finland|vienna|austria|warsaw|poland|prague|lisbon|portugal|athens|dubai|abu dhabi|"
    r"\bUAE\b|riyadh|saudi|doha|qatar|tel aviv|israel|mumbai|bangalore|bengaluru|india|delhi|gurgaon|gurugram|"
    r"hyderabad|pune|chennai|singapore|tokyo|japan|seoul|korea|sydney|melbourne|australia|new zealand|auckland|"
    r"toronto|canada|montreal|vancouver|calgary|mexico|s(?:ã|a)o paulo|brazil|buenos aires|bogot|santiago|lima|"
    r"johannesburg|south africa|taipei|taiwan|macau|macao|manila|philippines|jakarta|indonesia|kuala lumpur|malaysia|"
    r"bangkok|thailand|vietnam|budapest|bucharest|krak(?:ó|o)w|northampton|leeds|\bEMEA\b|\bLDN\b|\bAMS\b|"
    r"\bSGP\b|cayman|bermuda|jersey channel|guernsey", re.I)


def location_ok(j):
    """Keep postings in the US, mainland China or Hong Kong, plus any whose location is unknown."""
    raw_path = re.sub(r"\b[a-z]{2}[-_][A-Z]{2}\b", " ", urlparse(j.get("url") or "").path)  # drop locales like en-US
    raw = " ".join([j.get("title") or "", str(j.get("location") or ""), raw_path])
    text = raw.replace("-", " ").replace("_", " ")
    if ALLOWED.search(text) or ALLOWED_SHORT.search(text) or ALLOWED_CODES.search(raw):
        return True
    return not ELSEWHERE.search(text)


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


MAX_AGE_DAYS = 14
TODAY = dt.datetime.now(dt.timezone.utc).date()


def to_date(v):
    """Posting date from epoch ms/s, an ISO string, or Workday's 'Posted 5 Days Ago'. None if unknown."""
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):
            return dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, dt.timezone.utc).date()
        t = str(v).strip()
        m = re.search(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", t, re.I)
        if m:
            days = 0 if m.group(1).lower() == "today" else 1 if m.group(1).lower() == "yesterday" else int(m.group(2))
            if "+" in t:
                days += 1
            return TODAY - dt.timedelta(days=days)
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", t)
        if m:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.search(r"/Date\((\d+)", t)
        if m:
            return dt.datetime.fromtimestamp(int(m.group(1)) / 1000, dt.timezone.utc).date()
    except (ValueError, OverflowError, OSError):
        return None
    return None


def fresh(posted):
    """False only when the posting date is known and older than MAX_AGE_DAYS."""
    d = dt.date.fromisoformat(posted) if posted else None
    return d is None or (TODAY - d).days <= MAX_AGE_DAYS


def job(title, url, jid=None, loc="", posted=None):
    title = " ".join(html.unescape(title or "").split())
    d = to_date(posted)
    return {"title": title, "url": url, "id": str(jid or url), "location": loc or "",
            "posted": d.isoformat() if d else None}


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
                jobs.append(job(p.get("title"), f"https://{host}/en-US/{site}{path}", path, p.get("locationsText"),
                                p.get("postedOn")))
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
                                r.get("Id"), r.get("PrimaryLocation"), r.get("PostedDate")))
    return jobs


def a_greenhouse(board):
    data = get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs").json()
    return [job(j["title"], j.get("absolute_url"), j.get("id"), (j.get("location") or {}).get("name"),
                j.get("first_published")) for j in data.get("jobs", [])]


def a_lever(co):
    data = get(f"https://api.lever.co/v0/postings/{co}?mode=json").json()
    return [job(j.get("text"), j.get("hostedUrl"), j.get("id"), (j.get("categories") or {}).get("location"),
                j.get("createdAt")) for j in data]


def a_ashby(org):
    data = get(f"https://api.ashbyhq.com/posting-api/job-board/{org}").json()
    return [job(j.get("title"), j.get("jobUrl"), j.get("id"), j.get("location"), j.get("publishedAt"))
            for j in data.get("jobs", [])]


def a_smartrecruiters(co):
    data = get(f"https://api.smartrecruiters.com/v1/companies/{co}/postings?limit=100").json()
    return [job(j.get("name"), f"https://jobs.smartrecruiters.com/{co}/{j.get('id')}", j.get("id"),
                ", ".join(filter(None, [(j.get("location") or {}).get("city"), (j.get("location") or {}).get("country")])),
                j.get("releasedDate")) for j in data.get("content", [])]


def a_workable(co):
    data = post(f"https://apply.workable.com/api/v3/accounts/{co}/jobs",
                json={"query": "", "location": [], "department": [], "worktype": [], "remote": []}).json()
    return [job(j.get("title"), f"https://apply.workable.com/{co}/j/{j.get('shortcode')}", j.get("shortcode"),
                ", ".join(filter(None, [(j.get("location") or {}).get("city"), (j.get("location") or {}).get("country")])),
                j.get("published")) for j in data.get("results", [])]


def a_bamboo(host):
    data = get(f"https://{host}/careers/list", headers={"Accept": "application/json"}).json()
    return [job(j.get("jobOpeningName"), f"https://{host}/careers/{j.get('id')}", j.get("id"))
            for j in data.get("result", [])]


def a_breezy(host):
    data = get(f"https://{host}/json").json()
    return [job(j.get("name"), j.get("url"), j.get("id"), (j.get("location") or {}).get("name"), j.get("published_date"))
            for j in data]


def a_ukg(key):
    host, tenant, board = key.split("|")
    base = f"https://{host}/{tenant}/JobBoard/{board}"
    body = {"opportunitySearch": {"Top": 100, "Skip": 0, "QueryString": "",
                                  "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}],
                                  "Filters": []}, "matchCriteria": {"PreferredJobs": [], "Educations": [],
                                                                    "LicenseAndCertifications": [], "Skills": [],
                                                                    "hasNoLicenses": False, "SkippedSkills": []}}
    data = post(f"{base}/JobBoardView/LoadSearchResults", json=body).json()
    return [job(o.get("Title"), f"{base}/OpportunityDetail?opportunityId={o.get('Id')}", o.get("Id"),
                " ".join(str(((l.get("Address") or {}).get("Country") or {}).get("Name") or "") for l in o.get("Locations") or []),
                o.get("PostedDate")) for o in data.get("opportunities", [])]


def a_adp(cid):
    url = ("https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
           f"?cid={cid}&lang=en_US&locale=en_US&$top=100")
    data = get(url, headers={"Accept": "application/json"}).json()
    out = []
    for r in data.get("jobRequisitions", []):
        jid = r.get("itemID")
        out.append(job(r.get("requisitionTitle"),
                       f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}&jobId={jid}",
                       jid, "", r.get("postDate")))
    return out


def a_paylocity(url):
    page = get(url).text
    m = re.search(r"window\.pageData\s*=\s*", page)
    if not m:
        raise RuntimeError("Paylocity page data not found")
    data, _ = json.JSONDecoder().raw_decode(page, m.end())
    return [job(j.get("JobTitle"), f"https://recruiting.paylocity.com/Recruiting/Jobs/Details/{j.get('JobId')}", j.get("JobId"),
                (j.get("LocationName") or ""), j.get("PublishedDate")) for j in data.get("Jobs", [])]


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
        out.append(job(j.get("title"), f"{base}/job/{jid}", j.get("jobSeqNo") or jid, j.get("location"),
                       j.get("postedDate")))
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
                if j["title"] and is_match(j["title"]) and location_ok(j) and fresh(j.get("posted")):
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


def site_link(firm):
    """Best page for checking a firm by hand: its careers homepage, else its first job board."""
    if firm.get("careers_site"):
        return firm["careers_site"]
    for src in firm.get("sources", []):
        return board_url(src["kind"], src["key"])
    return ""


def why_unreadable(sources):
    if not sources:
        return "no job site in the config"
    errs = " ".join(x["error"] for x in sources)
    if "403" in errs or "429" in errs:
        return "site blocks automated checks"
    if "404" in errs:
        return "job-site link is broken or moved"
    if "401" in errs or "422" in errs:
        return "job system refused the request"
    if "Timeout" in errs:
        return "site timed out"
    if "SSLError" in errs or "ConnectionError" in errs:
        return "site couldn't be reached securely"
    return "no postings found (the page may load jobs in a way the watcher can't read)"


# ---------------------------------------------------------------- config
def load_firms():
    """config/firms.yaml -> firms with (kind, key) sources worked out from each address."""
    firms = []
    for e in yaml.safe_load(open(CONFIG)) or []:
        if e.get("watch") is False:
            continue
        homepage = (e.get("homepage") or "").strip()
        sources = []
        for url in e.get("career_sites") or []:
            c = classify(str(url).strip())
            if c and {"kind": c[0], "key": c[1]} not in sources:
                sources.append({"kind": c[0], "key": c[1]})
        if not sources and homepage and classify(homepage):
            c = classify(homepage)
            sources.append({"kind": c[0], "key": c[1]})
        firms.append({"id": str(e["id"]), "name": str(e.get("name") or e["id"]), "careers_site": homepage,
                      "categories": e.get("categories") or [], "sources": sources})
    return firms


# ---------------------------------------------------------------- main
def main():
    report_mode = "--report" in sys.argv
    flag = os.path.join(DATA, "quiet_next_run")
    quiet = "--quiet" in sys.argv or os.path.exists(flag)
    firms = load_firms()
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
                seen[m["key"]] = {"firm": r["name"], "title": m["title"], "url": m["url"], "first_seen": now,
                                  "posted": m.get("posted")}
                new.append((r["name"], m))

    def age_label(m):
        if m.get("posted"):
            return "posted " + dt.date.fromisoformat(m["posted"]).strftime("%b %-d")
        return "first seen " + dt.date.fromisoformat(seen[m["key"]]["first_seen"][:10]).strftime("%b %-d")

    def still_fresh(m):
        if m.get("posted"):
            return fresh(m["posted"])
        return fresh(seen[m["key"]]["first_seen"][:10])

    current = {}
    for r in results:
        keep = [m for m in r["matches"] if still_fresh(m)]
        if keep:
            current[r["name"]] = sorted(keep, key=lambda m: m.get("posted") or seen[m["key"]]["first_seen"][:10], reverse=True)
    total = sum(len(v) for v in current.values())
    with open(os.path.join(DATA, "open_now.md"), "w") as fh:
        fh.write(f"# Open right now: {total} postings at {len(current)} firms\n\n"
                 "Titles with 2027 or a full-time analyst sign, in the US, mainland China or Hong Kong "
                 "(postings with no location listed are included), posted in the last 14 days. Postings with no posting date "
                 "count from the day the watcher first saw them. Internships and senior roles are left out. "
                 "Refreshed on every check.\n\n")
        for f in sorted(current, key=str.lower):
            fh.write(f"## {f}\n" + "".join(f"- [{m['title']}]({m['url']}) · {age_label(m)}\n" for m in current[f]) + "\n")

    topic = os.environ.get("NTFY_TOPIC", "")
    if quiet:
        print(f"Quiet run: recorded {len(new)} new matches without alerting.")
        if os.path.exists(flag):
            os.remove(flag)
    elif first_run:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        groups = {}
        for f, m in new:
            groups.setdefault(f, []).append(m)
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
    by_id = {f["id"]: f for f in firms}
    manual = sorted((h for h in health if not h["readable"]), key=lambda h: h["firm"].lower())
    with open(os.path.join(DATA, "open_now.md"), "a") as fh:
        fh.write(f"## Check these yourself: {len(manual)} firms the watcher can't read\n\n"
                 "Their job sites didn't return postings on this check, so new roles there won't trigger an alert. "
                 "Visit them directly.\n\n")
        for h in manual:
            link = site_link(by_id.get(h["id"], {}))
            shown = f"[careers site]({link})" if link else "no link in the config"
            fh.write(f"- **{h['firm']}** · {shown} · {why_unreadable(h['sources'])}\n")
    good = sum(1 for h in health if h["readable"])
    with open(os.path.join(DATA, "status.md"), "w") as fh:
        fh.write(f"Last run {now}. {good}/{len(health)} firms readable. "
                 f"{len(new)} new matches this run. Took {time.time() - t0:.0f}s.\n")
    print(open(os.path.join(DATA, "status.md")).read())


if __name__ == "__main__":
    main()
