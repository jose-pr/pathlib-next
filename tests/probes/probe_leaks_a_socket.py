"""Run in a child pytest by `test_census.py`: leaves a socket open at the end
of the session."""

import socket

KEPT = []


def test_keeps_a_socket():
    left, right = socket.socketpair()
    right.close()
    KEPT.append(left)
