"""Executable reference for rules-dnf-v1; synthetic tests are not safety rules."""
from datetime import date, datetime
FIELDS={'left.storage_class':'str','right.storage_class':'str','same_location':'bool','adjacent':'bool','expiry_date':'date'}
def validate_bundle(bundle):
    seen=set()
    for rule in bundle['rules']:
        key=(rule['rule_id'],rule['scope'],rule['scope_id'])
        if key in seen:raise ValueError('duplicate scoped rule')
        seen.add(key)
        if (rule['scope']=='global') != (rule['scope_id'] is None):raise ValueError('invalid scope_id')
        if rule['effective_to'] and datetime.fromisoformat(rule['effective_to'].replace('Z','+00:00'))<=datetime.fromisoformat(rule['effective_from'].replace('Z','+00:00')):raise ValueError('invalid effective interval')
        if len(set(rule['case_ids']))!=len(rule['case_ids']):raise ValueError('duplicate case ids')
        import re
        placeholders=re.findall(r'\{([^{}]*)\}',rule['explanation_template'])
        if any(x not in {'left_name','right_name','expiry_date','reference_date'} for x in placeholders):raise ValueError('unknown explanation placeholder')
        for clause in rule['clauses']:
            for atom in clause:
                kind=FIELDS[atom['field']];op=atom['op'];value=atom.get('value')
                if kind=='date':
                    if op!='before_reference_date' or 'value' in atom:raise ValueError('date only supports before_reference_date without value')
                elif kind=='bool':
                    if op!='eq' or not isinstance(value,bool):raise ValueError('boolean only supports eq(boolean)')
                elif op=='eq':
                    if not isinstance(value,str):raise ValueError('string eq requires string')
                elif op=='in':
                    if not isinstance(value,list) or not value or any(not isinstance(x,str) for x in value) or len(set(value))!=len(value):raise ValueError('in requires unique string list')
                else:raise ValueError('invalid string operator')
        if rule['finding_type']=='incompatible_storage':
            for clause in rule['clauses']:
                needed={('same_location','eq',True),('adjacent','eq',True)}
                present={(a['field'],a['op'],a.get('value')) for a in clause if not isinstance(a.get('value'),list)}
                if not needed<=present:raise ValueError('pair rule requires same_location and adjacent guards')

def atom_value(atom, facts, reference_date):
    field=atom['field'];value=facts.get(field)
    if value is None:return None
    kind=FIELDS[field]
    if kind=='bool' and not isinstance(value,bool):return None
    if kind=='str' and not isinstance(value,str):return None
    if atom['op']=='before_reference_date':
        try:return date.fromisoformat(value)<date.fromisoformat(reference_date)
        except (ValueError,TypeError):return None
    if atom['op']=='eq':return value==atom['value']
    return value in atom['value']

def tri_and(values):return False if False in values else (None if None in values else True)
def tri_or(values):return True if True in values else (None if None in values else False)
def evaluate_rule(rule,facts,reference_date):return tri_or([tri_and([atom_value(a,facts,reference_date) for a in clause]) for clause in rule['clauses']])

def select_rules(bundle,tenant_id,laboratory_id,reference_at):
    validate_bundle(bundle)
    at=datetime.fromisoformat(reference_at.replace('Z','+00:00'));chosen={};rank={'global':0,'tenant':1,'laboratory':2}
    for rule in bundle['rules']:
        if rule['scope']=='tenant' and rule['scope_id']!=tenant_id:continue
        if rule['scope']=='laboratory' and rule['scope_id']!=laboratory_id:continue
        start=datetime.fromisoformat(rule['effective_from'].replace('Z','+00:00'))
        end=datetime.fromisoformat(rule['effective_to'].replace('Z','+00:00')) if rule['effective_to'] else None
        if at<start or (end and at>=end):continue
        key=rule['rule_id'];old=chosen.get(key)
        if not old or (rank[rule['scope']],rule['priority'])>(rank[old['scope']],old['priority']):chosen[key]=rule
    return [chosen[k] for k in sorted(chosen) if chosen[k]['enabled']]
