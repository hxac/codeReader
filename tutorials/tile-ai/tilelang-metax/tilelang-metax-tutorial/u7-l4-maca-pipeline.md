# MACA 编译流水线与 transform

## 1. 本讲目标

本讲承接 u7-l1（MACA 后端架构总览）和 u4-l1（lowering 流程），把镜头从「MACA 的零件清单」推进到「这些零件如何被串成一条编译流水线」。

读完本讲，你应该能够：

- 说清楚 `tilelang/maca/pipeline.py` 中 `MACAPassPipelineBody` 是如何被引擎按 target 名分派、又按什么顺序串联每个 pass 的。
- 解释 MACA 流水线相对 CUDA 流水线「复用了什么、丢弃了什么、新增了什么」，并能指到具体代码行。
- 讲清楚 `LowerMACAIntrin` pass **到底下译了什么 intrinsic**（关键结论：它只处理 L2 persistent 的 stream access policy 调用，**不**处理 mfma 计算指令）。
- 理解 L2 persistent 策略的三段式落地链路：DSL 注解 → `LowerL2Persistent` 收集属性 → `LowerMACAIntrin` 发射 runtime 调用。
- 了解 2:4 结构化稀疏 GEMM 在 MACA 上的指令选择与 warp 划分。

## 2. 前置知识

在进入正题前，先回顾三个关键概念（细节见对应讲义）：

- **pass / 流水线（pipeline）**：TileLang 把一个 PrimFunc 从高层 Tile IR 一层层降级为设备可编译的 TIR。每一步降级就是一个「pass」。一条 pass 流水线就是「按固定顺序串联的一组 pass」。C++ 侧实现每个 pass，Python 侧的 `pipeline.py` 负责**编排顺序**（见 u5-l2）。
- **target 分派**：引擎 `tilelang.engine.lower` 会先跑与 target 无关的语义检查，再用 `resolve_pipeline(target)` 取出与 `target.kind.name` 同名的流水线（见 u4-l1）。`maca` 这个 target kind 对应的就是本讲的 `MACAPassPipelineBody`。
- **MACA 后端零件**（见 u7-l1）：C++ 侧通过 `TVM_REGISTER_TARGET_KIND("maca", kDLMACA)` 注册一等 target，最显眼属性是 `thread_warp_size=64`；Python 侧在 `import tilelang` 时经 `from . import maca` 挂载 target detector、device codegen、execution backend。

本讲新增两个术语：

- **L2 persistent（持久化 L2 访问策略）**：GPU 的 L2 cache 允许为某个 global buffer 区域设置一个「访问策略窗口」，提示硬件尽量把它常驻 L2，减少反复回读显存。CUDA 用 `cudaStreamSetAttribute(... accessPolicyWindow ...)` 实现；MACA 用等价的 `mc*` runtime 接口实现。
- **2:4 结构化稀疏（structured sparsity）**：沿 K 维每连续 4 个元素里恰好有 2 个非零（50% 稀疏），硬件张量核可直接加速。压缩后矩阵 A 只存非零（列数减半），另用一个 metadata 张量 E 编码「哪 2 个位置非零」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/maca/pipeline.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py) | MACA 的 pass 流水线主体，定义 `MACAPassPipelineBody` 并注册为 `"maca"` 流水线。 |
| [tilelang/maca/transform/\_\_init\_\_.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/transform/__init__.py) | MACA 专属 pass 的 Python 前端，暴露 `LowerMACAIntrin()`。 |
| [src/maca/transform/lower_maca_intrin.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/transform/lower_maca_intrin.cc) | `LowerMACAIntrin` 的 C++ 实现：把 `l2_persistent_map` 属性翻译成 MACA stream access policy 的 prologue/epilogue。 |
| [src/cuda/transform/lower_l2_persistent_annotation.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/transform/lower_l2_persistent_annotation.cc) | `LowerL2Persistent`（被 MACA 复用）：收集 `l2_hit_ratio_map` 注解、计算 buffer 字节数、写入 `l2_persistent_map` PrimFunc 属性。 |
| [src/maca/runtime/maca_runtime.h](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_runtime.h) | 定义 MACA stream access policy 的 runtime 函数名字符串。 |
| [src/maca/op/gemm_sp.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm_sp.cc) | MACA 的 2:4 稀疏 GEMM 指令选择（恒返回 `maca.mma.sp`）与 warp 划分，并注册到全局实现表。 |
| [tilelang/maca/op/gemm_sp/gemm_sp_mma.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/op/gemm_sp/gemm_sp_mma.py) | 稀疏 GEMM 的 Python 实现 `GemmSPMMA`：`infer_layout` 与 `lower`，构造 `SparseTensorCoreIntrinEmitter`。 |
| [examples/gemm_sp/example_gemm_sp.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm_sp/example_gemm_sp.py) | 端到端稀疏 GEMM 示例，含压缩、`T.gemm_sp` 调用与正确性比对。 |

## 4. 核心概念与源码讲解

### 4.1 MACA pipeline：一条「复用 cuda 骨架 + 插一个专属 pass」的流水线

#### 4.1.1 概念说明

流水线的本质是一份「按顺序执行的 pass 脚本」。TileLang 把它做成「一个 target 一条流水线」：每个后端在 `tilelang/<backend>/pipeline.py` 里写一个 `XxxPassPipelineBody(mod, target) -> mod` 函数，再用 `register_pipeline(PassPipeline("<kind>", XxxPassPipelineBody))` 挂进一张全局表。

MACA 没有从零写流水线，而是**借用 CUDA 流水线的骨架**，再：

- **复用**大量 cuda 命名空间的 pass（如 `LowerL2Persistent`、`PersistThreadblock`、`LowerSharedTmem`）——这些 pass 内部要么 target 中立，要么自带「非本 target 就 no-op」的守卫。
- **丢弃** CUDA 专属的 Hopper/Blackwell pass（warp specialization、TCGEN05、mbarrier、TMA 相关）——MACA 硬件没有这些能力。
- **新增**唯一的 MACA 专属 pass：`LowerMACAIntrin`。

这是 metax 分支「最小改动、对齐主流程」设计哲学的体现。

#### 4.1.2 核心流程

引擎的分派非常简单——按 target 名查表：

```
engine.lower(mod, target)
  → PreLowerSemanticCheck(mod)          # 与 target 无关的语义检查
  → pipeline = resolve_pipeline(target) # 按 target.kind.name 取流水线
  → mod = pipeline.lower(mod, target)   # 串联所有 pass
```

而 `resolve_pipeline` 就是「拿 target kind 名字查注册表」：

[backend/pass_pipeline/pipeline.py:46-48](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/pass_pipeline/pipeline.py#L46-L48) 这三行就是分派的核心：`get_pipeline(target.kind.name)` 用 target kind 名（`"maca"`）去 `_PIPELINES` 字典里取出对应的 `PassPipeline`。

MACA 的注册发生在本文件末尾：

[maca/pipeline.py:151-153](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py#L151-L153) 这两行把 `MACAPassPipelineBody` 注册成名为 `"maca"` 的流水线——名字必须与 `maca_target_kind.cc` 里注册的 target kind 完全一致，引擎才能通过 `resolve_pipeline` 找到它。

`MACAPassPipelineBody` 把 pass 分成两段执行：先调 `MACAPassPipelineBodyPrologue`（高层 Tile IR 降级 + 布局推断），再继续后半段（buffer 平坦化、向量化、host/device 拆分、收尾）：

[maca/pipeline.py:77-82](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py#L77-L82) 这里 `mod = MACAPassPipelineBodyPrologue(mod, target)` 先跑前半段，随后两条 `tilelang.cuda.transform.*` 是**复用 CUDA 命名空间**的 shared tmem/barrier 处理。

#### 4.1.3 源码精读

前半段（Prologue）是 MACA 与 CUDA **最一致**的部分，几乎逐行相同——绑定 target、物化 kernel launch、做流水线规划与布局推断：

[maca/pipeline.py:45-57](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py#L45-L57) 这段把 `PipelinePlanning` + `InjectSoftwarePipeline`（软件流水线，见 u4-l4）、`LayoutInference`（布局推断，见 u4-l3）、`LowerTileOp`（算子降级）依次跑完后，第 57 行 `tilelang.cuda.transform.LowerL2Persistent()` 就是 MACA **复用** cuda pass 处理 L2 persistent 注解——注意它虽然挂在 `cuda` 命名空间下，但写出的 `l2_persistent_map` 属性对 MACA 同样有效，随后由 `LowerMACAIntrin` 消费。

后半段（body）则是观察 MACA 差异化的最佳窗口。我们看三个关键位置：

**① 复用 cuda pass（无差异）。** 这些 pass 跨后端通用：

[maca/pipeline.py:122-123](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py#L122-L123) `LowerLDGSTG` 与 `LowerHopperIntrin` 都是 cuda 命名空间的 pass。`LowerHopperIntrin` 在 MACA 上是 **no-op**（pass 内部会按 target 自检，非 Hopper 直接放行），出现在这里只是为了与 cuda 流水线保持结构同构。

**② MACA 唯一的专属 pass——夹在两个 cuda pass 之间：**

[maca/pipeline.py:124-126](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py#L124-L126) 第 124 行 `tilelang.maca.transform.LowerMACAIntrin()` 是整条流水线里**唯一**的 MACA 专属 pass。它紧跟 `LowerHopperIntrin`、先于 `AnnotateDeviceRegions` + `SplitHostDevice`——即它必须在 host/device 拆分**之前**完成，因为它要往设备函数体里插入 prologue/epilogue 语句（见 4.2）。

**③ 收尾同样复用 cuda pass：**

[maca/pipeline.py:146](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py#L146) 流水线末尾的 `tilelang.cuda.transform.PersistThreadblock()` 把线程块改写成 persistent kernel（见 u8-l2），这一步 MACA 同样适用，因此直接复用 cuda 实现。

**对比 CUDA 流水线，MACA 丢弃了什么**（对照 [cuda/pipeline.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py)）：

- `ProducerConsumerWarpSpecialized`（cuda L95）、`LowerBlackwell2SM`（cuda L100）——Hopper/Blackwell 专属。
- `FuseMBarrierArriveExpectTx`（cuda L174）、`InjectFenceProxy`（cuda L229）、`InjectTcgen05Fence`（cuda L239）、`AnnotateWarpGroupRegAlloc`（cuda L244）——TMA / mbarrier / TMEM 相关，MACA 不支持。
- CUDA 流水线里对 `have_mbarrier(target)` 的版本守卫（cuda L159-168）在 MACA 也无需存在。

一句话总结：**MACA 流水线 = cuda 流水线骨架 − Hopper/Blackwell 专属 pass ＋ `LowerMACAIntrin`**。

#### 4.1.4 代码实践

1. **实践目标**：把 `MACAPassPipelineBody` 里实际执行的 pass 按顺序列出来，并标注哪些是 cuda 复用、哪个是 MACA 专属。
2. **操作步骤**：打开 `tilelang/maca/pipeline.py`，从 `MACAPassPipelineBody`（L77）开始，把每一条 `mod = ...(...)(mod)` 摘录下来（不要漏掉 prologue 里 `MACAPassPipelineBodyPrologue` 内部的 pass）。
3. **需要观察的现象**：你会得到约 40+ 条 pass；其中只有一条来自 `tilelang.maca.transform`，其余要么来自 `tilelang.transform`（后端中立），要么来自 `tilelang.cuda.transform`（cuda 复用）。
4. **预期结果**：唯一一条 `tilelang.maca.transform.*` 是 L124 的 `LowerMACAIntrin()`。
5. **待本地验证**：若想看到这些 pass 的实际效果，可在本地用 `tilelang.tools.pass_visualizer`（参见 [tilelang/tools/pass_visualizer](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tools/pass_visualizer) 目录）对一个 MACA kernel 逐 pass 可视化 IR 变化。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MACAPassPipelineBodyPrologue` 与 cuda 的 `CUDAPassPipelineBodyPrologue` 几乎逐行相同，metax 分支仍要单独写一份而不是直接 import 复用？

> **答案**：因为 cuda 版里夹着 cuda 专属 pass（如 `ProducerConsumerWarpSpecialized`、`LowerBlackwell2SM`，见 cuda L94-100），且 prologue 是按 target 精确编排顺序的「脚本」。MACA 需要一份不含这些专属 pass 的纯净版本，所以另写一份；未来若 MACA 在 prologue 阶段也需要专属处理（例如某条 MACA 布局修正），它有独立插入点而不会污染 cuda。

**练习 2**：`LowerHopperIntrin` 出现在 MACA 流水线里（L123），它会在 MACA 上做实际改写吗？

> **答案**：不会。它是 cuda 命名空间的 pass，内部按 target 自检，遇到非 Hopper（含 MACA）会直接原样返回（no-op）。保留它是为了与 cuda 流水线保持结构同构，降低维护成本。

### 4.2 lower_maca_intrin：把 L2 persistent 注解翻译成 runtime 调用

#### 4.2.1 概念说明

`LowerMACAIntrin` 这个名字容易让人误以为它「下译所有 MACA intrinsic（包括 mfma 计算指令）」。**这是误解**。回顾 u7-l2/u7-l3：

- mfma 计算指令（`T.tvm_mfma` → `__builtin_mxc_mma_*`）走的是 **codegen visitor + intrin_rule** 通道，在代码生成阶段处理，**不**经过这个 pass。
- fast-math / warp shuffle 等可移植 intrinsic 走 **intrin_rule + `LowerIntrin`** 通道（u7-l2）。

`LowerMACAIntrin` 的**唯一职责**是：读取 PrimFunc 上的 `l2_persistent_map` 属性，把它翻译成两条 MACA runtime 调用——在函数体开头插入「设置访问策略窗口」（prologue），在末尾插入「重置访问策略窗口」（epilogue）。换句话说，它处理的是 **L2 persistent 的 stream access policy intrinsic**，而非计算类 intrinsic。

#### 4.2.2 核心流程

```
PrimFunc
  ├─ 属性 l2_persistent_map: { buffer名 -> [hit_ratio, size_in_bytes] }
  │        （由 LowerL2Persistent 写入，见 4.3）
  │
  └─ LowerMACAIntrin::Substitute:
       1. 守卫：target 不是 maca → 原样返回
       2. 守卫：没有 l2_persistent_map 属性 → 原样返回
       3. 对每个 (buffer名, [hit_ratio, size]) ：
            生成 Evaluate(tvm_call_packed(
                "__tvm_maca_stream_set_access_policy_window",
                base_ptr, size, hit_ratio))    # 注意参数顺序
       4. 生成 epilogue：Evaluate(tvm_call_packed(
                "__tvm_maca_stream_reset_access_policy_window"))
       5. body = SeqStmt({ prologue, body, epilogue })
```

注意第 3 步的参数顺序：源码里 `packed_args` 先压入 `args[1]`（size），再压入 `args[0]`（hit_ratio），即调用顺序是 `(base_ptr, size, hit_ratio)`——这是 MACA runtime 约定的形参顺序。

#### 4.2.3 源码精读

Python 前端极薄，只是 FFI 转发：

[maca/transform/\_\_init\_\_.py:6-8](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/transform/__init__.py#L6-L8) `LowerMACAIntrin()` 只是把调用转发给 C++ 侧的 `_ffi_api.LowerMACAIntrin`，没有任何业务逻辑——典型的「Python 注册、C++ 实现」模式。

C++ 实现先做两层守卫，过滤掉与本 pass 无关的函数：

[lower_maca_intrin.cc:29-39](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/transform/lower_maca_intrin.cc#L29-L39) 这段先检查「target 必须是 maca」（否则原样返回，保证这个被 cuda 流水线骨架调用的 pass 在非 maca 上无害），再检查「必须带 `l2_persistent_map` 属性」（否则说明用户没要求 L2 persistent，直接返回）。这两道守卫解释了为何这个 pass 出现在流水线里却对绝大多数 kernel 零影响。

接着逐条构造 prologue 调用：

[lower_maca_intrin.cc:48-59](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/transform/lower_maca_intrin.cc#L48-L59) 每个 buffer 被翻译成一条 `tvm_call_packed("__tvm_maca_stream_set_access_policy_window", base_ptr, size=args[1], ratio=args[0])`。`MakeBasePtr`（L87-96）会把 buffer 的 `data` 指针加上 `elem_offset` 折算的字节偏移，得到真正的基地址。

最后把 prologue 和 epilogue 包裹到原 body 两端：

[lower_maca_intrin.cc:65-73](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/transform/lower_maca_intrin.cc#L65-L73) epilogue 是一条 `__tvm_maca_stream_reset_access_policy_window` 调用；最终 `fptr->body = SeqStmt({prologue, 原body, epilogue})`。这正解释了 4.1 里「为何必须在 `SplitHostDevice` 之前运行」——它要改写的是**设备函数体本身**，拆分之后再插就找不准位置了。

这两条 runtime 函数名定义在头文件里：

[maca/runtime/maca_runtime.h:11-14](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_runtime.h#L11-L14) 这两个字符串就是 MACA 对 L2 persistent 的 runtime 抽象，最终由 `MACADeviceAPI` / `MACAModule` 侧映射到 MetaX 的 `mc*` runtime 接口（详见 u7-l1 的「运行时四件套」）。

#### 4.2.4 代码实践

1. **实践目标**：确认 `LowerMACAIntrin` 处理的 intrinsic 范围，理解它**不**碰 mfma。
2. **操作步骤**：通读 [lower_maca_intrin.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/transform/lower_maca_intrin.cc) 全文，搜索其中是否出现 `mma`、`mfma`、`shfl`、`__expf` 等字样。
3. **需要观察的现象**：你会发现全文**只**引用 `kL2PersistentMap` 属性与两个 `tvm_maca_stream_*` runtime 名，没有任何计算指令的处理。
4. **预期结果**：得出结论——`LowerMACAIntrin` 处理的 intrinsic 仅限 L2 persistent 的 stream access policy 调用；计算类 intrinsic 在 codegen 阶段处理。
5. **待本地验证**：可选——在测试 [test_tilelang_issue_1810.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/python/issue/test_tilelang_issue_1810.py) 里（无设备时可读源码），可见它断言生成的 host 源码含 `__tvm_maca_stream_set_access_policy_window_packed` 与 `__tvm_maca_stream_reset_access_policy_window_packed`，正好印证本 pass 的输出。

#### 4.2.5 小练习与答案

**练习 1**：如果用户没有写 `T.annotate_l2_hit_ratio`，`LowerMACAIntrin` 还会做任何改写吗？

> **答案**：不会。没有注解就不会产生 `l2_persistent_map` 属性（`LowerL2Persistent` 在 map 为空时不写属性，见 4.3），本 pass 的第二道守卫（L37-39）命中「属性未定义」分支，原样返回函数。

**练习 2**：为什么 prologue 调用的实参顺序是 `(base_ptr, size, hit_ratio)`，而属性里存的数组是 `[hit_ratio, size]`（即 `args[0]=ratio, args[1]=size`）？

> **答案**：因为 MACA runtime 函数 `set_access_policy_window` 的形参顺序约定为 `(ptr, size, ratio)`，与属性数组的存储顺序不同。源码 L55-56 故意先压 `args[1]`（size）再压 `args[0]`（ratio）来匹配 runtime 约定，这是「属性存储顺序」与「runtime 调用顺序」解耦的一个细节。

### 4.3 L2 persistent 策略：三段式落地链路

#### 4.3.1 概念说明

L2 persistent 是一项访存优化：当一个 global buffer 会被反复访问时，提示硬件把它尽量钉在 L2 cache，避免每次都回读显存。它对带宽受限的 kernel（如反复读同一块权重）收益明显。

TileLang 把它做成一条贯穿 DSL → 公共 pass → 后端 pass 的链路：

| 阶段 | 位置 | 产出 |
| --- | --- | --- |
| ① DSL 注解 | `T.annotate_l2_hit_ratio({buf: ratio})` | SBlock 上的 `l2_hit_ratio_map` 注解 |
| ② 收集与属性化 | `LowerL2Persistent`（cuda pass，MACA 复用） | PrimFunc 上的 `l2_persistent_map` 属性 |
| ③ 发射 runtime | `LowerMACAIntrin`（MACA）/ cuda codegen（CUDA） | 函数体里的 set/reset 访问策略调用 |

注意第 ② 步用的是 **cuda 命名空间**的 `LowerL2Persistent`，但它对 MACA 同样有效——因为它只做 target 中立的「收集注解、算字节数、写属性」工作，不涉及具体 runtime 调用。**target 差异被推迟到第 ③ 步**：CUDA 在其 codegen 里发 `cudaStreamSetAttribute`，MACA 在 `LowerMACAIntrin` 里发 `__tvm_maca_stream_*`。这是「公共逻辑下沉、后端差异延后」的典型设计。

#### 4.3.2 核心流程

第 ② 步 `LowerL2Persistent` 的算法：

```
遍历 PrimFunc 体内所有 SBlock：
  若 SBlock 带 l2_hit_ratio_map 注解：
    对每个 (buffer_var, hit_ratio)：
      查 buffer_var 对应的 Buffer
      hit_ratio_map_[buffer] = hit_ratio
    抹除该 SBlock 的 l2_hit_ratio_map 注解（已消费）

函数级收尾：
  对 hit_ratio_map_ 中每个 (buffer, hit_ratio)：
    size_in_bytes = elem_size × Π(各维 shape)
    l2_persistent_map[buffer.name] = [hit_ratio, size_in_bytes]
  若 map 非空：把它作为 PrimFunc 属性挂上
```

字节数计算用了一个朴素的连乘：

\[ \text{size\_in\_bytes} = \text{elem\_bytes}(dtype) \times \prod_{d \in shape} d \]

#### 4.3.3 源码精读

DSL 入口在注解模块里，要求目标 buffer 必须是 global：

[language/annotations.py:53-59](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/annotations.py#L53-L59) `annotate_l2_hit_ratio` 把字典规整成 `{buffer.data -> FloatImm}` 并作为 SBlock 属性 `l2_hit_ratio_map` 挂上；它还断言 `buffer.scope() == "global"`——因为 L2 cache 只对 global 显存有意义，shared/fragment 无此概念。

`LowerL2Persistent` 在遍历时收集这些注解：

[lower_l2_persistent_annotation.cc:71-78](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/transform/lower_l2_persistent_annotation.cc#L71-L78) 这里把 SBlock 上的 `l2_hit_ratio_map`（key 是 buffer 的 data Var）反查回真正的 `Buffer` 对象，记入 `hit_ratio_map_`，并在 L82 抹除已被消费的注解——这样后续 pass 看到的是干净 IR。

收尾时计算字节数并写 PrimFunc 属性：

[lower_l2_persistent_annotation.cc:39-55](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/transform/lower_l2_persistent_annotation.cc#L39-L55) 这段对每个 buffer 连乘 `dtype.bytes() × 各维 shape` 得到字节数，组装 `[hit_ratio, size_in_bytes]` 数组，以 **buffer 名字**（不是 Var）为 key 写入 `l2_persistent_map` 属性。用「名字」做 key 是因为 `LowerMACAIntrin` 需要在另一趟遍历里按名字找回 buffer（见 4.2 的 `FindBufferByName`）。map 为空则不写属性——这正是 4.2 练习 1 提到的守卫来源。

第 ③ 步（MACA 侧）已在 4.2 详述。三段串起来即：注解（DSL）→ 属性（公共 pass）→ runtime 调用（MACA 专属 pass）。

#### 4.3.4 代码实践

1. **实践目标**：跟踪一个带 L2 persistent 注解的 MACA kernel，看 `l2_persistent_map` 属性如何从注解诞生并被消费。
2. **操作步骤**：阅读测试 [test_tilelang_issue_1810.py:17-28](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/python/issue/test_tilelang_issue_1810.py#L17-L28)，它定义了一个最小 kernel：仅 `T.annotate_l2_hit_ratio({A: 0.9})` 加一个空操作，编译后断言 host 源码含 set/reset 两个 packed 符号。
3. **需要观察的现象**：即便 kernel 没有任何实质计算，只要带注解，生成的源码就会包含 `__tvm_maca_stream_set_access_policy_window_packed`。
4. **预期结果**：从测试断言可反推——注解 → `LowerL2Persistent` 写属性 → `LowerMACAIntrin` 发 runtime 调用，整条链路对最小 kernel 也成立。
5. **待本地验证**：在 MetaX 设备上，可对比加/不加 `T.annotate_l2_hit_ratio` 时一个访存密集 kernel 的延迟差异（L2 命中率提升应带来带宽收益）；无设备时可仅做源码阅读。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `LowerL2Persistent` 放在 cuda 命名空间，却出现在 MACA 流水线里（pipeline.py L57）？

> **答案**：因为它只做 target 中立的「收集注解、算字节数、写 `l2_persistent_map` 属性」，不涉及任何具体后端的 runtime 调用。target 差异被推迟到后续：CUDA 在 codegen 里发 cuda runtime 调用，MACA 在 `LowerMACAIntrin` 里发 maca runtime 调用。所以同一个收集 pass 可被两个后端复用。

**练习 2**：`l2_persistent_map` 为什么用 buffer 的**名字**做 key，而不是直接用 buffer 对象/Var？

> **答案**：因为属性是挂在 PrimFunc 上的纯数据（`Map<String, Array<PrimExpr>>`），跨 pass 边界传递时 buffer 对象引用不一定保持稳定；而 `LowerMACAIntrin` 在另一趟遍历里需要重新按 key 找回 buffer，它用 `FindBufferByName`（L78-85）遍历 `buffer_map` 按 name 匹配。用名字做 key 解耦了「写属性」与「读属性」两趟遍历。

### 4.4 2:4 稀疏 GEMM（gemm_sp）：指令选择与 warp 划分

#### 4.4.1 概念说明

2:4 结构化稀疏是 MetaX（及 NVIDIA Ampere+）张量核支持的硬件特性：沿 K 维每连续 4 个元素中恰好 2 个非零，硬件可只用一半算力完成矩阵乘。其代价是必须存储：

- **压缩矩阵 A_sparse**：只存非零，列数为 `K // 2`。
- **元数据 E**：编码「每 4 个里哪 2 个非零」，列数为 `K // e_factor`（`e_factor` 取决于 dtype 与硬件，见 `get_e_factor`）。

TileLang 暴露的 DSL 是 `T.gemm_sp(A_sparse, E, B, C, ...)`，其下译与普通 `T.gemm`（见 u4-l2）同构：C++ 侧做**指令选择**，Python 侧做**实现**（infer_layout + lower）。MACA 上的指令键恒为 `maca.mma.sp`，对应 `__builtin_mxc_mma_*` 的稀疏变体。

#### 4.4.2 核心流程

稀疏 GEMM 的下译分派与普通 GEMM 一致（两级分派）：

```
T.gemm_sp(A, E, B, C, policy, ...)
   ↓ DSL 反序列化
C++ GemmSPNode（TileOperatorNode 子类）
   ↓ ResolveGemmSPImpl(target).select_inst   # 第一级：C++ 选指令键
       maca target → 恒返回 "maca.mma.sp"
   ↓ resolve_gemm_sp_impl(键)                 # 第二级：键 → Python 实现类
       "maca.mma.sp" → GemmSPMMA
   ↓
GemmSPMMA.infer_layout  # 布局（驱动 LayoutInference）
GemmSPMMA.lower         # 指令（驱动 LowerTileOp）
```

warp 划分用 MACA 的 `warp_size=64`：`num_warps = block_size / 64`，稀疏 mma 原子输出瓦片为 16×16（`kMPerWarp=16`，`k_n_per_warp=16`）。

#### 4.4.3 源码精读

C++ 侧的指令选择极其简单——MACA 上不区分 dtype/形状，恒返回同一个键：

[maca/op/gemm_sp.cc:103-106](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm_sp.cc#L103-L106) `SelectInst` 无视所有参数直接返回 `"maca.mma.sp"`（常量定义在 L28）。这与 CUDA 稀疏 GEMM 会按架构选 `mma.sp`/`wgmma.sp`/`tcgen05.sp` 形成对比——MACA 只有一种稀疏 mma 路径。

warp 划分核心是用 MACA 的 warp_size 算 warp 数：

[maca/op/gemm_sp.cc:108-114](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm_sp.cc#L108-L114) `num_warps = block_size / TargetMacaGetWarpSize(target)`，由于 MACA `warp_size=64`，同样的 block_size 下 MACA 的 warp 数只有 CUDA 的一半；`k_n_per_warp=16` 与普通 GEMM（u4-l2）一致。`ComputeDefaultWarpPartition`（L30-98）按 policy（FullRow/FullCol/Square）把 warp 切到 M/N 两维，Square 策略还会按 `M/N` 理想比例搜最均衡划分。

最后把这套实现注册进全局表，让 `resolve_gemm_sp_impl` 能按 target 找到：

[maca/op/gemm_sp.cc:136-147](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm_sp.cc#L136-L147) `RegisterMacaGemmSP` 用 `MatchMacaGemmSPTarget`（L132-134：`TargetIsMaca || TargetIsCuTeDSL`）作为 target 谓词，把 MACA 的 `SelectInst`/`ComputeWarpPartition`/`ReuseExistingSharedLayout` 注册为 `"maca.GemmSP"`。文件末尾的 `const bool maca_gemm_registered = RegisterMacaGemmSP();`（L147）是典型的「静态初始化即注册」手法——库加载时自动执行。

Python 侧实现类 `GemmSPMMA` 的结构与普通 `GemmMMA`（u7-l3）平行，同样双方法：

[gemm_sp_mma.py:16-34](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/op/gemm_sp/gemm_sp_mma.py#L16-L34) `infer_layout` 用 `SparseTensorCoreIntrinEmitter` 构造发射器（传入 dtype、转置标志、warp 划分），再按 A/B 的 scope 组合（ss/sr/rs/rr）返回不同布局——shared 端用 `make_swizzled_layout`，fragment 端用发射器的 `make_mma_load_layout`/`make_mma_store_layout`。

[gemm_sp_mma.py:62-81](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/op/gemm_sp/gemm_sp_mma.py#L62-L81) `lower` 同样构造发射器，然后按 scope 组合生成不同的内联 `@T.prim_func`（如 `_gemm_ssr`）：里面用 `ldmatrix_a`/`ldmatrix_b`/`ldmatrix_e` 把 shared 数据搬进 fragment，再调 `mma`/`mma_sp` 发射稀疏张量核指令。注意稀疏路径多了 `E`（元数据）的加载与 `mma_sp(A, E, B, C, ki)` 调用。

端到端示例展示了 DSL 用法：

[example_gemm_sp.py:33-37](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm_sp/example_gemm_sp.py#L33-L37) 流水线里先 `T.copy` 把 `A_sparse`/`E`/`B` 搬进 shared，再调 `T.gemm_sp(A_shared, E_shared, B_shared, C_local, ...)`——稀疏 GEMM 的输入是**两个 shared**（压缩矩阵 + 元数据）加一个 shared（稠密 B），累加器是 fragment。注意 L18-19 里 A 的形状是 `(M, K // 2)`、E 是 `(M, K // e_factor)`，正是 2:4 压缩的结果。

#### 4.4.4 代码实践

1. **实践目标**：理解 2:4 稀疏 GEMM 的输入压缩与 `T.gemm_sp` 的接口。
2. **操作步骤**：阅读 [example_gemm_sp.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm_sp/example_gemm_sp.py)。重点看三处：① L18-22 的张量形状（A 为 `K//2`、E 为 `K//e_factor`）；② L33-37 的 `T.Pipelined` + `T.gemm_sp`；③ L64 的 `compress(a, meta_dtype=...)` 把稠密 `a` 压成 `(a_sparse, e)`。
3. **需要观察的现象**：压缩前后形状变化——稠密 `a: (M, K)` → `a_sparse: (M, K//2)`、`e: (M, K//e_factor)`；`kernel(a_sparse, e, b)` 的输出与稠密参考 `a @ b` 数值一致（L67-70 的 `torch.testing.assert_close`）。
4. **预期结果**：稀疏 TFLOPS 约为稠密参考的接近 2 倍（因为只算一半非零），印证 2:4 结构化稀疏的硬件加速。
5. **待本地验证**：本例默认 `device="cuda"` 并用 `do_bench`，需 MetaX 设备才能跑通；无设备时可只做源码阅读，并在 [docs/deeplearning_operators/matmul_sparse.md](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/deeplearning_operators/matmul_sparse.md) 查阅稀疏文档。

#### 4.4.5 小练习与答案

**练习 1**：MACA 的 `GemmSP::SelectInst` 恒返回 `"maca.mma.sp"`，而 CUDA 的稀疏 GEMM 会按架构分派不同指令键。为什么 MACA 可以这么做？

> **答案**：MetaX 的张量核稀疏路径只有一种（mfma 的稀疏变体，发射成 `__builtin_mxc_mma_*` 的 sp 形式），不像 NVIDIA 跨 Volta/Ampere/Hopper/Blackwell 多代有 MMA/WGMMA/TCGEN05 多套指令。因此 MACA 无需按架构细分，一个键足够；dtype/形状的差异由 Python 侧 `SparseTensorCoreIntrinEmitter` 在发射时处理。

**练习 2**：稀疏 GEMM 的 warp 划分里，`num_warps = block_size / TargetMacaGetWarpSize(target)`。若 `block_size=128`，MACA 与 CUDA 各得到多少 warp？

> **答案**：MACA `warp_size=64`，故 `num_warps = 128/64 = 2`；CUDA `warp_size=32`，故 `num_warps = 128/32 = 4`。同样的 block_size，MACA 的 warp 数是 CUDA 的一半——这正是 u7-l1 强调的 `warp_size=64` 牵动后续分派的又一处体现。

## 5. 综合实践

把本讲四个模块串起来，完成一次「MACA 流水线 + L2 persistent」的端到端源码追踪：

**任务**：写一个最小 MACA kernel（可在无设备环境只看源码），它满足三个条件——① 带 `T.annotate_l2_hit_ratio({A: 0.9})`；② 用 `T.Pipelined` 做软件流水；③ 用 `T.gemm` 做一次矩阵乘。然后回答：

1. 这个 kernel 编译时，引擎如何选中 `MACAPassPipelineBody`？（答：`resolve_pipeline(target)` 用 `target.kind.name == "maca"` 查注册表。）
2. `LowerL2Persistent` 在 IR 上留下了什么？（答：PrimFunc 的 `l2_persistent_map` 属性，SBlock 上的 `l2_hit_ratio_map` 注解被抹除。）
3. `LowerMACAIntrin` 把该属性翻译成了什么语句？（答：函数体开头/结尾的 `__tvm_maca_stream_set_access_policy_window` / `__tvm_maca_stream_reset_access_policy_window` 两条 packed 调用。）
4. 流水线里 `LowerMACAIntrin` 的前后各是什么 pass？为什么必须在 `SplitHostDevice` 之前？（答：前是 `LowerHopperIntrin`、后是 `AnnotateDeviceRegions`；因为它要改写设备函数体本身，拆分之后就插不准位置。）

**验收**：用一句话总结「注解 → 属性 → runtime 调用」三段链路在哪些文件、哪些 pass 之间传递，并指出整条 MACA 流水线里唯一一个 MACA 专属 pass 的名字。

## 6. 本讲小结

- MACA 流水线 = cuda 流水线骨架 − Hopper/Blackwell 专属 pass ＋ `LowerMACAIntrin`；它通过 `register_pipeline(PassPipeline("maca", ...))` 注册，引擎用 `resolve_pipeline(target)` 按 target kind 名分派。
- `LowerMACAIntrin` 是整条 MACA 流水线里**唯一**的 MACA 专属 pass，它**只**处理 L2 persistent 的 stream access policy intrinsic（`__tvm_maca_stream_set/reset_access_policy_window`），**不**碰 mfma 计算指令（后者由 codegen + intrin_rule 处理）。
- L2 persistent 是一条三段式链路：DSL `T.annotate_l2_hit_ratio` → 公共 pass `LowerL2Persistent`（被 MACA 复用）写 `l2_persistent_map` 属性 → MACA 专属 `LowerMACAIntrin` 发射 runtime 调用；target 差异被推迟到最后一步。
- `l2_persistent_map` 以 buffer **名字**为 key、存 `[hit_ratio, size_in_bytes]`，因为属性是跨 pass 的纯数据，读侧需用 `FindBufferByName` 重新匹配。
- 2:4 稀疏 GEMM 的下译与普通 GEMM 同构：C++ `SelectInst` 在 MACA 上恒返回 `"maca.mma.sp"`，Python `GemmSPMMA` 用 `SparseTensorCoreIntrinEmitter` 发射指令；warp 划分用 `warp_size=64`。
- 多个原本 cuda 命名空间的 pass（`LowerL2Persistent`、`LowerHopperIntrin`、`PersistThreadblock` 等）被 MACA 直接复用——它们要么 target 中立，要么自带「非本 target 即 no-op」守卫，体现了 metax 分支最小改动的对齐策略。

## 7. 下一步学习建议

- **多后端横向对比**：继续阅读 u7-l5（MACA vs CUDA vs ROCm 差异对比），把本讲的「流水线骨架复用」放到三后端对照中理解 warp_size、target triple、MMA 命名的系统差异。
- **代码生成深入**：若想看清 mfma 计算指令为何不经过 `LowerMACAIntrin`，可回看 u7-l2（MACA codegen 实现）的 `VisitExpr_(CallNode*)` 与 intrin_rule 通道。
- **性能与持久化**：u8-l2 会讲 `T.use_swizzle` 的 L2 友好栅格化与 persistent kernel（本讲流水线末尾的 `PersistThreadblock`），可与本讲的 L2 persistent 访问策略对照——两者都是「压榨 L2」但机制不同（栅格化改访问顺序，persistent 改硬件缓存策略）。
- **稀疏扩展**：若需新增其他稀疏模式，可仿照 `src/maca/op/gemm_sp.cc` 的「静态注册 + target 谓词」模式，并参考 u9-l2（新增 tile 算子）理解 `TileOperatorNode` 的 `Lower`/`InferLayout` 抽象。
