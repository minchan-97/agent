"""
push.py — 앱 자체 알림 (웹 푸시). ntfy 앱 없이 홈 화면 앱으로 바로 온다.

구성
  · VAPID 열쇠: 처음 필요할 때 서버가 스스로 만들어 app_secrets 에 넣는다.
    사용자가 열쇠를 만들거나 환경변수에 옮겨 적을 필요가 없다.
    app_secrets 는 RLS 정책이 없어서 service role 만 읽는다.
  · 구독: 기기마다 하나 (push_subscriptions). 폰·태블릿을 따로 켜면 둘 다 받는다.
  · 발송: RFC 8291(aes128gcm) 로 내용을 암호화하고, RFC 8292(VAPID) 로 서명한다.
    외부 라이브러리 없이 cryptography 만 쓴다.
  · 만료된 구독(404·410)은 지운다.

알림 내용은 기기에 도착하기 전까지 암호화돼 있지만, 잠금 화면에 그대로 보이므로
privacy.ok_to_notify 필터를 똑같이 건다.
"""
import base64
import json
import os
import struct
import time
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .privacy import ok_to_notify

TTL = 60 * 60 * 24
CONTACT = os.environ.get("PUSH_CONTACT", "mailto:admin@example.com")


# ---------------- 기본 도구 ----------------
def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hmac(key: bytes, data: bytes) -> bytes:
    h = hmac.HMAC(key, hashes.SHA256())
    h.update(data)
    return h.finalize()


def _pub_bytes(pub: ec.EllipticCurvePublicKey) -> bytes:
    return pub.public_bytes(serialization.Encoding.X962,
                            serialization.PublicFormat.UncompressedPoint)


def new_vapid() -> dict:
    k = ec.generate_private_key(ec.SECP256R1())
    return {"private": b64u(k.private_numbers().private_value.to_bytes(32, "big")),
            "public": b64u(_pub_bytes(k.public_key()))}


def _priv_from(b64: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(b64u_dec(b64), "big"), ec.SECP256R1())


# ---------------- RFC 8291: 내용 암호화 ----------------
def encrypt(payload: bytes, p256dh: str, auth: str, _as_key=None, _salt=None) -> bytes:
    """_as_key, _salt 는 RFC 8291 예시 값으로 시험할 때만 쓴다."""
    ua_pub = b64u_dec(p256dh)
    auth_secret = b64u_dec(auth)
    as_key = _as_key or ec.generate_private_key(ec.SECP256R1())
    as_pub = _pub_bytes(as_key.public_key())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    shared = as_key.exchange(ec.ECDH(), ua_key)

    prk_key = _hmac(auth_secret, shared)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_pub + as_pub + b"\x01")
    salt = _salt or os.urandom(16)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]

    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)   # \x02 = 마지막 레코드
    header = salt + struct.pack("!I", 4096) + bytes([len(as_pub)]) + as_pub
    return header + body


# ---------------- RFC 8292: VAPID 서명 ----------------
def vapid_header(endpoint: str, keys: dict) -> str:
    u = urlparse(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}",
                              "exp": int(time.time()) + 12 * 3600, "sub": CONTACT},
                             separators=(",", ":")).encode())
    signing = f"{head}.{claims}".encode()
    der = _priv_from(keys["private"]).sign(signing, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"vapid t={head}.{claims}.{b64u(sig)}, k={keys['public']}"


# ---------------- 열쇠 보관 ----------------
_cache = {}


def keys(service_sb) -> dict:
    """VAPID 열쇠. 없으면 만들어 저장한다. 동시에 두 번 만들어져도 먼저 저장된 하나만 쓴다."""
    if _cache.get("vapid"):
        return _cache["vapid"]
    rows = service_sb.table("app_secrets").select("value").eq("key", "vapid").limit(1).execute().data
    if not rows:
        try:
            service_sb.table("app_secrets").insert({"key": "vapid", "value": new_vapid()}).execute()
        except Exception:
            pass                                    # 다른 요청이 먼저 넣었다
        rows = service_sb.table("app_secrets").select("value").eq("key", "vapid").limit(1).execute().data
    _cache["vapid"] = rows[0]["value"]
    return _cache["vapid"]


# ---------------- 발송 ----------------
def send_one(sub: dict, payload: dict, vapid: dict) -> int:
    body = encrypt(json.dumps(payload, ensure_ascii=False).encode(), sub["p256dh"], sub["auth"])
    r = requests.post(sub["endpoint"], data=body, timeout=10, headers={
        "Authorization": vapid_header(sub["endpoint"], vapid),
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
        "TTL": str(TTL), "Urgency": "high"})
    return r.status_code


def send(service_sb, user_id: str, title: str, body: str, url: str = "/") -> dict:
    """그 사람의 모든 기기로 보낸다. 돌려주는 값: {"sent": 성공 수, "devices": 기기 수}"""
    if not ok_to_notify(f"{title} {body}"):
        return {"sent": 0, "devices": 0, "why": "민감한 내용이라 안 보냄"}
    subs = service_sb.table("push_subscriptions").select("*").eq("user_id", user_id) \
                     .execute().data or []
    if not subs:
        return {"sent": 0, "devices": 0}
    vapid = keys(service_sb)
    payload = {"title": title[:80], "body": body[:240], "url": url}
    sent = 0
    for s in subs:
        try:
            code = send_one(s, payload, vapid)
        except Exception:
            code = 0
        if code in (200, 201, 202):
            sent += 1
        elif code in (404, 410):                    # 앱을 지웠거나 알림을 끈 기기
            service_sb.table("push_subscriptions").delete().eq("id", s["id"]).execute()
    return {"sent": sent, "devices": len(subs)}
