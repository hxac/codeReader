# C++ 算子实现机制

## 1. 本讲目标

本讲把视角从 Python DSL 前端（u3-l2）切到 C++ 后端，回答一个问题：

> 当你在 Python 里写下 `T.gemm(...)`、`T.copy(...)`、`T.reduce_sum(...)` 时，这些高层 tile 原语到底是由谁、在哪里、用什么机制被翻译成 GPU 上真实的 mma/wgmma/TMA 指令的？

读完本讲你应当能够：

1. 理解 TVM 的 **Op 注册表**机制，并讲清 TileLang 的「双轨制」：`tl.tileop.*`（重型算子）与 `tl.*`（轻量 intrin）的区别。
2. 看懂 `src/op/` 下每个算子文件 `gemm.cc / copy.cc / reduce.cc` 的三段式骨架：**构造反序列化 → InferLayout 推布局 → Lower 出硬件指令**。
3. 说出 `distributed.cc / sync.cc / remote_copy.cc` 这三类「分布式/同步」算子如何落地为 `nvshmem` 调用或 `tl::cp_*` 模板。
4. 画出一条完整的 `T.gemm` 调用链：Python `call_intrin` → TIR Call 节点 → Op 注册表查表 → `TLOpBuilder` 构造 `GemmNode` → 编译 pass 调 `Lower/InferLayout`。

本讲承接 u3-l2（前端把 `T.*` 原语都先变成「待降级的高层 intrin」）与 u3-l3（`LowerTileOp` 把高层 op 降为真实硬件指令），把 u3-l2 里「黑盒」的那段 C++ 降级逻辑彻底打开。

## 2. 前置知识

- **TVM 的 TIR 与 Op**：TVM 把所有可调用对象（函数、intrinsic）抽象成 `Op`，每个 `Op` 有一个全局字符串名（如 `"tl.tileop.gemm"`）和一个属性表（attribute map）。Python 里写 `tir.call_intrin(dtype, Op.get("tl.tileop.gemm"), ...)`，在 TIR 里就生成一个 `Call` 节点，其 `op` 字段指向该 `Op`。
- **属性表（attribute map）**：TVM 允许给每个 `Op` 挂各种「属性」，例如 `TScriptPrinterName`（打印成什么名字）、`TCallEffectKind`（是否有副作用）、`TLOpBuilder`（谁来把这次调用构造成 TileLang 的算子对象）。属性表是「按名字查回调」的注册表。
- **TileOperator 抽象**：TileLang 给所有「需要被降级」的算子定义了一个 C++ 基类 `TileOperatorNode`，每个具体算子（Gemm、Copy、ReduceOp……）继承它，实现三个虚函数：`Lower`（降级成 TIR 语句）、`InferLayout`（推导线程级布局）、`Clone`（深拷贝）。这是本讲最核心的抽象。
- **`call_extern`**：TVM 的一个 builtin，表示「调用一个外部 C/C++ 函数」，第一个参数是函数名字符串。TileLang 的算子 `Lower` 往往把硬件指令包成一个 `tl::xxx<...>` 的模板名字符串，用 `call_extern` 发出，最终由 codegen 当成 C++ 源码打印出来（见 u7-l3）。

如果你对「布局（Layout）」「fragment」「LowerTileOp pass」这些词还陌生，建议先读 u3-l3 与 u4-l1。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/op/operator.{h,cc}` | TileLang 算子的**基础设施**：`TileOperatorNode` 基类、`TLOpBuilder` 属性、`TIR_REGISTER_TL_TILE_OP` 注册宏、`ParseOperator` 查表入口 |
| `src/op/gemm.{h,cc}` | `T.gemm` 的实现：`GemmNode`、反序列化构造、`InferLayout`（C 必须是 fragment）、`Lower`（出 `tl::gemm_ss/sr/rs` 或 `tl::tcgen5mma_gemm`） |
| `src/op/copy.cc` | `T.copy` 的实现：`CopyNode`、按 scope 选搬运路径、`InferLayout` 委托给 `ParallelOp` |
| `src/op/reduce.cc` | `T.reduce_*` 的实现：`ReduceOpNode`、`MakeInitValue/MakeReduce`、跨线程 AllReduce 降级 |
| `src/op/builtin.cc` | **轻量 intrin** 与编译 pass 的开关（pass config option）注册：数学函数、IEEE 运算、rng、mbarrier |
| `src/op/distributed.cc` | **分布式轻量 intrin** 注册：`tl.GetPE`、`tl.BarrierAll`、`tl.PutmemBlock`、`tl.Quiet` 等（直接对接 NVSHMEM） |
| `src/op/sync.cc` | 分布式同步重型算子：`BarrierBlocksOp`（`tl.tileop.barrier_blocks`）、`WaitOp`（`tl.tileop.wait`）的 `Lower` |
| `src/op/remote_copy.cc` | CP-engine 远程拷贝重型算子：`PutOp`（`tl.tileop.put`）→ `tl::cp_warp/cp_block` |
| `src/transform/lower_tile_op.cc` | 编译 pass，遍历 TIR，对每个 tile op 调 `ParseOperator` + `tile_op->Lower(...)` |
| `src/transform/layout_inference.cc` | 编译 pass，对每个 tile op 调 `ParseOperator` + `tile_op->InferLayout(...)` |
| `tilelang/language/gemm_op.py` | Python 侧：`T.gemm_v1/v2` 生成 `tir.call_intrin(Op.get("tl.tileop.gemm"), ...)` |
| `tilelang/language/distributed/common.py` | Python 侧：`put_warp/put_block/get_*` 生成 `tl.tileop.put/get`，`wait_*` 生成 `tl.tileop.wait` |

## 4. 核心概念与源码讲解

### 4.1 Op 注册与 tl.tileop.*/tl.* intrin（双轨制）

#### 4.1.1 概念说明

TileLang 的所有 `T.*` 原语在前端阶段**都只是 TIR 的 `call_intrin`**——这是 u3-l2 已经建立的认知。但「这个 intrin 后面接什么」在 C++ 层面分成了**两条完全不同的轨道**：

- **A 轨：`tl.tileop.*`（重型算子）**。例如 `tl.tileop.gemm`、`tl.tileop.copy`、`tl.tileop.reduce`、`tl.tileop.put`。这类 Op 注册时附带一个 `TLOpBuilder` 属性，能把一次调用**反序列化成一个 `TileOperator` 对象**（如 `GemmNode`）。该对象实现 `Lower`（降级出硬件指令）和 `InferLayout`（推理布局）。换句话说，它们是「有思想」的算子——编译器要为它们专门跑 layout 推理和降级。

- **B 轨：`tl.*`（轻量 intrin）**。例如 `tl.GetPE`、`tl.PutmemBlock`、`tl.__exp`、`tl.Quiet`。这类 Op **只注册名字和副作用属性，不挂 `TLOpBuilder`**。它们不会被任何降级 pass 解释，而是「原样」存活到 codegen 阶段，由 codegen 直接打印成对应的 C/CUDA 函数调用（如 `nvshmem...`、`__expf`）。

可以用一张表对比：

| 维度 | A 轨 `tl.tileop.*` | B 轨 `tl.*` |
| --- | --- | --- |
| 是否构造 `TileOperator` | 是（`TLOpBuilder`） | 否 |
| `InferLayout` | 有 | 无 |
| `Lower` | 有（出硬件指令） | 无（codegen 直出文本） |
| 典型代表 | `gemm`/`copy`/`reduce`/`put` | `GetPE`/`PutmemBlock`/`__exp`/`Quiet` |
| 注册宏 | `TIR_REGISTER_TL_TILE_OP` | `TIR_DEFINE_TL_BUILTIN`（手写 `TVM_REGISTER_OP`） |

理解这个双轨制是看懂 `src/op/` 的钥匙。

#### 4.1.2 核心流程

一个 A 轨算子从被 Python 调用到变成硬件指令，经过这些环节：

1. **Python 发出 intrin**：`tilelang/language/gemm_op.py` 里 `T.gemm` 调 `tir.call_intrin("handle", Op.get("tl.tileop.gemm"), A_arg, B_arg, C_arg, ...)`，生成一个 TIR `Call` 节点。
2. **C++ 注册表建表**：程序启动时，`TIR_REGISTER_TL_TILE_OP(Gemm, gemm)` 宏在全局注册表里登记 `Op("tl.tileop.gemm")`，并挂上 `TLOpBuilder` 属性——一个 lambda，调用它就等于 `Gemm(args, annotations)`。
3. **编译 pass 查表**：`LowerTileOp`（降级）和 `layout_inference`（布局推理）两个 pass 在遍历 TIR 时，对每个 `Call` 调 `ParseOperator(call)`：在注册表里按 `Op` 查 `TLOpBuilder`，查到就调它，**反序列化得到 `TileOperator` 对象**。
4. **pass 调虚函数**：layout 推理 pass 调 `tile_op->InferLayout(...)`，降级 pass 调 `tile_op->Lower(...)`。两者都通过 C++ 多态分发到具体算子（`GemmNode`、`CopyNode`……）。
5. **Lower 出 call_extern**：`Lower` 把硬件指令包成 `tl::gemm_ss<M,N,K,...>` 这样的模板字符串，用 `call_extern` 发出，留给 codegen 当 C++ 源码打印。

B 轨则跳过第 3~5 步：它没有任何 `TileOperator`，直接在 codegen 阶段被打印。

#### 4.1.3 源码精读

**基类与注册宏**——一切算子的根基在 `operator.h`。`TileOperatorNode` 定义了三个纯虚函数，强制每个算子自己实现降级、布局推理和克隆：

[operator.h:75-85](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/operator.h#L75-L85) — 定义 `TileOperatorNode` 基类：`Lower` 与 `InferLayout` 是每个算子必须实现的核心接口，`LowerArgs`/`LayoutInferArgs` 是 pass 传给算子的「上下文包」（含 target、线程范围、布局表、workspace 回调等）。

[operator.h:98-112](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/operator.h#L98-L112) — 定义 `OpBuilderFunc` 类型与 `TIR_REGISTER_TL_TILE_OP(Entry, OpName)` 宏。这个宏做了两件事：(1) 让 `Entry::Get()` 返回 `Op::Get("tl.tileop.OpName")`；(2) 用 `TVM_REGISTER_OP` 注册该 Op，并挂上 `TLOpBuilder` 属性——一个 lambda，调用时执行 `Entry(args, annotations)`，即调用算子的构造函数把参数反序列化成对象。注意名字前缀写死成 `tl.tileop.`，这正是「A 轨」的命名来源。

**查表入口**——`ParseOperator` 就是「按 Op 查 `TLOpBuilder` 并调用」的函数：

[operator.cc:30-39](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/operator.cc#L30-L39) — `ParseOperator(Call)`：取 `Op` 的 `TLOpBuilder` 属性表，若该 Op 注册了 builder 就调 `op_map[op](call->args, call->annotations)` 得到 `TileOperator`；否则返回空的 `TileOperator`（这就是 B 轨算子的归宿——查不到 builder，返回空对象，pass 不会对它做任何降级）。

**两个调用方**——`LowerTileOp` 在 `VisitStmt_(EvaluateNode)` 里对每个 `Evaluate(Call)` 调 `ParseOperator` 再 `Lower`：

[lower_tile_op.cc:608-650](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_tile_op.cc#L608-L650) — 对每个 `Evaluate` 节点先 `ParseOperator` 得到 `tile_op`，若 `defined()` 则构造 `LowerArgs`（含 target、thread_bounds、workspace 回调、layout_map、buffer_remap）并调 `tile_op->Lower(...)`，返回降级后的语句。

[layout_inference.cc:488](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/layout_inference.cc#L488) — 布局推理 pass 同样用 `ParseOperator(GetRef<Call>(op))` 拿到算子对象，随后在算法主循环里调 `tile_op->InferLayout(LayoutInferArgs{...}, level)`（`level` 即 u4-l1 提到的 `InferLevel`：`kStrict/kCommon/kFree`）。

**B 轨的注册样板**——以分布式 intrin 为例，`distributed.cc` 用一个本地宏 `TIR_DEFINE_TL_BUILTIN` 注册 `tl.*` Op，**只挂 `TScriptPrinterName` 和副作用属性，不挂 `TLOpBuilder`**：

[distributed.cc:19-25](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/distributed.cc#L19-L25) — B 轨的注册宏：`TVM_REGISTER_OP("tl." #OpName)` 只设打印名，没有任何 builder。

[distributed.cc:28-51](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/distributed.cc#L28-L51) — 注册 `tl.GetPE`、`tl.GetPENum`、`tl.BarrierAll`、`tl.SyncAll` 等，全部标成 `CallEffectKind::kOpaque`（有副作用）。它们没有 `Lower`，全靠 codegen 直接打印成 `nvshmem_*` / `nvshmemx_*` 调用（详见 u6-l2）。

对比 A 轨 `BarrierBlocksOp`（在 sync.cc 末尾）：

[sync.cc:167-175](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/sync.cc#L167-L175) — `TIR_REGISTER_TL_TILE_OP(BarrierBlocksOp, barrier_blocks)` 走的是 A 轨注册，名字是 `tl.tileop.barrier_blocks`，带 `TLOpBuilder`，因此它有 `Lower`（下文 4.3 详述）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`tl.tileop.*` 有 builder、`tl.*` 没有 builder」这一双轨判断。

**操作步骤**（源码阅读型）：

1. 打开 `src/op/operator.h`，看清 `TIR_REGISTER_TL_TILE_OP` 宏把名字前缀写死成 `tl.tileop.`，并把一个 lambda 挂到 `TLOpBuilder` 属性上。
2. 打开 `src/op/distributed.cc`，确认 `TIR_DEFINE_TL_BUILTIN` 只调 `TVM_REGISTER_OP("tl." #OpName)` 而**不设** `TLOpBuilder`。
3. 在仓库根目录执行下面的命令，统计两类 Op 的数量（注意 `Op::Get("...")` 的字符串里能区分前缀）。

```bash
# 统计 A 轨（带 TLOpBuilder 的 tileop）
grep -rn "TIR_REGISTER_TL_TILE_OP(" src/op/
# 统计 B 轨（tl.* 轻量 intrin）
grep -rn "TIR_DEFINE_TL_BUILTIN(" src/op/
```

**需要观察的现象**：A 轨出现在 `gemm.cc / copy.cc / reduce.cc / remote_copy.cc / sync.cc / fill.cc / atomic_add.cc` 等「需要降级」的文件；B 轨出现在 `distributed.cc / builtin.cc / sync.cc / math.cc / logical.cc` 等「直出 codegen」的文件。

**预期结果**：你会得到约 17 个 A 轨算子（gemm、copy、reduce、cumsum、fill、put、get、st、ld、barrier_blocks、wait、atomicadd、region、finalize_reducer、c2d_im2col、gemm_sp、gemm_py……）和一大批 B 轨 intrin。**待本地验证**：具体计数以你本地 `grep` 输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果有人想新增一个「需要 layout 推理和降级」的算子 `T.foo`，应该用 `TIR_REGISTER_TL_TILE_OP` 还是 `TIR_DEFINE_TL_BUILTIN`？它的 intrin 名字会是什么？

**答案**：用 `TIR_REGISTER_TL_TILE_OP(Foo, foo)`，名字自动是 `tl.tileop.foo`。必须让 `FooNode` 继承 `TileOperatorNode` 并实现 `Lower` 与 `InferLayout`，否则 `LowerTileOp`/`layout_inference` 调虚函数会编译失败。

**练习 2**：`ParseOperator` 对一个 B 轨 Op（如 `tl.GetPE`）返回什么？为什么 pass 不会对它报错？

**答案**：返回默认构造的空 `TileOperator`（`defined()` 为 false）。因为 `ParseOperator` 查 `TLOpBuilder` 属性表时 `count(op)` 为假，走 `return TileOperator();` 分支，调用方据此跳过降级，让该 intrin 原样留给 codegen。

---

### 4.2 compute/copy/gemm 算子实现

#### 4.2.1 概念说明

A 轨算子虽然各有不同，但骨架完全一致，可抽象成「三段式」：

1. **构造反序列化**：`Op(Array<PrimExpr> args, Map<String,ObjectRef> annotations)`。Python 侧 `call_intrin` 把所有实参按固定顺序塞进 `args`；C++ 构造函数按这个顺序逐个取出来、做类型断言（`.as<IntImm>()` 等），填进 `XxxNode` 的成员字段。**这相当于一个手写的、位置固定的二进制反序列化协议——Python 和 C++ 必须严格对齐参数顺序。**
2. **InferLayout**：编译期推理，告诉 layout 推理 pass「我要求 A/B/C 各自是什么线程级布局」。它的返回是一个 `Map<Buffer, Layout>`，且通过 `InferLevel` 表达约束强度（`kStrict` 最强，必须满足）。
3. **Lower**：读 layout 注解、重算下标，把高层 op 变成具体的硬件指令调用（`tl::gemm_ss<...>`、TMA load 等）。

本模块聚焦最重的两个计算算子：`gemm` 与 `copy`（reduce 放 4.3）。它们是「真正决定性能」的算子，也是 layout 推理最复杂的地方。

#### 4.2.2 核心流程

以 `T.gemm` 为例的降级流程（粗体是 C++ 代码点）：

```
Python T.gemm(A,B,C,...)
  → call_intrin(Op("tl.tileop.gemm"), A,B,C, transA,transB, M,N,K, policy,
                 clear_accum, stride_a,stride_b, offset_a,offset_b, k_pack,
                 wg_wait, mbar, Cx,Cy)            [19 个位置参数]
  → TIR Call 节点
  → layout_inference pass: ParseOperator → Gemm(args)
        → GemmNode::InferLayout  (C 必须是 fragment，按架构选 mma/wgmma/tcgen5mma 布局)
  → LowerTileOp pass: ParseOperator → Gemm(args)
        → GemmNode::Lower
              → 选 op_name: "tl::gemm_ss" 或 "tl::gemm_sr"/"tl::gemm_rs"
                            或 Hopper 的 wgmma / sm100 的 "tl::tcgen5mma_gemm_*"
              → 拼模板字符串 ss << op_name << "<M,N,K, warp_m,warp_n, transA,transB, ...>"
              → Call(tl::tl_gemm(), {StringImm(ss.str()), Aptr, Bptr, Cptr})
  → codegen 把 tl::tl_gemm("tl::gemm_ss<...>", ...) 打印成 CUTLASS 模板调用
```

关键设计：**Lower 不直接写死指令，而是生成一个「模板名字字符串」交给 `tl::tl_gemm`**。最终由 codegen（u7-l3）把这个字符串当成 C++ 模板名实例化。这就是 TileLang「用 TIR 描述、用 C++ 模板落地」的分层。

#### 4.2.3 源码精读

**Gemm 的数据结构**——先看头文件里 `GemmNode` 的字段，它就是构造函数要填的目标：

[gemm.h:118-137](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.h#L118-L137) — `GemmNode` 成员：输入输出 buffer `a_/b_/c_`、对应的 `BufferRegion`、转置标志 `transA_/transB_`、矩阵维度 `m_/n_/k_`、stride/offset、`clearAccum_`、warp 分配策略 `policy_`、可选的 mbar（TCGEN5MMA 用）。还声明了 `Lower`/`InferLayout`/`Clone` 三个 override。

[gemm.h:42](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.h#L42) — `enum class GemmInst { kMMA, kWGMMA, kTCGEN5MMA, kMFMA }`：把「用哪条 tensor core 指令」枚举出来，对应 NVIDIA mma（Ampere/Turing）、Hopper wgmma、Blackwell tcgen05、AMD MFMA。

**Gemm 构造反序列化**——逐个位置取参数，与 Python `call_intrin` 的实参顺序**严格一一对应**：

[gemm.cc:53-95](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L53-L95) — `Gemm::Gemm(args, annotations)`：`args[0..2]` 经 `NormalizeToBufferRegion` 还原成 A/B/C 的 region，`args[3..4]` 是转置 Bool，`args[5..7]` 是 M/N/K，`args[8]` 是 policy，`args[9]` 是 clear_accum，`args[10..13]` 是 stride/offset，`args[14..15]` 是可选的 kPack/wg_wait，`args[16]` 是可选的 mbar，`args[17..18]` 是 C 的坐标。注意末尾用 `args.size() > N` 做可选参数的向后兼容——这是手写反序列化的典型写法。

对照 Python 侧发送端：

[gemm_op.py:104-126](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/gemm_op.py#L104-L126) — `_gemm_impl` 用 `tir.call_intrin("handle", Op.get(op_key), A_arg, B_arg, C_arg, transpose_A, transpose_B, M, N, K, policy, clear_accum, stride_a, stride_b, offset_a, offset_b, k_pack, wg_wait, mbar, C_coords...)` 发出调用。逐参数对照上面 C++ 的 `args[0..18]`，顺序完全吻合。[gemm_op.py:144](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/gemm_op.py#L144) 确认 `gemm_v1` 的 `op_key="tl.tileop.gemm"`。

**Gemm 的 Lower**——核心是选 `op_name` 并拼模板字符串：

[gemm.cc:525-572](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L525-L572) — 先按 A/B 是否在 fragment 选出 `tl::gemm_rs`（A 是寄存器）/`tl::gemm_sr`（B 是寄存器）/`tl::gemm_ss`（都在 shared），并断言 C 必须是 fragment；再用 `stringstream` 把 `<M, N, K, warp_m, warp_n, transA, transB, clear_accum, strideA, strideB, offsetA, offsetB, ...>` 拼进模板参数（Hopper 追加 wgmma 标志与 wg_wait，CDNA 追加 kPack）；最后 `Call(tl::tl_gemm(), {StringImm(ss.str()), Aptr, Bptr, Cptr})` 发出。`tl::tl_gemm` 是一个 B 轨 builtin，它本身不降级，只是 codegen 打印时的「外层壳」。

[gemm.cc:455-499](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L455-L499) — Blackwell（sm100）走另一条路：选 `tl::tcgen5mma_gemm_ts/ss`，要求 C 在 `shared.tmem`、必须传 mbar，并对线程范围做 warp 对齐检查（`tcgen05` 只能由一个 warp 的 leader 发射）。

**Gemm 的 InferLayout**——是 layout 推理里最严格的算子之一：

[gemm.cc:594-654](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L594-L654) — `GemmNode::InferLayout`：先按架构（Volta / Ampere+Turing+SM120 / Hopper / Blackwell）选 `GemmInst`，再用 `policy_->computeWarpPartition` 算 warp 在 M/N 上的切分 `(warp_m, warp_n)`，然后 `ICHECK(IsFragmentBuffer(c_))` 强制 C 是 fragment，并为 C/A/B 调 `makeGemmFragmentC/...` 生成与 mma/wgmma 指令固定输出分布匹配的 fragment 布局。这就是 u4-l1 所说「gemm 累加器必须是 fragment」的编译期根因——指令的输出分布决定了布局，不可自由选择。

**注册**——把 `Gemm` 接入 A 轨：

[gemm.cc:817](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L817) — `TIR_REGISTER_TL_TILE_OP(Gemm, gemm)` 展开后注册 `Op("tl.tileop.gemm")` 并挂 `TLOpBuilder`（lambda 调 `Gemm(args, annotations)`）。

**Copy 算子**——同样的三段式，但 InferLayout 更特殊（它要委托给 `ParallelOp`）：

[copy.cc:106-120](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L106-L120) — `Copy::Copy`：`args[0..1]` 是 src/dst 的 region，额外把整个 `annotations` 存下来（copy 的旋钮如 `disable_tma`、`coalesced_width`、`eviction_policy` 都走注解通道，而不是位置参数）。

[copy.cc:1770-1773](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L1770-L1773) — `TIR_REGISTER_TL_TILE_OP(Copy, copy)` 标 `set_num_inputs(5)` 与 `kOpaque`。注意 copy 的 `Lower` 会按优先级在 TMA → LDSM/STSM → tcgen05 → 普通 SIMT 之间选路（见 u2-l5），这正是它「重型」的体现。

对照 Python 发送端：[copy_op.py:107](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L107) 用 `Op.get("tl.tileop.copy")`。

#### 4.2.4 代码实践

**实践目标**：取出一个真实 `T.gemm` 编译后生成的 CUDA 源码，亲眼看到 4.2.2 流程里拼出的 `tl::gemm_ss<...>` 模板调用。

**操作步骤**（源码阅读 + 运行型）：

1. 复制 `examples/quickstart.py` 里的 matmul kernel（或自写一个 128×128 matmul）。
2. 用 `@tilelang.jit` 编译后，调用 `kernel.get_kernel_source()`（JITKernel 提供的方法）拿到生成的 CUDA 源码字符串。
3. 在源码里搜索 `tl::gemm` 或 `tl_gemm`，定位到那个形如 `tl::gemm_ss<128, 128, 32, 64, 4, 0, 0, true, ...>(...)` 的调用。

```python
import tilelang
import tilelang.language as T

# （省略 kernel 定义，参见 examples/quickstart.py）
kernel = tilelang.compile(matmul_kernel, target="auto")
src = kernel.get_kernel_source()
# 把源码写到本地文件方便阅读
open("matmul_kernel.cu", "w").write(src)
```

**需要观察的现象**：生成的 `.cu` 文件里能看到 `tl::gemm_ss<...>`（Ampere）或 `tl::wgmma`（Hopper）的模板实例化，模板参数与你在 kernel 里设的 `block_M/block_N/block_K/num_stages` 对应；外层包在 `tl_gemm(...)` 调用里。

**预期结果**：源码中存在 `tl_gemm` 与 `tl::gemm_ss<...>`（或 wgmma/tcgen5mma 变体），证明 `GemmNode::Lower` 的字符串拼接确实成了最终 CUDA 代码的一部分。若无 GPU 环境可用 `enable_device_compile=False` 仅取源码（u3-l5）。**待本地验证**：具体模板参数与目标架构相关。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Gemm::Gemm` 的 `args[0..2]` 要先经 `NormalizeToBufferRegion`，而 `args[5..7]`（M/N/K）直接 `.as<IntImm>()`？

**答案**：`args[0..2]` 是 buffer 引用（可能带切片/region，或经 `tl.region` 包装），`NormalizeToBufferRegion` 把它统一还原成 `(buffer, 访问区间)` 的 `BufferRegion`，供后续 shape/stride/offset 分析；M/N/K 是纯整数标量，直接断言取值即可。

**练习 2**：`GemmNode::InferLayout` 里 `ICHECK(IsFragmentBuffer(c_))` 失败时（用户把 C 放在 shared）会怎样？这对应 u2-l3 的哪条结论？

**答案**：编译期直接 `ICHECK` 失败报错终止。这对应 u2-l3 的结论：「`T.gemm` 的 C 必须是 fragment 累加器」——因为 mma/wgmma 指令的输出分布固定，只能写进寄存器 fragment，不能写进 shared。

---

### 4.3 reduce/sync/distributed 算子

#### 4.3.1 概念说明

这一组算子负责「规约」与「同步」，它们横跨 A 轨和 B 轨，正好能展示双轨制在不同场景下的取舍：

- **reduce（`tl.tileop.reduce`，A 轨）**：`T.reduce_sum/max/min/...`。规约要把多线程的中间结果合并，既要做布局推理（fragment 上的 AllReduce），又要降级成实际的 shuffle/AllReduce 指令，所以是 A 轨。
- **sync 重型算子（`tl.tileop.barrier_blocks`、`tl.tileop.wait`，A 轨）**：分布式跨 PE 的栅栏与条件等待。它们需要计算「本地地址相对对称堆基址的偏移」（u6-l2/u6-l3 的远程寻址公式），所以要在 `Lower` 里做地址改写，属 A 轨。
- **sync/distributed 轻量 intrin（`tl.BarrierAll`、`tl.PutmemBlock`、`tl.Quiet`、`tl.GetPE`，B 轨）**：这些是 NVSHMEM 的 1:1 映射，codegen 直接打印成 `nvshmem*` / `nvshmemx*` 调用即可，无需 layout 推理或地址改写，所以是 B 轨。
- **remote_copy（`tl.tileop.put`、`tl.tileop.get`，A 轨）**：CP-engine 的 `put_block/put_warp/get_*`。需要按 scope（warp/block）选不同的 `tl::cp_*` 模板、处理 `unroll_factor` 和远程寻址，所以也是 A 轨。

判断「某算子该走哪条轨」的经验法则：**只要需要在编译期做布局推理、地址改写或根据 scope/arch 选不同实现，就上 A 轨；如果只是「把函数名原样翻译成 C 函数调用」，就用 B 轨。**

#### 4.3.2 核心流程

**reduce 的降级流程**：

```
T.reduce_sum(src, dst, dim, clear)
  → call_intrin(Op("tl.tileop.reduce"), src, dst, "sum", dim, clear)
  → ReduceOp(args)  反序列化: reduce_type 字符串 → ReduceType 枚举
  → InferLayout: 按 src/dst 的 scope 组合(shared/fragment)决定 fragment 布局
  → Lower:
       MakeInitValue()  按 reduce 类型给幺元(sum=0, max=-∞, min=+∞, bitand=全1)
       MakeReduce(a,b)  按类型给合并表达式(sum→a+b, max→Max(a,b), bitand→a&b)
       线程内展开 + 跨线程 AllReduce(shuffle)
```

规约的数学本质是对一个二元运算 \(\oplus\) 求 \(\bigoplus_{i} x_i\)，每种运算需要：(1) 幺元 \(e\)（满足 \(e \oplus x = x\)）；(2) 合并函数。`ReduceOpNode` 把这两件事分别抽成 `MakeInitValue` 与 `MakeReduce`。

**分布式 sync 的降级流程**（以 `barrier_blocks` 为例）：

```
T.barrier_blocks(bar)            # Python: tl.tileop.barrier_blocks
  → BarrierBlocksOp(args)
  → Lower:
       offset = 本地 bar 地址 − 本 rank 的对称堆基址        # 对称寻址
       Call(call_extern, {"tl::barrier_blocks", offset, rank, num_ranks})
  → codegen 打印成 tl::barrier_blocks<...>(offset, rank, num_ranks)
```

这里出现了 u6-l2/u6-l5 强调的「远程寻址公式」：device 侧用 `get_remote_base_ptr(peer) + (addr − base[me])` 把本地偏移换算成各 PE 上的对称地址，依赖运行时注入的远程基址表。

#### 4.3.3 源码精读

**reduce 的三件套**——`ReduceOpNode` 的构造与两个辅助函数：

[reduce.cc:31-43](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/reduce.cc#L31-L43) — `ReduceOp::ReduceOp`：`args[0..1]` 是 src/dst region，`args[2]` 是 reduce 类型字符串（如 `"sum"`），`args[3]` 是规约维 `dim`，`args[4]` 是 `clear`。`ReduceType(reduce_type)` 把字符串转成枚举（见 reduce.h）。

[reduce.cc:55-100](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/reduce.cc#L55-L100) — `MakeInitValue()`：按 reduce 类型返回幺元。`sum/abssum/or/xor→0`；`max→`整数负无穷/浮点 `-INFINITY`、`min→`对应正无穷；`bitand→`全 1（整数 `-1`、无符号全 1）。这正是 u2-l3 总结的「sum=0、max=−∞、min=+∞」幺元表。

[reduce.cc:102-120](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/reduce.cc#L102-L120) — `MakeReduce(lhs, rhs)`：返回两个值的合并表达式。`sum→lhs+rhs`、`max→Max`、`min→Min`、`abssum→lhs+Max(rhs,-rhs)`、`absmax→Max(abs(lhs),abs(rhs))`、`bitand→lhs&rhs`。这两个函数把「规约语义」与「具体指令」解耦——Lower 时复用它们即可生成任意类型的 AllReduce。

[reduce.cc:483](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/reduce.cc#L483) — `TIR_REGISTER_TL_TILE_OP(ReduceOp, reduce)` 注册为 `tl.tileop.reduce`。对照 Python：[reduce_op.py:17](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/reduce_op.py#L17) `_REDUCE_OP_KEY = "tl.tileop.reduce"`。

**sync 重型算子的 Lower（地址改写）**——`BarrierBlocksOp` 与 `WaitOp` 是展示「A 轨在做 B 轨做不了的事」的最佳样本：

[sync.cc:57-84](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/sync.cc#L57-L84) — `BarrierBlocksOpNode::Lower`：先决定模板名 `tl::barrier_blocks`（或 `tl::barrier_blocks<false>` 当 `need_fence` 为假），再用 `tl::get_rank()`、`tl::get_num_ranks()`、`tl::get_remote_base_ptr(rank)`、`tl::get_uintptr_t()` 把「本地 bar 地址」换算成「相对本 rank 对称堆基址的偏移」，最后 `call_extern` 发出。这套 `get_rank/get_remote_base_ptr` 正是 u6-l5 由 `kernel.initialize` 注入的远程基址表的 device 侧读取接口。

[sync.cc:126-153](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/sync.cc#L126-L153) — `WaitOpNode::Lower`：按 `relation`（eq/ne/ge/le/gt/lt）拼出 `tl::wait_eq` / `tl::wait_ne` 等模板名；若 `is_distributed()`（peer 不是 -1）就把等待地址改写成远端基址 + 偏移，否则本地等待。`is_distributed()` 判断依据就是 `peer` 是否为 `IntImm(-1)`（见 [sync.cc:121-124](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/sync.cc#L121-L124)）。

**remote_copy 的 CP-engine 算子**——`PutOp`（`tl.tileop.put`）：

[remote_copy.cc:54-84](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/remote_copy.cc#L54-L84) — `PutOp::PutOp`：`args[0..1]` 是 src/dst 的 `address_of(BufferLoad)`（断言必须是 `address_of`），从中解析出 buffer、下标、字节偏移；`args[2]` 是拷贝大小，`args[3]` 是 `dst_pe`（−1 表示本地），`args[4]` 是 `unroll_factor`，`args[5]` 是 scope（`"warp"`/`"block"`），`args[6]` 是 `enable_aggressive_vectorize`。

[remote_copy.cc:91-100](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/remote_copy.cc#L91-L100) — `PutOpNode::Lower`：按 scope 拼 `tl::cp_warp<copy_size, unroll_factor, aggressive>` 或 `tl::cp_block<copy_size>`。这正是 u6-l3 所说「warp 级走 `cp_warp`、块级走 `cp_block`」的代码出处。

[remote_copy.cc:382-396](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/remote_copy.cc#L382-L396) — 注册 `tl.tileop.put`、`tl.tileop.get`、`tl.tileop.st`、`tl.tileop.ld` 四个 A 轨算子。对照 Python：[common.py:48](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/common.py#L48) 用 `Op.get("tl.tileop.put")` 发出 `put_warp`。

**B 轨分布式 intrin 对照**——同样是「put」，NVSHMEM 路线的 `PutmemBlock` 走 B 轨：

[distributed.cc:71-89](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/distributed.cc#L71-L89) — `tl.GetmemBlock`、`tl.GetmemNbiBlock`、`tl.GetmemWarp` 等只注册名字与 `kOpaque`，无 builder、无 Lower，由 codegen 直接打印成 `nvshmem_getmem_block` 等。对照 Python [nvshmem.py:74-86](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L74-L86) 的 `Op.get("tl.GetmemNbiBlock")`。

> **为何有 `tl.tileop.put` 和 `tl.PutmemBlock` 两套「远程搬运」？** 这正是 u6-l1/u6-l3 区分的两条路线：CP-engine 路线（`put_block/put_warp`）用 CUDA 线程自己 load/store 远端对称堆，需要 scope/unroll 等编译期决策，所以是 A 轨 `tl.tileop.put`；NVSHMEM 路线（`putmem_*`）直接调 NVSHMEM C API，无需编译期决策，所以是 B 轨 `tl.Putmem*`。**双轨制不是冗余，而是两条通信路线在算子层的自然投影。**

#### 4.3.4 代码实践

**实践目标**：追踪 `T.reduce_sum` 的「幺元 + 合并函数」机制，验证 `MakeInitValue/MakeReduce` 如何覆盖多种规约类型。

**操作步骤**（源码阅读型）：

1. 打开 `src/op/reduce.cc`，对照 [reduce.cc:55-120](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/reduce.cc#L55-L120) 列一张表，把 `sum/abssum/max/min/absmax/bitand/bitor/bitxor` 八种类型对应的「幺元」「合并表达式」各填一行。
2. 自检：对 `bitand`，验证幺元「全 1」满足 \(e \,\&\, x = x\)；对 `xor`，验证幺元「0」满足 \(e \oplus x = x\)。
3. （进阶）在 `reduce.h` 的 `ReduceType` 构造函数里 [reduce.h:56-78](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/reduce.h#L56-L78)，确认 Python 传来的字符串（`"sum"`/`"max"`…）如何映射到枚举；若传一个非法字符串（如 `"avg"`）会怎样。

**需要观察的现象**：八种规约类型在「幺元」和「合并函数」上各不相同，但 `Lower` 主流程是共享的——只需把 `MakeInitValue/MakeReduce` 当成插件即可。

**预期结果**：得到一张 8 行的表（见下方答案）。`ReduceType("avg")` 会命中 `LOG(FATAL) << "Invalid reduce type"`。

#### 4.3.5 小练习与答案

**练习 1**：填写 reduce 类型的幺元/合并表达式表。

**答案**：

| 类型 | 幺元（init） | 合并表达式 `MakeReduce(a,b)` |
| --- | --- | --- |
| sum | 0 | a + b |
| abssum | 0 | a + Max(b, −b) |
| max | −∞（或整型最小） | Max(a, b) |
| min | +∞（或整型最大） | Min(a, b) |
| absmax | 0 | Max(abs(a), abs(b)) |
| bitand | 全 1 | a & b |
| bitor | 0 | a \| b |
| bitxor | 0 | a ^ b |

**练习 2**：`WaitOp` 的 `is_distributed()` 用 `peer == -1` 判断本地/远程。为什么用 −1 而不是 0？

**答案**：合法的 rank/PE 编号从 0 开始，0 是真实的第 0 号 PE，不能用 0 当「本地」哨兵；−1 不是合法 rank，可安全当作「未指定 peer → 本地等待」的标记。这与 u6-l3 的 `dst_pe/src_pe == IntImm(-1)` 约定一致。

**练习 3**：如果要新增一个 `T.putmem_signal`（NVSHMEM 的 `putmem_signal`），应该走 A 轨还是 B 轨？为什么？

**答案**：走 B 轨（在 `distributed.cc` 用 `TIR_DEFINE_TL_BUILTIN` 注册 `tl.PutmemSignal`）。因为它只是 NVSHMEM C API 的 1:1 映射，不需要编译期 layout 推理或 scope/arch 决策，codegen 直接打印即可。事实上 `nvshmem.py` 里已经能看到 `tl.PutmemSignal` 的调用。

## 5. 综合实践

把本讲三个模块串起来，完成 **「`T.gemm` 全链路追踪 + intrin 对应关系图」**，这是本讲的practice_task：

**任务**：画出从 Python `T.gemm(A, B, C)` 到生成 CUDA 源码的完整调用链，并标注每一步对应的源码文件与行号，最后给出一张「Python intrin 名 → C++ Op 类 → 注册宏 → 选用的轨」对应表。

**步骤**：

1. **Python 发出**：在 [gemm_op.py:104-126](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/gemm_op.py#L104-L126) 确认 `T.gemm` 发出 `call_intrin(Op("tl.tileop.gemm"), ...)`，记录 19 个实参的顺序。
2. **注册表建表**：在 [gemm.cc:817](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L817) 确认 `TIR_REGISTER_TL_TILE_OP(Gemm, gemm)` 把 `Gemm(args)` 挂到 `tl.tileop.gemm` 的 `TLOpBuilder`。
3. **pass 查表 + 构造**：在 [operator.cc:30-39](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/operator.cc#L30-L39) 与 [lower_tile_op.cc:608-650](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_tile_op.cc#L608-L650) 确认 `ParseOperator` → `tile_op->Lower`。
4. **构造反序列化**：在 [gemm.cc:53-95](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L53-L95) 把 `args[0..18]` 与 Python 的 19 个实参逐一对齐。
5. **InferLayout**：在 [gemm.cc:594-654](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L594-L654) 确认 C 必须是 fragment、按架构选 mma/wgmma 布局。
6. **Lower 出指令**：在 [gemm.cc:525-572](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L525-L572) 确认拼出 `tl::gemm_ss<...>` 字符串并经 `tl::tl_gemm` 发出。
7. **取源码验证**：用 `kernel.get_kernel_source()` 取出 `.cu`，搜索 `tl::gemm_ss`（4.2.4 步骤）。

**产出一张对应关系图**（示例答案）：

| Python 入口 | intrin 名 | C++ 类 | 注册宏 | 轨 | 关键 Lower 产物 |
| --- | --- | --- | --- | --- | --- |
| `T.gemm` | `tl.tileop.gemm` | `GemmNode` | `TIR_REGISTER_TL_TILE_OP` | A | `tl::gemm_ss/sr/rs`、`tl::tcgen5mma_gemm_*` |
| `T.copy` | `tl.tileop.copy` | `CopyNode` | `TIR_REGISTER_TL_TILE_OP` | A | TMA / LDSM / SIMT 拷贝 |
| `T.reduce_sum` | `tl.tileop.reduce` | `ReduceOpNode` | `TIR_REGISTER_TL_TILE_OP` | A | AllReduce（shuffle） |
| `T.barrier_blocks` | `tl.tileop.barrier_blocks` | `BarrierBlocksOpNode` | `TIR_REGISTER_TL_TILE_OP` | A | `tl::barrier_blocks` |
| `T.wait_eq` | `tl.tileop.wait` | `WaitOpNode` | `TIR_REGISTER_TL_TILE_OP` | A | `tl::wait_eq/ne/...` |
| `T.put_block` | `tl.tileop.put` | `PutOpNode` | `TIR_REGISTER_TL_TILE_OP` | A | `tl::cp_block`、`tl::cp_warp` |
| `T.putmem_block` | `tl.PutmemBlock` | （无） | `TIR_DEFINE_TL_BUILTIN` | B | `nvshmem_putmem_block` |
| `T.get_pe` | `tl.GetPE` | （无） | `TIR_DEFINE_TL_BUILTIN` | B | `nvshmem_my_pe` |

## 6. 本讲小结

- TileLang 算子在 C++ 层是**双轨制**：A 轨 `tl.tileop.*` 注册 `TLOpBuilder`、构造 `TileOperator`、实现 `Lower`+`InferLayout`；B 轨 `tl.*` 只注册名字，原样留给 codegen 打印。
- 一切 A 轨算子共享**三段式骨架**：构造反序列化（`args[i]` 与 Python `call_intrin` 实参顺序严格对齐）→ `InferLayout`（声明布局约束）→ `Lower`（出硬件指令）。
- `ParseOperator` 是双轨的「分叉点」：查到 `TLOpBuilder` 就构造算子对象，查不到就返回空对象、跳过降级。`LowerTileOp` 与 `layout_inference` 两个 pass 都靠它分发。
- `T.gemm` 的 `Lower` 不直接写指令，而是拼一个 `tl::gemm_ss<M,N,K,...>` 模板字符串交给 `tl::tl_gemm`，由 codegen 实例化——这是「TIR 描述、C++ 模板落地」的分层。
- `GemmNode::InferLayout` 里 `ICHECK(IsFragmentBuffer(c_))` 是「gemm 累加器必须是 fragment」的编译期根因；`reduce` 用 `MakeInitValue/MakeReduce` 把规约语义与指令解耦。
- 「远程搬运」分两套：CP-engine 的 `tl.tileop.put/get`（A 轨，要选 scope）与 NVSHMEM 的 `tl.Putmem*`（B 轨，直出 `nvshmem*`）——双轨制是两条通信路线在算子层的投影。

## 7. 下一步学习建议

- **深入单个算子的指令封装**：本讲只到「`Lower` 拼出 `tl::gemm_ss` 字符串」。这些字符串对应的真实 CUDA 模板在 `src/tl_templates/cuda/`（如 `gemm.h`、`gemm_sm90.h`、`instruction/wgmma.h`），那是 **u7-l2（CUDA 模板与 GEMM 内核族）** 的主题。
- **看 codegen 如何把 `tl::gemm_ss` 打印成源码**：`tl::tl_gemm` 这类 builtin 在 codegen 里如何被翻译，见 **u7-l3（目标后端 codegen 深入）**。
- **layout 推理的算法细节**：本讲只说「`InferLayout` 返回布局约束」，约束如何在连通分量里传播、`InferLevel` 如何影响搜索，见 **u4-l1（Layout 推理机制）**。
- **新增一个自定义算子**：参考 `region.cc`（最简单的 A 轨算子，`InferLayout` 返回空、`Lower` 几乎透传）作为模板，照「三段式 + `TIR_REGISTER_TL_TILE_OP`」仿写，再把它接到 `LowerAndLegalize` 的 pass 链（u3-l3）里验证。
