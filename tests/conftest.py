"""Shared test fixtures: a throwaway PostgreSQL server for the Postgres store (and later the API).

The server is started once per test session from the installed PostgreSQL binaries (`initdb`,
`pg_ctl`; Homebrew on a Mac, the preinstalled packages on GitHub's Ubuntu runners), listening on a
unix socket in a temporary directory only (no TCP port, no credentials); each test gets a fresh
database. Without the binaries, tests that need it are skipped.
"""

import base64
import os
import secrets
import sys
import uuid
from collections.abc import Callable, Generator, Iterator
from typing import Any

import pg_throwaway
import pytest
import temp_policy
from arg_order import group_args_by_folder
from guard_hooks import (  # noqa: F401  (hooks and the fixture, registered by name; ND119)
    _open_handle_check,
    _write_check,
    pytest_runtest_logstart,
    pytest_runtest_teardown,
    pytest_sessionfinish,
    pytest_terminal_summary,
    pytest_testnodedown,
    worker_id,
)
from guard_hooks import (
    pytest_configure as _guard_configure,
)
from net import (
    forbid_fixed_port_binds,  # noqa: F401  (autouse guard, registered by name; H-1)
    loopback_only,  # noqa: F401  (opt-in: no network but 127.0.0.1 and ::1; X7)
)
from serial_marks import (
    _serial_lock,  # noqa: F401  (autouse fixture, registered by name; the `serial` mark)
    add_group_to_node_ids,
    mark_serial_items,
)

from nuramesh.stores.base import Clock, Store, utc_now

# Tests import the packaged templates' team.py (and run their workers); no test may leave a
# `__pycache__` in `src/`, where the packaged-template file-list tests would see it.
sys.dont_write_bytecode = True


def _use_memory_backed_temp() -> None:
    """ND339: every SQLite commit is an fsync (`synchronous=FULL`, a store rule). On a disk that is
    ~6 ms per commit, on tmpfs ~0.01 ms, and the suite makes tens of thousands of them. Where
    Linux offers `/dev/shm`, the tests' temp directories go there (pytest's `tmp_path` and every
    `tempfile` call). Off on a Mac (no such directory), with `NURAMESH_TEST_TMPFS=0` and when
    `TMPDIR` is set (the guard and run-small give a per-run directory); see `temp_policy`. The
    session removes its own base temp dir at the end (`_remove_own_basetemp`). No test relies on
    the disk: durability is a pragma the store sets, and the tests assert the pragma."""
    import tempfile

    root = temp_policy.memory_temp_root(os.environ)
    if root:
        tempfile.tempdir = root


_use_memory_backed_temp()


def pytest_configure(config: pytest.Config) -> None:
    """Guard hook, then a short base temp dir under TMPDIR (`temp_policy.short_basetemp`).

    The caller's own --basetemp and xdist workers (they get theirs from the controller) are left
    alone."""
    _guard_configure(config)
    if hasattr(config, "workerinput"):
        return  # workers get their base dir and the ownership marker from the controller
    short, marker = temp_policy.claim_basetemp(
        os.environ, config.option.basetemp is not None, os.getpid()
    )
    if short is not None:
        config.option.basetemp = short
    config._nuramesh_own_basetemp = marker  # type: ignore[attr-defined]


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: Any) -> None:
    """xdist controller: hand each worker the ownership marker (`claim_basetemp`)."""
    node.workerinput[temp_policy.OWN_MARKER] = getattr(node.config, "_nuramesh_own_basetemp", "")


@pytest.fixture(scope="session", autouse=True)
def _remove_own_basetemp(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[None]:
    """Delete this session's (or xdist worker's) own pytest base temp dir at the end, so runs do
    not pile `pytest-of-<user>` trees up in /dev/shm. Keep it with NURAMESH_KEEP_BASETEMP=1."""
    yield
    config = request.config
    marker: str = getattr(config, "workerinput", {}).get(temp_policy.OWN_MARKER) or ""
    if not marker:
        marker = getattr(config, "_nuramesh_own_basetemp", "")
    temp_policy.remove_own_basetemp(
        tmp_path_factory.getbasetemp(), {**os.environ, temp_policy.OWN_MARKER: marker}
    )


@pytest.fixture
def closing_stores(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Close every SqliteStore the test opens, directly or in a helper, when the test ends
    (ND119 open-handle check, ND339). Opt in with `pytestmark = pytest.mark.usefixtures(...)`;
    it is torn down after the test's own fixtures, so no app or client still uses a store."""
    from nuramesh.stores import SqliteStore

    opened: list[SqliteStore] = []
    original = SqliteStore.__init__

    def tracking(self: SqliteStore, *args: Any, **kw: Any) -> None:
        original(self, *args, **kw)
        opened.append(self)

    monkeypatch.setattr(SqliteStore, "__init__", tracking)
    yield
    for store in opened:
        store.close()


@pytest.fixture(autouse=True)
def _key_dir(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Organization keys of a test go to a temporary directory, never to `~/.nuramesh/keys`
    (PH-KEY R5: every run and eval builds a file key ring)."""
    monkeypatch.setenv("NURAMESH_KEY_DIR", str(tmp_path_factory.mktemp("keys")))


def _template_dir() -> str | None:
    """Template clusters on unless NURAMESH_PG_TEMPLATE=off (A3)."""
    if os.environ.get("NURAMESH_PG_TEMPLATE") == "off":
        return None
    return pg_throwaway.template_base()


@pytest.fixture(scope="session")
def postgres_server(request: pytest.FixtureRequest) -> Iterator[str]:
    """A running throwaway server; yields the socket directory. Cleans up after itself."""
    pytest.importorskip("psycopg")
    initdb, pg_ctl = pg_throwaway.find_binary("initdb"), pg_throwaway.find_binary("pg_ctl")
    if not initdb or not pg_ctl:
        message = pg_throwaway.mismatch_message("initdb" if not initdb else "pg_ctl")
        if os.environ.get("NURAMESH_REQUIRE_POSTGRES"):  # CI sets this: never skip silently there
            pytest.fail(message)
        pytest.skip(message)
    root, sockets = pg_throwaway.start(
        initdb,
        pg_ctl,
        worker=worker_id(request.config),
        template_dir=_template_dir(),
    )
    cleanup = pg_throwaway.Cleanup(pg_ctl, root)
    cleanup.install()
    try:
        _create_definer_role(sockets)
        yield sockets
    finally:
        cleanup.run()
        cleanup.uninstall()


def _create_definer_role(sockets: str) -> None:
    """The cluster-wide role migration 28 checks (compose: deploy/compose/initdb/10-roles.sh).

    The tests migrate as the `postgres` superuser, which can SET ROLE to it without a grant.
    """
    pg_throwaway.ensure_definer_role(sockets)


@pytest.fixture
def make_postgres_dsn(postgres_server: str) -> Iterator[Callable[[], str]]:
    """Creates a new empty database on the throwaway server and returns its connection string.

    Every database made is dropped when the test ends (connections still open are cut).
    """
    import psycopg

    admin_dsn = f"host={postgres_server} user=postgres dbname=postgres"
    made: list[str] = []

    def make() -> str:
        name = "t_" + uuid.uuid4().hex[:12]
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{name}"')
        made.append(name)
        return f"host={postgres_server} user=postgres dbname={name}"

    yield make
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        for name in made:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def store_factory(request: pytest.FixtureRequest) -> Callable[..., Store]:
    """`store_factory(kind, clock=None)`: a fresh memory, sqlite or postgres store."""
    from nuramesh.stores import MemoryStore, SqliteStore

    def make(kind: str, clock: Clock | None = None) -> Store:
        use = clock or utc_now
        if kind == "memory":
            return MemoryStore(clock=use)
        if kind == "sqlite":
            return SqliteStore(":memory:", clock=use)
        from nuramesh.stores.postgres import PostgresStore

        # the fence clock follows the injected one so lease tests can move time by hand; the
        # database-clock path has its own tests (tests/stores/test_fence_clock.py)
        return PostgresStore(
            request.getfixturevalue("make_postgres_dsn")(), clock=use, fence_clock=use
        )

    return make


_NO_REAL_SERVER = (
    "a test started a real uvicorn server; stub it or mark the test with @pytest.mark.real_server"
)


def _uvicorn() -> Any:
    """The uvicorn module, or None where the `server` extra is not installed."""
    try:
        import uvicorn
    except ImportError:
        return None
    return uvicorn


def _refuse_real_server(*_args: object, **_kwargs: object) -> None:
    raise AssertionError(_NO_REAL_SERVER)


@pytest.fixture(autouse=True)
def _block_real_uvicorn(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that starts a real server (and may hang on it) must say so with `real_server`.

    A test's own `monkeypatch` of the same names applies after this one, so stubs keep working.
    """
    if request.node.get_closest_marker("real_server") is not None:
        return
    uvicorn = _uvicorn()
    if uvicorn is None:  # the lean install has no server extra: nothing to block
        return
    monkeypatch.setattr(uvicorn.Server, "run", _refuse_real_server)
    monkeypatch.setattr(uvicorn.Server, "serve", _refuse_real_server)
    monkeypatch.setattr(uvicorn, "run", _refuse_real_server)
    # `uvicorn.main` is the click command, so the module comes from sys.modules
    monkeypatch.setattr(sys.modules["uvicorn.main"], "run", _refuse_real_server)


@pytest.fixture(autouse=True)
def refresh_successor_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """`nuramesh serve --auth` needs the refresh successor key (T109); a throwaway one is made per
    test, never a literal. Here, not in `tests/server/conftest.py`: pytest drops a package's
    conftest fixtures when a run interleaves `tests/server` files with files of other folders, which
    left the variable unset in the later server tests (order-dependent failure)."""
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    monkeypatch.setenv("NURAMESH_REFRESH_SUCCESSOR_KEY", key)


@pytest.hookimpl(tryfirst=True)
def pytest_collection(session: pytest.Session) -> None:
    """Group the named files by folder before collecting (#928, `tests/arg_order.py`). This changes
    the order of execution (named folders run grouped), never which tests are selected."""
    session.config.args = group_args_by_folder(list(session.config.args))


@pytest.hookimpl(wrapper=True)  # marks before xdist reads the groups, ids fixed after (9.2)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> Generator[None, None, None]:
    """Tests marked `quiet` measure speed, so they run only on a quiet machine (T189); tests marked
    `serial` get their xdist group (`tests/serial_marks.py`)."""
    mark_serial_items(items)
    if os.environ.get("NURAMESH_QUIET_LANE") != "1":
        skip = pytest.mark.skip(reason="a speed measurement: set NURAMESH_QUIET_LANE=1 (T189)")
        for item in items:
            if item.get_closest_marker("quiet") is not None:
                item.add_marker(skip)
    yield
    add_group_to_node_ids(config, items)


@pytest.fixture(autouse=True)
def _allow_private_network_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake model servers of the suite live on 127.0.0.1, which the outbound guard refuses by
    default (R1.30 review 1). The guard honours the setting only in a process that recorded it in
    the ledger, so the suite marks that as done too. Tests of the guard itself undo both."""
    from nuramesh.engine import net_guard

    monkeypatch.setenv("NURAMESH_ALLOW_PRIVATE_NETWORK", "1")
    monkeypatch.setattr(net_guard, "_recorded", True)
