import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

flow = InstalledAppFlow.from_client_secrets_file(
    "credentials.json",
    scopes=SCOPES,
)
credentials = flow.run_local_server(port=0)

Path("token.json").write_text(
    credentials.to_json(),
    encoding="utf-8",
)
print("Created token.json")