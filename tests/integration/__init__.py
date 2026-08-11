"""Integration tests: the real transport over a real socket, on loopback only.

These drive AniDBLink against tests.fake_anidb.FakeAniDBServer rather than a mock,
so the socket handling, tag correlation, compression and encryption paths are all
genuinely exercised. Nothing here leaves 127.0.0.1 -- the autouse guard in
tests/conftest.py enforces that.
"""
