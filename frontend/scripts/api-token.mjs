// 1.2.52: write requests to SharkyClips need the install's API token (backend/sharkyclips/web/security.py).
// The checks read it the way the UI does: from the <meta> tag in index.html. Pass it only on the
// requests to SharkyClips itself (never as a context-wide header: that would go to other sites too).
export async function apiHeaders(base) {
  const html = await (await fetch(`${base.replace(/\/$/, "")}/`)).text();
  const token = /name="sharkyclips-token" content="([^"]+)"/.exec(html)?.[1] ?? "";
  if (!token) throw new Error("SharkyClips served no API token in index.html");
  return { "X-SharkyClips-Token": token };
}
