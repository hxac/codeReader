# u5-l2 MXFP4 量化权重与 scale 重排

## 1. 本讲目标

上一讲（u5-l1）我们读懂了预取内核 `PrefetchKernel` 的流水线本体，但刻意绕开了两个函数：`retile_for_prefetch` 和 `prefetch_retile_nbytes`，并且对「bf16 权重」一笔带过。本讲把这两个补丁补上，专门回答三个问题：

1. **类型映射**：`_ELEM_TYPES` 这张三行的小表如何让同一个内核既搬 bf16、又搬 int8/uint8？`elem_bytes`（每个元素的字节数）在内核里有哪几个去处？
2. **量化布局**：MXFP4（e2m1 每字节打包两个 4-bit 值）与它的 ue8m0 块缩放因子（block scale）张量长什么样？为什么它们能和 bf16 走完全相同的预取路径？
3. **scale 重排**：scale 张量的自然尾维是 \(K/32\)，几乎不可能是 128 的倍数，为什么？`retile_for_prefetch` 如何在不搬动任何字节的前提下把它变成 `[N, 128, X]`？`H`、`H'` 必须是 128 倍数的约束到底从哪来？

读完本讲，你应该能对一个真实模型（如 3584×3072 的 MoE 专家）独立推导出它的全部 MXFP4 张量形状、retile 后的形状、每个专家占多少 tile，并判断哪些维度需要 padding。

## 2. 前置知识

### 2.1 块缩放量化：FP4 e2m1 与 ue8m0

大模型推理/训练常用 **MX（microscaling）格式**量化权重来省显存、省带宽。MoonEP 预取路径支持的是其中最常用的组合：

- **e2m1（FP4）**：4 个 bit —— 1 符号位 + 2 指数位 + 1 尾数位，幅度只能取 \(0, 0.5, 1, 1.5, 2, 3, 4, 6\) 八个值。**两个 4-bit 值打包进一个字节**（一个占低半字节、一个占高半字节），所以一个 `[rows, K]` 的权重矩阵打包后是 `[rows, K/2]` 的 uint8 张量。
- **ue8m0（block scale）**：8 个 bit 的纯指数（无符号、无尾数），只能表示 2 的幂。MX 规范规定**每 32 个连续元素共享一个 scale 字节**，因此 `[rows, K]` 矩阵的 scale 张量是 `[rows, K/32]` 的 uint8。

每个权重点数的平均字节数：

\[
\underbrace{\tfrac{1}{2}}_{\text{e2m1 数据}} + \underbrace{\tfrac{1}{32}}_{\text{ue8m0 scale}} = \tfrac{17}{32} \approx 0.53 \ \text{字节/值}
\]

对比 bf16 的 2 字节/值，压缩比是 \(2 \div \tfrac{17}{32} = \tfrac{64}{17} \approx 3.76\)。预取走 NVLink 读远程显存，权重小 3.76 倍意味着同样 B 个槽的预取流量也小 3.76 倍——这就是 MoonEP 支持量化预取的直接动机（这套支持由 HEAD 提交 `2bd860b` "Support MXFP4 expert weights in remote prefetch" 引入）。

### 2.2 回顾 u5-l1 的关键结论

- 预取内核把任何 `[E, H, H']` 张量**按字节搬运**：3D 张量被看作 2D 行主序矩阵 `[E*H, H']`，以固定 128×128 的 tile 用 2D TMA 复制；它不关心元素语义。
- `launch_prefetch` 是契约守门人：dtype 白名单、连续性、`H % 128 == 0 and H' % 128 == 0`、空槽（`experts_to_copy == -1`）不写。
- 权重缓冲布局：`[E+B, H, H']`，前 E 行是经对称内存映射的全组专家，后 B 行是本地预取槽。

### 2.3 PyTorch 的视图语义（本讲工具箱）

- `t.view(torch.uint8)`：**不搬数据**，把张量按字节重新解释（例如最后一维是 bf16 时字节维度翻倍；本来就是 uint8 时是恒等）。
- `t.reshape(...)`：在张量连续时也只是视图（view），`data_ptr` 不变。
- 因此「改变形状」可以完全不等于「改变存储」。`retile_for_prefetch` 正是只靠这两个操作完成的**零拷贝重排**。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [moonep/prefetch.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py) | 主角：`_ELEM_TYPES` 映射表、`elem_bytes` 的消费链、`retile_for_prefetch` / `prefetch_retile_nbytes`、128 约束的出处 |
| [moonep/api.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py) | 集成点：`Buffer.prefetch_weight` 的 scale 参数契约、`_launch_full_weight_prefetches` 如何把 scale 接到同一条路径 |
| [tests/test_prefetch.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_prefetch.py) | 四个量化用例（打包 gate/up/down + 已 retile 的 scale），是形状契约的「可执行文档」 |
| [benchmarks/bench_prefetch.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_prefetch.py) | `derive_extents`：一个 MoE 层的 MXFP4 六个张量如何从 `(H, Hp)` 推导出来 |

## 4. 核心概念与源码讲解

### 4.1 元素类型映射：`_ELEM_TYPES` 与字节宽度的三个去处

#### 4.1.1 概念说明

预取内核做的是「把远端一段字节搬进本地槽位」，值是什么格式它并不关心。但 CuTe DSL 编译内核时必须知道指针指向的类型；共享内存预算、mbarrier 的事务计数也依赖**每元素字节数**。于是 `moonep/prefetch.py` 用一张小表把 PyTorch dtype 翻译成 (CuTe 类型, 字节数) 二元组：

```python
# 示例代码：摘自源码
_ELEM_TYPES = {
    torch.bfloat16: (BFloat16, 2),
    torch.int8:     (Int8, 1),
    torch.uint8:    (Uint8, 1),
}
```

这张表有三重身份：

1. **白名单**：不在表里的 dtype 会在 `launch_prefetch` 的断言处被直接拒绝；
2. **编译期类型**：`(elem_ty, elem_bytes)` 传入 `PrefetchKernel`，参与 CuTe DSL 的类型特化（不同 dtype 编译出不同的 cubin，`lru_cache` 按 dtype 分开缓存）；
3. **字节宽度来源**：`elem_bytes` 决定一个 128×128 tile 占多少共享内存、TMA 完成时要向 mbarrier 报告多少字节。

#### 4.1.2 核心流程

从用户传入张量到内核吃进参数，dtype 信息流如下：

```text
launch_prefetch(remote_expert, ...)
  │  断言 dtype ∈ _ELEM_TYPES           （白名单）
  ▼
_get_compiled(E, H, Hp, B, num_sms, device, torch_dtype)   [lru_cache]
  │  elem_ty, elem_bytes = _ELEM_TYPES[torch_dtype]        （查表）
  │  PrefetchKernel(..., elem_ty, elem_bytes)
  │     ├─ _smem_bytes: tile_bytes = 128×128×elem_bytes    （去处①：smem 预算/深度）
  │     └─ TILE_BYTES = TILE_ELEMS × elem_bytes
  │              └─ tx_count = TILE_BYTES                  （去处②：mbarrier 事务计数）
  │  make_ptr(elem_ty, ..., assumed_align=16)              （去处③：指针类型/对齐）
  ▼
cute.compile(...) → 按 dtype 特化的 cubin
```

关键理解：**类型只影响「每个元素几字节」，不影响搬运逻辑**。bf16 的 tile 是 128×128×2 = 32,768 字节，uint8（MXFP4 打包值或 scale）是 16,384 字节；流水线的 stage 数量按设备 smem 预算从 6 往下试（`_pick_stages`），字节宽度减半意味着同样的预算能容纳同样的 stage 深度还有大量富余——量化权重因此在 smem 紧张的设备上也更从容。

#### 4.1.3 源码精读

- [moonep/prefetch.py:L32-L36](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L32-L36)：`_ELEM_TYPES` 登记表本体——bf16/int8/uint8 三项；`torch.dtype → (cutlass 类型, 字节数)`。
- [moonep/prefetch.py:L378-L382](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L378-L382)：`launch_prefetch` 的 dtype 白名单断言，报错信息直接列出支持集合。
- [moonep/prefetch.py:L299-L332](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L299-L332)：`_get_compiled`（`lru_cache`）——第 309 行查表取 `(elem_ty, elem_bytes)`，第 311–320 行把它们传给 `PrefetchKernel` 构造器；注意 `torch_dtype` 是缓存键的一员，同一形状不同 dtype 会得到不同的编译产物。
- [moonep/prefetch.py:L74-L84](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L74-L84)：`_smem_bytes`——`tile_bytes = M_BLOCK * N_BLOCK * self.elem_bytes`（第 78 行），加上 mbarrier、紧凑表和固定余量后按 stage 数求和；这是字节宽度的去处①。
- [moonep/prefetch.py:L167-L168](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L167-L168) 与 [moonep/prefetch.py:L208-L214](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L208-L214)：`TILE_BYTES = TILE_ELEMS * elem_bytes`，并作为 `PipelineTmaAsync.create(..., tx_count=TILE_BYTES)` 的事务计数——G2S 的 TMA 完成时硬件把实际传输的**字节数**累加到 mbarrier，消费者等满 `tx_count` 字节才认为 stage 就绪；这是去处②。若 dtype 换成 1 字节而 `tx_count` 仍按 2 字节计算，流水线会永远等不齐字节而挂死。
- [moonep/prefetch.py:L413-L431](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L413-L431)：`launch_prefetch` 尾部用 `make_ptr(elem_ty, data_ptr(), ..., assumed_align=16)` 构造 CuTe 指针——去处③：指针类型让 TMA 描述符按该元素尺寸切块，16 字节对齐假设则与 128×128 tile 的整块性匹配。

#### 4.1.4 代码实践：字节宽度如何决定流水线深度

本实践只需要纸笔/纯 Python 算术，不需要 GPU。目标：亲手复算 `_smem_bytes` / `_pick_stages`，体会 `elem_bytes` 的作用。

1. **实践目标**：对 bf16 与 uint8 两种 dtype，在两个假设的 smem 预算下求出 `_pick_stages` 的选择，验证「量化 tile 减半 ⇒ 更容易保住 6 级流水」。
2. **操作步骤**：把下面的脚本存为 `smem_arithmetic.py` 运行（示例代码，复刻 [moonep/prefetch.py:L74-L90](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L74-L90) 的公式）：

   ```python
   # 示例代码：复刻 PrefetchKernel._smem_bytes / _pick_stages 的算术
   M_BLOCK = N_BLOCK = 128
   def round_up(n, a): return (n + a - 1) // a * a

   def smem_bytes(stages, elem_bytes, B):
       tile = M_BLOCK * N_BLOCK * elem_bytes
       return (round_up(stages * tile, 128) + round_up(stages * 2 * 8, 16)
               + round_up(2 * B * 4, 16) + 256)

   def pick_stages(budget, elem_bytes, B):
       for s in (6, 5, 4, 3, 2):
           if smem_bytes(s, elem_bytes, B) <= budget:
               return s
       return 0

   B = 32
   for label, budget in [("budget_A(≈H100级)", 231_424),
                         ("budget_B(≈A100级)", 166_912)]:
       for dt, eb in [("bf16", 2), ("uint8(MXFP4)", 1)]:
           s = pick_stages(budget, eb, B)
           print(f"{label:18s} {dt:14s} -> stages={s}, "
                 f"tile={128*128*eb}B, smem={smem_bytes(s, eb, B)}B")
   ```

3. **需要观察的现象**：预算充足时（budget_A）两种 dtype 都选 6；预算收紧（budget_B）时 bf16 掉到 5 而 uint8 仍是 6。
4. **预期结果**（手工推演）：
   - budget_A：bf16 `6×32768=196608` + 96 + 256 + 256 = 197,216 ≤ 231,424 → 6；uint8 `6×16384=98304` + 96 + 256 + 256 = 98,912 → 6。
   - budget_B：bf16 stages=6 需 197,216 > 166,912 ✗，stages=5 需 163,840 + 96 + 256 + 256 = 164,448 ≤ 166,912 → 5；uint8 仍 6（98,912）。
   - 真机上预算来自 `_max_smem_per_block_optin`（见 [moonep/prefetch.py:L294-L296](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L294-L296)）再减 1024 字节（[moonep/prefetch.py:L310](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L310)）。
5. 本环境无法运行 Python，以上输出为按源码公式手工推演——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`_ELEM_TYPES` 里为什么没有 float32？给 fp32 权重做预取缺什么？
**答案**：不是技术障碍，而是白名单还没登记——fp32 权重体量大、场景少（梯度走的是 `grad_reduce`，那边的缓冲本来就强制 fp32）。要支持只需加一行 `torch.float32: (Float32, 4)` 并确认 16 字节对齐假设成立（连续分配天然满足）。这正是 u6-l6 扩展练习的模板。

**练习 2**：如果把 `tx_count` 写死成 `128*128*2`，uint8 路径会发生什么？
**答案**：uint8 的 tile 只传 16,384 字节，mbarrier 却在等 32,768 字节的事务计数，永远凑不齐——消费者在 `consumer_wait` 处挂死。`tx_count` 必须与 dtype 的实际传输字节数一致，这就是它作为 `elem_bytes` 去处②的原因。

**练习 3**：`_get_compiled` 的 `lru_cache` 键里有 `torch_dtype`，为什么？
**答案**：dtype 决定 `elem_ty`/`elem_bytes`，进而决定 TMA 描述符的元素尺寸、`tx_count` 和 smem 布局——这些都被 `cute.compile` 烧进 cubin。不同 dtype 是不同的编译产物，必须分开缓存。

### 4.2 MXFP4 权重与 ue8m0 scale 的形状契约

#### 4.2.1 概念说明

一个 MoE 专家层有三个投影：gate、up（都是 `[I, H]`，I 为中间维）、down（`[H, I]`）。MXFP4 量化后每个投影变成**两个张量**：

| 张量 | 形状（单个专家） | dtype | 来源 |
| --- | --- | --- | --- |
| 打包权重 | `[rows, K/2]`（K 为该矩阵的收缩维） | uint8 | 两个 e2m1 值打包一字节 |
| 块 scale | `[rows, K/32]` | uint8 | 每 32 个值共享一个 ue8m0 字节 |

MoonEP 侧的约定（写在 `Buffer.prefetch_weight` 的文档里）：

- 打包权重沿用 `[E+B, H, H']` 三维契约，只是 dtype 换成 uint8，且 **H' = K/2**（「e2m1 packs two values per byte, so H' is K/2」）；
- scale 是**可选的第二个三元组** `full_gate_scale / full_up_scale / full_down_scale`：`[E+B, ...]` 连续张量、同样的「前 E 行源 + 后 B 行槽」行约定，量化专家必须提供、bf16 专家省略。

由于内核按字节搬运（4.1），打包权重与 scale **不需要任何专门的内核分支**——它们和 bf16 走完全相同的 2D TMA 流水线，唯一的差别是形状与 `elem_bytes=1`。

#### 4.2.2 核心流程

`Buffer.prefetch_weight(...)` 一次调用的内部展开：

```text
用户调用 prefetch_weight(plan, full_gate_weight=…, full_gate_scale=…, …)
  │ 断言：三个 weight 一起给、dtype ∈ _ELEM_TYPES、[E+B] 行、rank-3
  │ 断言：三个 scale 一起给（或一起不给）、连续、rank ≥ 2、[E+B] 行
  ▼
_launch_full_weight_prefetches(ctx, gate, up, down, experts_to_copy, scales)
  │ for full_weight in (gate, up, down):        ← 打包权重：直接进
  │     launch_prefetch(full_weight[:E], full_weight[E:], experts_to_copy, …)
  │ for full_scale in scales:                   ← scale：先重排再进（4.3 的主角）
  │     tiled = retile_for_prefetch(full_scale)
  │     launch_prefetch(tiled[:E], tiled[E:], experts_to_copy, …)
```

即：**一层六次内核发射**（三个打包权重直通，三个 scale 先 retile），全部共用同一份 `experts_to_copy` 计划与同一个编译好的 uint8 内核实例（形状相同则命中同一个 `lru_cache` 条目）。

#### 4.2.3 源码精读

- [moonep/api.py:L860-L895](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L860-L895)：`prefetch_weight` 签名与文档——权重「bf16 for unquantized experts, uint8 for MXFP4 (e2m1 packs two values per byte, so H' is K/2)」、scale「[E+B, ...]，量化必给、bf16 省略」。文档还说明它与 dispatch 分离是为了 plan 复用路径能跳过重复预取。
- [moonep/api.py:L903-L917](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L903-L917)：断言块——权重三元组同给、dtype 白名单（复用 `_ELEM_TYPES`，import 见 [moonep/api.py:L71](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L71)）、`ndim == 3` 且首维 `E+B`；scale 三元组同给、`ndim >= 2` 且首维 `E+B`。注意 scale 允许更高维（retile 前的自然形状可以是 `[E+B, rows, K/32]`）。
- [moonep/api.py:L158-L182](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L158-L182)：`_launch_full_weight_prefetches`——第 168–174 行三个打包权重循环直发；第 175–182 行 scale 循环 `retile_for_prefetch(full_scale)` 后按同样的 `[:E]` / `[E:]` 切分进 `launch_prefetch`。
- [tests/test_prefetch.py:L126-L148](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_prefetch.py#L126-L148)：打包权重用例——`mxfp4_packed_k3_gate_up` 用 `H=2*3072=6144, Hp=3584//2=1792`（gate+up 沿行拼接成 `[2I, H/2]`）；`mxfp4_packed_k3_down` 用 `H=3584, Hp=3072//2=1536`；dtype 均为 uint8。
- [tests/test_prefetch.py:L149-L171](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_prefetch.py#L149-L171)：**已 retile 的 scale** 用例——`H=128`，`Hp = 2*3072*3584//32//128 = 5376`（gate+up 合并）与 `Hp = 3584*3072//32//128 = 2688`（down）。这两个用例证明：只要张量以 retile 布局分配，scale 与打包权重在内核眼里毫无区别。
- [tests/test_prefetch.py:L238-L241](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_prefetch.py#L238-L241)：`_sentinel_for`——uint8 的哨兵是 `0xAB`，空槽事后逐位不变（u5-l1 讲过的「精确写」语义在量化路径同样成立）。
- [benchmarks/bench_prefetch.py:L28-L35](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_prefetch.py#L28-L35)：`K3_MXFP4_LAYER` 把一个 K3 层拆成 6 个 part（3 个 `pack: 2` 的打包投影 + 3 个 `scale: True` 的 scale），配合 [benchmarks/bench_prefetch.py:L94-L96](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_prefetch.py#L94-L96) 的 `k3_layer_mxfp4` 用例整体计时。

#### 4.2.4 代码实践：推导一个 K3 层的六个张量

1. **实践目标**：给定模型超参，独立写出 MXFP4 层每个张量的自然形状，并与测试/基准中的真实数字对上。
2. **操作步骤**：存为 `k3_shapes.py`（示例代码，纯算术；`derive_extents` 复刻自 [benchmarks/bench_prefetch.py:L100-L106](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_prefetch.py#L100-L106)）：

   ```python
   # 示例代码：K3 专家形状推导（H=3584, I=3072）
   H, I = 3584, 3072
   def extents(rows, contracted, pack=1, scale=False):
       if scale:
           nbytes = rows * contracted // 32          # 每 32 值一个 ue8m0 字节
           return 128, nbytes // 128                 # re-cut 成整 128x128 tile
       return rows, contracted // pack               # e2m1 两值一字节 -> K/2

   parts = [
       ("gate packed", extents(I, H, pack=2)),        # [I, H/2]
       ("up   packed", extents(I, H, pack=2)),
       ("down packed", extents(H, I, pack=2)),        # [H, I/2]
       ("gate scale",  extents(H, I, scale=True)),
       ("up   scale",  extents(H, I, scale=True)),
       ("down scale",  extents(H, I, scale=True)),
   ]
   TILE = 128 * 128
   for name, (r, c) in parts:
       assert r % 128 == 0 and c % 128 == 0, name
       print(f"{name:14s} shape=({r}, {c})  bytes/expert={r*c:>8,}  tiles={r*c//TILE}")
   ```

3. **需要观察的现象**：六个形状全部通过 128 整除断言；三个打包投影的 `bytes/expert` 与 `tiles` 完全相同。
4. **预期结果**（手工推演）：
   - gate/up packed `(3072, 1792)`、down packed `(3584, 1536)`，三者都是 5,505,024 字节/专家 = **336 tiles**（\(I \times H/2 = H \times I/2\)，乘法交换律的巧合）；
   - scale `(128, 2688)`，344,064 字节/专家 = **21 tiles**；
   - 对照 [tests/test_prefetch.py:L151-L171](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_prefetch.py#L151-L171) 的 5376/2688，数字吻合（5376 是 gate+up 合并的翻倍）。
5. 本环境无法运行 Python——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：某专家 `H=7168, I=2048`，写出门（gate）投影打包权重与 scale 的自然形状及 retile 后 scale 的形状。
**答案**：打包 `[2048, 3584]` uint8（H/2=3584）；scale 每 expert 字节数 \(2048 \times 7168/32 = 458{,}752 = 28 \times 16{,}384\)，retile 成 `[128, 3584]`（X=3584 恰是 128 的 28 倍）。整层 down 打包 `[7168, 1024]`。

**练习 2**：为什么 scale 的断言是 `ndim >= 2` 而权重是 `ndim == 3`？
**答案**：权重直接进内核，必须已是内核要求的 rank-3 `[E+B, H, H']`；scale 会先被 `retile_for_prefetch` 重排，进内核前自然形状可以是任意 `[E+B, ...]`（如 `[E+B, rows, K/32]`），所以只约束首维。真正的形状约束由 `retile_for_prefetch` 自己的字节断言把关（见 4.3）。

**练习 3**：预取一个量化专家比 bf16 专家省多少 NVLink 读流量？
**答案**：每值字节从 2 降到 17/32，流量比 1/3.76；等价地，同样带宽下单位时间能多预取约 3.76 个专家，这正是量化对「预取带宽优化」的放大作用。

### 4.3 scale 重排助手：`retile_for_prefetch` 与 `prefetch_retile_nbytes`

#### 4.3.1 概念说明

现在回答本讲最初的问题：**为什么 scale 不能直接进内核？**

内核的 tile 切分是「无边界处理」的均匀切分（u5-l1）：`MTILES = H // M_BLOCK`、`NTILES = Hp // N_BLOCK` 直接整除，TMA 描述符按固定 128×128 box 构建，`launch_prefetch` 也用断言 `H % 128 == 0 and Hp % 128 == 0` 把关。对打包权重这不成问题——`K/2` 通常仍是 128 的倍数（K 是 256 的倍数即可）。但 scale 的自然尾维是 \(K/32\)：K=3584 时是 **112**，K=3072 时是 **96**——都是 128 的非倍数，而且**这个维度没法靠 padding 行来解决**：行主序布局里每行的字节数（112）卡在中间，加行不改行宽。

MoonEP 的解法漂亮在「不搬任何字节」：既然内核本质上是把**每个专家的连续字节区间**当作 `[H, H']` 的行主序矩阵切块搬运，那么只要把这个字节区间**换一种切块解释**——重新切成 `[128, X]`，其中 \(X = \text{per\_expert\_bytes}/128\)——内核看到的就是合法形状，而存储里的字节一位都没动。消费侧（量化 GEMM 内核）按同样的 retile 布局解释 scale 即可。

这就是 `retile_for_prefetch`：`[N, ...] → view(uint8) → reshape(N, 128, -1)`，全程视图操作，零拷贝。

#### 4.3.2 核心流程

设 scale 张量 \(t\) 有 \(N\) 行（\(N = E+B\)），每行连续：

\[
\text{per\_expert} = \frac{t.\text{nbytes}}{N} = \text{rows} \times \frac{K}{32} \ \text{字节}
\]

重排与合法性条件：

\[
t \in [N, \text{rows}, K/32] \ \xrightarrow{\text{view(uint8)}} \ [N, \text{rows} \times K/32\text{ 字节}] \ \xrightarrow{\text{reshape}} \ [N, 128, X], \quad X = \frac{\text{per\_expert}}{128}
\]

合法性要求 retile 出的两个维度都是 128 的倍数：

\[
X \bmod 128 = 0 \iff \text{per\_expert} \bmod (128 \times 128) = 0 \iff \text{per\_expert} \bmod 16{,}384 = 0
\]

这正是源码断言的条件（`per_expert % tile == 0`，`tile = M_BLOCK * N_BLOCK = 16384`）。若不满足，`prefetch_retile_nbytes` 告诉你分配时每专家应该多给多少 padding 字节：

\[
\text{alloc\_bytes} = \left\lceil \frac{\text{per\_expert}}{16{,}384} \right\rceil \times 16{,}384
\]

多出来的 padding 字节永远不被写入（空槽不写 + tile 整块对齐，语义上安全）。

**为什么不能用 `transpose`/`permute` 之类的重排？** 因为那会产生非连续视图（stride ≠ 1），`reshape` 要么失败要么触发隐式数据拷贝、改变字节序——而 TMA 搬运和 GEMM 消费都依赖「每专家一段连续字节」这一事实。retile 的本质不是重排数据，而是**同一字节区间的另一种切块解释**。

#### 4.3.3 源码精读

- [moonep/prefetch.py:L11-L15](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L11-L15)：模块文档点明设计——tile 固定 128×128，H/H' 必须是 128 的倍数；「For a scale tensor whose natural trailing extent is K/32 (never a multiple of 128), re-tile its contiguous per-expert byte range to `[128, nbytes // 128]` before calling in」。
- [moonep/prefetch.py:L42-L46](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L42-L46)：`M_BLOCK = N_BLOCK = 128` 类常量——约束的源头在此（首个实现的固定 tile；模块文档称 "first implementation"）。
- [moonep/prefetch.py:L169-L171](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L169-L171)：`MTILES = H // M_BLOCK`、`NTILES = Hp // N_BLOCK`、`TILES_PER_EXPERT`——整除切分、无边界 tile，128 约束在内核侧的根据。
- [moonep/prefetch.py:L401-L402](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L401-L402)：`launch_prefetch` 的宿主侧断言 `H % 128 == 0 and Hp % 128 == 0`——约束被前移到入口，错误信息直接给出两个常量值。
- [moonep/prefetch.py:L335-L339](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L335-L339)：`prefetch_retile_nbytes`——把 per-expert 字节数向上取整到 16,384 的倍数，供**分配器**决定 scale 张量该开多大。
- [moonep/prefetch.py:L342-L354](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L342-L354)：`retile_for_prefetch` 本体——断言连续；`per_expert = t.nbytes // n`；断言 `per_expert % 16384 == 0`（错误信息会直接建议 `allocate {prefetch_retile_nbytes(per_expert)} bytes`）；返回 `t.view(torch.uint8).reshape(n, 128, -1)`。三行核心逻辑，零数据搬运。
- [moonep/api.py:L175-L182](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L175-L182)：`_launch_full_weight_prefetches` 中的调用点——`tiled = retile_for_prefetch(full_scale)` 后 `tiled[:E]`（源）/`tiled[E:]`（槽）进内核，说明用户分配的 `full_*_scale` 本身必须按「可 retile」的形状开出来。
- [benchmarks/bench_prefetch.py:L100-L106](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_prefetch.py#L100-L106)：`derive_extents` 的 scale 分支 `return 128, nbytes // 128`——基准与测试直接按 retile 后的形状分配，等价于「先想好 retile 布局再分配」。
- [benchmarks/bench_prefetch.py:L19-L21](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_prefetch.py#L19-L21)：注释「3583 padded up to a 128 multiple」——模型原生维度非 128 倍数时的权宜做法：行方向 padding（3583→3584）。而行内宽度无法这样 pad，正是 scale 必须走字节级 retile 的原因。

#### 4.3.4 代码实践：retile 的零拷贝 round-trip 与 padding 反例

本讲主实践，只需 CPU 版 torch（`retile_for_prefetch` 不触碰 CUDA；若已 `pip install -e .` 且装好 `nvidia-cutlass-dsl`/`cuda-bindings`，可把复刻函数换成 `from moonep.prefetch import retile_for_prefetch, prefetch_retile_nbytes` 直接验证源码）。

1. **实践目标**：证明 retile 是零拷贝重排（`data_ptr` 不变、round-trip 逐位一致、每专家字节区间不动），并亲手触发一次失败断言、用 `prefetch_retile_nbytes` 算出正确的分配大小。
2. **操作步骤**：存为 `retile_roundtrip.py`（示例代码，函数体逐行对应 [moonep/prefetch.py:L335-L354](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L335-L354)）：

   ```python
   # 示例代码：retile_for_prefetch 的 round-trip 验证（CPU 即可）
   import torch
   TILE = 128 * 128  # M_BLOCK * N_BLOCK

   def prefetch_retile_nbytes(per_expert_nbytes):
       return (per_expert_nbytes + TILE - 1) // TILE * TILE

   def retile_for_prefetch(t):
       assert t.is_contiguous()
       n = int(t.shape[0])
       per_expert = t.nbytes // n if n else 0
       assert per_expert % TILE == 0, (
           f"per-expert extent {per_expert} bytes is not a multiple of {TILE}; "
           f"allocate {prefetch_retile_nbytes(per_expert)} bytes per expert instead")
       return t.view(torch.uint8).reshape(n, 128, -1)

   E, B, H, K = 8, 4, 3584, 3072            # down 投影的 scale：[E+B, H, K/32]
   scale = torch.randint(0, 256, (E + B, H, K // 32), dtype=torch.uint8)
   tiled = retile_for_prefetch(scale)
   X = tiled.shape[2]
   print(f"retile: {tuple(scale.shape)} -> {tuple(tiled.shape)}  (X={X})")

   assert tiled.data_ptr() == scale.data_ptr()                # ① 零拷贝：同一块存储
   back = tiled.reshape(E + B, -1).reshape(scale.shape)       # ② round-trip 仍是视图
   assert torch.equal(back, scale)
   assert torch.equal(tiled[3].flatten(), scale[3].flatten()) # ③ 专家 3 的字节区间不变
   assert tiled.shape[1] % 128 == 0 and X % 128 == 0          # ④ 形状满足内核 128 约束
   print("all checks passed")

   # 反例：模型原生维度 3583 行 -> per_expert 不是 16384 的倍数，断言应失败
   bad = torch.randint(0, 256, (E + B, 3583, K // 32), dtype=torch.uint8)
   try:
       retile_for_prefetch(bad)
       raise SystemExit("ERROR: should have failed")
   except AssertionError as e:
       print("expected failure:", e)
       print(f"per_expert={3583 * (K // 32):,} -> "
             f"allocate {prefetch_retile_nbytes(3583 * (K // 32)):,} bytes/expert")
   ```

3. **需要观察的现象**：retile 后形状为 `[12, 128, 2688]`；四条断言全过；反例抛出 `AssertionError`，建议分配 344,064 字节/专家。
4. **预期结果**（手工推演）：
   - `per_expert = 3584 × 96 = 344,064 = 21 × 16,384` ✓，`X = 2,688` 且 `2688 % 128 == 0` ✓——与 [tests/test_prefetch.py:L162-L171](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_prefetch.py#L162-L171) 的 `mxfp4_sf_k3_down_retiled` 用例形状一致；
   - 反例 `per_expert = 3583 × 96 = 343,968`，`343,968 mod 16,384 = 16,288 ≠ 0` 触发断言；`prefetch_retile_nbytes(343,968) = 344,064`——恰好多出的 96 字节正好等于**一行 scale 的宽度**，即等价于把行数 pad 到 3584；
   - 有多卡 NVLink 环境时，可进一步把 retile 后的张量塞进 `tests/test_prefetch.py` 的 `CASES` 跑真实内核对拍（该文件已有现成的 retile 用例）。
5. 本环境无法运行 Python——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`retile_for_prefetch` 对一个 bf16 的 `[E+B, 3584, 3072]` 权重张量会返回什么形状？这样用有意义吗？
**答案**：`view(torch.uint8)` 把最后一维翻倍（6144 字节），`per_expert = 3584×3072×2 = 22,020,096 = 1,344×16,384`，返回 `[E+B, 128, 1344]`。语法上合法、内核也能搬，但没必要——bf16 权重的自然形状 `[3584, 3072]` 本来就满足 128 约束，直接进即可；retile 是专为「尾维 K/32」这类无法整除的形状准备的。

**练习 2**：为什么 retile 只能用 `view + reshape`，不能 `permute`？
**答案**：`permute` 交换维度顺序会产生 stride≠1 的非连续视图，后续 `reshape` 要么报错要么触发隐式拷贝并改变字节序；而 TMA 与消费侧 GEMM 都要求每专家是一段**原序**连续字节。retile 的本质是对同一段连续字节的「重新切块解释」，`view/reshape` 在连续张量上恰好零拷贝地做到这一点。

**练习 3**：某模型 K=2048（收缩维）、行数 3584。scale 的 per_expert 字节数是多少？需要 padding 吗？
**答案**：\(3584 \times 2048/32 = 3584 \times 64 = 229{,}376 = 14 \times 16{,}384\)，恰好整除——不需要 padding，retile 成 `[128, 1792]`（1792 = 14×128 ✓）。若行数是 3583：\(3583×64=229{,}312\)，除 16,384 余 16,320（比 14 倍恰少 64 字节），`prefetch_retile_nbytes` 建议 229,376 字节/专家。

## 5. 综合实践

把三个模块串成一个「MXFP4 层预取计划生成器」。写一个脚本 `mxfp4_prefetch_plan.py`（示例代码），输入 `E+B, H, I`（如 `12, 3584, 3072`）和一个 `experts_to_copy` 列表（含空槽），输出一份**预取核对单**：

1. **形状推导**：按 4.2.4 的方式算出 6 个张量（3 packed + 3 scale）的自然形状与进内核形状（scale 用 4.3 的公式 retile）。
2. **字节宽度账**：按 4.1 复算每个张量的 `tile_bytes`（uint8 均为 16,384）、在给定 smem 预算下的 `stages`、每个专家的 tiles 数（应得 packed 336 / scale 21）。
3. **约束校验**：对每个张量断言 `H % 128 == 0`、`Hp % 128 == 0`（scale 断言 `per_expert % 16384 == 0`），任一失败则打印 `prefetch_retile_nbytes` 建议。
4. **流量账**：`n_active × Σ(bytes/expert)` 为本次预取的 NVLink 读流量，与 bf16 同配置对比，验证 ≈1/3.76。
5. **对照真实用例**：把输出与 [tests/test_prefetch.py:L126-L171](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_prefetch.py#L126-L171)、[benchmarks/bench_prefetch.py:L28-L35](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_prefetch.py#L28-L35) 的数字逐项核对；有多卡环境时用 `torchrun --nproc_per_node=8 -m pytest tests/test_prefetch.py` 跑真实内核做终极对拍。

这份核对单就是把本讲三块知识（字节宽度 → 形状契约 → retile 数学）应用到任意新模型配置的通用工具；数值输出**待本地验证**。

## 6. 本讲小结

- `_ELEM_TYPES` 是 dtype 支持的唯一登记处（bf16/int8/uint8），`torch.dtype → (CuTe 类型, 字节数)`；`elem_bytes` 有三个去处——smem 预算与流水线深度（`tile_bytes = 128×128×elem_bytes`）、mbarrier 的 `tx_count`（TMA 按字节报告事务）、`make_ptr` 的编译期类型。拷贝本身类型无关，MXFP4 与 ue8m0 scale 因此零分支地复用 bf16 路径。
- MXFP4 契约：打包权重 `[E+B, H, K/2]` uint8（e2m1 两值一字节，H'=K/2）；scale `[E+B, rows, K/32]` uint8（每 32 值一个 ue8m0 字节），经 `prefetch_weight` 的 `full_*_scale` 参数成组传入，一层共六次内核发射，全部共用同一份 `experts_to_copy`。
- 128 约束的来源：内核按 `MTILES = H//128`、`NTILES = Hp//128` 均匀整除切 tile、无边界处理，smem tile 固定 128×128×elem_bytes；`launch_prefetch` 把约束前移成宿主断言，模型原生维度非 128 倍数时需行 padding（3583→3584）。
- `retile_for_prefetch` 是零拷贝重排：`view(uint8) + reshape(N, 128, X)`，本质是对每专家连续字节区间的「重新切块解释」；合法条件 `per_expert % 16384 == 0` 等价于 retile 出的 `[128, X]` 两维都是 128 的倍数；不满足时用 `prefetch_retile_nbytes` 向上取整分配 padding 字节（永不写入）。
- 量化的收益是带宽：每值字节从 2 降到 17/32（≈3.76× 压缩），同样的预取槽数下 NVLink 流量同比例缩小；测试用 uint8 哨兵 `0xAB` 证明「空槽不写」的精确写在量化路径同样成立。

## 7. 下一步学习建议

- **u5-l3（梯度缓冲与 reduce_grad）**：`[E+B, H, H']` 布局在 fp32 梯度侧的镜像——预取槽的梯度为什么必须绕开框架归约、走独立的 `[R,B,H,H']` reduce 缓冲。注意量化训练时梯度缓冲仍是 fp32（量化只作用于权重侧）。
- **u6-l5（基准测试）**：`bench_prefetch.py` 的 `mxfp4_*` 与 `k3_layer_mxfp4` 用例正是本讲形状推导的计时版本，可把综合实践的流量账与实测 GB/s 对照。
- **u6-l6（扩展与二次开发）**：在 `_ELEM_TYPES` 里加 float16 的最小改动练习——本讲 4.1 已给出全部背景（登记表、`elem_bytes` 消费链、缓存键）。
- 复习锚点：128×128 tile 流水线本体（u5-l1）、TMA/mbarrier 事务计数机制（u4-l1）、`[E+B]` 行约定与对称内存映射（u2-l2、u5-l1）。
