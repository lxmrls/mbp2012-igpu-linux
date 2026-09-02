# MacBook Pro 2012 Retina — Intel iGPU on Linux

Run the mid-2012 15" Retina MacBook Pro (**MacBookPro10,1**, Ivy Bridge, Intel HD 4000 +
NVIDIA GK107 GT 650M) as an efficient Linux daily driver: drive the internal 2880×1800
Retina panel from the **Intel iGPU** and power the **discrete NVIDIA GPU off**, saving
roughly **10–15 W** and running noticeably cooler.

The core of this is a **hand-built Intel VBT** (Video BIOS Table) that makes the `i915`
driver light the internal eDP panel — something Apple's firmware normally prevents on Linux.
As far as I can tell this is the first working VBT published for this model.

> Tested on Ubuntu 22.04, kernel 6.8. Kernel-agnostic — the VBT works the same on newer
> kernels and other distros (Arch, Fedora, etc.).

## The problem

On this machine the internal panel is wired through a mux (`apple_gmux`) between the two GPUs.
Under Linux the panel lands on the NVIDIA GPU (nouveau), which cannot reclock the Kepler chip,
so it sits pinned at high power doing nothing.

You would think you could just switch the panel to the Intel iGPU. But `i915` refuses:

```
i915 0000:00:02.0: [drm] Failed to find VBIOS tables (VBT)
i915 0000:00:02.0: [drm] [ENCODER:78:DP A] failed to retrieve link info, disabling eDP
```

Apple's firmware **allocates** the Intel graphics OpRegion but never fills in the VBT — macOS
uses hardcoded `ig-platform-id` tables instead, so the firmware has no reason to generate one.
Without the VBT, `i915` doesn't know the eDP panel exists and disables it. No firmware trick
(`apple_set_os`, `gpu-power-prefs`) makes the firmware produce a VBT, because it never had one.

## The fix

Build a minimal VBT from scratch that declares the internal eDP panel on DDI port A, and hand
it to `i915` via the `i915.vbt_firmware=` kernel parameter. `vbt/mkvbt.py` generates it
(298 bytes, blocks 1/2/40/27, `child_dev_size=33` for BDB 170). `i915` accepts it and trains
the eDP link:

```
Found valid VBT firmware "i915/vbt.bin"
VBT signature "$VBT MACBOOKPRO10,1", BDB version 170
Port A VBT info: ... DP:1 eDP:1
[CONNECTOR:79:eDP-2][ENCODER:78:DP A] Link Training passed at link rate = 270000, lane count = 4
```

The second half of the problem is the display **mux**. Apple's firmware routes the panel to
the NVIDIA GPU unless the `gpu-power-prefs` NVRAM variable says otherwise, that variable can
only be written from macOS, and macOS rewrites it on every boot — so a setup that depends on
it breaks the first time you boot macOS. This repo instead flips the mux **from Linux**: a
`modprobe` install hook (`gmux/`) switches the gmux to Intel immediately before `i915` loads,
so `i915` finds the panel at probe time, and then powers the dGPU off through `vga_switcheroo`.
No macOS step, and nothing macOS can undo.

## Repo contents

| Path | What it is |
|------|------------|
| `vbt/vbt.bin` | The ready-to-use VBT (drop into `/lib/firmware/i915/`) |
| `vbt/mkvbt.py` | Generator that reproduces `vbt.bin` byte-for-byte |
| `vbt/panel.edid` | The panel EDID the timings came from |
| `gmux/gmuxctl.c` | Tiny static tool that drives the Apple gmux mux registers (status / igd / dis) |
| `gmux/gmux-igd-hook` | modprobe install hook: mux→Intel before `i915`, then dGPU off after both drivers bind |
| `gmux/gmux-igd.conf` | `/etc/modprobe.d` rules that route `i915` and `nouveau` loads through the hook |
| `tray/dgpu-ctl` | Root helper: report / power the dGPU on/off |
| `tray/dgpu-tray.py` | GNOME AppIndicator **status** applet (read-only) |
| `scripts/install.sh` | Installs the VBT, GRUB entries, off-by-default service, and applet |

## Install

1. Run the installer (Ubuntu / Debian-style system with `initramfs-tools` and GRUB):
   ```
   git clone https://github.com/lxmrls/mbp2012-igpu-linux
   cd mbp2012-igpu-linux
   sudo bash scripts/install.sh
   ```
2. Reboot. The default GRUB entry runs the iGPU with the dGPU off. Pick
   **"Ubuntu (External Display — dGPU on)"** when you want an external monitor.

No macOS step is required. (If you had previously set `gpu-power-prefs` from macOS, it is
harmless either way — the hook just finds the mux already on Intel.)

Verify: `glxinfo -B | grep renderer` should say **Mesa Intel(R) HD Graphics 4000**, and
`sudo cat /sys/kernel/debug/vgaswitcheroo/switch` should show `IGD:+` and `DIS: :Off`.

## Usage

- **Battery / daily:** boot normally. iGPU drives the panel; dGPU off; ~10–15 W saved, ~10 °C cooler.
- **External monitor:** boot the **External Display** entry, then plug in. The HDMI / DisplayPort /
  Thunderbolt ports are wired to the NVIDIA GPU, so they need it powered.
- **Tray applet:** read-only status (render GPU, dGPU state, external monitor, CPU temp).

## How the boot-time mux flip works

`i915` only creates the eDP connector if it can talk to the panel while it probes, so the mux
has to be on Intel *before* the driver loads. On Ubuntu the GPU drivers are loaded from the
root filesystem by udev a few seconds into boot (not from the initramfs), which makes a
`modprobe` **install rule** the right hook point:

```
install i915    /usr/local/sbin/gmux-igd-hook i915    $CMDLINE_OPTS
install nouveau /usr/local/sbin/gmux-igd-hook nouveau $CMDLINE_OPTS
```

The hook: flips the gmux (DDC/AUX, display, external) to Intel with `gmuxctl igd` → loads
`apple_gmux` and `i915` → loads `nouveau` → once both drivers are bound, writes `IGD` to
`vga_switcheroo`, which switches and suspends the dGPU through nouveau's own power path. It
runs inside the `i915` udev event, before Plymouth can grab the DRM device (and deactivates
Plymouth and retries if the switch is refused as busy).

Kernel command-line switches:

| Flag | Effect |
|------|--------|
| `nomodeset`, `i915.modeset=0`, `gmux_noflip` | Don't touch the mux (recovery mode boots exactly as before) |
| `dgpu_keep_on` | Flip the mux but leave the dGPU powered (the External Display entry) |

Diagnose with `dmesg | grep gmux-igd-hook`. Two harmless kernel warnings appear at boot: one
from `i915` (`vbt_get_panel_type`, because the VBT deliberately has no LFP block) and one from
nouveau's display engine timing out while being suspended.

**Other distros:** the hook assumes the GPU drivers load from the root filesystem. If your
initramfs does early KMS (Arch/Fedora with `i915` in the initramfs modules list), copy
`gmuxctl` and the hook into the initramfs too, or drop `i915` from early KMS.

## Known limitations

These are hardware / nouveau realities, not bugs in this setup:

- **No runtime GPU power toggling.** Powering the dGPU on at runtime wedges it
  (`Unable to change power state from D3hot to D0`) and can crash the machine. dGPU on/off is a
  **boot-time** choice (the two GRUB entries). The tray is status-only for this reason.
- **External resolution:** DisplayPort/Thunderbolt does 4K@30; HDMI is limited to ≤1440p
  (nouveau's Kepler HDMI SCDC path fails at 4K, `ret:-22`). Use DP for 4K.
- **No CUDA / no dGPU compute.** nouveau only; the proprietary driver (390) won't build on
  modern kernels. The dGPU's only real use here is external displays.
- **Recovery:** if the internal panel ever comes up black, pick the GRUB *recovery mode* entry
  (or add `gmux_noflip`) — the hook then leaves the mux alone and the machine boots on the
  NVIDIA GPU as stock. Uninstall the flip entirely with `sudo rm /etc/modprobe.d/gmux-igd.conf`.
  Note that with the panel on nouveau, this model green-screens at boot fairly often on its own
  (nouveau `core notifier timeout`); that is the stock behaviour, not this setup.

## Credits

Reverse-engineered and built interactively with [Claude Code](https://claude.com/claude-code).
VBT structure from the Linux kernel `intel_vbt_defs.h`; validated with `intel_vbt_decode`
(intel-gpu-tools). Shared in the hope it helps other 2012 Retina owners.

## License

MIT — see [LICENSE](LICENSE).
