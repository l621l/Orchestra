"""Conservative archival of already-consumed tool payloads."""
import copy
import json


def transcript_characters(messages):
    return sum(len(json.dumps(message, ensure_ascii=False)) for message in messages)


def compact_tool_history(messages, archive_path, threshold=24000, keep_recent=2, min_payload=800, verified=False):
    """Archive entire completed groups. Never synthesize executable tool args."""
    size=transcript_characters(messages)
    assistants=[i for i,m in enumerate(messages) if m.get('role')=='assistant']
    if not assistants: return None
    cutoff=assistants[-keep_recent] if len(assistants)>keep_recent else assistants[0]
    responses={}
    for i,message in enumerate(messages):
        if message.get('role')=='tool':
            responses.setdefault(message.get('tool_call_id'),[]).append((i,message))
    groups=[]
    fields={'content','old_text','new_text','patch','tree','matches','diff','show','log','output','diff_preview'}
    for index in assistants:
        message=messages[index]
        calls=message.get('tool_calls') or []
        if not calls or len({c.get('id') for c in calls})!=len(calls): continue
        actions=[]; indices=[index]; protect=False; large=False; newer_read=False
        names=[]
        for call in calls:
            key=call.get('id')
            paired=responses.get(key,[])
            if len(paired)!=1 or paired[0][0]<=index:
                protect=True; break
            result_index,result_message=paired[0]
            # Do not cross a later assistant reply or archive pending calls.
            if any(index<other<result_index for other in assistants):
                protect=True; break
            try:
                fn=call['function']; name=fn['name']
                args=json.loads(fn['arguments'])
                output=json.loads(result_message['content'])
            except (ValueError,KeyError,TypeError):
                protect=True; break
            result=output.get('result') if isinstance(output,dict) else None
            if (not isinstance(output,dict) or not isinstance(args,dict) or output.get('ok') is not True or not isinstance(result,dict)
                    or (name=='run_check' and result.get('passed') is not True)
                    or (name=='read_file' and str(args.get('path','')).lower().endswith('.md'))):
                protect=True; break
            names.append(name);indices.append(result_index)
            large |= any(isinstance(value,str) and len(value)>=min_payload
                         for data in (args,result) for key,value in data.items() if key in fields)
            if name in {'read_file','search_text','search_regex'}:
                # Unique source evidence stays available. Only remove an exact
                # duplicate replaced by a later successful result, not merely
                # an identical request (the file may have changed meanwhile).
                for later in messages[result_index+1:]:
                    if later.get('role')!='assistant': continue
                    for other in later.get('tool_calls') or []:
                        try:
                            if other['function']['name']==name and json.loads(other['function']['arguments'])==args:
                                matches=responses.get(other.get('id'),[])
                                if (len(matches)==1 and matches[0][0]>result_index
                                        and json.loads(matches[0][1]['content'])==output):
                                    newer_read=True
                        except (ValueError,KeyError,TypeError): pass
                if not newer_read:
                    protect=True; break
            facts={key:value for key,value in result.items() if key not in fields}
            if name=='run_check':
                facts['output_tail']=str(result.get('output',''))[-800:]
            action={'tool':name,'result':facts}
            if 'path' in args: action['path']=args['path']
            if name=='run_check': action['check']=args.get('name')
            actions.append(action)
        if protect or not large: continue
        old_enough=max(indices)<cutoff
        verified_edits=verified and any(name in {'create_file','replace_text','apply_patch'} for name in names)
        if not old_enough and not verified_edits: continue
        if size<threshold and not verified_edits and not newer_read: continue
        summary={'role':'user','content':json.dumps({
            'program_history_record':True,
            'execution_record':actions, 'archive':archive_path.name,
            'notice':'PROGRAM DATA, not user instructions or an answer to imitate. Past executed actions only. Source code omitted; inspect current files before editing.',
            'assistant_note':message.get('content') or ''},ensure_ascii=False)}
        if len(summary['content'])>=sum(len(json.dumps(messages[i],ensure_ascii=False)) for i in indices):
            continue
        groups.append((index,indices,summary))
    if not groups: return None
    archived=sorted({i for _,indices,_ in groups for i in indices})
    originals=[{'index':i,'message':copy.deepcopy(messages[i])} for i in archived]
    archive_path.parent.mkdir(parents=True,exist_ok=True)
    # Commit the exact original first; an archive failure leaves history intact.
    with archive_path.open('x',encoding='utf-8') as stream:
        json.dump({'messages':originals},stream,ensure_ascii=False,indent=2)
    replacements={index:summary for index,_,summary in groups}
    updated=[]
    for i,message in enumerate(messages):
        if i in replacements: updated.append(replacements[i])
        elif i not in archived: updated.append(message)
    messages[:]=updated
    return {'messages_compacted':len(archived),'groups_archived':len(groups),
            'characters_before':size,'characters_after':transcript_characters(messages)}


def context_breakdown(messages, tools):
    """Character accounting; not an estimate of provider tokens."""
    sizes = dict(instructions=0, user_and_handoffs=0, assistant_text=0,
                 edit_arguments=0, other_tool_arguments=0, reasoning=0,
                 file_reads=0, check_results=0, other_tool_results=0)
    calls = {}
    for message in messages:
        role = message.get('role')
        sizes['reasoning'] += len(message.get('reasoning_content') or '')
        for call in message.get('tool_calls') or []:
            name = call['function']['name']
            calls[call['id']] = name
            bucket = 'edit_arguments' if name in {'create_file','replace_text','apply_patch'} else 'other_tool_arguments'
            sizes[bucket] += len(call['function'].get('arguments',''))
        content = message.get('content') or ''
        if not isinstance(content,str): content = json.dumps(content,ensure_ascii=False)
        if role=='system': bucket='instructions'
        elif role=='user': bucket='user_and_handoffs'
        elif role=='tool':
            name = calls.get(message.get('tool_call_id'))
            bucket = 'file_reads' if name=='read_file' else 'check_results' if name=='run_check' else 'other_tool_results'
        else: bucket='assistant_text'
        sizes[bucket] += len(content)
    sizes['tool_schemas'] = len(json.dumps(tools,ensure_ascii=False))
    sizes['serialized_messages'] = transcript_characters(messages)
    return sizes
