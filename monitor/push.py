"""'Your turn' notifications: Web Push to your phone and laptop from the web monitor.

When a Claude session finishes and waits for you, asks a question, or needs attention, every
subscribed device gets a notification (Web Push: VAPID + aes128gcm, RFC 8291/8292).
Implemented on `cryptography` directly, no pywebpush. The payload is encrypted for the device,
so the push service (Apple, Google, Mozilla) cannot read it.

State lives in <state_dir>/push/: the VAPID key (generated on first use) and device subscriptions.
Never commit that folder.
"""
import base64
import json
import os
import struct
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from kitconfig import PUSH_CONTACT, STATE_DIR

PUSH_DIR = STATE_DIR / 'push'
KEY_FILE = PUSH_DIR / 'vapid.pem'
SUBSCRIPTIONS_FILE = PUSH_DIR / 'subscriptions.json'
_lock = threading.Lock()


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def from_b64(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def _raw(public_key):
    return public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def private_key():
    with _lock:
        PUSH_DIR.mkdir(parents=True, exist_ok=True)
        if KEY_FILE.exists():
            return serialization.load_pem_private_key(KEY_FILE.read_bytes(), None)
        key = ec.generate_private_key(ec.SECP256R1())
        KEY_FILE.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        return key


def public_key():
    """applicationServerKey for the browser."""
    return b64(_raw(private_key().public_key()))


def subscriptions():
    try:
        return json.loads(SUBSCRIPTIONS_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []


def _save(items):
    PUSH_DIR.mkdir(parents=True, exist_ok=True)
    SUBSCRIPTIONS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding='utf-8')


def subscribe(sub, device='', origin=''):
    if not (isinstance(sub, dict) and str(sub.get('endpoint', '')).startswith('https://')
            and sub.get('keys', {}).get('p256dh') and sub.get('keys', {}).get('auth')):
        raise ValueError('invalid subscription')
    with _lock:
        items = [s for s in subscriptions() if s['endpoint'] != sub['endpoint']]
        items.append({'endpoint': sub['endpoint'], 'keys': sub['keys'], 'device': device[:120],
                      'origin': origin[:200], 'since': int(time.time())})
        _save(items)


def unsubscribe(endpoint):
    with _lock:
        _save([s for s in subscriptions() if s['endpoint'] != endpoint])


def _contact(sub):
    # VAPID 'sub' claim: a mailto: or https: URL push services can use to reach the sender.
    # Default: the address the device subscribed from (the monitor itself), so no email is needed.
    return PUSH_CONTACT or sub.get('origin') or 'https://localhost'


def _jwt(sub, key):
    u = urlparse(sub['endpoint'])
    head = b64(json.dumps({'typ': 'JWT', 'alg': 'ES256'}).encode())
    body = b64(json.dumps({'aud': f'{u.scheme}://{u.netloc}', 'exp': int(time.time()) + 12 * 3600,
                           'sub': _contact(sub)}).encode())
    signature = key.sign(f'{head}.{body}'.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(signature)
    return f'{head}.{body}.{b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}'


def _hmac(key, data):
    h = hmac.HMAC(key, hashes.SHA256())
    h.update(data)
    return h.finalize()


def _encrypt(data, p256dh, auth):
    """aes128gcm per RFC 8291, a single record."""
    device = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), from_b64(p256dh))
    own = ec.generate_private_key(ec.SECP256R1())
    shared = own.exchange(ec.ECDH(), device)
    own_pub, device_pub = _raw(own.public_key()), _raw(device)
    ikm = _hmac(_hmac(from_b64(auth), shared), b'WebPush: info\x00' + device_pub + own_pub + b'\x01')
    salt = os.urandom(16)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b'Content-Encoding: aes128gcm\x00\x01')[:16]
    nonce = _hmac(prk, b'Content-Encoding: nonce\x00\x01')[:12]
    cipher = AESGCM(cek).encrypt(nonce, data + b'\x02', None)
    return salt + struct.pack('!IB', 4096, len(own_pub)) + own_pub + cipher


def send(title, body, tag='', url='./'):
    """Notify every device. Dead subscriptions (404/410) are removed. Returns the number delivered."""
    key = private_key()
    pub = b64(_raw(key.public_key()))
    data = json.dumps({'title': title, 'body': body, 'tag': tag, 'url': url}, ensure_ascii=False).encode()
    delivered, dead = 0, []
    for sub in subscriptions():
        try:
            request = urllib.request.Request(
                sub['endpoint'], method='POST', data=_encrypt(data, **sub['keys']),
                headers={'Content-Encoding': 'aes128gcm', 'TTL': '3600', 'Urgency': 'high',
                         'Content-Type': 'application/octet-stream',
                         'Authorization': f'vapid t={_jwt(sub, key)}, k={pub}'})
            with urllib.request.urlopen(request, timeout=10):
                delivered += 1
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                dead.append(sub['endpoint'])
        except Exception:
            pass
    for endpoint in dead:
        unsubscribe(endpoint)
    return delivered


# --- what to notify about --------------------------------------------------------------

def _event(s, before, after):
    """(title, text) of a notification for a session state change, or None."""
    tr = s.transcript
    question = next((st for st in reversed(tr.steps) if st[0] == 'AskUserQuestion' and st[3] is None), None)
    if question and before != 'question':
        return f'❓ {s.name} is asking', question[1] or 'needs an answer'
    if after == 'your turn' and before in ('working', 'question'):
        return f'✔ {s.name}: done, your turn', (tr.answer or tr.title or 'answer is ready')
    if after == 'attention' and before != 'attention':
        return f'! {s.name}: needs attention', f'status: {s.record.get("status")}'
    return None


def watch(collector, lock, interval=2.0):
    """Background thread: watches session states and sends notifications on transitions."""
    def loop():
        previous = None
        while True:
            try:
                with lock:
                    sessions = collector.collect()
                current = {}
                for s in sessions:
                    asking = any(st[0] == 'AskUserQuestion' and st[3] is None for st in s.transcript.steps)
                    current[s.record.get('sessionId')] = ('question' if asking else s.state, s)
                if previous is not None and subscriptions():
                    for key, (after, s) in current.items():
                        before = previous.get(key, ('new', None))[0]
                        if after == before:
                            continue
                        event = _event(s, before, after)
                        if event:
                            title, text = event
                            send(title, ' '.join(str(text).split())[:180], tag=key or '')
                previous = current
            except Exception:
                pass
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True).start()
