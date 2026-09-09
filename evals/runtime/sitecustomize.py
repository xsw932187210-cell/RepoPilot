"""Pytest fixture API compatibility for 2017-2019 BugsInPy snapshots.

Only restore the removed lookup alias. Never modify assertions or target source.
Applied equally to buggy, fixed, visible-test and hidden-test containers.
"""

import pytest

if not hasattr(pytest.Collector, "get_marker"):
    from _pytest.nodes import Node

    Node.get_marker = Node.get_closest_marker
