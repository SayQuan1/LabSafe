"""Synthetic design gate tests; these are NOT real model evaluation results."""
import copy,json
from pathlib import Path
from jsonschema import Draft202012Validator,FormatChecker
from release_reference import digest,content_digest,validate_manifest,assess_report
from dfine_reference import SOURCE_COMMIT
ROOT=Path(__file__).resolve().parents[2]
TENANT='11111111-1111-4111-8111-111111111111'
def read(name):return json.loads((ROOT/'contracts'/name).read_text(encoding='utf-8'))
def schema_check(name,value):Draft202012Validator(read(name),format_checker=FormatChecker()).validate(value)
def fixture():
    policy=read('development-acceptance-policy.json');policy.update(policy_id='SYNTHETIC-APPROVED-FOR-UNIT-TEST',status='approved')
    manifest={'bundle_id':'22222222-2222-4222-8222-222222222222','version_label':'synthetic-unit-test','pipeline_version':'vision-v1','dictionary_version_id':'33333333-3333-4333-8333-333333333333','dictionary_sha256':'a'*64,'git_commit':'a'*40,'dataset_version':'SYNTHETIC-NO-REAL-DATA','device_profiles':['cuda'],'artifacts':[{'role':role,'object_key':'fixtures/'+role+'.json','sha256':digest({'synthetic':role}),'license':'SYNTHETIC TEST ONLY','runtime':'fixture'} for role in ['detector','ocr','quality','dictionary']],'thresholds':{'detection_min':.25,'ocr_min':.6,'entity_min':.9,'blur_min':80,'dark_min':.12,'glare_max':.3,'adjacent_gap_ratio':.25},'input_max_side':640,'detector_backend':'onnxruntime','ocr_backend':'paddleocr','evaluation_report_key':'fixtures/synthetic-evaluation.json','evaluation_passed':True,'purpose':'production','adapter_id':'dfine-n4-rgb-stretch-v1','model_family':'dfine_n','source_ref':'https://github.com/Peterande/D-FINE','source_commit':SOURCE_COMMIT,'runtime_profile':'dfine-cuda-fp32-ocrv4cpu-v1','calibration':{'status':'calibrated','validation_split_sha256':'b'*64,'report_sha256':'c'*64},'evaluation_policy_id':policy['policy_id'],'evaluation_policy_sha256':digest(policy)}
    manifest['runtime']={'detector_device':'cuda','ocr_device':'cpu','detector_precision':'fp32','preprocess_id':'dfine-rgb-stretch640-v1','postprocess_id':'dfine-qmax-v1','onnx_opset':16,'runtime_lock_sha256':digest({'synthetic':'runtime-lock'})}
    manifest['content_sha256']=content_digest(manifest)
    measurements=[]
    for metric in policy['metrics']:
        for group in metric['groups']:
            seconds=metric['unit']=='seconds';value=1 if metric['direction']=='ge' or seconds else 0
            measurements.append({'metric_id':metric['metric_id'],'group':group,'numerator':None if seconds else value*1000,'denominator':200 if seconds else 1000,'scene_count':50,'positive_count':1000,'negative_count':1000,'value':value,'status':'pass'})
    report={'bundle_id':manifest['bundle_id'],'content_sha256':manifest['content_sha256'],'policy_id':policy['policy_id'],'policy_sha256':digest(policy),'dataset_split_sha256':'d'*64,'evaluator_commit':'e'*40,'environment_ref':'SYNTHETIC-UNIT-TEST-NOT-HARDWARE','completed_at':'2026-09-27T00:00:00Z','measurements':measurements,'latency_samples_ms':[1000]*200,'technical_failure_count':0}
    manifest['evaluation_report_sha256']=digest(report)
    registry={'schema_version':'1.0','entries':[{'tenant_id':TENANT,'bundle_id':manifest['bundle_id'],'manifest_sha256':digest(manifest),'policy_id':policy['policy_id'],'policy_sha256':digest(policy),'report_sha256':digest(report),'expert_approver':'SYNTHETIC-EXPERT','business_approver':'SYNTHETIC-BUSINESS','approved_at':'2026-09-27T00:00:00Z','evidence_ref':'SYNTHETIC UNIT TEST; NOT ACTUAL APPROVAL'}]}
    return manifest,policy,report,registry

def run_tests():
    count=0
    def rejected(call):
        nonlocal count
        try:call()
        except ValueError:count+=1
        else:raise AssertionError('invalid release input accepted')
    manifest,policy,report,registry=fixture()
    for file,value in [('model-manifest-v1.json',manifest),('acceptance-policy-v1.json',policy),('model-evaluation-v1.json',report),('release-approvals-v1.json',registry)]:schema_check(file,value);count+=1
    assert validate_manifest(manifest,'production',TENANT,policy,report,registry);count+=1
    dev=copy.deepcopy(manifest);dev['purpose']='development';dev['calibration']={'status':'uncalibrated','validation_split_sha256':None,'report_sha256':None}
    assert validate_manifest(dev,'dev');count+=1
    rejected(lambda:validate_manifest(dev,'production',TENANT,policy,report,registry))
    mock=copy.deepcopy(dev);mock.update(adapter_id='fixture-v1',model_family='mock_fixture',runtime_profile='fixture-v1',detector_backend='mock',ocr_backend='mock')
    mock['runtime'].update(detector_device='mock',ocr_device='mock',detector_precision='mock',preprocess_id='fixture-v1',postprocess_id='fixture-v1',onnx_opset=0)
    mock['content_sha256']=content_digest(mock)
    schema_check('model-manifest-v1.json',mock);count+=1
    assert validate_manifest(mock,'test');count+=1
    rejected(lambda:validate_manifest(mock,'production',TENANT,policy,report,registry))
    for field,value in [('content_sha256','0'*64),('adapter_id','unregistered'),('input_max_side',512),('runtime_profile','dfine-cpu-fp32-ocrv4cpu-v1'),('adapter_id','yolox-tiny4-decoded-v1'),('model_family','yolox_tiny'),('source_commit','b'*40)]:
        bad=copy.deepcopy(manifest);bad[field]=value
        if field!='content_sha256':bad['content_sha256']=content_digest(bad)
        rejected(lambda:validate_manifest(bad,'dev'))
    cpu=copy.deepcopy(dev);cpu['device_profiles']=['cpu'];cpu['runtime_profile']='dfine-cpu-fp32-ocrv4cpu-v1';cpu['runtime']['detector_device']='cpu';cpu['content_sha256']=content_digest(cpu)
    schema_check('model-manifest-v1.json',cpu);assert validate_manifest(cpu,'dev');count+=2
    for key,value in [('detector_device','cpu'),('ocr_device','cuda'),('detector_precision','fp16'),('preprocess_id','letterbox'),('postprocess_id','nms'),('onnx_opset',17),('runtime_lock_sha256','not-a-hash')]:
        bad=copy.deepcopy(manifest);bad['runtime'][key]=value;bad['content_sha256']=content_digest(bad)
        rejected(lambda:validate_manifest(bad,'dev'))
    for change in [lambda x:x['runtime'].update(runtime_lock_sha256='f'*64),lambda x:x.update(git_commit='f'*40)]:
        bad=copy.deepcopy(manifest);change(bad);bad['content_sha256']=content_digest(bad)
        rejected(lambda:validate_manifest(bad,'production',TENANT,policy,report,registry))
    bad=copy.deepcopy(manifest);bad['artifacts'][1]['role']='detector';bad['content_sha256']=content_digest(bad)
    rejected(lambda:validate_manifest(bad,'dev'))
    rejected(lambda:validate_manifest(manifest,'production',TENANT,policy,report,{'schema_version':'1.0','entries':[]}))
    reg=copy.deepcopy(registry);reg['entries'][0]['business_approver']=reg['entries'][0]['expert_approver']
    rejected(lambda:validate_manifest(manifest,'production',TENANT,policy,report,reg))
    proposed=copy.deepcopy(policy);proposed['status']='proposed'
    rejected(lambda:validate_manifest(manifest,'production',TENANT,proposed,report,registry))
    rejected(lambda:validate_manifest(manifest,'production','44444444-4444-4444-8444-444444444444',policy,report,registry))
    bad=copy.deepcopy(report);bad['measurements'].pop()
    rejected(lambda:assess_report(policy,bad))
    bad=copy.deepcopy(report);bad['measurements'][0]['value']=.2
    rejected(lambda:assess_report(policy,bad))
    bad=copy.deepcopy(report);bad['measurements'][-1]['value']=.2
    rejected(lambda:assess_report(policy,bad))
    bad=copy.deepcopy(report);bad['measurements'][0].update(denominator=0,numerator=None,value=None,status='inconclusive')
    assert not assess_report(policy,bad);count+=1
    bad=copy.deepcopy(report);bad['measurements'][0].update(scene_count=0,status='inconclusive')
    assert not assess_report(policy,bad);count+=1
    bad=copy.deepcopy(report);bad['technical_failure_count']=1;bad['measurements'][-1]['status']='fail'
    risk=next(row for row in bad['measurements'] if row['metric_id']=='RISK-R');risk.update(negative_count=0,status='inconclusive')
    assert not assess_report(policy,bad);count+=1
    bad=copy.deepcopy(report);row=bad['measurements'][0];row.update(numerator=850,value=.85)
    assert assess_report(policy,bad);count+=1
    row.update(numerator=849,value=.849,status='fail');assert not assess_report(policy,bad);count+=1
    return count
if __name__=='__main__':print('Synthetic release design checks:',run_tests())
