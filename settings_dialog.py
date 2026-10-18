"""
Окно настроек Altalk на GTK 4 + libadwaita (стандартный тулкит GNOME/Ubuntu).

Запускается системным /usr/bin/python3: текущие значения (и тексты на языке интерфейса) приходят
JSON-ом в stdin, сохранённые уходят JSON-ом в stdout, так что ключ не попадает в аргументы процесса.
Код выхода: 0 — сохранено, 1 — отмена, 3 — нет GTK 4/libadwaita.
"""

import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

EXIT_SAVED = 0
EXIT_CANCELLED = 1
EXIT_UNAVAILABLE = 3

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, GLib, Gtk
except (ImportError, ValueError):
    sys.exit(EXIT_UNAVAILABLE)


def check_api_key(api_key, model, texts):
    if not api_key:
        return texts["enter_key"]
    request = urllib.request.Request(
        f"https://api.openai.com/v1/models/{urllib.parse.quote(model)}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        urllib.request.urlopen(request, timeout=15).close()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return texts["key_rejected"]
        if e.code in (403, 404):
            return texts["model_unavailable"].format(model=model)
    except OSError:
        pass  # offline: save without the check rather than block the user
    return None


class SettingsWindow(Adw.ApplicationWindow):
    # Only widgets from libadwaita 1.1 / GTK 4.6, so it also runs on Ubuntu 22.04 and Debian 12.
    def __init__(self, app, current):
        self.texts = texts = current["texts"]
        super().__init__(application=app, title=texts["dialog_title"], default_width=480, resizable=False)
        self.result = None

        self.models = list(current.get("models") or [])
        model = current.get("model") or (self.models[0] if self.models else "")
        if model not in self.models:
            self.models.insert(0, model)

        self.key_entry = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True, valign=Gtk.Align.CENTER)
        self.key_entry.set_text(current.get("key", ""))
        self.key_entry.connect("activate", self.on_save)
        key_row = Adw.ActionRow(title=texts["api_key"])
        key_row.add_suffix(self.key_entry)
        self.model_row = Adw.ComboRow(title=texts["model"], model=Gtk.StringList.new(self.models))
        self.model_row.set_selected(self.models.index(model))
        self.languages = current["languages"]
        language_ids = [language for language, _name in self.languages]
        self.language_row = Adw.ComboRow(
            title=texts["speech_language"],
            model=Gtk.StringList.new([name for _language, name in self.languages]),
        )
        if current.get("language") in language_ids:
            self.language_row.set_selected(language_ids.index(current["language"]))

        group = Adw.PreferencesGroup(title="OpenAI", description=texts["stored_in"].format(path=current.get("path", "")))
        group.add(key_row)
        group.add(self.model_row)
        group.add(self.language_row)

        self.hotkeys = current.get("hotkeys") or []
        hotkey_ids = [hotkey for hotkey, _name in self.hotkeys]
        self.hotkey_row = Adw.ComboRow(
            title=texts["hotkey"],
            subtitle=texts["hotkey_hint"],
            model=Gtk.StringList.new([name for _hotkey, name in self.hotkeys]),
        )
        if current.get("hotkey") in hotkey_ids:
            self.hotkey_row.set_selected(hotkey_ids.index(current["hotkey"]))
        self.autostart_check = Gtk.CheckButton(active=current.get("autostart", False), valign=Gtk.Align.CENTER)
        autostart_row = Adw.ActionRow(
            title=texts["autostart"],
            subtitle=texts["autostart_hint"],
            activatable_widget=self.autostart_check,
        )
        autostart_row.add_prefix(self.autostart_check)
        general_group = Adw.PreferencesGroup(title=texts["general"])
        general_group.add(self.hotkey_row)
        general_group.add(autostart_row)

        page = Adw.PreferencesPage(vexpand=True)
        page.add(group)
        page.add(general_group)

        cancel_button = Gtk.Button(label=texts["cancel"])
        cancel_button.connect("clicked", lambda _button: self.close())
        self.save_button = Gtk.Button(label=texts["save"])
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self.on_save)
        header = Adw.HeaderBar(show_end_title_buttons=False)
        header.pack_start(cancel_button)
        header.pack_end(self.save_button)

        self.error_label = Gtk.Label(wrap=True, xalign=0, visible=False, margin_start=24, margin_end=24, margin_top=18)
        self.error_label.add_css_class("error")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(header)
        content.append(self.error_label)
        content.append(page)
        self.set_content(content)
        # Otherwise focus starts on «Отмена» (first in the header) and Enter would cancel.
        self.set_default_widget(self.save_button)
        self.set_focus(self.key_entry)

        shortcuts = Gtk.ShortcutController()
        shortcuts.add_shortcut(
            Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Escape"), Gtk.NamedAction.new("window.close"))
        )
        self.add_controller(shortcuts)

    def on_save(self, *_args):
        key = self.key_entry.get_text().strip()
        model = self.models[self.model_row.get_selected()]
        self.save_button.set_sensitive(False)
        self.save_button.set_label(self.texts["checking"])
        # The HTTP check runs off the GTK thread so the window keeps redrawing.
        threading.Thread(target=self.check, args=(key, model), daemon=True).start()

    def check(self, key, model):
        GLib.idle_add(self.on_checked, key, model, check_api_key(key, model, self.texts))

    def on_checked(self, key, model, error):
        self.save_button.set_sensitive(True)
        self.save_button.set_label(self.texts["save"])
        if error:
            self.error_label.set_text(error)
            self.error_label.set_visible(True)
            self.key_entry.grab_focus()
            return False
        self.result = {
            "key": key,
            "model": model,
            "language": self.languages[self.language_row.get_selected()][0],
            "hotkey": self.hotkeys[self.hotkey_row.get_selected()][0],
            "autostart": self.autostart_check.get_active(),
        }
        self.close()
        return False


def main():
    try:
        current = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        current = {}
    # Without an application id GTK uses the program name as the Wayland app_id,
    # so GNOME matches the window to altalk.desktop and shows the Altalk icon.
    GLib.set_prgname("altalk")
    app = Adw.Application(flags=Gio.ApplicationFlags.NON_UNIQUE)
    windows = []

    def on_activate(app):
        window = SettingsWindow(app, current)
        windows.append(window)
        window.present()

    app.connect("activate", on_activate)
    app.run([])
    if not windows or windows[0].result is None:
        return EXIT_CANCELLED
    print(json.dumps(windows[0].result))
    return EXIT_SAVED


if __name__ == "__main__":
    sys.exit(main())
