#!/usr/bin/env python3
"""MCP server that lets Claude Desktop translate a Mist WLAN guest portal.

Claude itself does the translating (on the user's Claude subscription - no
API key). This server only talks to Mist and keeps a translation memory:

  1. get_translation_plan  - compares each language with the default text and
                             returns only the strings that are new or changed
  2. save_translations     - Claude hands back its translations; they are
                             validated (HTML tags, {{placeholders}}) and stored
  3. apply_translations    - fills every selected language, backs up the
                             template, writes it to Mist and verifies it

Settings come from mist_portal_translate.ini next to this file, or from the
path in the MIST_PORTAL_INI environment variable.
"""
import configparser
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests
from mcp.server.mcpserver import MCPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INI_PATH = os.environ.get("MIST_PORTAL_INI", os.path.join(SCRIPT_DIR, "mist_portal_translate.ini"))
STOCK_DIR = os.path.join(SCRIPT_DIR, "translations")  # stock Mist wording, shipped with the repo
CACHE_DIR = os.path.join(SCRIPT_DIR, "cache")          # translations of customised text, local only
BACKUP_DIR = os.path.join(SCRIPT_DIR, "backups")

# stdout is the MCP channel, so all logging goes to stderr.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mist-portal-translate")

ALL_LOCALES = [
    "ar", "ca-ES", "cs-CZ", "da-DK", "de-DE", "el-GR", "en-GB", "en-US",
    "es-ES", "fi-FI", "fr-FR", "he-IL", "hi-IN", "hr-HR", "hu-HU", "id-ID",
    "it-IT", "ja-JP", "ko-KR", "ms-MY", "nb-NO", "nl-NL", "pl-PL", "pt-BR",
    "pt-PT", "ro-RO", "ru-RU", "sk-SK", "sv-SE", "th-TH", "tr-TR", "uk-UA",
    "vi-VN", "zh-Hans", "zh-Hant",
]
ENGLISH_LOCALES = {"en-GB", "en-US"}

# String fields that are settings rather than visitor-facing text.
NON_TEXT_FIELDS = {
    "color", "alignment", "smsValidityDuration", "smsCountryFormat",
    "sponsorEmailTemplate", "logo",
}
# Placeholders for the customer's own legal text - opt-in via translate_policy_text.
POLICY_TEXT_FIELDS = {"tosText", "privacyPolicyText", "marketingPolicyOptInText"}

# Attributes whose values are human-readable and may be translated; every
# other attribute (style, href, class, ...) must be copied unchanged.
TRANSLATABLE_ATTRS = {"alt", "title", "placeholder", "aria-label"}
PLACEHOLDER_RE = re.compile(r"\{\{\s*\w+\s*\}\}")
HTML_RE = re.compile(r"<[a-zA-Z/!]")

REQUEST_TIMEOUT = 60

TRANSLATION_RULES = (
    "Translation rules: write natural, concise UI wording for a Wi-Fi guest portal, "
    "in the formal register usual for that language. Keep {{placeholders}} exactly as they are. "
    "Brand names (Google, Facebook, Amazon, Microsoft, Azure, Wi-Fi) stay in Latin script. "
    "Some strings are HTML: translate ONLY the visible text between tags (plus alt/title "
    "attributes). Copy every tag, attribute, inline style, entity such as &nbsp; and "
    "line break exactly as in the source, in the same order. Never add or remove tags."
)

mcp = MCPServer("mist-portal-translate", instructions=(
    "Translates a Juniper Mist WLAN guest (captive) portal into the languages enabled in "
    "the user's .ini. Workflow: call list_portal_wlans if the WLAN isn't known, then "
    "get_translation_plan, translate every string it lists into each language it lists "
    "yourself, pass them to save_translations (one call can cover several languages; fix and "
    "resend anything it rejects), then apply_translations with dry_run=true to preview and "
    "dry_run=false once the user confirms. " + TRANSLATION_RULES
))


class MistError(Exception):
    pass


# string id -> English text for everything get_translation_plan has handed out
# in this server process, so save_translations can map ids back.
PLANNED = {}


def load_config():
    config = configparser.ConfigParser(inline_comment_prefixes=(";",))
    config.optionxform = str  # keep locale codes like zh-Hans case-sensitive
    config.BOOLEAN_STATES = {**config.BOOLEAN_STATES, "y": True, "n": False}
    if not config.read(INI_PATH):
        raise MistError(f"Could not read {INI_PATH} - copy mist_portal_translate.ini.example and fill it in.")
    for key in ("org_id", "api_token", "cloud"):
        if not config.get("mist", key, fallback="").strip():
            raise MistError(f"[mist] {key} is required in {INI_PATH}.")
    return config


def cloud_to_host(cloud):
    """Normalise the .ini cloud value to an API host URL like https://api.eu.mist.com."""
    value = cloud.lower().strip()
    if "://" in value:
        value = urlparse(value).netloc
    value = value.strip("/")
    if value.endswith("mist.com"):
        if value.startswith("manage."):
            value = "api." + value[len("manage."):]
        return f"https://{value}"
    if value in ("us", "global", "global01"):
        return "https://api.mist.com"
    return f"https://api.{value}.mist.com"


class Mist:
    """Mist API access with the optional [network] Zscaler / proxy settings."""

    def __init__(self, config):
        self.config = config
        self.org_id = config.get("mist", "org_id").strip()
        self.token = config.get("mist", "api_token").strip()
        self.cloud = config.get("mist", "cloud").strip()
        self.api_base = cloud_to_host(self.cloud) + "/api/v1"
        self.session = requests.Session()
        if "network" in config:
            section = config["network"]
            ca_bundle = section.get("ca_bundle", "").strip()
            if ca_bundle:
                ca_path = os.path.expanduser(ca_bundle)
                if not os.path.isfile(ca_path):
                    raise MistError(f"[network] ca_bundle does not exist: {ca_path}")
                self.session.verify = ca_path
            elif not section.getboolean("verify_ssl", fallback=True):
                self.session.verify = False
                requests.packages.urllib3.disable_warnings(
                    requests.packages.urllib3.exceptions.InsecureRequestWarning)
            for scheme in ("http", "https"):
                proxy = section.get(f"{scheme}_proxy", "").strip()
                if proxy:
                    self.session.proxies[scheme] = proxy

    def request(self, method, url, mist_auth=True, **kwargs):
        """mist_auth=False is for the signed Google Storage URL, which rejects an
        extra Authorization header."""
        headers = {"Authorization": f"Token {self.token}"} if mist_auth else {}
        for attempt in range(1, 6):
            try:
                resp = self.session.request(method, url, headers=headers, timeout=REQUEST_TIMEOUT, **kwargs)
            except requests.exceptions.SSLError as e:
                raise MistError(f"TLS certificate verification failed for {url}: {e}. If you're behind "
                                f"a TLS-inspecting proxy (e.g. Zscaler), set [network] ca_bundle in "
                                f"{INI_PATH} to its root CA certificate (PEM).")
            except requests.exceptions.ProxyError as e:
                raise MistError(f"Could not reach the proxy for {url}: {e}. Check [network] "
                                f"http_proxy / https_proxy in {INI_PATH}.")
            if resp.status_code != 429:
                break
            wait = int(resp.headers.get("Retry-After", 30))
            log.info("Rate limited - waiting %ss (attempt %s/5)", wait, attempt)
            time.sleep(wait)
        if resp.status_code == 401:
            raise MistError(f"401 Unauthorized - the API token is not valid on this cloud ({self.cloud}).")
        if resp.status_code == 403:
            raise MistError(f"403 Forbidden - the API token can't access org {self.org_id} (needs write access).")
        if resp.status_code == 404:
            raise MistError(f"404 Not Found: {url} - check the WLAN id belongs to org {self.org_id}.")
        if resp.status_code >= 400:
            raise MistError(f"HTTP {resp.status_code} from {method} {url}: {resp.text[:500]}")
        return resp

    def wlans(self):
        return self.request("GET", f"{self.api_base}/orgs/{self.org_id}/wlans?limit=1000").json()

    def template(self, wlan_id):
        """The WLAN only carries a short-lived signed URL to its portal template."""
        wlan = self.request("GET", f"{self.api_base}/orgs/{self.org_id}/wlans/{wlan_id}").json()
        url = wlan.get("portal_template_url")
        if not url:
            raise MistError(f"WLAN {wlan_id} ({wlan.get('ssid')}) has no portal template - "
                            f"is the guest portal enabled on it?")
        return wlan, self.request("GET", url, mist_auth=False).json()

    def put_template(self, wlan_id, template):
        self.request("PUT", f"{self.api_base}/orgs/{self.org_id}/wlans/{wlan_id}/portal_template",
                     json={"portal_template": template})


def selected_locales(config):
    if "languages" not in config:
        raise MistError(f"No [languages] section in {INI_PATH}.")
    section = config["languages"]
    unknown = [k for k in section if k not in ALL_LOCALES]
    if unknown:
        raise MistError(f"Unknown locale(s) in [languages]: {', '.join(unknown)}")
    chosen = [c for c in ALL_LOCALES if section.get(c, "N").strip().upper() == "Y"]
    if not chosen:
        raise MistError(f"No languages are set to Y in [languages] in {INI_PATH}.")
    return chosen


def text_fields(config, template):
    """Top-level default-language fields that hold visitor-facing text."""
    translate_policy = config.getboolean("options", "translate_policy_text", fallback=False)
    return {k: v for k, v in template.items()
            if isinstance(v, str) and v.strip() and k not in NON_TEXT_FIELDS
            and (translate_policy or k not in POLICY_TEXT_FIELDS)}


def string_id(text):
    """Stable short id for an English string, so Claude needn't echo long HTML back as a key."""
    return "s_" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def _read_json(path):
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def memory(locale):
    """English -> translation for one locale: stock wording overlaid with the local cache."""
    return {**_read_json(os.path.join(STOCK_DIR, f"{locale}.json")),
            **_read_json(os.path.join(CACHE_DIR, f"{locale}.json"))}


def translation_for(locale, mem, english):
    return english if locale in ENGLISH_LOCALES else mem.get(english)


def classify(locale, mem, english, current):
    """How one field of one language stands against the current default text."""
    target = translation_for(locale, mem, english)
    if target is None:
        return "needs_translation", None
    if not current:
        return "fill", target
    if current == target:
        return "up_to_date", target
    # A value we generated for some earlier default text is stale; anything
    # else was typed in by a person and is left alone unless asked.
    known = known_english() if locale in ENGLISH_LOCALES else set(mem.values())
    return ("stale", target) if current in known else ("manual", target)


def known_english():
    """Every English default text we hold a translation for - for English
    locales, a value matching one of these is an older default, not a manual edit."""
    known = set()
    for folder in (STOCK_DIR, CACHE_DIR):
        if os.path.isdir(folder):
            for name in os.listdir(folder):
                if name.endswith(".json"):
                    known.update(_read_json(os.path.join(folder, name)))
    return known


class _Structure(HTMLParser):
    """Collects the tag skeleton of an HTML string, ignoring the text."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts = []

    def _attrs(self, attrs):
        return tuple(sorted((k, v) for k, v in attrs if k not in TRANSLATABLE_ATTRS))

    def handle_starttag(self, tag, attrs):
        self.parts.append(("start", tag, self._attrs(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.parts.append(("startend", tag, self._attrs(attrs)))

    def handle_endtag(self, tag):
        self.parts.append(("end", tag))


def html_structure(text):
    parser = _Structure()
    parser.feed(text)
    parser.close()
    return parser.parts


def validate(english, translated):
    """Return a reason the translation is unusable, or None if it's fine."""
    if not isinstance(translated, str) or not translated.strip():
        return "empty translation"
    if sorted(PLACEHOLDER_RE.findall(english)) != sorted(PLACEHOLDER_RE.findall(translated)):
        return f"placeholders must be exactly {PLACEHOLDER_RE.findall(english)}"
    if HTML_RE.search(english) or HTML_RE.search(translated):
        if html_structure(english) != html_structure(translated):
            return ("HTML tags/attributes differ from the source - translate only the text "
                    "between tags and copy all markup exactly")
    return None


@mcp.tool()
def list_portal_wlans() -> list[dict]:
    """List the org-level WLANs in the configured Mist org that have a guest portal enabled."""
    mist = Mist(load_config())
    return [{"wlan_id": w["id"], "ssid": w.get("ssid"), "portal_auth": w.get("portal", {}).get("auth"),
             "template_id": w.get("template_id")}
            for w in mist.wlans() if w.get("portal", {}).get("enabled")]


@mcp.tool()
def get_translation_plan(wlan_id: str = "") -> dict:
    """Compare every enabled language of the WLAN's guest portal with its default text and
    return only the English strings that still need translating, per language.

    wlan_id: the WLAN to check; blank uses wlan_id from the .ini.
    Translate each string in `strings` into every language listed for it under `needed`,
    then call save_translations."""
    config = load_config()
    wlan_id = wlan_id or config.get("mist", "wlan_id", fallback="").strip()
    if not wlan_id:
        raise MistError("No wlan_id given and none set in the .ini - call list_portal_wlans first.")
    mist = Mist(config)
    wlan, template = mist.template(wlan_id)
    fields = text_fields(config, template)

    strings, needed, summary = {}, {}, {}
    for locale in selected_locales(config):
        mem = memory(locale)
        current = template.get(locale) if isinstance(template.get(locale), dict) else {}
        counts = {"up_to_date": 0, "fill": 0, "stale": 0, "manual": 0, "needs_translation": 0}
        manual = []
        for key, english in fields.items():
            state, _ = classify(locale, mem, english, current.get(key))
            counts[state] += 1
            if state == "manual":
                manual.append(key)
            if state == "needs_translation":
                sid = string_id(english)
                strings[sid] = english
                PLANNED[sid] = english
                needed.setdefault(locale, [])
                if sid not in needed[locale]:
                    needed[locale].append(sid)
        summary[locale] = {**counts, **({"manually_edited_fields": manual} if manual else {})}

    return {
        "wlan": {"wlan_id": wlan_id, "ssid": wlan.get("ssid")},
        "default_text_fields": len(fields),
        "strings": strings,
        "needed": needed,
        "per_language": summary,
        "notes": ("fill = known translation, just not set yet; stale = our older translation of "
                  "text that has since changed; manual = typed in by a person, kept unless "
                  "apply_translations(overwrite_manual_edits=true)."),
        "rules": TRANSLATION_RULES,
    }


@mcp.tool()
def save_translations(translations: dict[str, dict[str, str]], wlan_id: str = "") -> dict:
    """Store translations into the local translation memory after validating them.

    translations: {locale: {string_id: translated_text}} using the ids from
    get_translation_plan, e.g. {"fr-FR": {"s_1a2b3c4d5e": "Bienvenue"}}.
    wlan_id: the WLAN the plan was for (blank = the .ini default).
    Anything rejected is returned with the reason - fix it and send it again."""
    config = load_config()
    allowed = set(ALL_LOCALES) - ENGLISH_LOCALES
    sources = dict(PLANNED)
    wanted = {sid for items in translations.values() for sid in items}
    wlan_id = wlan_id or config.get("mist", "wlan_id", fallback="").strip()
    if wanted - set(sources) and wlan_id:
        # Server restarted since the plan - rebuild the id map from the template.
        _, template = Mist(config).template(wlan_id)
        sources.update({string_id(v): v for v in text_fields(config, template).values()})

    saved, rejected = {}, {}
    os.makedirs(CACHE_DIR, exist_ok=True)
    for locale, items in translations.items():
        if locale not in allowed:
            rejected[locale] = {"*": f"unknown or English locale {locale!r}"}
            continue
        path = os.path.join(CACHE_DIR, f"{locale}.json")
        cache = _read_json(path)
        for sid, text in items.items():
            english = sources.get(sid)
            if english is None:
                rejected.setdefault(locale, {})[sid] = "unknown string id - rerun get_translation_plan"
                continue
            problem = validate(english, text)
            if problem:
                rejected.setdefault(locale, {})[sid] = problem
                continue
            cache[english] = text
            saved[locale] = saved.get(locale, 0) + 1
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=1, ensure_ascii=False)
    return {"saved": saved, "rejected": rejected}


@mcp.tool()
def apply_translations(wlan_id: str = "", dry_run: bool = True, overwrite_manual_edits: bool = False) -> dict:
    """Fill every enabled language of the WLAN's guest portal from the translation memory,
    back up the current template, write it to Mist and verify it persisted.

    wlan_id: blank uses wlan_id from the .ini.
    dry_run: true (default) only reports what would change - confirm with the user before false.
    overwrite_manual_edits: also replace text a person typed into a language by hand."""
    config = load_config()
    wlan_id = wlan_id or config.get("mist", "wlan_id", fallback="").strip()
    if not wlan_id:
        raise MistError("No wlan_id given and none set in the .ini.")
    mist = Mist(config)
    wlan, template = mist.template(wlan_id)
    fields = text_fields(config, template)

    new_template = json.loads(json.dumps(template))
    written, report = {}, {}
    for locale in selected_locales(config):
        mem = memory(locale)
        current = dict(template.get(locale)) if isinstance(template.get(locale), dict) else {}
        changes, missing, kept = {}, [], []
        for key, english in fields.items():
            state, target = classify(locale, mem, english, current.get(key))
            if state in ("fill", "stale") or (state == "manual" and overwrite_manual_edits):
                changes[key] = target
            elif state == "manual":
                kept.append(key)
            elif state == "needs_translation":
                missing.append(key)
        current.update(changes)
        new_template[locale] = current
        if changes:
            written[locale] = changes
        report[locale] = {"changed": len(changes),
                          **({"kept_manual_edits": kept} if kept else {}),
                          **({"still_untranslated": missing} if missing else {})}

    result = {"wlan": {"wlan_id": wlan_id, "ssid": wlan.get("ssid")},
              "dry_run": dry_run, "per_language": report,
              "total_changes": sum(len(v) for v in written.values())}
    if dry_run or not written:
        if not written:
            result["message"] = "Nothing to change - every enabled language is up to date."
        return result

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(BACKUP_DIR, f"portal_template_{wlan_id}_{stamp}.json")
    with open(backup, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=1, ensure_ascii=False)
    result["backup"] = backup

    mist.put_template(wlan_id, new_template)
    # A 200 doesn't prove the change stuck - re-read and compare.
    _, after = mist.template(wlan_id)
    lost = [f"{loc}.{key}" for loc, values in written.items() for key, value in values.items()
            if not isinstance(after.get(loc), dict) or after[loc].get(key) != value]
    result["verified"] = not lost
    if lost:
        result["not_persisted"] = lost[:20]
    return result


if __name__ == "__main__":
    mcp.run()
