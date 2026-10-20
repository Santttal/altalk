# <img src="assets/icon.png" alt="" width="40" align="top"> Altalk

![Зажми Alt и говори → отпусти — текст в буфере обмена → Ctrl+V](assets/hero.jpg)

*Зажми Left Alt и говори → отпусти → вставляй текст через `Ctrl+V`.*

**Alt + talk**: голос → текст через OpenAI API (`gpt-4o-mini-transcribe`, ~$0.003/мин).
Linux, Windows 10/11 и macOS 11+ (на Mac вместо Alt — Option).

- **Push-to-talk**: держишь Left Alt — пишет микрофон, отпустил — текст в буфер обмена
  + уведомление. Нажатие любой другой клавиши при зажатом Alt отменяет запись (это было
  сочетание вроде Alt+Tab); клики и прокрутка мышью запись не прерывают.
- **Трей**: пока клавиша зажата, значок — белое облачко с точками, пока текст распознаётся —
  жёлтое. В меню значка — 5 последних транскрипций (время + начало текста). Клик по
  любой копирует её текст в буфер обмена, иконка в трее на секунду становится галочкой.
  «Выйти» закрывает приложение.
- **Настройки…** в меню трея: ключ OpenAI, модель распознавания (`gpt-4o-mini-transcribe` —
  по умолчанию, `gpt-4o-transcribe`, `gpt-transcribe`, `whisper-1`), язык речи (57 языков,
  которые распознаёт OpenAI; по умолчанию — язык системы), клавиша записи (Left Alt —
  по умолчанию, или Left Ctrl) и автозапуск при входе в систему. Ключ проверяется при
  сохранении, всё применяется сразу, без перезапуска. Окно — стандартное для системы:
  GTK 4 + libadwaita в Linux, обычные элементы Windows в Windows.
- **Язык интерфейса** — русский или английский, по языку системы.
- **Свои термины**: `vocabulary.json` рядом с настройками (образец — `vocabulary.example.json`):
  `prompt` — подсказка для распознавания, `corrections` — regex-правки после него.

## Установка

Готовые файлы — на странице [Releases](https://github.com/Santttal/altalk/releases/latest).
Внутри каждого пакета один исполняемый файл с Python и всеми библиотеками. В Linux из системы
берутся только звук (ALSA) и, для окна настроек, GTK 4 + libadwaita — менеджер пакетов
поставит их сам. В Windows и macOS не нужно ничего.

| Система | Установка |
|---|---|
| Windows 10, 11 (x64) | `altalk-0.4.0-setup-x64.exe` — установщик, права администратора не нужны |
| Windows без установки | `altalk-windows-x64.exe` — просто запустить |
| macOS 11+, Apple Silicon (M1 и новее) | `altalk-0.4.0-macos-arm64.dmg` — открыть и перетащить Altalk в «Программы» |
| macOS 11+, Intel | `altalk-0.4.0-macos-x86_64.dmg` — так же |
| Ubuntu 22.04+, Debian 12+, Linux Mint 21+ | `sudo apt install ./altalk_0.4.0_amd64.deb` |
| Fedora, RHEL / AlmaLinux / Rocky 9 | `sudo dnf install ./altalk-0.4.0-1.x86_64.rpm` |
| Arch, Manjaro | `sudo pacman -U altalk-0.4.0-1-x86_64.pkg.tar.zst` |
| Любой Linux x86-64 с glibc 2.30+ | `install -m755 altalk-linux-x86_64 ~/.local/bin/altalk && altalk --install` |

При первом запуске откроются настройки — вставь ключ OpenAI. Он хранится в
`~/.config/altalk/.env` (Linux), `%APPDATA%\Altalk\.env` (Windows) или
`~/Library/Application Support/Altalk/.env` (macOS).

**Linux.** Один раз дай доступ к клавиатуре:

```bash
sudo usermod -aG input $USER   # Alt читается из /dev/input; после этого перелогиниться
```

Значок в трее: в Ubuntu и KDE работает сразу. В GNOME на Fedora, Debian и Arch нужно
расширение AppIndicator (`gnome-shell-extension-appindicator`) — поставить и включить
в приложении «Расширения».

**Windows.** Файлы не подписаны, поэтому при первом запуске SmartScreen покажет «Система Windows
защитила ваш компьютер» — «Подробнее» → «Выполнить в любом случае». Новые значки Windows прячет
под стрелку «^» на панели задач — значок Altalk можно перетащить оттуда на панель. Микрофон
открывается только пока клавиша зажата, так что индикатор «микрофон используется» не висит
постоянно.

**macOS.** Приложение не подписано платным сертификатом Apple, поэтому при первом запуске macOS
его не откроет: после этой попытки — «Системные настройки» → «Конфиденциальность и безопасность» →
«Всё равно открыть» (или в Терминале `xattr -dr com.apple.quarantine /Applications/Altalk.app`).
Затем Altalk попросит два разрешения: «Мониторинг ввода» (чтобы видеть Option/Control) и микрофон.
После каждого обновления macOS считает Altalk новым приложением — разрешения придётся дать заново.

## Запуск

Из меню приложений (или «Пуск») — **Altalk**, или `altalk` в терминале Linux. Закрыть — «Выйти»
в меню трея. Второй экземпляр не запустится: в Linux покажет уведомление «Уже запущен», в Windows
и macOS откроет настройки уже запущенного.
Лог: `~/.local/state/altalk/altalk.log` (Linux), `%LOCALAPPDATA%\Altalk\altalk.log` (Windows),
`~/Library/Logs/Altalk/altalk.log` (macOS).

## Сборка

```bash
./packaging/build.sh   # → dist/: .deb, .rpm, .pkg.tar.zst, altalk-linux-x86_64,
                       #   altalk-*-setup-x64.exe, altalk-windows-x64.exe, SHA256SUMS
```

Нужен только Docker. Linux собирается в контейнере Debian 11 (`packaging/Dockerfile`), поэтому
бинарник требует лишь glibc 2.30+ и работает на всех системах из таблицы выше. Windows —
в контейнере с Wine (`packaging/Dockerfile.windows`): Python для Windows, PyInstaller
и Inno Setup для установщика.

macOS собирается только на Mac: `./packaging/build-macos.sh` → `dist/altalk-*-macos-<arch>.dmg`
для архитектуры этого Mac. Всё вместе собирает GitHub Actions (`.github/workflows/build.yml`)
на каждый push, а к опубликованному релизу сам прикладывает все файлы и `SHA256SUMS`.

Из исходников без сборки: `python3 -m pip install -r requirements.txt`, ключ в `.env` рядом
со скриптом (`cp .env.example .env`), затем `python3 icon2.py`.

## Лицензия

MIT — см. [LICENSE](LICENSE).
