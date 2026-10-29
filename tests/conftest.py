"""Shared test setup.

The tests' servers are reached on 127.0.0.1, which NWR Stream Manager never counts as its local
network (anything could be behind it, such as a tunnel). Most tests are about something else, and
want a browser on the local network, as they had before two-step verification: so 127.0.0.1 counts
as local here, unless a test is marked real_local_network.
"""

from __future__ import annotations

import sys

import pytest


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "real_local_network: 127.0.0.1 isn't the local network, as when running for real")


@pytest.fixture(autouse=True)
def loopback_counts_as_local(request, monkeypatch):
    if request.node.get_closest_marker("real_local_network"):
        yield
        return
    for name, module in list(sys.modules.items()):
        if not name.endswith(".local_network") or not hasattr(module, "LocalNetworks"):
            continue
        original = module.LocalNetworks.is_local

        def is_local(self, host, headers, port=0, _original=original, _module=module):
            if str(host) in {"127.0.0.1", "::1", "::ffff:127.0.0.1"} and not _module.request_through_proxy(headers):
                return True
            return _original(self, host, headers, port)

        monkeypatch.setattr(module.LocalNetworks, "is_local", is_local)
    yield
