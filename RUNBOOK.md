# LiveTalking — Runbook (подробно)

Этот документ описывает, как повторить текущую настройку **через Codex CLI**: мультипроцессный WebRTC‑сервер, автозапуск воркеров по требованию, авто‑остановка при неактивности, Cloudflare Tunnel на постоянный домен, переключение качества/буфера.

## 0) Что уже сделано в коде
- Добавлен **gateway** (`gateway.py`) — запускает до N воркеров `app.py` на портах 8091..8095 **по требованию**.
- Таймаут бездействия: **5 минут**, после чего воркер выключается.
- В UI добавлены: **качество** (Low/Balanced/High) + **плейаут‑буфер** (0/200/400ms).
- В WebRTC включён **relay‑режим TURN** по умолчанию (STUN опционален).
- Автоприветствие на английском после подключения.

## 1) Базовая установка
### 1.1 Зависимости (Python)
```bash
cd /workspace/LiveTalking
python -m venv /venv/nerfstream
source /venv/nerfstream/bin/activate
pip install -r requirements.txt
```

> Важно: для GPU‑рендера нужен PyTorch с CUDA под вашу версию драйвера.

### 1.2 Файлы секретов (НЕ коммитим)
1) Создай **keys.json** по шаблону:
```bash
cp /workspace/LiveTalking/keys.example.json /workspace/LiveTalking/keys.json
```
Заполни поля:
- `openai_key`, `openai_base`, `openai_model`
- `eleven_key`, `eleven_voice`, `eleven_model`, `eleven_latency`, `eleven_output_format`

2) Создай **turn.env** по шаблону:
```bash
cp /workspace/LiveTalking/turn.example.env /workspace/LiveTalking/turn.env
```
Заполни:
- `CF_TURN_TOKEN_ID`
- `CF_TURN_API_TOKEN`
- `CF_TURN_TTL`

> `keys.json` и `turn.env` добавлены в `.gitignore`.

## 2) Запуск сервера (мультипроцесс)
Один командный запуск (через Codex):
```bash
/workspace/LiveTalking/start_gateway.sh
```

Что делает скрипт:
- экспортирует TURN‑секреты
- ограничивает спавн потоков CPU
- убивает старые `gateway.py`/`app.py`
- стартует `gateway.py` на **8090**

**Gateway поднимает воркеров автоматически**, когда приходит /offer:
- воркеры на портах **8091–8095**
- один воркер = один активный пользователь
- если нет сообщений **60 секунд**, воркер остановится

Логи:
- Gateway: `gateway.log`
- Воркеры: `logs/worker-8091.log` (и т.д.)

## 3) Web‑интерфейс
Открывай:
```
https://liveavatar.beintouch.me/dashboard.html
```
В UI:
- Переключай **Качество потока** (низкое/сбаланс./высокое)
- Настраивай **Буфер (задержка)**: авто или 0/200/400ms
- Нажимай **«Подключиться»**

> После изменений качества/буфера — **переподключиться**.

## 4) Cloudflare Tunnel (постоянный домен)
### 4.1 Логин в Cloudflare
```bash
cloudflared tunnel login
```
Открой ссылку, авторизуйся.

### 4.2 Создать туннель и привязать домен
```bash
cloudflared tunnel create liveavatar
cloudflared tunnel route dns liveavatar liveavatar.beintouch.me
```

### 4.3 Конфиг tunnel
Файл: `/root/.cloudflared/config.yml`
```yaml
tunnel: <TUNNEL_ID>
credentials-file: /root/.cloudflared/<TUNNEL_ID>.json
ingress:
  - hostname: liveavatar.beintouch.me
    service: http://127.0.0.1:8090
  - service: http_status:404
```

### 4.4 Запуск tunnel
```bash
cloudflared --config /root/.cloudflared/config.yml tunnel run liveavatar
```

> systemd недоступен, поэтому можно запускать через `nohup`/`screen`.

## 5) Качество / битрейт / FPS
Профили задаются в `app.py`:
```python
QUALITY_PROFILES = {
  "low": {"max_bitrate": 350_000, "max_fps": 15, "scale": 1.5},
  "balanced": {"max_bitrate": 800_000, "max_fps": 20, "scale": 1.0},
  "high": {"max_bitrate": 1_600_000, "max_fps": 25, "scale": 1.0},
}
```

Эти параметры применяются **только к выбранному режиму**, не грузят сервер лишней генерацией.

## 6) Таймаут неактивности
- Таймаут = 5 минут без **сообщений**.
- Счётчик **начинается после успешного подключения**, а не после клика.
- UI показывает предупреждение + кнопку «Перезагрузить страницу».

Чтобы изменить:
```bash
# в start_gateway.sh
--idle_timeout 300
```

## 7) Быстрый чек‑лист (если не работает)
- `curl http://127.0.0.1:8090/ice` — должен вернуть ICE‑servers
- `tail -n 100 gateway.log` — увидеть старт воркера
- `tail -n 200 logs/worker-8091.log` — нет ли ошибок по модели/порту
- Обнови страницу **Ctrl+Shift+R**

## 8) Как повторить через Codex (короткий набор команд)
```bash
# 1) создать secrets
cp keys.example.json keys.json
cp turn.example.env turn.env

# 2) старт gateway
./start_gateway.sh

# 3) (разово) Cloudflare tunnel
cloudflared tunnel login
cloudflared tunnel create liveavatar
cloudflared tunnel route dns liveavatar liveavatar.beintouch.me
cloudflared --config /root/.cloudflared/config.yml tunnel run liveavatar
```

Готово.
