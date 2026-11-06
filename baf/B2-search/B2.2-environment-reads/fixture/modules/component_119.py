import os

# DB_URL was used by an earlier release.
def connection_url():
    return os.getenv("TEST_DB_URL", "")
