"""Exercise the shared-memory frontend's reconvergence and rejection paths."""
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools/startup"))
from compile_lds_request import ComputeStage
from gfx2hlsl import Unsupported


barrier = "GroupMemoryBarrierWithGroupSync();"
blocks = [(0,[],("branch","(exec & 1) == 0",16)), (8,["v1 = 2;"],None),
          (16,[barrier],("end",))]
source = "\n".join(ComputeStage.body(SimpleNamespace(blocks=blocks)))
assert "!WaveActiveAnyTrue((exec&1u)!=0u)" in source
assert "}\n"+barrier+"\nif(pc==" in source

for invalid, expected in [
    ([(0,[],("branch","scc",16)),(8,[barrier],None),(16,[],("end",))],"conditional"),
    ([(0,[barrier],("jump",0))],"forward"),
]:
    try:
        ComputeStage.body(SimpleNamespace(blocks=invalid))
    except Unsupported as error:
        assert expected in str(error), error
    else:
        raise AssertionError("accepted invalid compute control flow")
print("uniform barrier and wave-level EXEC branch passed; divergent barrier and back edge rejected")
