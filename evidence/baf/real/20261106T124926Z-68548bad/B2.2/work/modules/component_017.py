import os
from os import getenv

def connection_url():
    return os.getenv("DB_URL")
