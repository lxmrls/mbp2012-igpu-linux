#!/bin/bash
# Installer for the MacBookPro10,1 iGPU-on-Linux setup.
# Drives the internal Retina panel from the Intel iGPU via a synthetic VBT,
# powers the discrete NVIDIA GPU off by default, and adds a GRUB entry that
# keeps the dGPU on for external displays. Run from the repo root:
#     sudo bash scripts/install.sh
#
# No macOS step is needed: the gmux hook installed in step 3 flips the display
# mux to the iGPU from Linux at boot, whatever the firmware's gpu-power-prefs says.
set -e
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
U="${SUDO_USER:-$(logname)}"; UH="$(getent passwd "$U" | cut -d: -f6)"
REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
echo "installing for user '$U' (home $UH) from $REPO"

echo "== 1. VBT firmware =="
install -d -m0755 /lib/firmware/i915
install -m0644 "$REPO/vbt/vbt.bin" /lib/firmware/i915/vbt.bin

echo "== 2. GRUB: load the VBT on every entry (prevents a black/green internal panel) =="
if ! grep -q 'i915.vbt_firmware' /etc/default/grub; then
  sed -i 's|^\(GRUB_CMDLINE_LINUX_DEFAULT="[^"]*\)"|\1 i915.vbt_firmware=i915/vbt.bin"|' /etc/default/grub
fi
# make the boot menu visible so the External Display entry is selectable
sed -i 's/^GRUB_TIMEOUT_STYLE=hidden/GRUB_TIMEOUT_STYLE=menu/' /etc/default/grub || true
grep -q '^GRUB_TIMEOUT=0' /etc/default/grub && sed -i 's/^GRUB_TIMEOUT=0/GRUB_TIMEOUT=5/' /etc/default/grub || true

echo "== 3. boot-time mux flip: gmuxctl + modprobe install hook =="
# Apple's firmware leaves the panel on the NVIDIA GPU unless a macOS-only NVRAM
# variable says otherwise (and macOS resets that variable on every boot). i915
# only discovers the eDP panel at probe time, so the hook flips the mux to Intel
# right before i915 loads, then powers the dGPU off via vga_switcheroo.
command -v gcc >/dev/null || apt-get install -y gcc libc6-dev
gcc -static -O2 -Wall -o /tmp/gmuxctl "$REPO/gmux/gmuxctl.c"
install -m0755 /tmp/gmuxctl /usr/local/sbin/gmuxctl; rm -f /tmp/gmuxctl
install -m0755 "$REPO/gmux/gmux-igd-hook" /usr/local/sbin/gmux-igd-hook
install -m0644 "$REPO/gmux/gmux-igd.conf" /etc/modprobe.d/gmux-igd.conf

echo "== 4. dGPU control helper + passwordless status =="
install -m0755 "$REPO/tray/dgpu-ctl" /usr/local/bin/dgpu-ctl
echo "$U ALL=(root) NOPASSWD: /usr/local/bin/dgpu-ctl" > /etc/sudoers.d/dgpu-ctl
chmod 0440 /etc/sudoers.d/dgpu-ctl
visudo -c -f /etc/sudoers.d/dgpu-ctl

echo "== 5. power the dGPU off on normal boots (skip when dgpu_keep_on is set) =="
cat > /etc/systemd/system/dgpu-off.service <<'UNIT'
[Unit]
Description=Power off discrete GPU (iGPU-only daily driver)
After=graphical.target
ConditionPathExists=/sys/kernel/debug/vgaswitcheroo/switch
ConditionKernelCommandLine=!dgpu_keep_on
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'grep -q "IGD:+" /sys/kernel/debug/vgaswitcheroo/switch && /usr/local/bin/dgpu-ctl off || true'
[Install]
WantedBy=graphical.target
UNIT
systemctl daemon-reload
systemctl enable dgpu-off.service

echo "== 6. External Display GRUB entry (dGPU stays on for a monitor) =="
BLOCK=$(sed -n "/^menuentry 'Ubuntu' /,/^}/p" /boot/grub/grub.cfg)
LINUX=$(echo "$BLOCK"  | grep -m1 '^[[:space:]]*linux[[:space:]]')
INITRD=$(echo "$BLOCK" | grep -m1 '^[[:space:]]*initrd[[:space:]]')
ROOTLINE=$(echo "$BLOCK" | grep -m1 'search --no-floppy')
cat > /etc/grub.d/40_custom <<EOF
#!/bin/sh
exec tail -n +3 \$0
menuentry "Ubuntu (External Display — dGPU on)" --class ubuntu {
	recordfail
	load_video
	gfxmode \$linux_gfx_mode
	insmod gzio
	insmod part_gpt
	insmod ext2
	${ROOTLINE}
	${LINUX} dgpu_keep_on
	${INITRD}
}
EOF
chmod 0755 /etc/grub.d/40_custom
update-grub

echo "== 7. status tray applet (needs: apt install gir1.2-ayatanaappindicator3-0.1) =="
apt-get install -y gir1.2-ayatanaappindicator3-0.1 || echo "  (install the AppIndicator lib manually if this failed)"
install -D -m0755 -o "$U" -g "$U" "$REPO/tray/dgpu-tray.py" "$UH/.local/bin/dgpu-tray.py"
install -d -m0755 -o "$U" -g "$U" "$UH/.config/autostart"
sed "s|Exec=.*|Exec=python3 $UH/.local/bin/dgpu-tray.py|" "$REPO/tray/dgpu-tray.desktop" \
  > "$UH/.config/autostart/dgpu-tray.desktop"
chown "$U:$U" "$UH/.config/autostart/dgpu-tray.desktop"

echo
echo "DONE. Reboot. Default entry = dGPU off (battery); pick 'External Display' for a monitor."
echo "Uninstall the mux flip alone with: sudo rm /etc/modprobe.d/gmux-igd.conf"
