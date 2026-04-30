"""
Fyers API V3 - Automated Login & Trading Script
ORB Option Seller Strategy
Balfund Trading Private Limited
================================================
APP_ID and SECRET_KEY are set from the GUI at runtime.
FY_ID, TOTP_KEY, PIN remain hardcoded.
"""

import json
import requests
import pyotp
import time
from urllib.parse import parse_qs, urlparse
from fyers_apiv3 import fyersModel

# ============================================================
# CREDENTIALS
# ============================================================
# These two are overwritten at runtime by GUI → _apply_credentials()
APP_ID = ""
APP_TYPE = "200"
SECRET_KEY = ""
CLIENT_ID = ""

# These stay hardcoded (user-specific, not client-facing)
FY_ID = ""
TOTP_KEY = ""
PIN = ""

REDIRECT_URI = "https://trade.fyers.in/api-login/redirect-uri/index.html"
APP_ID_TYPE = "2"  # 2 = web login

# ============================================================
# API ENDPOINTS
# ============================================================
BASE_URL = "https://api-t2.fyers.in/vagator/v2"
BASE_URL_2 = "https://api-t1.fyers.in/api/v3"

URL_SEND_LOGIN_OTP = BASE_URL + "/send_login_otp"
URL_VERIFY_TOTP = BASE_URL + "/verify_otp"
URL_VERIFY_PIN = BASE_URL + "/verify_pin"
URL_TOKEN = BASE_URL_2 + "/token"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Content-Type": "application/json"
}


# ============================================================
# LOGIN FUNCTIONS
# ============================================================

def send_login_otp(fy_id, app_id):
    try:
        payload = {"fy_id": fy_id, "app_id": app_id}
        result = requests.post(url=URL_SEND_LOGIN_OTP, json=payload, headers=HEADERS)
        if result.status_code != 200:
            return None, f"HTTP {result.status_code}: {result.text}"
        data = result.json()
        if "request_key" in data:
            return data["request_key"], None
        return None, f"No request_key in response: {data}"
    except Exception as e:
        return None, str(e)


def generate_totp(totp_key):
    try:
        # Clean the key: remove spaces, uppercase, fix padding (base32 requirement)
        clean_key = totp_key.strip().replace(" ", "").replace("-", "").upper()
        # Add base32 padding if needed
        padding = len(clean_key) % 8
        if padding:
            clean_key += "=" * (8 - padding)
        totp = pyotp.TOTP(clean_key).now()
        return totp, None
    except Exception as e:
        return None, str(e)


def verify_totp(request_key, totp):
    try:
        payload = {"request_key": request_key, "otp": totp}
        result = requests.post(url=URL_VERIFY_TOTP, json=payload, headers=HEADERS)
        if result.status_code != 200:
            return None, f"HTTP {result.status_code}: {result.text}"
        data = result.json()
        if "request_key" in data:
            return data["request_key"], None
        return None, f"No request_key: {data}"
    except Exception as e:
        return None, str(e)


def verify_pin(request_key, pin):
    try:
        payload = {"request_key": request_key, "identity_type": "pin", "identifier": pin}
        result = requests.post(url=URL_VERIFY_PIN, json=payload, headers=HEADERS)
        if result.status_code != 200:
            return None, f"HTTP {result.status_code}: {result.text}"
        data = result.json()
        if "data" in data and "access_token" in data["data"]:
            return data["data"]["access_token"], None
        return None, f"No access_token: {data}"
    except Exception as e:
        return None, str(e)


def get_auth_code(fy_id, app_id, redirect_uri, app_type, access_token):
    try:
        payload = {
            "fyers_id": fy_id, "app_id": app_id, "redirect_uri": redirect_uri,
            "appType": app_type, "code_challenge": "", "state": "sample_state",
            "scope": "", "nonce": "", "response_type": "code", "create_cookie": True
        }
        headers = {**HEADERS, "Authorization": f"Bearer {access_token}"}
        result = requests.post(url=URL_TOKEN, json=payload, headers=headers)
        if result.status_code not in [200, 308]:
            return None, f"HTTP {result.status_code}: {result.text}"
        data = result.json()
        url_key = "Url" if "Url" in data else "url" if "url" in data else None
        if url_key:
            url = data[url_key]
            auth_code = parse_qs(urlparse(url).query)["auth_code"][0]
            return auth_code, None
        return None, f"No Url in response: {data}"
    except Exception as e:
        return None, str(e)


def generate_access_token(auth_code):
    try:
        session = fyersModel.SessionModel(
            client_id=CLIENT_ID,
            secret_key=SECRET_KEY,
            redirect_uri=REDIRECT_URI,
            response_type="code",
            grant_type="authorization_code"
        )
        session.set_token(auth_code)
        response = session.generate_token()
        if "access_token" in response:
            return response["access_token"], None
        return None, f"Token Error: {response}"
    except Exception as e:
        return None, str(e)


# ============================================================
# MAIN AUTO-LOGIN FLOW
# ============================================================

def auto_login():
    """Complete automated login flow → returns access_token"""
    print("=" * 60)
    print("  FYERS API V3 - Automated Login")
    print("  ORB Option Seller | Balfund Trading Pvt. Ltd.")
    print("=" * 60)

    if not CLIENT_ID or not SECRET_KEY:
        print("  ✗ App ID / Secret Key not set. Save credentials first.")
        return None

    print(f"\n  Client ID: {CLIENT_ID}")
    print(f"  Fyers ID:  {FY_ID}")

    # Step 1
    print("\n[1/6] Sending login OTP...")
    request_key, err = send_login_otp(FY_ID, APP_ID_TYPE)
    if err:
        print(f"  ✗ FAILED: {err}")
        return None
    print(f"  ✓ Request key received")

    # Step 2
    print("\n[2/6] Generating TOTP...")
    totp, err = generate_totp(TOTP_KEY)
    if err:
        print(f"  ✗ FAILED: {err}")
        return None
    print(f"  ✓ TOTP generated")

    # Step 3
    print("\n[3/6] Verifying TOTP...")
    request_key_2 = None
    for attempt in range(1, 4):
        request_key_2, err = verify_totp(request_key, totp)
        if request_key_2:
            break
        print(f"  Attempt {attempt} failed: {err}")
        time.sleep(1)
        totp, _ = generate_totp(TOTP_KEY)
    if not request_key_2:
        print(f"  ✗ TOTP verification failed after 3 attempts")
        return None
    print(f"  ✓ TOTP verified")

    # Step 4
    print("\n[4/6] Verifying PIN...")
    trade_access_token, err = verify_pin(request_key_2, PIN)
    if err:
        print(f"  ✗ FAILED: {err}")
        return None
    print(f"  ✓ PIN verified")

    # Step 5
    print("\n[5/6] Getting auth code...")
    auth_code, err = get_auth_code(FY_ID, APP_ID, REDIRECT_URI, APP_TYPE, trade_access_token)
    if err:
        print(f"  ✗ FAILED: {err}")
        return None
    print(f"  ✓ Auth code received")

    # Step 6
    print("\n[6/6] Generating API access token...")
    access_token, err = generate_access_token(auth_code)
    if err:
        print(f"  ✗ FAILED: {err}")
        return None

    print("\n" + "=" * 60)
    print("  ✅ LOGIN SUCCESSFUL!")
    print("=" * 60)
    return access_token


def get_fyers_client(access_token=None):
    if access_token is None:
        access_token = auto_login()
        if not access_token:
            return None
    fyers = fyersModel.FyersModel(
        client_id=CLIENT_ID, is_async=False,
        token=access_token, log_path=""
    )
    return fyers


if __name__ == "__main__":
    print("Use GUI to set credentials and run strategy.")
