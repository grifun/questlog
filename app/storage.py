"""Where the quest log's JSON files live: a local folder, or a folder in an S3 bucket (e.g. Filebase).

Both backends expose the same small interface, with names relative to the root ("questlog.json",
"history/2026-11.json"):

    list(prefix) -> {name: version token}     files directly under prefix ("" or "history/")
    version(name) -> version token or None    None = the file doesn't exist
    read(name) -> (text, version) or None
    write(name, text) -> version token of the written file

A version token changes whenever the file's content changes, so the app can tell when a file was
edited outside the app (by hand, or by uploading to the bucket) and reload it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("questlog.storage")


class StorageError(Exception):
    """The storage could not be read or written (missing folder, S3 error, network...)."""


class LocalBackend:
    min_interval = 0.0          # local files are cheap to check on every request

    def __init__(self, directory: Path | str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.location = str(self.dir)

    @staticmethod
    def _token(st: os.stat_result) -> str:
        return f"{st.st_mtime_ns}-{st.st_size}"

    def list(self, prefix: str = "") -> dict[str, str]:
        folder = self.dir / prefix
        out = {}
        if not folder.is_dir():
            return out
        try:
            files = list(folder.glob("*.json"))
        except OSError as e:
            raise StorageError(str(e)) from e
        for f in files:
            try:
                out[prefix + f.name] = self._token(f.stat())
            except OSError:          # removed between listing and stat
                continue
        return out

    def version(self, name: str) -> str | None:
        try:
            return self._token((self.dir / name).stat())
        except FileNotFoundError:
            return None
        except OSError as e:
            raise StorageError(str(e)) from e

    def read(self, name: str) -> tuple[str, str] | None:
        path = self.dir / name
        try:
            text = path.read_text(encoding="utf-8")
            return text, self._token(path.stat())
        except FileNotFoundError:
            return None
        except OSError as e:
            raise StorageError(str(e)) from e

    def write(self, name: str, text: str) -> str:
        path = self.dir / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)            # atomic: readers never see a half-written file
            return self._token(path.stat())
        except OSError as e:
            raise StorageError(str(e)) from e

    def where(self, name: str) -> str:
        return str(self.dir / name)


class S3Backend:
    min_interval = 10.0         # each check costs a request; don't do it more often than this

    def __init__(self, bucket: str, prefix: str = "", client=None, **client_kwargs):
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("s3", config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=20,
                                                      retries={"max_attempts": 3, "mode": "standard"}),
                                  **client_kwargs)
        self.client = client
        self.bucket = bucket
        prefix = prefix.strip("/")
        self.prefix = prefix + "/" if prefix else ""
        self.location = f"s3://{bucket}/{self.prefix}"
        self.env_names: dict[str, str] = {}   # which environment variable each setting came from (for the log)

    def _call(self, what: str, fn, **kwargs):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            return fn(**kwargs)
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise StorageError(f"{what} {self.location}: {e}") from e
        except BotoCoreError as e:
            raise StorageError(f"{what} {self.location}: {e}") from e

    @staticmethod
    def _token(etag: str, size: int) -> str:
        return f"{etag.strip(chr(34))}-{size}"

    def list(self, prefix: str = "") -> dict[str, str]:
        out = {}
        full = self.prefix + prefix
        paginator = self.client.get_paginator("list_objects_v2")
        pages = self._call("list", lambda: list(paginator.paginate(Bucket=self.bucket, Prefix=full))) or []
        for page in pages:
            for obj in page.get("Contents", []):
                rest = obj["Key"][len(full):]
                if "/" in rest or not rest.endswith(".json"):   # only files directly under the prefix
                    continue
                out[prefix + rest] = self._token(obj["ETag"], obj["Size"])
        return out

    def version(self, name: str) -> str | None:
        res = self._call("check", self.client.head_object, Bucket=self.bucket, Key=self.prefix + name)
        return None if res is None else self._token(res["ETag"], res["ContentLength"])

    def read(self, name: str) -> tuple[str, str] | None:
        res = self._call("read", self.client.get_object, Bucket=self.bucket, Key=self.prefix + name)
        if res is None:
            return None
        return res["Body"].read().decode("utf-8"), self._token(res["ETag"], res["ContentLength"])

    def write(self, name: str, text: str) -> str:
        body = text.encode("utf-8")
        res = self._call("write", self.client.put_object, Bucket=self.bucket, Key=self.prefix + name,
                         Body=body, ContentType="application/json; charset=utf-8")
        return self._token(res["ETag"], len(body))

    def where(self, name: str) -> str:
        return f"s3://{self.bucket}/{self.prefix}{name}"


# Accepted names for each setting. Names match loosely: case, "_", "-" and spaces are ignored,
# so S3FOLDER, S3_FOLDER and s3folder are all the same variable.
S3_VARS = {
    "folder": ("S3FOLDER",),
    "key": ("S3_ACCESS_KEY_ID", "S3_ACCESS_KEY", "S3_KEY", "FILEBASE_KEY", "ACCESS_KEY_ID"),
    "secret": ("S3_SECRET_ACCESS_KEY", "S3_SECRET_KEY", "S3_SECRET", "FILEBASE_SECRET", "SECRET_ACCESS_KEY"),
    "endpoint": ("S3_ENDPOINT",),
    "region": ("S3_REGION",),
}


def _norm(name: str) -> str:
    return "".join(ch for ch in name.upper() if ch.isalnum())


def _clean(value: str) -> str:
    """Trim whitespace and surrounding quotes, e.g. a value pasted as 'https://s3.filebase.io'."""
    value = value.strip()
    while len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].strip()
    return value


def _lookup(env, setting: str) -> tuple[str, str]:
    """(value, variable name used) for a setting, or ("", "") if none of its names is set."""
    for want in (_norm(n) for n in S3_VARS[setting]):      # in order of preference
        for name, value in env.items():
            if _norm(name) == want and _clean(value):
                return _clean(value), name
    return "", ""


def s3_related_names(env=os.environ) -> list[str]:
    """Names (never values) of environment variables that look storage-related, for diagnostics."""
    return sorted(n for n in env if any(w in n.upper() for w in ("S3", "FILEBASE", "BUCKET", "AWS_")))


def s3_config_from_env(env=os.environ) -> dict | None:
    """S3 settings if S3FOLDER ("bucket" or "bucket/folder") is set, otherwise None."""
    found = {k: _lookup(env, k) for k in S3_VARS}
    folder = found["folder"][0].strip("/")
    if not folder:
        return None
    bucket, _, prefix = folder.partition("/")
    kwargs = {"endpoint_url": found["endpoint"][0] or "https://s3.filebase.io",
              "region_name": found["region"][0] or "auto"}
    if found["key"][0]:
        kwargs["aws_access_key_id"] = found["key"][0]
    if found["secret"][0]:
        kwargs["aws_secret_access_key"] = found["secret"][0]
    names = {k: v[1] for k, v in found.items() if v[1]}
    return {"bucket": bucket, "prefix": prefix, "client_kwargs": kwargs, "names": names}


def s3_backend_from_env(env=os.environ) -> S3Backend | None:
    cfg = s3_config_from_env(env)
    if cfg is None:
        return None
    backend = S3Backend(cfg["bucket"], cfg["prefix"], **cfg["client_kwargs"])
    backend.env_names = cfg["names"]
    return backend
