All data stays local. Under **Settings → Storage** you can view the data directory, manage session data, and import/export everything.

## Data directory

The top of the page shows where the app data directory lives. All sessions, memory, settings, and more are stored there; your local data is encrypted with **AES-256-GCM** by default.

## Clear & delete

| Action | Description |
| --- | --- |
| Clear all sessions | Delete all session records |

> [!WARNING]
> Clearing sessions cannot be undone. Export what you need first.

## Export all data

Exports all data as a single decrypted archive (it never contains logs or keys).

- Great for backup or moving to another machine
- Make sure the destination is safe before exporting

## Import all data

Import previously exported data. Three merge modes:

| Mode | Description |
| --- | --- |
| Overwrite-merge (recommended) | Overwrite existing data of the same name; keep the rest |
| Clear and replace | Clear current data, then import everything |
| Skip existing files | Only import what isn't present locally |

> [!NOTE]
> Imported data is re-encrypted with the current machine's key.

![Storage management](/screenshots/storage-panel.png)

## FAQ

**Q: Does the export contain keys?**
A: No. Exports never include logs or keys.

**Q: Is data stored in the cloud?**
A: Sessions and settings live in the local data directory. Only the gateways/models you configure communicate with their services.