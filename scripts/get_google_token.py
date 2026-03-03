"""
One-time helper to get a Google OAuth2 refresh token for Drive uploads.

Prerequisites:
  1. Google Cloud Console → APIs & Services → Credentials
  2. Create OAuth2 Client ID → Application type: Desktop app → Download JSON
  3. Add to .env:
       GOOGLE_OAUTH_CLIENT_ID=<your-client-id>
       GOOGLE_OAUTH_CLIENT_SECRET=<your-client-secret>

Then run:
  python3 scripts/get_google_token.py

It will print a URL. Open it, authorize with your Google account, copy the
code from the redirect, paste it back. The refresh token is then written
to .env as GOOGLE_DRIVE_REFRESH_TOKEN.

Run from repo root.
"""
import urllib.request, urllib.parse, json, re, webbrowser

def load_env(path="./.env"):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

def set_env_var(path, key, value):
    """Add or replace a key=value line in the .env file."""
    with open(path) as f:
        content = f.read()
    pattern = rf'^{re.escape(key)}=.*$'
    replacement = f'{key}={value}'
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
    else:
        content = content.rstrip('\n') + f'\n{replacement}\n'
    with open(path, 'w') as f:
        f.write(content)

env = load_env()
client_id     = env.get("GOOGLE_OAUTH_CLIENT_ID", "")
client_secret = env.get("GOOGLE_OAUTH_CLIENT_SECRET", "")

if not client_id or not client_secret:
    print("✗ GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET must be set in .env")
    raise SystemExit(1)

SCOPE        = "https://www.googleapis.com/auth/drive.file"
REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"  # copy/paste flow

auth_url = (
    "https://accounts.google.com/o/oauth2/v2/auth?"
    + urllib.parse.urlencode({
        "client_id":     client_id,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         SCOPE,
        "access_type":   "offline",
        "prompt":        "consent",   # force refresh_token to be returned
    })
)

print("Open this URL in your browser and authorise with the Google account")
print("that owns the Drive folder:\n")
print(f"  {auth_url}\n")
try:
    webbrowser.open(auth_url)
except Exception:
    pass

code = input("Paste the authorisation code here: ").strip()

# Exchange code for tokens
token_data = urllib.parse.urlencode({
    "code":          code,
    "client_id":     client_id,
    "client_secret": client_secret,
    "redirect_uri":  REDIRECT_URI,
    "grant_type":    "authorization_code",
}).encode()

req = urllib.request.Request(
    "https://oauth2.googleapis.com/token",
    data=token_data,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    method="POST"
)
with urllib.request.urlopen(req) as r:
    tokens = json.loads(r.read())

if "error" in tokens:
    print(f"✗ Token exchange failed: {tokens['error']} — {tokens.get('error_description', '')}")
    raise SystemExit(1)

refresh_token = tokens.get("refresh_token")
if not refresh_token:
    print("✗ No refresh_token in response — try revoking access at")
    print("  https://myaccount.google.com/permissions and re-running this script")
    raise SystemExit(1)

set_env_var("./.env", "GOOGLE_DRIVE_REFRESH_TOKEN", refresh_token)
print(f"\n✓ Refresh token saved to .env as GOOGLE_DRIVE_REFRESH_TOKEN")
print("  Next: docker compose up -d && python3 scripts/patch_google_drive.py")
