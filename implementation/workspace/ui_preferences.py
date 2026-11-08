"""Small, owner-local UI preferences, separate from exported projects and model credentials."""
import json
from pathlib import Path
from .projects import read,write

def preferences(data,language=None):
    path=Path(data)/'ui-preferences.json'
    locales=json.loads((Path(__file__).resolve().parents[2]/'console/static/connection-locales.json').read_text(encoding='utf-8'))
    if language is not None:
        if language!='auto' and language not in locales:raise ValueError('Unsupported software language')
        write(path,{'language':language})
    return {**(read(path) if path.is_file() else {'language':'auto'}),'locales':locales}
