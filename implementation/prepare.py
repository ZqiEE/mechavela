import json,hashlib,re,os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
 target=Path(os.environ.get('ATOMS_PHYSICAL_HOME','/opt/atoms-g06'))/'design';target.mkdir(parents=True,exist_ok=True)
 original=ROOT/'evidence/G05/source-burst-2/hw/rtl/libs/VX_gto_arbiter.sv'
 if original.exists():source=original.read_text()
 else:
  from gpu.make_g05_patch import generate
  source=generate(2)[1]['hw/rtl/libs/VX_gto_arbiter.sv']
 text=source.replace('`include "VX_platform.vh"','''`define LOG2UP(x) (($clog2(x) > 0) ? $clog2(x) : 1)
`define TRACING_OFF
`define TRACING_ON
`define UNUSED_VAR(x)
''')
 (target/'arbiter-original.sv').write_text(text)
 text=re.sub(r'for\s*\(genvar\s+(\w+)',r'for (\1',text)
 text=re.sub(r'(\w+)\+\+',r'\1 = \1 + 1',text)
 text=re.sub(r'\+\+(\w+)',r'\1 = \1 + 1',text)
 text=text.replace("CW'(LIMIT)",'LIMIT[CW-1:0]').replace("CW'(1)","{{(CW-1){1'b0}},1'b1}").replace("N'(0)","{N{1'b0}}")
 text=text.replace('    if (NUM_REQS == 1)', '    genvar i, j, k;\n    if (NUM_REQS == 1)',1)
 (target/'arbiter.sv').write_text(text)
 wrapper='''module atoms_issue_select #(parameter POLICY=0) (
 input wire clk,reset,
 input wire [7:0] requests,base_suppress,
 input wire [23:0] ex_types,
 input wire grant_ready,
 output reg valid,
 output reg [7:0] onehot,
 output reg [2:0] index,selected_ex_type
);
 reg [7:0] requests_r,base_r;
 reg [23:0] ex_r;
 reg ready_r;
 wire gvalid;
 wire [7:0] gonehot,mask,memory;
 wire [2:0] gindex;
 for(genvar w=0;w<8;w=w+1) begin
  assign memory[w]=(ex_r[w*3+:3]==3'd1);
 end
 if(POLICY) begin
  VX_atoms_lsu_policy #(.N(8),.LIMIT(2)) p (
   .clk(clk),.reset(reset),.requests(requests_r),.base_suppress(base_r),.memory(memory),
   .grant_fire(gvalid && ready_r),.grant_memory(memory[gindex]),.policy_suppress(mask));
 end else begin
  assign mask=base_r;
 end
 VX_gto_arbiter #(.NUM_REQS(8)) a (.clk(clk),.reset(reset),.requests(requests_r),.suppress(mask),
   .grant_valid(gvalid),.grant_index(gindex),.grant_onehot(gonehot),.grant_ready(ready_r));
 always @(posedge clk) begin
  if(reset) begin
   requests_r<=0;base_r<=0;ex_r<=0;ready_r<=0;valid<=0;onehot<=0;index<=0;selected_ex_type<=0;
  end else begin
   requests_r<=requests;base_r<=base_suppress;ex_r<=ex_types;ready_r<=grant_ready;
   valid<=gvalid;onehot<=gonehot;index<=gindex;selected_ex_type<=ex_r[gindex*3+:3];
  end
 end
endmodule
'''
 (target/'top-original.sv').write_text(wrapper)
 wrapper=wrapper.replace('for(genvar w','for(w').replace(' reg [7:0] requests_r,base_r;',' genvar w;\n reg [7:0] requests_r,base_r;')
 (target/'top.sv').write_text(wrapper)
 constraints='''create_clock -name core_clock -period 5.0 [get_ports clk]
set_clock_uncertainty 0.05 [get_clocks core_clock]
set_input_delay 0.5 -clock core_clock [get_ports {requests* base_suppress* ex_types* grant_ready reset}]
set_output_delay 0.5 -clock core_clock [all_outputs]
set_input_transition 0.1 [all_inputs]
set_load 0.01 [all_outputs]
'''
 (target/'constraints.sdc').write_text(constraints)
 hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in target.iterdir() if p.is_file()}
 (ROOT/'evidence/G06').mkdir(parents=True,exist_ok=True)
 (ROOT/'evidence/G06/design.lock.json').write_text(json.dumps({'scope':'module','requests':8,'ex_bits':3,'candidate':'G05 A burst2','hashes':hashes,
  'normalization':'platform include replaced by LOG2UP and empty tracing/unused macros; inline genvar declarations hoisted for Yosys0.9; verify cycle-equivalence before acceptance',
  'boundary_context':'common input/output registers, includes selected ex_type output so original type data stays live in baseline too; not full scoreboard'},indent=2))
 print(json.dumps(hashes))
if __name__=='__main__':main()
