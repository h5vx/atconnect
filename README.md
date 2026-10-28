# atconnect

Companion-приложение для беспроводных наушников Audio-Technica под Linux —
замена официальному *Audio-Technica | Connect*. Протокол восстановлен по APK
версии 2.8.0, подробности в [PROTOCOL.md](PROTOCOL.md).

Сейчас поддерживается **ATH-M50xBT2** (чип Realtek). Остальные модели из
официального приложения работают на Qualcomm GAIA или Airoha и требуют
отдельного бэкенда.

## Возможности

- батарея, версия прошивки, текущий кодек (LDAC/AAC/SBC)
- предпочитаемый кодек, режим низкой задержки
- эквалайзер: вкл/выкл, пресеты официального приложения и своя 5-полосная кривая
  (коэффициенты считаются так же, как в нативной библиотеке Realtek, байт в байт)
- multipoint: вкл/выкл, сопряжение ещё одного устройства, «подключить последнее»,
  список подключённых устройств по событиям наушников, передача наушников другому
  устройству и возврат, отдельное управление A2DP/HFP этого компьютера; управление
  списком спаренных устройств — если его поддерживает прошивка (1.0.0.8 не поддерживает)
- sidetone и его уровень, голосовые подсказки (голос/бип), голосовой ассистент, имя Bluetooth

Не сделано: обновление прошивки.

## Установка

Нужны BlueZ и системные PyGObject, GTK 4, libadwaita ≥ 1.6
(`python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`).

```bash
uv venv --python /usr/bin/python3 --system-site-packages
uv sync
```

Наушники должны быть спарены и подключены (`bluetoothctl connect <MAC>`).

## Использование

```bash
uv run atconnect status
```

```bash
uv run atconnect eq preset bass-boost
```

```bash
uv run atconnect eq set 3 0 -1 1 2
```

```bash
uv run atconnect multipoint watch
```

Эквалайзер нужно включить (`atconnect eq on`): пока он выключен, пресет только
сохраняется. При включении/выключении наушники на секунду перезапускают звук.

Multipoint: `atconnect multipoint pair` делает наушники видимыми — на M50xBT2 это
разрывает связь с компьютером, поэтому есть `--reconnect SECONDS` или
`atconnect multipoint reclaim`. `handover` отпускает наушники для другого
устройства, `link a2dp|hfp on|off` — профили этого компьютера.

Остальные команды: `atconnect --help`. Отладка: `-vv` печатает все пакеты,
`atconnect monitor` — события от наушников, `atconnect raw` / `atconnect at` —
произвольные команды.

GUI:

```bash
uv run atconnect-gtk
```

Чтобы GUI появился в меню приложений, скопируй
`data/io.github.atconnect.desktop` в `~/.local/share/applications/` и поправь
`Exec` на путь к `.venv/bin/atconnect-gtk`.

## Как это работает

Наушники говорят протоколом Realtek BeePro поверх RFCOMM (у M50xBT2 — на
обычном Serial Port). `atconnect` регистрирует в BlueZ внешний клиентский
профиль `org.bluez.Profile1` и вызывает `Device1.ConnectProfile`: BlueZ сам
делает SDP и передаёт готовый сокет. Команды Audio-Technica туннелируются через
вендорский opcode `0x0800`.

## Тесты

```bash
uv run pytest
```
