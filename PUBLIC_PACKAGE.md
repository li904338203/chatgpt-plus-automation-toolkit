# Public Package Notes

The repository contains source code plus a sanitized runtime package under `release/`.

To restore the ready-to-run control panel with bundled browser, run:

```powershell
cd release
powershell -ExecutionPolicy Bypass -File .\restore_runtime.ps1
```

Then configure your own `.env`, `config.yaml`, and `data/` pools.
