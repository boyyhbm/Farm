"""Bounded in-memory import. Raw credentials never enter exceptions or diagnostics."""

import base64
import csv
import io
import json
import re
import stat
import zipfile
from pathlib import PurePosixPath

MAX_ROWS = 5000
MAX_FILE = 4 * 1024 * 1024
MAX_ZIP = 32 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_MEMBER = 512 * 1024
STEAM_ID = re.compile(r"7656119[0-9]{10}")
LOGIN = re.compile(r"[A-Za-z0-9_@.\-]{1,100}")


class ImportProblem(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def text(value, code, limit=100):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ImportProblem(code)
    return value.strip()


def steam_id(value):
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ImportProblem("INVALID_STEAM_ID")
    value = str(value).strip()
    if not STEAM_ID.fullmatch(value):
        raise ImportProblem("INVALID_STEAM_ID")
    return value


def login(value):
    if value in (None, ""):
        return None
    value = text(value, "INVALID_LOGIN")
    if not LOGIN.fullmatch(value):
        raise ImportProblem("INVALID_LOGIN")
    return value


def guard(value):
    if not isinstance(value, dict):
        raise ImportProblem("INVALID_MAFILE")
    if any(
        value.get(k) for k in ("encrypted", "Encrypted", "encryption", "Encryption")
    ) or (
        "salt" in value
        and any(k in value for k in ("data", "ciphertext", "encrypted_data"))
    ):
        raise ImportProblem("ENCRYPTED_MAFILE_UNSUPPORTED")
    for wrapper in ("maFile", "authenticator", "MobileAuthenticator"):
        if wrapper in value:
            value = value[wrapper]
            if not isinstance(value, dict):
                raise ImportProblem("INVALID_MAFILE")
            break
    if any(
        value.get(k) for k in ("encrypted", "Encrypted", "encryption", "Encryption")
    ):
        raise ImportProblem("ENCRYPTED_MAFILE_UNSUPPORTED")
    value = dict(value)
    for canonical, alias in (
        ("shared_secret", "SharedSecret"),
        ("identity_secret", "IdentitySecret"),
    ):
        if alias in value:
            if canonical in value and value[canonical] != value[alias]:
                raise ImportProblem("IDENTIFIER_MISMATCH")
            value[canonical] = value[alias]
    session = value.get("Session", value.get("session", {}))
    if not isinstance(session, dict):
        raise ImportProblem("INVALID_MAFILE")
    identifiers = [
        steam_id(v)
        for v in (
            value.get("steamid"),
            value.get("steam_id"),
            value.get("steam_id64"),
            value.get("SteamID"),
            session.get("SteamID"),
            session.get("SteamId"),
        )
        if v not in (None, "", 0, "0")
    ]
    if len(set(identifiers)) > 1:
        raise ImportProblem("IDENTIFIER_MISMATCH")
    name = login(
        value.get("account_name", value.get("AccountName", value.get("login")))
    )
    for key in ("shared_secret", "identity_secret"):
        secret = value.get(key)
        if not isinstance(secret, str) or len(secret) > 128:
            raise ImportProblem("INVALID_GUARD_SECRET")
        try:
            if len(base64.b64decode(secret, validate=True)) not in (20, 32):
                raise ImportProblem("INVALID_GUARD_SECRET")
        except (ValueError, TypeError):
            raise ImportProblem("INVALID_GUARD_SECRET") from None
    # Complete supported JSON stays encrypted, including any Session tokens.
    if len(json.dumps(value).encode()) > MAX_MEMBER:
        raise ImportProblem("MAFILE_TOO_LARGE")
    return {
        "login": name,
        "steam_id": identifiers[0] if identifiers else None,
        "password": None,
        "guard": value,
        "issues": [],
    }


def credential(value):
    if not isinstance(value, dict):
        raise ImportProblem("INVALID_RECORD")
    if any(
        key in value
        for key in (
            "shared_secret",
            "SharedSecret",
            "maFile",
            "authenticator",
            "MobileAuthenticator",
        )
    ):
        return guard(value)
    name = login(value.get("login", value.get("username", value.get("account_name"))))
    identifier = steam_id(value.get("steam_id", value.get("steam_id64")))
    password = value.get("password")
    if password is not None and (
        not isinstance(password, str)
        or not 1 <= len(password) <= 1024
        or "\x00" in password
    ):
        raise ImportProblem("INVALID_PASSWORD")
    if not name:
        raise ImportProblem("LOGIN_REQUIRED")
    result = {
        "login": name,
        "steam_id": identifier,
        "password": password,
        "guard": None,
        "issues": [],
    }
    if value.get("guard") is not None:
        g = guard(value["guard"])
        if (g["login"] and g["login"].lower() != name.lower()) or (
            g["steam_id"] and identifier and g["steam_id"] != identifier
        ):
            raise ImportProblem("IDENTIFIER_MISMATCH")
        result["guard"] = g["guard"]
        result["steam_id"] = identifier or g["steam_id"]
    return result


def parse_file(raw, kind):
    if not raw or len(raw) > (MAX_ZIP if kind == "zip" else MAX_FILE):
        raise ImportProblem("FILE_SIZE_LIMIT")
    if kind == "zip":
        return parse_zip(raw)
    try:
        source = raw.decode("utf-8-sig")
    except UnicodeError:
        raise ImportProblem("UTF8_REQUIRED") from None
    try:
        if kind in ("json", "mafile"):
            value = json.loads(source)
            if kind == "mafile":
                values, parser = [value], guard
            else:
                values = (
                    value.get("accounts")
                    if isinstance(value, dict) and "accounts" in value
                    else value
                )
                values = values if isinstance(values, list) else [values]
                parser = credential
        elif kind == "csv":
            head = source.partition("\n")[0]
            delimiter = ";" if head.count(";") > head.count(",") else ","
            reader = csv.DictReader(io.StringIO(source), delimiter=delimiter)
            if not reader.fieldnames or not set(reader.fieldnames) & {
                "login",
                "username",
                "account_name",
            }:
                raise ImportProblem("CSV_LOGIN_HEADER_REQUIRED")
            values, parser = list(reader), credential
        elif kind == "txt":
            values = []
            for line in source.splitlines():
                if not line.strip():
                    continue
                if "\t" in line:
                    fields = line.split("\t")
                elif ";" in line:
                    fields = next(csv.reader([line], delimiter=";"))
                else:
                    fields = line.split(":", 1)
                if not 2 <= len(fields) <= 3:
                    raise ImportProblem("TXT_EXPECTS_LOGIN_PASSWORD")
                values.append(
                    {
                        "login": fields[0],
                        "password": fields[1],
                        "steam_id": fields[2] if len(fields) == 3 else None,
                    }
                )
            parser = credential
        else:
            raise ImportProblem("UNSUPPORTED_FORMAT")
    except (json.JSONDecodeError, csv.Error, RecursionError):
        raise ImportProblem("CORRUPT_FILE") from None
    if not values or len(values) > MAX_ROWS:
        raise ImportProblem("RECORD_COUNT_LIMIT")
    result = []
    for value in values:
        try:
            result.append(parser(value))
        except ImportProblem as exc:
            result.append(
                {
                    "login": None,
                    "steam_id": None,
                    "password": None,
                    "guard": None,
                    "issues": [exc.code],
                }
            )
    return result


def parse_zip(raw):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ROWS + 100 or not members:
                raise ImportProblem("ZIP_ENTRY_LIMIT")
            seen, total, files = set(), 0, []
            for member in members:
                name = member.filename
                path = PurePosixPath(name.replace("\\", "/"))
                if (
                    path.is_absolute()
                    or any(p in ("..", "") for p in path.parts)
                    or ":" in name
                    or "\\" in name
                    or "\x00" in name
                ):
                    raise ImportProblem("ZIP_UNSAFE_PATH")
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ImportProblem("ZIP_UNSAFE_PATH")
                if name.casefold() in seen:
                    raise ImportProblem("ZIP_DUPLICATE_MEMBER")
                seen.add(name.casefold())
                if member.flag_bits & 1:
                    raise ImportProblem("ENCRYPTED_ZIP_UNSUPPORTED")
                if member.is_dir():
                    continue
                if not name.lower().endswith(".mafile"):
                    raise ImportProblem("ZIP_ONLY_MAFILES")
                total += member.file_size
                if (
                    member.file_size > MAX_MEMBER
                    or total > MAX_EXPANDED
                    or member.file_size > max(1, member.compress_size) * 100
                ):
                    raise ImportProblem("ZIP_EXPANSION_LIMIT")
                files.append(member)
            if not files or len(files) > MAX_ROWS:
                raise ImportProblem("ZIP_ENTRY_LIMIT")
            result = []
            for member in files:
                with archive.open(member) as stream:
                    data = stream.read(MAX_MEMBER + 1)
                if len(data) > MAX_MEMBER:
                    raise ImportProblem("ZIP_EXPANSION_LIMIT")
                result.extend(parse_file(data, "mafile"))
            return result
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError):
        raise ImportProblem("CORRUPT_ZIP") from None


def merge_rows(rows):
    """Merge credentials and authenticators by verified identifiers, never file names."""
    result, names, ids = [], {}, {}
    for row in rows:
        row = dict(row)
        key = row["login"].lower() if row.get("login") else None
        identifier = row.get("steam_id")
        found = {i for i in (names.get(key), ids.get(identifier)) if i is not None}
        if len(found) > 1:
            row["issues"] = sorted(set(row["issues"] + ["IDENTIFIER_MISMATCH"]))
        elif found and not row["issues"]:
            target = result[next(iter(found))]
            if (key and target.get("login") and key != target["login"].lower()) or (
                identifier
                and target.get("steam_id")
                and identifier != target["steam_id"]
            ):
                row["issues"] = ["IDENTIFIER_MISMATCH"]
            elif any(
                target.get(k) and row.get(k) and target[k] != row[k]
                for k in ("password", "guard")
            ):
                row["issues"] = ["CONFLICTING_DUPLICATE"]
            else:
                target["login"] = target.get("login") or row.get("login")
                target["steam_id"] = target.get("steam_id") or identifier
                target["password"] = target.get("password") or row.get("password")
                target["guard"] = target.get("guard") or row.get("guard")
                target["sources"] = target.get("sources", 1) + row.get("sources", 1)
                if target.get("login"):
                    names[target["login"].lower()] = next(iter(found))
                if target.get("steam_id"):
                    ids[target["steam_id"]] = next(iter(found))
                continue
        index = len(result)
        row["row_id"] = str(index)
        result.append(row)
        if not row["issues"]:
            if key:
                names[key] = index
            if identifier:
                ids[identifier] = index
    if len(result) > MAX_ROWS:
        raise ImportProblem("RECORD_COUNT_LIMIT")
    return result
