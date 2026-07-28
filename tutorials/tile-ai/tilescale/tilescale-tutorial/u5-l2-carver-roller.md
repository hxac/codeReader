# Carver 与 Roller 代价模型

## 1. 本讲目标

上一讲（u5-l1）我们学了 TileLang 内置的 **Autotuner**：它把 `block_M/block_N/block_K/num_stages/threads` 等编译期常量暴露成可调参数，**穷举 + 实测**选出最优配置。穷举的痛点很明显——配置空间是参数的笛卡尔积，候选越多编译与评测越贵，而且要靠人凭经验圈定候选范围。

本讲介绍另一条思路：**用代价模型（cost model）在编译/运行之前就预测哪些 tile 配置更优**，直接给出一个排好序的小候选集（hint）。这条路由 `tilelang/carver/` 下的 **Carver** 与 **Roller** 实现。读完本讲你应当能：

1. 说清 Carver 是什么、它产出什么样的 `Hint`，以及它和 tilelang 主编译流水线是什么关系（重要：是「建议」而非「自动应用」）。
2. 理解 **架构抽象 `TileDevice`** 如何把硬件参数（共享内存上限、warp 大小、SM 分区数、带宽……）抽象成统一接口，并被 cuda/cdna/metal/cpu 复用。
3. 读懂 **DefaultPolicy** 这套启发式代价模型：它如何用「访存量 × 波数」做优先级、用 best-fit 估算共享内存代价、用贪心因子分解给线程块分轴。
4. 读懂 **TensorCorePolicy** 如何在 DefaultPolicy 之上叠加 mma/wgmma 的形状与 bank-conflict 约束。
5. 能动手调用 `carver.MatmulTemplate(...).recommend_hints()`，把返回的 hint「翻译」成 tilelang kernel 的 `block_M/block_N/block_K/num_stages` 并实测对比。

---

## 2. 前置知识

本讲假设你已掌握 u5-l1（Autotuner 的可调参数、`do_bench` 计时），以及 u2-l3（`T.gemm`、fragment 累加器）、u2-l2（shared/fragment/local 显存层级）。下面补充三个本讲要用到的概念。

### 2.1 什么是「tile / 调度策略（scheduling hint）」

GPU 上一个 GEMM 通常按两层 tile 切分：

- **block tile**（`block_M × block_N`）：一个线程块（CTA）负责计算的输出子矩阵大小。
- **warp tile / thread tile**：块内再分给每个 warp / 线程。
- **reduce step**（`block_K` / `rstep`）：沿 K 维每次搬多大一块进 shared memory。

一组 `(block_M, block_N, block_K, warp_M, warp_N, num_stages, ...)` 就叫一个**调度策略 / tiling hint**。它本身不是可执行代码，而是「建议编译器/手写 kernel 按这个粒度去切循环」。Carver 的工作就是**生成并排序**这样一组组 hint。

### 2.2 屋顶线（roofline）与「访存量 × 波数」直觉

一个 kernel 要么受限于算力（compute bound），要么受限于访存（memory bound）。粗略地：

\[ \text{访存密度} = \frac{\text{FLOPs}}{\text{访存字节数}} \]

对 GEMM，分块越大，每个元素被复用次数越多，访存密度越高，越偏 compute bound——但分块越大，shared memory / 寄存器开销也越大，占用率（occupancy）下降。Roller 的核心打分函数就是在这个权衡里找甜点。本讲会看到它的优先级公式：

\[ \text{priority} = (\text{traffic} + 1) \times \text{num\_wave} \]

traffic 越小（少搬数据）、num_wave 越小（块数少、占用率高），优先级数值越小、越靠前。

### 2.3 best-fit 内存分配

「best-fit」是经典内存分配策略：在一串空闲块里挑**能放下且最小**的那一块，以减少外部碎片。Roller 用它来**模拟**一组 tile 在 shared memory 里的占用情况，从而估算代价——注意，这是代价模型里的仿真，不是 GPU 真正的运行时分配。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tilelang/carver/__init__.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/__init__.py) | Carver 包入口，聚合 analysis / roller / arch / template 的导出 |
| [tilelang/carver/analysis.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/analysis.py) | TIR 块/循环分析：`normalize_prim_func`、`BlockInfo/IterInfo`、合并访问宽度等 |
| [tilelang/carver/roller/hint.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/hint.py) | `Hint`（一条调度策略）、`TileDict`、`IntrinInfo`、`Stride` 的定义 |
| [tilelang/carver/roller/policy/default.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/default.py) | **DefaultPolicy**：Roller 通用代价模型与候选生成 |
| [tilelang/carver/roller/policy/tensorcore.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/tensorcore.py) | **TensorCorePolicy**：在 DefaultPolicy 之上叠加 mma/wgmma 约束 |
| [tilelang/carver/roller/bestfit.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/bestfit.py) | `BestFit`：best-fit free-list 分配器，用于估算 shared memory 代价 |
| [tilelang/carver/arch/arch_base.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/arch/arch_base.py) | `TileDevice`：架构抽象基类 |
| [tilelang/carver/arch/cuda.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/arch/cuda.py) | `CUDA` 子类及架构判定（Volta/Ampere/Ada/Hopper、tensorcore 支持精度） |
| [tilelang/carver/utils.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/utils.py) | `get_roller_hints_from_func`：从 PrimFunc 直接生成 hint 的总入口 |
| [tilelang/carver/template/base.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/template/base.py) | `BaseTemplate`：模板抽象，`recommend_hints()` 在此定义 |
| [tilelang/carver/template/matmul.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/template/matmul.py) | `MatmulTemplate`：用 TVM `te.compute` 构造等价 matmul，再交给 Roller |
| [tilelang/carver/README.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/README.md) | Carver 官方文档与用法示例 |

> 说明：本讲引用的源码路径均为仓库 `tilelang/carver/...`（Carver 最初是独立项目，被合并进 TileScale 作为 `tilelang.carver` 子包）。

---

## 4. 核心概念与源码讲解

### 4.1 Carver 框架总览与 Hint 抽象

#### 4.1.1 概念说明

Carver 的定位见其 README 第一句：「**A Tile-Structure Based Hint Recommend Framework for Machine Learning Compilers**」——一个基于 tile 结构的**调度策略推荐框架**。它的产出不是可执行 kernel，而是一组排好序的 `Hint`（即第 2.1 节所说的「调度策略」）。

Carver 由四块组成：

1. **架构信息（arch）**：`TileDevice` 及子类，描述硬件约束（下节细讲）。
2. **模板（template）**：把一类算子（matmul / gemv / elementwise / general reduction / flash attention）抽象成「一个等价的 TVM 计算图 + 循环结构标记」，如 `MatmulTemplate`。
3. **代价模型（roller policy）**：在模板与架构之上，用启发式搜索 + 代价估算生成候选 `Hint`。
4. **Hint**：最终产出，可序列化为 dict，供任意编译器（TVM / Triton / tilelang）「翻译」成自己的 schedule。

> **重要现状（避免误解）**：Carver 的 Hint 是**建议性**的，仓库**目前没有**「把 Hint 自动应用到 tilelang schedule」的一键函数。Carver README 的 TODO 一节明确写着：「Adapt to tile language: Provide ready-made scheduling calls or wrappers for tilelang to streamline end-to-end integration.」也就是说，你需要**人工把 hint 里的 `block/warp/rstep/pipeline_stage` 读出来，填进 tilelang kernel 的 `T.Kernel(...)` 与 `T.Pipelined(...)` 参数里**（第 5 节综合实践就是这么做的）。这也回答了它与 u5-l1 Autotuner 的分工：Autotuner 负责「实测选优」，Carver/Roller 负责「凭代价模型给出高质量小候选集」，二者可串联——用 Carver 缩小空间、再用 Autotuner 实测。

#### 4.1.2 核心流程

以 `MatmulTemplate` 为例，从「用户调用」到「拿到 hint」的链路是：

```
MatmulTemplate(M, N, K, in_dtype, ...).with_arch(CUDA(...))
        │  __post_init__ → initialize_function()
        │     用 tvm.te 构造等价 matmul 计算图，转成 PrimFunc 存到 self._func
        ▼
.recommend_hints(topk)
        │  BaseTemplate.recommend_hints → get_hardware_aware_configs(arch, topk)
        ▼
MatmulTemplate.get_hardware_aware_configs
        │  调 tilelang/carver/utils.py: get_roller_hints_from_func(self._func, arch, topk)
        ▼
get_roller_hints_from_func
        │  尝试 get_tensorized_func_and_tags(...) 识别能否走 tensor core
        │  能 → TensorCorePolicy.emit_config(topk)；不能 → DefaultPolicy.emit_config(topk)
        ▼
list[Hint]
```

`Hint` 的关键字段（见 [hint.py:150-188](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/hint.py#L150-L188)）：`block`（块 tile）、`thread`/`warp`（线程/warp 切分）、`rstep`（reduce 轴 tile）、`reduce_thread`（reduce 轴线程数）、`use_tc`（是否走 tensor core）、`vectorize`（向量化宽度）、`pipeline_stage`（软件流水级数，对应 tilelang 的 `num_stages`）、`step`（合并访问步长）、`shared_scope`、`rasterization_plan`。

#### 4.1.3 源码精读

`BaseTemplate` 把「生成 hint」声明为抽象方法，并把对外入口统一成 `recommend_hints`：

[BaseTemplate.get_hardware_aware_configs](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/template/base.py#L34-L48) —— 子类必须实现：返回 `list[Hint]`。

[BaseTemplate.recommend_hints](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/template/base.py#L144-L154) —— 对外统一入口，转发到 `get_hardware_aware_configs`。

`MatmulTemplate` 的实现很薄，关键是它先把「matmul 语义」用 `tvm.te` 写成一个标准计算图，再交给 Roller：

[MatmulTemplate.get_hardware_aware_configs](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/template/matmul.py#L41-L53) —— 直接调 `get_roller_hints_from_func`。

[MatmulTemplate._compute_matmul](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/template/matmul.py#L91-L104) —— 用 `te.reduce_axis` + `te.sum` 定义标准 GEMM，`trans_A/trans_B` 通过调整下标实现。注意默认 `trans_B=True`（权重按 `(N,K)` 存），与 tilelang `T.gemm(..., transpose_B=True)` 语义一致。

真正生成 hint 的总入口在 utils.py：

[get_roller_hints_from_func](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/utils.py#L29-L65) —— 它先尝试 `get_tensorized_func_and_tags` 把函数「张量化」并打上 `tensorcore_config` 等 tag；若成功则用 `TensorCorePolicy`，否则退回 `DefaultPolicy`；两条路都调 `policy.emit_config(topk)`。这段就是「Carver 模板 → Roller policy」的桥。

`Hint` 如何序列化（便于落盘/跨编译器传递）：

[Hint.to_dict / from_dict](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/hint.py#L189-L225) —— `to_dict` 只输出非默认字段（如 `use_tc` 为真才输出 `warp`，否则输出 `thread`），`from_dict` 反向用 `setattr` 还原。

#### 4.1.4 代码实践

- **实践目标**：用 Carver 的 `MatmulTemplate` 为一个 `1024×1024×1024` 的 fp16 GEMM 生成前 10 条 hint，读懂每个字段含义。
- **操作步骤**：
  1. 确认已安装 tilelang（u1-l2），且有 CUDA 设备。
  2. 新建脚本（**示例代码**，非仓库自带）：
     ```python
     from tilelang import carver
     from tilelang.carver.arch import CUDA

     arch = CUDA("cuda")                      # 也可写 "nvidia/geforce-rtx-4090"
     tmpl = carver.MatmulTemplate(
         M=1024, N=1024, K=1024,
         in_dtype="float16", accum_dtype="float16", out_dtype="float16",
     ).with_arch(arch)

     for i, hint in enumerate(tmpl.recommend_hints(topk=10)):
         print(i, hint.to_dict())
     ```
- **需要观察的现象**：每条 hint 是一个 dict，应包含 `block`（如 `[128,128]`）、`warp`（如 `[16,32]`）、`rstep`（如 `[64]`）、`use_tc: True`、`pipeline_stage`、`vectorize` 等键；前几条 `block` 较大、`rstep` 较大。
- **预期结果**：打印 10 条按 Roller 代价排序的配置，`block` 多为 64/128/256 的组合，`rstep` 是 16 的倍数。
- **若无法确定运行结果**：标「待本地验证」（无 GPU 时 `CUDA("cuda")` 会因找不到 device 0 而报错）。

#### 4.1.5 小练习与答案

- **练习 1**：`Hint.to_dict()` 在 `use_tc` 为真时输出 `warp`，否则输出 `thread`。为什么同一字段要分两种名字？
  - **答案**：走 tensor core 时，块内并行度按「warp tile」组织（每个 warp 算一个 `warp_M×warp_N` 子块，配 mma/wgmma 指令）；不走 tensor core 时按「每线程负责的元素数」组织，故用 `thread`。二者数学上都是「块内并行划分」，但语义和底层指令不同。
- **练习 2**：`MatmulTemplate` 默认 `trans_B=True`。如果你要算 `C = A @ B` 且 `B` 按 `(K,N)` 存（列主序的转置相反），应该把哪个参数改成 `False`？
  - **答案**：把 `trans_B=False`，此时 `weight_shape=(K,N)`，`_compute_matmul` 里 `B_indices=[k,j]`。

---

### 4.2 架构抽象：TileDevice 与 CUDA

#### 4.2.1 概念说明

代价模型离不开硬件参数——同一个 tile 配置在 A100（80GB shared、SM 分区 4）和 RTX 4090 上代价完全不同。Carver 用一个基类 `TileDevice` 把这些参数抽象成统一字段，再由 `CUDA`/`CDNA`/`CPU`/`Metal` 子类填值。Policy 代码只读 `self.arch.xxx`，不关心具体后端，从而做到「写一遍 policy、多架构复用」。

`TileDevice` 抽象的硬件参数（见 [arch_base.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/arch/arch_base.py)）：寄存器上限 `reg_cap`、共享内存上限 `smem_cap`、SM 数 `compute_max_core`、warp 大小 `warp_size`、SM 分区数 `sm_partition`、内存事务粒度 `transaction_size`、允许的最大 shared 使用 `max_smem_usage`、带宽 `bandwidth`。

#### 4.2.2 核心流程

`CUDA` 构造时做三件事：(1) 解析 target；(2) 通过 `tvm.runtime.cuda(0)` 与 `cuda_driver` 拿真实设备属性（SM 数、warp 大小、shared 上限）；(3) 设定经验常量（`reg_cap=65536`、`sm_partition=4`、`transaction_size=[32,128]`、`bandwidth=[750,12080]`）。另外提供一组**架构判定函数**（`is_ampere_arch`/`is_hopper_arch`...）与**tensorcore 支持精度表**，供 TensorCorePolicy 查表。

#### 4.2.3 源码精读

`TileDevice` 的字段全是「硬件资源」抽象：

[TileDevice.__init__](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/arch/arch_base.py#L6-L28) —— 注意 `transaction_size` 与 `bandwidth` 都是二元列表，下标 0/1 分别对应「写」「读」两条路径的估算参数。

`CUDA` 子类的真实取值：

[CUDA.__init__](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/arch/cuda.py#L104-L134) —— `smem_cap` 与 `compute_max_core`、`warp_size` 来自真实设备；`max_smem_usage = 2 * smem_cap`（允许用动态 shared 翻倍）；`reg_cap=65536`、`sm_partition=4` 是 NVIDIA GPU 的经验值。注释里诚实标注 `bandwidth` 是「近似值，靠设备间比例近似」。

[get_avaliable_tensorintrin_shapes](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/arch/cuda.py#L136-L141) —— 声明可用的张量指令形状（mma/wmma 的 `[16,16]`），供 TensorCorePolicy 校验块是否够大。

[is_tensorcore_supported_precision](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/arch/cuda.py#L80-L90) —— 按 sm 版本查表，例如 Ampere 支持 `(float16,float32)`、`(int8,int32)`；Hopper 复用 Ada 的表并加 fp8。

#### 4.2.4 代码实践

- **实践目标**：打印本机 CUDA 设备的架构参数，理解每个字段会被 policy 的哪一步用到。
- **操作步骤**：运行（**示例代码**）
  ```python
  from tilelang.carver.arch import CUDA, is_ampere_arch, is_hopper_arch
  a = CUDA("cuda")
  for k in ["sm_version","smem_cap","compute_max_core","warp_size",
            "sm_partition","reg_cap","max_smem_usage","transaction_size","bandwidth"]:
      print(f"{k:24s} = {getattr(a, k)}")
  print("ampere?", is_ampere_arch(a), "hopper?", is_hopper_arch(a))
  ```
- **需要观察的现象**：`smem_cap` 通常是 49152（A100 可配到更高）或 65536/100KB（Hopper 动态 shared）；`warp_size=32`；`sm_partition=4`；`transaction_size=[32,128]`。
- **预期结果**：得到一张硬件参数表，`is_ampere_arch`/`is_hopper_arch` 之一为 True。
- **待本地验证**：具体数值随你的 GPU 而变。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `max_smem_usage` 设成 `2 * smem_cap` 而不是等于 `smem_cap`？
  - **答案**：GPU 可通过「动态共享内存」把每块可用 shared 提升到静态上限的约 2 倍（代价是降低占用率）。代价模型允许 tile 用到这个放宽后的上限，对应 `Hint.shared_scope` 被设成 `"shared.dyn"` 的情形。
- **练习 2**：`transaction_size[1]=128`（读）比 `transaction_size[0]=32`（写）大，这个不对称会被 policy 的哪个函数用到？
  - **答案**：被 `_compute_memory_traffic` 用来把「合并访问的元素数」换算成「字节数」（[default.py:422 与 :427](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/default.py#L421-L430)），读路径用 128B、写路径用 32B。

---

### 4.3 Roller 代价模型核心：DefaultPolicy 的搜索与打分

#### 4.3.1 概念说明

`DefaultPolicy`（[default.py:20](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/default.py#L20)）是 Roller 的通用代价模型，它的 docstring 说得很直白：「a heuristic plan that tries to minimize memory traffic and maximize parallelism」（最小化访存、最大化并行）。它不依赖 tensor core，适用于 elementwise / general reduction 等任意结构。

它的产出流程（`emit_config`）分四步：

1. **算 base tile**：找到「无冗余计算」的最小 tile（每个输入元素尽量被复用）。
2. **算 reduce step**：把 reduce 轴的步长放大到让读访问「合并（coalesced）」。
3. **DFS 搜 block tile**：以「(访存+1)×波数」为优先级做优先队列搜索，展开 block tile 空间，每个候选用 `compute_tile_dict` 估算代价、剔除超 shared/reg 上限的。
4. **分配 block size**：对每个合法 tile，把线程数贪心地分配到各轴，产出最终 `Hint`。

#### 4.3.2 核心流程

下面用伪代码刻画主链路（`emit_config` → `dfs_smem_tile` → `compute_tile_dict` → `assign_block_size`）：

```
emit_config(topk):
    base_tile = get_base_tile()                      # 无冗余最小 tile
    rstep_map = {node: _assign_reduce_step(node)}    # 合并访问 friendly 的 reduce 步长
    candidates = dfs_smem_tile(base_tile, rstep_map)  # 优先队列搜索，按 prio 排序
    for td in candidates:                             # 从优到劣
        if not check_tile_shape_isvalid(td): continue
        _expand_reduce_axis(td)                       # 在 shared 上限内尽量放大 rstep
        for hint in assign_block_size(td):            # 分配线程，产出 Hint
            results.append(hint)
            if len(results) >= topk: return results
```

优先级与代价的关键公式：

\[ \text{priority}(td) = (td.\text{traffic} + 1) \times td.\text{num\_wave} \]

其中

\[ td.\text{num\_wave} = \left\lceil \frac{td.\text{grid\_size}}{td.\text{block\_per\_SM} \times arch.\text{compute\_max\_core}} \right\rceil \]

即「所有线程块要分几波才能在 SM 数 × 每块占用 上跑完」。traffic 越小越好、num_wave 越小越好，故 priority 数值越小越优，优先队列先弹出。

`compute_tile_dict` 还做合法性裁剪：若 `smem_cost > smem_cap` 或估算寄存器 `reg_usage > reg_cap`，就把 `td.valid=False` 直接丢弃。

#### 4.3.3 源码精读

`emit_config` 是总调度（注意它在收集到 `topk` 条结果后即提前返回）：

[DefaultPolicy.emit_config](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/default.py#L72-L94)

DFS 搜索与优先级函数——本讲最核心的一段：

[dfs_smem_tile 与 prio](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/default.py#L96-L134) —— `prio(td) = (td.traffic + 1) * td.num_wave`（L111-112）；用 `PriorityQueue` 做 best-first 搜索，用 `visited_tiles` 字典去重并设了 2000 的访问上限防止爆炸；最后按 `prio` 升序返回所有合法 tile。

代价估算 `compute_tile_dict`——把一个 tile 翻译成 traffic / smem_cost / grid_size / block_per_SM / num_wave，并做合法性检查：

[DefaultPolicy.compute_tile_dict](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/default.py#L537-L574) —— 注意 `block_per_SM` 取 `min(max_smem_usage//smem_cost, reg_cap//reg_usage, sm_partition)`（L568-572），即占用率受 shared、寄存器、SM 分区三者同时约束；`reg_usage` 用 `2 * max(每个 node tile 元素数 × bits / 32)` 粗估（L564）。

访存量估算 `_compute_memory_traffic`——遍历计算图节点，按「合并访问」把读/写元素数折算成字节：

[DefaultPolicy._compute_memory_traffic](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/default.py#L398-L432) —— 读用 `transaction_size[1]`、写用 `transaction_size[0]`，合并因子由 `coalesced_tensor_shape` 计算（见 policy/common.py）。

base tile 与「每元素工作量」：

[get_base_tile 与 compute_workload_per_item](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/default.py#L136-L167) —— 从全 1 的 tile 出发，逐维尝试放大，只要能让「每元素分摊的计算量」下降就采纳，目的是消除冗余搬运。

线程块大小打分——偏好 warp 数接近 `sm_partition`、且是 warp 整数倍：

[score_block_size 与 get_block_size](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/default.py#L202-L239) —— `r1 = max(warps/sm_partition, sm_partition/warps)` 惩罚偏离 4 个 warp；`r2` 惩罚「向上取整到 warp 倍数」造成的线程浪费。

线程分轴 `assign_block_size` → `_assign_block_size`：把 `block_size` 质因数分解，逐个因子贪心地分配到「让合并访问分数最优」的轴：

[DefaultPolicy._assign_block_size](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/default.py#L673-L755) —— 先尝试分给空间轴（按 `_score`，越小越好，本质是带宽加权访存），分不下去再分给 reduce 轴；最后据 dtype 设 `_step`（fp16→2、int8→4，保证合并访问）并调 `_plan_vectorize` 规划向量化宽度。

#### 4.3.4 代码实践

- **实践目标**：观察 `compute_tile_dict` 如何把一个 tile 变成一组代价数字，体会 priority 的含义。
- **操作步骤**：写一个**源码阅读型实践**。在 [default.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/default.py) 的 `compute_tile_dict` 末尾（约 L573 `return td` 前）临时加一行 `print(output_tile, "traffic=", td.traffic, "smem=", td.smem_cost, "waves=", td.num_wave, "block_per_SM=", td.block_per_SM)`，然后运行 4.1.4 的 `MatmulTemplate.recommend_hints(topk=10)`。
- **需要观察的现象**：会打印一连串 tile 及其代价；越靠前（priority 越小）的 tile，通常 `traffic` 与 `num_wave` 都偏小。
- **预期结果**：能直观看到「大 tile → traffic 小但 smem 大、block_per_SM 可能降」的权衡。
- **注意**：这是阅读/调试型实践，看完结论后记得删掉 `print`（本讲禁止改源码，仅临时调试用，请勿提交）。

#### 4.3.5 小练习与答案

- **练习 1**：`block_per_SM` 为什么取 `min(shared 约束, reg 约束, sm_partition)`？
  - **答案**：一个 SM 上能同时驻留多少个线程块，受三类资源同时约束——shared memory、寄存器、以及 SM 的子分区（sub-partition/warp slot）。任何一项用满都会限制占用率，所以取最小。
- **练习 2**：`dfs_smem_tile` 为什么设 `len(visited_tiles) > 2000` 的上限？
  - **答案**：tile 空间是各维因子的组合，高维时会指数膨胀。设上限是为了把代价模型控制在「秒级」内，宁可少评估也不让它跑飞；超过后直接用已访问的合法 tile 排序返回。

---

### 4.4 BestFit：共享内存代价估算

#### 4.4.1 概念说明

`BestFit`（[bestfit.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/bestfit.py)）是一个经典 best-fit free-list 分配器，但它在本项目里的用途有点特殊：**它不是 GPU 真正的运行时分配器，而是代价模型用来「仿真」一个 tile 配置下 shared memory 峰值占用的小工具**。

为什么需要它？因为一个 tile 在 kernel 里会用到多个 shared buffer（输入 A/B 的 tile、reduce 中间结果、输出 tile……），它们的**生命期**可能重叠也可能错开。如果直接把每个 buffer 大小加起来会严重高估——生命期不重叠的 buffer 可以复用同一片 shared。BestFit 配合活跃变量分析（liveness），把「可复用」的部分折叠掉，给出一个更接近真实的 `smem_cost`，供 4.3 节的合法性判断使用。

#### 4.4.2 核心流程

`BestFit` 维护一个有序的 `Block` 链表（每个 Block 标记 `is_free`），以及当前已用上限 `limit`：

- `malloc(size)`：先按 `align`（默认 32 字节）向上对齐 size；在所有 free 块里找**能放下且最小**的（best-fit）；命中则切分剩余；否则若尾部是 free 块就扩容，否则在末尾新开一块，并推进 `limit`。
- `free(block)`：标记为 free，并与**左右相邻的 free 块合并**（减少碎片）。

调用方（`_compute_shared_memory_usage`）按拓扑序遍历节点：malloc 每个节点所需 → 立即 free（因为只关心峰值而非累计）→ 处理完一个节点后 free 掉不再被下游使用的输入、malloc 它的输出。遍历结束 `allocator.limit` 就是峰值 shared 占用。

#### 4.4.3 源码精读

`BestFit.malloc`——best-fit 选择 + 切分 + 尾部扩容三种情况：

[BestFit.malloc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/bestfit.py#L28-L51) —— 注意 L32 的 best-fit 判据 `not found or found.size() > block.size()`（选最小的够用块）；L29 的对齐 `(size + align - 1)//align * align`。

`BestFit.free`——标记 + 双向合并：

[BestFit.free](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/bestfit.py#L53-L62)

真正调用它估算 shared 代价的地方：

[DefaultPolicy._compute_shared_memory_usage](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/default.py#L452-L495) —— L469 `allocator = BestFit()`；L474-475 的 `can_free` 判断「某输出是否所有下游都已处理完」（即生命期结束）；遍历结束 L494 `assert len(block_map) == 0`（所有分配都应被释放，保证一致性）；返回 `allocator.limit` 作为 `smem_cost`。

`infer_node_smem_usage` 给出单个节点的 shared 字节数（即它的输入/输出 tile footprint）：

[infer_node_smem_usage](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/default.py#L434-L450)

#### 4.4.4 代码实践

- **实践目标**：脱离 GPU，纯 Python 验证 BestFit 的行为，理解「生命期错开 → 复用 → 峰值更小」。
- **操作步骤**：运行（**示例代码**）
  ```python
  from tilelang.carver.roller.bestfit import BestFit
  alloc = BestFit(align=32)
  a = alloc.malloc(100)   # 实际分配 128（对齐到 32 的倍数）
  b = alloc.malloc(64)
  print("peak after a,b =", alloc.limit)   # 128 + 64 = 192
  alloc.free(a)           # a 生命期结束，释放
  c = alloc.malloc(64)    # 应复用 a腾出的空间，limit 不再增长
  print("peak after free(a),malloc(c) =", alloc.limit)
  ```
- **需要观察的现象**：第一次 `limit` 增长到 192；`free(a)` 后再 malloc 64，`limit` 不变（复用了 a 的空位）。
- **预期结果**：两次打印分别约为 `192` 和 `192`，体现 best-fit 复用。
- **待本地验证**：具体数值取决于对齐与切分实现，重点是「释放后峰值不再涨」。

#### 4.4.5 小练习与答案

- **练习 1**：`_compute_shared_memory_usage` 里每个节点 malloc 之后立刻 free 自己（L479-480），这会不会让 limit 永远等于单个最大节点？
  - **答案**：不会。因为节点的**输出**（L487-492）会被单独 malloc 并保留在 `block_map` 里，直到所有下游处理完才 free（`can_free`）。所以峰值反映的是「在产/在用」的 buffer 集合，而非单点。
- **练习 2**：默认 `align=32`。Hopper 上 TMA/wgmma 常要 1024 字节对齐，这个对齐值由谁控制？
  - **答案**：BestFit 的 `align` 是构造参数；在 Carver 的代价模型里用的是默认 32（见 [default.py:469](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/default.py#L469)）。真实的 1024 对齐发生在 tilelang 主编译流水线（u4-l4 的 `MergeSharedMemoryAllocations`），不在 Carver 代价模型里。

---

### 4.5 TensorCorePolicy：张量核心专用调度

#### 4.5.1 概念说明

`TensorCorePolicy`（继承自 `DefaultPolicy`）覆盖了若干方法，叠加 mma/wgmma 的硬约束：(1) reduce 步长必须是 `wmma_k`（默认 16）的倍数；(2) 块的 M/N 维必须不小于指令形状（如 16×16），否则非法；(3) 按 warp tile（而非 thread）切分块内并行；(4) 为 shared memory 加 stride 填充，避免 tensor core 布局下的 bank conflict；(5) 据 sm 版本设 `pipeline_stage` 与 `use_async_copy`（Ampere/Hopper 默认开 2 级流水 + cp.async/TMA）。

它还决定 `shared_scope`：当 `smem_cost > smem_cap` 或累加 dtype 是 float32/int32（占 shared 大）时，把输出 scope 设为 `"shared.dyn"`（动态 shared），并在 `complete_config` 里打开 `tir.merge_static_smem`（见 u4-l4）。

#### 4.5.2 核心流程

```
TensorCorePolicy.emit_config（继承自 DefaultPolicy）
  ├─ check_tile_shape_isvalid: 块的 ax_m/ax_n 必须 ≥ 某个可用 intrin 形状
  ├─ get_node_reduce_step_candidates: rstep 候选必须是 wmma_k 的倍数
  ├─ _assign_reduce_step / _expand_reduce_axis: 优先让读访问合并（512B 事务）
  └─ _assign_block_size: 按 warp tile 切分（block_size 必须是 warp_size 倍数）
        ├─ factors = factorize(prod(space) // warps)
        ├─ 逐因子贪心分配到 warp_tile 各维
        ├─ _compute_tc_strides: 给 A/B/C 算 shared padding stride
        └─ 产出 Hint(use_tc=True, warp=..., pipeline_stage=..., shared_scope=...)
```

#### 4.5.3 源码精读

合法性与候选步长的 tensorcore 约束：

[TensorCorePolicy.check_tile_shape_isvalid](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/tensorcore.py#L199-L213) —— 块的 `(block_m, block_n)` 必须至少能放进一个可用 intrin 形状。

[get_node_reduce_step_candidates](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/tensorcore.py#L192-L197) —— rstep 候选 = `wmma_k` 的倍数因子。

按 warp tile 切分块内并行——和 DefaultPolicy 的「按 thread 切分」是关键区别：

[TensorCorePolicy._assign_block_size](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/tensorcore.py#L240-L315) —— L244 要求 `block_size % warp_size == 0`；L250 取最大的可用 intrin 形状作 wmma_tile；L260 `factors = factorize(prod(space) // warps)`；最终产出 `Hint` 并设 `use_tc=True`、`pipeline_stage`、`shared_scope="shared.dyn"`（L308）、调 `tensorcore_legalization()` 只保留最后两维（L314，见 [Hint.tensorcore_legalization](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/hint.py#L227-L231)）。

shared memory bank-conflict 的 padding stride：

[TensorCorePolicy._compute_tc_strides](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/tensorcore.py#L52-L76) —— 在最高维之外加一个 `offset=8` 的 stride（L69），用于避免 tensor core 布局下 shared memory 的 bank conflict。

架构相关的流水/异步拷贝默认值：

[TensorCorePolicy._legalize_info](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/roller/policy/tensorcore.py#L29-L51) —— sm_80/sm_90 默认 `pipeline_stage=2` 且 `use_async_copy=True`；其他架构默认 1 级、不开异步。这些值最终进入 Hint 的 `pipeline_stage`，对应 tilelang 的 `T.Pipelined(num_stages=...)`。

#### 4.5.4 代码实践

- **实践目标**：对比 DefaultPolicy 与 TensorCorePolicy 的输出差异，体会 `use_tc` 带来的约束。
- **操作步骤**：写一个**源码阅读 + 运行型实践**（**示例代码**）：
  ```python
  import tilelang.language as T
  from tilelang.carver.arch import CUDA
  from tilelang.carver.utils import get_roller_hints_from_func
  from tilelang.carver.template import MatmulTemplate

  arch = CUDA("cuda")
  func = MatmulTemplate(M=1024,N=1024,K=1024,
                        in_dtype="float16",accum_dtype="float16",out_dtype="float16").equivalent_function()
  hints_tc = get_roller_hints_from_func(func, arch, topk=5, tensorcore_only=True)
  hints_def = get_roller_hints_from_func(func, arch, topk=5, tensorcore_only=False)
  print("TC hints:", [h.to_dict() for h in hints_tc])
  print("Default hints:", [h.to_dict() for h in hints_def])
  ```
- **需要观察的现象**：`tensorcore_only=True` 时每条 hint 有 `warp`、`use_tc=True`、`rstep` 是 16 的倍数；`tensorcore_only=False` 时通常仍走 tensorcore（因为 matmul 能被张量化），二者接近。
- **预期结果**：两组 hint 的 `block` 形相近，但 TC 组的 `rstep` 满足 16 倍数约束、含 `pipeline_stage`。
- **待本地验证**：无 GPU 时 `CUDA("cuda")` 失败。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 `TensorCorePolicy._assign_block_size` 要求 `block_size % warp_size == 0`，而 `DefaultPolicy` 没有这个硬要求？
  - **答案**：tensor core 指令（mma/wgmma）以 warp 为最小执行单位，块内线程数必须是 warp（32）的整数倍，才能整除成若干 warp 各算一块 warp tile。DefaultPolicy 走 SIMT 路径，线程可更自由分配。
- **练习 2**：`tensorcore_legalization()` 把 `self.warp` 和 `self.block` 截断为只保留最后两维。为什么只关心最后两维？
  - **答案**：tensor core 的 mma/wgmma 指令只对最后两个空间维（M、N）做矩阵乘；更高维的 batch 等通过外层循环处理，调度参数只需刻画最内两维的 tile。

---

## 5. 综合实践：用 Roller hint 指导 tilelang matmul 并实测对比

把本讲四块（Carver 模板、架构抽象、DefaultPolicy、TensorCorePolicy）串成一个端到端任务：**用 Carver 生成 hint → 人工翻译成 tilelang kernel 参数 → 实测延迟，对比「凭感觉手写的 baseline」与「Roller 推荐的 top-1」**。

> 这一步必须人工翻译，因为（4.1 已说明）仓库目前**没有**把 Hint 自动应用为 tilelang schedule 的一键函数，README 把它列为 TODO。这也正是理解 Hint 字段含义的最佳练习。

### 步骤

1. **生成 hint**：对 `M=N=K=1024` 的 fp16 GEMM，运行 4.1.4 的 `MatmulTemplate.recommend_hints(topk=10)`，记录 top-1 的 `block`、`rstep`、`pipeline_stage`。

2. **写 baseline kernel**（凭经验手写，比如 `block_M=block_N=128, block_K=32, num_stages=2, threads=128`），结构参考 [examples/analyze/example_gemm_analyze.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/analyze/example_gemm_analyze.py) 与 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/gemm/example_gemm.py)：
   ```python
   # 示例代码（骨架）
   import tilelang
   import tilelang.language as T

   def matmul(M, N, K, block_M, block_N, block_K, num_stages, threads,
              dtype="float16", accum_dtype="float32"):
       @T.prim_func
       def kernel(A: T.Tensor((M, K), dtype),
                  B: T.Tensor((N, K), dtype),     # transpose_B=True ⇒ B 存成 (N,K)
                  C: T.Tensor((M, N), dtype)):
           with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=threads) as (bx, by):
               A_s = T.alloc_shared((block_M, block_K), dtype)
               B_s = T.alloc_shared((block_N, block_K), dtype)
               C_f = T.alloc_fragment((block_M, block_N), accum_dtype)
               T.clear(C_f)
               for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=num_stages):
                   T.copy(A[by*block_M, k*block_K], A_s)
                   T.copy(B[bx*block_N, k*block_K], B_s)
                   T.gemm(A_s, B_s, C_f, transpose_B=True)
               T.copy(C_f, C[by*block_M, bx*block_N])
       return kernel

   kernel = tilelang.jit(out_idx=[-1])(matmul)(1024, 1024, 1024,
                                               block_M=128, block_N=128, block_K=32,
                                               num_stages=2, threads=128)
   latency = kernel.get_profiler().do_bench()
   print("baseline latency(ms):", latency)
   ```
   （`T.gemm(..., transpose_B=True)` 与 `MatmulTemplate` 默认 `trans_B=True` 一致，二者 B 都按 `(N,K)` 存。）

3. **翻译 hint**：把 top-1 hint 的 `block → (block_M, block_N)`、`rstep[0] → block_K`、`pipeline_stage → num_stages`、`warp` 推算的线程数（`prod(warp) * 32` 或 hint 推荐的 block_size）→ `threads`，重新编译并 `do_bench()`。

4. **对比**：记录两组延迟与 TFLOPS（`2*M*N*K / latency / 1e12`），说明 Roller 推荐是否更优、优在哪（block 更大复用更高？pipeline_stage 更高隐藏延迟？）。

### 观察与预期

- 若你的 GPU 是 Ampere/Hopper，Roller 的 top-1 通常会给出较大 block（如 128×128 或 128×256）+ `pipeline_stage≥2` + `rstep` 为 16/32/64 的倍数，延迟应不差于、常优于「128/128/32/stage2」的朴素 baseline。
- 若 hint 给的 block 超出 shared 上限，`TensorCorePolicy` 会把 `shared_scope` 设为 `shared.dyn`——你会在生成的 CUDA 源码里看到动态 shared 分配（可用 `kernel.get_kernel_source()` 核对，见 u3-l5）。

### 待本地验证

无 GPU 环境无法实测；此时可退化为「源码阅读型」：打印 top-1 hint 的 dict，对照本讲字段表，逐项解释它推荐的 tile 含义，并口算其 `traffic`/`num_wave` 是否合理。

---

## 6. 本讲小结

- **Carver 是「调度策略推荐框架」**：产出是 `Hint`（block/warp/rstep/use_tc/vectorize/pipeline_stage 等的 dict），不是可执行 kernel，目前需人工翻译进 tilelang（README 把自动集成列为 TODO）。
- **架构抽象 `TileDevice`**：把 `smem_cap/warp_size/sm_partition/transaction_size/bandwidth` 等硬件参数抽象成统一字段，CUDA/CDNA/CPU/Metal 复用同一套 policy。
- **DefaultPolicy 是通用代价模型**：base tile → reduce step（合并访问）→ DFS 搜 block tile（优先级 `(traffic+1)×num_wave`）→ 合法性裁剪（shared/reg 上限）→ 贪心分配线程，产出 `Hint`。
- **BestFit** 是代价模型内部的 shared memory 仿真器：用 best-fit free-list + 活跃变量分析估算 tile 的峰值 shared 占用，而非 GPU 真正的运行时分配。
- **TensorCorePolicy** 在 DefaultPolicy 上叠加 mma/wgmma 约束：rstep 是 16 倍数、块不小于 intrin 形状、按 warp tile 切分、加 shared padding stride 防 bank conflict、按 sm 版本默认开 `pipeline_stage` 与 `use_async_copy`。
- **与 u5-l1 Autotuner 的关系**：Carver/Roller 负责「凭代价模型给高质量小候选集」，Autotuner 负责「穷举实测选优」；二者可串联——用 Carver 缩小空间再交给 Autotuner 实测。

---

## 7. 下一步学习建议

- **回到 Autotuner 做串联**：试着把本讲 Roller 给出的 top-k hint 翻译成 u5-l1 的 `@tilelang.autotune(configs=...)` 的 configs 列表，让 Autotuner 在这个小空间里实测选优——这是「代价模型 + 实测」的工程最佳实践。
- **深入算子后端**：Carver 生成的 `use_tc=True`、`intrin_info`、warp tile 最终如何在 C++ 侧落地为 mma/wgmma 指令，见 u7-l1（C++ 算子实现）与 u7-l2（CUDA GEMM 模板族，`src/tl_templates/cuda/gemm_*.h`）。
- **看分析侧**：本讲多次引用 `normalize_prim_func`、`BlockInfo`、`get_reduction_blocks`（[analysis.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/analysis.py)），想理解「Carver 如何识别一个 TIR block 是 SSR 结构、能被张量化」可精读 [matmul_analysis.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/matmul_analysis.py) 的 `get_tensorized_func_and_tags`。
- **扩展模板**：若你的算子不在现有模板（matmul/gemv/elementwise/general_reduce/flash_attention）覆盖范围内，可参考 [template/base.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/carver/template/base.py) 继承 `BaseTemplate`，实现 `initialize_function` 与 `get_hardware_aware_configs`，复用 Roller policy 生成自己的 hint。
