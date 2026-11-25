from __future__ import annotations

import socket
import uvicorn
from settings import SERVER_HOST, SERVER_PORT, APP_VERSION


def _lan_ipv4s() -> list[str]:
    ips: list[str] = []
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip and not ip.startswith("127.") and ":" not in ip:
                ips.append(ip)
    except OSError:
        pass
    # UDP connect does not send traffic; it is a common way to ask the OS which
    # local interface would be used. It still works when internet is blocked.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127.") and ip not in ips:
            ips.append(ip)
    except OSError:
        pass
    return ips


if __name__ == "__main__":
    print(f"\n压制动态热能 & 成型管理 V{APP_VERSION}")
    print(f"本机访问: http://127.0.0.1:{SERVER_PORT}/")
    for ip in _lan_ipv4s():
        print(f"局域网访问: http://{ip}:{SERVER_PORT}/")
    print("如局域网无法访问，请以管理员身份运行 open_firewall_8800_admin.bat。\n")
    uvicorn.run("app:app", host=SERVER_HOST, port=SERVER_PORT, reload=False)
