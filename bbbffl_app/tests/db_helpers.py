import tempfile
from pathlib import Path

from app.db import connect
from app.migrations import migrate


def migrated_connection():
    path = Path(tempfile.mkstemp(suffix=".db")[1])
    url = f"sqlite:///{path}"
    migrate(url)
    return connect(url)
