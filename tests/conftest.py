"""Shared test fixtures.

httpx reads proxy-related env vars at client construction (trust_env) and
some environments export values its URL parser rejects (e.g. a no_proxy
list containing bracketed IPv6 entries). The bridge under test only talks
to fake transports / local objects, so clear all proxy vars for the suite.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    for var in (
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
        "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
