#!/usr/bin/env python3
"""Status-only tray applet for the MacBookPro10,1 iGPU/dGPU setup.

The iGPU always drives the internal panel. This applet only REPORTS state —
it never changes GPU power, because runtime power changes to the discrete GPU
crash this hardware. Choose dGPU on/off at the GRUB menu instead:
  - default entries      -> dGPU off (battery)
  - "External Display"   -> dGPU on  (drives an external monitor)

Status is read-only via /usr/local/bin/dgpu-ctl status and world-readable sysfs.
"""
import gi, glob, subprocess
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import Gtk, GLib, AyatanaAppIndicator3 as AppIndicator

HELPER = "/usr/local/bin/dgpu-ctl"
ICON_OFF = "video-display-symbolic"
ICON_ON  = "video-joined-displays-symbolic"

def dgpu_state():
    """Read-only status via the helper. Returns 'on'/'off'/'?'. Never writes."""
    try:
        out = subprocess.run(["sudo", "-n", HELPER, "status"],
                             capture_output=True, text=True, timeout=10).stdout
        if "dgpu=on" in out:  return "on"
        if "dgpu=off" in out: return "off"
    except Exception:
        pass
    return "?"

def external_monitor():
    """World-readable check of the dGPU's external connectors."""
    for path in glob.glob("/sys/class/drm/card2-*/status"):
        try:
            if open(path).read().strip() == "connected":
                name = path.split("/")[-2].split("-", 1)[1]  # e.g. DP-1, HDMI-A-1
                if not name.startswith("eDP"):
                    return name
        except Exception:
            pass
    return None

def cpu_temp():
    for n in glob.glob("/sys/class/hwmon/hwmon*/name"):
        try:
            if open(n).read().strip() == "coretemp":
                return f"{int(open(n.replace('name','temp1_input')).read())//1000}°C"
        except Exception:
            pass
    return "?"

class DgpuTray:
    def __init__(self):
        self.ind = AppIndicator.Indicator.new(
            "dgpu-tray", ICON_OFF, AppIndicator.IndicatorCategory.HARDWARE)
        self.ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.menu = Gtk.Menu()
        self.i_display = self._info("Display: —")
        self.i_dgpu    = self._info("dGPU: —")
        self.i_ext     = self._info("External: —")
        self.i_cpu     = self._info("CPU: —")
        self.i_hint    = self._info("Switch dGPU at the GRUB menu (reboot)")
        i_refresh = Gtk.MenuItem(label="Refresh"); i_refresh.connect("activate", lambda *_: self.refresh())
        i_quit    = Gtk.MenuItem(label="Quit");    i_quit.connect("activate", Gtk.main_quit)
        for w in (self.i_display, self.i_dgpu, self.i_ext, self.i_cpu,
                  Gtk.SeparatorMenuItem(), self.i_hint,
                  Gtk.SeparatorMenuItem(), i_refresh, i_quit):
            self.menu.append(w)
        self.menu.show_all()
        self.ind.set_menu(self.menu)
        self.refresh()
        GLib.timeout_add_seconds(5, self.refresh)

    def _info(self, text):
        it = Gtk.MenuItem(label=text); it.set_sensitive(False); return it

    def refresh(self):
        st = dgpu_state()
        on = st == "on"
        self.ind.set_icon_full(ICON_ON if on else ICON_OFF, "GPU status")
        self.ind.set_label("iGPU+dGPU" if on else "iGPU", "")
        self.i_display.set_label("Display: Intel HD 4000 (iGPU)")
        self.i_dgpu.set_label({"on": "dGPU: ON (external-capable)",
                               "off": "dGPU: OFF (saving power)",
                               "?": "dGPU: (status unavailable)"}[st])
        ext = external_monitor()
        self.i_ext.set_label(f"External: {ext} connected" if ext else "External: none")
        self.i_cpu.set_label(f"CPU: {cpu_temp()}")
        return True

if __name__ == "__main__":
    DgpuTray()
    Gtk.main()
