# Google Docs export — 3-step setup

The PDF and Excel exports work out of the box. Google Docs export needs OAuth
credentials that only you can generate (Google policy — Anthropic cannot ship
these).

## 1. Create OAuth credentials (~2 minutes, one-time)

1. Go to <https://console.cloud.google.com/apis/credentials>.
2. Create a project (or pick an existing one).
3. Enable the **Google Docs API**: [click here](https://console.cloud.google.com/apis/library/docs.googleapis.com).
4. Configure the OAuth consent screen (User type: **External**, add your email
   as a test user). Only the `.../auth/documents` scope is needed.
5. Create credentials → **OAuth client ID** → application type **Desktop app**.
6. Download the JSON — this is your `credentials.json`.

## 2. Drop the credentials file

Place the downloaded file at:

```
~/.config/financial-models/credentials.json
```

(You can pass a different path via ``export_google_doc(credentials_path=...)``.)

## 3. Export

The first time you click **⬇ Google Doc** in the notebook, a browser tab opens
asking you to authorise the app for your Google account. Once approved, the
token is cached to `~/.config/financial-models/token.json` and subsequent
exports run silently.

The returned URL is an editable Google Doc containing the full analysis (header,
extracted financials, assumptions with rationale, and every model's results).

## Troubleshooting

* **`ModelError: Google OAuth credentials not found`** — `credentials.json`
  is missing at the path above.
* **`invalid_scope`** — enable the Google Docs API (step 1.3).
* **`access_denied`** on the consent screen — add your Google account to the
  OAuth consent screen's **Test users** list.
