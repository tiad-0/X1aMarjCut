// 1.2.52: every POST / PUT / PATCH / DELETE to our own API carries the install's API token
// (backend/sharkyclips/web/security.py). The server puts it into index.html; another website
// can't read it, so it can't make this app do anything. Installed before anything else runs,
// for fetch() and XMLHttpRequest (uploads with progress) alike.

const HEADER = "X-SharkyClips-Token";
const SAFE = new Set(["GET", "HEAD", "OPTIONS"]);

export function apiToken(): string {
  return document.querySelector<HTMLMetaElement>('meta[name="sharkyclips-token"]')?.content ?? "";
}

function sameOrigin(url: string | URL): boolean {
  try {
    return new URL(String(url), location.href).origin === location.origin;
  } catch {
    return false;
  }
}

/** A request the server refused with code "csrf" (the token changed): reload once. */
export function onCsrfRefused() {
  try {
    if (sessionStorage.getItem("sc-csrf-reload")) return;
    sessionStorage.setItem("sc-csrf-reload", "1");
  } catch {
    return;
  }
  location.reload();
}

export function installCsrf() {
  const token = apiToken();
  if (!token) return;
  try {
    sessionStorage.removeItem("sc-csrf-reload");
  } catch {
    /* private mode */
  }
  const origFetch = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : null;
    const method = (init?.method ?? req?.method ?? "GET").toUpperCase();
    const url = req ? req.url : String(input);
    if (SAFE.has(method) || !sameOrigin(url)) return origFetch(input, init);
    const headers = new Headers(init?.headers ?? req?.headers ?? undefined);
    headers.set(HEADER, token);
    return origFetch(input, { ...init, method, headers });
  };
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  const meta = new WeakMap<XMLHttpRequest, boolean>();
  XMLHttpRequest.prototype.open = function (this: XMLHttpRequest, method: string, url: string | URL, ...rest: unknown[]) {
    meta.set(this, !SAFE.has(String(method).toUpperCase()) && sameOrigin(url));
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (open as any).call(this, method, url, ...rest);
  } as typeof XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.send = function (this: XMLHttpRequest, body?: Document | XMLHttpRequestBodyInit | null) {
    if (meta.get(this)) this.setRequestHeader(HEADER, token);
    return send.call(this, body);
  };
}

installCsrf();
