"""Trusted live control loop; ALL displayed poses come from actual mj_step outputs."""
import json,math,time
from pathlib import Path
from .projects import read,write

def run(directory,seconds):
    import mujoco,numpy as np
    from .worker import validate
    folder=Path(directory);xml,c,req=validate(folder)
    model=mujoco.MjModel.from_xml_string(xml);data=mujoco.MjData(model)
    arm=c['kind']=='joint_pd/1'
    if (model.nq,model.nv,model.nu)!=((2,2,2) if arm else (9,8,2)):raise ValueError('Unsupported live robot shape')
    if not np.all(model.actuator_ctrllimited) or np.max(np.abs(model.actuator_ctrlrange))>(2 if arm else 15):raise ValueError('Actuator command limits required')
    if not arm and (not np.all(model.actuator_forcelimited) or np.max(np.abs(model.actuator_forcerange))>.4):raise ValueError('Wheel force limits required')
    dt=float(model.opt.timestep)
    if not .001<=dt<=.005 or abs(.02/dt-round(.02/dt))>1e-8:raise ValueError('Live timestep must divide20ms')
    start=time.monotonic();ticks=0;reason='TIME_LIMIT';last_target=np.array(c.get('target_rad',[0,0]),dtype=float);paused=False
    with (folder/'trace.jsonl').open('w',encoding='utf-8') as trace:
        while time.monotonic()-start<seconds:
            command=read(folder/'control.json');age=time.monotonic()-command['received'];fresh=age<.35
            if command['stop']:reason='USER_STOP';break
            if age>2:reason='CONTROLLER_DISCONNECTED';break
            paused=command['paused']
            if not paused:
                previous_time=float(data.time)
                if arm:last_target=np.array(command['target']) if fresh else data.qpos.copy()
                for _ in range(round(.02/dt)):
                    if arm:data.ctrl[:]=np.clip(c['kp']*(last_target-data.qpos)-c['kd']*data.qvel,model.actuator_ctrlrange[:,0],model.actuator_ctrlrange[:,1])
                    else:data.ctrl[:]=np.clip(command['wheels'] if fresh else [0,0],-min(12,c['max_wheel_rad_s']),min(12,c['max_wheel_rad_s']))
                    mujoco.mj_step(model,data)
                    if not np.all(np.isfinite(data.qpos)):raise RuntimeError('Nonfinite solver state')
                if sum(int(w.number) for w in data.warning) or abs(float(data.time)-previous_time-.02)>1e-7:raise RuntimeError('Solver reset or numerical warning; interactive session stopped')
            row={'time_s':float(data.time),'wall_s':time.monotonic()-start,'seq':command['seq'],'paused':paused,'controller_fresh':fresh,'contacts':int(data.ncon),'ctrl':data.ctrl.tolist()}
            if arm:row.update(q1_rad=float(data.qpos[0]),q2_rad=float(data.qpos[1]))
            else:row.update(x_m=float(data.qpos[0]),y_m=float(data.qpos[1]),z_m=float(data.qpos[2]),qw=float(data.qpos[3]),qx=float(data.qpos[4]),qy=float(data.qpos[5]),qz=float(data.qpos[6]),left_rad=float(data.qpos[7]),right_rad=float(data.qpos[8]))
            write(folder/'state.json',row);trace.write(json.dumps(row,allow_nan=False)+'\n');trace.flush();ticks+=1
            time.sleep(max(0,start+ticks*.02-time.monotonic()))
    write(folder/'summary.json',{'reason':reason,'simulation_time_s':float(data.time),'frames':ticks,'solver':{'name':'MuJoCo','version':mujoco.__version__},'requirements':'NOT_EVALUATED','physical':'NOT_TESTED','warnings':sum(int(w.number) for w in data.warning)})
