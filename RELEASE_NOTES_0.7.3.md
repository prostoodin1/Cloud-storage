# Cloud Storage 0.7.3 — SSD transfer pipeline

- Added dedicated «Приём» and «Отправка» pages to Server Manager.
- A disk assigned the «Кэш» role is now used as an SSD staging area for incoming files.
- Incoming network progress and the verified SSD-to-HDD move are tracked separately.
- Size and SHA-256 are verified before a file is published in its logical space.
- A fully received file stays on SSD after an HDD failure and can be retried from Manager.
- Outbound downloads are recorded with progress and persistent history.
- Interrupted transfer state is recovered explicitly after a Core restart.
- Safe fallback keeps uploads working when the configured SSD is unavailable or lacks space.
