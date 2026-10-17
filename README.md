# <img src="assets/icon.png" alt="" width="40" align="top"> Altalk

![Зажми Alt и говори → отпусти — текст в буфере обмена → Ctrl+V](assets/hero.jpg)

*Зажми Left Alt и говори → отпусти → вставляй текст через `Ctrl+V`.*

**Alt + talk**: голос → текст через OpenAI API (`gpt-4o-mini-transcribe`, ~$0.003/мин). Linux.

- **Push-to-talk**: держишь Left Alt — пишет микрофон, отпустил — текст в буфер обмена
  + уведомление. Нажатие любой другой клавиши при зажатом Alt отменяет запись.
- **Трей**: в меню иконки — 5 последних транскрипций (время + начало текста). Клик по
  любой копирует её текст в буфер обмена, иконка в трее на секунду становится галочкой.
  «Выйти» закрывает приложение.
- **Настройки…** в меню трея: ключ OpenAI и модель распознавания (`gpt-4o-mini-transcribe` —
  по умолчанию, `gpt-4o-transcribe`, `gpt-transcribe`, `whisper-1`). Ключ проверяется при
  сохранении, применяется сразу, без перезапуска. Окно — стандартное GNOME (GTK 4 + libadwaita).
- **Свои термины**: `~/.config/altalk/vocabulary.json` (образец — `vocabulary.example.json`):
  `prompt` — подсказка для распознавания, `corrections` — regex-правки после него.

## Установка

Готовые файлы — на странице [Releases](https://github.com/Santttal/altalk/releases/latest).
Внутри каждого пакета один исполняемый файл `altalk` с Python и всеми библиотеками; из системы
берутся только звук (ALSA) и, для окна настроек, GTK 4 + libadwaita — менеджер пакетов
поставит их сам.

| Система | Установка |
|---|---|
| Ubuntu 22.04+, Debian 12+, Linux Mint 21+ | `sudo apt install ./altalk_0.1.0_amd64.deb` |
| Fedora, RHEL / AlmaLinux / Rocky 9 | `sudo dnf install ./altalk-0.1.0-1.x86_64.rpm` |
| Arch, Manjaro | `sudo pacman -U altalk-0.1.0-1-x86_64.pkg.tar.zst` |
| Любой Linux x86-64 с glibc 2.30+ | `install -m755 altalk-linux-x86_64 ~/.local/bin/altalk && altalk --install` |

При первом запуске откроются настройки — вставь ключ OpenAI (хранится в
`~/.config/altalk/.env`). И один раз дай доступ к клавиатуре:

```bash
sudo usermod -aG input $USER   # Alt читается из /dev/input; после этого перелогиниться
```

Значок в трее: в Ubuntu и KDE работает сразу. В GNOME на Fedora, Debian и Arch нужно
расширение AppIndicator (`gnome-shell-extension-appindicator`) — поставить и включить
в приложении «Расширения».

## Запуск

Из меню приложений — **Altalk**, или `altalk` в терминале. Закрыть — «Выйти» в меню трея.
Второй экземпляр не запустится — покажет уведомление «Уже запущен».
Лог: `~/.local/state/altalk/altalk.log`.

## Сборка

```bash
./packaging/build.sh   # → dist/: .deb, .rpm, .pkg.tar.zst, altalk-linux-x86_64, SHA256SUMS
```

Нужен только Docker: сборка идёт в контейнере Debian 11 (`packaging/Dockerfile`), поэтому
бинарник требует лишь glibc 2.30+ и работает на всех системах из таблицы выше.

Из исходников без сборки: `python3 -m pip install -r requirements.txt`, ключ в `.env` рядом
со скриптом (`cp .env.example .env`), затем `python3 icon2.py`.

## Лицензия

MIT — см. [LICENSE](LICENSE).
