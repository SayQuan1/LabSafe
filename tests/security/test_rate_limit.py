from unittest.mock import Mock

import pytest
from redis.exceptions import ConnectionError

from packages.application.rate_limit import RateLimits
from packages.domain.security import Principal, ServiceError


def test_login_canonical_identity_and_no_plaintext_account_in_key():
    redis = Mock()
    redis.eval.return_value = [1, 5]
    limits = RateLimits(redis, clock=lambda: 899)
    one = limits.login("tenant", "ＡＤＭＩＮ", "1.2.3.4")
    two = limits.login("tenant", "admin", "1.2.3.4")
    assert one == two
    assert "admin" not in one and "1.2.3.4" not in one
    redis.eval.return_value = [6, 1]
    with pytest.raises(ServiceError) as error:
        limits.login("tenant", "admin", "1.2.3.4")
    assert error.value.code == "RATE_LIMITED" and error.value.retry_after == 1


def test_redis_outage_fails_closed_for_writes_and_login_but_bounds_reads(caplog):
    redis = Mock()
    redis.eval.side_effect = ConnectionError("private URL")
    limits = RateLimits(redis, clock=lambda: 10)
    principal = Principal("user", "tenant", "admin", ())
    for call in (lambda: limits.login("t", "u", "ip"), lambda: limits.user(principal, write=True)):
        with pytest.raises(ServiceError) as error:
            call()
        assert error.value.status == 503
    for _ in range(30):
        limits.user(principal, write=False)
    with pytest.raises(ServiceError) as error:
        limits.user(principal, write=False)
    assert error.value.status == 429
    assert "conservative" in caplog.text and "private" not in caplog.text


@pytest.mark.parametrize("result", [False, ConnectionError("private URL")])
def test_readiness_rejects_failed_redis_ping(result):
    redis = Mock()
    if isinstance(result, Exception):
        redis.ping.side_effect = result
    else:
        redis.ping.return_value = result
    with pytest.raises(ServiceError) as error:
        RateLimits(redis).ready()
    assert error.value.status == 503 and "private" not in str(error.value)


def test_upload_grant_rate_limit_is_separate_and_fails_closed():
    redis = Mock()
    limits = RateLimits(redis, clock=lambda: 10)
    actor = Principal("user", "tenant", "name", ())
    redis.eval.return_value = [10, 50]
    limits.upload_grant(actor)
    assert ":upload:" in redis.eval.call_args.args[2]
    redis.eval.return_value = [11, 50]
    with pytest.raises(ServiceError) as caught:
        limits.upload_grant(actor)
    assert caught.value.status == 429 and caught.value.retry_after == 50
    redis.eval.side_effect = ConnectionError("private")
    with pytest.raises(ServiceError) as caught:
        limits.upload_grant(actor)
    assert caught.value.status == 503
