"""Load trusted project configuration without exposing secret values."""
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def load_environment():
    """Existing process values win; never search a caller's working directory."""
    load_dotenv(ROOT / '.env', override=False, interpolate=False)
