"""Versioned model-authored notes; always separate from parsed declarations."""
import ast
import hashlib
import json
import os
from pathlib import Path
import tempfile

OWNER = 'orchestra.interface_notes.v1'
FILE = 'INTERFACE_NOTES.json'


def atomic_text(target, content):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent,prefix='.orchestra-memory-',delete=False) as stream:
            temporary=stream.name
            stream.write(content.encode('utf-8'))
        os.replace(temporary,target)
    finally:
        if temporary:
            try: os.unlink(temporary)
            except FileNotFoundError: pass


def symbols(tree):
    result=set()
    for node in tree.body:
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
            result.add(node.name)
        if isinstance(node,(ast.Assign,ast.AnnAssign)):
            for target in node.targets if isinstance(node,ast.Assign) else [node.target]:
                if isinstance(target,ast.Name): result.add(target.id)
        if isinstance(node,ast.ClassDef):
            for child in node.body:
                if isinstance(child,(ast.FunctionDef,ast.AsyncFunctionDef)):
                    result.add(node.name+'.'+child.name)
                    pending=list(child.body)
                    while pending:
                        item=pending.pop()
                        if isinstance(item,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef,ast.Lambda)): continue
                        targets=item.targets if isinstance(item,ast.Assign) else [item.target] if isinstance(item,ast.AnnAssign) else []
                        for target in targets:
                            if isinstance(target,ast.Attribute) and isinstance(target.value,ast.Name) and target.value.id=='self':
                                result.add(node.name+'.'+target.attr)
                        pending.extend(ast.iter_child_nodes(item))
                if isinstance(child,(ast.Assign,ast.AnnAssign)):
                    for target in child.targets if isinstance(child,ast.Assign) else [child.target]:
                        if isinstance(target,ast.Name): result.add(node.name+'.'+target.id)
    return result


class InterfaceNotes:
    def __init__(self,policy): self.policy=policy

    def load(self):
        target=self.policy.resolve_write(FILE)
        if not target.exists(): return []
        if target.stat().st_size>256000: raise ValueError('Notes file exceeds limit')
        doc=json.loads(target.read_text(encoding='utf-8'))
        if not isinstance(doc,dict) or doc.get('managed_by')!=OWNER or not isinstance(doc.get('notes'),list):
            raise ValueError('Existing notes file is not managed by Orchestra; left untouched')
        if len(doc['notes'])>200: raise ValueError('Too many stored notes')
        for note in doc['notes']:
            if (not isinstance(note,dict) or not all(isinstance(note.get(key),str) for key in ('path','symbol','note'))
                    or not isinstance(note.get('source_hashes'),dict)
                    or not all(isinstance(k,str) and isinstance(v,str) for k,v in note['source_hashes'].items())):
                raise ValueError('Invalid notes file; left untouched')
        return doc['notes']

    def digest(self,path):
        target=self.policy.resolve_read(path)
        if not target.is_file() or target.stat().st_size>2000000: raise ValueError('Unsupported note source')
        return hashlib.sha256(target.read_bytes()).hexdigest()

    def view(self):
        result=[]
        for note in self.load():
            stale=[]
            for path,digest in note['source_hashes'].items():
                try:
                    if self.digest(path)!=digest: stale.append(path)
                except (OSError,ValueError): stale.append(path)
            result.append({**note,'status':'stale' if stale else 'current_at_recorded_version',
                           'changed_sources':stale})
        return result

    def save(self,notes,changed_paths):
        if not notes: return {'saved':0,'warnings':[]}
        existing=self.load()
        keyed={(n['path'],n['symbol']):n for n in existing}
        saved=0;warnings=[]
        changed={self.policy.relative(path).as_posix() for path in changed_paths}
        for note in notes:
            try:
                path=self.policy.relative(note['path']).as_posix()
                if path not in changed: raise ValueError('Note source must be a file changed by this operation')
                source=self.policy.resolve_read(path)
                if source.suffix!='.py' or source.stat().st_size>500000:
                    raise ValueError('Notes require a bounded Python source file')
                if note['symbol'] not in symbols(ast.parse(source.read_bytes())):
                    raise ValueError('Symbol is not statically declared; attach the note to its declared class/function instead')
                sources={path,*[self.policy.relative(value).as_posix() for value in note.get('related_paths',[])]}
                hashes={value:self.digest(value) for value in sorted(sources)}
                keyed[(path,note['symbol'])]={'path':path,'symbol':note['symbol'],'note':note['note'],
                                           'source_hashes':hashes}
                saved+=1
            except (OSError,ValueError,SyntaxError) as error:
                warnings.append(f"Note {note.get('symbol','?')} was not saved: {type(error).__name__}; inspect its declared symbol and source paths.")
        values=list(keyed.values())
        content=json.dumps({'managed_by':OWNER,'notes':values},ensure_ascii=False,indent=2)+'\n'
        if len(values)>200 or len(content.encode('utf-8'))>256000:
            raise ValueError('Notes storage limit exceeded; existing file left untouched')
        if saved:
            atomic_text(self.policy.resolve_write(FILE),content)
        return {'saved':saved,'warnings':warnings}
