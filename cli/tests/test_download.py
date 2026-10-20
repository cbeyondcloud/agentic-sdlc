"""The CLI's mirror download: https only, the OS trust store, errors as OSError (M1-51)."""

import httpx2
import pytest

from agentic_cli.download import download


def mock(handler):
    return httpx2.MockTransport(handler)


def test_a_mirror_file_is_downloaded():
    def handler(request):
        assert request.url == "https://mirror.example/a/b.tgz"
        return httpx2.Response(200, content=b"bytes")

    assert download("https://mirror.example/a/b.tgz", transport=mock(handler)) == b"bytes"


def test_an_http_error_is_an_oserror_naming_the_status():
    with pytest.raises(OSError, match="404"):
        download("https://mirror.example/x", transport=mock(lambda r: httpx2.Response(404)))


def test_a_network_failure_is_an_oserror():
    def handler(request):
        raise httpx2.ConnectError("no route", request=request)

    with pytest.raises(OSError, match="ConnectError"):
        download("https://mirror.example/x", transport=mock(handler))


def test_a_redirect_off_https_is_refused():
    def handler(request):
        if request.url.scheme == "https":
            return httpx2.Response(302, headers={"location": "http://mirror.example/x"})
        return httpx2.Response(200, content=b"plain")

    with pytest.raises(OSError, match="https"):
        download("https://mirror.example/x", transport=mock(handler))
