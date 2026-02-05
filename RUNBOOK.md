# LiveTalking — Runbook (актуально)

ВАЖНО: каждый раз при изменениях **обновляй этот файл** — добавляй всё нужное, удаляй неверное и ненужное. В следующий раз **сначала перечитай этот файл**.

## 1) Текущее состояние (что запущено и как работает)
- Gateway: `/workspace/LiveTalking/gateway.py` слушает **8090**, старт через `./start_gateway.sh`.
- Медиа‑доставка: **Daily** (бот публикует аудио/видео в Daily‑комнату).
- Профили воркеров: `/workspace/LiveTalking/worker_profiles.json`.
  - `head`: 5 воркеров, порты **8091–8095**, готов при наличии `ernerf/obama_eo_head/checkpoints/ngp.pth`.
  - `torso`: 1 воркер, порт **8101**, готов при наличии `ernerf/obama_eo_torso/checkpoints/ngp_ep0028.pth`.
    Если нужно включить раньше — поменяй `ready_check` на `.../ngp.pth`.
- Таймаут бездействия: **300 секунд** после последнего сообщения; сессия сбрасывается, воркер остаётся тёплым.
- UI: Daily управляет транспортом; переключение профиля модели требует **переподключения**.

## 2) Установка и зависимости
```bash
cd /workspace/LiveTalking
python -m venv /venv/nerfstream
source /venv/nerfstream/bin/activate
pip install -r requirements.txt
```

> Для GPU нужен PyTorch с CUDA под вашу версию драйвера.

## 3) Секреты (не коммитим)
### 3.1 `keys.json`
```bash
cp /workspace/LiveTalking/keys.example.json /workspace/LiveTalking/keys.json
```
Заполнить:
- `openai_key`, `openai_base`, `openai_model`
- `eleven_key`, `eleven_voice`, `eleven_model`, `eleven_latency`, `eleven_output_format`, `eleven_speed`

### 3.2 `daily.env`
```bash
cp /workspace/LiveTalking/daily.example.env /workspace/LiveTalking/daily.env
```
Заполнить:
- `DAILY_API_KEY`
- `DAILY_DOMAIN` (например `smartblog.daily.co`)
- `DAILY_ROOM_TTL`, `DAILY_TOKEN_TTL`
- `DAILY_AUDIO_RATE`, `DAILY_AUDIO_BITRATE` (опционально, **рекомендовано 16000/64000 для лучшей синхронизации**)
- `DAILY_VIDEO_WIDTH`, `DAILY_VIDEO_HEIGHT`, `DAILY_VIDEO_FPS` (опционально)
- `DAILY_VIDEO_QUALITY`, `DAILY_VIDEO_CODEC` (опционально)
- `DAILY_QUALITY_AUTO` (1/0), `DAILY_QUALITY_INTERVAL`, `DAILY_QUALITY_*_STREAK` — авто‑адаптация качества
- `DAILY_AUDIO_QUEUE`, `DAILY_AUDIO_MAX_BACKLOG` — очередь аудио для ровного тайминга

> `daily.env` уже в `.gitignore`.

## 4) Запуск сервера (мультипроцесс)
```bash
/workspace/LiveTalking/start_gateway.sh
```
Скрипт:
- экспортирует Daily‑секреты из `daily.env`
- ограничивает sprawl CPU‑потоков
- убивает старые `gateway.py`/`app.py`
- запускает gateway на 8090

Логи:
- `/workspace/LiveTalking/gateway.log`
- `/workspace/LiveTalking/logs/worker-8091.log` (и т.д.)

## 5) Веб‑интерфейс
Открывать:
- локально: `http://127.0.0.1:8090/dashboard.html`

В UI:
- **Подключиться** создаёт Daily‑комнату и подключает браузер.
- **Режим модели**: `Только голова` / `Голова + торс` — требует **переподключения**.

Если видишь старый JS / ошибки в консоли — делай **Ctrl+Shift+R** (жёсткое обновление).

## 6) Полная тренировка ER‑NeRF (head + lips + torso)
Скрипт:
```bash
/workspace/LiveTalking/scripts/train_obama_hq.sh
```
Запускает 3 этапа:
1) Head (100k итераций)
2) Lips fine‑tune (125k, LPIPS + landmarks)
3) Torso (200k, с замороженной головой)

Запуск в фоне:
```bash
cd /workspace/LiveTalking
nohup ./scripts/train_obama_hq.sh > logs/train_obama_hq.log 2>&1 &
```

Прогресс:
```bash
tail -n 60 logs/train_obama_hq.log
```

Ожидаемые выходы:
- `ernerf/obama_eo_head/checkpoints/ngp.pth`
- `ernerf/obama_eo_torso/checkpoints/ngp.pth`

## 7) Быстрый чек‑лист
- `curl http://127.0.0.1:8090/health` — должен вернуть `ok`
- `tail -n 100 gateway.log` — есть старт воркеров
- `tail -n 200 logs/worker-8091.log` — нет ошибок по модели/порту

ВАЖНО: каждый раз при изменениях **обновляй этот файл** — добавляй всё нужное, удаляй неверное и ненужное. В следующий раз **сначала перечитай этот файл**.
