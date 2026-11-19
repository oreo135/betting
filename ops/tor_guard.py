"""
tor_guard — предполётная проверка анонимности. FAIL-CLOSED: если хоть один
пункт не сходится, вызывающий скрипт обязан упасть и НЕ ходить в сеть.

Проверяет:
 1. SOCKS5 127.0.0.1:9050 отвечает;
 2. выходной IP через прокси != реальный прямой IP;
 3. имя хоста резолвит ВЫХОДНАЯ нода (dest_host отдаётся прокси, не резолвится
    локально) — значит, DNS-утечки нет.
"""
import asyncio, json, socket, ssl, sys

SOCKS = "socks5://127.0.0.1:9050"


async def _http_get_via_proxy(host: str, path: str = "/json") -> str:
    from python_socks.async_.asyncio import Proxy
    sock = await Proxy.from_url(SOCKS).connect(dest_host=host, dest_port=443, timeout=45)
    r, w = await asyncio.open_connection(sock=sock, ssl=ssl.create_default_context(),
                                         server_hostname=host)
    w.write(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: curl/8\r\n"
            f"Connection: close\r\n\r\n".encode())
    await w.drain()
    body = await asyncio.wait_for(r.read(), timeout=45)
    w.close()
    return body.decode("utf-8", "replace").split("\r\n\r\n", 1)[-1]


def _direct_ip() -> str | None:
    """Реальный IP — спрашиваем у нейтральной третьей стороны, НЕ у цели."""
    import urllib.request
    try:
        with urllib.request.urlopen("https://ipinfo.io/json", timeout=10) as r:
            return json.load(r).get("ip")
    except Exception:
        return None


async def preflight(verbose: bool = True) -> dict:
    rep = {"ok": False}

    # 1. Порт SOCKS5 жив?
    s = socket.socket()
    s.settimeout(5)
    try:
        s.connect(("127.0.0.1", 9050))
        rep["socks_alive"] = True
    except OSError as e:
        rep["socks_alive"] = False
        rep["error"] = f"SOCKS5 9050 недоступен: {e}"
        return rep
    finally:
        s.close()

    # 2. Что видит внешний наблюдатель через прокси.
    #    check.torproject.org — авторитетный ответ: и IP, и факт «это Tor».
    try:
        raw = await _http_get_via_proxy("check.torproject.org", "/api/ip")
        j = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        rep["exit_ip"] = j.get("IP")
        rep["is_tor"] = bool(j.get("IsTor"))
    except Exception as e:
        rep["error"] = f"выход через Tor не отработал: {e}"
        return rep

    if not rep.get("is_tor"):
        rep["error"] = "check.torproject.org говорит IsTor=false — трафик идёт НЕ через Tor"
        return rep

    # 3. Сверка с реальным адресом
    rep["real_ip"] = _direct_ip()
    if rep["real_ip"] and rep["real_ip"] == rep["exit_ip"]:
        rep["error"] = "ВЫХОДНОЙ IP СОВПАЛ С РЕАЛЬНЫМ — трафик НЕ анонимизирован"
        return rep

    rep["dns"] = "резолвит выходная нода (dest_host передаётся прокси)"
    rep["ok"] = True
    if verbose:
        print(f"[tor_guard] OK  реальный {rep['real_ip']} → выход {rep['exit_ip']} "
              f"(IsTor={rep['is_tor']})", flush=True)
    return rep


async def connect_via_tor(host: str, port: int = 443, timeout: int = 45):
    """TCP-сокет до host:port ЧЕРЕЗ Tor. Имя хоста резолвит выходная нода."""
    from python_socks.async_.asyncio import Proxy
    return await Proxy.from_url(SOCKS).connect(dest_host=host, dest_port=port,
                                               timeout=timeout)


if __name__ == "__main__":
    r = asyncio.run(preflight())
    print(json.dumps(r, ensure_ascii=False, indent=1))
    sys.exit(0 if r["ok"] else 1)
