"""
_win_console.pyx ビルド用スクリプト (setup.py)

使い方:
1. Visual C++ Build Tools がある場合:
   uv run python setup.py build_ext --inplace

2. MSVC がない環境 (ziglang C コンパイラで直接 .pyd をビルドする場合):
   uv run --with ziglang python setup.py
"""

from pathlib import Path
import subprocess
import sys
import sysconfig


def build_with_zig() -> Path:
    """
    MSVC (cl.exe) が未インストールの環境向けに、
    Cython で `_win_console.pyx` -> `_win_console.c` を生成後、
    `zig cc` (`-target x86_64-windows-gnu`) でネイティブ `.pyd` を生成する。
    """
    from Cython.Compiler.Main import compile_single

    tools_dir = Path(__file__).resolve().parent
    pyx_file = tools_dir / "_win_console.pyx"
    c_file = tools_dir / "_win_console.c"
    ext_suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".pyd"
    pyd_file = tools_dir / f"_win_console{ext_suffix}"

    print(f"[1/2] Cython compiling: {pyx_file.name} -> {c_file.name}")
    res = compile_single(str(pyx_file), language_level=3)
    if res.num_errors > 0:
        raise RuntimeError(f"Cython compilation failed with {res.num_errors} errors.")

    include_dir = sysconfig.get_path("include")
    base_dir = Path(sysconfig.get_config_var("installed_base") or sys.base_prefix)
    libs_dir = base_dir / "libs"
    py_ver_nodot = f"{sys.version_info.major}{sys.version_info.minor}"

    cmd = [
        sys.executable,
        "-m",
        "ziglang",
        "cc",
        "-target",
        "x86_64-windows-gnu",
        "-shared",
        "-O2",
        f"-I{include_dir}",
        f"-L{libs_dir}",
        str(c_file),
        f"-lpython{py_ver_nodot}",
        "-lkernel32",
        "-luser32",
        "-lrstrtmgr",
        "-o",
        str(pyd_file),
    ]
    print(f"[2/2] Building native extension: {pyd_file.name}")
    subprocess.run(cmd, check=True)
    print(f"✅ Build complete: {pyd_file}")
    return pyd_file


if __name__ == "__main__":
    if len(sys.argv) == 1 or "--zig" in sys.argv:
        build_with_zig()
    else:
        from setuptools import Extension, setup
        from Cython.Build import cythonize

        extensions = [
            Extension(
                name="_win_console",
                sources=["_win_console.pyx"],
                libraries=["kernel32", "user32", "rstrtmgr"],
            )
        ]

        setup(
            name="antigravity-win-console",
            version="0.1.0",
            description="Cython native Win32 console input injector and Restart Manager PID resolver",
            ext_modules=cythonize(
                extensions,
                compiler_directives={
                    "language_level": "3",
                    "boundscheck": False,
                    "wraparound": False,
                },
            ),
        )
