"""Bounded, explicit Chat Completions tool transport; no provider or protocol fallback."""
import json
from pathlib import Path
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request


class ProviderError(RuntimeError):
    def __init__(self, code, http_status=None):
        self.code, self.http_status = code, http_status
        super().__init__(code)


class CompatibleProvider:
    protocol = 'chat-completions-tools/1'

    def __init__(self, model, base_url, key='', timeout=30):
        u = urllib.parse.urlparse(base_url or '')
        if (not isinstance(model, str) or not model.strip() or not u.hostname or
                u.username or u.password or u.query or u.fragment):
            raise ValueError('Explicit model and clean provider URL required')
        local = u.hostname in ('127.0.0.1', 'localhost', '::1')
        if u.scheme != 'https' and not (u.scheme == 'http' and local):
            raise ValueError('Use HTTPS for remote APIs; HTTP is restricted to local inference')
        self.model, self.base, self.key, self.timeout = model, base_url.rstrip('/'), key, timeout
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        self.opener = urllib.request.build_opener(*([urllib.request.ProxyHandler({})] if local else []), NoRedirect)

    def redact(self, value):
        # Scrub before persistence AND execution, including escaped appearances in arguments.
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if self.key:
            raw = raw.replace(json.dumps(self.key, ensure_ascii=False)[1:-1], '[REDACTED]')
        return json.loads(raw)

    def request(self, messages, tools=None, max_tokens=2000):
        body = {'model': self.model, 'messages': messages, 'max_tokens': max_tokens}
        if tools: body.update(tools=tools, tool_choice='auto')
        return body

    def exchange(self, body):
        raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        if len(raw) > 2000000: raise ProviderError('MODEL_CONTEXT_BYTE_LIMIT')
        headers = {'Content-Type': 'application/json'}
        if self.key: headers['Authorization'] = 'Bearer ' + self.key
        req = urllib.request.Request(self.base + '/chat/completions', data=raw, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                result = response.read(262145)
            if len(result) > 262144: raise ProviderError('MODEL_RESPONSE_BYTE_LIMIT')
            data = json.loads(result)
            message = data['choices'][0]['message']
            if not isinstance(message, dict) or message.get('role', 'assistant') != 'assistant':
                raise ValueError('Invalid message')
            # Preserve provider usage/request/model IDs, but never response headers/credentials.
            return self.redact(data)
        except ProviderError: raise
        except urllib.error.HTTPError as e:
            raise ProviderError('MODEL_HTTP_ERROR', e.code) from None
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            raise ProviderError('MODEL_TRANSPORT_OR_PROTOCOL_ERROR') from None

    def complete(self, prompt):
        """Legacy text-only caller; the engineering runtime exclusively uses exchange()."""
        return self.exchange(self.request([{'role':'user','content':prompt}]))['choices'][0]['message']['content']

    def check_connection(self, data_dir):
        """Persist the tiny UI connection request too; it is not an engineering verdict."""
        from .projects import write
        path=Path(data_dir)/'ai_connections'/(uuid.uuid4().hex+'.json')
        body=self.request([{'role':'user','content':'Reply briefly to confirm connection. Do not claim any engineering validation.'}],max_tokens=32)
        record={'scope':'Connection response only; structured engineering NOT_TESTED','state':'REQUESTING',
                'endpoint':self.base,'request':self.redact(body),'started_ns':time.perf_counter_ns()}
        write(path,record)  # No network if the audit cannot be persisted.
        try:
            response=self.exchange(body)
            record.update(state='RECEIVED',response=response,ended_ns=time.perf_counter_ns())
            write(path,self.redact(record));return {'response':response,'journal':str(path)}
        except ProviderError as error:
            record.update(state='FAILED',error=error.code,http_status=error.http_status,ended_ns=time.perf_counter_ns())
            write(path,self.redact(record));error.journal=str(path);raise


def configured_provider(settings, key=''):
    kind = settings.get('provider')
    base = settings.get('base_url') or ''
    if kind == 'local':
        u = urllib.parse.urlparse(base)
        if u.scheme not in ('http','https') or u.hostname not in ('127.0.0.1','localhost','::1'):
            raise ValueError('Local model requires an explicit loopback endpoint; no model request was sent')
    elif kind == 'openai':
        if not key: raise ValueError('BYOK API key missing; no model request was sent')
        base = base or 'https://api.openai.com/v1'
    else:
        raise ValueError('Choose a compatible API/local model; native provider protocols are not inferred')
    return CompatibleProvider(settings.get('model'), base, key)
