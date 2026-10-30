"""Small workspace-scoped continuation state; never stores tool transcripts."""
import hashlib
import json
import re
from pathlib import Path
from interface_notes import atomic_text

OWNER='orchestra.session.v1'
CONTINUE={'continue','resume','продолжи','продолжить','дальше'}


class SessionState:
    def __init__(self, logs, workspace):
        self.workspace=str(Path(workspace).resolve())
        key=hashlib.sha256(self.workspace.casefold().encode()).hexdigest()[:24]
        self.path=Path(logs)/'sessions'/(key+'.json')

    @staticmethod
    def is_continue(text):
        return SessionState.parse_continue(text)[0]

    @staticmethod
    def parse_continue(text):
        match=re.match(r'^(continue|resume|продолжи|продолжить|дальше)(?=$|[\s.,!?:;—-])',text.strip(),re.I)
        if not match: return False,''
        return True,text.strip()[match.end():].lstrip(' \t\r\n.,!?:;—-')

    @staticmethod
    def layout_suggestion(text):
        if re.search('[А-Яа-яЁё]',text): return None
        english="qwertyuiop[]asdfghjkl;'zxcvbnm,."
        russian='йцукенгшщзхъфывапролджэячсмитьбю'
        mapping=dict(zip(english,russian))
        mapping.update({a.upper():b.upper() for a,b in zip(english,russian) if a.isalpha()})
        table=str.maketrans(mapping)
        candidate=text.translate(table)
        first=candidate.lower().split()[0].strip('.,!?') if candidate.split() else ''
        if first in {'продолжи','продолжить','дальше','сделай','исправь','добавь','проверь','посмотри'} and len(candidate.split())>=2:
            return candidate
        return None

    def load(self):
        if not self.path.exists(): return None
        if self.path.stat().st_size>64000: raise ValueError('Continuation file is too large')
        value=json.loads(self.path.read_text(encoding='utf-8'))
        if (not isinstance(value,dict) or value.get('managed_by')!=OWNER
                or not isinstance(value.get('workspace'),str)
                or value['workspace'].casefold()!=self.workspace.casefold()
                or not isinstance(value.get('original_task'),str)
                or not value['original_task'].strip()
                or not isinstance(value.get('handoff'),str)):
            raise ValueError('Invalid continuation file')
        clarifications=value.get('clarifications',[])
        if not isinstance(clarifications,list) or any(not isinstance(item,str) for item in clarifications):
            raise ValueError('Invalid clarifications')
        value['clarifications']=clarifications
        return value

    def save(self, task, handoff, turn_id, clarifications=None):
        value={'managed_by':OWNER,'workspace':self.workspace,'original_task':task,
               'handoff':handoff,'turn_id':turn_id,'clarifications':list(clarifications or [])}
        content=json.dumps(value,ensure_ascii=False,indent=2)+'\n'
        if len(content.encode('utf-8'))>64000:
            raise ValueError('Continuation state exceeds size limit')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        atomic_text(self.path,content)
