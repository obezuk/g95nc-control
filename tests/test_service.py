import http.client
import json
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor import DDCMonitor, MonitorError, decode_status, parse_registers, validate_write
from service import Controller, make_handler
SINGLE = 'VCP 60 SNC x0f\nVCP E2 CNC x00 x7f x39 xd0\nVCP E3 CNC x51 x51 x10 x10\n'
SPLIT = 'VCP 60 SNC x11\nVCP E2 CNC x00 x7f x39 xd1\nVCP E3 CNC x51 x51 x00 x10\n'

class ProtocolTests(unittest.TestCase):
    def test_real_captured_status(self):
        state = decode_status(parse_registers(SPLIT))
        self.assertEqual(state['mode'], 'two-pane')
        self.assertEqual(state['panes'], ['HDMI1', 'DisplayPort'])
        self.assertEqual(state['registers']['E3']['maximum'], 0x5151)
        self.assertEqual(state['ratio'], '50 / 50')
        self.assertEqual(decode_status(parse_registers(SINGLE))['mode'], 'single')
    def test_disabled_remembers_layout_bits(self):
        for signature in range(0,16,2):
            state=decode_status(parse_registers(SPLIT.replace('xd1',f'xd{signature:x}')))
            self.assertEqual(state['mode'],'single')
            self.assertIsNone(state['panes'])
    def test_reject_incomplete_or_wrong_monitor(self):
        for output in ['', 'VCP E2 unsupported', SINGLE.replace('x39', 'x40')]:
            with self.assertRaises(MonitorError):
                decode_status(parse_registers(output))
    def test_value_validation(self):
        self.assertEqual(validate_write('E3', '0x0110'), 272)
        for code, value in [('E2', True), ('E2', -1), ('E2', '01'), ('E2', 65536), ('E3', 0x0310), ('E3', 0x010f), ('FF', 1), ('60', '0x10')]:
            with self.subTest(code=code, value=value), self.assertRaises(ValueError):
                validate_write(code, value)
    def test_confirmed_sequence_and_readback(self):
        writes = []
        panes = [16, 0]
        def runner(command, **kwargs):
            if 'setvcp' in command:
                code, value = command[-2], int(command[-1], 16)
                writes.append((code, value))
                if code == 'E3':
                    if value == 0x1000:panes.reverse()
                    else:panes[value >> 8] = value & 255
                    self.assertEqual(len(set(panes)), 2)
            output = f'VCP 60 SNC x11\nVCP E2 CNC x00 x7f x39 xd1\nVCP E3 CNC x51 x51 x{panes[0]:02x} x{panes[1]:02x}\n'
            return SimpleNamespace(returncode=0, stdout=output, stderr='')
        state = DDCMonitor('serial', runner=runner, sleep=lambda _: None).apply('hdmi1-left-dp-right')
        self.assertEqual(writes[:1], [('E2', 3)])
        self.assertEqual(writes[1:], [('E3', 0x1000)])
        self.assertEqual(state['panes'], ['HDMI1', 'DisplayPort'])
    def test_partial_failure_stops_remaining_writes(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=1 if len(calls) == 3 else 0, stdout=SINGLE, stderr='I/O error')
        with self.assertRaises(MonitorError):
            DDCMonitor('serial', runner=runner, sleep=lambda _: None).apply('hdmi1-left-dp-right')
        self.assertEqual(len(calls), 3)
    def test_ignored_preset_not_success(self):
        def runner(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout=SINGLE, stderr='')
        with self.assertRaises(MonitorError):
            DDCMonitor('serial', runner=runner, sleep=lambda _: None).apply('hdmi1-left-dp-right')

class FakeMonitor:
    serial = 'test-monitor'
    def __init__(self):
        self.reads, self.writes = 0, []
    def read(self):
        self.reads += 1
        return decode_status(parse_registers(SPLIT))
    def read_register(self, code):
        self.reads += 1
        return parse_registers(SPLIT)[code]
    def apply(self, preset, progress):
        self.writes.append(preset)
        return self.read()
    def apply_layout(self, layout, progress):
        self.writes.append(layout)
        return self.read()
    def perform(self, writes, progress):
        self.writes.extend(writes)
        return self.read()

class APITests(unittest.TestCase):
    def setUp(self):
        self.monitor = FakeMonitor()
        self.controller = Controller(self.monitor)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.controller, 'x' * 40))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
    def request(self, method, path, body=None, auth=True):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        headers = {'Authorization': 'Bearer ' + 'x' * 40} if auth else {}
        if body is not None: headers['Content-Type'] = 'application/json'
        conn.request(method, path, None if body is None else json.dumps(body), headers)
        response = conn.getresponse()
        status, data = response.status, response.read()
        conn.close()
        return status, json.loads(data)
    def test_auth_required(self):
        self.assertEqual(self.request('GET','/v1/status',auth=False)[0],401)
        self.assertEqual(self.request('POST','/v1/vcp/E2',{'value':1},auth=False)[0],401)
        self.assertEqual(self.monitor.reads,0)
        self.assertEqual(self.monitor.writes,[])
    def test_get_never_writes(self):
        for path in ['/v1/status','/v1/monitor','/v1/vcp/E3','/v1/vcp','/v1/presets']:
            self.assertEqual(self.request('GET',path)[0],200)
        self.assertEqual(self.request('GET','/v1/presets/dp-fullscreen')[0],404)
        self.assertEqual(self.monitor.writes,[])
    def test_validation_and_busy(self):
        for body in [{'value':True},{'value':'0xffff'},{'value':1,'extra':1},[]]:
            self.assertEqual(self.request('POST','/v1/vcp/E2',body)[0],400)
        status, job = self.request('POST','/v1/vcp/E2',{'value':'0x01'})
        self.assertEqual(status,202)
        self.assertEqual(self.request('POST','/v1/presets/dp-fullscreen')[0],409)
        self.assertEqual(self.request('GET','/v1/monitor')[0],409)
        self.assertEqual(self.request('GET','/v1/status')[0],200)
        self.controller.execute(job['id'])
        _, done = self.request('GET',job['status_url'])
        self.assertEqual(done['status'],'succeeded')
        self.assertEqual(done['verification'],'readback_only')
        self.assertEqual(self.monitor.writes,[('E2',1)])
    def test_failed_job_retained(self):
        def failed(*args): raise MonitorError('partial write failure')
        self.monitor.apply = failed
        _, job = self.request('POST','/v1/presets/dp-fullscreen')
        self.controller.execute(job['id'])
        self.assertEqual(self.controller.job(job['id'])['status'],'failed')
        self.assertFalse(self.controller.snapshot()['busy'])
        self.assertEqual(self.controller.snapshot()['commands_failed'],1)
if __name__ == '__main__': unittest.main()
