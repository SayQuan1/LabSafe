"""IRR design semantics only: no IO, application transactions, IAM or model execution."""
import math
import re
import unicodedata
from datetime import date
from urllib.parse import urlsplit

LIMITS = {'inspection_items': 100, 'rules': 200, 'detections': 100,
          'relations': 200, 'ocr_fields': 500, 'dates': 100,
          'contexts': 20000, 'findings': 200, 'evidence': 10}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def capacity(kind, count):
    require(type(count) is int and count >= 0, 'VALIDATION_ERROR: invalid count')
    code = ('VALIDATION_ERROR' if kind == 'inspection_items' else
            'RULESET_INVALID' if kind in ('rules', 'contexts', 'findings') else 'MODEL_ERROR')
    require(count <= LIMITS[kind], code + ': ' + kind + ' capacity')
    return count


def inspection_size(template_items, locations):
    require(type(template_items) is int and type(locations) is int and
            1 <= template_items <= 100 and 1 <= locations <= 100, 'VALIDATION_ERROR: input sizes')
    return capacity('inspection_items', template_items * locations)


def pair_count(bottles):
    require(type(bottles) is int and 0 <= bottles <= 100, 'MODEL_ERROR: bottle count')
    return capacity('relations', bottles * (bottles - 1) // 2)


def claim_inference(task, run, item, *, current=True, due=True):
    require(current and due, 'LEASE_LOST: stale or not due')
    require(item == 'queued' and (task, run) in
            {('ready', 'queued'), ('retry_wait', 'retrying')}, 'STATE_CONFLICT: claim')
    return ('leased', 'processing', 'quality_checking', 'quality')


def recover_inference(attempt, *, retryable=True, current=True,
                      owner_valid=True, expired=False, sweeper=False):
    require(type(attempt) is int and 1 <= attempt <= 4, 'STATE_CONFLICT: attempt')
    require((sweeper and expired) or
            (not sweeper and owner_valid and not expired), 'LEASE_LOST: recovery authority')
    if not current:
        return ('failed', None, None, None)
    if retryable and attempt < 4:
        return ('retry_wait', 'retrying', 'queued', 'queued')
    return ('dead_letter' if retryable else 'failed', 'failed', 'failed', 'done')


def completion_outcome(statuses, has_unknown):
    require(type(has_unknown) is bool, 'STATE_CONFLICT: uncertainty must be evaluated')
    allowed = {'needs_review', 'rejected', 'confirmed', 'dispatched', 'closed', 'cannot_determine'}
    require(set(statuses) <= allowed and 'needs_review' not in statuses, 'STATE_CONFLICT: pending finding')
    if has_unknown or 'cannot_determine' in statuses:
        return 'cannot_determine'
    return 'issues_confirmed' if set(statuses) & {'confirmed', 'dispatched', 'closed'} else 'no_issue'


def same_location(parent_a, parent_b):
    return None if parent_a is None or parent_b is None else parent_a == parent_b


def rule_relation(location, relation):
    require(location is None or type(location) is bool, 'VALIDATION_ERROR: same_location')
    require(relation in ('adjacent', 'not_adjacent', 'unknown'), 'VALIDATION_ERROR: relation')
    require(location is True or relation == 'unknown', 'VALIDATION_ERROR: relation/location')
    return {'same_location': location, 'adjacent':
            {'adjacent': True, 'not_adjacent': False, 'unknown': None}[relation]}


OBJECT_GRANTS = {
    'api': {'S': {'PutObject', 'GetObject', 'GetObjectVersion'},
            **{p: {'GetObject', 'GetObjectVersion'} for p in 'OADR'}},
    'general': {'S': {'GetObject', 'GetObjectVersion'},
                **{p: {'GetObject', 'GetObjectVersion', 'PutObject'} for p in 'OAR'},
                'D': {'GetObject', 'GetObjectVersion'}},
    'inference': {'A': {'GetObject', 'GetObjectVersion'},
                  'D': {'GetObject', 'GetObjectVersion', 'PutObject'}},
    'ai': {'A': {'GetObject', 'GetObjectVersion'}},
    'cleanup': {p: {'GetObjectVersion', 'DeleteObjectVersion'} for p in 'OADR'},
}


def object_allowed(role, action, key, tenant, lab, version_id=None):
    """Prefix/operation specification, not an IAM engine or domain authorization check."""
    if not re.fullmatch(r'[A-Za-z0-9_./-]+', key) or any(p in ('', '.', '..') for p in key.split('/')):
        return False
    parts = key.split('/')
    family = None
    if len(parts) == 3 and parts[:2] == ['staging', tenant]:
        family = 'S'
    elif len(parts) >= 6 and parts[:4] == ['tenant', tenant, 'lab', lab]:
        family = {'original': 'O', 'analysis': 'A', 'derivatives': 'D', 'reports': 'R'}.get(parts[4])
    if action.endswith('Version') and not version_id:
        return False
    return action in OBJECT_GRANTS.get(role, {}).get(family, set())


def public_endpoint(endpoint, origin):
    parsed = urlsplit(endpoint)
    require(endpoint == origin and parsed.scheme == 'https' and bool(parsed.hostname)
            and parsed.port in (None, 443) and not parsed.path and not parsed.query
            and not parsed.fragment and not parsed.username and not parsed.password,
            'CONFIG_ERROR: public S3 endpoint')
    return endpoint


def resize_shape(width, height):
    require(type(width) is int and type(height) is int and min(width, height) > 0, 'IMAGE_INVALID: size')
    longest = max(width, height)
    if longest <= 1024:
        return width, height
    return tuple(max(1, (v * 1024 + longest // 2) // longest) for v in (width, height))


def gray(pixel):
    require(len(pixel) == 3 and all(type(v) is int and 0 <= v <= 255 for v in pixel), 'IMAGE_INVALID: RGB8')
    return (77 * pixel[0] + 150 * pixel[1] + 29 * pixel[2] + 128) // 256


def quality_resized(pixels):
    """Already-resized RGB8 input; does not claim to implement or test Pillow resize."""
    require(bool(pixels) and bool(pixels[0]), 'IMAGE_INVALID: empty')
    width, height = len(pixels[0]), len(pixels)
    require(all(len(row) == width for row in pixels), 'IMAGE_INVALID: ragged')
    plane = [[gray(p) for p in row] for row in pixels]

    def reflect(pos, n):
        return 0 if n == 1 else 1 if pos < 0 else n - 2 if pos == n else pos

    lap = []
    for y in range(height):
        for x in range(width):
            lap.append(float(plane[reflect(y-1, height)][x] + plane[reflect(y+1, height)][x]
                             + plane[y][reflect(x-1, width)] + plane[y][reflect(x+1, width)]
                             - 4 * plane[y][x]))
    values = [v for row in plane for v in row]
    mean = math.fsum(lap) / len(lap)
    return {'blur_score': math.fsum((v-mean)**2 for v in lap) / len(lap),
            'brightness': math.fsum(values) / len(values) / 255,
            'glare_ratio': sum(v >= 250 for v in values) / len(values)}


def quality_reasons(scores, blur_min, dark_min, glare_max):
    return [name for name, fails in [('blur', scores['blur_score'] < blur_min),
            ('dark', scores['brightness'] < dark_min), ('glare', scores['glare_ratio'] > glare_max)] if fails]


def valid_box(box):
    require(len(box) == 4 and all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in box)
            and box[2] > box[0] and box[3] > box[1], 'MODEL_ERROR: bbox')
    return box


def parent_box(child, candidates, label=False):
    valid_box(child)
    cx, cy = (child[0]+child[2])/2, (child[1]+child[3])/2
    area = (child[2]-child[0]) * (child[3]-child[1])
    eligible = []
    for ident, box in candidates:
        valid_box(box)
        intersection = max(0, min(child[2], box[2])-max(child[0], box[0])) * max(0, min(child[3], box[3])-max(child[1], box[1]))
        if box[0] <= cx <= box[2] and box[1] <= cy <= box[3] and (not label or intersection/area >= .8):
            eligible.append(((box[2]-box[0])*(box[3]-box[1]), ident))
    if not eligible:
        return None
    best_area = min(a for a, _ in eligible)
    best = [ident for a, ident in eligible if a == best_area]
    return best[0] if len(best) == 1 else None


def box_relation(a, b, parent_a, parent_b, gap_limit):
    valid_box(a); valid_box(b)
    location = same_location(parent_a, parent_b)
    if location is not True:
        return location, 'unknown'
    gap = max(0, max(a[0], b[0]) - min(a[2], b[2])) / max(a[2]-a[0], b[2]-b[0])
    overlap = max(0, min(a[3], b[3]) - max(a[1], b[1])) / min(a[3]-a[1], b[3]-b[1])
    return True, 'adjacent' if gap <= gap_limit and overlap >= .5 else 'not_adjacent'


PREFIXES = [('化学品名称', 'name'), ('名称', 'name'), ('品名', 'name'), ('NAME', 'name'), ('CAS', 'name'),
            ('有效期', 'expiry'), ('失效', 'expiry'), ('EXP', 'expiry'),
            ('生产日期', 'production'), ('生产', 'production'), ('MFG', 'production'),
            ('开封日期', 'opened'), ('开封', 'opened'), ('浓度', 'concentration'), ('含量', 'concentration'),
            ('CONCENTRATION', 'concentration'), ('危险标识', 'hazard_mark'), ('危险性', 'hazard_mark'), ('HAZARD', 'hazard_mark')]
PREFIXES.sort(key=lambda p: -len(p[0]))
DATE_FIELDS = {'expiry', 'production', 'opened'}
DATE_KEYWORDS = re.compile('|'.join(re.escape(p) + (r'(?![A-Za-z])' if p.isascii() else '')
                                  for p, field in PREFIXES if field in DATE_FIELDS), re.I)
DATE_HINT = re.compile(r'\d{4}[-/年]\d{1,2}|\d{1,2}/\d{1,2}/\d{4}')


def normalize(text):
    return ' '.join(unicodedata.normalize('NFKC', text).split())


def split_prefix(text):
    for prefix, field in PREFIXES:
        if text.casefold().startswith(prefix.casefold()):
            tail = text[len(prefix):]
            if prefix.isascii() and tail and tail[0].isascii() and tail[0].isalpha():
                continue
            return prefix, field, tail.lstrip(' :：')
    return None


def parse_date(text):
    match = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})|(\d{4})/(\d{2})/(\d{2})|(\d{4})年(\d{2})月(\d{2})日', text)
    if not match:
        return None
    parts = [int(v) for v in match.groups() if v is not None]
    try:
        return date(*parts).isoformat()
    except ValueError:
        return None


def valid_cas(text):
    if not re.fullmatch(r'\d{2,7}-\d{2}-\d', text):
        return False
    digits = text.replace('-', '')
    return sum(int(v)*i for i, v in enumerate(reversed(digits[:-1]), 1)) % 10 == int(digits[-1])


def extract_fields(lines, aliases=()):
    """lines are already spatially sorted (raw_text, confidence), for one label crop."""
    for raw, confidence in lines:
        require(isinstance(raw, str) and len(raw) <= 2000 and type(confidence) in (float, int)
                and math.isfinite(confidence) and 0 <= confidence <= 1, 'MODEL_ERROR: OCR line')
    known = {normalize(a).casefold() for a in aliases}
    fields = []
    i = 0
    while i < len(lines):
        raw, score = lines[i]
        value = normalize(raw)
        prefix = split_prefix(value)
        if len(DATE_KEYWORDS.findall(value)) > 1:
            field, norm = 'date_unknown', None
        elif prefix:
            keyword, field, text = prefix
            if not text and i+1 < len(lines):
                following = normalize(lines[i+1][0])
                if following and not split_prefix(following):
                    i += 1
                    raw += '\n' + lines[i][0]
                    score = min(score, lines[i][1])
                    text = following
            if field in DATE_FIELDS:
                norm = parse_date(text)
            elif keyword == 'CAS':
                norm = text if valid_cas(text) else None
            else:
                norm = text or None
                if field == 'name' and DATE_HINT.search(text):
                    norm = None
        elif valid_cas(value) or value.casefold() in known:
            field, norm = 'name', value
        elif DATE_HINT.search(value):
            field, norm = 'date_unknown', None
        else:
            i += 1
            continue
        require(len(raw) <= 2000 and (norm is None or len(norm) <= 500), 'MODEL_ERROR: OCR field length')
        fields.append({'field': field, 'raw_text': raw, 'normalized_text': norm, 'confidence': score})
        i += 1
    capacity('ocr_fields', len(fields))
    return fields


def merge_dates(fields, ocr_min):
    merged = {}
    for field in fields:
        kind = 'unknown' if field['field'] == 'date_unknown' else field['field']
        if kind not in DATE_FIELDS | {'unknown'}:
            continue
        value = field['normalized_text'] if kind != 'unknown' and field['confidence'] >= ocr_min else None
        if value is not None:
            value = parse_date(value)
        if kind not in merged:
            merged[kind] = value
        elif merged[kind] != value:
            merged[kind] = None
    return merged


def entity_consensus(proposals, ocr_min, entity_min):
    """Each field proposes (OCR confidence, [(entity_id, score), ...]); no dictionary IO."""
    merged, winners, by_field = {}, [], []
    for confidence, candidates in proposals:
        candidates = sorted(candidates, key=lambda p: (-p[1], p[0]))
        for ident, score in candidates:
            merged[ident] = min(merged.get(ident, score), score)
        by_field.append(dict(candidates))
        unique = bool(candidates) and (len(candidates) == 1 or candidates[0][1] > candidates[1][1])
        winners.append(candidates[0][0] if unique and confidence >= ocr_min and candidates[0][1] >= entity_min else None)
    merged = {ident: min(field.get(ident, 0) for field in by_field) for ident in merged}
    candidates = sorted(merged.items(), key=lambda p: (-p[1], p[0]))[:5]
    resolved = bool(winners) and None not in winners and len(set(winners)) == 1
    return ('resolved' if resolved else 'candidate' if candidates else 'unknown', candidates)
