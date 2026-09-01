#!/usr/bin/env python3
"""Build a synthetic Intel VBT (Video BIOS Table) for the MacBookPro10,1
(mid-2012 Retina 15") internal eDP panel, so the i915 driver will drive the
2880x1800 panel from the Intel HD 4000 iGPU under Linux.

Apple's firmware allocates the graphics OpRegion but never populates the VBT
mailbox (macOS uses hardcoded ig-platform-id tables instead), so i915 logs
"Failed to find VBIOS tables (VBT)" and disables eDP. This file hand-builds a
minimal VBT declaring the internal eDP panel on DDI port A, which i915 accepts.

Load it with:  i915.vbt_firmware=i915/vbt.bin   (file at /lib/firmware/i915/vbt.bin)
Validate it with:  intel_vbt_decode --file=vbt.bin   (from intel-gpu-tools)

The panel timing below is the fixed Apple "Color LCD" mode (identical across all
MacBookPro10,1 units), taken from the panel EDID's first detailed timing.
"""
import struct, os

# --- Apple Retina 15" panel timing (2880x1800 @ 60 Hz, 337.75 MHz) ---
clock10k = 33775            # pixel clock in 10 kHz units
hact, hblank = 2880, 160
vact, vblank = 1800, 52
hso, hsw = 48, 32           # hsync offset / width
vso, vsw = 3, 6             # vsync offset / width
himg, vimg = 331, 207       # image size (mm)

def blk(bid, data):
    return struct.pack('<BH', bid, len(data)) + data

# --- child_device_config: 33 bytes (BDB 170 expects the old format) ---
CHILD = 33
def child():
    c = bytearray(CHILD)
    struct.pack_into('<H', c, 0, 0x0000)   # handle
    struct.pack_into('<H', c, 2, 0x1806)   # device_type = internal eDP
    c[16] = 10                             # dvo_port = DVO_PORT_DPA (port A)
    c[24] = 0x00                           # aux_channel = AUX_A
    return bytes(c)

# --- BDB blocks: 1 general features, 2 general definitions, 40 lvds opts, 27 eDP ---
b1  = blk(1,  bytes([0x0c]) + bytes(39))                    # general features
gd  = bytearray(5); gd[4] = CHILD                           # crt_pin,dpms,boot[2],child_dev_size
b2  = blk(2,  bytes(gd) + child())                          # general definitions + eDP child
b40 = blk(40, bytes(28))                                    # LVDS options, panel_type 0
b27 = blk(27, bytes(110))                                   # eDP block (defaults)
blocks = b1 + b2 + b40 + b27

BDBHDR = 22
bdb = b'BIOS_DATA_BLOCK ' + struct.pack('<HHH', 170, BDBHDR, BDBHDR + len(blocks)) + blocks

sig = b'$VBT MACBOOKPRO10,1'
sig += b'\x00' * (20 - len(sig))
vbt = (sig + struct.pack('<HHH', 170, 48, 48 + len(bdb))
       + struct.pack('<BB', 0, 0) + struct.pack('<I', 48)
       + struct.pack('<IIII', 0, 0, 0, 0) + bdb)
cs = (-sum(vbt)) & 0xff
vbt = vbt[:26] + bytes([cs]) + vbt[27:]

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vbt.bin')
open(out, 'wb').write(vbt)
print(f"wrote {out}: {len(vbt)} bytes (blocks 1,2,40,27; child_dev_size={CHILD})")
