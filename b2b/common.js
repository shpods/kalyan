// Общие функции оптовой страницы и админки shpods
const SH = {
  REPO: "shpods/kalyan",
  BRANCH: "main",
  ITER: 150000,
  enc: new TextEncoder(),
  dec: new TextDecoder(),

  b64(buf) { let s = ""; new Uint8Array(buf).forEach(b => s += String.fromCharCode(b)); return btoa(s); },
  unb64(str) { return Uint8Array.from(atob(str), c => c.charCodeAt(0)); },
  utf8b64(text) { return SH.b64(SH.enc.encode(text)); },
  b64utf8(b64) { return SH.dec.decode(SH.unb64(b64.replace(/\s/g, ""))); },

  async key(pass, salt) {
    const base = await crypto.subtle.importKey("raw", SH.enc.encode(pass), "PBKDF2", false, ["deriveKey"]);
    return crypto.subtle.deriveKey({ name: "PBKDF2", salt, iterations: SH.ITER, hash: "SHA-256" }, base, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
  },
  async encrypt(pass, obj) {
    const salt = crypto.getRandomValues(new Uint8Array(16)), iv = crypto.getRandomValues(new Uint8Array(12));
    const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, await SH.key(pass, salt), SH.enc.encode(JSON.stringify(obj)));
    return { s: SH.b64(salt), i: SH.b64(iv), c: SH.b64(ct) };
  },
  async decrypt(pass, box) {
    const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: SH.unb64(box.i) }, await SH.key(pass, SH.unb64(box.s)), SH.unb64(box.c));
    return JSON.parse(SH.dec.decode(pt));
  },
  // один ключ на много записей (имена клиентов): соль в файле, IV на каждую запись
  async keyFor(pass, saltB64) { return SH.key(pass, SH.unb64(saltB64)); },
  async encWith(key, obj) {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, SH.enc.encode(JSON.stringify(obj)));
    return { i: SH.b64(iv), c: SH.b64(ct) };
  },
  async decWith(key, box) {
    const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: SH.unb64(box.i) }, key, SH.unb64(box.c));
    return JSON.parse(SH.dec.decode(pt));
  },
  today() { return new Date().toISOString().slice(0, 10); },
  async hash(code) {
    const d = await crypto.subtle.digest("SHA-256", SH.enc.encode("shpods-bonus:" + SH.normCode(code)));
    return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, "0")).join("");
  },
  normCode(c) { return String(c || "").toUpperCase().replace(/[^A-Z0-9]/g, ""); },
  newCode() {
    const A = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789", r = crypto.getRandomValues(new Uint8Array(8));
    const s = [...r].map(x => A[x % A.length]).join("");
    return "SH-" + s.slice(0, 4) + "-" + s.slice(4);
  },
  money(n) { return Math.round(Number(n) || 0).toLocaleString("ru-RU").replace(/,/g, " ") + " ₸"; },
  esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); },
  store: {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
    del(k) { try { localStorage.removeItem(k); } catch (e) {} },
  },

  // Чтение публичных файлов (без токена). raw.githubusercontent обновляется ~5 минут
  // берём оба источника и выбираем самый свежий по полю u
  async readPublic(path) {
    const t = Date.now();
    const tries = [`../${path}?t=${t}`, `https://raw.githubusercontent.com/${SH.REPO}/${SH.BRANCH}/${path}?t=${t}`];
    const res = await Promise.all(tries.map(u => fetch(u, { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null)));
    return res.filter(Boolean).sort((a, b) => (b.u || 0) - (a.u || 0))[0] || null;
  },

  // GitHub API для админки
  gh: {
    token: null,
    async req(method, path, body) {
      const r = await fetch(`https://api.github.com/repos/${SH.REPO}/contents/${path}` + (method === "GET" ? `?ref=${SH.BRANCH}&t=${Date.now()}` : ""), {
        method, cache: "no-store",
        headers: { "Authorization": "Bearer " + SH.gh.token, "Accept": "application/vnd.github+json", ...(body ? { "Content-Type": "application/json" } : {}) },
        body: body ? JSON.stringify(body) : undefined,
      });
      if (r.status === 404 && method === "GET") return null;
      if (!r.ok) { const t = await r.text(); const e = new Error(r.status + " " + t.slice(0, 200)); e.status = r.status; throw e; }
      return r.json();
    },
    async read(path) {
      const f = await SH.gh.req("GET", path);
      if (!f) return { data: null, sha: null };
      return { data: JSON.parse(SH.b64utf8(f.content)), sha: f.sha };
    },
    async write(path, data, sha, message) {
      const body = { message, branch: SH.BRANCH, content: SH.utf8b64(JSON.stringify(data, null, 1)) };
      if (sha) body.sha = sha;
      const r = await SH.gh.req("PUT", path, body);
      return r.content.sha;
    },
    // прочитать → изменить → записать; если файл успели изменить с другого телефона, повторить
    async mutate(path, fn, message) {
      for (let k = 0; k < 4; k++) {
        const { data, sha } = await SH.gh.read(path);
        const nd = await fn(data);
        nd.u = Date.now();
        try { await SH.gh.write(path, nd, sha, message); return nd; }
        catch (e) { if (e.status === 409 || e.status === 422) continue; throw e; }
      }
      throw new Error("Файл одновременно меняли с другого устройства. Попробуйте ещё раз.");
    },
  },
};
