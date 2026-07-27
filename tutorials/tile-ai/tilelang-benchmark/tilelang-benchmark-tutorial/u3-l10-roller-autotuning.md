# tilelang.carver 与 Roller 自动调优

## 1. 本讲目标

本讲承接 [u3-l9（块级 GEMM 内核解剖）](u3-l9-block-gemm-anatomy.md)。在上一讲里，我们把 `@T.prim_func` 内核本体的「五要素」（`T.Kernel` / `alloc_*` / `T.copy` / `T.gemm` / `T.Pipelined`）逐行拆透了，但一直把 `block_M / block_N / block_K / num_stages / thread_num / policy / enable_rasteration` 这些值当成「从天上掉下来的常量」。本讲就来回答：**这些值到底从哪儿来？**

在 [u3-l8（内核骨架）](u3-l8-tilelang-kernel-skeleton.md) 里我们已经见过 `get_configs`，并且知道它有两条路：一条是 `itertools.product` 笛卡尔积**暴搜**，另一条是 `with_roller=True` 时由 **Roller** 推导。本讲专攻后者。

学完本讲，你应当能够：

- 说出 `with_roller=True` 这条路径的四个关键件：`MatmulTemplate`（把 GEMM 语义「雕刻」成可调优模板）、`CUDA`（架构描述符）、`recommend_hints`（推导 TensorCore 调度提示）、`NoRasterization`（栅格化哨兵）。
- 理解 Roller 是如何把搜索空间从「暴搜的几百个配置」**缩小**到「top-10 个高质量配置」的，并说出这种缩小的代价与收益。
- 把一条 `hint` 的字段（`block / warp / rstep / pipeline_stage / rasterization_plan`）**逐字段换算**回内核认识的 `config` 字典（`block_M/N/K`、`num_stages`、`thread_num`、`policy`、`enable_rasteration`）。
- 能说出两条 `get_configs` 分支各自的取舍，并为一个新算子选择合适的调优入口。

本讲只看一个文件，但它是后续所有「想让 Roller 帮自己生成搜索空间」的算子（反量化 GEMV、Attention 等）的共同范式。

## 2. 前置知识

### 2.1 为什么需要「聪明的」搜索空间

回顾 u3-l8/u3-l9：`@autotune` 会对 `configs` 列表里**每一个**配置都把内核编译一遍、跑一遍计时，再选出 latency 最小的那一个。这意味着 `configs` 越长，调优越慢。

暴搜分支把 7 个列表做笛卡尔积：

\[
|\text{configs}_{\text{暴搜}}| = \underbrace{3}_{block\_M} \times \underbrace{3}_{block\_N} \times \underbrace{3}_{block\_K} \times \underbrace{4}_{num\_stages} \times \underbrace{2}_{thread\_num} \times \underbrace{1}_{policy} \times \underbrace{2}_{enable\_raster} = 432
\]

（本文件实际为 432 个；上一讲的概要里曾提到 1296，那是误算——本讲以代码里 7 个列表的乘积为准，你可以在源码里逐项核对。）

也就是说，暴搜要让 autotuner 编译并计时 **432 次**。更要命的是，这 432 个里有很多是**根本不合法或明显低质**的组合：例如 `block_K=256` 在某些精度下会让共享内存爆掉、`num_stages=3` 叠加大 block 可能超出 block 共享内存上限。autotuner 编译它们只会白白浪费时间，最后还得靠「编译失败/运行失败」把它们淘汰掉。

Roller 的出发点就是：**与其盲搜再淘汰，不如一开始就只用硬件约束允许的、高质量的少数几个调度方案。**

### 2.2 什么是「carve」与「hint」

两个术语先建立直觉：

- **carve（雕刻）**：把一个抽象的算子（这里是 GEMM，知道 M/N/K 和三种 dtype）「雕刻」成一个**可调优模板**（template）。模板本身不是某个具体调度，而是一个「我知道这是个 GEMM、我知道它的数据流」的描述，能够据此推导调度方案。本讲里的 `MatmulTemplate` 就是干这件事的，所以它在 `tilelang.carver.template` 模块下。
- **hint（提示）**：Roller 给出的**调度提示**，描述「这个 GEMM 在这块 GPU 上，一种值得试的调度长什么样」——块多大、warp 怎么切、K 步长多少、流水几级、要不要栅格化。一条 hint 不是最终答案，而是「请你按这个去编译计时试试」的候选。Roller 一次给出 top-K 条 hint，autotuner 只需在这 K 条里选最优。

一句话：`MatmulTemplate` 负责「懂这个算子」，`recommend_hints` 负责「懂这块硬件」，二者结合产出少量高质量候选。

> 本讲只讲本仓库里**可观测**的 API 与直觉。Roller 的内部推导算法（如何用硬件约束递归构造可行调度）属于 `tilelang` 包自身，不在本仓库源码内，相关细节标注为「待本地验证/属于包内部」。

### 2.3 架构描述符：`CUDA("cuda")` 与 `target="auto"` 是两回事

初学者最容易混淆两个「架构」概念，先区分清楚：

| 概念 | 出现位置 | 作用 | 谁用 |
|---|---|---|---|
| `CUDA("cuda")` | Roller 路径，`get_configs` 内（[L36](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L36)） | **推理搜索空间时**告诉 Roller「假设目标是 NVIDIA CUDA GPU」，让它按 NVIDIA 的 TensorCore 形状、共享内存容量来裁剪 hint | carver / Roller |
| `target="auto"` | `@jit(...)` 装饰器（[L150](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L150)） | **真正编译内核时**让 TileLang 编译器自动探测本机 GPU 后端（NVIDIA→CUDA、AMD→HIP） | TileLang 编译器 |

也就是说：`CUDA("cuda")` 是**给 Roller 用的假设**（在生成配置阶段），`target="auto"` 是**给编译器用的实际目标**（在编译阶段）。Roller 路径只在「假设是 NVIDIA」的前提下推导 hint；这也解释了为什么本讲聚焦的 `with_roller` 分支主要出现在 hopper 等 NVIDIA 目录里——`CUDA` 架构描述符是 NVIDIA 专用的。跨架构（AMD/CDNA）的对应做法留到 [u6-l21（tvm.tl 与卷积）](u6-l21-tvmtl-conv-amd.md) 与 [u7-l24（跨架构适配）](u7-l24-cross-architecture-adaptation.md)。

## 3. 本讲源码地图

本讲只看一个文件，聚焦其中的 `get_configs` 函数：

| 文件 | 作用 |
|---|---|
| [`benchmark_tilelang_matmul.py`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py) | hopper dense matmul 的 TileLang 内核。本讲聚焦 `get_configs`（[L17-L104](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L17-L104)），尤其是 `with_roller=True` 分支（[L32-L72](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L32-L72)）。内核本体（`@T.prim_func main`）已在 u3-l9 讲透，本讲不重复。 |

辅助参考（本讲提及但不展开）：

| 文件 | 作用 |
|---|---|
| [`benchmark_tilelang_matmul_fp16xfp4.py`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xfp4.py) | 反量化 matmul 的 Roller 路径，`get_configs` 结构与本讲完全一致（[L40-L80](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xfp4.py#L40-L80)），说明这套「四件套」是跨算子复用的范式。 |

> 「以代码为准」提醒（沿用 u3-l8/u3-l9 建立的意识）：Roller 分支构造 `MatmulTemplate` 时填的是 `in_dtype="float16"` / `accum_dtype="float"`（[L43-L45](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L43-L45)），但内核本体实际跑的是 `dtype="int8"` / `accum_dtype="int32"`（[L188-L189](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L188-L189)）。也就是说 **Roller 是按 fp16 问题推导 hint，再把结构性的调度参数套到一个 int8 内核上**。好在 block/warp/rstep/pipeline/rasterization 这些决策对精度大体不敏感，hint 仍能产出结构合法的配置；但这是一处真实的不一致，复现时心里要有数。

## 4. 核心概念与源码讲解

Roller 路径全貌（[L32-L72](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L32-L72)）：

```python
if with_roller:
    from tilelang.carver.template import MatmulTemplate          # ① 模板
    from tilelang.carver.arch import CUDA                        # ② 架构
    from tilelang.carver.roller.rasterization import NoRasterization  # ④ 哨兵
    arch = CUDA("cuda")
    topk = 10

    carve_template = MatmulTemplate(
        M=M, N=N, K=K,
        in_dtype="float16", out_dtype="float16", accum_dtype="float",
    ).with_arch(arch)                                            # ①+② 把模板绑到架构

    func = carve_template.equivalent_function()
    assert func is not None, "Function is None"

    roller_hints = carve_template.recommend_hints(topk=topk)     # ③ 推导提示
    if roller_hints is None:
        raise ValueError("No Roller Hints Found for TensorCore Scheduling")

    configs = []
    for hint in roller_hints:                                    # ③ 把 hint 换算成 config
        ...                                                       #    （见 4.3 的映射表）
```

下面按四个最小模块逐一拆。

### 4.1 模块一：MatmulTemplate——把 GEMM 语义「雕刻」成可调优模板

#### 4.1.1 概念说明

`MatmulTemplate` 是 `tilelang.carver.template` 提供的**算子模板**。它解决的问题是：Roller 需要知道「我要调度的是一个什么样的计算」，才能据此推导调度方案。`MatmulTemplate` 就是把一个 GEMM 的语义（输入 A/B 的形状与 dtype、输出 C 的 dtype）封装成一个对象，让 Roller 知道这是个矩阵乘、数据怎么流动。

它的关键能力有两个（本讲都会用到）：

1. `with_arch(arch)`：把模板**绑定到一个目标架构**，返回一个「懂算子 + 懂硬件」的复合对象。
2. `recommend_hints(topk=...)`：基于绑定的架构，推导出 top-K 条调度提示。

> 直觉：`MatmulTemplate(M,N,K,dtypes)` 回答「算什么」，`.with_arch(arch)` 回答「在哪算」，`.recommend_hints()` 回答「怎么算才高效」。

#### 4.1.2 核心流程

构造一个模板的步骤：

1. 告诉它算子的形状：`M, N, K`（这些来自命令行 `--m/--n/--k`，见 [L264](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L264)）。
2. 告诉它三种 dtype：`in_dtype`（输入）、`out_dtype`（输出）、`accum_dtype`（累加）。
3. 调 `.with_arch(arch)` 绑定架构，得到 `carve_template`。
4. 后续在 `carve_template` 上调 `equivalent_function()` 与 `recommend_hints()`。

#### 4.1.3 源码精读

模板构造在 [benchmark_tilelang_matmul.py:39-46](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L39-L46)：

```python
carve_template = MatmulTemplate(
    M=M,
    N=N,
    K=K,
    in_dtype="float16",
    out_dtype="float16",
    accum_dtype="float",
).with_arch(arch)
```

说明：

- `M/N/K` 是本次要测的矩阵维度，由 `argparse` 传入（[L254-L256](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L254-L256)）。注意：Roller 推导 hint 时**会用到具体形状**——不同 M/N/K 会得到不同的 top-10，这正是它「形状感知」的优势（暴搜分支的 432 个配置与形状无关，盲搜一视同仁）。
- 三个 dtype 字段描述精度。如前所述，这里填的是 fp16/float，与内核实际 int8/int32 不符（精度不一致 quirk，见本讲源码地图提醒）。
- `.with_arch(arch)` 把模板绑到 `arch = CUDA("cuda")`，返回值赋给 `carve_template`，之后所有推导都在这个绑定后的对象上进行。

紧跟着的 `equivalent_function()` 在 [L48-L49](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L48-L49)：

```python
func = carve_template.equivalent_function()
assert func is not None, "Function is None"
```

说明：`equivalent_function()` 会生成一个与该模板**语义等价的 IR/TIR 函数**。但在**本文件里**，`func` 只被用于一次非空断言，之后并未被消费——真正的 hint 生成由下一行的 `recommend_hints()` 完成。`equivalent_function` 的具体内部用途（例如供 carver/Roller 内部分析）属于 `tilelang` 包内部，不在本仓库，故不展开。

#### 4.1.4 代码实践

**实践目标**：确认模板构造的输入参数都从哪里来，并发现精度不一致。

**操作步骤**：

1. 打开 [benchmark_tilelang_matmul.py:39-46](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L39-L46)。
2. 顺着 `M/N/K` 往回找：它们是 `matmul(M, N, K, with_roller)` 的入参（[L107](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L107)），再往上是 `__main__` 里的 `args.m/n/k`（[L264](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L264)），默认 `16384`。
3. 对比模板填的 `in_dtype="float16"` 与内核本体的 `dtype = "int8"`（[L188](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L188)）。

**需要观察的现象**：模板的 dtype 与内核的 dtype 不一致；但形状 M/N/K 是一致的。

**预期结果**：你会确认这是一处「模板按 fp16 推导、内核按 int8 运行」的不一致。由于 block/warp/rstep/pipeline 这些调度参数大体与精度无关，hint 仍能产出结构合法的配置；但若未来要让 Roller 给出**精度精确**的 hint（例如 int8 下 TensorCore 的 MMA 形状约束与 fp16 不同），应把模板的 dtype 改成与内核一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MatmulTemplate` 要把 `M/N/K` 作为构造参数，而不是只传 dtype？
**答案**：因为 Roller 推导 hint 是**形状感知**的。比如 M=1（GEMV）和 M=8192（大方阵 GEMM）的最佳 block/warp 切分完全不同；M=1 时甚至不该用块级 TensorCore GEMM，而该走 GEMV 路径（见 [u4-l13 反量化 GEMV](u4-l13-dequant-gemv-thread-reduction.md)）。把形状传给模板，Roller 才能据此裁剪出适配该形状的少数高质量调度。

**练习 2**：`.with_arch(arch)` 返回的对象为什么赋值给了一个**新名字** `carve_template`，而不是原地修改？
**答案**：这是一种「链式构造 + 不可变风格」的 API 设计——`with_arch` 返回一个绑定了架构的新对象，原 `MatmulTemplate` 实例本身不一定是「已绑架构」的状态。这样做可以让模板在绑定不同架构时复用（例如同一个模板先绑 CUDA、再绑别的架构做对比），也避免了隐式状态修改带来的混淆。本文件只用了一个架构，所以直观上像是「配置好模板」。

---

### 4.2 模块二：CUDA arch——告诉 Roller 硬件的「形状」

#### 4.2.1 概念说明

`CUDA` 是 `tilelang.carver.arch` 提供的**架构描述符**（architecture descriptor）。它解决的问题：Roller 要裁剪搜索空间，就得知道目标硬件的约束——这块 GPU 的 TensorCore 支持哪些 MMA 指令形状、共享内存每块有多大、一个 block 最多多少线程/寄存器。这些「硬件事实」就封装在 `CUDA("cuda")` 里。

把架构描述符传给 `MatmulTemplate.with_arch`，相当于告诉模板：「请按 NVIDIA CUDA GPU 的硬件事实来推导调度」。这样 `recommend_hints` 产出的每一条 hint，都是在该架构上**可行**（不会爆共享内存、不会用不存在的指令形状）的调度。

#### 4.2.2 核心流程

架构描述符在 Roller 路径里的生命周期：

1. `arch = CUDA("cuda")`：构造一个 NVIDIA CUDA 架构描述符（[L36](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L36)）。`"cuda"` 是它的标识串。
2. `MatmulTemplate(...).with_arch(arch)`：把架构事实注入模板（[L46](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L46)）。
3. 此后 `recommend_hints` 在推导每条 hint 时，都会用 `arch` 里的硬件约束去**校验和裁剪**：凡是不符合 NVIDIA TensorCore 形状、或超出共享内存的候选，都不会出现在 hint 列表里。

这正是 Roller 相对暴搜的核心优势所在：暴搜的 432 个配置里有许多是「编译时才发现非法」的，而 Roller 在**生成阶段**就用架构约束把它们过滤掉了。

#### 4.2.3 源码精读

架构相关的两行在 [benchmark_tilelang_matmul.py:33-36](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L33-L46)：

```python
from tilelang.carver.arch import CUDA
...
arch = CUDA("cuda")
```

说明：

- `CUDA("cuda")` 构造的是 **NVIDIA CUDA 架构描述符**。它内部携带的硬件事实（具体指令形状、共享内存上限等）属于 `tilelang` 包内部，不在本仓库；本讲只强调它的**角色**——作为 Roller 裁剪空间的依据。
- 注意它和 `@jit(target="auto")`（[L150](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L150)）的区别（见 2.3 节的表）：`CUDA` 是**生成配置时**给 Roller 的假设，`target="auto"` 是**编译内核时**给编译器的实际后端。两者阶段不同、用途不同。
- 因为这里写死了 `CUDA`（NVIDIA），所以本路径天然只适合 hopper/ada/ampere 等 NVIDIA 目录；AMD/CDNA 上的对应做法是 `tvm.tl` 变体加 `target="hip"`（见 u6-l21）。

> 关于 `CUDA("cuda")` 的参数 `"cuda"` 是否能换成更细的架构号（如 `"89"`/`"90"`）来区分 Ada/Hopper：本仓库所有 Roller 调用都只用了 `CUDA("cuda")` 这一种写法，未见传架构号的例子，相关能力**待本地验证/属于包内部**，本讲不臆测。

#### 4.2.4 代码实践

**实践目标**：分清两个「架构」概念，并定位本仓库里所有用 Roller 的地方都是 NVIDIA。

**操作步骤**：

1. 打开 [benchmark_tilelang_matmul.py:36](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L36)（`arch = CUDA("cuda")`）与 [L150](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L150)（`target="auto"`）。
2. 在仓库里搜索 `from tilelang.carver.arch import` （参考本讲已检索的结果），看它出现在哪些架构目录下。

**需要观察的现象**：所有 `tilelang.carver.arch import CUDA` 的调用都集中在 `hopper_benchmark` / `ampere_benchmark` 等 **NVIDIA** 目录；`cdna_benchmark`（AMD）下没有用 `CUDA` 架构描述符的 Roller 路径。

**预期结果**：你会确认「`CUDA` 架构描述符 = NVIDIA 专用」这一判断。这与本讲 2.3 节的区分一致：Roller 在本仓库里只服务 NVIDIA 算子。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `arch = CUDA("cuda")` 删掉、直接调 `MatmulTemplate(...).recommend_hints(topk=10)`（不先 `with_arch`），会发生什么？
**答案**：模板没有绑定架构，Roller 就缺乏「硬件事实」来裁剪空间——`recommend_hints` 要么报错（缺少架构信息），要么无法推导出任何 TensorCore 调度而返回 `None`，从而触发 [L53-L54](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L53-L54) 的 `raise ValueError("No Roller Hints Found ...")`。`with_arch` 是 Roller 能工作的前提。

**练习 2**：为什么说 `CUDA("cuda")` 让 Roller 产出的 hint「天然合法」，而暴搜的 432 个配置不保证合法？
**答案**：Roller 在生成每条 hint 时，都用 `CUDA` 描述符里的硬件约束（TensorCore 指令形状、共享内存容量等）做了校验，所以产出的 block/warp/rstep/pipeline 组合在该架构上都是可行的。暴搜则是不加约束的笛卡尔积，里面会包含「block_K 过大导致共享内存溢出」「num_stages 过深叠加大 block 超限」等非法组合，要等 autotuner 真去编译时才暴露、再被淘汰。

---

### 4.3 模块三：recommend_hints——Roller 推导 TensorCore 调度提示

#### 4.3.1 概念说明

`recommend_hints(topk=K)` 是 `carve_template` 上的方法，它是 Roller 路径的**核心**。它做一件事：基于已绑定的架构，为这个 GEMM 推导出 **top-K 条调度提示（hint）**，每条 hint 描述一种值得试的调度方案。

一条 hint 是一个对象，携带这些字段（从本文件消费它的方式反推，见 4.3.3）：

| hint 字段 | 含义 |
|---|---|
| `hint.block` | 一个二元组 `(block_m, block_n)`：block 级输出块大小 |
| `hint.warp` | 一个二元组 `(warp_m, warp_n)`：每个 warp 负责的输出块大小 |
| `hint.rstep` | K 维归约步长，`hint.rstep[0]` 即 `block_K` |
| `hint.pipeline_stage` | 软件流水深度（对应 u3-l9 的 `num_stages`） |
| `hint.rasterization_plan` | 栅格化方案（对应 u3-l9 的 `enable_rasteration`，见 4.4） |

`recommend_hints` 可能返回 `None`——当架构约束下推导不出任何 TensorCore 调度时（例如形状不匹配任何 MMA 指令）。本文件对此做了显式处理（[L53-L54](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L53-L54)）。

#### 4.3.2 核心流程：从 hint 到 config 的换算

Roller 给的是 hint，但 `@autotune` 需要的是内核认识的 `config` 字典（键为 `block_M/N/K`、`num_stages`、`thread_num`、`policy`、`enable_rasteration`）。所以中间需要一段**逐字段换算**。换算的核心思路：

- `block` 直接给 `block_M / block_N`。
- `warp` 不直接进 config，而是用来算 **warp 切分**：一个 block 在 M 方向切几段、N 方向切几段，从而推出 warp 数、线程数与 warp 切分策略。
- `rstep[0]` 给 `block_K`。
- `pipeline_stage` 给 `num_stages`。
- `rasterization_plan` 给 `enable_rasteration`（用 `NoRasterization` 哨兵判断，见 4.4）。

关键的线程数推导（warp 大小恒为 32 线程）：

\[
\text{block\_rows} = \left\lfloor \frac{block_m}{warp_m} \right\rfloor, \quad
\text{block\_cols} = \left\lfloor \frac{block_n}{warp_n} \right\rfloor
\]

\[
\text{thread\_num} = \underbrace{\text{block\_rows} \times \text{block\_cols}}_{\text{block 内 warp 数}} \times \underbrace{32}_{\text{每 warp 线程数}}
\]

例如 `block=(128,128)`、`warp=(64,64)` 时：`block_rows=2`、`block_cols=2`、warp 数=4、`thread_num=128`。

#### 4.3.3 源码精读

hint 推导与 None 检查在 [benchmark_tilelang_matmul.py:51-54](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L51-L54)：

```python
roller_hints = carve_template.recommend_hints(topk=topk)   # topk=10

if roller_hints is None:
    raise ValueError("No Roller Hints Found for TensorCore Scheduling")
```

逐字段换算的循环在 [L56-L70](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L56-L70)：

```python
configs = []
for hint in roller_hints:
    config = {}
    block_m, block_n = hint.block
    warp_m, warp_n = hint.warp
    # block_rows, block_cols represents warp partitioning
    block_rows, block_cols = block_m // warp_m, block_n // warp_n
    config["block_M"] = block_m
    config["block_N"] = block_n
    config["block_K"] = hint.rstep[0]
    config["num_stages"] = hint.pipeline_stage
    config["thread_num"] = block_rows * block_cols * 32
    config["policy"] = T.GemmWarpPolicy.from_warp_partition(block_rows, block_cols)
    config["enable_rasteration"] = hint.rasterization_plan is not NoRasterization
    configs.append(config)
```

完整的「hint → config」映射表（本讲核心，也是综合实践的依据）：

| hint 字段 | 含义 | 换算到的 config 字段 | 代码行 |
|---|---|---|---|
| `hint.block` → `(block_m, block_n)` | block 级输出块 | `block_M`、`block_N` | [L59](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L59), [L63-L64](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L63-L64) |
| `hint.warp` → `(warp_m, warp_n)` | warp 级输出块（每 warp 算多大） | 算出 `block_rows/block_cols`，再得 `thread_num`、`policy` | [L60](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L60), [L62](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L62) |
| `hint.rstep[0]` | K 维归约步长 | `block_K` | [L65](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L65) |
| `hint.pipeline_stage` | 软件流水深度 | `num_stages` | [L66](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L66) |
| `block_rows × block_cols × 32` | 线程数 | `thread_num` | [L67](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L67) |
| `GemmWarpPolicy.from_warp_partition(block_rows, block_cols)` | warp 切分策略 | `policy` | [L68](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L68) |
| `hint.rasterization_plan is not NoRasterization` | 是否启用栅格化 | `enable_rasteration` | [L69](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L69) |

注意换算里两个「暴搜分支做不到」的点：

1. **`thread_num` 是推导出来的，不是枚举出来的**。暴搜分支只能在 `[128, 256]` 里二选一（[L79](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L79)），而 Roller 根据 `block`/`warp` 推出精确的 warp 数 × 32，能得到暴搜列表里没有的线程数（例如 4 个 warp=128、8 个 warp=256 之外的取值）。
2. **`policy` 是按 warp 切分动态构造的**。暴搜分支写死 `T.GemmWarpPolicy.Square`（[L80](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L80)），而 Roller 用 `from_warp_partition(block_rows, block_cols)`，能表达**非方形**的 warp 切分（例如 `block_rows=4, block_cols=1` 这种瘦高切分）。`Square` 是一种特殊的（方形）切分策略，详见 [u3-l11（swizzle 与 warp 策略）](u3-l11-swizzle-and-warp-policy.md)。

换算完成后，[L71-L72](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L71-L72) 把每条 config 打印出来，方便你在日志里直接看到 Roller 推出了哪些配置。

#### 4.3.4 代码实践

**实践目标**：用一组具体的 hint 值，手算换算出完整的 config 字典。

**操作步骤**：

1. 通读换算循环 [L56-L70](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L56-L70)。
2. 假设 Roller 给出一条 hint：`block=(128,128)`、`warp=(64,64)`、`rstep=(64,)`、`pipeline_stage=3`、`rasterization_plan` 为某个**非** `NoRasterization` 的方案。
3. 逐字段套用 4.3.3 的映射表，写出这条 hint 对应的 `config` 字典。

**需要观察的现象**：每个 config 字段都能从 hint 唯一确定；尤其 `thread_num` 与 `policy` 是经 `block_rows/block_cols` 中间推导的。

**预期结果**：

```python
{
    "block_M": 128,                # hint.block[0]
    "block_N": 128,                # hint.block[1]
    "block_K": 64,                 # hint.rstep[0]
    "num_stages": 3,               # hint.pipeline_stage
    "thread_num": 128,             # (128//64)*(128//64)*32 = 2*2*32
    "policy": GemmWarpPolicy.from_warp_partition(2, 2),
    "enable_rasteration": True,    # rasterization_plan 不是 NoRasterization
}
```

其中 `block_rows = 128//64 = 2`、`block_cols = 128//64 = 2`、`thread_num = 2*2*32 = 128`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `block_K` 取 `hint.rstep[0]`（带下标 `[0]`），而 `block_M/block_N` 不带下标？
**答案**：`hint.block` 是一个二元组 `(block_m, block_n)`，直接解包成两个标量；而 `hint.rstep` 是一个序列（K 维归约步长列表，可能为多维归约预留），本算子只在单一 K 维上归约，所以取第一个元素 `rstep[0]` 作为 `block_K`。下标 `[0]` 反映了 rstep 是序列这一结构。

**练习 2**：暴搜分支的 `thread_num` 只能取 128 或 256。这两个值分别对应几 warp？Roller 能否给出这两个值以外的 `thread_num`？
**答案**：128 = 4 warp × 32，256 = 8 warp × 32，所以暴搜只试 4 warp 或 8 warp 两种 block 规模。Roller 的 `thread_num = block_rows*block_cols*32`，只要 `block_rows*block_cols` 取到 4 或 8 以外的值（如 2、1），就能给出 64、32 等暴搜列表里没有的线程数——前提是架构与形状允许这种切分。

**练习 3**：`recommend_hints` 返回 `None` 时程序会怎样？为什么需要这个检查？
**答案**：会触发 [L53-L54](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L53-L54) 的 `raise ValueError("No Roller Hints Found for TensorCore Scheduling")`。需要这个检查是因为：当形状/精度在该架构下匹配不到任何合法 TensorCore 调度时（例如某个维度太小、凑不齐一条 MMA 指令的形状），Roller 会返回 `None`；若不拦截，后续 `for hint in roller_hints` 会对 `None` 迭代而抛出难以理解的 `TypeError`。这条显式的 `ValueError` 把失败原因说清楚了。

---

### 4.4 模块四：NoRasterization——rasterization_plan 的哨兵

#### 4.4.1 概念说明

`NoRasterization` 是 `tilelang.carver.roller.rasterization` 提供的一个**哨兵**（sentinel）。它代表「不使用任何栅格化（rasterization）方案」。

回顾 u3-l9：内核里有一句 `T.use_swizzle(panel_size=10, enable=enable_rasteration)`（[L222](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L222)），`enable_rasteration` 这个布尔值控制是否启用 swizzle/栅格化优化（一种改变 block 遍历顺序以提升 L2 命中的技术，详见 u3-l11）。Roller 在每条 hint 里用 `rasterization_plan` 字段给出它的栅格化建议：要么是一个具体的栅格化方案，要么是 `NoRasterization`（建议不栅格化）。

本文件用 `NoRasterization` 做一次**身份比较**，把「方案对象」翻译成「布尔开关」。

#### 4.4.2 核心流程

把 hint 的栅格化建议翻译成 config 布尔值的逻辑：

\[
\text{enable\_rasteration} = (\text{hint.rasterization\_plan} \;\mathtt{is\;not}\; \text{NoRasterization})
\]

- 若 `rasterization_plan` 是 `NoRasterization`（Roller 建议不栅格化）→ `enable_rasteration = False`。
- 若 `rasterization_plan` 是任何**别的**方案（Roller 给出了具体栅格化建议）→ `enable_rasteration = True`。

注意这里用的是 `is not`（**身份**比较）而非 `!=`（值比较）。这说明 `rasterization_plan` 字段在「不栅格化」时被设成 `NoRasterization` 这个对象本身（作为类型/标记），代码靠身份比较来识别「是不是这个默认标记」。

#### 4.4.3 源码精读

导入与使用分散在三处。导入在 [L35](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L35)：

```python
from tilelang.carver.roller.rasterization import NoRasterization
```

身份比较在 [L69](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L69)：

```python
config["enable_rasteration"] = hint.rasterization_plan is not NoRasterization
```

说明：

- `NoRasterization` 在这里扮演「默认/空方案」的角色，类似 `None` 但更语义化——它专门表示「栅格化方案：无」。
- `is not` 做身份比较：只要 `rasterization_plan` 不是这个默认标记（即 Roller 给出了某种真实栅格化方案），就启用 swizzle。这是一种「有具体方案就开、默认标记就关」的简洁翻译。
- 翻译出的 `enable_rasteration` 直接喂给内核的 `T.use_swizzle(enable=...)`（[L222](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L222)），形成「Roller 决策 → config 布尔 → 内核开关」的完整链路。
- 对比暴搜分支：那里 `enable_rasterization = [True, False]`（[L81](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L81)）两种都试；Roller 则**按方案直接决定**开或关，不再把两种都列入候选。

> 拼写提醒：config 的键名是 `enable_rasteration`（少了一个 `i`，正确拼写应为 rasterization）。这是全文件一致的历史遗留拼写——内核参数（[L159](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L159)）和暴搜分支（[L101](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L101) 注释写 "keep param name for backward-compat"）都用这个拼写。读代码时认这个错拼名即可，不要以为是笔误而「纠正」它——改了反而会让 config 键与内核参数对不上。

#### 4.4.4 代码实践

**实践目标**：把「Roller 栅格化决策 → 内核 swizzle 开关」这条链路串起来。

**操作步骤**：

1. 打开三处：[L35](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L35)（导入 `NoRasterization`）、[L69](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L69)（身份比较生成布尔）、[L222](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L222)（内核里 `T.use_swizzle(enable=enable_rasteration)`）。
2. 假设某条 hint 的 `rasterization_plan` 恰好是 `NoRasterization`，追踪 `enable_rasteration` 最终把内核的 swizzle 打开了还是关上了。

**需要观察的现象**：一个布尔值从 hint 流到 config、再流到内核原语。

**预期结果**：`rasterization_plan is NoRasterization` → `is not NoRasterization` 为 `False` → `enable_rasteration=False` → `T.use_swizzle(enable=False)` → 该 config 下内核**关闭** swizzle 栅格化。反之，若 `rasterization_plan` 是某个具体方案，则链路把 swizzle **打开**。

#### 4.4.5 小练习与答案

**练习 1**：为什么用 `is not` 而不是 `!=` 来比较 `rasterization_plan` 与 `NoRasterization`？
**答案**：`is not` 是**身份**比较（是否同一个对象），`!=` 是**值**比较。这里 `NoRasterization` 被当作一个「标记对象/类型」使用——当 Roller 不打算栅格化时，把 `rasterization_plan` 设成这个标记本身。用身份比较能精确识别「是不是这个默认标记」，避免被 `__eq__` 的语义干扰。这也是 Python 里用单例对象当哨兵（如 `None`）时的标准做法。

**练习 2**：暴搜分支在栅格化上试 `[True, False]` 两种，Roller 只给一种。这算 Roller 的优势还是劣势？
**答案**：两面都有。优势是：Roller 根据形状/架构判断该不该栅格化，省掉了「明明不该开却开了」的无效候选，搜索空间更小（top-10 vs 432）。劣势是：Roller 的判断不一定总是最优——如果它的栅格化建议与实际 L2 行为有偏差，暴搜反而能靠「两种都试」兜底找到更优解。实践中常见做法是：先用 Roller 快速定位大致最优区域，再在可疑维度上用小范围暴搜细化。

---

## 5. 综合实践

本讲综合实践把四个模块串起来，完成一个**两条路径的对比 + hint 换算**任务。

### 5.1 任务

通读 `get_configs` 全函数 [benchmark_tilelang_matmul.py:17-104](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L17-L104)，完成两件事：

1. **比较 `with_roller=True/False` 两条分支产出的配置数量级**，并填出下表的「Roller 列」是如何从 hint 推出来的。
2. **写出 `hint.block / hint.warp / hint.rstep` 如何换算成 config 的 `block_M/N/K`、`thread_num`、`policy`**（已在 4.3.3 给出，这里要求你用自己的话再表述一遍并代入一组数值）。

### 5.2 参考答案

**第一问：两路对比表**

| 维度 | 暴搜（`with_roller=False`，[L73-L103](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L73-L103)） | Roller（`with_roller=True`，[L32-L72](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L32-L72)） |
|---|---|---|
| 配置数量 | **432**（7 列表笛卡尔积：3×3×3×4×2×1×2） | **≤ 10**（`topk = 10`，[L37](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L37)） |
| 数量级比 | — | 约 **43:1**（432 / 10） |
| `block_M/N/K` 来源 | 手工列表 `[64,128,256]`（[L75-L77](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L75-L77)） | `hint.block`、`hint.rstep[0]` 推导 |
| `num_stages` 来源 | 手工列表 `[0,1,2,3]`（[L78](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L78)） | `hint.pipeline_stage` 推导 |
| `thread_num` 来源 | 手工列表 `[128,256]`（[L79](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L79)） | `block_rows × block_cols × 32` 推导，能取到 128/256 以外的值 |
| `policy` 来源 | 写死 `Square`（[L80](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L80)） | `from_warp_partition(block_rows, block_cols)`，支持非方形切分 |
| `enable_rasteration` 来源 | `[True,False]` 都试（[L81](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L81)） | 由 `rasterization_plan is not NoRasterization` 直接决定 |
| 形状感知 | 否（与 M/N/K 无关，盲搜） | 是（不同形状产出不同 top-10） |
| 硬件感知 | 否（含非法/低质组合，靠编译失败淘汰） | 是（按 `CUDA` 架构约束裁剪） |
| 风险 | 慢；但覆盖全，能兜底找最优 | 快；但依赖 Roller 判断，可能漏掉它没想到的方案 |

**第二问：hint → config 换算（代入数值）**

取一条具体 hint：`block=(128,128)`、`warp=(64,64)`、`rstep=(64,)`、`pipeline_stage=3`、`rasterization_plan=<非 NoRasterization>`。

换算步骤：

1. `block_M = block[0] = 128`，`block_N = block[1] = 128`（块级输出块大小）。
2. `block_K = rstep[0] = 64`（K 维归约步长）。
3. `block_rows = block_M // warp_m = 128 // 64 = 2`，`block_cols = block_N // warp_n = 128 // 64 = 2`（M/N 方向各切几段 warp）。
4. `thread_num = block_rows × block_cols × 32 = 2 × 2 × 32 = 128`（warp 数 × 每 warp 32 线程）。
5. `policy = GemmWarpPolicy.from_warp_partition(2, 2)`（按 2×2 的 warp 切分构造策略）。
6. `num_stages = pipeline_stage = 3`。
7. `enable_rasteration = (rasterization_plan is not NoRasterization) = True`。

最终 config：

```python
{"block_M":128, "block_N":128, "block_K":64, "num_stages":3,
 "thread_num":128, "policy": GemmWarpPolicy.from_warp_partition(2,2),
 "enable_rasteration": True}
```

这个 config 会被 `@autotune`（[L142-L146](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L142-L146)）拿去编译计时，与其他 9 条 hint 一起竞速，选出 latency 最小者作为 `best_result`。

### 5.3 进阶（可选，性能待本地验证）

在真实 H100 上，对同一组 shape 分别跑 `python benchmark_tilelang_matmul.py --m 16384 --n 16384 --k 16384`（默认 `with_roller=False`，432 配置）与 `... --with_roller`（≤10 配置），对比：

1. **调优总耗时**：Roller 路径应明显更短（编译次数少 ~43 倍）。
2. **最终 Best TFlops**：两者可能接近，也可能 Roller 略低（因为它没试全部组合）。

**预期结果**：**待本地验证**。这是 Roller「快 vs 全」取舍的实测体现——工程上常先用 Roller 缩短迭代周期，再在需要榨干最后性能时回到暴搜细化。

## 6. 本讲小结

- `get_configs` 有两条路：`with_roller=False` 用 `itertools.product` 暴搜出 **432** 个配置；`with_roller=True` 用 Roller 推导出 **≤ topk(=10)** 个，约 **43:1** 的搜索空间缩减。
- Roller 路径的「四件套」：`MatmulTemplate(M,N,K,dtypes)` 把 GEMM 语义雕刻成模板（4.1），`CUDA("cuda")` 提供 NVIDIA 硬件事实作为裁剪依据（4.2），`recommend_hints(topk=10)` 推导 top-K 调度提示（4.3），`NoRasterization` 充当栅格化方案的哨兵（4.4）。
- 一条 hint 通过逐字段换算变成 config：`block→block_M/N`、`rstep[0]→block_K`、`pipeline_stage→num_stages`、`warp→block_rows/cols→thread_num(×32) 与 policy(from_warp_partition)`、`rasterization_plan is not NoRasterization→enable_rasteration`。
- Roller 相对暴搜的优势：**形状感知 + 硬件感知**——既按具体 M/N/K 给不同 hint，又按架构约束过滤掉非法组合，且能推出暴搜列表之外的 `thread_num` 与非方形 `policy`。
- 两个「架构」概念要分清：`CUDA("cuda")` 是**生成配置时**给 Roller 的 NVIDIA 假设，`@jit(target="auto")` 是**编译内核时**给编译器的实际后端——阶段与用途都不同。
- 本文件多处「以代码为准」提醒：Roller 模板填 fp16/float 而内核实跑 int8/int32（精度不一致）；config 键名 `enable_rasteration` 是历史遗留错拼（勿改）；`equivalent_function()` 的返回值在本文件仅作非空断言、未被消费。

## 7. 下一步学习建议

- 本讲只讲了 hint 怎么换算成 config，但没讲 `policy=GemmWarpPolicy.from_warp_partition(...)`、`T.use_swizzle`、`Square` 这些「调优旋钮」具体如何影响 L2 命中与 bank conflict。接着读 [u3-l11（swizzle、warp 策略与调优旋钮）](u3-l11-swizzle-and-warp-policy.md)，它会把 `from_warp_partition` 与 `Square` 的区别讲透。
- 想看 Roller「四件套」如何复用到别的算子，跳到 [u4-l14（量化与 lop3 快速解码）](u4-l14-quantize-fast-decoding.md) 或反量化 matmul 的 [`benchmark_tilelang_matmul_fp16xfp4.py`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xfp4.py)，它们的 `get_configs` 与本讲结构一致。
- 想了解非装饰器的「显式 AutoTuner」写法（与本讲的 `@autotune` 装饰器式对照），见 [u6-l22（显式 AutoTuner API 与多内核组合）](u6-l22-explicit-autotuner-multi-kernel.md)。
- 跨架构方面，`CUDA` 是 NVIDIA 专用；AMD/CDNA 上 Roller 对应物的差异（`tvm.tl` + `target="hip"`）见 [u6-l21（tvm.tl 与卷积）](u6-l21-tvmtl-conv-amd.md) 与 [u7-l24（跨架构适配）](u7-l24-cross-architecture-adaptation.md)。
