import json
import operator
import os
import re
import urllib
from collections import defaultdict
from datetime import datetime
from functools import reduce
from operator import itemgetter
from pathlib import Path
from types import SimpleNamespace

from coleo import Option, tooled
from ovld import ovld
from sqlalchemy import select

from ...config import config
from ...db import schema as sch
from ...model import (
    Author,
    DatePrecision,
    Institution,
    InstitutionCategory,
    Link,
    Paper,
    PaperAuthor,
    Release,
    Topic,
    UniqueAuthor,
    UniqueInstitution,
    Venue,
    VenueType,
)
from ...tools import extract_date, keyword_decorator
from ..acquire import readpage
from .base import BaseScraper
from .pdftools import find_fulltext_affiliations, pdf_to_text

refiners = defaultdict(list)


@keyword_decorator
def refiner(fn, *, type, priority):
    refiners[type].append((priority, fn))
    return fn


def _paper_from_jats(soup, links):
    date1 = soup.select_one('pub-date[date-type="pub"] string-date')
    if date1:
        date = extract_date(date1.text)
    else:
        date2 = soup.select_one('pub-date[pub-type="epub"]') or soup.select_one(
            'pub-date[date-type="pub"]'
        )
        if date2:
            y = date2.find("year")
            m = date2.find("month")
            d = date2.find("day")
            date = {
                "date": datetime(
                    int(y.text),
                    int((m and m.text) or 1),
                    int((d and d.text) or 1),
                ),
                "date_precision": (
                    DatePrecision.day
                    if d
                    else DatePrecision.month
                    if m
                    else DatePrecision.year
                ),
            }

    def find_affiliation(aff):
        node = soup.select_one(f'aff#{aff.attrs["rid"]} institution')
        if not node:
            node = soup.select_one(f'aff#{aff.attrs["rid"]}')
        name = node.text
        name = re.sub(pattern="^[0-9]+", string=name, repl="")
        return Institution(
            name=name,
            category=InstitutionCategory.unknown,
            aliases=[],
        )

    return Paper(
        title=soup.find("article-title").text,
        authors=[
            PaperAuthor(
                author=Author(
                    name=" ".join(
                        [
                            x.text
                            for x in [
                                *author.find_all("given-names"),
                                author.find("surname"),
                            ]
                        ]
                    ),
                    aliases=[],
                    links=[],
                    roles=[],
                    quality=(0,),
                ),
                affiliations=[
                    find_affiliation(aff)
                    for aff in author.select('xref[ref-type="aff"]')
                ],
            )
            for author in soup.select('contrib[contrib-type="author"]')
            if author.find("surname")
        ],
        abstract="",
        links=links,
        releases=[
            Release(
                venue=Venue(
                    name=(
                        jname := soup.select_one(
                            "journal-meta journal-title"
                        ).text
                    ),
                    series=jname,
                    type=VenueType.journal,
                    **date,
                    publisher=soup.select_one(
                        "journal-meta publisher-name"
                    ).text,
                    links=[],
                    aliases=[],
                ),
                status="published",
                pages=None,
            )
        ],
        topics=[Topic(name=kwd.text) for kwd in soup.select("kwd-group kwd")],
        quality=(0,),
    )


@refiner(type="doi", priority=190)
def refine_doi_with_ieeexplore(db, paper, link):
    doi = link.link
    if not doi.startswith("10.1109/"):
        return None

    apikey = os.environ.get("XPLORE_API_KEY", None)
    if not apikey:
        return None

    data = readpage(
        f"http://ieeexploreapi.ieee.org/api/v1/search/articles?apikey={apikey}&format=json&max_records=25&start_record=1&sort_order=asc&sort_field=article_number&doi={doi}",
        format="json",
    )
    (data,) = data["articles"]

    topics = []
    for _, terms in data["index_terms"].items():
        topics.extend(Topic(name=term) for term in terms["terms"])

    return Paper(
        title=data["title"],
        authors=[
            PaperAuthor(
                author=Author(
                    name=author["full_name"],
                    roles=[],
                    aliases=[],
                    links=[Link(type="xplore", link=author["id"])],
                ),
                affiliations=[
                    Institution(
                        name=author["affiliation"],
                        category=InstitutionCategory.unknown,
                        aliases=[],
                    )
                ],
            )
            for author in sorted(
                data["authors"]["authors"], key=itemgetter("author_order")
            )
        ],
        abstract=data["abstract"],
        links=[Link(type="doi", link=doi)],
        topics=topics,
        releases=[
            Release(
                venue=Venue(
                    name=(jname := data["publication_title"]),
                    series=jname,
                    type=VenueType.journal,
                    **extract_date(data["publication_date"]),
                    publisher=data["publisher"],
                    links=[],
                    aliases=[],
                    volume=data.get("volume", None),
                ),
                status="published",
                pages=f"{data['start_page']}-{data['end_page']}",
            )
        ],
        quality=(0,),
    )


@refiner(type="doi", priority=100)
def refine_doi_with_crossref(db, paper, link):
    doi = link.link
    if "arXiv" in doi:
        return None

    data = readpage(f"https://api.crossref.org/v1/works/{doi}", format="json")

    if data["status"] != "ok":
        return None
    data = SimpleNamespace(**data["message"])

    if not any(auth.get("affiliation", None) for auth in data.author):
        return None

    return Paper(
        title=data.title[0],
        authors=[
            PaperAuthor(
                author=Author(
                    name=f"{author['given']} {author['family']}",
                    roles=[],
                    aliases=[],
                    links=[],
                ),
                affiliations=[
                    Institution(
                        name=aff["name"],
                        category=InstitutionCategory.unknown,
                        aliases=[],
                    )
                    for aff in author["affiliation"]
                ],
            )
            for author in data.author
        ],
        abstract="",
        links=[Link(type="doi", link=doi)],
        topics=[],
        releases=[],
        quality=(0,),
    )


@refiner(type="doi", priority=90)
def refine_doi_with_biorxiv(db, paper, link):
    doi = link.link
    if not doi.startswith("10.1101/"):
        return None

    data = readpage(
        f"https://api.biorxiv.org/details/biorxiv/{doi}", format="json"
    )

    if {"status": "ok"} not in data["messages"] or not data["collection"]:
        return None

    jats = data["collection"][0]["jatsxml"]

    return _paper_from_jats(
        readpage(jats, format="xml"),
        links=[Link(type="doi", link=doi)],
    )


@ovld
def _sd_find(d: dict, tag, indices):
    if d.get("#name", None) == tag:
        for idx in indices:
            d = d[idx]
        return [d]
    return _sd_find(list(d.values()), tag, indices)


@ovld
def _sd_find(li: list, tag, indices):
    results = []
    for v in li:
        results += _sd_find(v, tag, indices)
    return results


@ovld
def _sd_find(x, tag, indices):
    return []


def _sd_find_one(x, tag, indices=[]):
    return _sd_find(x, tag, indices)[0]


@refiner(type="doi", priority=90)
def refine_doi_with_sciencedirect(db, paper, link):
    doi = link.link
    if not doi.startswith("10.1016/"):
        return None

    info = readpage(f"https://doi.org/api/handles/{doi}", format="json")
    url1 = [v for v in info["values"] if v["type"] == "URL"][0]["data"]["value"]
    redirector = readpage(url1, format="html")
    url2 = redirector.select_one("#redirectURL").attrs["value"]
    url2 = urllib.parse.unquote(url2)
    if "sciencedirect" not in url2:
        return

    soup = readpage(url2, format="html", headers={"User-Agent": "Mozilla/5.0"})
    data = json.loads(soup.select_one('script[type="application/json"]').text)

    authors_raw = _sd_find(data["authors"], "author", [])
    aff_raw = {
        aff["$"].get("id", "n/a"): aff
        for aff in _sd_find(data["authors"], "affiliation", [])
    }

    authors = []
    for author in authors_raw:
        given = _sd_find_one(author, "given-name", "_")
        surname = _sd_find_one(author, "surname", "_")
        name = f"{given} {surname}"
        affids = [
            x
            for x in _sd_find(author, "cross-ref", ("$", "refid"))
            if x.startswith("af")
        ]
        affs = [
            _sd_find(aff_raw[affid], "organization", "_") for affid in affids
        ]
        affs = reduce(operator.add, affs, [])
        authors.append(
            PaperAuthor(
                author=Author(
                    name=name,
                    roles=[],
                    aliases=[],
                    links=[],
                    quality=(0,),
                ),
                affiliations=[
                    Institution(
                        name=aff,
                        category=InstitutionCategory.unknown,
                        aliases=[],
                    )
                    for aff in affs
                ],
            )
        )

    return Paper(
        title=_sd_find_one(data["article"], "title", "_"),
        abstract="",
        authors=authors,
        links=[Link(type=link.type, link=link.link)],
        releases=[],
        topics=[],
        quality=(0,),
    )


@refiner(type="pmc", priority=110)
def refine_with_pubmedcentral(db, paper, link):
    pmc_id = link.link
    soup = readpage(
        f"https://www.ncbi.nlm.nih.gov/pmc/oai/oai.cgi?verb=GetRecord&identifier=oai:pubmedcentral.nih.gov:{pmc_id}&metadataPrefix=pmc_fm",
        format="xml",
    )
    return _paper_from_jats(
        soup,
        links=[Link(type="pmc", link=pmc_id)],
    )


def _pdf_refiner(db, paper, link, pth, url):

    fulltext = pdf_to_text(cache_base=pth, url=url)

    institutions = {}
    for (inst,) in db.session.execute(select(sch.Institution)):
        institutions.update({alias: inst for alias in inst.aliases})

    author_affiliations = find_fulltext_affiliations(
        paper, fulltext, institutions
    )

    if not author_affiliations:
        return None

    return Paper(
        title=paper.title,
        abstract="",
        authors=[
            PaperAuthor(
                author=UniqueAuthor(
                    author_id=author.author_id,
                    name=author.name,
                    roles=[],
                    aliases=[],
                    links=[],
                    quality=author.quality,
                ),
                affiliations=[
                    aff
                    if isinstance(aff, Institution)
                    else UniqueInstitution(
                        institution_id=aff.institution_id,
                        name=aff.name,
                        category=aff.category,
                        aliases=[],
                    )
                    for aff in affiliations
                ],
            )
            for author, affiliations in author_affiliations.items()
        ],
        links=[Link(type=link.type, link=link.link)],
        releases=[],
        topics=[],
        quality=(0,),
    )


@refiner(type="arxiv", priority=6)
def refine(db, paper, link):
    arxiv_id = link.link

    return _pdf_refiner(
        db=db,
        paper=paper,
        link=link,
        pth=Path(f"{config.get().paths.cache}/arxiv/{arxiv_id}.pdf"),
        url=f"https://arxiv.org/pdf/{arxiv_id}.pdf",
    )


@refiner(type="openreview", priority=5)
def refine(db, paper, link):
    openreview_id = link.link

    return _pdf_refiner(
        db=db,
        paper=paper,
        link=link,
        pth=Path(f"{config.get().paths.cache}/openreview/{openreview_id}.pdf"),
        url=f"https://openreview.net/pdf?id={openreview_id}",
    )


class Refiner(BaseScraper):
    @tooled
    def query(
        self,
        # [positional]
        # Link to query
        link: Option = None,
    ):
        type, link = link.split(":", 1)
        pq = (
            select(sch.Paper)
            .join(sch.PaperLink)
            .filter(sch.PaperLink.type == type, sch.PaperLink.link == link)
        )
        [[paper], *_] = self.db.session.execute(pq)

        _refiners = []
        for link in paper.links:
            _refiners.extend(
                [(p, link, r) for (p, r) in refiners.get(link.type, [])]
            )

        _refiners.sort(reverse=True, key=lambda data: data[0])

        for _, link, refiner in _refiners:
            try:
                if result := refiner(self.db, paper, link):
                    yield result
                    return
            except Exception as exc:
                print(exc)
                continue

    @tooled
    def acquire(self):
        pass

    @tooled
    def prepare(self):
        pass


__scrapers__ = {"refine": Refiner}
