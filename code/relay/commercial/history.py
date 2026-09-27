"""Local-only, bounded, redacted history. Export files contain no account credentials."""

import hashlib
import html
import ipaddress
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

PRIVATE_KEYS = {
    "wifi_network",
    "ssid",
    "bssid",
    "email",
    "token",
    "authorization",
    "cookie",
    "password",
    "secret",
    "access_token",
    "refresh_token",
    "accesstoken",
    "refreshtoken",
}


def redact(value, key=""):
    lower = key.lower()
    if lower in PRIVATE_KEYS or any(
        word in lower for word in ("password", "secret", "token", "api_key", "apikey", "api-key")
    ):
        return "[已隐藏]"
    if isinstance(value, dict):
        result = {}
        for index, (k, v) in enumerate(value.items()):
            safe_key = redact(str(k))
            if safe_key in result:
                safe_key += "_" + str(index)
            result[safe_key] = redact(v, str(k))
        return result
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value if isinstance(value, (int, float, bool, type(None))) else str(value)

    # Remove URL credentials, query, fragments and paths, which may contain opaque secrets.
    def safe_url(match):
        try:
            p = urlsplit(match.group())
            return p.scheme + "://" + (p.hostname or "[主机]")
        except ValueError:
            return "[URL]"

    value = re.sub(r"https?://[^\s\]\)<>]+", safe_url, value)
    value = re.sub(
        r"(?im)\b(?:proxy-authorization|authorization|set-cookie|cookie)\s*:\s*[^\r\n]*",
        "[认证头已隐藏]",
        value,
    )
    value = re.sub(
        r"(?i)\b(?:bearer|authorization|api[_-]?key|token|password|cookie|secret)\s*[:= ]\s*[^\s,;]+",
        "[凭证已隐藏]",
        value,
    )
    value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[凭证已隐藏]", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱]", value)
    value = re.sub(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b", "[MAC]", value)
    value = re.sub(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])", "[IP]", value)

    def safe_ipv6(match):
        try:
            ipaddress.IPv6Address(match.group().split("%")[0])
            return "[IPv6]"
        except ValueError:
            return match.group()

    return re.sub(r"(?<!\w)[0-9a-fA-F]*:[0-9a-fA-F:%.-]+", safe_ipv6, value)


class HistoryStore:
    def __init__(self, path, retention_days=30, max_records=10000, clock=time.time):
        self.path, self.retention_days, self.max_records, self.clock = (
            Path(path),
            retention_days,
            max_records,
            clock,
        )
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Mode is applied at creation, not after sensitive contents are written.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL)"
            )

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def record(self, snapshot):
        # Only diagnostic fields, never arbitrary outer application/session state.
        payload = redact(
            {
                key: snapshot.get(key, {} if key == "status" else [])
                for key in ("status", "issues", "check_errors")
            }
        )
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        now = int(self.clock())
        with self._connect() as db:
            db.execute(
                "DELETE FROM snapshots WHERE created_at < ?", (now - self.retention_days * 86400,)
            )
            latest = db.execute(
                "SELECT id,digest FROM snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if latest and latest[1] == digest:
                return latest[0]
            cursor = db.execute(
                "INSERT INTO snapshots(created_at,digest,payload) VALUES(?,?,?)",
                (now, digest, encoded),
            )
            db.execute(
                "DELETE FROM snapshots WHERE id NOT IN (SELECT id FROM snapshots ORDER BY id DESC LIMIT ?)",
                (self.max_records,),
            )
            return cursor.lastrowid

    def list(self, limit=50):
        now = int(self.clock())
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,created_at,payload FROM snapshots WHERE created_at >= ? ORDER BY id DESC LIMIT ?",
                (now - self.retention_days * 86400, max(1, min(limit, self.max_records))),
            ).fetchall()
        return [{"id": row[0], "createdAt": row[1], **json.loads(row[2])} for row in rows]

    def compare_latest(self):
        rows = self.list(2)
        if len(rows) < 2:
            return {"available": False, "changes": [], "message": "至少需要两次不同的检测记录。"}
        after, before = rows
        changes = []
        for key in sorted(set(before["status"]) | set(after["status"])):
            a, b = before["status"].get(key), after["status"].get(key)
            if a != b:
                changes.append({"field": key, "before": a, "after": b})
        if before["issues"] != after["issues"]:
            changes.append(
                {"field": "issues", "before": before["issues"], "after": after["issues"]}
            )
        return {
            "available": True,
            "before": before["createdAt"],
            "after": after["createdAt"],
            "changes": changes,
        }

    def export(self, directory):
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {
            "product": "NetCare",
            "schema": "diagnostic-bundle-v1",
            "redacted": True,
            "generatedAt": int(self.clock()),
            "records": self.list(200),
            "comparison": self.compare_latest(),
        }
        stem = (
            "netcare-report-"
            + datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + uuid.uuid4().hex[:6]
        )
        paths = [directory / (stem + ".json"), directory / (stem + ".html")]
        title = "NetCare 脱敏诊断报告"
        body = (
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>'
            + title
            + "</title><style>body{font:16px system-ui;max-width:900px;margin:48px auto;padding:0 24px;line-height:1.65;color:#222}pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:20px;background:#f4f5f6;border-radius:12px}h1{font-size:30px}</style><h1>"
            + title
            + "</h1><p>仅包含本机最近的诊断记录。IP、网络名称和常见凭证字段已隐藏；发送给他人前仍请检查内容。</p>"
        )
        for row in payload["records"]:
            stamp = (
                datetime.fromtimestamp(row["createdAt"])
                .astimezone()
                .strftime("%Y-%m-%d %H:%M:%S %Z")
            )
            body += (
                "<h2>"
                + html.escape(stamp)
                + "</h2><pre>"
                + html.escape(json.dumps(row, ensure_ascii=False, indent=2))
                + "</pre>"
            )
        body += "</html>"
        for path, content in zip(paths, [json.dumps(payload, ensure_ascii=False, indent=2), body]):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
        return paths
