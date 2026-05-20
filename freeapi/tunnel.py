"""
Tunnel managers: Serveo (primary, permanent URL) + Cloudflare (fallback).

ServeoManager:
 - Подключается через SSH к serveo.net с фиксированным именем
 - Даёт постоянный URL https://<name>.serveo.net
 - Авто-переподключение при обрыве (с backoff)
 - on_url вызывается при каждом (пере)подключении

CloudflareManager:
 - Quick Tunnel через cloudflared
 - URL меняется при каждом перезапуске процесса
 - Используется как fallback если serveo недоступен
"""
import re
import subprocess
import threading
import time
import logging
from typing import Callable, Optional

logger = logging.getLogger('freeapi')

_CF_URL_RE = re.compile(r'https://[a-z0-9\-]+\.trycloudflare\.com')


# ══════════════════════════════════════════════════════════════════════
#  ServeoManager — постоянный публичный URL через ssh serveo.net
# ══════════════════════════════════════════════════════════════════════

class ServeoManager:
    """SSH reverse tunnel через serveo.net.

    Даёт постоянный HTTPS URL: https://<name>.serveo.net
    Автоматически переподключается при обрыве.
    """

    def __init__(self, port: int, name: str,
                 on_url: Optional[Callable[[str], None]] = None):
        self._port = port
        self._name = name
        self._on_url = on_url
        self._proc: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._url = f'https://{name}.serveo.net'

    def start(self):
        """Запустить туннель в фоне. Не блокирует."""
        t = threading.Thread(target=self._loop, daemon=True, name='serveo-runner')
        t.start()
        logger.info('[Serveo] Менеджер запущен (порт %s → %s)', self._port, self._url)

    def stop(self):
        logger.info('[Serveo] Остановка...')
        self._stop_event.set()
        self._kill_proc()
        logger.info('[Serveo] Остановлен')

    @property
    def url(self) -> str:
        return self._url

    # ── internal ──────────────────────────────────────────────────────

    def _loop(self):
        backoff = 5
        while not self._stop_event.is_set():
            connected = self._run_once()
            if self._stop_event.is_set():
                break
            if connected:
                backoff = 5  # сброс backoff при успешном подключении
                logger.info('[Serveo] Соединение прервано, переподключение через %ss...', backoff)
            else:
                logger.warning('[Serveo] Не удалось подключиться, повтор через %ss...', backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)

    def _run_once(self) -> bool:
        """Запустить SSH, вернуть True если соединение установилось."""
        try:
            proc = subprocess.Popen(
                [
                    'ssh',
                    '-R', f'{self._name}:80:localhost:{self._port}',
                    '-o', 'StrictHostKeyChecking=no',
                    '-o', 'ServerAliveInterval=30',
                    '-o', 'ServerAliveCountMax=3',
                    '-o', 'ExitOnForwardFailure=yes',
                    '-o', 'LogLevel=VERBOSE',
                    'serveo.net',
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            with self._proc_lock:
                self._proc = proc

            connected = False
            for line in proc.stdout:
                if self._stop_event.is_set():
                    break
                line = line.strip()
                if line:
                    logger.debug('[Serveo] %s', line)
                # Serveo пишет "Forwarding HTTP traffic from https://..." когда готов
                if 'serveo.net' in line and ('Forwarding' in line or 'forwarding' in line):
                    connected = True
                    logger.info('[Serveo] Туннель активен: %s', self._url)
                    if self._on_url:
                        try:
                            self._on_url(self._url)
                        except Exception as exc:
                            logger.error('[Serveo] on_url callback ошибка: %s', exc)

            proc.wait()
            return connected

        except FileNotFoundError:
            logger.warning('[Serveo] ssh не найден — туннель недоступен')
            return False
        except Exception as exc:
            logger.error('[Serveo] Ошибка: %s', exc)
            return False

    def _kill_proc(self):
        with self._proc_lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception as exc:
            logger.warning('[Serveo] Ошибка при завершении: %s', exc)


# ══════════════════════════════════════════════════════════════════════
#  CloudflareManager — Quick Tunnel (fallback, URL меняется при рестарте)
# ══════════════════════════════════════════════════════════════════════

class CloudflareManager:
    def __init__(self, port: int, on_url: Optional[Callable[[str], None]] = None):
        self._port = port
        self._on_url = on_url
        self._proc: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._current_url: Optional[str] = None

    def start(self):
        t = threading.Thread(target=self._run_once, daemon=True, name='cf-runner')
        t.start()
        logger.info('[Cloudflare] Менеджер запущен (порт %s)', self._port)

    def stop(self):
        logger.info('[Cloudflare] Получен сигнал остановки...')
        self._stop_event.set()
        self._kill_proc()
        logger.info('[Cloudflare] Менеджер остановлен')

    @property
    def current_url(self) -> Optional[str]:
        return self._current_url

    def _run_once(self):
        if self._stop_event.is_set():
            return
        self._start_proc()

    def _start_proc(self) -> Optional[str]:
        try:
            proc = subprocess.Popen(
                ['cloudflared', 'tunnel', '--url', f'http://localhost:{self._port}'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            with self._proc_lock:
                self._proc = proc

            url = None
            for line in proc.stdout:
                if self._stop_event.is_set():
                    break
                m = _CF_URL_RE.search(line)
                if m:
                    url = m.group(0)
                    break

            if url:
                self._current_url = url
                logger.info('[Tunnel] Cloudflare Tunnel активен: %s', url)
                if self._on_url:
                    try:
                        self._on_url(url)
                    except Exception as exc:
                        logger.error('[Cloudflare] on_url callback ошибка: %s', exc)

            threading.Thread(target=self._drain_stdout, args=(proc,), daemon=True).start()
            return url

        except FileNotFoundError:
            logger.warning('[Cloudflare] cloudflared не найден — тоннель не запущен')
            return None
        except Exception as exc:
            logger.error('[Cloudflare] Ошибка запуска: %s', exc)
            return None

    def _drain_stdout(self, proc: subprocess.Popen):
        try:
            for _ in proc.stdout:
                pass
        except Exception:
            pass

    def _kill_proc(self):
        with self._proc_lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            logger.info('[Cloudflare] Процесс завершён')
        except Exception as exc:
            logger.warning('[Cloudflare] Ошибка при завершении процесса: %s', exc)
