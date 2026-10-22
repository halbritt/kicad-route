"""Independent old/new public-inspect behavior on identical retained manifests."""
from pathlib import Path
import hashlib,json,subprocess,types
from kicad_route import configuration_evidence as current
import test_configuration_evidence as fixtures
ROOT=Path(__file__).resolve().parent
SOURCE=Path(current.__file__).resolve()
head=subprocess.run(['git','-C',str(SOURCE.parents[2]),'rev-parse','HEAD'],capture_output=True,text=True,check=True).stdout.strip()
assert head=='c22581d17a782cf294c00461ade1a960fc5c48c2'
source_before=hashlib.sha256(SOURCE.read_bytes()).hexdigest()
oldcode=subprocess.run(['git','-C',str(SOURCE.parents[2]),'show','a5fa636dc49fbb3ff84dcb0c001195986a856ce8:src/kicad_route/configuration_evidence.py'],capture_output=True,text=True,check=True).stdout
old=types.ModuleType('kicad_route.configuration_evidence_old');old.__package__='kicad_route';exec(compile(oldcode,'<frozen-a5fa636-configuration-evidence>','exec'),old.__dict__)
fixture=ROOT/'fixture';fixture.mkdir(exist_ok=True)
request,manifest,output,_=fixtures.capture(fixture)
original=json.loads(manifest.read_text())
doc={**original,'issues':None,'execution':{'outer_batches':2,'later_subruns':0}};manifest.write_text(json.dumps(doc))
try:
 old.inspect(manifest,request,output)
 old_red='unexpectedly returned'
except TypeError as exc:old_red=type(exc).__name__+': '+str(exc)
assert old_red.startswith('TypeError:'),old_red
cases=[]
malformed=[('missing',None),('null',None),('int',7),('false',False),('string','not a list'),('object',{'reason':'not a list'}),('null-element',[None]),('nested-list',[[]]),('mixed-list',['prior',{'bad':True}])]
for name,issues in malformed:
 for outer,later in [(1,0),(2,0),(1,1),(0,0)]:
  doc={**original,'execution':{'outer_batches':outer,'later_subruns':later}}
  if name=='missing':doc.pop('issues')
  else:doc['issues']=issues
  manifest.write_text(json.dumps(doc));result=current.inspect(manifest,request,output)
  cases.append({'scenario':name,'execution':doc['execution'],'status':result['status'],'reasons':result['reasons'],'passed':result['status']=='mismatch' and 'invalid capture qualification' in result['reasons'] and all(isinstance(r,str) for r in result['reasons'])})
controls=[]
for name,capture_status,issues,execution,status in [
 ('complete-single','complete',[],{'outer_batches':1,'later_subruns':0},'bound'),
 ('complete-multiple','complete',[],{'outer_batches':2,'later_subruns':0},'unsupported'),
 ('complete-later','complete',[],{'outer_batches':1,'later_subruns':1},'unsupported'),
 ('incomplete-single','incomplete',['prior missing capture'],{'outer_batches':1,'later_subruns':0},'unsupported'),
 ('incomplete-multiple','incomplete',['prior missing capture'],{'outer_batches':2,'later_subruns':0},'unsupported'),
 ('complete-with-issue','complete',['prior missing capture'],{'outer_batches':1,'later_subruns':0},'mismatch')]:
 doc={**original,'capture_status':capture_status,'issues':issues,'execution':execution};manifest.write_text(json.dumps(doc));result=current.inspect(manifest,request,output)
 controls.append({'scenario':name,'status':result['status'],'reasons':result['reasons'],'passed':result['status']==status and (not issues or any('prior missing capture' in r or 'complete capture' in r for r in result['reasons']))})
report={'review_handle':'K91-R507-EI-ISSUES','candidate':head,'source_sha256_before':source_before,'source_sha256_after':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),'old_red':old_red,'malformed_cases':cases,'valid_controls':controls,'passed':all(c['passed'] for c in cases+controls)}
assert report['source_sha256_before']==report['source_sha256_after']
(ROOT/'result.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({'candidate':head,'old_red':old_red,'malformed_cases':len(cases),'valid_controls':len(controls),'passed':report['passed'],'source_sha256':source_before},indent=2));assert report['passed']
