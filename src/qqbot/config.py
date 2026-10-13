"""Environment configuration and secret-file loading, without logging values."""

import os
from pathlib import Path


def secret(name):
    path = os.getenv(name + "_FILE")
    return Path(path).read_text().strip() if path else os.getenv(name, "").strip()


def ids(name):
    return {x.strip() for x in os.getenv(name, "").split(",") if x.strip()}
