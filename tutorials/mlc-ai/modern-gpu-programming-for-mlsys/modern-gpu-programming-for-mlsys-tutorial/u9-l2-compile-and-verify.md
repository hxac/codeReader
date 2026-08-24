# u9-l2 编译与验证 TIRx 内核

## 1. 本讲目标

上一讲（u9-l1）我们逐段读完了第一个 TIRx 内核 `hgemm_v1`，知道它「写了什么」。本讲解决另外两个问题：**「怎么把它变成能在 GPU 上跑的代码」** 和 **「怎么证明跑出来的结果是对的」**。学完本讲你应该能够：

1. 说出 `tvm.compile(..., tir_pipeline="tirx")` 这一行背后发生的完整流程：`PrimFunc` → `IRModule` → TIRx lowering pipeline（核心是 `LowerTIRx`）→ 主机/设备代码分离 → CUDA 生成。
2. 用 PyTorch 张量直接调用编译产物，并用 fp32 参考实现 + `torch.testing.assert_close` 做数值断言，理解容差 `rtol=2e-2, atol=1e-2` 为什么这么选。
3. 用 `kernel.show()` / `kernel.script()` / `ex.mod.imports[0].inspect_source()` 打印 lowering 前后的两级代码，亲眼看到「一条 `Tx.gemm_async` tile 操作变成 4 条 `tcgen05.mma` 指令」这类映射，为后续读性能内核打下「对照生成代码」的习惯。

本讲的三个最小模块：**tvm.compile 流程**、**PyTorch 参考验证**、**IR 与 CUDA 源检视**。

## 2. 前置知识

本讲不需要新的硬件知识，但依赖以下已经建立的概念（见前置讲义摘要）：

- **`hgemm_v1` 内核本身**（u9-l1）：单 tile、单 CTA、128 线程，计算 `D = ABᵀ`，五阶段组织——分配 SMEM/TMEM → CTA 协作拷贝 A/B → 单线程发起 `Tx.gemm_async` → warpgroup 读回 TMEM 写 GMEM → 释放 TMEM。本讲把它当作「被编译的对象」，不再重读内部细节。
- **TIRx 是 IR 层的 DSL**（u9-l1）：`Tx.*` 是 tile 操作（声明做什么），`T.ptx.*` / `T.cuda.*` 是底层辅助（资源与同步）。
- **环境**（u1-l3）：`pip install apache-tvm==0.26.0 cuda-bindings`，内核必须写在文件或 notebook 单元格里（TIRx 依赖 Python 源码检视解析），不能塞进 `python -c`；真正运行需要 Blackwell GPU（`sm_100a`）。
- **两级代码的直觉**（u1-l3 的五步回路）：构造 `PrimFunc` → 编译 → 调用 → 断言 → 检视。本讲就是把这条回路展开讲透。

再补充三个本讲要用的编译器术语：

| 术语 | 通俗解释 |
|---|---|
| `PrimFunc` | TIR 里「一个函数」的表示。`@T.prim_func` 装饰的 `kernel` 就是一个 `PrimFunc`，它是还没 lowered 的 authored IR。 |
| `IRModule` | 一组 `PrimFunc` 的容器。`tvm.compile` 的输入是 `IRModule` 而不是裸 `PrimFunc`，所以要把 `kernel` 包成 `tvm.IRModule({"main": kernel})`。 |
| pass（编译 pass） | 流水线上的一个变换步骤，对 IR 做一种特定的转换、检查或标注。多个 pass 按固定顺序组成 pipeline。 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [chapter_intro_tirx/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md) | **本讲主源码**。包含 `hgemm_v1` 内核全文、`Compile and Verify the Result` 一节的完整编译验证代码，以及 `How TIRx Is Compiled` 一节对流水线的概述与两级检视代码。 |
| [tirx_guide/arch/lowering_pipeline.rst](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst) | 编译器附录，给出 `tirx_pipeline` 全部 19 个 pass 的表格、`LowerTIRx` 内部结构（`TilePrimitiveDispatch` + `LowerTIRxCleanup`）以及「只跑前几个 pass 看中间 IR」的方法。本讲摘其骨架，完整精读留给 u15-l3。 |
| [appendix/debugging_warp_specialized.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md) | 调试附录，提供「TIRx 写法 → 生成 CUDA 片段」的对照表和保存/搜索生成代码的套路。本讲借它说明检视输出该看什么，方法论完整版留给 u15-l7。 |

注意：这个仓库是一本教材，没有可执行的 `src/` 源码树；「源码精读」的对象是书中的内核代码与它引用的 TVM 机制描述。书中所有内核都在 Apache TVM（`apache-tvm==0.26.0`）中实现，附录给出了 TVM 仓库内的对应文件链接。

## 4. 核心概念与源码讲解

### 4.1 模块一：tvm.compile 流程——一行调用背后的流水线

#### 4.1.1 概念说明

在 u9-l1 里我们写出了 `hgemm_v1(M, N, K)`，调用它返回一个 `PrimFunc`。但 `PrimFunc` 里的 `Tx.gemm_async`、`Tx.cta.copy` 是 **tile 级的声明**——「我要做一个 128×128×64 的分块矩阵乘」「我要把整个 tile 从 GMEM 拷到 SMEM」——GPU 硬件并不认识这些话。把声明变成线程级指令，就是编译器的工作。

关键角色有三个：

- **`IRModule`**：编译的输入容器。`tvm.compile` 不收裸 `PrimFunc`，要包成 `tvm.IRModule({"main": kernel})`，`"main"` 是入口函数名。
- **`target`**：告诉编译器「为哪种硬件生成代码」。书里用 `tvm.target.Target("cuda")`，TVM 会检测当前设备的架构（例如 `sm_100a`）。
- **`tir_pipeline="tirx"`**：选择 **TIRx 专用 lowering 流水线**。这是与普通 TIR 流水线不同的关键参数——只有走这条流水线，`Tx.*` tile 操作才会被理解和 lowered；不指定它，编译器面对 `TilePrimitiveCall` 会无从下手。

#### 4.1.2 核心流程

教材附录把 `tvm.compile(mod, target, tir_pipeline="tirx")` 的总路径画成：

```text
authored TIRx（我们写的 PrimFunc）
      │  BindTarget（把 target 信息挂到模块上）
      ▼
tirx_pipeline（共 19 个 pass，第 1 个就是 LowerTIRx）
（其中的 SplitHostDevice 把 CPU/GPU 两条路径分开）
      ├── host PrimFunc   ──host finalization──▶  C/LLVM（CPU 侧启动器）
      └── device PrimFunc ─device finalization─▶  CUDA（GPU 内核）
```

19 个 pass 中与本讲最相关的四个：

| 顺序 | pass | 做什么 |
|---|---|---|
| 1 | `LowerTIRx` | **核心 lowering**：为每个 tile 操作选择具体实现，把逻辑布局变成物理索引，把 `T.cta_id`/`T.thread_id` 等抽象标识变成线程绑定。 |
| 5 | `FlattenBuffer` | 把剩余的多维 `BufferLoad`/`BufferStore` 摊平成一维访问。 |
| 15 | `SplitHostDevice` | 识别设备区域，把一个函数拆成 host 启动器 + device 内核，并把 host→device 调用 lower 成 kernel-launch ABI。 |
| 16 之后 | `MakePackedAPI` 等 | 把 host 函数改写成 TVM runtime 统一的 packed-function 调用约定。 |

其中 `LowerTIRx` 本身又是一个两步序列：

```text
LowerTIRx = Sequential([ TilePrimitiveDispatch, LowerTIRxCleanup ])
```

- `TilePrimitiveDispatch`：为每个 `TilePrimitiveCall`（copy、gemm、reduction……）**挑选具体后端实现**——比如 `Tx.gemm_async` 的 `dispatch="tcgen05"` 在这里落实为 tcgen05 指令序列。
- `LowerTIRxCleanup`：把逻辑坐标映射成物理索引，让后续 pass 直接面对具体的地址表达式。

最终产物是一个 `Executable`（代码里叫 `ex`），可以直接调用。

#### 4.1.3 源码精读

**编译入口的两行。** 教材正文先给出最核心的两行，见 [chapter_intro_tirx/index.md:232-237](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L232-L237)：先 `target = tvm.target.Target("cuda")`，再把 `kernel` 包进 `IRModule` 传给 `tvm.compile` 并指定 `tir_pipeline="tirx"`。这段代码就是本模块的「最小可记忆单元」。

**target 与管线的文字说明。** [chapter_intro_tirx/index.md:175-179](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L175-L179) 说明：target 直接写 `"cuda"` 即可，TVM 会检测当前设备架构（如 `sm_100a`）；`tir_pipeline="tirx"` 选中 TIRx lowering pipeline。同一段还预告了下一模块的事实——编译出的 `ex.mod(...)` 可以直接接收 PyTorch 张量，无需手动转换。

**对 LowerTIRx 的一段总述。** [chapter_intro_tirx/index.md:239-241](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L239-L241)：`PrimFunc` 先放进 `IRModule` 再交给 `tvm.compile`；中央 pass `LowerTIRx` 利用每个 tile primitive 的 scope、layout、dispatch 三要素选择具体实现，把 `Tx.gemm_async`、`Tx.cta.copy` 这类操作 lower 成低层 TIR；后续 pass 摊平 buffer、分离主机与设备代码、生成设备代码，产出可直接调用的 `Executable`。

**总路径图与两块产出。** [tirx_guide/arch/lowering_pipeline.rst:21-29](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L21-L29) 指出 `tvm.compile` 最终产出两块代码：CPU 侧负责准备参数并 launch 的启动器，和执行计算的 GPU 内核；pass 的确切顺序定义在 Apache TVM 的 `python/tvm/tirx/compilation_pipeline.py`。[tirx_guide/arch/lowering_pipeline.rst:41-54](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L41-L54) 用上图那棵路径树描述 `BindTarget` → `tirx_pipeline` → host/device 各自 finalization 的过程，并解释 host PrimFunc 就是 CPU 侧启动器、device PrimFunc 就是 GPU 内核。

**19 个 pass 的表格。** [tirx_guide/arch/lowering_pipeline.rst:56-63](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L56-L63) 引出按执行次序列出的 19 步表格；其中第 1 步 `LowerTIRx` 见 [tirx_guide/arch/lowering_pipeline.rst:73-76](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L73-L76)，`FlattenBuffer` 见 [tirx_guide/arch/lowering_pipeline.rst:93-95](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L93-L95)，`SplitHostDevice` 见 [tirx_guide/arch/lowering_pipeline.rst:140-143](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L140-L143)。host/device 各自的 finalization pass 清单见 [tirx_guide/arch/lowering_pipeline.rst:161-170](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L161-L170)（host：`LowerTVMBuiltin`、`LowerIntrin`；device：`LowerWarpMemory`、`StmtSimplify`、`LowerIntrin`）。

**LowerTIRx 内部。** [tirx_guide/arch/lowering_pipeline.rst:172-200](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L172-L200) 展开 `LowerTIRx` 的两大职责（选实现 + 布局变索引）与 `TilePrimitiveDispatch` / `LowerTIRxCleanup` 的两步序列，并说明 `LowerTIRx` 之后 tile 操作已被替换成所选实现、抽象标识已变成线程绑定。

#### 4.1.4 代码实践

**实践目标**：不依赖 GPU，亲手构造 `PrimFunc` 并确认 authored IR 里 tile 操作「原样存在」，建立「编译前」这一端的直观；同时把 19-pass 表读成自己的速查表。

**操作步骤**：

1. 新建文件 `hgemm_v1_file.py`（必须是文件，不能 `python -c`，原因见 u1-l3），把 [chapter_intro_tirx/index.md:72-171](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L72-L171) 的 import 与 `hgemm_v1` 函数原样抄入，然后在文件末尾追加：

   ```python
   kernel = hgemm_v1(128, 128, 64)   # 构造 PrimFunc，无需 GPU
   kernel.show()                      # 打印 authored TIRx IR
   ```

2. 运行 `python hgemm_v1_file.py`，在输出中定位三处：`Tx.cta.copy`、`Tx.gemm_async`、`Tx.wg.copy_async`，以及 `T.ptx.tcgen05.alloc` / `T.ptx.mbarrier.init` 这些底层辅助调用。
3. 对照 [tirx_guide/arch/lowering_pipeline.rst:65-159](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L65-L159) 的 19-pass 表，把每个 pass 归类到「TIRx lowering / TIR normalization / Compute legalization / Validation and ABI / Loop lowering / Storage legalization」六类之一，做成自己的速查表。
4. （可选，需本地已装 `apache-tvm`）仿照 [tirx_guide/arch/lowering_pipeline.rst:289-297](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L289-L297) 的单 pass 检视法，用 `TT.BindTarget(target)(mod)` + `TT.LowerTIRx()(mod)` 只跑第一个 pass，再 `print(mod.script())` 观察抽象标识消失、线程绑定出现的瞬间。

**需要观察的现象**：`kernel.show()` 的输出仍然是 tile 级的——一条 `Tx.gemm_async` 就是一个调用节点，没有展开成 4 条 MMA；`T.cta_id` 等还是抽象标识，不是 `blockIdx.x`。

**预期结果**：步骤 1–3 在只装了 TVM、没有 GPU 的机器上也能完成（构造 `PrimFunc` 是纯 IR 操作）。步骤 4 的 `Target("cuda")` 在无设备机器上是否需要显式指定架构、能否走通 `LowerTIRx`，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么必须传 `tir_pipeline="tirx"`？不传会发生什么？

**答案**：`tir_pipeline` 选择 lowering 流水线。`Tx.*` tile 操作以 `TilePrimitiveCall` 节点存在于 IR 中，只有 TIRx 流水线的第 1 个 pass `LowerTIRx`（内部 `TilePrimitiveDispatch`）认识它们并为其挑选具体实现；普通 TIR 流水线没有这个 pass，无法把 tile 操作 lower 成线程级代码。

**练习 2**：`tvm.compile` 的输入为什么是 `tvm.IRModule({"main": kernel})` 而不是 `kernel` 本身？

**答案**：`tvm.compile` 的接口以模块为单位；`IRModule` 是一组 `PrimFunc` 的容器，`"main"` 指定入口函数。后续 pass（如 `AnnotateEntryFunc`、`SplitHostDevice`）都在模块层面工作，需要知道哪个是入口、把 host/device 函数组织在同一个模块里。

**练习 3**：说出编译产物的「两块代码」各是什么、分别走什么 finalization。

**答案**：CPU 侧启动器（host PrimFunc，准备参数并 launch 内核，经 `LowerTVMBuiltin`、`LowerIntrin` 走 C/LLVM）和 GPU 内核（device PrimFunc，经 `LowerWarpMemory`、`StmtSimplify`、`LowerIntrin` 走 CUDA 后端）。

### 4.2 模块二：PyTorch 参考验证——让 PASS 有意义

#### 4.2.1 概念说明

内核写完、编译通过，只说明「语法和类型对了」，不说明「算对了」。TIRx 内核是手写硬件指令序列，布局错一位、barrier 少等一次，结果都会静默出错（这是异步内核的典型故障模式，见 u8 系列）。所以教材的纪律是：**每次编译后立刻与参考实现做数值对照**。

验证回路的四个设计决策：

1. **参考实现用 PyTorch，且先升 fp32 再算**：`D_ref = (A.float() @ B.float().T).half()`。内核内部是 fp32 累加、最后转 fp16；参考也在 fp32 里做矩阵乘再舍入 fp16，两边只在「舍入时机与累加顺序」上有差异，而不是精度等级上的差异。
2. **编译产物直接吃 PyTorch 张量**：`ex.mod(A_tensor, B_tensor, D_tensor)`，不需要手工转 `tvm.ndarray`，降低验证脚本的摩擦。
3. **容差不是 0**：fp16 输出本身的表示精度有限，且内核与 cuBLAS/PyTorch 的累加顺序不同，逐位相等既不可能也无必要。教材用 `rtol=2e-2, atol=1e-2`。
4. **先打印 max error 再断言**：`assert_close` 失败时只有一个异常，先打印 `max_err` 能留下量级信息，便于判断是「正常舍入误差」还是「完全算错」。

#### 4.2.2 核心流程

`torch.testing.assert_close` 对浮点张量的判据是每个元素满足：

\[ |a - b| \;\le\; \text{atol} + \text{rtol} \cdot |b| \]

其中 \(a\) 是实际值（内核输出 D）、\(b\) 是期望值（`D_ref`）。也就是说容差随期望值的绝对大小放缩：元素值越大，允许的绝对误差越大。验证流程：

```text
1. target = Target("cuda"); 构造 kernel = hgemm_v1(128,128,64)
2. with target: ex = tvm.compile(IRModule({"main": kernel}), tir_pipeline="tirx")
3. 清理并同步 CUDA 状态（empty_cache + synchronize）
4. A/B 用 randn、D 用 zeros，均为 cuda 上的 fp16 张量
5. ex.mod(A, B, D)                # 内核写入 D
6. D_ref = (A.float() @ B.float().T).half()   # fp32 参考
7. 打印 max_err；torch.testing.assert_close(D, D_ref, rtol=2e-2, atol=1e-2)
8. 打印 PASS
```

#### 4.2.3 源码精读

**验证代码全文**位于 [chapter_intro_tirx/index.md:181-205](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L181-L205)，可拆成三段读：

- **编译段**（[chapter_intro_tirx/index.md:184-190](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L184-L190)）：设定 `M, N, K = 128, 128, 64`（与内核里 `BLK_M, BLK_N, BLK_K = 128, 128, 64` 对应，grid 恰为 1×1），`with target:` 上下文中执行 `tvm.compile`。这一段就是模块一流程的落地。
- **张量准备与调用段**（[chapter_intro_tirx/index.md:192-198](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L192-L198)）：先 `torch.cuda.empty_cache()` 与 `torch.cuda.synchronize()` 把显存与流状态清干净；`A`/`B` 用 `randn`、`D` 用 `zeros`（输出缓冲无需初始化为随机值）；然后 `ex.mod(A_tensor, B_tensor, D_tensor)` 一行完成调用——正文 [chapter_intro_tirx/index.md:179](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L179) 特别强调编译产物直接接受 PyTorch 张量。
- **参考与断言段**（[chapter_intro_tirx/index.md:200-204](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L200-L204)）：参考 `(A_tensor.float() @ B_tensor.float().T).half()` 注意转置作用在 fp32 化之后的 B 上（对应 `D = ABᵀ` 的约定）；`max_err` 打印最大绝对误差；`assert_close` 带 `rtol=2e-2, atol=1e-2`；最后 `print("PASS")`。

正文 [chapter_intro_tirx/index.md:207](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L207) 给出判读标准：程序打印 `PASS`，即编译内核与 PyTorch 参考在所选容差内一致。

#### 4.2.4 代码实践

**实践目标**：建立「容差判据」的手感——给定一组误差和元素值，能预判 PASS/FAIL；有 GPU 时进一步观察真实误差量级。

**操作步骤**：

1. **（无 GPU 可做）纯 CPU 推演容差**：写一小段 PyTorch CPU 脚本，构造几个 `(实际值 a, 期望值 b)` 对，用公式 \( |a-b| \le 10^{-2} + 2\times 10^{-2}\,|b| \) 手工判定，再与 `torch.testing.assert_close` 的结果对照。建议覆盖：\(b=1.0, a=1.02\)（应通过，恰在边界）、\(b=0.0, a=0.02\)（边界）、\(b=100.0, a=100.5\)（应通过，因为容差随 \(|b|\) 放大到 ±2.01）。
2. **（无 GPU 可做）误差来源分析**：在 CPU 上用 fp32 算一次 `A @ B.T`，再模拟「fp16 输入、fp32 累加」的路径（把 A/B 舍入 fp16 后升回 fp32 再乘），比较两条路径的差异量级，体会 `rtol=2e-2` 留出的余量。
3. **（需 Blackwell GPU）真实回路**：运行 [chapter_intro_tirx/index.md:181-205](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L181-L205) 的完整脚本，记录打印出的 `Max error vs torch reference` 数值。
4. **（需 GPU）收紧容差**：把 `rtol` 从 `2e-2` 改成 `1e-3`、`atol` 改成 `1e-4`，重跑并观察断言是否失败、失败信息里报告的最大差异是多少。

**需要观察的现象**：步骤 3 应打印远小于容差的 max_err（具体量级**待本地验证**）；步骤 4 收紧容差后若失败，异常会报告首个不满足判据的元素位置与两边数值——这是定位「算错」还是「精度不够」的第一手信息。

**预期结果**：步骤 1–2 在任何机器可完成；步骤 3–4 需要 `sm_100a` 设备，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么参考实现要 `.float()` 之后再做矩阵乘，而不是直接用 fp16 矩阵乘？

**答案**：内核的累加器是 fp32（`acc_type = float32`），只在输出时转 fp16。参考若用 fp16 乘累加，自身会引入比内核大得多的舍入误差，对照就分不清「内核错」还是「参考不准」。两边都在 fp32 域做乘加、最后统一舍入 fp16，差异只剩下舍入时机与求和顺序，量级可控。

**练习 2**：把 `D_tensor` 从 `torch.zeros` 改成 `torch.randn` 初始化，验证还成立吗？

**答案**：成立（前提是内核确实写满了整个 D）。内核会把 128×128 的每个元素都写一遍，初始值被完全覆盖；`randn` 初始化反而是个免费的「检查内核是否漏写」手段——若有元素没被写，会残留随机值，误差立刻爆掉。教材用 `zeros` 是保守写法。

**练习 3**：`max_err` 打印发生在 `assert_close` 之前，这个顺序有什么好处？

**答案**：断言失败会抛异常终止，只留下异常信息；先打印 max_err 保证即使失败也留下了误差量级——是 1e-3 级（可能是精度/容差问题）还是 1e1 级（大概率是布局或同步错了），这一信息直接决定排查方向。

### 4.3 模块三：IR 与 CUDA 源检视——对比两个抽象层级

#### 4.3.1 概念说明

TIRx 的学习曲线陡，很大程度因为「我写的」和「GPU 执行的」隔着整条 lowering 流水线。教材给出一对检视 API，让你同时看到两端：

| 调用 | 展示什么 | 位于流水线哪一端 |
|---|---|---|
| `kernel.show()` | 打印 authored TIRx `PrimFunc`（tile 操作层） | 编译前 |
| `kernel.script()` | 返回同一 `PrimFunc` 的文本（字符串形式，可存盘/diff） | 编译前 |
| `ex.mod.imports[0].inspect_source()` | 打印最终生成的 CUDA C 源码 | 编译后 |

`ex.mod` 是 host 模块；它 import 恰好一个 device 模块，`imports[0]` 就是生成的 CUDA 模块。调试附录还给出带参数形式 `inspect_source("cuda")` 用于显式指定语言。

这对输出为什么重要：

1. **验证推演**：u9-l1 我们从 `K=64`、每条 `tcgen05.mma` 沿 K 前进 16 个元素推出「一条 `Tx.gemm_async` 展开 4 条 MMA」——生成代码里数一数即可确认。
2. **建立映射直觉**：scope 变成控制流（`warp_id == 0` 变成位运算守卫），layout 变成地址计算，dispatch 变成具体指令（`tcgen05`、`cp.async.bulk.tensor`）。
3. **无 GPU 的学习路径**：`show()`/`script()` 不需要编译成功即可查看；即便不能运行内核，仍能研究「编译器如何理解我的代码」。

#### 4.3.2 核心流程

两级对照的读法（先看高级端，再到低级端找对应物）：

```text
authored IR（kernel.show()）                  生成 CUDA（inspect_source()）
─────────────────────────────                 ─────────────────────────────
Tx.gemm_async(...)           ──────────────▶  4 条 tcgen05.mma 指令序列
dispatch="tcgen05"                            （K 每 16 步进一次，共 64/16=4）
T.ptx.tcgen05.commit(...)    ──────────────▶  tcgen05 commit（挂到 mbarrier）
T.ptx.mbarrier.try_wait(..)  ──────────────▶  mbarrier try_wait 相位等待
Tx.wg.copy_async(...)        ──────────────▶  warp 集体的 tcgen05.ld 序列
T.cuda.cta_sync()            ──────────────▶  __syncthreads();
if warp_id == 0:             ──────────────▶  (warp_id_in_cta & 3) == 0
if T.ptx.elect_sync():       ──────────────▶  tvm_builtin_elect_one_sync_op()
```

注意右列的映射来自调试附录的对照表，`hgemm_v1` 只用到其中一部分（它没有 TMA、没有 wg_id 分支）。检视生成代码时的推荐动线是**先搜索、后通读**：搜 `tcgen05` 确认 Tensor Core 路径生成、搜 `mbarrier_init` 确认屏障初始化存在且在角色分支之前、搜 `__syncthreads` 确认 CTA 级同步的位置。

#### 4.3.3 源码精读

**「4 条 MMA」的出处。** [chapter_intro_tirx/index.md:59-66](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L59-L66)：矩阵乘表达为一条 `Tx.gemm_async` tile 操作，它描述完整的 128×128×64 tile GEMM；因为每条底层 `tcgen05.mma` 沿 K 前进 16 个元素，编译器发射 4 条 MMA 指令覆盖整个 K 维，确切序列由 shape、layout、dispatch 信息推出。同段还给出四阶段读码框架（分配/拷贝/发起/写回）。

**检视 API 三行。** [chapter_intro_tirx/index.md:243-250](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L243-L250)：`kernel.show()` 与 `print(kernel.script())` 打印 lowering 前的 `PrimFunc`；`print(ex.mod.imports[0].inspect_source())` 打印最终 CUDA C 源码。

**对照的意义。** [chapter_intro_tirx/index.md:252](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L252)：比较这两级，可以看到一个 tile 操作生成了哪些底层指令、它的 layout 与线程 scope 如何变成具体的地址计算与控制流。

**为什么 `imports[0]` 是 CUDA 模块。** [tirx_guide/arch/lowering_pipeline.rst:302-313](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L302-L313)：跑完整条流水线后，host 模块恰好 import 一个 device 模块，`imports[0]` 即生成的 CUDA 模块，`inspect_source()` 返回其源码；该节用更简单的 scale 内核演示，输出应包含 `blockIdx.x`、`threadIdx.x` 与逐元素乘 2。

**TIRx 写法 → 生成 CUDA 的对照表。** [appendix/debugging_warp_specialized.md:75-85](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L75-L85) 给出逐条映射：`wg_id == 0` → `(warp_id_in_cta >> 2) == 0`、`warp_id == 0` → `(warp_id_in_cta & 3) == 0`、`lane_id == 0` → `(((int)threadIdx.x) % 32) == 0`、`elect_sync()` → `tvm_builtin_elect_one_sync_op()` 等。

**搜索清单。** [appendix/debugging_warp_specialized.md:87-95](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L87-L95)：通读生成内核前先扫这些字符串——`tcgen05`（Tensor Core 路径已生成）、`cp.async.bulk.tensor`（拷贝 lower 成了 TMA）、`mbarrier_init`（屏障初始化存在且在角色分支之前）、`__syncthreads();`（由 `T.cuda.cta_sync()` 生成，不得落在 warpgroup-only 分支内）。保存生成代码的文件化写法见 [appendix/debugging_warp_specialized.md:62-74](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L62-L74)。

#### 4.3.4 代码实践

**实践目标**：完成本讲的主实践——写一份「tile 操作如何变成线程级代码」的观察笔记，用两级输出互相印证。

**操作步骤**：

1. 在 4.1.4 的 `hgemm_v1_file.py` 基础上，把末尾改成：

   ```python
   kernel = hgemm_v1(128, 128, 64)
   kernel.show()                    # 层级 1：authored TIRx IR
   src = kernel.script()
   open("hgemm_v1_authored.py", "w").write(src)   # 存盘便于 diff
   ```

   运行后确认输出中 `Tx.gemm_async` 是**一个**调用节点。

2. 若本地能完成 `tvm.compile`（需要 TVM；目标架构可用性**待本地验证**），追加：

   ```python
   target = tvm.target.Target("cuda")
   with target:
       ex = tvm.compile(tvm.IRModule({"main": kernel}), target=target, tir_pipeline="tirx")
   print(ex.mod.imports[0].inspect_source())
   ```

3. 在生成 CUDA 中依次搜索并记录命中次数：`tcgen05.mma`（预期与 4 条 MMA 的推演对照）、`mbarrier`（init 与 try_wait 各一处）、`__syncthreads`（对应三次 `cta_sync`）、`elect`（`tvm_builtin_elect_one_sync_op`）。
4. 用 [appendix/debugging_warp_specialized.md:75-85](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L75-L85) 的对照表，把 `if warp_id == 0:` 的守卫在生成代码里找出来。
5. 把观察结果填进下面的笔记模板（示例代码框架，非项目原有代码）：

   | authored IR 中的写法 | 生成 CUDA 中的对应物 | 命中次数 | 与预期是否一致 |
   |---|---|---|---|
   | `Tx.gemm_async` | `tcgen05.mma...` | ？（预期 4） | ？ |
   | `T.ptx.tcgen05.commit` | … | ？ | ？ |
   | `T.ptx.mbarrier.try_wait` | … | ？ | ？ |
   | `Tx.wg.copy_async` | `tcgen05.ld...` | ？ | ？ |
   | `T.cuda.cta_sync()` | `__syncthreads();` | ？（预期 3） | ？ |

**需要观察的现象**：层级 1 中 tile 操作未展开；层级 2 中 `tcgen05.mma` 出现的次数、守卫表达式的位运算形态、`__syncthreads` 的位置（不得在任何单 warp 分支内）。

**预期结果**：`tcgen05.mma` 命中 4 次左右（以生成代码实际为准，**待本地验证**；若编译器合并或重复发射，如实记录并分析原因）。无 GPU 时步骤 2 起不可执行，笔记退化为「层级 1 + 推演列」，并在表格里标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`kernel.show()` 和 `kernel.script()` 内容基本相同，为什么还要两个 API？

**答案**：`show()` 是「打印到屏幕」的便捷方法，适合交互式查看；`script()` 返回字符串，适合程序化处理——写入文件、在不同版本间 diff、在测试中做字符串断言。学习时用 `show()`，做笔记和对比时用 `script()` 存盘。

**练习 2**：如果生成 CUDA 里搜不到 `tcgen05`，最可能是什么问题？

**答案**：dispatch 路径没有按预期 lower。对照调试附录的编译失败表（[appendix/debugging_warp_specialized.md:51-60](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L51-L60)）：可能是 `dispatch=` 参数与目标能力不匹配（`tcgen05` 路径要求 Blackwell）、target 不是 `sm_100a`，或安装的 TVM wheel 版本与教程代码不一致。先查这些，再回头改内核。

**练习 3**：两级检视对无 Blackwell GPU 的读者价值何在？

**答案**：`show()`/`script()` 只构造 IR、不需要设备，永远可看；`inspect_source()` 若能在显式指定目标架构的条件下编译成功，也无须真正运行内核即可看到生成代码（能否如此编译**待本地验证**）。因此即使不能执行，仍能研究 scope/layout/dispatch 到线程级代码的映射——这正是 u9-l3 三要素分析所需的全部素材。

## 5. 综合实践

把三个模块串成一个完整的「编译—验证—检视」脚本。在 `hgemm_v1_file.py` 中组织成三段（以下为示例代码框架，内核本体须原样取自 [chapter_intro_tirx/index.md:85-171](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L85-L171)）：

```python
# 第一段：构造与 authored IR 检视（无 GPU 可运行）
kernel = hgemm_v1(128, 128, 64)
open("hgemm_v1_authored.py", "w").write(kernel.script())

# 第二段：编译 + PyTorch 验证（需 Blackwell GPU，见 u1-l3 环境要求）
target = tvm.target.Target("cuda")
with target:
    ex = tvm.compile(tvm.IRModule({"main": kernel}), target=target, tir_pipeline="tirx")
A = torch.randn(128, 64, dtype=torch.float16, device="cuda")
B = torch.randn(128, 64, dtype=torch.float16, device="cuda")
D = torch.zeros(128, 128, dtype=torch.float16, device="cuda")
ex.mod(A, B, D)
D_ref = (A.float() @ B.float().T).half()
print(f"Max error vs torch reference: {float((D - D_ref).abs().max()):.6f}")
torch.testing.assert_close(D, D_ref, rtol=2e-2, atol=1e-2)
print("PASS")

# 第三段：生成 CUDA 检视（依赖第二段编译成功）
open("hgemm_v1_generated.cu", "w").write(ex.mod.imports[0].inspect_source("cuda"))
```

然后完成两份交付物：

1. **观察笔记**（4.3.4 的表格填完整），核心回答一个问题：一条 `Tx.gemm_async` 在生成代码里变成了什么？多少条指令、按什么顺序、每条覆盖 K 的哪 16 步？
2. **流程自述**：用自己的话（不超过 10 行）写出从 `PrimFunc` 到 `PASS` 经过的主要阶段，并标注每个阶段「出过错你会看到什么症状」——例如编译期症状查 target/dispatch/scope（对照 [appendix/debugging_warp_specialized.md:51-60](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L51-L60)），运行期数值症状先看 max_err 量级再查布局与同步。

无 GPU 环境只交付：第一段 + 观察笔记的「层级 1 + 推演」版 + 流程自述，并附环境限制清单（沿用 u1-l3 的做法）。所有依赖设备的观察项一律标注「待本地验证」，不得臆造输出。

## 6. 本讲小结

- `tvm.compile(tvm.IRModule({"main": kernel}), target="cuda", tir_pipeline="tirx")` 是编译入口：`tir_pipeline="tirx"` 选中 TIRx 专用流水线，19 个 pass 中第 1 个 `LowerTIRx`（= `TilePrimitiveDispatch` + `LowerTIRxCleanup`）负责为 tile 操作选实现、把布局变成物理索引、把抽象线程标识变成线程绑定。
- 流水线后段由 `FlattenBuffer` 摊平访存、`SplitHostDevice` 拆出 CPU 启动器与 GPU 内核，各自 finalization 后产出可直接调用的 `Executable`——host 与 device 是两块代码。
- 验证纪律：`ex.mod(...)` 直接收 PyTorch 张量；参考实现先 `.float()` 升 fp32 再乘、最后 `.half()`，与内核「fp32 累加、fp16 输出」对齐；容差 `rtol=2e-2, atol=1e-2`，判据为 \( |a-b| \le \text{atol} + \text{rtol}\,|b| \)；先打印 max_err 再断言。
- 两级检视是一对固定的观察窗口：`kernel.show()` / `kernel.script()` 看 lowering 前的 tile 级 IR，`ex.mod.imports[0].inspect_source()`（可带 `"cuda"` 参数）看生成的 CUDA C 源码。
- 两级对照能直接验证教材的关键断言：一条 `Tx.gemm_async` 因 K 每 16 步进而展开为 4 条 `tcgen05.mma`；`warp_id == 0` 变成位运算守卫、`cta_sync()` 变成 `__syncthreads()`、`elect_sync()` 变成 `tvm_builtin_elect_one_sync_op()`。
- 无 Blackwell GPU 时，IR 构造与 `show()`/`script()` 检视仍然可用，学习路径不中断；依赖设备的结果一律如实标注「待本地验证」。

## 7. 下一步学习建议

- **下一讲 u9-l3「Scope、Layout、Dispatch 三要素」**：本讲我们看到编译器「用三要素选实现」，下一讲反过来把 `hgemm_v1` 的每个 tile 操作按三要素填表，并预测「把 GMEM→SMEM 拷贝改派 TMA」要动哪些代码——那是 GEMM Step 4 的伏笔。
- **单元十（u10）**：`TileLayout`、命名轴与 swizzle 的完整 API，本讲只见到 `S[(128, 512):(1@TLane, 1@TCol)]` 一个实例。
- **想深挖编译器**：直接跳读 [tirx_guide/arch/lowering_pipeline.rst](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst) 的 19-pass 表与 scale 内核逐级 lowered 示例（u15-l3 会系统精读）。
- **想深挖生成代码排查**：[appendix/debugging_warp_specialized.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md) 的「先搜后读」清单与角色工作表（u15-l7 的主题），本讲的观察笔记就是那张工作表的第一行。
