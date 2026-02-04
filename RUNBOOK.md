# LiveTalking — Runbook (актуально)

ВАЖНО: каждый раз при изменениях **обновляй этот файл** — добавляй всё нужное, удаляй неверное и ненужное. В следующий раз **сначала перечитай этот файл**.

## 1) Текущее состояние (что запущено и как работает)
- Gateway: `/workspace/LiveTalking/gateway.py` слушает **8090**, старт через `./start_gateway.sh`.
- Профили воркеров: `/workspace/LiveTalking/worker_profiles.json`.
  - `head`: 5 воркеров, порты **8091–8095**, готов при наличии `ernerf/obama_eo_head/checkpoints/ngp.pth`.
  - `torso`: 1 воркер, порт **8101**, готов при наличии `ernerf/obama_eo_torso/checkpoints/ngp_ep0028.pth`.
    Если нужно включить раньше — поменяй `ready_check` на `.../ngp.pth`.
- Таймаут бездействия: **300 секунд** после последнего сообщения; сессия сбрасывается, воркер остаётся тёплым.
- UI: качество/буфер применяются **на лету**, переключение профиля модели требует **переподключения**.

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

### 3.2 `turn.env`
```bash
cp /workspace/LiveTalking/turn.example.env /workspace/LiveTalking/turn.env
```
Заполнить:
- `CF_TURN_TOKEN_ID`
- `CF_TURN_API_TOKEN`
- `CF_TURN_TTL`

> `keys.json` и `turn.env` уже в `.gitignore`.

## 4) Запуск сервера (мультипроцесс)
```bash
/workspace/LiveTalking/start_gateway.sh
```
Скрипт:
- экспортирует TURN‑секреты из `turn.env`
- ограничивает sprawl CPU‑потоков
- убивает старые `gateway.py`/`app.py`
- запускает gateway на 8090

Логи:
- `/workspace/LiveTalking/gateway.log`
- `/workspace/LiveTalking/logs/worker-8091.log` (и т.д.)

## 5) Веб‑интерфейс
Открывать:
- локально: `http://127.0.0.1:8090/dashboard.html`
- домен: `https://liveavatar.beintouch.me/dashboard.html`

В UI:
- **Качество**: `Авто` или фиксированные уровни (emergency/very_low/low/balanced/high) — **на лету**.
- **Буфер (задержка)**: `Авто` или 0/200/400 ms — **на лету**.
- **Режим модели**: `Только голова` / `Голова + торс` — требует **переподключения**.
- **Только TURN (relay)**: стабильнее через сложные сети, но подключение дольше.

Если видишь старый JS / ошибки в консоли — делай **Ctrl+Shift+R** (жёсткое обновление).

## 6) Профили качества (сервер)
Профили заданы в `app.py`:
```python
QUALITY_PROFILES = {
  "emergency": {"max_bitrate": 80_000, "max_fps": 8,  "scale": 3.0},
  "very_low":  {"max_bitrate": 150_000, "max_fps": 10, "scale": 2.5},
  "low":       {"max_bitrate": 350_000, "max_fps": 15, "scale": 1.5},
  "balanced":  {"max_bitrate": 800_000, "max_fps": 20, "scale": 1.0},
  "high":      {"max_bitrate": 1_600_000, "max_fps": 25, "scale": 1.0},
}
```

## 7) Cloudflare Tunnel (постоянный домен)
### 7.1 Логин
```bash
cloudflared tunnel login
```

### 7.2 Создать туннель и привязать домен
```bash
cloudflared tunnel create liveavatar
cloudflared tunnel route dns liveavatar liveavatar.beintouch.me
```

### 7.3 Конфиг `/root/.cloudflared/config.yml`
```yaml
tunnel: <TUNNEL_ID>
credentials-file: /root/.cloudflared/<TUNNEL_ID>.json
ingress:
  - hostname: liveavatar.beintouch.me
    service: http://127.0.0.1:8090
  - service: http_status:404
```

### 7.4 Запуск
```bash
cloudflared --config /root/.cloudflared/config.yml tunnel run liveavatar
```

## 8) Полная тренировка ER‑NeRF (head + lips + torso)
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

## 9) Быстрый чек‑лист
- `curl http://127.0.0.1:8090/ice` — должен вернуть ICE‑servers
- `tail -n 100 gateway.log` — есть старт воркеров
- `tail -n 200 logs/worker-8091.log` — нет ошибок по модели/порту

ВАЖНО: каждый раз при изменениях **обновляй этот файл** — добавляй всё нужное, удаляй неверное и ненужное. В следующий раз **сначала перечитай этот файл**.
