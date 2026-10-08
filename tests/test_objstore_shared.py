"""What the object-store schemes share: how a transport failure reads."""

import errno

import pytest

from pathlib_next.uri.schemes import _objstore as store


def _named(name, *bases):
    return type(name, bases or (Exception,), {})


def test_a_timeout_is_found_by_the_name_of_its_type_or_of_what_it_wraps():
    read = _named("ReadTimeout")
    assert store.mentions_timeout(read("slow"))
    assert store.mentions_timeout(TimeoutError())
    assert store.mentions_timeout(_named("ConnectTimeoutError")("x"))
    outer = _named("ServiceRequestError")("sent")
    outer.inner_exception = read("slow")
    assert store.mentions_timeout(outer)
    reason = _named("MaxRetryError")("retries")
    reason.reason = read("slow")
    assert store.mentions_timeout(reason)
    wrapped = _named("RetryError")("deadline")
    wrapped.cause = _named("Timeout")("x")
    assert store.mentions_timeout(wrapped)


def test_a_refused_connection_is_not_a_timeout_although_urllib3_derives_it_from_one():
    connect_timeout = _named("ConnectTimeoutError")
    refused = _named("NewConnectionError", connect_timeout)("refused")
    assert not store.mentions_timeout(refused)
    wrapper = _named("ConnectionError")("Max retries")
    wrapper.args = (refused,)
    assert not store.mentions_timeout(wrapper)
    assert not store.mentions_timeout(ValueError("unrelated"))


@pytest.mark.parametrize(
    "kind, cls, number",
    [
        (store.TIMEOUT, TimeoutError, errno.ETIMEDOUT),
        (store.UNREACHABLE, ConnectionError, errno.EHOSTUNREACH),
        (store.INTERRUPTED, ConnectionResetError, errno.ECONNRESET),
        (store.FAILED, OSError, errno.EIO),
    ],
)
def test_a_transport_error_names_the_path_and_the_type_of_the_failure_only(
    kind, cls, number
):
    failure = _named("ReadTimeoutError")(
        "GET http://h/b/k?X-Amz-Signature=SECRET failed"
    )
    error = store.transport_error(kind, "S3", "s3://b/k", failure)
    assert type(error) is cls
    assert error.errno == number
    assert error.filename == "s3://b/k"
    assert "ReadTimeoutError" in str(error) and "S3" in str(error)
    assert "SECRET" not in str(error) and "http://h" not in str(error)
