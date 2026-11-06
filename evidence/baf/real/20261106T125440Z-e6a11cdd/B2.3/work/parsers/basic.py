from datetime import datetime

def parse_date(value):
    return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
