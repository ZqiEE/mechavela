"""Read-only installed capabilities. No port discovery, connection or motion."""
import importlib.metadata
import json
from .dynamixel import PROFILES

def capabilities():
    try:version=importlib.metadata.version('dynamixel-sdk')
    except importlib.metadata.PackageNotFoundError:version=None
    return {'schema':'rlsok.hardware-capabilities/1','sdk_version':version,
        'core':'arbitrary unit-bearing modes, command/feedback channels, limits, connection/fault states',
        'simulated_transport':'TESTED; not a physics solver','physical_motion':'DISABLED',
        'physical_validation':'NOT_TESTED','port_opened':False,
        'dynamixel_profiles':[{'model_number':model,'protocol':protocol,'name':p['name'],'source':p['source'],
            'firmware':'Exact caller-expected version checked on connection; device revisions NOT_TESTED',
            'supported_command':'single-turn position, simulated transport only',
            'physical_path':'explicit SDK read-only connection; best-effort torque-disable stop'}
            for (model,protocol),p in PROFILES.items()],
        'custom_devices':'Implement Device protocol; physical safety validation remains equipment-specific'}

def main():print(json.dumps(capabilities(),indent=2))
if __name__=='__main__':main()
