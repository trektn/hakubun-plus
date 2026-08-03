/**
 * MyAnimeList OAuth redirect landing page.
 *
 * MAL requires a public HTTPS redirect_uri and rejects "localhost" -- this
 * Worker is that public endpoint. It does nothing but show the user the
 * authorization code MAL just issued (or an error, if they denied access),
 * so they can copy it into Hakubun+'s existing "PIN" field -- the app
 * already supports pasting a code in manually, it just needed somewhere
 * public and HTTPS for MAL to land the user after they approve. The actual
 * PKCE token exchange still happens entirely client-side in the app; this
 * Worker never sees a client secret and never stores anything.
 */

const CALLBACK_PATH = '/mal/callback';

// Generous but bounded -- MAL's codes are short, this just keeps a
// malformed/abusive request from rendering something absurd.
const MAX_PARAM_LENGTH = 2048;

function isValidParam(value) {
  return typeof value === 'string' && value.length > 0 && value.length <= MAX_PARAM_LENGTH;
}

// Query values are attacker-influenced (a phishing link could point here
// with a crafted code/error), so they're only ever inserted into the page
// through this -- never via raw string interpolation into the HTML.
function escapeHtml(value) {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function page(title, bodyHtml, status) {
  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>${escapeHtml(title)}</title>
<style>
  :root { color-scheme: light dark; }
  body {
    font-family: system-ui, sans-serif;
    max-width: 32rem;
    margin: 4rem auto;
    padding: 0 1.5rem;
    line-height: 1.5;
  }
  h1 { font-size: 1.25rem; }
  .code-box {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    margin: 1.5rem 0;
  }
  code {
    flex: 1;
    display: block;
    font-family: ui-monospace, monospace;
    font-size: 1rem;
    padding: 0.75rem;
    border: 1px solid light-dark(#ccc, #444);
    border-radius: 0.375rem;
    background: light-dark(#f6f6f6, #1e1e1e);
    overflow-wrap: anywhere;
    user-select: all;
  }
  button {
    font: inherit;
    padding: 0.75rem 1rem;
    border-radius: 0.375rem;
    border: 1px solid light-dark(#ccc, #444);
    background: light-dark(#fff, #2a2a2a);
    cursor: pointer;
  }
  button:active { transform: translateY(1px); }
  .error { color: #b3261e; }
</style>
</head>
<body>
${bodyHtml}
</body>
</html>`;

  return new Response(html, {
    status,
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      // The code on this page is one-time and sensitive -- never cache
      // or reveal this URL/page via a referrer header.
      'Cache-Control': 'no-store',
      'Referrer-Policy': 'no-referrer',
      'X-Content-Type-Options': 'nosniff',
    },
  });
}

function codePage(code) {
  const safeCode = escapeHtml(code);
  return page('MyAnimeList authorization code', `
<h1>Copy this code into Hakubun+</h1>
<p>Paste it into the "PIN" field where you added your MyAnimeList account.</p>
<div class="code-box">
  <code id="code">${safeCode}</code>
  <button type="button" onclick="navigator.clipboard.writeText(document.getElementById('code').textContent)">Copy</button>
</div>
<p>You can close this page once it's pasted in.</p>
`, 200);
}

function errorPage(error, description) {
  const parts = [`<p class="error"><strong>${escapeHtml(error)}</strong></p>`];
  if (description) parts.push(`<p>${escapeHtml(description)}</p>`);
  return page('MyAnimeList authorization failed', `
<h1>Authorization failed</h1>
${parts.join('\n')}
<p>Go back to Hakubun+ and try adding the account again.</p>
`, 200);
}

function notFound() {
  return page('Not found', '<h1>Not found</h1>', 404);
}

function badRequest(message) {
  return page('Missing parameter', `<h1>Bad request</h1><p>${escapeHtml(message)}</p>`, 400);
}

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (url.pathname !== CALLBACK_PATH || request.method !== 'GET') {
      return notFound();
    }

    const params = url.searchParams;
    const code = params.get('code');
    const error = params.get('error');
    const errorDescription = params.get('error_description');

    // MAL denied the request (or the user cancelled) -- show that instead
    // of a bare "missing code" error.
    if (isValidParam(error)) {
      return errorPage(error, isValidParam(errorDescription) ? errorDescription : null);
    }

    if (!isValidParam(code)) {
      return badRequest('Missing required "code" parameter.');
    }

    return codePage(code);
  },
};
