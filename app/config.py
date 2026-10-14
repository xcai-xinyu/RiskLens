import os

from pydantic import Field

from app.schemas import Contract


class Settings(Contract):
    max_tool_calls: int = Field(default=12, ge=0, le=100)
    max_llm_calls: int = Field(default=10, ge=0, le=100)
    max_repair_rounds: int = Field(default=1, ge=0, le=10)
    retrieval_limit: int = Field(default=12, ge=1, le=100)
    run_timeout_seconds: float = Field(default=180, gt=0, le=3600)

    @classmethod
    def from_env(cls):
        return cls(**{name: os.environ[name.upper()] for name in cls.model_fields
                      if name.upper() in os.environ})
