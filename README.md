# <img src="assets/icon.png" alt="" width="40" align="top"> Altalk

![Зажми Alt и говори → отпусти — текст в буфере обмена → Ctrl+V](assets/hero.jpg)

*Зажми Left Alt и говори → отпусти → вставляй текст через `Ctrl+V`.*

**Alt + talk**: голос → текст через OpenAI API (`gpt-4o-mini-transcribe`, ~$0.003/мин).
Скрипт `icon2.py`, Linux.

- **Push-to-talk**: держишь Left Alt — пишет микрофон, отпустил — текст в буфер обмена
  + уведомление. Нажатие любой другой клавиши при зажатом Alt отменяет запись.
- `PROMPT` — доменные термины для API, `CORRECTIONS` — regex-правки после распознавания.
  Свои термины — в `vocabulary.json` рядом со скриптом (не в git, образец —
  `vocabulary.example.json`): `prompt` заменяет `PROMPT`, `corrections` дополняют `CORRECTIONS`.

## Установка

```bash
python3 -m pip install -r requirements.txt   # системный python3 (3.10)
cp .env.example .env                         # и вписать OPENAI_API_KEY
cp vocabulary.example.json vocabulary.json   # опционально: свои термины
```

Нужны `ffmpeg`/`ffprobe`, `notify-send`, пользователь в группе `input`
(клавиатура читается через evdev): `sudo usermod -aG input $USER`.

## Запуск

```bash
python3 icon2.py                                   # в терминале

# в фоне, лог в icon2.log (абсолютный путь — чтобы находился через pkill -f)
D=~/projects/ai/voice-transcriber
setsid python3 -u $D/icon2.py >> $D/icon2.log 2>&1 </dev/null &
```

Перезапуск после правок: `pkill -f voice-transcriber/icon2.py`, затем снова команда выше.
Должен работать ровно один экземпляр.
