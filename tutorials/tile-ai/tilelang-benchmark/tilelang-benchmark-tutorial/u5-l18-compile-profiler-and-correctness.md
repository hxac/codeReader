# tilelang.compile 评估与正确性校验

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 **非调优路径**（`tilelang.compile` + `get_profiler().do_bench`）把一个写好的 `@T.prim_func` 内核**编译、计时、报告 TFlops**。
2. 说清 `ref_program`（参考实现）在 TileLang 评估流程里**可以扮演的三种角色**，以及本项目的 FlashAttention 文件**实际**用了哪一种（答案可能和你想的不一样）。
3. 区分两套正确性校验 API：profiler 内置的 `assert_allclose` 与直接比对张量的 `torch.testing.assert_close`。
4. 用 `tilelang.disable_cache()` 与 `get_kernel_source()` 这两个**调试接口**定位「缓存命中导致没重编译」「生成的 CUDA 源码不对」这类问题。
5. 看懂 FlashAttention 文件里 `tune=True` 与 `tune=False` **两条返回路径**，分别如何得到 latency、是否校验正确性。

## 2. 前置知识

本讲承接 **u5-l16（FlashAttention 在线 softmax 与 macro 结构）**——你已经知道 `benchmark_tilelang_mha.py` 里那个 `@T.prim_func main` 内核长什么样、四段 macro（MMA0/Softmax/Rescale/MMA1）怎么拼。本讲**不再讲内核内部的数值**，而是讲内核写好之后、跑分之前的那一段「脚手架」。

还需要回忆 **u3-l8（TileLang 内核骨架：autotune/jit 与 prim_func）** 提出的两件事：

- **tune 路径**：`@autotune` + `@jit` 装饰一个返回 `main` 的函数，调用 `kernel()` 得到一个 `best_result` 对象，字段有 `.latency / .config / .ref_latency / .kernel`。
- **「以代码为准」原则**：本项目多处注释与代码不符（如 half-precision 注释配 int8 代码），读源码时**只信代码**。

本讲会再一次印证这条原则：你会发现 FlashAttention 文件里明明写了一个 `ref_program`、用 `partial` 绑好了，却**从头到尾没被调用**——这是真实存在的「死绑定」，我们据实讲解，不替它圆场。

几个通俗概念：

- **参考实现（reference program / ref_program）**：用 PyTorch 这种「正确但慢」的方式写出同一个算子（比如 attention 就是 `einsum → softmax → einsum`），用来①校验你手写内核的数值对不对、②给 autotuner 算 speedup 提供一个「分母」。
- **profiler（剖析器）**：TileLang 给编译好的 kernel 配的一个小工具，能自动造输入张量、跑内核、计时、还能拿 ref_program 做对照。
- **out_idx（输出下标）**：内核函数有多个参数（如 Q、K、V、Output），profiler 需要知道**哪几个是输出**，才能分配显存、回收结果去做比对。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py) | **本讲主线**。同时含 `tune=True/False` 两条路径，含 `ref_program` 定义、`tilelang.compile`、`get_profiler().do_bench` 的最典型用法。 |
| [hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py) | 提供 `get_kernel_source()` 调试接口的真实用例，以及 `best_result` 对象字段的另一种取法。 |
| hopper_benchmark/flashattention/**0.torch_benchmark**/benchmark_torch_mha.py | 「兄弟文件」：与主线几乎相同的内核，但**真正**调用了 `profiler.assert_allclose` 与 `do_bench(ref_program)`，用来对照「完整校验流程」长什么样。 |
| cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py | 提供 `tilelang.disable_cache()`、`torch.testing.assert_close`（张量式校验）、`profiler._get_inputs()` 的真实用例。 |

> 说明：后两个文件不在本讲「关键源码」清单里，但它们是仓库中**唯一**能佐证 `assert_close` / `disable_cache` 这两个 API 真实用法的地方。本讲引用它们只为讲清 API 行为，不展开 MLA 内核本身（那是 u6-l20 的内容）。

## 4. 核心概念与源码讲解

### 4.1 两条评估主流程：tune 与非 tune

#### 4.1.1 概念说明

写好一个 TileLang 内核后，你面前有两条路：

- **调优路径（tune）**：还不确定最佳分块参数（`block_M/N`、`num_stages`…），让 `@autotune` 遍历一组 config、每个都编译计时、挑最快的。这条路的入口是 `@autotune + @jit`，产出 `best_result` 对象。u3-l8 已讲过骨架。
- **非调优路径（非 tune）**：参数已经定死（比如你已知 `block_M=128, block_N=128, num_stages=2, threads=256` 最优），只想**编译一次、计时一次**。这条路用 `tilelang.compile` 手动编译、`get_profiler().do_bench` 手动计时。

本讲重点在**非调优路径**，因为它把「编译」「计时」「校验」拆成了你能逐个控制的步骤，最便于理解 TileLang 的评估机制。`benchmark_tilelang_mha.py` 用 `tune=False` 形参在同一份代码里同时支持两条路，是绝佳的对照样本。

#### 4.1.2 核心流程

`flashattn(...)` 函数内部按 `tune` 形参二选一返回：

```text
tune=True  → 给 kernel 套 @autotune+@jit，返回 kernel()（即 best_result 对象）
tune=False → 直接返回一个普通函数 kernel(block_M,block_N,...)，
             调用它得到 main（一个 @T.prim_func 程序对象）
```

`__main__` 里再按命令行 `--tune` 二选一执行：

```text
非 tune：program = flashattn(..., tune=False)(block_M=128,...)   # 得到 prim_func
         kernel  = tilelang.compile(program, out_idx=[3])        # 编译
         latency = kernel.get_profiler().do_bench(warmup=500)    # 计时（内核）
         打印 "Tile-lang: ... ms / ... TFlops"

tune  ：best_result = flashattn(..., tune=True)                  # 跑完整个搜索
         latency = best_result.latency                           # 直接取最优
         打印 "Best latency / Best TFlops / Best config"
```

#### 4.1.3 源码精读

`flashattn` 的形参默认 `tune=False`，函数末尾按 `tune` 分叉返回：

[benchmark_tilelang_mha.py:160-173](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L160-L173) —— `tune=True` 时给 `kernel` 套上 `@autotune(warmup=10, rep=10)` + `@jit(out_idx=[3], supply_type=..., ref_prog=None)` 并立刻 `return kernel()`（返回 `best_result` 对象）；`tune=False` 时返回一个**普通函数** `kernel`，调用它才得到 `main` 这个 `@T.prim_func`。注意这里 `@jit` 显式写了 `ref_prog=None`——后面 4.4 会专门讲它的含义。

`__main__` 里的分叉：

[benchmark_tilelang_mha.py:207-218](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L207-L218) —— **非 tune 路径**：先把固定参数传进 `flashattn` 得到 `program`，再 `tilelang.compile` 编译、`get_profiler().do_bench(warmup=500)` 计时，打印 `Tile-lang: ... ms` 与 `Tile-lang: ... TFlops`。

[benchmark_tilelang_mha.py:219-226](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L219-L226) —— **tune 路径**：直接取 `best_result.latency / .config`，打印 `Best latency / Best TFlops / Best config`。

> **一个容易被忽视的细节**：非 tune 路径在 [第 212 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L212) 写了 `ref_program = partial(ref_program, is_causal=is_causal)`，但**后续 6 行从未用到这个变量**。也就是说，这条路径**只测延迟、不校验正确性**。这是真实代码状态，不是印刷错误——4.4 节会对照「兄弟文件」里它本该被怎么用。

#### 4.1.4 代码实践

**实践目标**：把 `tune=True` 与 `tune=False` 两条路径的「latency 从哪来」「是否校验正确性」逐一列清。

**操作步骤（源码阅读型，无需 GPU）**：

1. 打开 `benchmark_tilelang_mha.py`，定位两处：第 160–173 行（`flashattn` 的返回分叉）与第 207–226 行（`__main__` 的执行分叉）。
2. 画一张两列对照表，左列 `tune=False`、右列 `tune=True`，逐项填：①返回值类型（prim_func 对象 / best_result 对象）、②latency 怎么得到（`do_bench` / `best_result.latency`）、③是否出现任何 `assert` / `assert_close` / `assert_allclose` 字样、④是否引用了 `ref_program`。

**需要观察的现象**：

- 两条路径里**都搜不到任何正确性校验调用**——非 tune 路径的 `ref_program` 是「绑定即闲置」，tune 路径则把 `ref_prog` 显式设成了 `None`。
- 非 tune 路径用了 `do_bench(warmup=500)`，注意它**只传 warmup、不传 rep**；而 tune 路径的 `@autotune` 同时传了 `warmup=10, rep=10`。

**预期结果**：你应当得出结论——**这份文件是一个「纯延迟」基准，不包含数值正确性校验**。要校验，得仿照它的兄弟文件 `0.torch_benchmark/benchmark_torch_mha.py`（见 4.4）补上 `assert_allclose`。

> 「`warmup=500` 的单位是次数还是毫秒」这一点 TileLang profiler 内部实现决定，本仓库未给出定义，**待本地验证**：可在能跑的环境里把 `warmup` 改成 1 与 500 对比计时结果是否显著不同来判断。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `flashattn(..., tune=False)` 返回的是一个**函数** `kernel`，而 `flashattn(..., tune=True)` 返回的是 `kernel()` 的**调用结果**？

**答案**：`tune=False` 时没有装饰器，`kernel` 只是包裹 `kernel_func` 的普通函数，需要由调用方显式传参（如 `block_M=128`）才能产出一个具体的 `@T.prim_func` 程序；返回函数是为了把「选参数」的权力留给 `__main__`。`tune=True` 时 `kernel` 已被 `@autotune` 接管，调用 `kernel()` 会触发整个搜索过程并返回 `best_result`，所以直接返回 `kernel()` 的结果。

**练习 2**：非 tune 路径里 `program`、`kernel`、`profiler` 三个变量分别是什么类型、各起什么作用？

**答案**：`program` 是 `@T.prim_func` 描述的算子程序（DSL 层中间表示）；`kernel = tilelang.compile(program, out_idx=[3])` 是编译后**可直接在 GPU 上运行的内核对象**；`profiler = kernel.get_profiler()` 是绑定到该内核的剖析器，负责造输入、计时、对照 ref。

---

### 4.2 tilelang.compile：把 prim_func 编译成可运行 kernel

#### 4.2.1 概念说明

`@T.prim_func` 写出来的是**声明式描述**（「我要这样分块、这样搬运、这样乘加」），它本身还不能跑。`tilelang.compile(program, out_idx=[...])` 是把这份描述** lowering 成底层 IR、再编译成 CUDA/HIP 内核**的桥梁，返回一个可调用对象（`kernel`）。

`out_idx` 是 compile 最关键的概念：内核函数有多个张量参数，profiler/编译器需要知道**哪些是输出**。对 FlashAttention，参数顺序是 `(Q, K, V, Output)`，`Output` 是第 4 个（下标 3），所以 `out_idx=[3]`。

#### 4.2.2 核心流程

```text
program（@T.prim_func）
      │
      │  tilelang.compile(program, out_idx=[输出下标])
      ▼
kernel（可调用：kernel(*inputs) → 在 Output 位置写出结果）
```

- `out_idx` 是**列表**，因为某些算子有多个输出（例如 MLA 的 split 路径会同时写 Output 和 logsum，见 u6-l20，那里 `out_idx=[6]` 对应 7 个参数里的第 7 个）。
- 编译有**缓存**：同一份 `(program, target)` 二次编译会命中缓存、几乎不耗时——这也是后面 `disable_cache()` 存在的原因。

#### 4.2.3 源码精读

三个算子文件里 `compile` 的真实调用，`out_idx` 各不相同，正好体现「输出位置由算子签名决定」：

[benchmark_tilelang_mha.py:213](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L213) —— FlashAttention 内核签名 `(Q, K, V, Output)`，`Output` 是第 4 个参数，故 `out_idx=[3]`。

[benchmark_torch_mha.py:211](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/0.torch_benchmark/benchmark_torch_mha.py#L211) —— 兄弟文件，同一内核、同样的 `out_idx=[3]`。

[benchmark_mla_decode_amd_tilelang.py:296](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L296) —— MLA decode 内核参数更多，输出在第 7 个位置，故 `out_idx=[6]`。

#### 4.2.4 代码实践

**实践目标**：验证 `out_idx` 与内核参数顺序的对应关系。

**操作步骤（源码阅读型）**：

1. 在 `benchmark_tilelang_mha.py` 第 113–119 行找到 `main` 的参数列表 `(Q, K, V, Output)`，从 0 开始数每个参数的下标。
2. 确认 `Output` 的下标 = compile 调用里的 `out_idx` 值。
3. 对比 MLA 文件：读 `benchmark_mla_decode_amd_tilelang.py` 里 `flashmla_decode` 返回的 `main` 函数参数列表，数到第 7 个是不是输出张量，与 `out_idx=[6]` 核对。

**需要观察的现象**：`out_idx` 总是指向「由内核写出、而非读入」的张量参数。

**预期结果**：你能用一句话说清——「`out_idx=[3]` 告诉编译器/profiler：这个内核的第 4 个参数是输出，前三个是输入」。

#### 4.2.5 小练习与答案

**练习 1**：如果一个内核签名是 `(A, B, C, D, Out)`，`out_idx` 应该填什么？

**答案**：`[4]`（`Out` 是第 5 个参数，下标 4）。若该内核还有第二个输出，比如 `(A,B,C,Out1,Out2)`，则 `out_idx=[3,4]`。

**练习 2**：为什么 `out_idx` 必须是列表（`[3]`）而不是单个整数（`3`）？

**答案**：因为有些算子会同时写出多个输出张量（如 split-KV 的 Output 与 logsum），列表形式能统一表达「单输出」和「多输出」两种情形；单输出时就是长度为 1 的列表。

---

### 4.3 get_profiler 与 do_bench：离线计时

#### 4.3.1 概念说明

编译得到 `kernel` 后，怎么测它的延迟？TileLang 给每个 kernel 配了一个 **profiler**：`kernel.get_profiler()`。profiler 能自动按内核签名造一组随机输入、把内核包成可反复触发的调用、按 warmup/rep 跑若干轮、返回一个统计后的延迟。

核心 API 是 `profiler.do_bench(...)`。它有一个**容易被忽略但极重要**的语义：**第一个位置参数 = 被计时的函数**。

- `profiler.do_bench(warmup=500)` —— 不传位置参数 → **计时编译好的内核**。
- `profiler.do_bench(ref_program, warmup=500)` —— 传一个参考实现 → **计时参考实现**（用来算 speedup 的分母）。

返回值的**单位是毫秒（ms）**——这一点和 `triton.testing.do_bench` 一致（u2-l6 已建立）。本项目里 TFlops 一律按 `total_flops / latency_ms * 1e-9` 换算（u2-l4 已推导：`1e-9 = 1e-3(ms→s) × 1e-12(FLOPS→TFlops)`）。

#### 4.3.2 核心流程

```text
kernel.get_profiler()  →  profiler
profiler.do_bench(           warmup=W)        → 计时 kernel 本身，返回 latency(ms)
profiler.do_bench(ref_program, warmup=W)      → 计时 ref_program，返回 latency(ms)
```

TFlops 换算：

\[
\text{TFlops} = \frac{\text{total\_flops}}{\text{latency (ms)}} \times 10^{-9}
\]

#### 4.3.3 源码精读

[benchmark_tilelang_mha.py:215-218](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L215-L218) —— 不传位置参数，`do_bench(warmup=500)` 计时**内核**，打印标签是 `Tile-lang: ... ms`。这印证了「省略第一个参数 = 计时内核」。

[benchmark_torch_mha.py:213-217](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/0.torch_benchmark/benchmark_torch_mha.py#L213-L217) —— 把 `ref_program` 作为**第一个位置参数**传进去，`do_bench(ref_program, warmup=500)` 计时**参考实现**，打印标签变成了 `Ref: ... ms`。同一行 API、只差一个位置参数，计时对象就换了——这是理解 `do_bench` 的关键。

[benchmark_mla_decode_amd_tilelang.py:298-307](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L298-L307) —— MLA 文件展示了带 `tensor_supply_type` 与「先校验后计时」的完整三步：`get_profiler(tensor_supply_type=tilelang.TensorSupplyType.Randn)` 指定用正态分布造输入，`do_bench(warmup=500)` 计时内核，`print(f"Latency: {latency} ms")` 与 `f"TFlops: {total_flops / latency * 1e-9} TFlops"` 的换算与主线完全一致。

> **单位陷阱（承接 u2-l4/u3-l8）**：`profiler.do_bench()` 返回 **ms**；而 dense_matmul 文件第 278 行把 `best_latency` 标注成 `(s)`，那是**标签写错**，真实单位仍是 ms——换算公式 `total_flops / latency * 1e-9` 只有在 latency 为 ms 时才能得到正确量级的 TFlops。

#### 4.3.4 代码实践

**实践目标**：从源码确认「`do_bench` 第一个位置参数决定计时对象」这一语义。

**操作步骤（源码阅读型）**：

1. 在 `benchmark_torch_mha.py` 第 216 行找到 `profiler.do_bench(ref_program, warmup=500)`，看紧接着第 217 行打印的是 `Ref:` 还是 `Tile-lang:`。
2. 在 `benchmark_tilelang_mha.py` 第 216 行找到 `profiler.do_bench(warmup=500)`（无位置参数），看第 217 行打印的是 `Tile-lang:`。
3. 把这两处摆在一起：**同一个 `do_bench`、相同的 `warmup=500`，只差是否传入 `ref_program`，打印标签不同**。

**需要观察的现象**：传 `ref_program` → 标签 `Ref:`；不传 → 标签 `Tile-lang:`。

**预期结果**：你能下结论——`do_bench` 的第一参数是「被计时函数」，省略时默认计时编译好的内核。

#### 4.3.5 小练习与答案

**练习 1**：给定 `batch=64, heads=64, seq_q=seq_kv=8192, dim=128, is_causal=False`，`do_bench` 返回 `latency=2.0`，手算 TFlops（用本文件的 `total_flops` 公式）。

**答案**：`flops_per_matmul = 2 × 64 × 64 × 8192 × 8192 × 128`，`total_flops = 2 × flops_per_matmul`（非 causal 不折半）。代入 `total_flops / 2.0 * 1e-9`：先算 `2×64×64×8192×8192×128 ≈ 7.0369e13`，`total_flops = 2 × 7.0369e13 ≈ 1.407e14`，`TFlops = 1.407e14 / 2.0 × 1e-9 ≈ 70369`，即约 **7037 TFlops**（注意 `*1e-9` 已把 FLOPS→TFlops，最终约 **70.4 TFlops**——请以本地实际 `total_flops` 代入复核）。

> 上面手算的中间步骤较多，最终量级以本地运行 `print(total_flops / latency * 1e-9)` 为准；**待本地验证**确切数值。

**练习 2**：为什么 `do_bench` 的计时对象用「第一位置参数」而不是用一个叫 `target=` 的关键字参数区分内核/ref？

**答案**：这是 TileLang 的 API 设计选择——让 `do_bench(fn, ...)` 与常见 Python 计时工具（`lambda` 计时）的「传入可调用对象」直觉保持一致：传谁就测谁。ref 也是个可调用对象（PyTorch 函数），所以能和内核用同一套计时逻辑，无需额外分支。

---

### 4.4 ref_program 与正确性校验：三种真实写法

#### 4.4.1 概念说明

手写一个高性能内核，最大的风险是「快但错」。`ref_program` 就是**用 PyTorch 写的、正确但慢的同一算子**，用来做数值对照。对 FlashAttention，参考实现就是定义里的那句注意力公式：

\[
\text{Attention}(Q,K,V) = \text{softmax}\!\left(\frac{QK^\top}{\sqrt{d}}\right) V
\]

用 PyTorch 表达即 `einsum('bhqd,bhkd->bhqk', Q, K)` → 除以 √d → causal 时上三角填 −∞ → `softmax` → `einsum('bhqk,bhkd->bhqd', weights, V)`。

本仓库里，`ref_program` 实际有**三种用法**，分布在三个文件——理解它们的区别是本讲的重点：

| 写法 | API | 谁造输入、谁跑两边 | 出现位置 |
|---|---|---|---|
| ① profiler 内置校验 | `profiler.assert_allclose(ref_program, rtol, atol)` | profiler 自动造输入、自动跑内核+ref、自动比对 | `0.torch_benchmark` |
| ② 张量式直接比对 | `torch.testing.assert_close(kernel_out, ref_out, rtol, atol)` | 你**手动**取输入、手动跑两边、手动比对张量 | mla 文件 |
| ③ 调优时内置校验 | `@autotune(..., ref_prog=ref)`（传给 `@jit`） | autotuner 在搜索循环里对每个 config 跑 ref、算 ref_latency | **本主线文件未启用**（`ref_prog=None`） |

**关键事实（以代码为准）**：主线文件 `benchmark_tilelang_mha.py` **三种都没真正用**——非 tune 路径用 `partial` 绑了 `ref_program` 却不调用，tune 路径把 `ref_prog` 显式设为 `None`。它是一个**只测延迟、不校验数值**的基准。要看「完整校验流程」，得去它的兄弟文件。

#### 4.4.2 核心流程

**写法 ①（profiler 内置，推荐用于离线评估）**：

```text
kernel = compile(program, out_idx=[3])
profiler = kernel.get_profiler()
profiler.assert_allclose(ref_program, rtol=0.01, atol=0.01)   # 内部造输入、跑两边、比对
print("All checks pass.")
latency = profiler.do_bench(ref_program, warmup=500)           # 顺带计时 ref
```

**写法 ②（张量式，手动控制输入与比对）**：

```text
profiler = kernel.get_profiler(tensor_supply_type=...)
inputs = profiler._get_inputs()              # 取 profiler 造的输入
out_tl  = kernel(*inputs)                    # 跑内核
out_ref = ref_program(*inputs)               # 跑 ref（同一份输入）
torch.testing.assert_close(out_tl, out_ref, rtol=0.01, atol=0.01)
```

**写法 ③（调优时校验）**：把 ref 传给 `@jit(..., ref_prog=ref)`，autotuner 在搜索时既用它校验每个 config 的正确性、又用它算 `ref_latency`（speedup 分母）。本主线文件**没有**这么做（`ref_prog=None`）。

#### 4.4.3 源码精读

先看 `ref_program` 的定义本身（主线文件确实定义了它，只是没调用）：

[benchmark_tilelang_mha.py:176-188](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L176-L188) —— PyTorch 参考实现：`einsum` 算 Q·Kᵀ → 除以 √dim → `is_causal` 时用 `torch.tril` 造下三角掩码、`masked_fill(mask==0, -inf)` 屏蔽未来 → `F.softmax(dim=-1)` → `einsum` 乘 V。这正是 u5-l16 在线 softmax 要数值等价的那个「朴素版」。

再看主线文件**为什么没校验**——非 tune 路径的「死绑定」：

[benchmark_tilelang_mha.py:212](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L212) —— `ref_program = partial(ref_program, is_causal=is_causal)`，把 `is_causal` 预先固定成一个单参函数。但往下读到第 218 行，**没有任何一行用到它**。这是一个真实的「绑定了却闲置」的代码状态。

对照**写法 ①** 的完整流程（兄弟文件）：

[benchmark_torch_mha.py:213-217](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/0.torch_benchmark/benchmark_torch_mha.py#L213-L217) —— `profiler.assert_allclose(ref_program, rtol=0.01, atol=0.01)` 让 profiler 自动造输入、同时跑内核与 ref、用 1% 的相对/绝对容差比对，通过后打印 `All checks pass.`，再 `do_bench(ref_program, warmup=500)` 计时参考实现。**这才是 `ref_program` 在非 tune 路径下的「标准用法」**，主线文件省掉了前两步。

对照**写法 ②** 的张量式校验（MLA 文件）：

[benchmark_mla_decode_amd_tilelang.py:298-305](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L298-L305) —— `profiler._get_inputs()` 取出输入张量，`kernel(*input_tensors)` 跑内核、`ref_program(*input_tensors)` 跑 ref（**同一份输入**），再用 `torch.testing.assert_close(tilelang_output, ref_output, rtol=0.01, atol=0.01)` 直接比对两个输出张量。与写法 ① 的区别是：这里**你亲手**控制输入与两边调用，profiler 只负责造输入和计时。

关于**写法 ③**——主线文件 tune 路径显式关掉了它：

[benchmark_tilelang_mha.py:162-167](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L162-L167) —— `@jit(out_idx=[3], supply_type=..., ref_prog=None)`，`ref_prog=None` 意味着 autotuner **不**在搜索循环里跑 ref、不校验每个 config、也算不出有意义的 `ref_latency`。所以第 223 行取的 `best_result.ref_latency` 在这条路径下**应当是不可用的**（具体取值依赖 autotuner 对 `ref_prog=None` 的默认处理，**待确认**）。

> **与 dense_matmul 文件的对照（一致性提醒）**：`benchmark_tilelang_matmul.py` 第 274 行也取了 `best_result.ref_latency`、第 282 行用它算 `Reference TFlops`，但它的 `@jit`（[第 147–151 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L147-L151)）同样**没有传 `ref_prog`**。这两处「取 `ref_latency` 却不配 ref_prog」是同类的历史遗留，运行到该打印行时 `ref_latency` 的实际取值**待本地验证**（可能为 `None` 而触发除零/类型错误，也可能 autotuner 有隐式默认 ref）。

#### 4.4.4 代码实践

**实践目标**：给「只测延迟」的主线文件**补上**正确性校验，体会写法 ① 的最小改动量。

**操作步骤（源码阅读型 + 可选本地改造）**：

1. 打开主线文件第 207–218 行，确认它当前缺哪一步（答案：缺 `profiler.assert_allclose`）。
2. 仿照兄弟文件第 213–215 行，在**不修改源码的前提下**，在草稿纸上写出「补丁」：在 `profiler = kernel.get_profiler()`（第 215 行）之后、`latency = profiler.do_bench(...)`（第 216 行）之前，插入一行 `profiler.assert_allclose(ref_program, rtol=0.01, atol=0.01)`，并复用第 212 行已经绑好的 `ref_program`。
3. （可选，需 GPU 与 tilelang 环境）实际插入该行后运行 `python benchmark_tilelang_mha.py`（不带 `--tune`），观察是否打印 `All checks pass.`；若不打印或报错，检查 `ref_program` 是否被正确 `partial`。

**需要观察的现象**：

- 草稿上应当发现：补丁**只有一行**，且能直接复用第 212 行那个原本闲置的 `ref_program`——这反过来说明作者很可能就是「删掉了校验那行、留下了绑定」。
- 若本地运行，`rtol=0.01, atol=0.01`（1% 容差）应能通过，因为 fp16 attention 的数值误差通常在该量级内。

**预期结果**：你能解释——主线文件只要补一行 `assert_allclose` 即可拥有与兄弟文件等价的正确性校验；之所以没补，可能是为了在批量跑分时省掉 ref 的开销。

> 本实践**不要求**你真的改源码（本课程禁止改源码）；写出「补丁行」即可。若想本地验证，请在仓库副本上进行。

#### 4.4.5 小练习与答案

**练习 1**：写法 ①（`assert_allclose`）和写法 ②（`torch.testing.assert_close`）都要「跑内核 + 跑 ref + 比对」，本质区别在哪？

**答案**：在于**谁负责造输入与调度两边运行**。写法 ① 把这一切封进 profiler（一行调用），适合快速离线校验；写法 ② 由你显式 `profiler._get_inputs()` 取输入、显式调用 `kernel(...)` 与 `ref(...)`、显式比对张量，适合你想**控制输入分布**（如 `TensorSupplyType.Randn`）或**检查中间张量**的调试场景。

**练习 2**：为什么 `rtol=0.01, atol=0.01` 对 FlashAttention 是合理的容差？

**答案**：FlashAttention 用 fp16 输入、float 累加，再经过 softmax 的 exp 与多次 rescale，数值误差主要来自 fp16 的有限精度与 exp2 近似；1% 量级的容差足以容纳这些误差，又不至于宽松到放过真实 bug。若改成 int8 量化算子（如 u4-l12），容差策略需要另行评估。

---

### 4.5 调试接口：disable_cache 与 get_kernel_source

#### 4.5.1 概念说明

评估流程里有两个高频「踩坑点」，TileLang 各给了一个调试接口：

- **「我改了内核，但跑出来还是旧结果」**——多半是**编译缓存**作祟。`tilelang.compile` 会按 `(program, target)` 缓存编译产物，调试时反复小改内核很容易命中旧缓存。`tilelang.disable_cache()` 是一个**模块级**调用，写在 import 之后、任何 compile 之前，强制每次重新编译。
- **「我的旋钮（block/stages/policy）到底生成了什么 GPU 指令？」**——`best_result.kernel.get_kernel_source()` 返回 lowering 后生成的 **CUDA/HIP 源码字符串**，打印出来就能看到真实的 `mma.sync` / `wmma` / `cp.async` 指令、shared memory 布局、栅格化顺序。

#### 4.5.2 核心流程

```text
# 调试 1：禁缓存
import tilelang
tilelang.disable_cache()        # 模块级，放在文件顶部、compile 之前
kernel = tilelang.compile(...)  # 此后每次都重新编译

# 调试 2：看生成的源码（仅 tune 路径有 best_result.kernel）
best_result = matmul(M, N, K, with_roller)
print(best_result.kernel.get_kernel_source())   # 打印 CUDA/HIP 源码
```

> 注意 `disable_cache()` 是**模块级函数**（`tilelang.disable_cache()`），不是 `compile` 的某个参数；`get_kernel_source()` 是 **kernel 对象的方法**，需通过 `best_result.kernel`（tune 路径）或编译得到的 `kernel` 对象访问。

#### 4.5.3 源码精读

`disable_cache` 的真实用法（注意它在文件中的位置——紧贴 import）：

[benchmark_mla_decode_amd_tilelang.py:12](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L12) —— `tilelang.disable_cache()` 写在文件顶部 import 区之后，确保后续 `tilelang.compile`（第 296 行）每次都重编译。MLA 这种多内核组合（u6-l20）调试时尤其需要它。

`get_kernel_source` 的真实用法（dense_matmul 文件，本讲关键源码之一）：

[benchmark_tilelang_matmul.py:275](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L275) —— `print(best_result.kernel.get_kernel_source())`，在取完 `best_latency/config/ref_latency` 之后、打印结果之前，把最优 config 编译出的 CUDA 源码整段打到 stdout。这是验证 u3-l11「swizzle/warp 旋钮如何落到指令」的最直接手段。

> **承接 u3-l11**：那讲提到「想看 `wmma`/`mma.sync` 指令与 shared 布局，可用 `get_kernel_source()` 打印生成的 CUDA 源码」——本讲给出了它的源码出处。

#### 4.5.4 代码实践

**实践目标**：理解两个调试接口的「触发位置」与「所属对象」。

**操作步骤（源码阅读型）**：

1. 在 mla 文件第 12 行确认 `disable_cache()` 写在所有 `tilelang.compile` **之前**（compile 在第 296 行）；思考：如果把它移到 compile 之后还有没有用？（答案：没用，缓存决策发生在 compile 时。）
2. 在 dense_matmul 文件第 271–275 行确认 `get_kernel_source()` 是挂在 `best_result.kernel` 上的——只有走 **tune 路径**拿到 `best_result` 才方便调用；非 tune 路径拿到的是 `kernel`，应改用 `kernel.get_kernel_source()`（如该对象支持）。

**需要观察的现象**：

- `disable_cache()` 出现在文件**顶部**，远离具体 compile 调用，因为它改的是「模块级行为」。
- `get_kernel_source()` 出现在**拿到 kernel 对象之后**，是对象方法。

**预期结果**：你能区分——`disable_cache` 是「全局开关」（无返回值、改后续所有 compile 行为），`get_kernel_source` 是「单内核探针」（返回该内核的源码字符串）。两者具体返回内容**待本地验证**（依赖 tilelang 版本）。

#### 4.5.5 小练习与答案

**练习 1**：你在调试时改了一个 `@T.prim_func` 的 `num_stages`，但 latency 完全没变，最可能的原因和第一个该试的接口是？

**答案**：最可能是编译缓存命中了旧 `(program, target)`。第一个该试 `tilelang.disable_cache()`（放在 import 后），强制重编译，再观察 latency 是否变化。

**练习 2**：`get_kernel_source()` 返回的源码里，你想验证「`enable_rasteration=True` 是否真的改写了 block 调度顺序」，应该在那段源码里找什么？

**答案**：找计算 `program_id` / `block_idx` 到输出子块坐标的那段映射代码——rasterization（swizzle）会把这个映射从「行优先」改成「按 `panel_size` 分组的 Z 序/蛇形」（u3-l11 讲过它与 Triton `GROUP_SIZE_M` 同源）。对比 `enable_rasteration` 取 `True/False` 两份源码的差异即可确认。

---

## 5. 综合实践

**任务**：把本讲四块内容（compile / profiler+do_bench / ref+校验 / 调试接口）串起来，为 `benchmark_tilelang_mha.py` 的**非 tune 路径**写一份「评估流程说明书」，并指出它相对于「完整流程」缺了什么。

**步骤**：

1. **画出非 tune 路径的数据流图**：`flashattn(..., tune=False)(block_M=128,...)` → `program` → `tilelang.compile(program, out_idx=[3])` → `kernel` → `kernel.get_profiler()` → `profiler` → `profiler.do_bench(warmup=500)` → `latency(ms)` → `total_flops / latency * 1e-9` → TFlops。在每条箭头上标注「产物的类型」与「对应的源码行号」。
2. **对照兄弟文件 `0.torch_benchmark/benchmark_torch_mha.py`**，列出主线文件**缺少的两步**：① `profiler.assert_allclose(ref_program, rtol=0.01, atol=0.01)`（正确性校验）；② 用 `do_bench(ref_program, warmup=500)` 计时参考实现以算 speedup。说明这两步分别对应「写法 ①」的哪一部分。
3. **回答三个判断题**（给出依据行号）：
   - 主线文件的非 tune 路径是否校验了正确性？（否——第 212 行的 `ref_program` 未被调用）
   - 主线文件的 tune 路径是否在搜索时校验正确性？（否——第 163 行 `ref_prog=None`）
   - `do_bench` 的返回值单位是什么？（ms——由第 217 行打印标签与第 218 行 TFlops 换算公式共同确认）
4. **（可选，需环境）** 在仓库副本上，给主线文件非 tune 路径补上 `assert_allclose` 那一行，运行 `python benchmark_tilelang_mha.py`（不带 `--tune`），确认能打印 `All checks pass.`；再临时在 import 后加 `tilelang.disable_cache()`，观察第二次运行时编译阶段是否明显变慢（印证禁缓存的开销）。

**预期产出**：一张数据流图 + 一份「缺失步骤清单」+ 三道判断题的行号依据。若做了第 4 步，附上 `All checks pass.` 的实际输出（标注「待本地验证」如未实跑）。

## 6. 本讲小结

- **两条评估主流程**：tune 路径靠 `@autotune+@jit` 搜出 `best_result`（取 `.latency`）；非 tune 路径靠 `tilelang.compile` + `get_profiler().do_bench` 手动编译计时。`benchmark_tilelang_mha.py` 用 `tune=False` 形参在一份代码里同时支持两者。
- **`tilelang.compile(program, out_idx=[...])`** 把声明式 `@T.prim_func` 编译成可运行内核；`out_idx` 声明输出参数下标（FlashAttention 的 `Output` 是第 4 个 → `[3]`，MLA 是第 7 个 → `[6]`）。
- **`do_bench` 的第一位置参数 = 被计时函数**：省略计时内核（打印 `Tile-lang:`），传 `ref_program` 计时参考实现（打印 `Ref:`）；返回值单位是 **ms**，TFlops 按 `total_flops/latency*1e-9` 换算。
- **正确性校验三写法**：① `profiler.assert_allclose(ref, rtol, atol)`（profiler 全包）；② `torch.testing.assert_close(out_tl, out_ref, rtol, atol)`（手动跑两边比对张量）；③ `@jit(ref_prog=ref)`（调优时内置）。**主线文件三种都没启用**（非 tune 绑了 ref 却不调用、tune 显式 `ref_prog=None`），是「纯延迟」基准；兄弟文件 `0.torch_benchmark` 才展示完整校验。
- **调试接口**：`tilelang.disable_cache()`（模块级，禁编译缓存）与 `best_result.kernel.get_kernel_source()`（打印生成的 CUDA/HIP 源码），分别解决「改了没生效」与「想看落地指令」两类问题。
- **再次印证「以代码为准」**：主线文件第 212 行的 `ref_program` 是「绑定即闲置」、dense_matmul 第 278 行 `(s)` 标签单位错（实为 ms）、两文件取 `ref_latency` 却不配 `ref_prog`——读评估代码时只信实际调用，不信注释与变量名。

## 7. 下一步学习建议

- **横向**：若想看「调优时内置 ref 校验（写法 ③）」的**完整**示例，可去 tilelang 上游仓库找带 `ref_prog=<callable>` 的 `@jit` 用例；本仓库内的算子普遍像主线文件一样省略了它。这是本讲留下的「待确认」点，值得在能跑的环境里亲手补全并观察 `best_result.ref_latency` 的取值。
- **纵向（下一讲 u5-l19）**：进入 **block-sparse attention**——它复用本讲的 `compile + get_profiler + do_bench` 脚手架，但内核里多了「按块掩码条件跳过 MMA」的逻辑，且 `do_bench(input_tensors=[...])` 会展示「带显式输入」的第三种 `do_bench` 调用形态，是对本讲 4.3 的延伸。
- ** deeper（u6-l20/u6-l22）**：MLA decode 会用 `tilelang.disable_cache()` + 多个 `@T.prim_func`（main_split/main_no_split）+ 显式 `AutoTuner` 组合多内核，那时你会看到本讲的 compile/profiler 机制如何扩展到「一个算子多个内核」的复杂场景。
