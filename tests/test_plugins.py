import unittest.mock as mock
import pytest
import importlib.metadata

from pathlib_next.uri import UriPath


def test_plugin_dynamic_loading():
    # Verify "dummy" scheme is not registered initially
    assert "dummy" not in UriPath._schemesmap()

    mock_ep = mock.Mock()
    mock_ep.name = "dummy"

    def mock_load():
        class DummyPluginPath(UriPath):
            __SCHEMES = ("dummy",)
        return DummyPluginPath

    mock_ep.load.side_effect = mock_load

    def mock_entry_points(*args, **kwargs):
        if kwargs.get("group") == "pathlib_next.schemes":
            return [mock_ep]
        return {"pathlib_next.schemes": [mock_ep]}

    with mock.patch("importlib.metadata.entry_points", side_effect=mock_entry_points):
        # Accessing dummy:// path should trigger loading of dummy plugin
        p = UriPath("dummy://host/path")
        assert type(p).__name__ == "DummyPluginPath"
        assert type(p) is not UriPath
        assert "dummy" in UriPath._schemesmap()

    # Clean up the registered subclass from UriPath.__subclasses__() if possible,
    # or reload schemesmap to keep tests clean.
    # Note: in Python, subclasses cannot be easily removed, so we keep reload=True.


def test_plugin_non_matching_scheme():
    # Verify "otherdummy" scheme is not registered
    assert "otherdummy" not in UriPath._schemesmap()

    mock_ep = mock.Mock()
    mock_ep.name = "dummy2"

    def mock_entry_points(*args, **kwargs):
        if kwargs.get("group") == "pathlib_next.schemes":
            return [mock_ep]
        return {"pathlib_next.schemes": [mock_ep]}

    with mock.patch("importlib.metadata.entry_points", side_effect=mock_entry_points):
        # Accessing otherdummy:// should not load mock_ep since names don't match
        p = UriPath("otherdummy://host/path")
        assert type(p) is UriPath
        mock_ep.load.assert_not_called()


# ---------------------------------------------------------------------------
# _load_entry_point edge cases
# ---------------------------------------------------------------------------

class TestLoadEntryPoint:
    def test_returns_false_on_empty_group(self):
        with mock.patch("importlib.metadata.entry_points", return_value=[]):
            assert UriPath._load_entry_point("anything") is False

    def test_returns_false_when_no_name_match(self):
        ep = mock.Mock()
        ep.name = "other"
        with mock.patch("importlib.metadata.entry_points", return_value=[ep]):
            assert UriPath._load_entry_point("missing_xyz") is False
        ep.load.assert_not_called()

    def test_calls_load_on_match_and_returns_true(self):
        ep = mock.Mock()
        ep.name = "mytestscheme"
        ep.load.return_value = object
        with mock.patch("importlib.metadata.entry_points", return_value=[ep]):
            result = UriPath._load_entry_point("mytestscheme")
        assert result is True
        ep.load.assert_called_once()


# ---------------------------------------------------------------------------
# _load_builtin_scheme
# ---------------------------------------------------------------------------

class TestLoadBuiltinScheme:
    @pytest.mark.parametrize("scheme", ["file", "data", "zip", "tar", "ftp", "http", "https"])
    def test_known_stdlib_schemes_return_true(self, scheme):
        """All stdlib-only schemes should always load successfully."""
        assert UriPath._load_builtin_scheme(scheme) is True

    def test_unknown_scheme_returns_false(self):
        assert UriPath._load_builtin_scheme("__no_such_scheme_xyz__") is False

    def test_import_error_returns_false(self):
        """If the target module raises ImportError, _load_builtin_scheme returns False."""
        import sys
        with mock.patch("importlib.import_module", side_effect=ImportError("no dep")):
            result = UriPath._load_builtin_scheme("ftp")
        assert result is False


# ---------------------------------------------------------------------------
# get_scheme_cls integration
# ---------------------------------------------------------------------------

class TestGetSchemeCls:
    def test_known_scheme_returns_subclass(self):
        from pathlib_next.uri.schemes.file import FileUri
        from pathlib_next.uri.source import Source
        source = Source("file", None, None, None)
        cls = source.get_scheme_cls()
        assert issubclass(cls, UriPath)

    def test_unknown_scheme_falls_back_to_uripath(self):
        from pathlib_next.uri.source import Source
        source = Source("__totally_unknown__", None, None, None)
        with mock.patch.object(UriPath, "_load_entry_point", return_value=False):
            with mock.patch.object(UriPath, "_load_builtin_scheme", return_value=False):
                cls = source.get_scheme_cls()
        assert cls is UriPath
