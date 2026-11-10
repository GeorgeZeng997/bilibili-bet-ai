import json
import os
import struct
import time
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Dict

from bili_client import BiliClient


def parse_cookie_string(raw: str) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    cleaned = raw.replace("\r", "").replace("\n", "").strip()
    if cleaned.lower().startswith("cookie:"):
        cleaned = cleaned[7:].strip()
    for part in cleaned.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            key = k.strip()
            if key:
                cookies[key] = v.strip()
    return cookies


def write_qr_png(url: str, out_path: Path, scale: int = 10, border: int = 4) -> None:
    """
    Pure-Python PNG renderer for QRCode matrix (no Pillow dependency needed).
    Writes a crisp black-on-white PNG image that opens directly in IDE/image viewers.
    """
    import qrcode  # type: ignore

    qr = qrcode.QRCode(border=border)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    size = len(matrix)
    img_size = size * scale

    raw_rows = bytearray()
    white_row = b"\xff" * img_size
    for r in range(size):
        row_bytes = bytearray()
        for c in range(size):
            pixel = b"\x00" if matrix[r][c] else b"\xff"
            row_bytes.extend(pixel * scale)
        for _ in range(scale):
            raw_rows.append(0)  # Filter type 0 (None)
            raw_rows.extend(row_bytes)

    def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", img_size, img_size, 8, 0, 0, 0, 0)  # 8-bit grayscale
    idat = zlib.compress(bytes(raw_rows), 9)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", idat)
        + png_chunk(b"IEND", b"")
    )
    out_path.write_bytes(png_bytes)


def interactive_qr_login(client: BiliClient, max_rounds: int = 15) -> bool:
    """
    Generates `login_qr.png` in the project directory and automatically refreshes it every 140s
    before Bilibili's 180s expiry window so the QR code on disk is always valid.
    """
    png_path = Path(__file__).parent / "login_qr.png"

    for round_idx in range(max_rounds):
        gen = client.request_json("https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
        if gen.get("code") != 0 or not gen.get("data"):
            raise RuntimeError(f"Failed to generate QR code: {gen}")

        qr_url = gen["data"]["url"]
        qrcode_key = gen["data"]["qrcode_key"]
        write_qr_png(qr_url, png_path, scale=10, border=4)

        if round_idx == 0:
            print(f"QR_READY|{png_path}", flush=True)
            try:
                os.startfile(str(png_path))
            except Exception:
                pass
        else:
            print(f"QR_REFRESHED|round={round_idx+1}", flush=True)

        start = time.time()
        scanned_printed = False
        # Refresh at 140s unless the user has already scanned it (86090)
        while time.time() - start < (175 if scanned_printed else 140):
            time.sleep(2.0)
            poll_url = (
                "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
                f"?qrcode_key={urllib.parse.quote(qrcode_key)}"
            )
            req = urllib.request.Request(
                poll_url,
                headers={"User-Agent": client.USER_AGENT, "Referer": "https://www.bilibili.com/"},
            )
            try:
                with client.opener.open(req, timeout=8) as resp:
                    set_cookies = resp.headers.get_all("Set-Cookie") or []
                    body = json.loads(resp.read().decode("utf-8"))
            except Exception:
                continue

            data = body.get("data") or {}
            status_code = data.get("code")
            if status_code == 86101:
                continue
            elif status_code == 86090:
                if not scanned_printed:
                    print("QR code scanned. Please tap [Confirm Login] on your phone...", flush=True)
                    scanned_printed = True
            elif status_code == 86038:
                break
            elif status_code == 0:
                extracted: Dict[str, str] = {}
                callback_url = data.get("url", "")
                if "?" in callback_url:
                    qs = urllib.parse.parse_qs(callback_url.split("?", 1)[1])
                    for k in ["DedeUserID", "DedeUserID__ckMd5", "SESSDATA", "bili_jct", "sid"]:
                        if k in qs and qs[k]:
                            extracted[k] = qs[k][0]
                for sc in set_cookies:
                    first_pair = sc.split(";", 1)[0]
                    if "=" in first_pair:
                        k, v = first_pair.split("=", 1)
                        extracted[k.strip()] = v.strip()

                if data.get("refresh_token"):
                    extracted["refresh_token"] = data["refresh_token"]

                client.save_cookies(extracted)
                nav = client.get_nav_info()
                uname = (nav.get("data") or {}).get("uname", "Unknown")
                coins = client.get_coin_balance()
                print(f"\nLogin successful! Account: {uname} | Coin balance: {coins}", flush=True)
                return True

    print("QR login timed out.", flush=True)
    return False
