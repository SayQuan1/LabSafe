"""AI, rule, model and queue schemas share the public type conventions."""
import copy
from dfine_reference import CLASSES as DFINE_CLASSES

def extend(g):
    globals().update(g)
    op('get','/inference-runs/{id}/crops/{crop_id}/download','downloadCrop','DownloadGrant',permission='image_read')
    resource('RoleAssignment',{'user_id':ID,'role':ROLES,'scope_kind':enum('tenant','laboratory'),'laboratory_id':nullable(ID)})
    op('get','/users/{id}/roles','listUserRoles','RoleAssignment',permission='admin',list_of=True)
    op('get','/dictionaries/{id}/entries','listDictionaryEntries','DictionaryEntry',list_of=True)
    PATHS['/chemical-entities']['get']['parameters'].append(parameter('dictionary_version_id','query',ID,True))
    PATHS['/findings']['get']['parameters'].append(parameter('current_only','query',{'type':'boolean','default':True}))
    for operation_id in ['listUsers','getUser']:
        for methods in PATHS.values():
            for operation in methods.values():
                if operation['operationId']==operation_id:operation['x-permission']='admin'
        for operation in OPS:
            if operation['operation_id']==operation_id:operation['permission']='admin'
    S['Activate']['properties']['pipeline_version']=enum('vision-v1')
    atom=obj({'field':enum('left.storage_class','right.storage_class','same_location','adjacent','expiry_date'),'op':enum('eq','in','before_reference_date'),'value':{'oneOf':[text(100),BOOL,arr(text(100),1,50)]}},optional=('value',))
    S['RuleAtom']=atom
    model('RuleDefinition',{'rule_id':text(80),'scope':enum('global','tenant','laboratory'),'scope_id':nullable(ID),'priority':integer(0,1000),'enabled':BOOL,'effective_from':DT,'effective_to':nullable(DT),'clauses':arr(arr(ref('RuleAtom'),1,10),1,10),'finding_type':enum('incompatible_storage','expired_label'),'severity':SEVERITY,'action_code':enum('review_storage','review_date'),'explanation_template':text(2000),'source':text(1000),'case_ids':arr(text(80),3,100)})
    model('RuleBundle',{'rule_set_id':ID,'version_label':text(64),'rules':arr(ref('RuleDefinition'),1,200),'evaluator_version':enum('rules-dnf-v1')})
    S['RuleVersion']['properties']['rules']=arr(ref('RuleDefinition'),1,200)
    model('RuleImport',{'bundle':ref('RuleBundle'),'reason':text(2000)})
    op('post','/rule-versions','importRuleVersion','RuleVersion','RuleImport','admin',201)
    model('ModelArtifact',{'role':enum('detector','ocr','quality','dictionary'),'object_key':text(1024),'sha256':SHA,'license':text(200),'runtime':text(100)})
    model('Thresholds',{'detection_min':PROB,'ocr_min':PROB,'entity_min':PROB,'blur_min':{'type':'number','minimum':0},'dark_min':PROB,'glare_max':PROB,'adjacent_gap_ratio':{'type':'number','minimum':0,'maximum':2}})
    model('ModelManifest',{'bundle_id':ID,'version_label':text(64),'pipeline_version':enum('vision-v1'),'dictionary_version_id':ID,'dictionary_sha256':SHA,'git_commit':{'type':'string','pattern':'^[a-f0-9]{40}$'},'dataset_version':text(100),'device_profiles':arr(enum('cpu','cuda'),1,2),'artifacts':arr(ref('ModelArtifact'),4,4),'thresholds':ref('Thresholds'),'input_max_side':integer(512,2048),'detector_backend':enum('onnxruntime'),'ocr_backend':enum('onnxruntime'),'evaluation_report_key':text(1024),'evaluation_passed':BOOL})
    model('ModelImport',{'manifest':ref('ModelManifest'),'reason':text(2000)})
    op('get','/model-versions/{id}/manifest','getModelManifest','ModelManifest',permission='admin')
    op('post','/model-versions','importModelVersion','ModelVersion','ModelImport','admin',201)
    # Query whitelists. Never accept arbitrary SQL or arbitrary field names.
    for path,method,name,schema in [('/findings','get','status',FINDING),('/findings','get','severity',SEVERITY),('/remediation-tasks','get','status',TASK),('/remediation-tasks','get','overdue',BOOL),('/chemical-entities','get','q',text(200)),('/audit-events','get','resource_id',ID),('/images/{id}/download','get','variant',enum('analysis','original'))]:
        PATHS[path][method]['parameters'].append(parameter(name,'query',schema))
    A={k:copy.deepcopy(S[k]) for k in ['Error','ErrorDetail']}
    def am(n,p,optional=()): A[n]=obj(p,optional);return ref(n)
    am('ImageRef',{'image_id':ID,'object_key':text(1024),'object_version':{'type':'string','minLength':1,'maxLength':200,'pattern':'^[!-~]+$','not':{'anyOf':[{'const':'null'},{'pattern':'[^!-~]'}]}},'sha256':SHA,'mime_type':enum('image/jpeg','image/png','image/webp'),'role':enum('overview','detail'),'location_id':ID,'parent_image_id':nullable(ID)})
    am('InferenceRequest',{'run_id':ID,'attempt_id':ID,'fencing_token':integer(1),'tenant_id':ID,'laboratory_id':ID,'item_id':ID,'submission_revision':integer(1),'model_bundle_id':ID,'model_checksum':SHA,'dictionary_version_id':ID,'dictionary_sha256':SHA,'pipeline_version':enum('vision-v1'),'device_profile':enum('cpu','cuda'),'deadline_at':DT,'request_hash':SHA,'image_refs':arr(ref('ImageRef'),1,3)})
    am('Point',{'x':PROB,'y':PROB})
    am('Quality',{'image_id':ID,'status':enum('pass','needs_retake'),'reasons':arr(enum('blur','dark','glare','occluded','unreadable'),hi=5),'blur_score':{'type':'number','minimum':0},'brightness':PROB,'glare_ratio':PROB})
    am('Detection',{'detection_id':ID,'image_id':ID,'parent_detection_id':nullable(ID),'class_id':{'type':'integer','minimum':0,'maximum':79},'type':enum(*DFINE_CLASSES),'bbox':arr(PROB,4,4),'confidence':PROB})
    am('CropEvidence',{'png_sha256':SHA,'png_size_bytes':integer(1,16*1024*1024),'source_rgb_sha256':SHA,'recognition_rotation_ccw':{'type':'integer','enum':[0,90]},'recognition_width':integer(1,2048),'recognition_height':integer(1,2048),'recognition_rgb_sha256':SHA})
    am('TextRegion',{'line_id':ID,'image_id':ID,'crop_id':ID,'detection_id':nullable(ID),'raw_text':text(2000,0),'confidence':PROB,'quad':arr(ref('Point'),4,4)})
    A['TextRegion']['properties']['detection_id']['description']='text-bottle-quad80-v1: same-image real COCO bottle (class 39), center inside and >=80% quad intersection area, unique smallest bbox; otherwise null. Worker recomputes. No synthetic label/container.'
    am('CropRecipe',{'crop_id':ID,'image_id':ID,'line_id':ID,'detection_id':nullable(ID),'quad':arr(ref('Point'),4,4),'output_width':integer(1,2048),'output_height':integer(1,2048),'transform_version':enum('perspective-rgb-v1'),'evidence':ref('CropEvidence')})
    A['CropRecipe']['properties']['detection_id']['description']='Same nullable bottle association as the paired TextRegion; it identifies the bottle, not an invented label detection.'
    am('OCRFieldSource',{'line_id':ID,'crop_id':ID,'raw_text':text(2000,0),'confidence':PROB})
    am('OCRField',{'image_id':ID,'detection_id':ID,'crop_id':ID,'source_lines':arr(ref('OCRFieldSource'),1,2),'field':enum('name','expiry','production','opened','date_unknown','concentration','hazard_mark'),'raw_text':text(2000,0),'normalized_text':nullable(text(500)),'confidence':PROB})
    A['OCRField']['description']='ocr-fields-v1: actual unique bottle, all 1-2 consecutive source lines with original text/confidence/crop. crop_id is first source only; raw_text joins sources with LF and confidence is min. Worker recomputes full ordered fields; no chemical/date facts.'
    am('Candidate',{'entity_id':ID,'canonical_name':text(),'confidence':PROB,'match_method':enum('cas_exact','alias_exact','fuzzy')})
    am('EntityCandidates',{'image_id':ID,'detection_id':ID,'candidates':arr(ref('Candidate'),0,5),'resolution':enum('resolved','candidate','unknown')})
    am('Relation',{'image_id':ID,'source_detection_id':ID,'target_detection_id':ID,'relation':enum('adjacent','not_adjacent','unknown'),'confidence':PROB})
    am('Timing',{'download_ms':integer(),'quality_ms':integer(),'detection_ms':integer(),'ocr_ms':integer(),'normalization_ms':integer(),'total_ms':integer()})
    am('InferenceResult',{'run_id':ID,'attempt_id':ID,'fencing_token':integer(1),'tenant_id':ID,'request_hash':SHA,'input_hashes':arr(obj({'image_id':ID,'sha256':SHA}),1,3),'model_bundle_id':ID,'model_checksum':SHA,'dictionary_version_id':ID,'dictionary_sha256':SHA,'pipeline_version':enum('vision-v1'),'outcome':enum('facts_ready','needs_retake','needs_review'),'quality':arr(ref('Quality'),1,3),'detections':arr(ref('Detection'),0,100),'text_regions':arr(ref('TextRegion'),0,100),'crops':arr(ref('CropRecipe'),0,100),'ocr_fields':arr(ref('OCRField'),0,500),'entities':arr(ref('EntityCandidates'),0,100),'relations':arr(ref('Relation'),0,200),'timing_ms':ref('Timing')})
    S['Point']=copy.deepcopy(A['Point'])
    S['TextRegion']=copy.deepcopy(A['TextRegion'])
    S['InferenceRun']['properties']['text_regions']=arr(ref('TextRegion'),0,100)
    S['InferenceRun']['required'].append('text_regions')
    am('Health',{'status':enum('alive','ready','not_ready'),'model_bundle_id':nullable(ID),'active_attempt_id':nullable(ID)})
    am('Version',{'service_commit':text(40),'pipeline_version':enum('vision-v1'),'model_bundle_id':ID,'dictionary_version_id':ID,'device_profile':enum('cpu','cuda')})
    A['Version']['properties'].update({'model_checksum':SHA,'dictionary_sha256':SHA})
    A['Version']['required']+=['model_checksum','dictionary_sha256']
    A['InferenceResult']['properties']['execution_identity']=ref('Version')
    am('ExtractionContext',{'bundle_json':text(65536),'dictionary_json':text(65536)})
    A['InferenceResult']['properties']['extraction_context']=ref('ExtractionContext')
    cpu_files=['detector_model','detector_config','detector_preprocessor','ocr_det_model','ocr_det_config','ocr_rec_model','ocr_rec_config','dictionary','runtime_lock']
    cpu_packages=['fastapi','pydantic','uvicorn','PyYAML','jsonschema','rfc3339-validator','onnxruntime','numpy','Pillow','opencv-python-headless','pyclipper']
    am('DevelopmentCPUBundle',{'purpose':enum('development'),'bundle_id':ID,'dictionary_version_id':ID,'pipeline_version':enum('vision-v1'),'device_profile':enum('cpu'),'adapter_id':enum('dfine-coco80-rgb-stretch-v1'),'runtime_profile':enum('dfine-cpu-fp32-ocrv6smallcpu-chemical-v2'),'artifacts':obj({k:obj({'path':text(2048),'sha256':SHA}) for k in cpu_files}),'thresholds':obj({'detection_min':PROB,'ocr_min':PROB,'entity_min':PROB,'blur_min':{'type':'number','minimum':0},'dark_min':PROB,'glare_max':PROB}),'threads':integer(1,16)})
    am('DevelopmentCPULock',{'purpose':enum('development'),'scope':enum('cpu-direct-runtime-v1'),'python':text(40),'platform':text(200),'packages':obj({k:text(64) for k in cpu_packages}),'crop_runtime_sha256':SHA,'code_sha256':SHA})
    am('DevelopmentDictionaryEntry',{'entity_id':ID,'canonical_name':text(200),'aliases':arr(text(200),0,10),'cas':nullable(text(12)),'source':text(1000)})
    am('DevelopmentDictionary',{'purpose':enum('development'),'dictionary_version_id':ID,'entries':arr(ref('DevelopmentDictionaryEntry'),0,100)})
    for name,filename in [('DevelopmentCPUBundle','development-cpu-bundle-v1.json'),('DevelopmentCPULock','development-cpu-lock-v1.json'),('DevelopmentDictionary','development-dictionary-v1.json')]:
        OUT['contracts/'+filename]=standalone(name,{name:A[name],**({'DevelopmentDictionaryEntry':A['DevelopmentDictionaryEntry']} if name=='DevelopmentDictionary' else {})})
    AP={}
    for path,method,req,res in [('/quality','post','InferenceRequest','InferenceResult'),('/runs','post','InferenceRequest','InferenceResult'),('/health','get',None,'Health'),('/ready','get',None,'Health'),('/version','get',None,'Version')]:
        operation={'operationId':'ai'+path[1:].title(),'responses':{'200':{'description':'结构化事实或健康状态','content':{'application/json':{'schema':ref(res)}}},**{str(v):{'description':'统一错误','content':{'application/json':{'schema':ref('Error')}}} for v in [401,403,404,409,413,415,422,429,500,503,504]}}}
        if req:operation['requestBody']={'required':True,'content':{'application/json':{'schema':ref(req)}}}
        AP[path]={method:operation}
    OUT['contracts/inference-v1.yaml']=document('LabSafe 独立 AI 事实 API',AP,A,'/internal/inference/v1','serviceToken')
    OUT['contracts/rule-dsl-v1.json']=standalone('RuleBundle',{k:S[k] for k in ['RuleBundle','RuleDefinition','RuleAtom']})
    OUT['contracts/model-manifest-v1.json']=standalone('ModelManifest',{k:S[k] for k in ['ModelManifest','ModelArtifact','Thresholds']})
    OUT['contracts/inference-routes-v1.json']={'$schema':'https://json-schema.org/draft/2020-12/schema',**obj({'routes':arr(obj({'tenant_id':ID,'model_bundle_id':ID,'dictionary_version_id':ID,'device_profile':enum('cpu','cuda'),'endpoint':{'type':'string','format':'uri','pattern':'^https?://[a-zA-Z0-9][a-zA-Z0-9.:-]*/internal/inference/v1$'},'token_file':text(1024)}),1,100)})}
    task_payloads={
     'validate_image':obj({'upload_id':ID,'image_id':ID}),
     'inference_pipeline':obj({'run_id':ID,'submission_revision':integer(1),'model_bundle_id':ID,'dictionary_version_id':ID,'rule_bundle_id':ID}),
     'rule_evaluation':obj({'run_id':ID,'fact_revision_id':ID,'rule_bundle_id':ID}),
     'report_export':obj({'export_id':ID}),
     'notification_create':obj({'event_id':ID,'recipient_id':ID}),
     'overdue_scan':obj({'cutoff_at':DT}),
     'object_cleanup':obj({'image_id':ID,'deletion_request_id':ID})}
    def tagged(kinds,event=False):
        branches=[]
        for k,p in kinds.items():
            common=({'event_id':ID,'event_type':{'const':k},'aggregate_type':text(80),'aggregate_id':ID,'occurred_at':DT,'aggregate_version':integer(1)} if event else {'task_id':ID,'task_type':{'const':k},'resource_id':ID,'replay_generation':integer(),'dispatch_sequence':integer(1),'created_at':DT})
            branches.append(obj({'schema_version':{'const':'1.1'},'tenant_id':ID,'trace_id':{'type':'string','pattern':'^[a-f0-9]{32}$'},**common,'payload':p}))
        return {'$schema':'https://json-schema.org/draft/2020-12/schema','oneOf':branches}
    OUT['contracts/task-message-v1.json']=tagged(task_payloads)
    events={
     'ImageValidated':obj({'image_id':ID,'owner_id':ID,'owner_type':enum('inspection_item','remediation_task'),'analysis_sha256':SHA}),
     'InferenceRequested':task_payloads['inference_pipeline'],
     'InferenceCompleted':obj({'run_id':ID,'item_id':ID,'fact_revision_id':ID,'result_hash':SHA}),
     'QualityCheckFailed':obj({'run_id':ID,'item_id':ID,'image_ids':arr(ID,1,3)}),
     'FactsRevised':task_payloads['rule_evaluation'],
     'RuleEvaluationCompleted':obj({'evaluation_id':ID,'fact_revision_id':ID,'finding_ids':arr(ID,0,200)}),
     'FindingConfirmed':obj({'finding_id':ID,'actor_id':ID}),
     'FindingRejected':obj({'finding_id':ID,'actor_id':ID}),
     'RemediationDispatched':obj({'task_id':ID,'assignee_id':ID,'finding_id':ID}),
     'EvidenceSubmitted':obj({'task_id':ID,'evidence_id':ID}),
     'RemediationClosed':obj({'task_id':ID,'finding_id':ID,'reviewer_id':ID}),
     'RuleVersionPublished':obj({'rule_version_id':ID,'checksum':SHA}),
     'ReportRequested':task_payloads['report_export'],
     'ObjectDeletionRequested':task_payloads['object_cleanup'],
     'RemediationOverdue':obj({'task_id':ID,'assignee_id':ID,'due_at':DT,'notification_date':{'type':'string','format':'date'}})}
    OUT['contracts/events-v1.json']=tagged(events,True)
    from release_specs import extend_release
    extend_release(g)
