# Public Runtime Package

This release folder contains a sanitized, split runtime package with the control panel and bundled Playwright browser.

## Restore

Run in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\restore_runtime.ps1
```

It will create:

```text
release\runtime\ChatGPTAssistantPanel\ChatGPTAssistantPanel.exe
```

## Before Running

Edit these files with your own data:

- `.env`
- `config.yaml`
- `data\hotmail\accounts.txt`
- `data\proxies\proxies_us.txt` / `data\proxies\proxies_jp.txt`
- `data\paypal\cards.txt`
- `data\paypal\phones.txt`
- `data\auth\phones.txt`

No private account, proxy, card, phone, API key, or local runtime data is included.
