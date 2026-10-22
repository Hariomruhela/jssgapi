"""Call the trustee listing from the command line.

The trustee directory is derived from the members' group designation: every
member whose designation is filled in is a trustee, and a member without one is
a normal member. This script logs in through the real endpoint and prints the
listing, so the API can be checked without opening the panel.

Usage:
    python -m scripts.trustee_directory --phone +919088776655
    python -m scripts.trustee_directory --phone +919088776655 --password secret
    python -m scripts.trustee_directory --phone +919088776655 --designation President

The base URL defaults to the deployed API and can be pointed at a local server
with ``--base-url http://127.0.0.1:8099/api/v1``.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def _request(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    payload: dict | None = None,
) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        raise SystemExit(f"{method} {url} -> {error.code}\n{body}") from error
    except urllib.error.URLError as error:
        raise SystemExit(f"{method} {url} -> {error.reason}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://jssgapi.vercel.app/api/v1")
    parser.add_argument("--phone", default="", help="Login phone number")
    parser.add_argument("--password", default="", help="Login password")
    parser.add_argument(
        "--token",
        default="",
        help="Use this access token instead of logging in with a phone number",
    )
    parser.add_argument("--designation", default="", help="Only this designation")
    parser.add_argument("--group-id", default="", help="Only this group")
    parser.add_argument("--query", default="", help="Search the name")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--json", action="store_true", help="Print the raw JSON")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    if args.token:
        token = args.token
    else:
        if not args.phone:
            raise SystemExit("either --phone or --token is required")
        login = _request(
            f"{base}/auth/login",
            method="POST",
            payload={"phone_number": args.phone, "password": args.password},
        )
        token = login["tokens"]["access_token"]

    query = f"?page_size={args.page_size}"
    if args.designation:
        query += f"&designation={urllib.parse.quote(args.designation)}"
    if args.group_id:
        query += f"&group_id={urllib.parse.quote(args.group_id)}"
    if args.query:
        query += f"&query={urllib.parse.quote(args.query)}"
    listing = _request(f"{base}/trustees/directory{query}", token=token)

    if args.json:
        print(json.dumps(listing, indent=2, ensure_ascii=False))
        return 0

    pagination = listing.get("pagination", {})
    print(
        f"{pagination.get('total', 0)} trustee(s), "
        f"page {pagination.get('page', 1)}/{pagination.get('total_pages', 1)}"
    )
    for item in listing.get("data", []):
        print(
            f"  {item['full_name']:<32} {item['designation']:<20} "
            f"{item.get('group_name') or '-':<32} {item.get('city') or '-':<16} "
            f"{item['id']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
