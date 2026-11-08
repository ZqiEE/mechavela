"""Offline per-sample checks. Numerical consistency is not hardware certification."""
import csv
import math
from .projects import read

def verify_components(folder, result, controller, req):
    p=read(folder/'actuators.json'); groups={}
    with (folder/'scenarios.csv').open() as f:
        for row in csv.DictReader(f):
            key=(row['scenario'],float(row['dt_s']))
            groups.setdefault(key,[]).append({k:float(v) for k,v in row.items() if k!='scenario'})
    expected={(s['id'],dt) for s in p['scenarios'] for dt in p['timesteps_s']}
    if set(groups)!=expected:raise ValueError('Scenario coverage mismatch')
    kt,resistance=p['motor']['params']['kt'],p['motor']['params']['R']
    def close(a,b):
        if not math.isfinite(a) or not math.isfinite(b) or abs(a-b)>1e-9*max(1,abs(b)):
            raise ValueError('Component trace measurement mismatch')
    summaries={}
    for key,rows in groups.items():
        dt=key[1]
        if len(rows)!=round(req['duration_s']/dt):raise ValueError('Incomplete scenario samples')
        energy=0.;temp=[p['thermal']['ambient_C']]*2
        peaks={'peak_current_A':0.,'peak_speed_rad_s':0.,'peak_supply_current_A':0.,
               'peak_command_Nm':0.,'max_temperature_C':p['thermal']['ambient_C']}
        for index,row in enumerate(rows):
            if any(not math.isfinite(v) for v in row.values()):raise ValueError('Nonfinite scenario trace')
            close(row['time_s'],(index+1)*dt);terminal_power=0.
            for j in (1,2):
                current,voltage=row[f'current{j}_A'],row[f'voltage{j}_V']
                close(row[f'torque{j}_Nm'],kt*current)
                close(voltage,current*resistance+kt*row[f'pre_speed{j}_rad_s'])
                if abs(voltage)>p['supply_voltage_V']+1e-9 or abs(current)>p['max_current_A']+1e-9:
                    raise ValueError('Stored sample violates electrical envelope')
                terminal_power+=max(voltage*current,0)
                equilibrium=p['thermal']['ambient_C']+current*current*resistance*p['thermal']['r_th_K_W']
                temp[j-1]=equilibrium+(temp[j-1]-equilibrium)*math.exp(-dt/p['thermal']['tau_s'])
                close(row[f'temp{j}_C'],temp[j-1])
                peaks['peak_current_A']=max(peaks['peak_current_A'],abs(current))
                peaks['peak_speed_rad_s']=max(peaks['peak_speed_rad_s'],abs(row[f'dq{j}_rad_s']))
                peaks['peak_command_Nm']=max(peaks['peak_command_Nm'],abs(row[f'torque{j}_Nm']))
                peaks['max_temperature_C']=max(peaks['max_temperature_C'],temp[j-1])
            power=terminal_power/p['driver_efficiency']+p['electronics_power_W'];energy+=power*dt
            close(row['input_power_W'],power);close(row['input_energy_J'],energy)
            peaks['peak_supply_current_A']=max(peaks['peak_supply_current_A'],power/p['supply_voltage_V'])
        peaks['input_energy_J']=energy
        peaks['final_joint_error_rad']=max(abs(rows[-1][f'q{j}_rad']-controller['target_rad'][j-1]) for j in (1,2))
        summaries[key]=peaks
    if len(result['scenarios'])!=len(expected):raise ValueError('Scenario summary count mismatch')
    for s in result['scenarios']:
        for key,value in summaries[(s['scenario'],s['dt_s'])].items():close(s['measured'][key],value)
    measured={key:max(s[key] for s in summaries.values()) for key in next(iter(summaries.values()))}
    coarse,fine=p['timesteps_s']
    measured['final_error_step_delta_rad']=max(abs(summaries[(s['id'],coarse)]['final_joint_error_rad']-
        summaries[(s['id'],fine)]['final_joint_error_rad']) for s in p['scenarios'])
    for key,value in measured.items():close(result['measured'][key],value)
    limits={'final_joint_error_rad':req['maximum'],**req['component_limits']}
    for key,limit in limits.items():
        check=result['checks'][key];close(check['maximum'],limit);close(check['measured'],measured[key])
        if check['passed']!=(measured[key]<=limit):raise ValueError('Component criterion mismatch')
    expected_pass=all(measured[k]<=v for k,v in limits.items()) and not result['warnings']
    if result['passed']!=expected_pass or result['levels']['requirements']!=('PASSED_CONDITIONAL' if expected_pass else 'FAILED'):
        raise ValueError('Component aggregate verdict mismatch')
    if result['thermal_status']!='ASSUMED_NOT_IDENTIFIED' or result['levels']['physical']!='NOT_TESTED':
        raise ValueError('Unsupported qualification claim')
    return {'scenarios':len(groups),'samples':sum(len(v) for v in groups.values())}
