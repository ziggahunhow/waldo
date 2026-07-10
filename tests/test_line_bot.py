from unittest.mock import MagicMock, patch

import line_bot


def _reset_token_cache():
    line_bot._token_cache["token"] = None
    line_bot._token_cache["expires_at"] = 0.0


@patch.dict("os.environ", {"LINE_CHANNEL_ACCESS_TOKEN": "explicit-token"}, clear=True)
@patch("line_bot.requests")
def test_access_token_prefers_explicit_token(mock_requests):
    _reset_token_cache()

    assert line_bot._access_token() == "explicit-token"
    mock_requests.post.assert_not_called()


@patch.dict("os.environ", {"LINE_CHANNEL_ID": "cid", "LINE_CHANNEL_SECRET": "csecret"}, clear=True)
@patch("line_bot.requests")
def test_access_token_reuses_cached_token_before_expiry(mock_requests):
    _reset_token_cache()
    line_bot._token_cache["token"] = "cached-token"
    line_bot._token_cache["expires_at"] = line_bot.time.time() + 3600

    assert line_bot._access_token() == "cached-token"
    mock_requests.post.assert_not_called()


@patch.dict("os.environ", {"LINE_CHANNEL_ID": "cid", "LINE_CHANNEL_SECRET": "csecret"}, clear=True)
@patch("line_bot.requests")
def test_access_token_exchanges_and_caches_new_token(mock_requests):
    _reset_token_cache()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"access_token": "fresh-token", "expires_in": 2592000}
    mock_requests.post.return_value = mock_resp

    result = line_bot._access_token()

    assert result == "fresh-token"
    mock_requests.post.assert_called_once()
    assert line_bot._token_cache["token"] == "fresh-token"

    # second call reuses the cache instead of exchanging again
    result2 = line_bot._access_token()
    assert result2 == "fresh-token"
    mock_requests.post.assert_called_once()


@patch.dict("os.environ", {}, clear=True)
@patch("line_bot.requests")
def test_access_token_raises_when_credentials_missing(mock_requests):
    _reset_token_cache()

    try:
        line_bot._access_token()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "LINE credentials missing" in str(e)
    mock_requests.post.assert_not_called()
