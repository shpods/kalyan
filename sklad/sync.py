"""Синхронизация сайта с МоимСкладом.

Запускается GitHub Actions по расписанию. Токен берётся из секрета MOYSKLAD_TOKEN
и никуда не записывается. Результат:
  stock-auto.txt    — вкусы табака с нулевым остатком (сайт помечает «нет в наличии»)
  hookahs-auto.txt  — кальяны в наличии: Название | Цена | фото | Описание
  foto/auto/*.jpg   — фото кальянов из МоегоСклада
  sklad-report.txt  — что не удалось сопоставить (без количеств)
"""
import gzip, json, os, re, sys, time, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.moysklad.ru/api/remap/1.2"
CFG = json.loads((ROOT / "sklad" / "config.json").read_text(encoding="utf-8"))
TOKEN = os.environ.get("MOYSKLAD_TOKEN", "").strip()


# ---------- запросы ----------
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

def call(url, raw=False, follow=True):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + TOKEN,
        "Accept": "application/json;charset=utf-8",
        "Accept-Encoding": "gzip",
        "User-Agent": "shpods-site-sync",
    })
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
    for attempt in range(4):
        try:
            with opener.open(req, timeout=60) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data if raw else json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if not follow and e.code in (301, 302, 303, 307, 308):
                return e.headers.get("Location")
            if e.code == 429 and attempt < 3:
                time.sleep(2 + attempt * 2); continue
            body = e.read()
            try: body = gzip.decompress(body)
            except Exception: pass
            raise SystemExit(f"МойСклад ответил {e.code}: {body[:300].decode('utf-8', 'replace')}")

def stock_rows():
    rows, offset = [], 0
    while True:
        d = call(f"{API}/report/stock/all?limit=1000&offset={offset}&filter=stockMode%3Dall")
        part = d.get("rows", [])
        rows += part
        if len(part) < 1000: return rows
        offset += 1000


# ---------- сопоставление ----------
WEIGHT = re.compile(r"\b\d+\s*(?:г|гр|g|gr|грамм|ml|мл)\b\.?")
def norm(s):
    s = (s or "").lower().replace("ё", "е")
    s = s.replace("nаш", "наш")  # латинская N в названии бренда
    s = WEIGHT.sub(" ", s)
    s = re.sub(r"[^a-zа-я0-9]+", " ", s)
    return " ".join(s.split())

def tokens(s): return norm(s).split()

def has_all(need, have):
    hs = set(have)
    return all(t in hs or (len(t) > 4 and any(h.startswith(t) for h in have)) for t in need)

def brand_of(name):
    n = " " + norm(name) + " "
    for brand, aliases in CFG["brands"].items():
        if any(" " + norm(a) + " " in n for a in aliases):
            return brand
    return None

def folder_text(r):
    f = r.get("folder") or {}
    return norm((f.get("pathName") or "") + " " + (f.get("name") or ""))

def in_folders(r, keys):
    ft = folder_text(r)
    return any(norm(k) in ft for k in keys)


def main():
    if not TOKEN:
        sys.exit("Нет секрета MOYSKLAD_TOKEN: добавьте его в Settings → Secrets and variables → Actions")
    rows = stock_rows()
    flavors = json.loads((ROOT / "flavors.json").read_text(encoding="utf-8"))
    by_brand = {}
    for f in flavors:
        by_brand.setdefault(f["brand"], []).append(f)

    # табак: каждый товар относим к самому точному вкусу своего бренда
    status = {}          # (brand, name) -> True есть / False нет
    unmatched_products = []
    for r in rows:
        name = r.get("name", "")
        if in_folders(r, CFG["hookah_folders"]) or in_folders(r, CFG["ignore_folders"]):
            continue
        brand = brand_of(name)
        if not brand:
            continue
        pt = tokens(name)
        best, best_len = None, 0
        for f in by_brand.get(brand, []):
            ft = tokens(f["name"])
            if ft and has_all(ft, pt) and len(ft) > best_len:
                best, best_len = f, len(ft)
        if not best:
            unmatched_products.append(name); continue
        key = (best["brand"], best["name"])
        status[key] = status.get(key, False) or (r.get("stock") or 0) > 0

    out = sorted(k for k, v in status.items() if not v)
    lines = ["# Создаётся автоматически из МоегоСклада. Не редактируйте: изменения затрутся.",
             "# Ручной стоп-лист — в файле stock.txt"]
    lines += [f"{b}: {n}" for b, n in out]
    (ROOT / "stock-auto.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # кальяны
    photo_dir = ROOT / "foto" / "auto"; photo_dir.mkdir(parents=True, exist_ok=True)
    keep, hl = set(), ["# Создаётся автоматически из МоегоСклада. Свои кальяны добавляйте в hookahs.txt"]
    for r in sorted(rows, key=lambda x: x.get("name", "")):
        if not in_folders(r, CFG["hookah_folders"]) or (r.get("stock") or 0) <= 0:
            continue
        name = re.sub(r"\s+", " ", r.get("name", "")).replace("|", "/").strip()
        price = int(round((r.get("salePrice") or 0) / 100))
        href = (r.get("meta") or {}).get("href", "").split("?")[0]
        pid = href.rstrip("/").split("/")[-1] if href else ""
        photo = ""
        if pid and CFG.get("download_photos", True):
            fn = f"{pid}.jpg"; fp = photo_dir / fn
            if not fp.exists():
                try:
                    imgs = call(href + "/images?limit=1").get("rows", [])
                    if imgs:
                        dl = (imgs[0].get("meta") or {}).get("downloadHref")
                        loc = call(dl, follow=False) if dl else None
                        if loc:
                            with urllib.request.urlopen(loc, timeout=60) as resp:
                                fp.write_bytes(resp.read())
                except SystemExit as e:
                    print("Фото не скачалось:", name, e)
            if fp.exists():
                photo = "auto/" + fn; keep.add(fn)
        desc = (r.get("folder") or {}).get("name", "")
        hl.append(f"{name} | {price or ''} | {photo} | {desc}")
    for old in photo_dir.glob("*.jpg"):
        if old.name not in keep: old.unlink()
    (ROOT / "hookahs-auto.txt").write_text("\n".join(hl) + "\n", encoding="utf-8")

    # отчёт без количеств и цен
    found = {k for k in status}
    missing = [f"{f['brand']}: {f['name']}" for f in flavors if (f["brand"], f["name"]) not in found]
    rep = [f"Обновлено: {time.strftime('%Y-%m-%d %H:%M')} UTC",
           f"Товаров в МоёмСкладе: {len(rows)}",
           f"Вкусов сайта найдено на складе: {len(found)} из {len(flavors)}",
           f"Из них нет в наличии: {len(out)}",
           f"Кальянов в наличии: {len(hl) - 1}", "",
           "Товары табака, которые не удалось сопоставить с сайтом:"] + [" - " + n for n in sorted(set(unmatched_products))] + \
          ["", "Вкусы сайта, которых нет в МоёмСкладе (возможно, иначе названы):"] + [" - " + m for m in missing]
    (ROOT / "sklad-report.txt").write_text("\n".join(rep) + "\n", encoding="utf-8")
    print("\n".join(rep[:6]))


if __name__ == "__main__":
    main()
