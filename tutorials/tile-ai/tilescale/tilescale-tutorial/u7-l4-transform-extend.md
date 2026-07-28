# Transform pass 深入与扩展

## 1. 本讲目标

本讲承接 u3-l4（`OptimizeForTarget` 流水线），把镜头从「流水线全景」推进到「单个 pass 的内部实现」，并教你怎么自己写一个 pass。

学完后你应该能够：

- 看懂 `InjectFenceProxy` 用「代理状态机」在 generic / async 之间插栅栏的算法；
- 区分 LetStmt 的「激进内联（`LetInline`）」与 `Simplify` 内部的「带缓冲区保护的内联」两条路径；
- 说清 `tensor_checks`（host 端张量校验）并不是一道独立的 TIR pass，而是由 `MakePackedAPI` + `ArgBinder` 在生成 host stub 时织入的断言；
- 理解 `LowerThreadAllreduce` 如何把跨线程规约降到「warp shuffle + 共享内存」两阶段算法；
- 掌握「写一个 `.cc` → 注册 FFI → 加 Python 包装 → 接入 `phase.py`」的完整新增 pass 流程，并能照着 `ASTPrinter` 写一个纯 Python 的诊断 pass。

## 2. 前置知识

在进入本讲前，先确认你已经理解下面这些概念（前面讲义已建立）：

- **TIR 与 PrimFunc**：TileLang 把 `@T.prim_func` 解析成 TVM 的 TensorIR，编译就是对 `PrimFunc`（装在 `IRModule` 里）做一系列变换（见 u3-l1、u3-l2）。
- **pass（变换工序）**：一道 pass 输入一个 `IRModule`，输出一个新的 `IRModule`。TileLang 的 pass 大多按 `PrimFunc` 粒度工作，由 `tir::transform::CreatePrimFuncPass` 包装（见 u3-l4）。
- **`OptimizeForTarget` 流水线**：编译第三阶段，按「流水与 warp 特化段 → 缓冲区与索引整形段 → codegen 收尾段」三段线性推进，本讲剖析的 `InjectFenceProxy`、`LowerThreadAllreduce`、`MakePackedAPI` 都生活在这条流水线里（见 u3-l4）。
- **warp / wgmma / TMA**：Hopper（SM90+）上的张量核心指令族与异步搬运机制（见 u4-l2、u4-l3）。
- **C++ 算子的双轨注册**：`tl.tileop.*`（A 轨，经 `TIR_REGISTER_TL_TILE_OP` 参与降级）与 `tl.*`（B 轨，仅注册名字），本讲的 pass 是第三类——不生产算子，只搬运/改写 IR（见 u7-l1）。

一句话复习：**pass 就是 `IRModule → IRModule` 的纯函数**，`phase.py` 是把这些纯函数按顺序串起来的导演。本讲要回答的问题是：这些纯函数内部长什么样，以及怎么自己加一个。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/transform/inject_fence_proxy.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc) | `InjectFenceProxy` pass 实现：generic/async 代理状态机 + 自动插 `fence.proxy.async` |
| [src/transform/lower_thread_allreduce.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc) | `LowerThreadAllreduce` pass 实现：把 `tvm_thread_allreduce` 降为两阶段 warp / 共享内存规约 |
| [src/transform/frontend_legalize.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/frontend_legalize.cc) | `LetInline` pass（激进 let 内联）实现 |
| [src/transform/simplify.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/simplify.cc) | `Simplify` pass 内部的「带缓冲区保护的 let 内联」（`CanInlineLetStmt`） |
| [src/transform/make_packed_api.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/make_packed_api.cc) | `MakePackedAPI` pass，生成 host stub，发射 `num_args` 等校验（tensor_checks 的来源之一） |
| [src/transform/arg_binder.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/arg_binder.cc) | `ArgBinder`，被 `MakePackedAPI` 调用，发射非空指针 / dtype / shape / strides / device 等断言 |
| [tilelang/engine/phase.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py) | 流水线编排：决定每个 pass 在哪一段、什么条件下跑 |
| [tilelang/transform/__init__.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/transform/__init__.py) | C++ pass 的 Python 包装层（`_ffi_api.*`） |
| [tilelang/analysis/ast_printer.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/analysis/ast_printer.py) | 纯 Python `prim_func_pass` 诊断 pass 范例，本讲实践的模板 |
| [CMakeLists.txt](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/CMakeLists.txt) | `src/transform/*.cc` 的 glob 编译规则，新增 pass 无需手改构建 |

---

## 4. 核心概念与源码讲解

### 4.1 InjectFenceProxy：generic / async 代理状态机

#### 4.1.1 概念说明

NVIDIA Hopper（SM90+）把访存指令分成两条「代理（proxy）」通路：

- **generic proxy**：普通显存 / 共享内存读写、`ldmatrix`、`stmatrix`、描述符初始化等同步操作。
- **async proxy**：`cp.async`、TMA load/store、`wgmma` 等异步操作。

当一段 **generic** 操作之后紧接着一段 **async** 操作，硬件要求中间有一条 `fence.proxy.async` 指令来保证顺序，否则可能出现竞态或未定义行为。`InjectFenceProxy` 这道 pass 就是在 TIR 层面自动扫描语句序列，在「generic → async」的转换点补上这条栅栏。

为什么不能让人手写？因为 warp 特化、软件流水线等上游 pass 会大量重排语句、复制缓冲、插入 mbarrier，手写既容易漏也容易错。把这件事下沉成一道确定性 pass 是更稳妥的工程选择。

> 注意：`InjectFenceProxy` 只在「有 TMA」的目标上跑，由 `allow_fence_proxy(target)` 守卫——见 4.1.3 的 `phase.py` 引用。

#### 4.1.2 核心流程

pass 的本质是一个**单遍、有状态的 IR 遍历**，状态是一个枚举 `ProxyKind`：

```text
ProxyKind ∈ { Unknown, Generic, Async, Mixed, Neutral }
```

算法骨架（自顶向下）：

1. 为复合节点（`IfThenElse` / `AttrStmt` / `Block` / `For` …）递归求出其「整体代理种类」= 子节点的 `CombineProxy`。
2. 在 `SeqStmt`（语句序列）里，**从左到右**维护一个 `prev_kind`：
   - 若 `NeedsFence(prev_kind, current_kind)` 为真，就在二者之间插入一条 `fence.proxy.async`（其种类记为 `Neutral`），并把插入后的状态更新为 `prev_kind`。
   - 否则直接 `prev_kind = current_kind` 继续前进。
3. 只有 `prev == Generic && curr == Async` 才需要插栅栏（`Neutral`、`Mixed`、`Unknown` 都不触发）。

`CombineProxy` 的合并规则保证「neutral 吞噬对方」「不同种类合并成 mixed」；而 `NeedsFence` 严格只认 `Generic → Async` 这一种边，避免重复插桩。

整条 pass 还顺带做一件副业：把裸 `tma_store` 改写成「store → arrive → wait」三连，使 TMA store 参与到正常的同步里。

#### 4.1.3 源码精读

**状态枚举与合并规则。** 这是整道 pass 的「状态机定义」：

[源码：ProxyKind 枚举](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc#L28-L35) 定义了五种代理状态，其中 `kNeutral` 表示「栅栏类语句，会重置状态」。

[源码：NeedsFence 判定](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc#L60-L68) 是唯一的插桩触发条件——只有 `IsGeneric(prev) && IsAsync(curr)` 才返回 `true`，其余一律不需要。

**指令归类。** 两张白名单决定一条 call 是 async 还是 generic：

[源码：IsAsyncIntrinsic](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc#L76-L103) 列出所有 TileLang / PTX 的异步 intrin（`tma_load`、`ptx_wgmma_ss`、`ptx_cp_async`、`tl_gemm`、`tl_gemm_sp` …）。注意 `tl_gemm`/`tl_gemm_sp` 也在这里——它们最终落到 wgmma，属 async。

[源码：IsKnownGeneric](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc#L106-L113) 列出 `ldmatrix`/`stmatrix`/`initialize_wgmma_descriptor`/`initialize_tcgen05_descriptor` 等 generic 操作。**保守策略**体现在 `EvaluateNode` 的默认分支（[L204-L226](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc#L204-L226)）：识别不出的 extern call 一律当 `Generic` 处理——宁可多插一道栅栏，也不漏。

**核心遍历。** 状态机真正跑起来的地方在 `SeqStmt` 处理器：

[源码：ProxyFenceInjector::VisitStmt_(SeqStmtNode)](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc#L177-L202) 维护 `prev_kind`，对每条子语句先递归 `VisitStmt`，再用 `NeedsFence` 判定是否在它前面插 `MakeFenceStmt()`。最后用 `SetProxyKind` 把整个序列的合并种类贴回节点，供父节点使用——这是让分析能穿越嵌套控制流的关键。

[源码：MakeFenceStmt](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc#L302-L306) 构造 `Evaluate(Call(..., fence_proxy_async(), {}))`，并把它的代理种类标成 `Neutral`（栅栏自身会重置状态）。`fence_proxy_async` 这个 Op 声明在 [op/builtin.h:321](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/builtin.h#L321)。

**TMA store 三连重写。** 这是一道独立的预 pass：

[源码：TMAStoreSyncInjector::VisitStmt_(EvaluateNode)](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc#L146-L160) 在每条 `tma_store` 后面追加 `tma_store_arrive` + `tma_store_wait`，确保 TMA store 不会变成「发射即忘」。

**pass 装配与 FFI 注册。** 两段式是 TileLang 所有 C++ pass 的统一写法：

[源码：InjectFenceProxy() 工厂](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc#L313-L321) 用 `CreatePrimFuncPass` 把一个 `lambda(PrimFunc, IRModule, PassContext) -> PrimFunc` 包成 TVM pass，名字 `tl.InjectFenceProxy`，先跑 `TMAStoreSyncInjector` 再跑 `ProxyFenceInjector`。

[源码：TVM_FFI_STATIC_INIT_BLOCK 注册](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc#L323-L326) 把 `InjectFenceProxy` 暴露为全局 FFI 符号 `tl.transform.InjectFenceProxy`，这正是 Python 侧 `_ffi_api.InjectFenceProxy` 调用的目标。

**流水线接入。** 在 `phase.py` 里，`InjectFenceProxy` 出现在 warp 特化分支的末段，以及普通分支的 `allow_fence_proxy` 守卫下：

[源码：allow_fence_proxy 守卫](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L30-L31) ——只有 `have_tma(target)` 才允许；[普通分支的调用](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L223) 与 [warp 特化分支的调用](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L213)。Python 包装在 [transform/__init__.py:241-249](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/transform/__init__.py#L241-L249)。

#### 4.1.4 代码实践

**目标**：用单元测试验证「generic → async 必插栅栏、async → generic 不重复插」。

**操作步骤**（阅读型实践，源码已写好测试）：

1. 打开 [testing/python/transform/test_tilelang_transform_inject_fence_proxy.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/testing/python/transform/test_tilelang_transform_inject_fence_proxy.py)。
2. 关注 `test_async_to_generic_no_double_fence`（[L63-L93](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/testing/python/transform/test_tilelang_transform_inject_fence_proxy.py#L63-L93)）：它构造「`cp.async`（async）→ 已有 `fence_proxy_async`（neutral）→ `generic_op`（generic）」的序列，断言整段只有 **1** 条栅栏。
3. 对照 `test_lower_fence_proxy`（[L24-L60](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/testing/python/transform/test_tilelang_transform_inject_fence_proxy.py#L24-L60)）：`unroll` 写清零（generic）之后紧跟 `tl_gemm`（async），`after` 版本在二者之间被插入了 `T.fence_proxy_async()`。

**需要观察的现象**：

- `test_lower_fence_proxy`：before/after 结构除了多一条 `T.fence_proxy_async()` 完全一致；
- `test_async_to_generic_no_double_fence`：栅栏计数恒为 1（说明 neutral 正确吞噬了后续 generic，没有在 generic 前再插一道）；
- `test_tma_store_sync_injection`：每个 `tma_store` 后正好 `arrive == 1` 且 `wait == 1`。

**预期结果**：在本机执行 `pytest testing/python/transform/test_tilelang_transform_inject_fence_proxy.py -v` 全绿（若没有 Hopper 硬件，pass 仍可在 IR 层面正确运行，因为它只生成 IR 文本、不真正发射 PTX）。若你无 GPU 或编译环境，则**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果一条 `seq` 是 `[generic, async, async]`，会插几道栅栏？
**答**：1 道。第二个 async 紧跟第一个 async，`NeedsFence(async, async) = false`；只有第一处 `generic → async` 边触发。

**练习 2**：为什么 `EvaluateNode` 对未知 extern call 默认当 `Generic`，而不是 `Async`？
**答**：出于「宁可多插不可漏」的保守原则。漏插会导致硬件层面的竞态/未定义行为，多插只损失一条指令的性能。注释（[L217-L220](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc#L217-L220)）还特别说明 `gemm`/`gemm_sp` 一定是 intrin 而非 extern，已被 `IsAsyncIntrinsic` 覆盖，所以默认 generic 不会误伤它们。

---

### 4.2 LetStmt 内联：激进内联与缓冲区保护

#### 4.2.1 概念说明

`LetStmt` 是 TIR 里的临时变量绑定（`let x = expr in body`）。TileLang 有两条「把 let 内联掉」的路径，初学者很容易混淆：

1. **激进内联 `LetInline`**（独立 pass，[frontend_legalize.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/frontend_legalize.cc)）：无脑把所有 let 绑定替换进 body，等同于「宏展开」。
2. **保守内联（`Simplify` 内部）**（[simplify.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/simplify.cc)）：只有满足 `CanInlineLetStmt`、且该变量**未被任何缓冲区定义引用**时才内联。

为什么要两套？激进内联用于「确定性地、在合法化之前」把表达式拍平（调试或规避后端 bug）；保守内联是 `Simplify` 的日常行为，必须保护一类关键变量——**出现在 Buffer 的 shape/strides/elem_offset/data 里的变量**。这类变量一旦被内联，Buffer 对象的字段并不会被同步更新，后续通过 Buffer 访问的代码会找不到变量定义而崩。

#### 4.2.2 核心流程

**`LetInline`（激进）** 的流程非常直白：

```text
维护 let_bindings_: VarNode* -> PrimExpr
  VisitStmt(LetStmt v = expr in body):
      let_bindings_[v] = VisitExpr(expr)
      return VisitStmt(body)              # 直接丢弃绑定，进入 body
  VisitExpr(Var x):
      若 x ∈ let_bindings_，返回 let_bindings_[x]（递归展开）
      否则原样返回
```

它还顺手记了 `parallel_for_scope_`，但在本 pass 里只是计数，不做条件分支——它就是「能换就换」。

**`Simplify` 内联（保守）** 的流程多了两道闸门：

```text
1. 预扫描：CollectVarsUsedInBufferDefinition 遍历所有 BufferLoad/BufferStore，
   把出现在 Buffer 定义里的变量收集进 used_in_buffer_def_。
2. 遍历到 LetStmt：
   can_inline = CanInlineLetStmt(op)              # 闸门 1：值要够「纯」
   used_in_buffer_def = used_in_buffer_def_.count(op->var)   # 闸门 2：不在缓冲区定义里
   if (can_inline && !used_in_buffer_def)  return body;      # 内联：丢掉绑定
```

`CanInlineLetStmt` 满足以下任一即返回 true（[simplify.cc:322-333](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/simplify.cc#L322-L333)）：值是常量、值是变量、或值是 int 类型且副作用 ≤ kPure。

#### 4.2.3 源码精读

**激进内联实现。**

[源码：LetInliner 类](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/frontend_legalize.cc#L37-L81) 继承 `arith::IRMutatorWithAnalyzer`。关键两处：`VisitStmt_(LetStmtNode)`（[L69-L72](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/frontend_legalize.cc#L69-L72)）登记绑定后**直接返回 body**（丢弃 let）；`VisitExpr_(VarNode)`（[L61-L67](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/frontend_legalize.cc#L61-L67)）在遇到变量时查表替换。

[源码：LetInline() 工厂 + FFI 注册](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/frontend_legalize.cc#L85-L95) 同样是 `CreatePrimFuncPass` + `TVM_FFI_STATIC_INIT_BLOCK` 两段式，注册名 `tl.transform.LetInline`。Python 包装在 [transform/__init__.py:5](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/transform/__init__.py#L5)（从 `simplify` 模块再导出）。

**保守内联实现。**

[源码：CanInlineLetStmt](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/simplify.cc#L322-L333) 定义「可内联」的判据——注意第三条只接受 `int` dtype，理由（注释 [L327-L329](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/simplify.cc#L327-L329)）：let 表达式不像 let 语句那样会引发「表达式爆炸」，所以整数索引可以尽量内联。

[源码：VisitStmt_(LetStmtNode) 的双重闸门](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/simplify.cc#L386-L397) 是保守策略的落点：`can_inline && !used_in_buffer_def` 才 `return body` 内联，否则保留绑定（或更新值）。`used_in_buffer_def_` 由预扫描填充（见 [L99-L208](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/simplify.cc#L99-L208) 的两个 collector）。

**流水线接入与开关。**

[源码：should_force_let_inline 配置读取](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L60-L63) 读取 `tl.force_let_inline` 配置项；[源码：LowerAndLegalize 中的条件调用](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L154-L156) 只在用户显式开启时才在 `LowerAndLegalize` 开头跑 `LetInline()`。默认关闭——日常只走 `Simplify` 的保守路径。

#### 4.2.4 代码实践

**目标**：对比 `LetInline`（激进）与默认 `Simplify`（保守）对同一段 IR 的处理差异。

**操作步骤**：

1. 打开 [testing/python/transform/test_tilelang_transform_let_inline.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/testing/python/transform/test_tilelang_transform_let_inline.py)。
2. 读 `test_let_binding`（[L14-L31](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/testing/python/transform/test_tilelang_transform_let_inline.py#L14-L31)）：`factor = T.float32(2.0)` 是 float（非 int），但 `LetInline` 仍然把它内联成 `A[i,j] * T.float32(2.0)`——证明激进 pass 不看 dtype。
3. 对比：同一段代码若只走 `Simplify`，因为值是 `float32`（不满足 `CanInlineLetStmt` 的 int 条件），`factor` 会被**保留**。

**需要观察的现象**：

- `LetInline` 后 IR 里 `factor` / `value` 等临时变量全部消失；
- 手动给一个 kernel 开 `tl.force_let_inline: True`，比较 `lower` 前后的 `mod.show()` 差异。

**预期结果**：开启 `LetInline` 后 IR 更「扁平」但可能更长（表达式重复）；关闭时 `Simplify` 只内联纯整数索引。**待本地验证**（取决于具体 kernel 与目标）。

**实践延伸（可选代码）**：

```python
import tilelang as tl
from tilelang import transform
from tilelang.engine.phase import LowerAndLegalize
import tvm, tilelang.language as T
from tilelang.utils.target import determine_target

@T.prim_func
def f(A: T.Tensor((128,), T.float32)):
    for i in range(128):
        with T.block("b"):
            v = T.float32(2.0)          # float，非 int
            A[i] = A[i] * v

target = tvm.target.Target(determine_target("auto"))
# 激进：强制 let inline
with transform.PassContext(config={transform.PassConfigKey.TL_FORCE_LET_INLINE: True}):
    mod_aggressive = LowerAndLegalize(tvm.IRModule({"main": f}), target)
mod_aggressive["main"].show()
```

#### 4.2.5 小练习与答案

**练习 1**：下面这段里，`stride` 会被 `Simplify` 内联吗？为什么？

```python
let stride = M * 16
let buf = Buffer(data, shape=[M,N], strides=[stride, 1])
buf[i,j] = ...
```

**答**：不会。`stride` 满足 `CanInlineLetStmt`（纯 int 表达式），但它出现在 `buf` 的 `strides` 字段里，被预扫描收进 `used_in_buffer_def_`，所以双重闸门 `can_inline && !used_in_buffer_def` 为 false，绑定被保留。

**练习 2**：什么场景下应该打开 `TL_FORCE_LET_INLINE`？
**答**：当你怀疑保守内联导致了下游 pass（如 Layout rewrite / FlattenBuffer）的 bug，或想在多环境下获得确定的、可复现的内联行为时，用它做调试 / 兜底。日常关掉即可。

---

### 4.3 Tensor Checks：host stub 的自动张量校验

#### 4.3.1 概念说明

`tensor_checks`（[文档](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/compiler_internals/tensor_checks.md)）描述的是：当你把 `torch.Tensor` 或任何 DLPack 对象传给一个编译好的 TileLang kernel 时，**自动**发生的入参校验——参数个数、指针类型、dtype、shape、strides、device 等等。

**关键澄清**：它**不是**一道独立的 TIR transform pass，也不会出现在 `phase.py` 的 pass 列表里。这些校验是 **host stub（主机端启动器）生成时织入的断言**，物理上由两道 pass 协作发射：

- `MakePackedAPI`：在 host 函数开头发射 `num_args` 计数断言、args 指针非空等；
- `ArgBinder`（被 `MakePackedAPI` 调用）：对每个张量发射「非空指针 / dtype / ndim / shape / strides / device」等 `AssertStmt`。

之所以这么设计，是为了**ABI 稳定 + 低开销**：校验写在生成的 C 代码里，比在 Python 里逐字段 `getattr` 快，也比 pybind 方案更省。

#### 4.3.2 核心流程

host stub 生成时的校验织入流程：

```text
MakePackedAPI(func):
    1. 发射 num_args 断言：调用方传入的参数个数必须等于形参数。
    2. 对每个参数，判断 FFI 类型是否为指针类型（DLTensor/handle）或合法标量。
    3. 构造 ArgBinder，对 buffer_map 里的每个 buffer：
         a. 静态分析：该 buffer 是否被函数体用到？→ 决定是否可空（nullable）。
         b. 用到 → 发射 AssertStmt(handle != NULL, "... expected to have non-NULL pointer")。
         c. BindDLTensor：发射 ndim / dtype / shape / strides / byte_offset / device 断言。
    4. 最后接上真正的 device kernel launch。
```

可空性（nullability）规则很有意思：若一个输入张量在**静态分析**下不可达（例如只在 `if False` 分支里访问），则允许传 `NULL`，其它字段校验只在 `handle != NULL` 时执行；否则必须非空。

#### 4.3.3 源码精读

**参数个数断言（num_args）。**

[源码：MakePackedAPI 发射 num_args 断言](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/make_packed_api.cc#L284-L289) 用 `MakeAssertEQ(v_num_packed_args, num_args, ...)` 生成「`<name>: num_args should be N`」断言，注释（[L279-L283](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/make_packed_api.cc#L279-L283)）强调它必须在任何 `BindDLTensor` 初始化**之前**发射。

**非空指针断言（nullable）。**

[源码：ArgBinder 发射 non-NULL 断言](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/arg_binder.cc#L359-L378) 是可空性规则的落点：`is_used = used_param_buffers.count(handle)` 决定该 buffer 是否被用到；用到时 `is_null = const_false()` 并发射 `AssertStmt(!is_null_var, "... is expected to have non-NULL pointer")`，未用到则 `is_null = is_null_var`（允许运行时为空）。dtype / shape / strides / device 的同类断言都在该文件后续展开。

**流水线接入。**

[源码：OptimizeForTarget 中的 MakePackedAPI 调用](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L275) 紧跟在 `SplitHostDevice` 之后——因为只有把 host/device 拆开后，host stub 才能被独立生成并织入校验。

> 这些校验**无法关闭**（见文档 FAQ），它们保证 ABI 稳定并在尽量靠近 device 调用的位置 fail fast。

#### 4.3.4 代码实践

**目标**：亲眼看到编译产物里的 host 校验代码。

**操作步骤**：

1. 用任意已编译的 kernel（例如 u1-l3 的 matmul+relu）调用 `print(kernel.get_host_source())`，或在 IR 层面对比 `MakePackedAPI` 前后的 `mod.show()`。
2. 在生成的 host 源码里定位：`num_args should be`、`is expected to have non-NULL pointer`、`ndim is expected to equal` 等字样。
3. 故意触发一个错误（参考 [docs/.../tensor_checks.md 的 Minimal Repros](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/compiler_internals/tensor_checks.md#L236-L251)），例如少传一个参数或传错 dtype。

**需要观察的现象**：

- 正确调用：无任何报错，kernel 正常执行；
- 少传参数：报 `<kernel>: num_args should be 3; expected <num_args>, got 2`；
- dtype 错：报 `<kernel>.A_handle.dtype is expected to be float16, but got incompatible dtype`。

**预期结果**：报错信息与 `arg_binder.cc` / `make_packed_api.cc` 里写的字符串完全一致。**待本地验证**（需要可编译的 GPU 环境；无环境时可只读 host 源码做静态确认）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `tensor_checks` 不做成 Python 侧的 `if not isinstance(x, torch.Tensor): raise ...`？
**答**：两个原因——(1) ABI 稳定：入口基于 TVM FFI + DLPack，统一接受张量与标量；(2) 低开销：把校验编进生成的 C 代码，避免 Python 解释器与属性访问开销，比 pybind 方案更省（见文档 [L4-L9](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/compiler_internals/tensor_checks.md#L4-L9)）。

**练习 2**：下面哪个 `A` 可以传 `None`？

```python
some_cond: bool = False
@T.prim_func
def main(A: T.Tensor((M, K), d)):
    if some_cond:
        A[0] = 1
```

**答**：可以。`some_cond` 在编译期是 `False`，静态分析判定 `A` 不可达，属 nullable；运行时传 `None` 不会触发「non-NULL pointer」断言（文档 [L98-L104](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/compiler_internals/tensor_checks.md#L98-L104)）。若 `some_cond` 改为运行期 `T.bool` 参数，则静态分析无法证明未用，`A` 必须 non-NULL。

---

### 4.4 LowerThreadAllreduce：跨线程规约的两阶段降低

#### 4.4.1 概念说明

`LowerThreadAllreduce` 把 TIR 里的 `tvm_thread_allreduce` intrin（一个「跨线程规约」的占位符）降级成**设备上真正可执行的 IR**。它处理的是 block 内线程间的归约——例如「512 个线程各持有一个值，求和广播回所有人」。

核心难点：GPU 上跨线程规约没有单一指令，必须组合两类原语：

- **warp shuffle**（`__shfl_down_sync`）：warp 内（CUDA 32 线程）无需共享内存即可规约；
- **共享内存 + `__syncthreads`**：跨 warp 时必须落共享内存中转。

`LowerThreadAllreduce` 的精妙之处在于：它会**根据线程规模自动选路**——规约规模 ≤ 一个 warp 时走「纯 shuffle」；规模是 warp 整数倍且不超过 `warp_size²` 时走「两阶段 warp 规约」（先各 warp 内 shuffle，再把 warp 数量级的结果汇总）；否则退回「共享内存 + 树形规约」。

#### 4.4.2 核心流程

pass 主流程（`ThreadAllreduceBuilder::MakeAllreduce`）：

```text
1. AllocateCollector 预扫描：统计动态 / 静态共享内存分配，决定 shared_scope 用 "shared" 还是 "shared.dyn"。
2. 维护 thread_extents_ 栈：遇到 thread_extent AttrStmt 入栈，从而知道规约发生在哪些线程维度上。
3. 遇到 tvm_thread_allreduce call：
   a. 从 reduce_set（参与规约的迭代变量）+ thread_extents 推出：
        - vred：规约维度线程（reduce threads）
        - vpar：并行维度线程（group threads）
        - reduce_extent / group_extent：各自展平后的总规模
        - contiguous_reduce_extent：规约维度连续展宽后的最大连续段
   b. IsWarpReduction(...) 判定能否走 warp 路线。
4. 选路：
   - reduce_extent <= warp_size：
       MakeWarpAllreduce（单 warp shuffle）→ 把 lane 0 结果 broadcast 给所有 lane。
   - reduce_extent > warp_size 且满足多 warp 条件：
       两阶段：先各 warp 内 shuffle → 落 staging 共享内存 → 第一 warp 的前 n_warps 个 lane 再 shuffle → 写广播共享内存。
   - 否则：
       共享内存 + MakeBufAllreduce（树形规约，含边界对齐到 2 的幂、warp 内同步）。
5. 用 load_remap_ / buf_remap_ 把「原本对结果 buffer 的读」改写成「读规约后的共享/局部 buffer」，保证后续 IR 拿到正确值。
```

两阶段算法（注释 [L350-L378](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L350-L378)）举了个例子：512 线程规约 512 元素、warp=32 → 16 个 warp。第一阶段每个 warp 内 shuffle 掉 32 个元素，剩 16 个落共享内存；第二阶段用第一 warp 的前 16 个 lane 再 shuffle 一次。

#### 4.4.3 源码精读

**pass 入口与目标读取。**

[源码：AllocateCollector](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L50-L81) 用 `IsDynamicSharedMemory` / `IsStaticSharedMemory` 区分两类共享内存，决定下游 `shared_scope`（[L93-L95](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L93-L95)）。当动态共享分配数 > 1 时 `is_dynamic = true`，切到 `shared.dyn`。

[源码：LowerThreadAllreduce() 工厂](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L930-L946) 从 `PrimFunc` 读 `kTarget` 属性（`ICHECK` 强制存在），构造 `ThreadAllreduceBuilder`，跑一遍 mutator。FFI 注册在 [L948-L952](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L948-L952)，名字 `tl.transform.LowerThreadAllreduce`。

**选路判定 IsWarpReduction。**

[源码：IsWarpReduction](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L841-L899) 是整道 pass 的「调度器」。关键判据：

- 只在 cuda / rocm / metal 上启用；
- 类型约束：cuda 接受 {u}int/long/longlong/float/double/half/half2（[L860-L870](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L860-L870)）；
- **规约维度必须连续**（`contiguous_reduce_extent == reduce_extent`，[L875-L878](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L875-L878)），否则 shuffle 无从对齐；
- 规模判据：sub-warp（`warp_size % reduce_extent == 0`）、multi-warp（`reduce_extent % warp_size == 0` 且线程总数 ≤ `warp_size²`）等（[L884-L898](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L884-L898)）。

**warp 内规约 MakeWarpAllreduce。**

[源码：MakeWarpAllreduce](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L546-L670) 是 warp shuffle 的核心循环：以 2 的幂为步长 `offset` 从大到小，对每个 buffer 发射 `tvm_warp_shuffle_down`，再用 `combiner(a, b)` 合并、写回。注意它特意**先把 shuffle 结果存进 local buffer 再做 combiner**（注释 [L614-L625](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L614-L625)），避免 `__shfl_sync` 出现在三元表达式里导致 warp 死锁。边界处理 [L655-L660](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L655-L660) 用 `if (reduce_index + offset < reduce_extent)` 防止读到非活跃线程的垃圾值（对 max/prod 至关重要）。

**两阶段编排在 MakeAllreduce。**

[源码：MakeAllreduce 的两阶段注释与调度](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L350-L379) 给出算法直觉；[L381-L494](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L381-L494) 是落点：`reduce_extent <= warp_size_` 走单 warp + broadcast（[L387-L403](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L387-L403)），否则走「warp shuffle → staging 共享内存 → 二次 shuffle → 广播共享内存」五步（[L404-L479](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L404-L479)）。`MakeBufAllreduce`（[L673-L784](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L673-L784)）是共享内存树形规约的兜底路径，含「对齐到 2 的幂 → 跨块同步 → warp 内用 `SyncThread("warp")` 分离 load/store」三段。

**流水线接入（顺序很关键）。**

[源码：OptimizeForTarget 中的调用](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L253-L254) 在 `InferFragment()` 之后、`SplitHostDevice` 之前——`phase.py` 里的 TODO 注释（[L244-L252](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L244-L252)）说明：因为 TileLang 只用一个线程维度，合法化与 Simplify 会丢失变量绑定信息，必须先用 `InferFragment` 补回再 allreduce。Python 包装 [transform/__init__.py:458-460](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/transform/__init__.py#L458-L460)。

#### 4.4.4 代码实践

**目标**：跟踪一道 allreduce 从 intrin 到 warp shuffle 的降低过程。

**操作步骤**（源码阅读型实践）：

1. 在 `lower_thread_allreduce.cc` 里追踪 `MakeAllreduce`（[L209](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L209)）如何从 `call->args` 还原出 `values`（规约值）、`buffers`（结果 buffer）、`reduce_set`（规约线程变量）。
2. 找到 `IsWarpReduction`（[L841](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L841)），假设 `reduce_extent=32, group_extent=1`，确认它会走单 warp 分支。
3. 跟进 `MakeWarpAllreduce`（[L546](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L546)），数一下 32 元素规约会发射几次 `tvm_warp_shuffle_down`（答案：5 次，offset = 16,8,4,2,1）。

**需要观察的现象**：

- 一个 `tvm_thread_allreduce` call 被替换成一段包含 `DeclBuffer` / `Allocate` / `BufferStore` / `tvm_warp_shuffle_down` / `tvm_storage_sync` 的复合语句；
- 规约规模变化时（32 vs 512），生成的 IR 结构会从「纯 shuffle」切到「shuffle + staging 共享内存」。

**预期结果**：你能在降低后的 IR 里清楚指认「warp shuffle 段」「staging 共享内存段」「broadcast 段」。**待本地验证**（需要构造一个含跨线程规约的 kernel；普通 matmul 不会触发此 pass）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `IsWarpReduction` 要求 `contiguous_reduce_extent == reduce_extent`？
**答**：warp shuffle 假设规约维度在硬件线程上是**连续**铺开的（lane k 持有第 k 个元素）。如果规约维度不连续（中间夹着并行维度），lane 与元素的对应关系被打破，shuffle 会读到错误的搭档值。

**练习 2**：`MakeWarpAllreduce` 为什么把 shuffle 结果先存进 local buffer 再做 combiner，而不是直接 `combiner(a, shuffle(b))`？
**答**：为了避免 `__shfl_sync` 出现在 `if_then_else`（三元）表达式里——那样会在发散分支里嵌入 warp 同步调用，可能死锁（注释 [L614-L625](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L614-L625)）。先 materialize 成普通变量再合并更安全。

---

### 4.5 新增并注册一个 transform pass 的步骤

#### 4.5.1 概念说明

把上面四道 pass 的共性抽出来，新增一道 TileLang pass 只需要四步，且**无需手改构建脚本**——因为 `CMakeLists.txt` 用 glob 收集 `src/transform/*.cc`。本节用「写一个打印每个 `PrimFunc` 名字的诊断 pass」作为贯穿示例。

pass 分两类，按你的目的选择：

- **诊断 / 校验类**（不改 IR 或只读）：推荐写成**纯 Python** `prim_func_pass`，放 `tilelang/analysis/`，像 `ASTPrinter`、`NestedLoopChecker` 那样；接入 `PreLowerSemanticCheck`。
- **改写类**（要变换 IR）：写成 **C++** `CreatePrimFuncPass`，放 `src/transform/`，像 `InjectFenceProxy` 那样；接入 `LowerAndLegalize` / `OptimizeForTarget`。

本讲的实践任务是诊断类，所以走纯 Python 路线。

#### 4.5.2 核心流程

新增一道 C++ 改写 pass 的四步：

```text
1. 写 .cc（放 src/transform/）：
     - 实现 pass_func = [](PrimFunc f, IRModule, PassContext) -> PrimFunc { ... }
     - 用 tir::transform::CreatePrimFuncPass(pass_func, opt_level, "tl.YourPass", {}) 包成 Pass
     - TVM_FFI_STATIC_INIT_BLOCK { refl::GlobalDef().def("tl.transform.YourPass", YourPass); }
   → CMake glob 自动编译，无需改 CMakeLists.txt。

2. 加 Python 包装（tilelang/transform/__init__.py）：
     def YourPass():
         return _ffi_api.YourPass()

3. 接入流水线（tilelang/engine/phase.py）：
     在 LowerAndLegalize / OptimizeForTarget 的合适位置插一行 mod = tilelang.transform.YourPass()(mod)，
     必要时用 allow_xxx() 守卫。

4. 加测试（testing/python/transform/test_tilelang_transform_your_pass.py）。
```

新增一道纯 Python 诊断 pass 更简单：跳过步骤 1，直接在 `tilelang/analysis/` 写一个用 `prim_func_pass` 装饰的函数，在 `analysis/__init__.py` 导出，然后在 `PreLowerSemanticCheck` 里调用。

#### 4.5.3 源码精读

**glob 编译规则（为何不用改构建）。**

[源码：CMakeLists.txt 的源文件 glob](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/CMakeLists.txt#L135-L147) 用 `file(GLOB ... src/transform/*.cc src/transform/common/*.cc ...)` 收集源码——你往 `src/transform/` 丢一个 `.cc`，重新编译即被纳入。

**C++ pass 的两段式范式。** 以 `InjectFenceProxy` 为模板：

[源码：CreatePrimFuncPass 包装](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc#L313-L321) + [源码：FFI 注册](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_fence_proxy.cc#L323-L326)。`LowerThreadAllreduce`（[L930-L952](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_thread_allreduce.cc#L930-L952)）和 `LetInline`（[L85-L95](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/frontend_legalize.cc#L85-L95)）是完全同构的写法。

**Python 包装范式。**

[源码：transform/__init__.py 的 InjectFenceProxy 包装](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/transform/__init__.py#L241-L249) 就是一层 `_ffi_api.InjectFenceProxy()` 的薄封装。带参数的 pass（如 `ThreadSync(storage_scope)`、`VectorizeLoop(enable_vectorize)`）照抄即可。

**纯 Python 诊断 pass 范式（实践模板）。**

[源码：ASTPrinter](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/analysis/ast_printer.py#L79-L102) 是本讲实践的最佳模板：核心是 `prim_func_pass(pass_fn, opt_level=0)`，`pass_fn(func, mod, ctx)` 收到每个 `PrimFunc`，做完事 return func 即可。`NestedLoopChecker`（[L61-L119](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/analysis/nested_loop_checker.py#L61-L119)）展示了「诊断 pass 抛 `ValueError` 做语义校验」的写法。

[源码：analysis/__init__.py 导出](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/analysis/__init__.py#L1-L7) 把每个诊断 pass 显式 import 出来。

**接入点。**

[源码：PreLowerSemanticCheck](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L114-L128) 是诊断 pass 的挂载点——它在合法化之前运行，且「只校验、不改模块」（文档字符串 [L120-L122](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L120-L122)）。`ASTPrinter` 就是在这里被 `should_enable_ast_print()` 守卫着调用（[L122-L123](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L122-L123)）。

#### 4.5.4 代码实践

**目标**：写一个最小的「打印每个 `PrimFunc` 名字」的诊断 pass，挂到 `PreLowerSemanticCheck`，并验证它打印出 `main`。

**操作步骤**：

1. 新建 `tilelang/analysis/func_name_printer.py`（这是本实践为学习目的新建的诊断工具，**非项目原有代码**，仅作练习；本讲义不修改源码，请你手动创建）：

```python
# 示例代码：新增的诊断 pass（非项目原有文件）
from tvm.tir import PrimFunc
from tvm.tir.transform import prim_func_pass


def FuncNamePrinter():
    """A diagnostic pass that prints the global_symbol of every PrimFunc."""

    def pass_fn(func: PrimFunc, mod, ctx) -> PrimFunc:
        # global_symbol 是 TVM 给入口函数打的“名字”属性
        name = func.attrs.get("global_symbol", "<anonymous>") if func.attrs else "<no attrs>"
        print(f"[FuncNamePrinter] PrimFunc name = {name}")
        return func  # 诊断 pass：原样返回，不改 IR

    return prim_func_pass(pass_fn, opt_level=0)
```

2. 在 `tilelang/analysis/__init__.py` 末尾加一行导出：

```python
# 示例代码
from .func_name_printer import FuncNamePrinter  # noqa: F401
```

3. 在 `tilelang/engine/phase.py` 的 `PreLowerSemanticCheck` 里挂上（参照 `ASTPrinter` 的位置）：

```python
# 示例代码
def PreLowerSemanticCheck(mod: IRModule) -> None:
    ...
    tilelang.analysis.FuncNamePrinter()(mod)   # 新增：打印每个 PrimFunc 名字
    tilelang.analysis.NestedLoopChecker()(mod)
    tilelang.analysis.FragmentLoopChecker()(mod)
```

4. 编译并运行任意 kernel（如 examples/quickstart.py）。

**需要观察的现象**：编译开始时控制台先打印一行 `[FuncNamePrinter] PrimFunc name = main`，然后才进入正常编译。

**预期结果**：因为 `PreLowerSemanticCheck` 在合法化之前跑、且此时模块里的入口函数已被 `global_symbol="main"` 标注（由前端 / `AnnotateEntryFunc` 设置），所以一定打印出 `main`。**待本地验证**（需要可重新编译 tilelang 的开发环境；若只读不改，可对照 `ASTPrinter` 的 [pass_fn](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/analysis/ast_printer.py#L93-L100) 确认 `func.attrs` 确有 `global_symbol` 字段）。

**进阶变体**：把打印改成抛错（`raise ValueError(f"forbidden name {name}")`），你就得到一个「校验类」诊断 pass，行为与 `NestedLoopChecker` 完全同构。

#### 4.5.5 小练习与答案

**练习 1**：为什么新增 C++ pass 不用改 `CMakeLists.txt`？
**答**：因为 [CMakeLists.txt:135-147](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/CMakeLists.txt#L135-L147) 用 `file(GLOB src/transform/*.cc)` 收集源码，新 `.cc` 会被自动纳入；但需要**重新跑一次 cmake 配置**（glob 在配置期求值），仅 `make` 不会自动捡到新文件。

**练习 2**：诊断 pass 为什么挂在 `PreLowerSemanticCheck` 而不是 `OptimizeForTarget`？
**答**：`PreLowerSemanticCheck` 在合法化之前、IR 最接近用户原始意图时运行，最适合做「语义校验 / 早期诊断」；且其文档明确声明「只校验、不改模块」（[phase.py:120-122](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L120-L122)）。改写类 pass 才挂到 `OptimizeForTarget`。

---

## 5. 综合实践

把本讲四道 pass 与新增流程串起来，完成一个「pass 观察实验台」：

1. **准备一个 kernel**：用 u1-l3 的 matmul+relu（或任意带 `T.gemm` 的 kernel）。
2. **手动驱动单 pass**：参考 `InjectFenceProxy` 测试里的写法，构造 `IRModule`，依次手动调用 `tilelang.transform.Simplify()`、`tilelang.transform.LowerTileOp()`，每步用 `mod["main"].show()` 打印 IR。
3. **观察 fence**：定位到 `LowerTileOp` 把 `T.gemm` 降成 `tl_gemm`（async）之后，确认 `InjectFenceProxy`（若目标支持 TMA）会在它前面插入 `fence_proxy_async`。
4. **观察 host 校验**：编译完成后 `print(kernel.get_host_source())`，在 host stub 里找到由 `MakePackedAPI`/`ArgBinder` 发射的 `num_args should be` 与 `is expected to have non-NULL pointer` 断言。
5. **挂上你的诊断 pass**：把 4.5.4 的 `FuncNamePrinter` 接入 `PreLowerSemanticCheck`，确认它在第 2 步之前就打印出 `main`。

**产出**：一张「pass 名 → 该 pass 对 IR 的可见改变」的对照表（例如 `Simplify` 消除了冗余 let、`LowerTileOp` 把 `tl.tileop.gemm` 变成 `tl_gemm`、`InjectFenceProxy` 插入 fence、`MakePackedAPI` 在 host 织入校验）。这张表是你日后调试任何 TileLang 编译问题的导航图。

## 6. 本讲小结

- **`InjectFenceProxy`** 是一个 generic/async **代理状态机**：单遍扫描语句序列，只在 `Generic → Async` 边插 `fence.proxy.async`，对未知 extern call 保守按 generic 处理，顺带把 `tma_store` 改写成 arrive/wait 三连。
- **LetStmt 内联有两条路径**：激进 `LetInline`（无脑替换、由 `tl.force_let_inline` 触发）与 `Simplify` 内部的保守内联（`CanInlineLetStmt` + `used_in_buffer_def` 双闸门，保护出现在 Buffer 定义里的变量）。
- **`tensor_checks` 不是独立 pass**，而是 `MakePackedAPI` + `ArgBinder` 在生成 host stub 时织入的断言（num_args、非空指针、dtype/shape/strides/device），目的是 ABI 稳定 + 低开销，且不可关闭。
- **`LowerThreadAllreduce`** 用 `IsWarpReduction` 选路，把 `tvm_thread_allreduce` 降为「warp shuffle」/「两阶段 shuffle + staging 共享内存」/「共享内存树形规约」三选一，规约维度必须连续。
- **新增 pass 四步**：写 `.cc`（`CreatePrimFuncPass` + `TVM_FFI_STATIC_INIT_BLOCK`，glob 自动编译）→ 加 Python 包装 → 接入 `phase.py` → 加测试；诊断类可走纯 Python `prim_func_pass`，挂 `PreLowerSemanticCheck`。

## 7. 下一步学习建议

- **向「算子层」深入**：本讲的 pass 只搬运 / 改写 IR，不生产算子。若你想看「`T.gemm` 如何变成 mma/wgmma 指令」，接 u7-l1（C++ 算子实现机制）与 u7-l2（CUDA 模板与 GEMM 内核族）。
- **向「codegen」深入**：`tensor_checks` 的 host stub 最终由 codegen 打印成 C/CUDA，接 u7-l3（目标后端 codegen 深入），看 `CodeGenTileLangCUDA` 如何把 IR 翻译成源码。
- **动手扩展**：在 4.5 的诊断 pass 基础上，尝试写一道真正的改写 pass——例如「统计每个 kernel 用了多少个 shared buffer」——走 C++ 四步流程，跑通后即可作为贡献 PR 的练手题（参考 u7-l5 的贡献流程与 `format.sh`）。
- **回查上游 TVM**：`CreatePrimFuncPass`、`prim_func_pass`、`ArgBinder` 都来自上游 TVM，遇到本讲未覆盖的细节可直接读 `3rdparty/tvm` 对应源码。
