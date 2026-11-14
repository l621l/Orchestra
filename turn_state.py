"""Edit and verification state for a single model run."""
from tool_contracts import EDIT_TOOL_NAMES


class TurnState:
    def __init__(self):
        self.revision = 0
        self.changed = set()
        self.checks = {}
        self.edit_errors = set()

    def can_commit(self, required_checks):
        return bool(required_checks) and all(
            self.checks.get(name) == (self.revision, True) for name in required_checks
        )

    def record_result(self, name, args, value):
        if name == 'git_pull' and value.get('updated'):
            self.revision += 1
        if name in EDIT_TOOL_NAMES and value.get('applied'):
            self.revision += 1
            self.changed.update(value.get('files', []))
            self.edit_errors.difference_update(value.get('files', []))
            self.edit_errors.discard('*')
        if name == 'run_check':
            self.checks[args['name']] = (self.revision, value['passed'])
        elif name == 'run_command':
            if value.get('qualifies_as_verification'):
                self.checks['runtime'] = (self.revision, True)
            elif value.get('passed') is False:
                self.checks['runtime'] = (self.revision, False)

    def record_failure(self, name, args, required_checks):
        if name in EDIT_TOOL_NAMES:
            self.edit_errors.add(args.get('path', '*'))
        if name == 'run_check' and args.get('name') in required_checks:
            self.checks[args['name']] = (self.revision, False)
        elif name == 'run_command':
            self.checks['runtime'] = (self.revision, False)

    def result(self, answer, required_checks, *, partial, require_checks):
        required = set(required_checks)
        passed = {name for name, (rev, ok) in self.checks.items() if rev == self.revision and ok}
        verified = required <= passed if required else 'runtime' in passed
        if any(rev == self.revision and not ok for rev, ok in self.checks.values()):
            check_status = 'failed'
        elif verified:
            check_status = 'passed'
        else:
            check_status = 'not_run_or_stale' if required else 'not_configured'
        incomplete = (partial or self.edit_errors or check_status == 'failed'
                      or (self.changed and not verified and require_checks))
        return {
            'answer': answer or 'No answer text received.',
            'status': 'partial' if incomplete else 'completed',
            'modified_files': sorted(self.changed),
            'verification': {
                'status': check_status,
                'revision': self.revision,
                'checks': {name: {'revision': rev, 'passed': ok}
                           for name, (rev, ok) in self.checks.items()},
            },
        }
