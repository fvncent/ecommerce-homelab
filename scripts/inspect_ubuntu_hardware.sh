#!/usr/bin/env bash
# Read-only inventory. No serial numbers or system UUIDs are requested.
set -uo pipefail
printf '\n=== CPU MODEL ===\n'
if command -v lscpu >/dev/null 2>&1; then
  lscpu | grep -E 'Model name:|CPU\(s\):|Thread\(s\) per core:|Core\(s\) per socket:' || true
fi
printf '\n=== MEMORY TOTAL / AVAILABLE ===\n'
free -h || true
printf '\n=== MOTHERBOARD ===\n'
if command -v dmidecode >/dev/null 2>&1; then
  sudo dmidecode -s baseboard-manufacturer || true
  sudo dmidecode -s baseboard-product-name || true
  sudo dmidecode -s baseboard-version || true
  printf '\n=== MEMORY MODULES (NON-SERIAL FIELDS) ===\n'
  sudo dmidecode -t 17 | grep -E '^\s*(Size:|Locator:|Bank Locator:|Type:|Form Factor:|Speed:|Configured Memory Speed:|Manufacturer:|Part Number:|Rank:)' || true
  printf '\n=== PHYSICAL MEMORY ARRAY ===\n'
  sudo dmidecode -t 16 | grep -E '^\s*(Maximum Capacity:|Number Of Devices:)' || true
else
  printf 'dmidecode not installed. Install with: sudo apt install dmidecode\n'
fi
printf '\n=== PHYSICAL STORAGE MEDIA ===\n'
lsblk -d -o NAME,SIZE,ROTA,MODEL,TRAN || true
printf '\n=== MOUNTED FILESYSTEMS ===\n'
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS || true
printf '\nNOTE: ROTA 0 usually means SSD/NVMe; 1 usually means HDD.\n'
printf 'DMI can be incomplete; check your exact motherboard manufacturer manual before buying RAM.\n'
