#!/usr/bin/env python3
"""Local monitor control service. GET reads; POST queues monitor changes."""
import argparse
import copy
import hmac
import json
import logging
import math
import queue
import signal
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from monitor import DDCMonitor, MonitorError, PRESETS, REGISTERS, validate_write, PROFILES, validate_layout

LOG = logging.getLogger('g95nc')
STATIC = Path(__file__).parent / 'web'


class BusyError(RuntimeError):
    pass


class Controller:
    def __init__(self, monitor, poll_interval=30):
        self.monitor, self.poll_interval = monitor, poll_interval
        self.lock, self.io_lock = threading.Lock(), threading.Lock()
        self.work = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.started = time.monotonic()
        self.jobs = {}
        self.state = {'available': False, 'monitor': None, 'last_updated': None,
                      'last_error': None, 'busy': False, 'last_completed_preset': None,
                      'last_job': None, 'commands_succeeded': 0, 'commands_failed': 0,
                      'read_failures': 0, 'last_read_duration_ms': None}
        self.thread = threading.Thread(target=self.worker, name='ddc-worker', daemon=True)

    def start(self):
        self.thread.start()

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.state)
        age = None if result['last_updated'] is None else max(0, time.time() - result['last_updated'])
        result.update(uptime_seconds=round(time.monotonic() - self.started),
                      sample_age_seconds=None if age is None else round(age, 1),
                      stale=age is None or age > self.poll_interval * 2 + 20,
                      serial=self.monitor.serial)
        return result

    def submit(self, action):
        with self.lock:
            if self.state['busy'] or self.stop_event.is_set():
                return None
            job = {'id': uuid.uuid4().hex, 'action': action, 'status': 'queued', 'progress': None,
                   'created_at': time.time(), 'finished_at': None, 'error': None, 'result': None}
            self.jobs[job['id']] = job
            while len(self.jobs) > 32:
                del self.jobs[next(iter(self.jobs))]
            self.state.update(busy=True, last_job=copy.deepcopy(job))
            self.work.put_nowait(job['id'])
            return copy.deepcopy(job)

    def job(self, job_id):
        with self.lock:
            return copy.deepcopy(self.jobs.get(job_id))

    def record_read(self, state):
        self.state.update(available=True, monitor=state, last_updated=time.time(), last_error=None)

    def _read(self, code=None):
        started = time.monotonic()
        try:
            value = self.monitor.read_register(code) if code else self.monitor.read()
            with self.lock:
                if code is None:
                    self.record_read(value)
            return value
        except Exception as error:
            with self.lock:
                self.state.update(available=False, last_error=str(error))
                self.state['read_failures'] += 1
            raise
        finally:
            with self.lock:
                self.state['last_read_duration_ms'] = round((time.monotonic() - started) * 1000)

    def fresh_read(self, code=None):
        if not self.io_lock.acquire(blocking=False):
            raise BusyError('Monitor I/O is busy; cached state is available at /v1/status')
        try:
            with self.lock:
                if self.state['busy']:
                    raise BusyError('A monitor action is queued or running')
            return self._read(code)
        finally:
            self.io_lock.release()

    def execute(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            job['status'] = 'running'
            self.state['last_job'] = copy.deepcopy(job)
        def progress(step):
            with self.lock:
                job['progress'] = step
                self.state['last_job'] = copy.deepcopy(job)
            LOG.info('Job %s: VCP %s <- %s', job_id, step['code'], step['value'])
        try:
            with self.io_lock:
                action = job['action']
                if action['type'] == 'layout':
                    state = self.monitor.apply_layout(action['layout'], progress)
                elif action['type'] == 'preset':
                    state = self.monitor.apply(action['preset'], progress)
                else:
                    state = self.monitor.perform([(action['code'], action['value'])], progress)
            with self.lock:
                self.record_read(state)
                job.update(status='succeeded', result=state,
                           verification=state.get('verification', 'readback_only'))
                # Historical command record, never presented as measured current preset.
                self.state['last_completed_preset'] = action.get('preset')
                self.state['commands_succeeded'] += 1
        except Exception as error:
            with self.lock:
                job.update(status='failed', error=str(error))
                self.state.update(available=False, last_error=str(error))
                self.state['commands_failed'] += 1
            LOG.warning('Job %s failed; earlier writes may have applied: %s', job_id, error)
        finally:
            with self.lock:
                job['finished_at'] = time.time()
                self.state.update(busy=False, last_job=copy.deepcopy(job))

    def worker(self):
        next_poll = 0
        while not self.stop_event.is_set():
            try:
                job_id = self.work.get(timeout=max(0, min(1, next_poll - time.monotonic())))
            except queue.Empty:
                if time.monotonic() >= next_poll:
                    try:
                        self.fresh_read()
                    except BusyError:
                        pass
                    except Exception as error:
                        LOG.warning('Status read failed: %s', error)
                    next_poll = time.monotonic() + self.poll_interval
                continue
            self.execute(job_id)
            self.work.task_done()
            next_poll = time.monotonic() + self.poll_interval


def make_handler(controller, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'G95NC/1'

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, fmt, *args):
            # Do not log URLs/query strings, headers, bodies, or credentials.
            LOG.info('%s %s request', self.client_address[0], self.command)

        def send(self, status, payload, content_type='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def reply(self, status, body):
            self.send(status, json.dumps(body).encode())

        def authorized(self):
            if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + token).encode()):
                self.reply(401, {'error': 'Authentication required'})
                return False
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                      '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if path in assets:
                name, mime = assets[path]
                self.send(200, (STATIC / name).read_bytes(), mime)
                return
            if not self.authorized():
                return
            try:
                if path == '/v1/status':
                    self.reply(200, controller.snapshot())
                elif path == '/v1/monitor':
                    self.reply(200, {'monitor': controller.fresh_read(), 'read_at': time.time()})
                elif path == '/v1/layouts':
                    self.reply(200, {'profiles': PROFILES, 'inputs': ['HDMI1', 'HDMI2', 'HDMI3', 'DisplayPort']})
                elif path == '/v1/vcp':
                    self.reply(200, {'registers': REGISTERS})
                elif path.startswith('/v1/vcp/') and path.rsplit('/', 1)[-1].upper() in REGISTERS:
                    self.reply(200, {**controller.fresh_read(path.rsplit('/', 1)[-1].upper()), 'read_at': time.time()})
                elif path == '/v1/presets':
                    self.reply(200, {'presets': [{'id': k, **v} for k, v in PRESETS.items()]})
                elif path.startswith('/v1/jobs/'):
                    job = controller.job(path.rsplit('/', 1)[-1])
                    self.reply(200 if job else 404, job or {'error': 'Unknown job'})
                else:
                    self.reply(404, {'error': 'Unknown endpoint'})
            except BusyError as error:
                self.reply(409, {'error': str(error)})
            except MonitorError as error:
                self.reply(503, {'error': str(error)})

        def body(self):
            if self.headers.get('Transfer-Encoding'):
                raise ValueError('Chunked bodies are not supported')
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise ValueError('Invalid Content-Length') from None
            if not 0 <= size <= 1024:
                raise ValueError('Body must be at most 1024 bytes')
            if size == 0:
                return {}
            if self.headers.get_content_type() != 'application/json':
                raise ValueError('Use Content-Type: application/json')
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError('Incomplete request body')
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ValueError('Invalid JSON') from None
            if not isinstance(result, dict):
                raise ValueError('Expected a JSON object')
            return result

        def do_POST(self):
            if not self.authorized():
                return
            path = urlsplit(self.path).path
            try:
                body = self.body()
                if path == '/v1/layout':
                    action = {'type': 'layout', 'layout': validate_layout(body)}
                elif path.startswith('/v1/presets/') and path.rsplit('/', 1)[-1] in PRESETS:
                    if body:
                        raise ValueError('Preset actions take an empty body')
                    action = {'type': 'preset', 'preset': path.rsplit('/', 1)[-1]}
                elif path.startswith('/v1/vcp/') and path.rsplit('/', 1)[-1].upper() in REGISTERS:
                    if set(body) != {'value'}:
                        raise ValueError('Expected exactly one field: value')
                    code = path.rsplit('/', 1)[-1].upper()
                    if code in ('E3', '60'):
                        raise ValueError('Input changes require POST /v1/layout with a complete unique assignment; mode is applied first')
                    action = {'type': 'vcp', 'code': code, 'value': validate_write(code, body['value'])}
                else:
                    self.reply(404, {'error': 'Unknown action'})
                    return
                job = controller.submit(action)
                self.reply(202, {**job, 'status_url': '/v1/jobs/' + job['id']}) if job else self.reply(409, {'error': 'A monitor action is queued or running'})
            except (ValueError, TimeoutError) as error:
                self.reply(400, {'error': str(error)})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    token = Path(config['token_file']).expanduser().read_text().strip()
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        parser.error('token_file must contain at least 32 non-whitespace ASCII characters')
    interval, settle = float(config.get('poll_interval', 30)), float(config.get('settle', 3))
    if not math.isfinite(interval) or not math.isfinite(settle) or interval < 5 or not 1 <= settle <= 30:
        parser.error('poll_interval must be finite and >=5; settle must be between 1 and 30')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    controller = Controller(DDCMonitor(config['serial'], settle=settle), interval)
    server = ThreadingHTTPServer((config.get('host', '127.0.0.1'), int(config.get('port', 8765))), make_handler(controller, token))
    server.daemon_threads = True
    def shutdown(signum, frame):
        controller.stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    controller.start()
    LOG.info('Listening on %s:%s for serial %s', *server.server_address, controller.monitor.serial)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        controller.stop_event.set()
        controller.thread.join(timeout=180)


if __name__ == '__main__':
    main()
