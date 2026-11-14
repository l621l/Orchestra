"""Stateless Responses transport preserving encrypted reasoning between tool turns."""
import copy
import json
import time
from model_errors import ModelResponseError, OutputLimitError, error_description
from openai import AsyncOpenAI


def input_items(messages):
    items=[]
    for message in messages:
        if '_response_items' in message:
            items.extend(copy.deepcopy(message['_response_items']))
        elif message['role']=='tool':
            items.append({'type':'function_call_output','call_id':message['tool_call_id'],'output':message['content']})
        elif message.get('tool_calls'):
            if message.get('content'): items.append({'role':'assistant','content':message['content']})
            items.extend({'type':'function_call','call_id':c['id'],'name':c['function']['name'],
                          'arguments':c['function']['arguments']} for c in message['tool_calls'])
        else:
            items.append({'role':message['role'],'content':message.get('content') or ''})
    return items


def request_payload(app, role, messages, tools, resource_status, force_final=False,
                    max_output_tokens=None):
    settings = app.role_settings(role)
    instructions = resource_status
    if force_final:
        instructions += '\n' + app.prompts['final_response']
    definitions = [{'type': 'function', **tool['function'], 'strict': False}
                   for tool in tools] if not force_final else []
    payload = {
        'model': settings['model'],
        'input': input_items(messages) + [{'role': 'system', 'content': instructions}],
        'tools': definitions,
        'reasoning': {'effort': app.settings.get('reasoning_effort', 'low')},
        'max_output_tokens': max_output_tokens or app.settings['generation']['max_output_tokens'],
        'store': False,
        'include': ['reasoning.encrypted_content'],
    }
    if definitions and getattr(app, 'requires_plan_call', lambda: False)():
        payload['tool_choice'] = 'required'
    return payload


async def request(app, role, messages, tools, resource_status, turn_usage, trace, task_id,
                  label=None, force_final=False, max_output_tokens=None):
    settings = app.role_settings(role)
    label = label or role
    provider_name = settings['provider']
    payload = request_payload(app, role, messages, tools, resource_status, force_final,
                              max_output_tokens)
    if hasattr(app, 'validate_request_input'):
        app.validate_request_input(role, payload)
    wire, definitions = payload['input'], payload['tools']
    cap = payload['max_output_tokens']
    effort = payload['reasoning']['effort']
    entry=app.begin_usage(turn_usage,label,role,provider_name,settings['model'])
    if provider_name not in app.clients:
        provider=app.config['providers'][provider_name]
        app.clients[provider_name]=AsyncOpenAI(api_key=provider['api_key'],base_url=provider.get('base_url') or None,
            timeout=app.settings['api']['timeout_seconds'],max_retries=0)
    started=time.monotonic(); request_id=trace.new_id('req'); usage=None; response=None; error_type=None
    metrics={'api':'responses','reasoning_effort':effort,'max_output_tokens':cap,
             'messages':len(wire),'tools_available':len(definitions),'forced_final':force_final,
             'input_characters':len(json.dumps(wire, ensure_ascii=False)),
             'tool_schema_characters':len(json.dumps(definitions, ensure_ascii=False))}
    app.dashboard.update(label,trace.tasks[task_id]['depth'],'waiting for '+settings['model']+' / Responses')
    try:
        response=await app.clients[provider_name].responses.create(**payload)
        if response.usage:
            raw=response.usage.model_dump()
            usage={'prompt_tokens':raw['input_tokens'],'completion_tokens':raw['output_tokens'],
                   'total_tokens':raw['total_tokens'],'prompt_tokens_details':raw.get('input_tokens_details') or {},
                   'completion_tokens_details':raw.get('output_tokens_details') or {}}
        output=[item.model_dump(exclude_none=True) for item in response.output]
        calls=[]
        for item in output:
            if item['type']=='function_call':
                if item.get('status')!='completed': break
                calls.append({'id':item['call_id'],'type':'function',
                              'function':{'name':item['name'],'arguments':item['arguments']}})
        answer=response.output_text or ''
        if response.status=='incomplete':
            if getattr(response.incomplete_details,'reason',None) != 'max_output_tokens':
                raise ModelResponseError('Responses stopped before completion for a reason other than the output limit.')
            error=OutputLimitError('Responses output limit reached.',draft=answer or None,tool_drafts=calls)
            error.response_items=[item for item in output if item['type']=='reasoning' or
                                  (item['type']=='function_call' and item.get('call_id') in {c['id'] for c in calls})]
            raise error
        if any(part.get('type')=='refusal' for item in output if item['type']=='message' for part in item.get('content',[])):
            raise ModelResponseError('The model declined this request.')
        if response.status!='completed': raise ModelResponseError('Responses request did not complete: '+response.status)
        if any(item['type']=='function_call' and item.get('status')!='completed' for item in output):
            raise ModelResponseError('Completed response contains an unfinished function call; no tools executed.')
        return {'role':'assistant','content':answer or None,'tool_calls':calls,'_response_items':output}
    except Exception as error:
        error_type=type(error).__name__
        description=error_description(error)
        trace.error_message=description
        trace.event('model_request_error',task_id=task_id,request_id=request_id,error_type=error_type,message=description)
        app.dashboard.action(label,description,failed=True)
        raise
    finally:
        app.add_usage(entry,usage,getattr(response,'model',None))
        entry['output_budget_tokens']=entry.get('output_budget_tokens',0)+(usage['completion_tokens'] if usage else cap)
        if usage is None: entry['output_budget_estimated']=True
        app.dashboard.record_usage(label, entry)
        app.dashboard.update_output_budget(sum(v.get('output_budget_tokens',0) for v in turn_usage.values()),
            app.settings['execution']['max_output_tokens_per_turn'],
            estimated=any(v.get('output_budget_estimated',False) for v in turn_usage.values()))
        app.dashboard.update(label,trace.tasks[task_id]['depth'],'response received' if not error_type else 'request failed',
                             cost=app.estimated_cost(entry) if usage else None)
        trace.record_request(task_id,request_id,getattr(response,'id',None),provider_name,settings['model'],
            getattr(response,'model',None),time.monotonic()-started,getattr(response,'status',None),usage,error_type,
            metrics,{'answer_characters':len(getattr(response,'output_text','') or '')})
