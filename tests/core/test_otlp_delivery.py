import io
import urllib.error
from unittest import mock

import pytest

from core.common import _post_otlp


def _response():
    response = mock.MagicMock()
    response.status = 200
    response.__enter__.return_value = response
    return response


def test_sender_identifies_itself_without_mutating_caller_headers():
    headers = {"Content-Type": "application/x-protobuf"}
    with mock.patch("core.common.urllib.request.urlopen", return_value=_response()) as send:
        assert _post_otlp("https://collector.example/v1/traces", b"payload", headers, "Phoenix")
    assert send.call_args.args[0].get_header("User-agent") == "LiFl-Coding-Harness-Tracing/1.0"
    assert headers == {"Content-Type": "application/x-protobuf"}


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_transient_rejection_retries_the_same_payload(status):
    rejection = urllib.error.HTTPError("https://collector.example", status, "Retry", {}, io.BytesIO(b"busy"))
    with mock.patch("core.common.urllib.request.urlopen", side_effect=[rejection, _response()]) as send:
        with mock.patch("core.common.time.sleep") as sleep:
            assert _post_otlp("https://collector.example/v1/traces", b"payload", {}, "Phoenix")
    assert send.call_count == 2
    assert all(call.args[0].data == b"payload" for call in send.call_args_list)
    sleep.assert_called_once()


def test_auth_rejection_is_not_retried(capsys):
    rejection = urllib.error.HTTPError(
        "https://collector.example", 403, "Forbidden", {}, io.BytesIO(b"error code: 1010")
    )
    with mock.patch("core.common.urllib.request.urlopen", side_effect=rejection) as send:
        with mock.patch("core.common.time.sleep") as sleep:
            assert not _post_otlp("https://collector.example/v1/traces", b"payload", {}, "Phoenix")
    send.assert_called_once()
    sleep.assert_not_called()
    assert "1010" in capsys.readouterr().err


def test_connection_failure_has_bounded_retries():
    with mock.patch("core.common.urllib.request.urlopen", side_effect=urllib.error.URLError("offline")) as send:
        with mock.patch("core.common.time.sleep") as sleep:
            assert not _post_otlp("https://collector.example/v1/traces", b"payload", {}, "Phoenix")
    assert send.call_count == 3
    assert sleep.call_count == 2
