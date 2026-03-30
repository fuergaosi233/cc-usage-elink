# cc-usage-elink

Display your **Claude Code usage** on a BLE tri-color e-ink screen — always visible, zero screen time.

```
┌─────────────────────────────────────┐
│ Plan Usage Limits          30 Mar   │
├─────────────────────────────────────┤
│ Current session           81% used  │
│ Resets in 3h 29m                    │
│ [████████████░░░░░░░░░░░░░░░░░░░░░] │
├─────────────────────────────────────┤
│ Weekly limits                       │
│ All models                 3% used  │
│ [████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░] │
├─────────────────────────────────────┤
│ Sonnet only                0% used  │
│ [░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░] │
└─────────────────────────────────────┘
```

## Hardware

**Required: 蓝签 (LANCOS) 4.2-inch tri-color e-ink display, model EDP-42000DDF**

- Resolution: 400 × 300 px, tri-color (black / white / red)
- Interface: Bluetooth BLE
- Protocol: 蓝牙BLE通讯协议 V2.1 (LANCOS proprietary)

> This tool uses the LANCOS BLE V2.1 protocol. It is only compatible with the
> 蓝签 EDP-42000DDF (and same-protocol variants). Other e-ink displays will not work.

## Requirements

- macOS (Bluetooth + Keychain APIs used)
- Python 3.13+
- [`uv`](https://docs.astral.sh/uv/) — dependencies install automatically on first run
- Claude Code OAuth token with `user:profile` + `user:inference` scopes

## Quick Start

`elink.py` is a single-file script — no installation needed, just [`uv`](https://docs.astral.sh/uv/).

```bash
# Option A: run directly from URL (uv fetches dependencies automatically)
uv run https://raw.githubusercontent.com/fuergaosi233/cc-usage-elink/main/elink.py setup

# Option B: download the single file
curl -O https://raw.githubusercontent.com/fuergaosi233/cc-usage-elink/main/elink.py
uv run elink.py setup

# Option C: clone
git clone https://github.com/fuergaosi233/cc-usage-elink && cd cc-usage-elink
uv run elink.py setup
```

After setup:

```bash
uv run elink.py push          # push usage to screen once
uv run elink.py watch         # background mode, refresh every 5 min
uv run elink.py watch -i 15   # refresh every 15 min
```

## Commands

| Command | Description |
|---------|-------------|
| `setup [--force]` | Auto-configure: scan device + OAuth token |
| `push [--dry-run]` | Generate usage image → send to screen |
| `watch [-i MIN]` | Background mode, push every N minutes (default 30) |
| `scan [-t SEC]` | Scan for nearby devices |
| `bind [ADDRESS]` | Bind default device |
| `clear` | Clear screen (all white) |
| `send IMAGE [ADDRESS]` | Send any image file |
| `config show` | Show current config |
| `config set-token` | Set OAuth token (hidden input) |
| `config clear-token` | Remove saved token (fall back to Keychain) |

## OAuth Token

You need a Claude OAuth token with usage-reading scopes. Three ways to provide it:

**Option 1 — Via `claude` CLI (recommended)**

```bash
# Run Claude Code's token setup but modify the scope in the URL
claude setup-token
# When the OAuth URL is printed, change &scope=... to:
# &scope=user%3Ainference%20user%3Aprofile
# Open the modified URL in your browser and complete authorization.
# Claude Code stores the token automatically; cc-usage-elink reads it from Keychain.
```

**Option 2 — Manual paste**

```bash
uv run elink.py config set-token
# Paste your token (hidden input)
```

**Option 3 — Keychain (auto-detected)**

```bash
security add-generic-password -s "usage-elink-oauth" -a "elink" -w "<your-token>"
# No further config needed; elink reads this automatically
```

Token lookup priority: `~/.config/elink/config.json` → Keychain `usage-elink-oauth` → Keychain `Claude Code-credentials`

## Config File

Stored at `~/.config/elink/config.json`:

```json
{
  "device_address": "XXXX-XXXX-...",
  "oauth_token": "sk-ant-oat01-..."
}
```

## BLE Protocol Notes

The LANCOS BLE V2.1 protocol sends full-screen image data in two passes (black channel + red channel). Key timings that prevent display artifacts:

- 3 s delay after start packet (device init)
- 1 s per packet for first 10 packets
- 0.6 s per packet thereafter
- Write Without Response only (`response=False`)

Total transfer time: ~100 seconds per full refresh.

## Known Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| `CancelledError` after connect | bleak 3.x CoreBluetooth bug | Pinned to bleak 0.22.x |
| Connect fails after scan | CoreBluetooth drops peripheral ref | Keep scanner running until connected |
| Artifacts at top of screen | Dropped BLE packets | Larger inter-packet delays |
| `Writing is not permitted` | Device only supports write-without-response | Use `response=False` |

## License

MIT
