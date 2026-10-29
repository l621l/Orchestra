"""Short criterion IDs with automated or file-review evidence.

This validates the review contract, not the semantic truth of model claims.
Configured checks are rerun by promotion on the transferred candidate.
"""
from tool_errors import ToolError


def validate_criteria(criteria):
    if (not isinstance(criteria, list) or not criteria
            or any(not isinstance(item, str) or not item.strip() for item in criteria)
            or len(set(criteria)) != len(criteria)):
        raise ToolError('acceptance_criteria_required',
                        'Supply unique, nonempty acceptance_checks for this milestone.')
    return list(criteria)


def criterion_catalog(criteria):
    return [{'id': f'C{index}', 'description': text}
            for index, text in enumerate(validate_criteria(criteria), 1)]


def validate_acceptance(criteria, evidence, configured_checks, review_file=None):
    catalog = criterion_catalog(criteria)
    ids = [item['id'] for item in catalog]
    details = {'criteria': catalog, 'configured_checks': sorted(configured_checks),
               'examples': [{'id': ids[0], 'check': next(iter(sorted(configured_checks)), 'configured_check'),
                             'evidence': 'tests/test_feature.py::test_behavior passed'},
                            {'id': ids[0], 'files': ['README.md'],
                             'evidence': 'Reviewed launch instructions and examples.'}]}

    def reject(code, message):
        raise ToolError(code, message, **details)

    if not isinstance(evidence, list):
        reject('acceptance_evidence_required', 'Supply one evidence entry per returned criterion ID.')
    seen, result = set(), []
    for item in evidence:
        if (not isinstance(item, dict) or set(item) - {'id', 'check', 'files', 'evidence'}
                or any(not isinstance(item.get(key), str) or not item[key].strip()
                       for key in ('id', 'evidence'))):
            reject('invalid_acceptance_evidence', 'Use id and evidence, plus either check or files.')
        criterion_id = item['id']
        if criterion_id not in ids or criterion_id in seen:
            reject('invalid_acceptance_evidence', 'Use each returned ID (C1, C2, ...) exactly once; do not repeat criterion text.')
        if ('check' in item) == ('files' in item):
            reject('invalid_acceptance_evidence', 'Choose check for an automated result OR files for inspected documents/code.')
        if 'check' in item:
            if not isinstance(item['check'], str) or item['check'] not in configured_checks:
                reject('acceptance_check_unavailable', 'Unknown configured check. For document review use files instead.')
            result.append(dict(item))
        else:
            files = item['files']
            if (not isinstance(files, list) or not files
                    or any(not isinstance(path, str) or not path.strip() for path in files)):
                reject('invalid_acceptance_evidence', 'files must list existing relative candidate file paths.')
            if review_file is None:
                reject('acceptance_review_unavailable', 'Candidate file review is unavailable.')
            for path in files:
                review_file(path)
            result.append({**item, 'files': list(files)})
        seen.add(criterion_id)
    if set(ids) != seen:
        details['missing_ids'] = [key for key in ids if key not in seen]
        reject('acceptance_incomplete', 'Criteria remain unverified. Add their evidence or preserve a checkpoint.')
    return result
