# Google login

Active employees can sign in with the email already on their employee record,
using the company's Google accounts. **Sign in with Google** takes the whole page
to Google and back (no popup, so it also works in the app added to an iPhone home
screen). Google returns an ID token; the portal checks it itself (Google's
signature, that it was issued to the configured client ID, the company domain and
a one-time nonce tied to this browser) and then uses its own eight-hour session. No client secret is involved. This is the same
token as in [GOOGLE-SIGNIN.md](GOOGLE-SIGNIN.md), so the OffspringInternal Web
Client ID can be shared.
Every employee starts as **viewer**. Viewers can view Today, Review, Reports,
Accuracy, Audit and permitted evidence/downloads for the workplace; this is not
a personal-data-only role. They cannot change attendance, employees or settings.

Admins have full portal access. On **People**, click the employee's Admin/Viewer
button to open **Portal access**, choose **Admin** or **Viewer** and press
**Save portal access**. The employee/support category
is separate and still controls attendance behavior. No extra email list is needed.

## Initial setup

1. Update the application and install its dependencies (`pip install .`, or
   `pip install -r requirements.txt`). Google token verification uses
   `google-auth[requests]`; installing it does not download inference models.
2. Sign in with the existing admin credentials. In **Settings → Access &
   privacy**, set an admin password if one is not already set, and save.
   Keep this password as the recovery login.
3. Open your employee's page, set Portal access to **Admin** and press **Save portal access**.
4. Ask the OffspringInternal team for their **Web Client ID**, and ask them to
   add the portal's return address to that OAuth client's **Authorized redirect
   URIs**, for example `https://attendance.example.com/auth/google/callback`
   (Settings shows the exact address to send). For trying it on your own computer
   also ask for `http://localhost:8000/auth/google/callback` and open the portal at
   `localhost:8000`, not `127.0.0.1`. Google accepts only HTTPS or localhost: on the
   office Mac's LAN address use password login.
5. In **Settings → Access & privacy → Google login**, enter the client ID and the
   company domain (`offspringdigital.com`). Only Google Workspace accounts of that
   domain may sign in; leave it empty to accept any verified Gmail or Workspace account.
6. Enable Google login and press **Save Google login**.
7. If you use your own Google Cloud project instead, create an OAuth client of
   type **Web application** and add the same return address to its Authorized
   redirect URIs. No client secret is needed.
8. Test **Sign in with Google** in a separate browser/private window. Your
   Google email must match an active employee record. Then verify an ordinary
   employee can view reports and receives HTTP 403 for Settings or writes.

The application's normal **Save settings** and **Save Google login** buttons
save separate forms. Changing working hours or Google Chat settings does not
toggle Google login. Google login configuration stays in the local database;
configuration export and camera sync do not copy it to another deployment.
Full database backups still contain authentication data and must be protected.

## Account changes and recovery

- Changing admin/viewer takes effect on the next request, including open sessions.
- Disabling an employee removes Google access; deleting them removes their Google
  binding. Changing their email or inactive status invalidates existing sessions
  and resets the Google account binding. Reactivation requires a fresh login.
- The first successful login binds the employee to Google's stable account ID.
  A different Google account cannot take over that employee by presenting the
  same email. With a company domain set, only that Workspace domain is accepted;
  without one, Gmail and any Google Workspace identity with a verified email are
  accepted and other Google accounts using third-party email are rejected.
- Sharing a client ID means trusting every site the OffspringInternal team adds to
  it: such a site could obtain a token valid here. Ask them to keep that list short.
- While Google login is enabled, the last active Google admin cannot be demoted,
  disabled or deleted. Designate a replacement admin first.
- Sessions expire eight hours after sign-in. **Sign out** clears the browser
  session. Password sessions also stop working when their password changes.
- If Google is offline or misconfigured, visit `/login` and use **Password login**.
  Existing HTTP Basic clients still work. Google errors show a safe failure
  message. If Google shows `redirect_uri_mismatch` instead of the account chooser,
  the return address shown in Settings is missing from the client's Authorized
  redirect URIs.
- Settings reset and configuration import cannot remove the admin recovery
  password while Google login is enabled.
- Disabling Google login ends Google portal access on the next request; existing
  password authentication remains enabled. No anonymous admin access is allowed
  while Google login is enabled, even if configuration is incomplete.

Authentication belongs to the web server in the two-machine deployment. No Mac
port, camera job or inference model is involved. Standalone installations can
configure their own client in the same way. A real sign-in requires outbound
HTTPS to Google (the server fetches Google's public keys), a registered return
address and a trusted HTTPS deployment.
Google login cannot be configured on the read-only shadow deployment. Signing
in/out still works when an existing deployment switches to shadow mode; the
migration/import APIs continue to require the admin Basic credentials. Admins
can change employee portal roles through the separate Portal access form in
shadow mode; attendance and employee details remain read-only. This does not
enable scheduled integrations or change the shadow/live deployment mode.

Protocol details: [Google OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect)
and [Google server-side token verification](https://developers.google.com/identity/gsi/web/guides/verify-google-id-token).
