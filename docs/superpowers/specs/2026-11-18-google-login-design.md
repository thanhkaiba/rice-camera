# Google login and portal access

## Agreed scope

Add Google login to the existing Flask portal with exactly two access roles:
`admin` and `viewer`. Every active employee defaults to viewer, using their
existing employee email. The user chose to manage portal roles on the Employee screen;
do not maintain a second email list. Reuse the existing admin/viewer route restrictions and page controls.
Employee attendance categories (`employee` and `support`) remain separate.

## Approaches

- Employee-screen access management (chosen): admins assign admin/viewer directly
  while editing an existing employee, without entering email again. Google
  client configuration stays in Settings.
- File-managed access: smaller UI but requires configuration edits for each change.

Use Google OpenID Connect on the server, building on Flask sessions and the
existing store. Keep the existing Basic login as a recovery path; replacing it
was not requested. This compatibility choice is proposed by the implementer.

## Proposed behavior

- Settings → Access contains Google client ID/secret configuration, the exact
  callback URL to register in Google Cloud and an enable switch.
  Secrets are masked and excluded from diagnostics and camera sync.
- The Employee edit screen contains a separate portal access selector with
  exactly admin/viewer, editable only by an admin. Keep it visually distinct
  from the existing employee/support attendance category. No role editor or
  duplicate employee list is added in Settings.
- An admin uses the existing login to configure Google and designate at least one
  Google admin before enabling it. Enabling Google requires a configured admin
  password for the existing recovery login. Anonymous admin access is disabled
  while Google login is enabled, including when configuration is incomplete.
- Unauthenticated browser visits show a login page with “Sign in with Google”
  and the existing password-login option. Authenticated machine APIs retain
  their own credentials and response behavior.
- Match the verified Google email to an existing active employee; all such
  employees have viewer access unless promoted to admin on the Employee screen. Unknown
  emails and inactive employees cannot log in. Do not create employee records
  during login, grant access to an entire domain, or promote the first person to admin. Only accept
  verified Gmail/Google Workspace email identities for email-based provisioning;
  bind the employee to Google's stable subject ID on their first successful login.
- Admin retains existing full portal access. Viewer retains the current allowed
  pages/downloads and cannot change data, access Settings or manage users.
- Read the current employee status, email and portal role on each request so
  disabling/deleting an employee, changing their email or demoting an admin
  takes effect on the next request. Demoting an admin returns them to viewer;
  disable their employee record to remove portal access. Do not allow disabling,
  deleting or demoting the last Google admin while Google login is enabled.
  An employee email change clears the Google binding and invalidates existing
  sessions for that employee. Record access changes and the acting
  email in the existing audit log.
- Sessions expire after eight hours. Logout clears the portal session. Google
  being unavailable prevents new Google logins but leaves recovery login usable.
- Google Chat's existing connection is independent: use a separate web OAuth
  client configuration and callback for portal login.

## Security and storage

Use authorization-code OIDC with short-lived, single-use state/nonce and PKCE.
Validate Google token signature, issuer, audience, expiry, nonce and verified
email; use a maintained verification library rather than custom JWT crypto.
Use bounded network timeouts, safe local redirect targets, CSRF protection for
session-authenticated writes and logout, and HttpOnly/SameSite cookies with
Secure enabled on HTTPS deployments. OAuth errors show useful safe messages
without exposing tokens or the client secret.

Store credentials, Google bindings and employee portal roles in the existing
server database; default existing and newly added employees to viewer. Keep
portal roles distinct from employee attendance categories. Do not
send credentials, Google bindings, portal roles or sessions to the Mac through settings sync,
configuration export or diagnostics. Standalone camera mode keeps working with
the existing login and may configure its own Google client. No camera jobs,
inference, model loading or inbound Mac connection is needed.

Google reference: https://developers.google.com/identity/openid-connect/openid-connect

## Change surface and verification

Update the shared web guard/current-user helper, add login/callback/logout and
admin-only employee access updates, extend the Employee screen, Google settings
and navigation, add store helpers
and employee role/binding persistence with a backwards-compatible migration,
and document Google Cloud setup. Inspect settings export, diagnostics and edge
sync paths to prevent credential leakage. Keep existing API/edge-token guards,
signed personal links and Basic credentials working.

Test allowed/denied identities, invalid/expired/replayed OAuth state and tokens,
role enforcement on direct URLs and writes, existing/new employee viewer defaults,
inactive/unknown employee rejection, changed-email session invalidation,
immediate access revocation, last
admin protection, CSRF, logout/expiry and existing authentication modes. Mock
Google in tests; a real end-to-end sign-in requires the deployment URL and
Google OAuth credentials supplied through Settings.

No custom roles, employee self-service, user invitations or domain-wide access.
Library choice and detailed code organization remain implementation decisions.
