# Vid 消除与自动 CV 配比

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 Ascend 上 `vid`（vector id）的含义，以及为什么「让前端直接看见 vid」会污染编程模型。
- 区分 `T.Kernel(..., threads=None)`（vid 可见）与 `T.Kernel(..., threads=2)`（vid 消除）两种写法在 UB 形状、GM 偏移、循环范围上的差别。
- 理解 `threads=1/2` 如何在 IR 层变成 `threadIdx.x` 绑定与 `npu_cv_ratio` 属性（即「自动 CV 配比」）。
- 掌握 `AscendVidReduction` 这个 pass 对 UB 形状减半、GM 注入 vid 偏移、tile op / reduce / 循环范围改写的完整逻辑，以及它的「例外名单」（skip-set）机制。

本讲是 u5 单元（CV 分离与跨核机制）的第三讲，承接 u5-l1（Cube/Vector 分离）与 u2-l2（kernel launch 与 cid/vid）。

## 2. 前置知识

在进入本讲前，请确认你已理解以下概念（均来自前置讲义）：

- **Cube 与 Vector 两类核**：Ascend AI Core 内 Cube（AIC）负责矩阵乘、Vector（AIV）负责向量计算，二者经 GM/L2 workspace 中转数据（见 u5-l1）。
- **cid 与 vid**：`cid`（core id）表示「我负责哪个 tile」，`vid`（vector id）表示「我是该 tile 里第几个 Vector 子核」。A2/A3 等型号一个 Cube 可配 1 个或 2 个 Vector，即 C:V = 1:1 或 1:2（见 u2-l2）。
- **UB（Unified Buffer）**：属 Vector 核的片上存储，在 tile-lang 前端用 `T.alloc_shared` 声明、scope 为 `shared.ub`（见 u3-l1、u3-l2）。
- **scope 推断**：`AscendInferBufferScope` pass 会把 `dynamic` scope 的 buffer 钉死到具体物理存储（L1/UB/L0A/L0B/L0C），这是 vid 消除能够「知道哪些 buffer 是 UB」的前提（见 u3-l1）。
- **pass 流水线两阶段**：`LowerAndLegalize`（语义降级与合法化）与 `OptimizeForTarget`（硬件优化），由 `tilelang/engine/phase.py` 编排（见 u1-l5、u6-l1）。

一个关键直觉：**vid 消除不是删掉 vid 这个变量，而是把「按 vid 切分数据」这件事从前端挪到编译器里做。** 硬件层面 vid 始终存在，只是对写 kernel 的人不可见。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tilelang/language/kernel.py` | `T.Kernel` 的 Python 入口，处理 `threads` 参数，决定返回 `cid` 还是 `(cid, vid)`。 |
| `src/ir.cc` | C++ 侧 `KernelLaunch`：把 `threads` 翻译成 `vid`→`threadIdx.x` 绑定，并写 `npu_cv_ratio` 属性。 |
| `src/transform/common/attr.h` | 定义 `cv_1_1` / `cv_1_2` 等 CV 配比字符串常量。 |
| `src/transform/ascend_vid_reduction.cc` | **本讲主角**：`AscendVidReduction` pass，在 IR 层完成全部 vid 切分改写。 |
| `tilelang/engine/phase.py` | 把 `AscendVidReduction` 排在 `LowerAndLegalize` 中、`AscendInferBufferScope` 之后。 |
| `src/transform/ascend_workspace_reduction.cc` | 下游消费者：读取本 pass 产出的 `buffers_skip_vid_reduction` 属性。 |
| `examples/developer_mode/matmul_add_developer.py` | 最简单的 `threads=2` 示例。 |
| `examples/developer_mode/sparse_flash_attn_developer_vid_reduce.py` | 触发 skip-set 例外（gather 间接索引）的真实算子。 |
| `testing/python/language/cvseparate/test_tilelang_ascend_language_vid_reduction.py` | 覆盖 8 类改写场景的测试，是本讲实践的依据。 |
| `docs/tutorials/vid_reduction_and_auto_cv_ratio.md` | 官方特性说明文档。 |

## 4. 核心概念与源码讲解

### 4.1 vid 是什么，为什么要消除它

#### 4.1.1 概念说明

在 C:V = 1:2 的硬件上，一个 Cube 核挂着两个 Vector 子核（vid=0 与 vid=1）。如果一块 UB 数据逻辑上是 `[block_M, block_N]`，那么物理上需要把它**沿第 0 维对半切**：vid=0 的子核拿前一半 `[block_M/2, block_N]`，vid=1 拿后一半。相应地，从 GM 搬数据时也要给起点加上 `vid * (block_M/2)` 的偏移。

这带来一个糟糕的后果：**硬件配比（1:2）的细节会泄漏到前端代码里。** 用户必须维护一个 `VEC_NUM=2` 的常量，并在三处地方手动切分：

1. UB 申请：`T.alloc_shared((block_M // VEC_NUM, block_N), ...)`
2. GM 偏移：`T.copy(workspace[bx*block_M + vid*block_M//VEC_NUM, ...], c_ub)`
3. 循环范围：遍历 UB 第 0 维的循环要 `// VEC_NUM`

官方文档把这一痛点说得很直白：

[docs/tutorials/vid_reduction_and_auto_cv_ratio.md:1-4](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/tutorials/vid_reduction_and_auto_cv_ratio.md#L1-L4) — 这段说明 vid 消除的设计目标：屏蔽 1:1/1:2 配比的硬件细节，让 vid 对用户不可见。

#### 4.1.2 核心流程

「vid 消除」的思路是：**前端只写「完整形状 + 不带 vid 偏移」，编译器自动补上三处切分。**

```text
用户写法 (threads=2)                编译器自动改写 (等价于旧的 vid 可见写法)
─────────────────────              ──────────────────────────────────────
c_ub = alloc([block_M, N])    ─►   c_ub = alloc([block_M/2, N])          # 第0维减半
copy(workspace[bx*block_M], c_ub) ─► copy(workspace[bx*block_M + vid*block_M/2], c_ub)  # GM 加 vid 偏移
for k in serial(loop_k): ...   ─►   for k in serial(loop_k): ...          # 若 k 索引 vid-UB 第0维则范围减半
```

两种写法在硬件上**行为完全一致**，差别只在于「切分由人写还是编译器写」。这正是 u2-l2 总结的那句：「vid 在 TIR 层始终存在，仅对前端不可见」。

#### 4.1.3 源码精读

最简单的对照样本是 `matmul_add_developer.py`。注意它的 UB 申请写的是**完整** `block_M`，`T.Kernel` 也只返回单个 `cid`：

[examples/developer_mode/matmul_add_developer.py:40-66](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/matmul_add_developer.py#L40-L66) — 这段是 `threads=2` 的标准写法：`with T.Kernel(..., threads=2, is_npu=True) as (cid)` 只解包出 `cid`；`c_ub`/`d_ub` 都按完整 `block_M` 申请；`T.copy(c_ub, C[bx*block_M, ...])` 不带任何 vid 偏移。所有切分交给 pass。

对比官方文档里给出的「旧式 vid 可见」注释，能清楚看到被省掉的三处手动切分：

[docs/tutorials/vid_reduction_and_auto_cv_ratio.md:17-38](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/tutorials/vid_reduction_and_auto_cv_ratio.md#L17-L38) — 这段注释明确指出：UB 申请「不需要除以 2」、GM 搬运「不需要加 `vid * block_M // VEC_NUM`」，因为「编译器 pass 会处理相关操作」。

#### 4.1.4 代码实践

**实践目标**：用肉眼对照两种写法，建立「vid 消除 = 省掉三处手动切分」的直觉。

**操作步骤**：

1. 打开 `examples/developer_mode/matmul_add_developer.py`，定位 L40 的 `T.Kernel(..., threads=2, ...)` 与 L48-49 的 `c_ub`/`d_ub` 申请。
2. 想象把它改回「vid 可见」写法：`with T.Kernel(m_num*n_num, is_npu=True) as (cid, vid)`，然后 `c_ub = T.alloc_shared((block_M//2, block_N), dtype)`，并在 L66 的写回处改成 `C[bx*block_M + vid*block_M//2, by*block_N]`。
3. 数一数：一个 `threads=2` 关键字替你省掉了至少 3 处 `//2` 与 `vid*...` 的手写。

**需要观察的现象**：两种写法描述的是同一个计算，只是一个把切分写给人看、一个写给编译器看。

**预期结果**：你应当能口述出「vid 消除后，前端不再出现 `VEC_NUM`、`vid`、`//2` 这些与硬件配比耦合的记号」。

> 是否真能在 NPU 上跑通见 4.3.4；本步纯属源码阅读，无需设备。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `matmul_add_developer.py` 的 `threads=2` 改成 vid 可见的 `(cid, vid)` 写法，至少要改哪三处？

**参考答案**：(1) UB 申请第 0 维 `block_M → block_M//2`；(2) GM→UB 与 UB→GM 的 GM 起点加 `vid*(block_M//2)`；(3) 任何以 UB 第 0 维为范围的循环要 `//2`。

**练习 2**：vid 消除后，`vid` 这个变量是否真的从程序里消失了？

**参考答案**：没有。它只是对 Python 前端不可见（`T.Kernel` 不再返回它），但在 TIR 层 `vid` 仍绑定到 `threadIdx.x`，由 `AscendVidReduction` pass 在 IR 里使用它来注入偏移。

---

### 4.2 threads=1/2 如何变成 IR 层的 vid 绑定与 CV 配比

#### 4.2.1 概念说明

`threads` 参数同时承担两件事：**声明 CV 配比**（1→C:V=1:1，2→C:V=1:2）与**触发 vid 消除模式**。前端只是把一个整数传下去，真正的「翻译」发生在两处：Python 的 `T.Kernel` 决定返回值形态，C++ 的 `KernelLaunch` 决定 IR 结构。

#### 4.2.2 核心流程

```text
T.Kernel(blocks, threads=2, is_npu=True)
   │
   │  Python: kernel.py
   │  ├─ assert threads in [1, 2]
   │  ├─ attrs["tilelang.is_npu_kernel_frame_dev_mode"] = True   # 关键标记
   │  └─ __enter__ 只返回单个 cid（dev_mode 分支）
   ▼
_ffi_api.KernelLaunch(blocks, [2], attrs)
   │
   │  C++: src/ir.cc  (is_npu_kernel_frame_dev_mode 分支)
   │  ├─ cid  → blockIdx.x   (extent = blocks)
   │  ├─ vid  → threadIdx.x  (extent = 2)          ← vid 由此进入 TIR
   │  └─ prim_func.attrs["npu_cv_ratio"] = "cv_1_2"  ← CV 配比由此进入 TIR
   ▼
PrimFunc（带 threadIdx.x 绑定 + npu_cv_ratio 属性）
   │
   ▼  AscendInferBufferScope（先确定哪些 buffer 是 UB）
   ▼  AscendVidReduction（读到 threadIdx.x extent==2，开始改写）
```

`threads=None`（旧的 `is_npu_kernel_frame`）走的是另一条分支：`vid → blockIdx.y`（extent 固定为 2），并且 `__enter__` 返回 `(cid, vid)` 两个变量——这就是「vid 可见」的旧模式。

#### 4.2.3 源码精读

**Python 侧**：`T.Kernel` 在 `is_npu=True` 时分支处理 `threads`：

[tilelang/language/kernel.py:247-263](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/kernel.py#L247-L263) — 这段断言 `threads` 只能是 1 或 2，把 `threads` 规整成单元素列表，并打上 `tilelang.is_npu_kernel_frame_dev_mode` 标记；与之相对，`threads is None` 时打的是 `tilelang.is_npu_kernel_frame`（旧模式）。

而 `__enter__` 依据该标记决定返回值：

[tilelang/language/kernel.py:101-104](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/kernel.py#L101-L104) — 这段是返回值分叉点：`maybe_npu`（旧模式）返回 `[cid, vid]` 两个变量，而 `maybe_npu_dev_mode`（threads=1/2）只返回 `cid` 一个变量——这就是 vid 对前端不可见的直接原因。

**C++ 侧**：`KernelLaunch` 在 `is_npu_kernel_frame_dev_mode` 分支构造 IR：

[src/ir.cc:260-287](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L260-L287) — 这段做三件事：把 `cid` 绑到 `blockIdx.x`、把 `vid` 绑到 `threadIdx.x`（extent 即 `threads` 的值），并根据 `threads` 是 1 还是 2 把 `npu_cv_ratio` 属性设为 `cv_1_1` 或 `cv_1_2`。注意它显式断言 `block_size.size() == 1`，即 dev 模式只支持一维 thread。

`cv_1_1` / `cv_1_2` 是字符串常量，定义在：

[src/transform/common/attr.h:23-25](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/common/attr.h#L23-L25) — 这段定义了两种 CV 配比的属性值，下游 codegen 据此知道该 kernel 是 1:1 还是 1:2。

#### 4.2.4 代码实践

**实践目标**：验证「`threads` 的值会原样落到 `threadIdx.x` 的 extent 与 `npu_cv_ratio` 上」。

**操作步骤**：

1. 在 `matmul_add_developer.py` 同目录写一个最小 prim_func（参考测试用例 `gm_ub_gm_identity`），用 `threads=2`。
2. 用 `tilelang.lower(prim_func, target="ascendc", pass_configs=...)` 拿到 lowered IR（`lower` 只产出源码级 IR，不需要真实 NPU）。
3. 在打印的 TIR 顶部找到 `thread_extent` 注解，确认 `"threadIdx.x"` 的 extent 是 2；并在 PrimFunc 属性里找到 `npu_cv_ratio = "cv_1_2"`。
4. 把 `threads=2` 改成 `threads=1`，重新 lower，确认 extent 变 1、`npu_cv_ratio = "cv_1_1"`。

**需要观察的现象**：`threadIdx.x` 的 extent 与 `npu_cv_ratio` 随 `threads` 同步变化。

**预期结果**：`threads=k` ⇒ `threadIdx.x.extent == k` 且 `npu_cv_ratio == ("cv_1_2" if k==2 else "cv_1_1")`。

> 若当前环境无法 import tilelang（缺 CANN/wheel），此步标注「待本地验证」；源码逻辑已由 4.2.3 的链接固定。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `threads` 只允许取 1 或 2？

**参考答案**：因为它直接映射到硬件 CV 配比，而 Ascend 一个 Cube 最多配 2 个 Vector（C:V=1:1 或 1:2）。`kernel.py:253` 与 `ir.cc:282-287` 都以此为基础断言。

**练习 2**：`npu_cv_ratio` 属性是给谁看的？

**参考答案**：给下游 codegen 与运行时看，告诉它们这个 kernel 该按 1:1 还是 1:2 启动 Cube/Vector 核。它本身不参与 vid 消除的改写决策（改写只看 `threadIdx.x` 的 extent）。

---

### 4.3 AscendVidReduction pass 的核心改写

#### 4.3.1 概念说明

`AscendVidReduction` 是一个 `IRMutatorWithAnalyzer`，注册名 `tl.AscendVidReduction`。它只做一件事：**当 `threadIdx.x` 的 extent 等于 2 时，把所有该切分的 UB 相关 IR 节点改写成「已按 vid 切分」的等价形式；否则整个 pass 是 no-op。**

它必须排在 `AscendInferBufferScope` 之后（见 4.3.3 的 phase.py 引用），因为它依赖 buffer 的 scope 已经被钉死为 `shared.ub`，才能判断「哪些 buffer 需要处理」。

#### 4.3.2 核心流程

pass 内部维护一个核心开关 `threads_cnt_`（默认 1）和隐藏变量 `vid_`。它先在 `AttrStmt(thread_extent, "threadIdx.x")` 处把它们捕获，随后对 5 类 IR 节点做改写：

```text
捕获阶段：VisitStmt_(AttrStmtNode)
   读 thread_extent["threadIdx.x"] → vid_ = 其 Var, threads_cnt_ = 其 extent
   若 threads_cnt_ != 2 ⇒ 后续所有 Visit 直接走基类（no-op）

改写阶段（仅 threads_cnt_==2 时生效）：
  ① BlockNode        : 把每个 UB buffer 的第 0 维 shape // 2  （ModifyExtents）
  ② tl.ascend_copy   : GM↔UB 时，给 GM 下标加 vid_*(ub_shape[0]//2)  （ModifyBufferLoadIndices）
  ③ IsTileOp 一族     : 把末尾的 size 参数 // 2              （ModifyTileOpSize）
  ④ tl.ascend_reduce : 解析模板串 "reduce_sum<dtype,M,N,dim>"，把 M // 2 （ModifyAscendReduce）
  ⑤ ForNode          : 若循环变量用在某 vid-UB 的第 0 维，则 extent // 2 （VisitStmt_ ForNode）
```

#### 4.3.3 源码精读

**pass 注册与入口**：

[src/transform/ascend_vid_reduction.cc:1157-1167](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_vid_reduction.cc#L1157-L1167) — 这段注册 `tl.AscendVidReduction` 为 `CreatePrimFuncPass`，即对模块里每个 `PrimFunc` 单独跑 `Substitute`。

它在流水线中的位置（紧随 scope 推断）：

[tilelang/engine/phase.py:52-54](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L52-L54) — 这段把 `AscendVidReduction` 排在 `AscendInferBufferScope` 之后、`BufferShapeCollector` 之前，属于 `LowerAndLegalize` 阶段最靠前的几步之一。顺序很关键：必须先知道 buffer 是 UB，才能决定是否减半。

**① 捕获 vid 与 threads_cnt（no-op 总开关）**：

[src/transform/ascend_vid_reduction.cc:1143-1154](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_vid_reduction.cc#L1143-L1154) — 这段在 `thread_extent` 属性处捕获 `vid_`（`threadIdx.x` 的 Var）与 `threads_cnt_`（其 extent）。`threads_cnt_` 是贯穿全 pass 的总开关，后续每个 `Visit*` 一进来就判断它是否等于 2（例如 L307、L663），不等于 2 就直接返回原节点——这就是 `threads=1` 时整 pass 变 no-op 的根本原因。

**② UB 形状第 0 维减半**：

[src/transform/ascend_vid_reduction.cc:111-138](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_vid_reduction.cc#L111-L138) — 这段是 `ModifyExtents`：只对第 0 维（`i==0`）做 `// threads_cnt_`（带「小于 1 则取 1」的保护），其余维度原样保留。它被 `VisitStmt_(BlockNode)` 在遍历 `alloc_buffers` 时对每个需要 vid 消除的 UB buffer 调用。

哪些 buffer 「需要 vid 消除」由 `NeedsVidReduction` 判定——是 UB 且不在 skip-set 里：

[src/transform/ascend_vid_reduction.cc:269-271](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_vid_reduction.cc#L269-L271) — 这段定义判定条件：`IsUbBuffer(buffer) && buffers_skip_vid_reduction_.count(buffer) == 0`。`IsUbBuffer` 即检查 storage_scope 是否为 `"shared.ub"`（L90-98）。

**③ GM 注入 vid 偏移**：

[src/transform/ascend_vid_reduction.cc:389-422](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_vid_reduction.cc#L389-L422) — 这段是 `ModifyBufferLoadIndices`：对 GM 的 `BufferLoad`，在第 `(下标数 - ub维数)` 维加上 `vid_ * modified_ub_buf->shape[0]`。换言之，把「UB 第 0 维对应的那一维 GM 坐标」平移半个块，使两个 vid 各取相邻的半块。这正是 4.1 里「GM 加 vid 偏移」的 IR 实现。

**④ tile op 与 reduce 的 size 减半**：

[src/transform/ascend_vid_reduction.cc:497-547](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_vid_reduction.cc#L497-L547) — 这段定义 `IsTileOp`（一张涵盖 add/mul/exp/compare/sin 等 `tl.ascend_*` 的白名单）并实现 `ModifyTileOpSize`：把这些 intrinsic 末尾那个表示元素总数的 size 参数 `// threads_cnt_`。因为 UB 减半后，一次向量指令处理的元素数也得减半。

[src/transform/ascend_vid_reduction.cc:560-660](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_vid_reduction.cc#L560-L660) — 这段是 `ModifyAscendReduce`：它把 reduce 的模板串（如 `"reduce_sum<float, 64, 64, -1>"`）解析出 `M`，将 `M // 2` 后重新拼回（变成 `"reduce_sum<float, 32, 64, -1>"`）。因为被收缩的 UB 第 0 维已减半，reduce 模板的 M 维度必须同步减半，否则指令会越界。

**⑤ 循环范围减半**：

[src/transform/ascend_vid_reduction.cc:1105-1141](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_vid_reduction.cc#L1105-L1141) — 这段处理 `ForNode`：先用 `LoopVarUsedInVidReducedUbFirstDim` 判断循环变量是否被用作某个 vid-UB 的第 0 维下标（或在其 `access_ptr` 的 offset/extent 里），若是则把循环 `extent // 2`，并把该循环压入 `current_loops_` 供后续 GM 偏移复用。注意它**不是无脑减半所有循环**，只减半「确实索引了 vid-UB 第 0 维」的循环。

> 小贴士：第 0 维之所以特殊，是因为 vid 切分永远沿 UB 的第 0 维进行（见 `ModifyExtents` 只处理 `i==0`）。所以只有用到第 0 维的循环和搬运才需要改写。

#### 4.3.4 代码实践

**实践目标**：亲眼看到 `AscendVidReduction` 把一个 `threads=2` 的 kernel 改写成「UB 减半 + GM vid 偏移」，并确认 `threads=1` 时 pass 不动。

**操作步骤**（参考测试 `gm_ub_gm_identity`，见 [testing/python/language/cvseparate/test_tilelang_ascend_language_vid_reduction.py:67-80](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/cvseparate/test_tilelang_ascend_language_vid_reduction.py#L67-L80)）：

1. 写如下最小 prim_func（一个 GM→UB→GM 的恒等拷贝）：

   ```python
   # 示例代码：仅供观察 IR，不一定是项目原有文件
   import tilelang, tilelang.language as T
   M, N, block_M, block_N = 128, 128, 128, 128
   m_num, n_num = M // block_M, N // block_N
   @T.prim_func
   def main(A: T.Tensor((M, N), "float16"), C: T.Tensor((M, N), "float16")):
       with T.Kernel(m_num * n_num, threads=2, is_npu=True) as (cid):
           bx = cid // n_num
           by = cid % n_num
           a_ub = T.alloc_shared((block_M, block_N), "float16")
           T.copy(A[bx * block_M, by * block_N], a_ub)
           T.copy(a_ub, C[bx * block_M, by * block_N])
   ```

2. 用 `tilelang.lower(main, target="ascendc", out_idx=[1], pass_configs=pass_configs)` 打印 lowered IR（`pass_configs` 至少含 `TL_ASCEND_AUTO_CV_COMBINE` / `TL_ASCEND_AUTO_SYNC` 等，参考测试 L39-44）。
3. 在 IR 里定位 `a_ub` 的分配：确认其 shape 已由 `[128, 128]` 变为 `[64, 128]`。
4. 定位 GM→UB 的 `ascend_copy`：确认 GM 行下标出现 `+ threadIdx.x * 64` 形式的 vid 偏移。
5. 把 `threads=2` 改为 `threads=1`，重新 lower，确认 `a_ub` 仍是 `[128, 128]`、GM 下标无 vid 偏移（pass no-op）。

**需要观察的现象**：`threads=2` 时 UB 第 0 维减半、GM 出现 vid 偏移；`threads=1` 时一切不变。

**预期结果**：与上述一致。若数值正确性需在 NPU 上验证，可直接跑测试文件中的 `test_vid_reduction_gm_ub_gm_identity`（pytest）。

> 若当前环境无 NPU/CANN，第 2-4 步的 IR 打印可尝试 `tilelang.lower`（源码级，理论上不依赖设备）；若 lower 也无法执行，则标注「待本地验证」，源码改写逻辑已由 4.3.3 的链接固定。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `AscendVidReduction` 必须排在 `AscendInferBufferScope` 之后？

**参考答案**：它用 `IsUbBuffer`（检查 `storage_scope == "shared.ub"`）来决定哪些 buffer 要减半，而 scope 是 `AscendInferBufferScope` 钉死的。若顺序反了，所有 buffer 的 scope 还是 `dynamic`，pass 将无从下手。见 `phase.py:52-54`。

**练习 2**：`threads=1` 时，`AscendVidReduction` 会修改 IR 吗？

**参考答案**：不会。`threads_cnt_` 取到 1，所有 `Visit*` 入口的 `if (threads_cnt_ != 2)` 判断成立，直接走基类返回原节点，整 pass 是 no-op。测试 `test_no_vid_reduction_threads1_identity` 正是为此基线而设。

**练习 3**：reduce 模板串里的 `M` 为什么也要 `// 2`？只减半 UB shape 不够吗？

**参考答案**：不够。`tl.ascend_reduce` 的模板 `reduce_sum<dtype,M,N,dim>` 里的 `M` 是告诉硬件指令「这次要 reduce 的矩阵行数」，它必须与已减半的 UB 第 0 维一致，否则指令会按原始 `M` 访问越界。见 `ModifyAscendReduce` L560-660。

---

### 4.4 skip-set 例外与下游 pass 的衔接

#### 4.4.1 概念说明

并非所有 UB 都能机械地「第 0 维减半」。典型反例是 **gather（间接索引）**：当 GM 的下标本身来自另一块 UB（如 `KV[b_i, idx_ub[bi_i], g_i, :D]`，其中 `idx_ub` 是 UB），那么这块目标 UB（`kv_ub`）在每个 vid 上要取的数据地址取决于 `idx_ub[bi_i]` 的运行时值，不能简单按第 0 维对半切。

为此 pass 维护一个 **skip-set**（`buffers_skip_vid_reduction_`）：命中 skip-set 的 UB 不减半，其形状被原样保留，并在函数末尾以 `buffers_skip_vid_reduction` 属性的形式告诉下游 pass。

#### 4.4.2 核心流程

```text
遍历 ascend_copy：
  若 GM↔UB 且 GM 的下标「直接」包含某个 UB 的 BufferLoad（即 gather）
     ⇒ 把该 UB 加入 buffers_skip_vid_reduction_（shape 不减半）

函数出口：
  把 skip-set 里所有 buffer 的名字打包成 PrimFunc 属性 "buffers_skip_vid_reduction"

下游 AscendWorkspaceReduction：
  读取该属性，对 skip 名单里的 UB 走「保留全 shape + 用循环变量做 vid 偏移」的另一条路径
```

注意两条路径的区别：

- **普通 UB（被 vid 消除）**：shape `//2`，GM 偏移用 `vid * (shape[0]//2)`。
- **skip UB（gather 目标）**：shape 不变，GM 偏移改用循环变量维度（`!ub_was_vid_reduced` 分支，靠 `current_loops_` + `GmDimNeedsVidOffset` 判定）。

#### 4.4.3 源码精读

**skip-set 的检测**：`AscendCopyAnalyzer` 在每个 GM↔UB 的 copy 处，检查 GM 下标是否「直接」含有 UB 的 `BufferLoad`：

[src/transform/ascend_vid_reduction.cc:199-267](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_vid_reduction.cc#L199-L267) — 这段定义 `AscendCopyAnalyzer`：对 `tl.ascend_copy`，若 src 或 dst 是 UB 而对端 GM 的下标含 UB `BufferLoad`，就把该 UB 加入 `buffers_skip_vid_reduction`。判定函数 `IndicesContainUbBufferLoad` 只看「下标直接就是一个 BufferLoad」的情形（L142-153），不下钻到复合表达式。

**skip 名单 export 为属性**：

[src/transform/ascend_vid_reduction.cc:44-56](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_vid_reduction.cc#L44-L56) — 这段在 `Substitute` 末尾，若 skip-set 非空，就把其中所有 buffer 的名字收集成一个 `Array<String>`，写入 PrimFunc 属性 `buffers_skip_vid_reduction`，供下游 pass 读取。

**真实样本**：`sparse_flash_attn_developer_vid_reduce.py` 里有一段典型的 gather——`KV` 的行下标来自 UB `indices_ub_`：

[examples/developer_mode/sparse_flash_attn_developer_vid_reduce.py:137-143](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/sparse_flash_attn_developer_vid_reduce.py#L137-L143) — 这段是 skip-set 的触发点：`T.copy(KV[b_i, indices_ub_[bi_i], g_i, :D], kv_ub)` 中 GM 的第二维下标 `indices_ub_[bi_i]` 是一个 UB 的 `BufferLoad`，因此 `kv_ub`（及 `kv_tail_ub`）会被加入 skip-set，其 shape 不被减半；而 `indices_ub_` 本身是普通 UB，正常减半。注意该 kernel 用 `threads=2`（L85）。

**下游消费**：`AscendWorkspaceReduction` 读取该属性，对 skip 名单里的 buffer 走特殊路径：

[src/transform/ascend_workspace_reduction.cc:888-899](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_workspace_reduction.cc#L888-L899) — 这段从 PrimFunc 属性读出 `buffers_skip_vid_reduction` 名单（以及 `buffer_shapes`），后续在 `copy_ub_to_gm` 等处（如 L1349-1354）对名单内的 UB 使用原始全 shape 而非减半 shape。这是 vid 消除与 workspace 消除两个 pass 之间的契约。

#### 4.4.4 代码实践

**实践目标**：在一个真实算子里区分「被 vid 消除的 UB」与「进 skip-set 的 UB」。

**操作步骤**：

1. 打开 `examples/developer_mode/sparse_flash_attn_developer_vid_reduce.py`，定位 L85 的 `threads=2` 与 L137-143 的 gather 拷贝。
2. 列出该 kernel 里所有 UB（L100-112 的一堆 `alloc_shared`），分为两组：
   - **普通组**：如 `acc_s_ub`、`acc_o`、`sumexp`、`m_i`——它们第 0 维是 `v_block`，会被减半。
   - **skip 组**：`kv_ub`、`kv_tail_ub`——因为它们的 GM 源下标含 `indices_ub_[bi_i]`，是 gather 目标。
3. 解释为什么 `indices_ub_` 本身不在 skip 组（提示：它的 GM 源下标 `IDX[b_i, s_i, g_i, 0:BI]` 是纯整数切片，不含 UB BufferLoad）。

**需要观察的现象**：同一份 kernel 里，有的 UB 形状被 pass 减半、有的原样保留，取决于其 GM 搬运下标是否含 UB 间接索引。

**预期结果**：能准确把每个 UB 归入「减半」或「skip」两组，并说出判定依据。

> 若要在 IR 里实证，可用 `tilelang.lower` 打印该 prim_func 的 lowered IR，在 skip-set 属性里确认 `kv_ub`/`kv_tail_ub` 的名字出现、而 `acc_s_ub` 不出现。标注「待本地验证」如环境不具备。

#### 4.4.5 小练习与答案

**练习 1**：`IndicesContainUbBufferLoad` 为什么只看「下标直接是 BufferLoad」，不下钻到复合表达式（如 `idx_ub[bi_i] + 1`）？

**参考答案**：因为只有「下标直接是另一个 UB 的值」才是硬件可表达的 gather 模式（地址完全由 UB 内容决定）。复合表达式通常意味着用户已在做更复杂的手动切分，pass 不宜擅自假设，故保守地不把它判为 skip。见 L142-153。

**练习 2**：如果某个 UB 既出现在 skip-set 里、又被普通 `T.copy` 当成完整 buffer 搬运，会发生什么？

**参考答案**：因为 `NeedsVidReduction`（L269-271）要求 `count(buffer) == 0` 才减半，命中 skip-set 的 buffer 不会被减半；它走 `!ub_was_vid_reduced` 分支（L831 起），GM 偏移由 `current_loops_` + `GmDimNeedsVidOffset` 用循环变量维度来注入，而不是 `vid*shape[0]`。

---

## 5. 综合实践

**任务**：把一段「vid 可见」的伪代码手工翻译成 `threads=2` 的 vid 消除写法，并用 `AscendVidReduction` 的源码验证你的翻译是否等价。

给定如下 vid 可见写法（C:V=1:2，`VEC_NUM=2`）：

```python
# 示例代码：待翻译的 vid 可见写法
VEC_NUM = 2
with T.Kernel(block_num, is_npu=True) as (cid, vid):
    bx = cid // n_num
    by = cid % n_num
    a_ub = T.alloc_shared((block_M // VEC_NUM, block_N), dtype)   # ① 已减半
    c_ub = T.alloc_shared((block_M // VEC_NUM, block_N), dtype)
    T.copy(A[bx*block_M + vid*(block_M//VEC_NUM), by*block_N], a_ub)  # ② GM 含 vid 偏移
    for i in T.Parallel(block_M // VEC_NUM):                          # ③ 循环已减半
        c_ub[i] = a_ub[i] * 2.0
    T.copy(c_ub, C[bx*block_M + vid*(block_M//VEC_NUM), by*block_N])
```

**要求**：

1. 把它改写成 `threads=2` 的 vid 消除写法：`T.Kernel` 只返回 `cid`，UB 写完整 `block_M`，GM 不带 vid 偏移，循环写完整 `block_M`。
2. 对照本讲 4.3.3 引用的五处源码（`ModifyExtents` L111-138、`ModifyBufferLoadIndices` L389-422、`ModifyTileOpSize` L497-547、`VisitStmt_(ForNode)` L1105-1141、skip-set L199-267），逐条说明：你省掉的每一处 `//2` / `vid*...`，分别由 pass 的哪一段代码补回来。
3. 思考：如果你的 kernel 里还有一个 gather（GM 下标含 UB BufferLoad），你的翻译里那一块的 UB 形状该写多少？为什么？

**预期产出**：一份改写后的 prim_func + 一张「省掉的写法 ↔ 补回来的 pass 代码行」对照表 + 对 gather 情形的说明（应答：gather 目标 UB 仍写完整 shape，因为它会进 skip-set 而不被减半）。

> 本实践为纯源码阅读与翻译型任务，不强制在 NPU 上运行；若要验证数值，可把改写后的 prim_func 放进测试文件 `test_tilelang_ascend_language_vid_reduction.py` 的模式里跑一次 pytest。

## 6. 本讲小结

- Ascend 的 `vid`（vector id）在 C:V=1:2 硬件上要求把 UB 数据沿第 0 维对半切；旧式写法把这一细节泄漏到前端，需在 UB 形状、GM 偏移、循环范围三处手动 `//2`。
- `T.Kernel(..., threads=2, is_npu=True)` 同时声明 CV 配比并触发 vid 消除：Python 侧只返回单个 `cid`，C++ 侧把 `vid` 绑到 `threadIdx.x`（extent=2）并写入 `npu_cv_ratio="cv_1_2"` 属性。
- `AscendVidReduction` pass（注册名 `tl.AscendVidReduction`）排在 `LowerAndLegalize` 中、`AscendInferBufferScope` 之后，仅当 `threadIdx.x` 的 extent==2 时生效；它对 UB 形状减半、GM 注入 vid 偏移、tile op / reduce 的 size 减半、用到 vid-UB 第 0 维的循环范围减半。
- 并非所有 UB 都减半：gather（GM 下标含 UB BufferLoad）目标 UB 进入 `buffers_skip_vid_reduction_` skip-set，形状保留，走「循环变量维度做 vid 偏移」的另一条路径。
- skip-set 以 `buffers_skip_vid_reduction` PrimFunc 属性的形式传给下游 `AscendWorkspaceReduction`，是两个 pass 之间的契约。
- `threads=1` 时整个 pass 是 no-op（所有 `Visit*` 在 `threads_cnt_ != 2` 时直接返回原节点），这正是测试里 `threads=1` 基线用例的存在意义。

## 7. 下一步学习建议

- **继续 u5 单元**：阅读 u5-l4（Workspace 消除），看 `AscendWorkspaceReduction` 如何消费本讲产出的 `buffers_skip_vid_reduction` 属性，把 Cube→Vector 的两阶段 GM 中转自动化。
- **回到 pass 全景**：学完 u5 后建议读 u6-l1（编译 Pass 全景与配置），把 `AscendVidReduction` 放回 `LowerAndLegalize` 的完整顺序里，理解它为何必须早于 `AscendLowerParallelToVector`、`LowerTileOp`。
- **动手方向**：尝试给一个自己的算子加上 `threads=2`，用 `tilelang.lower` 打印 IR，逐条核对本讲列出的五类改写是否都按预期出现；这是检验你是否真正理解 vid 消除的最快方式。
