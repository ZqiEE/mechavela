"""One request-derived configuration for R10 preparation and bridge receivers.

No resource or authorization enforcement is replaced. Admission still requires
the recorded human grant, and actual execution remains owned by existing Stage
and EngineeringCLI/Workflow. This module only derives paths and descriptors.
"""
import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .reality_bridge_plan import bridge_plan
from .reality_linux import host_path, linux_path


def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()


@dataclass(frozen=True)
class SolidConfiguration:
    request_file: Path
    request_sha256: str
    encoded_request: bytes

    @classmethod
    def receive(cls, request_file):
        path=host_path(str(request_file)).resolve()
        if path.is_symlink() or not path.is_file():raise ValueError('REGULAR_FINAL_REQUEST_REQUIRED')
        data=path.read_bytes();request=json.loads(data.decode('utf-8-sig'))
        if (request.get('request_id')!='RLSOK_REALITY_SOLID_20261019_R10'
            or request.get('single_concurrency') is not True or request.get('previous_budget_reused') is not False
            or request.get('automatic_retry') is not False):raise ValueError('EXACT_R10_REQUEST_REQUIRED')
        root=host_path(request['project_root']).resolve();base=host_path(request['destination']).resolve()
        if not base.is_relative_to(root/'usage') or base==root/'usage':raise ValueError('DEDICATED_R10_DESTINATION_REQUIRED')
        if request.get('distribution')!='Ubuntu-22.04':raise ValueError('FROZEN_DISTRIBUTION_REQUIRED')
        return cls(path,hashlib.sha256(data).hexdigest(),data)

    @property
    def request(self):return json.loads(self.encoded_request.decode('utf-8-sig'))
    @property
    def root(self):return host_path(self.request['project_root']).resolve()
    @property
    def base(self):return host_path(self.request['destination']).resolve()
    @property
    def bridge(self):return self.base/'windows-bridge'
    @property
    def engine(self):return self.bridge/'engine'
    @property
    def parent_authorization(self):return self.base/'authorization.json'
    @property
    def child_authorization(self):return self.bridge/'authorization.json'
    @property
    def active_request(self):return self.base/'RESOURCE_REQUEST.json'
    @property
    def identity(self):
        return {'request_id':self.request['request_id'],'request_sha256':self.request_sha256,
                'project_root':linux_path(str(self.root)),'destination':linux_path(str(self.base)),
                'distribution':self.request['distribution']}

    def validate_grant(self,grant):
        if (grant.get('status')!='AUTHORIZED' or grant.get('authority')!='human:user' or not grant.get('reference')
            or grant.get('request_id')!=self.request['request_id'] or grant.get('request_sha256')!=self.request_sha256
            or grant.get('limits')!=self.request['limits']):raise ValueError('EXACT_NEW_HUMAN_APPROVAL_REQUIRED')

    def require_approval(self):
        if (self.base/'STOP.json').exists():raise ValueError('STOPPED_STAGE_CANNOT_RESUME')
        if self.request_file.read_bytes()!=self.encoded_request or self.active_request.read_bytes()!=self.encoded_request:
            raise ValueError('FINAL_AND_ACTIVE_REQUEST_BYTES_MUST_MATCH')
        grant=read(self.parent_authorization)
        self.validate_grant(grant)
        if (self.child_authorization.read_bytes()!=self.parent_authorization.read_bytes()
            or read(self.bridge/'RESOURCE_REQUEST.json')!={'limits':self.request['limits'],'parent_request_sha256':self.request_sha256}):
            raise ValueError('BRIDGE_MUST_RECEIVE_EXACT_PARENT_APPROVAL_AND_REQUEST')
        manifest=self.base/'frozen/INPUTS.json'
        if sha(manifest)!=self.request['inputs_sha256']:raise ValueError('FROZEN_INPUT_MANIFEST_CHANGED')
        for p,h in read(manifest)['files'].items():
            if sha(host_path(p))!=h:raise ValueError('FROZEN_STAGE_INPUT_CHANGED')
        return grant

    def child_plan(self):
        return bridge_plan(self.bridge,self.parent_authorization,self.request['allocations']['windows_bridge'],
                           self.request['bridge_controller_hold'],self.request['windows_bridge_controlled_memory_bytes'])

    def operation(self, name):
        records=self.request['operations']
        if name not in records:raise ValueError('OPERATION_NOT_IN_FINAL_REQUEST')
        return records[name]

    def operation_file(self,name):
        self.operation(name)
        return self.base/'operations'/(name+'.json')

    def operation_record(self,name,payload,binds=(),identities=None):
        op=self.operation(name)
        return {'configuration':self.identity,'label':name,'phase':op['phase'],'cap':dict(op['cap']),
                'payload':str(Path(payload).resolve()),'payload_sha256':sha(payload),
                'binds':list(binds),'identities':dict(identities or {})}

    def receive_operation(self,name):
        value=read(self.operation_file(name));op=self.operation(name)
        if (value.get('configuration')!=self.identity or value.get('label')!=name
            or value.get('phase')!=op['phase'] or value.get('cap')!=op['cap']):
            raise ValueError('FINAL_REQUEST_AND_RECEIVED_OPERATION_MUST_MATCH')
        if sha(host_path(value['payload']))!=value['payload_sha256']:raise ValueError('FROZEN_PAYLOAD_CHANGED')
        return value

    def stage_parameters(self,name):
        op=self.operation(name)
        return {'name':name,'label':name,'cap':{'cpu':2,'wall':op['cap']['wall']+8},
                'engine':self.engine,'base':self.bridge,
                'memory_cap':self.request['windows_bridge_controlled_memory_bytes'],
                'disk_cap':self.request['limits']['disk_ceiling_bytes']}

    def make_stage(self,name,recipe,tools):
        # Caller must perform require_approval() before any actual preparation
        # or admission. Keeping descriptor construction pure permits software
        # checks with explicit TEST_DATA without inventing human authority.
        from .pcb_cli import Stage
        op=self.receive_operation(name);recipe=Path(recipe).resolve()
        inputs={self.request_file:self.request_sha256,self.active_request:self.request_sha256,
                self.parent_authorization:sha(self.parent_authorization),
                self.child_authorization:sha(self.child_authorization),
                self.operation_file(name):sha(self.operation_file(name)),
                host_path(op['payload']):op['payload_sha256'],recipe:sha(recipe)}
        return Stage(**self.stage_parameters(name),inputs=inputs,tools={recipe:sha(recipe),**tools},recipe=recipe,
                     command='reality-solid-r10',path_formatter=lambda p:str(Path(p).resolve()),result_updater=lambda *_:None)

    def linux_arguments(self,name):
        self.operation(name)
        return ['--linux','--request',linux_path(str(self.active_request)),'--operation',name]


def parse_bridge_arguments(argv=None):
    parser=argparse.ArgumentParser(description='R10 bridge: all paths and identities derive from one final request.')
    parser.add_argument('--linux',action='store_true');parser.add_argument('--request',required=True)
    parser.add_argument('--operation',required=True)
    args=parser.parse_args(argv)
    configuration=SolidConfiguration.receive(args.request)
    configuration.operation(args.operation)
    return args,configuration
