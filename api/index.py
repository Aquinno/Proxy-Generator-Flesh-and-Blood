from flask import Flask, request, jsonify, send_file, render_template_string
import io
import re
import unicodedata
from pathlib import Path

import requests
from PIL import Image, ImageDraw
from bs4 import BeautifulSoup
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.lib.units import mm

app = Flask(__name__)

CARDVAULT_BASE = "https://cardvault.fabtcg.com"
API_BASE = "https://api.cardvault.fabtcg.com/carddb/api/v1"
IMAGE_BASE_LARGE = "https://legendstory-production-s3-public.s3.amazonaws.com/media/cards/large"
CARD_W_MM = 63
CARD_H_MM = 88
# A practical approximation of a standard TCG corner: about 1/8 inch.
CARD_CORNER_RADIUS_MM = 3.0  # Aproximação de canto arredondado padrão de cartas TCG (~3 mm)
COLS = 3
ROWS = 3
CARDS_PER_PAGE = 9
REQUEST_TIMEOUT = 25

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{CARDVAULT_BASE}/",
})

PAGE_CACHE = {}
IMAGE_CACHE = {}

# Fallback names when Card Vault gives us only the set code.
# This covers the normal/core FAB sets plus the special products seen in the database.
SET_NAMES = {
    "WTR": "Welcome to Rathe",
    "ARC": "Arcane Rising",
    "CRU": "Crucible of War",
    "MON": "Monarch",
    "ELE": "Tales of Aria",
    "EVR": "Everfest",
    "UPR": "Uprising",
    "DYN": "Dynasty",
    "OUT": "Outsiders",
    "EVO": "Bright Lights",
    "HVY": "Heavy Hitters",
    "MST": "Part the Mistveil",
    "ROS": "Rosetta",
    "HNT": "The Hunted",
    "PEN": "Compendium of Rathe",
    "1HP": "History Pack 1",
    "2HP": "History Pack 2",
    "FAB": "Premier Organized Play",
    "LSS": "Legend Story Studios Promos",
    "GEM": "GEM Pack",
    "MAP": "Archive Pack - Part the Mistveil",
    "ARC": "Arcane Rising",
    "DVR": "Dusk Till Dawn",
    "FAB": "Flesh and Blood Promos",
    "PRO": "Promos",
    "LGS": "Local Game Store Promos",
    "PRY": "Promos",
    "HER": "Heritage",
    "SVD": "Savage Lands",
}


def slugify(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = name.replace("&", " and ")
    name = re.sub(r"[’'`]", "", name)
    name = re.sub(r"[^a-zA-Z0-9]+", "-", name)
    return name.strip("-").lower()


def _get_json(url, params=None):
    try:
        r = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise ConnectionError(f"Could not access Card Vault/API: {exc}") from exc
    if r.status_code == 404:
        return None
    r.raise_for_status()
    try:
        return r.json()
    except ValueError as exc:
        raise ValueError("Card Vault returned a non-JSON response. Its API may have changed.") from exc


def _as_list(payload):
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "data", "cards"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _name_norm(value):
    value = str(value or "")
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", value)


def _nested_value(obj, keys):
    if isinstance(obj, dict):
        for key in keys:
            if key in obj and obj[key] not in (None, "", [], {}):
                return obj[key]
        for value in obj.values():
            found = _nested_value(value, keys)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _nested_value(value, keys)
            if found not in (None, "", [], {}):
                return found
    return None


def _image_from_face(face):
    image = face.get("image") if isinstance(face, dict) else None
    if isinstance(image, str):
        return image
    if isinstance(image, dict):
        for key in ("large", "normal", "small", "url"):
            if image.get(key):
                return image[key]
    return _nested_value(face, ("large", "normal", "image_url", "imageUrl"))


def _absolute_image_url(url):
    if not url:
        return None
    url = str(url).strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return CARDVAULT_BASE + url
    return url


def _finish_from_face(face):
    finish = str((face or {}).get("finish_type") or (face or {}).get("finishType") or "Regular")
    return finish.replace("_", " ").replace("-", " ").strip().title() or "Regular"


def _clean_set_name(value):
    if isinstance(value, dict):
        for key in ("name", "display_name", "printed_name", "title", "label", "code"):
            if value.get(key):
                return str(value[key]).strip()
        return None
    if value not in (None, ""):
        return str(value).strip()
    return None


def _set_from_code(code):
    """Converte o prefixo do código de impressão no nome humano do set.

    Exemplos: DYN234 -> Dynasty, U-WTR150-RF -> Welcome to Rathe,
    FR_PEN311 -> Compendium of Rathe.
    """
    raw = str(code or "").strip().upper()
    raw = re.sub(r"^[A-Z]{2}_", "", raw)  # idioma: FR_, DE_, JA_...
    raw = re.sub(r"^[UR]-", "", raw)     # Unlimited/Revised

    # Alguns códigos têm sufixos de finish (RF/CF/GF), que não fazem
    # parte do código do set. Procuramos o prefixo conhecido mais longo.
    for prefix in sorted(SET_NAMES, key=len, reverse=True):
        if raw.startswith(prefix):
            return SET_NAMES[prefix]
    return None


def _print_set_name(printing, print_id):
    """Obtém o nome do set sem depender exclusivamente de um campo da API."""
    # Campos explícitos primeiro.
    explicit_keys = (
        "print_set_name", "set_name", "setName", "printSetName",
        "product_name", "productName", "collection_name", "collectionName",
    )
    for key in explicit_keys:
        value = _clean_set_name(printing.get(key))
        if value:
            translated = SET_NAMES.get(value.upper())
            return translated or value

    # Estruturas aninhadas (a API já mudou o formato desses dados algumas vezes).
    nested = _nested_value(printing, explicit_keys + ("name", "display_name", "title"))
    value = _clean_set_name(nested)
    if value:
        translated = SET_NAMES.get(value.upper())
        # Evita transformar o nome da própria carta em nome de set.
        if _name_norm(value) != _name_norm(printing.get("name")):
            return translated or value

    # Alguns retornos trazem apenas um código de set.
    for key in ("print_set", "printSet", "set", "set_code", "setCode", "product_code", "productCode"):
        raw = printing.get(key)
        if isinstance(raw, dict):
            code = _clean_set_name(raw)
        else:
            code = str(raw or "").strip()
        if code:
            translated = SET_NAMES.get(code.upper()) or _set_from_code(code)
            if translated:
                return translated

    # Último recurso: o próprio print_id normalmente começa pelo código do set.
    return _set_from_code(print_id) or "Unknown set"


def _parse_prints(detail, requested_name):
    results = _as_list(detail)
    if not results and isinstance(detail, dict):
        results = [detail]
    if not results:
        raise ValueError("The API found the card but returned no card data.")

    card = results[0]
    prints = card.get("card_prints") or card.get("cardPrints") or []
    if not isinstance(prints, list):
        prints = []
    if not prints and (card.get("print_id") or card.get("printId")):
        prints = [card]

    versions = []
    seen = set()
    for printing in prints:
        print_id = str(printing.get("print_id") or printing.get("printId") or "").strip()
        if not print_id:
            continue

        language = str(printing.get("print_language") or printing.get("printLanguage") or "EN").upper()
        if language not in ("EN", "ENG", "ENGLISH"):
            continue

        set_name = _print_set_name(printing, print_id)
        faces = printing.get("faces") or []
        if not isinstance(faces, list):
            faces = []
        face = next(
            (f for f in faces if str(f.get("face_language", "EN")).upper() in ("EN", "ENG", "ENGLISH")),
            None,
        ) or (faces[0] if faces else {})

        # O site usa a versão LARGE hospedada no bucket público para a imagem
        # principal. Algumas respostas da API também trazem URLs menores/
        # thumbnails; para impressão, nunca devemos escolher essas versões.
        image = f"{IMAGE_BASE_LARGE}/{print_id}.webp"
        # Se por algum motivo o arquivo grande não existir, o download_card_png
        # fará fallback para a URL fornecida pela API.
        fallback_image = _absolute_image_url(
            _image_from_face(face) or _nested_value(printing, ("large", "normal", "image_url", "imageUrl"))
        )
        if not image and not fallback_image:
            continue

        key = (set_name, print_id)
        if key in seen:
            continue
        seen.add(key)

        versions.append({
            "set": set_name,
            "code": print_id,
            "label": f"{set_name} - {print_id}",
            "finish": _finish_from_face(face),
            "image_url": image,
            "fallback_image_url": fallback_image,
        })

    if not versions:
        raise ValueError("The card was found, but no English print versions with images were found.")

    name = card.get("printed_name") or card.get("name") or requested_name
    return {
        "name": name,
        "slug": slugify(requested_name),
        "versions": versions,
        "source_url": f"{CARDVAULT_BASE}/card/{slugify(requested_name)}/",
    }


def _display_card_name(card):
    return str(card.get("printed_name") or card.get("name") or "").strip()


def _color_from_name(name):
    """Detecta as variantes de cor do FaB.

    O Card Vault pode expor essas cartas como Pummel-1/Pummel-2/Pummel-3
    ou como Pummel (Red)/(Yellow)/(Blue), dependendo do ponto da API.
    """
    text = str(name or "").strip()
    low = text.lower()
    if re.search(r"(?:\s*[-–]\s*1|\(\s*red\s*\)|\bred\b)$", low):
        return "Red"
    if re.search(r"(?:\s*[-–]\s*2|\(\s*yellow\s*\)|\byellow\b)$", low):
        return "Yellow"
    if re.search(r"(?:\s*[-–]\s*3|\(\s*blue\s*\)|\bblue\b)$", low):
        return "Blue"
    return None


def _base_card_name(name):
    text = str(name or "").strip()
    text = re.sub(r"\s*[-–]\s*[123]$", "", text)
    text = re.sub(r"\s*\(\s*(?:red|yellow|blue)\s*\)\s*$", "", text, flags=re.I)
    return text.strip()


def _card_summary_from_detail(detail, requested_name):
    parsed = _parse_prints(detail, requested_name)
    return {
        "name": parsed["name"],
        "color": _color_from_name(parsed["name"]),
        "image_url": parsed["versions"][0]["image_url"] if parsed["versions"] else None,
        "fallback_image_url": parsed["versions"][0].get("fallback_image_url") if parsed["versions"] else None,
        "card": parsed,
    }


def _search_api(query):
    """Consulta a busca oficial. Aceita tanto q como um fallback de consulta.
    A API é a fonte principal; o HTML do Card Vault só entra como fallback para
    variantes coloridas cujo slug é conhecido.
    """
    try:
        payload = _get_json(
            f"{API_BASE}/advanced-search/",
            params={"q": query, "page_size": 60, "orderby": "name"},
        )
        return _as_list(payload)
    except Exception:
        return []


def _fetch_card_page(slug):
    """Busca uma página pública do Card Vault e extrai dados básicos.

    Isso é usado principalmente para Pummel-1/-2/-3, porque a busca da API é
    por card_id e pode devolver somente uma das variantes coloridas quando o
    termo é o nome-base.
    """
    url = f"{CARDVAULT_BASE}/card/{slug}/"
    try:
        r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")

        # Nome visível: primeiro h1; fallback para title/og:title.
        name = None
        h1 = soup.find("h1")
        if h1:
            name = h1.get_text(" ", strip=True)
        if not name:
            og = soup.find("meta", attrs={"property": "og:title"})
            if og:
                name = og.get("content")
        if not name and soup.title:
            name = soup.title.get_text(" ", strip=True).split("|")[0].strip()
        if not name:
            return None

        # Imagem LARGE é a melhor fonte para o PDF. O HTML contém o URL real.
        image = None
        img = soup.find("img", attrs={"alt": re.compile(r"Card Front", re.I)})
        if img:
            image = img.get("src")
        if not image:
            for meta_key in (("property", "og:image"), ("name", "twitter:image")):
                meta = soup.find("meta", attrs={meta_key[0]: meta_key[1]})
                if meta and meta.get("content"):
                    image = meta.get("content")
                    break
        if not image:
            for tag in soup.find_all("img"):
                src = tag.get("src") or tag.get("data-src") or tag.get("data-lazy-src") or ""
                if "/media/cards/large/" in src or "/media/cards/" in src:
                    image = src
                    break
        image = _absolute_image_url(image)

        # Extrai códigos de print e nomes de coleção diretamente da seção de
        # versões. O Card Vault coloca cada linha em divs e o código é um texto
        # como WTR207, U-WTR207-RF etc.
        versions = []
        code_re = re.compile(r"(?:[A-Z]{2}_)?(?:[A-Z0-9]+-)?[A-Z]{2,5}\d{3}(?:-[A-Z]{1,3})?$", re.I)
        text_nodes = soup.find_all(string=code_re)
        seen = set()
        for node in text_nodes:
            code = node.strip()
            if not code or code in seen:
                continue
            row = node.parent
            # Sobe até encontrar um bloco razoável que contenha o código.
            for _ in range(4):
                if row is None:
                    break
                txt = row.get_text(" ", strip=True)
                if code in txt and len(txt) < 250:
                    break
                row = row.parent
            txt = row.get_text(" ", strip=True) if row else code
            pieces = [x.strip() for x in txt.split(code)]
            set_name = pieces[0].strip(" -") if pieces else ""
            if not set_name or set_name.lower() == name.lower():
                set_name = _set_from_code(code) or "Unknown set"
            seen.add(code)
            versions.append({
                "set": set_name,
                "code": code,
                "label": f"{set_name} - {code}",
                "finish": "Rainbow Foil" if code.endswith("-RF") else ("Cold Foil" if code.endswith("-CF") else "Regular"),
                "image_url": image or f"{IMAGE_BASE_LARGE}/{code}.webp",
                "fallback_image_url": image,
            })

        # Se a página não expôs as linhas de print para o parser, a API pelo
        # menos consegue preencher as versões depois que encontramos o slug.
        slug_color = {"1": "Red", "2": "Yellow", "3": "Blue"}.get(slug.rsplit("-", 1)[-1])
        display_name = f"{_base_card_name(name)} - {slug_color}" if slug_color else name
        return {
            "name": display_name,
            "display_name": display_name,
            "slug": slug,
            "image_url": image,
            "versions": versions,
            "source_url": url,
        }
    except Exception:
        return None




def _extract_slug_near_image(raw_html, src, alt):
    """Try to recover the exact Card Vault /card/<slug>/ route attached to a
    result tile. Card Vault currently renders result tiles as React divs, so
    there may be no literal <a> element in the HTML. The route is often kept
    in hydration/router data near the image URL.
    """
    candidates = []
    needles = [src]
    if src.startswith(CARDVAULT_BASE):
        needles.append(src.replace(CARDVAULT_BASE, ""))
    for needle in needles:
        pos = raw_html.find(needle)
        if pos < 0:
            continue
        lo=max(0,pos-7000); hi=min(len(raw_html),pos+7000)
        chunk=raw_html[lo:hi]
        patterns = [
            r'(?:/card/|\\/card\\/)([a-z0-9][a-z0-9-]+)(?:/|\\/)',
            r'(?i)(?:"|\\")(?:href|url|path|route|slug)(?:"|\\")\s*:\s*(?:"|\\")([^"\\]+)',
            r"(?i)(?:slug|cardSlug)(?:\"|')\s*[:=]\s*(?:\"|')([a-z0-9][a-z0-9-]+)",
            r'(?i)(?:push|navigate|href)\s*\([^)]*?(?:/card/|\\/card\\/)([a-z0-9][a-z0-9-]+)',
        ]
        for pat in patterns:
            for m in re.finditer(pat, chunk):
                slug=m.group(1).strip().strip('/')
                if re.fullmatch(r'[a-z0-9][a-z0-9-]*', slug, re.I):
                    if slug not in candidates:
                        candidates.append(slug.lower())
    # Prefer a slug whose base name matches the visible card name.
    target=_name_norm(alt)
    for slug in candidates:
        if _name_norm(_base_card_name(slug.replace('-', ' '))) == target:
            return slug
    return candidates[0] if candidates else None


def _image_basename(url):
    """Return the card image filename without extension, e.g. U-ARC127."""
    if not url:
        return ""
    m = re.search(r"/([^/]+)\.(?:webp|png|jpg|jpeg)(?:\?.*)?$", str(url), re.I)
    return m.group(1).lower() if m else ""


def _api_card_image_codes(card):
    codes = set()
    def walk(obj):
        if isinstance(obj, dict):
            image = obj.get("image")
            if isinstance(image, dict):
                for v in image.values():
                    if isinstance(v, str):
                        code = _image_basename(v)
                        if code:
                            codes.add(code)
            elif isinstance(image, str):
                code = _image_basename(image)
                if code:
                    codes.add(code)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
    walk(card)
    return codes


def _card_url_from_api(card):
    card_id = str(card.get("card_id") or card.get("id") or "").strip()
    print_id = str(card.get("print_id") or card.get("printId") or "").strip()
    if card_id and print_id:
        return f"{CARDVAULT_BASE}/card/{card_id}/{print_id}"
    return None


def _api_cards_for_query(query):
    payload = _get_json(
        f"{API_BASE}/advanced-search/",
        params={"q": query, "page_size": 60, "orderby": "name"},
    )
    return _as_list(payload)


def _extract_identity_near_image(raw_html, src, alt):
    """Recover card_id/print_id from Card Vault's hydrated search data.

    The visible result tiles are React divs rather than normal <a> tags. The
    actual navigation identity can still be present in the page's hydration
    payload. We search the local payload around the image URL and only accept
    a pair that is syntactically valid.
    """
    basename = _image_basename(src)
    if not basename:
        return None, None
    positions = []
    for needle in (basename, src, src.replace(CARDVAULT_BASE, "")):
        pos = raw_html.find(needle)
        if pos >= 0:
            positions.append(pos)
    patterns_id = [
        r'(?i)["\\]?card_id["\\]?\s*[:=]\s*["\\]([^"\\]+)',
        r'(?i)["\\]?cardId["\\]?\s*[:=]\s*["\\]([^"\\]+)',
    ]
    patterns_print = [
        r'(?i)["\\]?print_id["\\]?\s*[:=]\s*["\\]([^"\\]+)',
        r'(?i)["\\]?printId["\\]?\s*[:=]\s*["\\]([^"\\]+)',
    ]
    for pos in positions:
        chunk = raw_html[max(0, pos-25000):min(len(raw_html), pos+25000)]
        ids=[]; prints=[]
        for pat in patterns_id:
            ids += [m.group(1).strip() for m in re.finditer(pat, chunk)]
        for pat in patterns_print:
            prints += [m.group(1).strip() for m in re.finditer(pat, chunk)]
        for cid in ids:
            for pid in prints:
                if re.fullmatch(r'[A-Za-z0-9_-]{2,100}', cid) and re.fullmatch(r'[A-Za-z0-9_-]{2,100}', pid):
                    return cid, pid
    return None, None


def _match_tile_to_api(tile, api_cards):
    code = _image_basename(tile.get("image_url"))
    if code:
        for card in api_cards:
            if code in _api_card_image_codes(card):
                return card
    target = _name_norm(tile.get("name"))
    same=[c for c in api_cards if _name_norm(c.get("printed_name") or c.get("name")) == target]
    return same[0] if len(same)==1 else None


def _search_results_page(query):
    """Use Card Vault's own results page as the search UI source of truth.

    Every image returned by that page is a separate selectable result. We do
    not infer colours and we do not probe <name>-1/-2/-3. Instead, each tile is
    resolved to the exact Card Vault card identity (card_id + print_id) when
    possible, then that identity is used for the next step.
    """
    url=f"{CARDVAULT_BASE}/results/"
    try:
        r=SESSION.get(url, params={"q":query}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        soup=BeautifulSoup(r.text,"html.parser")
        raw_html=r.text

        # The API gives us stable card_id/print_id/cardUrl data. We use it only
        # to resolve the identity behind each visual tile; the visual tile list
        # itself comes from the actual Card Vault results page.
        try:
            api_cards=_api_cards_for_query(query)
        except Exception:
            api_cards=[]

        found=[]; seen=set()
        for img in soup.find_all("img"):
            alt=(img.get("alt") or "").strip()
            src=img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
            if not alt or not src or "/media/cards/" not in src:
                continue
            src=_absolute_image_url(src)
            large_src=re.sub(r"/media/cards/(?:small|normal)/","/media/cards/large/",src or "")
            tile={"name":alt,"image_url":large_src or src}

            card=_match_tile_to_api(tile, api_cards)
            # If the normal name search only returns one card family member,
            # ask Card Vault's search endpoint once more using the exact image
            # filename (for example U-ARC127). This can resolve the other
            # colour tiles without ever guessing -1/-2/-3.
            if not card:
                code=_image_basename(src)
                if code:
                    try:
                        code_cards=_api_cards_for_query(code)
                        card=_match_tile_to_api(tile, code_cards)
                    except Exception:
                        card=None
            card_id = str(card.get("card_id") or "").strip() if card else ""
            print_id = str(card.get("print_id") or "").strip() if card else ""
            if not card_id or not print_id:
                embedded_id, embedded_print = _extract_identity_near_image(raw_html, src, alt)
                card_id = embedded_id or card_id
                print_id = embedded_print or print_id

            # If the search API can resolve the exact image, use its canonical
            # direct page URL. This is the documented Card Vault route.
            source_url = None
            if card_id and print_id:
                source_url=f"{CARDVAULT_BASE}/card/{card_id}/{print_id}"

            # Last-resort route hint. This is NOT used to decide colour; it is
            # only a navigation fallback when Card Vault exposes no identity in
            # its server HTML/hydration data.
            if not source_url:
                slug_hint=_extract_slug_near_image(raw_html,src,alt)
                if slug_hint:
                    source_url=f"{CARDVAULT_BASE}/card/{slug_hint}/"

            identity_key=(card_id,print_id) if card_id and print_id else (alt,src)
            if identity_key in seen:
                continue
            seen.add(identity_key)
            found.append({
                "name": alt,
                "image_url": large_src or src,
                "source_url": source_url,
                "card_id": card_id or None,
                "print_id": print_id or None,
                "slug": None,
            })
        return found
    except Exception:
        return []


def _resolve_colors_from_results(base_name):
    """Find Red/Yellow/Blue variants using Card Vault's search UI results."""
    results = _search_results_page(base_name)
    base_norm = _name_norm(_base_card_name(base_name))
    colors = []
    seen = set()
    for item in results:
        if _name_norm(_base_card_name(item["name"])) != base_norm:
            continue
        if item.get("color") not in ("Red", "Yellow", "Blue"):
            continue
        if item["color"] in seen:
            continue
        seen.add(item["color"])
        page = _fetch_card_page(item["slug"])
        if not page:
            continue
        page["name"] = f"{_base_card_name(item['name'])} - {item['color']}"
        page["display_name"] = page["name"]
        page["color"] = item["color"]
        page["slug"] = item["slug"]
        # The results page has a reliable large image even if the detail page
        # parser changes.
        page["image_url"] = item["image_url"] or page.get("image_url")
        for v in page.get("versions", []):
            v["image_url"] = item["image_url"] or v.get("image_url")
        colors.append({
            "name": page["name"],
            "color": item["color"],
            "image_url": page.get("image_url"),
            "fallback_image_url": page.get("image_url"),
            "card": page,
        })
    order = {"Red": 0, "Yellow": 1, "Blue": 2}
    return sorted(colors, key=lambda x: order[x["color"]])

def _search_candidates(query):
    """Pesquisa normal + fallback por slug e variantes coloridas."""
    candidates = _search_api(query)

    # Algumas consultas parciais podem ser rejeitadas/ranqueadas de maneira
    # inesperada pelo endpoint. Tente também uma consulta só com o termo base.
    if not candidates and query.strip() != query.strip().lower():
        candidates = _search_api(query.strip().lower())

    # Para nomes coloridos, a página pública usa slugs -1, -2 e -3.
    # Testamos os três diretamente. Isso é deliberado: não dependemos da busca
    # da API para descobrir que existem as outras cores.
    base = query.strip()
    if not re.search(r"[-–]\s*[123]$", base):
        for suffix in ("-1", "-2", "-3"):
            slug = slugify(base + suffix)
            page = _fetch_card_page(slug)
            if page:
                # Não temos card_id nesse fallback; o objeto é tratado depois
                # como uma carta já identificada pelo slug.
                candidates.append({
                    "__page_fallback": page,
                    "card_id": f"slug:{slug}",
                    "printed_name": page["display_name"],
                    "name": page["display_name"],
                    "faces": [{"image": {"large": page["image_url"]}}],
                })

    # Deduplica por card_id/slug.
    out=[]; seen=set()
    for c in candidates:
        cid=c.get("card_id") or c.get("id")
        key=str(cid or _display_card_name(c)).lower()
        if key in seen: continue
        seen.add(key); out.append(c)
    return out

def _detail_for_candidate(c, requested_name):
    fallback = c.get("__page_fallback")
    if fallback:
        return {
            "name": fallback["display_name"],
            "slug": fallback["slug"],
            "versions": fallback["versions"],
            "source_url": fallback["source_url"],
        }
    cid = c.get("card_id") or c.get("id")
    detail = _get_json(f"{API_BASE}/card_id/{cid}/")
    if detail is None:
        raise LookupError(f"Could not load details for: {requested_name}")
    return _parse_prints(detail, requested_name)


def _probe_color_variants(base_name):
    """Resolve the three FaB colour slugs explicitly.

    Card Vault exposes colour variants as separate public card slugs:
    <card>-1 = Red, <card>-2 = Yellow, <card>-3 = Blue.
    We deliberately probe these URLs instead of relying on the generic search
    endpoint, which may rank/return only one colour.
    """
    base = _base_card_name(base_name)
    base_slug = slugify(base)
    found = []
    order = [("1", "Red"), ("2", "Yellow"), ("3", "Blue")]
    for suffix, color in order:
        slug = f"{base_slug}-{suffix}"
        page = _fetch_card_page(slug)
        if not page:
            continue
        # A 200 shell is not enough: require an image or print data.
        if not page.get("image_url") and not page.get("versions"):
            continue
        card_name = _base_card_name(page.get("display_name") or page.get("name") or base)
        page["name"] = f"{card_name} - {color}"
        page["display_name"] = page["name"]
        page["color"] = color
        page["slug"] = slug
        found.append({"name": page["name"], "color": color,
                      "image_url": page.get("image_url") or (page.get("versions") or [{}])[0].get("image_url"),
                      "fallback_image_url": page.get("image_url") or (page.get("versions") or [{}])[0].get("fallback_image_url"),
                      "card": page})
    return found


def _exact_color_slug(name):
    m = re.search(r"^(.*?)\s*[-–]\s*([123])$", str(name or "").strip())
    if not m:
        return None
    return slugify(m.group(1)) + "-" + m.group(2)


def _finalize_selected_card(parsed):
    """After a card is selected, run the colour step before showing prints."""
    selected_name = parsed.get("name") or ""
    # If the selected card is already a colour-specific slug, there is no
    # second colour-selection step: its page is the requested colour.
    if re.search(r"[-–]\s*[123]$", selected_name):
        return {"mode": "versions", **parsed}

    colors = _probe_color_variants(selected_name)
    if len(colors) >= 2:
        return {
            "mode": "colors",
            "base_name": _base_card_name(selected_name),
            "colors": colors,
        }
    if len(colors) == 1:
        # If only one colour-specific page exists, treat it as the only colour
        # and continue directly to its prints (equipment and ordinary cards
        # normally never enter this branch).
        return {"mode": "versions", **colors[0]["card"]}
    return {"mode": "versions", **parsed}


def _result_for_selected_slug(slug):
    """The user clicked an exact result tile from Card Vault's search page.
    Follow that exact route and show only its print versions. Colour is part of
    the selected card identity, not a print/version dimension.
    """
    slug=str(slug or '').strip().strip('/')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*',slug,re.I):
        raise ValueError("Invalid card identifier.")
    page=_fetch_card_page(slug)
    if not page:
        # Fallback to API only to resolve this exact clicked identity.
        human=re.sub(r"[-_]+"," ",re.sub(r"-(?:1|2|3)$","",slug)).strip()
        api_candidates=_search_api(human)
        target=_name_norm(human)
        exact=[]
        for c in api_candidates:
            cname=_base_card_name(_display_card_name(c))
            if _name_norm(cname)==target:
                exact.append(c)
        if exact:
            try:
                page=_detail_for_candidate(exact[0],human)
                page["slug"]=slug
            except Exception:
                page=None
    if not page:
        raise LookupError("Could not load the selected card.")
    m=re.search(r"-(1|2|3)$",slug)
    if m:
        color={"1":"Red","2":"Yellow","3":"Blue"}[m.group(1)]
        base=_base_card_name(page.get("name") or page.get("display_name") or slug)
        page["name"]=base
        page["display_name"]=base
        page["color"]=color
    page["slug"]=slug
    return {"mode":"versions",**page}

def _search_result_from_api_card(card):
    """Turn ONE Card Vault search result into a selectable UI tile.

    Important: search results are card-level, not print-level. We deliberately
    keep EVERY returned card instead of choosing an exact-name match. This is
    what lets differently coloured cards with the same printed name appear as
    separate selectable results.
    """
    card_id = str(card.get("card_id") or card.get("id") or "").strip()
    if not card_id:
        return None

    name = _display_card_name(card)
    if not name:
        return None

    image = None
    faces = card.get("faces") or []
    if isinstance(faces, list):
        face = next(
            (f for f in faces if str(f.get("face_language", "EN")).upper() in ("EN", "ENG", "ENGLISH")),
            None,
        ) or (faces[0] if faces else {})
        image = _absolute_image_url(_image_from_face(face))

    # Search results may expose an image deeper in the JSON.
    if not image:
        image = _absolute_image_url(
            _nested_value(card, ("large", "normal", "image_url", "imageUrl"))
        )

    # The API analysis confirms the Card Vault search page uses card-level
    # advanced-search and maps the result to /card/{card_id}/{print_id}.
    print_id = str(card.get("print_id") or card.get("printId") or "").strip()
    if not print_id:
        prints = card.get("card_prints") or card.get("cardPrints") or []
        if isinstance(prints, list) and prints:
            first = prints[0] or {}
            print_id = str(first.get("print_id") or first.get("printId") or "").strip()

    source_url = f"{CARDVAULT_BASE}/card/{card_id}/{print_id}" if print_id else f"{CARDVAULT_BASE}/card/{card_id}/"

    return {
        "name": name,
        "image_url": image,
        "source_url": source_url,
        "card_id": card_id,
        "print_id": print_id or None,
    }


def search_card(name):
    """Search exactly the same Card Vault backend used by its results page.

    The browser's /results/?q=... page is a React-rendered page. A normal
    Python requests call receives the app shell, not the hydrated <img> tiles
    the user sees in DevTools. The Card Vault API analysis confirms that the
    results page itself uses advanced-search. Therefore we reproduce that
    search request directly and return ALL card-level results.

    Crucially, there is NO exact-name shortcut here. If Pummel has multiple
    colour cards, each card_id returned by Card Vault becomes its own clickable
    result. We never infer colours and never probe -1/-2/-3.
    """
    name = name.strip()
    if not name:
        raise ValueError("Enter a card name.")

    cache_key = "search:" + _name_norm(name)
    if cache_key in PAGE_CACHE:
        return PAGE_CACHE[cache_key]

    try:
        candidates = _api_cards_for_query(name)
    except Exception as exc:
        raise ConnectionError(f"Could not access Card Vault search: {exc}") from exc

    results = []
    seen = set()
    for card in candidates:
        tile = _search_result_from_api_card(card)
        if not tile:
            continue
        if tile["card_id"] in seen:
            continue
        seen.add(tile["card_id"])
        results.append(tile)

    if not results:
        raise LookupError(f"No cards were found for: {name}")

    result = {
        "mode": "candidates",
        "query": name,
        "candidates": results[:60],
    }
    PAGE_CACHE[cache_key] = result
    return result

def _result_for_selected_identity(card_id=None, print_id=None, slug=None):
    """Load the exact card selected from the search results.

    Preferred identity is card_id + print_id, which is the route used by the
    current Card Vault search API. Slug is retained only as a legacy fallback.
    """
    card_id=str(card_id or "").strip()
    print_id=str(print_id or "").strip()
    slug=str(slug or "").strip().strip("/")

    if card_id:
        detail=_get_json(f"{API_BASE}/card_id/{card_id}/")
        if detail is None:
            raise LookupError("Could not load the selected card from Card Vault.")
        parsed=_parse_prints(detail, "Selected card")
        parsed["card_id"]=card_id
        if print_id:
            parsed["selected_print_id"]=print_id
        parsed["source_url"]=f"{CARDVAULT_BASE}/card/{card_id}/{print_id}" if print_id else f"{CARDVAULT_BASE}/card/{card_id}/"
        return {"mode":"versions",**parsed}

    if slug:
        page=_fetch_card_page(slug)
        if page:
            page["source_url"]=f"{CARDVAULT_BASE}/card/{slug}/"
            return {"mode":"versions",**page}

    raise LookupError("The selected search result did not expose a usable Card Vault card identity.")


def download_card_png(url, fallback_url=None):
    cache_key = (url, fallback_url)
    if cache_key in IMAGE_CACHE:
        return IMAGE_CACHE[cache_key]

    urls = [u for u in (url, fallback_url) if u]
    last_error = None
    for candidate in urls:
        try:
            r = SESSION.get(candidate, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            with Image.open(io.BytesIO(r.content)) as source:
                # IMPORTANTE: não redimensionamos a imagem em nenhum momento.
                # A imagem LARGE original é mantida na resolução nativa e só
                # recebe uma máscara alfa para os cantos arredondados.
                im = source.convert("RGBA")
                radius_px = max(1, round(CARD_CORNER_RADIUS_MM / CARD_W_MM * im.width))
                mask = Image.new("L", im.size, 0)
                draw = ImageDraw.Draw(mask)
                draw.rounded_rectangle(
                    (0, 0, im.width - 1, im.height - 1),
                    radius=radius_px,
                    fill=255,
                )
                im.putalpha(mask)

                out = io.BytesIO()
                # PNG é lossless. optimize=True apenas otimiza o tamanho do
                # arquivo; não reduz qualidade nem resolução.
                im.save(out, format="PNG", optimize=True, compress_level=6)
                data = out.getvalue()

                IMAGE_CACHE[cache_key] = data
                return data
        except Exception as exc:
            last_error = exc
            continue

    raise RuntimeError(f"Could not download the card image: {last_error}")


def build_pdf(items):
    if not items:
        raise ValueError("The list is empty.")

    expanded = []
    for item in items:
        qty = int(item.get("quantity", 0))
        if qty < 1 or qty > 999:
            raise ValueError("Invalid quantity.")
        for _ in range(qty):
            expanded.append(item)

    pdf = io.BytesIO()
    c = canvas.Canvas(pdf, pagesize=A4)
    page_w, page_h = A4
    card_w = CARD_W_MM * mm
    card_h = CARD_H_MM * mm
    grid_w = COLS * card_w
    grid_h = ROWS * card_h
    left = (page_w - grid_w) / 2
    top = (page_h + grid_h) / 2

    for index, item in enumerate(expanded):
        slot = index % CARDS_PER_PAGE
        col = slot % COLS
        row = slot // COLS
        x = left + col * card_w
        y = top - (row + 1) * card_h

        png = download_card_png(item["image_url"], item.get("fallback_image_url"))
        c.drawImage(
            ImageReader(io.BytesIO(png)),
            x, y,
            width=card_w,
            height=card_h,
            preserveAspectRatio=False,
            mask="auto",
        )

        if slot == CARDS_PER_PAGE - 1 or index == len(expanded) - 1:
            c.showPage()

    c.save()
    pdf.seek(0)
    return pdf, len(expanded), (len(expanded) + CARDS_PER_PAGE - 1) // CARDS_PER_PAGE


HTML = r'''
<!doctype html>
<html lang="en">
<head>
<link rel="icon" href="{{ url_for('static', filename='favicon.ico') }}">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<div class="brand">
    <img src="{{ url_for('static', filename='logo.png') }}" alt="Proxy Temple">
    <h1>Proxy Temple</h1>
</div>
<style>
.brand {
    display: flex;
    align-items: center;
    gap: 10px;
}

.brand img {
    width: 50px;
    height: 50px;
    object-fit: contain;
    flex-shrink: 0;
}

.brand h1 {
    margin: 0;
}
:root{--bg:#120e0a;--panel:#20160e;--gold:#ecbe6e;--gold2:#c99445;--text:#f5ead7;--muted:#b9a990;--line:#49321e}
*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:radial-gradient(circle at top,#2a1b0e 0,var(--bg) 42rem),var(--bg);color:var(--text)}
.wrap{max-width:1100px;margin:0 auto;padding:32px 20px 70px}h1{margin:0;font-size:clamp(28px,4vw,44px);letter-spacing:-1px}.subtitle{color:var(--muted);margin:6px 0 28px}
.panel{background:rgba(32,22,14,.94);border:1px solid var(--line);border-radius:16px;padding:20px;box-shadow:0 18px 45px rgba(0,0,0,.25);margin-bottom:18px}.search{display:flex;gap:10px}input,button{font:inherit}input[type=text],input[type=number]{width:100%;border:1px solid var(--line);background:#130f0b;color:var(--text);border-radius:10px;padding:13px 14px;outline:none}input:focus{border-color:var(--gold2)}button{border:0;border-radius:10px;padding:12px 17px;cursor:pointer;font-weight:700;background:var(--gold);color:#20160e}button:hover{filter:brightness(1.08)}button.danger{background:transparent;color:#ff9c91;border:1px solid #6b342e}button:disabled{opacity:.5;cursor:not-allowed}
.status{min-height:24px;margin-top:12px;color:var(--muted)}.error{color:#ff9c91}.success{color:#a8dfb0}.version-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:14px;margin-top:14px}.version{position:relative;display:flex;flex-direction:column;align-items:stretch;gap:0;background:#17110c;border:1px solid var(--line);border-radius:12px;padding:0;cursor:pointer;overflow:hidden;transition:.15s}.version:hover{border-color:#8a6030;background:#1d150e}.version input{width:auto;accent-color:var(--gold)}
.choice-thumb{width:100%;aspect-ratio:63/88;height:auto;object-fit:cover;display:block;background:#090705}.choice-card{position:relative;display:flex;flex-direction:column;background:#17110c;border:1px solid var(--line);border-radius:12px;overflow:hidden;cursor:pointer;transition:.15s}.choice-card:hover{border-color:#8a6030;background:#1d150e}.choice-card .choice-info{padding:9px 10px}.choice-card .choice-name{font-weight:700;font-size:14px}.choice-card .choice-sub{color:var(--gold);font-size:12px;margin-top:3px}.version-thumb{width:100%;aspect-ratio:63/88;height:auto;object-fit:cover;border-radius:0;background:#090705;box-shadow:none;display:block}.version-details{min-width:0;display:flex;flex-direction:column;gap:4px;padding:9px 10px 10px}.version-set{font-weight:700;font-size:15px}.version-code{color:var(--gold);font-weight:700;font-size:13px}.finish{color:var(--muted);font-size:12px}.version-check{position:absolute;right:8px;top:8px;width:28px;height:28px;display:grid;place-items:center;border-radius:50%;background:#17110cdd;color:var(--gold);font-size:20px;opacity:.35;border:1px solid var(--line)}.version:has(input:checked){border-color:var(--gold2);box-shadow:0 0 0 1px rgba(236,190,110,.15)}.version:has(input:checked) .version-check{opacity:1}
.add-row{display:flex;gap:10px;align-items:end;margin-top:16px}.qty{width:150px}.label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}.chosen-header{display:flex;justify-content:space-between;align-items:center;gap:12px}.chosen-header h2{margin:0;font-size:20px}.count{color:var(--gold)}.chosen{display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:14px;margin-top:16px}.card{position:relative;background:#17110c;border:1px solid var(--line);border-radius:12px;overflow:hidden}.card img{width:100%;aspect-ratio:63/88;display:block;object-fit:cover;background:#090705}.remove{position:absolute;right:7px;top:7px;width:31px;height:31px;border-radius:50%;padding:0;display:grid;place-items:center;background:#1a100d;color:#fff;border:1px solid #8d443b;font-size:20px}.card-info{padding:9px}.card-name{font-weight:700;font-size:13px;line-height:1.2}.card-version{color:var(--gold);font-size:12px;margin-top:3px}.card-qty{color:var(--muted);font-size:12px;margin-top:5px}.actions{display:flex;gap:10px;margin-top:18px;flex-wrap:wrap}.empty{color:var(--muted);padding:28px 0 8px;text-align:center}.tip{font-size:12px;color:var(--muted);margin-top:10px;line-height:1.5}.legal{margin-top:24px;padding:14px 2px;color:#8f8374;font-size:11px;line-height:1.6;text-align:center}
@media(max-width:650px){.search{flex-direction:column}.add-row{flex-direction:column;align-items:stretch}.qty{width:auto}.chosen{grid-template-columns:repeat(2,minmax(0,1fr))}.version-list{grid-template-columns:repeat(2,minmax(0,1fr))}.version-set{font-size:13px}}
</style></head>
<body><div class="wrap">
<h1>Flesh and Blood Proxy Generator</h1><div class="subtitle">Build your proxy list and generate an A4 print-ready PDF.</div>
<section class="panel"><form id="searchForm" class="search"><input id="cardName" type="text" autocomplete="off" placeholder="Enter a card name..." autofocus><button id="searchBtn" type="submit">Search</button></form><div id="status" class="status"></div></section>
<section id="resultPanel" class="panel" hidden><div id="resultTitle" style="font-size:20px;font-weight:800"></div><div id="resultTip" class="tip"></div><div id="versions" class="version-list"></div><div id="addArea" class="add-row"><div class="qty"><label class="label" for="quantity">Quantity</label><input id="quantity" type="number" min="1" max="999" value="1"></div><button id="addBtn" type="button">Add to list</button></div></section>
<section class="panel"><div class="chosen-header"><h2>Selected cards <span class="count" id="count">(0 cartas)</span></h2><button class="danger" id="clearBtn" type="button">Clear All</button></div><div id="chosen" class="chosen"></div><div id="empty" class="empty">No cards added yet.</div><div class="actions"><button id="pdfBtn" type="button">Save PDF</button></div><div id="pdfStatus" class="status"></div><div class="tip">A4 portrait • 3 × 3 cards • each card exactly 63 × 88 mm • rounded corners ~3 mm. Up to 9 cards per sheet; the last sheet uses only the required slots.</div></section>
<footer class="legal">Unofficial fan-made proxy utility. Flesh and Blood, card names, artwork, logos, and related trademarks are the property of Legend Story Studios. This project is not affiliated with, endorsed by, or sponsored by Legend Story Studios. Card data and images are fetched from Card Vault for reference. Use responsibly and do not represent generated proxies as authentic products.</footer>
</div>
<script>
let chosen=[];let currentCard=null;const $=id=>document.getElementById(id);const status=(msg,cls="")=>{$("status").className="status "+cls;$('status').textContent=msg};

$("searchForm").addEventListener("submit",async e=>{e.preventDefault();const name=$("cardName").value.trim();if(!name)return;$('searchBtn').disabled=true;$('resultPanel').hidden=true;status("Searching Card Vault...");try{const r=await fetch("/api/search?name="+encodeURIComponent(name));const data=await r.json();if(!r.ok)throw new Error(data.error||"Erro ao pesquisar.");currentCard=null;renderSearchResult(data);$('resultPanel').hidden=false;status(searchSuccessMessage(data),"success")}catch(err){currentCard=null;status(err.message,"error")}finally{$('searchBtn').disabled=false}});

function searchSuccessMessage(data){if(data.mode==='colors')return 'This card has multiple color variants. Choose a color first.';if(data.mode==='candidates')return `Card Vault returned ${data.candidates.length} result${data.candidates.length===1?'':'s'}. Choose the card you want.`;return 'Card found. Now choose a print version.'}

function renderSearchResult(data){
  $('versions').innerHTML='';
  $('addArea').style.display='none';
  if(data.mode==='versions'){
    currentCard=data;
    $('resultTitle').textContent=data.name;
    $('resultTip').textContent='Choose a print version by image. The set name and print code are shown beside it.';
    renderVersions();
    $('addArea').style.display='flex';
    return;
  }
  if(data.mode==='colors'){
    $('resultTitle').textContent=data.base_name+' — choose a color';
    $('resultTip').textContent='This card has multiple colors. Choose the color first; then you will see its available print versions.';
    data.colors.forEach(c=>$('versions').appendChild(makeChoiceCard(c, c.color, ()=>selectColor(c))));
    return;
  }
  $('resultTitle').textContent='Possible cards for “'+data.query+'”';
  $('resultTip').textContent='Choose the exact card you want. Different color cards are separate results and are shown separately.';
  data.candidates.forEach(c=>$('versions').appendChild(makeChoiceCard(c, c.color||'', ()=>selectCandidate(c))));
}

function makeChoiceCard(item, sub, onClick){
  const el=document.createElement('div');el.className='choice-card';
  el.innerHTML=`<img class="choice-thumb" src="${escapeAttr(item.image_url)}" alt="Preview" loading="lazy"><div class="choice-info"><div class="choice-name">${escapeHtml(item.name)}</div>${sub?`<div class="choice-sub">${escapeHtml(sub)}</div>`:''}</div>`;
  el.addEventListener('click',onClick);return el;
}

function selectColor(summary){currentCard=summary.card; $('resultTitle').textContent=summary.card.name; $('resultTip').textContent='Choose a print version for this color.'; $('versions').innerHTML=''; renderVersions(); $('addArea').style.display='flex';}
async function selectCandidate(summary){
  // The result itself carries the exact Card Vault identity. Never search again
  // by name and never infer a colour.
  if(!summary.card_id && !summary.slug){ status('This Card Vault result has no usable card link. Please search again.','error'); return; }
  $('versions').innerHTML=''; $('addArea').style.display='none';
  $('resultTitle').textContent=summary.name;
  $('resultTip').textContent='Loading the selected card and its print versions...';
  try{
    const params=new URLSearchParams();
    if(summary.card_id) params.set('card_id',summary.card_id);
    if(summary.print_id) params.set('print_id',summary.print_id);
    if(summary.slug) params.set('slug',summary.slug);
    const r=await fetch('/api/select?'+params.toString());
    const data=await r.json();
    if(!r.ok) throw new Error(data.error||'Could not load the selected card.');
    renderSearchResult(data);
    status('Card selected. Choose a print version.','success');
  }catch(err){ status(err.message,'error'); }
}

function renderVersions(){currentCard.versions.forEach((v,i)=>{const label=document.createElement('label');label.className='version';label.innerHTML=`<input type="radio" name="version" value="${i}" ${i===0?'checked':''}><img class="version-thumb" src="${escapeAttr(v.image_url)}" alt="Preview" loading="lazy"><span class="version-details"><span class="version-set">${escapeHtml(v.set)}</span><span class="version-code">${escapeHtml(v.code)}</span><span class="finish">${escapeHtml(v.finish)}</span></span><span class="version-check">✓</span>`;$('versions').appendChild(label)})}

$("addBtn").addEventListener('click',()=>{if(!currentCard)return;const selected=document.querySelector('input[name="version"]:checked');if(!selected)return alert('Choose a print version.');const quantity=parseInt($('quantity').value,10);if(!Number.isInteger(quantity)||quantity<1||quantity>999)return alert('Enter a quantity between 1 and 999.');const v=currentCard.versions[Number(selected.value)];chosen.push({name:currentCard.name,set:v.set,code:v.code,finish:v.finish,image_url:v.image_url,quantity});renderChosen();$('quantity').value=1});
function renderChosen(){const total=chosen.reduce((sum,x)=>sum+x.quantity,0);$('count').textContent=`(${total} ${total===1?'card':'cards'})`;$('chosen').innerHTML='';$('empty').style.display=chosen.length?'none':'block';chosen.forEach((item,index)=>{const el=document.createElement('div');el.className='card';el.innerHTML=`<button class="remove" title="Remove" onclick="removeCard(${index})">−</button><img src="${escapeAttr(item.image_url)}" alt="${escapeAttr(item.name)}"><div class="card-info"><div class="card-name">${escapeHtml(item.name)}</div><div class="card-version">${escapeHtml(item.set)} - ${escapeHtml(item.code)}</div><div class="card-qty">Quantity: ${item.quantity}</div></div>`;$('chosen').appendChild(el)})}
function removeCard(index){chosen.splice(index,1);renderChosen()}
$('clearBtn').addEventListener('click',()=>{if(!chosen.length)return;if(confirm('Are you sure you want to clear ALL selected cards? This cannot be undone.')){chosen=[];renderChosen();$('pdfStatus').textContent=''}});
$('pdfBtn').addEventListener('click',async()=>{if(!chosen.length)return alert('Add at least one card before saving the PDF.');const total=chosen.reduce((sum,x)=>sum+x.quantity,0),pages=Math.ceil(total/9);$('pdfBtn').disabled=true;$('pdfStatus').className='status';$('pdfStatus').textContent=`Preparing ${total} card(s) on ${pages} A4 sheet(s)...`;try{const r=await fetch('/api/pdf',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:chosen})});if(!r.ok){const data=await r.json().catch(()=>({}));throw new Error(data.error||'Could not generate the PDF.')}const blob=await r.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='proxy-temple.pdf';document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url);$('pdfStatus').className='status success';$('pdfStatus').textContent=`PDF ready: ${pages} A4 sheet(s), ${total} card(s).`}catch(err){$('pdfStatus').className='status error';$('pdfStatus').textContent=err.message}finally{$('pdfBtn').disabled=false}});
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]))}function escapeAttr(s){return escapeHtml(s)}renderChosen();
</script></body></html>
'''

@app.get('/')
def index():
    return render_template_string(HTML)

@app.get('/api/select')
def api_select():
    try:
        return jsonify(_result_for_selected_identity(
            card_id=request.args.get('card_id',''),
            print_id=request.args.get('print_id',''),
            slug=request.args.get('slug',''),
        ))
    except LookupError as e:
        return jsonify({'error':str(e)}),404
    except Exception as e:
        return jsonify({'error':str(e)}),500

@app.get('/api/search')
def api_search():
    try:
        return jsonify(search_card(request.args.get('name','')))
    except LookupError as e:
        return jsonify({'error':str(e)}),404
    except requests.RequestException:
        return jsonify({'error':'Could not access Card Vault right now. Check your internet connection and try again.'}),502
    except Exception as e:
        return jsonify({'error':str(e)}),500

@app.post('/api/pdf')
def api_pdf():
    try:
        payload=request.get_json(silent=True) or {}
        items=payload.get('items',[])
        if not isinstance(items,list) or not items:return jsonify({'error':'The list is empty.'}),400
        pdf,total,pages=build_pdf(items)
        response=send_file(pdf,mimetype='application/pdf',as_attachment=True,download_name='proxy-temple.pdf')
        response.headers['X-Proxy-Total-Cards']=str(total);response.headers['X-Proxy-Pages']=str(pages)
        return response
    except requests.RequestException:
        return jsonify({'error':'Failed to download one or more card images.'}),502
    except Exception as e:
        return jsonify({'error':str(e)}),500

if __name__=='__main__':
    print('\nProxy Temple v15')
    print('Open: http://127.0.0.1:5000')
    print('Press Ctrl+C to stop.\n')
    app.run(host='127.0.0.1',port=5000,debug=False)
