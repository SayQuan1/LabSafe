"""Synthetic IRR closure tests; no service, GPU, storage or live signatures."""
import copy
import json
from uuid import UUID
from jsonschema import Draft202012Validator, FormatChecker
from readiness_reference import (
    LIMITS, capacity, inspection_size, pair_count, claim_inference, recover_inference,
    completion_outcome, same_location, rule_relation, object_allowed, public_endpoint,
    resize_shape, gray, quality_resized, quality_reasons, parent_box, box_relation,
    extract_fields, parse_date, valid_cas, merge_dates, entity_consensus,
)


def run_tests():
    from build_specs import OUT
    checks = []

    def ok(condition, name):
        assert condition, name
        checks.append(name)

    def rejects(fn, message, name):
        try:
            fn()
        except ValueError as exc:
            ok(message in str(exc), name)
        else:
            raise AssertionError(name + ': unexpectedly accepted')

    # IRR-01: model a whole retry cycle, not just independent enum values.
    for initial in [('ready', 'queued', 'queued'), ('retry_wait', 'retrying', 'queued')]:
        ok(claim_inference(*initial) == ('leased', 'processing', 'quality_checking', 'quality'), 'claim '+str(initial))
    for failed_stage in ('quality_checking', 'processing'):
        recovered = recover_inference(1)
        ok(recovered == ('retry_wait', 'retrying', 'queued', 'queued'), 'recover '+failed_stage)
        ok(claim_inference(*recovered[:3])[-1] == 'quality', 'restart from quality '+failed_stage)
    ok(recover_inference(2, sweeper=True, expired=True)[:3] == ('retry_wait', 'retrying', 'queued'), 'kill reclaim')
    ok(recover_inference(4)[0] == 'dead_letter', 'four attempts exhausted')
    ok(recover_inference(1, retryable=False)[0] == 'failed', 'permanent failure')
    ok(recover_inference(1, current=False) == ('failed', None, None, None), 'never revive stale run')
    rejects(lambda: claim_inference('retry_wait', 'retrying', 'processing'), 'STATE_CONFLICT', 'old retry guard rejected')
    rejects(lambda: claim_inference('ready', 'queued', 'queued', current=False), 'LEASE_LOST', 'stale claim')
    rejects(lambda: claim_inference('ready', 'queued', 'queued', due=False), 'LEASE_LOST', 'early claim')
    rejects(lambda: recover_inference(1, owner_valid=False), 'LEASE_LOST', 'stale worker cannot fail task')
    rejects(lambda: recover_inference(1, expired=True), 'LEASE_LOST', 'expired worker cannot commit')
    rejects(lambda: recover_inference(1, sweeper=True), 'LEASE_LOST', 'sweeper cannot take live lease')

    # IRR-02: mutually exclusive completion, nullable but strictly typed tri-state.
    for statuses in ([], ['rejected'], ['confirmed'], ['dispatched'], ['closed'], ['cannot_determine']):
        for unknown in (False, True):
            expected = ('cannot_determine' if unknown or 'cannot_determine' in statuses else
                        'issues_confirmed' if set(statuses) & {'confirmed', 'dispatched', 'closed'} else 'no_issue')
            ok(completion_outcome(statuses, unknown) == expected, 'outcome '+str((statuses, unknown)))
    rejects(lambda: completion_outcome(['needs_review'], True), 'STATE_CONFLICT', 'review pending blocks completion')
    api = OUT['contracts/public-api-v1.yaml']
    location_schema = api['components']['schemas']['RelationFact']['properties']['same_location']
    location_validator = Draft202012Validator(location_schema)
    for value in (None, True, False):
        ok(location_validator.is_valid(json.loads(json.dumps(value))), 'nullable roundtrip '+str(value))
    for value in (0, 1, 'false', 'unknown'):
        ok(not location_validator.is_valid(value), 'strict location '+str(value))
    ok(same_location(None, 'shelf') is None, 'missing container unknown')
    ok(same_location('a', 'b') is False, 'different known containers false')
    ok(same_location('a', 'a') is True, 'same known container true')
    ok(rule_relation(None, 'unknown') == {'same_location': None, 'adjacent': None}, 'unknown rule context')
    rejects(lambda: rule_relation(None, 'not_adjacent'), 'VALIDATION_ERROR', 'unknown not coerced false')
    rejects(lambda: rule_relation(False, 'adjacent'), 'VALIDATION_ERROR', 'cross container cannot adjacent')

    # IRR-03: preserve wire limits and apply guards before result/event writes.
    for kind, maximum in LIMITS.items():
        ok(capacity(kind, maximum-1) == maximum-1, kind+' below')
        ok(capacity(kind, maximum) == maximum, kind+' boundary')
        rejects(lambda k=kind, n=maximum: capacity(k, n+1), 'capacity', kind+' overflow')
    ok(inspection_size(2, 50) == 100, '100 inspection items')
    rejects(lambda: inspection_size(2, 51), 'VALIDATION_ERROR', 'IRR 102-item counterexample closed')
    ok(pair_count(20) == 190, '20 bottles fit')
    rejects(lambda: pair_count(21), 'MODEL_ERROR', '21 bottles must not truncate')
    rejects(lambda: capacity('findings', 101*2), 'RULESET_INVALID', 'IRR 202-finding counterexample closed')
    schemas = api['components']['schemas']
    ties = [('inspection_items', schemas['Inspection']['properties']['item_ids']),
            ('relations', schemas['FactRevision']['properties']['relations']),
            ('dates', schemas['FactsEdit']['properties']['dates'])]
    ai = OUT['contracts/inference-v1.yaml']['components']['schemas']['InferenceResult']['properties']
    ties += [(k, ai[k]) for k in ('detections', 'relations', 'ocr_fields')]
    event = next(b for b in OUT['contracts/events-v1.json']['oneOf']
                 if b['properties']['event_type']['const'] == 'RuleEvaluationCompleted')
    ids_schema = event['properties']['payload']['properties']['finding_ids']
    ties.append(('findings', ids_schema))
    for kind, schema in ties:
        ok(schema['maxItems'] == LIMITS[kind], 'wire capacity '+kind)
    id_validator = Draft202012Validator(ids_schema, format_checker=FormatChecker())
    ids = [str(UUID(int=i+1)) for i in range(202)]
    ok(id_validator.is_valid(ids[:200]), 'event complete 200 IDs')
    ok(not id_validator.is_valid(ids), 'wire still rejects 202 IDs')

    # IRR-04/05: declared matrix and configuration checks, not real IAM/SigV4 tests.
    keys = {p: 'tenant/t/lab/l/'+folder+'/asset/v' for p, folder in
            [('O','original'),('A','analysis'),('D','derivatives'),('R','reports')]}
    keys['S'] = 'staging/t/upload'
    for role, action, prefix in [('api','PutObject','S'), ('general','GetObjectVersion','S'),
            ('general','PutObject','O'), ('general','PutObject','A'), ('general','GetObjectVersion','D'),
            ('general','PutObject','R'), ('inference','GetObjectVersion','A'), ('inference','PutObject','D'),
            ('ai','GetObjectVersion','A'), ('cleanup','DeleteObjectVersion','O')]:
        ok(object_allowed(role,action,keys[prefix],'t','l','fixed-v1'), 'object allow '+str((role,action,prefix)))
    for role, action, prefix in [('ai','PutObject','A'), ('ai','GetObject','O'),
            ('inference','PutObject','O'), ('api','DeleteObjectVersion','A'),
            ('general','DeleteObjectVersion','A'), ('cleanup','DeleteObject','A'), ('api','ListBucket','A')]:
        ok(not object_allowed(role,action,keys[prefix],'t','l','fixed-v1'), 'object deny '+str((role,action,prefix)))
    ok(not object_allowed('ai','GetObject',keys['A'],'another-tenant','l'), 'tenant prefix isolation')
    ok(not object_allowed('ai','GetObject',keys['A'],'t','another-lab'), 'lab prefix isolation')
    ok(not object_allowed('ai','GetObject',keys['A']+'/../original/x','t','l'), 'path traversal')
    ok(not object_allowed('cleanup','DeleteObjectVersion',keys['A'],'t','l'), 'delete requires version')
    origin = 'https://labsafe.example.org'
    ok(public_endpoint(origin, origin) == origin, 'same origin endpoint')
    for endpoint in ('http://minio:9000', origin+'/s3', 'https://other.example.org', origin+'?a=b', origin+'/'):
        rejects(lambda v=endpoint: public_endpoint(v,origin), 'CONFIG_ERROR', 'invalid endpoint '+endpoint)

    # IRR-06: exact scalar, geometry and OCR golden cases.
    ok(resize_shape(640,480) == (640,480), 'no image upscaling')
    ok(resize_shape(2048,1536) == (1024,768), 'quality resize landscape')
    ok(resize_shape(513,1025) == (512,1024), 'quality integer rounding portrait')
    ok(gray([255,0,0]) == 77 and gray([0,255,0]) == 149, 'fixed grayscale coefficients')
    black = quality_resized([[[0,0,0]]]); white = quality_resized([[[255,255,255]]])
    ok(black == {'blur_score':0,'brightness':0,'glare_ratio':0}, 'black singleton')
    ok(white == {'blur_score':0,'brightness':1,'glare_ratio':1}, 'white singleton')
    edge = quality_resized([[[0,0,0],[255,255,255]]])
    ok(edge == {'blur_score':260100,'brightness':.5,'glare_ratio':.5}, 'reflect101 population variance')
    ok(quality_reasons(edge,260100,.5,.5) == [], 'all quality threshold equalities pass')
    ok(quality_reasons(edge,260101,.6,.4) == ['blur','dark','glare'], 'quality reason order')
    box = [0,0,.5,.5]
    ok(parent_box(box,[('large',[0,0,1,1]),('small',box)]) == 'small', 'smallest parent')
    ok(parent_box(box,[('a',box),('b',box)]) is None, 'equal area parent ambiguous')
    ok(parent_box([0,0,1,1],[('a',[.25,.25,.75,.75])],label=True) is None, 'label 80 percent coverage')
    a,b = [0,0,.25,.5],[.25,.375,.5,.625]
    ok(box_relation(a,b,'s','s',0) == (True,'adjacent'), 'vertical ratio exactly half of smaller height')
    ok(box_relation(a,b,'s',None,0) == (None,'unknown'), 'unknown parent geometry')
    ok(box_relation(a,b,'s','t',0) == (False,'unknown'), 'known different containers')
    field = extract_fields([('NAME',.9),('Ethanol',.6)])[0]
    ok(field == {'field':'name','raw_text':'NAME\nEthanol','normalized_text':'Ethanol','confidence':.6}, 'two-line name min confidence')
    ok(extract_fields([('NAME:Eth',.8),('anol',.8)])[0]['normalized_text'] == 'Eth', 'no unrequested name joining')
    fields = extract_fields([('有效期',.9),('2027/03/01',.8)])
    ok(fields[0]['normalized_text'] == '2027-03-01', 'two-line date')
    fields = extract_fields([('有效期',.9),('生产日期:2026-03-01',.8)])
    ok([f['field'] for f in fields] == ['expiry','production'] and fields[0]['normalized_text'] is None, 'do not consume next keyword')
    ok(extract_fields([('生产日期:2026-03-01 有效期:2027-03-01',.9)])[0]['field'] == 'date_unknown', 'multiple date keywords ambiguous')
    for value in ('2027-03','2027-02-30','10/11/2027','2027-3-01'):
        ok(parse_date(value) is None, 'invalid/ambiguous date '+value)
    ok(parse_date('2028年02月29日') == '2028-02-29', 'valid leap date')
    ok(valid_cas('64-17-5') and not valid_cas('64-17-6'), 'CAS check digit')
    ok(extract_fields([('64-17-5',.9)])[0]['field'] == 'name', 'standalone CAS')
    ok(extract_fields([('unlabelled marketing text',.9)]) == [], 'do not fuzzy-match arbitrary lines')
    ok(extract_fields([('乙醇',.9)],aliases=['乙醇'])[0]['normalized_text'] == '乙醇', 'exact known alias')
    ok(extract_fields([('有效期',.9),('',.9),('2027-03-01',.9)])[0]['normalized_text'] is None, 'do not skip empty line')
    fields = extract_fields([('有效期:2027-03-01',.9),('EXP:2027-04-01',.9)])
    ok(merge_dates(fields,.6) == {'expiry':None}, 'conflicting dates retain unknown')
    ok(merge_dates(extract_fields([('EXP:2027-03-01',.5)]),.6) == {'expiry':None}, 'low OCR date unknown')
    ok(entity_consensus([(.9,[('a',.98)]),(.9,[('b',.99)])],.6,.9)[0] == 'candidate', 'conflicting entity names')
    resolved = entity_consensus([(.9,[('a',.99),('b',.98)]),(.9,[('a',.91)])],.6,.9)
    ok(resolved[0] == 'resolved' and resolved[1][0][0] == 'a', 'consensus stays first candidate')
    ok(entity_consensus([(.5,[('a',.99)])],.6,.9)[0] == 'candidate', 'low confidence cannot resolve')
    rejects(lambda: extract_fields([('NAME:'+'a'*501,.9)]), 'MODEL_ERROR', 'normalized text overflow')
    rejects(lambda: extract_fields([('NAME:a',float('nan'))]), 'MODEL_ERROR', 'nonfinite OCR confidence')
    rejects(lambda: extract_fields([('NAME:a',.9)]*501), 'MODEL_ERROR', 'OCR field overflow')
    return len(checks)


if __name__ == '__main__':
    print(str(run_tests()) + ' synthetic readiness checks passed')
