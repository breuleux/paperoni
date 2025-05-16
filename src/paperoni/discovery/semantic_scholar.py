from dataclasses import dataclass
from datetime import datetime
from functools import partial

from ..acquire import HTTPSAcquirer
from ..model import (
    Author,
    DatePrecision,
    Link,
    Paper,
    PaperAuthor,
    Release,
    Topic,
    Venue,
    VenueType,
)
from .base import Discoverer, QueryError

external_ids_mapping = {
    "pubmedcentral": "pmc",
}


venue_type_mapping = {
    "JournalArticle": VenueType.journal,
    "Conference": VenueType.conference,
    "Book": VenueType.book,
    "Review": VenueType.review,
    "News": VenueType.news,
    "Study": VenueType.study,
    "MetaAnalysis": VenueType.meta_analysis,
    "Editorial": VenueType.editorial,
    "LettersAndComments": VenueType.letters_and_comments,
    "CaseReport": VenueType.case_report,
    "ClinicalTrial": VenueType.clinical_trial,
    "_": VenueType.unknown,
}


def _paper_long_fields(parent=None, extras=()):
    fields = (
        "paperId",
        "externalIds",
        "url",
        "title",
        "abstract",
        "venue",
        "publicationTypes",
        "publicationDate",
        "year",
        "journal",
        "referenceCount",
        "citationCount",
        "influentialCitationCount",
        "isOpenAccess",
        "openAccessPdf",
        "fieldsOfStudy",
        *extras,
    )
    return fields if parent is None else tuple(f"{parent}.{field}" for field in fields)


def _paper_short_fields(parent=None):
    fields = (
        "paperId",
        "url",
        "title",
        "venue",
        "year",
        "authors",  # {authorId, name}
    )
    return fields if parent is None else tuple(f"{parent}.{field}" for field in fields)


def _author_fields(parent=None):
    fields = (
        "authorId",
        "externalIds",
        "url",
        "name",
        "affiliations",
        "homepage",
        "paperCount",
        "citationCount",
    )
    return fields if parent is None else tuple(f"{parent}.{field}" for field in fields)


def _date_from_data(data):
    if pubd := data["publicationDate"]:
        return {
            "date": datetime.strptime(pubd, "%Y-%m-%d").date(),
            "date_precision": DatePrecision.day,
        }
    else:
        return DatePrecision.assimilate_date(data["year"])


# "authors" will have fields "authorId" and "name"
SEARCH_FIELDS = _paper_long_fields(extras=("authors",))
PAPER_FIELDS = (
    *_paper_long_fields(),
    *_author_fields(parent="authors"),
    # *_paper_short_fields(parent="citations"),
    *_paper_short_fields(parent="references"),
    "embedding",
)
PAPER_AUTHORS_FIELDS = _author_fields() + _paper_long_fields(
    parent="papers", extras=("authors",)
)
PAPER_CITATIONS_FIELDS = (
    "contexts",
    "intents",
    "isInfluential",
    *SEARCH_FIELDS,
)
PAPER_REFERENCES_FIELDS = PAPER_CITATIONS_FIELDS
AUTHOR_FIELDS = PAPER_AUTHORS_FIELDS
AUTHOR_PAPERS_FIELDS = (
    SEARCH_FIELDS
    # + _paper_short_fields(parent="citations")
    + _paper_short_fields(parent="references")
)


@dataclass
class SemanticScholar(Discoverer):
    api_key: str = None

    def __post_init__(self):
        self.conn = HTTPSAcquirer("api.semanticscholar.org", format="json")

    def _evaluate(self, path: str, **params):
        jdata = self.conn.get(
            f"/graph/v1/{path}",
            params=params,
            headers={"x-api-key": self.api_key},
        )
        if jdata is None or "error" in jdata:
            raise QueryError(jdata["error"] if jdata else "Received bad JSON")
        return jdata

    def _list(
        self,
        path: str,
        fields: tuple[str],
        block_size: int = 100,
        limit: int = 10000,
        **params,
    ):
        params = {
            "fields": ",".join(fields),
            "limit": min(block_size or 10000, limit),
            **params,
        }
        next_offset = 0
        while next_offset is not None and next_offset < limit:
            results = self._evaluate(path, offset=next_offset, **params)
            next_offset = results.get("next", None)
            if "data" not in results:
                print("Could not get data:", results["message"])
                return
            for entry in results["data"]:
                yield entry

    def _wrap_paper_author(self, data):
        return PaperAuthor(
            affiliations=[],
            author=(au := self._wrap_author(data)),
            display_name=au.name,
        )

    def _wrap_author(self, data):
        lnk = (aid := data["authorId"]) and Link(type="semantic_scholar", link=aid)
        return Author(
            name=data["name"],
            aliases=[data["name"]],
            links=[lnk] if lnk else [],
            # roles=[],
        )

    def _wrap_paper(self, data):
        links = [Link(type="semantic_scholar", link=data["paperId"])]
        for typ, ref in data["externalIds"].items():
            links.append(
                Link(type=external_ids_mapping.get(t := typ.lower(), t), link=str(ref))
            )
        if data["openAccessPdf"] and (url := data["openAccessPdf"]["url"]):
            url = url.replace("://arxiv.org", "://export.arxiv.org")
            links.append(
                Link(
                    type="pdf",
                    link=url,
                )
            )

        authors = list(map(self._wrap_paper_author, data["authors"]))

        if "ArXiv" in data["externalIds"]:
            release = Release(
                venue=Venue(
                    type=VenueType.preprint,
                    name="ArXiv",
                    series="ArXiv",
                    volume=None,
                    **_date_from_data(data),
                    aliases=[],
                    links=[],
                ),
                status="preprint",
                pages=None,
            )

        else:
            is_preprint = "rxiv" in data.get("venue", data.get("journal", "")).lower()
            release = Release(
                venue=Venue(
                    type=venue_type_mapping[
                        (pubt := data.get("publicationTypes", [])) and pubt[0] or "_"
                    ],
                    name=data["venue"],
                    series=data["venue"],
                    volume=(j := data["journal"]) and j.get("volume", None),
                    **_date_from_data(data),
                    aliases=[],
                    links=[],
                ),
                status="preprint" if is_preprint else "published",
                pages=None,
            )

        return Paper(
            links=links,
            authors=authors,
            title=data["title"],
            abstract=data["abstract"] or "",
            # citation_count=data["citationCount"],
            topics=[Topic(name=field) for field in (data["fieldsOfStudy"] or ())],
            releases=[release],
        )

    def search(self, query, fields=SEARCH_FIELDS, **params):
        papers = self._list(
            "paper/search",
            query=query,
            fields=fields,
            **params,
        )
        yield from map(self._wrap_paper, papers)

    def paper(self, paper_id, fields=PAPER_FIELDS):
        return self._wrap_paper(
            self._evaluate(f"paper/{paper_id}", fields=",".join(fields))
        )

    def paper_authors(self, paper_id, fields=PAPER_AUTHORS_FIELDS, **params):
        yield from self._list(f"paper/{paper_id}/authors", fields=fields, **params)

    def paper_citations(self, paper_id, fields=PAPER_CITATIONS_FIELDS, **params):
        yield from self._list(f"paper/{paper_id}/citations", fields=fields, **params)

    def paper_references(self, paper_id, fields=PAPER_REFERENCES_FIELDS, **params):
        yield from self._list(f"paper/{paper_id}/citations", fields=fields, **params)

    def author(self, name=None, author_id=None, fields=AUTHOR_FIELDS, **params):
        wrap_author = partial(self._wrap_author)
        if name:
            name = name.replace("-", " ")
            authors = self._list("author/search", query=name, fields=fields, **params)
            yield from map(wrap_author, authors)
        else:
            yield wrap_author(
                self._evaluate(f"author/{author_id}", fields=",".join(fields), **params)
            )

    def author_with_papers(self, name, fields=AUTHOR_FIELDS, **params):
        name = name.replace("-", " ")
        authors = self._list("author/search", query=name, fields=fields, **params)
        for author in authors:
            yield (
                self._wrap_author(author),
                [self._wrap_paper(p) for p in author["papers"]],
            )

    def author_papers(self, author_id, fields=AUTHOR_PAPERS_FIELDS, **params):
        papers = self._list(f"author/{author_id}/papers", fields=fields, **params)
        yield from map(self._wrap_paper, papers)

    def query(
        self,
        # Author of the article
        author: str = None,
        # Title of the article
        title: str = None,
        # Maximal number of results per query
        block_size: int = 100,
        # Maximal number of results to return
        limit: int = 10000,
    ):
        """Query semantic scholar"""

        if isinstance(author, list):
            author = " ".join(author)
        if isinstance(title, list):
            title = " ".join(title)

        if author and title:
            raise QueryError("Cannot query both author and title")

        if title:
            yield from self.search(title, block_size=block_size, limit=limit)

        elif author:
            for _, papers in self.author_with_papers(
                author, block_size=block_size, limit=limit
            ):
                yield from papers
