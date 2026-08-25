# u15-l3 编译器内部：TIRx lowering pipeline

## 1. 本讲目标

u9-l2 里我们已经把 `tvm.compile(..., tir_pipeline="tirx")` 当作黑盒用过：塞进一个 `PrimFunc`，吐出一个能收 PyTorch 张量的 `Executable`。本讲打开这个黑盒，钻进书的编译器附录（Compiler Internals）。读完本讲，你应该能够：

1. 按执行顺序列出 `tirx_pipeline` 的 **19 个 pass**，并说出每个 pass 的职责与所属类别。
2. 讲清 **`LowerTIRx`** 内部的两步结构（`TilePrimitiveDispatch` + `LowerTIRxCleanup`），以及它如何用三要素（scope/layout/dispatch）把 tile 操作变成底层 TIR。
3. 解释**缓冲展平**（`FlattenBuffer`）与**主机/设备分离**（`SplitHostDevice`）这两组关键变换做了什么、为什么必须做。
4. 跟踪一个具体的 tile 操作——`Tx.gemm_async`——从 authored IR 到生成 CUDA 源码的**完整形态变化**，写成一份流水账笔记。
5. 掌握「只跑前几个 pass 就停下来看中间 IR」的检视技巧（`TT.BindTarget` + `TT.LowerTIRx`）。

## 2. 前置知识

本讲属专家层（advanced），依赖 u9-l2 建立的编译回路认知。用四段话把需要的背景补齐：

- **编译回路回顾（u9-l2）。** `tvm.compile(tvm.IRModule({"main": kernel}), target="cuda", tir_pipeline="tirx")` 把 TIRx `PrimFunc` 编译成可执行对象；`kernel.show()` / `kernel.script()` 看 lowering 前的 tile 级 IR，`ex.mod.imports[0].inspect_source()` 看生成的 CUDA 源码。本讲就是回答「这两个端点之间发生了什么」。
- **三要素（u9-l3）。** 每个 tile 操作由 scope（哪些线程执行）、layout（数据怎么摆）、dispatch（走哪条硬件路径）刻画。编译器的核心工作正是消费这三要素：scope 生成控制流与线程绑定，layout 生成地址计算，dispatch 选择指令。本讲会在 `LowerTIRx` 内部看到这条对应关系的官方表述。
- **pass 与 IRModule。** 「pass」指对 IR 做一次特定变换、校验或标注的处理步骤；`IRModule` 是一组函数的容器。附录还给出两个术语的精确定义：**ABI** 是函数之间的调用约定；**PassContext** 盛放编译器选项（例如可关闭公共子表达式消除，也控制向量化与展开的若干方面）。
- **一条 `Tx.gemm_async` 的展开事实（u9-l1）。** hgemm_v1 中一条 `Tx.gemm_async` 描述完整的 \(128 \times 128 \times 64\) tile GEMM；因为每条底层 `tcgen05.mma` 沿 K 只前进 16 个元素，编译器需要发出 \(\lceil K/16 \rceil = \lceil 64/16 \rceil = 4\) 条 MMA 指令。本讲全程用这个数字做对照。

不需要 Blackwell GPU 也能读懂本讲；涉及实际运行的实践都给出「无 GPU 时的源码推演」替代路径。

## 3. 本讲源码地图

本讲的「源码」是编译器附录的两页 RST 文档，加上被跟踪的 hgemm_v1 内核：

| 文件 | 作用 |
|---|---|
| [tirx_guide/arch/index.rst](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/index.rst) | 编译器内部附录入口：声明本节讲解 TIRx 编译器如何把 authored module 降到 CPU 侧 launcher 与 GPU 设备代码 |
| [tirx_guide/arch/lowering_pipeline.rst](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst) | 本讲主文档：整体编译路径、19 个 pass 的表格、`LowerTIRx` 内部结构、scale 内核三步变形示例、中间 IR 与最终 CUDA 的检视方法 |
| [chapter_intro_tirx/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md) | hgemm_v1 内核全文与 `Tx.gemm_async` 调用点，是本讲跟踪实践的对象 |

外部参照：附录明确指出 pass 的确切顺序定义在 Apache TVM 仓库的 `python/tvm/tirx/compilation_pipeline.py`（v0.26.0），`LowerTIRx` 的两步序列定义在 `src/tirx/transform/lower_tirx.cc`。本讲不深入 TVM 源码，但把这两个入口当作「权威仲裁处」——凡本书表格与实际行为有出入，以它们为准。

## 4. 核心概念与源码讲解

### 4.1 编译全景：两份产物与 19 个 pass

#### 4.1.1 概念说明

`tvm.compile(mod, target, tir_pipeline="tirx")` 最终产出**两份代码**：一份是 CPU 侧 launcher，负责准备参数并启动 GPU 内核；另一份是执行计算的 GPU 内核。编译器通过一串**有序的 pass** 到达这个结果，每个 pass 对 IR 做一次特定的变换、校验或标注。

要理解流水线，先分清三个角色：

- **target**：标识硬件与代码生成后端。书中的例子设备侧用 CUDA、主机侧用 LLVM。
- **BindTarget**：`tvm.compile` 的第一步动作之一，把 target 信息挂到模块上，之后才运行模块级的 `tirx_pipeline`。
- **finalization（终化）**：host 与 device 分离之后，各自走的最后一段目标相关变换，然后才进入代码生成。

#### 4.1.2 核心流程

附录给出的整体编译路径：

```text
authored TIRx
      │ BindTarget
      ▼
tirx_pipeline
(SplitHostDevice creates the two paths)
      ├── host PrimFunc   ──host finalization──▶ C/LLVM
      └── device PrimFunc ─device finalization─▶ CUDA
```

用中文复述这条路径：

1. 你写下的 TIRx（authored TIRx）先经 `BindTarget` 挂上 target。
2. 模块级流水线 `tirx_pipeline` 开始运行，共 19 步；其中第 15 步 `SplitHostDevice` 把一条 TIRx 函数拆成 host / device 两条路径。
3. host `PrimFunc` 走 host finalization，最终生成 C/LLVM；device `PrimFunc` 走 device finalization，最终生成 CUDA。

`PrimFunc` 是 TIR 对「函数」的表示——上图的 host PrimFunc 就是 CPU 侧 launcher，device PrimFunc 就是 GPU 内核。

#### 4.1.3 源码精读

- [tirx_guide/arch/lowering_pipeline.rst:L21-L29](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L21-L29)：开篇定义——`tvm.compile` 产出 CPU launcher 与 GPU 内核两份代码，编译经一串有序 pass 完成，每个 pass 做一次变换/校验/标注；确切 pass 顺序定义在 TVM 的 `compilation_pipeline.py`。
- [tirx_guide/arch/lowering_pipeline.rst:L41-L49](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L41-L49)：上面那张编译路径图的原文；`SplitHostDevice creates the two paths` 是两条路径的分叉点。
- [tirx_guide/arch/lowering_pipeline.rst:L51-L54](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L51-L54)：`PrimFunc` 与 `Finalization` 两个术语的定义。
- [tirx_guide/arch/lowering_pipeline.rst:L59-L63](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L59-L63)：pass 表引言——表格列出 19 步的执行顺序，并定义 ABI（函数间调用约定）与 PassContext（编译器选项容器）。
- [tirx_guide/arch/lowering_pipeline.rst:L65-L159](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L65-L159)：完整的 19-pass 表。下表是它的中文整理版（职责列压缩为一句）：

| # | 类别 | Pass | 职责 |
|---|---|---|---|
| 1 | TIRx lowering | `LowerTIRx` | 核心 lowering（见 4.2） |
| 2 | TIR normalization | `UnifyThreadBinding` | 合并等价的线程轴绑定，使每个 `threadIdx`/`blockIdx` 轴只声明一次 |
| 3 | TIR normalization | `StmtSimplify` | 化简 IR 中的算术表达式 |
| 4 | TIR normalization | `LowerTIRxOpaque` | 转换线程绑定循环、消除无标注的单位循环、规范化循环 pragma |
| 5 | TIR normalization | `FlattenBuffer` | 把剩余的多维 `BufferLoad`/`BufferStore` 摊平成一维 |
| 6 | Compute legalization | `BF16ComputeLegalize` | 目标无原生 bfloat16 计算时，提升到 float32 并改写为合法形式 |
| 7 | TIR normalization | `NarrowDataType(32)` | 在可证明安全的前提下把索引表达式与循环变量收窄到 32 位 |
| 8 | Loop lowering | `VectorizeLoop` | 把 `T.vectorized` 循环降为向量操作；设 `tir.disable_vectorize` 时改为标量化 |
| 9 | Loop lowering | `UnrollLoop` | 展开 `T.unroll` 标记的循环；普通常量循环仅在对应配置/pragma 开启时自动展开 |
| 10 | TIR normalization | `StmtSimplify` | 向量化与展开暴露出更多常量后再次化简 |
| 11 | TIR normalization | `CommonSubexprElim` | 把重复子表达式提升为临时变量（`tir.disable_cse_tir` 时跳过） |
| 12 | Compute legalization | `FP8ComputeLegalize` | 目标无原生 float8 计算时提升到受支持类型（默认 float32） |
| 13 | Validation and ABI | `VerifyMemory` | 确保主机侧代码不直接解引用设备内存 |
| 14 | Validation and ABI | `AnnotateEntryFunc` | 标记入口函数（单函数模块取该函数；多函数模块取唯一对外可见的 PrimFunc） |
| 15 | Validation and ABI | `SplitHostDevice` | 识别设备区域、拆分 host/device PrimFunc、把 host 到 device 的调用降到内核启动 ABI |
| 16 | Validation and ABI | `LowerIket` | 普通构建中移除 NVIDIA IKET 标注；启用 IKET 追踪时则将其 lowering |
| 17 | Validation and ABI | `MakePackedAPI` | 把 host 函数改写为 TVM runtime 使用的 packed-function ABI |
| 18 | Storage legalization | `FP8StorageLegalize` | 目标无原生 float8 存储时用 `uint8` 容器 |
| 19 | Storage legalization | `BF16StorageLegalize` | 目标无原生 bfloat16 存储时用 `uint16` 容器 |

读这张表的一个诀窍：**类别比名字重要**。19 个 pass 按六类分工——TIRx lowering（1 个）、TIR normalization（7 个）、Compute legalization（2 个）、Loop lowering（2 个）、Validation and ABI（5 个）、Storage legalization（2 个）。真正「TIRx 特有」的只有第 1 步和第 4 步；第 6 步以后基本是通用 TIR 的合法化、校验与 ABI 适配。也就是说：**TIRx 的个性集中在流水线前端，后端复用 TIR 的成熟能力。**

#### 4.1.4 代码实践

**实践目标**：不看本讲表格，独立重建 19-pass 清单与顺序，并检验自己的分类直觉。

**操作步骤**：

1. 打开 [tirx_guide/arch/lowering_pipeline.rst:L65-L159](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L65-L159) 的原表，通读一遍后合上。
2. 在纸上画一张六列分类表（类别作列头），凭记忆把 19 个 pass 填进对应类别，并标出执行序号。
3. 翻开原表核对，重点检查三个易错点：
   - `StmtSimplify` 出现了**两次**（#3 与 #10），为什么第二次排在向量化/展开之后？
   - `FlattenBuffer` 是 #5，但它摊平的是「剩余的」（the remaining）访问——言外之意，谁的访问在此之前已经被摊平了？（提示：`LowerTIRx` 已经处理了带逻辑布局的 tile 操作访问，见 4.2。）
   - `SplitHostDevice` 是 #15 而不是更早——为什么必须排在 `LowerTIRx`（#1）之后？

**需要观察的现象**：重建表与原表的差异集中在哪几行；自己是否把类别归属记混。

**预期结果**：能完整复述 19 个 pass 的顺序与六类归属；能回答上面三个检查点的答案（参见 4.1.5 练习 1 的参考答案）。

本实践为纯阅读型，无需运行环境。

#### 4.1.5 小练习与答案

**练习 1**：`StmtSimplify` 为什么在流水线里出现两次？

**参考答案**：第一次（#3）在 `LowerTIRx` 刚把 tile 操作与抽象标识符降级之后，化简新生成的算术表达式；第二次（#10）排在 `VectorizeLoop`（#8）与 `UnrollLoop`（#9）之后，因为向量化与展开会暴露出更多常量，此时再化简一次才能吃到这些新信息。这是「变换→化简→再变换→再化简」的经典交替结构。

**练习 2**：`VerifyMemory`（#13）校验的内容与 host/device 分离（#15）有什么关系？

**参考答案**：`VerifyMemory` 确保主机侧代码不直接解引用设备内存。它排在 `SplitHostDevice`（#15）之前，作为拆分前的最后一道内存合法性检查——如果 authored 代码里混入了「CPU 侧直接读 GPU 指针」这类非法访问，会在拆分前就被拦截，而不是等生成代码后在运行期崩溃。

**练习 3**：如果把 `tir_pipeline="tirx"` 换成普通 TIR 流水线，这 19 步里哪些必然消失？

**参考答案**：`LowerTIRx`（#1）与 `LowerTIRxOpaque`（#4）必然消失——它们处理的是 `TilePrimitiveCall`、抽象线程标识、TIRx 循环标注这些 TIRx 特有结构；其余大部分归一化、合法化与 ABI pass 是通用 TIR 机制。这也印证了 4.1.3 的结论：TIRx 的个性集中在前端。

### 4.2 深入 LowerTIRx：TilePrimitiveDispatch 与 LowerTIRxCleanup

#### 4.2.1 概念说明

`LowerTIRx` 是整条流水线的第一步，也是唯一标注为「TIRx lowering」类别的 pass。它有**两大职责**：

1. 为 tile 级操作**选择具体实现**。TIRx 把 `copy`、`gemm`、`reduction` 这类操作表示为 `TilePrimitiveCall` 节点——这是「声明做什么」的抽象层；`LowerTIRx` 负责为每个节点选定一个后端实现。
2. 把**逻辑数据布局变成物理内存索引**。让后续 pass 可以直接处理具体的索引表达式。

这正是 u9-l3 三要素框架的编译器侧落点：scope、layout、dispatch 三份信息在 `TilePrimitiveCall` 上齐备，`LowerTIRx` 据此完成「声明」到「实现」的翻译。

#### 4.2.2 核心流程

`LowerTIRx` 内部是一个两步的顺序组合，定义在 TVM 的 `src/tirx/transform/lower_tirx.cc`：

```text
LowerTIRx = Sequential([ TilePrimitiveDispatch, LowerTIRxCleanup ])
```

1. **`TilePrimitiveDispatch`**：
   - 遍历 IR 中的 `TilePrimitiveCall` 节点，为每个 tile 操作选择具体后端实现；
   - 同时把抽象的执行范围标识符——`T.cta_id`、`T.thread_id` 这类——变成 kernel-launch 参数与线程绑定。
2. **`LowerTIRxCleanup`**：
   - 把逻辑坐标映射为物理索引；
   - 把受支持的逻辑布局应用到 buffer 访问上，使后续 pass 直接面对具体索引表达式。

`LowerTIRx` 结束后的 IR 状态：tile 操作已被替换为选定的实现；逻辑布局已变成物理索引；`T.cta_id`、`T.thread_id` 等抽象标识符已变成线程绑定。但**线程绑定循环和 TIRx 特有的循环标注可能仍然残留**——这些结构由后续的 `LowerTIRxOpaque`（#4）规范化，然后才轮到 `FlattenBuffer`（#5）摊平普通 TIR 的 buffer 访问。

#### 4.2.3 源码精读

- [tirx_guide/arch/lowering_pipeline.rst:L172-L183](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L172-L183)：`LowerTIRx` 两大职责的原文（为 tile 操作选实现、把逻辑布局变物理索引），以及 `Sequential([TilePrimitiveDispatch, LowerTIRxCleanup])` 的定义与源码位置。
- [tirx_guide/arch/lowering_pipeline.rst:L185-L193](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L185-L193)：两个子 pass 的分工——`TilePrimitiveDispatch` 处理 `TilePrimitiveCall`（copy/gemm/reduction）并把 `T.cta_id`/`T.thread_id` 变成 launch 参数与线程绑定；`LowerTIRxCleanup` 应用逻辑布局、映射逻辑坐标到物理索引。
- [tirx_guide/arch/lowering_pipeline.rst:L195-L200](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L195-L200)：`LowerTIRx` 之后的残留状态与 `LowerTIRxOpaque`、`FlattenBuffer` 的衔接关系——这解释了 4.1.4 检查点 2「the remaining accesses」的措辞。
- [chapter_intro_tirx/index.md:L143-L149](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L143-L149)：被跟踪对象——hgemm_v1 中 `Tx.gemm_async` 的调用点：

  ```python
  if warp_id == 0:
      if T.ptx.elect_sync():
          Tx.gemm_async(
              tmem[:, :BLK_N], Asmem[:, :], Bsmem[:, :],
              accum=False, dispatch="tcgen05", cta_group=1
          )
          T.ptx.tcgen05.commit(mma_bar.ptr_to([0]), cta_group=1)
  ```

  这条 `TilePrimitiveCall` 携带的三要素：scope 由双层守卫（`warp_id == 0` 加 `elect_sync`）表达——恰好一个线程发起；layout 由 `tmem` 的 `TLane/TCol` 布局与 `Asmem/Bsmem` 的 128B swizzle 布局共同约定；dispatch 由 `dispatch="tcgen05"` 显式选定 Blackwell 的 `tcgen05.mma` 路径。

- [chapter_intro_tirx/index.md:L59](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L59)：展开事实——一条 `Tx.gemm_async` 描述完整的 \(128 \times 128 \times 64\) tile GEMM；因每条底层 `tcgen05.mma` 沿 K 前进 16 个元素，编译器发出 4 条 MMA 指令覆盖整个 K 维，其确切序列由 shape、layout 与 dispatch 信息推导。这就是 `TilePrimitiveDispatch` 「选择具体实现」的可观察结果。

#### 4.2.4 代码实践

**实践目标**：亲手只运行流水线的第一个 pass，观察 `LowerTIRx` 前后 IR 的差异。

**操作步骤**：

1. 安装 `apache-tvm==0.26.0` 与 `cuda-bindings`（见 u1-l3）。把附录的 scale 内核逐字保存为 `inspect_lower_tirx.py`（来自 [tirx_guide/arch/lowering_pipeline.rst:L289-L297](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L289-L297)）：

   ```python
   from tvm.tirx import transform as TT

   target = tvm.target.Target("cuda").with_host("llvm")
   mod = tvm.IRModule({"main": scale})
   mod = TT.BindTarget(target)(mod)
   mod = TT.LowerTIRx()(mod)         # run LowerTIRx to lower abstract thread IDs
   print(mod.script())               # inspect the IR after LowerTIRx
   ```

   注意文件顶部需要先定义 scale 内核（[tirx_guide/arch/lowering_pipeline.rst:L212-L224](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L212-L224)）并 `import tvm`、`from tvm.script import tirx as T`；且因 TIRx 依赖源码检视，内核必须写在文件里，不能塞进 `python -c`。
2. 运行 `python inspect_lower_tirx.py`，在输出的 `mod.script()` 中搜索 `blockIdx.x` 与 `threadIdx.x`。
3. 对照确认：`T.cta_id` 与 `T.thread_id` 的调用应当消失，取而代之的是 `launch_thread` 形式的线程绑定（文档的预期输出说明见 [tirx_guide/arch/lowering_pipeline.rst:L299-L300](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L299-L300)）。
4. （进阶）把同样的手法套到 hgemm_v1 上。示例代码（改编自文档，hgemm_v1 的构造见 [chapter_intro_tirx/index.md:L85-L170](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L85-L170)）：

   ```python
   kernel = hgemm_v1(128, 128, 64)
   mod = tvm.IRModule({"main": kernel})
   mod = TT.BindTarget(target)(mod)
   mod = TT.LowerTIRx()(mod)
   print(mod.script())
   ```

   在输出中搜索 `gemm_async`（应已不存在于 tile 抽象形式）与 `tcgen05`（应出现选定实现引入的指令痕迹）。

**需要观察的现象**：抽象标识符消失、线程绑定出现；tile 操作被替换为实现级的 IR 结构。

**预期结果**：scale 内核的输出含 `blockIdx.x`/`threadIdx.x` 绑定且无 `T.cta_id`/`T.thread_id`；hgemm_v1 的输出不再有 `Tx.gemm_async` 这一抽象调用。hgemm_v1 变体的具体输出形态**待本地验证**（文档只对 scale 演示了该手法）；无 GPU 环境下能否仅凭 `Target("cuda")` 完成 IR 级 lowering 也**待本地验证**——若报错，退回纯阅读路径：对比 `kernel.show()` 与书中描述，在纸上推演 `LowerTIRx` 应做的替换。

#### 4.2.5 小练习与答案

**练习 1**：`TilePrimitiveDispatch` 与 `LowerTIRxCleanup` 各自对应三要素中的哪些？

**参考答案**：`TilePrimitiveDispatch` 消费 dispatch（为 `TilePrimitiveCall` 选后端实现）与 scope（把 `T.cta_id`/`T.thread_id` 等抽象执行范围标识符变成 launch 参数与线程绑定）；`LowerTIRxCleanup` 消费 layout（把逻辑坐标映射为物理索引、把逻辑布局应用到 buffer 访问）。两个子 pass 合起来正好覆盖三要素。

**练习 2**：为什么 `FlattenBuffer`（#5）描述自己只摊平「剩余的」多维访问？

**参考答案**：因为带逻辑布局的 tile 操作访问已经在 #1 `LowerTIRx`（确切说是其子 pass `LowerTIRxCleanup`）里被映射成了物理索引；`FlattenBuffer` 处理的是此后仍以多维 `BufferLoad`/`BufferStore` 形式存在的普通 TIR 访问。「剩余」一词标记了两条摊平路径的分界。

**练习 3**：hgemm_v1 中 `Tx.gemm_async` 的 `dispatch="tcgen05"` 如果换成其他 dispatch（假设备选），哪个 pass 负责反映这个变化？

**参考答案**：`TilePrimitiveDispatch`。dispatch 信息记录在 `TilePrimitiveCall` 上，该 pass 的职责就是为每个 tile 操作「选择具体实现」——dispatch 不同，选中的实现序列就不同（u9-l3 已见：后续 Step 4 把 GMEM→SMEM 拷贝改派 TMA，改变的正是 `Tx.copy_async` 的 dispatch 参数）。

### 4.3 缓冲展平与主机/设备分离

#### 4.3.1 概念说明

`LowerTIRx` 之后、代码生成之前，还有两组关键变换把 IR「整理成可拆分、可生成的形状」：

- **缓冲展平与结构规范化**：`LowerTIRxOpaque`（#4）清掉 TIRx 残留结构（线程绑定循环、无标注单位循环、循环 pragma）；`FlattenBuffer`（#5）把多维 buffer 访问摊平成一维。代码生成器只愿意面对一维地址与规范化循环，这两步是「打扫干净再请客」。
- **主机/设备分离**：`SplitHostDevice`（#15）识别设备区域，把一条 TIRx 函数拆成 host PrimFunc 与 device PrimFunc，并把 host 到 device 的调用降到内核启动 ABI。它是编译路径图里「creates the two paths」的分叉点。配套的 `VerifyMemory`（#13）先确保主机侧不直接碰设备内存；`AnnotateEntryFunc`（#14）标出入口；随后 `LowerIket`（#16）清理 IKET 标注，`MakePackedAPI`（#17）把 host 函数改写成 TVM runtime 的 packed-function ABI——这就是为什么编译出的 `ex.mod(...)` 能直接收 PyTorch 张量。

分离的「钉子」是 `T.device_entry()`：它标记 GPU 代码的入口；`LowerTIRx` 用这个标记建立对应的线程绑定，`SplitHostDevice` 再据此把设备区域抽取成独立内核。

#### 4.3.2 核心流程

以附录的 scale 内核（1024 个元素、4 个 CTA、每 CTA 256 线程）为例，三步变形：

```text
第 1 步  authored TIRx        bx = T.cta_id([4]); tx = T.thread_id([256])
              │
第 2 步  LowerTIRx            with T.launch_thread("blockIdx.x", 4) as bx:
                             tx = T.launch_thread("threadIdx.x", 256)
              │
第 3 步  SplitHostDevice      host launcher  ── launch scale_kernel, gridDim.x=4, blockDim.x=256
        + 后续 ABI pass        device kernel ── 每线程乘 2
```

拆分之后的终化路线（在 19 步流水线之外，`tvm.compile` 对每类函数再跑一段）：

- **host**：`LowerTVMBuiltin`（降低 `tvm_*` 内建）→ `LowerIntrin`（降低目标相关 intrinsic）→ C/LLVM。
- **device**：`LowerWarpMemory`（把 warp 级 buffer 降到 shuffle）→ `StmtSimplify` → `LowerIntrin` → CUDA。

#### 4.3.3 源码精读

- [tirx_guide/arch/lowering_pipeline.rst:L85-L95](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L85-L95)：`LowerTIRxOpaque`（#4）与 `FlattenBuffer`（#5）的表项原文——前者转换线程绑定循环、消除无标注单位循环、规范化 pragma；后者摊平「剩余的」多维访问。
- [tirx_guide/arch/lowering_pipeline.rst:L129-L151](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L129-L151)：Validation and ABI 五连（#13–#17）的表项，其中 #15 `SplitHostDevice` 的职责描述：识别设备区域、拆分 host/device PrimFunc、把 host 到 device 的调用降到内核启动 ABI。
- [tirx_guide/arch/lowering_pipeline.rst:L161-L170](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L161-L170)：host 与 device 两条终化序列的原文。
- [tirx_guide/arch/lowering_pipeline.rst:L210-L231](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L210-L231)：scale 内核源码与第一步说明——`T.device_entry()` 标记 GPU 代码入口；`T.cta_id([4])` 声明 4 个 CTA、`T.thread_id([256])` 声明每 CTA 256 线程；此刻 `bx`/`tx` 还是抽象的 TIRx 标识符。
- [tirx_guide/arch/lowering_pipeline.rst:L233-L244](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L233-L244)：第二步——`LowerTIRx` 把 `bx` 绑到 `blockIdx.x`、`tx` 绑到 `threadIdx.x`，得到 `launch_thread` 形式的等价 TIR（仍是 TIR，不是 CUDA）。
- [tirx_guide/arch/lowering_pipeline.rst:L246-L261](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L246-L261)：第三步——`SplitHostDevice` 产出两个 PrimFunc（host 保留启动逻辑、device 保留逐元素计算），`MakePackedAPI` 再把 host 函数适配到 TVM runtime 的统一调用约定。
- [chapter_intro_tirx/index.md:L101-L107](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L101-L107)：hgemm_v1 里的同一套钉子——`T.device_entry()` 之后紧跟 `T.cta_id`/`T.warpgroup_id`/`T.warp_id_in_wg`/`T.lane_id`，它们在 `LowerTIRx` 中同样会被绑定为具体的线程标识。

#### 4.3.4 代码实践

**实践目标**：用 scale 内核把「一个函数 → 两份代码」的分离过程在纸面上完整走一遍。

**操作步骤**：

1. 抄录 [tirx_guide/arch/lowering_pipeline.rst:L212-L224](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L212-L224) 的 scale 内核到笔记上。
2. 用三种颜色分别标出：① 只属于设备侧的语句（buffer 访问与乘法）；② 将变成 host 启动逻辑的信息（`[4]` 与 `[256]` 这两个 launch 形状）；③ 分离的钉子（`T.device_entry()`）。
3. 参照 [tirx_guide/arch/lowering_pipeline.rst:L250-L256](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L250-L256) 的两行伪结构，写出你自己的 host launcher 与 device kernel 草图，标明 gridDim.x 与 blockDim.x 的值从原文哪来。
4. 回答：1024 个元素为什么不需要边界守卫？（依据 [tirx_guide/arch/lowering_pipeline.rst:L275-L277](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L275-L277)。）

**需要观察的现象**：launch 形状（4、256）如何原样出现在 host 侧启动调用里；计算语句全部落在 device 侧。

**预期结果**：得到一张三色标注图与两行式拆分草图；答案——因为 \(4 \times 256 = 1024\) 恰好覆盖全部元素；一般长度 \(N\) 需用向上取整选 CTA 数并用 `i < N` 守卫内核体。纯纸面实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SplitHostDevice` 必须排在 `LowerTIRx` 之后，而不能提前？

**参考答案**：拆分依赖两个 `LowerTIRx` 的产物：其一，`T.device_entry()` 标记处的线程绑定已由 `LowerTIRx` 建立，设备区域的边界因此可识别；其二，tile 操作已被替换为实现级 TIR，拆出的 device 函数才是自洽的 TIR 函数。若提前拆分，设备区域里还残留 `TilePrimitiveCall` 与抽象标识符，无法进入后续通用 pass。

**练习 2**：`MakePackedAPI` 改写 host 函数后，用户侧看到的最直接好处是什么？

**参考答案**：host 函数被改写为 TVM runtime 的 packed-function ABI，于是编译产物 `ex.mod(...)` 可以被 runtime 统一调度、直接接收运行期参数（如 PyTorch 张量），无需用户手写任何参数打包代码——这正是 u9-l2 里 `ex.mod(A_tensor, B_tensor, D_tensor)` 直接可用的编译器侧原因。

**练习 3**：host 与 device 的终化序列为何不同？

**参考答案**：两边面对的代码形态与目标后端不同。host 侧要对接 TVM runtime 与 LLVM/C 后端，需要 `LowerTVMBuiltin` 降低 `tvm_*` 内建；device 侧要对接 CUDA 后端，需要 `LowerWarpMemory` 把 warp 级 buffer 降到 shuffle，并再跑一次 `StmtSimplify` 清理化简。共同点是都以 `LowerIntrin` 收尾，把目标相关 intrinsic 降到后端可生成的形式。

### 4.4 CUDA 代码生成：跟踪 Tx.gemm_async 的完整变形

#### 4.4.1 概念说明

终化之后，device PrimFunc 进入 CUDA 后端，生成 CUDA 源码（再经 NVRTC 编译为可加载的内核）。对学习者来说，比「知道有这一步」更有价值的是**横向跟踪一个 tile 操作的全程变形**：同一条 `Tx.gemm_async`，在流水线的四个观察点上分别长什么样。这给了我们一把「编译器做了什么」的标尺——也直接服务于 u15-l7 的调试方法（在生成的 CUDA 里核对关键指令与守卫）。

#### 4.4.2 核心流程

`Tx.gemm_async` 的四站变形（观察点 → 形态）：

| 站点 | 所处阶段 | `Tx.gemm_async` 的形态 | 观察方法 |
|---|---|---|---|
| A | authored IR | 一条 tile 级调用：`Tx.gemm_async(tmem[:,:128], Asmem, Bsmem, accum=False, dispatch="tcgen05", cta_group=1)`，由 `warp_id==0` + `elect_sync` 双守卫 | `kernel.show()` / `kernel.script()` |
| B | `LowerTIRx` 之后 | 抽象调用消失，替换为选定的实现：4 条 `tcgen05.mma`（\(\lceil 64/16 \rceil = 4\)）及配套的地址计算与守卫；布局成为物理索引 | `TT.LowerTIRx()` 后 `mod.script()`（待本地验证） |
| C | `SplitHostDevice` 之后 | 落在 device PrimFunc 中；host 侧只剩启动逻辑 | 拆分后检视模块结构 |
| D | CUDA 源码 | 设备后端生成的 CUDA 代码，含 `tcgen05` 指令的最终形式 | `ex.mod.imports[0].inspect_source()` |

附录用一句话概括这条链路（就 scale 而言）：TIRx 描述线程组织与计算，`LowerTIRx` 把抽象标识符变成 TIR 线程绑定，`SplitHostDevice` 分离 CPU 侧启动逻辑与 GPU 侧计算，CUDA 后端最终产出 CUDA 源码。

#### 4.4.3 源码精读

- [tirx_guide/arch/lowering_pipeline.rst:L263-L273](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L263-L273)：scale 的最终 CUDA 代码（`__global__ void scale_kernel`，用 `blockIdx.x * 256 + threadIdx.x` 索引）与四步链路的一句话总结。
- [tirx_guide/arch/lowering_pipeline.rst:L302-L313](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L302-L313)：检视最终 CUDA 的标准写法——`tvm.compile(..., tir_pipeline="tirx")` 得到 `exe`，host 模块恰好导入一个设备模块，故 `exe.mod.imports[0]` 即生成的 CUDA 模块，`inspect_source()` 返回其源码。
- [chapter_intro_tirx/index.md:L243-L252](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L243-L252)：两级对照检视法——`kernel.show()` 与 `kernel.script()` 看 lowering 前的 TIRx `PrimFunc`，`ex.mod.imports[0].inspect_source()` 看最终 CUDA；书中明言「对比这两级，可以看出一个 tile 操作生成了哪些低层指令、它的布局与线程 scope 如何变成具体的地址计算与控制流」。
- [chapter_intro_tirx/index.md:L184-L190](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L184-L190)：完整编译 hgemm_v1 的原代码——`target = tvm.target.Target("cuda")`（TVM 自动探测当前设备架构如 `sm_100a`），`tvm.compile(tvm.IRModule({"main": kernel}), target=target, tir_pipeline="tirx")`。
- [chapter_intro_tirx/index.md:L230-L241](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L230-L241)：`LowerTIRx` 在正文中的定位——用每个 tile primitive 的 scope、layout 与 dispatch 选择具体实现，把 `Tx.gemm_async`、`Tx.cta.copy` 一类操作降为低层 TIR；随后的 pass 摊平 buffer、拆分 host/device 并生成设备代码。

#### 4.4.4 代码实践

**实践目标**：在有 GPU 的环境完整跑通「authored IR → CUDA」两级对照，并在生成代码中找到 `Tx.gemm_async` 的最终形态。

**操作步骤**：

1. 保存 hgemm_v1 的构造代码（[chapter_intro_tirx/index.md:L72-L170](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L72-L170)，含 import 块）与编译验证代码（[chapter_intro_tirx/index.md:L181-L205](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L181-L205)）为 `track_gemm_async.py`，先确认打印 `PASS`。
2. 追加两级打印：

   ```python
   kernel.show()          # 站点 A：authored IR，应能看到 Tx.gemm_async
   print(ex.mod.imports[0].inspect_source())   # 站点 D：最终 CUDA
   ```

3. 在站点 D 的输出里搜索 `tcgen05`，统计 MMA 指令出现的形式与次数；对照 \(\lceil 64/16 \rceil = 4\) 的预期。
4. 顺带核对两个映射：`T.cuda.cta_sync()` 是否变为 CUDA 的 `__syncthreads()`；守卫 `warp_id == 0` 变成了什么样的线程级判断（u9-l2 曾用同样方法验证过这两条映射）。

**需要观察的现象**：站点 A 中一条 `Tx.gemm_async` 对应站点 D 中的多条 MMA 指令；SMEM 的 128B swizzle 布局体现为生成代码中的地址计算；TMEM 的 `TLane/TCol` 布局体现为对 `tmem` 地址的组装。

**预期结果**：生成 CUDA 中能检索到 `tcgen05` 相关指令且 MMA 数量与 K 维覆盖一致（书中明言为 4 条）；若数量或形式不符，优先检查自己的 `BLK_K` 是否仍为 64。本实践需要 Blackwell GPU（`sm_100a`）才能编译运行；无 GPU 时改做推演：以 [chapter_intro_tirx/index.md:L59](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L59) 的陈述为依据，在 4.4.2 的表格里逐站填写「预期形态」并标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么一条 `Tx.gemm_async` 会变成 4 条 MMA 指令而不是 1 条？

**参考答案**：`Tx.gemm_async` 描述的是完整的 \(128 \times 128 \times 64\) tile GEMM，而 Blackwell 每条 `tcgen05.mma` 沿 K 只前进 16 个元素，因此需要 \(\lceil 64/16 \rceil = 4\) 条指令覆盖整个 K 维。指令的准确序列由 shape、layout 与 dispatch 信息推导——这正是 `TilePrimitiveDispatch`「选择具体实现」的含义。

**练习 2**：`exe.mod.imports[0]` 为什么是设备模块？

**参考答案**：`SplitHostDevice` 把模块拆成 host PrimFunc 与 device PrimFunc，host 模块以导入（imports）的方式持有设备模块；hgemm_v1 只有一个设备函数，因此 `imports[0]` 唯一对应生成的 CUDA 模块，`inspect_source()` 即返回其源码。这也是附录对 scale 示例给出的用法说明。

**练习 3**：如果想观察「`LowerTIRx` 之后、`FlattenBuffer` 之前」的 IR，应该怎么做？

**参考答案**：不要一次跑完 `tvm.compile`，而是手动执行部分 pass 后停下：`TT.BindTarget(target)(mod)` 之后再 `TT.LowerTIRx()(mod)`，然后 `print(mod.script())` 检查。附录明确演示了这种「只跑前几个 pass、在流水线余下部分之前停下」的检视技巧；更后面的中间态需要继续追加对应 pass（具体可用的 pass 构造器**待本地验证**，权威清单在 TVM 的 `python/tvm/tirx/compilation_pipeline.py`）。

## 5. 综合实践

本讲的综合实践就是任务书里的那件事：**写一份 `Tx.gemm_async` 的 lowering 流水账**。建议按下表逐行填写，形成一页可长期维护的笔记：

| 观察点 | 观察命令/依据 | 你看到/推演的形态 | 证据行号或输出摘录 |
|---|---|---|---|
| ① pass 全景 | 4.1.3 的重建表 | 19 pass 按六类归位 | （贴你的默写表） |
| ② authored IR | `kernel.show()` | `Tx.gemm_async(...)` 原文与双守卫 | `chapter_intro_tirx/index.md:L143-L149` |
| ③ LowerTIRx 后 | `TT.BindTarget` + `TT.LowerTIRx` + `mod.script()` | 抽象标识符与 tile 调用消失，4 条 MMA 的实现序列出现 | 待本地验证 |
| ④ host/device 拆分 | scale 三步变形对照 | 启动形状落入 host、计算落入 device | `lowering_pipeline.rst:L246-L261` |
| ⑤ 最终 CUDA | `ex.mod.imports[0].inspect_source()` | `tcgen05` 指令、`__syncthreads()`、线程守卫 | 待本地验证 |

三条路径任选：

- **有 Blackwell GPU**：走完 ①–⑤，⑤ 必须实际运行；把 MMA 指令计数与 \(\lceil K/16 \rceil\) 核对。
- **有 tvm 无 GPU**：完成 ①②④（④ 用 scale 的 IR 级检视，③ 视 `LowerTIRx` 是否需要设备信息而定，标注待本地验证）。
- **纯阅读**：完成 ①②④ 的纸面版，③⑤ 填「预期形态 + 待本地验证」。

验收标准：不看讲义能画出「BindTarget → tirx_pipeline(19 pass) → host/device 终化 → CUDA」的全图，并说出 `LowerTIRx` 两个子 pass 分别消费三要素中的哪几个。

## 6. 本讲小结

- `tvm.compile(..., tir_pipeline="tirx")` 产出两份代码：CPU 侧 launcher（备参、启动内核）与 GPU 内核；整体路径是 BindTarget → 19 步 `tirx_pipeline`（`SplitHostDevice` 分出两条路径）→ host/device 各自终化 → C/LLVM 与 CUDA。
- 19 个 pass 分六类（TIRx lowering / TIR normalization / Compute legalization / Loop lowering / Validation and ABI / Storage legalization）；TIRx 的个性集中在前端（#1 `LowerTIRx`、#4 `LowerTIRxOpaque`），后端复用 TIR 的归一化、合法化与 ABI 机制。
- `LowerTIRx = Sequential([TilePrimitiveDispatch, LowerTIRxCleanup])`：前者为 `TilePrimitiveCall` 选后端实现并把 `T.cta_id`/`T.thread_id` 变成 launch 参数与线程绑定（消费 dispatch 与 scope），后者把逻辑布局应用为物理索引（消费 layout）。
- 缓冲展平分两条路径：tile 操作的带布局访问在 `LowerTIRx` 内物理化，其余多维访问由 `FlattenBuffer`（#5）摊平；`LowerTIRxOpaque`（#4）负责清理线程绑定循环等 TIRx 残留结构。
- 主机/设备分离以 `T.device_entry()` 为钉子：`LowerTIRx` 借它建立线程绑定，`SplitHostDevice`（#15）据此拆出两个 PrimFunc 并降到内核启动 ABI，`MakePackedAPI`（#17）再适配 TVM runtime——这是 `ex.mod(...)` 能直接收张量的原因。
- 跟踪 `Tx.gemm_async` 的四站变形（authored IR → `LowerTIRx` 后 → 拆分后 → CUDA 源码）是理解编译器的最好练习；一条 tile 调用展开为 \(\lceil K/16 \rceil = 4\) 条 `tcgen05.mma`，可在 `inspect_source()` 输出中实地核对。

## 7. 下一步学习建议

- **下一讲 u15-l4（可复现基准测试）**：有了「内核是怎么生成的」这层理解，接着学怎么测它——正确性基线、计时边界与 CUDA events，`inspect_source()` 的输出将同时成为你核对指令的依据。
- **回头看 TVM 源码（可选进阶）**：附录指出的两个权威入口——`python/tvm/tirx/compilation_pipeline.py`（pass 确切顺序）与 `src/tirx/transform/lower_tirx.cc`（两步序列）——当你怀疑书中表格与实际行为有出入时，去那里仲裁。
- **与 u15-l7（调试 warp-specialized 内核）联动**：那讲的调试流程要求「对照生成的 CUDA 检查守卫与屏障」，本讲的站点 D 检视法（`inspect_source()` + 搜索 `tcgen05`）正是它的基本功；学完 u15-l4/u15-l5 后回头重做本讲综合实践，会有新的收获。
