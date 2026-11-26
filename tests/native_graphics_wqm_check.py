"""Check whole-wave zero and pixel coverage using source-built guest shaders."""
import struct
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from native_direct_container_check import patched_pixel, signature
from native_scalar_lds_check import sop1, vmove


def packed(lo, hi, narrow=False):
    move = 3 if narrow else 4
    wqm = 9 if narrow else 10
    # Both paths are scalar until the original coverage is restored.
    # Observe the actual expanded words and which direction EXECZ took.
    # The harness supplies zero in b0. The front end and DXIL optimizer
    # must still evaluate both words from runtime data at draw time.
    def xor_root_word(dst, root):
        return 0x80000000 | (0x12 << 23) | (dst << 16) | (root << 8) | dst
    seq = [sop1(move,20,126), sop1(3,4,255),lo, sop1(3,5,255),hi,
           xor_root_word(4,0), xor_root_word(5,1),
           sop1(3,10,128), sop1(3,11,128), sop1(3,12,129),
           sop1(wqm,126,4),0xbf880002,sop1(move,10,126),sop1(3,12,130),
           sop1(move,126,20)]
    # Byte-sized values remain exactly representable in the float target.
    seq += [vmove(0,10),vmove(1,11),vmove(2,12),vmove(3,10)]
    for v in (0,1,3):
        seq += [(0x16<<25) | (v<<17) | (v<<9) | (152 if v==3 else 128),
                (0x1b<<25) | (v<<17) | (v<<9) | 255,255]
    for v in range(4):
        seq += [0x7e000c00 | (v<<17) | (256+v)]
    return patched_pixel(len(seq),seq,constants=(0,2),wave32=narrow,export=(0xf800180f,0x03020100),
                         input_signature=((b"OSG1",signature(b"SV_Target",64)),))


def raster_program(limit=None, *, lower=None, cross=False):
    # Drop coverage where limit < SV_Position.x. In a surviving quad
    # the horizontal peer's coordinate must remain available, even if its
    # own coverage was dropped. An empty quad branches to EXP_NULL.
    seq=[sop1(4,12,126), sop1(10,126,126)]
    attributes=()
    if limit is None:
        # Perspective-center P1/P2 of attr3.z, followed by 0 > v5.
        p1=0xc8000000 | (5<<18) | (3<<10) | (2<<8)
        seq += [p1, p1 | (1<<16) | 1, 0x7c080000 | (5<<9) | 128]
        attributes=((6,0,15,3,0,0,0,0),)
        inputs, input_address, value = signature(b"TEXCOORD"), 2, 5
    else:
        threshold=struct.unpack("<I",struct.pack("<f",limit))[0]
        seq += [0x7c0200ff,threshold]
        inputs, input_address, value = signature(b"SV_Position",1), 1<<8, 0
    branches=[]
    def filter_and_test():
        seq.extend([0x8a8c6a0c, sop1(10,126,12)])
        branches.append(len(seq))
        seq.append(0)
    filter_and_test()
    if lower is not None:
        assert limit is not None
        seq += [0x7c0800ff,struct.unpack("<I",struct.pack("<f",lower))[0]]
        filter_and_test()
    seq += [vmove(2,250), 0xff040000 | ((0x141 if cross else 0xb1)<<8) | value,sop1(4,126,12)]
    if limit is None:
        # The interpolator may round the fractional input by one ULP. Move
        # it onto a coarser float grid before the bit-exact color comparison.
        seq += [(3<<25) | (2<<17) | (2<<9) | 255, 0x41800000]
    for branch in branches:
        seq[branch] = 0xbf880000 | (len(seq)+3-(branch+1))  # after color export and END
    return patched_pixel(len(seq),seq,input_address=input_address,attributes=attributes,
                         input_signature=((b"ISG1",inputs),(b"OSG1",signature(b"SV_Target",64))),
                         export=(0xf800180f,0x02020202),tail=(0xf8001890,0x80808080,0xbf810000))


def quad_mask(word):
    return sum(15<<shift for shift in range(0,32,4) if word & (15<<shift))


def main(cli, raster, sdk):
    with TemporaryDirectory() as tmp:
        work=Path(tmp)
        source, vs, ps = work/"source", work/"vs.cso", work/"ps.cso"
        source.write_text("float4 main(uint i:SV_VertexID):SV_Position {"
                          "return float4(i==1?3:-1,i==2?3:-1,0,1);}")
        subprocess.run([Path(sdk)/"dxc.exe","-T","vs_6_6","-Fo",vs,source],check=True)
        def compile_guest(blob):
            source.write_bytes(blob)
            return subprocess.run([cli,sdk,work/"cache","gfx1013-container-ps",source,ps],
                                  capture_output=True,text=True)
        def draw(blob, pixels):
            result=compile_guest(blob)
            assert not result.returncode,result.stderr
            expected=work/"expected.bin"
            expected.write_bytes(b"".join(struct.pack("<4f",*pixel) for pixel in pixels))
            result=subprocess.run([raster,"hardware","--startup-pixel-words",vs,ps,"8","2",expected],
                                  capture_output=True,text=True)
            if result.returncode==77: sys.exit(77)
            assert result.returncode==0,(result.stdout,result.stderr)
            print(result.stdout.strip())
        for lo,hi,narrow in ((0,0,False),(0,1,False),(0x80000000,0,False),
                             (0x80000021,0x42,False),(0,1,True),(0x22,0,True)):
            a,b=quad_mask(lo),0 if narrow else quad_mask(hi)
            pixel=(a&255,b&255,2 if a or b else 1,a>>24)
            draw(packed(lo,hi,narrow),[pixel]*16)
            print(f"wave{32 if narrow else 64} {hi:08x}:{lo:08x} -> {pixel}")
        for limit in (0,0.5,2.5,9):
            values=[(float((x^1)+0.5) if x+0.5<=limit else -1.0,)*4 for y in range(2) for x in range(8)]
            draw(raster_program(limit),values)
            print(f"filtered coverage limit={limit}: {sum(x[0]!=-1.0 for x in values)}/16 pixels survived")
        for lower in (1.5,4.5):
            values=[(float((x^1)+0.5) if lower<=x+0.5<=2.5 else -1.0,)*4 for y in range(2) for x in range(8)]
            draw(raster_program(2.5,lower=lower),values)
            print(f"two coverage filters [{lower},2.5]: {sum(x[0]!=-1.0 for x in values)}/16 pixels survived")
        rejected=compile_guest(raster_program(2.5,cross=True))
        assert rejected.returncode and "complete guest vote or a quad-local continuation" in rejected.stderr,rejected.stderr
        # The pixel program now consumes an interpolated value produced by
        # the VS, including a negative coordinate from a WQM helper.
        source.write_text("struct Out { float4 tc:TEXCOORD; float4 pos:SV_Position; };"
                          "Out main(uint i:SV_VertexID) { Out o;"
                          "o.pos=float4(i==1?3:-1,i==2?3:-1,0,1);"
                          "o.tc=float4(0,0,3.25-(o.pos.x+1)*4,0); return o; }")
        subprocess.run([Path(sdk)/"dxc.exe","-T","vs_6_6","-Fo",vs,source],check=True)
        values=[(16+3.25-((x^1)+0.5) if x+0.5<=3.25 else -1.0,)*4 for y in range(2) for x in range(8)]
        draw(raster_program(),values)
        print("attr3.z perspective interpolation, VCC filter and WQM/EXECZ: all 16 pixels matched")


if __name__=="__main__":
    main(*sys.argv[1:])
