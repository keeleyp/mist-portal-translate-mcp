# Mist Portal Translate — Claude Desktop (MCP) edition

Translate a Juniper Mist WLAN guest portal into any of the 35 portal languages from
inside Claude Desktop. **Claude does the translating on your Claude subscription, so no
API key is needed.** This MCP server reads and writes Mist and keeps a translation memory.

This is a separate project from the standalone script
([keeleyp/mist-portal-translate](https://github.com/keeleyp/mist-portal-translate)), which uses
only the fixed stock translations. This edition also translates text you've customised,
including HTML.

## How it works

Ask Claude Desktop something like *"Translate the guest portal on the Guest WLAN"*. Claude then:

1. **`list_portal_wlans`**: finds the org's WLANs that have a guest portal, if you didn't give a WLAN id.
2. **`get_translation_plan`**: compares each language enabled in the .ini with the default text and returns **only the strings that are new or changed**. Each field in each language is in one of these states:
   - **up to date**: already correct.
   - **fill**: the translation is already known but hasn't been set yet. No translating needed.
   - **stale**: an older translation of English text that has since been changed.
   - **manual**: text a person typed into that language by hand. It's kept unless you ask for it to be overwritten.
   - **needs translation**: the English is new or changed, so Claude translates it.
3. **`save_translations`**: Claude passes back its translations. Each one is checked before it's stored:
   - `{{placeholders}}` must be unchanged.
   - In HTML strings, the tags, attributes and inline styles must match the source exactly, so only the visible text is translated.

   Anything that fails is sent back to Claude with the reason, so it can fix it.
4. **`apply_translations`**: does a dry run first. Once you confirm, it backs up the template to `backups/`, writes it to Mist, then re-reads it to check every value was saved.

### Translation memory

- `translations/`: Mist's stock wording, already translated. It's committed to this repo.
- `cache/`: Claude's translations of your customised text. It stays on your machine (git-ignored) because it may contain customer wording.

Once a string has been translated, it's never sent to Claude again. You can review or correct any entry in these JSON files.

## Setup

```bash
git clone https://github.com/keeleyp/mist-portal-translate-mcp.git ~/mist-portal-translate-mcp
cd ~/mist-portal-translate-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp mist_portal_translate.ini.example mist_portal_translate.ini
```

Fill in `org_id`, `api_token` (it needs write access), `cloud`, and optionally a default `wlan_id`. Then set each language to Y or N.

Then add the server to Claude Desktop under **Settings → Developer → Edit Config** (`~/Library/Application Support/Claude/claude_desktop_config.json`) and restart Claude Desktop:

```json
{
  "mcpServers": {
    "mist-portal-translate": {
      "command": "/Users/YOU/mist-portal-translate-mcp/.venv/bin/python",
      "args": ["/Users/YOU/mist-portal-translate-mcp/server.py"]
    }
  }
}
```

To keep the .ini somewhere else, add `"env": {"MIST_PORTAL_INI": "/path/to/file.ini"}`.

## Options (.ini)

| Option | Default | Meaning |
|---|---|---|
| `[languages]` | | Y/N for each of the 35 locales. Only Y languages are planned or written. `en-GB`/`en-US` copy the default text |
| `translate_policy_text` | N | Also translate the Terms of Service, Privacy and Marketing body text. Off by default because it's your own legal wording |

## Proxy / Zscaler

The optional `[network]` section in the .ini supports a custom CA bundle (`ca_bundle`), disabling TLS verification as a last resort (`verify_ssl`), and explicit proxies. Leave it blank for normal behaviour.

## Example session

> **You:** Translate the guest portal on WLAN 6f8aaed0-8892-4859-bd7b-89312767ba16
>
> **Claude:** Only one string needs translating. The welcome message (HTML) has a new third line, "Enter Details To Continue". The other 93 of 94 fields are up to date in every language. I've translated it into the 33 languages, and all of them passed the HTML check. Dry run: 35 changes, nothing typed in by hand would be overwritten. Shall I write it?
>
> **You:** yes go ahead
>
> **Claude:** Done. All 35 changes were saved and verified; the previous template is in `backups/`.

Other useful prompts:
- *"List the guest portal WLANs"*
- *"What would change if I translated the Guest portal?"* (plan only, no writing)
- *"Translate the portal, and overwrite the manual edits in fr-FR too"*

## Troubleshooting

- **The tools don't appear in Claude Desktop.** Quit it fully (Cmd+Q) and reopen it after editing the config. Server start-up errors are shown under **Settings → Developer**, and logged in `~/Library/Logs/Claude/mcp-server-mist-portal-translate.log`.
- **`No module named 'mcp.server.fastmcp'`.** This server needs version 2 of the MCP Python SDK (`mcp>=2`), where `FastMCP` was renamed `MCPServer`. Reinstall with `.venv/bin/pip install -r requirements.txt`.
- **TLS / certificate errors.** You're probably behind Zscaler. Set `[network] ca_bundle` in the .ini to the Zscaler root CA (PEM).
- **401 or 403 errors.** A 401 means the token isn't valid on that `cloud`. A 403 means the token can't access the org, or doesn't have write access.
- **Undoing a write.** Every write saves the previous template to `backups/portal_template_<wlan>_<timestamp>.json`.

## Notes

- Only org-level WLANs are supported. Site-level WLANs aren't.
- The translations are AI-generated. Have a native speaker review a language before using it in production.
