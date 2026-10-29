"""Edit-gap policy evaluated between responses, never an in-flight timeout."""
import time


class EditProgress:
    def __init__(self, policy, active=True, clock=time.monotonic):
        self.policy = policy
        self.enabled = active and policy.get('enabled', False)
        self.clock = clock
        self.started = self.last_edit = clock()
        self.calls = self.last_edit_call = 0
        self.recovery_started = None
        self.recovery_call = None
        self.stage = 'investigation'
        self.stage_started = self.started
        self.stage_call = 0
        self.recovery_calls = policy.get('recovery_calls', 0)
        self.recovery_seconds = policy.get('recovery_seconds', 0)

    def state(self):
        if not self.enabled:
            return 'disabled'
        if self.stage in {'verification', 'report'}:
            return self.stage
        if self.recovery_started is not None:
            if (self.calls - self.recovery_call >= self.recovery_calls
                    or self.clock() - self.recovery_started >= self.recovery_seconds):
                return 'exhausted'
            return 'recovery'
        if (self.calls - self.stage_call >= self.policy['calls_without_edit']
                or self.clock() - self.stage_started >= self.policy['seconds_without_edit']):
            return 'warn'
        return 'working'

    def recover(self, calls=None, seconds=None):
        self.recovery_started = self.clock()
        self.recovery_call = self.calls
        self.recovery_calls = self.policy['recovery_calls'] if calls is None else calls
        self.recovery_seconds = self.policy['recovery_seconds'] if seconds is None else seconds

    def enter_stage(self, stage):
        if stage != self.stage:
            self.stage = stage
            self.stage_started, self.stage_call = self.clock(), self.calls
            self.recovery_started = self.recovery_call = None

    def edited(self):
        self.last_edit = self.clock()
        self.last_edit_call = self.calls
        self.recovery_started = self.recovery_call = None
        self.stage = 'implementation'
        self.stage_started, self.stage_call = self.last_edit, self.calls

    def snapshot(self):
        return {'calls_without_edit': self.calls - self.last_edit_call,
                'seconds_without_edit': round(self.clock() - self.last_edit, 1),
                'state': self.state(), 'stage': self.stage,
                'recovery_calls_available': self.recovery_calls,
                'recovery_seconds_available': self.recovery_seconds}


def validate_role_policies(settings):
    for role in ('director', 'worker'):
        options = settings.get('roles', {}).get(role, {})
        for key, default in [('thinking', 'enabled'), ('recovery_thinking', 'disabled'),
                             ('report_thinking', 'disabled')]:
            if options.get(key, default) not in ('enabled', 'disabled'):
                raise ValueError(f'roles.{role}.{key} must be enabled or disabled.')
        policy = options.get('no_progress', {})
        allowed = {'enabled', 'calls_without_edit', 'seconds_without_edit',
                   'recovery_calls', 'recovery_seconds', 'verification_reserve_calls',
                   'report_reserve_calls', 'verification_reserve_seconds', 'report_reserve_seconds'}
        if not isinstance(policy, dict) or set(policy) - allowed:
            raise ValueError(f'Invalid roles.{role}.no_progress policy.')
        if type(policy.get('enabled', False)) is not bool:
            raise ValueError(f'roles.{role}.no_progress.enabled must be boolean.')
        for key in allowed - {'enabled'}:
            if key.endswith('_reserve_calls') or key.endswith('_reserve_seconds'):
                default = 1 if key.endswith('_calls') else 30
                value = policy.get(key, default)
                if (type(value) not in (int, float) or not 0 <= value < float('inf')
                        or ('calls' in key and type(value) is not int)
                        or (key.startswith('report') and value <= 0)):
                    raise ValueError(f'Invalid roles.{role}.no_progress.{key}')
                continue
            if key not in policy and not policy.get('enabled', False):
                continue
            value = policy.get(key)
            if (type(value) not in (int, float) or not 0 < value < float('inf')
                    or ('calls' in key and type(value) is not int)):
                raise ValueError(f'Invalid roles.{role}.no_progress.{key}')
