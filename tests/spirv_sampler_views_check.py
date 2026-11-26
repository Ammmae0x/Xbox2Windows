"""Unit, comment-stripping, table-splitting, refusal, and temp-copy mutation suite for tools/spirv_sampler_views.py."""

import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "spirv_sampler_views.py"
sys.path.insert(0, str(ROOT / "tools"))

import spirv_sampler_views  # noqa: E402


BASE_MODULE = """\
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main" %texture %samplers %out %tid
OpExecutionMode %main LocalSize 1 1 1
OpDecorate %tid BuiltIn WorkgroupId
OpDecorate %texture DescriptorSet 0
OpDecorate %texture Binding 0
OpDecorate %samplers DescriptorSet 0
OpDecorate %samplers Binding 1
OpDecorate %out DescriptorSet 0
OpDecorate %out Binding 0
%void = OpTypeVoid
%fn = OpTypeFunction %void
%uint = OpTypeInt 32 0
%float = OpTypeFloat 32
%vec2 = OpTypeVector %float 2
%vec4 = OpTypeVector %float 4
%xyz = OpTypeVector %uint 3
%input = OpTypePointer Input %xyz
%tid = OpVariable %input Input
%zero = OpConstant %uint 0
%two = OpConstant %uint 2
%lod = OpConstant %float 0
%half = OpConstant %float 0.5
%uv = OpConstantComposite %vec2 %half %half
%img = OpTypeImage %float 2D 0 0 0 1 Unknown
%pair = OpTypeSampledImage %img
%smp = OpTypeSampler
%ptr_img = OpTypePointer UniformConstant %img
%texture = OpVariable %ptr_img UniformConstant
%samparr = OpTypeArray %smp %two
%_ptr_UC_rt_smp = OpTypePointer UniformConstant %samparr
%_ptr_UC_smp = OpTypePointer UniformConstant %smp
%samplers = OpVariable %_ptr_UC_rt_smp UniformConstant
%out_ptr = OpTypePointer StorageBuffer %uint
%out = OpVariable %out_ptr StorageBuffer
%main = OpFunction %void None %fn
%entry = OpLabel
%xyzval = OpLoad %xyz %tid
%i = OpCompositeExtract %uint %xyzval 0
%p = OpAccessChain %_ptr_UC_smp %samplers %i
%s = OpLoad %smp %p
%im = OpLoad %img %texture
%si = OpSampledImage %pair %im %s
%raw = OpImageSampleExplicitLod %vec4 %si %uv Lod %lod
%cmp = OpImageSampleDrefExplicitLod %float %si %uv %half Lod %lod
OpReturn
OpFunctionEnd
"""


MUTATIONS = (
    (
        "drop_non_str_guard",
        "    if not isinstance(assembly, str):\n"
        '        raise ValueError(f"assembly must be a str, got {type(assembly).__name__}")\n',
        "    if False:\n"
        '        raise ValueError(f"assembly must be a str, got {type(assembly).__name__}")\n',
    ),
    (
        "drop_register_end_safe_lookup",
        "    decl = defs.get(table, [])\n"
        "    pointer = defs.get(decl[1], []) if len(decl) >= 2 else []\n"
        "    shape = defs.get(pointer[2], []) if len(pointer) >= 3 else []\n"
        '    length = defs.get(shape[2], []) if len(shape) >= 3 and shape[0] == "OpTypeArray" else []\n',
        "    shape = defs[defs[defs[table][1]][2]]\n"
        '    length = defs.get(shape[2], []) if shape[0] == "OpTypeArray" else []\n',
    ),
    (
        "drop_query_lod_ordinary",
        '        elif words[2] == "OpImageQueryLod" or words[2].startswith(\n',
        "        elif words[2].startswith(\n",
    ),
    (
        "drop_indirect_decoration_guard",
        '    if module["indirect"]:\n',
        "    if False:\n",
    ),
    (
        "drop_bridge_set3_guard",
        "    if BRIDGE in assembly or set_three:\n",
        "    if False:\n",
    ),
    (
        "drop_overlap_check",
        "            if (stop is None or int(binding) < stop) and (end is None or start < end):\n",
        "            if False:\n",
    ),
)


def _sub(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise AssertionError(f"expected single occurrence of {old!r}")
    return text.replace(old, new)


def _load_temp_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not create module spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return mod


class SpirvSamplerViewsTests(unittest.TestCase):
    def _verify_module_contract(self, mod) -> None:
        self.assertEqual(mod.BRIDGE, "%bridge_cmp")
        self.assertEqual(mod.BRIDGE_SET, "3")

        # strip_comments with quoted ';', escaped quotes, and multi-line strings.
        comment_input = (
            'OpEntryPoint GLCompute %main "main;x" %samplers ; entry comment\n'
            '%s = OpString "quote \\" ; keep" ; trailing\n'
            '%multi = OpString "first line\n'
            'second;still_quoted" ; after multi\n'
            "%c = OpConstant %uint 1 ; plain comment"
        )
        self.assertEqual(
            mod.strip_comments(comment_input),
            [
                'OpEntryPoint GLCompute %main "main;x" %samplers',
                '%s = OpString "quote \\" ; keep"',
                '%multi = OpString "first line',
                'second;still_quoted"',
                "%c = OpConstant %uint 1",
            ],
        )

        # Non-str rejection in strip_comments and split_sampler_views.
        for bad in (None, 123, b"Dref", ["OpCapability Shader"]):
            with self.assertRaises(ValueError, msg=f"strip_comments({bad!r})"):
                mod.strip_comments(bad)
            with self.assertRaises(ValueError, msg=f"split_sampler_views({bad!r})"):
                mod.split_sampler_views(bad)

        # Single-table splitting preserves BRIDGE, BRIDGE_SET, OpEntryPoint, and Dref formatting.
        base_lines = BASE_MODULE.splitlines()
        cmp_index = base_lines.index("%cmp = OpImageSampleDrefExplicitLod %float %si %uv %half Lod %lod")
        split_lines = mod.split_sampler_views(BASE_MODULE).splitlines()
        self.assertIn(
            'OpEntryPoint GLCompute %main "main" %texture %samplers %out %tid %bridge_cmp',
            split_lines,
        )
        self.assertIn("OpDecorate %bridge_cmp DescriptorSet 3", split_lines)
        self.assertIn("OpDecorate %bridge_cmp Binding 1", split_lines)
        self.assertIn("%bridge_cmp = OpVariable %_ptr_UC_rt_smp UniformConstant", split_lines)
        self.assertIn("%raw = OpImageSampleExplicitLod %vec4 %si %uv Lod %lod", split_lines)
        prefix = f"%bridge_cmp_{cmp_index}"
        self.assertIn(f"{prefix}_ptr = OpAccessChain %_ptr_UC_smp %bridge_cmp %i", split_lines)
        self.assertIn(f"{prefix}_value = OpLoad %smp {prefix}_ptr", split_lines)
        self.assertIn(f"{prefix}_image = OpSampledImage %pair %im {prefix}_value", split_lines)
        self.assertIn(f"%cmp = OpImageSampleDrefExplicitLod %float {prefix}_image %uv %half Lod %lod", split_lines)

        # OpImageDrefGather rewrite preserves OpSampledImage + OpImageDrefGather formatting.
        dref_gather = _sub(
            BASE_MODULE,
            "%cmp = OpImageSampleDrefExplicitLod %float %si %uv %half Lod %lod",
            "%cmp = OpImageDrefGather %vec4 %si %uv %half",
        )
        gather_lines = mod.split_sampler_views(dref_gather).splitlines()
        self.assertIn(f"%cmp = OpImageDrefGather %vec4 {prefix}_image %uv %half", gather_lines)

        # OpImageQueryLod is classified as ordinary use and triggers split.
        query_lod = _sub(
            BASE_MODULE,
            "%raw = OpImageSampleExplicitLod %vec4 %si %uv Lod %lod",
            "%raw = OpImageQueryLod %vec2 %si %uv",
        )
        query_lines = mod.split_sampler_views(query_lod).splitlines()
        self.assertIn("%bridge_cmp = OpVariable %_ptr_UC_rt_smp UniformConstant", query_lines)
        self.assertIn("%raw = OpImageQueryLod %vec2 %si %uv", query_lines)

        # Multi-table splitting: two mixed tables + one comparison-only table.
        multi = _sub(
            BASE_MODULE,
            'OpEntryPoint GLCompute %main "main" %texture %samplers %out %tid',
            'OpEntryPoint GLCompute %main "main" %texture %samplers %second %cmp_only %out %tid',
        )
        multi = _sub(
            multi,
            "OpDecorate %samplers Binding 1",
            "OpDecorate %samplers Binding 1\n"
            "OpDecorate %second DescriptorSet 1\n"
            "OpDecorate %second Binding 4\n"
            "OpDecorate %cmp_only DescriptorSet 2\n"
            "OpDecorate %cmp_only Binding 9",
        )
        multi = _sub(
            multi,
            "%samplers = OpVariable %_ptr_UC_rt_smp UniformConstant",
            "%samplers = OpVariable %_ptr_UC_rt_smp UniformConstant\n"
            "%rt_smp = OpTypeRuntimeArray %smp\n"
            "%_ptr_UC_rt2 = OpTypePointer UniformConstant %rt_smp\n"
            "%second = OpVariable %_ptr_UC_rt2 UniformConstant\n"
            "%cmp_only = OpVariable %_ptr_UC_rt_smp UniformConstant",
        )
        multi = _sub(
            multi,
            "OpReturn",
            "%p2 = OpAccessChain %_ptr_UC_smp %second %i\n"
            "%s2 = OpLoad %smp %p2\n"
            "%si2 = OpSampledImage %pair %im %s2\n"
            "%raw2 = OpImageSampleExplicitLod %vec4 %si2 %uv Lod %lod\n"
            "%cmp2 = OpImageSampleDrefExplicitLod %float %si2 %uv %half Lod %lod\n"
            "%p3 = OpAccessChain %_ptr_UC_smp %cmp_only %i\n"
            "%s3 = OpLoad %smp %p3\n"
            "%si3 = OpSampledImage %pair %im %s3\n"
            "%cmp3 = OpImageSampleDrefExplicitLod %float %si3 %uv %half Lod %lod\n"
            "OpReturn",
        )
        multi_lines = mod.split_sampler_views(multi).splitlines()
        self.assertIn("%bridge_cmp = OpVariable %_ptr_UC_rt_smp UniformConstant", multi_lines)
        self.assertIn("%bridge_cmp_table_1 = OpVariable %_ptr_UC_rt2 UniformConstant", multi_lines)
        self.assertIn("OpDecorate %bridge_cmp Binding 1", multi_lines)
        self.assertIn("OpDecorate %bridge_cmp_table_1 Binding 4", multi_lines)
        self.assertFalse(any("Binding 9" in line and "bridge" in line for line in multi_lines))

        # Pass-through when no comparison or no mixed table.
        no_dref = _sub(BASE_MODULE, "%cmp = OpImageSampleDrefExplicitLod %float %si %uv %half Lod %lod\n", "")
        self.assertEqual(mod.split_sampler_views(no_dref), no_dref)

        # register_end handles missing OpTypePointer / OpTypeArray keys without KeyError.
        self.assertIsNone(mod.register_end({}, "%samplers", "1"))
        self.assertIsNone(mod.register_end({"%samplers": ["OpVariable", "%missing_ptr", "UniformConstant"]}, "%samplers", "1"))
        self.assertIsNone(
            mod.register_end(
                {
                    "%samplers": ["OpVariable", "%ptr", "UniformConstant"],
                    "%ptr": ["OpTypePointer", "UniformConstant", "%missing_arr"],
                },
                "%samplers",
                "1",
            )
        )
        self.assertIsNone(
            mod.register_end(
                {
                    "%samplers": ["OpVariable", "%ptr", "UniformConstant"],
                    "%ptr": ["OpTypePointer", "UniformConstant", "%arr"],
                    "%arr": ["OpTypeArray", "%smp"],
                },
                "%samplers",
                "1",
            )
        )
        self.assertEqual(
            mod.register_end(
                {
                    "%samplers": ["OpVariable", "%ptr", "UniformConstant"],
                    "%ptr": ["OpTypePointer", "UniformConstant", "%arr"],
                    "%arr": ["OpTypeArray", "%smp", "%two"],
                    "%two": ["OpConstant", "%uint", "2"],
                },
                "%samplers",
                "1",
            ),
            3,
        )

        # table_slot refuses missing table, missing/non-literal/duplicate/extra decorations.
        parsed = mod.read_module(mod.strip_comments(BASE_MODULE))
        self.assertEqual(mod.table_slot(parsed, "%samplers"), ("0", "1"))
        with self.assertRaises(ValueError):
            mod.table_slot(parsed, "%missing")

        # Refusals: indirect decorations, set 3, %bridge_cmp, overlap, malformed OpDecorate.
        indirect = _sub(
            BASE_MODULE,
            "OpDecorate %samplers Binding 1",
            "OpDecorate %samplers Binding 1\n%grp = OpDecorationGroup\nOpGroupDecorate %grp %raw",
        )
        with self.assertRaisesRegex(ValueError, "indirect"):
            mod.split_sampler_views(indirect)

        indirect_id = _sub(
            BASE_MODULE,
            "OpDecorate %samplers Binding 1",
            "OpDecorate %samplers Binding 1\nOpDecorateId %raw AlignmentId %two",
        )
        with self.assertRaisesRegex(ValueError, "indirect"):
            mod.split_sampler_views(indirect_id)

        for set3_line in ("OpDecorate %samplers DescriptorSet 3", "\tOpDecorate  %samplers  DescriptorSet 03"):
            with self.assertRaisesRegex(ValueError, "free descriptor set 3"):
                mod.split_sampler_views(_sub(BASE_MODULE, "OpDecorate %samplers DescriptorSet 0", set3_line))

        with self.assertRaisesRegex(ValueError, "bridge_cmp"):
            mod.split_sampler_views(
                _sub(BASE_MODULE, "%two = OpConstant %uint 2", "%two = OpConstant %uint 2\n%bridge_cmp = OpConstant %uint 9")
            )

        # Overlapping set 3 register range (binding 1 size 2 covers 1..2; second at binding 2 overlaps).
        overlap = _sub(multi, "OpDecorate %second Binding 4", "OpDecorate %second Binding 2")
        with self.assertRaisesRegex(ValueError, "overlap"):
            mod.split_sampler_views(overlap)

        # Malformed OpDecorate refusals.
        for bad_dec in (
            _sub(BASE_MODULE, "OpDecorate %samplers DescriptorSet 0\n", ""),
            _sub(BASE_MODULE, "OpDecorate %samplers Binding 1\n", ""),
            _sub(BASE_MODULE, "OpDecorate %samplers DescriptorSet 0", "OpDecorate %samplers DescriptorSet"),
            _sub(BASE_MODULE, "OpDecorate %samplers DescriptorSet 0", "OpDecorate %samplers DescriptorSet %bad"),
            _sub(BASE_MODULE, "OpDecorate %samplers Binding 1", "OpDecorate %samplers Binding -1"),
            _sub(BASE_MODULE, "OpDecorate %samplers Binding 1", "OpDecorate %samplers Binding 1 2"),
            _sub(
                BASE_MODULE,
                "OpDecorate %samplers Binding 1",
                "OpDecorate %samplers Binding 1\nOpDecorate %samplers Binding 2",
            ),
            _sub(
                BASE_MODULE,
                "OpDecorate %samplers Binding 1",
                "OpDecorate %samplers Binding 1\nOpDecorate %samplers NonUniformEXT",
            ),
        ):
            with self.assertRaises(ValueError):
                mod.split_sampler_views(bad_dec)

    def test_contract_and_refusals(self) -> None:
        self._verify_module_contract(spirv_sampler_views)

    def test_fast_temp_copy_mutations(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        for label, target, replacement in MUTATIONS:
            with self.subTest(mutation=label), TemporaryDirectory() as tmp:
                self.assertEqual(source.count(target), 1, target)
                mut_path = Path(tmp) / f"spirv_sampler_views_{label}.py"
                mut_path.write_text(source.replace(target, replacement, 1), encoding="utf-8")
                mod_name = f"spirv_sampler_views_mut_{label}"
                mod = _load_temp_module(mut_path, mod_name)
                try:
                    with self.assertRaises(
                        (AssertionError, KeyError, IndexError, AttributeError, TypeError),
                        msg=f"mutation {label} escaped detection",
                    ):
                        self._verify_module_contract(mod)
                finally:
                    sys.modules.pop(mod_name, None)


if __name__ == "__main__":
    unittest.main()
