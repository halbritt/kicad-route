"""Bounded evidence-index tests; no remote exports or native jobs."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('indexer',HERE/'index-archon-evidence.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class IndexTests(unittest.TestCase):
    def test_exact_external_identity_and_internal_raw_locator_preserved(self):
        with tempfile.TemporaryDirectory(dir=HERE) as folder:
            base=Path(folder).resolve();root=base/'evidence';root.mkdir();outside=base/'driver.py';outside.write_text('frozen')
            raw=root/'result.json';raw.write_text(json.dumps({'original':str(raw)}));host=root/'host.json';host.write_text('{}')
            entry=lambda p:{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
            protocol={'hash_prerequisites':[entry(outside)],'host_before_attestation':entry(host)}
            value=module.create_index(root,root/'index.json',protocol)
            files={r['source']:r for r in value['files']}
            self.assertIn(str(outside),files);self.assertEqual(files[str(raw)]['retained'],'result.json')
            self.assertEqual(json.loads(raw.read_text())['original'],str(raw))
            for row in value['files']:self.assertEqual(module.digest(root/row['retained']),row['sha256'])
            with self.assertRaises(ValueError):module.create_index(root,root/'index.json',protocol)

    def test_drift_and_pending_write_rejected_symlink_never_followed(self):
        with tempfile.TemporaryDirectory(dir=HERE) as folder:
            root=Path(folder).resolve();host=root/'host';host.write_text('x');protocol={'hash_prerequisites':[],'host_before_attestation':{'path':str(host),'sha256':'0'*64}}
            with self.assertRaises(ValueError):module.create_index(root,root/'index.json',protocol)
            protocol['host_before_attestation']['sha256']=module.digest(host)
            (root/'pending.writing').write_text('x')
            with self.assertRaises(ValueError):module.create_index(root,root/'index.json',protocol)
            (root/'pending.writing').unlink();(root/'alias').symlink_to('/nonexistent/outside')
            value=module.create_index(root,root/'index.json',protocol)
            self.assertEqual(len(value['symlinks']),1);self.assertNotIn(str(root/'alias'),[r['source'] for r in value['files']])

if __name__=='__main__':unittest.main()
