"""Read-only candidate review using independent copies of retained native runs."""
from pathlib import Path
import copy,hashlib,json,shutil
from kicad_route import experiment,common
ROOT=Path(__file__).resolve().parent
ART=Path('/home/halbritt/git/kicad-route/build/krt91-builder-delivery-agent507/artifacts')
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
results=[]
for name in ['positive-current','positive-historical','negative-stale-runtime']:
 q=experiment.qualify(ART/name);results.append({'scenario':name,'status':q['status'],'reasons':q['reasons']})
print('baseline',results,flush=True)

def run_case(name,mutate,rebuild=False):
 dst=ROOT/name
 if dst.exists():shutil.rmtree(dst)
 shutil.copytree(ART/'positive-current',dst,symlinks=True)
 result=json.loads((dst/'result.json').read_text())
 mutate(dst,result)
 try:
  if rebuild:
   for rel in result['evidence']['records']:
    if (dst/rel).is_file():result['evidence']['records'][rel]=common.sha256(dst/rel)
   receipt=experiment.write(dst,result)
   common.write_json(dst/'result.json',result)
  q=experiment.qualify(dst,result)
  row={'scenario':name,'status':q['status'],'reasons':q['reasons']}
 except Exception as exc:row={'scenario':name,'exception':type(exc).__name__,'message':str(exc)}
 results.append(row);print(row,flush=True)

def edit(path,fn):
 doc=json.loads(path.read_text());fn(doc);path.write_text(json.dumps(doc))

def wrong_execution(dst,result):
 edit(dst/'work/effective-configuration.json',lambda d:d.update(execution={'outer_batches':2,'later_subruns':1}))
 result['process']['effective_configuration']['sha256']=common.sha256(dst/'work/effective-configuration.json')

def wrong_request(dst,result):
 edit(dst/'work/effective-configuration.json',lambda d:d.update(request_sha256='0'*64))
 result['process']['effective_configuration']['sha256']=common.sha256(dst/'work/effective-configuration.json')

def wrong_capture_status(dst,result):
 edit(dst/'work/effective-configuration.json',lambda d:d.update(capture_status='incomplete',issues=['later unknown']))
 result['process']['effective_configuration']['sha256']=common.sha256(dst/'work/effective-configuration.json')

def self_cycle(dst,result):
 edit(dst/'attempt.json',lambda d:d['source'].update(source_dir=str((dst/d['candidate_dir']).resolve())))

run_case('malformed-attempt',lambda d,r:(d/'attempt.json').write_text('[{"wrong":"shape"}]'))
run_case('malformed-candidate-facts',lambda d,r:(d/'validate/candidate-facts.json').write_text('[{"wrong":"shape"}]'))
run_case('malformed-effective-environment',lambda d,r:edit(d/'work/effective-configuration.json',lambda doc:doc.update(routing_environment=['wrong'])))
run_case('resealed-multiple-batches',wrong_execution,True)
run_case('resealed-wrong-request',wrong_request,True)
run_case('resealed-incomplete-capture',wrong_capture_status,True)
run_case('resealed-self-cycle',self_cycle,True)
(ROOT/'challenge-results.json').write_text(json.dumps({'candidate':'6e41a13d3543426c2fbf1e5e9c99c066038c432c','source_sha256':sha(experiment.__file__),'cases':results},indent=2)+'\n')
