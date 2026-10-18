"""Adversarial assurance that a decodable WAL sample never becomes a restore GO."""
from pathlib import Path
import importlib.util
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('wal',ROOT/'audit_vm101_wal_checkpoint_readonly.py')
wal=importlib.util.module_from_spec(spec)
spec.loader.exec_module(wal)
CFG='name: vitrial-prod-db\nscsi0: local:101/vm-101-disk-0.qcow2,size=160G\n'
BASE={
 'control_stable':True,'postgres_stopped':True,
 'control_state':'in production', 'redo_lsn_exact':True,
 'checkpoint_lsn_exact':True,'redo_wal_file_exact':True,
 'timeline_one':True,'wal_segment_bytes':16*1024**2,'wal_segment_count':5,
 'wal_sequence_contiguous':True,'wal_segments_regular_16mib':True,
 'redo_segment_present':True,'wal_timeline_consistent':True,
 'unexpected_partial_segment':False,'redo_record_readable':True,
 'checkpoint_record_readable':True,'data_page_checksums_disabled':True,
 'wal_readable_prefix_records':5,
 'wal_prefix_boundary':'zero_length_record_boundary',
 'wal_prefix_boundary_lsn':'0/1B000060',
}

class WalAuditTests(unittest.TestCase):
    def test_positive_metadata_never_proves_restore(self):
        r=wal.assess(BASE,'one',CFG)
        self.assertTrue(r['technical_wal_metadata_pass'])
        self.assertEqual(r['data_page_checksums'],'disabled')
        self.assertEqual(r['wal_segment_count'],5)
        self.assertEqual(r['wal_readable_prefix_records'],5)
        self.assertEqual(r['wal_first_boundary'],'zero_length_record_boundary')
        self.assertFalse(r['wal_end_of_written_data_proven'])
        for key in ('whole_wal_stream_verified','full_replay_verified',
                    'tenant_data_verified','wal_archival_independently_proven',
                    'restore_authorized','production_authorized'):
            self.assertFalse(r[key])
        self.assertTrue(r['checkpoint_record_sampled_only'])
        self.assertEqual(r['gate'],'HOLD')
        self.assertEqual(r['executable_commands'],[])

    def test_missing_or_corrupt_wal_evidence_fails_closed(self):
        for key in wal.REQUIRED_TRUE:
            with self.subTest(key=key):
                r=wal.assess({**BASE,key:False},'one',CFG)
                self.assertFalse(r['technical_wal_metadata_pass'])
        for key in ('wal_segment_bytes','wal_segment_count'):
            with self.subTest(key=key):
                r=wal.assess({**BASE,key:True},'one',CFG)
                self.assertFalse(r['technical_wal_metadata_pass'])
        self.assertFalse(wal.assess({**BASE,'unexpected_partial_segment':True},'one',CFG)['technical_wal_metadata_pass'])

    def test_missing_fields_unexpected_types_and_unknown_state(self):
        for mutation in ('drop_key','extra_key','wrong_type','wrong_control','bad_count','missing_count'):
            with self.subTest(mutation=mutation):
                d=BASE.copy()
                if mutation=='drop_key':d.pop('redo_record_readable')
                if mutation=='extra_key':d['raw_customer_wal']='fake-private'
                if mutation=='wrong_type':d['wal_timeline_consistent']='yes'
                if mutation=='wrong_control':d['control_state']='shut down'
                if mutation=='bad_count':d['wal_segment_count']=0
                if mutation=='missing_count':d['wal_segment_count']=513
                r=wal.assess(d,'one',CFG)
                self.assertFalse(r['technical_wal_metadata_pass'])
                self.assertNotIn('fake-private',str(r))

    def test_unclassified_prefix_and_broken_boundary_fail_closed(self):
        variants=(
            ('wal_readable_prefix_records',0),
            ('wal_readable_prefix_records',True),
            ('wal_readable_prefix_records',-1),
            ('wal_readable_prefix_records',17),
            ('wal_readable_prefix_records','5'),
            ('wal_prefix_boundary','unclassified_failure'),
            ('wal_prefix_boundary','assumed_clean_shutdown'),
            ('wal_prefix_boundary_lsn','0/ZZZZZZ'),
            ('wal_prefix_boundary_lsn',None),
        )
        for key,val in variants:
            with self.subTest(key=key,value=val):
                res=wal.assess({**BASE,key:val},'one',CFG)
                self.assertFalse(res['technical_wal_metadata_pass'])
                self.assertEqual(res['gate'],'HOLD')
                self.assertFalse(res['wal_end_of_written_data_proven'])
        inconsistent={**BASE,'wal_prefix_boundary':'not_observed_within_16',
                      'wal_prefix_boundary_lsn':None}
        self.assertFalse(wal.assess(inconsistent,'one',CFG)['technical_wal_metadata_pass'])
        no_boundary={**BASE,'wal_prefix_boundary':'not_observed_within_16',
                     'wal_prefix_boundary_lsn':None,
                     'wal_readable_prefix_records':16}
        r=wal.assess(no_boundary,'one',CFG)
        self.assertTrue(r['technical_wal_metadata_pass'])
        self.assertFalse(r['full_replay_verified'])
        self.assertFalse(r['wal_end_of_written_data_proven'])
        self.assertEqual(r['wal_first_boundary'],'not_observed_within_16')

    def test_unexpected_machine_or_vm_does_not_pass(self):
        self.assertFalse(wal.assess(BASE,'other',CFG)['technical_wal_metadata_pass'])
        self.assertFalse(wal.assess(BASE,'one',CFG.replace('vitrial-prod-db','replacement'))['technical_wal_metadata_pass'])

    def test_checksummed_data_page_is_not_marked_disabled(self):
        r=wal.assess({**BASE,'data_page_checksums_disabled':False},'one',CFG)
        self.assertTrue(r['technical_wal_metadata_pass'])
        self.assertEqual(r['data_page_checksums'],'enabled_unverified')
        self.assertFalse(r['full_replay_verified'])

    def test_unavailable_remote_and_forbidden_mutations(self):
        with patch.object(wal,'remote',side_effect=RuntimeError('node unavailable')):
            with self.assertRaises(RuntimeError):wal.capture('root@test')
        s=(ROOT/'audit_vm101_wal_checkpoint_readonly.py').read_text()
        for phrase in ("'pg_ctlcluster','17','main','start'",'qmrestore',
                       'pg_resetwal','initdb','pg_rewind'):
            self.assertNotIn(phrase,s)

if __name__=='__main__':unittest.main()
