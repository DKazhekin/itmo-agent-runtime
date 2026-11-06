import os
from os import getenv

def connection_url():
    return getenv("DB_URL", "")
