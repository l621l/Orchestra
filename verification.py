"""Track which configured checks describe the current workspace revision."""


class Verification:
    def __init__(self, configured):
        self.configured = sorted(configured)
        self.revision = 0
        self.results = {}

    def edited(self):
        self.revision += 1

    def checked(self, name, passed):
        self.results[name] = {'name': name, 'passed': passed,
                              'revision': self.revision}

    def summary(self):
        current = [r for r in self.results.values() if r['revision'] == self.revision]
        if not self.configured:
            status = 'not_configured'
        elif any(not r['passed'] for r in current):
            status = 'failed'
        elif current:
            status = 'passed'
        elif self.results:
            status = 'stale'
        else:
            status = 'not_run'
        return {'status': status, 'workspace_revision': self.revision,
                'checks': [self.results[name].copy() for name in sorted(self.results)]}
