from pathlib import Path
import json,subprocess,gzip,hashlib,tarfile
repo=Path('/home/halbritt/git/fome-mc');ref=subprocess.check_output(['git','-C',str(repo),'rev-parse','7e6fb02'],text=True).strip()
base='telemetry-pod/expansion/routing-diagnostic/corrected-policy-iter7/'
out=Path('/home/halbritt/git/kicad-route/build/krt91-input-identity-agent507')
def blob(path):return subprocess.check_output(['git','-C',str(repo),'show',ref+':'+path])
def sha(data):return hashlib.sha256(data).hexdigest()
def parsed(path):return json.loads(blob(path))
receipt=parsed(base+'policy-receipt.json');source=gzip.decompress(blob(base+'source/ca2.kicad_pcb.gz'));corrected=blob(base+'ca2.kicad_pcb')
expected=receipt['source']['files_sha256'];raw=Path(receipt['source']['original_path'])
actual={n:sha((raw/n).read_bytes()) for n in expected};assert actual==expected
assert sha(source)==expected['ca2.kicad_pcb']
assert {n:sha(blob(base+n)) for n in receipt['output']['files_sha256']}==receipt['output']['files_sha256']
assert all(blob(base+n)==(raw/n).read_bytes() for n in ('ca2.kicad_pro','ca2.kicad_dru'))
sealed=[]
for line in blob(base+'SHA256SUMS').decode().splitlines():
 checksum,name=line.split(None,1);name=name.removeprefix('./');assert sha(blob(base+name))==checksum;sealed.append(name)
parent='telemetry-pod/expansion/routing-diagnostic/';parent_entries=[]
for line in blob(parent+'SHA256SUMS').decode().splitlines():
 checksum,name=line.split(None,1)
 if name.startswith('./iteration-7/'):
  assert sha(blob(parent+name[2:]))==checksum;parent_entries.append(name)
assert len(parent_entries)==30 and len(sealed)==16
snapshot_path=parent+'iteration-7/candidate/iter8d-campaign-r0/snapshot.json';snapshot=parsed(snapshot_path)
assert snapshot['files']==expected and snapshot['source_dir']==str(raw)
campaign=parsed(parent+'iteration-7/campaign.json');assert campaign['input']['dir']==str(raw)
planes=parsed(parent+'iteration-7/planes/result.json');origin=receipt['source']['origin']
assert sha(blob(origin['placement_board']['file']))==origin['placement_board']['gz_sha256']
placement=sha(gzip.decompress(blob(origin['placement_board']['file'])));assert placement==planes['input']['files']['ca2.kicad_pcb']==origin['placement_board']['sha256']
assert planes['candidate']==str(raw/'ca2.kicad_pcb')
after=planes['evidence']['after_drc'];assert after['board_sha256_graded']==after['board_sha256_checked']==expected['ca2.kicad_pcb']
assert after['refill']['board_sha256_before']==after['refill']['board_sha256_after']==expected['ca2.kicad_pcb']
for role,name in [('project','ca2.kicad_pro'),('rules','ca2.kicad_dru')]:
 assert sha(blob(origin[role]['file']))==origin[role]['sha256']==expected[name]==planes['input']['files'][name]
assert sha(blob(origin['plane_plan']['file']))==planes['input']['plan_sha256']==origin['plane_plan']['sha256']
prefix=0
while prefix<min(len(source),len(corrected)) and source[prefix]==corrected[prefix]:prefix+=1
suffix=0
while suffix<min(len(source)-prefix,len(corrected)-prefix) and source[-1-suffix]==corrected[-1-suffix]:suffix+=1
removed=source[prefix:len(source)-suffix];added=corrected[prefix:len(corrected)-suffix]
assert removed==b'' and added.count(b'\n')==62
report_root=Path('/home/halbritt/git/kicad-route/reports/routability-m1-20261021/krt98');index=json.loads((report_root/'evidence-index.json').read_text());archive=report_root/'evidence.tar.gz'
assert sha(archive.read_bytes())==index['archive_sha256'];lookup={v['path']:v for v in index['files']}
names=['independent-verification/summary.json','independent-verification/origin-production-terminals.json','independent-verification/production-ratchet-verification.json','independent-verification/independent-equivalence-expectations.json']
records={}
with tarfile.open(archive) as tar:
 for name in names:
  data=tar.extractfile(name).read();assert sha(data)==lookup[name]['sha256'];records[name]=json.loads(data)
summary=records[names[0]];assert summary['independent_acceptance_passed'] and summary['origin_reconciliation']['actual_origin_complete_nets_lost']==0
assert summary['partial_connected_groups_on_incomplete_origin_nets']==5 and all(summary['partial_groups_preserved'].values())
terminals=records[names[1]];assert terminals['identity']['board_sha256']==expected['ca2.kicad_pcb'] and terminals['identity']['rules']=={n:expected[n] for n in ('ca2.kicad_pro','ca2.kicad_dru')}
import pcbnew
board=pcbnew.LoadBoard(str(raw/'ca2.kicad_pcb'))
positions={}
for reference,number,want in [('U3','11',(77.5,29.1)),('J2','1',(86.96,28.0))]:
 fp=next(fp for fp in board.GetFootprints() if fp.GetReference()==reference)
 pad=next(p for p in fp.Pads() if p.GetNumber()==number);xy=(pad.GetPosition().x/1e6,pad.GetPosition().y/1e6)
 assert xy==want;positions[reference+'.'+number]={'position_mm':xy,'net':pad.GetNetname(),'uuid':pad.m_Uuid.AsString()}
board_areas=[z.m_Uuid.AsString() for z in board.Zones() if z.GetIsRuleArea()]
footprint_areas=[{'owner':fp.GetReference(),'uuid':z.m_Uuid.AsString()} for fp in board.GetFootprints() for z in fp.Zones() if z.GetIsRuleArea()]
assert len(board_areas)==0 and len(footprint_areas)==6
assert {n:sha((raw/n).read_bytes()) for n in expected}==actual
result={'schema':'kicad-route/diagnostic-input-identity-confirmation/1','candidate_fome_commit':ref,'candidate_policy_receipt_sha256':sha(blob(base+'policy-receipt.json')),'source_sha256':actual,'corrected_pcb_sha256':sha(corrected),'project_and_custom_rules_identical':True,'diagnostic_manifest_entries_verified':len(sealed),'iteration7_parent_manifest_entries_verified':len(parent_entries),'snapshot_sha256':sha(blob(snapshot_path)),'placement_input_sha256':placement,'planes_output_and_refill_identity_verified':True,'all_original_board_bytes_preserved':True,'only_change':{'removed_bytes':len(removed),'inserted_bytes':len(added),'inserted_newlines':62},'native_source_geometry':positions,'source_rule_area_inventory':{'board_owned':board_areas,'footprint_owned':footprint_areas},'sealed_native_evidence':{'repository_commit':'367578e4c303c02580d338af5ae829a39495ab57','directory':'reports/routability-m1-20261021/krt98','archive_sha256':index['archive_sha256'],'index_sha256':sha((report_root/'evidence-index.json').read_bytes()),'members':{name:lookup[name]['sha256'] for name in names}},'reconciled_historical_observations':summary['origin_reconciliation'],'verdict':'PASS for archived PCB/project/custom-rule identity and geometric capture only','holds':['Corrected-policy branch is unreviewed/unaccepted; no merge yet.449 owns receipt correction and renewed review/hashes.','Receipt must label capped-report inference superseded and preserve55/32 and51/30 as measurements; all53complete and5partial groups preserved.','Receipt source zero rule areas is board-owned only; six footprint-owned keepouts exist and remain byte-preserved.','New policy requires fresh native grading; schematic/parity and actual native/policy findings remain unresolved.91 release and target route prerequisites remain.'],'scope':'Read-only source/manifest/native-position inspection and verification of existing sealed terminal evidence. No project save/refill/DRC rerun, routing, geometry/policy edit, branch integration or adoption.'}
from kicad_route import native_components,statemanifest
result['native_tools']={'pcbnew':native_components.native_identity(),'kicad_cli':statemanifest.kicad_cli_identity()}
assert result['native_tools']['pcbnew'] is not None
result['verification_script_sha256']=sha(Path(__file__).read_bytes())
result['corrected_policy_native_grade_status']='NOT_RUN; existing sealed partitions apply to historical source67782a5d, not a grade of correctedfe533602'
(out/'identity.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:result[k] for k in ('candidate_fome_commit','source_sha256','corrected_pcb_sha256','diagnostic_manifest_entries_verified','iteration7_parent_manifest_entries_verified','verdict')},indent=2))
