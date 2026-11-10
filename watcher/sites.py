"""Job-site addresses: work out which job system a link belongs to, and turn
any link into the plain board address that the config stores.

classify(url)        -> (kind, key) the watcher's readers need, or None
board_url(kind, key) -> the clean board address, e.g.
                        https://pjtpartners.wd1.myworkdayjobs.com/Careers
clean_page(url)      -> any other page with tracking tags and filters removed
"""
import re
from urllib.parse import parse_qsl, urlencode, urlparse, parse_qs

# Board names that mean "students only" (campus, early careers, graduates, interns).
STUDENT = re.compile(r"campus|students?|early|graduates?|universit|interns?(hips?)?(?![a-z])|college|(?<![a-z])grads?(?![a-z])",
                     re.I)


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
    if h == "higher.gs.com":
        return ("goldman", "higher.gs.com")
    if h.endswith("janestreet.com"):
        return ("janestreet", "main.json")
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
        return ("paylocity", clean_page(url))
    if (h.endswith("ultipro.com") or h.endswith("ukg.net")) and len(seg) >= 3 and seg[1].lower() == "jobboard":
        return ("ukg", f"{h}|{seg[0]}|{seg[2]}")
    if "workforcenow" in h and "adp.com" in h:
        cid = (q.get("cid") or [None])[0]
        return ("adp", cid) if cid else None
    if h.endswith("linkedin.com") or h == "bit.ly" or h.endswith("the-trackr.com"):
        return None
    return ("site", clean_page(url))


# Query parameters that pick which job board a page shows, so they stay.
# Everything else (utm_ tracking, source=Trackr, search filters) is dropped.
KEEP_PARAMS = {"partnerid", "siteid", "pagetype", "company", "microsite", "tag", "cid"}


def clean_page(url):
    """Drop tracking tags, search filters and the #fragment; keep board-picking parameters."""
    base, _, query = url.split("#")[0].partition("?")
    keep = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k.lower() in KEEP_PARAMS]
    return base + ("?" + urlencode(keep) if keep else "")


_JOB_SEG = {"job", "job-details", "jobdetail", "jobdetails", "job.html", "details", "vacancy", "position",
            "posting", "job-posting", "requisition", "opportunity"}
# A trailing path piece that names one job: has a year, or ends in a job number.
_JOB_SLUG = re.compile(r"(?<!\d)20\d\d(?!\d)|-\d{4,}$|^[0-9a-f\-]{16,}$|^\d{4,}$", re.I)


def listing_page(url):
    """Turn a single-job page on a plain careers site into that site's job list.
    https://careers.truist.com/us/en/job/R0121314/2028-... -> https://careers.truist.com/us/en/
    https://www.deshaw.com/careers/quantitative-analyst-2636 -> https://www.deshaw.com/careers/"""
    cleaned = clean_page(url)
    u = urlparse(cleaned)
    seg = orig = [s for s in u.path.split("/") if s]
    for i, s in enumerate(seg):
        low = s.lower()
        if low in _JOB_SEG:
            seg = seg[:i]
            break
        if low == "jobs" and i + 1 < len(seg):
            seg = seg[: i + 1] if re.fullmatch(r"\d+|[0-9a-f\-]{16,}", seg[i + 1], re.I) else seg[:i]
            break
    while seg and _JOB_SLUG.search(seg[-1]) and seg[-1].lower() not in ("jobs", "careers"):
        seg = seg[:-1]
    if seg == orig:
        return cleaned
    return f"{u.scheme}://{u.netloc}/" + "".join(f"{s}/" for s in seg) + (f"?{u.query}" if u.query else "")


def board_url(kind, key):
    """The plain address of a job board. classify(board_url(k, key)) == (k, key)."""
    if kind == "workday":
        host, tenant, site = key.split("|")
        if host.endswith("myworkdaysite.com"):
            return f"https://{host}/recruiting/{tenant}/{site}"
        return f"https://{host}/{site}"
    if kind == "oracle":
        host, site = key.split("|")
        return f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}"
    if kind == "greenhouse":
        return f"https://job-boards.greenhouse.io/{key}"
    if kind == "lever":
        return f"https://jobs.lever.co/{key}"
    if kind == "ashby":
        return f"https://jobs.ashbyhq.com/{key}"
    if kind == "smartrecruiters":
        return f"https://jobs.smartrecruiters.com/{key}"
    if kind == "workable":
        return f"https://apply.workable.com/{key}/"
    if kind == "icims":
        return f"https://{key}/jobs"
    if kind == "avature":
        host, base = key.split("|")
        return f"https://{host}{base}/SearchJobs"
    if kind == "bamboo":
        return f"https://{key}/careers"
    if kind == "breezy":
        return f"https://{key}/"
    if kind == "ukg":
        host, tenant, board = key.split("|")
        return f"https://{host}/{tenant}/JobBoard/{board}"
    if kind == "adp":
        return f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={key}"
    if kind == "goldman":
        return "https://higher.gs.com/"
    if kind == "janestreet":
        return "https://www.janestreet.com/join-jane-street/open-roles/"
    return key  # oleeo, paylocity, site: the key is already the address


def board_name(kind, key):
    """The part of the key that names the board, used to spot student-only boards."""
    if kind == "workday":
        return key.split("|")[2]
    if kind == "oracle":
        return key.split("|")[1]
    if kind in ("greenhouse", "lever", "ashby"):
        return key
    return ""


def is_student_board(kind, key):
    return bool(STUDENT.search(board_name(kind, key)))
