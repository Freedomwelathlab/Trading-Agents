import uuid

import jwt
import pytest

from apps.api.app.auth.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from apps.api.app.core.config import Settings


def make_settings(**overrides) -> Settings:
    defaults = dict(_env_file=None, jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256")
    defaults.update(overrides)
    return Settings(**defaults)


def test_hash_password_never_stores_the_plaintext():
    hashed = hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert hashed.startswith("$2b$")


def test_verify_password_accepts_the_right_password():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True


def test_verify_password_rejects_the_wrong_password():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("wrong password", hashed) is False


def test_access_token_round_trips_the_user_id():
    settings = make_settings()
    user_id = uuid.uuid4()
    token = create_access_token(user_id, settings)
    assert decode_access_token(token, settings) == user_id


def test_decode_rejects_a_token_signed_with_a_different_secret():
    token = create_access_token(uuid.uuid4(), make_settings(jwt_secret_key="secret-a"))
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, make_settings(jwt_secret_key="secret-b"))


def test_decode_rejects_an_expired_token():
    settings = make_settings(jwt_access_token_expire_minutes=-1)
    token = create_access_token(uuid.uuid4(), settings)
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings)


def test_decode_rejects_garbage():
    with pytest.raises(InvalidTokenError):
        decode_access_token("not-a-real-token", make_settings())


def test_decode_rejects_a_token_with_a_non_uuid_subject():
    settings = make_settings()
    token = jwt.encode(
        {"sub": "not-a-uuid"}, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings)
