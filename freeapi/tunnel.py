"""
ServeoManager — постоянный HTTPS туннель через serveo.net.

Использует paramiko (Python SSH), системный ssh не нужен.
Даёт постоянный URL https://<name>.serveo.net.
Автоматически переподключается при обрыве соединения.
"""
import logging
import socket
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger('freeapi')


class ServeoManager:
    """SSH reverse tunnel через serveo.net (paramiko, без системного ssh).

    Даёт постоянный HTTPS URL: https://<name>.serveo.net
    Автоматически переподключается при обрыве.
    """

    def __init__(self, port: int, name: str,
                 on_url: Optional[Callable[[str], None]] = None):
        self._port = port
        self._name = name
        self._on_url = on_url
        self._stop_event = threading.Event()
        self._url = f'https://{name}.serveo.net'
        self._key = None  # генерируется один раз при первом подключении

    def start(self):
        """Запустить туннель в фоне. Не блокирует."""
        t = threading.Thread(target=self._loop, daemon=True, name='serveo-runner')
        t.start()
        logger.info('[Serveo] Менеджер запущен (порт %s → %s)', self._port, self._url)

    def stop(self):
        logger.info('[Serveo] Остановка...')
        self._stop_event.set()

    @property
    def url(self) -> str:
        return self._url

    # ── internal ──────────────────────────────────────────────────────

    def _loop(self):
        backoff = 5
        while not self._stop_event.is_set():
            try:
                self._connect_once()
                backoff = 5
                logger.info('[Serveo] Соединение прервано, переподключение через %ss...', backoff)
            except Exception as exc:
                logger.warning('[Serveo] Ошибка подключения: %s | повтор через %ss', exc, backoff)
            if self._stop_event.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)

    def _get_key(self):
        """Возвращает RSA ключ (генерирует один раз на старте)."""
        import paramiko
        if self._key is None:
            logger.info('[Serveo] Генерирую RSA ключ для аутентификации...')
            self._key = paramiko.RSAKey.generate(2048)
        return self._key

    def _connect_once(self):
        import paramiko  # импорт здесь — чтобы не ломать старт если пакет ещё ставится

        key = self._get_key()

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            'serveo.net',
            port=22,
            username='serveo',
            pkey=key,
            look_for_keys=False,
            allow_agent=False,
            timeout=30,
        )
        transport = client.get_transport()
        transport.set_keepalive(30)

        # Запрашиваем reverse port forwarding: serveo:80 → localhost:self._port
        transport.request_port_forward(self._name, 80, handler=self._make_handler())

        logger.info('[Serveo] Туннель активен: %s', self._url)
        if self._on_url:
            try:
                self._on_url(self._url)
            except Exception as exc:
                logger.error('[Serveo] on_url callback ошибка: %s', exc)

        # Держим соединение пока transport жив
        while transport.is_active() and not self._stop_event.is_set():
            time.sleep(2)

        client.close()

    def _make_handler(self):
        """Возвращает handler для входящих соединений от serveo → localhost."""
        local_port = self._port

        def handler(channel, origin_addr, server_addr):
            sock = socket.socket()
            try:
                sock.connect(('localhost', local_port))
            except Exception as exc:
                logger.warning('[Serveo] Не удалось подключиться к localhost:%s: %s', local_port, exc)
                channel.close()
                return
            # Двусторонний проброс данных
            threading.Thread(
                target=_forward, args=(channel, sock),
                daemon=True, name='serveo-fwd',
            ).start()

        return handler


def _forward(src, dst):
    """Пробрасывает данные между двумя сокетами до закрытия."""
    import select
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
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass
