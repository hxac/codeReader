# 调试、测试与性能剖析

## 1. 本讲目标

本讲是「缓存、自动调优、运行时与扩展开发」单元的调试篇。前面几讲我们已经能写出内核、读懂 IR、看懂缓存目录，但当内核**算错了**、**编译失败了**、或者**跑得慢**时，该用什么工具定位？

学完本讲，你应当能够：

- 用 `tilus.option.debug.dump_ir()` 把每一条变换（Pass）前后的 IR 落到磁盘，逐 Pass 追踪布局推理、降级、死代码消除等环节对内核做了什么。
- 用 `tilus.option.debug.disable_ptxas_opt()` 关闭 ptxas 优化，得到与源码逐行对应的「可读 PTX/SASS」，定位地址计算、寄存器分配类问题；并知道为何改完发射器（emitter）后要手动清缓存。
- 在缓存目录里找到生成的 `source.cu` / `compile.sh` / `lib.so`，对照 codegen 逻辑读懂最终 CUDA 代码。
- 理解 `tests/conftest.py` 的测试约定与 `_requires` 装饰器提供的 **compile-only 模式**——在没有目标 GPU 的机器上也能验证内核「能编译」。
- 用 `ncu_run` / `nsys_run` / `sanitizer_run` 三件套做性能剖析（找瓶颈）与内存检查（找越界）。

本讲全部围绕**真实源码**展开，给出的每一个命令、选项、文件路径都来自当前仓库（HEAD `9a22de0`）。

---

## 2. 前置知识

阅读本讲前，你应当已经掌握（否则建议先读对应讲义）：

- **编译流水线六阶段**（u3-l1）：`build_program` 依次做 verify → 高层 Tilus IR 优化 → `generate_ir_module` 降级 → Hidet IR 优化 → codegen 出 `source.cu` → nvcc 编译成 `lib.so`。本讲的 `dump_ir` 正是插在这条流水线上的「观察点」。
- **Pass 与仪器（Instrument）框架**（u5-l1）：变换用 `Pass`「改」IR，用 `PassInstrument`「看」IR；`apply_transforms` 会在每个 Pass 前后回调仪器。`DumpIRInstrument` 就是一个仪器。
- **两层 IR 的区别**（u6-l1）：Tilus IR 面向张量/布局/指令，Hidet IR 贴近 CUDA C。`dump_ir` 对两层都落盘，但目录不同。
- **内容寻址缓存**（u8-l1）：编译产物按 `sha256(options_text + 程序文本)[:12]` 存到 `programs/<12位摘要>/`。`dump_ir` 产物也落在该目录里。

两个名词解释：

- **PTX**：NVIDIA 的并行线程执行中间表示，是 CUDA C 编译后的「类汇编」文本，再经 ptxas 汇编成机器码 SASS。读懂 PTX 是排查底层问题的硬功夫。
- **ptxas**：把 PTX 汇编成 SASS 的工具，默认会做寄存器分配等优化；优化后代码与源码对应关系会被打乱，调试时需要关掉它。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/tilus/option.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py) | 注册全部全局选项，提供 `debug.dump_ir`、`debug.disable_ptxas_opt`、`cache_dir` 等便捷函数。 |
| [python/tilus/transforms/instruments/dump_ir.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/instruments/dump_ir.py) | `DumpIRInstrument`：在每个 Pass 前后把程序文本、耗时、高亮 HTML 写入磁盘。 |
| [python/tilus/transforms/base.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/base.py) | `PassContext.dump_ir()` 把仪器挂进上下文；`apply_transforms` 在 Pass 前后回调仪器。 |
| [python/tilus/drivers.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py) | `build_program` 编排六阶段；在 `optimize_program` / `optimize_ir_module` 里据 `dump_ir` 选项挂仪器，并把 `disable_ptxas_opt` 计入缓存键。 |
| [python/tilus/hidet/backend/build.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/backend/build.py) | nvcc 调用处：把 `disable_ptxas_opt` 翻译成 `--opt-level=0`，并加 `-lineinfo` 服务 ncu 源码关联。 |
| [tests/conftest.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/conftest.py) | pytest 会话级初始化：把缓存目录切到 `.test_cache`，每条用例前清显存。 |
| [python/tilus/testing/_requires.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/testing/_requires.py) | `requires` 装饰器与 compile-only 模式：当前 GPU 不支持目标架构时只编译不运行。 |
| [python/tilus/lang/instantiated_script.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py) | `InstantiatedScript.compile()`：编译全部调度但不运行，是 compile-only 的底层入口。 |
| [python/tilus/utils/profiler/ncu.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/profiler/ncu.py) | `ncu_run`：封装 Nsight Compute，导出 `.ncu-rep` 报告。 |
| [python/tilus/utils/profiler/nsys.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/profiler/nsys.py) | `nsys_run`：封装 Nsight Systems，做系统级时间线剖析。 |
| [python/tilus/utils/cuda_sanitizer.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/cuda_sanitizer.py) | `sanitizer_run`：封装 compute-sanitizer，检查越界等内存错误。 |

---

## 4. 核心概念与源码讲解

### 4.1 调试选项与逐 Pass IR 落盘：dump_ir / disable_ptxas_opt

#### 4.1.1 概念说明

Tilus 把内核从「Python `__call__`」编译成「`.so`」要经过十几个变换，中间任何一步出错都很难只看最终 CUDA 代码定位。`dump_ir` 解决的就是**可观测性**：它把每一个 Pass 执行完的整份程序文本写到一个独立文件里，你可以像看「分镜脚本」一样，一帧一帧地看 IR 是怎么演化的。

`disable_ptxas_opt` 解决的是**底层可读性**：默认 ptxas 会做优化，生成的 SASS 与源码对应关系被打乱；关掉它后，PTX/SASS 与你写的 IR/codegen 输出几乎逐行对应，便于排查「地址算错了」「寄存器被错误复用」之类的问题。

两者都是**全局选项**，统一由 `tilus.option` 管理，并且都**会进入缓存键**——这点很关键，后面会专门讲。

#### 4.1.2 核心流程

开启两个调试开关后，一次编译的产物分布如下：

```
<cache_dir>/programs/<12位摘要>/
├── program.txt          # 原始 Tilus IR 文本（缓存键的一部分）
├── options.txt          # 含 disable_ptxas_opt、target 等（缓存键的一部分）
├── ir/                  # ← Tilus IR 各 Pass 的快照（dump_ir 产物）
│   ├── 0_Original.txt
│   ├── 1_DeclareToLet.txt
│   ├── 2_LetPropagation.txt
│   ├── ...
│   ├── lower_time.txt   # 每个 Pass 耗时表
│   └── programs.html    # 带高亮的并排对照页
└── module/              # ← Hidet IR / CUDA 产物
    ├── ir/              # Hidet IR 各 Pass 的快照（dump_ir 产物，见 SaveIRInstrument）
    ├── source.cu        # codegen 生成的 CUDA C 源码
    ├── compile.sh       # 实际执行的 nvcc 命令行（含 -O0/-lineinfo 等）
    └── lib.so           # 编译出的共享库
```

整体调用链（粗体是本讲关心的挂载点）：

1. 用户调用 `kernel(...)` → 最终走到 [drivers.py:build_program](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L280-L325)。
2. `optimize_program` 跑高层 Tilus IR 变换：**若 `dump_ir` 为真，就在 `PassContext` 上挂 `DumpIRInstrument`，目标目录 `cache_dir/"ir"`**。
3. `generate_ir_module` 把 Tilus IR 降级成 Hidet IR。
4. `optimize_ir_module` 跑底层 Hidet IR 变换：**若 `dump_ir` 为真，挂 hidet 的 `SaveIRInstrument` 与 `ProfileInstrument`**。
5. `codegen` 把 Hidet IR 写成 `source.cu`；`compile_source` 用 nvcc 编译，**若 `disable_ptxas_opt` 为真，ptxas 加 `--opt-level=0`**。

一条贯穿始终的隐线：`disable_ptxas_opt` 与 `target` 都被加进了缓存键，所以**开关一变，缓存目录的哈希就变，必然重新编译**。

#### 4.1.3 源码精读

**(1) 选项注册与便捷函数**——所有调试开关的源头在 [option.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py)。两个布尔选项注册时都带了环境变量名，没有 GPU 的 CI 环境也能用 `TILUS_DUMP_IR=1` 开启：

[option.py:L66-L79](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L66-L79) —— 注册 `tilus.debug.dump_ir`（env `TILUS_DUMP_IR`）与 `tilus.debug.disable_ptxas_opt`（env `TILUS_DISABLE_PTXAS_OPT`），默认都是 `False`。

[option.py:L162-L188](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L162-L188) —— `debug.dump_ir()` 与 `debug.disable_ptxas_opt()` 两个静态方法，转发到 hidet 的 `set_option`。`disable_ptxas_opt` 的 docstring 明确说明：开启后 ptxas 会以 `-O0` 调用，禁用全部优化。

> 注意调用方式：是 `tilus.option.debug.dump_ir()`（`debug` 是一个类），不是 `tilus.option.dump_ir()`。

**(2) `DumpIRInstrument` 的三个回调**——这是「逐 Pass 落盘」的核心实现，位于 [dump_ir.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/instruments/dump_ir.py)。它继承自 `PassInstrument`，实现四个钩子中的三个：

[dump_ir.py:L36-L48](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/instruments/dump_ir.py#L36-L48) —— `before_all_passes`：**先删掉旧的 dump 目录**（保证每次干净），再把「还没跑任何 Pass」的原始程序写成 `0_Original.txt`。

[dump_ir.py:L53-L63](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/instruments/dump_ir.py#L53-L63) —— `after_pass`：每跑完一个 Pass，用 `IRPrinter` 渲染整份程序，存成 `{序号}_{Pass名}.txt`，同时记录该 Pass 耗时。

[dump_ir.py:L65-L76](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/instruments/dump_ir.py#L65-L76) —— `after_all_passes`：汇总每个 Pass 的耗时写入 `lower_time.txt`（表格），并把所有分镜渲染成带语法高亮的 `programs.html`，便于浏览器对照。

**(3) 仪器如何被挂进流水线**——`PassContext.dump_ir()` 只是把 `DumpIRInstrument` 追加到仪器列表：

[base.py:L57-L58](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/base.py#L57-L58) —— `dump_ir` 方法的全部实现就一行 `self.instruments.append(DumpIRInstrument(dump_dir))`。

[drivers.py:L89-L93](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L89-L93) —— 高层 Tilus IR 优化阶段：进入 `PassContext` 后，若 `debug.dump_ir` 为真就 `ctx.dump_ir(cache_dir / "ir")`，然后 `apply_transforms` 跑变换。仪器在 [base.py:L105-L112](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/base.py#L105-L112) 的循环里被逐 Pass 回调。

[drivers.py:L184-L186](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L184-L186) —— 底层 Hidet IR 优化阶段：同样据 `dump_ir` 挂 hidet 的 `SaveIRInstrument`（写 IR 快照）与 `ProfileInstrument`（写耗时），落在 `module` 子树下。所以「Tilus IR 看左边的 `ir/`，Hidet IR 看 `module/` 下的快照」。

**(4) `disable_ptxas_opt` 的两处消费**——这个选项同时影响「编译行为」和「缓存命中」。

[build.py:L196-L201](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/backend/build.py#L196-L201) —— nvcc 命令行拼接：`--ptxas-options=-v` 始终带上（让 ptxas 打印寄存器/共享内存用量），当 `disable_ptxas_opt` 为真时追加 `,--opt-level=0`；同时 `-lineinfo` 把源码行号嵌入二进制，**这是后续 Nsight Compute 能把性能数据关联回源码的前提**。

[drivers.py:L213-L219](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L213-L219) —— `get_cache_dir` 计算缓存键时，把 `disable_ptxas_opt` 与 `target` 一并塞进 `options_dict`。**含义**：你昨天用默认（开优化）编过一份，今天为了调试关掉优化再跑，缓存哈希不同，会触发全新编译——这正是我们想要的；但反过来，如果你只改了 emitter 而没改任何选项，缓存键不变（见 u8-l1），这时候**必须手动删 `programs/<hash>/` 才能强制重编**。

**(5) 附赠：`debug_block` 运行期打印**——除了静态落盘，Tilus 还有一个「让指定线程块在运行时把自己的每条指令打印出来」的功能。用户在 Script 类上设类属性 `debug_block = (x, y, z)`（[script.py:L44-L45](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L44-L45)），`build_program` 就会额外追加一条 `inject_print_instruction_pass`（[drivers.py:L86-L87](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L86-L87)）。该 Pass（[inject_print_instruction.py:L44-L53](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/inject_print_instruction.py#L44-L53)）会在 `Allocate/Load/Dot/Cast/Store` 等指令前后注入 `printf`，并用 `block_indices == debug_block` 作为条件（[inject_print_instruction.py:L63-L64](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/inject_print_instruction.py#L63-L64)），从而只有目标块打印——非常适合排查「某一块算错」。注意：当存在多份调度时必须同时设 `debug_schedule`（[instantiated_script.py:L643-L644](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L643-L644)），否则不知道打印哪份。

#### 4.1.4 代码实践

**实践目标**：用 `dump_ir` 看清 vector_add 内核的 IR 演化，并对照缓存里的 `source.cu`。

**操作步骤**：

1. 新建 `dbg_vecadd.py`（示例代码，非项目原有文件）：

   ```python
   import tilus
   tilus.option.cache_dir("/tmp/tilus-dbg")   # 用一个干净、临时的缓存目录
   tilus.option.debug.dump_ir(True)           # 开启逐 Pass 落盘

   import torch
   from examples.vector_add.vector_add import VectorAdd  # 复用项目示例内核

   n = 1024 * 16   # 必须能被 block_elems=1024 整除
   a = torch.randn(n, dtype=torch.float32, device="cuda")
   b = torch.randn(n, dtype=torch.float32, device="cuda")
   c = torch.empty(n, dtype=torch.float32, device="cuda")
   kernel = VectorAdd()
   kernel(n, a, b, c)
   print("done")
   ```

2. 运行后查看产物目录（待本地验证，路径以实际哈希为准）：

   ```bash
   ls /tmp/tilus-dbg/programs/*/ir/
   cat /tmp/tilus-dbg/programs/*/ir/0_Original.txt          # 转译器直出的 IR
   cat /tmp/tilus-dbg/programs/*/ir/lower_time.txt          # 各 Pass 耗时
   ```

3. 打开浏览器看高亮对照页：

   ```bash
   # 路径形如 /tmp/tilus-dbg/programs/<hash>/ir/programs.html
   ```

4. 读最终 CUDA 源码与编译命令：

   ```bash
   cat /tmp/tilus-dbg/programs/*/module/source.cu
   cat /tmp/tilus-dbg/programs/*/module/compile.sh
   ```

**需要观察的现象**：

- `ir/` 下应出现 `0_Original.txt`、`1_DeclareToLet.txt`、`2_LetPropagation.txt`……一直到 `dead_code_elimination.txt` 等多个文件（文件名 = 序号 + Pass 名）。
- vector_add 没有矩阵乘，所以看不到 `layout_inference` 引入 MMA 布局，但能看到 `lower_load_store` 把 `load_global` 展开成带指针/偏移/掩码的 generic 指令。
- `source.cu` 里的 kernel 函数体应当是一段朴素的逐元素 `c[i] = a[i] + b[i]`。

**预期结果**：`ir/` 目录存在且包含至少 5–10 个 `.txt` 分镜；`module/source.cu` 可读；`torch.testing.assert_close(a+b, c)` 通过。

#### 4.1.5 小练习与答案

**练习 1**：为什么修改了某个 emitter 之后，即使反复运行也不会重新编译？该怎么办？

> **答案**：缓存键 = `sha256(options_text + str(prog))`，**只含 Tilus IR 文本与选项，不含 codegen/emitter 输出**（见 u8-l1、[drivers.py:L213-L223](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L213-L223)）。改 emitter 不改变 Tilus IR，故哈希不变、命中旧 `.so`。办法是删除 `programs/<hash>/`（或整个 `.cache/`），或换一个 `cache_dir`。

**练习 2**：`disable_ptxas_opt(True)` 之后，缓存目录的哈希会变吗？为什么？

> **答案**：会变。`get_cache_dir` 把 `disable_ptxas_opt` 显式加进了 `options_dict`（[drivers.py:L216](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L216)），它参与 SHA256，因此开关前后会落到两个不同的 `programs/<hash>/` 目录，各自有独立的 `source.cu` / `.so`。

---

### 4.2 compile-only 测试模式

#### 4.2.1 概念说明

Tilus 的内核按 GPU 架构分代：Ampere（sm_80）、Hopper（sm_90）、Blackwell（sm_100）。一个为 Blackwell 写的 `tma` / `tcgen05` 内核，在只有 Ampere 显卡的机器上**根本跑不起来**，但它的代码**仍然应当能通过编译**——这能在 CI（通常只有有限型号的 GPU）上提前发现语法/布局/发射器层面的回归。

compile-only 模式就是为了这个：**当前 GPU 不支持目标架构时，只编译全部调度、不运行内核、不做 benchmark**，编译通过即视为测试通过。它由测试装饰器 `requires` 与底层方法 `InstantiatedScript.compile()` 配合实现。

#### 4.2.2 核心流程

```
@tilus.testing.requires.nvgpu_sm100a
def test_something():
    kernel = MyBlackwellKernel()
    out = kernel(...)        # ← 在不支持 sm100a 的机器上，这一行被改写成「只编译」
    torch.testing.assert_close(...)   # ← 这行及之后不会执行
```

机制（装饰器在导入期就决定走哪条路）：

1. 装饰器先问 `get_current_target().supports(target)`：**支持** → 原样返回测试函数，照常运行。
2. **不支持**（或取不到 target）→ 返回一个 wrapper。
3. wrapper 运行时**临时替换** `InstantiatedScript.__call__` 为 `compile_only_call`：它调用 `self.compile(*args)` 编译全部调度，然后**抛出哨兵异常 `_CompileOnlyDone`** 短路掉剩下的测试体。
4. wrapper 捕获哨兵，当作测试通过；同时用 `scope(target)` 把编译目标临时切到所需架构，保证按目标架构编译。
5. `finally` 里恢复原始 `__call__`。

关键点：**编译不需要运行时 GPU**（nvcc 是离线编译器），所以无 GPU 的机器也能跑 compile-only 测试；这套机制让同一份测试代码在「有/无目标 GPU」两种环境下都能给出有意义的结论。

#### 4.2.3 源码精读

**（1）装饰器的两条分支**——[_requires.py:L25-L52](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/testing/_requires.py#L25-L52)。导入期判断 `supports_target`：为真直接 `return test_func`，为假进入 wrapper 分支（注释明确说「编译不需要运行时 GPU，因此无 GPU 时落入 compile-only」）。

**（2）猴子补丁 + 哨兵短路**——compile-only 的核心 9 行：

[_requires.py:L57-L77](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/testing/_requires.py#L57-L77) —— `compile_only_call` 调 `self.compile(*call_args, **call_kwargs)` 后 `raise _CompileOnlyDone()`；wrapper 在 `with scope(target):` 里跑测试体，用 `except _CompileOnlyDone: pass` 吞掉哨兵，`finally` 恢复 `InstantiatedScript.__call__`。用异常而非返回值来短路，是因为被替换的是 `__call__`，测试体里可能写出 `out = kernel(...); assert out...`，唯有抛异常能干净地跳出。

**（3）底层 `compile()` 的语义**——它「只编译、不跑、不选优、不落 dispatch」：

[instantiated_script.py:L860-L885](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L860-L885) —— docstring 写明：转译并构建 autotune 空间内**每一份**调度为 `.so`，但不运行、不 benchmark、不持久化 dispatch 选择；编译产物可通过返回的 `jit_instance.valid_programs` / `compiled_programs` 访问。这正是 compile-only 想要的「把全部调度都过一遍编译器」。

**（4）四个现成的架构门槛**——[_requires.py:L83-L88](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/testing/_requires.py#L83-L88) —— `requires.nvgpu_sm80 / sm90 / sm100 / sm100a`，分别对应 Ampere / Hopper / Blackwell / Blackwell+tensor-core(`a` 后缀)。测试里写 `@tilus.testing.requires.nvgpu_sm100a` 即声明「这条用例需要 Blackwell 才能真正运行」。

**（5）测试会话约定**——conftest 把缓存隔离，避免测试互相污染：

[conftest.py:L115-L129](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/conftest.py#L115-L129) —— `pytest_sessionstart` 把缓存目录切到 `{cache_dir}/.test_cache`（注释说明不清缓存是因为 VSCode 可能并行跑测试）；在 CI 环境还会打印 GPU/CUDA/torch 诊断信息，方便把失败和环境对上号。

[conftest.py:L132-L143](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/tests/conftest.py#L132-L143) —— `clear_before_test` 是 `autouse=True` 的 fixture，每条用例前 `torch.cuda.empty_cache()` + `gc.collect()`，释放环形引用占住的资源，减少 OOM 干扰。

#### 4.2.4 代码实践

**实践目标**：亲手验证 compile-only 的「编译但不运行」语义（不依赖具体 GPU 型号）。

**操作步骤**：

1. 写一个临时测试 `tests/test_compile_only_demo.py`（示例代码）：

   ```python
   import tilus
   import tilus.testing

   @tilus.testing.requires.nvgpu_sm100a   # 声明：真正运行需要 Blackwell
   def test_compile_only_demo():
       # 无论当前机器是什么 GPU，进入这里时：
       #   - 若支持 sm100a：真正运行 kernel 并校验；
       #   - 若不支持   ：只编译、然后短路。
       from examples.blackwell_matmul.matmul_v0 import Matmul  # 待确认示例内类名
       import torch
       a = torch.randn(128, 128, dtype=torch.float16, device="cuda")
       b = torch.randn(128, 128, dtype=torch.float16, device="cuda")
       c = torch.empty(128, 128, dtype=torch.float16, device="cuda")
       Matmul()(a, b, c, 128, 128, 128)   # 参数顺序以示例 __call__ 为准（待确认）
       print("这行在 compile-only 模式下不会执行")
   ```

2. 跑这条用例，并查看 `.test_cache` 下是否生成了编译产物：

   ```bash
   python -m pytest tests/test_compile_only_demo.py -s
   ls .cache/.test_cache/programs/    # 期望出现新的 <hash> 目录
   ```

**需要观察的现象**：

- 即便当前 GPU 不是 Blackwell，用例也**通过**（PASS），且终端**不会**打印「这行……不会执行」——证明测试体被哨兵短路。
- `.test_cache/programs/` 下出现新编译目录，里面有 `source.cu` / `lib.so`——证明编译确实发生了。
- 若当前 GPU **是** Blackwell，则用例会真正运行内核，可能打印那行（待本地验证）。

**预期结果**：在不支持 sm100a 的机器上，用例 PASS 且无运行期输出；编译产物落盘。若上面示例的类名/参数与仓库实际不符，请先打开 `examples/blackwell_matmul/matmul_v0.py` 核对 `__call__` 签名再调整（**待确认**）。

#### 4.2.5 小练习与答案

**练习 1**：compile-only 模式为什么用「抛 `_CompileOnlyDone` 异常」而不是让 `compile_only_call` 直接 `return None` 来短路？

> **答案**：因为被替换的是 `__call__`，测试体通常写成 `out = kernel(...); <用 out 的代码>; <断言>`。若只 `return None`，后续对 `out` 的使用或断言仍会执行，可能在「本不该运行」的架构上引发无关失败。抛哨兵异常能在 `__call__` 处立即展开栈、跳出整个测试体，wrapper 再捕获它当作通过（[_requires.py:L65-L74](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/testing/_requires.py#L65-L74)）。

**练习 2**：`conftest.py` 里为什么不在 `pytest_sessionstart` 中清缓存？

> **答案**：注释点明 VSCode 等工具可能**并行**跑测试，清缓存会导致并发用例互相删除对方正在使用的 `.so`。所以只把缓存隔离到 `.test_cache` 子目录，靠 `FileLock`（见 u8-l1）保证多进程编译安全，而不做粗暴的全局清理。

---

### 4.3 性能剖析与内存检查：ncu / nsys / compute-sanitizer

#### 4.3.1 概念说明

当内核「能跑、也对」但「慢」时，需要**剖析（profiling）**找瓶颈；当内核出现离奇数值或崩溃，怀疑越界访问时，需要**内存检查（sanitizer）**。Tilus 把 NVIDIA 的三件套工具各封装成一个函数：

- **Nsight Compute（ncu）**：**内核级**剖析器，针对单个 kernel 给出指令吞吐、访存合并度、occupancy、bank conflict、流水线停顿等上百项指标与「规则（rule）」诊断，回答「这个 kernel 卡在哪」。
- **Nsight Systems（nsys）**：**系统级**剖析器，给整段程序的时间线（CPU/GPU、内核启动、内存拷贝、流重叠），回答「时间花在哪、有没有可重叠的空闲」。
- **compute-sanitizer**：运行期内存错误检查器（越界、未初始化、竞态），相当于 CUDA 版的 valgrind。

三者都通过**子进程 + JSON 序列化参数**的方式工作：Tilus 把目标函数及其参数序列化成 JSON，再用 `python profiler_entry.py user_script.py func_name args.json` 在剖析器包裹下重新导入并调用它。

#### 4.3.2 核心流程

以 `ncu_run` 为例（nsys/sanitizer 同构）：

```
ncu_run(func, *args, kernel_regex=".*", **kwargs)
   │
   ├─ Profiler.run():
   │    1. inspect.getfile(func) 拿到用户脚本路径、func.__name__
   │    2. 找一个未占用的报告路径 <脚本目录>/ncu-reports/report{N}.ncu-rep
   │    3. json.dumps({"args":..., "kwargs":...}) 写临时 JSON
   │    4. 用模板拼出 ncu 命令行（含 --set full、15 条 --rule、--import-source yes）
   │    5. subprocess.run(命令)  ← ncu 作为父进程启动 python 跑你的函数
   │    6. 返回 ProfilerReport(报告路径, ui_binary)
   │
   └─ （被 ncu 拉起的子进程）profiler_main → run_profiled_func:
        读 JSON → import 用户模块 → getattr(func_name) → func(*args, **kwargs)
```

`ProfilerReport.visualize()` 还能拉起 `ncu-ui` / `nsys-ui` 图形界面打开报告。

一个**前置依赖**值得强调：ncu 的 `--import-source yes` 想把性能数据关联回源码，前提是编译时带了 `-lineinfo`——而 Tilus 的 nvcc 命令行**默认就带**（[build.py:L201](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/backend/build.py#L201)），所以无需额外操作即可获得源码关联。

#### 4.3.3 源码精读

**（1）ncu 封装与诊断规则集**——[ncu.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/profiler/ncu.py)：

[ncu.py:L19-L42](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/profiler/ncu.py#L19-L42) —— 命令模板：`--set full`（全指标集）、`--kernel-name regex:"{kernel_regex}"`（只采匹配的 kernel）、十五条 `--rule`（`CPIStall`、`Occupancy`、`SOLBottleneck`、`SOLFPRoofline`、`UncoalescedGlobalAccess`、`SharedMemoryConflicts`、`ThreadDivergence`……）、`--import-source yes`、`--check-exit-code yes`。这些 rule 就是 ncu 自动给出的「瓶颈诊断结论」。

[ncu.py:L44-L58](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/profiler/ncu.py#L44-L58) —— 构造一个 `Profiler` 实例（报告目录 `ncu-reports`、扩展名 `.ncu-rep`、入口脚本 `__file__` 即 ncu.py 自身），对外暴露 `ncu_run(func, *args, kernel_regex=".*", **kwargs) -> ProfilerReport`。

**（2）nsys 封装**——[nsys.py:L19-L33](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/profiler/nsys.py#L19-L33) —— 模板极简：`nsys profile -o {report_path} {python_executable} {python_script} {args}`，报告目录 `nsys-reports`、扩展名 `.nsys-rep`，对外暴露 `nsys_run(func, *args, **kwargs)`。两个剖析器都复用同一个 `Profiler` 基类，差异只在命令模板。

**（3）`Profiler.run` 的通用骨架**——[common.py:L81-L131](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/profiler/common.py#L81-L131) —— 关键细节：用 `inspect.getfile(func)` 定位用户脚本（所以**被剖析的函数必须写在 `.py` 文件里、不能是 REPL 临时对象**）；参数必须可 JSON 序列化（[common.py:L102-L108](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/profiler/common.py#L102-L108)，传 torch 张量会报错——应在函数**内部**创建张量）；报告路径自增编号避免覆盖；命令打印到 stdout 便于复现。

**（4）子进程入口**——[common.py:L134-L157](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/profiler/common.py#L134-L157) —— `run_profiled_func` 读 JSON、动态 import 用户脚本模块、`getattr` 取函数并调用。`profiler_main()`（[common.py:L160-L173](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/profiler/common.py#L160-L173)）是各 entry script 的 `if __name__ == "__main__"` 入口，解析三个位置参数后转发。

**（5）compute-sanitizer 封装**——[cuda_sanitizer.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/cuda_sanitizer.py)，结构与剖析器几乎一致：

[cuda_sanitizer.py:L25-L30](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/cuda_sanitizer.py#L25-L30) —— 命令模板：`--show-backtrace=device --print-limit 128`，把输出重定向到报告文件。

[cuda_sanitizer.py:L73-L146](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/cuda_sanitizer.py#L73-L146) —— `sanitizer_run(func, *args, **kwargs)`：解析 `compute-sanitizer` 路径（[L63-L70](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/cuda_sanitizer.py#L63-L70)，先 `which` 再回退 `/usr/local/cuda/bin/compute-sanitizer`），序列化参数，跑子进程，最后把报告内容打印并落盘到 `sanitizer-reports/report{N}.txt`。

> 调用方式（实际模块路径）：`from tilus.utils.cuda_sanitizer import sanitizer_run`。函数要写在 `__main__` 脚本里（见其 docstring 示例）。

#### 4.3.4 代码实践

**实践目标**：对一个简单内核跑 ncu，找到它的主要瓶颈规则。

**操作步骤**：

1. 把待剖析逻辑写成一个**模块级函数**（不能是 lambda/闭包），张量在函数内创建（示例代码）：

   ```python
   # bench_vecadd.py
   import torch
   from examples.vector_add.vector_add import VectorAdd

   def run_vecadd():
       n = 1024 * 1024 * 16
       a = torch.randn(n, dtype=torch.float32, device="cuda")
       b = torch.randn(n, dtype=torch.float32, device="cuda")
       c = torch.empty(n, dtype=torch.float32, device="cuda")
       kernel = VectorAdd()
       for _ in range(3):       # 多跑几次，确保 ncu 能采到
           kernel(n, a, b, c)
       torch.cuda.synchronize()

   if __name__ == "__main__":
       run_vecadd()
   ```

2. 用 ncu 剖析（待本地验证，需本机装有 `ncu`）：

   ```python
   from tilus.utils.profiler import ncu_run
   from bench_vecadd import run_vecadd
   report = ncu_run(run_vecadd, kernel_regex=".*")  # 生成 ncu-reports/report0.ncu-rep
   report.visualize()   # 可选：拉起 ncu-ui 图形界面
   ```

3. 或命令行直接看报告文本（待本地验证）：

   ```bash
   ncu --import yes --page raw ncu-reports/report0.ncu-rep | less
   ```

**需要观察的现象**：

- 终端会先打印 Tilus 拼出的完整 ncu 命令（每行一个 `--` 参数），可用于手动复现。
- 报告里关注这些 **rule 结论**（ncu.py 模板里列出的）：`SOLBottleneck`（Memory/Compute 哪个是瓶颈）、`UncoalescedGlobalAccess`（全局访存是否合并）、`Occupancy`（占用率）、`CPIStall`（每指令停顿周期）。
- vector_add 是典型的**带宽受限**内核，预期 `SOLBottleneck` 指向 Memory，`UncoalescedGlobalAccess` 应为通过（连续访问）。

**预期结果**：得到一份 `.ncu-rep` 报告，`SOLBottleneck` 规则判定为 Memory Bound；若改剖析 naive matmul（`examples/matmul/matmul_v0.py`），则可能看到计算/访存都未饱和、occupancy 偏低等结论（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ncu_run` 要求被剖析函数「写在 `.py` 文件里」，且参数「必须可 JSON 序列化」？

> **答案**：剖析器靠子进程重新执行你的代码：`Profiler.run` 用 `inspect.getfile(func)` 拿到脚本路径（[common.py:L90-L91](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/profiler/common.py#L90-L91)），再把参数 `json.dumps` 写临时文件（[common.py:L102-L108](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/profiler/common.py#L102-L108)），子进程 `import` 模块后 `getattr(func_name)` 取函数并调用。REPL 临时对象没有文件路径，torch 张量不可 JSON 序列化，所以两者都不行——张量应在函数内部创建。

**练习 2**：ncu 能把性能数据关联回 CUDA 源码行，这依赖编译期的什么设置？Tilus 默认开了吗？

> **答案**：依赖 nvcc 的 `-lineinfo`（嵌入源码行号到二进制）。Tilus 的 nvcc 命令行默认就带 `-lineinfo`（[build.py:L201](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/backend/build.py#L201)），配合 ncu 模板里的 `--import-source yes`（[ncu.py:L39](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/profiler/ncu.py#L39)），无需用户额外操作即可获得源码关联。

---

## 5. 综合实践

把本讲三块内容串起来：**用 `dump_ir` + `disable_ptxas_opt` 生成可读 PTX，再用 ncu 找瓶颈**。以一个 naive matmul 为对象（比 vector_add 更有剖析价值）。

1. 写脚本 `dbg_matmul.py`（示例代码），开启两个调试开关并指定干净缓存：

   ```python
   import tilus
   tilus.option.cache_dir("/tmp/tilus-matmul-dbg")
   tilus.option.debug.dump_ir(True)            # 逐 Pass IR 落盘
   tilus.option.debug.disable_ptxas_opt(True)  # 关 ptxas 优化，得到可读 PTX/SASS

   import torch
   from examples.matmul.matmul_v0 import Matmul  # 待确认示例类名
   M = N = K = 512
   a = torch.randn(M, K, dtype=torch.float16, device="cuda")
   b = torch.randn(K, N, dtype=torch.float16, device="cuda")
   c = torch.empty(M, N, dtype=torch.float16, device="cuda")
   kernel = Matmul()           # 若 v0 用 @autotune/debug_schedule，按需固定
   kernel(a, b, c, M, N, K)    # 参数顺序以 __call__ 为准（待确认）
   ```

2. 读 IR 演化，重点看布局推理：

   ```bash
   # 找到 layout_inference 前后两个分镜，对比寄存器张量的 optional_layout 是否被填充
   ls /tmp/tilus-matmul-dbg/programs/*/ir/
   ```

3. 读可读 PTX（关优化后行号与源码对应）：

   ```bash
   # compile.sh 里能看到 --opt-level=0；用 cuobjdump 从 lib.so 反汇编 PTX
   cat /tmp/tilus-matmul-dbg/programs/*/module/compile.sh
   cuobjdump --dump-ptx /tmp/tilus-matmul-dbg/programs/*/module/lib.so | less
   ```

4. 把上面的运行逻辑包成模块级函数 `run_matmul()`（张量在函数内创建），用 ncu 剖析，定位瓶颈 rule：

   ```python
   from tilus.utils.profiler import ncu_run
   from dbg_matmul import run_matmul
   ncu_run(run_matmul, kernel_regex=".*")
   ```

**串联要点**：

- `dump_ir` 让你看到「naive matmul 经过布局推理后，`dot` 是否被配上了 MMA 布局」（结合 u4-l5、u7-l1）；若没有共享内存分块与流水线，IR 里应是朴素的逐 K 循环。
- `disable_ptxas_opt` 让 PTX 可读，可验证「累加器是否如预期落在寄存器」「地址计算是否正确」。
- ncu 的 `SOLBottleneck` 通常会显示 naive matmul 既非纯带宽也非纯算力受限，`Occupancy` / `LaunchConfiguration` 规则会指出线程块配置的不足——这正是 u7-l1 里 v0→v5 优化的起点。

**预期结果**（待本地验证）：`ir/` 下能看到布局推理前后张量布局从空变为具体；`compile.sh` 含 `--opt-level=0`；ncu 报告判定 naive matmul 远未达算力/带宽上限。若示例类名/签名与仓库不符，请先核对 `examples/matmul/matmul_v0.py` 的 `__call__` 再调整。

---

## 6. 本讲小结

- `tilus.option.debug.dump_ir()` 把每个 Pass 前后的程序文本、耗时、高亮 HTML 落到 `programs/<hash>/ir/`（Tilus IR）与 `module/` 子树（Hidet IR），是追踪 IR 演化的主工具；它通过 `PassContext.dump_ir()` 把 `DumpIRInstrument` 挂进 `apply_transforms` 的回调点。
- `tilus.option.debug.disable_ptxas_opt()` 让 ptxas 以 `--opt-level=0` 运行（[build.py:L197](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/backend/build.py#L197)），得到与源码逐行对应的可读 PTX；它同时进入缓存键，开关即重编。
- 缓存键**不含 codegen/emitter 输出**，所以改 emitter 后必须手动删 `programs/<hash>/` 才能强制重编；读最终 CUDA 看 `module/source.cu`，读编译命令看 `module/compile.sh`。
- compile-only 模式（`tilus.testing.requires.*`）在当前 GPU 不支持目标架构时，用猴子补丁把 `__call__` 改写为「`compile()` + 哨兵异常短路」，编译通过即算测试通过，让 CI 在无目标 GPU 时也能挡住编译期回归。
- 性能剖析三件套：`ncu_run`（内核级，自带 15 条诊断 rule）、`nsys_run`（系统级时间线）、`sanitizer_run`（compute-sanitizer 内存检查）；都靠「子进程 + JSON 参数」重跑用户函数，故被剖析函数须在 `.py` 文件内、参数须可 JSON 序列化。
- `-lineinfo` 默认开启，使 ncu 能把性能数据关联回源码；`debug_block` 类属性可让指定线程块在运行时打印每条指令，是数值排查的利器。

---

## 7. 下一步学习建议

- **写自定义 Pass / 指令**（u8-l5）：本讲的 `dump_ir`、`inject_print_instruction` 都是「仪器/Pass」的典范；当你自己写 Pass 时，`DumpIRInstrument` 配合 `debug.dump_ir` 是验证变换正确性的标配。
- **结合 u7 系列做性能闭环**：用本讲的 ncu 工作流度量 Ampere/Hopper/Blackwell matmul 的瓶颈，再回到 u7-l1~l4 看共享内存分块、wgmma、TMA、软件流水线分别把哪个瓶颈指标改善了。
- **深入 ncu 报告**：项目提供 `/ncu-report` skill，可直接解析 `.ncu-rep` 提取指标、SASS、源码与瓶颈；当你拿到综合实践里的报告后，可用它做二次深入分析。
- **扩展测试基建**：参照 `tests/conftest.py` 与 `tests/transforms/test_dead_code_elimination.py`（u8-l5 提到），把 `collect_instructions`、`verify`、compile-only 模式组合进自己的回归测试，建立「改一处、测全局」的安全网。
