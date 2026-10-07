"""Tests for the Priompt Python client's cache logic and URI mapping.

The gRPC stub is faked (no server, no network) so we can assert exactly how
many RPCs the cache lets through. Time is driven by a fake clock so TTL tests
are deterministic. Most cases here are "looks like it worked but returned the
wrong thing" traps: a pinned read served from the HEAD cache, a get()/list()
key collision, a value handed back after its TTL expired.
"""

import threading

import pytest

from priompt_sdk import PromptClient
from priompt_sdk.client import _subject


class FakeStub:
    """Records call counts and returns a distinct token per RPC so a caller can
    tell a fresh response from a cached one."""

    def __init__(self):
        self.get_calls = 0
        self.list_calls = 0
        self.diff_calls = 0

    def GetPrompt(self, req, metadata=None):
        self.get_calls += 1
        # echo back what was asked + a sequence number to prove freshness
        return f"get:{req.uri}:{req.ref}:{self.get_calls}"

    def ListPrompts(self, req, metadata=None):
        self.list_calls += 1
        return type("Resp", (), {"entries": f"list:{req.prefix}:{self.list_calls}"})

    def DiffPrompt(self, req, metadata=None):
        self.diff_calls += 1
        return f"diff:{req.uri}:{self.diff_calls}"


@pytest.fixture
def clock(monkeypatch):
    """A settable monotonic clock. clock.now advances only when we say so."""
    import priompt_sdk.client as mod

    state = {"t": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: state["t"])

    class Clock:
        def advance(self, dt):
            state["t"] += dt

    return Clock()


def make_client(cache_ttl=0):
    # insecure_channel is lazy (no connect until an RPC), so no server needed;
    # we swap in the fake stub before any call.
    c = PromptClient("localhost:1", cache_ttl=cache_ttl)
    c._stub = FakeStub()
    return c


# --- _subject: pure URI -> NATS subject mapping ---------------------------

@pytest.mark.parametrize("uri,expected", [
    ("priompt://team/agent/sys", "priompt.team.agent.sys"),
    ("priompt://a", "priompt.a"),
    ("team/agent", "priompt.team.agent"),          # no scheme prefix
    ("priompt://a/b/", "priompt.a.b."),           # trailing slash preserved
])
def test_subject(uri, expected):
    assert _subject(uri) == expected


# --- caching happy path ---------------------------------------------------

def test_get_caches_within_ttl(clock):
    c = make_client(cache_ttl=10)
    first = c.get("priompt://x")
    second = c.get("priompt://x")
    assert first == second
    assert c._stub.get_calls == 1  # second served from cache


def test_ttl_zero_disables_cache():
    c = make_client(cache_ttl=0)
    c.get("priompt://x")
    c.get("priompt://x")
    assert c._stub.get_calls == 2  # no caching at all


# --- expected-failure traps ----------------------------------------------

def test_expired_entry_is_refetched_not_served(clock):
    """Perceived success: a value is in the cache. Trap: it's past its TTL, so
    serving it would be a stale read. Must refetch."""
    c = make_client(cache_ttl=10)
    a = c.get("priompt://x")
    clock.advance(11)  # cross the TTL boundary
    b = c.get("priompt://x")
    assert c._stub.get_calls == 2
    assert a != b  # got a genuinely fresh response, not the stale one


def test_pinned_read_is_never_cached(clock):
    """A ref= read pins a specific version and must not be cached, or a later
    pinned read would silently get an old pin."""
    c = make_client(cache_ttl=10)
    c.get("priompt://x", ref="abc123")
    c.get("priompt://x", ref="abc123")
    assert c._stub.get_calls == 2  # each pinned read hits the server


def test_pinned_read_does_not_poison_head_cache(clock):
    """The dangerous version of the above: a pinned read must not populate (or
    be served from) the HEAD cache. Otherwise get(uri) returns the pinned
    version instead of HEAD."""
    c = make_client(cache_ttl=10)
    pinned = c.get("priompt://x", ref="abc123")
    head = c.get("priompt://x")  # HEAD read, no ref
    assert pinned != head
    assert c._stub.get_calls == 2  # HEAD was not served from the pinned read


def test_get_and_list_keys_do_not_collide(clock):
    """get() keys the cache by bare uri; list() by ("list", prefix). If they
    collided, list('priompt://x') would return a cached GetPrompt response."""
    c = make_client(cache_ttl=10)
    g = c.get("priompt://x")
    lst = c.list("priompt://x")  # same string as the get key
    assert g != lst
    assert c._stub.get_calls == 1 and c._stub.list_calls == 1


def test_list_caches_within_ttl(clock):
    c = make_client(cache_ttl=10)
    a = c.list("team/")
    b = c.list("team/")
    assert a == b
    assert c._stub.list_calls == 1


def test_diff_is_never_cached():
    c = make_client(cache_ttl=10)
    c.diff("priompt://x", "new template")
    c.diff("priompt://x", "new template")
    assert c._stub.diff_calls == 2


def test_subscribe_requires_nats_url():
    c = make_client()
    with pytest.raises(ValueError):
        c.subscribe("priompt://x", lambda *_: None)


# --- stress: lock-free cache under concurrent readers ---------------------

def test_concurrent_gets_do_not_crash_or_corrupt(clock):
    """The cache is a plain dict with no lock (ponytail: fine for CPython, RPC
    dedup under a race is best-effort). Hammer it from many threads on many
    keys and assert every reader got a correct, self-consistent value and the
    dict didn't corrupt."""
    c = make_client(cache_ttl=100)
    keys = [f"priompt://k{i}" for i in range(20)]
    errors = []

    def worker():
        try:
            for _ in range(200):
                for k in keys:
                    v = c.get(k)
                    # value must belong to the key we asked for
                    assert v.startswith(f"get:{k}:")
        except Exception as e:  # noqa: BLE401 - collect for the assert below
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(c._cache) == len(keys)  # one entry per key, no garbage
