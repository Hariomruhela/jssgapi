"""MSG91 widget diagnostics.

Run from the repo root so ``.env`` is loaded:

    .venv/bin/python scripts/msg91_send_test.py 9876543210

Step 1 reads the widget config via GET (no SMS is sent).
Step 2 sends a real OTP to the given mobile number.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

import app.integrations.msg91 as msg91


def _tiny_url() -> str:
    return "https://control.msg91.com/api/v5/widget/getWidgetProcess"


async def show_widget_config() -> None:
    print("=== STEP 1: widget config (no SMS sent) ===")
    m = msg91.settings
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        r = await client.get(
            _tiny_url(),
            params={"widgetId": m.msg91_widget_id, "tokenAuth": m.msg91_widget_token},
        )
    print("GET status:", r.status_code)
    body = r.json() if r.text.strip() else None
    if isinstance(body, dict) and body.get("data"):
        data = body["data"]
        print("widget name     :", data.get("name"))
        print("status          :", data.get("status"))
        print("captcha ON      :", data.get("captchaValidations"))
        print("mobileIntegr.   :", data.get("mobileIntegration"))
        print("globalChannel   :", data.get("globalDefaultChannel"))
        for p in data.get("processes", []):
            print(
                "  process src >",
                p.get("processVia", {}).get("name"),
                "chan >",
                p.get("channel", {}).get("name"),
                "defaultTpl >",
                p.get("use_default"),
            )
    else:
        print("RAW:", r.text[:500])


async def send_otp(phone: str) -> None:
    print("\n=== STEP 2: send real OTP via our code path ===")
    normalized = f"+91{phone.lstrip('+91')}"
    try:
        req_id = await msg91.send_otp(normalized)
        print("OK -> req_id:", req_id)
    except Exception as exc:  # noqa: BLE001
        print("FAILED ->", exc)
        sys.exit(1)


async def main() -> None:
    parser = argparse.ArgumentParser(description="MSG91 diagnostics")
    parser.add_argument("phone", help="10-digit Indian mobile number")
    args = parser.parse_args()

    if msg91.direct_mode():
        print("MODE: standard OTP API (authkey + template id) - no widget guard")
    else:
        print(
            "MODE: OTP Widget (tap on captcha/anti-bot; "
            "set MSG91_OTP_TEMPLATE_ID to avoid)"
        )
        await show_widget_config()
    await send_otp(args.phone)


if __name__ == "__main__":
    asyncio.run(main())
