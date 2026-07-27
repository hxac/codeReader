# 显式 AutoTuner API 与多内核组合

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 TileLang 中「装饰器式调优」与「显式 `AutoTuner` 式调优」这两条入口在写法上的差异，并能分别写出它们的 config 传入方式与结果取出方式。
- 读懂 `AutoTuner.from_kernel(...).set_compile_args(...).run(...)` 这条链式调用每一步在做什么。
- 理解一个算子工厂里可以定义**多个 `@T.prim_func`**，并按参数（如 `num_split`）**条件返回**不同的内核结构。
- 理解 `tilelang.compile(program, out_idx=[...])` 如何把「被选中的那一个 prim_func」固化成可运行、可计时的 kernel，并把 `out_idx` 与多内核的参数顺序对上。

本讲是「高级机制」单元的第三篇，承接 u6-l20（MLA decode 的 split-KV 与 combine 数学）。u6-l20 讲的是「内核里算了什么」，本讲讲的是「这套内核在 Python 驱动层是怎么被定义、挑选、调优、编译的」——即工程化的「外壳」，不再重复 combine 的在线 log-sum-exp 数学。

## 2. 前置知识

在进入本讲前，请确认你已理解以下概念（前序讲义已建立）：

- **`@T.prim_func`**：TileLang 里一段声明式、可被编译成 GPU 代码的底层计算函数（u3-l8、u3-l9）。
- **`@T.macro`**：可被内联进 prim_func 的「计算片段」，类似函数但会被展开进调用处（u5-l16 用它拆 FlashAttention 的四段 MMA/Softmax）。
- **config / 搜索空间**：一组 dict，每个 dict 描述一组调度参数（如 `block_N`、`num_stages`），调优器会逐个编译计时（u3-l8、u3-l10）。
- **`@autotune` + `@jit` 装饰器**：把「定义内核 → 遍历 config → 编译计时 → 取最优」打包成一次 `kernel()` 调用，返回 `best_result` 对象（u3-l8）。
- **`out_idx`**：声明内核输出参数在参数列表里的下标，编译器据此分配输出张量（u5-l18）。
- **`tilelang.compile`**：非调优路径下，把一个 prim_func 直接编译成 kernel，再用 `get_profiler().do_bench` 计时（u5-l18）。
- **MLA 的 split-KV**：把 KV 沿长度切成 `num_split` 段并行，每段算出局部 LSE 与局部输出，再由 combine 合并（u6-l20）。

一句话复习：**装饰器式**把「内核定义」和「调优流程」焊在一起；**显式 `AutoTuner` 式**把它们拆开，让你先拿到一个「能产出 prim_func 的普通 Python 函数」，再交给 `AutoTuner` 去遍历。这种拆分恰好适合「一个工厂、多个内核、按条件挑选」的场景。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py` | 主线文件。同时演示「多 prim_func + 条件返回」「`tilelang.compile` 评估」「显式 `AutoTuner` 调优」三种机制，是本讲最重要的样本。 |
| `hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py` | 显式 `AutoTuner` 的最简写法（省略 `set_compile_args`、`run()` 不传参），用于对照「最短可用形态」。 |
| `hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py` | 装饰器式 `@autotune`+`@jit` 的代表作（u3-l8 已精读），本讲只取其「入口与返回」做对照，不重复内核细节。 |

辅助参照（仅引用其调用形态，不展开）：`cdna_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py` 与 `cdna_benchmark/blocksparse_attention/2.torch-becnhmark/benchmark_torch_bsa.py` 里也有同一套显式 `AutoTuner` 的写法（后者与 mla 完全同构，前者留有注释掉的旧式构造器写法）。

## 4. 核心概念与源码讲解

本讲按四个最小模块组织：先对比两种调优入口（4.1），再拆显式 API 的三步链（4.2），接着讲多 prim_func 与条件返回（4.3），最后讲 `tilelang.compile` 如何把选中的内核固化（4.4）。

### 4.1 两种调优入口：装饰器式 vs 显式式

#### 4.1.1 概念说明

「调优」本质是同一件事：**给定一组 config，逐个把内核编译出来、跑若干次计时，挑延迟最低的那一个**。TileLang 给了两条入口去描述这件事：

- **装饰器式**：用 `@autotune(configs=...)` 叠在 `@jit(out_idx=..., supply_type=..., target=...)` 之上，再去装饰一个「参数全为 `None`」的 `kernel()` 函数。config 的每个字段对应 `kernel()` 的一个形参，调优器把每个 config dict 当 kwargs 注入。**调用 `kernel()`（不传参）即触发整个搜索**，返回 `best_result`。
- **显式式**：把「能产出 prim_func 的普通 Python 函数」（下文称 **kernel 工厂**）和「config 列表」分别作为两个独立对象，交给 `AutoTuner.from_kernel(...)` 绑定，再用 `.set_compile_args(...)`、`.run(...)` 显式驱动。

两者底层做的事一样，区别在于**「内核定义」与「调优驱动」是否耦合**：装饰器式把它们焊死在一个嵌套函数里，写起来短；显式式把它们拆开，工厂是一个普通的、可被任意调用的 Python 函数，于是你可以在调优之外单独 `tilelang.compile` 它、单独传不同的 `num_split` 让它返回不同结构的内核——这正是 4.3「多内核组合」所需要的灵活性。

#### 4.1.2 核心流程

两种入口的对照流程：

```
装饰器式：
  configs ──┐
            ├──> @autotune + @jit ──> kernel()  ──> best_result
  (out_idx, supply_type, target 写在 @jit 里)        (.latency/.config/.ref_latency/.kernel)

显式式：
  工厂函数(返回 prim_func) ──┐
  configs              ──────┼──> AutoTuner.from_kernel(...)
                             └──.set_compile_args(supply_type, target)
                                 .run(warmup, rep) ──> tune_result
                                                       (.latency/.config)
  (out_idx 不在链上；要拿到可运行 kernel，需另行 tilelang.compile(program, out_idx=...))
```

关键差异点有三处，后续模块会逐一落到源码：

1. **config 怎么传**：装饰器式作为 `@autotune(configs=...)` 的参数；显式式作为 `from_kernel(kernel=..., configs=...)` 的第二个参数。
2. **`out_idx` 在哪声明**：装饰器式写在 `@jit(out_idx=...)` 里，返回的 `.kernel` 已经绑好输出；显式式的链上**不出现** `out_idx`，需另用 `tilelang.compile` 指定。
3. **结果怎么取**：装饰器式拿 `best_result`（字段更多，含 `.kernel`/`.ref_latency`）；显式式拿 `tune_result`（本仓观测到的用法只取 `.latency`/`.config`）。

#### 4.1.3 源码精读

装饰器式入口，以 dense matmul 为例。`@autotune` 在外、`@jit` 在内，`kernel()` 的七个形参全为 `None`：

[benchmark_tilelang_matmul.py:142-160](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L142-L160) — `@autotune(configs=get_configs(...), warmup=3, rep=20)` 叠 `@jit(out_idx=[2], supply_type=..., target="auto")`，装饰参数全 `None` 的 `kernel()`。`out_idx=[2]` 在此处声明，意味着返回结果里的 `.kernel` 已把第 3 个参数（C）绑为输出。

调用处只需 `return kernel()`，即触发搜索：

[benchmark_tilelang_matmul.py:248](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L248) — 不传任何参数地调用被装饰的 `kernel()`，返回的就是 `best_result`。

外层取出结果，字段是对象属性：

[benchmark_tilelang_matmul.py:271-275](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L271-L275) — `best_result.latency / .config / .ref_latency / .kernel`，注意还能直接 `best_result.kernel.get_kernel_source()` 打印生成的 CUDA 源码。

对照显式式入口，以 mla decode 为例（同一条链拆成三步看）：

[benchmark_mla_decode_amd_tilelang.py:336-341](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L336-L341) — `AutoTuner.from_kernel(kernel=wrapped_kernel, configs=get_configs()).set_compile_args(supply_type=..., target="auto")`，再 `.run(warmup=3, rep=20)` 得到 `tune_result`。注意链上**没有** `out_idx`。

> 提醒：dense matmul 的注释多处与代码不符（注释写 half-precision、代码是 int8），u3-l8、u4-l12 已反复强调「以代码为准」，本讲只引用其「入口与返回」结构，不依赖注释。

#### 4.1.4 代码实践

**实践目标**：把两种入口的「config 传入」与「结果取出」逐字段对齐。

**操作步骤**：

1. 打开 `hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py`，定位 `@autotune`、`@jit`、`return kernel()`、`best_result = matmul(...)` 四处。
2. 打开 `cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py`，定位 `AutoTuner.from_kernel(...)` 链与 `tune_result = autotuner.run(...)`。
3. 在笔记里画一张两列对照表（见下方「预期结果」）。

**需要观察的现象**：装饰器式的 `out_idx` 出现在 `@jit` 里；显式式的链里搜不到 `out_idx`，它出现在文件上方的 `tilelang.compile(program, out_idx=[6])`。

**预期结果**（参考答案，可直接填入笔记）：

| 维度 | 装饰器式（dense matmul） | 显式式（mla decode） |
| --- | --- | --- |
| config 传入 | `@autotune(configs=get_configs(...))` | `AutoTuner.from_kernel(kernel=..., configs=get_configs())` |
| `out_idx` 在哪 | `@jit(out_idx=[2], ...)` | 链上无；另写 `tilelang.compile(program, out_idx=[6])` |
| supply_type / target | `@jit(supply_type=..., target="auto")` | `.set_compile_args(supply_type=..., target="auto")` |
| 触发搜索 | `kernel()`（无参调用） | `autotuner.run(warmup=3, rep=20)` |
| 结果对象 | `best_result` | `tune_result` |
| 可取字段 | `.latency .config .ref_latency .kernel` | `.latency .config`（本仓观测用法） |

#### 4.1.5 小练习与答案

**练习 1**：如果把装饰器式 `kernel()` 的某个形参（如 `block_M=None`）去掉默认值、写成 `block_M`，会发生什么？

**答案**：`@autotune` 依赖「形参全为 `None` 占位」的约定，把每个 config dict 的键当作 kwargs 注入。去掉默认值后，`kernel()` 无参调用会因缺少必填参数而报错，搜索无法启动。这正是 u3-l8 强调的「被装饰函数参数全设 `None` 作占位符」。

**练习 2**：显式式里 `tune_result.kernel` 能像 `best_result.kernel` 那样直接取吗？

**答案**：在本仓观测到的用法里，mla decode 只取了 `tune_result.latency` 与 `tune_result.config`，并未使用 `.kernel`；该对象是否暴露 `.kernel` 字段**待确认**（无法访问 tilelang 包源码验证）。若需要可运行 kernel，本文件的做法是另行 `tilelang.compile(program, out_idx=[6])`（见 4.4），而非从 `tune_result` 取。

---

### 4.2 `AutoTuner.from_kernel` + `set_compile_args` + `run`

#### 4.2.1 概念说明

显式调优由三个动作串成一条链：

- **`from_kernel(kernel, configs)`**：把「kernel 工厂」与「config 列表」绑定成一个 `AutoTuner` 对象。工厂是一个普通 Python 函数，**形参为各 config 字段（默认 `None`）**，**返回值是一个 `@T.prim_func`**。调优器逐个 config，把 dict 当 kwargs 喂给工厂，拿到对应的 prim_func 去编译计时。
- **`set_compile_args(...)`**：设置「编译/剖析时」的参数。本仓观测到的用法传入 `supply_type`（profiler 用什么策略填充测试输入张量）与 `target`（编译后端，如 `"auto"`）。这与装饰器式 `@jit(supply_type=, target=)` 是同一组概念，只是换了安放位置。
- **`run(warmup=, rep=)`**：真正执行搜索，返回最优结果对象（`.latency`、`.config`）。

引入两个术语：**kernel 工厂**（接受 config kwargs、返回 prim_func 的普通函数）；**supply_type**（profiler 的输入填充策略，如 `TensorSupplyType.Randn`、`TensorSupplyType.Integer`）。

#### 4.2.2 核心流程

显式调优的伪代码：

```
def wrapped_kernel(block_N=None, block_H=None, num_split=None, thread_num=None):
    return flashmla_decode(..., block_N, block_H, num_split, thread_num)  # 返回一个 prim_func

configs = [{"block_N":.., "block_H":.., "num_split":.., "thread_num":..}, ...]

autotuner = AutoTuner.from_kernel(kernel=wrapped_kernel, configs=configs)
autotuner = autotuner.set_compile_args(supply_type=Randn/Integer, target="auto")
tune_result = autotuner.run(warmup=3, rep=20)
# 对每个 config：把 dict 当 kwargs 调 wrapped_kernel → 得 prim_func → 编译 → warmup 次 + rep 次计时 → 记 Best
print(tune_result.latency, tune_result.config)
```

注意 `run` 的两个参数与装饰器式 `@autotune(warmup=, rep=)` 同义：`warmup` 是计时前热身次数（结果丢弃），`rep` 是正式计时次数、取最优。这与 u2-l4 讲的「TileLang 用固定 warmup+rep」一致。

#### 4.2.3 源码精读

config 列表由 `get_configs()` 用 `itertools.product` 暴搜生成（4 维 × 各档位 = 4×4×6×2 = 192 个 config）：

[benchmark_mla_decode_amd_tilelang.py:311-326](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L311-L326) — 注意这里没有 Roller 分支（对照 u3-l10 的 `with_roller`），是纯笛卡尔积暴搜；`num_split` 也在搜索空间里 `[1,2,4,8,16,32]`，这一点在 4.3 会成为关键。

kernel 工厂 `wrapped_kernel`，形参全 `None`，返回 `flashmla_decode(...)` 的结果（一个 prim_func）：

[benchmark_mla_decode_amd_tilelang.py:329-333](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L329-L333) — 工厂本身**没有任何装饰器**，是一个普通函数；它把 config 字段透传给 `flashmla_decode`，由后者决定返回哪个 prim_func。

三步链与结果取出：

[benchmark_mla_decode_amd_tilelang.py:336-346](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L336-L346) — `from_kernel` → `set_compile_args(supply_type=Integer, target="auto")` → `run(warmup=3, rep=20)` → `tune_result.latency / .config`。

**最简形态对照**：fp16xint4 的反量化 GEMV 把这条链压到最短——省略 `set_compile_args`（用默认）、`run()` 不传 `warmup/rep`（用默认）：

[benchmark_tilelang_matmul_fp16xint4.py:195-198](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L195-L198) — `tilelang.autotuner.AutoTuner.from_kernel(tune_kernel, get_configs())` 用**位置参数**（不写 `kernel=`/`configs=`），且**直接 `.run()`**。说明 `set_compile_args` 与 `run` 的参数都有默认值，可省略。

> 补充：blocksparse attention 的 tilelang 版本里留有一行被注释的旧式写法 `tilelang.autotuner.AutoTuner(tune_kernel, get_config()).set_compile_args(...)`（见 `cdna_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py:249`），即用构造器 `AutoTuner(...)` 而非 `AutoTuner.from_kernel(...)`。这是历史/备用拼写，本项目实际启用的是 `from_kernel` 形式；该构造器签名的完整参数**待确认**。

#### 4.2.4 代码实践

**实践目标**：跟踪一个 config 是如何被注入工厂并产出 prim_func 的。

**操作步骤**：

1. 在 `benchmark_mla_decode_amd_tilelang.py` 的 `get_configs()`（311-326 行）里，取第一个 config `{"block_N":16, "block_H":16, "num_split":1, "thread_num":128}`。
2. 沿 `wrapped_kernel(329-333)` 把这些值透传进 `flashmla_decode(..., block_N=16, block_H=16, num_split=1, thread_num=128)`。
3. 进入 `flashmla_decode`，在 226-229 行看 `num_split > 1` 这个分支判断：`num_split=1` 时会返回哪个 prim_func？

**需要观察的现象**：`num_split=1` 走 `else` 分支返回 `main_no_split`；`num_split=2/4/8/16/32` 走 `if` 分支返回 `main_split`。也就是说，**同一个搜索空间里同时包含了两种不同结构的内核**——这是显式 API + 工厂模式才能自然表达的（见 4.3）。

**预期结果**：192 个 config 中，`num_split=1` 的 32 个（4×4×1×2）映射到 `main_no_split`，其余 160 个映射到 `main_split`；调优器把两者放在同一张延迟表里比较，挑全局最优。这一结论**待本地验证**（可在 `run` 后打印 `tune_result.config` 看最优 config 的 `num_split` 落在哪边）。

#### 4.2.5 小练习与答案

**练习 1**：mla 用 `set_compile_args(supply_type=Integer)`，而 fp16xint4 完全省略了 `set_compile_args`。省略意味着什么？

**答案**：`supply_type` 与 `target` 都有默认值（fp16xint4 的 `.run()` 也用默认 warmup/rep），省略即用默认。本项目里显式写 `set_compile_args` 通常是为了指定 `supply_type`（如 `Integer`/`Randn`）以匹配内核期望的输入分布，或固定 `target`。

**练习 2**：为什么 mla 的 `from_kernel` 传入的是 `wrapped_kernel` 而不是 `flashmla_decode` 本身？

**答案**：`flashmla_decode` 的前几个参数是 `batch, heads, kv_head_num, ...`（固定形状），只有末尾的 `block_N, block_H, num_split, thread_num` 是调优旋钮。`wrapped_kernel` 把这些旋钮提到形参（默认 `None`）并固定其余形状，正好匹配「config dict 当 kwargs 注入」的约定；直接传 `flashmla_decode` 会因多了固定形状的必填参数而无法被 autotuner 正确调用。

---

### 4.3 多 `prim_func` 与条件返回

#### 4.3.1 概念说明

u3-l9 讲的 GEMM 内核是「一个工厂、一个 `@T.prim_func`」。但在 MLA decode 这种场景下，**算子结构本身会随参数变化**：

- `num_split == 1`：KV 不切分，一个内核 `flash_attn` 走完即可（**单内核路径**）。
- `num_split > 1`：KV 切成多段，需要先并行算各段（`flash_attn_split`）、再用 `combine` 合并各段的 LSE 与输出（**两内核流水路径**）。

TileLang 的做法是：在一个 Python 工厂函数里定义**多个 `@T.macro`（可复用的计算片段）**和**多个 `@T.prim_func`（可编译的入口）**，再用一个普通 `if/else` **条件返回**其中的一个 prim_func。调优器或 `tilelang.compile` 拿到的是「被选中的那一个」。

引入术语：**多内核组合**——一个算子由多个 prim_func 协作完成（如 `main_split` 内部依次调用 `flash_attn_split` 和 `combine`）；**条件返回**——工厂按参数返回不同的 prim_func，把「结构选择」也变成可调优的维度。

> 数学背景（u6-l20 已详述，这里只回顾要点）：split 路径里每个 split 段写出段内 base-2 LSE（`glse`）与段内已归一输出（`Output_partial`）；combine 用 \(\text{lse\_max}\to\exp2\to\log2\) 三步做在线 log-sum-exp，把各段条件 softmax 升级为全局 softmax，合并系数之和恰为 1。

#### 4.3.2 核心流程

`flashmla_decode` 工厂内部的结构分层：

```
flashmla_decode(...):           # 工厂：返回一个 prim_func
  ├─ @T.macro flash_attn(...)        # 单段注意力（用于 no_split）
  ├─ @T.macro flash_attn_split(...)  # 切段注意力（用于 split，写出 glse/Output_partial）
  ├─ @T.macro combine(...)           # 合并各段 LSE 与输出（用于 split）
  ├─ @T.prim_func main_split(...):   # = flash_attn_split(...) ; combine(...)
  ├─ @T.prim_func main_no_split(...):# = flash_attn(...)
  └─ return main_split if num_split > 1 else main_no_split
```

两个 prim_func 的关键区别：

| | `main_split` | `main_no_split` |
| --- | --- | --- |
| 内部调用 | `flash_attn_split` + `combine`（两步） | `flash_attn`（一步） |
| 网格 | 多一个 `bz=num_split` 维（split 并行） | 无 split 维 |
| 是否用 `glse`/`Output_partial` | 是（中转张量） | 否（参数仍在签名里但 `flash_attn` 不写它们） |
| 触发条件 | `num_split > 1` | `num_split == 1` |

#### 4.3.3 源码精读

两个 prim_func 的定义与内部组合。`main_split` 依次调用 `flash_attn_split` 与 `combine`，形成两内核流水：

[benchmark_mla_decode_amd_tilelang.py:201-212](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L201-L212) — 注意 `main_split` 的函数体只是两行「调用 `flash_attn_split(...)`、再调用 `combine(...)`」；它把多个 `@T.macro` 串成一个可编译的入口。

`main_no_split` 只调用单个 `flash_attn`：

[benchmark_mla_decode_amd_tilelang.py:214-224](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L214-L224) — 两个 prim_func 的**参数签名完全相同**（都是 7 个张量参数），这是为了让下游 `tilelang.compile(out_idx=[6])` 无论选哪个都能用同一套 `out_idx` 与同一套输入张量来剖析（profiler 分配输入时不必关心走哪条路径）。

条件返回，把「结构选择」变成普通 Python 分支：

[benchmark_mla_decode_amd_tilelang.py:226-229](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L226-L229) — `if num_split > 1: return main_split else: return main_no_split`。

被组合的两个 macro 的「签名头」可顺带一看，理解它们各自需要什么缓冲（内部数学见 u6-l20，不在此重复）：

[benchmark_mla_decode_amd_tilelang.py:95-106](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L95-L106) — `flash_attn_split` 的 `T.Kernel` 多了第三维 `num_split`（绑定到 `bz`），并在循环里按 `kv_start/kv_end` 只取本段 KV。

[benchmark_mla_decode_amd_tilelang.py:164-170](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L164-L170) — `combine` 的 `T.Kernel(heads, batch, threads=128)`，逐 head/batch 合并 `num_split` 段。

> 关键洞察：因为 `num_split` 同时是 (a) 条件返回的开关、(b) 4.2 搜索空间的一维，所以**显式 AutoTuner 在调优 tile 尺寸的同时，也在调优「要不要 split、切几段」这一结构决策**。这是装饰器式（单一嵌套 `kernel()`）不容易自然表达的——装饰器式更擅长「同一个内核、多组 tile 参数」。

#### 4.3.4 代码实践

**实践目标**：把 `num_split > 1` 时 `main_split` 调用的两个子内核及其输入输出张量梳理清楚。

**操作步骤**：

1. 读 `main_split`（201-212 行），列出它调用的两个 macro 名。
2. 读 `flash_attn_split`（95-162 行）的签名，标出它**写出**的两个张量（看 `T.copy(..., glse[...])` 与 `T.copy(..., Output_partial[...])`）。
3. 读 `combine`（164-199 行）的签名，标出它**读入**的 `glse`/`Output_partial` 与**写出**的 `Output`。
4. 画出 `flash_attn_split → (glse, Output_partial) → combine → Output` 的数据流。

**需要观察的现象**：`glse` 与 `Output_partial` 是两个 prim_func 之间的「中转张量」——`flash_attn_split` 写、`combine` 读；它们的存在仅因为拆成了两段，`main_no_split` 路径里 `flash_attn` 直接写 `Output`、不需要它们（虽然签名里仍保留）。

**预期结果**：

| 子内核 | 读入 | 写出 |
| --- | --- | --- |
| `flash_attn_split` | Q, Q_pe, KV, K_pe | **glse**, **Output_partial** |
| `combine` | glse, Output_partial | **Output** |

（`main_no_split` 的 `flash_attn`：读 Q, Q_pe, KV, K_pe，直接写 Output。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `main_split` 与 `main_no_split` 要保持**完全相同**的参数签名（都是 7 个张量）？

**答案**：为了让下游统一处理——`tilelang.compile(program, out_idx=[6])` 不必关心 `program` 是哪一个 prim_func，`out_idx=[6]` 都指向 `Output`；profiler 分配输入张量时也只需按同一套签名构造，无论走 split 还是 no_split。这是「多内核、统一外壳」的关键约定。

**练习 2**：如果不把 `num_split` 放进搜索空间、而是写死 `num_split=4`，调优器还能比较「split vs no_split」吗？

**答案**：不能。写死 `num_split=4` 后条件返回恒走 `main_split`，调优器只在 split 这一种结构内比 tile 尺寸。把 `num_split` 放进搜索空间（含 `1`），才让两种结构同台竞技。代价是搜索空间变大（本例 192 个 config）。

---

### 4.4 `tilelang.compile` 组合多内核

#### 4.4.1 概念说明

无论 prim_func 是单内核还是多内核组合、是手挑的还是调优器选出的，最后都要被 `tilelang.compile(program, out_idx=[...])` 固化成一个**可运行、可计时**的 kernel 对象。`out_idx` 告诉编译器「参数列表里第几个是输出」，profiler 据此分配输出张量并在计时后回收。

mla decode 文件同时存在**两条评估路径**，由命令行 `--auto_tune` 开关切换：

- **默认（非调优）路径**：用写死的 `num_split=4` 调 `flashmla_decode` 拿到 `main_split`，`tilelang.compile` 固化，跑 `ref_program` 校验正确性，再 `do_bench` 计时。这条路径**总是执行**。
- **调优路径**：仅当传 `--auto_tune` 时执行，跑 4.2 的显式 `AutoTuner`，输出最优 latency/config。

这与 u5-l18 讲的「tune 与非 tune 两条主流程」是同一思想，只是这里非调优路径用 `tilelang.compile`、调优路径用显式 `AutoTuner`。

#### 4.4.2 核心流程

非调优路径的伪代码：

```
program = flashmla_decode(..., BLOCK_N=32, BLOCK_H=64, num_split=4, threads=128)  # 返回 main_split
kernel  = tilelang.compile(program, out_idx=[6])           # 固化：第 7 个参数 Output 是输出
profiler = kernel.get_profiler(tensor_supply_type=Randn)
inputs   = profiler._get_inputs()
tilelang_output = kernel(*inputs)                          # 跑内核
ref_output      = ref_program(*inputs)                     # 跑 PyTorch 参考实现
torch.testing.assert_close(tilelang_output, ref_output, rtol=0.01, atol=0.01)  # 校验
latency = profiler.do_bench(warmup=500)                    # 计时（单位 ms）
```

`out_idx=[6]` 的数法：参数顺序为 `Q(0), Q_pe(1), KV(2), K_pe(3), glse(4), Output_partial(5), Output(6)`，故输出 `Output` 在下标 6。

#### 4.4.3 源码精读

`flashmla_decode` 返回 prim_func 并立即编译（写死 `num_split=4` → `main_split`）：

[benchmark_mla_decode_amd_tilelang.py:295-296](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L295-L296) — `program = flashmla_decode(...)` 拿到 prim_func，`kernel = tilelang.compile(program, out_idx=[6])` 固化。

正确性校验 + 计时（非调优路径的主体）：

[benchmark_mla_decode_amd_tilelang.py:298-307](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L298-L307) — `get_profiler(tensor_supply_type=Randn)` → `_get_inputs()` → 跑内核与 `ref_program` → `torch.testing.assert_close(rtol=0.01, atol=0.01)` → `profiler.do_bench(warmup=500)` → `total_flops / latency * 1e-9` 算 TFlops。

注意此处的 `do_bench(warmup=500)` 与调优链里 `run(warmup=3, rep=20)` 的差异：这里是「单点精测」（500 次热身、取统计延迟），调优里是「逐 config 快测」（3 热 20 测、取 Best 以控制总时长）。这是 u2-l4「测量稳定性」的两种取舍。

还有一处值得留意——文件顶部的 `tilelang.disable_cache()`：

[benchmark_mla_decode_amd_tilelang.py:12](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L12) — 模块级禁用编译缓存。u5-l18 已说明：调试时避免「改了内核却命中旧缓存」的错觉。开发期常开，正式跑分时可关以省编译时间。

> 单位提醒（贯穿第 2 单元）：`do_bench` 返回 ms，`total_flops / latency * 1e-9` 中的 `1e-9` 即 `1e-3`（ms→s）与 `1e-12`（FLOPS→TFlops）之积。调优链里 `tune_result.latency` 与这里的 `latency` 单位一致（ms），可直接比较。

#### 4.4.4 代码实践

**实践目标**：解释 `out_idx=[6]` 为何指向 `Output`，并验证两条路径的 latency 口径一致。

**操作步骤**：

1. 读 `main_split`（201-212 行）的参数列表，按下标 0…6 数出每个张量，确认下标 6 是 `Output`。
2. 在非调优路径（295-307 行）找到 `latency = profiler.do_bench(warmup=500)`，记下其 TFlops 打印公式。
3. 在调优路径（336-346 行）找到 `best_latency = tune_result.latency`，记下其 TFlops 打印公式。
4. 比较两处 `total_flops / latency * 1e-9` 是否相同。

**需要观察的现象**：两处 TFlops 公式完全一致，说明「调优选出的 Best」与「单点精测」用的是同一把尺子；区别仅在 warmup/rep 与「是否遍历 config」。

**预期结果**：`out_idx=[6]` 对应 `Output`（下标从 0 起：Q=0, Q_pe=1, KV=2, K_pe=3, glse=4, Output_partial=5, Output=6）。两条路径 TFlops 公式相同，单位均为 ms→TFlops（`*1e-9`）。是否在真实 MI300X 上运行**待本地验证**（本仓为归档项目，需自备 AMD GPU 与 tilelang/tvm 环境）。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `main_split` 的参数顺序改成把 `Output` 放第一个，需要同步改什么？

**答案**：`out_idx` 必须从 `[6]` 改成 `[0]`。`out_idx` 是按参数位置声明输出的，参数顺序一变它就得跟着变；否则 profiler 会把别的张量当输出分配/回收，校验与计时都会错。

**练习 2**：为什么非调优路径用 `warmup=500`，而调优路径用 `warmup=3, rep=20`？

**答案**：调优要遍历上百个 config，每个若都热身 500 次则总时长不可接受，故用小 warmup/rep 控制「每 config」的开销、只取相对最优；非调优路径只测一个已选定的内核，用大 warmup 换取更稳的绝对延迟。这是 u2-l4「测量稳定性 vs 调优成本」的典型权衡。

---

## 5. 综合实践

把本讲四条线索串成一个任务：**用两种入口分别驱动同一个「多内核算子」，并比较它们的 config 传入与结果取出**。

任务背景：mla decode 的 `flashmla_decode` 工厂天然支持多内核（`num_split>1` 走 `main_split`，否则走 `main_no_split`）。请你：

1. **显式式（已存在）**：阅读 `benchmark_mla_decode_amd_tilelang.py` 的 311-346 行，写出：(a) `get_configs()` 产出多少个 config；(b) `wrapped_kernel` 把哪些字段透传给工厂；(c) `tune_result` 取了哪两个字段。
2. **装饰器式（假设改造）**：如果把同一个 `flashmla_decode` 改写成装饰器式（参考 dense matmul 的 142-160 行），你会把 `@autotune(configs=...)` 与 `@jit(out_idx=, supply_type=, target=)` 叠在一个怎样的 `kernel()` 上？写出这个 `kernel()` 的形参列表（提示：旋钮是 `block_N/block_H/num_split/thread_num`，全设 `None`），并指出 `out_idx` 应取何值。
3. **对比结论**：用一句话回答 spec 提出的核心问题——两种写法在「config 传入方式」与「结果取出方式」上的差异。

参考答案：

1. (a) `4×4×6×2 = 192` 个 config；(b) `block_N, block_H, num_split, thread_num` 四个字段；(c) `tune_result.latency` 与 `tune_result.config`。
2. 形如：

   ```python
   @autotune(configs=get_configs(), warmup=3, rep=20)
   @jit(out_idx=[6], supply_type=tilelang.TensorSupplyType.Integer, target="auto")
   def kernel(block_N=None, block_H=None, num_split=None, thread_num=None):
       return flashmla_decode(batch, heads, kv_heads, kv_ctx, dim, pe_dim,
                              block_N, block_H, num_split, thread_num)
   ```
   `out_idx=[6]`（`Output` 是第 7 个参数）。注意：装饰器式要求 `kernel()` 无参调用即触发搜索并返回结果对象，而此处 `kernel()` 内部 return 的是 prim_func——能否直接被 `@autotune` 当作「逐 config 编译计时」的对象，取决于装饰器对「返回 prim_func 的工厂」的支持情况，**待本地验证**；这正是本项目对 MLA 选择**显式 `AutoTuner`** 而非装饰器的可能原因（显式 API 明确接受「返回 prim_func 的工厂」）。
3. **config 传入**：装饰器式作为 `@autotune(configs=...)` 的参数；显式式作为 `from_kernel(kernel=, configs=)` 的第二参数。**结果取出**：装饰器式 `best_result`（含 `.latency/.config/.ref_latency/.kernel`，且 `.kernel` 已由 `@jit(out_idx=)` 绑好输出）；显式式 `tune_result`（本仓取 `.latency/.config`，`out_idx` 不在调优链上，需另用 `tilelang.compile(program, out_idx=)` 才能得到可运行 kernel）。

> 若你有 MI300X 环境，可进一步：运行 `python benchmark_mla_decode_amd_tilelang.py --auto_tune --kv_ctx 1024`，在 `run` 结束后打印 `tune_result.config`，观察最优 config 的 `num_split` 是 1 还是 >1——即 MLA decode 在该 KV 长度下到底更偏好 split 还是 no_split。该结果**待本地验证**。

## 6. 本讲小结

- TileLang 有两条调优入口：**装饰器式**（`@autotune`+`@jit` 装饰一个全 `None` 形参的 `kernel()`，无参调用即搜索，返回字段更全的 `best_result`）与**显式式**（`AutoTuner.from_kernel(kernel, configs).set_compile_args(...).run(...)`，返回 `tune_result`）。
- 显式 API 把「kernel 工厂」与「调优驱动」解耦：工厂是一个返回 prim_func 的普通 Python 函数，既能被 `AutoTuner` 调优，也能被 `tilelang.compile` 直接编译。
- `set_compile_args(supply_type=, target=)` 对应装饰器 `@jit` 的同名参数；两者以及 `run` 的 `warmup/rep` 都有默认值，可省略（fp16xint4 即最简形态）。
- 一个算子工厂可定义**多个 `@T.macro` 与多个 `@T.prim_func`**，并用普通 `if/else` **条件返回**其中一个；mla decode 据 `num_split>1` 在 `main_split`（flash_attn_split+combine 两内核流水）与 `main_no_split`（单 flash_attn）间分发。
- 把 `num_split` 放进搜索空间，使显式 AutoTuner 在调 tile 尺寸的**同时**调「要不要 split、切几段」这一结构决策——这是显式 API 相对装饰器式的独特优势。
- 无论哪条路径，最终都用 `tilelang.compile(program, out_idx=[6])` 固化内核；`out_idx` 按参数位置声明输出，两个 prim_func 保持相同签名以共用同一 `out_idx`。

## 7. 下一步学习建议

- **回到工程全局**：本讲是 u6（高级机制）的收尾，建议接着读 u7-l23「对比基线生态总览」，把 TileLang 自家的调优/编译机制放到 cuBLAS/Triton/BitBLAS/Marlin/CUTLASS 等基线的全景里对照。
- **二次开发**：若你想为自己的算子写基准，u7-l25「新增一个算子基准」会给出目录约定与「内核 + shell + data/plot」的完整工程清单；本讲的「显式 AutoTuner + 多内核」正是其中「内核驱动层」可选的写法之一。
- **继续读源码**：对照 `cdna_benchmark/blocksparse_attention/2.torch-becnhmark/benchmark_torch_bsa.py:332-338`，它有一份与 mla 完全同构的显式 `AutoTuner` 调用，可作为脱离 MLA 语境的第二个练习样本；并留意 `blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py:249` 注释里的旧式构造器写法 `AutoTuner(...)`，体会 API 的演化痕迹。
- **若需核对 API 细节**：本讲对 `set_compile_args`/`run`/`tune_result` 的描述均以本仓实际调用为准；`tilelang` 包的完整签名（如 `set_compile_args` 是否还接受 `out_idx`、`tune_result` 是否暴露 `.kernel`）建议直接 `python -c "import tilelang; help(tilelang.autotuner.AutoTuner)"` 核对，相关结论标了「待确认」。
