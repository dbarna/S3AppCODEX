# S3 Backup Manager (macOS Terminal)

Interactive terminal application for creating backup and restore routines to AWS S3 using API calls (`boto3`).

## Features

- Full menu navigation using keyboard (`↑/↓`, `Enter`, `ESC`).
- Global quick-path support: press `P` from menus to paste a path at any time and reuse it.
- Backup routine creation asks typical backup settings:
  - source path
  - bucket and prefix
  - frequency
  - run interval in hours
  - how many backups to keep
  - how many days to keep backups
- Preset routine generation option:
  - Monthly: keep last 12
  - Weekly: keep last 4
  - Daily every 2 hours: keep previous day
- Edit/Delete existing routines.
- Optional daemon script generation (`launchd` plist + shell script).
- If bucket is wrong/inaccessible, app shows S3 bucket management URL.
- Restore latest backup for a selected routine.

## Requirements

- Python 3.10+
- `boto3`
- AWS credentials configured (for example via `aws configure`)

Install dependency:

```bash
pip install boto3
```

## Usage

```bash
python3 s3_backup_manager.py
```

### Non-interactive routine execution (for daemon)

```bash
python3 s3_backup_manager.py --run-routine <routine-id>
```

## Notes

- Routine data is stored in:
  - `~/.s3_backup_manager/routines.json`
  - `~/.s3_backup_manager/state.json`
- Generated daemon files are also written to `~/.s3_backup_manager/`.

