#!/usr/bin/env python3
"""Status-only tray applet for the MacBookPro10,1 iGPU/dGPU setup.

The iGPU always drives the internal panel. This applet only REPORTS state —
it never changes GPU power, because runtime power changes to the discrete GPU
crash this hardware. Choose dGPU on/off at the GRUB menu instead:
  - default entries      -> dGPU off (battery)
  - "External Display"   -> dGPU on  (drives an external monitor)

Status is read-only via /usr/local/bin/dgpu-ctl status and world-readable sysfs.

Also hosts the battery "Charge limit" control (SMC BCLM via applesmc-next):
presets 50-100% and a slider dialog. Writes go through /usr/local/bin/battcap
(sudoers NOPASSWD), which enforces the 50-100 range as root.
"""
import gi, glob, os, subprocess, threading
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import Gtk, GLib, AyatanaAppIndicator3 as AppIndicator

HELPER = "/usr/local/bin/dgpu-ctl"
BATTCAP = "/usr/local/bin/battcap"          # root helper (sudoers NOPASSWD), clamps 50-100
CAP_END = "/sys/class/power_supply/BAT0/charge_control_end_threshold"  # applesmc-next
CAP_MIN, CAP_MAX, CAP_DEFAULT = 50, 100, 80
CAP_PRESETS = (50, 60, 70, 80, 90, 100)
CAL_FILE = os.path.expanduser("~/.config/dgpu-tray/calibrating")  # holds the cap to restore
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

def charge_cap():
    """Current SMC charge cap (%), read from world-readable sysfs. None if unsupported."""
    try:
        return int(open(CAP_END).read())
    except Exception:
        return None

def set_charge_cap(pct):
    """Write the cap via the root helper. Returns (ok, message)."""
    pct = max(CAP_MIN, min(CAP_MAX, int(pct)))
    try:
        r = subprocess.run(["sudo", "-n", BATTCAP, "set", str(pct)],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)

def smc_soc():
    """The SMC's own state of charge (%), i.e. what the charge cap is enforced against.
    Via the root helper (reads applesmc key BRSC). None if unavailable."""
    try:
        out = subprocess.run(["sudo", "-n", BATTCAP, "get"], capture_output=True, text=True, timeout=5).stdout
        for tok in out.split():
            if tok.startswith("soc="):
                return int(tok[4:])
    except Exception:
        pass
    return None

def battery_state():
    """'83% (SMC) · Full · gauge 92%' summary, or None. The SMC figure is authoritative for
    the cap; the gauge ratio (charge_now/charge_full) drifts while the pack is capped."""
    try:
        b = "/sys/class/power_supply/BAT0/"
        st = open(b + "status").read().strip()
        now = int(open(b + "charge_now").read()); full = int(open(b + "charge_full").read())
        gauge = now * 100 // full
    except Exception:
        return None
    soc = smc_soc()
    if soc is None:
        return f"{st} {gauge}%"
    if abs(soc - gauge) > 2:
        return f"{soc}% (SMC) · {st} · gauge {gauge}%"
    return f"{soc}% (SMC) · {st}"

def bt_powered():
    """Bluetooth adapter power state via bluetoothctl (unprivileged). None if no adapter."""
    try:
        out = subprocess.run(["bluetoothctl", "show"], capture_output=True, text=True, timeout=5).stdout
        if "Powered: yes" in out: return True
        if "Powered: no" in out: return False
    except Exception:
        pass
    return None

def bt_set(on):
    subprocess.run(["bluetoothctl", "power", "on" if on else "off"], capture_output=True, text=True, timeout=10)

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
        self.i_batt    = self._info("Battery: —")
        self.m_cap, self.cap_items = self._build_cap_menu()
        i_refresh = Gtk.MenuItem(label="Refresh"); i_refresh.connect("activate", lambda *_: self.refresh())
        i_quit    = Gtk.MenuItem(label="Quit");    i_quit.connect("activate", Gtk.main_quit)
        for w in (self.i_display, self.i_dgpu, self.i_ext, self.i_cpu,
                  Gtk.SeparatorMenuItem(), self.i_batt, self.m_cap,
                  Gtk.SeparatorMenuItem(), self.i_hint,
                  Gtk.SeparatorMenuItem(), i_refresh, i_quit):
            self.menu.append(w)
        self.menu.show_all()
        self.ind.set_menu(self.menu)
        self.refresh()
        GLib.timeout_add_seconds(5, self.refresh)

    def _info(self, text):
        it = Gtk.MenuItem(label=text); it.set_sensitive(False); return it

    def _build_cap_menu(self):
        """'Charge limit' submenu: preset radio items + a slider dialog for any value."""
        top = Gtk.MenuItem(label="Charge limit: —")
        sub = Gtk.Menu(); group = None; items = {}
        self._cap_updating = False
        for p in CAP_PRESETS:
            it = Gtk.RadioMenuItem.new_with_label_from_widget(group, f"{p}%")
            group = group or it
            it.connect("toggled", self._on_cap_preset, p)
            sub.append(it); items[p] = it
        sub.append(Gtk.SeparatorMenuItem())
        custom = Gtk.MenuItem(label="Custom…"); custom.connect("activate", self._on_cap_custom)
        sub.append(custom)
        sub.append(Gtk.SeparatorMenuItem())
        self.i_cal = Gtk.MenuItem(label="Calibrate…"); self.i_cal.connect("activate", self._on_calibrate)
        sub.append(self.i_cal)
        top.set_submenu(sub)
        return top, items

    def _cal_pending(self):
        """Cap to restore if a calibration cycle is in progress, else None."""
        try:
            v = int(open(CAL_FILE).read().strip())
            return v if CAP_MIN <= v <= CAP_MAX else None
        except Exception:
            return None

    def _on_calibrate(self, *_):
        pending = self._cal_pending()
        if pending is not None:                      # finish: restore the saved cap
            ok, msg = set_charge_cap(pending)
            if ok:
                try: os.remove(CAL_FILE)
                except Exception: pass
            self.refresh(); return
        cur = charge_cap() or CAP_DEFAULT
        dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL,
                                text="Calibrate the battery gauge",
                                secondary_text=("This lifts the charge limit to 100% so the pack can re-learn its true full "
                                                "capacity. Then:\n\n"
                                                "1. Charge to 100% and leave it on the charger about an hour.\n"
                                                "2. Unplug and run it down until it shuts off.\n"
                                                "3. Charge back to 100% without interruption.\n\n"
                                                f"Afterwards pick “Finish calibration” here to restore the {cur}% limit."))
        def on_resp(d, resp):
            d.destroy()
            if resp != Gtk.ResponseType.OK: return
            ok, msg = set_charge_cap(CAP_MAX)
            if ok:
                try:
                    os.makedirs(os.path.dirname(CAL_FILE), exist_ok=True)
                    open(CAL_FILE, "w").write(str(cur))
                except Exception: pass
            self.refresh()
        dlg.connect("response", on_resp); dlg.show()

    def _on_cap_preset(self, item, pct):
        if self._cap_updating or not item.get_active():
            return
        self._apply_cap(pct)

    def _on_cap_custom(self, *_):
        cur = charge_cap() or CAP_DEFAULT
        dlg = Gtk.Dialog(title="Battery charge limit", modal=False)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Apply", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        box = dlg.get_content_area(); box.set_spacing(8); box.set_border_width(12)
        box.add(Gtk.Label(label="Stop charging at:"))
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, CAP_MIN, CAP_MAX, 1)
        scale.set_value(cur); scale.set_digits(0); scale.set_size_request(320, -1)
        scale.set_hexpand(True)
        for m in (CAP_MIN, CAP_DEFAULT, CAP_MAX):
            scale.add_mark(m, Gtk.PositionType.BOTTOM, f"{m}%")
        box.add(scale)
        box.add(Gtk.Label(label=f"{CAP_MIN}% minimum · {CAP_DEFAULT}% recommended · 100% = no limit"))
        dlg.show_all()
        def on_resp(d, resp):
            if resp == Gtk.ResponseType.OK:
                self._apply_cap(int(round(scale.get_value())))
            d.destroy()
        dlg.connect("response", on_resp)

    def _apply_cap(self, pct):
        ok, msg = set_charge_cap(pct)
        if not ok:
            d = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK,
                                  text="Could not set charge limit", secondary_text=msg or "unknown error")
            d.connect("response", lambda w, *_: w.destroy()); d.show()
        self.refresh()

    def _sync_cap_menu(self):
        cap = charge_cap()
        if cap is None:
            self.m_cap.set_label("Charge limit: unavailable"); self.m_cap.set_sensitive(False); return
        self.m_cap.set_sensitive(True)
        pending = self._cal_pending()
        if pending is not None:
            self.m_cap.set_label(f"Charge limit: {cap}% · calibrating")
            self.i_cal.set_label(f"Finish calibration (restore {pending}%)")
        else:
            self.m_cap.set_label(f"Charge limit: {cap}%" + ("" if cap < 100 else " (off)"))
            self.i_cal.set_label("Calibrate…")
        self._cap_updating = True
        try:
            for p, it in self.cap_items.items():
                it.set_active(p == cap)
            if cap not in self.cap_items:           # custom value: no preset active
                for it in self.cap_items.values(): it.set_active(False)
        finally:
            self._cap_updating = False

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
        bs = battery_state()
        self.i_batt.set_label(f"Battery: {bs}" if bs else "Battery: —")
        self._sync_cap_menu()
        return True

class BtTray:
    """Second top-bar indicator: the standard Bluetooth icon, lit when the adapter is on.
    Left-click opens a one-item menu (indicators can't act on a plain click); middle-click
    toggles directly."""
    ICON_ON, ICON_OFF = "bluetooth-active-symbolic", "bluetooth-disabled-symbolic"

    def __init__(self):
        self.ind = AppIndicator.Indicator.new("bt-tray", self.ICON_OFF, AppIndicator.IndicatorCategory.HARDWARE)
        self.ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.menu = Gtk.Menu()
        self.i_toggle = Gtk.MenuItem(label="Bluetooth"); self.i_toggle.connect("activate", self.toggle)
        self.menu.append(self.i_toggle); self.menu.show_all()
        self.ind.set_menu(self.menu)
        self.ind.set_secondary_activate_target(self.i_toggle)   # middle-click = toggle
        self.state = None
        self.refresh()
        GLib.timeout_add_seconds(5, self.refresh)

    def toggle(self, *_):
        if self.state is not None:
            bt_set(not self.state)
        GLib.timeout_add(800, lambda: (self.refresh(), False)[1])

    def refresh(self):
        st = bt_powered()
        self.state = st
        if st is None:
            self.ind.set_status(AppIndicator.IndicatorStatus.PASSIVE); return True
        self.ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.ind.set_icon_full(self.ICON_ON if st else self.ICON_OFF, "Bluetooth on" if st else "Bluetooth off")
        self.i_toggle.set_label("Turn Bluetooth off" if st else "Turn Bluetooth on")
        return True

TB_POWER = "/usr/local/sbin/tb-power"          # root helper (sudoers NOPASSWD)
ICON_DIR = os.path.expanduser("~/.local/share/dgpu-tray/icons")

def tb_powered():
    """True/False from the PCI bus (no root); None if the helper is missing."""
    if not os.path.exists(TB_POWER):
        return None
    try:
        out = subprocess.run([TB_POWER, "status"], capture_output=True, text=True, timeout=5).stdout.strip()
        return out == "on"
    except Exception:
        return None

class TbTray:
    """Thunderbolt controller power: the Yaru bolt icon, crossed out while the controller is
    powered off (tb-power). Left-click = one-item menu, middle-click = toggle. Powering on
    takes a few seconds (firmware link training), so it runs off the main loop."""
    def __init__(self):
        self.ind = AppIndicator.Indicator.new("tb-tray", "thunderbolt-off-symbolic", AppIndicator.IndicatorCategory.HARDWARE)
        self.ind.set_icon_theme_path(ICON_DIR)
        self.ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.menu = Gtk.Menu()
        self.i_toggle = Gtk.MenuItem(label="Thunderbolt"); self.i_toggle.connect("activate", self.toggle)
        self.menu.append(self.i_toggle); self.menu.show_all()
        self.ind.set_menu(self.menu)
        self.ind.set_secondary_activate_target(self.i_toggle)
        self.state = None; self.busy = False
        self.refresh()
        GLib.timeout_add_seconds(5, self.refresh)

    def toggle(self, *_):
        if self.state is None or self.busy:
            return
        want_on = not self.state
        self.busy = True
        self.i_toggle.set_label("Powering Thunderbolt on…" if want_on else "Powering Thunderbolt off…")
        self.i_toggle.set_sensitive(False)
        def work():
            subprocess.run(["sudo", "-n", TB_POWER, "on" if want_on else "off"], capture_output=True, text=True, timeout=60)
            GLib.idle_add(self._done)
        threading.Thread(target=work, daemon=True).start()

    def _done(self):
        self.busy = False; self.i_toggle.set_sensitive(True); self.refresh(); return False

    def refresh(self):
        if self.busy:
            return True
        st = tb_powered(); self.state = st
        if st is None:
            self.ind.set_status(AppIndicator.IndicatorStatus.PASSIVE); return True
        self.ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.ind.set_icon_full("thunderbolt-on-symbolic" if st else "thunderbolt-off-symbolic",
                               "Thunderbolt on" if st else "Thunderbolt off")
        self.i_toggle.set_label("Power Thunderbolt off" if st else "Power Thunderbolt on")
        return True

if __name__ == "__main__":
    DgpuTray()
    BtTray()
    TbTray()
    Gtk.main()
