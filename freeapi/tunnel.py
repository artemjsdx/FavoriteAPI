"""
Tunnel manager v2 — multi-provider fallback:

  1. Serveo (paramiko, custom subdomain из SERVEO_NAME, new key)
  2. Serveo fallback (генерирует альтернативные имена если основное занято)
  3. localhost.run (SSH, тот же ключ = тот же random URL между перезапусками)
  4. Cloudflare Quick Tunnel (cloudflared, random URL — последний резорт)

SSH ключ сохраняется в SERVEO_KEY_PATH (одинаковый для Serveo и localhost.run),
что гарантирует стабильность URL при перезапусках.

Env vars:
  SERVEO_KEY_PATH  — путь к файлу RSA ключа (def: ../serveo_key)
  SERVEO_NAME      — желаемый субдомен Serveo (def: favapi)
  TUNNEL_PROVIDER  — принудительно выбрать провайдер: serveo|localhostrun|cloudflare
"""
import logging
import os
import re
import select
import socket
import subprocess
import threading
import time
from typing import Callable, List, Optional

logger = logging.getLogger('freeapi')

DEFAULT_KEY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'serveo_key'
)

# Список кандидатов субдомена Serveo: основной + fallback имена
def _serveo_candidates(primary: str) -> List[str]:
    """Возвращает список кандидатов субдомена (основной + альтернативы)."""
    alts = [
        primary,
        f'{primary}2',
        f'{primary}3',
        f'{primary}api',
        f'{primary}bot',
        f'{primary}app',
    ]
    seen = set()
    result = []
    for a in alts:
        if a not in seen:
            seen.add(a)
            result.append(a)
    return result


class TunnelManager:
    def __init__(self, port: int,
                 on_url: Optional[Callable[[str], None]] = None):
        self._port = port
        self._on_url = on_url
        self._stop_event = threading.Event()
        self._url: Optional[str] = None
        self._lock = threading.Lock()

        key_path = os.environ.get('SERVEO_KEY_PATH', DEFAULT_KEY_PATH)
        self._key_path = os.path.realpath(key_path)

        # Читаем имя и автоматически мигрируем старое favoriteapi -> favapi
        raw_name = os.environ.get('SERVEO_NAME', 'favapi').strip()
        if 'favoriteapi' in raw_name.lower():
            logger.info('[Tunnel] SERVEO_NAME=%r -> мигрируем на favapi', raw_name)
            raw_name = 'favapi'
        self._primary_name = raw_name

        self._forced_provider = os.environ.get('TUNNEL_PROVIDER', '').lower()

    def start(self):
        t = threading.Thread(target=self._run, daemon=True, name='tunnel-mgr')
        t.start()

    def stop(self):
        self._stop_event.set()

    @property
    def url(self) -> Optional[str]:
        return self._url

    def _set_url(self, url: str):
        with self._lock:
            self._url = url
        logger.info('[Tunnel] ✅ URL активен: %s', url)
        if self._on_url:
            try:
                self._on_url(url)
            except Exception as exc:
                logger.error('[Tunnel] on_url callback ошибка: %s', exc)

    def _run(self):
        providers = self._build_provider_list()
        while not self._stop_event.is_set():
            for provider_fn, label in providers:
                if self._stop_event.is_set():
                    return
                logger.info('[Tunnel] Пробую провайдер: %s', label)
                try:
                    provider_fn()
                    logger.warning('[Tunnel] Провайдер %s отвалился', label)
                except Exception as exc:
                    logger.warning('[Tunnel] Провайдер %s ошибка: %s', label, exc)
                if self._stop_event.is_set():
                    return
                time.sleep(3)
            logger.warning('[Tunnel] Все провайдеры упали, ждём 30с перед повтором...')
            time.sleep(30)

    def _build_provider_list(self):
        forced = self._forced_provider
        if forced == 'serveo':
            return [(self._run_serveo, 'Serveo')]
        if forced == 'localhostrun':
            return [(self._run_localhostrun, 'localhost.run')]
        if forced == 'cloudflare':
            return [(self._run_cloudflare, 'Cloudflare')]
        return [
            (self._run_serveo, 'Serveo'),
            (self._run_localhostrun, 'localhost.run'),
            (self._run_cloudflare, 'Cloudflare'),
        ]

    # ── Serveo ───────────────────────────────────────────────────────────────

    def _run_serveo(self):
        import paramiko
        key = self._load_or_generate_key()
        candidates = _serveo_candidates(self._primary_name)
        backoff = 5

        while not self._stop_event.is_set():
            success = False
            for name in candidates:
                if self._stop_event.is_set():
                    return
                try:
                    logger.info('[Serveo] Пробую субдомен: %s.serveo.net', name)
                    self._serveo_connect(key, name)
                    success = True
                    backoff = 5
                    logger.info('[Serveo] Туннель %s.serveo.net отвалился', name)
                    break
                except _ServeoAuthError as e:
                    logger.warning('[Serveo] Субдомен %s занят/недоступен: %s', name, e)
                    continue
                except Exception as e:
                    logger.warning('[Serveo] Ошибка %s: %s', name, e)
                    break
            if success:
                if not self._stop_event.is_set():
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
            else:
                raise RuntimeError(f'Serveo: все кандидаты {candidates} недоступны')

    def _serveo_connect(self, key, name: str):
        import paramiko

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                'serveo.net', port=22,
                username='serveo',
                pkey=key,
                look_for_keys=False,
                allow_agent=False,
                timeout=20,
                banner_timeout=30,
                auth_timeout=20,
            )
        except paramiko.AuthenticationException as e:
            raise _ServeoAuthError(str(e)) from e

        transport = client.get_transport()
        transport.set_keepalive(30)

        try:
            transport.request_port_forward(name, 80, handler=self._make_forward_handler())
        except Exception as e:
            client.close()
            if 'Authentication' in str(e) or 'auth' in str(e).lower():
                raise _ServeoAuthError(str(e)) from e
            raise

        url = f'https://{name}.serveo.net'
        self._set_url(url)

        while transport.is_active() and not self._stop_event.is_set():
            time.sleep(2)

        client.close()

    # ── localhost.run ─────────────────────────────────────────────────────────

    def _run_localhostrun(self):
        self._load_or_generate_key()
        backoff = 5
        while not self._stop_event.is_set():
            try:
                self._localhostrun_connect()
                backoff = 5
            except Exception as e:
                logger.warning('[localhost.run] Ошибка: %s | повтор через %ss', e, backoff)
            if self._stop_event.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)

    def _localhostrun_connect(self):
        cmd = [
            'ssh',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', f'IdentityFile={self._key_path}',
            '-o', 'ServerAliveInterval=30',
            '-o', 'ServerAliveCountMax=3',
            '-o', 'BatchMode=yes',
            '-o', 'LogLevel=QUIET',
            '-R', f'80:localhost:{self._port}',
            'localhost.run',
        ]
        logger.info('[localhost.run] Запускаю: %s', ' '.join(cmd))
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        url_found = False
        # Читаем stdout в отдельном потоке чтобы stderr не блокировал
        lines_buf = []

        def _read_stdout():
            for line in proc.stdout:
                lines_buf.append(('out', line.rstrip()))
        def _read_stderr():
            for line in proc.stderr:
                lines_buf.append(('err', line.rstrip()))

        t1 = threading.Thread(target=_read_stdout, daemon=True)
        t2 = threading.Thread(target=_read_stderr, daemon=True)
        t1.start(); t2.start()

        deadline = time.time() + 30  # ждём URL максимум 30 секунд
        try:
            while time.time() < deadline and not self._stop_event.is_set():
                while lines_buf:
                    src, line = lines_buf.pop(0)
                    logger.debug('[localhost.run][%s] %s', src, line)
                    # Форматы URL от localhost.run:
                    # https://xxxxx.lhr.rocks tunneled with tls termination
                    # your url is: https://xxxxx.lhr.rocks
                    # Connect to http://localhost.run:80 []
                    # Forwarding HTTP traffic from https://xxxxx.lhr.rocks
                    m = re.search(r'(https://[w-]+.(?:lhr.rocks|localhost.run))', line)
                    if m:
                        self._set_url(m.group(1))
                        url_found = True
                if url_found:
                    break
                time.sleep(0.2)

            if url_found:
                # Туннель активен — ждём пока процесс жив
                while proc.poll() is None and not self._stop_event.is_set():
                    time.sleep(2)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            t1.join(timeout=2)
            t2.join(timeout=2)

        if not url_found:
            raise RuntimeError('localhost.run: URL не найден в выводе')

    # ── Cloudflare Quick Tunnel ───────────────────────────────────────────────

    def _run_cloudflare(self):
        cloudflared = self._find_cloudflared()
        if not cloudflared:
            raise RuntimeError('cloudflared не найден')

        backoff = 5
        while not self._stop_event.is_set():
            try:
                self._cloudflare_connect(cloudflared)
                backoff = 5
            except Exception as e:
                logger.warning('[Cloudflare] Ошибка: %s | повтор через %ss', e, backoff)
            if self._stop_event.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)

    def _find_cloudflared(self) -> Optional[str]:
        for name in ('cloudflared', 'cloudflared-linux-amd64', '/usr/local/bin/cloudflared'):
            try:
                subprocess.run([name, 'version'], capture_output=True, timeout=5)
                return name
            except Exception:
                continue
        return None

    def _cloudflare_connect(self, cloudflared: str):
        cmd = [cloudflared, 'tunnel', '--url', f'http://localhost:{self._port}']
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        url_found = False
        try:
            for line in proc.stdout:
                line = line.rstrip()
                logger.debug('[Cloudflare] %s', line)
                if 'trycloudflare.com' in line:
                    m = re.search(r'(https://[w-]+.trycloudflare.com)', line)
                    if m:
                        self._set_url(m.group(1))
                        url_found = True
                if self._stop_event.is_set():
                    break
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if not url_found:
            raise RuntimeError('Cloudflare: URL не найден')

    # ── SSH key management ────────────────────────────────────────────────────

    def _load_or_generate_key(self):
        import paramiko
        if os.path.exists(self._key_path):
            try:
                key = paramiko.RSAKey.from_private_key_file(self._key_path)
                logger.info('[Tunnel] Ключ загружен из %s (fp: %s)', self._key_path, key.get_fingerprint().hex())
                return key
            except Exception as e:
                logger.warning('[Tunnel] Не удалось загрузить ключ: %s — генерирую новый', e)

        logger.info('[Tunnel] Генерирую новый RSA ключ...')
        key = paramiko.RSAKey.generate(2048)
        try:
            os.makedirs(os.path.dirname(self._key_path) or '.', exist_ok=True)
            key.write_private_key_file(self._key_path)
            logger.info('[Tunnel] Ключ сохранён: %s', self._key_path)
            logger.info('[Tunnel] Публичный ключ: ssh-rsa %s favapi-key', key.get_base64())
        except Exception as e:
            logger.warning('[Tunnel] Не удалось сохранить ключ: %s', e)
        return key

    # ── Port forwarding handler (для Serveo paramiko) ─────────────────────────

    def _make_forward_handler(self):
        local_port = self._port

        def handler(channel, origin_addr, server_addr):
            sock = socket.socket()
            try:
                sock.connect(('localhost', local_port))
            except Exception as exc:
                logger.warning('[Serveo] Не удалось подключиться к localhost:%s: %s', local_port, exc)
                channel.close()
                return
            threading.Thread(
                target=_forward_sockets, args=(channel, sock),
                daemon=True, name='serveo-fwd',
            ).start()

        return handler


class _ServeoAuthError(Exception):
    """Serveo вернул ошибку аутентификации (субдомен занят другим ключом)."""


def _forward_sockets(src, dst):
    try:
        while True:
            r, _, _ = select.select([src, dst], [], [], 5)
            if src in r:
                data = src.recv(4096)
                if not data:
                    break
                dst.sendall(data)
            if dst in r:
                data = dst.recv(4096)
                if not data:
                    break
                src.sendall(data)
    except Exception:
        pass
    finally:
        for s in (src, dst):
            try:
                s.close()
            except Exception:
                pass


# Backward compatibility alias
class ServeoManager(TunnelManager):
    """Alias для старых импортов."""
    def __init__(self, port: int, name: str,
                 on_url: Optional[Callable[[str], None]] = None):
        os.environ.setdefault('SERVEO_NAME', name)
        super().__init__(port=port, on_url=on_url)
