"""Pure design references. No model loading, filesystem approval or business writes."""
import hashlib,json,math
from dfine_reference import SOURCE_COMMIT
CONTENT_FIELDS=['artifacts','pipeline_version','dictionary_version_id','dictionary_sha256','thresholds','input_max_side','adapter_id','model_family','source_ref','source_commit','git_commit','detector_backend','ocr_backend','device_profiles','runtime_profile','runtime']
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()
def content_digest(manifest):return digest({k:manifest[k] for k in CONTENT_FIELDS})
def require(condition,message):
    if not condition:raise ValueError(message)
def assess_report(policy,report):
    require(report['policy_id']==policy['policy_id'] and report['policy_sha256']==digest(policy),'policy mismatch')
    desired={(m['metric_id'],group):m for m in policy['metrics'] for group in m['groups']}
    for metric in policy['metrics']:
        require(metric['unit']!='ratio' or 0<=metric['target']<=1,'invalid ratio target')
    require(len(desired)==sum(len(m['groups']) for m in policy['metrics']),'duplicate policy metric/group')
    got={(m['metric_id'],m['group']):m for m in report['measurements']}
    require(len(got)==len(report['measurements']) and set(got)==set(desired),'missing/duplicate/extra measurement')
    decisions={}
    for key,metric in desired.items():
        row=got[key];n=row['denominator'];v=row['value']
        if n==0:require(v is None and row['numerator'] in [None,0],'zero denominator needs null value')
        if n==0 or n<metric['min_denominator'] or row['scene_count']<metric['min_scene_count'] or row['positive_count']<metric['min_positive_count'] or row['negative_count']<metric['min_negative_count']:
            status='inconclusive'
        elif metric['unit']=='ratio':
            require(row['numerator'] is not None and 0<=row['numerator']<=n,'invalid numerator')
            computed=row['numerator']/n
            require(v is not None and math.isfinite(v) and abs(v-computed)<=1e-9,'fabricated ratio')
            status='pass' if (v>=metric['target'] if metric['direction']=='ge' else v<=metric['target']) else 'fail'
        else:
            samples=report['latency_samples_ms'];require(n==len(samples),'latency count mismatch')
            computed=sorted(samples)[math.ceil(.95*n)-1]/1000
            require(v is not None and abs(v-computed)<=1e-9,'fabricated P95')
            status='pass' if computed<=metric['target'] and report['technical_failure_count']==0 else 'fail'
        require(row['status']==status,'incorrect metric status');decisions[key]=status
    return all(v=='pass' for v in decisions.values()) and report['technical_failure_count']==0

def validate_manifest(manifest,environment,tenant_id=None,policy=None,report=None,registry=None):
    require(environment in ['dev','test','production'],'unknown environment')
    roles=[x['role'] for x in manifest['artifacts']]
    require(len(roles)==4 and set(roles)=={'detector','ocr','quality','dictionary'},'artifact role mismatch')
    require(manifest['content_sha256']==content_digest(manifest),'content hash mismatch')
    require(manifest['input_max_side']==640,'unexpected detector shape')
    mock=manifest['adapter_id']=='fixture-v1'
    require(len(manifest['device_profiles']) in (1,2),'invalid device profiles')
    runtime=manifest['runtime']
    expected_runtime={'detector_device':'mock' if mock else manifest['device_profiles'][0],'ocr_device':'mock' if mock else 'cpu','detector_precision':'mock' if mock else 'fp32','preprocess_id':'fixture-v1' if mock else 'dfine-rgb-stretch640-v1','postprocess_id':'fixture-v1' if mock else 'dfine-qmax-v1','onnx_opset':0 if mock else 16}
    require(all(runtime.get(k)==v for k,v in expected_runtime.items()),'runtime contract mismatch')
    require(isinstance(runtime.get('runtime_lock_sha256'),str) and len(runtime['runtime_lock_sha256'])==64 and all(c in '0123456789abcdef' for c in runtime['runtime_lock_sha256']),'missing runtime lock hash')
    if mock:
        require(manifest['purpose']=='development' and manifest['model_family']=='mock_fixture' and manifest['runtime_profile']=='fixture-v1','invalid fixture identity')
        require(manifest['detector_backend']=='mock' and manifest['ocr_backend']=='mock','invalid fixture backends')
    else:
        require(manifest['adapter_id']=='dfine-n4-rgb-stretch-v1' and manifest['model_family']=='dfine_n','unsupported adapter')
        require(manifest['source_ref']=='https://github.com/Peterande/D-FINE' and manifest['source_commit']==SOURCE_COMMIT,'unsupported source revision')
        require(manifest['detector_backend']=='onnxruntime' and manifest['ocr_backend']=='paddleocr','unsupported backend')
        expected={'dfine-cpu-fp32-ocrv4cpu-v1':['cpu'],'dfine-cuda-fp32-ocrv4cpu-v1':['cuda']}
        require(manifest['runtime_profile'] in expected and manifest['device_profiles']==expected[manifest['runtime_profile']],'device/runtime mismatch')
    if environment!='production':return True
    require(not mock and manifest['purpose']=='production','development bundle cannot run in production')
    calibration=manifest['calibration'];require(calibration['status']=='calibrated' and calibration['validation_split_sha256'] and calibration['report_sha256'],'missing calibration')
    require(policy and report and registry and tenant_id,'missing trusted release inputs')
    require(policy['status']=='approved','proposed policy cannot authorize production')
    require(policy['profile']==('cuda-single-v1' if manifest['device_profiles']==['cuda'] else 'cpu-functional'),'evaluation hardware profile mismatch')
    require(manifest['evaluation_passed'] and manifest['evaluation_report_key'],'missing evaluation')
    require(manifest['evaluation_policy_id']==policy['policy_id'] and manifest['evaluation_policy_sha256']==digest(policy),'manifest policy mismatch')
    require(manifest['evaluation_report_sha256']==digest(report),'report hash mismatch')
    require(report['bundle_id']==manifest['bundle_id'] and report['content_sha256']==manifest['content_sha256'],'report bundle mismatch')
    require(report['dataset_split_sha256']!=calibration['validation_split_sha256'],'validation and test must differ')
    require(assess_report(policy,report),'evaluation not passed')
    matches=[e for e in registry['entries'] if e['tenant_id']==tenant_id and e['bundle_id']==manifest['bundle_id'] and e['manifest_sha256']==digest(manifest) and e['policy_id']==policy['policy_id'] and e['policy_sha256']==digest(policy) and e['report_sha256']==digest(report)]
    require(len(matches)==1,'missing or ambiguous trusted approval')
    require(matches[0]['expert_approver']!=matches[0]['business_approver'] and bool(matches[0]['evidence_ref']),'approval separation failed')
    return True
