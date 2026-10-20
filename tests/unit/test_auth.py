import pytest

from llm_gateway.auth import ANONYMOUS, Authenticator
from llm_gateway.config import ClientKeyConfig, RateLimitConfig
from llm_gateway.errors import AuthenticationError

DEFAULT = RateLimitConfig(requests_per_minute=10)


def _auth() -> Authenticator:
    return Authenticator(
        [
            ClientKeyConfig(name="a", key="key-a"),
            ClientKeyConfig(
                name="b", key_env="B_KEY", rate_limit=RateLimitConfig(requests_per_minute=1)
            ),
        ],
        DEFAULT,
        environ={"B_KEY": "key-b"},
    )


def test_anonymous_when_no_clients() -> None:
    auth = Authenticator([], DEFAULT)
    assert not auth.enabled
    client = auth.authenticate({})
    assert client.name == ANONYMOUS
    assert client.limits == DEFAULT


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"authorization": "Bearer key-a"}, "a"),
        ({"authorization": "bearer   key-b "}, "b"),
        ({"x-api-key": "key-b"}, "b"),
        ({"authorization": "Basic xyz", "x-api-key": "key-a"}, "a"),
    ],
)
def test_valid_keys(headers: dict[str, str], expected: str) -> None:
    assert _auth().authenticate(headers).name == expected


def test_client_specific_limits() -> None:
    auth = _auth()
    assert auth.authenticate({"x-api-key": "key-a"}).limits == DEFAULT
    assert auth.authenticate({"x-api-key": "key-b"}).limits.requests_per_minute == 1


@pytest.mark.parametrize(
    "headers",
    [{}, {"authorization": "Bearer "}, {"x-api-key": "  "}, {"authorization": "Token key-a"}],
)
def test_missing_key(headers: dict[str, str]) -> None:
    with pytest.raises(AuthenticationError, match="Missing API key"):
        _auth().authenticate(headers)


def test_invalid_key() -> None:
    with pytest.raises(AuthenticationError, match="Invalid API key") as exc:
        _auth().authenticate({"authorization": "Bearer nope"})
    assert exc.value.status_code == 401
    assert exc.value.code == "invalid_api_key"


def test_missing_env_key_fails_fast() -> None:
    with pytest.raises(ValueError, match="B_KEY"):
        Authenticator([ClientKeyConfig(name="b", key_env="B_KEY")], DEFAULT, environ={})
