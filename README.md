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

`altalk` — один исполняемый файл: Python и все библиотеки внутри. Окно настроек берёт GTK 4 и
libadwaita из системы — они уже есть в Ubuntu с GNOME (на них работает сам GNOME Shell),
а где их нет, `.deb` поставит их как зависимости.

```bash
sudo apt install ./altalk_0.1.0_amd64.deb      # как пакет: меню приложений, /usr/bin/altalk
# или без установки — скопировать файл и запустить:
cp altalk ~/.local/bin/ && altalk --install    # --install добавляет Altalk в меню приложений
```

При первом запуске откроются настройки — вставь ключ OpenAI (хранится в
`~/.config/altalk/.env`). И один раз дай доступ к клавиатуре:

```bash
sudo usermod -aG input $USER   # Alt читается из /dev/input; после этого перелогиниться
```

Иконку в трее на GNOME показывает расширение AppIndicator (в Ubuntu включено по умолчанию),
в KDE — штатно. Нужна glibc 2.39+ (Ubuntu 24.04 и новее); проверено на Ubuntu 24.04, GNOME, Wayland.

## Запуск

Из меню приложений — **Altalk**, или `altalk` в терминале. Закрыть — «Выйти» в меню трея.
Второй экземпляр не запустится — покажет уведомление «Уже запущен».
Лог: `~/.local/state/altalk/altalk.log`.

## Сборка

```bash
./packaging/build.sh   # → dist/altalk и dist/altalk_0.1.0_amd64.deb
```

Скрипт сам создаёт `build/venv` и ставит туда зависимости и PyInstaller. На машине сборки
нужен `libportaudio2` — он вкладывается внутрь `altalk`, пользователям его ставить не надо.

Из исходников без сборки: `python3 -m pip install -r requirements.txt`, ключ в `.env` рядом
со скриптом (`cp .env.example .env`), затем `python3 icon2.py`.
