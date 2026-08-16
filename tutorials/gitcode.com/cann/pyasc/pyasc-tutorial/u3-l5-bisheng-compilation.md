# 面向硬件的编译：毕昇编译器调用与二进制生成

## 1. 本讲目标

上一讲（u3-l4）我们看到 `Compiler.run` 的五步主流程，跑到第四步时手里已经有了一份 Ascend C 源码（`ascendc.cpp`）。本讲往下钻最后一步：**pyasc 如何把这份 C++ 风格的源码变成可以下发到昇腾 NPU 上执行的 Kernel 二进制**。

学完本讲你应该能够：

1. 说清 `CompilationTarget.get` 如何根据 `KernelType`（核类型）与平台推导出毕昇编译器的目标架构（如 `dav-c220-vec`）与公共编译选项。
2. 掌握 `_gen_dst_kernel` 的三条路径：MIX 双目标「编 cube + 编 vec + 链接」、其他 MIX 单目标「编译 + 链接」、纯 AIV/AIC「编译 + 自链接」。
3. 逐项读懂 `_get_compiler_cmd` 拼出的命令行：`-x cce`、`--cce-aicore-arch`、`--cce-aicore-only`、tikcpp 头文件路径、`-D` 宏（`ASCENDC_DUMP`、`__MIX_CORE_MACRO__`、`__NPU_TILING__` 等）。
4. 理解 `CompiledKernel` 这个「编译产物信封」装了什么：二进制字节、核类型、调试开关、Kernel 参数表。
5. 知道毕昇编译器 `bisheng` 与链接器 `ld.lld` 分别通过 `PYASC_COMPILER`、`PYASC_LINKER` 环境变量定位。

## 2. 前置知识

### 2.1 毕昇编译器是什么

在前几讲反复出现的「毕昇编译器」，是 CANN 工具链里的设备侧编译器，可执行文件名叫 `bisheng`。它接收一份以 C++ 语法书写、但带有昇腾扩展（如 `__aicore__`、`__gm__`、Cube/Vector 指令内建）的源文件，把它编译成 AI Core 上可执行的 ELF 镜像。pyasc 不自己生成机器码——它生成 Ascend C 源码，然后把「翻译成机器码」这件事外包给 bisheng。

一个关键认知：**bisheng 是随 CANN 包安装的**，安装后位于 CANN 的 `compiler` 目录下。所以本讲的编译步骤强依赖环境变量 `ASCEND_HOME_PATH`（由 `source <ascend-toolkit>/set_env.sh` 设置），这一点在 [u1-l2 环境搭建](u1-l2-build-and-install.md) 中已经铺过。

### 2.2 核类型：为什么会有 cube 和 vec 两种架构

昇腾 AI 处理器的一个「AI Core」内部可能同时包含：

- **Vector 核（向量核）**：擅长逐元素的向量运算（`asc.add`、`asc.exp` 这类）。
- **Cube 核（矩阵核）**：擅长矩阵乘（Matmul）。

一个算子可能只用向量核（如 Add），可能只用矩阵核（如纯 Cube 的 Matmul），也可能两者混用（如「矩阵乘 + 激活」融合算子，MIX 模式）。这个信息由上一讲的 `KernelType` 枚举承载（[python/asc/runtime/config.py:L36-L45](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/config.py#L36-L45)）：

| 值 | 名称 | 含义（结合本讲用法） |
|---|---|---|
| 0 | `AIV_ONLY` | 只用向量核，单目标编译 |
| 1 | `AIC_ONLY` | 只用矩阵核，单目标编译 |
| 2/3 | `MIX_AIV_HARD_SYNC` / `MIX_AIC_HARD_SYNC` | 混合形态，硬同步，单目标编译 |
| 4/5 | `MIX_AIV_1_0` / `MIX_AIC_1_0` | 混合形态 1.0 版本，单目标编译 |
| 6/7 | `MIX_AIC_1_1` / `MIX_AIC_1_2` | 混合形态 1.1/1.2 版本，**双目标编译**（cube + vec 各编一份再链接） |

回忆上一讲：如果用户没有显式传 `kernel_type`，`run_passes` 会在跑完 Pass 后根据 IR 上的 `asc.compile_mix` 属性自动推导——有该属性则推导为 `MIX_AIC_1_2`（或开启 `matmul_cube_only` 时为 `AIC_ONLY`），否则为 `AIV_ONLY`。所以 `examples/01_add` 走 `AIV_ONLY`，`examples/03_matmul_mix` 走 `MIX_AIC_1_2`，本讲的实践正是对比这两条路径。

### 2.3 平台（CompilePlatform）与芯片代号

`Compiler` 构造时通过 `get_soc_version()`（定义于 [python/asc/lib/runtime/interface.py:L62-L63](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L62-L63)）读取当前 `config.set_platform` 设置的 SoC 型号，然后折叠成一个粗粒度的编译平台枚举 `CompilePlatform`（[python/asc/runtime/compiler.py:L44-L47](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L44-L47)）：只有 `Ascend910B` 与 `Ascend910_93` 两档，判定逻辑在构造函数里（[python/asc/runtime/compiler.py:L94-L96](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L94-L96)）——SoC 名以 `Ascend910_93` 开头归为 `Ascend910_93`，否则归为 `Ascend910B`。平台决定芯片代号 `c220`，再拼进架构字符串。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
|---|---|
| [python/asc/runtime/compiler.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py) | 主角。`CompilationTarget`、`CompiledKernel`、`_gen_dst_kernel`、`_get_compiler_cmd`、`_run_cmd`、`run_compilation` 都在这里 |
| [python/asc/runtime/config.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/config.py) | `KernelType` 八种核类型、`Platform` SoC 枚举、`set_platform` |
| [python/asc/lib/utils.py](https://github.com/gitcode.com/cann-pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/utils.py) | `get_ascend_path()`：从 `ASCEND_HOME_PATH` 找到 CANN 安装根目录 |
| [python/asc/lib/runtime/support.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/support.py) | `CoreType` 枚举（`CompiledKernel` 携带的核类型） |
| [examples/01_add/add.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py) | AIV_ONLY 路径的实践样本 |
| [examples/03_matmul_mix/matmul_mix.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/03_matmul_mix/matmul_mix.py) | MIX_AIC_1_2 路径的实践样本 |

整条调用链在编译器内部的位置：

```text
Compiler.run(mod, func_name)                     # u3-l4 已讲：dump + Pass + 翻译
  └─ run_compilation(source, kernel_args)        # 本讲入口
       ├─ 临时目录：input.cce / output.o
       ├─ _gen_dst_kernel(tmp_dir, src, dst)     # 拼命令、调 bisheng、调 ld.lld
       │    ├─ CompilationTarget.get(...)        # 推导 arch 与公共选项
       │    ├─ _get_compiler_cmd(...)            # 拼完整 bisheng 命令行
       │    └─ _run_cmd(cmd, "compile"/"link")   # 子进程执行 + 失败重试
       ├─ 复制 output.o → dump 目录的 binary.o
       └─ return CompiledKernel(字节, CoreType, enable_debug, kernel_args)
```

## 4. 核心概念与源码讲解

### 4.1 模块一：CompilationTarget —— 目标架构的推导器

#### 4.1.1 概念说明

「同一份 Ascend C 源码」要变成「能在特定硬件单元上跑的机器码」，必须先回答两个问题：

1. **编给哪种核？**（向量核还是矩阵核——决定指令集）
2. **编给哪代芯片？**（决定架构代号）

`CompilationTarget` 就是这两个答案的载体。它是一个冻结 dataclass（`frozen=True`，不可变，天然适合当纯函数的返回值），携带三个架构槽位和三组选项：

```python
@dataclass(frozen=True)
class CompilationTarget:
    common_arch: Optional[str] = None    # 单目标路径用的架构
    vec_arch: Optional[str] = None       # MIX 双目标路径：向量核架构
    cube_arch: Optional[str] = None      # MIX 双目标路径：矩阵核架构
    common_options: List[str] = field(default_factory=list)
    vec_options: List[str] = field(default_factory=list)
    cube_options: List[str] = field(default_factory=list)
```

#### 4.1.2 核心流程

`CompilationTarget.get(kernel_type, platform)` 是一个纯函数（静态方法），推导规则可以画成决策表：

```text
平台 ∈ {Ascend910B, Ascend910_93}？
  ├─ 否 → 抛 RuntimeError（当前代码不可达，见 4.1.3 的阅读提示）
  └─ 是 → arch = "c220"
        ├─ kernel_type ∈ {MIX_AIC_1_1, MIX_AIC_1_2}
        │     → vec_arch="dav-c220-vec", cube_arch="dav-c220-cube"（双目标）
        ├─ kernel_type ∈ {MIX_AIV_1_0, MIX_AIV_HARD_SYNC, AIV_ONLY}
        │     → common_arch="dav-c220-vec"（单目标，向量核）
        └─ 其余 {AIC_ONLY, MIX_AIC_1_0, MIX_AIC_HARD_SYNC}
              → common_arch="dav-c220-cube"（单目标，矩阵核）
```

架构字符串的构成是 `dav-<芯片代号>-<核类型>`：`dav` 即 DaVinci（达芬奇架构），`c220` 是 910B/910_93 这一代的芯片代号，后缀 `-vec`/`-cube` 分别指明向量核与矩阵核目标。这个后缀与后面 `run_compilation` 里推导的 `CoreType.VectorCore`/`CubeCore` 一一呼应。

公共选项（对所有核类型都一样）有五组：

| 选项 | 作用 |
|---|---|
| `-std=c++17` | 按 C++17 标准编译 Ascend C 源码 |
| `--cce-disable-kernel-global-attr-check` | 关闭 kernel 全局属性检查 |
| `-mllvm -cce-aicore-stack-size=0x8000` | 设 AI Core 栈大小 32KB |
| `-mllvm -cce-aicore-function-stack-size=0x8000` | 设函数栈大小 32KB |
| `-mllvm -cce-aicore-dcci-insert-for-scalar=false` | 关闭标量场景的 DCCI 缓存一致性指令插入 |

#### 4.1.3 源码精读

推导逻辑全貌在 [python/asc/runtime/compiler.py:L50-L75](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L50-L75)。其中关键字段定义与静态工厂方法：

- [python/asc/runtime/compiler.py:L59-L70](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L59-L70) —— `get` 的主体：先拼 `common_option` 五件套；两代平台目前共用 `arch = "c220"`；遇到 `MIX_AIC_1_1/1_2` 时同时填 `vec_arch` 与 `cube_arch`，这是「双目标编译」的源头。
- [python/asc/runtime/compiler.py:L71-L74](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L71-L74) —— 其余核类型只填 `common_arch`：AIV 系走 `-vec` 后缀，AIC 系走 `-cube` 后缀。
- [python/asc/runtime/compiler.py:L62-L65](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L62-L65) —— 公共选项列表本体，全是传给 bisheng/LLVM 后端的开关。

两个值得留意的阅读细节（如实指出，不改变结论）：

1. 第 66 行 `if platform == ... or platform == ...` 与外层第 61 行的判断重复，属于冗余但无害的写法。
2. 第 75 行 `raise RuntimeError(f"... {CompilePlatform.value} ...")` 引用的是**类**而不是传入的 `platform` 实例——若真走到这个分支，格式化时会先抛 `AttributeError`。不过由于 `Compiler.__init__` 只会把 `platform` 赋成两个合法值之一（见 [python/asc/runtime/compiler.py:L94-L96](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L94-L96)），该分支当前不可达。这提醒我们：读开源代码时对「不可达的错误处理」也要保持怀疑。

#### 4.1.4 代码实践

**实践目标**：亲手调用 `CompilationTarget.get`，验证决策表。

**操作步骤**（示例代码，非项目原有）：

```python
# verify_target.py —— 示例代码：需要已安装编译好的 pyasc（提供 asc._C）
from asc.runtime.compiler import CompilationTarget, CompilePlatform
from asc.runtime.config import KernelType

for kt in KernelType:
    t = CompilationTarget.get(kt, CompilePlatform.Ascend910B)
    print(f"{kt.name:20s} common={t.common_arch} vec={t.vec_arch} cube={t.cube_arch}")
```

**需要观察的现象**：8 行输出中，只有 `MIX_AIC_1_1` 与 `MIX_AIC_1_2` 两行的 `vec`/`cube` 字段非空，其余 6 行填在 `common` 字段。

**预期结果**：与 4.1.2 的决策表完全一致。运行前提是本机已按 u1-l2 完成 `pip install`（该模块顶层 `from asc._C import ir, ...` 依赖编译产物）。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`AIV_ONLY` 在 `Ascend910B` 平台上得到什么架构字符串？走的是哪个字段？

答案：`dav-c220-vec`，填在 `common_arch` 字段（`AIV_ONLY` 与 `MIX_AIV_1_0`、`MIX_AIV_HARD_SYNC` 同属向量核单目标组）。

**练习 2**：为什么 `MIX_AIC_1_2` 需要两个架构字段，而 `AIC_ONLY` 只需要一个？

答案：MIX_AIC_1_2 的算子里同时有 Cube 部分（Matmul）与 Vector 部分（逐元素计算），两类指令分属不同硬件单元、不同指令集，必须把同一份源码分别编译成 cube 目标和 vec 目标再链接到一起；`AIC_ONLY` 只跑在矩阵核上，一个目标即可。

**练习 3**：把 `Platform.Ascend910B4`（见 config.py 的 SoC 枚举）传给 `CompilationTarget.get` 会怎样？

答案：不会怎么样——`CompilationTarget.get` 接收的是粗粒度 `CompilePlatform`（只有两档），而不是细粒度 `Platform`。910B1/B2/B3/B4 都会先在 `Compiler.__init__` 里折叠成 `CompilePlatform.Ascend910B` 再进入推导。

### 4.2 模块二：_gen_dst_kernel —— 编译与链接的编排器

#### 4.2.1 概念说明

`_gen_dst_kernel` 是本讲的枢纽：它拿到临时目录里的 `input.cce`（Ascend C 源码）与目标路径 `output.o`，负责把「找头文件 → 拼公共选项 → 按核类型编排编译/链接」整件事做完。它解决的核心问题是：**不同 KernelType 需要不同数量的编译目标和不同形式的链接**。

#### 4.2.2 核心流程

先看公共准备段（所有路径共享）：

```text
1. target = CompilationTarget.get(kernel_type, platform)   # 4.1 的推导器
2. ascend_path = get_ascend_path()                         # 读 ASCEND_HOME_PATH
3. tikcpp_path = realpath(ascend_path/compiler/tikcpp)     # bisheng 的头文件根
4. 若存在 <ascend_path>/compiler/include/version/cann_version.h
      → common_options += ["-include", cann_version.h]     # 让版本宏对齐当前 CANN
5. common_options += 三个 -I：
      tikcpp/tikcfw、tikcpp/tikcfw/impl、tikcpp/tikcfw/interface
6. common_options += 用户传入的 bisheng_options（CompileOptions 透传）
7. common_options += target.common_options                  # 4.1 的五件套
```

然后按 `kernel_type` 分三条路径，编译/链接次数不同。用公式直观对比开销（\(t_c\) 为单次编译耗时、\(t_l\) 为链接耗时）：

- MIX 双目标（`MIX_AIC_1_1/1_2`）：\( T = 2t_c + t_l \)
- 其他 MIX 单目标：\( T = t_c + t_l \)
- AIV_ONLY / AIC_ONLY：\( T = t_c + t_l \)（但编译产物只有一个对象文件）

三条路径的编排：

```text
路径 A：kernel_type ∈ {MIX_AIC_1_1, MIX_AIC_1_2}
    bisheng(cube_arch) → output_cube.o      # 同一份 input.cce，按矩阵核架构编
    bisheng(vec_arch)  → output_vec.o       # 同一份 input.cce，按向量核架构编
    ld.lld -m aicorelinux -Ttext=0 output_cube.o output_vec.o -static -o output.o

路径 B：kernel_type ∈ {MIX_AIC_1_0, MIX_AIC_HARD_SYNC, MIX_AIV_1_0, MIX_AIV_HARD_SYNC}
    bisheng(common_arch) → output_mix_aic.o（或 AIV 系的 output_mix_aiv.o）
    ld.lld -m aicorelinux -Ttext=0 output_mix_*.o -static -o output.o

路径 C：其余（AIV_ONLY、AIC_ONLY）
    bisheng(common_arch) → output.o
    ld.lld -m aicorelinux -Ttext=0 output.o -static -o output.o   # 「自链接」
```

链接命令的三个参数值得记住：

- `-m aicorelinux`：指定仿真目标为 aicorelinux（AI Core 的 Linux 仿真环境）。
- `-Ttext=0`：把代码段（text section）的链接起始地址固定为 0，运行时加载时再重定位。
- `-static`：静态链接，产出一个不依赖外部库的完整 ELF 镜像——这正是能被 aclrt 直接下发的形态（运行时会用 `CoreType` 对应的 ELF magic 校验它，见 [python/asc/lib/runtime/interface.py:L83-L93](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L83-L93) 的 `magic_elf_value`，细节留给 u3-l6）。

路径 C 的「自链接」乍看多余（输入输出同名），但目的正是把 bisheng 产出的**可重定位对象文件**加工成**静态可执行 ELF 镜像**，与路径 A/B 的最终产物形态对齐，后续 `run_compilation` 才能统一处理。

命令的实际执行由 `_run_cmd` 完成（[python/asc/runtime/compiler.py:L144-L160](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L144-L160)）：`subprocess.Popen` 起子进程并把 stderr 合并进 stdout；返回非零时，若是带 `--cce-aicore-only` 的编译命令，会先做一次**降级重试**——在命令的第 4、5 位插入 `-mllvm -disable-machine-licm`（关闭机器级循环不变量外提，规避 bisheng 的一个已知优化缺陷）再编一次，仍失败才抛出带完整命令行的 `RuntimeError`，错误消息里的 `Please rerun ...` 就是给用户手工复现用的。

#### 4.2.3 源码精读

- [python/asc/runtime/compiler.py:L274-L290](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L274-L290) —— 公共准备段：取 `CompilationTarget`、解析 tikcpp 路径、条件包含 `cann_version.h`、追加三个 `-I`、并入用户 `bisheng_options` 与 `common_options`。`get_ascend_path` 的实现在 [python/asc/lib/utils.py:L16-L21](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/utils.py#L16-L21)：只读 `ASCEND_HOME_PATH` 环境变量，未设置直接抛 `EnvironmentError`——这就是「没 source set_env.sh 就编译不了」的报错源头。
- [python/asc/runtime/compiler.py:L291-L304](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L291-L304) —— 路径 A：先按 `target.cube_arch` 编出 `output_cube.o`，再按 `target.vec_arch` 编出 `output_vec.o`，最后用 `ld.lld` 把两个对象文件链接成 `output.o`。
- [python/asc/runtime/compiler.py:L305-L319](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L305-L319) —— 路径 B：单次编译到 `output_mix_aic.o`/`output_mix_aiv.o`，再链接。
- [python/asc/runtime/compiler.py:L320-L324](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L320-L324) —— 路径 C：直接编译到 `dst`，再做一次自链接统一产物形态。
- [python/asc/runtime/compiler.py:L193-L209](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L193-L209) —— 调用方 `run_compilation`：在 `pyasc_compiler_` 前缀的临时目录里落 `input.cce`，调 `_gen_dst_kernel`，把产物复制为 dump 目录的 `binary.o`，最后打包成 `CompiledKernel` 返回。临时目录由上下文管理器保证退出即清理，所以磁盘上只有 dump 副本会留下来。

#### 4.2.4 代码实践

**实践目标**：对比 `01_add`（AIV_ONLY，路径 C）与 `03_matmul_mix`（MIX_AIC_1_2，路径 A）两条编译路径的实际差异；确认 bisheng 的定位方式。

**操作步骤**：

1. 准备两份 dump 目录并分别运行两个示例（Model 仿真模式即可，无需 NPU）：

   ```bash
   mkdir -p /tmp/dump_add /tmp/dump_matmul
   PYASC_DUMP_PATH=/tmp/dump_add \
     python3 examples/01_add/add.py -r Model -v Ascend910B1
   PYASC_DUMP_PATH=/tmp/dump_matmul \
     python3 examples/03_matmul_mix/matmul_mix.py -r Model -v Ascend910B1
   ```

2. 若 `01_add` 第二次运行没有产出 dump 文件，说明命中了文件缓存（真编译才会落盘，见 u1-l5）——可删除缓存目录，或参照 03 示例把装饰器临时改成 `@asc.jit(always_compile=True)` 后重跑。
3. 查看两个目录中的 `binary.o`：`ls -l /tmp/dump_add/ /tmp/dump_matmul/`，再用 `file /tmp/dump_*/binary.o` 查看 ELF 类型（如有 `readelf`，可 `readelf -h` 对比两份 `binary.o` 的 Machine/Type 字段）。
4. 定位 bisheng：`which bisheng`，并检查 `echo $PYASC_COMPILER` 是否设置。
5. 验证环境变量的作用（可选）：`PYASC_COMPILER=/nonexistent/bisheng python3 examples/01_add/add.py -r Model`，观察报错。

**需要观察的现象**：

- 两个目录都有 `codegen.mlir`、`ascir.mlir`、`ascendc.cpp`、`binary.o` 四级产物。
- 03 的 `ascir.mlir` 顶层能找到 `asc.compile_mix` 属性，01 没有——这正是两者 kernel_type 分流的直接原因（对照 [python/asc/runtime/compiler.py:L184-L189](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L184-L189)）。
- 步骤 5 应抛出 `Compiler executable is not found, check PYASC_COMPILER environment variable`。

**预期结果**（依据源码推演，**待本地验证**）：

| 对比项 | 01_add | 03_matmul_mix |
|---|---|---|
| kernel_type | `AIV_ONLY`（自动推导） | `MIX_AIC_1_2`（自动推导） |
| 走哪条路径 | 路径 C | 路径 A |
| bisheng 调用次数 | 1（`dav-c220-vec`） | 2（`dav-c220-cube` + `dav-c220-vec`） |
| 链接输入 | 1 个对象文件（自链接） | 2 个对象文件（cube.o + vec.o） |
| `CompiledKernel.core_type` | `VectorCore` | `AiCore` |

`binary.o` 是同一份 `ascendc.cpp` 在两条路径下的不同产物；由于临时目录即时清理，中间的 `output_cube.o`/`output_vec.o` 不会留在磁盘上，只能在源码层面确认其存在（[python/asc/runtime/compiler.py:L292-L297](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L292-L297)）。

**bisheng 的定位答案**：`PYASC_COMPILER` 环境变量指定，未设置时默认取 `bisheng` 并用 `shutil.which` 在 `PATH` 中查找（[python/asc/runtime/compiler.py:L106-L109](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L106-L109)）；链接器同理走 `PYASC_LINKER`，默认 `ld.lld`（[python/asc/runtime/compiler.py:L110-L113](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L110-L113)）。

#### 4.2.5 小练习与答案

**练习 1**：`03_matmul_mix` 一次编译总共产生哪些 `.o` 文件？

答案：临时目录里三个——`output_cube.o`（cube 架构编译）、`output_vec.o`（vec 架构编译）、`output.o`（前两者经 `ld.lld` 链接的最终产物）。dump 目录中的 `binary.o` 是 `output.o` 的副本。

**练习 2**：路径 C 明明只编出一个 `output.o`，为什么还要再用 `ld.lld` 把它「链给自己」一次？

答案：bisheng `-c` 产出的是可重定位对象文件；`ld.lld -m aicorelinux -Ttext=0 -static` 把它变成代码段从 0 地址开始、静态自包含的可执行 ELF 镜像。统一成这个形态后，三条路径的最终产物语义一致，运行时才能按 `CoreType` 校验 ELF magic 并直接加载下发。

**练习 3**：报错 `ASCEND_HOME_PATH is not set, source <ascend-toolkit>/set_env.sh first` 是从哪条链路抛出来的？

答案：`_gen_dst_kernel` → `get_ascend_path()`（[python/asc/lib/utils.py:L16-L21](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/utils.py#L16-L21)）。`_gen_dst_kernel` 需要它拼 tikcpp 头文件路径，所以即使 bisheng 在 PATH 上，没设 `ASCEND_HOME_PATH` 也会失败。

### 4.3 模块三：_get_compiler_cmd —— 命令行的组装车间

#### 4.3.1 概念说明

如果说 `_gen_dst_kernel` 是编排器，`_get_compiler_cmd` 就是把每一个编译选项落到命令行 token 上的车间。它解决的问题是：把 `CompileOptions` 里的语义化选项（`opt_level`、`auto_sync`、`debug`、`kernel_type`…）翻译成 bisheng 认识的命令行参数与预处理宏。

#### 4.3.2 核心流程

一条完整命令的组装顺序（逐段叠加）：

```text
[bisheng, -c, -x, cce, -O<opt_level>]                     # 基本段：编译、语言=cce、优化级别
+ [input.cce, --cce-aicore-arch=<arch>, --cce-aicore-only, -o, output.o]
+ common_options                                          # 4.2 的头文件 -I、cann_version.h、用户选项、五件套
+ [-DASCENDC_DUMP=0 或 1]                                  # 是否启用设备侧 dump（取决于 enable_debug）
+ (debug=True 时)  [-g, -mllvm, --cce-aicore-jump-expand=true]
+ (auto_sync=True 时) [--cce-auto-sync, -mllvm, -api-deps-filter]
+ (auto_sync_log 非空时) [-cce-auto-sync-log=<值>]
+ (MIX_AIC_1_1/1_2 时) [-cce-enable-mix, -D__MIX_CORE_MACRO__=1]
+ (仅 MIX_AIC_1_1)  [-D__MIX_CORE_AIC_RATION__=1]
+ [-D__NPU_TILING__, -DTILING_KEY_VAR=0]                   # 恒定追加
```

几个关键开关的含义：

| 命令行片段 | 来源选项 | 含义 |
|---|---|---|
| `-x cce` | 固定 | 告诉 bisheng 输入是 CCE（昇腾 C 扩展）源码，而不是普通 C++ |
| `-O3`（默认） | `opt_level: Optional[int] = 3` | 优化级别，取值 1/2/3（构造时校验，见 `_check_compile_options`） |
| `--cce-aicore-arch=dav-c220-vec` | `CompilationTarget` | 目标架构，由 4.1 的推导器给出 |
| `--cce-aicore-only` | 固定 | 只做 AI Core 编译（不编 host 侧），也是 `_run_cmd` 降级重试的识别标记 |
| `-DASCENDC_DUMP=0/1` | `enable_debug` | 控制 Ascend C 源码里 `#if defined ASCENDC_DUMP` 段的开关，与 `_gen_init_dump_code` 注入的调试代码配套 |
| `-g` + jump-expand | `debug` | 生成调试信息并展开跳转，便于单步调试 |
| `--cce-auto-sync` + `-api-deps-filter` | `auto_sync`（默认 True） | 让 bisheng 在设备侧按 API 依赖分析自动插入同步指令——这是 pyasc 前端 Pass 之外的第二层自动同步 |
| `-cce-enable-mix` + `-D__MIX_CORE_MACRO__=1` | kernel_type 为 MIX_AIC_1_1/1_2 | 声明混合编译模式，源码中的 Cube/Vector 分段据此被正确分配到两种目标 |
| `-D__NPU_TILING__ -DTILING_KEY_VAR=0` | 固定 | tiling 相关宏，声明 tiling 数据来自 NPU 侧、tiling key 取 0 |

注意 `-D` 宏与 `-I`、`--cce-*` 的分工：`-I`/`-include` 解决「编译期看得到哪些头」，`-D` 解决「头与源码里的条件编译段开还是关」，`--cce-*` 是 bisheng 自身的编译器选项。

#### 4.3.3 源码精读

- [python/asc/runtime/compiler.py:L326-L330](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L326-L330) —— 函数签名与基本段：`opt_level` 拼 `-O3`；`-c -x cce` 声明「只编译、语言为 cce」；源码、架构、`--cce-aicore-only`、输出路径依次排开，最后挂上 `common_options`。
- [python/asc/runtime/compiler.py:L331-L340](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L331-L340) —— `ASCENDC_DUMP` 宏按 `enable_debug` 二选一；`debug=True` 追加 `-g` 与 jump-expand；`auto_sync`（默认真）追加 `--cce-auto-sync` 与 `-api-deps-filter`，日志选项只在 `auto_sync_log` 非空时追加。
- [python/asc/runtime/compiler.py:L341-L347](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L341-L347) —— MIX 专属段：`MIX_AIC_1_1/1_2` 追加 `-cce-enable-mix` 与 `__MIX_CORE_MACRO__`；`MIX_AIC_1_1` 再加 `__MIX_CORE_AIC_RATION__`；末尾恒定追加 tiling 两个宏。
- [python/asc/runtime/compiler.py:L27-L41](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L27-L41) —— `CompileOptions` 数据类：本讲直接消费的字段是 `opt_level`、`auto_sync`、`auto_sync_log`、`bisheng_options`、`debug`、`kernel_type`；`enable_debug` 则不是它直接的字段，而是 `run_passes` 依据 IR 属性 `asc.enable_debug` 与环境变量 `ASCENDC_DUMP`（默认 True）共同算出（[python/asc/runtime/compiler.py:L190-L191](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L190-L191)）。
- [python/asc/runtime/compiler.py:L211-L217](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L211-L217) —— `_check_compile_options`：构造 `Compiler` 时的三道校验——SoC 必须是 910B/910_93 系、`kernel_type` 若给定必须是 0~7 的枚举、`opt_level` 必须 ∈ {1,2,3}。

#### 4.3.4 代码实践

**实践目标**：手工拼出 01_add 的完整编译命令，与运行期行为互相印证。

**操作步骤**：

1. 按 4.2.4 跑通 `01_add` 并拿到 dump 产物。
2. 依据源码手工拼命令（示例代码，路径按本机 CANN 实际位置替换）：

   ```bash
   # 01_add（AIV_ONLY, Ascend910B, 默认选项）应等价于：
   bisheng -c -x cce -O3 \
     /tmp/xxx/input.cce \
     --cce-aicore-arch=dav-c220-vec --cce-aicore-only \
     -o /tmp/xxx/output.o \
     -include $ASCEND_HOME_PATH/compiler/include/version/cann_version.h \
     -I $ASCEND_HOME_PATH/compiler/tikcpp/tikcpp_x64/tikcfw \      # 以 realpath 实际结果为准
     -std=c++17 --cce-disable-kernel-global-attr-check \
     -mllvm -cce-aicore-stack-size=0x8000 \
     -mllvm -cce-aicore-function-stack-size=0x8000 \
     -mllvm -cce-aicore-dcci-insert-for-scalar=false \
     -DASCENDC_DUMP=0 --cce-auto-sync -mllvm -api-deps-filter \
     -D__NPU_TILING__ -DTILING_KEY_VAR=0
   ```

3. （可选实验）把 01_add 的装饰器改为 `@asc.jit(opt_level=1, auto_sync=False)` 重跑，diff 两次的 `binary.o` 大小与编译日志。
4. 把 `_get_compiler_cmd` 的返回值打印出来核对：在本地副本中临时给 `_run_cmd` 加一行 `print(" ".join(cmd))`（不要提交），重跑示例收集真实命令。

**需要观察的现象**：步骤 2 手拼命令与步骤 4 打印的真实命令逐 token 一致（`-I` 路径因 `os.path.realpath` 解析符号链接可能与手拼不同）；步骤 3 中 `opt_level=1` 的 `binary.o` 通常更大（优化变弱）、`auto_sync=False` 时命令里不再出现 `--cce-auto-sync` 与 `-api-deps-filter`。

**预期结果**：命令结构完全对应 4.3.2 的组装表。步骤 3 的体积差异与同步指令差异**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`opt_level` 传 0 或 4 会发生什么？

答案：`Compiler.__init__` 调 `_check_compile_options`，其中 `is_opt_level_valid = self.options.opt_level in [1, 2, 3]` 不成立，构造直接抛 `RuntimeError("Please check input compile option")`（[python/asc/runtime/compiler.py:L88-L92](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L88-L92)、[L216](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L216)）。注意校验发生在构造期，早于任何编译动作。

**练习 2**：`-DASCENDC_DUMP=1` 什么时候会出现？它和 `CompileOptions.debug` 是一回事吗？

答案：当 `self.enable_debug` 为真时出现——即 IR 上打了 `asc.enable_debug` 属性（由 DetectEnableDebug Pass 写入，且环境变量 `ASCENDC_DUMP` 不为 false）。它与 `CompileOptions.debug` 不是一回事：`debug=True` 控制的是 `-g` 调试信息；`enable_debug` 控制的是设备侧 dump/printf 宏，二者在命令上是两段独立追加的选项（[L331-L336](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L331-L336)）。

**练习 3**：`auto_sync`（bisheng 侧自动同步）与上一讲的 `insert_sync`（pyasc Pass 侧自动插同步）是什么关系？

答案：两层独立的自动同步机制。`insert_sync` 在 Python 前端跑的 MLIR Pass 里重建 `set_flag/wait_flag`（作用于 Ascend C 源码生成之前）；`auto_sync` 是把源码交给 bisheng 后，由编译器基于 API 依赖分析在设备侧插入同步（作用于机器码生成阶段）。两者默认都开启，前者默认值 `True`、由 `insert_sync` 选项控制，后者由 `auto_sync` 选项映射为 `--cce-auto-sync -mllvm -api-deps-filter`。

### 4.4 模块四：CompiledKernel —— 编译产物的「信封」

#### 4.4.1 概念说明

编译完成的那一刻，`run_compilation` 要把成果交还给 `JITFunction`（存入缓存、交给 Launcher）。交接不能只给一坨字节——运行时还需要知道「这坨字节该按哪种核加载」「要不要额外分配 dump 缓冲」「kernel 的参数表长什么样」。`CompiledKernel` 就是装下这四样东西的信封，同样是 `frozen=True` 的不可变 dataclass。

#### 4.4.2 核心流程

`run_compilation` 的收尾三步：

```text
1. core_type ← 从 kernel_type 映射：
     MIX_AIC_1_1 / MIX_AIC_1_2            → CoreType.AiCore      （cube+vec 混合，挂在 AI Core 上）
     AIV_ONLY / MIX_AIV_1_0 / MIX_AIV_HARD_SYNC → CoreType.VectorCore
     其余（AIC_ONLY / MIX_AIC_1_0 / MIX_AIC_HARD_SYNC）→ CoreType.CubeCore
2. binary ← dst.read_bytes()              # 读入 output.o 全部字节
3. return CompiledKernel(binary, core_type, enable_debug, kernel_args)
```

四个字段各自的去向：

| 字段 | 类型 | 去向 |
|---|---|---|
| `binary` | `bytes` | Launcher 注册设备二进制并下发执行（u3-l6） |
| `core_type` | `CoreType` | 运行时按核类型选择加载通道与校验 ELF magic（`magic_elf_value`） |
| `enable_debug` | `bool` | 为真时 Launcher 会追加 dump 缓冲参数（见 u3-l6 / u7-l4） |
| `kernel_args` | `Tuple[ir.KernelArgument]` | kernel 参数 ABI 表，来自 `ir.get_kernel_arg_attrs(mod)`（[L172](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L172)），指导参数打包 |

注意 `core_type` 的映射与 `CompilationTarget` 的架构分组**并不完全同构**：MIX_AIC_1_1/1_2 双目标编出 vec+cube 两份代码，但最终镜像整体挂在 `AiCore`（一个 AI Core 含 cube 与 vec 两个执行单元）；而单目标的 MIX_AIV/MIX_AIC 系列分别归 `VectorCore`/`CubeCore`。`CoreType` 枚举定义见 [python/asc/lib/runtime/support.py:L21-L25](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/support.py#L21-L25)，比 kernel_type 多一个 `AiCpu`。

#### 4.4.3 源码精读

- [python/asc/runtime/compiler.py:L78-L83](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L78-L83) —— `CompiledKernel` 字段定义，`core_type` 默认 `CoreType.VectorCore`，其余默认空。
- [python/asc/runtime/compiler.py:L201-L209](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L201-L209) —— core_type 三段映射与最终打包；`dst.read_bytes()` 把 ELF 镜像读进内存，此后不再依赖磁盘。
- [python/asc/runtime/compiler.py:L199-L200](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L199-L200) —— dump 副本：`shutil.copyfile(dst, self.dump_dir / "binary.o")`，这就是 PYASC_DUMP_PATH 四级产物的最后一级。
- [python/asc/runtime/compiler.py:L162-L173](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L162-L173) —— 串联上下文：`run` 是 `@final` 的总入口，先 dump `codegen.mlir`、跑 Pass、dump `ascir.mlir`、翻译成 Ascend C（必要时注入 dump 代码）、dump `ascendc.cpp`，最后 `run_compilation` 返回 `CompiledKernel`。本讲的全部内容都发生在最后一步调用里。

#### 4.4.4 代码实践

**实践目标**：从缓存里摸出一个真实的 `CompiledKernel`，验证字段内容。

**操作步骤**（示例代码）：

1. 在 4.2.4 跑过 01_add 之后，同进程内检查缓存（`kernel_cache` 是 `JITFunction` 实例上的字典，见 [python/asc/runtime/jit.py:L46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L46)；简单办法是把下面片段追加到示例脚本 `vadd_launch` 调用之后、`assert` 之前）：

   ```python
   # 示例代码：查看 JITFunction 的内存缓存（追加到 add.py 的 vadd_custom 末尾）
   for key, k in vadd_kernel.kernel_cache.items():
       print(type(k).__name__, len(k.binary), k.core_type, k.enable_debug)
   ```

2. 对 03_matmul_mix 做同样的事，对比 `core_type`。

**需要观察的现象**：01_add 的记录 `core_type` 打印为 `CoreType.VectorCore`；03 的打印为 `CoreType.AiCore`；`len(k.binary)` 与各自 dump 目录 `binary.o` 的文件字节数一致。

**预期结果**：与 4.4.2 的映射表一致。`kernel_cache` 的确切结构以 [python/asc/runtime/jit.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py) 当前实现为准，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`MIX_AIC_1_2` 编出了 vec 和 cube 两份目标文件，为什么 `CompiledKernel.core_type` 只有一个值 `AiCore`？

答案：因为最终交付给运行时的是**一个**链接后的 ELF 镜像，它的加载单位是 AI Core（其内部含 Cube 与 Vector 两个执行单元）。`core_type` 描述的是镜像挂载的核，而不是编译期间用过的架构数量。

**练习 2**：`CompiledKernel.kernel_args` 从哪里来、给谁用？

答案：在 `Compiler.run` 里由 `ir.get_kernel_arg_attrs(mod)` 从最终 IR 模块提取（[L172](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L172)），描述 kernel 的参数 ABI；消费方是 Launcher——它据此把 Host 侧实参打包成字节流（u3-l6 的 `expand_kernel_args`）。

**练习 3**：为什么 `CompiledKernel` 要设计成 `frozen=True`？

答案：它是「编译结果」的不可变快照：进缓存、跨函数传递，任何中途改写（比如改 core_type）都会造成缓存内容与二进制不一致的隐患。冻结 dataclass 还自带 `__hash__`（配合 tuple 字段），适合做字典值与比较。

## 5. 综合实践

把四个模块串成一次完整的「编译考古」。任务：**给两条编译路径各写一份档案卡**。

1. **跑样本**：按 4.2.4 的命令分别在 `01_add` 与 `03_matmul_mix` 上开启 `PYASC_DUMP_PATH` 运行（Model 模式），得到两套四级产物。
2. **确认核类型来源**：在两份 `ascir.mlir` 里搜索 `asc.compile_mix` 属性，结合 [python/asc/runtime/compiler.py:L184-L189](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L184-L189) 说明两者的 `kernel_type` 是怎么定下来的。
3. **还原命令**：对照 `_gen_dst_kernel`（[L274-L324](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L274-L324)）与 `_get_compiler_cmd`（[L326-L347](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L326-L347)），分别写出两个示例触发的每条 bisheng/ld.lld 命令（可以用临时加 print 的方式取证）。
4. **填写档案卡**（每示例一张）：

   | 档案项 | 01_add | 03_matmul_mix |
   |---|---|---|
   | kernel_type / 推导依据 | | |
   | 目标架构（一条或两条） | | |
   | bisheng 调用次数与关键差异参数 | | |
   | 链接命令与输入对象 | | |
   | binary.o 大小 / ELF 类型 | | |
   | core_type | | |
   | bisheng 定位方式（环境变量与默认值） | | |

5. **收尾**：执行 `PYASC_COMPILER=/nonexistent/bisheng python3 examples/01_add/add.py -r Model`，确认报错文案，然后在自己的环境里记录 `PYASC_COMPILER`、`PYASC_LINKER`、`ASCEND_HOME_PATH` 三个变量的实际值——这三个变量就是本讲编译链的全部外部依赖入口。

无 NPU 时全部步骤均可在 Model 模式完成；涉及运行输出的部分**待本地验证**。

## 6. 本讲小结

- `CompilationTarget.get(kernel_type, platform)` 是纯函数推导器：平台给出芯片代号 `c220`，核类型决定 `dav-c220-vec`/`dav-c220-cube` 的单/双目标组合，并附五件套公共编译选项。
- `_gen_dst_kernel` 有三条路径：`MIX_AIC_1_1/1_2` 双目标「编 cube + 编 vec + ld.lld 链接」，其余 MIX 单目标「编译 + 链接」，`AIV_ONLY`/`AIC_ONLY`「编译 + 自链接」，最终统一为 `-Ttext=0 -static` 的可执行 ELF 镜像。
- 公共选项的三类来源：CANN 安装目录（`ASCEND_HOME_PATH` 下的 tikcpp 头文件与 `cann_version.h`）、用户透传的 `bisheng_options`、`CompilationTarget` 五件套。
- `_get_compiler_cmd` 把语义选项映射为命令行：`opt_level→-O3`、`auto_sync→--cce-auto-sync -api-deps-filter`、`debug→-g`，并用 `-DASCENDC_DUMP`、`-D__MIX_CORE_MACRO__`、`-D__NPU_TILING__` 等宏控制源码中的条件编译段。
- `bisheng`/`ld.lld` 分别由 `PYASC_COMPILER`/`PYASC_LINKER` 环境变量定位（默认在 PATH 上找 `bisheng` 与 `ld.lld`）；`_run_cmd` 失败时会带 `--cce-aicore-only` 的编译命令做一次 `-disable-machine-licm` 降级重试。
- `CompiledKernel` 是不可变信封：`binary` 字节 + `core_type`（MIX_AIC_1_1/1_2→AiCore，AIV 系→VectorCore，AIC 系→CubeCore）+ `enable_debug` + `kernel_args` 参数 ABI 表，交给 Launcher 完成下发。

## 7. 下一步学习建议

`CompiledKernel` 交出去之后的故事就是下一讲 [u3-l6 Launcher：Kernel 参数打包与任务下发](u3-l6-launcher.md)：`core_type` 如何参与 ELF magic 校验、`kernel_args` 如何指导参数按 8 字节对齐打包成 blob、`enable_debug` 如何追加 dump 缓冲参数。想提前建立直觉的话，可以先读 [python/asc/lib/runtime/interface.py:L83-L104](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L83-L104) 的 `magic_elf_value` 与 `msprof_task_type`——它们消费的正是本讲产出的 `core_type`。若你对「为什么 MIX 模式的核数要除以 2」感兴趣，可回看 [examples/03_matmul_mix/matmul_mix.py:L84-L85](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/03_matmul_mix/matmul_mix.py#L84-L85) 的注释（block_num 取 AIC-AIV 组数）。
