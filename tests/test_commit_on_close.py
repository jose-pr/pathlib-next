"""The write buffer `ftp:` and the archive schemes share: its content goes to
`commit` when it is closed, once, and only if it was written to."""

import gc

import pytest

from pathlib_next.utils._commit import _CommitOnClose


def _recording():
    seen = []
    return seen, lambda buffer: seen.append(buffer.getvalue())


def test_a_new_file_is_committed_at_close_even_when_nothing_was_written():
    seen, commit = _recording()
    stream = _CommitOnClose(commit)
    assert seen == []
    stream.close()
    stream.close()
    assert seen == [b""]


def test_what_was_written_is_committed_once():
    seen, commit = _recording()
    with _CommitOnClose(commit) as stream:
        stream.write(b"ab")
        stream.writelines([b"c", b"d"])
    assert seen == [b"abcd"]


def test_a_file_opened_with_content_is_committed_only_if_it_was_modified():
    seen, commit = _recording()
    _CommitOnClose(commit, initial=b"keep").close()
    assert seen == []
    stream = _CommitOnClose(commit, initial=b"keep")
    assert stream.read() == b"keep"
    stream.truncate(2)
    stream.close()
    assert seen == [b"ke"]


def test_the_commit_reads_the_buffer_before_it_is_closed():
    states = []

    def commit(buffer):
        states.append(buffer.closed)
        buffer.seek(0)
        states.append(buffer.read())

    with _CommitOnClose(commit) as stream:
        stream.write(b"xyz")
    assert states == [False, b"xyz"]


def test_a_failed_commit_is_not_tried_again_at_garbage_collection():
    calls = []

    def commit(buffer):
        calls.append(1)
        raise OSError("refused")

    stream = _CommitOnClose(commit)
    stream.write(b"x")
    with pytest.raises(OSError):
        stream.close()
    assert stream.closed
    del stream
    gc.collect()
    assert calls == [1]
