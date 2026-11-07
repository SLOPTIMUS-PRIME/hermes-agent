import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('maintainer',ROOT/'maintainer.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Hardening(unittest.TestCase):
    def test_git_blob_is_not_truncated_to_log_tail_limit(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);content='BEGIN\n'+('x'*40000)+'\nEND\n'
            subprocess.run(['git','init','-q',td],check=True)
            (p/'source.py').write_text(content)
            subprocess.run(['git','-C',td,'add','source.py'],check=True)
            subprocess.run(['git','-C',td,'-c','user.name=test','-c','user.email=test@example.invalid','commit','-qm','base'],check=True)
            git=m.Git({'command_timeout':10,'output_capture_bytes':20000,'max_input_bytes':100000},td,[])
            self.assertEqual(git.file_bytes('HEAD:source.py').decode(),content)
    def test_path_identifier_is_not_silently_normalized(self):
        for bad in ['agent/../agent/curator.py',' agent/curator.py','agent/curator.py ','agent//curator.py']:
            with self.subTest(path=bad):self.assertIsNone(m.normalize_rel_path(bad))
    def test_serialized_unicode_payload_fits_budget_without_changing_intents(self):
        payload={'patch_intents':{'x.py':'preserve these rules'},'context':{},'diagnostics':[{'source':'test','detail':'trace '*1000}], 'conflicts':[{'path':'x.py','base':'é'*5000,'ours':'日'*5000,'theirs':'é'*5000,'merged':'日'*5000}]}
        fitted=m.fit_repair_payload(payload,5000)
        self.assertLessEqual(len(json.dumps(fitted).encode('utf-8')),5000)
        self.assertEqual(fitted['patch_intents'],payload['patch_intents'])
        self.assertIn('truncated',json.dumps(fitted))
    def test_streaming_output_cap_stops_a_noisy_child(self):
        result=m.run_cmd([sys.executable,'-c','import sys; sys.stdout.write("x"*5000000)'],timeout=5,capture_bytes=1000)
        self.assertNotEqual(result.rc,0)
        self.assertLessEqual(len(result.out.encode()),1000)
        self.assertIn('hard byte limit',result.err)
    def test_failed_remote_lookup_is_not_an_absent_branch(self):
        cfg={'state_dir':str(ROOT),'command_timeout':5,'output_capture_bytes':20000}
        with self.assertRaises(m.MaintainerAbort):m.lsremote_sha(cfg,'/nonexistent-maintainer-fixture-repo','nick-patches')
import test_maintainer as fixtures
class RecoveryAndRepairContext(fixtures.MaintainerTestBase):
    def test_failed_test_repair_receives_current_allowlisted_source(self):
        self.lab.seed_at(self.lab.A)
        self.lab.test_adapter(fixtures.TEST_ADAPTER_NEEDS_ADAPTED)
        self.lab.repair({'status':'blocked','reason':'test stopped','edits':[]})
        r=self.lab.run(self.lab.config(seed_sha=self.lab.A))
        self.assertEqual(r.returncode,1)
        calls=self.lab.repair_calls()
        records=calls[0]['stdin']['conflicts']
        self.assertTrue(any(c['path']=='src/lib.py' and 'value =' in c['merged'] for c in records))
    def test_post_failure_owner_edit_is_never_reset(self):
        self.lab=self.new_lab(official_variant='lib')
        self.lab.repair({'status':'blocked','reason':'test stopped','edits':[]})
        cfg=self.lab.config()
        self.assertEqual(self.lab.run(cfg).returncode,1)
        p=self.lab.state/'candidate'/'src/lib.py'
        p.write_text(p.read_text()+'\n# owner edit after failed maintenance\n')
        expected=p.read_text()
        r=self.lab.run(cfg)
        self.assertEqual(r.returncode,4,r.stderr)
        self.assertEqual(p.read_text(),expected)

if __name__=='__main__':unittest.main()
