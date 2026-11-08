"""Read-only runtime declarations; never imports CAD, executes or grants access."""
import json,re
from pathlib import Path

def describe(path=None):
    path=Path(path) if path else Path(__file__).resolve().parents[2]/'release/cad-runtimes/catalog.json'
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data,dict) or data.get('schema')!='rlsok.cad-runtime-catalog/1' or not isinstance(data.get('profiles'),list):raise ValueError('Invalid runtime catalog')
        seen=set();profiles=[]
        for row in data['profiles']:
            if not isinstance(row,dict) or not re.fullmatch('[a-z0-9-]{1,100}',row.get('id','')) or row['id'] in seen:raise ValueError('Invalid or duplicate runtime ID')
            if row.get('mode') not in ('bundled-python','external-host','remote-api'):raise ValueError('Unknown runtime mode')
            if not isinstance(row.get('versions'),dict) or not isinstance(row.get('declared_capabilities'),list):raise ValueError('Invalid compatibility declaration')
            seen.add(row['id'])
            # Ignore any availability/permissions inserted into a declaration.
            profiles.append({k:row[k] for k in ('id','mode','platform','python_abi','versions','declared_capabilities','known_constraint') if k in row})
        return {'schema':data['schema'],'profiles':profiles,'authority':'DECLARATIONS_ONLY_NOT_EXECUTION_APPROVAL',
                'cad_execution':'NOT_QUALIFIED_BY_CATALOG','automatic_fallback':False,
                'requirements':'Owner permission, exact runtime identity, OS trust and real native functional qualification remain separate. Package metadata, signatures and version output alone never prove CAD execution. Never mix native DLLs across profiles or retry known policy-denied binaries.'}
    except (OSError,ValueError,TypeError) as e:
        return {'schema':'rlsok.cad-runtime-catalog/1','profiles':[],'cad_execution':'UNKNOWN','automatic_fallback':False,'error':str(e)}
