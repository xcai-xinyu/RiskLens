"""Disable Chroma's implicit .env parsing before importing its package defaults."""
from pydantic_settings import DotEnvSettingsSource

# Chroma constructs Settings() at package import and declares env_file='.env'.
# Explicit _env_file=None alone is too late for those package-level defaults.
# This narrow guard keeps other settings providers unchanged; no dependency file is edited.
_original = DotEnvSettingsSource._read_env_files


def _trusted_environment_only(self):
    if self.settings_cls.__module__.startswith('chromadb'):
        return {}
    return _original(self)


if not getattr(DotEnvSettingsSource._read_env_files,'_risklens_chroma_guard',False):
    _trusted_environment_only._risklens_chroma_guard = True
    DotEnvSettingsSource._read_env_files = _trusted_environment_only

import chromadb
from chromadb.config import Settings as ChromaSettings

__all__ = ['ChromaSettings','chromadb']
