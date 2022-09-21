import functools
import re
import unicodedata
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher

_uuid_tags = ["transient", "canonical"]


class QueryError(Exception):
    pass


class MutuallyExclusiveError(RuntimeError):
    """Exception raised when mutually exclusive parameters are used in queries."""

    def __init__(self, *args):
        self.args = args

    def __str__(self):
        return "Mutually exclusive parameters: " + " vs ".join(
            self._param_to_str(arg) for arg in self.args
        )

    def _param_to_str(self, param):
        return param if isinstance(param, str) else f"({', '.join(param)})"


def asciiify(s):
    """Translate a string to pure ASCII, removing accents and the like.

    Non-ASCII characters that are not accented characters are removed.
    """
    norm = unicodedata.normalize("NFD", s)
    stripped = norm.encode("ASCII", "ignore")
    return stripped.decode("utf8")


def squash_text(txt):
    """Convert text to a sequence of lowercase characters and numbers.

    * Non-ASCII characters are converted to ASCII or dropped
    * Uppercase is converted to lowercase
    * All spaces and special characters are removed, only letters and numbers remain
    """
    txt = asciiify(txt).lower()
    return re.sub(pattern=r"[^a-z0-9]+", string=txt, repl="")


url_extractors = {
    r"https?://[a-z.]*arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]+).*": "arxiv",
    r"https?://[a-z.]*arxiv-vanity\.com/papers/([0-9]{4}\.[0-9]+).*": "arxiv",
    r"https?://(?:[^/]*)arxiv(?:[^/]*)\.cornell\.edu/abs/([0-9]{4}\.[0-9]+).*": "arxiv",
    r"https?://scirate\.com/arxiv/([0-9]{4}\.[0-9]+).*": "arxiv",
    r"https?://pubmed\.ncbi\.nlm\.nih\.gov/([^/]*)/": "pubmed",
    r"https?://www\.ncbi\.nlm\.nih\.gov/pubmed/([^/]*)": "pubmed",
    r"https?://www\.ncbi\.nlm\.nih\.gov/pmc/articles/([^/]*)": "pmc",
    r"https?://europepmc.org/article/PMC/([^/]*)": "pmc",
    r"https?://(?:dx\.)?doi\.org/(.*)": "doi",
    r"https?://(?:www\.)?openreview\.net/(?:pdf\?|forum\?)id=(.*)": "openreview",
    r"https?://dblp.uni-trier.de/db/([^/]+)/([^/]+)/[^/]+\.html#(.*)": "dblp",
}


def url_to_id(url):
    for pattern, key in url_extractors.items():
        if m := re.match(pattern, url):
            lnk = "/".join(m.groups())
            return (key, lnk)
    return None


def canonicalize_links(links):
    links = {
        url_to_id(url := link["link"]) or (link["type"], url) for link in links
    }
    return [{"type": typ, "link": lnk} for typ, lnk in links]


def similarity(s1, s2):
    s1 = re.sub(string=s1, pattern="[. -]", repl="")
    s2 = re.sub(string=s2, pattern="[. -]", repl="")
    return SequenceMatcher(a=s1, b=s2).ratio()


def extract_date(txt):
    from .model import DatePrecision

    if not isinstance(txt, str):
        return None

    months = [
        "Jan-uary",
        "Feb-ruary",
        "Mar-ch",
        "Apr-il",
        "May-",
        "Jun-e",
        "Jul-y",
        "Aug-ust",
        "Sep-tember",
        "Oct-ober",
        "Nov-ember",
        "Dec-ember",
    ]
    months = [m.split("-") for m in months]
    stems = [a.lower() for a, b in months]
    months = [(f"{a}(?:{b})?\\.?" if b else a) for a, b in months]
    month = "|".join(months)

    patterns = {
        rf"({month}) ([0-9]{{1,2}}) *- *(?:{month}) [0-9]{{1,2}}[, ]+([0-9]{{4}})": (
            "m",
            "d",
            "y",
        ),
        rf"({month}) ([0-9]{{1,2}}) *- *[0-9]{{1,2}}[, ]+([0-9]{{4}})": (
            "m",
            "d",
            "y",
        ),
        rf"({month}) ?([0-9]{{1,2}})[, ]+([0-9]{{4}})": ("m", "d", "y"),
        rf"([0-9]{{1,2}}) *- *[0-9]{{1,2}}[ ,]+({month})[, ]+([0-9]{{4}})": (
            "d",
            "m",
            "y",
        ),
        rf"([0-9]{{1,2}})[ ,]+({month})[, ]+([0-9]{{4}})": ("d", "m", "y"),
        rf"({month}) +([0-9]{{4}})": ("m", "y"),
        rf"([0-9]{{4}}) ({month}) ([0-9]{{1,2}})": ("y", "m", "d"),
        rf"([0-9]{{4}}) ({month})": ("y", "m"),
    }

    for pattern, parts in patterns.items():
        if m := re.search(pattern=pattern, string=txt, flags=re.IGNORECASE):
            results = {k: m.groups()[i] for i, k in enumerate(parts)}
            precision = DatePrecision.day
            if "d" not in results:
                results.setdefault("d", 1)
                precision = DatePrecision.month
            return {
                "date": datetime(
                    int(results["y"]),
                    stems.index(results["m"].lower()[:3]) + 1,
                    int(results["d"]),
                ),
                "date_precision": precision,
            }
    else:
        return None


def tag_uuid(uuid, status):
    bit = _uuid_tags.index(status)
    nums = list(uuid)
    if bit:
        nums[0] = nums[0] | 128
    else:
        nums[0] = nums[0] & 127
    return bytes(nums)


def get_uuid_tag(uuid):
    return _uuid_tags[(uuid[0] & 128) >> 7]


def is_canonical_uuid(uuid):
    # return get_uuid_tag(uuid) == "canonical"
    return bool(uuid[0] & 128)


class EquivalenceGroups:
    def __init__(self):
        self.representatives = {}
        self.names = {}
        self.classes = {}

    def equiv(self, a, b):
        ar = self.follow(a)
        br = self.follow(b)
        self.representatives[a] = ar
        self.representatives[b] = ar
        self.representatives[br] = ar

    def equiv_all(self, ids, cls=None, under=None):
        if not ids:
            return
        a, *rest = list(ids)
        for b in rest:
            self.equiv(a, b)
        for x in ids:
            self.names[x] = under
            self.classes[x] = cls

    def follow(self, a):
        if b := self.representatives.get(a, None):
            if a == b:
                return a
            self.representatives[a] = res = self.follow(b)
            return res
        else:
            return a

    def groups(self):
        for k in self.representatives:
            self.follow(k)
        results = defaultdict(set)
        for k, v in self.representatives.items():
            results[v].add(k)
        return results

    def __iter__(self):
        for main, ids in self.groups().items():
            assert len(ids) > 1
            print(f"Merging {len(ids)} IDs for {self.names[main]}")
            yield self.classes[main](ids=ids)


def keyword_decorator(deco):
    """Wrap a decorator to optionally takes keyword arguments."""

    @functools.wraps(deco)
    def new_deco(fn=None, **kwargs):
        if fn is None:

            @functools.wraps(deco)
            def newer_deco(fn):
                return deco(fn, **kwargs)

            return newer_deco
        else:
            return deco(fn, **kwargs)

    return new_deco
