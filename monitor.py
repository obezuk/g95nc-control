"""Samsung G95NC DDC/CI commands. No shell or arbitrary VCP writes."""
import os
import subprocess
import time

PRESETS = {
    'hdmi1-left-dp-right': {
        'name': 'HDMI1 + DisplayPort', 'description': '50/50 · HDMI1 left, DisplayPort right',
        'tested': True,
        'writes': [('E2', 0x01), ('E2', 0x03), ('E3', 0x0110), ('E3', 0x0000)],
    },
    'dp-fullscreen': {
        'name': 'DisplayPort', 'description': 'Full screen · PBP disabled',
        'tested': False,
        'writes': [('E2', 0x00), ('60', 0x0f)],
    },
}
INPUTS = {0x0f: 'DisplayPort', 0x11: 'HDMI1', 0x12: 'HDMI2', 0x32: 'HDMI3'}
PANE_INPUTS = {0: 'HDMI1', 1: 'HDMI2', 2: 'HDMI3', 0x10: 'DisplayPort'}
LAYOUTS = {
    0: 'Disable PBP/PIP', 1: 'Enable remembered PBP/PiP mode', 2: 'Select PBP mode', 5: '25/75 split', 6: 'Three panes: equal thirds', 3: '50/50 split (enable PBP first)',
    4: '75/25 split', 7: 'Three panes: 50/25/25',
    8: 'Three panes: 25/50/25', 9: 'Three panes: 25/25/50', 0x10: 'Picture-in-picture',
}
REGISTERS = {
    'E2': {'name': 'PBP / PIP mode and layout', 'encoding': 'Command value; readback is packed status',
           'values': {f'0x{k:04x}': v for k, v in LAYOUTS.items()}},
    'E3': {'name': 'Pane input', 'encoding': '(zero-based pane << 8) | source; packed readback',
           'sources': {f'0x{k:02x}': v for k, v in PANE_INPUTS.items()},
           'native_swap': {'0x1000': 'Swap first two panes; verified for two-pane PBP'}},
    '60': {'name': 'Main input', 'encoding': 'MCCS input code; differs from E3 source codes',
           'values': {f'0x{k:04x}': v for k, v in INPUTS.items()}},
}


class MonitorError(RuntimeError):
    pass


def parse_registers(output):
    values = {}
    for line in output.splitlines():
        f = line.split()
        if len(f) < 4 or f[0] != 'VCP':
            continue
        try:
            if f[2] == 'CNC' and len(f) == 7:
                parts = [int(v.removeprefix('x'), 16) for v in f[3:]]
                if any(not 0 <= v <= 255 for v in parts):
                    continue
                maximum, current = parts[0] * 256 + parts[1], parts[2] * 256 + parts[3]
            elif f[2] == 'SNC' and len(f) == 4:
                maximum, current = None, int(f[3].removeprefix('x'), 16)
            else:
                continue
            if not 0 <= current <= 65535:
                continue
            values[f[1].upper()] = {'code': f[1].upper(), 'current': current,
                                    'current_hex': f'0x{current:04x}', 'maximum': maximum,
                                    'type': f[2]}
        except ValueError:
            continue
    return values


def decode_status(registers):
    if not all(code in registers for code in REGISTERS):
        raise MonitorError('Incomplete DDC reply: expected E2, E3 and 60')
    e2, e3, main = (registers[c]['current'] for c in ('E2', 'E3', '60'))
    if e2 >> 8 != 0x39:
        raise MonitorError('Unexpected E2 status family; expected G95NC 0x39xx')
    # Only decode the signatures seen on this monitor; preserve unknown modes.
    signature = e2 & 0x0f
    profile_id = {1: '50-50', 3: '75-25', 5: '25-75',
                  7: '33-33-33', 9: '50-25-25', 11: '25-50-25', 13: '25-25-50', 15: 'pip'}.get(signature)
    if not signature & 1:
        profile_id = 'single'
    mode = ('single' if not signature & 1 else 'two-pane' if signature in (1, 3, 5)
            else 'three-pane' if signature in (7, 9, 11, 13) else 'pip' if signature == 15 else 'unknown')
    panes = None
    if mode in ('two-pane', 'pip'):
        left, right = e3 >> 8, e3 & 255
        if left in PANE_INPUTS and right in PANE_INPUTS:
            panes = [PANE_INPUTS[left], PANE_INPUTS[right]]
    return {'registers': registers, 'raw': {c: v['current_hex'] for c, v in registers.items()},
            'mode': mode, 'input': INPUTS.get(main, 'unknown'), 'panes': panes,
            'profile': profile_id,
            'ratio': PROFILES[profile_id]['label'] if profile_id else None}


def validate_write(code, value):
    if code not in REGISTERS:
        raise ValueError('Supported registers: E2, E3, 60')
    if isinstance(value, bool):
        raise ValueError('value must be an integer or a 0x-prefixed hex string')
    if isinstance(value, str):
        if not value.lower().startswith('0x'):
            raise ValueError('String values must start with 0x; JSON integers are decimal')
        try:
            value = int(value, 16)
        except ValueError:
            raise ValueError('Invalid hexadecimal value') from None
    if not isinstance(value, int) or not 0 <= value <= 65535:
        raise ValueError('value must be a 16-bit integer')
    valid = (code == 'E2' and value in LAYOUTS or
             code == '60' and value in INPUTS or
             code == 'E3' and (value == 0x1000 or value >> 8 in (0, 1, 2) and value & 255 in PANE_INPUTS))
    if not valid:
        raise ValueError('Value is not supported for this register; see GET /v1/vcp')
    return value


class DDCMonitor:
    def __init__(self, serial, settle=3.0, executable='/usr/bin/ddcutil', runner=subprocess.run, sleep=time.sleep):
        self.serial, self.settle = serial, settle
        self.executable, self.runner, self.sleep = executable, runner, sleep

    def command(self, *args):
        try:
            result = self.runner([self.executable, '--noconfig', '--sn', self.serial, *args],
                                 capture_output=True, text=True, timeout=20,
                                 env={**os.environ, 'LC_ALL': 'C'})
        except (OSError, subprocess.TimeoutExpired) as error:
            raise MonitorError(f'DDC command failed: {error}') from error
        if result.returncode:
            raise MonitorError(f'DDC exit {result.returncode}: {(result.stderr or result.stdout).strip()[-1000:]}')
        return result.stdout

    def read_register(self, code):
        if code not in REGISTERS:
            raise ValueError('Unsupported register')
        values = parse_registers(self.command('getvcp', code, '--brief'))
        if code not in values:
            raise MonitorError(f'No valid reply for {code}')
        return values[code]

    def read(self):
        return decode_status(parse_registers(self.command('getvcp', 'E2', 'E3', '60', '--brief')))

    def perform(self, writes, progress=lambda step: None):
        self.read()  # Validate the selected display before writes.
        for code, value in writes:
            validate_write(code, value)
        for index, (code, value) in enumerate(writes):
            progress({'index': index + 1, 'total': len(writes), 'code': code, 'value': f'0x{value:04x}'})
            self.command('--noverify', '--permit-unknown-feature', 'setvcp', code, f'0x{value:04x}')
            self.sleep(self.settle)
        last_error = None
        for attempt in range(3):
            try:
                return self.read()
            except MonitorError as error:
                last_error = error
                if attempt < 2:
                    self.sleep(self.settle)
        raise last_error



    def apply_layout(self, layout, progress=lambda step: None):
        profile = PROFILES[layout['profile']]
        # This is the one transaction path for both the editor and old preset URLs.
        # ALL mode writes finish (including settling/readback) before any input write.
        before = self.read()
        signature = int(before['raw']['E2'], 16) & 15
        modes = []
        if profile['kind'] != 'single':
            if not signature & 1:
                modes.append(('E2', 0x01))
            # Enable restores the remembered mode. Select PBP only when that
            # mode is PiP, avoiding a detour through another PBP layout.
            if profile['kind'] == 'pbp' and signature & 14 == 14:
                modes.append(('E2', 0x02))
        # Reassert and verify the requested layout before any input assignment.
        modes.append(('E2', layout['command']))
        def mode_progress(step):
            progress({**step, 'phase': 'layout'})
        state = self.perform(modes, mode_progress)
        target = layout['inputs']
        expected = {0: 0, 1: 1, 3: 1, 4: 3, 5: 5, 6: 7, 7: 9, 8: 11, 9: 13, 16: 15}.get(layout['command'])
        actual = int(state['raw']['E2'], 16) & 15
        matches = not actual & 1 if expected == 0 else actual == expected
        if expected is not None and not matches:
            raise MonitorError(f'Mode readback {state["raw"]["E2"]} did not match {layout["profile"]}; no input commands were sent')
        if profile['kind'] == 'single':
            writes = [('60', MAIN_CODES[target[0]])]
        elif (profile['kind'] == 'pbp' and state['mode'] == 'two-pane'
              and len(target) == 2 and state.get('panes') == list(reversed(target))):
            # Firmware routes E3=0x1000 to the OSD swap callback. One command
            # avoids the temporary spare-input assignment. Never retry a toggle.
            writes = [('E3', 0x1000)]
        elif state.get('panes') and len(state['panes']) == len(target) and len(set(state['panes'])) == len(target):
            writes = unique_assignment_plan(state['panes'], target)
        else:
            # Packed three-pane/PIP source readback is not fully decoded. Assign the
            # complete validated, distinct target; do not claim transient verification.
            writes = [('E3', (i << 8) | SOURCE_CODES[name]) for i, name in enumerate(target)]
        def input_progress(step):
            progress({**step, 'phase': 'inputs'})
        result = self.perform(writes, input_progress) if writes else state
        # Verify every source when decoded. Otherwise expose the raw reply and the
        # explicit verification limit rather than falsely reporting full confirmation.
        if profile['kind'] == 'single':
            if result['mode'] != 'single' or result['input'] != target[0]:
                raise MonitorError('Full-screen readback does not match requested input')
        elif result.get('panes'):
            if result['panes'] != target:
                raise MonitorError('Pane readback does not match requested inputs')
        result['applied_layout'] = layout
        result['verification'] = 'sources_confirmed' if profile['kind'] == 'single' or result.get('panes') == target else 'readback_only'
        return result


    def apply(self, preset, progress=lambda step: None):
        if preset not in PRESETS:
            raise ValueError('Unknown preset')
        body = {'profile': '50-50', 'inputs': ['HDMI1', 'DisplayPort']} if preset == 'hdmi1-left-dp-right' else {'profile': 'single', 'inputs': ['DisplayPort']}
        return self.apply_layout(validate_layout(body), progress)


# Geometry belongs to the editor. None means an exact command-to-ratio mapping
# has not been established; the caller must supply an explicit candidate command.
PROFILES = {
    'single': {'label': 'Full screen', 'widths': [100], 'command': 0, 'kind': 'single'},
    '50-50': {'label': '50 / 50', 'widths': [50, 50], 'command': 3, 'kind': 'pbp'},
    '25-75': {'label': '25 / 75', 'widths': [25, 75], 'command': 5, 'kind': 'pbp'},
    '75-25': {'label': '75 / 25', 'widths': [75, 25], 'command': 4, 'kind': 'pbp'},
    '33-33-33': {'label': '33 / 33 / 33', 'widths': [100/3]*3, 'command': 6, 'kind': 'pbp'},
    '50-25-25': {'label': '50 / 25 / 25', 'widths': [50, 25, 25], 'command': 7, 'kind': 'pbp'},
    '25-50-25': {'label': '25 / 50 / 25', 'widths': [25, 50, 25], 'command': 8, 'kind': 'pbp'},
    '25-25-50': {'label': '25 / 25 / 50', 'widths': [25, 25, 50], 'command': 9, 'kind': 'pbp'},
    'pip': {'label': '100 / PiP', 'widths': [100, 25], 'command': 16, 'kind': 'pip'},
}
SOURCE_CODES = {name: code for code, name in PANE_INPUTS.items()}
MAIN_CODES = {name: code for code, name in INPUTS.items()}


def validate_layout(body):
    if not isinstance(body, dict) or set(body) - {'profile', 'inputs', 'layout_command'}:
        raise ValueError('Expected profile, inputs, and optional layout_command')
    profile_id = body.get('profile')
    if not isinstance(profile_id, str) or profile_id not in PROFILES:
        raise ValueError('Unknown layout profile')
    profile = PROFILES[profile_id]
    inputs = body.get('inputs')
    if not isinstance(inputs, list) or len(inputs) != len(profile['widths']):
        raise ValueError('Assign one input to each pane')
    if any(not isinstance(name, str) or name not in SOURCE_CODES for name in inputs):
        raise ValueError('Inputs must be HDMI1, HDMI2, HDMI3, or DisplayPort')
    if len(set(inputs)) != len(inputs):
        raise ValueError('Each input may appear only once; swap pane assignments instead')
    command = profile['command']
    if command is None:
        candidate = body.get('layout_command')
        if candidate is None:
            raise ValueError('Exact ratio mapping is unconfirmed; choose an explicit layout_command for calibration')
        command = validate_write('E2', candidate)
        allowed = (2, 4, 5) if len(inputs) == 2 else (6, 7, 8, 9)
        if command not in allowed:
            raise ValueError('Calibration command must match the pane count')
    elif 'layout_command' in body:
        raise ValueError('This profile already has a command mapping')
    return {'profile': profile_id, 'inputs': list(inputs), 'command': command,
            'mapping_confirmed': profile['command'] is not None}


def unique_assignment_plan(current, target):
    """Resolve known pane swaps via an unused input; never create duplicates.

    Four physical inputs leave at least one spare for a cycle of <=3 panes.
    """
    current = list(current)
    if len(current) != len(target) or len(set(current)) != len(current):
        raise ValueError('Current pane state is not unique and complete')
    writes = []
    while current != target:
        pending = [i for i in range(len(target)) if current[i] != target[i]]
        free = next((i for i in pending if target[i] not in current), None)
        if free is None:
            free = pending[0]
            source = next(name for name in SOURCE_CODES if name not in current)
        else:
            source = target[free]
        current[free] = source
        writes.append(('E3', (free << 8) | SOURCE_CODES[source]))
    return writes
