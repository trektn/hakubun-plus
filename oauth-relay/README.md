# mal-oauth-relay

A tiny Cloudflare Worker that acts as MyAnimeList's OAuth redirect target.

## Why this exists

MyAnimeList's OAuth app config requires a public HTTPS `redirect_uri` --
`http://localhost:...` is not accepted. Hakubun+ already supports pasting an
authorization code manually into a "PIN" field when adding a MAL account,
so this Worker doesn't need to talk to the app at all: MAL redirects here
with the code, the Worker renders it on a plain page, and the user copies
it into Hakubun+ themselves. It's stateless -- no code, token, or client
secret is ever stored or logged here. The actual PKCE token exchange still
happens entirely inside the app.

## Behavior

`GET /mal/callback?code=<code>`

- `code` present -> `200` HTML page showing the code with a copy button
- `error` present (MAL denied the request / user cancelled) -> `200` HTML
  page showing the error (and `error_description` if MAL sent one)
- Neither present -> `400`
- Any other path, or a non-GET request -> `404`

Query values are HTML-escaped before being placed on the page (a crafted
link to this endpoint is otherwise a reflected-XSS vector), and are
length-capped so a malformed request can't render something absurd.

## Deploy

This deploys to `oauth.poopf.art` (already on Cloudflare), configured via
the `routes` entry in `wrangler.toml`. Adjust that hostname first if you
want a different one, or delete the `routes` block to fall back to the
default `mal-oauth-relay.<your-subdomain>.workers.dev`, which is also
HTTPS by default and needs no zone/DNS setup.

```sh
npm install
npx wrangler login   # first time only -- opens a browser to authorize wrangler
npm run deploy
```

`wrangler deploy` provisions the DNS record for `oauth.poopf.art`
automatically (as a proxied Worker custom domain) the first time it runs,
as long as `poopf.art`'s zone is in the same Cloudflare account.

Then, in MyAnimeList's API config (https://myanimelist.net/apiconfig),
set the app's **Redirect URL** to:

```
https://oauth.poopf.art/mal/callback
```

This has to be done manually in the MAL dashboard -- it's a client-config
setting on MAL's side, not something this repo can set for you. Hakubun+'s
current authorize request doesn't send a `redirect_uri` param, so MAL falls
back to whatever's registered for the app; as long as this is the only
redirect URL configured there, no app-side code change is needed for it
to take effect.

## Local testing

```sh
npm install
npm run dev
```

```sh
# code page
curl -si "http://localhost:8787/mal/callback?code=abc123" | head -5

# error page (MAL denied / user cancelled)
curl -si "http://localhost:8787/mal/callback?error=access_denied&error_description=User+denied" | head -5

# 400 -- no code, no error
curl -si "http://localhost:8787/mal/callback" | head -5

# 404 -- unknown path
curl -si "http://localhost:8787/" | head -5
```
