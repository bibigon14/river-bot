# RiverBot

Telegram-бот, который присылает уровень воды, расход (CFS) и температуру
для American River at Fair Oaks и Sacramento River at Freeport (данные USGS,
без ключа API). Поддерживает команду `/now` и ежедневную рассылку по расписанию.
Если температура опускается ниже порога — добавляет пометку про миграцию лосося.

## 1. Создать Telegram-бота

1. В Telegram напишите **@BotFather** → `/newbot`, задайте имя.
2. Скопируйте токен вида `123456789:AAAA...` — это `BOT_TOKEN`.
3. Напишите новому боту любое сообщение (`/start`).
4. Откройте в браузере:
   `https://api.telegram.org/bot<ВАШ_ТОКЕН>/getUpdates`
   и найдите `"chat":{"id": ...}` — это ваш `CHAT_ID`.

## 2. Установка на Raspberry Pi

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip

# скопируйте папку riverbot на Pi, например в /home/pi/riverbot
cd /home/pi/riverbot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env   # впишите BOT_TOKEN и CHAT_ID, проверьте посты и время рассылки
```

Проверка вручную:

```bash
source venv/bin/activate
python bot.py
```

В Telegram отправьте боту `/now` — должен прийти отчёт. Остановить: `Ctrl+C`.

## 3. Автозапуск через systemd (чтобы бот жил постоянно)

```bash
# поправьте User= и пути в файле, если пользователь на Pi не "pi"
sudo cp riverbot.service /etc/systemd/system/riverbot.service
sudo systemctl daemon-reload
sudo systemctl enable riverbot.service
sudo systemctl start riverbot.service

# проверить статус
sudo systemctl status riverbot.service

# логи
tail -f /home/pi/riverbot/riverbot.log
```

Бот теперь запускается автоматически при загрузке Pi и перезапускается при сбое.

## Настройки (.env)

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | токен от @BotFather |
| `CHAT_ID` | ваш chat_id |
| `USGS_SITES` | номера постов USGS через запятую (по умолчанию Fair Oaks + Freeport) |
| `SCHEDULE_TIME` | время ежедневной рассылки, HH:MM |
| `TIMEZONE` | часовой пояс, например `America/Los_Angeles` |
| `SALMON_TEMP_THRESHOLD_F` | порог температуры (°F) для пометки про лосося |

## Другие посты USGS

Искать номер поста: https://waterdata.usgs.gov/nwis/rt — найдите нужную реку/город,
номер site_no впишите в `USGS_SITES` через запятую.

## Обновление бота

```bash
cd /home/pi/riverbot
# замените файлы новой версией, затем:
sudo systemctl restart riverbot.service
```
