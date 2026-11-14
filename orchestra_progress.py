"""Progress formatting shared by terminal and headless runs."""


def clean(value, limit=240):
    text = ' '.join(''.join(c if c.isprintable() else ' ' for c in str(value)).split())
    return text if len(text) <= limit else text[:limit-3] + '...'

def describe_tool(name, args):
    detail = args.get('path', '')
    if name == 'read_file':
        detail += f":{args.get('start_line', 1)}-{args.get('end_line', 'end')}"
    elif name in {'search_text', 'search_regex'}:
        detail = f"{detail or '.'} | {args.get('query', args.get('pattern', ''))}"
    elif name == 'run_check':
        detail = args.get('name', '')
    elif name == 'run_command':
        detail = ' '.join(args.get('argv',[]))
    elif name == 'finish_task':
        detail = args.get('id','')
    elif name == 'set_plan':
        detail = f"{len(args.get('tasks',[]))} tasks"
    elif name == 'git_show':
        detail = f"{args.get('revision', '')} {detail}"
    elif name == 'git_create_branch':
        detail = args.get('name', '')
    elif name == 'create_commit':
        detail = args.get('message', '')
    return clean(f'{name} {detail}'.strip())

class SilentProgress:
    """Default progress sink for an engine without an interactive display."""

    @staticmethod
    def ignore(*args, **kwargs):
        pass

    configure_models = activate_model = record_usage = ignore
    action = tool_finished = update_output_budget = ignore
    start = stop = update = finish = ignore
