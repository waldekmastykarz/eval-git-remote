#!/usr/bin/env python3
"""Imports profiles, scenarios and criteria into SCOPE through its REST API.

Usage: python3 import_definitions.py --env-file <env file> --owner <email>

Reads AUTHORITY and API_ACCESS_TOKEN from the env file (the same values
the API uses). When the token has expired, gets a new one
from the Azure CLI (`az account get-access-token`). Definitions whose name already exists in SCOPE
are skipped, so the script can be re-run. IDs are written to imported.json.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KINDS = [
    # (folder, API route, id field in the POST response)
    ("profiles", "profiles", "profileId"),
    ("scenarios", "scenarios", "scenarioId"),
    ("criteria", "criteria", "criteriaId"),
]


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def access_token(env: dict[str, str]) -> str:
    """Uses API_ACCESS_TOKEN from the env file, or a fresh token from the Azure CLI once it expires."""
    token = env.get("API_ACCESS_TOKEN", "")
    try:
        payload = token.split(".")[1]
        expires = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
        if expires > time.time() + 300:
            return token
    except (IndexError, KeyError, ValueError):
        pass
    result = subprocess.run(
        ["az", "account", "get-access-token", "--resource", "https://management.core.windows.net/",
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        sys.exit("API_ACCESS_TOKEN is missing or expired and `az account get-access-token` failed. Run `az login`.")
    return result.stdout.strip()


def api_headers(env_file: Path, owner: str) -> tuple[str, dict]:
    env = read_env(env_file)
    authority = env.get("AUTHORITY")
    if not authority:
        sys.exit(f"AUTHORITY must be set in {env_file}")
    return authority, {
        "authorization": f"Bearer {access_token(env)}",
        "x-ms-client-principal-name": owner,
        "content-type": "application/json",
    }


def request(method: str, url: str, headers: dict, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()

    authority, headers = api_headers(args.env_file, args.owner)

    imported_path = ROOT / "imported.json"
    imported = json.loads(imported_path.read_text(encoding="utf-8")) if imported_path.exists() else {}
    imported["authority"] = authority
    failures = 0

    for folder, route, id_field in KINDS:
        status, existing = request("GET", f"{authority}/api/{route}", headers)
        if status != 200:
            sys.exit(f"GET /api/{route} failed with {status}: {existing}")
        items = existing if isinstance(existing, list) else []
        existing_ids = {item.get("name"): item.get("_id") or item.get("id") for item in items}
        section = imported.setdefault(folder, {})

        for file in sorted((ROOT / folder).glob("*.json")):
            definition = json.loads(file.read_text(encoding="utf-8"))
            name = definition["name"]
            if name in existing_ids:
                section[file.stem] = {"name": name, "id": existing_ids[name]}
                print(f"skip    {folder}/{file.name}: already in SCOPE ({existing_ids[name]})")
                continue
            status, response = request("POST", f"{authority}/api/{route}", headers, definition)
            if status == 201 and isinstance(response, dict):
                new_id = response.get(id_field) or next(iter(response.values()), None)
                section[file.stem] = {"name": name, "id": new_id}
                print(f"created {folder}/{file.name}: {new_id}")
                if folder == "profiles" and definition.get("scripts"):
                    # Older API versions silently dropped scripts; without them runs start with no fixture.
                    _, stored = request("GET", f"{authority}/api/{route}/{new_id}", headers)
                    stored_scripts = (stored.get("configuration") or {}).get("scripts") if isinstance(stored, dict) else None
                    if stored_scripts != definition["scripts"]:
                        failures += 1
                        print(f"FAILED  {folder}/{file.name}: created, but the API didn't store its scripts. "
                              "Use an API deployment that persists profile scripts, delete the profile and re-run.")
            else:
                failures += 1
                print(f"FAILED  {folder}/{file.name}: {status} {str(response)[:300]}")

    imported_path.write_text(json.dumps(imported, indent=2) + "\n", encoding="utf-8")
    print(f"IDs written to {imported_path.name}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
