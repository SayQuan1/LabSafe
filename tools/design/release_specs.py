"""Design schemas for model provenance, acceptance and offline release approval."""
def extend_release(g):
    globals().update(g)
    S['Session']['properties']['environment']=enum('dev','test','production');S['Session']['required'].append('environment')
    S['InferenceRun']['properties']['is_simulated']=BOOL;S['InferenceRun']['required'].append('is_simulated')
    version=OUT['contracts/inference-v1.yaml']['components']['schemas']['Version']
    version['properties'].update({'purpose':enum('development','evaluation','production'),'is_simulated':BOOL,'adapter_id':enum('dfine-n4-rgb-stretch-v1','fixture-v1'),'runtime_profile':enum('dfine-cpu-fp32-ocrv4cpu-v1','dfine-cuda-fp32-ocrv4cpu-v1','fixture-v1'),'runtime_lock_sha256':SHA,'detector_device':enum('cpu','cuda','mock'),'ocr_device':enum('cpu','mock')})
    version['required']+=['purpose','is_simulated','adapter_id','runtime_profile','runtime_lock_sha256','detector_device','ocr_device']
    model('Calibration',{'status':enum('uncalibrated','calibrated','failed'),'validation_split_sha256':nullable(SHA),'report_sha256':nullable(SHA)})
    model('InferenceRuntime',{'detector_device':enum('cpu','cuda','mock'),'ocr_device':enum('cpu','mock'),'detector_precision':enum('fp32','mock'),'preprocess_id':enum('dfine-rgb-stretch640-v1','fixture-v1'),'postprocess_id':enum('dfine-qmax-v1','fixture-v1'),'onnx_opset':{'type':'integer','enum':[16,0]},'runtime_lock_sha256':SHA})
    additions={'purpose':enum('development','evaluation','production'),'adapter_id':enum('dfine-n4-rgb-stretch-v1','fixture-v1'),'model_family':enum('dfine_n','mock_fixture'),'source_ref':text(200),'source_commit':{'type':'string','pattern':'^[a-f0-9]{40}$'},'runtime_profile':enum('dfine-cpu-fp32-ocrv4cpu-v1','dfine-cuda-fp32-ocrv4cpu-v1','fixture-v1'),'runtime':ref('InferenceRuntime'),'content_sha256':SHA,'calibration':ref('Calibration'),'evaluation_policy_id':nullable(text(80)),'evaluation_policy_sha256':nullable(SHA),'evaluation_report_sha256':nullable(SHA)}
    S['ModelManifest']['properties'].update(additions);S['ModelManifest']['required']+=list(additions)
    S['ModelManifest']['properties']['input_max_side']={'type':'integer','const':640}
    S['ModelManifest']['properties']['detector_backend']=enum('onnxruntime','mock')
    S['ModelManifest']['properties']['ocr_backend']=enum('paddleocr','mock')
    S['ModelManifest']['properties']['evaluation_report_key']=nullable(text(1024))
    metric_ids=['DET-P','DET-R','OCR-NAME','OCR-DATE','ENT-P','ENT-COVER','ENT-OOV','Q-FALSE-REJECT','Q-FALSE-ACCEPT','RISK-P','RISK-R','UNCERTAIN-ROUTING','LATENCY-CUDA']
    model('AcceptanceMetric',{'metric_id':enum(*metric_ids),'groups':arr(text(80),1,10),'unit':enum('ratio','seconds'),'direction':enum('ge','le'),'target':{'type':'number','minimum':0,'maximum':1000000},'min_denominator':integer(1),'min_scene_count':integer(),'min_positive_count':integer(),'min_negative_count':integer()})
    model('AcceptancePolicy',{'policy_id':text(80),'version':text(64),'status':enum('proposed','approved'),'profile':enum('cpu-functional','cuda-single-v1'),'metrics':arr(ref('AcceptanceMetric'),1,100),'description':text(2000)})
    model('MetricMeasurement',{'metric_id':enum(*metric_ids),'group':text(80),'numerator':nullable({'type':'number','minimum':0}),'denominator':integer(),'scene_count':integer(),'positive_count':integer(),'negative_count':integer(),'value':nullable({'type':'number','minimum':0}),'status':enum('pass','fail','inconclusive')})
    model('ModelEvaluation',{'bundle_id':ID,'content_sha256':SHA,'policy_id':text(80),'policy_sha256':SHA,'dataset_split_sha256':SHA,'evaluator_commit':{'type':'string','pattern':'^[a-f0-9]{40}$'},'environment_ref':text(1024),'completed_at':DT,'measurements':arr(ref('MetricMeasurement'),1,1000),'latency_samples_ms':arr(integer(),0,100000),'technical_failure_count':integer()})
    model('ReleaseApproval',{'tenant_id':ID,'bundle_id':ID,'manifest_sha256':SHA,'policy_id':text(80),'policy_sha256':SHA,'report_sha256':SHA,'expert_approver':text(100),'business_approver':text(100),'approved_at':DT,'evidence_ref':text(1024)})
    model('ReleaseApprovals',{'schema_version':{'type':'string','const':'1.0'},'entries':arr(ref('ReleaseApproval'),0,10000)})
    OUT['contracts/model-manifest-v1.json']=standalone('ModelManifest',{k:S[k] for k in ['ModelManifest','ModelArtifact','Thresholds','Calibration','InferenceRuntime']})
    OUT['contracts/acceptance-policy-v1.json']=standalone('AcceptancePolicy',{k:S[k] for k in ['AcceptancePolicy','AcceptanceMetric']})
    OUT['contracts/model-evaluation-v1.json']=standalone('ModelEvaluation',{k:S[k] for k in ['ModelEvaluation','MetricMeasurement']})
    OUT['contracts/release-approvals-v1.json']=standalone('ReleaseApprovals',{k:S[k] for k in ['ReleaseApprovals','ReleaseApproval']})
    metrics=[]
    def target(mid,value,direction='ge',minimum=100,scenes=30,groups=None,unit='ratio'):
        metrics.append({'metric_id':mid,'groups':groups or ['overall'],'unit':unit,'direction':direction,'target':value,'min_denominator':minimum,'min_scene_count':scenes,'min_positive_count':200 if mid.startswith('DET-') else (100 if mid.startswith('RISK-') else 0),'min_negative_count':100 if mid.startswith('RISK-') else 0})
    target('DET-P',.85,minimum=200,groups=['bottle','label','shelf','cabinet']);target('DET-R',.90,minimum=200,groups=['bottle','label','shelf','cabinet'])
    target('OCR-NAME',.90,minimum=200);target('OCR-DATE',.95)
    target('ENT-P',.98,minimum=200);target('ENT-COVER',.80,minimum=200);target('ENT-OOV',.99)
    target('Q-FALSE-REJECT',.10,'le');target('Q-FALSE-ACCEPT',.05,'le')
    target('RISK-P',.85,groups=['incompatible_storage','expired_label']);target('RISK-R',.95,groups=['incompatible_storage','expired_label'])
    target('UNCERTAIN-ROUTING',.99);target('LATENCY-CUDA',30,'le',200,0,['cuda-single-v1'],'seconds')
    OUT['contracts/development-acceptance-policy.json']={'policy_id':'LABSAFE-PILOT-DRAFT-01','version':'1.0','status':'proposed','profile':'cuda-single-v1','metrics':metrics,'description':'Explicit engineering development targets. Not achieved measurements, expert approval or permission for production deployment.'}
