"""Owner-local startup state, scoped to a project library, never exported or executable."""
from pathlib import Path
from .projects import read,write


def session(data,store,changes=None):
    path=Path(data)/'workspace-session.json'
    with store.lock:
        state={'reopen':True,'project':None}
        if path.is_file():
            try:
                saved=read(path)
                if type(saved.get('reopen')) is bool:state['reopen']=saved['reopen']
                if saved.get('project') is None or isinstance(saved.get('project'),str):state['project']=saved.get('project')
            except (OSError,ValueError,TypeError,AttributeError):pass
        if changes is not None:
            if not isinstance(changes,dict) or not changes or set(changes)-{'reopen','project'}:raise ValueError('Invalid workspace session setting')
            if 'reopen' in changes and type(changes['reopen']) is not bool:raise ValueError('Invalid startup restore choice')
            if 'project' in changes and changes['project'] is not None:store.get(changes['project'])
            state.update(changes);write(path,{'schema':'rlsok.workspace-session/1',**state})
        available=True
        if state['project'] is not None:
            try:store.get(state['project'])
            except (OSError,ValueError,KeyError,TypeError):available=False
        return {**state,'available':available}
