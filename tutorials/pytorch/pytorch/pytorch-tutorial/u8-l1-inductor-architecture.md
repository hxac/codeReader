# Inductor 架构与 FX 图输入

## 1. 本讲目标

本讲是 Unit 8（Inductor 代码生成）的第一篇，目标是建立对 Inductor 整体架构的「鸟瞰图」。读完本讲，你应当能够：

- 说清 Inductor 在 `torch.compile` 整条编译链路中处于什么位置、上游交给它什么、它又交给下游什么。
- 读懂 Inductor 的对外入口 `compile_fx`：它的输入是一个 FX `GraphModule`，输出是一个可以直接调用的 `OutputCode`（编译产物）。
- 理解从「拿到 FX 图」到「生成代码」之间的几个关键阶段：分解（decompositions）、图改写（joint_graph_passes）、IR 构建（`GraphLowering.run`）。
- 掌握 `compile_fx.py` 的分层结构（`compile_fx` → `compile_fx_inner` → `_compile_fx_inner` → `fx_codegen_and_compile`），以及进程内 / 子进程 / 序列化三种编译模式。
- 认识 `config.py` 中的关键开关，尤其是与「输出 stride」相关的 `keep_output_stride` 与本次更新新增的 `strict_output_strides`，并理解为什么要在 `joint_graph_passes` **之前**就把原始输出 stride 记录下来。

本讲只讲「入口与流程」，不深入调度器融合（u8-l2）、kernel 代码生成（u8-l3）、AOTI 部署（u8-l4）的细节。

## 2. 前置知识

阅读本讲前，建议你先建立以下认知（对应前置讲义）：

- **FX 图（GraphModule）**：PyTorch 用 `torch.fx` 把一段 Python 计算表达成一张有向无环图，节点是算子调用（`call_function` / `call_method`）、输入（`placeholder`）和输出（`output`）。每个节点带一个 `meta["val"]`，记录它在 FakeTensor 语义下的形状/dtype/stride。Inductor 的输入就是这样的图。
- **torch.compile 流水线**（u7-l1 / u7-l4）：Dynamo 捕获字节码得到 FX 图 → 默认后端里 AOTAutograd 把「前向 + 反向」拆成两张可独立编译的图 → 最终把每张图交给一个 **backend compiler**。Inductor 就是那个默认的 backend compiler。
- **decompositions（分解）**：把一个高阶算子改写成若干更基础的算子，方便后端做融合优化。例如把 `aten.tanh_backward` 拆成更原子的形式。
- **stride 与内存布局**（u2-l2）：一个张量的视图由 `sizes + strides + storage_offset` 描述；同样的数据，stride 不同就代表不同的「读法」。这条线索在本讲的「输出 stride」部分会反复出现。

如果你对上面任何一项还陌生，先回到对应讲义补一下再继续。

## 3. 本讲源码地图

本讲主要涉及两个文件：

| 文件 | 角色 |
| --- | --- |
| `torch/_inductor/compile_fx.py` | Inductor 的编译入口与编排逻辑。`compile_fx` 是对外暴露给 AOTAutograd 的 backend compiler；`compile_fx_inner` / `_compile_fx_inner` 负责单张图的真正编译；`compile_fx_forward` 负责前向图的特化处理。 |
| `torch/_inductor/config.py` | Inductor 的全部可调开关（环境变量、运行时配置）。本讲聚焦其中与后端语言、缓存、输出 stride 相关的少数几个。 |

辅助理解的文件（本讲会点到但不会深入）：

| 文件 | 角色 |
| --- | --- |
| `torch/_inductor/decomposition.py` | 提供 `select_decomp_table()`，给出当前配置下要应用的分解表（基于 `core_aten_decompositions` + Inductor 自己的扩展）。 |
| `torch/_inductor/graph.py` | `GraphLowering` 的实现，`graph.run()` 在这里把 FX 图 lowering 成 Inductor 的 IR。本讲只到「调用 `graph.run()`」这一层。 |
| `torch/_inductor/compile_fx_subproc.py` | 子进程编译模式 `_SubprocessFxCompile` 的实现。 |
| `torch/_inductor/compile_fx_ext.py` | 序列化 / 跨进程编译模式（`_OutOfProcessFxCompile` 等）。 |

## 4. 核心概念与源码讲解

### 4.1 Inductor 在编译链路中的位置：`compile_fx` 入口

#### 4.1.1 概念说明

回顾 u7-l4：Dynamo 默认后端是「AOTAutograd + Inductor」。AOTAutograd 的工作是把一张带 autograd 的图拆成「纯前向图」和「纯反向图」，然后对每一张图调用一个 **forward/backward compiler**。Inductor 暴露给 AOTAutograd 的那个 compiler，就是 `compile_fx`。

换句话说，`compile_fx` 的契约非常简单：

- **输入**：一个 FX `GraphModule`（不含 autograd 节点，已经被 AOTAutograd 处理过）+ 一组 `example_inputs`（FakeTensor，用于推导形状与代码生成时的 dry-run）。
- **输出**：一个 `OutputCode`，本质是一个可调用的 Python/C++ 函数 `compiled_fn(real_inputs) -> real_outputs`。

它的 docstring 把这层关系说得很清楚：

[torch/_inductor/compile_fx.py:2845-2855](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx.py#L2845-L2855) — 说明 `compile_fx` 是 Inductor 的主编排函数，它负责「调用 AOT Autograd，并最终收到一个 `inner_compile` 回调来做真正的编译」。

注意一个反直觉的点：**`compile_fx` 自己并不直接 lowering FX 图**。它先把图与分解表交给 AOTAutograd，AOTAutograd 在拆分前后向、应用分解之后，会回调 Inductor 提供的 `inner_compile`（默认就是 `compile_fx_inner`）来做真正的「单张图编译」。这是 Inductor 名字里 inductor 与 AOTAutograd 协作的典型形态。

#### 4.1.2 核心流程

`compile_fx` 的顶层流程可以概括为：

```text
compile_fx(model_, example_inputs_, inner_compile=compile_fx_inner, ...)
  │
  ├─ 选择分解表 get_decomp_fn（select_decomp_table 或自定义）
  ├─ 处理短路：CompilerBisector、config_patches（递归调用自身）
  ├─ 有 CUDA/XPU 输入时，提前唤醒 async compile 子进程池
  ├─ 若 cpp_wrapper / fx_wrapper：走 _maybe_wrap_and_compile_fx_main 的另一条路
  └─ 否则 → _maybe_wrap_and_compile_fx_main(...)
          └─ 最终构造 AOTAutograd compiler，其 forward/backward compiler
             都指向 compile_fx_inner（即真正的单图编译）
```

其中 `inner_compile` 默认值就是 `compile_fx_inner`：

[torch/_inductor/compile_fx.py:2836-2844](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx.py#L2836-L2844) — `compile_fx` 的签名，`inner_compile` 默认是 `compile_fx_inner`。

分解表的选择也很关键：当调用方没有传入自定义 `decompositions` 时，使用 `select_decomp_table`：

[torch/_inductor/compile_fx.py:2856-2861](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx.py#L2856-L2861) — 决定用哪张分解表。

`select_decomp_table` 会根据 `config.fallback_random` 等开关返回不同的分解集合，底料是 `core_aten_decompositions()` 加上 Inductor 自己注册的扩展（`inductor_decompositions`）：

[torch/_inductor/decomposition.py:1168-1177](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/decomposition.py#L1168-L1177) — 分解表随配置变化。

#### 4.1.3 源码精读

`compile_fx` 在处理 `config_patches` 时会递归调用自身，保证补丁在整个编译（含反向）范围内生效：

[torch/_inductor/compile_fx.py:2872-2882](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx.py#L2872-L2882) — 用 `config.patch(config_patches)` 递归调用 `compile_fx`。

末尾两条出口都会汇聚到 `_maybe_wrap_and_compile_fx_main`：

[torch/_inductor/compile_fx.py:2933-2940](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx.py#L2933-L2940) — 普通路径的最终调用。

> 小结：把 `compile_fx` 理解成「Inductor 的接待员 + 调度员」即可——它不亲自写代码，只负责把图、分解表、配置、`inner_compile` 组装好，再交给 AOTAutograd 编排。

#### 4.1.4 代码实践

1. **目标**：确认 `compile_fx` 的输入输出契约。
2. **步骤**：
   - 在 `compile_fx.py` 中找到 `compile_fx` 的定义与 docstring。
   - 用 `TORCH_COMPILE_DEBUG=1` 跑一个小函数（见综合实践），在 Inductor 日志里找到它打印的 FX 图。
3. **需要观察的现象**：日志中会出现 `before_joint_graph` / `after_joint_graph` / `before_post_grad_graph` 等 artifact 名称，对应本讲后面要讲的几个阶段。
4. **预期结果**：你能把日志里的图与「Dynamo 交给 Inductor 的那张 FX 图」对应起来。
5. 待本地验证（取决于是否安装了带 Inductor 的 PyTorch）。

#### 4.1.5 小练习与答案

**练习 1**：`compile_fx` 的 `inner_compile` 参数默认值是什么？为什么需要把它作为参数传入而不是写死？

**答案**：默认是 `compile_fx_inner`。把它做成参数是为了让测试、AOTI 等场景可以替换内部实现（例如传入 `cpp_wrapper=True` 的变体），同时方便 `config_patches` 递归调用时把补丁层层包到 `inner_compile` 上（见 2877-2878 行）。

**练习 2**：`compile_fx` 自己会把 FX 图 lowering 成 IR 吗？

**答案**：不会。它只负责编排，真正的 lowering 发生在它（经 AOTAutograd）回调的 `inner_compile` → `_compile_fx_inner` → `GraphLowering.run` 这条链路里。

### 4.2 单图编译主链路：`compile_fx_inner` → `fx_codegen_and_compile`

#### 4.2.1 概念说明

AOTAutograd 拆好图、应用完分解之后，会回调 Inductor 的 `inner_compile` 对「一张已经不含 autograd 的图」做编译。这条链路的入口是 `compile_fx_inner`，真正的活儿在 `_compile_fx_inner` 与 `fx_codegen_and_compile` 里。

这里有一个三层结构，初学者容易混：

| 函数 | 职责 |
| --- | --- |
| `compile_fx_inner` | 「外壳」：设置默认 kwargs、装配各种上下文（triton 配置、lazy graph module、调试上下文、计时），再把活儿委托给 `_compile_fx_inner`。 |
| `_compile_fx_inner` | 「调度员」：处理空图短路、缓存查询（`fx_graph_cache`）、决定走缓存还是真正编译，最终调用 `fx_codegen_and_compile`。 |
| `fx_codegen_and_compile` | 「执行者」：根据编译模式（进程内 / 子进程 / 序列化）选择一个 `FxCompile` 实现，调用其 `codegen_and_compile` 完成 lowering + codegen。 |

#### 4.2.2 核心流程

```text
compile_fx_inner(gm, example_inputs, **kwargs)
  ├─ 设置 kwargs 默认值（cudagraphs/static_input_idxs/...）
  ├─ 装配上下文栈（config.patch、lazy graph module、dynamo 计时、DebugContext）
  └─ wrap_compiler_debug(_compile_fx_inner)(gm, example_inputs, **kwargs)

_compile_fx_inner(gm, example_inputs, **graph_kwargs)
  ├─ 若图为空（无 call）→ 直接返回 make_boxed_func(gm.forward)
  ├─ 计算 inputs_to_check、设置 cudagraphs
  └─ fx_codegen_and_compile(gm, example_inputs, inputs_to_check, ...)

fx_codegen_and_compile(gm, example_inputs, inputs_to_check, **graph_kwargs)
  ├─ 选择 scheme：
  │     NORMAL     → _InProcessFxCompile（进程内）
  │     SERIALIZE  → _DebugSerdeFxCompile（序列化，调试用）
  │     SUBPROCESS → _SubprocessFxCompile（子进程）
  ├─ 可选叠加 async / progressive 包装
  └─ scheme.codegen_and_compile(gm, example_inputs, inputs_to_check, graph_kwargs)
```

#### 4.2.3 源码精读

`compile_fx_inner` 把一堆上下文叠成一个 `ExitStack`，最后通过 `wrap_compiler_debug` 调用 `_compile_fx_inner`：

[torch/_inductor/compile_fx.py:827-875](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx.py#L827-L875) — 装配上下文并委托给 `_compile_fx_inner`。注意第 850-851 行禁用了 Python dispatcher 与切换 lazy graph module，保证 lowering 在「干净的执行环境」中进行。

`_compile_fx_inner` 对「空图」（没有任何算子调用）做了快速短路，直接把 `gm.forward` 包成 boxed func 返回——这是反向图常见的情形：

[torch/_inductor/compile_fx.py:913-932](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx.py#L913-L932) — 空图短路。

`fx_codegen_and_compile` 根据 `fx_compile_mode` 选择 scheme，这是「编译在哪个进程里做」的开关：

[torch/_inductor/compile_fx.py:1885-1936](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx.py#L1885-L1936) — 三种编译模式的分流。`FxCompileMode` 枚举见 [torch/_inductor/compile_fx.py:179-185](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx.py#L179-L185)。

子进程模式 `_SubprocessFxCompile` 用一个大小为 1 的 `SubprocPool`（SPAWN 方式）来跑编译，并把 `TORCHINDUCTOR_CACHE_DIR` / `TRITON_CACHE_DIR` 透传给子进程：

[torch/_inductor/compile_fx_subproc.py:31-61](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx_subproc.py#L31-L61) — 子进程编译池。

> 为什么需要子进程编译？主要两个动机：(1) 把「重活」（max-autotune、大量代码生成）从主训练进程挪走，避免 GIL 与内存膨胀影响训练吞吐；(2) 隔离编译失败。默认仍是进程内（`NORMAL`）。

#### 4.2.4 代码实践

1. **目标**：看清「进程内 / 子进程 / 序列化」三种模式如何切换。
2. **步骤**：在 shell 中设置 `TORCHINDUCTOR_FX_COMPILE_MODE=SUBPROCESS`，跑一个 `torch.compile` 的小函数；再设回默认对比。
3. **需要观察的现象**：`SUBPROCESS` 模式下首次编译会多出一个子进程的启动开销，且日志/trace 会显示编译发生在 worker 里。
4. **预期结果**：功能上两种模式产出等价的编译函数，但编译耗时分布不同。
5. 待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`compile_fx_inner` 与 `_compile_fx_inner` 为什么要分成两层？

**答案**：外层 `compile_fx_inner` 负责「不变的基础设施」（默认 kwargs、上下文栈、计时、调试包装），这些对任何调用方式都一样；内层 `_compile_fx_inner` 负责「可变的业务逻辑」（缓存查询、模式分流）。分层后，缓存与调试逻辑不会和上下文装配耦合在一起。

**练习 2**：什么情况下 `_compile_fx_inner` 会直接返回而不做 lowering？

**答案**：当图里没有任何 `call` 节点（`dynamo_utils.count_calls(gm.graph) == 0`）、且不是 AOT 模式、且未开启 bundled autograd cache 时，直接返回 `make_boxed_func(gm.forward)`。

### 4.3 前向图特化：`compile_fx_forward` 与 joint_graph_passes

#### 4.3.1 概念说明

`compile_fx_forward` 是前向图（forward graph）的编译入口。在推理（inference）场景下，它要在真正 lowering 之前先跑一遍 **joint graph passes**——这是一组作用在「前向 + 反向合在一起的联合图」上的改写，比如 `pad_mm`（把矩阵乘补齐到更适合硬件的形状）。

本讲的更新焦点就在这里：**joint_graph_passes 里的 `pad_mm` 会引入带「补齐 stride」的 view 节点**。如果我们在 joint passes **之后**才去记录「用户原始的输出 stride」，就会把这些补齐过的 stride 当成「原始的」，从而在后续布局优化里把补齐 stride 泄漏到用户可见的输出张量上。修复办法很简单：**在 joint_graph_passes 之前就把原始 stride 记录下来**。

#### 4.3.2 核心流程

推理路径下的 `compile_fx_forward` 关键片段：

```text
if is_inference:
    记录 output_stack_traces（保存栈跟踪，后续 pass 可能抹掉）
    _recursive_record_original_output_strides(gm)   # ← 新增：在 joint passes 之前记录原始 stride
    gm = _recursive_joint_graph_passes(gm, input_device=...)
    （之后才进入 fx_codegen_and_compile → GraphLowering.run）
```

注意 `record_original_output_strides` 内部还有一个保护：如果某个 output 节点已经记录过 stride，就不再覆盖。这是为了让「提前记录」与其它路径（例如 `ir.py` 里对子图、`compile_fx_forward` 里对推理图）的记录调用互不干扰。

#### 4.3.3 源码精读

`record_original_output_strides` 的核心逻辑是：找到 `output` 节点，遍历它的每个输出，把对应的 FakeTensor（`node.meta["val"]`）的 `stride()` 收集成一个列表，写到 `output_node.meta["original_output_strides"]`：

[torch/_inductor/compile_fx.py:264-289](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx.py#L264-L289) — 记录原始输出 stride。本次更新新增了第 267-270 行的「不覆盖已记录 stride」保护：

```python
# Don't overwrite strides that were already recorded (e.g., before
# joint_graph_passes which can introduce padded strides via pad_mm).
if "original_output_strides" in output_node.meta:
    return
```

`_recursive_record_original_output_strides` 会递归处理图里的 `invoke_subgraph` 高阶算子（HOP），保证子图的输出 stride 也被记录：

[torch/_inductor/compile_fx.py:292-300](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx.py#L292-L300) — 递归记录，注释里点明 `invoke_subgraph` HOP 要求尊重输出 stride。

推理路径下，`compile_fx_forward` 把记录动作明确放在 joint passes **之前**：

[torch/_inductor/compile_fx.py:2603-2609](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx.py#L2603-L2609) — 本次更新的核心改动：

```python
# Record original output strides BEFORE joint_graph_passes, because
# pad_mm (run as part of joint_graph_passes) can introduce views with
# padded strides that would be incorrectly captured as "original".
_recursive_record_original_output_strides(gm)

inputs_devices = get_inputs_devices(example_inputs, gm)
gm = _recursive_joint_graph_passes(gm, input_device=next(iter(inputs_devices)))
```

`_recursive_joint_graph_passes` 会递归地对子图模块调用 `joint_graph_passes`，`pad_mm` 正是在这里被注入的：

[torch/_inductor/compile_fx.py:545-582](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx.py#L545-L582) — joint graph passes 的递归执行。

进程内编译路径 `_InProcessFxCompile.codegen_and_compile` 里也有一处对 stride 的记录，位于 FakeTensorProp 之后、post-grad passes 之前（它复用同一个带保护的 `record_original_output_strides`，所以不会覆盖推理路径已经记下的值）：

[torch/_inductor/compile_fx.py:1420-1432](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx.py#L1420-L1432) — 进程内路径在 FakeTensorProp 之后再次调用 `_recursive_record_original_output_strides`，由 269 行的 guard 保证幂等。

记录下来的 `original_output_strides` 在哪里被消费？主要在 IR lowering 阶段：`graph.py` 的 `get_user_visible_output_strides` 会读它，作为「用户期望的输出布局」约束：

[torch/_inductor/graph.py:200-215](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/graph.py#L200-L215) — 读取 `original_output_strides` 作为用户可见输出的 stride 约束。

> 直觉理解：Inductor 在内部可以做各种布局优化（channels-last、stride 重排），但对用户可见的输出张量，它需要「兜底」保证 stride 与 eager 一致（或至少 stride 顺序一致），否则用户的下游代码（比如直接依赖某维 stride 的 view 操作）会出问题。`original_output_strides` 就是这个兜底的「真值来源」——所以它必须在任何可能篡改 stride 的 pass 之前就被冻结下来。

#### 4.3.4 代码实践

1. **目标**：观察「输出 stride 被提前记录」如何保护用户可见输出。
2. **步骤**：
   - 阅读本节引用的四处源码（记录函数、递归版本、推理路径调用、进程内路径调用）。
   - 在 `_compile_fx_inner` 入口处用 `gm.print_readable(print_output=False, include_stride=True)` 打印图，找到 `output` 节点。
3. **需要观察的现象**：在 joint_graph_passes 之后，某些 matmul 相关节点附近可能出现 `view` / `slice` 等「补齐」痕迹，但 output 节点的 `meta["original_output_strides"]` 应保持 joint passes 之前的值（因为 guard 阻止了覆盖）。
4. **预期结果**：你能解释「为什么 guard 是必要的」——若没有它，第二次（进程内路径）记录会用被污染的 stride 覆盖第一次（推理路径）记录的干净值。
5. 若无法实际触发 pad_mm，可标注「待本地验证」并仅做源码阅读。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `record_original_output_strides` 要在 `joint_graph_passes` **之前**调用，而不是之后？

**答案**：因为 `joint_graph_passes` 里的 `pad_mm` 会插入带「补齐 stride」的 view 节点；若在之后记录，这些补齐 stride 会被错误地当成「用户原始 stride」固化下来，最终泄漏到用户可见的输出张量上，破坏与 eager 的一致性。

**练习 2**：`record_original_output_strides` 里 `if "original_output_strides" in output_node.meta: return` 这个 guard 解决了什么问题？

**答案**：同一张图可能在多个时机被记录（推理路径在 joint passes 前、进程内路径在 FakeTensorProp 后）。guard 保证「先到先得」——最早、最干净的记录生效，后续调用不会用可能已被污染的 stride 覆盖它，使记录过程幂等。

### 4.4 IR 构建：`GraphLowering.run` 在哪里被调用

#### 4.4.1 概念说明

到目前为止我们都在「图改写」层面打转。真正把 FX 图翻译成 Inductor 自己的 IR（中间表示，后续会用来生成 Triton/C++ kernel）的地方，是 `GraphLowering.run(*example_inputs)`。`GraphLowering` 在 `graph.py` 中定义（u8-l2 会详细讲它的内部），本讲只关心它「在哪被构造、在哪被调用」。

#### 4.4.2 核心流程

进程内路径 `_InProcessFxCompile.codegen_and_compile` 的中后段：

```text
view_to_reshape(gm)                       # 布局优化前把 view 换成 reshape
FakeTensorProp(gm, example_inputs)        # 重新传播 FakeTensor（含 stride）
_recursive_record_original_output_strides # 记录输出 stride（幂等）
_recursive_post_grad_passes(gm)           # post-grad 图改写
graph = GraphLowering(gm, ...)            # 构造 lowering 容器
graph.run(*example_inputs)                # ← 真正的 lowering：FX → IR + 应用 decomps
graph.codegen() / codegen_with_cpp_wrapper()  # IR → 目标代码（Python/C++ wrapper）
```

#### 4.4.3 源码精读

`GraphLowering` 的构造点（注意它把 `get_decomp_fn` 也传了进去，分解发生在 lowering 内部）：

[torch/_inductor/compile_fx.py:1560-1584](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx.py#L1560-L1584) — 构造 `GraphLowering`，传入分解表、shape_env、各种模式开关。

真正的 lowering 调用：

[torch/_inductor/compile_fx.py:1590-1595](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx.py#L1590-L1595) — `graph.run(*example_inputs)`，在 `V.set_graph_handler(graph)` 等上下文里执行。这一行之后，`graph.graph_outputs` 就是 IR 节点列表。

紧接着，代码会把 IR 节点的 stride 抽取出来，存进编译产物，方便运行时把「真实输出 stride」回传给调用方：

[torch/_inductor/compile_fx.py:1596-1612](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx.py#L1596-L1612) — 把 lowering 后的输出 stride 序列化进编译图。

> 这条「记录原始 stride → lowering 时当作约束 → lowering 后再抽出实际 stride 回传」的链路，就是 Inductor 保证「输出布局对用户友好」的完整机制。本次更新修的正是这条链路的源头（记录时机）。

#### 4.4.4 代码实践

1. **目标**：在源码里走完一遍「FX 图 → GraphLowering → 输出 stride」的路径。
2. **步骤**：从 [compile_fx.py:2836](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/compile_fx.py#L2836) 的 `compile_fx` 出发，沿调用链跳到 `_compile_fx_inner`（886）→ `fx_codegen_and_compile`（1874）→ `_InProcessFxCompile.codegen_and_compile`（1321）→ `graph.run`（1595）。
3. **需要观察的现象**：调用链层层下沉，每层的职责逐步从「编排」收敛到「单图编译」再到「IR 构建」。
4. **预期结果**：你能在不打开 `graph.py` 内部实现的前提下，画出这条调用链。
5. 待本地验证（阅读型实践，无需运行）。

#### 4.4.5 小练习与答案

**练习**：`GraphLowering` 的构造参数里有 `get_decomp_fn`，这说明分解发生在哪一步？

**答案**：分解发生在 `graph.run()` 内部（即 lowering 过程中），而不是在进入 Inductor 之前。`get_decomp_fn` 被传进 `GraphLowering`，让它在遍历 FX 节点、逐节点 lowering 时，能对每个算子先查分解表、把高阶算子拆成更原子的形式再做 IR 翻译。

### 4.5 `config.py` 关键开关：后端语言、缓存与输出 stride

#### 4.5.1 概念说明

`torch/_inductor/config.py` 是 Inductor 的「控制面板」。本讲只挑出与本讲主题（入口流程、输出 stride）最相关的几组开关。理解它们的最好方式是记住「它们大多既能通过环境变量 `TORCHINDUCTOR_*` 设置，也能在运行时用 `torch._inductor.config.patch(...)` 临时改」。

#### 4.5.2 关键开关一览

| 开关 | 含义 | 默认 |
| --- | --- | --- |
| `cpp_wrapper` | 用 C++ wrapper 而非 Python wrapper 包裹生成的 kernel | `False`（`TORCHINDUCTOR_CPP_WRAPPER`） |
| `fx_wrapper` | 用 FX GraphModule wrapper（AOTI 场景） | `False`（`TORCHINDUCTOR_FX_WRAPPER`） |
| `fx_graph_cache` | 是否启用本地 FX 图代码缓存（避免重复编译） | `True` |
| `force_disable_caches` | 强制关闭所有缓存（调试用） | `False` |
| `layout_optimization` | 是否做布局优化（如 channels-last） | 非 ROCm 默认开 |
| `keep_output_stride` | 布局优化后是否把输出 stride 保持与 eager 一致 | `True`（`TORCHINDUCTOR_KEEP_OUTPUT_STRIDE`） |
| `strict_output_strides` | 视图类输出是否必须**精确**匹配 eager stride（而非仅匹配 stride 顺序） | `False`（**本次更新新增**） |
| `triton.cudagraphs` | 在输出代码上启用 CUDA Graph | `False`（`TORCHINDUCTOR_CUDAGRAPHS`） |

#### 4.5.3 源码精读

后端语言相关：

[torch/_inductor/config.py:194-207](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/config.py#L194-L207) — `cpp_wrapper` 与 `fx_wrapper`。注意 `cpp_wrapper` 与 `disable_cpp_codegen` 不兼容。

缓存相关：

[torch/_inductor/config.py:109-114](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/config.py#L109-L114) — `fx_graph_cache`，Inductor 是否复用之前编译过的图代码。

输出 stride 相关（本讲重点）：

[torch/_inductor/config.py:884-889](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/config.py#L884-L889) — `keep_output_stride` 与新增的 `strict_output_strides`：

```python
# Whether to keep the output strides the same as eager after layout optimization.
keep_output_stride = os.environ.get("TORCHINDUCTOR_KEEP_OUTPUT_STRIDE", "1") == "1"

# Whether view outputs must match eager strides exactly instead of only matching
# their stride order. Exact matching can introduce additional copy kernels.
strict_output_strides = False
```

二者的区别很微妙但很重要：

- `keep_output_stride`（默认开）：保证输出 stride 与 eager **一致**。这是「对用户友好」的默认行为。
- `strict_output_strides`（默认关，无环境变量）：进一步要求**视图类输出**精确匹配 eager 的 stride 数值，而不仅仅是 stride **顺序**。它默认关，因为「精确匹配」往往需要额外插入 copy kernel，可能拖慢执行。它的消费点在 lowering 阶段的布局决策：

[torch/_inductor/graph.py:2119-2135](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/graph.py#L2119-L2135) — 当输出是 view 且没有开启 `strict_output_strides` 时，只要求 `stride_order`（更宽松、更省 copy）；否则要求 `exact_strides`。

`triton.cudagraphs`（与 CUDA Graph 录制相关，u9-l4 会深入）：

[torch/_inductor/config.py:1898](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/config.py#L1898) — `cudagraphs = os.environ.get("TORCHINDUCTOR_CUDAGRAPHS") == "1"`。

> 把本节与 4.3 串起来看：`original_output_strides`（记录的真值）+ `keep_output_stride`（是否兜底）+ `strict_output_strides`（兜底到多严格）三者共同决定了「用户拿到的输出张量到底是什么 stride」。本次更新修的是「真值的记录时机」，让它在任何能篡改 stride 的 pass 之前就被冻结。

#### 4.5.4 代码实践

1. **目标**：用日志验证 decompositions 与 IR 构建阶段，并定位输出 stride 相关开关。
2. **步骤**：
   - 在 `config.py` 中找到 `strict_output_strides`、`keep_output_stride`、`cpp_wrapper` 三个开关，确认它们的默认值与可用的环境变量。
   - 设置 `TORCHINDUCTOR_LOG_LEVEL=info`（或 `TORCH_COMPILE_DEBUG=1`），编译一个小函数，从日志中识别出 decompositions 应用与 IR 构建阶段。
3. **需要观察的现象**：info 日志里能看到 `_recursive_joint_graph_passes`、`fx_codegen_and_compile`、`GraphLowering.compile_to_fn` 等计时事件，以及 `before_joint_graph` / `after_joint_graph` 等图快照。
4. **预期结果**：你能把日志里的阶段名与本讲 4.1–4.4 的函数一一对应。
5. 待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：`keep_output_stride` 与 `strict_output_strides` 有什么区别？为什么后者默认关闭？

**答案**：`keep_output_stride` 保证输出 stride 与 eager 一致（粗粒度兜底）；`strict_output_strides` 进一步要求视图类输出**逐元素精确**匹配 eager 的 stride（仅匹配 stride 顺序不够）。后者默认关，因为精确匹配常常迫使 Inductor 额外插入 copy kernel，损害性能，只有当下游代码对 stride 数值（而非顺序）有硬性依赖时才需要打开。

**练习 2**：你想强制 Inductor 完全不复用任何已编译产物来排查一个缓存相关的 bug，应该用哪个开关？

**答案**：`config.force_disable_caches = True`（见 [config.py:166](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/config.py#L166)）。它会绕过 `fx_graph_cache` 等所有缓存路径。

## 5. 综合实践

**任务**：用一次 `torch.compile` 把本讲的全部线索串起来——从入口 `compile_fx`、到 joint passes 前的 stride 记录、到 `GraphLowering.run` 的 lowering、再到输出 stride 的兜底。

**操作步骤**（CPU 也可，无 GPU 时部分日志会缺少 CUDA 相关项）：

1. 准备一个会被 `pad_mm` 触发布局改写的小函数，例如一个矩阵乘加：

   ```python
   # 示例代码（非项目原有代码）
   import torch

   @torch.compile(mode="max-autotune-no-cudagraphs")
   def f(x, w, b):
       return torch.relu(x @ w + b)

   x = torch.randn(17, 23)   # 故意用非 2 的幂，更容易触发 pad_mm 相关改写
   w = torch.randn(23, 31)
   b = torch.randn(31)
   print(f(x, w, b).shape)
   ```

2. 用环境变量打开 Inductor 的分阶段日志，重新运行：

   ```bash
   TORCHINDUCTOR_LOG_LEVEL=info TORCH_COMPILE_DEBUG=1 python demo.py 2> inductor.log
   ```

3. 在 `inductor.log` 中按本讲的术语检索：
   - `before_joint_graph` / `after_joint_graph`：对应 4.3 的 joint_graph_passes 前后图快照。
   - `_recursive_joint_graph_passes`、`fx_codegen_and_compile`、`GraphLowering.compile_to_fn`：对应 4.2 / 4.4 的计时事件。

4. 在 `config.py` 里确认以下三个开关，并尝试改其中一个再跑一次，对比日志差异：
   - `keep_output_stride`（[config.py:885](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/config.py#L885)）
   - `strict_output_strides`（[config.py:889](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/config.py#L889)）
   - `cpp_wrapper`（[config.py:196](https://github.com/pytorch/pytorch/blob/ca62582ef0678dece3130b28bcf10ce0501777b9/torch/_inductor/config.py#L196)）

5. （源码阅读型）打开 `compile_fx.py:2603-2609`，对照日志里的 `before_joint_graph`，解释「为什么 stride 记录必须在这之前」。

**需要观察的现象**：编译期日志里能清晰看到阶段顺序：记录输出 stride → joint graph passes → post-grad passes → GraphLowering → codegen。运行期输出张量的 `stride` 应与 eager 一致（`keep_output_stride` 默认开）。

**预期结果**：你能用一句话回答「`compile_fx` 收到 FX 图之后、生成可执行代码之前，依次做了哪些事」，并能指出本次更新（在 joint passes 前记录 stride）落在哪一步。

> 若本地 PyTorch 版本未包含本次更新的 commit（`[inductor] Fix pad_mm leaking padded strides to user-visible outputs`），日志细节可能略有差异，但整体阶段顺序不变。可将结果标注「待本地验证」。

## 6. 本讲小结

- **Inductor 是 `torch.compile` 默认后端的最后一个 compiler**：Dynamo 产 FX 图 → AOTAutograd 拆前后向 → Inductor 的 `compile_fx` 接管单张图的编译。
- **`compile_fx` 是编排层**：它不亲自 lowering，只负责分解表选择、配置补丁、模式分流，真正的活儿经 `inner_compile` 回调完成。
- **三层单图编译结构**：`compile_fx_inner`（上下文外壳）→ `_compile_fx_inner`（缓存与分流）→ `fx_codegen_and_compile`（按进程内/子进程/序列化模式执行）。
- **真正的 FX→IR 翻译在 `GraphLowering.run`**（`compile_fx.py:1595`），分解也在这一步内完成。
- **本次更新的核心**：在 `compile_fx_forward` 里，把 `_recursive_record_original_output_strides(gm)` 提前到 `_recursive_joint_graph_passes` **之前**调用，并给 `record_original_output_strides` 加上「不覆盖已记录」的 guard，避免 `pad_mm` 引入的补齐 stride 污染用户可见输出。
- **输出 stride 三件套**：`original_output_strides`（真值）+ `keep_output_stride`（是否兜底）+ `strict_output_strides`（兜底到多严格，本次更新新增）共同决定用户拿到的输出布局。

## 7. 下一步学习建议

本讲只到「Inductor 拿到 FX 图、把它变成 IR」这一层。接下来建议：

- **u8-l2 调度器与算子融合**：深入 `scheduler.py`，看 IR 节点如何被分组成可融合的 kernel 组。本讲的 `GraphLowering.run` 产出的 IR，正是调度器的输入。
- **u8-l3 kernel 代码生成（C++/Triton）**：看 `codegen/cpp.py` 与 `kernel/mm.py` 如何把融合后的 IR 翻译成真实可执行代码，并理解 `cpp_wrapper` / Triton 两条路径的差异。
- **u8-l4 AOTI 编译与部署**：本讲提到的 `aot_mode` / `cpp_wrapper` / `fx_wrapper` 是 AOTI 的入口，u8-l4 会讲清「提前编译、脱离 Python 运行」的完整模型。
- **补充阅读源码**：`torch/_inductor/graph.py`（`GraphLowering` 的全貌）、`torch/_inductor/joint_graph_passes.py`（`pad_mm` 的注入点，理解本次更新修复问题的源头）。
