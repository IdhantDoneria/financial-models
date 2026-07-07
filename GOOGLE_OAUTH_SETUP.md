# Google OAuth Setup Guide

## The Issue
"Continue with Google" redirected to a blank page at `accounts.google.com/gsi/transform` because
the deployment was sending `Cross-Origin-Opener-Policy: same-origin` on every response (added as
part of general security hardening). That header blocks the `postMessage` handoff the Google
Identity Services popup uses to return your credential to the opener window, so the popup finishes
but nothing comes back — leaving the blank bounce page. `vercel.json` now sends
`same-origin-allow-popups` instead, which keeps the COOP protection everywhere else while allowing
this one popup flow to complete.

## Quick Fix Checklist

### 1. Create a Google OAuth Application

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (e.g., "FINMODELS TERMINAL")
3. Go to **APIs & Services** → **Credentials**
4. Click **Create Credentials** → **OAuth client ID**
5. If prompted, first set up the OAuth consent screen:
   - User type: External
   - App name: FINMODELS TERMINAL
   - User support email: (your email)
   - Developer contact info: (your email)
   - Scopes: Just click "Save and Continue" (no scopes needed for identity)
6. Back to Credentials: Select **OAuth client ID** → **Web application**
7. Name it "FINMODELS Login"
8. **CRITICAL**: Add Authorized JavaScript Origins (every domain the login page is served from):
   ```
   https://financial-models-six.vercel.app
   http://localhost:8124
   http://127.0.0.1:8124
   ```
9. **CRITICAL**: Add Authorized Redirect URIs:
   ```
   https://financial-models-six.vercel.app
   http://localhost:8124
   http://127.0.0.1:8124
   ```
10. Click **Create** and copy the **Client ID**

### 2. Set the Environment Variable in Vercel

1. Go to your [Vercel Project Settings](https://vercel.com/dashboard)
2. Select the **financial-models** project
3. Go to **Settings** → **Environment Variables**
4. Add a new variable:
   - **Name**: `GOOGLE_CLIENT_ID`
   - **Value**: (paste the Client ID from step 1)
   - **Environments**: All (Production, Preview, Development)
5. Click **Save** and redeploy

### 3. Test Locally (Optional)

```bash
GOOGLE_CLIENT_ID=your-client-id-from-step-1 node scripts/dev_auth_server.js 8124
# visit http://localhost:8124/login — click "Continue with Google", should open a popup
```

## Why the Header Matters

- **`ux_mode: "popup"`** (set in `public/assets/auth.js`) tells Google Identity Services to run
  sign-in in a popup rather than a full-page redirect.
- **`Cross-Origin-Opener-Policy: same-origin-allow-popups`** (set in `vercel.json`) is required for
  that popup to hand its result back to the page that opened it. `same-origin` (the previous,
  stricter value) silently blocks this handoff — the popup completes sign-in with Google but can
  never tell your page, so you're stuck looking at Google's own transition page.
- **`GOOGLE_CLIENT_ID`** env var makes `/api/auth-config` return the client id so the login page
  can initialize the library at all.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "GOOGLE SSO NOT CONFIGURED" | `GOOGLE_CLIENT_ID` not set in Vercel | Set it and redeploy |
| Blank page at `gsi/transform` | COOP header is `same-origin` instead of `same-origin-allow-popups` | Already fixed in `vercel.json` — verify it deployed |
| "DID NOT RETURN CREDENTIALS" | OAuth app authorized origins/redirect URIs missing this domain | Add the domain in Google Cloud Console |
| Popup closes without signing in | Popup blocker, or account 2FA/recovery flow interrupted | Try incognito; disable popup blockers |
| "GOOGLE LIBRARY LOAD FAILED" | `accounts.google.com` unreachable (network policy / ad blocker) | Whitelist the domain or use email/guest |

## Verifying On Production

```bash
curl -sI https://financial-models-six.vercel.app/login | grep -i cross-origin-opener-policy
# expect: cross-origin-opener-policy: same-origin-allow-popups
```

## Files Modified
- `vercel.json`: COOP header changed from `same-origin` to `same-origin-allow-popups`
- `public/assets/auth.js`: `ux_mode: "popup"`, load/error diagnostics, `auto_select: false`
