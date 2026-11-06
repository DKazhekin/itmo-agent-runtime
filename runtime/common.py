import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["0"] = "0"


class Session(Message):
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    files: dict[str, str] = Field(default_factory=dict)


class Run(Message):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    instruction: str = Field(min_length=1, max_length=50000)
    expected_mode: Literal["mock", "real"] = "mock"
    expected_model: str | None = Field(default=None, min_length=1, max_length=200)
    max_steps: int = Field(default=30, ge=1, le=60)
    timeout_seconds: int = Field(default=240, ge=1, le=600)


class Operation(Message):
    operation_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    tool: str = Field(pattern=r"^(RunCommand|ReadFile|WriteFile)$")
    arguments: dict
    timeout_seconds: int = Field(default=20, ge=1, le=60)


def check_version(message):
    if message.version != "0":
        raise HTTPException(422, "unsupported protocol version")


def call(base, path, data=None, method=None, timeout=75):
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        response = client.request(method or ("POST" if data is not None else "GET"), base + path, json=data)
        if response.status_code >= 400:
            raise HTTPException(response.status_code, response.text[:2000])
        return response.json()


class Journal:
    def __init__(self, name):
        root = Path(os.environ.get("STATE_DIR", "/state"))
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / f"{name}.db", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, fingerprint TEXT, value TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT, value TEXT)")
        self.db.commit()
        self.lock = threading.RLock()

    def reserve(self, key, payload, value):
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.lock:
            row = self.db.execute("SELECT fingerprint,value FROM records WHERE key=?", (key,)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise HTTPException(409, "identifier already used with different payload")
                return False, json.loads(row[1])
            self.db.execute("INSERT INTO records VALUES (?,?,?)", (key, fingerprint, json.dumps(value)))
            self.db.commit()
            return True, value

    def put(self, key, value):
        with self.lock:
            self.db.execute("UPDATE records SET value=? WHERE key=?", (json.dumps(value), key))
            self.db.commit()

    def get(self, key):
        with self.lock:
            row = self.db.execute("SELECT value FROM records WHERE key=?", (key,)).fetchone()
            if not row:
                raise HTTPException(404, "unknown identifier")
            return json.loads(row[0])

    def all(self):
        with self.lock:
            return [(k, json.loads(v)) for k, v in self.db.execute("SELECT key,value FROM records")]

    def event(self, run, kind, **data):
        value = {"version": "0", "trace_id": run, "type": kind, "time": time.time(), **data}
        with self.lock:
            self.db.execute("INSERT INTO events(run,value) VALUES (?,?)", (run, json.dumps(value)))
            self.db.commit()

    def events(self, run, after=0):
        with self.lock:
            return [{"seq": seq, **json.loads(v)} for seq, v in self.db.execute(
                "SELECT seq,value FROM events WHERE run=? AND seq>? ORDER BY seq", (run, after))]

    def complete(self, key, run, record):
        with self.lock:
            self.db.execute("UPDATE records SET value=? WHERE key=?", (json.dumps(record), key))
            value = {"version": "0", "trace_id": run, "type": "result", "time": time.time(), "result": record}
            exists = self.db.execute("SELECT 1 FROM events WHERE run=? AND json_extract(value,'$.type')='result'", (run,)).fetchone()
            if not exists:
                self.db.execute("INSERT INTO events(run,value) VALUES (?,?)", (run, json.dumps(value)))
            self.db.commit()
