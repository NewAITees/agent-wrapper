"""
ダッシュボードのURLをLAN内の端末(スマホなど)からアクセスしやすくするための表示ヘルパー。
起動時にターミナルへLAN IPとQRコードを表示する。
"""
import socket

import qrcode


def get_lan_ip() -> str:
    """外部疎通用のUDPソケットを使ってLAN側のIPアドレスを推定する(実際にパケットは送らない)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return str(s.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def print_dashboard_qr(url: str) -> None:
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make()
    qr.print_ascii(invert=True)
    print(f"スマホ/Macからは上のQRコードを読み取るか、次のURLを開いてください: {url}", flush=True)
