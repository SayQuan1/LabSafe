"""Declarative design source; generates specifications, not application code."""
from pathlib import Path
import argparse
import copy
import json
import re
import yaml
ROOT = Path(__file__).resolve().parents[2]
OUT, S, PATHS, OPS = {}, {}, {}, []
def text(n=200, lo=1): return {'type':'string','minLength':lo,'maxLength':n}
def enum(*v): return {'type':'string','enum':list(v)}
def integer(lo=0, hi=2147483647): return {'type':'integer','minimum':lo,'maximum':hi}
def arr(s, lo=0, hi=100): return {'type':'array','items':s,'minItems':lo,'maxItems':hi}
def obj(p, optional=()): return {'type':'object','properties':p,'required':[k for k in p if k not in optional],'additionalProperties':False}
def ref(n): return {'$ref':'#/components/schemas/'+n}
def nullable(s): return {'anyOf':[s,{'type':'null'}]}
def model(n,p,optional=()): S[n]=obj(p,optional); return ref(n)
ID={'type':'string','format':'uuid'}
DT={'type':'string','format':'date-time'}
SHA={'type':'string','pattern':'^[a-f0-9]{64}$'}
PROB={'type':'number','minimum':0,'maximum':1}
BOOL={'type':'boolean'}
BASE={'id':ID,'created_at':DT,'updated_at':DT,'version':integer(1)}
def resource(n,p): return model(n,{**BASE,**p})
def command(n,p=None): return model(n,{'expected_version':integer(1),**(p or {})})
ITEM=enum('draft','uploaded','queued','quality_checking','needs_retake','processing','needs_review','completed','failed')
RUN=enum('queued','processing','retrying','completed','needs_retake','needs_review','failed','superseded')
FINDING=enum('needs_review','confirmed','rejected','cannot_determine','dispatched','closed')
TASK=enum('pending_dispatch','in_progress','pending_recheck','rejected','cannot_remediate','closed')
JOB=enum('ready','leased','retry_wait','succeeded','failed','dead_letter')
SEVERITY=enum('low','medium','high','critical')
ROLES=enum('safety_admin','lab_manager','inspector','remediator','viewer','rule_expert')
ERRORS={
 'VALIDATION_ERROR':(422,False),'UNAUTHENTICATED':(401,False),'FORBIDDEN':(403,False),'NOT_FOUND':(404,False),
 'STATE_CONFLICT':(409,False),'VERSION_CONFLICT':(409,False),'IDEMPOTENCY_CONFLICT':(409,False),
 'REQUEST_IN_PROGRESS':(409,True),'RATE_LIMITED':(429,True),'IMAGE_TOO_LARGE':(413,False),
 'UNSUPPORTED_MEDIA_TYPE':(415,False),'IMAGE_INVALID':(422,False),'UNAUTHORIZED_REF':(403,False),
 'OBJECT_NOT_FOUND':(404,False),'HASH_MISMATCH':(422,False),'MODEL_NOT_READY':(503,True),
 'MODEL_VERSION_UNAVAILABLE':(409,False),'AI_BUSY':(429,True),'RUN_IN_PROGRESS':(409,True),
 'AI_TIMEOUT':(504,True),'STAGE_TIMEOUT':(504,True),'MODEL_OOM':(503,True),
 'DEPENDENCY_UNAVAILABLE':(503,True),'MODEL_ERROR':(500,False),'INTERNAL_ERROR':(500,False),
 'SCHEMA_MISMATCH':(502,False),'RULESET_INVALID':(422,False),'LEASE_LOST':(409,False)}
model('ErrorDetail',{'field':text(160),'reason':text(500)})
model('Error',{'request_id':ID,'error':obj({'code':enum(*ERRORS),'message':text(500),'retryable':BOOL,'details':arr(ref('ErrorDetail'),hi=30)})})
model('Ack',{'ok':BOOL})
model('Permission',{'action':text(80),'laboratory_id':nullable(ID)})
resource('User',{'username':text(128),'display_name':text(100),'status':enum('active','disabled')})
model('Session',{'user':ref('User'),'tenant_id':ID,'csrf_token':text(128),'expires_at':DT,'permissions':arr(ref('Permission'),hi=200)})
resource('College',{'name':text(),'code':text(64),'status':enum('active','archived')})
resource('Laboratory',{'college_id':ID,'name':text(),'code':text(64),'status':enum('active','archived')})
resource('Location',{'laboratory_id':ID,'parent_id':nullable(ID),'type':enum('room','area','shelf','cabinet'),'label':text(),'status':enum('active','archived')})
model('TemplateItem',{'id':ID,'code':text(64),'title':text(),'capture_hint':text(1000),'sort_order':integer(),'required':BOOL})
resource('Template',{'name':text(),'revision':integer(1),'status':enum('draft','published','retired'),'items':arr(ref('TemplateItem'),1,100)})
resource('Inspection',{'laboratory_id':ID,'template_id':ID,'inspector_id':ID,'status':enum('draft','in_progress','completed','cancelled'),'item_ids':arr(ID,1,100)})
resource('InspectionItem',{'inspection_id':ID,'laboratory_id':ID,'location_id':ID,'status':ITEM,'submission_revision':integer(),'current_run_id':nullable(ID),'current_fact_revision_id':nullable(ID),'review_outcome':nullable(enum('no_issue','issues_confirmed','cannot_determine')),'allowed_actions':arr(text(80),hi=30)})
resource('Image',{'owner_type':enum('inspection_item','remediation_task'),'owner_id':ID,'laboratory_id':ID,'status':enum('validating','ready','rejected','deleted'),'original_sha256':SHA,'analysis_sha256':nullable(SHA),'width':nullable(integer(1,10000)),'height':nullable(integer(1,10000)),'mime_type':enum('image/jpeg','image/png','image/webp'),'captured_at':DT})
model('UploadGrant',{'upload_id':ID,'object_key':text(1024),'put_url':text(4096),'required_content_type':enum('image/jpeg','image/png','image/webp'),'expires_at':DT,'max_bytes':integer(1,15728640)})
model('DownloadGrant',{'url':text(4096),'expires_at':DT})
model('EvidenceRef',{'image_id':ID,'detection_id':nullable(ID),'crop_id':nullable(ID)})
model('EntityFact',{'detection_id':ID,'entity_id':nullable(ID),'resolution':enum('resolved','candidate','unknown'),'source':enum('model','human'),'evidence':arr(ref('EvidenceRef'),1,10)})
model('RelationFact',{'source_detection_id':ID,'target_detection_id':ID,'relation':enum('adjacent','not_adjacent','unknown'),'same_location':nullable(BOOL),'source':enum('model','human'),'evidence':arr(ref('EvidenceRef'),1,10)})
model('DateFact',{'detection_id':ID,'kind':enum('expiry','production','opened','unknown'),'value':nullable({'type':'string','format':'date'}),'source':enum('model','human'),'evidence':arr(ref('EvidenceRef'),1,10)})
resource('FactRevision',{'item_id':ID,'run_id':ID,'revision':integer(1),'entities':arr(ref('EntityFact'),hi=100),'relations':arr(ref('RelationFact'),hi=200),'dates':arr(ref('DateFact'),hi=100),'reason':text(2000)})
resource('Finding',{'item_id':ID,'run_id':ID,'fact_revision_id':ID,'rule_evaluation_id':ID,'rule_id':text(80),'type':text(80),'severity':SEVERITY,'status':FINDING,'explanation':text(4000),'evidence':arr(ref('EvidenceRef'),1,20),'superseded_at':nullable(DT),'confirmed_by':nullable(ID),'task_id':nullable(ID),'allowed_actions':arr(text(80),hi=20)})
resource('Remediation',{'finding_id':ID,'laboratory_id':ID,'assignee_id':ID,'status':TASK,'priority':SEVERITY,'due_at':DT,'description':text(4000),'is_overdue':BOOL,'latest_evidence_id':nullable(ID),'closed_at':nullable(DT),'allowed_actions':arr(text(80),hi=20)})
resource('Evidence',{'task_id':ID,'image_ids':arr(ID,1,10),'description':text(4000),'submitted_by':ID})
resource('Job',{'task_type':enum('validate_image','inference_pipeline','rule_evaluation','report_export','notification_create','overdue_scan','object_cleanup'),'resource_id':ID,'state':JOB,'attempt':integer(0,4),'replay_generation':integer(),'available_at':DT,'last_error_code':nullable(enum(*ERRORS))})
resource('InferenceRun',{'item_id':ID,'status':RUN,'stage':enum('queued','quality','facts','rules','done'),'submission_revision':integer(1),'input_image_ids':arr(ID,1,3),'model_bundle_id':ID,'dictionary_version_id':ID,'rule_bundle_id':ID,'pipeline_version':text(64),'result_hash':nullable(SHA),'job_id':ID,'error_code':nullable(enum(*ERRORS))})
resource('Notification',{'type':text(80),'resource_type':text(80),'resource_id':ID,'read_at':nullable(DT)})
model('AuditChange',{'field':text(100),'before':nullable(text(4000,0)),'after':nullable(text(4000,0))})
model('AuditEvent',{'id':ID,'actor_id':nullable(ID),'action':text(100),'resource_type':text(80),'resource_id':ID,'changes':arr(ref('AuditChange'),hi=100),'reason':text(2000,0),'request_id':ID,'created_at':DT})
model('ExportFilter',{'laboratory_ids':arr(ID,1,100),'from':DT,'to':DT,'severity':arr(SEVERITY,hi=4),'finding_status':arr(FINDING,hi=6)})
resource('ReportExport',{'format':enum('csv','pdf'),'status':enum('queued','running','ready','failed','expired'),'snapshot_at':DT,'expires_at':nullable(DT),'job_id':ID,'error_code':nullable(enum(*ERRORS))})
resource('RuleVersion',{'rule_set_id':ID,'revision':integer(1),'status':enum('draft','submitted','approved','published','retired'),'checksum':SHA,'approved_by':nullable(ID),'rules':arr(text(80),1,200)})
resource('ModelVersion',{'name':text(100),'bundle_version':text(64),'status':enum('registered','validated','published','retired'),'checksum':SHA,'supported_devices':arr(enum('cpu','cuda'),1,2),'dataset_version':text(100),'license':text(200)})
resource('ChemicalEntity',{'canonical_name':text(),'cas_number':nullable(text(32)),'hazard_class':text(80),'storage_class':text(80),'dictionary_version_id':ID})
model('Dashboard',{'open_findings':integer(),'high_risk_findings':integer(),'overdue_tasks':integer(),'pending_review_items':integer(),'as_of':DT})
resource('Activation',{'laboratory_id':ID,'model_bundle_id':ID,'dictionary_version_id':ID,'rule_bundle_id':ID,'pipeline_version':text(64),'device_profile':enum('cpu','cuda')})
model('LoginRequest',{'tenant_code':text(64),'username':text(128),'password':text(256)})
model('CollegeCreate',{'name':text(),'code':text(64)})
model('LaboratoryCreate',{'college_id':ID,'name':text(),'code':text(64)})
model('LocationCreate',{'parent_id':nullable(ID),'type':enum('room','area','shelf','cabinet'),'label':text()})
model('UserCreate',{'username':text(128),'display_name':text(100),'initial_password':text(256,12)})
command('RoleGrant',{'role':ROLES,'scope_kind':enum('tenant','laboratory'),'laboratory_id':nullable(ID)})
model('TemplateCreate',{'name':text(),'items':arr(ref('TemplateItem'),1,100)})
command('VersionCommand',{'reason':text(2000)})
model('InspectionCreate',{'laboratory_id':ID,'template_id':ID,'location_ids':arr(ID,1,100)})
model('UploadRequest',{'owner_type':enum('inspection_item','remediation_task'),'owner_id':ID,'filename':text(255),'mime_type':enum('image/jpeg','image/png','image/webp'),'size_bytes':integer(1,15728640),'sha256':SHA,'captured_at':DT})
model('UploadComplete',{'upload_id':ID,'sha256':SHA})
model('ImageSelection',{'image_id':ID,'role':enum('overview','detail'),'parent_image_id':nullable(ID)})
command('ItemSubmit',{'images':arr(ref('ImageSelection'),1,3)})
command('ItemComplete',{'outcome':enum('no_issue','issues_confirmed','cannot_determine'),'reason':text(2000)})
command('FactsEdit',{'entities':arr(ref('EntityFact'),hi=100),'relations':arr(ref('RelationFact'),hi=200),'dates':arr(ref('DateFact'),hi=100),'reason':text(2000)})
command('FindingDecision',{'reason':text(2000)})
command('FindingUncertain',{'reason_code':enum('blur','occluded','label_unreadable','rule_missing','insufficient_evidence','other'),'reason':text(2000)})
command('RemediationCreate',{'assignee_id':ID,'due_at':DT,'priority':SEVERITY,'description':text(4000)})
command('EvidenceSubmit',{'image_ids':arr(ID,1,10),'description':text(4000)})
command('Recheck',{'evidence_id':ID,'reason':text(2000)})
command('TaskAssignment',{'assignee_id':ID,'due_at':DT,'reason':text(2000)})
model('ExportCreate',{'format':enum('csv','pdf'),'filters':ref('ExportFilter')})
command('Activate',{'model_bundle_id':ID,'dictionary_version_id':ID,'rule_bundle_id':ID,'pipeline_version':text(64),'device_profile':enum('cpu','cuda'),'reason':text(2000)})
resource('RuleSet',{'name':text(),'description':text(2000)})
model('RuleSetCreate',{'name':text(),'description':text(2000)})
resource('RuleSnapshot',{'checksum':SHA,'rule_version_ids':arr(ID,1,100),'evaluator_version':enum('rules-dnf-v1')})
model('RuleSnapshotCreate',{'rule_version_ids':arr(ID,1,100)})
model('DictionaryEntry',{'id':ID,'canonical_name':text(),'cas_number':nullable(text(32)),'hazard_class':text(80),'storage_class':text(80),'aliases':arr(text(),0,100)})
model('DictionaryImport',{'version_label':text(64),'entries':arr(ref('DictionaryEntry'),1,10000),'source':text(2000)})
resource('DictionaryVersion',{'version_label':text(64),'checksum':SHA,'status':enum('draft','published','retired'),'entry_count':integer(1,10000)})
command('RoleRevoke',{'role':ROLES,'scope_kind':enum('tenant','laboratory'),'laboratory_id':nullable(ID)})
def parameter(name,where,schema,required=False):return {'name':name,'in':where,'required':required,'schema':schema}
def envelope(n):
    k=n+'Response'
    if k not in S:model(k,{'data':ref(n),'request_id':ID})
    return ref(k)
def paged(n):
    k=n+'Page'
    if k not in S:model(k,{'items':arr(ref(n)),'page':integer(1),'page_size':integer(1,100),'total':integer()})
    return k
def op(method,path,name,response,request=None,permission='read',status=200,list_of=False):
    params=[parameter(p,'path',ID,True) for p in re.findall(r'\{(\w+)\}',path)]
    if list_of:
        params += [parameter('page','query',{'type':'integer','minimum':1,'default':1}),parameter('page_size','query',{'type':'integer','minimum':1,'maximum':100,'default':20}),parameter('laboratory_id','query',ID)]
        response=paged(response)
    if method!='get' and name!='login':
        params += [parameter('Idempotency-Key','header',text(128,8),True),parameter('X-CSRF-Token','header',text(128),True)]
    o={'operationId':name,'summary':name,'x-permission':permission,'parameters':params,'responses':{str(status):{'description':'成功；具体语义见命令目录','content':{'application/json':{'schema':envelope(response)}}},**{str(s):{'description':'稳定错误；不得按 message 判断','content':{'application/json':{'schema':ref('Error')}}} for s in [401,403,404,409,413,415,422,429,500,503]}}}
    if name=='login':o['security']=[]
    if request:o['requestBody']={'required':True,'content':{'application/json':{'schema':ref(request)}}}
    PATHS.setdefault(path,{})[method]=o
    OPS.append({'operation_id':name,'method':method.upper(),'path':'/api/v1'+path,'permission':permission,'request':request,'response':response})
op('post','/auth/login','login','Session','LoginRequest','anonymous')
op('post','/auth/logout','logout','Ack',permission='authenticated')
op('get','/me','getMe','Session',permission='authenticated')
op('get','/dashboard','getDashboard','Dashboard')
for noun,res,create,perm in [('colleges','College','CollegeCreate','admin'),('laboratories','Laboratory','LaboratoryCreate','admin'),('users','User','UserCreate','admin'),('templates','Template','TemplateCreate','admin'),('inspections','Inspection','InspectionCreate','capture')]:
    op('get','/'+noun,'list'+noun.title(),res,list_of=True)
    op('post','/'+noun,'create'+res,res,create,perm,201)
    op('get','/'+noun+'/{id}','get'+res,res)
op('get','/laboratories/{id}/locations','listLocations','Location',list_of=True)
op('post','/laboratories/{id}/locations','createLocation','Location','LocationCreate','admin',201)
op('post','/users/{id}/roles','grantRole','User','RoleGrant','admin')
op('post','/users/{id}/revoke-role','revokeRole','User','RoleRevoke','admin')
op('post','/users/{id}/disable','disableUser','User','VersionCommand','admin')
op('post','/templates/{id}/publish','publishTemplate','Template','VersionCommand','admin')
op('post','/templates/{id}/clone','cloneTemplate','Template','VersionCommand','admin',201)
for action in ['complete','cancel']:op('post','/inspections/{id}/'+action,action+'Inspection','Inspection','VersionCommand','capture')
op('get','/inspections/{id}/items','listInspectionItems','InspectionItem',list_of=True)
op('get','/inspection-items/{id}','getInspectionItem','InspectionItem')
op('post','/uploads','createUpload','UploadGrant','UploadRequest','capture_or_evidence',201)
op('post','/uploads/complete','completeUpload','Image','UploadComplete','capture_or_evidence',202)
op('get','/images/{id}','getImage','Image')
op('get','/images/{id}/download','downloadImage','DownloadGrant',permission='image_read')
op('post','/images/{id}/delete','deleteImage','Image','VersionCommand','admin')
for action,request,response in [('submit','ItemSubmit','InferenceRun'),('retry','VersionCommand','InferenceRun'),('facts','FactsEdit','Job'),('complete','ItemComplete','InspectionItem')]:
    op('post','/inspection-items/{id}/'+action,action+'InspectionItem',response,request,'review' if action in ['facts','complete','retry'] else 'capture',200 if action=='complete' else 202)
op('get','/inspection-items/{id}/inference','getCurrentInference','InferenceRun')
op('get','/inspection-items/{id}/facts','getCurrentFacts','FactRevision')
op('get','/inference-runs/{id}','getInferenceRun','InferenceRun')
op('get','/findings','listFindings','Finding',list_of=True)
op('get','/findings/{id}','getFinding','Finding')
for action,request in [('confirm','FindingDecision'),('reject','FindingDecision'),('cannot-determine','FindingUncertain')]:
    op('post','/findings/{id}/'+action,action.replace('-','')+'Finding','Finding',request,'review')
op('post','/findings/{id}/remediation-tasks','dispatchRemediation','Remediation','RemediationCreate','dispatch',201)
op('get','/remediation-tasks','listRemediations','Remediation',list_of=True)
op('get','/remediation-tasks/{id}','getRemediation','Remediation')
for action,request,permission,response in [('accept','VersionCommand','assignee','Remediation'),('submit-evidence','EvidenceSubmit','assignee','Evidence'),('recheck','Recheck','recheck','Remediation'),('reject','Recheck','recheck','Remediation'),('cannot-remediate','VersionCommand','admin','Remediation'),('reassign','TaskAssignment','dispatch','Remediation')]:
    op('post','/remediation-tasks/{id}/'+action,action.replace('-','')+'Remediation',response,request,permission)
op('get','/remediation-tasks/{id}/evidence','listEvidence','Evidence',list_of=True)
op('get','/chemical-entities','searchChemicals','ChemicalEntity',list_of=True)
op('post','/dictionaries','importDictionary','DictionaryVersion','DictionaryImport','admin',201)
op('get','/dictionaries','listDictionaries','DictionaryVersion',list_of=True)
op('get','/dictionaries/{id}','getDictionary','DictionaryVersion')
op('post','/dictionaries/{id}/publish','publishDictionary','DictionaryVersion','VersionCommand','admin')
op('post','/rule-sets','createRuleSet','RuleSet','RuleSetCreate','admin',201)
op('get','/rule-sets','listRuleSets','RuleSet',list_of=True)
op('get','/rule-bundles','listRuleBundles','RuleSnapshot',list_of=True)
op('post','/rule-bundles','createRuleBundle','RuleSnapshot','RuleSnapshotCreate','admin',201)
op('get','/rule-versions','listRuleVersions','RuleVersion',list_of=True)
op('get','/rule-versions/{id}','getRuleVersion','RuleVersion')
op('get','/model-versions','listModelVersions','ModelVersion',list_of=True)
op('get','/model-versions/{id}','getModelVersion','ModelVersion')
for action,permission in [('submit-approval','admin'),('approve','rule_approve'),('publish','admin'),('retire','admin')]:op('post','/rule-versions/{id}/'+action,action.replace('-','')+'RuleVersion','RuleVersion','VersionCommand',permission)
for action in ['validate','publish','retire']:op('post','/model-versions/{id}/'+action,action+'ModelVersion','ModelVersion','VersionCommand','admin')
op('get','/laboratories/{id}/activation','getActivation','Activation')
op('post','/laboratories/{id}/activation','setActivation','Activation','Activate','admin')
op('post','/reports/exports','createExport','ReportExport','ExportCreate','export',202)
op('get','/reports/exports/{id}','getExport','ReportExport')
op('get','/reports/exports/{id}/download','downloadExport','DownloadGrant',permission='export')
op('get','/audit-events','listAuditEvents','AuditEvent',permission='audit',list_of=True)
op('get','/notifications','listNotifications','Notification',list_of=True)
op('post','/notifications/{id}/read','readNotification','Notification','VersionCommand','recipient')
op('get','/jobs/{id}','getJob','Job')
op('get','/dead-letters','listDeadLetters','Job',permission='admin',list_of=True)
op('post','/dead-letters/{id}/replay','replayJob','Job','VersionCommand','admin',202)
def document(title,paths,schemas,base,security):
    return {'openapi':'3.1.0','info':{'title':title,'version':'1.1.0','description':'设计修订版，由 tools/design/build_specs.py 生成；未声明已部署。'},'servers':[{'url':base}],'security':[{security:[]}],'paths':paths,'components':{'securitySchemes':{security:({'type':'apiKey','in':'cookie','name':'labsafe_session'} if security=='cookieAuth' else {'type':'http','scheme':'bearer'})},'schemas':copy.deepcopy(schemas)}}
def standalone(n,schemas):
    root={'$schema':'https://json-schema.org/draft/2020-12/schema','$id':'https://labsafe.local/contracts/'+n+'.json','$ref':'#/$defs/'+n,'$defs':copy.deepcopy(schemas)}
    def visit(v):
        if isinstance(v,dict):
            if '$ref' in v:v['$ref']=v['$ref'].replace('#/components/schemas/','#/$defs/')
            for x in v.values():visit(x)
        elif isinstance(v,list):
            for x in v:visit(x)
    visit(root);return root
from extra_specs import extend
extend(globals())
OUT['contracts/public-api-v1.yaml']=document('LabSafe 公共 API',PATHS,S,'/api/v1','cookieAuth')
OUT['contracts/operation-catalog.json']={'version':'1.1.0','operations':OPS}
OUT['contracts/error-codes.json']={k:{'http_status':v[0],'retryable':v[1]} for k,v in ERRORS.items()}

# Package the exact inference contract for runtime validation, without runtime YAML/paths.
runtime_contract=copy.deepcopy(OUT['contracts/inference-v1.yaml'])
runtime_contract['x-error-codes']=copy.deepcopy(OUT['contracts/error-codes.json'])
OUT['packages/inference_protocol/contract.json']=runtime_contract
def serialize(path,value):
    return yaml.safe_dump(value,allow_unicode=True,sort_keys=False,width=110) if path.endswith('.yaml') else json.dumps(value,ensure_ascii=False,indent=2)+'\n'
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
    from data_model import build_database
    database,ddl=build_database();OUT['contracts/data-model.json']=database;OUT['contracts/database-design.sql']=ddl
    mismatches=[]
    for name,value in OUT.items():
        rendered=value if isinstance(value,str) else serialize(name,value)
        path=ROOT/name
        if args.check:
            if not path.exists() or path.read_text(encoding='utf-8')!=rendered:mismatches.append(name)
        else:
            path.parent.mkdir(parents=True,exist_ok=True);path.write_text(rendered,encoding='utf-8',newline='\n')
    if mismatches:raise SystemExit('Generated contract drift: '+', '.join(mismatches))
    print(f'{len(OUT)} design artifacts; {len(OPS)} public operations; {len(S)} schemas; {len(database["tables"])} tables')
if __name__=='__main__':main()
