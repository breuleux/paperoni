import json
import os
import sqlite3
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session
from tqdm import tqdm

from ..config import config
from ..sources.model import (
    Author,
    AuthorQuery,
    Institution,
    Paper,
    Release,
    Topic,
    Venue,
    from_dict,
)
from . import schema as sch


class Database:
    DATABASE_SCRIPT_FILE = os.path.join(
        os.path.dirname(__file__), "database.sql"
    )

    def __init__(self, filename):
        self.engine = create_engine(f"sqlite:///{filename}")
        connection = sqlite3.connect(filename)
        cursor = connection.cursor()
        with open(self.DATABASE_SCRIPT_FILE) as script_file:
            cursor.executescript(script_file.read())
            connection.commit()
        self.session = None
        self.cache = {}

    def __enter__(self):
        self.session = Session(self.engine).__enter__()
        return self

    def __exit__(self, *args):
        self.session.commit()
        self.session.__exit__(*args)
        self.session = None

    def acquire(self, x):
        if not (hid := x.hashid()) or hid not in self.cache:
            # If hid is None that will just always overwrite the value, which
            # is fine.
            self.cache[hid] = self._acquire(x)
        return self.cache[hid]

    def _acquire(self, x):
        match x:
            case Paper(
                title=title, abstract=abstract, citation_count=cc
            ) as paper:

                pp = sch.Paper(
                    paper_id=paper.hashid(),
                    title=title,
                    abstract=abstract,
                    citation_count=cc,
                )
                self.session.merge(pp)

                for i, author in enumerate(paper.authors):
                    aa = self.acquire(author)
                    pa = sch.PaperAuthor(
                        paper_id=pp.paper_id,
                        author_id=aa.author_id,
                        author_position=i,
                    )
                    self.session.merge(pa)

                    for affiliation in author.affiliations:
                        inst = self.acquire(affiliation)
                        stmt = (
                            insert(sch.t_paper_author_institution)
                            .values(
                                paper_id=pp.paper_id,
                                author_id=aa.author_id,
                                institution_id=inst.institution_id,
                            )
                            .on_conflict_do_nothing()
                        )
                        self.session.execute(stmt)

                for release in paper.releases:
                    rr = self.acquire(release)
                    stmt = (
                        insert(sch.t_paper_release)
                        .values(paper_id=pp.paper_id, release_id=rr.release_id)
                        .on_conflict_do_nothing()
                    )
                    self.session.execute(stmt)

                for topic in paper.topics:
                    tt = self.acquire(topic)
                    stmt = (
                        insert(sch.t_paper_topic)
                        .values(paper_id=pp.paper_id, topic_id=tt.topic_id)
                        .on_conflict_do_nothing()
                    )
                    self.session.execute(stmt)

                for link in paper.links:
                    lnk = sch.PaperLink(
                        paper_id=pp.paper_id,
                        type=link.type,
                        link=link.link,
                    )
                    self.session.merge(lnk)

                for scraper in paper.scrapers:
                    psps = sch.PaperScraper(
                        paper_id=pp.paper_id, scraper=scraper
                    )
                    self.session.merge(psps)

                return pp

            case Author(name=name) as author:
                aa = sch.Author(author_id=author.hashid(), name=name)
                self.session.merge(aa)
                self.acquire(AuthorQuery(author_id=aa.author_id, author=author))
                return aa

            case Institution(name=name, category=category) as institution:
                aa = sch.Institution(
                    institution_id=institution.hashid(),
                    name=name,
                    category=category,
                )
                self.session.merge(aa)
                return aa

            case Release(
                date=date,
                date_precision=date_precision,
                volume=volume,
                publisher=publisher,
            ) as release:
                vv = self.acquire(release.venue)
                rr = sch.Release(
                    release_id=release.hashid(),
                    date=date,
                    date_precision=date_precision,
                    volume=volume,
                    publisher=publisher,
                    venue_id=vv.venue_id,
                )
                self.session.merge(rr)
                return rr

            case Topic(name=name) as topic:
                tt = sch.Topic(topic_id=topic.hashid(), topic=name)
                self.session.merge(tt)
                return tt

            case Venue(type=vtype, name=name) as venue:
                vv = sch.Venue(venue_id=venue.hashid(), type=vtype, name=name)
                self.session.merge(vv)

                for link in venue.links:
                    lnk = sch.VenueLink(
                        venue_id=vv.venue_id,
                        type=link.type,
                        link=link.link,
                    )
                    self.session.merge(lnk)

                return vv

            case AuthorQuery(author_id=author_id, author=author):
                for link in author.links:
                    lnk = sch.AuthorLink(
                        author_id=author_id,
                        type=link.type,
                        link=link.link,
                    )
                    self.session.merge(lnk)

                for alias in author.aliases:
                    aal = sch.AuthorAlias(
                        author_id=author_id,
                        alias=alias,
                    )
                    self.session.merge(aal)

                for role in author.roles:
                    rr = sch.AuthorInstitution(
                        author_id=author_id,
                        institution_id=self.acquire(
                            role.institution
                        ).institution_id,
                        role=role.role,
                        start_date=role.start_date,
                        end_date=role.end_date,
                    )
                    self.session.merge(rr)

            case _:
                raise TypeError(f"Cannot acquire: {type(x).__name__}")

    def import_all(self, xs: list[BaseModel], history_file=None):
        if not xs:
            return
        history_file = history_file or config.history_file
        xs = list(xs)
        with self:
            for x in tqdm(xs):
                self.acquire(x)
        with open(history_file, "a") as f:
            data = [x.tagged_json() + "\n" for x in xs]
            f.writelines(data)

    def _accumulate_history_files(self, x, before, after, results):
        match x:
            case str() as pth:
                return self._accumulate_history_files(
                    Path(pth), before, after, results
                )
            case Path() as pth:
                if pth.is_dir():
                    self._accumulate_history_files(
                        list(pth.iterdir()), before, after, results
                    )
                else:
                    results.append(pth)
            case [*paths]:
                paths = list(sorted(paths))
                if before:
                    paths = [x for x in paths if x.name[: len(before)] < before]
                if after:
                    paths = [x for x in paths if x.name[: len(after)] > after]
                for subpth in paths:
                    self._accumulate_history_files(
                        subpth, before, after, results
                    )
            case _:
                assert False

    def replay(self, history=None, before=None, after=None):
        history = history or config.history_root
        history_files = []
        self._accumulate_history_files(history, before, after, history_files)
        for history_file in history_files:
            print(f"Replaying {history_file}")
            with self:
                with open(history_file, "r") as f:
                    lines = f.readlines()
                    for l in tqdm(lines):
                        self.acquire(from_dict(json.loads(l)))
