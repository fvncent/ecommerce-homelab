# 09 — Ubuntu RAM and Storage Identification Guide

## What is already known

Assuming the CPU is **AMD Ryzen 5 5600** (not 5600G, 5600X or another model), AMD lists **DDR4 UDIMM**, dual-channel memory, maximum memory specification of 3200 MT/s for common two-module configurations, and up to 128 GB CPU memory support. **Your motherboard BIOS, number of RAM sticks, ranks and qualified-memory list can impose tighter limits.**

Official AMD reference: https://www.amd.com/en/support/downloads/drivers.html/processors/ryzen/ryzen-5000-series/amd-ryzen-5-5600.html

## 1 — Optional install of diagnostic commands

```bash
sudo apt update
sudo apt install -y dmidecode lshw
```

`dmidecode` and `lshw` are passive hardware-inspection tools. On some firmware, DMI details may be missing, wrong or generic.

## 2 — Inspect installed memory modules

```bash
sudo dmidecode -t 17 | grep -E 'Size:|Locator:|Type:|Form Factor:|Speed:|Manufacturer:|Part Number:|Rank:'
```

Look at every **Memory Device** slot, including `No Module Installed`. Things to record:

- **Size** — one 16 GB DIMM or two 8 GB DIMMs?
- **Type** — DDR4 (not DDR5).
- **Form Factor** — standard desktop DIMM / UDIMM (not laptop SO-DIMM).
- **Speed** — module's stated speed, e.g., 3200 MT/s.
- **Configured Memory Speed** — actual current link data rate; it can be lower than the marketed rated speed.
- **Manufacturer / Part Number** — match the exact SKU when considering adding an identical module.
- **Locator** — DIMM_A1/A2/B1/B2 maps to physical motherboard slots (check manual).
- **Rank** — memory ranks can affect maximum stable frequency with 4 sticks.

The command intentionally excludes serial-number fields to make sharing outputs safer. If you run full `sudo dmidecode -t memory`, redact system/memory serial numbers and UUIDs before posting.

## 3 — Identify motherboard manufacturer and model

```bash
sudo dmidecode -s baseboard-manufacturer
sudo dmidecode -s baseboard-product-name
sudo dmidecode -s baseboard-version
```

Search the **official motherboard manufacturer support page/manual** for that exact model and revision. Confirm:

- DDR4 UDIMM type and maximum per slot / total supported capacity.
- Number of physical DIMM slots (2 or 4).
- Recommended dual-channel slots (usually A2/B2, but **verify manual**).
- Compatible 2×16 GB memory kits and whether XMP/DOCP is needed for advertised speeds.
- ECC UDIMM capability only if considering ECC RAM (do not assume ECC memory is supported just because the CPU supports it).

## 4 — Inspect BIOS-reported memory support

```bash
sudo dmidecode -t 16 | grep -E 'Maximum Capacity:|Number Of Devices:'
```

Treat this as advisory: firmware can report incorrect values. Cross-check the manual.

## 5 — Check current RAM and storage media

```bash
free -h
lsblk -d -o NAME,SIZE,ROTA,MODEL,TRAN
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS
```

`ROTA=0` usually denotes SSD/NVMe (non-rotating); `ROTA=1` denotes a spinning HDD. Virtualized devices and some controllers can misreport; confirm physical model if needed.

## Purchase decision examples

| Installed RAM | Practical 32 GB option | Note |
|---|---|---|
| 2×8 GB, 2-slot board | Replace with matched 2×16 GB kit | No free slots |
| 2×8 GB, 4-slot board | Prefer matched 2×16 GB replacement for predictable compatibility | Adding 2×8 can work, but mixing memory and 4-DIMM loads may reduce clocks |
| 1×16 GB, free matching slot | Add identical 1×16 GB if exact part available, or matched 2×16 GB | Kits are more predictable |
| 1×16 GB with mismatched part | Matched 2×16 GB kit | Avoid unstable or mismatched timing |

**Tentative shopping target:** 32 GB (2×16 GB) **DDR4-3200 desktop UDIMM** kit, generally non-ECC unless the motherboard explicitly supports your desired ECC module type. No specific SKU is safe to recommend until board and installed DIMM outputs are known.

## Post-upgrade verification

1. Power off, unplug and follow motherboard/ESD precautions; install DIMMs in the board's recommended slots.
2. Boot to BIOS and check total memory, detected speed and training/compatibility. Enable a rated memory profile only if supported and stable.
3. Boot Ubuntu and run `free -h` and `sudo dmidecode -t 17` again.
4. Run a memory diagnostic such as Memtest86+ from a suitable boot environment, where available, especially if enabling high memory speeds or adding mixed modules.
5. Monitor kernel logs for hardware errors after the upgrade.

## Quick read-only script

```bash
bash scripts/inspect_ubuntu_hardware.sh
```

This prints redacted-style summary fields and does **not** change BIOS, memory configuration, or disks. The script may prompt for sudo to read DMI information.

Ubuntu references: https://manpages.ubuntu.com/manpages/bionic/man8/dmidecode.8.html and https://manpages.ubuntu.com/manpages/jammy/man1/lshw.1.html
