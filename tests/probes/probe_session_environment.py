"""Run in a child pytest by `test_hermetic.py`, started from a shell that
carries proxy, AWS and home-directory settings: the session must not see them.
Not named `test_*`, so an ordinary run does not collect it."""

import os
import pathlib
import urllib.request


def test_the_proxy_and_aws_settings_of_the_shell_are_gone():
    assert not [
        name
        for name in os.environ
        if name.upper().endswith("_PROXY") and name.upper() != "NO_PROXY"
    ]
    assert not [
        name
        for name in os.environ
        if name.startswith("AWS_")
        and name
        not in (
            "AWS_CONFIG_FILE",
            "AWS_SHARED_CREDENTIALS_FILE",
            "AWS_EC2_METADATA_DISABLED",
        )
    ]
    assert {k: v for k, v in urllib.request.getproxies().items() if k != "no"} == {}


def test_the_developers_home_is_not_the_home_directory():
    developer_home = os.environ["PATHLIB_NEXT_DEVELOPER_HOME"]
    assert os.path.expanduser("~") != developer_home
    assert pathlib.Path.home() != pathlib.Path(developer_home)
    assert list(pathlib.Path.home().iterdir()) == []
    for variable in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        assert not os.path.exists(os.environ[variable])


def test_requests_and_botocore_find_neither_a_proxy_nor_a_profile():
    requests = __import__("pytest").importorskip("requests")
    assert requests.utils.get_environ_proxies("http://127.0.0.1:1/x") == {}
    assert requests.utils.get_netrc_auth("http://h/") is None
