"""The one username and password, the signed session token and the sign-in throttle."""
import pytest

from ledgersync.auth import LoginThrottle, check_settings, credentials_match, make_token, token_valid
from ledgersync.config import Settings

WEEK = 7 * 86400


def on(**kw) -> Settings:
    return Settings(**{"auth_username": "admin", "auth_password": "s3cret", "auth_secret": "k" * 32, **kw})


def test_check_settings_refuses_half_set_credentials():
    with pytest.raises(ValueError, match="LEDGERSYNC_AUTH_PASSWORD"):
        check_settings(Settings(auth_username="admin"))
    with pytest.raises(ValueError, match="LEDGERSYNC_AUTH_USERNAME"):
        check_settings(Settings(auth_password="pw"))


def test_check_settings_refuses_a_short_secret():
    with pytest.raises(ValueError, match="LEDGERSYNC_AUTH_SECRET"):
        check_settings(on(auth_secret="short"))
    assert check_settings(Settings()) is None
    assert check_settings(on()) is None


def test_credentials_match_only_the_exact_pair():
    assert credentials_match(on(), "admin", "s3cret")
    assert not credentials_match(on(), "admin", "S3cret")
    assert not credentials_match(on(), "Admin", "s3cret")
    assert not credentials_match(on(), "", "")
    assert not credentials_match(Settings(), "", "")
    assert not credentials_match(Settings(), "admin", "s3cret")


def test_a_fresh_token_is_valid_until_it_expires():
    t = make_token(on(), now=1000.0)
    assert token_valid(on(), t, 1000.0 + WEEK - 1)
    assert not token_valid(on(), t, 1000.0 + WEEK + 1)


def test_an_edited_expiry_is_refused():
    t = make_token(on(), now=1000.0)
    expires, signature = t.split(".", 1)
    edited = f"{int(expires) + 10 * 86400}.{signature}"
    assert not token_valid(on(), edited, 1000.0)


def test_changing_the_password_signs_everyone_out():
    t = make_token(on(), now=1000.0)
    assert not token_valid(on(auth_password="new"), t, 1000.0)
    assert not token_valid(on(auth_username="other"), t, 1000.0)


def test_rubbish_tokens_are_refused():
    for token in (None, "", "abc", "1.2.3", "notanumber.ff"):
        assert not token_valid(on(), token, 1000.0)


def test_throttle_blocks_after_ten_failures_and_reopens_after_the_window():
    now = [0.0]
    throttle = LoginThrottle(clock=lambda: now[0])
    for _ in range(10):
        assert throttle.allowed()
        throttle.failed()
    assert not throttle.allowed()
    now[0] += 901
    assert throttle.allowed()

    fresh = LoginThrottle(clock=lambda: now[0])
    for _ in range(9):
        fresh.failed()
    fresh.succeeded()
    fresh.failed()
    assert fresh.allowed()
