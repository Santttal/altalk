"""
Окно настроек Altalk на GTK 4 + libadwaita (стандартный тулкит GNOME/Ubuntu).

Запускается системным /usr/bin/python3: текущие значения приходят JSON-ом в stdin,
сохранённые уходят JSON-ом в stdout, так что ключ не попадает в аргументы процесса.
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


def check_api_key(api_key, model):
    if not api_key:
        return "Впиши ключ OpenAI"
    request = urllib.request.Request(
        f"https://api.openai.com/v1/models/{urllib.parse.quote(model)}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        urllib.request.urlopen(request, timeout=15).close()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return "OpenAI не принял ключ — проверь, что он скопирован целиком"
        if e.code in (403, 404):
            return f"Модель {model} недоступна для этого ключа"
    except OSError:
        pass  # offline: save without the check rather than block the user
    return None


class SettingsWindow(Adw.ApplicationWindow):
    def __init__(self, app, current):
        super().__init__(application=app, title="Настройки Altalk", default_width=480, resizable=False)
        self.result = None

        self.models = list(current.get("models") or [])
        model = current.get("model") or (self.models[0] if self.models else "")
        if model not in self.models:
            self.models.insert(0, model)

        self.key_row = Adw.PasswordEntryRow(title="Ключ API")
        self.key_row.set_text(current.get("key", ""))
        self.key_row.connect("entry-activated", self.on_save)
        self.model_row = Adw.ComboRow(title="Модель распознавания", model=Gtk.StringList.new(self.models))
        self.model_row.set_selected(self.models.index(model))

        group = Adw.PreferencesGroup(title="OpenAI", description=f"Хранится в {current.get('path', '')}")
        group.add(self.key_row)
        group.add(self.model_row)
        page = Adw.PreferencesPage()
        page.add(group)

        cancel_button = Gtk.Button(label="Отмена")
        cancel_button.connect("clicked", lambda _button: self.close())
        self.save_button = Gtk.Button(label="Сохранить")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self.on_save)
        header = Adw.HeaderBar(show_end_title_buttons=False)
        header.pack_start(cancel_button)
        header.pack_end(self.save_button)

        self.banner = Adw.Banner()
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_top_bar(self.banner)
        view.set_content(page)
        self.set_content(view)

        shortcuts = Gtk.ShortcutController()
        shortcuts.add_shortcut(
            Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Escape"), Gtk.NamedAction.new("window.close"))
        )
        self.add_controller(shortcuts)

    def on_save(self, *_args):
        key = self.key_row.get_text().strip()
        model = self.models[self.model_row.get_selected()]
        self.save_button.set_sensitive(False)
        self.save_button.set_label("Проверяю…")
        # The HTTP check runs off the GTK thread so the window keeps redrawing.
        threading.Thread(target=self.check, args=(key, model), daemon=True).start()

    def check(self, key, model):
        GLib.idle_add(self.on_checked, key, model, check_api_key(key, model))

    def on_checked(self, key, model, error):
        self.save_button.set_sensitive(True)
        self.save_button.set_label("Сохранить")
        if error:
            self.banner.set_title(error)
            self.banner.set_revealed(True)
            return False
        self.result = {"key": key, "model": model}
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
