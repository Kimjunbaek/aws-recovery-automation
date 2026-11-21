import copy
import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
import contextlib
import setup
import web_worker

class Response:
    status=200
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,n):return b'<title>Home - Emperor_penguin</title>'

class Tests(unittest.TestCase):
    def baseline(self):
        return json.loads((Path(__file__).parent/'test-baseline.json').read_text())
    def test_only_expected_existing_changes(self):
        before=self.baseline();after=setup.patch_definition(before,'3')
        a=after['States']['RunProxyRecovery']['Branches'][0]['States'];b=before['States']['RunProxyRecovery']['Branches'][0]['States']
        permitted={'VerifyProxyWordPress','SwitchDefaultTarget','ReadListenerAfterCutover','VerifyListenerTarget'}
        for key in b:
            if key not in permitted:self.assertEqual(a[key],b[key],key)
        self.assertEqual(before,self.baseline())
        self.assertEqual(a['VerifyAlbServiceEvaluate']['Choices'][0]['Next'],'TagRecoveredActive')
        self.assertEqual(a['VerifyListenerTarget']['Choices'][0]['Next'],'VerifyAlbService')
    def test_idempotent(self):
        a=setup.patch_definition(self.baseline(),'3')
        self.assertEqual(a,setup.patch_definition(a,'3'))
    def test_graph(self):
        names=set()
        def walk(d):
            states=d['States'];seen=set();pending=[d['StartAt']]
            for n in states:
                self.assertNotIn(n,names);names.add(n)
            while pending:
                name=pending.pop();self.assertIn(name,states)
                if name in seen:continue
                seen.add(name);v=states[name]
                for key in ('Next','Default'):
                    if key in v:pending.append(v[key])
                for c in v.get('Choices',[])+v.get('Catch',[]):pending.append(c['Next'])
            self.assertEqual(seen,set(states))
            for v in states.values():
                for b in v.get('Branches',[]):walk(b)
        walk(setup.patch_definition(self.baseline(),'3'))
    def test_retry_requires_three_consecutive_successes(self):
        import unittest.mock
        opener=unittest.mock.Mock()
        fail=urllib.error.HTTPError('http://example',503,'temporary',{},None)
        opener.open.side_effect=[fail,Response(),Response(),fail,Response(),Response(),Response()]
        with patch('urllib.request.build_opener',return_value=opener),patch.object(web_worker.time,'sleep'),contextlib.redirect_stdout(io.StringIO()):
            web_worker.verify_alb_service()
        self.assertEqual(opener.open.call_count,7)
    def test_http_failure_bounded_and_not_success(self):
        import unittest.mock
        opener=unittest.mock.Mock();opener.open.side_effect=urllib.error.HTTPError('http://example',503,'temporary',{},None)
        with patch('urllib.request.build_opener',return_value=opener),patch.object(web_worker.time,'sleep'),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError,'three consecutive'):web_worker.verify_alb_service()
        self.assertEqual(opener.open.call_count,30)
    def test_title_mismatch_not_success(self):
        import unittest.mock
        response=Response();response.read=lambda n:b'Error establishing a database connection'
        opener=unittest.mock.Mock();opener.open.return_value=response
        with patch('urllib.request.build_opener',return_value=opener),patch.object(web_worker.time,'sleep'),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):web_worker.verify_alb_service()
    def test_web_does_not_require_direct_rds(self):
        self.assertNotIn('real_connect',web_worker.VERIFY_PHP)
        self.assertIn("DB_HOST !== '192.0.2.242:3306'",web_worker.VERIFY_PHP)
        self.assertIn('SELECT @@server_uuid',web_worker.VERIFY_PHP)
    def test_local_install_patch_preserves_original(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            home=Path(tmp);b=home/'team5-bastion-integration-package/team5-bastion-integration';b.mkdir(parents=True)
            p=home/'team5-proxy-recovery-package/team5-proxy-recovery';p.mkdir(parents=True)
            fixtures=Path(__file__).parent/'fixtures'
            (b/'run-response.py').write_text((fixtures/'run-response.py').read_text())
            (p/'proxy-recovery.py').write_text((fixtures/'proxy-recovery.py').read_text())
            originals={f:f.read_text() for f in (b/'run-response.py',p/'proxy-recovery.py')}
            changes=setup.local_changes(home)
            for f,original in originals.items():self.assertEqual(f.read_text(),original)
            for f,content in changes.items():f.write_text(content)
            self.assertEqual(changes,setup.local_changes(home))
            self.assertIn("DocumentVersion=doc.get('version', '1')",changes[p/'proxy-recovery.py'])
            self.assertLess(changes[b/'run-response.py'].index('run_preflight(session)'),changes[b/'run-response.py'].index('sf.start_execution('))

if __name__=='__main__':unittest.main()

