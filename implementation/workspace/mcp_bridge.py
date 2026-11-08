"""Official MCP SDK stdio bridge to the ONE running RLSOK application. No model tokens."""
import argparse,json,sys,time,urllib.request,urllib.error,urllib.parse
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'console'))
ADDON=ROOT/'thirdparty/mcp/site-packages'
if not ADDON.exists():ADDON=ROOT/'packaging/build_mcp/site-packages'
sys.path.insert(0,str(ADDON))
import site
site.addsitedir(str(ADDON))  # Normal package .pth processing (notably pywin32 on Windows).

def main():
    ap=argparse.ArgumentParser(__doc__);ap.add_argument('--grant-file',required=True);a=ap.parse_args()
    import system
    def load():
        blob=Path(a.grant_file).read_bytes()
        if not blob.startswith(b'DPAPI1'):raise ValueError('Use a connection created in RLSOK on this Windows account')
        cfg=json.loads(system._dpapi(blob[6:],False));u=urllib.parse.urlparse(cfg['url'])
        if u.scheme!='http' or u.hostname not in ('127.0.0.1','localhost','::1') or u.username or u.password or u.query or u.fragment or u.path not in ('','/'):raise ValueError('Local application endpoint required')
        return cfg
    load()  # Validate the local protected endpoint before exposing tools.
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations
    from mcp.server.mcpserver.exceptions import ToolError
    from pydantic import BaseModel,ConfigDict
    class Args(BaseModel):model_config=ConfigDict(extra='forbid')
    class ReadAsset(Args):name:str
    class WriteAsset(Args):name:str;text:str;expected_revision:str
    class Build(Args):design:str;expected_revision:str
    class CodeFiles(Args):files:str;expected_revision:str
    class CodeRun(Args):tool:str;arguments:str='[]'
    class ReadRun(Args):run:str;name:str
    class Run(Args):run:str
    class Start(Args):adapter:str|None=None
    class InspectDesign(Args):source_run:str|None=None
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect)
    def invoke(tool,args):
        cfg=load()
        try:
            if cfg.get('refresh') and cfg.get('expires',0)<time.time()+60:
                req=urllib.request.Request(cfg['url']+'/api/workspace/mcp-refresh',data=json.dumps({'connection':cfg['id']}).encode(),headers={'Content-Type':'application/json','X-RLSOK':'1','X-RLSOK-Refresh':cfg['refresh']})
                try:
                    with opener.open(req,timeout=20) as response:json.load(response)
                except urllib.error.HTTPError as error:
                    # Another bridge may just have rotated this same protected file.
                    fresh=load()
                    if error.code!=403 or fresh.get('refresh')==cfg['refresh']:raise
                cfg=load()
            req=urllib.request.Request(cfg['url']+'/api/workspace/mcp-call',data=json.dumps({'connection':cfg['id'],'tool':tool,'arguments':args}).encode(),headers={'Content-Type':'application/json','X-RLSOK':'1','X-RLSOK-MCP':cfg['token']})
            with opener.open(req,timeout=40) as response:return json.load(response)
        except urllib.error.HTTPError as error:
            raise ToolError(json.loads(error.read(4096)).get('error','RLSOK denied this operation')) from None
        except OSError:raise ToolError('RLSOK is unreachable. Reopen the same application at its configured port; project trust is preserved. This action was NOT automatically retried; inspect project/run state before resubmitting.') from None
    mcp=MCPServer('Mechavela Robotics Workspace')
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(destructiveHint=False,openWorldHint=False))
    def inspect_design(arguments:InspectDesign)->dict[str,object]:
        """Queue native OCCT STEP validity/static interference and MuJoCo compiled geometry inspection. source_run selects immutable outputs of a completed project code build; omit to inspect saved model only. Requires simulate. Poll run_status/read_run. No project Python is executed. CAD/visualization is not motion or manufacturing acceptance."""
        return invoke('inspect_design',arguments.model_dump())
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False))
    def inspect_project()->dict[str,object]:
        """Inspect the authorized project and supported tools. Geometry preview is not physics."""
        return invoke('inspect_project',{})
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(destructiveHint=False,openWorldHint=False))
    def write_code(arguments:CodeFiles)->dict[str,object]:
        """Create/edit nested project source/model/tool files under code/. Requires edit_code. Owner requirements are protected."""
        return invoke('write_code',arguments.model_dump())
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(destructiveHint=True,openWorldHint=True))
    def run_code(arguments:CodeRun)->dict[str,object]:
        """Run any project-declared Python algorithm/tool using installed libraries in owner-approved TRUSTED_LOCAL environment. Has Windows-user filesystem/network access; resource bounds are NOT a security sandbox. Poll run_status and read_run for real output. No independent engineering PASS."""
        return invoke('run_code',arguments.model_dump())
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False))
    def read_asset(arguments:ReadAsset)->dict[str,object]:
        """Read a native project file; no arbitrary filesystem access."""
        return invoke('read_asset',arguments.model_dump())
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(destructiveHint=False,openWorldHint=False))
    def write_asset(arguments:WriteAsset)->dict[str,object]:
        """Revise an authorized design. Requirements, goals and controller kind are protected."""
        return invoke('write_asset',arguments.model_dump())
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(destructiveHint=False,openWorldHint=False))
    def build_assembly(arguments:Build)->dict[str,object]:
        """Generate original editable native geometry/controller from design JSON. Inspect assembly_authoring first. Owner targets/criteria/permissions remain fixed; no solver or model is hidden here."""
        return invoke('build_assembly',arguments.model_dump())
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(destructiveHint=False,openWorldHint=False))
    def build_motion(arguments:Build)->dict[str,object]:
        """Author original geometry and cyclic motion data; inspect motion_authoring. Owner criteria/counts are protected, no robot template is supplied."""
        return invoke('build_motion',arguments.model_dump())
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(destructiveHint=False,openWorldHint=False))
    def build_system(arguments:Build)->dict[str,object]:
        """Author original geometry/named position-velocity blocks. Inspect system_authoring; frozen owner tasks and permissions remain unchanged."""
        return invoke('build_system',arguments.model_dump())
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False))
    def system_diagnostics(arguments:Run)->dict[str,object]:
        """Actual coupled base/payload-point/channel samples and criteria; no supplied repair answer."""
        return invoke('system_diagnostics',arguments.model_dump())
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False))
    def motion_diagnostics(arguments:Run)->dict[str,object]:
        """Actual sampled CSV joint/base/foot observations for a completed trajectory run. No generated diagnosis or canned response."""
        return invoke('motion_diagnostics',arguments.model_dump())
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(destructiveHint=False,openWorldHint=False))
    def start_simulation(arguments:Start)->dict[str,object]:
        """Queue a real bounded engineering run. Poll run_status; completion may fail requirements."""
        return invoke('start_simulation',{k:v for k,v in arguments.model_dump().items() if v is not None})
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False))
    def run_status(arguments:Run)->dict[str,object]:
        """Actual execution state, measurements, failure details and resource receipts."""
        return invoke('run_status',arguments.model_dump())
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(destructiveHint=False,openWorldHint=False))
    def cancel_run(arguments:Run)->dict[str,object]:
        """Request cancellation of a run in this project; does not erase evidence."""
        return invoke('cancel_run',arguments.model_dump())
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False))
    def read_run(arguments:ReadRun)->dict[str,object]:
        """Read an actual output/log excerpt with its complete file hash."""
        return invoke('read_run',arguments.model_dump())
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False))
    def verify_evidence()->dict[str,object]:
        """Independent trace/criteria/integrity checks; no hardware/manufacturing certification."""
        return invoke('verify_evidence',{})
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(destructiveHint=False,openWorldHint=False))
    def export_project()->dict[str,object]:
        """Create a real native project and evidence ZIP snapshot, returning its path/hash."""
        return invoke('export_project',{})
    mcp.run()

if __name__=='__main__':
    try:main()
    except Exception as error:
        print('RLSOK MCP could not start: '+type(error).__name__,file=sys.stderr);raise SystemExit(1)
