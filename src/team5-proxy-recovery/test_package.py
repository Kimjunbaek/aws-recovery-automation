import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

if importlib.util.find_spec('fcntl') is None:
    sys.modules['fcntl'] = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *args: None)
import design
import proxy_worker
import web_worker

OLD = 'team5-rds-mysql.examplee1a0f.ap-northeast-2.rds.amazonaws.com'
NEW = 'team5-rds-restore-test.examplee1a0f.ap-northeast-2.rds.amazonaws.com'
OWNER = 'arn:aws:states:ap-northeast-2:123456789012:execution:team5-recovery-workflow:test-owner'


class FakeProxy:
    def __init__(self):
        row = dict(hostgroup_id='1', hostname=OLD, port='3306', status='ONLINE', weight='1',
                   compression='0', max_connections='1000', max_replication_lag='0', use_ssl='0', max_latency_ms='0')
        self.tables = {name: [copy.deepcopy(row)] for name in ('mysql_servers', 'runtime_mysql_servers', 'disk.mysql_servers')}
        self.write_count = 0
        self.fail_load_once = False

    def run(self, args, **kwargs):
        if args[0] == 'aws':
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({'Parameter': {'Value': json.dumps({'username': 'radmin', 'password': 'NOT_FOR_LOGS'})}}))
        query = args[-1]
        output = ''
        for statement in query.split(';'):
            statement = statement.strip()
            if not statement:
                continue
            if statement.startswith('SELECT'):
                match = re.search(r'FROM (\S+)', statement)
                rows = self.tables.get(match.group(1), [])
                if rows:
                    keys = list(rows[0])
                    output = '\t'.join(keys) + '\n' + '\n'.join('\t'.join(row[k] for k in keys) for row in rows) + '\n'
            elif statement.startswith('UPDATE'):
                self.write_count += 1
                host = re.search(r"hostname='([^']+)'", statement).group(1)
                self.tables['mysql_servers'][0]['hostname'] = host
                self.tables['mysql_servers'][0]['status'] = 'ONLINE'
            elif statement == 'LOAD MYSQL SERVERS TO RUNTIME':
                if self.fail_load_once:
                    self.fail_load_once = False
                    return types.SimpleNamespace(returncode=1, stdout='', stderr='secret SQL details')
                self.tables['runtime_mysql_servers'] = copy.deepcopy(self.tables['mysql_servers'])
            elif statement == 'SAVE MYSQL SERVERS TO DISK':
                self.tables['disk.mysql_servers'] = copy.deepcopy(self.tables['mysql_servers'])
            else:
                raise AssertionError(statement)
        return types.SimpleNamespace(returncode=0, stdout=output)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        test_root = design.HERE / '.test-tmp'
        test_root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(self.directory.cleanup)
        self.proxy = FakeProxy()

    def invoke(self, action, owner=OWNER, target=NEW):
        buffer = io.StringIO()
        with patch.object(proxy_worker, 'ROOT', Path(self.directory.name)), \
             patch.object(proxy_worker.os, 'geteuid', return_value=0, create=True), \
             patch.object(proxy_worker.shutil, 'which', return_value='/bin/tool'), \
             patch.object(proxy_worker.os, 'chmod'), \
             patch.object(proxy_worker.subprocess, 'run', side_effect=self.proxy.run), \
             patch.dict(os.environ, {'SSM_Action': action, 'SSM_Owner': owner, 'SSM_TargetHost': target}), \
             contextlib.redirect_stdout(buffer):
            proxy_worker.main()
        self.assertNotIn('NOT_FOR_LOGS', buffer.getvalue())
        return buffer.getvalue()

    def test_inspect_has_no_backend_writes(self):
        self.assertIn('PROXY_INSPECT_OK', self.invoke('inspect'))
        self.assertEqual(self.proxy.write_count, 0)

    def test_switch_restore_release_preserves_options(self):
        before = copy.deepcopy(self.proxy.tables['mysql_servers'])
        self.invoke('switch')
        self.assertEqual(self.proxy.tables['runtime_mysql_servers'][0]['hostname'], NEW)
        self.invoke('restore')
        self.assertEqual(self.proxy.tables['mysql_servers'], before)
        self.invoke('release')
        self.assertFalse((Path(self.directory.name) / 'active.json').exists())

    def test_other_execution_cannot_restore(self):
        self.invoke('switch')
        with self.assertRaisesRegex(RuntimeError, 'owned'):
            self.invoke('restore', owner=OWNER + '-other')
        self.assertEqual(self.proxy.tables['mysql_servers'][0]['hostname'], NEW)

    def test_changed_backend_is_not_overwritten(self):
        self.invoke('switch')
        self.proxy.tables['mysql_servers'][0]['hostname'] = 'other.example.com'
        with self.assertRaisesRegex(RuntimeError, 'another operation'):
            self.invoke('restore')

    def test_load_failure_compensates_and_retains_lock(self):
        self.proxy.fail_load_once = True
        with self.assertRaisesRegex(RuntimeError, 'SQL failed'):
            self.invoke('switch')
        self.assertEqual(self.proxy.tables['runtime_mysql_servers'][0]['hostname'], OLD)
        self.assertTrue((Path(self.directory.name) / 'active.json').exists())

    def test_release_requires_restored_database(self):
        self.invoke('switch')
        with self.assertRaisesRegex(RuntimeError, 'restored'):
            self.invoke('release')

    def test_inspect_blocks_new_execution_until_rollback(self):
        self.invoke('switch')
        with self.assertRaisesRegex(RuntimeError, 'still active'):
            self.invoke('inspect')

    def test_retry_switch_and_resume(self):
        self.invoke('switch')
        self.invoke('switch')
        self.invoke('restore')
        self.invoke('resume')
        self.assertEqual(self.proxy.tables['runtime_mysql_servers'][0]['hostname'], NEW)

    def test_drift_rejected_before_write(self):
        self.proxy.tables['disk.mysql_servers'][0]['weight'] = '2'
        with self.assertRaisesRegex(RuntimeError, 'differ'):
            self.invoke('switch')
        self.assertEqual(self.proxy.write_count, 0)

    def test_sql_injection_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'hostname'):
            self.invoke('switch', target="x';DELETE FROM mysql_servers;")
        self.assertEqual(self.proxy.write_count, 0)


class DesignTests(unittest.TestCase):
    def setUp(self):
        self.source = json.loads((design.HERE / 'reference-recovery.json').read_text())
        self.docs = design.documents()
        self.definition = design.build(self.source, self.docs)

    def test_all_graph_references_resolve_and_names_are_unique(self):
        all_names = set()
        def visit(graph):
            states = graph['States']
            self.assertIn(graph['StartAt'], states)
            for name, state in states.items():
                self.assertNotIn(name, all_names)
                all_names.add(name)
                refs = [state[k] for k in ('Next', 'Default') if k in state]
                refs += [item['Next'] for key in ('Choices', 'Catch') for item in state.get(key, [])]
                for ref in refs:
                    self.assertIn(ref, states)
                for branch in state.get('Branches', []):
                    visit(branch)
        visit(self.definition)

    def test_restoration_and_alb_tasks_preserved(self):
        branch = self.definition['States']['RunProxyRecovery']['Branches'][0]['States']
        for name in ('StartEc2Restore', 'StartRdsRestore', 'SwitchDefaultTarget', 'SavePreviousListener'):
            self.assertEqual(branch[name], self.source['States'][name])

    def test_every_state_is_reachable_in_each_graph(self):
        def visit(graph):
            states = graph['States']
            pending = [graph['StartAt']]
            reached = set()
            while pending:
                name = pending.pop()
                if name in reached:
                    continue
                reached.add(name)
                state = states[name]
                pending.extend(state[k] for k in ('Next', 'Default') if k in state)
                pending.extend(item['Next'] for key in ('Choices', 'Catch') for item in state.get(key, []))
            self.assertEqual(set(states), reached, 'Unreachable states: ' + str(set(states) - reached))
            for state in states.values():
                for branch in state.get('Branches', []):
                    visit(branch)
        visit(self.definition)

    def test_existing_document_is_not_reused(self):
        serialized = json.dumps(self.definition)
        self.assertNotIn('team5-configure-recovery-wordpress', serialized)
        self.assertIn(self.docs['proxy']['name'], serialized)
        self.assertIn(self.docs['web']['name'], serialized)

    def test_false_cutover_stops_before_restoration(self):
        states = self.definition['States']
        self.assertEqual(states['RequireProxyCutover']['Default'], 'SharedProxyRequiresCutover')
        self.assertEqual(states['SharedProxyRequiresCutover']['Type'], 'Fail')

    def test_env_rewrite_preserves_unrelated_settings(self):
        text = 'DB_USER=some_user\nDB_HOST=\nDB_PASSWORD=do-not-print\n'
        self.assertEqual(web_worker.env_text(text), text.replace('DB_HOST=', 'DB_HOST=192.0.2.242'))
        with self.assertRaises(RuntimeError):
            web_worker.env_text('DB_HOST=a\nDB_HOST=b\n')

    def test_document_size_and_interpolation(self):
        for item in self.docs.values():
            self.assertLess(len(json.dumps(item['content']).encode()), 64000)
            self.assertTrue(all(p['interpolationType'] == 'ENV_VAR' for p in item['content']['parameters'].values()))


if __name__ == '__main__':
    unittest.main()
