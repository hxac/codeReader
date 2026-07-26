# 扩展 TileKernels：新增一个算子的完整流程（综合实战）

## 1. 本讲目标

本讲是整套学习手册的收官篇。前面九个单元你分别学过了 TileLang 骨架、存储层级、循环/规约原语、各类算子家族（转置/量化/MoE/engram/mhc）、autograd 封装、测试与基准设施、硬件感知调优。本讲不再引入新概念，而是把所有知识点**整合成一个可复用的工程动作**：给 TileKernels 新增一个算子。

学完本讲你应该能够：

- 说清「四件套」——**TileLang kernel + Python wrapper + torch 参考实现 + pytest 测试**——各自的职责、落点与协作约定。
- 独立照着 `topk_gate` 与 `transpose` 两个样板，把一个新算法（如行最大值 rowmax 或 RMSNorm）端到端落地。
- 复用 `testing/`（参数生成、`assert_equal`/`calc_diff`）与 benchmark 设施（`benchmark_timer`、`count_bytes`、`benchmark_record`）验证正确性与性能。
- 判断什么情况下还需要「第五件」——modeling 层的 `torch.autograd.Function` 封装。

## 2. 前置知识

本讲默认你已经掌握以下认知（若陌生请先回看对应讲义）：

- **TileLang 骨架**（u2-l1）：`@tilelang.jit` 装饰的构造函数 + `@T.prim_func` 内核；编译期参数 vs 运行时符号 `T.dynamic`；wrapper 四步（校验、分配、编译、启动）。
- **循环与规约原语**（u2-l3）：`T.Parallel/unroll/serial/vectorized`、`T.reduce_max`、`T.alloc_reducer(replication='all')`。
- **包结构**（u1-l3）：算子层在 `tile_kernels/<家族>/`、torch 参考层在 `tile_kernels/torch/`、modeling 层在 `tile_kernels/modeling/`；子包 `__init__.py` 只做再导出。
- **测试三件套**（u3-l2、u9-l1、u9-l2）：参数生成器 + 数据生成器 + `assert_equal`（位精确）/`calc_diff`（浮点相似度）；`@pytest.mark.benchmark` + `benchmark_timer` + `count_bytes` + `bandwidth_gbs=num_bytes/t_us/1e3`。
- **autograd 封装契约**（u8-l1）：`forward`/`backward` 一一对应、`save_for_backward`、`MyFn.apply(...)`。

若以上概念你都还有印象，本讲就是一次「把它们拼起来」的总装练习。

## 3. 本讲源码地图

本讲以两个最小、最干净的样板算子为锚点，说明四件套的每一件长什么样：

| 文件 | 角色 | 作用 |
|------|------|------|
| [tile_kernels/moe/topk_gate_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py) | **第一件：kernel + wrapper** | TileLang kernel `get_topk_gate_kernel` 与用户入口 `topk_gate`。 |
| [tile_kernels/torch/topk.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/topk.py) | **第二件：torch 参考** | `stable_topk` 等纯 PyTorch「标准答案」。 |
| [tests/moe/test_topk_gate.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_topk_gate.py) | **第三件：测试** | 正确性对拍 + benchmark 两个用例。 |
| [tests/transpose/test_transpose.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py) | **第三件（对照样板）** | 展示内联参考（`x.T.contiguous()`）与 `twice_stride` 非连续输入测试。 |
| [tile_kernels/moe/__init__.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/__init__.py) / [tile_kernels/torch/__init__.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/__init__.py) | **导出枢纽** | 把 wrapper / 参考实现再导出给用户。 |
| [tile_kernels/modeling/engram/engram_gate.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py) | **第五件（可选）：modeling 封装** | `EngramGateFn(torch.autograd.Function)`，让黑盒 kernel 可求导。 |

## 4. 核心概念与源码讲解

### 4.1 四件套协同约定

#### 4.1.1 概念说明

把一个新算子「加进 TileKernels」不是只写一个 CUDA kernel。项目用一套**四件套（four-piece set）约定**来保证每个算子都同时具备「跑得快、对得上、可验证、好维护」：

1. **TileLang kernel**：用 DSL 写的高性能 GPU 内核，经 JIT 编译成 CUDA。这是算子的「灵魂」。
2. **Python wrapper**：用户真正调用的函数，负责校验、分配输出、触发/命中 JIT 缓存、按 `prim_func` 形参顺序启动 kernel。
3. **torch 参考实现**：纯 PyTorch 写的「标准答案」，可读、易信任、与硬件无关，供测试做**差分对拍（differential testing）**。
4. **pytest 测试**：正确性用例（kernel 输出 vs 参考实现）+ benchmark 用例（延迟与带宽）。

kernel 与 wrapper 通常合住在 `tile_kernels/<家族>/<name>_kernel.py` 同一个文件里（kernel 在上、wrapper 在下）；参考实现住在 `tile_kernels/torch/`；测试住在 `tests/<家族>/test_<name>.py`。

为什么要拆这四件、而不是全塞进一个文件？

- **信任链**：kernel 是编译后的 CUDA 黑盒，无法肉眼审计正确性；只有拿它去和一个「显然正确」的 PyTorch 参考逐位比对，才能建立信任。参考实现与 kernel 必须物理隔离，避免互相污染。
- **职责单一**：kernel 只管「怎么算得快」；wrapper 只管「怎么被调用」；参考只管「什么是对的」；测试只管「验证与计量」。四者各司其职，修改一处不会牵连其他。
- **性能与正确性解耦**：你可以放心地把 kernel 改得很快很 hacky，只要它仍然和参考实现对拍通过。

#### 4.1.2 核心流程

四件套之间的协作关系如下（以 `topk_gate` 为例）：

```
              用户调用
                 │
                 ▼
   tile_kernels.moe.topk_gate(scores, num_topk)        ← wrapper（第一件）
                 │  校验 / 分配 topk_idx / JIT 编译 / 启动
                 ▼
        get_topk_gate_kernel(...)(scores, topk_idx)    ← TileLang kernel（第一件）
                 │  返回 topk_idx
                 ▼
   tests/moe/test_topk_gate.py                         ← 测试（第三件）
        ├─ 正确性: topk_idx  ?=  torch_stable_topk(scores)   ← torch 参考（第二件）
        │            用 assert_equal 位精确判等
        └─ benchmark: benchmark_timer + count_bytes → bandwidth_gbs
```

关键协作点：

- **参考实现经 `tile_kernels/torch/__init__.py` 再导出**，测试里 `from tile_kernels.torch import stable_topk as torch_stable_topk` 引入。
- **wrapper 经家族子包 `__init__.py` 再导出**，用户写 `tile_kernels.moe.topk_gate(...)` 而非直接碰 kernel 对象。
- **测试是四件套的「胶水」**：它同时 import wrapper 与参考实现，把二者拉到一起比对。

#### 4.1.3 源码精读

看 `topk_gate` 是怎么被串起来的。

家族子包 `__init__.py` 把 wrapper 再导出（用户入口）：

[moe/\_\_init\_\_.py:L10](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/__init__.py#L10) —— 从 `topk_gate_kernel` 模块再导出 `topk_gate`，这就是 `tile_kernels.moe.topk_gate` 的来源。

参考实现同样经 torch 包再导出：

[torch/\_\_init\_\_.py:L8](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/__init__.py#L8) —— `from .topk import stable_topk, topk_sum_and_topk_group_idx, top2_sum_gate`，把参考实现挂到 `tile_kernels.torch` 命名空间。

测试则两边都拉：

[test_topk_gate.py:L9](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_topk_gate.py#L9) —— `from tile_kernels.torch import stable_topk as torch_stable_topk`，引入「标准答案」。

这就是四件套的物理落点：**两份实现（kernel+wrapper / torch 参考）各自经 `__init__` 导出，测试把它们拉到一起对拍。**

#### 4.1.4 代码实践

**实践目标**：建立「职责 → 文件 → 关键符号」的映射表。

**操作步骤**：

1. 打开本讲源码地图列出的 6 个文件。
2. 画一张三列表格：`职责` | `文件路径` | `关键符号`。
3. 至少填入 8 行：kernel 构造器、prim_func、wrapper、参考实现 `stable_topk`、参数生成器、数据生成器、正确性用例、benchmark 用例。

**预期结果**：你应能用一句话回答「为什么 `tile_kernels.moe.topk_gate` 这个调用能跑、能验证、能计量」——因为它背后站着四件套与两个 `__init__` 导出枢纽。

#### 4.1.5 小练习与答案

**练习 1**：如果把 wrapper 也写进 `tile_kernels/torch/topk.py`（和参考实现放一起），会有什么问题？

> **答案**：破坏了「实现隔离」。`torch/` 目录的语义是「纯 PyTorch、可信任、与硬件无关的参考」，把依赖 GPU + TileLang 编译的 wrapper 塞进去，会让参考实现不再「显然正确」，也让测试的信任链失去意义。此外编译依赖会污染参考实现的 import 路径。

**练习 2**：测试文件为什么要同时 import `tile_kernels.moe`（wrapper）和 `tile_kernels.torch`（参考），而不是只 import kernel 对象？

> **答案**：测试的目的是验证「用户入口（wrapper）的行为」与「标准答案（参考）」一致，而非 kernel 内部细节。直接 import kernel 对象会绕过 wrapper 的校验、分配、零规模守卫，测到的是非真实使用路径。

---

### 4.2 第一件：TileLang kernel + wrapper（以 topk_gate 为样板）

#### 4.2.1 概念说明

`topk_gate` 是一个非常适合做样板的小算子：输入 `scores (num_tokens, num_experts)`，输出每行 top-k 专家的下标 `topk_idx (num_tokens, num_topk)`。它的 kernel 与 wrapper 合住在同一个文件 `topk_gate_kernel.py` 里，体现 TileKernels 的标准组织方式。

它复用了你在 u2-l1 学过的全套骨架，以及 u2-l3 学过的「反复找最大 + min reducer」选取技巧。本节不重复算法细节，只关注「**作为一件可复制的模板，它的结构是什么**」。

#### 4.2.2 核心流程

一个标准算子文件分两段，自上而下：

```
┌─ @tilelang.jit(pass_configs={...})        ← 编译期总开关
│  def get_xxx_kernel(编译期参数):            ← 构造器
│      符号 = T.dynamic(...)                  ← 运行时维度
│      @T.prim_func
│      def xxx_kernel(形参张量):              ← 内核：每块算什么
│          with T.Kernel(grid..., threads=N) as (pids):
│              ... 算 ...
│      return xxx_kernel
│
└─ def xxx(用户参数) -> 输出张量:             ← wrapper：用户入口
       1. assert 校验
       2. 分配输出
       3. kernel = get_xxx_kernel(...)        ← 触发/命中 JIT 缓存
       4. kernel(输入, 输出)                   ← 启动
       return 输出
```

wrapper 固定四步：**校验 → 分配 → 编译 → 启动**。零规模守卫（`if num_tokens == 0: return`）也在 wrapper 里做，而不是塞进 kernel。

#### 4.2.3 源码精读

**kernel 段**——构造器 + prim_func + 网格：

[topk_gate_kernel.py:L10-L14](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L10-L14) —— `@tilelang.jit` 装饰器传入 `TL_DISABLE_WARP_SPECIALIZED: True`（逐元素/小 kernel 家族的标准开关，见 u10-l2）。构造器 `get_topk_gate_kernel(num_experts, num_topk)` 接收**编译期参数**——不同 `num_experts`/`num_topk` 各自特化出一份编译产物。

[topk_gate_kernel.py:L16-L18](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L16-L18) —— `num_tokens = T.dynamic('num_tokens')` 声明运行时符号；`num_aligned_experts = align(num_experts, 32)` 把专家数补齐到 32（一个 warp），这是后面并行规约的前提。

[topk_gate_kernel.py:L25-L30](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L25-L30) —— `T.Kernel(num_tokens, threads=32)` 定义一维网格（一个 block 处理一个 token、32 线程协作）；分配 fragment（scores/amax/idx）与 `alloc_reducer('min', replication='all')`。

[topk_gate_kernel.py:L41-L51](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L41-L51) —— 算法本体：`T.unroll(num_topk)` 轮「`reduce_max` 求最大分 → min reducer 在并列最大值中取最小下标 → `finalize_reducer` → 写出 → 把已选分数置 `-inf` 剔除」。这是 u5-l2 讲过的稳定 top-k。

[topk_gate_kernel.py:L53](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L53) —— `T.copy(topk_idx_shared, topk_idx[pid, 0], disable_tma=True)` 把结果从 shared 写回 global（目标是输出张量，故关 TMA 走向量化，见 u2-l2）。

**wrapper 段**——四步启动：

[topk_gate_kernel.py:L77-L90](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L77-L90) —— 校验（`dim==2`、连续、float32、`num_topk<=num_experts`）→ 分配 `topk_idx`（int64）→ **零规模守卫** `if num_tokens == 0: return` → `kernel = get_topk_gate_kernel(...)` 触发编译 → `kernel(scores, topk_idx)` 启动。注意启动时实参顺序必须与 `prim_func` 形参顺序一致（`scores` 在前、`topk_idx` 在后）。

[topk_gate_kernel.py:L86-L87](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L86-L87) —— `TK_PRINT_KERNEL_SOURCE` 环境变量钩子：设为 1 时打印生成的 CUDA 源码，调试新算子时极其有用。

#### 4.2.4 代码实践

**实践目标**：以 `topk_gate` 为模板，照抄结构写一个「行最大值」算子的 kernel + wrapper 雏形（**示例代码**，非项目原有文件）。

**操作步骤**：

1. 新建一个文件（不放进仓库，仅本地练手）`rowmax_kernel.py`。
2. 把 `topk_gate_kernel.py` 整体复制过来，按下述改动改写：
   - 构造器签名改为 `get_rowmax_kernel(num_experts: int)`（去掉 `num_topk`）。
   - prim_func 输出改为 `rowmax_val: T.Tensor[(num_tokens,), T.float32]`。
   - 把 L41-L51 的 `T.unroll(num_topk)` 循环替换成一次 `T.reduce_max(scores_fragment, amax_fragment)`，再 `rowmax_val[pid] = amax_fragment[0]`。
   - wrapper `rowmax(scores)` 分配 `(num_tokens,)` 的 float32 输出并启动。
3. 暂不要求跑通，只需保证「构造器→prim_func→wrapper」三段齐全、形参顺序一致。

**预期结果**：你能复述 wrapper 四步，并指出「编译期参数（`num_experts`）」与「运行时符号（`num_tokens`）」在你的 rowmax 里分别是哪个。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `num_experts` 是构造器参数（编译期），而 `num_tokens` 是 `T.dynamic`（运行时）？

> **答案**：`num_experts` 决定了 `num_aligned_experts`（fragment 大小、并行规约的 lane 数），这些是编译期必须烤进产物的；不同 `num_experts` 需要不同产物。而 `num_tokens` 只是网格维度的规模，把维度数与具体规模解耦后，一份产物可复用于任意 token 数，故用运行时符号即可。

**练习 2**：wrapper 里 `if num_tokens == 0: return topk_idx` 这行零规模守卫，为什么不能挪进 kernel？

> **答案**：kernel 的网格 `T.Kernel(num_tokens, ...)` 在 `num_tokens==0` 时会启动零个 block，理论上是空跑；但显式在 Python 侧守卫可以避免无意义的 kernel 启动开销、也避免下游对空输出的隐含假设。把「边界情况」统一放在 wrapper 是项目的一致约定。

---

### 4.3 第二件：torch 参考实现（以 stable_topk 为样板）

#### 4.3.1 概念说明

torch 参考实现是「**显然正确的标准答案**」。它有三个硬性要求：

1. **纯 PyTorch**：只用 `torch` 标准算子，不依赖 TileLang、不碰 GPU 特性，任何人都能读懂。
2. **语义与 kernel 完全一致**：包括边界语义（如并列取小下标），否则对拍会假阴性。
3. **可复用**：复杂参考收纳进 `tile_kernels/torch/<name>.py` 并经 `__init__` 导出；极简参考（一行能写完）可直接内联在测试里。

参考实现的价值在于**信任链**：我们不信 kernel（它是编译黑盒），我们信 PyTorch（成熟、可审计）；只要二者逐位一致，就反推出 kernel 正确。

#### 4.3.2 核心流程

写参考实现的标准动作：

```
1. 用 torch 算子表达与 kernel 相同的语义（注意 stable / 并列取小下标等细节）
2. 若复杂 → 放进 tile_kernels/torch/<name>.py，并在 torch/__init__.py 再导出
   若极简 → 直接内联在 test 里（如 y_ref = x.T.contiguous()）
3. 在测试里引入并 assert_equal(kernel_out, ref_out)
```

#### 4.3.3 源码精读

**收纳式参考**——`stable_topk`：

[torch/topk.py:L8-L10](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/topk.py#L8-L10) —— `stable_topk` 用 `torch.sort(stable=True)` 做稳定降序排序后取前 `num_topk` 列。关键是 `stable=True`：并列元素保持原顺序，这恰好对应 kernel 里「min reducer 取最小下标」的稳定语义（见 u5-l2）。**参考实现必须复刻 kernel 的并列处理规则**，否则对拍会在 tie 场景假阴性。

[torch/topk.py:L9](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/topk.py#L9) —— `_, sorted_indices = torch.sort(scores, dim=1, descending=True, stable=True)`，只要值、丢掉排序后的值、保留下标。

**内联式参考**——转置：

[test_transpose.py:L72](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py#L72) —— `y_ref = x.T.contiguous()`，转置的参考实现只有一行，没必要单独建文件，直接内联在测试体内。这就是「收纳 vs 内联」的取舍标准：**复杂度与复用度**。

> 同文件里 `top2_sum_gate`（[torch/topk.py:L22-L206](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/topk.py#L22-L206)）是一个 11 步的端到端路由流水线参考，因为太复杂、被多个 kernel 共用，所以必须收纳进文件并导出。

#### 4.3.4 代码实践

**实践目标**：为 4.2 写的 rowmax 算子配一份 torch 参考实现。

**操作步骤**：

1. 在练手文件里写 `def rowmax_ref(scores): return scores.max(dim=1).values`。
2. 思考：rowmax 有没有「并列」语义需要复刻？（取「值」而非「下标」，所以没有 tie 问题——这是 rowmax 比 topk_gate 更简单的根本原因。）
3. 判断它该收纳还是内联：因为只有一行，内联在测试里即可。

**预期结果**：你能说清「参考实现的复杂度决定它放哪」，并指出 rowmax 不需要处理 tie。

#### 4.3.5 小练习与答案

**练习 1**：如果 kernel 用了 min reducer 处理并列（取小下标），而参考实现用了普通 `torch.topk`（不稳定），对拍会发生什么？

> **答案**：在存在并列分数的输入上，kernel 与参考会给出不同的下标顺序，`assert_equal` 会报失败——但这是**假阴性**：kernel 其实是对的，只是参考实现的并列语义不一致。修复方法是参考实现改用 `torch.sort(stable=True)`，即 `stable_topk` 的做法。

**练习 2**：什么情况下参考实现「必须」收纳进 `tile_kernels/torch/` 而不能内联？

> **答案**：当参考实现 (a) 超过几行、内联会污染测试可读性，或 (b) 被多个测试文件/多个 kernel 共用时，必须收纳并经 `__init__` 导出。`top2_sum_gate` 同时满足这两条，故收纳；`x.T.contiguous()` 只有一行且只在一处用，故内联。

---

### 4.4 第三件：pytest 测试（正确性对拍 + benchmark）

#### 4.4.1 概念说明

每个算子的测试文件标配**两个用例**：

- **正确性用例**：参数化枚举形状 → 造数据 → 跑 kernel 与参考 → `assert_equal` 位精确判等。
- **benchmark 用例**：标 `@pytest.mark.benchmark` → `benchmark_timer` 计时 → `count_bytes` 算流量 → `bandwidth_gbs = num_bytes/t_us/1e3` → `benchmark_record` 落盘。

正确性用例默认每次 pytest 都跑；benchmark 用例默认跳过，需 `--run-benchmark` 显式开启（见 u1-l2、u9-l2）。

#### 4.4.2 核心流程

```
generate_test_params(is_benchmark)         ← 参数生成器：yield 参数 dict（可作 pytest id）
        │
        ▼
@pytest.mark.parametrize('params', ..., ids=make_param_id)
def test_xxx(params):
    data = generate_test_data(params)      ← 造输入张量
    out      = wrapper(data)               ← 被测
    out_ref  = torch_ref(data)             ← 参考
    assert_equal(out, out_ref)             ← 位精确判等

@pytest.mark.benchmark
@pytest.mark.parametrize('params', generate_test_params(is_benchmark=True), ...)
def test_xxx_benchmark(benchmark_timer, benchmark_record, params):
    data   = generate_test_data(params)
    out    = wrapper(data)
    t_us   = benchmark_timer(lambda: wrapper(data))     ← CUPTI 计时（rep=30，微秒）
    nbytes = count_bytes(data, out)                     ← 读+写字节
    benchmark_record(kernel=..., operation='fwd',
                     params=params, time_us=t_us,
                     bandwidth_gbs=nbytes/t_us/1e3)     ← GB/s，并写 JSONL
```

`make_param_id`（[bench.py:L108-L115](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/bench.py#L108-L115)）把参数 dict 格式化成可读的 pytest id（如 `num_tokens=4001-num_experts=72-num_topk=6`）。

#### 4.4.3 源码精读

**参数与数据生成**：

[test_topk_gate.py:L14-L25](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_topk_gate.py#L14-L25) —— `_EXPERT_CONFIGS` 枚举 `(num_experts, num_topk)` 组合，是算子专属的「业务参数表」。

[test_topk_gate.py:L35-L44](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_topk_gate.py#L35-L44) —— `generate_test_params` 把「token 数（来自通用 `generate_num_tokens`）× 业务参数表」做笛卡尔积。`is_benchmark` 旗标切换默认档/FULL 档（见 u9-l1）。

[test_topk_gate.py:L28-L32](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_topk_gate.py#L28-L32) —— `generate_test_data` 造 `randn` 输入。

**正确性用例**：

[test_topk_gate.py:L47-L54](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_topk_gate.py#L47-L54) —— 跑 `tile_kernels.moe.topk_gate` 与 `torch_stable_topk`，`assert_equal` 位精确判等。topk 输出是整数下标、无舍入，故用位精确而非 `calc_diff`。

**benchmark 用例**：

[test_topk_gate.py:L57-L75](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_topk_gate.py#L57-L75) —— 注意先跑一次 `topk_gate(scores, num_topk)` 触发编译（避免把编译时间计入计时），再用 `benchmark_timer(lambda: ...)` 计时；`count_bytes(scores, topk_idx)` 统计读 scores + 写 topk_idx；带宽 `bandwidth_gbs = num_bytes / t_us / 1e3`；最后 `benchmark_record` 打印 + 落盘 JSONL。

**对照样板——转置测试的额外技巧**：

[test_transpose.py:L14-L20](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py#L14-L20) —— `twice_stride` 故意构造**非连续（strided）**输入：分配双倍宽的张量再取前一半，使行步长翻倍。这用来验证 kernel（通过 `T.StridedTensor` + 运行时符号 `stride_x`）不依赖「输入连续」的隐藏假设。新算子若支持非连续输入，也应照此造例。

[test_transpose.py:L70-L74](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py#L70-L74) —— 转置正确性用例，`num_tokens == 0` 时直接 `return`（零规模守卫的测试侧呼应），否则 `assert_equal(y, x.T.contiguous())`。

#### 4.4.4 代码实践

**实践目标**：为 rowmax 算子写完整的测试文件（正确性 + benchmark）。

**操作步骤**：

1. 新建 `tests/moe/test_rowmax.py`（练手，不入库）。
2. 抄 `test_topk_gate.py` 结构：
   - 定义 `_EXPERT_CONFIGS`（rowmax 无 `num_topk`，只需 `num_experts` 列表）。
   - `generate_test_params` / `generate_test_data`（同 topk）。
   - 正确性用例：`assert_equal(rowmax(scores), scores.max(dim=1).values)`。注意 rowmax 输出是 float32，仍可用 `assert_equal`（无舍入）。
   - benchmark 用例：`count_bytes(scores, out)` + `benchmark_timer` + `benchmark_record(kernel='rowmax', ...)`。
3. 用 `pytest tests/moe/test_rowmax.py -n 4` 跑正确性；用 `pytest tests/moe/test_rowmax.py --run-benchmark` 跑基准。

**需要观察的现象**：

- 正确性用例应全绿（若你的 rowmax kernel 写对了）。
- benchmark 输出里 `bandwidth_gbs` 应接近「读一遍 scores 的理论带宽」（rowmax 是纯读算子）。

**预期结果**：你拿到一条 `time_us` 与 `bandwidth_gbs` 记录。

> 若本地无 SM90/SM100 GPU，编译/运行会失败——此时标注「待本地验证」，但测试代码本身应能通过静态检查。

#### 4.4.5 小练习与答案

**练习 1**：benchmark 用例里 `benchmark_timer(lambda: ...)` 之前为什么先「裸跑一次」`topk_gate(scores, num_topk)`？

> **答案**：第一次调用会触发 TileLang JIT 编译（可能耗时数百毫秒甚至秒级）。若把编译放进 `benchmark_timer` 的计时窗口，测出来的延迟会严重虚高。先裸跑一次让编译产物进入缓存，`benchmark_timer` 测到的才是纯执行时间。

**练习 2**：`count_bytes(scores, topk_idx)` 算出的字节数包含哪两部分？为什么用它除以时间能得到「有效带宽」？

> **答案**：包含读 `scores`（`num_tokens×num_experts×4` 字节）与写 `topk_idx`（`num_tokens×num_topk×8` 字节）。带宽 = 总流量 / 时间，衡量算子对显存带宽的利用率；对带宽受限算子（如 topk、转置），有效带宽越接近硬件峰值说明实现越好。

**练习 3**：为什么转置测试要用 `twice_stride` 造非连续输入，而 topk_gate 测试不用？

> **答案**：转置 kernel 显式支持 `T.StridedTensor`（输入可不连续），必须测这条路径；而 topk_gate 的 wrapper 用 `assert scores.is_contiguous()` 强制要求连续输入（[topk_gate_kernel.py:L77](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L77)），造非连续输入只会触发 assert 失败，没有测试价值。**测试要覆盖 kernel 声称支持的输入形态，而非所有形态。**

---

### 4.5（可选第五件）modeling 层 autograd 封装

#### 4.5.1 概念说明

四件套已经能让一个算子「跑得快、对得上、可验证」。但如果这个算子要参与 `loss.backward()`（如 engram 门控、mhc 归一化），它还缺一件：**用 `torch.autograd.Function` 把黑盒 kernel 包成可微算子**。

这是「可选」的：transpose、topk_gate 这类推理/路由算子不需要反向，四件套就够；engram/mhc 这类训练算子才需要第五件。是否需要，取决于算子是否出现在反向图中。

modeling 层只做一件事：**可微封装，不写算子逻辑**（u8-l1 的分层原则）。

#### 4.5.2 核心流程

```
class MyFn(torch.autograd.Function):
    @staticmethod
    def forward(ctx, ...输入):
        ctx.save_for_backward(...要复用的中间量)   ← 张量走 save_for_backward
        ctx.eps = eps                                ← 非张量挂 ctx 属性
        out, 中间量 = wrapper(...)                   ← 调第一件的 wrapper
        return out
    @staticmethod
    def backward(ctx, grad_out):
        ..., 中间量 = ctx.saved_tensors              ← 重放中间量（非重算）
        grad_in = bwd_wrapper(grad_out, 中间量, ...) ← 调反向 kernel
        return (grad_in, ..., None, None)            ← 必须与 forward 输入逐位对应

# 调用入口
out = MyFn.apply(...)
```

两条铁律（u8-l1）：

1. **`backward` 返回元组必须与 `forward` 输入（除 ctx）逐位一一对应**，非张量输入用 `None` 占位。
2. **重放而非重算**：从 `ctx.saved_tensors` 复用前向中间量喂给反向 kernel，不重复计算。

#### 4.5.3 源码精读

`EngramGateFn` 是项目里最完整的 modeling 封装样板：

[engram_gate.py:L6](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L6) —— `class EngramGateFn(torch.autograd.Function)`，标准的 autograd.Function 子类。

[engram_gate.py:L36-L48](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L36-L48) —— `forward(ctx, hidden_states, k, v, weight_hidden, weight_embed, clamp_value, eps)`：7 个输入（5 张量 + 2 标量）。

[engram_gate.py:L49](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L49) —— `ctx.save_for_backward(...)` 保存前向中间量（dot/gate_score/rstd_x/rstd_k），供反向重放。

[engram_gate.py:L58](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L58) —— `backward(ctx, grad_output)`，返回的梯度元组逐位对应 forward 的 7 个输入（标量 `clamp_value`、`eps` 对应 `None`）。

> 这里的反向 kernel（`engram_gate_bwd`）与权重梯度归约（`grad_w_reduce`）属于 engram 家族的「第二件 kernel」，已在 u6-l2 讲过。对建模封装而言，它们只是 wrapper 调用的目标。

#### 4.5.4 代码实践

**实践目标**：为 transpose 写一个 autograd.Function（**示例代码**，演示用）。

**操作步骤**：

1. 转置的导数仍是转置：\(Y = X^\top \Rightarrow \nabla_X = (\nabla_Y)^\top \)。
2. 照 `EngramGateFn` 结构写（不入库）：

```python
# 示例代码：仅演示封装骨架，非项目原有文件
class TransposeFn(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        ctx.input_shape = x.shape          # 非张量挂 ctx 属性
        return tile_kernels.transpose.transpose(x)
    @staticmethod
    def backward(ctx, grad_y):
        # 转置的反向 = 再转置一次
        grad_x = tile_kernels.transpose.transpose(grad_y)
        return grad_x                        # forward 只有 1 个输入，返回 1 个梯度

# 使用
y = TransposeFn.apply(x)
```

**需要观察的现象**：对一个依赖 `y.sum()` 的 loss 做 `loss.backward()`，`x.grad` 应等于全 1 矩阵的转置（即全 1）。

**预期结果**：你理解了「forward/backward 一一对应」契约，并知道 transpose 这种自对偶算子反向可以复用前向 kernel。

#### 4.5.5 小练习与答案

**练习 1**：如果一个算子只用于推理（永不出现在 `backward` 图里），还需要写第五件吗？

> **答案**：不需要。第五件的存在唯一理由是「参与 autograd」。topk_gate、group_count、expand_to_fused 等路由/派发算子只在推理/路由阶段用，故项目没有给它们写 modeling 封装。判断标准是「算子是否需要被 `loss.backward()` 追踪」。

**练习 2**：`backward` 返回元组里为什么对 `clamp_value`、`eps` 这种标量输入返回 `None`？

> **答案**：autograd 要求 `backward` 的返回与 `forward` 输入逐位一一对应。标量（如超参数 `eps`）不是可微张量、没有梯度概念，但位置必须占住，故返回 `None`。漏掉会导致 `number of gradients does not match number of inputs` 报错。

---

## 5. 综合实践

把四件套（+可选第五件）完整地走一遍：**给 TileKernels 新增一个 RMSNorm 算子**（或 rowmax，任选其一）。

RMSNorm 的数学定义（u6-l1 已接触过其 engram 变体）：

\[
\text{rms} = \sqrt{\frac{1}{H}\sum_{i=1}^{H} x_i^2 + \epsilon}, \qquad y_i = \frac{x_i}{\text{rms}} \cdot w_i
\]

**任务清单**（严格按四件套顺序）：

1. **第一件 kernel + wrapper**：
   - 新建 `tile_kernels/<自选家族>/rmsnorm_kernel.py`。
   - `@tilelang.jit` 构造器 `get_rmsnorm_kernel(hidden_size, dtype)`；`hidden_size` 编译期、`num_tokens` 用 `T.dynamic`。
   - prim_func 内：load → `T.reduce_sum`（或平方和规约）算 rms → 除法 + 乘权重 → store。
   - wrapper `rmsnorm(x, weight, eps)` 四步：校验、分配输出、编译、启动。
   - 在家族 `__init__.py` 加一行再导出。

2. **第二件 torch 参考**：
   - 极简参考可内联（`rms = (x.float().pow(2).mean(-1, keepdim=True) + eps).rsqrt(); y = (x * rms).to(x.dtype) * weight`），也可收纳进 `tile_kernels/torch/`。

3. **第三件 测试**：
   - `tests/<家族>/test_rmsnorm.py`，抄 `test_topk_gate.py` 结构。
   - 正确性用例：注意 RMSNorm 有舍入，**应改用 `calc_diff` 而非 `assert_equal`**（u9-l1：有舍入算子用浮点相似度）。
   - benchmark 用例：`count_bytes(x, weight, y)` + `benchmark_record(kernel='rmsnorm', ...)`。

4. **跑通并报告**：
   - `pytest tests/.../test_rmsnorm.py -n 4` 跑正确性，记录是否全绿。
   - `pytest tests/.../test_rmsnorm.py --run-benchmark` 跑基准，记录 `time_us` 与 `bandwidth_gbs`。
   - 若本地无目标 GPU，标注「待本地验证」，但确保代码能通过 `python -c "import ..."` 静态导入检查。

5. **（可选）第五件 modeling 封装**：若你想让 RMSNorm 可训练，照 `EngramGateFn` 写一个 `RMSNormFn(torch.autograd.Function)`，反向需要保存 rms 或 x（重计算策略见 u7-l3）。

**交付物**：一份带宽/延迟数据 + 一段「我复用了哪些既有设施（generator / assert_equal / calc_diff / benchmark_timer / count_bytes / make_param_id）」的说明。

> 这是本套手册的最终检验：如果你能独立完成它，说明你已经具备「读懂 TileKernels 并向它贡献新算子」的完整能力。

## 6. 本讲小结

- **四件套** = TileLang kernel + Python wrapper + torch 参考实现 + pytest 测试（正确性 + benchmark）；kernel 与 wrapper 合住一个 `*_kernel.py`，参考住 `torch/`，测试住 `tests/`。
- **wrapper 固定四步**：校验 → 分配输出 → 触发/命中 JIT 编译 → 按 prim_func 形参顺序启动；零规模守卫也在 wrapper。
- **参考实现是信任链**：纯 PyTorch、复刻 kernel 全部边界语义（含并列/稳定排序），复杂则收纳进 `torch/` 并经 `__init__` 导出，极简则内联在测试里。
- **测试标配两例**：正确性用 `assert_equal`（无舍入/整数）或 `calc_diff`（有舍入）；benchmark 用 `benchmark_timer` + `count_bytes` + `bandwidth_gbs = num_bytes/t_us/1e3` + `benchmark_record`，计时前务必先裸跑一次触发编译。
- **第五件可选**：仅当算子要参与 `loss.backward()` 时才写 `torch.autograd.Function` 封装；`backward` 返回元组必须与 `forward` 输入逐位对应，重放而非重算中间量。
- **导出枢纽**：两份实现各自经家族 `__init__` / `torch/__init__` 再导出，测试把它们拉到一起对拍——这是「用户入口」与「标准答案」能够相遇的物理基础。

## 7. 下一步学习建议

本讲已是手册收官，没有「下一讲」。建议你沿以下方向继续深耕：

1. **动手做综合实践**：把第 5 节的 RMSNorm（或自选算子）真正落到代码并提一个 PR 草稿，走通四件套的完整闭环。
2. **精读一个「大」算子**：从 `topk_gate` 这种几十行的样板，升级到读 `engram_gate_kernel.py` 或 `sinkhorn_kernel.py` 这类带持久化调度 / 自定义反向的复杂算子，体会四件套约定在复杂场景下如何延展（第五件 modeling 封装会成为刚需）。
3. **横向对比 Triton / cuBLAS**：挑一个算子（如转置或 per-token cast）用 Triton 重写一份，对比 TileLang DSL 的表达力与生成 CUDA 的质量，加深对「DSL 换生产力」的理解。
4. **回到源码地图**：以本讲的「职责 → 文件 → 符号」视角重新通读 `tile_kernels/` 各家族，你会发现整个项目其实就是「几十个四件套」的有序集合——这时你已经从「读一个算子」进阶到「读懂整个仓库的架构」。
