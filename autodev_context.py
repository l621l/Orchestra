"""Whole-line source excerpts and structured, bounded tool evidence."""
import copy
import json


def size(value):
    return len(json.dumps(value,ensure_ascii=False))


def fit_event(event,limit):
    item=copy.deepcopy(event)
    for index,arg in enumerate(item.get('args',{}).get('argv',[])):
        if len(arg)>500:
            item['args']['argv'][index]=arg[:500]+' [argument excerpt]'
            item['arguments_truncated']=True
    if size(item)<=limit:
        return item
    output=item.get('result',{})
    value=output.get('result',{})
    if item.get('tool')=='read_file' and isinstance(value,dict) and 'content' in value:
        lines=value['content'].splitlines()
        first=value.get('returned_start_line') or item.get('args',{}).get('start_line',1)
        original_end=value.get('returned_end_line')
        value['content']=''
        value['context_truncated']=True
        value['returned_end_line']=None
        value['next_start_line']=first
        value['available_end_line']=original_end
        for index,line in enumerate(lines):
            previous=value['content']
            value['content']=previous+('\n' if previous else '')+line
            value['returned_end_line']=first+index
            value['next_start_line']=first+index+1
            if size(item)>limit:
                value['content']=previous
                value['returned_end_line']=first+index-1 if index else None
                value['next_start_line']=first+index
                break
        if not value['content']:
            value['hint']='No complete source line fits. Use a narrower search for this long line.'
        return item if size(item)<=limit else None
    if item.get('tool')=='read_spec' and isinstance(value,dict) and 'content' in value:
        text=value['content'];offset=item.get('args',{}).get('offset',0)
        value['content']='';value['context_truncated']=True
        for count in range(min(len(text),limit//2),-1,-100):
            value['content']=text[:count];value['next_offset']=offset+count
            if size(item)<=limit:return item
        return None
    # Remove bulky payloads, preserving valid JSON, outcome and exact source ranges.
    if isinstance(value,dict):
        for key in ('content','output','diagnostic'):
            if isinstance(value.get(key),str):
                value[key]=value[key][-max(100,limit//4):]
                value[key+'_truncated_in_context']=True
        if item.get('tool')=='set_plan':
            current=value.pop('current_task',None)
            if current:value['current_task_id']=current['id']
    if size(item)<=limit:return item
    fallback={'tool':item.get('tool'),'args':item.get('args',{}),'result':{
        'ok':output.get('ok'),'error':output.get('error'),
        'message':'Payload omitted for context limit; operation outcome retained.'},'payload_omitted':True}
    return fallback if size(fallback)<=limit else None


def normalized_read_args(args):
    bounded=dict(args)
    start=bounded.get('start_line',1)
    bounded['start_line']=start
    bounded['end_line']=min(bounded.get('end_line',start+39),start+39)
    return bounded
