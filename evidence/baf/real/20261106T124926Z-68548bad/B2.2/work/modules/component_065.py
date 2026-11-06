import os

def configure():
    os.environ["DB_URL"] = "sqlite:///local.db"
