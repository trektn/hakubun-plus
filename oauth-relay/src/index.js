/**
 * MyAnimeList OAuth redirect relay.
 *
 * MAL requires a public HTTPS redirect_uri and rejects "localhost" -- this
 * Worker is that public endpoint. It does nothing but forward the
 * authorization code (or an error) from MAL's redirect back to the desktop
 * app via a hardcoded custom URI scheme. It never sees a client secret,
 * never performs the token exchange, and never stores anything; the app
 * does the actual PKCE exchange itself once it receives the code.
 */

// Hardcoded, not derived from any request input -- this is what prevents
// the endpoint from being an open redirect. Only the query values below
// (code/state/error/error_description) are attacker-influenced, and they
// only ever end up as query *parameter values* on this fixed destination,
// never as the destination itself.
const APP_CALLBACK_BASE = 'hakubun://oauth/mal';

const CALLBACK_PATH = '/mal/callback';

// Generous but bounded -- MAL's codes and our own state values are short,
// this just keeps a malformed/abusive request from producing an
// oversized Location header.
const MAX_PARAM_LENGTH = 2048;

function isValidParam(value) {
  return typeof value === 'string' && value.length > 0 && value.length <= MAX_PARAM_LENGTH;
}

function buildRelayUrl(params) {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) qs.set(key, value);
  }
  const query = qs.toString();
  return query ? `${APP_CALLBACK_BASE}?${query}` : APP_CALLBACK_BASE;
}

function plainText(status, body) {
  return new Response(body, {
    status,
    headers: {
      'Content-Type': 'text/plain; charset=utf-8',
      // Nothing here should ever be cached or leaked via a referrer --
      // the URL itself carries a one-time authorization code.
      'Cache-Control': 'no-store',
      'Referrer-Policy': 'no-referrer',
      'X-Content-Type-Options': 'nosniff',
    },
  });
}

function redirectToApp(destination) {
  // Built manually (rather than via Response.redirect()) so behavior
  // doesn't depend on a given runtime's willingness to validate/construct
  // a Response around a non-http(s) URL -- a raw Location header works
  // the same everywhere. URLSearchParams already percent-encodes every
  // value, so nothing here can inject extra headers via CR/LF.
  return new Response(null, {
    status: 302,
    headers: {
      Location: destination,
      'Cache-Control': 'no-store',
      'Referrer-Policy': 'no-referrer',
      'X-Content-Type-Options': 'nosniff',
    },
  });
}

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (url.pathname !== CALLBACK_PATH || request.method !== 'GET') {
      return plainText(404, 'Not found');
    }

    const params = url.searchParams;
    const code = params.get('code');
    const state = params.get('state');
    const error = params.get('error');
    const errorDescription = params.get('error_description');

    // MAL denied the request (or the user cancelled) -- forward that to
    // the app instead of failing here; the desktop app is in a better
    // position to show the user something meaningful than this relay is.
    if (isValidParam(error)) {
      return redirectToApp(buildRelayUrl({
        error,
        error_description: isValidParam(errorDescription) ? errorDescription : undefined,
        state: isValidParam(state) ? state : undefined,
      }));
    }

    if (!isValidParam(code)) {
      return plainText(400, 'Missing required "code" parameter.');
    }

    return redirectToApp(buildRelayUrl({
      code,
      state: isValidParam(state) ? state : undefined,
    }));
  },
};
