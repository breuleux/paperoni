import shutil
from pathlib import Path

from pytest import fixture


def _ensure_db(cfg):
    # Path of the writable database
    dbpath = cfg.paths.database
    # Path of the read-only database that serves as a model
    dbfixed = dbpath.with_suffix(".db.fixed")
    # Path to a JSONL history file that can be replayed to create the db
    dbhistory = dbpath.with_suffix(".jsonl")

    dbfixed_t = dbfixed.stat().st_mtime if dbfixed.exists() else 0
    dbhistory_t = dbhistory.stat().st_mtime if dbhistory.exists() else 0

    # Either of these are required to exist
    assert dbfixed_t or dbhistory_t

    if dbhistory_t > dbfixed_t:
        # Remove any existing db
        dbpath.unlink(missing_ok=True)
        # This replays the history into dbpath
        cfg.database.replay(history=dbhistory)
        # Cache the result of the replay
        shutil.copy(dbpath, dbfixed)

    elif dbpath.exists() and not cfg.writable:
        return

    else:
        shutil.copy(dbfixed, dbpath)


def transient_config(name):
    from paperoni.config import load_config

    config_file = Path(__file__).parent / "data" / name
    with load_config(config_file) as cfg:
        _ensure_db(cfg)
        yield cfg


@fixture
def config_writable():
    yield from transient_config("config-writable.yaml")


@fixture
def config_readonly():
    yield from transient_config("config-readonly.yaml")


@fixture
def config_refine():
    yield from transient_config("config-refine.yaml")


@fixture
def config_empty():
    yield from transient_config("config-empty.yaml")


@fixture
def config_profs():
    yield from transient_config("config-profs.yaml")


@fixture
def config_yoshua():
    yield from transient_config("config-yoshua.yaml")
