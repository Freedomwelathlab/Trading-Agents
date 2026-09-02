import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class PasswordResetRequest(BaseModel):
    """Phase 46 (docs/DECISIONS.md D063). Body of the PUBLIC, unauthenticated
    POST /auth/password-reset/request.

    `email` is validated only for length, not shape. A stricter validator
    would answer a malformed address with a 422 while a well-formed unknown
    one gets a 200 - which is a weaker version of the same enumeration
    oracle the generic 200 exists to close, and would reject some genuinely
    deliverable addresses besides. What matters here is that an
    unrecognised value costs the caller a full request and tells them
    nothing.
    """

    email: str = Field(min_length=3, max_length=320)


class PasswordResetConfirmRequest(BaseModel):
    """Body of POST /auth/password-reset/confirm.

    `new_password` carries the same `min_length=8` floor as
    `CreateUserRequest` in apps/api/app/api/schemas_admin.py - deliberately
    the same number, because a reset must not be a way to set a password
    the create endpoint would have refused.
    """

    token: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=8)


class PasswordResetAcknowledgement(BaseModel):
    """The PUBLIC request endpoint's response, and the only one it has.

    There is exactly one field and it is a fixed string: any variation at
    all - a different message, a different status, a different field set -
    would tell the caller whether the address is registered. That is why
    this type carries no `sent`, no `user_id`, and no `expires_at`.
    """

    detail: str


class AdminPasswordResetResponse(BaseModel):
    """The ADMIN endpoint's response (POST /admin/users/{id}/password-reset).

    Unlike the public acknowledgement above, this one is allowed to be
    specific: the caller already holds `admin:manage` and already named a
    real user id, so there is nothing left to enumerate.

    `delivery` is the honest record of what actually happened, and the two
    values are mutually exclusive with `reset_link`:

      "SENT"                            - an email provider is configured
                                          and ACCEPTED the message.
                                          `reset_link` is null, because the
                                          link is now in the user's inbox
                                          and echoing it here would put a
                                          live credential in an API
                                          response for no reason.
      "NOT_CONFIGURED_returned_directly" - no email provider is configured.
                                          `reset_link` carries the real
                                          link so an admin can relay it
                                          out of band.

    There is no third value meaning "we tried and it failed": a failed send
    is a 502, not a 200 with a caveat.
    """

    user_id: uuid.UUID
    expires_at: datetime
    delivery: str
    reset_link: str | None = None
