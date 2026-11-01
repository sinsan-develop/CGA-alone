from uuid import UUID
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")]


class ConnectionWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    bot_id: UUID
    installation_id: Identifier
    account_id: Identifier
    target_id: Identifier
    credentials: dict[str, SecretStr] | None = Field(default=None, repr=False, max_length=12)
