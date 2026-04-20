# Engine/defaults/

Default config files that `setup.command` seeds into user-owned locations on
fresh installs. Anything here is committed to the repo and distributed with
every clone.

## Files

- **`google_oauth_client.json`** — OAuth 2.0 credential for Google Calendar.
  It's an **installed application (Desktop)** client, which per Google's own
  documentation is designed to be embedded in distributed source code and is
  not treated as a secret in the cryptographic sense:
  <https://developers.google.com/identity/protocols/oauth2#installed>

  On fresh install, `setup.command` copies this to
  `Engine/.credentials/google_oauth_client.json`. Users who want to point at
  their own Google Cloud project can replace that copy with their own JSON;
  the defaults file isn't read at runtime.

  `Engine/.credentials/` itself stays gitignored (per-user tokens).
