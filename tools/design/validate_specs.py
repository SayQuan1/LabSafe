"""Read-only design validation. Does not claim runtime, migration or model evaluation."""
from pathlib import Path
import copy,json,re,sys
from datetime import date, datetime, timezone
import argparse
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate_spec
from rule_reference import validate_bundle,evaluate_rule,tri_and,tri_or,select_rules
ROOT=Path(__file__).resolve().parents[2]
COUNT={}
def check(ok,msg):
    if not ok:raise AssertionError(msg)
def count(name,n=1):COUNT[name]=COUNT.get(name,0)+n
def load(p):
    text=(ROOT/p).read_text(encoding='utf-8');return yaml.safe_load(text) if p.endswith('.yaml') else json.loads(text)
def resolve(doc,ref):
    check(ref.startswith('#/'),'external ref '+ref);v=doc
    for part in ref[2:].split('/'):v=v[part.replace('~1','/').replace('~0','~')]
    return v
def walk(v,doc):
    if isinstance(v,dict):
        if '$ref'in v:resolve(doc,v['$ref']);count('resolved_refs')
        if v.get('type')=='array':check(v.get('items') not in (None,{}),'untyped array')
        if v.get('type')=='object':check(bool(v.get('properties')),'untyped object');check(v.get('additionalProperties') is False,'open object')
        for x in v.values():walk(x,doc)
    elif isinstance(v,list):
        for x in v:walk(x,doc)
def sample(s,doc):
    if '$ref'in s:return sample(resolve(doc,s['$ref']),doc)
    if 'const'in s:return s['const']
    if 'enum'in s:return s['enum'][0]
    if 'oneOf'in s:return sample(s['oneOf'][0],doc)
    if 'anyOf'in s:return sample(s['anyOf'][0],doc)
    typ=s.get('type')
    if typ=='object':return {k:sample(v,doc) for k,v in s['properties'].items() if k in s.get('required',[])}
    if typ=='array':return [sample(s['items'],doc) for _ in range(s.get('minItems',0))]
    if typ in ['integer','number']:return s.get('minimum',0)
    if typ=='boolean':return False
    if typ=='null':return None
    if typ=='string':
        if s.get('format')=='uuid':return '11111111-1111-4111-8111-111111111111'
        if s.get('format')=='date-time':return '2026-09-27T00:00:00.000Z'
        if s.get('format')=='date':return '2026-09-27'
        if s.get('format')=='uri':return 'http://ai-inference:8001/internal/inference/v1'
        if 'pattern'in s:
            m=re.search(r'\{(\d+)\}',s['pattern']);return 'a'*int(m.group(1)) if m else 'sample'
        return 'x'*max(s.get('minLength',1),1)
    raise AssertionError('unhandled schema '+str(s))
def validator(schema,root):
    whole=copy.deepcopy(schema)
    if 'components'in root:whole['components']=root['components']
    if '$defs'in root:whole['$defs']=root['$defs']
    return Draft202012Validator(whole,format_checker=FormatChecker())

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--report',action='store_true');args=parser.parse_args()
    for name in ['public-api-v1.yaml','inference-v1.yaml']:
        doc=load('contracts/'+name);validate_spec(doc);count('openapi_specs');walk(doc,doc)
        ids=[]
        for path,methods in doc['paths'].items():
            for method,op in methods.items():
                ids.append(op['operationId']);count('operations')
                pathparams=set(re.findall(r'\{(\w+)\}',path));declared={p['name'] for p in op.get('parameters',[]) if p['in']=='path'}
                check(pathparams==declared,'path parameter mismatch '+path)
                if name.startswith('public'):
                    check(bool(op.get('x-permission')),'missing permission')
                    if method!='get' and op['operationId']!='login':
                        headers={p['name'] for p in op['parameters'] if p['in']=='header'};check({'Idempotency-Key','X-CSRF-Token'}<=headers,'missing mutation headers')
        check(len(ids)==len(set(ids)),'duplicate operationId')
        for key,s in doc['components']['schemas'].items():
            Draft202012Validator.check_schema(s)
            instance=sample(s,doc);v=validator(s,doc);v.validate(instance);count('positive_schema_instances')
            if s.get('type')=='object':
                bad=copy.deepcopy(instance);bad['_unexpected']=1;check(not v.is_valid(bad),'extra field accepted '+key);count('negative_schema_instances')
                if s.get('required'):
                    bad=copy.deepcopy(instance);bad.pop(s['required'][0]);check(not v.is_valid(bad),'missing required accepted '+key);count('negative_schema_instances')
    for name in ['task-message-v1.json','events-v1.json','rule-dsl-v1.json','model-manifest-v1.json','inference-routes-v1.json','acceptance-policy-v1.json','model-evaluation-v1.json','release-approvals-v1.json']:
        doc=load('contracts/'+name);Draft202012Validator.check_schema(doc);walk(doc,doc);count('json_schemas')
        v=validator(doc,doc)
        for branch in doc.get('oneOf',[doc]):
            instance=sample(branch,doc);v.validate(instance);count('positive_message_instances')
            bad=copy.deepcopy(instance);bad['unexpected']=True;check(not v.is_valid(bad),'unknown property accepted');count('negative_message_instances')
    ai=load('contracts/inference-v1.yaml');s=ai['components']['schemas']['InferenceRequest'];v=validator(s,ai);request=sample(s,ai)
    for mutate in [lambda x:x.pop('pipeline_version'),lambda x:x.update(rule_version='old'),lambda x:x.update(fencing_token=0),lambda x:x.update(image_refs=[]),lambda x:x.update(deadline_at='not-a-date')]:
        bad=copy.deepcopy(request);mutate(bad);check(not v.is_valid(bad),'invalid AI request accepted');count('ai_negative_cases')
    msg=load('contracts/task-message-v1.json');v=validator(msg,msg);instance=sample(msg['oneOf'][1],msg)
    for key,value in [('schema_version','1.0'),('replay_generation',-1),('task_type','unknown')]:
        bad=copy.deepcopy(instance);bad[key]=value;check(not v.is_valid(bad),'invalid task accepted');count('queue_negative_cases')
    db=load('contracts/data-model.json')['tables'];ddl=(ROOT/'contracts/database-design.sql').read_text(encoding='utf-8')
    for name,t in db.items():
        check('CREATE TABLE '+chr(96)+name+chr(96) in ddl,'missing table DDL '+name);count('tables')
        cols=t['columns'];check(set(t['primary_key'])<=set(cols),'bad PK')
        for index in t['unique']+t['indexes']:check(set(index)<=set(cols),'bad index')
        for fk in t['foreign_keys']:
            dst=db[fk['target']];check(len(fk['columns'])==len(fk['references']),'FK arity')
            check(fk['references'] in [dst['primary_key']]+dst['unique'],'FK target not unique')
            for a,b in zip(fk['columns'],fk['references']):check(cols[a]['type']==dst['columns'][b]['type'],'FK type mismatch')
            if 'tenant_id'in dst['columns']:check('tenant_id'in fk['columns'] and 'tenant_id'in fk['references'],'unscoped FK')
            count('foreign_keys')
    check(['tenant_id','run_id','crop_id'] in db['image_derivatives']['unique'],'crop uniqueness')
    state_pairs=[('Inspection','inspections'),('InspectionItem','inspection_items'),('InferenceRun','inference_runs'),('Finding','findings'),('Remediation','remediation_tasks'),('Image','asset_images'),('RuleVersion','rule_versions'),('ModelVersion','model_versions')]
    public=load('contracts/public-api-v1.yaml')
    for schema,table in state_pairs:
        enum_values=set(public['components']['schemas'][schema]['properties']['status']['enum'])
        db_values=set(re.findall(r"'([^']+)'",db[table]['columns']['status']['type']))
        check(enum_values==db_values,'API/DB status mismatch: '+schema);count('state_enum_tieouts')
    for col in ['lease_owner','lease_until','fencing_token','replay_generation','dispatch_sequence']:check(col in db['task_runs']['columns'],'missing durability column')
    fixture=load('contracts/examples/synthetic-rule-cases.json');bundle=fixture['bundle'];validate_bundle(bundle);validator(load('contracts/rule-dsl-v1.json'),load('contracts/rule-dsl-v1.json')).validate(bundle)
    for case in fixture['cases']:
        rule=next(x for x in bundle['rules'] if x['rule_id']==case['rule_id']);check(evaluate_rule(rule,case['facts'],case['reference_date'])==case['expected'],case['id']);count('rule_cases')
    for a in [True,False,None]:
        for b in [True,False,None]:
            expected_and=False if a is False or b is False else (None if a is None or b is None else True)
            expected_or=True if a is True or b is True else (None if a is None or b is None else False)
            check(tri_and([a,b]) is expected_and and tri_or([a,b]) is expected_or,'truth table');count('truth_table_cases')
    bad=copy.deepcopy(bundle);bad['rules'][0]['clauses'][0][0]['op']='in'
    try:validate_bundle(bad)
    except ValueError:count('rule_semantic_negative_cases')
    else:raise AssertionError('invalid boolean op accepted')
    scoped=copy.deepcopy(bundle);override=copy.deepcopy(scoped['rules'][0]);override.update(scope='tenant',scope_id='11111111-1111-4111-8111-111111111111',enabled=False)
    scoped['rules'].append(override)
    selected=select_rules(scoped,override['scope_id'],'22222222-2222-4222-8222-222222222222','2026-09-27T00:00:00Z')
    check('SYNTHETIC-PAIR' not in [x['rule_id'] for x in selected],'disabled tenant override failed');count('rule_scope_cases')
    selected=select_rules(scoped,'33333333-3333-4333-8333-333333333333','22222222-2222-4222-8222-222222222222','2026-09-27T00:00:00Z')
    check('SYNTHETIC-PAIR' in [x['rule_id'] for x in selected],'tenant rule leaked');count('rule_scope_cases')
    files=[p for p in (ROOT/'docs').rglob('*.md') if '90-archive' not in p.parts]
    for path in files:
        text=path.read_text(encoding='utf-8')
        for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)',text):
            if '://' in target or target.startswith('#'):continue
            check((path.parent/target.split('#')[0]).exists(),f'broken link {path.relative_to(ROOT)} -> {target}');count('doc_links')
        for old in ['19-coding-baseline.md','18-ai-inference-process.md']:
            check(old not in text,'stale name '+str(path))
        for old in ['yolox-tiny4-decoded-v1','YOLOX-tiny','ai-cuda-py311-v1','ai-cpu-py311-v1','output[1,8400,9]']:
            check(old not in text,'stale detector design '+str(path)+': '+old)
    catalog=load('contracts/operation-catalog.json')['operations'];api=load('contracts/public-api-v1.yaml')
    mapped={(x['method'],x['path'],x['operation_id'],x['permission']) for x in catalog}
    actual={(m.upper(),'/api/v1'+p,o['operationId'],o['x-permission']) for p,methods in api['paths'].items() for m,o in methods.items()}
    check(mapped==actual,'operation catalog drift');count('public_operations',len(actual))
    test_text=(ROOT/'docs/07-quality-operations/01-testing-evaluation.md').read_text(encoding='utf-8')
    trace_text=(ROOT/'docs/08-delivery/02-traceability-matrix.md').read_text(encoding='utf-8')
    defined=set(re.findall(r'\| ([A-Z]+-\d+) \|',test_text));used=set(re.findall(r'\b[A-Z]+-\d+\b',trace_text))-{f'R-{i:02}' for i in range(1,11)}-{f'IRR-{i:02}' for i in range(1,7)}
    check(used<=defined,'undefined test IDs: '+str(used-defined));count('traceable_test_ids',len(used))
    policy=load('contracts/development-acceptance-policy.json')
    Draft202012Validator(load('contracts/acceptance-policy-v1.json'),format_checker=FormatChecker()).validate(policy)
    check(policy['status']=='proposed','development policy must not impersonate approval')
    from test_release_design import run_tests
    count('synthetic_release_gate_checks',run_tests())
    from test_dfine_design import run_tests as run_dfine_tests
    from dfine_reference import ADAPTER_ID,SOURCE_COMMIT
    count('synthetic_dfine_semantic_checks',run_dfine_tests())
    from test_readiness_design import run_tests as run_readiness_tests
    count('synthetic_readiness_checks',run_readiness_tests())
    model_doc=(ROOT/'docs/04-ai-rules/02-model-data-plan.md').read_text(encoding='utf-8')
    for token in [ADAPTER_ID,SOURCE_COMMIT,'dfine-cpu-fp32-ocrv4cpu-v1','dfine-cuda-fp32-ocrv4cpu-v1','dfine-qmax-v1','runtime_lock_sha256']:
        check(token in model_doc,'model contract not documented: '+token);count('dfine_doc_tieouts')
    report={'status':'PASS','executed_at_utc':datetime.now(timezone.utc).isoformat(),'python_version':sys.version.split()[0],'counts':COUNT,'limitations':['No application runtime tests','No live MySQL DDL execution','No real model or expert safety-rule evaluation','No GPU, ONNX export or real-image resize validation','No live MinIO IAM, nginx or presigned URL validation','No human approval']}
    rendered=json.dumps(report,ensure_ascii=False,indent=2)+'\n'
    if args.report:(ROOT/'docs/08-delivery/design-validation-results.json').write_text(rendered,encoding='utf-8')
    print(rendered)
if __name__=='__main__':main()
