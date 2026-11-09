"""Task-scoped diagnosis and next action, separate from transient observations."""
import copy
import json


def short(value,limit=400):
    text=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
    return text if len(text)<=limit else text[:limit]+' [excerpt]'


def current_work(state,task):
    key=task['id'] if task else 'planning'
    if state.get('working_state',{}).get('task_id')!=key:
        state['working_state']={'task_id':key,'revision':state['revision'],'phase':'inspect',
                                'diagnosis':'','next_action':'','facts':[]}
    return state['working_state']


def work_context(state,task):
    work=copy.deepcopy(current_work(state,task))
    work['diagnosis']=short(work['diagnosis'],240)
    work['next_action']=short(work['next_action'],260)
    work['facts']=[{k:short(v,180) if isinstance(v,str) else v for k,v in fact.items()} for fact in work['facts'][-2:]]
    return work


def remember_result(state,task,name,args,output):
    work=current_work(state,task)
    value=output.get('result',{})
    failed=not output.get('ok') or name=='run_command' and not value.get('passed')
    fact=None
    if failed and output.get('error')!='skipped_after_failure':
        fact={'kind':'failure','tool':name,'arguments':short(args),
              'message':short(output.get('message') or value.get('output') or 'Command failed')}
        if name not in {'read_file','list_files','search_text','search_regex'} or work['phase']!='repair':
            work.update(phase='repair',diagnosis=fact['message'],
                next_action='Use the concrete failure to make one targeted correction. Read only the exact missing definition; do not rescan the project.')
    elif name in {'search_text','search_regex'} and value.get('matches_returned')==0:
        query=args.get('query',args.get('pattern',''))
        fact={'kind':'not_found','path':args.get('path','.'),'query':short(query,160)}
        if query and work['phase']=='repair':
            work['diagnosis']=short(work['diagnosis']+'; search found no '+str(query),600)
            work['next_action']='The searched capability is absent in the searched scope. Implement the missing support or choose an existing supported verification path; do not retry the same unsupported command.'
    elif name in {'create_file','replace_text'} and value.get('applied'):
        fact={'kind':'changed','files':value.get('files',[]),'revision':state['revision']}
        work.update(phase='verify',revision=state['revision'],
                    next_action='Verify the changed project behavior now, then finish the current task if its criteria pass.')
    elif name=='run_command' and value.get('qualifies_as_verification'):
        fact={'kind':'verified','criteria':value.get('criteria',[]),'project_files':value.get('executed_project_files',value.get('compiled_project_files',[]))}
        work.update(phase='verify',next_action='Check any remaining acceptance criteria; otherwise call finish_task. Do not restart source exploration.')
    if fact:
        if fact not in work['facts']:work['facts'].append(fact)
        work['facts']=work['facts'][-3:]
    return work


def make_turn(task_id,message,results):
    assistant={k:copy.deepcopy(v) for k,v in message.items() if k in {'role','content','tool_calls'}}
    assistant['role']='assistant'
    if assistant.get('tool_calls'):
        ids=[c['id'] for c in assistant['tool_calls']]
        if ids!=[r['tool_call_id'] for r in results]:
            return None
    elif results:
        return None
    return {'task_id':task_id,'messages':[assistant,*copy.deepcopy(results)]}
