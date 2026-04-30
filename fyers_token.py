"""
Fyers API V3 - Access Token Generator
ORB Option Seller | Balfund Trading Private Limited
APP_ID, SECRET_KEY, CLIENT_ID are patched at runtime by GUI.
"""

import requests
import pyotp
from urllib.parse import parse_qs, urlparse
from fyers_apiv3 import fyersModel

# These are overwritten at runtime by GUI → _apply_credentials()
APP_ID = ""
APP_TYPE = "200"
SECRET_KEY = ""
CLIENT_ID = ""

# These are overwritten at runtime by GUI → _apply_credentials()
FY_ID = ""
TOTP_KEY = ""
PIN = ""
REDIRECT_URI = "https://trade.fyers.in/api-login/redirect-uri/index.html"

BASE = "https://api-t2.fyers.in/vagator/v2"
BASE2 = "https://api-t1.fyers.in/api/v3"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Content-Type": "application/json"
}


def generate_token():
    if not CLIENT_ID or not SECRET_KEY:
        raise RuntimeError("App ID / Secret Key not set. Save credentials in the GUI first.")

    r1 = requests.post(f"{BASE}/send_login_otp", json={"fy_id": FY_ID, "app_id": "2"}, headers=HEADERS)
    assert r1.status_code == 200, f"Send OTP failed: {r1.text}"
    request_key = r1.json()["request_key"]
    print("✓ OTP sent")

    clean_totp = TOTP_KEY.strip().replace(" ", "").replace("-", "").upper()
    padding = len(clean_totp) % 8
    if padding:
        clean_totp += "=" * (8 - padding)
    totp = pyotp.TOTP(clean_totp).now()
    r2 = requests.post(f"{BASE}/verify_otp", json={"request_key": request_key, "otp": totp}, headers=HEADERS)
    assert r2.status_code == 200, f"Verify TOTP failed: {r2.text}"
    request_key_2 = r2.json()["request_key"]
    print("✓ TOTP verified")

    r3 = requests.post(f"{BASE}/verify_pin", json={
        "request_key": request_key_2, "identity_type": "pin", "identifier": PIN
    }, headers=HEADERS)
    assert r3.status_code == 200, f"Verify PIN failed: {r3.text}"
    trade_token = r3.json()["data"]["access_token"]
    print("✓ PIN verified")

    r4 = requests.post(f"{BASE2}/token", json={
        "fyers_id": FY_ID, "app_id": APP_ID, "redirect_uri": REDIRECT_URI,
        "appType": APP_TYPE, "code_challenge": "", "state": "sample_state",
        "scope": "", "nonce": "", "response_type": "code", "create_cookie": True
    }, headers={**HEADERS, "Authorization": f"Bearer {trade_token}"})
    r4_json = r4.json()
    url_key = "Url" if "Url" in r4_json else "url"
    if url_key not in r4_json:
        raise RuntimeError(f"Auth code step failed: {r4_json}")
    auth_code = parse_qs(urlparse(r4_json[url_key]).query)["auth_code"][0]
    print("✓ Auth code received")

    session = fyersModel.SessionModel(
        client_id=CLIENT_ID, secret_key=SECRET_KEY,
        redirect_uri=REDIRECT_URI, response_type="code", grant_type="authorization_code"
    )
    session.set_token(auth_code)
    response = session.generate_token()
    access_token = response["access_token"]
    print("✅ Access Token generated successfully")
    return access_token


if __name__ == "__main__":
    print("Use GUI to set credentials and run strategy.")
