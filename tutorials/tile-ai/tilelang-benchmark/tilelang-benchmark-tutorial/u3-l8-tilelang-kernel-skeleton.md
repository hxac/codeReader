# TileLang 内核骨架：autotune/jit 与 prim_func

## 1. 本讲目标

本讲是「TileLang 语言核心」单元的第一篇。读完本讲，你应该能够：

- 看懂一个 TileLang 算子文件「定义内核 → 自动调优 → 取出结果」的整体骨架；
- 说清 `get_configs`、`@autotune`、`@jit`、`best_result` 这「四件套」各自负责什么；
- 解释 `warmup=3, rep=20`、`out_idx=[2]`、`supply_type`、`target="auto"` 这些参数的含义；
- 在真实内核文件里准确标出「搜索空间生成 / 内核定义 / 结果取出」三段代码的位置。

本讲**只讲骨架**（装饰器如何驱动「定义—调优—评估」）。内核本体里的 `T.Kernel`/`T.copy`/`T.gemm`/`T.Pipelined` 等块级原语留给下一讲 u3-l9 逐行解剖，本讲遇到时只需知道「那里是真正的计算」即可。

## 2. 前置知识

本讲承接 u2-l6（Triton 基线）。先用三段话补齐两个概念。

**装饰器（decorator）是什么。** Python 里 `@xxx` 写在函数定义上方，等价于「先定义函数，再把它作为参数传给 `xxx`，用返回值替换原函数」。TileLang 用两个装饰器 `@autotune` 和 `@jit` 把你写的「普通函数」改造成「会自动搜索最优配置、能被编译执行的内核」。

**prim_func 是什么。** `@T.prim_func` 标记的函数是 TVM/TileLang 里的「底层计算函数」（primitive function）。它不是普通 Python，而是一段**声明式**的、可被编译成 GPU 机器码的计算描述——你声明「分配显存、搬运数据、做矩阵乘」，编译器负责把它翻译成 CUDA/HIP 指令。

**和 Triton 基线的对照（来自 u2-l6）。** Triton 用 `@triton.autotune` + 一组 `triton.Config(...)` 对象列出搜索空间，用 `triton.testing.do_bench` 计时。TileLang 走的是**另一套同源但不同形**的写法：

| 维度 | Triton（u2-l6） | TileLang（本讲） |
|---|---|---|
| 搜索空间 | 一组 `triton.Config` 对象 | 一组普通 `dict` |
| 调优入口 | `@triton.autotune` | `@autotune`（来自 `tilelang.autotuner`） |
| 编译入口 | `@triton.jit` | `@jit`（来自 `tilelang.autotuner`） |
| 计时统计 | `do_bench` 返回 ms，措辞用 "Mean" | 措辞用 "Best"（取最优 config 的延时） |

另外请记住 u2-l4 反复强调的「**单位陷阱**」：打印标签写的单位，未必是真实单位——本讲会再次撞见这个坑。

## 3. 本讲源码地图

本讲围绕一个文件展开，辅以两个驱动/解析脚本：

| 文件 | 角色 |
|---|---|
| `hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py` | **主角**。包含 `get_configs`、`matmul`、被 `@autotune`+`@jit` 包裹的 `kernel`、以及 `__main__` 取结果四段。 |
| `hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh` | shell 驱动。按 shape 列表循环调用上面的 `.py`，把输出 tee 成日志。 |
| `hopper_benchmark/dense_matmul/3.tilelang-benchmark/extract_benchmark_results.py` | 解析日志。用正则从「Best latency」行抽延时，验证输出单位。 |

> 注意：tilelang 语言本身的源码（`@autotune`/`@jit` 的内部实现）不在本仓库里，它来自外部 `tilelang` 包。本仓库是**基准测试套件**，只「使用」这套 API。因此本讲对装饰器内部行为的描述，依据的是本文件**如何使用**它们（参数名 + 可观测行为），而非包内部实现。

## 4. 核心概念与源码讲解

先给一张全局数据流图，把四件套串起来：

```
matmul(M, N, K, with_roller)            # 入口函数
  │
  ├─ get_configs(M,N,K,with_roller)     # 【4.1】产出搜索空间 [dict, dict, ...]
  │
  ├─ 定义 kernel（闭包，参数全 = None）  # 被 @autotune + @jit 包裹
  │     └─ @T.prim_func main(A,B,C): …  #   内核本体（u3-l9 详讲）
  │
  └─ return kernel()                    # 【4.2】触发调优  →  【4.4】返回 best_result
                                          # 【4.3】@jit 负责每次的编译配置

__main__
  ├─ best_result = matmul(M,N,K,with_roller)
  ├─ best_latency = best_result.latency
  └─ TFlops = 2*M*N*K / best_latency * 1e-9
```

下面按「空间 → 调优 → 编译 → 结果」的顺序拆四个最小模块。

### 4.1 搜索空间从哪里来：`get_configs`

#### 4.1.1 概念说明

**搜索空间（search space）** 是一组「候选配置」。每个配置是一个普通 `dict`，键是内核参数名（`block_M`、`block_N`、`block_K`、`num_stages`、`thread_num`、`policy`、`enable_rasteration`），值是该参数的一个具体取值。

「自动调优」本质上就是：遍历搜索空间里的每一个 `dict`，把它的值注入内核、编译、计时，最后挑出延时最小的那一个。所以搜索空间的**质量**直接决定调优效果：太大会跑很久，太小会错过更优解。

本文件提供**两条**生成搜索空间的路径，用 `with_roller` 开关切换。

#### 4.1.2 核心流程

```pseudo
get_configs(M, N, K, with_roller):
    if with_roller:                       # 路径 A：让 Roller 智能推导
        建一个 MatmulTemplate(M,N,K, dtypes).with_arch(CUDA)
        roller_hints = template.recommend_hints(topk=10)   # 只推 top-10 个高质量 hint
        把每个 hint 翻译成一个 config dict
    else:                                 # 路径 B：手工笛卡尔积暴搜
        对 7 个参数列表做 itertools.product
        把每个组合包成 dict
    return configs                        # List[dict]
```

#### 4.1.3 源码精读

函数定义与两分支骨架：[benchmark_tilelang_matmul.py:17-104](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L17-L104) —— `def get_configs(M, N, K, with_roller=False)`，`if with_roller:` 与 `else:` 两条路。

路径 A（Roller 智能推导）：[benchmark_tilelang_matmul.py:32-72](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L32-L72)。关键几句：

```python
carve_template = MatmulTemplate(M=M, N=N, K=N, ...).with_arch(CUDA("cuda"))
roller_hints = carve_template.recommend_hints(topk=10)   # 只取 10 个 hint
...
config["block_K"] = hint.rstep[0]
config["num_stages"] = hint.pipeline_stage
```

Roller 会根据目标架构的 Tensor Core 约束，**推导**出少量（这里是 10 个）高质量调度提示（block/warp/rstep/pipeline_stage/rasterization），从而把搜索空间从「天文数字」压到「十来个」。Roller 的原理留给 u3-l10 详讲，本讲只需知道：它产出**少量但靠谱**的 config。

路径 B（手工暴搜）：[benchmark_tilelang_matmul.py:73-104](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L73-L104)。关键几句：

```python
block_M  = [64, 128, 256]
block_N  = [64, 128, 256]
block_K  = [64, 128, 256]
num_stages = [0, 1, 2, 3]
thread_num = [128, 256]
policy   = [T.GemmWarpPolicy.Square]
enable_rasterization = [True, False]
_configs = list(itertools.product(block_M, block_N, block_K,
                                  num_stages, thread_num, policy,
                                  enable_rasterization))
```

注意第 101 行注释 `# keep param name for backward-compat`：字典键故意拼成 `enable_rasteration`（少一个 `i`），是为了向后兼容历史代码。这是历史遗留，读源码时不要被拼写迷惑——内核里用的也是同一个拼错的键名（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：感受两条路径产出的搜索空间量级差异。

**操作步骤**：

1. 打开 [benchmark_tilelang_matmul.py:75-91](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L75-L91)。
2. 手算路径 B 的笛卡尔积大小：把每个参数列表的长度相乘。

**需要观察的现象 / 预期结果**：

\[ 3 \times 3 \times 3 \times 4 \times 2 \times 1 \times 2 = 1296 \]

即**每个 shape 要评测 1296 个 config**。而路径 A 只评 10 个（`topk=10`）。两者相差约 130 倍——这就是 Roller 存在的意义。这个练习纯算术，无需 GPU，**可立即验证**。

#### 4.1.5 小练习与答案

**Q1**：路径 B 里如果把 `num_stages` 从 `[0,1,2,3]` 改成 `[1,2,3]`，搜索空间缩小多少？
**答**：从 1296 降到 \(3\times3\times3\times3\times2\times1\times2 = 972\)，少 324 个。

**Q2**：为什么 `policy` 列表只有一个元素 `Square`？
**答**：手工路径只探索这一种 warp 切分策略；其它策略（如 `from_warp_partition`）由 Roller 路径根据 hint 推导，见 [benchmark_tilelang_matmul.py:68](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L68)。warp 策略的细节属于 u3-l11。

### 4.2 消费搜索空间的驱动器：`@autotune`

#### 4.2.1 概念说明

`get_configs` 只产出「一张候选清单」，真正去遍历、编译、计时、选最优的，是 `@autotune` 装饰器。

把 `@autotune` 盖在一个函数上，这个函数就具备了「自动调优」能力：调用它时，不再是「执行一次函数体」，而是「跑完一整轮搜索后，返回一个最优结果对象」。它和 `@jit` 配合使用，`@autotune` 管「搜什么、怎么测」，`@jit` 管「编译成什么」（见 4.3）。

#### 4.2.2 核心流程

```pseudo
@autotune(configs=搜索空间, warmup=3, rep=20)
@jit(...)
def kernel(block_M=None, block_N=None, ...):   # 参数全 None，等 autotuner 注入
    @T.prim_func
    def main(A, B, C): ...
    return main

# 调用 kernel() 时，autotuner 内部：
for config in configs:                 # 遍历每个候选 dict
    用 config 的值填进 kernel 的 None 参数（block_M=64, block_N=128, ...）
    编译 main（由 @jit 的 target 决定后端）
    先跑 warmup 次「热身」（结果丢弃）
    再跑 rep 次，计时，记下延时
选出延时最小的那个 config → 打包成 best_result 返回
```

两个测量参数的含义：

- **`warmup=3`**：正式计时前先跑 3 次，结果丢弃。目的是把 GPU「热」起来——填满缓存、拉升频率，避免首次调用的冷启动偏差。
- **`rep=20`**：正式计时重复 20 次，取统计值。TileLang 取这 20 次里的最优（"Best"，见 u2-l6 对照），Triton 的 `do_bench` 默认 `warmup=25, rep=100` 取均值（"Mean"）。

为什么这里的数（3/20）比 Triton 默认（25/100）小？因为手工搜索空间有 1296 个 config，每个 config 都要 warmup+rep；放大 rep 会让总耗时爆炸。这是「搜索空间大小」与「单次测量精度」之间的工程权衡。

#### 4.2.3 源码精读

装饰器实参：[benchmark_tilelang_matmul.py:142-146](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L142-L146)

```python
@autotune(
    configs=get_configs(M, N, K, with_roller),
    warmup=3,
    rep=20,
)
```

注意三个细节：

1. `configs=` 直接吃 `get_configs` 的返回值（4.1 产出的 `List[dict]`）。
2. `@autotune` 只管「搜和测」，**不接** `target`、`out_idx` 这些编译参数——它们归下方的 `@jit`。
3. `warmup` / `rep` 是位置在「调优」这一层的原因：它们描述的是「如何测量一个 config 的延时」，属于搜索过程。

被装饰函数的参数全是 `None`：[benchmark_tilelang_matmul.py:152-160](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L152-L160)

```python
def kernel(block_M=None, block_N=None, block_K=None,
           num_stages=None, thread_num=None,
           policy=None, enable_rasteration=None):
```

这些 `None` 不是「默认不用」，而是**占位符**：autotuner 会从每个 config dict 里取同名键的值注入进来。键名（`block_M`、`enable_rasteration`…）必须和 `get_configs` 里字典的键**完全一致**（包括那个拼错的 `enable_rasteration`）。

#### 4.2.4 代码实践

**实践目标**：理解 warmup/rep 对测量稳定性的影响（定性）。

**操作步骤**：

1. 在 [benchmark_tilelang_matmul.py:142-146](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L142-L146) 处记下当前的 `warmup=3, rep=20`。
2. 阅读并回答：如果把 `warmup` 改成 `0`、`rep` 改成 `1`，测量结果可能出现什么问题？

**需要观察的现象 / 预期结果**：预期单个 config 的测时会变得不稳定、偏高（冷启动 + 单次采样无统计意义），最终 `best_latency` 可能偏大、`best TFlops` 偏低。**实际跑测需要 GPU + tilelang 环境，待本地验证。**

#### 4.2.5 小练习与答案

**Q1**：`@autotune` 和 `@jit` 谁负责选「最优 config」，谁负责「编译到 CUDA」？
**答**：`@autotune` 负责遍历 config、计时、选最优；`@jit` 负责把每个 config 实例编译成目标代码。两者协作：autotune 在每一轮里调用 jit 编译。

**Q2**：为什么 `kernel` 的参数必须全写成 `None`？
**答**：因为真正的值来自搜索空间。`None` 是占位符，让 autotuner 能在每轮把 config dict 的同名键注入进来。若写成固定值，就锁死了搜索空间，失去调优意义。

### 4.3 编译侧三参数：`@jit` 的 `out_idx` / `supply_type` / `target`

#### 4.3.1 概念说明

`@jit` 负责把 `@T.prim_func` 描述的内核**编译**成可运行、可评估的目标代码，并为 profiler 准备好「怎么测」。它接收三个关键参数：

| 参数 | 含义 |
|---|---|
| `out_idx=[2]` | 声明内核输出是第几个参数。profiler 据此分配输出显存、做正确性比对。 |
| `supply_type=tl.TensorSupplyType.Integer` | profiler 生成**测试输入**时的填充策略（这里填整数）。 |
| `target="auto"` | 编译目标，自动探测硬件（NVIDIA→CUDA）。 |

#### 4.3.2 核心流程

```pseudo
@jit(out_idx=[2], supply_type=Integer, target="auto")
def kernel(...):
    @T.prim_func
    def main(A: (M,K), B: (N,K), C: (M,N)): ...   # 三个张量，下标 0/1/2
    return main

# 编译/评估时：
# - target="auto" → 探测到 NVIDIA GPU → 用 CUDA 后端编译 main
# - out_idx=[2] → 把第 2 个参数 C 认定为输出
# - supply_type=Integer → 用整数值填充 A、B 作为测试输入
```

#### 4.3.3 源码精读

装饰器与内核签名：[benchmark_tilelang_matmul.py:147-160](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L147-L160)

```python
@jit(
    out_idx=[2],
    supply_type=tl.TensorSupplyType.Integer,
    target="auto",
)
def kernel(block_M=None, ...):
    ...
    @T.prim_func
    def main(
        A: T.Tensor((M, K), dtype),
        B: T.Tensor((N, K), dtype),
        C: T.Tensor((M, N), accum_dtype),
    ):
```

逐个解释三参数如何与内核对上：

- **`out_idx=[2]`**：`main` 有三个张量参数 `A, B, C`，下标依次是 0、1、2。`out_idx=[2]` 告诉编译器「`C` 是输出」。这样 profiler 知道该为 `C` 分配显存、该拿 `C` 去和参考实现比对正确性。
- **`supply_type=Integer`**：profiler 要给 `A`、`B` 灌测试数据。本内核是 int8 矩阵乘（见下方「注意」），用整数值填充输入最贴合语义。`tl.TensorSupplyType` 是 tilelang 提供的一组填充策略枚举。
- **`target="auto"`**：编译目标自动探测。在 NVIDIA GPU 上等价于走 CUDA 后端。

> **注意（读代码，不读注释）**：本内核实际用的是 `dtype = "int8"`、`accum_dtype = "int32"`（[第 188-189 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L188-L189)），驱动脚本 `benchmark_tilelang_matmul.sh` 里也只启用 `"int8 int8 int32 int32"` 这一组 dtype。但第 186-187 行注释却写「Use half-precision for input data... accumulate in float」——**注释是过时的，与代码不符**。同理，第 134-140 行的注释块写着「HIP as the compilation target」「the 'tvm' profiler backend」，但实际 `@jit` 用的是 `target="auto"`、且并没有 `profiler=` 这个参数。这正是 u2-l5/u2-l6 反复提醒的：**以代码为准，不盲信注释**。

#### 4.3.4 代码实践

**实践目标**：理解 `out_idx` 的位置语义。

**操作步骤**：

1. 在 [第 192-196 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L192-L196) 数出 `main(A, B, C)` 三个参数的下标。
2. 思考：如果把 `out_idx` 改成 `[0]`，profiler 会把谁当输出？

**需要观察的现象 / 预期结果**：`[0]` 会把 `A` 当输出。由于 `A` 是输入矩阵、形状是 `(M,K)` 而非结果 `(M,N)`，正确性比对（拿「输出」去和参考实现比）会错位甚至形状不匹配。**此为概念推演，待本地验证报错现象。**

#### 4.3.5 小练习与答案

**Q1**：`supply_type=Integer` 为什么和这个内核匹配？
**答**：因为内核 `dtype="int8"`，输入是整数张量，profiler 用整数值填充测试输入语义最贴合。若改成 Normal（正态浮点）填充，对 int8 输入语义不符。

**Q2**：把 `target="auto"` 改成 `target="cuda"` 在 NVIDIA 机器上行为会变吗？
**答**：基本不变——`"auto"` 在 NVIDIA GPU 上本就解析为 CUDA。显式写 `"cuda"` 只是去掉自动探测步骤。若要跑在 AMD MI300X 上，则需改成 `"hip"`（见 u6-l21 的 tvm.tl 变体）。

### 4.4 调优结果的取出：`best_result`

#### 4.4.1 概念说明

调优跑完后，`kernel()` 返回的**不是一个延时数字**，而是一个「调优结果对象」，本文件里命名为 `best_result`。它把「最优延时、最优配置、参考延时、编译好的内核」打包在一起，供调用方分别取用。

#### 4.4.2 核心流程

```pseudo
best_result = matmul(M, N, K, with_roller)   # 内部 return kernel()
#               └ kernel() 触发整轮调优，返回结果对象

best_latency   = best_result.latency      # 最优 config 的延时（见 4.4.3 单位说明）
best_config    = best_result.config       # 取得该延时的那一份 dict
ref_latency    = best_result.ref_latency  # 参考实现(torch)的延时，用于算 speedup
best_result.kernel.get_kernel_source()    # 取最优内核的源码（调试用）

TFlops = 2*M*N*K / best_latency * 1e-9
```

#### 4.4.3 源码精读

触发调优并返回结果：[benchmark_tilelang_matmul.py:246-248](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L246-L248) —— 内层 `return main`（交出 `@T.prim_func`），外层 `return kernel()`（触发 autotune，返回 `best_result`）。

取出四个字段并打印：[benchmark_tilelang_matmul.py:271-283](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L271-L283)

```python
best_result = matmul(M, N, K, with_roller)
best_latency = best_result.latency
best_config  = best_result.config
ref_latency  = best_result.ref_latency
print(best_result.kernel.get_kernel_source())

print(f"Best latency (s): {best_latency}")
print(f"Best TFlops: {total_flops / best_latency * 1e-9:.3f}")
print(f"Best config: {best_config}")
print(f"Reference TFlops: {total_flops / ref_latency * 1e-9:.3f}")
```

`best_result` 四个字段的含义：

| 字段 | 类型 | 含义 |
|---|---|---|
| `.latency` | float | 最优 config 的延时（单位见下方警告） |
| `.config` | dict | 取得该延时的那份配置（如 `{"block_M":128, "block_N":128, ...}`） |
| `.ref_latency` | float | 参考实现（torch）的延时，用于算 speedup：`ref_latency/latency` |
| `.kernel` | object | 编译好的最优内核对象，`.get_kernel_source()` 可导出其源码 |

> **注意 1（docstring 过时）**：`matmul()` 的 docstring（[第 124-131 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L124-L131)）声称返回一个三元组 `(best_latency, best_config, ref_latency)`，但实际返回的是带 `.latency/.config/.ref_latency/.kernel` 属性的**对象**。文档落后于代码，以代码为准。

> **注意 2（单位陷阱，承接 u2-l4/u2-l6）**：第 278 行打印标签写的是 `Best latency (s)`，但**真实单位是毫秒（ms）**。证据有二：一是 TFlops 公式 `total_flops / best_latency * 1e-9` 只有在 `best_latency` 以 ms 为单位时才成立（因为 \(\text{FLOP}/\text{ms} = 10^{3}\,\text{FLOPS}\)，再除以 \(10^{12}\) 得 TFlops，系数正是 \(10^{-9}\)）；二是同目录的 `extract_benchmark_results.py` 把这一行的数抽出来后直接打印成 `{latency:.3f} ms`。所以 `(s)` 是误导性标签，跨框架对比前必须统一成 ms。

#### 4.4.4 代码实践

**实践目标**：跟踪「日志 → 解析 → 单位」的链路，亲手验证延时单位。

**操作步骤**：

1. 读 [extract_benchmark_results.py:26-31](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/extract_benchmark_results.py#L26-L31)：它用正则 `(\d+\.\d+)` 从含 `"Best latency"` 的行抽第一个浮点数，然后 `print(f"{M}_{N}_{K}: {latency:.3f} ms")`。
2. 对照 [第 278 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L278) 的输出格式 `Best latency (s): <数值>`。
3. 得出结论：同一个数，内核标签叫它 `(s)`，extract 脚本叫它 `ms`——再加上 4.4.3 的公式推导，三方证据指向真实单位是 **ms**。

**预期结果**：能复述「标签 (s) 是 bug，真实单位 ms」的完整证据链。本实践为纯源码阅读，**可立即完成**。

#### 4.4.5 小练习与答案

**Q1**：想算 TileLang 相对参考实现的 speedup，应该用哪两个字段？
**答**：`best_result.ref_latency / best_result.latency`。>1 表示 TileLang 比参考（torch）快。

**Q2**：`.kernel.get_kernel_source()` 输出的是什么？
**答**：最优 config 编译后对应的内核源码（通常是生成的 CUDA/TIR 代码）。常用于调试——确认 autotune 最终选中的配置长什么样。本文件在 [第 275 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L275) 调用了它。

## 5. 综合实践

**任务**：在内核文件里标出「搜索空间生成 / 内核定义 / 结果取出」三段，并回答三个问题。这是本讲规格指定的核心实践。

**步骤**：

1. 打开 [benchmark_tilelang_matmul.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py)，用三色（或三个区间）标出：
   - **① 搜索空间生成**：`get_configs` 函数，[L17-L104](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L17-L104)。
   - **② 内核定义**：被 `@autotune`+`@jit` 包裹的 `kernel` 函数及其内部的 `@T.prim_func main`，[L142-L246](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L142-L246)。
   - **③ 结果取出**：`__main__` 里 `best_result = matmul(...)` 及其字段访问，[L271-L283](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L271-L283)。

2. **回答三问**（笔答）：
   - (a) `warmup=3, rep=20` 分别作用在调优的哪个环节？为什么这里比 Triton `do_bench` 默认值（25/100）小？
   - (b) 手工搜索空间（路径 B）一个 shape 要评多少个 config？Roller 路径呢？比值是多少？
   - (c) `best_latency` 的真实单位是什么？给出两条证据。

**预期结果（参考答案）**：

- (a) `warmup=3` 是每个 config 正式计时前的热身次数（丢弃）；`rep=20` 是正式计时的重复次数（取 Best）。数值小是因为搜索空间大（1296 个 config），放大 rep 会让总耗时爆炸，是精度与耗时的权衡。
- (b) 手工路径 1296 个；Roller 路径 `topk=10` 个；比值约 130:1。
- (c) 真实单位是 **ms**。证据一：TFlops 公式 `total_flops/latency*1e-9` 仅在 latency 为 ms 时成立；证据二：`extract_benchmark_results.py` 把该值打印为 `ms`。

## 6. 本讲小结

- TileLang 算子文件由「四件套」构成：`get_configs`（造搜索空间）→ `@autotune`（搜+测）→ `@jit`（编译）→ `best_result`（取结果）。
- `get_configs` 有两条路：`with_roller=True` 由 Roller 推 top-10 个高质量 config；`False` 用 `itertools.product` 暴搜出 1296 个。
- `@autotune(configs, warmup=3, rep=20)`：`warmup` 热身丢弃、`rep` 正式计时取 Best；它只管「搜与测」，不管编译。
- `@jit(out_idx=[2], supply_type=Integer, target="auto")`：`out_idx` 声明输出参数下标、`supply_type` 决定测试输入填充、`target` 决定编译后端。
- `best_result` 是对象（不是三元组），字段为 `.latency/.config/.ref_latency/.kernel`；`best_latency` 真实单位是 ms（标签 `(s)` 是 bug）。
- 本文件多处「注释与代码不符」（half-precision 注释 vs int8 代码、HIP 注释 vs `target="auto"`、三元组 docstring vs 对象返回），务必以代码为准。

## 7. 下一步学习建议

下一讲 **u3-l9《块级 GEMM 内核解剖》** 会钻进本讲故意跳过的 `@T.prim_func main` 内部，逐行讲 `T.Kernel` 的网格映射、`alloc_shared`/`alloc_fragment` 的显存分配、`T.copy` 的全局↔共享搬运、`T.gemm` 的 Tensor Core 矩阵乘、`T.Pipelined` 的软流水。建议：

1. 先回头在本讲标出的「② 内核定义」区间（L191-L244）通读一遍 `main`，建立印象。
2. 想理解 Roller 如何把 1296 砍到 10，跳到 u3-l10；想理解 `enable_rasteration`/`policy` 旋钮，看 u3-l11。
3. 若想看「不走 autotune、直接 `tilelang.compile` 评估」的另一种主流程，可先读 u5-l18。
