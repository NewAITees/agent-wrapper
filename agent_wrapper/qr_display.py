"""
ダッシュボードのURLをLAN内の端末(スマホなど)からアクセスしやすくするための表示ヘルパー。
起動時にターミナルへLAN IPとQRコードを表示する。
"""

import base64
import io
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


def _build_qr(url: str) -> qrcode.QRCode:
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make()
    return qr


def make_qr_data_url(url: str) -> str:
    qr = _build_qr(url)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def print_dashboard_qr(url: str) -> None:
    qr = _build_qr(url)
    qr.print_ascii(invert=True)
    print(
        f"スマホ/Macからは上のQRコードを読み取るか、次のURLを開いてください: {url}",
        flush=True,
    )
