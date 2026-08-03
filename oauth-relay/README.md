# mal-oauth-relay

A tiny Cloudflare Worker that acts as MyAnimeList's OAuth redirect target.

## Why this exists

MyAnimeList's OAuth app config requires a public HTTPS `redirect_uri` --
`http://localhost:...` is not accepted. Hakubun+ is a desktop app with no
public server of its own, so this Worker is a public, stateless relay:
MAL redirects here with an authorization code, and this Worker immediately
302-redirects the browser to the desktop app via a custom URI scheme
(`hakubun://oauth/mal?...`), which the OS hands off to the app. The actual
token exchange (with PKCE) happens entirely client-side, inside the app --
this Worker never sees a client secret and never stores a code or token.

## Behavior

`GET /mal/callback?code=<code>&state=<state>`

- `code` present -> `302` to `hakubun://oauth/mal?code=<code>&state=<state>`
  (`state` omitted if MAL didn't send one)
- `error` present (MAL denied the request / user cancelled) -> `302` to
  `hakubun://oauth/mal?error=<error>&error_description=<...>&state=<...>`
- Neither `code` nor `error` present -> `400`
- Any other path, or a non-GET request -> `404`

The destination scheme+host+path (`hakubun://oauth/mal`) is a hardcoded
constant in `src/index.js`, never derived from the request -- only the query
*values* are attacker-influenced, so this can't be used as an open redirect
to an arbitrary URL. Param values are also length-capped and percent-encoded
via `URLSearchParams`, so nothing here can inject extra response headers.

## Deploy

```sh
npm install
npx wrangler login   # first time only
npm run deploy
```

This publishes to `https://mal-oauth-relay.<your-subdomain>.workers.dev`,
which is HTTPS by default -- no custom domain is required. If you'd rather
use your own domain, add it under the Worker's Settings -> Domains & Routes
in the Cloudflare dashboard, or uncomment the `routes` entry in
`wrangler.toml`.

Then, in MyAnimeList's API config (https://myanimelist.net/apiconfig),
set the app's redirect URI to:

```
https://mal-oauth-relay.<your-subdomain>.workers.dev/mal/callback
```

## Local testing

```sh
npm install
npm run dev
```

```sh
# 302 with code + state
curl -si "http://localhost:8787/mal/callback?code=abc123&state=xyz" | head -5

# 302 with just code
curl -si "http://localhost:8787/mal/callback?code=abc123" | head -5

# 400 -- no code, no error
curl -si "http://localhost:8787/mal/callback" | head -5

# 302 relaying an MAL-side denial
curl -si "http://localhost:8787/mal/callback?error=access_denied&state=xyz" | head -5

# 404 -- unknown path
curl -si "http://localhost:8787/" | head -5
```

## Out of scope here (app-side follow-up)

This Worker only handles the redirect leg. For the full flow to work end
to end, the desktop app still needs to:

1. Register `hakubun://` as a custom URI scheme handler with the OS
   (a `.desktop` file's `MimeType=x-scheme-handler/hakubun;` on Linux, a
   `CFBundleURLTypes` entry on macOS, a registry key on Windows).
2. Listen for that scheme being invoked and pull `code`/`state` out of it,
   instead of (or alongside) the current copy-paste PIN flow.
3. Send `redirect_uri=https://mal-oauth-relay.<subdomain>.workers.dev/mal/callback`
   as part of the MAL authorize URL, matching whatever's registered in
   MAL's app config.
