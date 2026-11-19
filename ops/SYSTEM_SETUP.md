# SYSTEM_SETUP.md — системная часть анонимности (вне репозитория)

Снято с рабочей машины 2026-11-19 (Kali, `tor 0.4.9.8`, `torsocks 2.5.0`,
`proxychains4 4.17`). Это ОПИСАНИЕ и ШАБЛОН: сами файлы лежат в `/etc`, в git
их нет. Паролей, хэшей, кук и bridge-строк в них не было и нет — проверено
grep-ом по `HashedControlPassword|password|Bridge|ClientTransportPlugin` (0 совпадений).

Что эта обвязка даёт и чего не даёт — в `ANONYMITY.md` (корень). Префлайт,
который всё это проверяет перед сетевой работой, — `tools/tor_guard.py`.

---

## 1. Tor — `/etc/tor/torrc`

Пакет: `apt install tor`. Файл заменён целиком (дистрибутивный лежит рядом как
`torrc.bak`, в нём нет ни одной активной строки). Действующий конфиг:

```
## Tor - anonymity client

SocksPort 127.0.0.1:9050
SocksPolicy accept 127.0.0.1
SocksPolicy reject *

DNSPort 127.0.0.1:9053
AutomapHostsOnResolve 1
AutomapHostsSuffixes .exit,.onion

ControlPort 9051
CookieAuthentication 1

Log notice file /var/log/tor/notices.log
DataDirectory /var/lib/tor

ExitPolicy reject *:*
```

Зачем каждая директива:

| директива | зачем |
|---|---|
| `SocksPort 127.0.0.1:9050` | единственная точка входа для кода: `socks5h://127.0.0.1:9050` |
| `SocksPolicy accept 127.0.0.1` + `reject *` | SOCKS слушает только localhost; из сети не достучаться |
| `DNSPort 127.0.0.1:9053` | DNS через Tor для тех, кто умеет его спросить явно; системный резолвер на `:53` его НЕ использует |
| `AutomapHostsOnResolve` / `AutomapHostsSuffixes` | `.onion`/`.exit` резолвятся в фиктивные адреса, чтобы их можно было открыть через SOCKS |
| `ControlPort 9051` + `CookieAuthentication 1` | управление (NEWNYM и т.п.) только для локальных процессов с правом читать cookie-файл Tor; **пароля нет** |
| `Log notice file ...` | журнал в `/var/log/tor/notices.log` |
| `ExitPolicy reject *:*` | машина — клиент, НЕ выходная нода |

Служба: `tor.service` (мастер, `oneshot`, enabled) → `tor@default.service`
(реальный процесс, `Type=notify`, enabled-runtime, active). Юниты дистрибутивные,
не правились; `tor@default` запускает `/usr/bin/tor --defaults-torrc
/usr/share/tor/tor-service-defaults-torrc -f /etc/tor/torrc`.

```bash
systemctl is-active tor@default      # active
ss -ltn | grep -E ':9050|:9051'       # оба только на 127.0.0.1
```

## 2. torsocks — `/etc/tor/torsocks.conf`

Дистрибутивный, без правок. Активные строки:

```
TorAddress 127.0.0.1
TorPort 9050
OnionAddrRange 127.42.42.0/24
```

Использование: `torsocks <команда>`. Заворачивает libc-сокеты через LD_PRELOAD;
статически слинкованные бинари и raw-сокеты не покрывает.

## 3. proxychains4 — `/etc/proxychains4.conf`

Пакет `proxychains4`. Активные строки (всё остальное — комментарии дистрибутива):

```
strict_chain
proxy_dns
remote_dns_subnet 224
tcp_read_time_out 15000
tcp_connect_time_out 8000
[ProxyList]
socks5  127.0.0.1 9050
```

`strict_chain` + один прокси = «всё или ничего»; `proxy_dns` — имена резолвит
выходная нода, локальной DNS-утечки нет. Использование: `proxychains4 <команда>`.

## 4. Чего в системе НЕТ (и это важно для регламента)

- Прозрачной торификации нет: маршрут по умолчанию прямой, `iptables`-правил
  редиректа нет. Любой процесс, который явно не ходит в `127.0.0.1:9050`,
  выходит с реального адреса — даже при работающем Tor.
- VPN: `openvpn` установлен, но все `openvpn*`/`vpnc@` юниты disabled; конфигов
  `/etc/wireguard/*.conf`, `/etc/openvpn/*.conf` и интерфейсов tun/wg нет.
- Bridges, obfs4, snowflake — не настроены (`ClientTransportPlugin` отсутствует).
- Пользовательских systemd-юнитов (`~/.config/systemd/user/`) нет.
- Переменных `*_PROXY` в окружении нет; shell rc прокси не задаёт.

## 5. Воспроизвести на чистой машине

```bash
sudo apt install tor torsocks proxychains4
sudo cp /etc/tor/torrc /etc/tor/torrc.bak          # сохранить дистрибутивный
sudo tee /etc/tor/torrc >/dev/null <<'T'
SocksPort 127.0.0.1:9050
SocksPolicy accept 127.0.0.1
SocksPolicy reject *
DNSPort 127.0.0.1:9053
AutomapHostsOnResolve 1
AutomapHostsSuffixes .exit,.onion
ControlPort 9051
CookieAuthentication 1
Log notice file /var/log/tor/notices.log
DataDirectory /var/lib/tor
ExitPolicy reject *:*
T
sudo sed -i 's/^#\?\s*socks4\s.*$/socks5  127.0.0.1 9050/' /etc/proxychains4.conf
sudo sed -i 's/^#\?\s*proxy_dns\s*$/proxy_dns/; s/^dynamic_chain/#dynamic_chain/; s/^#\?strict_chain/strict_chain/' /etc/proxychains4.conf
sudo systemctl enable --now tor
python3 tools/tor_guard.py          # ok=true, is_tor=true, exit_ip != real_ip
```

`tor_guard.py` требует `python-socks` в том интерпретаторе, которым запускается
(в venv парсера его нет — там `PySocks`, другая библиотека; системный `python3`
подходит).

## 6. Что сюда НЕ входит

Профиль браузера с `cf_clearance` (`~/.config/chromium`, `~/.config/google-chrome-for-testing`)
и бинарь Chromium (`~/.cache/ms-playwright/chromium-*`) — это транспорт парсера,
не анонимность, и в git не кладётся. Пруфы префлайта (`guard*.json`,
`*_preflight.json`) содержат реальный IP и игнорируются `.gitignore`.
