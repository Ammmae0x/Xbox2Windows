"""CLI, path-guard, symlink, atomic-replace, and temp-copy mutation suite for tools/probe_root_devices.py."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "probe_root_devices.py"
sys.path.insert(0, str(ROOT / "tools"))

import probe_root_devices  # noqa: E402


IMAGE_BASE = 0x180000000
SECTION_RVA = 0x1000
RAW_OFFSET = 0x200
RAW_SIZE = 0x1000

MUTATIONS = (
    (
        "drop_bin_is_dir_guard",
        "        if not args.bin.is_dir():\n"
        '            raise NotADirectoryError(f"--bin path is not a directory: {args.bin}")\n',
        "        if False:\n"
        '            raise NotADirectoryError(f"--bin path is not a directory: {args.bin}")\n',
    ),
    (
        "drop_control_cso_is_file_guard",
        "            if not control_path.is_file():\n"
        '                raise FileNotFoundError(f"Control shader is missing or not a file: {control_path}")\n',
        "            if False:\n"
        '                raise FileNotFoundError(f"Control shader is missing or not a file: {control_path}")\n',
    ),
    (
        "drop_dest_is_symlink_guard",
        "        if dest.is_symlink():\n"
        '            raise OSError(f"Output path must not be a symlink: {dest}")\n',
        "        if False:\n"
        '            raise OSError(f"Output path must not be a symlink: {dest}")\n',
    ),
    (
        "drop_dest_is_dir_guard",
        "        if dest.is_dir():\n"
        '            raise IsADirectoryError(f"Output path is a directory: {dest}")\n',
        "        if False:\n"
        '            raise IsADirectoryError(f"Output path is a directory: {dest}")\n',
    ),
    (
        "drop_dest_inside_build_guard",
        "        if dest.resolve().is_relative_to(args.build.resolve()):\n"
        '            parser.error("Choose an input directory separate from the report directory")\n',
        "        if False:\n"
        '            parser.error("Choose an input directory separate from the report directory")\n',
    ),
    (
        "drop_tmp_unlink_cleanup",
        "            if temporary is not None:\n"
        "                temporary.unlink(missing_ok=True)\n",
        "            if False:\n"
        "                temporary.unlink(missing_ok=True)\n",
    ),
)


def _make_dxbc_rts0(rts0: bytes) -> bytes:
    program = struct.pack("<II4sIII", 0, 8, b"DXIL", 0, 16, 4) + b"BC\xc0\xde\x00\x00\x00\x00"
    parts = [
        b"DXIL" + struct.pack("<I", len(program)) + program,
        b"RTS0" + struct.pack("<I", len(rts0)) + rts0,
    ]
    table_end = 32 + 4 * len(parts)
    offsets = []
    cursor = table_end
    for part in parts:
        offsets.append(cursor)
        cursor += len(part)
    body = b"".join(parts)
    return (
        b"DXBC"
        + bytes(16)
        + struct.pack("<III", 1, table_end + len(body), len(parts))
        + struct.pack(f"<{len(parts)}I", *offsets)
        + body
    )


def _build_pe32_plus(containers: list[bytes]) -> tuple[bytes, list[tuple[int, int]]]:
    payload = bytearray(RAW_SIZE)
    cursor = 0
    layout = []
    for blob in containers:
        cursor = (cursor + 15) & ~15
        rva = SECTION_RVA + cursor
        payload[cursor:cursor + len(blob)] = blob
        cursor += len(blob)
        layout.append((rva, len(blob)))
    vsize = max(1, cursor)
    size_of_image = SECTION_RVA + ((vsize + 0xFFF) // 0x1000) * 0x1000
    pe_offset = 0x80
    buf = bytearray(RAW_OFFSET + RAW_SIZE)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 60, pe_offset)
    buf[pe_offset:pe_offset + 4] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", buf, pe_offset + 4, 0x8664, 1, 0, 0, 0, 240, 0x2022)
    opt = pe_offset + 24
    struct.pack_into(
        "<HBBIIIIIQIIHHHHHHIIIIHHQQQQII",
        buf,
        opt,
        0x20B, 0, 0, 0, RAW_SIZE, 0, 0, SECTION_RVA, IMAGE_BASE,
        0x1000, 0x200, 6, 0, 0, 0, 6, 0, 0, size_of_image, RAW_OFFSET,
        0, 3, 0, 0, 0, 0, 0, 0, 16,
    )
    sec = opt + 240
    struct.pack_into(
        "<8sIIIIIIHHI",
        buf,
        sec,
        b".rdata\0\0", vsize, SECTION_RVA, RAW_SIZE, RAW_OFFSET,
        0, 0, 0, 0, 0x40000040,
    )
    buf[RAW_OFFSET:RAW_OFFSET + RAW_SIZE] = payload
    return bytes(buf), layout


def _setup_bin(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "compute_root.cso").write_bytes(b"CTRL_ROOT")
    (bin_dir / "compute_tables.cso").write_bytes(b"CTRL_TABLES")


def _fake_probe_ok(cmd, *, input, stdout, check, timeout):
    warp_only = "--warp-only" in cmd
    row = {"cleared": 0, "remaining_flags": 0, "serialize": "0x00000000", "warp": "0x00000000"}
    if not warp_only:
        row["hardware"] = "0x00000000"
    return subprocess.CompletedProcess(cmd, 0, (json.dumps(row) + "\n").encode("utf-8"))


def _run_main(mod, argv: list[str], repo: Path, fake_run=_fake_probe_ok) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with (
        mock.patch.object(mod, "PROJECT", repo),
        mock.patch.object(mod.subprocess, "run", side_effect=fake_run),
        redirect_stdout(out),
        redirect_stderr(err),
    ):
        try:
            code = mod.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


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


class ProbeRootDevicesCliTests(unittest.TestCase):
    def _create_fixture(self, base: Path) -> tuple[Path, Path, Path, Path]:
        repo = base / "repo"
        calls = repo / "out" / "calls"
        calls.mkdir(parents=True)
        build_dir = base / "build"
        build_dir.mkdir()
        bin_dir = base / "bin"
        _setup_bin(bin_dir)

        rts0 = struct.pack("<6I", 2, 1, 24, 0, 0, 0)
        pe_bytes, layout = _build_pe32_plus([_make_dxbc_rts0(rts0)])
        names = probe_root_devices.NAMES
        (build_dir / names[0]).write_bytes(pe_bytes)
        inventory = {
            names[0]: {
                "sha256": hashlib.sha256(pe_bytes).hexdigest(),
                "containers": [{"rva": hex(layout[0][0]), "bytes": layout[0][1]}],
            },
            names[1]: {"sha256": "0" * 64, "containers": []},
            names[2]: {"sha256": "0" * 64, "containers": []},
        }
        (calls / "six-apis.json").write_text(json.dumps(inventory), encoding="utf-8")
        return repo, calls, build_dir, bin_dir

    def test_atomic_json_write_and_zero_leftover_temps(self) -> None:
        for warp_only in (True, False):
            with self.subTest(warp_only=warp_only), TemporaryDirectory() as tmp:
                repo, calls, build_dir, bin_dir = self._create_fixture(Path(tmp))
                argv = [str(build_dir), "--bin", str(bin_dir)]
                if warp_only:
                    argv.append("--warp-only")
                code, out, err = _run_main(probe_root_devices, argv, repo)
                self.assertEqual(code, 0, err)
                self.assertEqual(err, "")
                dest_name = "root-devices-warp.json" if warp_only else "root-devices.json"
                dest = calls / dest_name
                self.assertTrue(dest.is_file())
                self.assertIn(str(dest), out)
                self.assertEqual(sorted(p.name for p in calls.iterdir()), sorted([dest_name, "six-apis.json"]))
                report = json.loads(dest.read_text(encoding="utf-8"))
                self.assertIn(probe_root_devices.NAMES[0], report["images"])

    def test_upfront_bin_control_build_inventory_and_image_guards(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, calls, build_dir, bin_dir = self._create_fixture(base)
            file_bin = base / "bin_as_file"
            file_bin.write_text("not a dir", encoding="ascii")
            file_build = base / "build_as_file"
            file_build.write_text("not a dir", encoding="ascii")

            # Non-dir --bin (missing or file) exits 1 before invoking subprocess.run.
            for bad_bin in (base / "missing_bin", file_bin):
                code, _, err = _run_main(
                    probe_root_devices,
                    ["--warp-only", "--bin", str(bad_bin)],
                    repo,
                    fake_run=mock.Mock(side_effect=AssertionError("subprocess called for bad --bin")),
                )
                self.assertEqual(code, 1)
                self.assertNotIn("Traceback", err)
                self.assertIn("--bin path is not a directory", err)

            # Missing or directory control .cso exits 1 before invoking subprocess.run.
            bad_cso_bin = base / "bad_cso_bin"
            bad_cso_bin.mkdir()
            (bad_cso_bin / "compute_root.cso").mkdir()
            (bad_cso_bin / "compute_tables.cso").write_bytes(b"ok")
            code, _, err = _run_main(
                probe_root_devices,
                ["--warp-only", "--bin", str(bad_cso_bin)],
                repo,
                fake_run=mock.Mock(side_effect=AssertionError("subprocess called for dir .cso")),
            )
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertIn("Control shader is missing or not a file", err)

            # Non-dir --build exits 1 cleanly.
            for bad_build in (base / "missing_build", file_build):
                code, _, err = _run_main(
                    probe_root_devices,
                    [str(bad_build), "--warp-only", "--bin", str(bin_dir)],
                    repo,
                )
                self.assertEqual(code, 1)
                self.assertNotIn("Traceback", err)
                self.assertIn("Build directory is not a directory", err)

            # Missing or directory six-apis.json exits 1 cleanly.
            inv = calls / "six-apis.json"
            saved_inv = inv.read_text(encoding="utf-8")
            inv.unlink()
            code, _, err = _run_main(
                probe_root_devices, [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo
            )
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertIn("Inventory file is missing or not a file", err)

            inv.mkdir()
            code, _, err = _run_main(
                probe_root_devices, [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo
            )
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertIn("Inventory file is missing or not a file", err)
            inv.rmdir()
            inv.write_text(saved_inv, encoding="utf-8")

            # Directory where PE image is expected exits 1 cleanly.
            img = build_dir / probe_root_devices.NAMES[0]
            saved_img = img.read_bytes()
            img.unlink()
            img.mkdir()
            code, _, err = _run_main(
                probe_root_devices, [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo
            )
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertIn("Build image is missing or not a file", err)
            img.rmdir()
            img.write_bytes(saved_img)

            # Real missing root_device_probe.exe (unmocked subprocess.run) exits 1 cleanly.
            proc = subprocess.run(
                [sys.executable, "-B", str(TOOL), "--warp-only", "--bin", str(bin_dir)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertIn("probe_root_devices:", proc.stderr)

    def test_dest_dir_symlink_file_parent_inside_build_and_hardlink_guards(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, calls, build_dir, bin_dir = self._create_fixture(base)
            dest = calls / "root-devices-warp.json"

            # dest is a directory
            dest.mkdir()
            code, _, err = _run_main(
                probe_root_devices, [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo
            )
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertIn("Output path is a directory", err)
            dest.rmdir()

            # dest inside args.build exits 2
            code, _, err = _run_main(
                probe_root_devices, [str(repo), "--warp-only", "--bin", str(bin_dir)], repo
            )
            self.assertEqual(code, 2)
            self.assertNotIn("Traceback", err)
            self.assertIn("separate from the report directory", err)

            # dest hardlinked to six-apis.json is rejected without overwriting inventory
            inv = calls / "six-apis.json"
            inv_text = inv.read_text(encoding="utf-8")
            dest.hardlink_to(inv)
            code, _, err = _run_main(
                probe_root_devices, [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo
            )
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertIn("must not alias inventory", err)
            self.assertEqual(inv.read_text(encoding="utf-8"), inv_text)
            dest.unlink()

            # dest is a symlink (real symlink if permitted, else mock Path.is_symlink)
            target = base / "untouched_target.json"
            target.write_text("keep-target", encoding="ascii")
            try:
                dest.symlink_to(target)
                symlink_created = True
            except OSError as exc:
                if getattr(exc, "winerror", None) != 1314:
                    raise
                symlink_created = False

            if symlink_created:
                code, _, err = _run_main(
                    probe_root_devices, [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo
                )
                dest.unlink()
            else:
                real_is_symlink = Path.is_symlink

                def fake_is_symlink(path_self):
                    return path_self == dest or real_is_symlink(path_self)

                with mock.patch.object(Path, "is_symlink", fake_is_symlink):
                    code, _, err = _run_main(
                        probe_root_devices, [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo
                    )
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertIn("Output path must not be a symlink", err)
            self.assertEqual(target.read_text(encoding="ascii"), "keep-target")

            # dest.parent is a regular file
            repo2 = base / "repo_file_parent"
            (repo2 / "out").mkdir(parents=True)
            (repo2 / "out" / "calls").write_text("keep-parent-file", encoding="ascii")
            code, _, err = _run_main(
                probe_root_devices, [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo2
            )
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertIn("Output parent is not a directory", err)
            self.assertEqual((repo2 / "out" / "calls").read_text(encoding="ascii"), "keep-parent-file")

            # dest.parent is a symlink
            real_is_symlink = Path.is_symlink

            def fake_parent_symlink(path_self):
                return path_self == calls or real_is_symlink(path_self)

            with mock.patch.object(Path, "is_symlink", fake_parent_symlink):
                code, _, err = _run_main(
                    probe_root_devices, [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo
                )
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertIn("Output directory must not be a symlink", err)

    def test_os_replace_failure_preserves_bytes_and_zero_temps(self) -> None:
        with TemporaryDirectory() as tmp:
            repo, calls, build_dir, bin_dir = self._create_fixture(Path(tmp))
            dest = calls / "root-devices-warp.json"
            dest.write_text("existing-report\n", encoding="utf-8")
            with mock.patch.object(os, "replace", side_effect=OSError("simulated replace failure")):
                code, _, err = _run_main(
                    probe_root_devices, [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo
                )
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertIn("simulated replace failure", err)
            self.assertEqual(dest.read_text(encoding="utf-8"), "existing-report\n")
            self.assertEqual(sorted(p.name for p in calls.iterdir()), ["root-devices-warp.json", "six-apis.json"])

    def test_argparse_py314_safe(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            code_help, out_help, err_help = _run_main(probe_root_devices, ["--help"], repo)
            self.assertEqual(code_help, 0)
            self.assertEqual(err_help, "")
            self.assertIn("usage:", out_help.lower())

            for bad_argv in (["--bogus-flag"], ["build_one", "extra_arg"]):
                with self.subTest(argv=bad_argv):
                    code, _, err = _run_main(probe_root_devices, bad_argv, repo)
                    self.assertNotEqual(code, 0)
                    self.assertNotIn("Traceback", err)
                    self.assertIn("unrecognized", err.lower())

    def test_fast_temp_copy_mutations(self) -> None:
        source = TOOL.read_text(encoding="utf-8")
        for label, target, replacement in MUTATIONS:
            with self.subTest(mutation=label), TemporaryDirectory() as tmp:
                base = Path(tmp)
                repo, calls, build_dir, bin_dir = self._create_fixture(base)
                file_bin = base / "file_bin"
                file_bin.write_text("not dir", encoding="ascii")
                bad_cso_bin = base / "bad_cso_bin"
                bad_cso_bin.mkdir()
                (bad_cso_bin / "compute_root.cso").mkdir()
                (bad_cso_bin / "compute_tables.cso").write_bytes(b"ok")
                dest = calls / "root-devices-warp.json"

                checks = {
                    "drop_bin_is_dir_guard": lambda mod: self.assertIn(
                        "--bin path is not a directory",
                        _run_main(mod, ["--warp-only", "--bin", str(file_bin)], repo)[2],
                    ),
                    "drop_control_cso_is_file_guard": lambda mod: self.assertIn(
                        "Control shader is missing or not a file",
                        _run_main(mod, ["--warp-only", "--bin", str(bad_cso_bin)], repo)[2],
                    ),
                    "drop_dest_is_symlink_guard": lambda mod: self._assert_symlink_guard_fires(
                        mod, build_dir, bin_dir, repo, dest
                    ),
                    "drop_dest_is_dir_guard": lambda mod: self._assert_dir_guard_fires(
                        mod, build_dir, bin_dir, repo, dest
                    ),
                    "drop_dest_inside_build_guard": lambda mod: self.assertEqual(
                        _run_main(mod, [str(repo), "--warp-only", "--bin", str(bin_dir)], repo)[0],
                        2,
                    ),
                    "drop_tmp_unlink_cleanup": lambda mod: self._assert_replace_leaves_zero_temps(
                        mod, build_dir, bin_dir, repo, calls, dest
                    ),
                }

                self.assertEqual(source.count(target), 1, target)
                mut_path = base / f"probe_root_devices_{label}.py"
                mut_path.write_text(source.replace(target, replacement, 1), encoding="utf-8")
                mod_name = f"probe_root_devices_mut_{label}"
                mod = _load_temp_module(mut_path, mod_name)
                try:
                    with self.assertRaises(AssertionError, msg=f"mutation {label} escaped detection"):
                        checks[label](mod)
                finally:
                    sys.modules.pop(mod_name, None)

    def _assert_symlink_guard_fires(self, mod, build_dir: Path, bin_dir: Path, repo: Path, dest: Path) -> None:
        real_is_symlink = Path.is_symlink

        def fake_is_symlink(path_self):
            return path_self == dest or real_is_symlink(path_self)

        with mock.patch.object(Path, "is_symlink", fake_is_symlink):
            _, _, err = _run_main(mod, [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo)
        self.assertIn("Output path must not be a symlink", err)

    def _assert_dir_guard_fires(self, mod, build_dir: Path, bin_dir: Path, repo: Path, dest: Path) -> None:
        dest.mkdir()
        try:
            _, _, err = _run_main(mod, [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo)
        finally:
            if dest.is_dir():
                dest.rmdir()
        self.assertIn("Output path is a directory", err)

    def _assert_replace_leaves_zero_temps(
        self, mod, build_dir: Path, bin_dir: Path, repo: Path, calls: Path, dest: Path
    ) -> None:
        dest.write_text("keep", encoding="utf-8")
        try:
            with mock.patch.object(os, "replace", side_effect=OSError("boom")):
                _run_main(mod, [str(build_dir), "--warp-only", "--bin", str(bin_dir)], repo)
            self.assertEqual(sorted(p.name for p in calls.iterdir()), ["root-devices-warp.json", "six-apis.json"])
        finally:
            for extra in list(calls.iterdir()):
                if extra.name not in ("six-apis.json", "root-devices-warp.json"):
                    extra.unlink(missing_ok=True)
            dest.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
