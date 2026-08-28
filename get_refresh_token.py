"""
ONE-TIME setup script. Run this once, on your own laptop (needs a browser).
This is the one step Google requires a human for -- it cannot be automated,
because Google will not let anyone grant upload access to your channel except you.

Steps:
1. Go to https://console.cloud.google.com/apis/credentials
2. Create a new project (any name).
3. Enable the "YouTube Data API v3" (APIs & Services -> Library -> search it -> Enable).
4. Credentials -> Create Credentials -> OAuth client ID -> Application type: Desktop app.
5. Download the JSON, save it next to this script as client_secret.json
6. pip install google-auth-oauthlib
7. Run: python get_refresh_token.py
8. A browser window opens -- log in with the Google account that owns your channel, approve access.
9. This prints three values. Copy them into your GitHub repo's
   Settings -> Secrets and variables -> Actions -> New repository secret:
     YT_CLIENT_ID
     YT_CLIENT_SECRET
     YT_REFRESH_TOKEN
   (along with ELEVENLABS_API_KEY and PEXELS_API_KEY from SETUP.md)

After this, the pipeline uploads to your channel forever without asking you again.
"""
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
creds = flow.run_local_server(port=0)

print("\nSave these as GitHub repo secrets:\n")
print("YT_REFRESH_TOKEN =", creds.refresh_token)
print("YT_CLIENT_ID =", creds.client_id)
print("YT_CLIENT_SECRET =", creds.client_secret)
