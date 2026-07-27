# 仓库结构与入口文件地图

## 1. 本讲目标

本讲帮你建立 **TileScale（Python 包名 `tilelang`）仓库的全景地图**。学完后你应能：

1. 看着仓库目录树，一眼说出每个关键目录负责什么（C++ 编译器、Python DSL、分布式扩展、示例、基准、文档）。
2. 准确解释 `import tilelang` 这一行背后依次发生了什么：先算出库搜索路径、再加载底层 `.so`、再导出 DSL 原语与（可选的）分布式扩展。
3. 理解 `scikit-build-core + CMake` 如何把 C++ 源码编译成 `libtilelang.so`、`libtilelang_module.so`、`tilescale_ext._C.so`，以及这些产物在 wheel 里和开发模式下分别落在哪。
4. 掌握 `libinfo.find_lib_path` 的搜索逻辑，能据此诊断「找不到库」报错。

本讲承接 u1-l1（项目定位）与 u1-l2（安装与 `.so` 加载），把抽象的「加载机制」落到具体目录与文件上，为后续 u2（DSL 原语）、u3（编译流水线）、u6（分布式）提供「按图索骥」的能力。

## 2. 前置知识

阅读本讲前，你需要了解：

- **Python 包与模块**：`import tilelang` 会执行 `tilelang/__init__.py`；包内子目录若有 `__init__.py` 也是包。
- **共享库（`.so`）**：C/C++ 编译产物，运行时由 `ctypes.CDLL` 或 Python 扩展机制加载。TileLang 的核心算法不在 Python 里，而在编译出的 `libtilelang.so` 中，Python 只是「外壳 + 胶水」。
- **构建系统**：本仓库用 CMake 编译 C++，用 `scikit-build-core` 把 CMake 接进 Python 的 `pip`/`wheel` 流程。
- **TVM**：TileLang 建立在 TVM 之上（`3rdparty/tvm` 子模块），所以仓库里同时有「上游 TVM」和「TileLang 自己的 C++」两部分。

> 你已经在 u1-l2 知道 `import tilelang` 会通过 `env.py → libinfo.find_lib_path → ctypes.CDLL` 加载 `libtilelang.so`。本讲不再重复「为什么」，而是带你逐文件看清「在哪、加载了哪些、产物如何摆放」。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| `tilelang/__init__.py` | 包入口：算版本、配日志、加载 `.so`、导出 DSL 原语与可选分布式扩展 |
| `tilelang/env.py` | 计算库搜索路径 `TL_LIBS`、检测 CUDA/ROCm、设置 TVM/CUTLASS/NVSHMEM 路径、集中管理环境变量 |
| `tilelang/libinfo.py` | `find_lib_path`：在 `TL_LIBS` 列表中按平台命名规则定位 `.so` |
| `CMakeLists.txt` | 顶层 CMake：编译 `libtilelang.so`/`libtilelang_module.so`/`tilescale_ext._C.so`，决定后端（CUDA/ROCm/Metal） |
| `pyproject.toml` | Python 构建配置（scikit-build-core）、依赖清单、wheel 内包映射 |
| `src/` | C++ 编译器与算子后端（transform pass / op / codegen / 模板 / runtime） |
| `tilelang/` | Python DSL 包 |
| `tilescale_ext/` | 「分布式扩展」的 Python 入口包，导入编译出的 `_C` |
| `examples/`、`benchmark/`、`docs/`、`testing/` | 示例、基准、文档、测试 |

## 4. 核心概念与源码讲解

### 4.1 顶层目录职责地图

#### 4.1.1 概念说明

TileScale 仓库是一个「多语言、多后端、单仓库」项目：C++ 负责重活（编译器 pass、codegen、算子），Python 负责对外 DSL 与胶水，两者通过 `.so` 桥接。理解仓库的第一步，是分清「**哪些是源码、哪些是产物、哪些是依赖**」：

- **源码**：`src/`（C++）、`tilelang/`（Python）。
- **依赖（子模块）**：`3rdparty/`（上游 TVM、CUTLASS、Composable Kernel、可选 NVSHMEM 源码）。
- **构建相关**：`CMakeLists.txt`、`cmake/`、`pyproject.toml`、`docker/`。
- **学习与验证**：`examples/`、`benchmark/`、`testing/`、`docs/`。

#### 4.1.2 核心流程

下面是仓库顶层目录的职责树（只列关键目录，路径相对仓库根）：

```
tile-ai-tilescale/
├── src/                       # C++ 编译器与算子后端
│   ├── transform/             # 51 个 IR transform pass（layout_inference / inject_pipeline / storage_rewrite ...）
│   ├── op/                    # T.gemm / T.copy / T.reduce / distributed / sync 等算子的 C++ 实现 + Op 注册
│   ├── target/                # codegen（cuda/hip/cpp/cutedsl）+ rt_mod 运行时模块 + ptx + intrin_rule
│   ├── tl_templates/          # 设备端模板（cuda: gemm/copy/barrier/reduce，按 sm70~sm120 分发）
│   ├── layout/                # Layout / Fragment 的 C++ 数据结构
│   ├── runtime/               # 设备运行时（CUDA module 加载）
│   └── support/               # C++ 辅助工具
├── tilelang/                  # Python DSL 包（import tilelang 的入口）
│   ├── __init__.py            # 包导出 + libtilelang.so 加载
│   ├── env.py                 # TL_LIBS / CUDA/ROCm 检测 / TVM·CUTLASS·NVSHMEM 路径 / env 配置
│   ├── libinfo.py             # find_lib_path：在 TL_LIBS 中定位 .so
│   ├── language/              # T.* DSL 原语（kernel/gemm/copy/reduce/loop/parallel/pipeline）+ parser/tir/v2/distributed
│   ├── engine/                # 编译流水线（lower / phase / param）
│   ├── jit/                   # JITKernel + execution_backend adapter（dlpack/cython/torch/...）
│   ├── transform/             # Python 侧 transform 包装（绑定 C++ pass）
│   ├── layout/  analysis/     # Layout/Fragment Python 端 + 静态分析（如 layout_visual）
│   ├── autotuner/  carver/    # AutoTuner 自动调优 + Carver/Roller 代价模型
│   ├── distributed/           # 分布式运行时（pynvshmem / launch.sh / init_distributed / install_deepep）
│   └── utils/                 # 工具，含 ts_ext/（tilescale_ext 的 C++ 扩展源码）
│       └── ts_ext/            # tensor.cpp / ipc_ops.cpp / ts_ext_bindings.cpp（IPC 张量与内存管理）
├── tilescale_ext/             # 分布式扩展的 Python 入口包（__init__.py 导入 _C）
├── examples/                  # 示例（quickstart.py、gemm/flash_attention/distributed/...）
├── benchmark/                 # 基准（matmul / mamba2 / distributed / blocksparse_attention）
├── testing/                   # 测试（python/、cpp/）
├── docs/                      # 文档（get_started / programming_guides / compiler_internals）
├── cmake/                     # CMake 辅助（load_tvm.cmake、pypi-z3）
├── docker/                    # 各 CUDA/ROCm 版 Dockerfile
├── 3rdparty/                  # 子模块（tvm、cutlass、composable_kernel、可选 nvshmem_src）
├── CMakeLists.txt             # 顶层 CMake：编译 libtilelang.so / tilescale_ext._C
└── pyproject.toml             # Python 构建（scikit-build-core）+ 依赖 + wheel 包映射
```

几个**容易混淆**的点，先点出来：

1. **`tilescale_ext/`（顶层包）≠ `tilelang/utils/ts_ext/`（C++ 源码）**。顶层 `tilescale_ext/` 只有一个 `__init__.py`，它 `from tilescale_ext._C import ...`；而 `_C.so` 是 CMake 从 `tilelang/utils/ts_ext/` 下的 `.cpp` 编译出来的（见 4.4）。源码住在 `tilelang` 包内，产物却安装到 `tilescale_ext` 包内——这是「源码与产物分离」的典型布局。
2. **`src/` 是 C++ 源码目录，不是 Python `src` layout**。`pyproject.toml` 里甚至专门把 `src` 映射进 wheel 作为 `tilelang/src`，供代码生成时检索模板。
3. **`3rdparty/tvm` 是上游 TVM 整套源码**（子模块），TileLang 的 pass/op 依赖它的 IR 与 runtime；它会被一起编译。

#### 4.1.3 源码精读

`src/` 下的 C++ 按职责清晰分层，下面两组文件最能体现「目录即职责」：

C++ 源码的收集规则在顶层 CMake 中显式声明（`src/*.cc`、`src/transform/*.cc`、`src/op/*.cc` 等）：

[文件路径:CMakeLists.txt#L135-L147](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/CMakeLists.txt#L135-L147)

> 这段 `file(GLOB ...)` 把 `src/transform/`、`src/op/`、`src/target/` 等目录的 `.cc` 收进 `TILE_LANG_SRCS`，最终编译进 `libtilelang.so`。也就是说，你在 `src/transform/` 下看到的 51 个 pass、在 `src/op/` 下看到的算子，都是这个 `.so` 的组成部分。

`src/op/` 目录里每个算子一个文件，对应你后面会学的 `T.gemm`、`T.copy`、`T.reduce` 等原语的 C++ 落地点：

| Python 原语 | C++ 实现文件 |
| --- | --- |
| `T.gemm` | `src/op/gemm.cc` |
| `T.copy` | `src/op/copy.cc` |
| `reduce_max/sum` | `src/op/reduce.cc` |
| 分布式 put/get | `src/op/distributed.cc`、`src/op/remote_copy.cc` |
| 同步 barrier/sync | `src/op/sync.cc` |
| Op 注册总入口 | `src/op/operator.cc` |

这些文件的具体内部会在 u7-l1（C++ 算子实现机制）展开；本讲你只需建立「目录→职责」的索引。

#### 4.1.4 代码实践

**实践目标**：用「按目录归类」的方式亲手验证上面的职责树。

**操作步骤**：

1. 在仓库根目录运行（只读统计）：
   ```bash
   # 数一数 src/transform 下有多少个 IR pass
   ls src/transform/*.cc | wc -l
   # 看 src/op 下都有哪些算子文件
   ls src/op/*.cc
   ```
2. 打开 `examples/quickstart.py`（u1-l3 已精读过），确认它只用到了 `tilelang.language` 与 `tilelang.jit`，没有直接碰 `src/`。
3. 打开 `tilescale_ext/__init__.py`，确认它只有一行实质导入 `from tilescale_ext._C import ...`。

**需要观察的现象**：

- `src/transform/` 下 pass 数量应为数十个（本 HEAD 下 51 个）。
- `examples/quickstart.py` 不 import 任何 `src.*`，说明 C++ 对用户完全隐藏在 `.so` 之后。
- `tilescale_ext/__init__.py` 仅依赖编译产物 `_C`，目录本身不含 Python 逻辑。

**预期结果**：你会直观感受到「Python 目录是用户接口、`src/` 是实现、`3rdparty/` 是依赖」的三层分工。

#### 4.1.5 小练习与答案

**练习 1**：`tilelang/distributed/` 与 `src/op/distributed.cc` 是什么关系？
**答案**：`tilelang/distributed/` 是 Python 侧的分布式**运行时与启动**（pynvshmem、`launch.sh`、`init_distributed`）；`src/op/distributed.cc` 是 C++ 侧的分布式**设备原语**（putmem/getmem 等）注册与 lowering。Python 调度、C++ 执行，两者通过 `.so` 桥接。

**练习 2**：为什么顶层有一个空的 `tilescale_ext/` 包，而它的 C++ 源码却在 `tilelang/utils/ts_ext/`？
**答案**：C++ 扩展源码归在 `tilelang` 包内便于随主包一起编译与维护；但安装时 CMake 把编译出的 `_C.so` 放到 `tilescale_ext/` 目录（见 4.4），让用户能以 `import tilescale_ext` 独立导入分布式能力，且在 `tilescale_ext` 缺失时不影响 `import tilelang`。

---

### 4.2 `tilelang/__init__.py` 的模块导出与可选分布式扩展

#### 4.2.1 概念说明

`tilelang/__init__.py` 是「**用户看到的一切 tilelang.* API 的总开关**」。它做了三件事：

1. **加载底层 `.so`**（没有它，后面的 `T.gemm`、`compile` 全都会崩）。
2. **导出 DSL 原语与编译/JIT 接口**（`tilelang.jit`、`tilelang.language`、`tilelang.lower` 等）。
3. **可选地启用分布式扩展**（`tilescale_ext` 装了才有 `tilelang.tensor` / `tilelang.get_allocator`，否则为 `None`）。

第三点正是「TileScale = TileLang 的分布式扩展」在代码上的体现：分布式是**可选**的，单机 kernel 完全不依赖它。

#### 4.2.2 核心流程

`import tilelang` 触发 `__init__.py`，关键执行顺序（简化伪代码）：

```
1. 算版本 __version__（VERSION 文件 / 安装元数据）
2. 初始化 tqdm 日志 handler
3. from .env import env, ...        # ★ 运行 env.py：算出 TL_LIBS、配路径（见 4.3/4.4）
4. import tvm                       # env.py 已把 TVM 的 python 路径塞进 sys.path
5. from . import libinfo            # 运行 libinfo.py
6. _load_tile_lang_lib()            # ★ libinfo.find_lib_path → ctypes.CDLL 加载 libtilelang.so
7. from .jit import jit, compile ...; from .profiler import Profiler
8. from .utils import TensorSupplyType ...
9. try: from .utils.tensor import tensor          # ★ 可选分布式扩展
     from .utils.allocator import get_allocator
   except ImportError: tensor = None; get_allocator = None
10. from .layout import Layout, Fragment
11. from . import analysis, transform, language, engine, tools
12. from .autotuner import autotune
13. from .engine import lower, register_*_postproc
14. from .math import *; from . import ir, tileop
```

> 注意第 3 步必须在第 4 步之前：`env.py` 负责把上游 TVM 的 `python/` 目录加入 `sys.path`，否则第 4 步 `import tvm` 会失败。这就是为什么 `.so` 还没加载就要先 import `.env`。

#### 4.2.3 源码精读

**底层库加载函数**——根据是否「仅运行时」选择加载哪个 `.so`：

[文件路径:tilelang/__init__.py#L126-L135](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/__init__.py#L126-L135)

> `_load_tile_lang_lib` 在非「runtime-only」模式加载 `libtilelang.so`（含编译器），否则加载更精简的 `libtilelang_module.so`；具体名字由 `libinfo.find_lib_path` 解析为带平台前缀（Linux 为 `lib<name>.so`）的完整路径，再用 `ctypes.CDLL` 加载。

**触发加载**——受环境变量 `SKIP_LOADING_TILELANG_SO` 控制（默认 `"0"`，即加载）：

[文件路径:tilelang/__init__.py#L139-L140](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/__init__.py#L139-L140)

> 这一行把 `.so` 真正 `dlopen` 进进程。`_LIB`（句柄）和 `_LIB_PATH`（路径）随后被 FFI 层使用。

**可选分布式扩展**——用 `try/except ImportError` 让缺失 `tilescale_ext` 时也能正常 `import tilelang`：

[文件路径:tilelang/__init__.py#L151-L158](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/__init__.py#L151-L158)

> `tilelang.utils.tensor` 和 `tilelang.utils.allocator` 内部会 `import tilescale_ext`；若未安装则抛 `ImportError`，被这里捕获后置为 `None`。所以判断「分布式扩展是否可用」只需看 `tilelang.tensor is not None`。

**DSL / 编译 / 分析子模块的统一导出**：

[文件路径:tilelang/__init__.py#L164-L170](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/__init__.py#L164-L170)

> 这一段让用户能直接用 `tilelang.language.T`、`tilelang.engine.lower`、`tilelang.transform`、`tilelang.analysis`、`tilelang.tools`。注意它们都在 `.so` 加载成功之后才导入——因为它们内部大量调用 FFI 绑定到 C++ 侧。

#### 4.2.4 代码实践

**实践目标**：亲手验证「`.so` 加载顺序」与「分布式扩展是否可用」。

**操作步骤**：

```bash
python - <<'PY'
import tilelang, os
print("__file__       :", tilelang.__file__)
print("_LIB_PATH      :", getattr(tilelang, "_LIB_PATH", "(not loaded)"))
# libinfo 模块内部 `from .env import TL_LIBS`，所以可从这里读到搜索路径
print("TL_LIBS        :", tilelang.libinfo.TL_LIBS)
print("tensor (dist?) :", "available" if tilelang.tensor is not None else "None (tilescale_ext missing)")
print("tvm loaded at  :", os.path.dirname(__import__("tvm").__file__))
PY
```

**需要观察的现象**：

- `_LIB_PATH` 指向某个 `libtilelang.so` 或 `libtilelang_module.so`。
- `TL_LIBS` 是一个路径列表：pip 安装时通常是 `['<site-packages>/tilelang/lib']`；源码开发模式下是 `['<repo>/build/lib', '<repo>/build/tvm']`。
- 若未安装 `tilescale_ext`，`tensor` 输出 `None`。

**预期结果**：你能用一行话概括加载链——"`import tilelang` 先跑 `env.py` 算出 `TL_LIBS` 并配好 TVM 路径，再 `import tvm`，再由 `libinfo.find_lib_path` 在 `TL_LIBS` 里定位并 `ctypes.CDLL` 加载 `libtilelang.so`，最后导出 DSL/JIT/编译接口，并在 `tilescale_ext` 可用时挂上 `tensor/get_allocator`。"

> 待本地验证：`TL_LIBS` 与 `_LIB_PATH` 的具体取值取决于你的安装方式（pip / 开发模式 / Docker），上机后请记录实际输出。

#### 4.2.5 小练习与答案

**练习 1**：把环境变量 `SKIP_LOADING_TILELANG_SO=1` 设置后再 `import tilelang`，会发生什么？`tilelang.jit` 还能用吗？
**答案**：`_load_tile_lang_lib` 不会被调用，`_LIB`/`_LIB_PATH` 不会被设置。由于 `.so` 未加载，FFI 绑定无法工作，`tilelang.jit` / `tilelang.compile` 等在真正编译 kernel 时会报错（依赖 C++ 编译器）。但 `import tilelang` 这一步本身仍可能成功——该开关主要用于纯推理或特殊打包场景。

**练习 2**：为什么不把 `from .utils.tensor import tensor` 直接放在文件顶部，而要用 `try/except`？
**答案**：因为 `tilescale_ext` 是**可选**依赖，单机用户未必安装。若放在顶部，缺 `tilescale_ext` 会导致 `import tilelang` 直接失败，连单 GPU kernel 都跑不了；`try/except` 把分布式降级为「不可用」而非「致命错误」，保证核心功能独立。

---

### 4.3 `libinfo` 如何定位底层 `.so`

#### 4.3.1 概念说明

`tilelang/libinfo.py` 只有一个公开函数 `find_lib_path(name)`，却决定了「`.so` 到底从哪里来」。它的逻辑很简单：拿到一个库名（如 `tilelang`），按当前操作系统拼出实际文件名（Linux `libtilelang.so`、Windows `tilelang.dll`、macOS `libtilelang.dylib`），然后在 `TL_LIBS` 列出的若干目录里挨个找；找不到就把所有候选目录打印出来并抛 `RuntimeError`。

`TL_LIBS` 本身来自 `env.py`：pip 安装指向 `tilelang/lib`；开发模式指向 `build/lib` 与 `build/tvm`。所以「找不到库」几乎总是「路径没对上」——要么是开发模式下没编译，要么是 wheel 没把 `.so` 装进去。

#### 4.3.2 核心流程

```
env.py 决定 TL_LIBS = [目录A, 目录B, ...]
        │
        ▼
libinfo.find_lib_path("tilelang")
        │
        ├── 按平台拼文件名：Linux → libtilelang.so
        │
        ├── for lib_root in TL_LIBS:
        │       if 存在 lib_root/libtilelang.so: 返回该路径
        │
        └── 都没找到 → RuntimeError("Cannot find libraries: ...", 候选目录列表)
```

#### 4.3.3 源码精读

`libinfo.py` 顶部先把 `TL_LIBS` 引入（它本身不计算路径，路径由 `env.py` 负责）：

[文件路径:tilelang/libinfo.py#L4-L4](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/libinfo.py#L4-L4)

> `from .env import TL_LIBS`——所以「libinfo 找库」与「env 算路径」是两个文件分工协作。

核心搜索函数——平台命名规则 + 候选目录遍历 + 友好报错：

[文件路径:tilelang/libinfo.py#L7-L35](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/libinfo.py#L7-L35)

> 注意末尾的 `for ... else`：Python 的 `for-else` 表示「循环正常结束（没 break）时执行 else」，即所有目录都没命中时进入报错分支。报错信息会把 `TL_LIBS` 全部列出，这正是诊断「找不到库」时最该看的线索。

`TL_LIBS` 在 `env.py` 中按「安装模式」分流：

[文件路径:tilelang/env.py#L24-L25](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/env.py#L24-L25)

> 默认（pip 安装）：`TL_LIBS = [<tilelang 包根>/lib]`——即 wheel 里打包的 `tilelang/lib/` 目录。

[文件路径:tilelang/env.py#L28-L38](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/env.py#L28-L38)

> 开发模式（仓库根没有 `3rdparty/` 目录，说明是源码检出且产物在 `build/`）：`TL_LIBS = [<repo>/build/lib, <repo>/build/tvm]`。`build/lib` 放 `libtilelang.so`、`tilescale_ext._C.so`；`build/tvm` 放上游 TVM 的库。

#### 4.3.4 代码实践

**实践目标**：模拟 `find_lib_path` 的搜索过程，理解报错信息。

**操作步骤**：

```bash
python - <<'PY'
import os, tilelang
from tilelang import libinfo
roots = tilelang.libinfo.TL_LIBS
name = "tilelang"
# 复刻 find_lib_path 的平台命名
lib_name = f"lib{name}.so"   # Linux
print("candidates:")
for r in roots:
    p = os.path.join(r, lib_name)
    print(f"  {'OK ' if os.path.isfile(p) else 'MISS'} {p}")
# 真正调用一次
print("resolved:", tilelang.libinfo.find_lib_path(name))
PY
```

**需要观察的现象**：

- 候选目录中，至少有一个 `OK`，且 `resolved` 正是那个 `libtilelang.so` 的完整路径。
- 如果手动 `export SKIP_LOADING_TILELANG_SO=0`（默认）并删除/重命名该 `.so`，再 import 会得到形如 `Cannot find libraries: libtilelang.so \n List of candidates: ...` 的 `RuntimeError`。

**预期结果**：你应能解释报错信息里的「List of candidates」就是 `TL_LIBS`，从而知道该把 `.so` 放到哪、或该重新 `pip install` / 重新编译。

> 待本地验证：候选路径取决于安装方式，上机记录实际目录。

#### 4.3.5 小练习与答案

**练习 1**：开发模式下你 `git clone` 后没有编译就 `import tilelang`，`find_lib_path` 会在哪些目录找？为什么会失败？
**答案**：会在 `<repo>/build/lib` 和 `<repo>/build/tvm` 找 `libtilelang.so`。因为还没跑 CMake/编译，这两个目录不存在或没有 `.so`，所以遍历完所有候选都不命中，抛出列出候选目录的 `RuntimeError`。解决方法是先按 u1-l2 的源码安装流程编译。

**练习 2**：为什么 `find_lib_path` 里要区分 `py_ext=True`（`name.abi3.so`）和普通 `libname.so`？
**答案**：普通 `.so` 是 C/C++ 共享库（如 `libtilelang.so`，由 `ctypes` 加载）；`py_ext` 分支命名的 `.abi3.so` 是 Python 扩展模块（如 `tilescale_ext._C`，带稳定 ABI），由 Python import 机制加载。两者命名约定不同，故需区分。

---

### 4.4 scikit-build + CMake 的构建产物布局

#### 4.4.1 概念说明

TileLang 不是「纯 Python 包」——`pip install` 时要触发 CMake 把 `src/` 编成 `.so`。这套流程由 `scikit-build-core`（`pyproject.toml` 里的 `build-backend`）驱动：

- `scikit-build-core` 读 `pyproject.toml`，调用 CMake，执行 `CMakeLists.txt`。
- CMake 编译出多个目标：`libtilelang.so`、`libtilelang_module.so`、`tilelang_cython_wrapper.so`、（CUDA 时）`tilescale_ext._C.so`。
- 产物按 `pyproject.toml` 的 `[tool.scikit-build.wheel.packages]` 映射规则放进 wheel，最终落到 `site-packages` 的对应目录。

理解这一层，你才能回答「为什么 `import tilelang` 能在 `tilelang/lib` 找到 `.so`」「为什么 `tilescale_ext._C` 装在 `tilescale_ext/` 下」。

#### 4.4.2 核心流程

```
pip install tilelang
        │  build-backend = scikit_build_core.build
        ▼
CMakeLists.txt
        │
        ├── add_library(tilelang       SHARED ...)  → libtilelang.so       (编译器 + runtime)
        ├── add_library(tilelang_module SHARED ...) → libtilelang_module.so(仅 runtime/模块)
        ├── python_add_library(tilelang_cython_wrapper ...)              → cython adapter
        └── if(USE_CUDA) and Torch_FOUND:
                python_add_library(tilescale_ext_C ... OUTPUT_NAME _C)   → tilescale_ext/_C.so
        │
        ▼ install()
libtilelang.so / libtilelang_module.so / _C.so  →  wheel 内 tilelang/lib 或 tilescale_ext/
```

开发模式（`build/`）与 wheel（`site-packages/`）的产物对照：

| 产物 | 开发模式位置 | wheel 内位置 | 运行时由谁加载 |
| --- | --- | --- | --- |
| `libtilelang.so` | `build/lib/` | `tilelang/lib/` | `__init__.py` 的 `ctypes.CDLL` |
| `libtilelang_module.so` | `build/lib/` | `tilelang/lib/` | runtime-only 模式 |
| `tilelang_cython_wrapper.so` | `build/lib/` | `tilelang/lib/` | cython execution backend |
| `tilescale_ext._C.so` | `build/lib/`（名为 `_C.so`） | `tilescale_ext/` | `import tilescale_ext._C` |
| 上游 TVM 库（`libtvm*.so`） | `build/tvm/` 或 `build/lib/` | `tilelang/lib/`（随 tilelang 安装） | `import tvm` |

#### 4.4.3 源码精读

**后端选择**——决定编译哪些 codegen：

[文件路径:CMakeLists.txt#L68-L68](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/CMakeLists.txt#L68-L68)

> `TILELANG_BACKENDS = CUDA ROCM METAL` 是可选后端集合。其后 CMake 会按用户 `USE_CUDA/USE_ROCM/USE_METAL` 选择，并把对应 `src/target/codegen_*.cc`、`rt_mod_*.cc` 纳入编译（见下面 CUDA 分支）。

**CUDA 后端的额外源码**：

[文件路径:CMakeLists.txt#L214-L225](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/CMakeLists.txt#L214-L225)

> 仅当启用 CUDA 时，才编译 `codegen_cuda.cc`、`rt_mod_cuda.cc`、`ptx.cc`、`codegen_cutedsl.cc` 等。HIP（ROCm）、Metal 各有对应分支。这解释了为什么同一份 `src/` 能产出支持多后端的 `.so`。

**两个主库**——`tilelang`（含编译器，链接 `tvm + tvm_runtime`）与 `tilelang_module`（精简，只链接 `tvm`）：

[文件路径:CMakeLists.txt#L255-L258](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/CMakeLists.txt#L255-L258)

> 注意 `target_link_libraries(tilelang PUBLIC tvm_runtime tvm)` 与 `tilelang_module PUBLIC tvm` 的差异：前者带完整 runtime（`_RUNTIME_ONLY=False` 时 `__init__.py` 加载它），后者更轻量。这就是 4.2 里 `lib_name = "tilelang" if not _RUNTIME_ONLY else "tilelang_module"` 的来源。

**产物输出目录**——开发模式统一放 `build/lib`：

[文件路径:CMakeLists.txt#L260-L270](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/CMakeLists.txt#L260-L270)

> 这与 `env.py` 开发模式下 `TL_LIBS=[build/lib, build/tvm]` 完全对应——CMake 把 `.so` 放进 `build/lib`，`env.py` 就去那里找。

**安装到 wheel**——把主库装进 `tilelang/lib`：

[文件路径:CMakeLists.txt#L333-L336](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/CMakeLists.txt#L333-L336)

> 这就是 pip 安装后 `tilelang/lib/` 里出现 `libtilelang.so` 的原因，也对应 `env.py` 的 pip 模式 `TL_LIBS=[tilelang/lib]`。

**分布式扩展 `tilescale_ext._C` 的构建**——源码来自 `tilelang/utils/ts_ext/`，产物名设为 `_C`，安装到 `tilescale_ext` 目录：

[文件路径:CMakeLists.txt#L355-L359](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/CMakeLists.txt#L355-L359)

[文件路径:CMakeLists.txt#L369-L389](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/CMakeLists.txt#L369-L389)

> `OUTPUT_NAME _C` + `LIBRARY_OUTPUT_DIRECTORY build/lib`（开发模式）+ `install(... DESTINATION tilescale_ext)`（wheel）三者合起来，确保 `tilescale_ext/__init__.py` 里的 `from tilescale_ext._C import ...` 能在两种模式下都成立。该扩展依赖 libtorch（`target_link_libraries(... ${TORCH_LIBRARIES} ...)`），所以「装了 torch + CUDA」才会构建它。

**wheel 包映射**——决定 wheel 里包到目录的对应关系：

[文件路径:pyproject.toml#L118-L135](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/pyproject.toml#L118-L135)

> 左侧是 wheel 内路径、右侧是源码路径。注意 `"tilelang/src" = "src"`——把仓库 `src/`（C++ 源码）也打进 wheel 的 `tilelang/src/`，供 codegen 运行时检索 CUTLASS/模板头文件；`tilescale_ext = "tilescale_ext"` 则把那个入口包连同 `_C.so` 一起装好。

**运行时依赖**（构建期与运行期都要）：

[文件路径:pyproject.toml#L29-L45](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/pyproject.toml#L29-L45)

> 这里能看到本系列反复提到的几大依赖：`apache-tvm-ffi`（FFI）、`torch`（张量容器 + 构建 `tilescale_ext._C` 所需 libtorch）、`z3-solver`（SMT 约束求解）、`nvidia-nvshmem-cu12`（仅 Linux，分布式）。构建期还需要 `cython`、`scikit-build-core`、`patchelf`（见 build-system 段）。

#### 4.4.4 代码实践

**实践目标**：把「源码目录 → 产物 → 安装位置」三者的对应关系在本地确认一遍。

**操作步骤**：

```bash
python - <<'PY'
import tilelang, os, importlib.util
pkg = os.path.dirname(tilelang.__file__)
print("tilelang pkg :", pkg)
print("  has lib/ ? ", os.path.isdir(os.path.join(pkg, "lib")))
if os.path.isdir(os.path.join(pkg, "lib")):
    print("  lib/ files :", sorted(os.listdir(os.path.join(pkg, "lib")))[:8])
# 定位 tilescale_ext（若装了）
spec = importlib.util.find_spec("tilescale_ext")
print("tilescale_ext:", spec.origin if spec else "(not installed)")
PY
```

**需要观察的现象**：

- `tilelang/lib/` 下应能看到 `libtilelang.so`（或 `libtilelang_module.so`）、`tilelang_cython_wrapper.*.so`，以及上游 TVM 的 `libtvm*.so`。
- 若 `tilescale_ext` 已安装，`spec.origin` 指向 `tilescale_ext/__init__.py`，其同目录下应有 `_C.*.so`。

**预期结果**：你能在脑中画出「`src/*.cc`（+ `tilelang/utils/ts_ext/*.cpp`）→ 经 CMake → `libtilelang.so` / `tilescale_ext/_C.so` → 被 `env.py` 的 `TL_LIBS` 与 Python import 机制找到」的完整链路。

> 待本地验证：具体 `.so` 文件名带平台与 ABI 后缀（如 `.cpython-311-x86_64-linux-gnu.so`），上机以实际为准。

#### 4.4.5 小练习与答案

**练习 1**：`libtilelang.so` 与 `libtilelang_module.so` 有什么区别？谁在用？
**答案**：`libtilelang.so` 链接了 `tvm + tvm_runtime`，含完整编译器，用于需要编译 kernel 的场景（`__init__.py` 默认加载它）；`libtilelang_module.so` 只链接 `tvm`，更精简，用于「runtime-only」（仅加载已编译模块、不再编译）场景，由 `tvm.base._RUNTIME_ONLY` 控制。

**练习 2**：为什么 `tilescale_ext._C` 的构建条件是 `if(USE_CUDA)` 且 `Torch_FOUND`？
**答案**：该扩展提供 IPC 张量与显存管理（`tensor_from_ptr`、`_create_ipc_handle` 等），底层依赖 CUDA runtime 与 libtorch 的张量结构。没有 CUDA 或没装 torch 时无法构建；此时 `tilelang/__init__.py` 的 `try/except` 会把 `tensor`/`get_allocator` 置为 `None`，单机功能不受影响。

**练习 3**：`pyproject.toml` 里 `"tilelang/src" = "src"` 这条映射为什么必要？
**答案**：codegen 在生成 CUDA/C++ 源码时，需要 include `src/tl_templates/` 等头文件（如 CUTLASS 模板封装）。把 `src/` 打进 wheel 的 `tilelang/src/`，可保证 pip 安装的用户在不克隆仓库的情况下也能让 codegen 找到这些模板头文件（`env.py` 里的 `TL_TEMPLATE_PATH` 会指向这里）。

## 5. 综合实践

把本讲四个模块串起来，完成一份「**TileScale 仓库导览图**」：

1. **画目录树**：照着 4.1 的职责树，结合你本机实际 `ls` 的结果，画一张精简版仓库目录树（只保留 `src/`、`tilelang/`、`tilescale_ext/`、`examples/`、`benchmark/`、`docs/`、`3rdparty/`、`CMakeLists.txt`、`pyproject.toml`），每个节点写一句职责。
2. **标注加载链**：在 `tilelang/__init__.py` 旁边写上 `import tilelang` 的 6 个关键步骤（env → tvm → libinfo → load `.so` → 导出 DSL → 可选分布式），并标出每步对应的行号区间（参考 4.2.3）。
3. **标注产物落点**：在 `CMakeLists.txt` 与 `pyproject.toml` 旁边，分别写清 `libtilelang.so` 与 `tilescale_ext/_C.so` 在「开发模式」和「wheel」两种情况下的落点目录（参考 4.4.2 的对照表）。
4. **用一句话总结**：用一句话说明 `import tilelang` 时依次加载了哪些模块与底层库（把 4.2.4 里的那句话写进去）。

**检验标准**：把这张图交给一个没读过本仓库的人，他应能据此回答「T.gemm 的 C++ 实现在哪」「libtilelang.so 装在哪」「为什么没装 tilescale_ext 也能 import tilelang」三个问题。

## 6. 本讲小结

- 仓库分三层：`src/`（C++ 实现）、`tilelang/`（Python DSL 外壳）、`3rdparty/`（上游 TVM/CUTLASS/CK 等依赖）；`examples/benchmark/testing/docs` 是学习与验证。
- `tilescale_ext/`（顶层入口包）与 `tilelang/utils/ts_ext/`（C++ 源码）是「源码与产物分离」：源码在主包内，产物装到独立包，且 `tilescale_ext` 缺失不影响 `import tilelang`。
- `import tilelang` 的顺序是：`env.py`（算 `TL_LIBS`、配路径）→ `import tvm` → `libinfo` → `ctypes.CDLL` 加载 `libtilelang.so` → 导出 DSL/JIT/编译接口 → 可选挂载 `tensor/get_allocator`。
- `libinfo.find_lib_path` 按平台命名 `.so` 并在 `TL_LIBS` 候选目录里遍历，找不到时把候选目录打印出来——这是诊断「找不到库」的关键。
- `TL_LIBS` 在 pip 模式指向 `tilelang/lib`，开发模式指向 `build/lib` 与 `build/tvm`，与 CMake 的输出目录严格对应。
- 构建由 `scikit-build-core + CMake` 驱动：产物含 `libtilelang.so`（带编译器）、`libtilelang_module.so`（runtime-only）、cython wrapper、以及 CUDA+Torch 时的 `tilescale_ext._C.so`。

## 7. 下一步学习建议

有了这张地图，建议按下面的顺序深入：

- **想会写 kernel** → 进入 u2：从 `tilelang/language/kernel.py`（`T.Kernel`）出发，逐个学 `T.*` 原语（`u2-l1` 启动配置、`u2-l2` 显存层级、`u2-l3` gemm/reduce）。
- **想懂编译流程** → 进入 u3：对照 `tilelang/engine/lower.py` 与 `phase.py`，看 `src/transform/` 的 pass 如何被串联（`u3-l1` 编译总览、`u3-l3` LowerAndLegalize、`u3-l4` OptimizeForTarget）。
- **想懂分布式** → 进入 u6：先读 `tilelang/distributed/`（运行时）与 `src/op/distributed.cc`（设备原语），再回到 `tilescale_ext/_C` 的 IPC 张量（`u6-l1` 总览、`u6-l4` pynvshmem 启动）。
- **想做后端/二次开发** → 直接攻 `src/`：`u7-l1` C++ 算子实现、`u7-l2` CUDA 模板族、`u7-l3` codegen 内部。

无论走哪条线，本讲的「目录→职责」「源码→产物→落点」对照表都是你的随身地图：遇到任何报错，先回到这张图定位文件，再去读实现。
