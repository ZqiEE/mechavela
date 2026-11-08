"""Property-only AIGER specialization: transitions and constraints unchanged."""
def split_properties(data):
 header,rest=data.split(b'\n',1);v=header.decode().split()
 if v[0]!='aig' or len(v)!=10:raise ValueError('requires binary AIGER1.9 explicit header')
 nums=list(map(int,v[1:]));M,I,L,O,A,B,C,J,F=nums
 if O or J or F or B<1:raise ValueError('unsupported liveness/ordinary outputs')
 lines=[];pos=0
 for _ in range(L+B+C):
  end=rest.index(b'\n',pos)+1;lines.append(rest[pos:end]);pos=end
 and_start=pos
 for _ in range(2*A):
  while True:
   byte=rest[pos];pos+=1
   if not byte&128:break
 and_bytes=rest[and_start:pos]
 latches=b''.join(lines[:L]);bad=lines[L:L+B];constraints=b''.join(lines[L+B:])
 result=[]
 for index,lit in enumerate(bad):
  n=nums.copy();n[5]=1
  specialized=('aig '+' '.join(map(str,n))+'\n').encode()+latches+lit+constraints+and_bytes+b'c\nATOMS property-only specialization\n'
  result.append((index,int(lit),specialized))
 return result,{'inputs':I,'latches':L,'ands':A,'properties':B,'constraints':C,'transition_bytes':latches+and_bytes,'constraint_bytes':constraints}
