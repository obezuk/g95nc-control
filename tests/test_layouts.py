import itertools
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from monitor import validate_layout, unique_assignment_plan, SOURCE_CODES, PANE_INPUTS, DDCMonitor, MonitorError
from test_service import APITests, SINGLE

class LayoutTests(unittest.TestCase):
    def test_duplicate_inputs_and_wrong_count_rejected(self):
        for sources in [['DisplayPort','DisplayPort'], ['HDMI1'], ['HDMI1','HDMI2','HDMI3']]:
            with self.assertRaises(ValueError):
                validate_layout({'profile':'50-50','inputs':sources})
    def test_calibrated_mappings(self):
        for profile,command,count in [('25-75',5,2),('75-25',4,2),('33-33-33',6,3),('50-25-25',7,3),('25-50-25',8,3),('25-25-50',9,3),('pip',16,2)]:
            result=validate_layout({'profile':profile,'inputs':list(SOURCE_CODES)[:count]})
            self.assertEqual(result['command'],command)
            self.assertTrue(result['mapping_confirmed'])
    def test_captured_calibration_status(self):
        import json
        from monitor import decode_status
        fixture = Path(__file__).parent / 'fixtures' / 'firmware-1009.2.json'
        for row in json.loads(fixture.read_text()):
            state = decode_status(row['registers'])
            self.assertEqual(state['profile'], row['profile'])
            if row['profile'] == 'pip':
                self.assertEqual(state['panes'], ['DisplayPort', 'HDMI1'])
    def test_every_two_and_three_pane_permutation_stays_unique(self):
        for size in (2,3):
            for old in itertools.permutations(SOURCE_CODES,size):
                for new in itertools.permutations(SOURCE_CODES,size):
                    current=list(old)
                    for code,value in unique_assignment_plan(old,list(new)):
                        current[value>>8]=PANE_INPUTS[value&255]
                        self.assertEqual(len(set(current)),size)
                    self.assertEqual(current,list(new))
    def test_exit_pip_selects_pbp_before_ratio_and_inputs(self):
        signature=15
        writes=[]
        def run(cmd,**kwargs):
            nonlocal signature
            if 'setvcp' in cmd:
                code,value=cmd[-2],int(cmd[-1],16)
                writes.append((code,value))
                if code=='E2' and value==2:signature=1
                if code=='E2' and value==5 and signature!=15:signature=5
                if code=='E3':self.assertEqual(signature,5)
            output=f'VCP 60 SNC x0f\nVCP E2 CNC x00 x7f x39 xd{signature:x}\nVCP E3 CNC x51 x51 x10 x00\n'
            return SimpleNamespace(returncode=0,stdout=output,stderr='')
        result=DDCMonitor('serial',runner=run,sleep=lambda _:None).apply_layout(validate_layout({'profile':'25-75','inputs':['DisplayPort','HDMI1']}))
        self.assertEqual(writes[:2],[('E2',2),('E2',5)])
        self.assertEqual(result['profile'],'25-75')
    def test_layout_transition_commands_depend_on_current_mode(self):
        cases=[(1,'25-75',[5]),(5,'50-50',[3]),(7,'75-25',[4]),
               (15,'25-75',[2,5]),(14,'25-75',[1,2,5]),
               (0,'25-75',[1,5]),(4,'25-75',[1,5]),
               (1,'pip',[16]),(15,'pip',[16]),(14,'pip',[1,16])]
        for initial,profile,expected in cases:
            with self.subTest(initial=initial,profile=profile):
                signature=initial
                writes=[]
                def run(cmd,**kwargs):
                    nonlocal signature
                    if 'setvcp' in cmd:
                        code,value=cmd[-2],int(cmd[-1],16)
                        writes.append((code,value))
                        if code=='E2':
                            if value==1:signature|=1
                            elif value==2:signature=1
                            else:signature={3:1,4:3,5:5,16:15}[value]
                    return SimpleNamespace(returncode=0,stderr='',stdout=f'VCP 60 SNC x0f\nVCP E2 CNC x00 x7f x39 xd{signature:x}\nVCP E3 CNC x51 x51 x10 x00\n')
                DDCMonitor('serial',runner=run,sleep=lambda _:None).apply_layout(validate_layout({'profile':profile,'inputs':['DisplayPort','HDMI1']}))
                self.assertEqual(writes,[('E2',v) for v in expected])
    def test_native_swap_is_verified_and_not_retried(self):
        from test_service import SPLIT
        writes=[]
        def run(cmd,**kwargs):
            if 'setvcp' in cmd:writes.append((cmd[-2],int(cmd[-1],16)))
            return SimpleNamespace(returncode=0,stdout=SPLIT,stderr='')
        with self.assertRaises(MonitorError):
            DDCMonitor('serial',runner=run,sleep=lambda _:None).apply_layout(validate_layout({'profile':'50-50','inputs':['DisplayPort','HDMI1']}))
        self.assertEqual(writes,[('E2',3),('E3',0x1000)])
    def test_fullscreen_changes_mode_before_input(self):
        writes=[]
        def run(cmd,**kwargs):
            if 'setvcp' in cmd:writes.append((cmd[-2],int(cmd[-1],16)))
            return SimpleNamespace(returncode=0,stdout=SINGLE,stderr='')
        DDCMonitor('serial',runner=run,sleep=lambda _:None).apply('dp-fullscreen')
        self.assertEqual(writes,[('E2',0),('60',15)])
    def test_ignored_mode_sends_no_input_commands(self):
        writes=[]
        def run(cmd,**kwargs):
            if 'setvcp' in cmd:writes.append(cmd[-2])
            return SimpleNamespace(returncode=0,stdout=SINGLE,stderr='')
        with self.assertRaises(MonitorError):
            DDCMonitor('serial',runner=run,sleep=lambda _:None).apply('hdmi1-left-dp-right')
        self.assertEqual(writes,['E2','E2'])

class LayoutAPI(APITests):
    def test_layout_api_rejects_duplicates_before_queuing(self):
        code,_=self.request('POST','/v1/layout',{'profile':'50-50','inputs':['HDMI1','HDMI1']})
        self.assertEqual(code,400)
        self.assertFalse(self.controller.snapshot()['busy'])
    def test_layout_accepts_complete_assignment(self):
        code,job=self.request('POST','/v1/layout',{'profile':'50-50','inputs':['HDMI1','DisplayPort']})
        self.assertEqual(code,202)
        self.controller.execute(job['id'])
        self.assertEqual(self.controller.job(job['id'])['status'],'succeeded')
    def test_input_only_calls_cannot_bypass_layout(self):
        for register,value in [('E3',16),('60',15)]:
            self.assertEqual(self.request('POST','/v1/vcp/'+register,{'value':value})[0],400)
        self.assertEqual(self.monitor.writes,[])
if __name__=='__main__':unittest.main()
