import os
from os import getenv

def connection_url():
    return os.environ.get("DB_URL", "")
