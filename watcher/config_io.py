"""Read and write config/firms.yaml, keeping its header comments and layout."""
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config", "firms.yaml")


def load(path=CONFIG):
    return yaml.safe_load(open(path)) or []


def header(path=CONFIG):
    """The comment block at the top of the file."""
    lines = []
    for line in open(path):
        if line.startswith("#") or (lines and not line.strip() and not lines[-1].strip() == ""):
            lines.append(line)
            if not line.strip():
                break
        else:
            break
    return "".join(lines).rstrip("\n") + "\n\n"


def _scalar(v):  # block style keeps URLs unquoted
    return yaml.safe_dump({"x": v}, allow_unicode=True, width=1000, default_flow_style=False).strip()[3:]


def _flow(v):  # inside [ ]
    return yaml.safe_dump({"x": v}, allow_unicode=True, width=1000, default_flow_style=True).strip()[4:-1]


def save(entries, path=CONFIG, head=None):
    head = head if head is not None else header(path)
    out = [head]
    for e in entries:
        out.append(f"- id: {_scalar(e['id'])}\n  name: {_scalar(e['name'])}\n")
        out.append(f"  categories: [{', '.join(_flow(c) for c in e.get('categories') or [])}]\n")
        if e.get("watch") is False:
            out.append("  watch: false\n")
        if e.get("homepage"):
            out.append(f"  homepage: {_scalar(e['homepage'])}\n")
        sites = e.get("career_sites") or []
        if sites:
            out.append("  career_sites:\n" + "".join(f"    - {_scalar(s)}\n" for s in sites) + "\n")
        else:
            out.append("  career_sites: []\n\n")
    text = "".join(out)
    yaml.safe_load(text)  # fail loudly if it isn't valid YAML
    open(path, "w").write(text)
