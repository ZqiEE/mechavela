"""python -m implementation.workspace --root PATH reference|list|export|run|inspect"""
import argparse
import json
from pathlib import Path
from .projects import Projects, write
from .reference import create
from .runtime import Runtime, capabilities
from .product import storage_root

def main():
    ap = argparse.ArgumentParser(__doc__)
    ap.add_argument('--root', default=str(storage_root()/'robot_projects'))
    ap.add_argument('command', choices=['reference', 'component-create', 'mobile-create', 'bird-create', 'list', 'export', 'run', 'inspect', 'capabilities', 'verify'])
    ap.add_argument('--project')
    ap.add_argument('--out')
    args = ap.parse_args()
    store = Projects(args.root)
    if args.command == 'capabilities':
        value = capabilities()
    elif args.command=='bird-create':
        from .bird import create as bird_create
        value=bird_create(store)
    elif args.command == 'component-create':
        from .components import create as component_create
        value = component_create(store)
    elif args.command=='mobile-create':
        from .mobile import create as mobile_create
        value=mobile_create(store)
    elif args.command == 'verify':
        from .evidence import verify
        value = verify(store, args.project)
    elif args.command == 'list':
        value = store.list()
    elif args.command == 'inspect':
        value = store.get(args.project)
    elif args.command == 'export':
        if not args.out:
            ap.error('--out required')
        Path(args.out).write_bytes(store.export(args.project)); value = {'export': args.out}
    else:
        runtime = Runtime(store)
        try:
            if args.command == 'reference':
                p = create(store)
                first = runtime.submit(p['id']); failed = runtime.wait(p['id'], first['id'])
                if failed.get('result', {}).get('passed') is not False:
                    raise RuntimeError('Expected initial requirement failure; inspect evidence')
                # Explicit deterministic reference policy, never represented as an LLM decision.
                controller = json.loads(store.asset(p['id'], 'controller.json'))
                controller.update(kp=8., kd=1.2)
                store.revise(p['id'], {'controller.json': json.dumps(controller)},
                             'Reference policy: increase PD gains after observed positioning failure')
                second = runtime.submit(p['id']); revised = runtime.wait(p['id'], second['id'])
                value = {'project': p['id'], 'initial_run': failed, 'revised_run': revised,
                         'iteration_author': 'deterministic reference policy; not an autonomous model claim'}
                if args.out:
                    Path(args.out).mkdir(parents=True, exist_ok=True)
                    write(Path(args.out) / 'evidence.json', value)
                    (Path(args.out) / 'robot-project.zip').write_bytes(store.export(p['id']))
                if not revised.get('result', {}).get('passed'):
                    print(json.dumps(value, indent=2)); return 2
            else:
                run = runtime.submit(args.project)
                value = runtime.wait(args.project, run['id'])
        finally:
            runtime.close()
    print(json.dumps(value, ensure_ascii=False, indent=2))
    if args.command == 'run' and not value.get('result', {}).get('passed'):
        return 2
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
