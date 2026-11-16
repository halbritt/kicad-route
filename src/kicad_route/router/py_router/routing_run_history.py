"""Diagnostic history owned by one outer routing call, outside mutable geometry.

Both route and its CLI __main__ copy import this module by the same name. Child
calls keep distinct result snapshots; events are journaled when recorded, before
any geometry undo can erase or mutate RoutingState.net_history.
"""
from contextvars import ContextVar
from functools import wraps

_active = ContextVar('routing_diagnostic_call', default=None)


class _Run:
    def __init__(self, output):
        self.output = output
        self.calls = []
        self.events = []
        self.states = {}
        self.errors = []

    def story(self):
        story = {'board': self.output, 'nets': {}, 'bus_groups': {}, 'phase3_order': [],
                 'root_call_id': 0, 'calls': self.calls,
                 'diagnostics_complete': not self.errors, 'diagnostic_errors': self.errors}
        # Outer snapshots retain their legacy meaning. A child-only net is
        # included too; every call's own results remain available in calls.
        for call in self.calls:
            for name, net in call['nets'].items():
                story['nets'].setdefault(name, {**net, 'events': [], 'result_call_id': call['call_id']})
            for name, group in call['bus_groups'].items():
                story['bus_groups'].setdefault(name, group)
        story['phase3_order'] = self.calls[0]['phase3_order']
        for name, nid, event in self.events:
            net = story['nets'].setdefault(name, {'net_id': nid, 'status': 'failed_or_untouched', 'events': []})
            net['events'].append(event)
        return story


class _Call:
    def __init__(self, run, parent, input_file, output_file):
        self.run = run
        self.state = None
        self.data = {'call_id': len(run.calls), 'parent_call_id': parent.data['call_id'] if parent else None,
                     'children': [], 'input': str(input_file), 'output': str(output_file),
                     'completion': 'running', 'event_count': 0, 'state_available': False,
                     'nets': {}, 'bus_groups': {}, 'phase3_order': []}
        run.calls.append(self.data)
        if parent:
            parent.data['children'].append(self.data['call_id'])

    def snapshot(self):
        if self.state is None:
            return
        from net_story import build_net_story, _jsonable
        snapshot = _jsonable(build_net_story(self.state, self.run.output, include_events=False))
        for net in snapshot['nets'].values():
            # Events live only in the run's journal, not repeated per snapshot.
            net.pop('events')
        for key in ('nets', 'bus_groups', 'phase3_order'):
            self.data[key] = snapshot[key]


def bind_state(state):
    """Associate a state with its call without putting diagnostic owners in it."""
    call = _active.get()
    if call is not None:
        call.state = state
        call.run.states[id(state)] = call
        call.data['state_available'] = True


def record_event(state, net_id, event):
    active = _active.get()
    if active is None:
        return
    # Helpers may record from a temporary state without a separate batch
    # entry. That attempted work belongs to the current call.
    call = active.run.states.get(id(state), active)
    from net_story import _jsonable, _net_name
    # Keep one event even if its optional payload cannot be captured. The
    # fallback net name is attributable by ID; it is not a resolved net name.
    saved = {'seq': event['sequence'], 'event': event['event'], 'details': None,
             'call_id': call.data['call_id'], 'order': len(call.run.events)}
    captured = [f'net{net_id}', net_id, saved]
    call.run.events.append(captured)
    call.data['event_count'] += 1
    try:
        saved['details'] = _jsonable(event['details'])
    except BaseException as error:
        _capture_error(call, saved, net_id, 'event_details', error)
        if not isinstance(error, Exception):
            raise  # process signals remain signals, with an incomplete capture
    try:
        captured[0] = _net_name(state.pcb_data, net_id)
    except BaseException as error:
        _capture_error(call, saved, net_id, 'net_name', error)
        if not isinstance(error, Exception):
            raise


def _capture_error(call, event, net_id, stage, error):
    # Exception messages can themselves be user-defined diagnostic objects.
    try:
        message = str(error)
    except Exception:
        message = '<error message unavailable>'
    failure = {'call_id': event['call_id'], 'order': event['order'],
               'seq': event['seq'], 'event': event['event'], 'net_id': net_id,
               'stage': stage, 'error_type': type(error).__name__, 'error': message}
    call.run.errors.append(failure)
    event.setdefault('capture_errors', []).append(failure)


def defer_story():
    """Nested writer requests are satisfied when the outer call exits."""
    return _active.get() is not None


def diagnostic_run(function):
    """Wrap batch_route entry/exit, including early returns and exceptions."""
    @wraps(function)
    def run(*args, **kwargs):
        from net_story import net_story_enabled, write_net_story
        parent = _active.get()
        if parent is None and not net_story_enabled():
            return function(*args, **kwargs)
        input_file = args[0] if args else kwargs['input_file']
        output_file = args[1] if len(args) > 1 else kwargs['output_file']
        owner = parent.run if parent else _Run(str(output_file or input_file))
        call = _Call(owner, parent, input_file, output_file)
        token = _active.set(call)
        try:
            result = function(*args, **kwargs)
            call.data['completion'] = 'returned'
            return result
        except BaseException as error:
            call.data.update(completion='raised', exception=type(error).__name__)
            raise
        finally:
            try:
                call.snapshot()
            except Exception as error:
                # Keep already recorded events and make an incomplete snapshot
                # explicit without hiding the routing exception, if any.
                owner.errors.append({'call_id': call.data['call_id'], 'error': str(error)})
            finally:
                if call.state is not None:
                    owner.states.pop(id(call.state))
                    call.state = None
                _active.reset(token)
            if parent is None:
                try:
                    write_net_story(owner.story(), owner.output)
                except Exception as error:
                    # Same optional-diagnostic behavior as the previous writer.
                    print(f'  net story dump failed: {error}')
    return run
