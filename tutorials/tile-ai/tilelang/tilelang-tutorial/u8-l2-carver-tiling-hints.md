# Carver：切分提示推荐框架

## 1. 本讲目标

上一讲（u8-l1）的 Autotuner 解决的是「给我一组候选配置，我帮你逐一编译、测量、取最快的那个」。但这立刻引出一个更上游的问题：**这组候选配置从哪里来？** 一个 GEMM 的 `block_M/block_N/block_K/num_stages/thread_num` 可以组合出成百上千种取值，手工列举既费劲又容易漏掉真正快的那些。

本讲讲解 tilelang 内置的 **Carver** 框架——一个「切分提示（tiling hint）推荐器」。它不需要把候选编译成 kernel、也不需要真的运行，而是**纯粹靠硬件模型 + 启发式分析**，在几毫秒内排出一份「张量核友好、shared memory 放得下、并行度合理」的 tile 候选清单，直接喂给 Autotuner 或人工挑选。读完本讲，你应该能够：

1. 说清 Carver 的三层架构 `arch / template / roller` 各自的职责与协作关系。
2. 用 `MatmulTemplate` / `GeneralReductionTemplate` 配合 `CUDA` 架构对象，调用 `recommend_hints()` 拿到一份 tile 候选清单。
3. 理解 roller 内部「生成候选 → bestfit 评估 shared memory → 按访存与并行度打分排序」的启发式流程。
4. 区分两种 policy（`DefaultPolicy` 与 `TensorCorePolicy`），以及 `get_tensorized_func_and_tags` 如何判定一个算子能否走张量核。
5. 知道如何把一个 hint 翻译成 tilelang 的 `T.Kernel` 参数，并在 tilelang 中验证正确性。

本讲覆盖四个最小模块：`tilelang.carver`（总览与包入口）、`tilelang.carver.arch`（硬件模型）、`tilelang.carver.template`（算子模板）、`tilelang.carver.roller`（候选生成与排序引擎）。

## 2. 前置知识

阅读本讲前，建议你已经掌握：

- **Autotuner**（讲义 u8-l1）：Carver 的产物（hint 列表）最常见的去向就是 Autotuner 的配置空间。本讲是 u8-l1 的「上游」。
- **T.gemm 与 tile op 体系**（讲义 u3-l1）：理解「tile op 先留占位、后按硬件展开」的模型，有助于理解 Carver 为什么要在 IR 层面识别 matmul 模式。
- **类型系统与低精度 dtype**（讲义 u2-l4）：Carver 的张量核判定高度依赖 `in_dtype/accum_dtype` 的组合（如 `float16×float16`、`int8×int32`、`float8_e4m3`）。
- **目标判定**（讲义 u4-l4）：Carver 的架构对象由 target 推断而来（`cuda→CUDA`、`hip→CDNA/RDNA`、`llvm→CPU`、`metal→METAL`）。

几个术语先统一：

- **tile hint（切分提示）**：一份描述「这个 kernel 该怎么分块」的参数字典，例如 `{'block':[128,128], 'warp':[64,64], 'rstep':[32], 'use_tc':True}`。它本身不是 kernel，只是一组建议。
- **template（模板）**：一类算子的高层抽象（如 matmul、gemv、elementwise），负责用 TVM `te.compute` 搭出该算子的等价 `PrimFunc`，作为 roller 分析的输入。
- **arch（架构模型）**：对一块硬件的数值化描述，包括 `smem_cap`（每个 block 的 shared memory 上限）、`warp_size`、`compute_max_core`（SM 数）、`reg_cap`（寄存器上限）、`transaction_size`（一次访存事务的字节数）、可用张量指令形状等。
- **roller**：Carver 的「调度推断引擎」，名字沿用自经典编译器论文/项目 Roller（通过硬件约束推导 tile 形状）。它读入 `PrimFunc` + `arch`，产出排序后的 hint 列表。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/carver/README.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/README.md) | 框架总览、用法示例、支持的架构与模板清单。 |
| [tilelang/carver/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/__init__.py) | 包入口，把 `arch`/`template`/`roller` 的公共符号聚合导出。 |
| [tilelang/carver/arch/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/arch/__init__.py) | `get_arch()` 把 target 映射到架构类，`auto_infer_current_arch()` 自动探测当前设备。 |
| [tilelang/carver/arch/arch_base.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/arch/arch_base.py) | `TileDevice` 抽象基类，定义所有架构共有的硬件约束字段。 |
| [tilelang/carver/arch/cuda.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/arch/cuda.py) | `CUDA` 架构实现、SM 版本判定、各代张量核支持的精度表。 |
| [tilelang/carver/template/base.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/template/base.py) | `BaseTemplate` 抽象基类：`with_arch()`、`recommend_hints()`、`equivalent_function()`。 |
| [tilelang/carver/template/matmul.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/template/matmul.py) | `MatmulTemplate`：用 `te.compute` 搭等价 GEMM，委托 roller 出 hint。 |
| [tilelang/carver/template/general_reduce.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/template/general_reduce.py) | `GeneralReductionTemplate`：用 `SSR` 之类的结构串描述通用规约算子。 |
| [tilelang/carver/utils.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/utils.py) | `get_roller_hints_from_func()`：template 与 roller 之间的桥梁，决定走哪种 policy。 |
| [tilelang/carver/matmul_analysis.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/matmul_analysis.py) | `get_tensorized_func_and_tags()`：识别算子是否可张量化、推导 `tensorcore_config`/`pipeline_stage` 等标签。 |
| [tilelang/carver/roller/policy/default.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/policy/default.py) | `DefaultPolicy`：CUDA Core 通用调度，DFS 搜索 tile 形状并打分。 |
| [tilelang/carver/roller/policy/tensorcore.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/policy/tensorcore.py) | `TensorCorePolicy`：张量核专用调度，按 warp 切分、约束 tile 是 MMA 形状的倍数。 |
| [tilelang/carver/roller/bestfit.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/bestfit.py) | `BestFit`：_best-fit_ 内存分配器，用于估算一份 tile 配置的 shared memory 峰值占用。 |
| [tilelang/carver/roller/rasterization.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/rasterization.py) | `Rasterization` 系列：L2 cache 友好的 block 遍历顺序（blockIdx 栅格化）建议。 |
| [tilelang/carver/roller/hint.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/hint.py) | `Hint`/`TileDict`/`IntrinInfo` 数据结构：hint 的字段定义与序列化。 |
| [examples/gemm/example_gemm_autotune.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm_autotune.py) | 可运行示例：用 `MatmulTemplate.recommend_hints()` 生成 Autotuner 的配置空间。 |

---

## 4. 核心概念与源码讲解

### 4.1 Carver 全景：arch / template / roller 三层架构

#### 4.1.1 概念说明

Carver 的设计目标是**「不跑 kernel，只靠静态分析推荐 tile」**。为此它把问题拆成三个正交的关注点，构成一个清晰的三层管道：

```text
        ┌─────────────────────────────────────────────┐
        │  template（算子模板）                         │
        │  输入：算子的形状/dtype（如 M=N=K=1024, fp16）  │
        │  产出：一个等价的 TVM PrimFunc（te.compute 搭出）│
        └──────────────────────┬──────────────────────┘
                               │ PrimFunc
        ┌──────────────────────▼──────────────────────┐
        │  roller（调度推断引擎）                        │
        │  输入：PrimFunc + arch                        │
        │  产出：排序后的 Hint 列表（block/warp/rstep…） │
        └──────────────────────┬──────────────────────┘
                               │ Hint 列表
        ┌──────────────────────▼──────────────────────┐
        │  arch（硬件模型）—— 贯穿始终的「约束来源」       │
        │  smem_cap / warp_size / 张量指令形状 / 带宽 …  │
        └─────────────────────────────────────────────┘
```

- **template** 负责「这个算子长什么样」：它用 TVM 的 `te.placeholder`/`te.compute` 搭出一个语义等价的 `PrimFunc`。比如 `MatmulTemplate(M,N,K)` 会搭出标准的 `C[i,j] = sum_k A[i,k]*B[k,j]`。这一步**完全不知道目标硬件**，纯粹是算子结构。
- **arch** 负责「目标硬件长什么样」：它把一块 GPU/CPU 数值化成一堆约束常量（shared memory 多大、warp 多宽、有没有张量核、张量核支持哪些 dtype）。
- **roller** 负责「在硬件约束下，这个算子该怎么切」：它读入 template 产出的 `PrimFunc` 和 arch，枚举 tile 候选、用 arch 的约束筛掉非法的、用 bestfit 估 shared memory、按访存量与并行度打分，最后排出 top-K。

为什么要把 template 和 roller 分开？因为**同一个 roller 引擎可以服务多种算子**（matmul、gemv、conv、elementwise…），只要这些算子能被表达成一个 `PrimFunc`；而**同一个算子模板可以对接多种硬件**，只要换一个 arch 对象。这种正交解耦让 Carver 很容易扩展（加新算子只需加 template，加新硬件只需加 arch）。

一个关键认知：Carver **不依赖 tilelang 的 DSL**。template 用的是 TVM 原生的 `te.compute`，roller 分析的也是 TVM `PrimFunc`。这意味着 Carver 的 hint 理论上可以喂给任何能消费「block/warp/rstep」语义的编译器（README 明确提到可适配 TVM/Triton/tilelang）。

#### 4.1.2 核心流程

一次完整的 hint 推荐流程：

```text
用户代码：
  arch = CUDA("nvidia/geforce-rtx-4090")
  tpl  = MatmulTemplate(M=1024, N=1024, K=1024, in_dtype="float16", ...).with_arch(arch)
  hints = tpl.recommend_hints(topk=20)

内部展开：
  1. MatmulTemplate.__post_init__ → initialize_function()
       用 te.compute 搭等价 PrimFunc，存入 self._func
  2. tpl.recommend_hints(topk) → get_hardware_aware_configs(arch, topk)
       → get_roller_hints_from_func(self._func, arch, topk)
  3. get_roller_hints_from_func:
       a. get_tensorized_func_and_tags(func, target)  # 能走张量核吗？
       b. 若可张量化：policy = TensorCorePolicy(...)    # 走 MMA 切分
          否则      ：policy = DefaultPolicy(...)       # 走通用 CUDA Core 切分
       c. policy.emit_config(topk) → list[Hint]        # 枚举+评估+排序
  4. 返回排序后的 Hint 列表
```

注意第 3a 步的「分流」：Carver 会先尝试把算子识别成 matmul 并判定能否张量化；只有张量化成功且 dtype 被硬件张量核支持时，才走 `TensorCorePolicy`，否则退回 `DefaultPolicy`。这就是为什么同一份 `PrimFunc` 在不同 arch（比如有没有张量核）上会得到风格完全不同的 hint。

#### 4.1.3 源码精读

包入口只是把三个子模块的公共符号聚合再导出，结构非常薄：

[carver/\_\_init\_\_.py:1-16](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/__init__.py#L1-L16) —— 注意它从 `arch` 导入 `CUDA/CDNA/RDNA`，从 `template` 导入五个模板，并用 `from .roller import *` 把 `Hint`/`DefaultPolicy`/`TensorCorePolicy` 等也带出来。所以用户既可以 `from tilelang import carver; carver.MatmulTemplate`，也可以 `from tilelang.carver.template import MatmulTemplate`。

template 与 roller 之间的桥梁定义在 utils 里，是理解整个流程的关键一行：

[carver/utils.py:29-65](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/utils.py#L29-L65) —— `get_roller_hints_from_func`。第 55-64 行是核心：默认（`tensorcore_only=False`）先建 `DefaultPolicy`，再尝试 `get_tensorized_func_and_tags`；若拿到 `tags`（说明可张量化），就把 policy **替换**成 `TensorCorePolicy`，然后统一 `emit_config(topk)`。这就是「张量核优先、CUDA Core 兜底」的分流逻辑。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立「template 产 PrimFunc → roller 消费」的心智模型。
2. **操作步骤**：
   - 打开 [carver/utils.py:54-64](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/utils.py#L54-L64)，标注哪一行决定走 `TensorCorePolicy`、哪一行是兜底的 `DefaultPolicy`。
   - 打开 [carver/\_\_init\_\_.py:13-15](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/__init__.py#L13-L15)，确认 `roller` 的 `*` 导出里包含了 `Hint`、`DefaultPolicy`、`TensorCorePolicy`（见 [roller/\_\_init\_\_.py:1-6](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/__init__.py#L1-L6)）。
3. **需要观察的现象**：`get_roller_hints_from_func` 的返回值可能是 `None`（第 53 行 `roller_hints = None`），这正是 `example_gemm_autotune.py` 里 `if roller_hints is None: raise ValueError(...)` 的由来——不是所有算子/硬件组合都能产出 hint。
4. **预期结果**：能在源码里指出「policy 替换」发生的那一行（utils.py 第 62-63 行）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `get_roller_hints_from_func` 的 `tensorcore_only=True`（见 [utils.py:43-53](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/utils.py#L43-L53)），当算子无法张量化时返回什么？

**答案**：返回 `None`（第 53 行）。`tensorcore_only` 模式只接受张量核路径，张量化失败就不退回 `DefaultPolicy`，直接给 `None`。

**练习 2**：为什么 `carver/__init__.py` 要用 `from .roller import *` 而不是显式列出 `Hint`、`DefaultPolicy`？

**答案**：roller 是一个会持续扩展的子模块（未来可能加新 policy、新数据结构），用 `*` 让 `roller/__init__.py` 成为唯一的「公共符号清单」，避免每加一个类都要同步改 `carver/__init__.py`。代价是导出集合不够显式，但 roller 的 `__all__` 没定义，实际导出的是 `roller/__init__.py` 里所有不以下划线开头的名字。

---

### 4.2 arch：把硬件数值化成约束常量

#### 4.2.1 概念说明

roller 在打分时需要回答一堆「这个 tile 放不放得下」的数值问题：

- 这个 tile 的 shared buffer 会不会超过 `smem_cap`？
- 这么多线程是不是 `warp_size` 的整数倍，能不能占满 SM 的 `sm_partition` 个分区？
- 这个 dtype 组合有没有张量核指令可用，指令形状是多少？
- 一次 global 读多少字节算一个 transaction（`transaction_size`）？

这些问题需要一个「硬件数值模型」来回答——这就是 `arch` 子模块的职责。它定义了一个抽象基类 `TileDevice`，列出所有架构共有的字段；每个具体架构（`CUDA`/`CDNA`/`RDNA`/`METAL`/`CPU`）填上自己的数值。

一个重要设计：**arch 是「被动查询」的**。roller 在需要某个约束时去读 `arch.smem_cap`，arch 本身不做任何决策。这让 arch 与 policy 彻底解耦——换一块卡只需换 arch，policy 代码一行都不用改。

#### 4.2.2 核心流程

从 target 到 arch 对象的映射：

```text
target（字符串/dict/Target）
   │  get_arch(target)            # 按 target.kind.name 分发
   ├─ "cuda"  ──► CUDA(target)
   ├─ "llvm"  ──► CPU(target)
   ├─ "hip"   ──► RDNA(target)  （gfx11/gfx12）
   │            └► CDNA(target) （其它 gfx）
   ├─ "metal" ──► METAL(target)
   └─ 其它    ──► ValueError

auto_infer_current_arch()        # 不传 target 时的自动探测
   ├─ torch.version.hip is not None ──► get_arch("auto")
   ├─ torch.cuda.is_available()      ──► get_arch("cuda")
   ├─ torch.mps.is_available()       ──► get_arch("metal")
   └─ 否则                            ──► get_arch("llvm")
```

`auto_infer_current_arch` 是 `BaseTemplate` 的默认 arch 工厂：如果你建 template 时不调用 `.with_arch(...)`，它就会自动探测当前机器。这就是 README 里「不显式指定 arch 也能跑」的原因——但自动探测要求机器上真的有对应硬件（`CUDA` 构造时会 `tvm.runtime.cuda(0)` 并检查 `device.exist`）。

#### 4.2.3 源码精读

抽象基类定义了所有架构共享的字段，默认值都是 0（代表「未知/未设置」）：

[carver/arch/arch_base.py:1-31](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/arch/arch_base.py#L1-L31) —— `TileDevice`。重点字段：`reg_cap`（寄存器上限）、`smem_cap`（每 block shared memory）、`compute_max_core`（SM/核心数）、`warp_size`、`sm_partition`（SM 分区数）、`transaction_size`（`[写, 读]` 字节数）、`bandwidth`（`[写, 读]` 带宽 MB/s）、`l2_cache_size_bytes`。`get_avaliable_tensorintrin_shapes` 是抽象方法，子类必须实现，告诉 roller「这块卡的张量核有哪些指令形状」。

CUDA 的具体实现，逐字段从 TVM 设备属性里读取：

[carver/arch/cuda.py:124-167](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/arch/cuda.py#L124-L167) —— `CUDA` 类。注意几个关键点：
- 第 131 行 `self.sm_version = check_sm_version(...)`：从 target 的 `arch` 属性（如 `sm_90`）解析出数值 90，这是后面所有「代际判定」的基础。
- 第 139 行 `self.smem_cap`：每 block 的 shared memory 上限，是 roller 筛 tile 的硬约束。
- 第 143-144 行：`reg_cap=65536`、`max_smem_usage=2*smem_cap`（动态 shared memory 可以用到静态上限的 2 倍）。
- 第 148-153 行：`transaction_size=[32,128]`（写 32 字节、读 128 字节一个事务）、`bandwidth=[750,12080]`（近似带宽，注释说明「真实带宽难取，但跨设备比例相似，可用于相对打分」）。
- 第 158-163 行 `get_avaliable_tensorintrin_shapes`：返回 `[[16,16],[16,16]]`（mma 与 wmma 都是 16×16），roller 据此判断 tile 的 M/N 是否够放一个 MMA。

各代张量核支持的精度表是张量核判定的依据：

[carver/arch/cuda.py:73-110](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/arch/cuda.py#L73-L110) —— Volta 支持 `(fp16,fp32)/(fp16,fp16)`；Ampere 多了 `bf16/int8/int4`；Ada/Hopper 又加了 `float8_e5m2/float8_e4m3`。`is_tensorcore_supported_precision` 按 SM 代际查表。这解释了为什么同一个 `MatmulTemplate(in_dtype="float8_e4m3")` 在 Volta 上会退回 `DefaultPolicy`（张量核不支持）、在 Hopper 上才走 `TensorCorePolicy`。

target → arch 的分发与自动探测：

[carver/arch/\_\_init\_\_.py:15-45](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/arch/__init__.py#L15-L45) —— `get_arch` 按 `target.kind.name` 选类；HIP 还会进一步用 `target_is_rdna` 区分 RDNA（gfx11/12）与 CDNA。`auto_infer_current_arch` 用 torch 探测当前设备，顺序是 HIP→CUDA→MPS→CPU。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：理解「arch 是被动查询的数值模型」，以及哪些字段直接决定 tile 能不能被选中。
2. **操作步骤**：
   - 打开 [carver/roller/policy/default.py:558-560](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/policy/default.py#L558-L560)，看 `td.smem_cost > self.arch.smem_cap` 如何把一个 tile 直接判 `valid=False`。
   - 打开 [carver/roller/policy/default.py:565-567](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/policy/default.py#L565-L567)，看寄存器占用 `reg_usage > self.arch.reg_cap` 同样判失效。
   - 打开 [carver/roller/policy/default.py:216-219](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/policy/default.py#L216-L219)，看 `score_block_size` 如何用 `warp_size`/`sm_partition` 给 block size 打分。
3. **需要观察的现象**：arch 的每个字段都在 policy 里有对应的「读者」。
4. **预期结果**：能画出「arch 字段 → policy 中的使用点」的对照表（如 `smem_cap→compute_tile_dict 的 valid 判定`、`warp_size→score_block_size`、`transaction_size→_compute_memory_traffic`）。

#### 4.2.5 小练习与答案

**练习 1**：如果一块卡的 `transaction_size[1]`（读事务字节数）变大，roller 倾向于推荐更大的还是更小的 rstep？

**答案**：更大的 rstep。`_assign_reduce_step`（[default.py:298-303](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/policy/default.py#L298-L303)）用 `read_transaction_elements = transaction_size[1] // nbytes` 作为「理想连续读取元素数」，事务越大，理想 rstep 越大，启发式会放大 rstep 去逼近它。

**练习 2**：为什么 `auto_infer_current_arch` 在没有 CUDA/MPS 时退回 `llvm`（CPU）而不是抛错？

**答案**：为了保证 Carver 在纯 CPU 机器上也能 import 和分析（虽然产出的 hint 是 CPU 风格的）。这与 tilelang 的「轻量导入」哲学一致——无 GPU 机器也能用一部分功能。但 `CUDA` 类构造时会检查 `device.exist`（[cuda.py:132-134](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/arch/cuda.py#L132-L134)），所以无 N 卡的机器上显式建 `CUDA(...)` 会报错。

---

### 4.3 template：算子模板与等价函数

#### 4.3.1 概念说明

roller 只认 `PrimFunc`，不认「matmul」「gemv」这些高层概念。template 的职责就是**把用户的高层算子描述翻译成一个 roller 能分析的 `PrimFunc`**。它用 TVM 的 `te.placeholder`/`te.compute`/`te.create_prim_func` 三件套来搭这个等价函数：

- `te.placeholder`：声明输入张量（只有形状和 dtype，没有数据）。
- `te.compute`：描述输出张量每个元素怎么算（含 `te.reduce_axis` 表达规约轴）。
- `te.create_prim_func`：把上面两步的 compute DAG 包成一个 `PrimFunc`。

template 是 `@dataclass`，把算子的所有参数（如 `M/N/K/trans_A/in_dtype`）当成字段。构造完成后，`__post_init__` 会自动调 `initialize_function()` 把等价 `PrimFunc` 搭好存进 `self._func`。

`BaseTemplate` 还提供两个通用能力：
- `with_arch(arch)`：链式绑定架构对象（返回 self，所以可以 `.with_arch(arch)` 链式调用）。
- `recommend_hints(topk)`：模板方法，固定调用 `get_hardware_aware_configs(self._arch, topk)`，子类只需实现后者。

#### 4.3.2 核心流程

template 的工作分两段——构造时搭函数，调用时委托 roller：

```text
构造阶段（__init__ 触发 __post_init__）：
  MatmulTemplate(M=1024, N=1024, K=1024, in_dtype="float16", accum_dtype="float32", ...)
    └─ initialize_function():
         A = te.placeholder((M,K), dtype=in_dtype)
         B = te.placeholder((N,K), dtype=in_dtype)   # trans_B=True 默认
         k = te.reduce_axis((0,K))
         C = te.compute((M,N), lambda i,j: te.sum(A[i,k]*B[j,k], axis=k))
         self._func = te.create_prim_func([A,B,C])

推荐阶段：
  tpl.recommend_hints(topk=20)
    └─ get_hardware_aware_configs(arch, topk)
         └─ get_roller_hints_from_func(self._func, arch, topk)   # 委托 roller
              └─ list[Hint]
```

注意 `MatmulTemplate` 默认 `trans_B=True`（[matmul.py:34](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/template/matmul.py#L34)），即 B 以 `(N,K)` 行主存、计算时转置——这符合深度学习里权重通常存为 `(out,in)` 的惯例，也和 tilelang 示例里 `A @ B.T` 的参考实现一致。

#### 4.3.3 源码精读

抽象基类用 `@dataclass` + `ABC`，把 arch 默认值设为「自动探测」：

[carver/template/base.py:17-48](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/template/base.py#L17-L48) —— `BaseTemplate`。第 26 行 `_arch` 的 `default_factory=auto_infer_current_arch` 是关键：不显式 `with_arch` 时自动探测当前设备。第 34-48 行 `get_hardware_aware_configs` 是抽象方法，子类必须实现。

`recommend_hints` 是个朴素的模板方法：

[carver/template/base.py:153-163](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/template/base.py#L153-L163) —— 它只是把 `topk` 透传给 `get_hardware_aware_configs(self._arch, topk)`。所有「怎么生成 hint」的逻辑都在子类的 `get_hardware_aware_configs` 里（而子类又都委托给 roller）。

`MatmulTemplate` 的等价函数构造，是 template 模式的典型样本：

[carver/template/matmul.py:40-52](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/template/matmul.py#L40-L52) —— `get_hardware_aware_configs` 直接 `return get_roller_hints_from_func(self._func, arch, topk, allow_gemv=True)`。注意 `allow_gemv=True`：当 M 很小（退化为矩阵×向量）时，允许走 GEMV 的张量化路径。

[carver/template/matmul.py:82-110](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/template/matmul.py#L82-L110) —— 等价函数核心：`A=placeholder((M,K))`、`B=placeholder((N,K))`（因 `trans_B=True`）、`k=reduce_axis((0,K))`、`C=compute((M,N), _compute_matmul)`。`_compute_matmul` 里根据 `trans_A/trans_B` 调整下标顺序。这一段产出的 `PrimFunc` 就是 roller 的输入。

通用规约模板用「结构串」描述更一般的算子：

[carver/template/general_reduce.py:21-109](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/template/general_reduce.py#L21-L109) —— `GeneralReductionTemplate` 接受 `structure`（如 `"SSR"`）与 `shape`（如 `[1024,1024,1024]`），逐字符判断每个轴是 Spatial 还是 Reduce，动态拼出 `te.compute` 的 lambda。`"SS"` 就是 2D elementwise，`"SSR"` 就是通用矩阵乘。这是比 `MatmulTemplate` 更泛化、但信息更少（不告诉 roller「这是 matmul」）的入口。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：理解 `MatmulTemplate` 与 `GeneralReductionTemplate` 产出的 `PrimFunc` 差异，以及为什么前者能走张量核、后者通常不能。
2. **操作步骤**：
   - 在 Python 里（**待本地验证**，需要装好 tilelang）执行：
     ```python
     from tilelang.carver.template import MatmulTemplate, GeneralReductionTemplate
     print(MatmulTemplate(M=128, N=128, K=128).equivalent_function())
     print(GeneralReductionTemplate(structure="SSR", shape=[128,128,128]).equivalent_function())
     ```
   - 对比两个 `PrimFunc` 的 IR：`MatmulTemplate` 的 reduce block 读两个 tensor、写一个，且下标模式正好是 `C[i,j]+=A[i,k]*B[j,k]`，能被 `detect_iter_traits` 识别为 matmul（见下文 4.4.3）；`GeneralReductionTemplate` 的 reduce block 读的是同一个 `A` 的多轴，未必匹配 matmul 模式。
3. **需要观察的现象**：两者 IR 结构不同；`MatmulTemplate` 更容易被 `get_tensorized_func_and_tags` 识别。
4. **预期结果**：理解「template 决定了 roller 能不能识别出张量核机会」。

> 说明：若运行环境的 `te.create_prim_func` 行为有差异，IR 文本可能与上述描述略有出入，以本地实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：`MatmulTemplate(trans_B=True)` 时，B 的 placeholder 形状是什么？计算式怎么索引 B？

**答案**：B 形状是 `(N, K)`（[matmul.py:78](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/template/matmul.py#L78)）。计算时 `B_indices = [j, k]`（[matmul.py:102](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/template/matmul.py#L102)），即 `B[j,k]`，等价于数学上的 `Bᵀ[k,j]`，所以整体是 `C[i,j]=Σₖ A[i,k]·Bᵀ[k,j]`，即 `A @ B.T`。

**练习 2**：`BaseTemplate` 为什么把 `_func`/`_arch` 都设成 `init=False, repr=False` 的字段？

**答案**：`init=False` 让它们不出现在 `__init__` 参数列表（用户构造 `MatmulTemplate(M=128,...)` 时不用传 `_func`）；`repr=False` 让它们不出现在 `__repr__`（避免打印一大坨 PrimFunc）。`_func` 由 `__post_init__→initialize_function` 自动填充，`_arch` 由 `default_factory` 自动探测或由 `with_arch` 显式设置。

---

### 4.4 roller：候选生成、bestfit 评估与排序

#### 4.4.1 概念说明

roller 是 Carver 的「大脑」，负责把一个 `PrimFunc` + arch 变成排序后的 hint 列表。它内部又分三件事，对应三个子模块：

- **policy（策略）**：候选生成与排序的主体。`DefaultPolicy` 处理通用 CUDA Core 调度，`TensorCorePolicy` 处理张量核调度（继承自 `DefaultPolicy`，覆写关键钩子）。
- **bestfit（内存评估）**：一个 _best-fit_ 内存分配器，用来估算「在某个 tile 配置下，shared memory 的峰值占用是多少」。注意它的名字容易误导——它**不是**用来搜索 tile 的，而是 policy 在评估每个候选时调用它来算 shared memory 成本。
- **rasterization（栅格化）**：给出「blockIdx 该按什么顺序遍历输出网格」的建议，用于提升 L2 cache 局部性。

roller 的核心思路可以概括成一句话：**枚举 tile 形状 → 用硬件约束筛掉非法的 → 用 bestfit 估 shared memory → 按「访存量 × 波数」打分 → 取 top-K，再给每个 tile 分配 block/warp/thread**。

这里有个关键的简化假设：roller **不模拟指令执行**，只用静态公式估算两类成本：
- **访存量（traffic）**：根据 tile 形状和 transaction_size，算出要读写多少字节，并考虑合并访问（coalesced）的折扣。
- **波数（num_wave）**：`grid_size / (block_per_SM × SM数)`，即整个网格要分几「波」才能在所有 SM 上跑完。波数越多，末尾波的拖尾（tail）浪费越大。

打分函数就是两者的乘积（[default.py:111-112](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/policy/default.py#L111-L112)）：

\[
\text{prio}(\text{td}) = (\text{td.traffic} + 1) \times \text{td.num\_wave}
\]

值越小越好——既要访存少，又要波数少（并行度足）。

#### 4.4.2 核心流程

`emit_config` 是 roller 的总入口，串起整个流程：

```text
DefaultPolicy.emit_config(topk):
  base_tile = get_base_tile()                 # 最小无冗余 tile（全是 1 起步）
  rstep_map = {node: _assign_reduce_step(node) for node}   # 先定 K 维步进
  smem_tile_candidates = dfs_smem_tile(base_tile, rstep_map)  # DFS 枚举+排序 tile
      │  对每个候选 tile:
      │    compute_tile_dict(tile)            # 算 traffic / smem_cost / grid / 波数
      │      └─ _compute_shared_memory_usage(td)
      │           └─ BestFit 分配器估峰值 smem
      │    if smem_cost > smem_cap or reg_usage > reg_cap: valid=False
      │    按 prio=(traffic+1)*num_wave 入优先队列
      │  返回按 prio 升序的合法 tile 列表
  │
  for td in smem_tile_candidates（已按 prio 排序）:
      if not check_tile_shape_isvalid(td): continue
      _expand_reduce_axis(td)                 # 在 smem 余量内尽量放大 rstep
      for hint in assign_block_size(td):      # 给 tile 分配 block/warp/thread
          results.append(hint)
          if len(results) >= topk: return results
```

几个要点：

1. **枚举空间被刻意限制**：`dfs_smem_tile` 在 `len(visited_tiles) > 2000` 时停止（[default.py:123](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/policy/default.py#L123)），且每个轴的候选只取「该维度的因子 + {2,4,8,16,32}」，避免组合爆炸。
2. **排序用优先队列（Dijkstra 风格）**：从 base_tile 出发，每次取当前最优 tile，向「更大」的邻居扩展，保证先访问高质量区域。
3. **block 分配是最后一步**：tile 形状定了之后，才在 `assign_block_size` 里决定 block 内线程怎么切（或张量核路径下的 warp 怎么切）。

张量核路径的差别集中在 `_assign_block_size` 的覆写上：`TensorCorePolicy` 要求 `block_size % warp_size == 0`、tile 的 M/N 必须是 MMA 形状（如 16）的倍数、并按 warp 而非 thread 切分（[tensorcore.py:262-340](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/policy/tensorcore.py#L262-L340)），产出的 hint 带 `use_tc=True`、`warp=[...]`、`pipeline_stage`、`intrin_info` 等张量核专属字段。

#### 4.4.3 源码精读

`emit_config` 是 roller 的总编排，结构清晰：

[carver/roller/policy/default.py:72-94](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/policy/default.py#L72-L94) —— 三步走：`get_base_tile` → `dfs_smem_tile`（产出已排序的合法 tile）→ 对每个 tile 做 `_expand_reduce_axis` + `assign_block_size`，累积到 `topk` 个 hint 就返回。注意第 86-89 行处理多输出节点的情况。

DFS 搜索 + 优先队列排序是 roller 的算法核心：

[carver/roller/policy/default.py:96-134](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/policy/default.py#L96-L134) —— `dfs_smem_tile`。第 97-107 行构造每个轴的候选步长（因子 + 几个 2 的幂）；第 108-122 行用 `PriorityQueue` 按 `prio` 升序扩展 tile；第 123 行 `len(visited_tiles) > 2000` 是防爆栈；第 132-134 行最后再按 `prio` 全排序返回。

[carver/roller/policy/default.py:111-112](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/policy/default.py#L111-L112) —— 打分函数 `(traffic+1)*num_wave`。`+1` 是为了避免 traffic=0 时打分退化（极端情况下 traffic 可能为 0，此时退化为纯按波数比较）。

候选合法性评估，集中体现了 arch 约束如何筛选 tile：

[carver/roller/policy/default.py:537-574](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/policy/default.py#L537-L574) —— `compute_tile_dict`。第 558-560 行：`smem_cost > smem_cap` 判失效；第 564-567 行：寄存器占用超 `reg_cap` 判失效；第 568-573 行：算 `block_per_SM`（受 shared memory、寄存器、`sm_partition` 三者约束取 min）和 `num_wave`。这一段是「硬件约束→tile 去留」的集中体现。

bestfit 分配器估算 shared memory 峰值：

[carver/roller/bestfit.py:22-63](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/bestfit.py#L22-L63) —— `BestFit` 是一个经典的 best-fit 内存分配器：`malloc` 在所有空闲块里找「最小的、放得下的」（第 32 行 `found.size() > block.size()` 取最小），减少碎片；`free` 会合并相邻空闲块（第 56-62 行）。policy 在 `_compute_shared_memory_usage` 里（[default.py:469-495](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/policy/default.py#L469-L495)）按拓扑序对每个 node 的 shared buffer 做 `malloc`/`free`，最后 `allocator.limit` 就是这份 tile 配置的 shared memory 峰值占用——考虑了「先分配的 buffer 用完即释放、可与后分配的复用同一段」的真实复用情况，比简单求和更准。

张量核策略的分流入口：

[carver/roller/policy/tensorcore.py:17-53](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/policy/tensorcore.py#L17-L53) —— `TensorCorePolicy` 继承 `DefaultPolicy`，在 `_init_with_prim_func` 后多调一个 `_legalize_info`：从 node 的 tag 里读 `pipeline_stage`/`use_async_copy`/`block_reduction_depth`，若 tag 没给则按 arch 默认（sm_80/sm_90 默认 `pipeline_stage=2`、`use_async_copy=True`）。这些 tag 是上一步 `get_tensorized_func_and_tags` 打上的。

张量核路径的 block 分配，约束完全不同：

[carver/roller/policy/tensorcore.py:262-340](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/policy/tensorcore.py#L262-L340) —— `_assign_block_size`（张量核版）。第 266-267 行要求 `block_size % warp_size == 0`；第 272-275 行取出 MMA 形状（如 `[16,16]`）作为 warp tile 的下界；第 277-283 行检查 tile 能否被 warp 整分；第 309-316 行填入 `use_tc=True`、`warp=warp_tile`、`pipeline_stage`、`intrin_info` 等字段。产出的 hint 与 `DefaultPolicy` 的（带 `thread` 而非 `warp`）字段结构不同。

「能否走张量核」的判定在 matmul_analysis 里，是 roller 之前的预处理：

[carver/carver/matmul_analysis.py:513-633](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/matmul_analysis.py#L513-L633) —— `get_tensorized_func_and_tags`。它做三件事：(1) 检查 func 是否只有一个 reduction block 且可张量化（第 530、536-539 行）；(2) 检查 target 是否为 CUDA 张量核（sm≥70）或 RDNA WMMA，以及 dtype 是否被支持（第 546-648 行）；(3) 若通过，把 func 规约成标准 `C[S,I,J]+=A[S,I,K]*B[S,J,K]` 形式（`normalize_to_matmul`），并打上 `tensorcore_config`（M/N 轴位置）、`pipeline_stage`、`use_async_copy`、`intrin_info` 等 tag。这些 tag 正是 `TensorCorePolicy._legalize_info` 读取的来源。

栅格化建议（L2 优化）：

[carver/roller/rasterization.py:50-90](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/rasterization.py#L50-L90) —— `Rasterization2DColumn` 给出一小段 CUDA `__device__` 函数（`rasterization2DColumn`），让 blockIdx 按列优先的 panel 顺序遍历，提升 L2 命中。`plan_rasterization`（[tensorcore.py:342-362](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/policy/tensorcore.py#L342-L362)）只在「单 node、sm<80、总输入小于 L2」全部不满足时才建议栅格化——即多 node、Ampere+、且输入放不进 L2 的大算子才会用。这与讲义 u3-l4 讲的 `T.use_swizzle` 是同一类 L2 优化思想。

最后，hint 的字段定义：

[carver/roller/hint.py:154-222](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/hint.py#L154-L222) —— `Hint` 类。核心字段：`block`（block tile 形状）、`thread`（CUDA Core 路径的线程切分）或 `warp`（张量核路径的 warp 切分）、`rstep`（K 维步进）、`reduce_thread`、`use_tc`、`pipeline_stage`、`rasterization_plan`、`vectorize`、`intrin_info`、`split_k_factor`。`to_dict`（第 193-222 行）按需省略默认值字段，这就是 README 里那种精简字典输出的来源。

#### 4.4.4 代码实践（可运行型）

这是本讲的核心实践：用 `MatmulTemplate` + `CUDA` 生成 GEMM 的 tile hint，打印推荐配置，并任选一个在 tilelang 里验证正确性。**本实践需要一块 NVIDIA GPU（sm≥70 才会走张量核路径）。**

1. **实践目标**：端到端跑通「Carver 出 hint → 翻译成 tilelang 参数 → 编译验证正确性」。

2. **操作步骤**：

   步骤 a——生成 hint（参考 [examples/gemm/example_gemm_autotune.py:48-77](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm_autotune.py#L48-L77) 的写法）：

   ```python
   # 示例代码
   import torch
   from tilelang.carver.template import MatmulTemplate
   from tilelang.carver.arch import CUDA

   M = N = K = 1024
   arch = CUDA("cuda")
   tpl = MatmulTemplate(
       M=M, N=N, K=K,
       in_dtype="float16", out_dtype="float16", accum_dtype="float32",
   ).with_arch(arch)

   hints = tpl.recommend_hints(topk=10)
   for h in hints:
       print(h.to_dict())
   ```

   步骤 b——把第一个 hint 翻译成 tilelang 的 `T.Kernel` 参数，并验证正确性：

   ```python
   # 示例代码
   import tilelang as tl
   import tilelang.language as T

   h = hints[0]
   block_M, block_N = h.block
   block_K = h.rstep[0]
   warp_M, warp_N = h.warp
   thread_num = (block_M // warp_M) * (block_N // warp_N) * 32
   num_stages = h.pipeline_stage if h.pipeline_stage > 1 else 0

   @tl.jit
   def matmul_kernel(A: T.Tensor((M, K), "float16"),
                     B: T.Tensor((N, K), "float16"),
                     C: T.Tensor((M, N), "float16")):
        with T.Kernel(T.ceildiv(M, block_M), T.ceildiv(N, block_N), threads=thread_num) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), "float16")
            B_shared = T.alloc_shared((block_N, block_K), "float16")
            C_frag   = T.alloc_fragment((block_M, block_N), "float32")
            T.clear(C_frag)
            for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=num_stages):
                T.copy(A[bx*block_M:(bx+1)*block_M, k*block_K:(k+1)*block_K], A_shared)
                T.copy(B[by*block_N:(by+1)*block_N, k*block_K:(k+1)*block_K], B_shared)
                T.gemm(A_shared, B_shared, C_frag, transpose_B=True)
            T.copy(C_frag, C[bx*block_M:(bx+1)*block_M, by*block_N:(by+1)*block_N])

   kernel = matmul_kernel.compile()
   A = torch.randn(M, K, dtype=torch.float16).cuda()
   B = torch.randn(N, K, dtype=torch.float16).cuda()
   C = torch.empty(M, N, dtype=torch.float16).cuda()
   kernel(A, B, C)
   ref = (A @ B.T).to(torch.float16)
   print("max abs diff:", (C - ref).abs().max().item())
   ```

3. **需要观察的现象**：
   - 步骤 a 打印的字典里，张量核路径会出现 `'use_tc': True`、`'warp': [...]`、`'pipeline_stage': 2`（sm_80/90）；纯 CPU 或不支持张量核的 dtype 则只有 `'thread': [...]`、没有 `use_tc`。
   - 步骤 b 的 `max abs diff` 应在 fp16 数量级（约 1e-2 ~ 1e-1）。

4. **预期结果**：正确性校验通过（误差在 fp16 容忍范围内），证明 Carver 推荐的 tile 配置能产出正确 kernel。

5. **若无可用的 NVIDIA GPU**：步骤 a 可改用 `GeneralReductionTemplate(structure="SSR", shape=[...])` 在 CPU 上观察 `DefaultPolicy` 的输出（但 `CUDA` 构造本身需要 CUDA 设备，需换 `get_arch("llvm")` 对应的 `CPU` 架构——**待本地验证** CPU 路径是否产出非空 hint）。

#### 4.4.5 小练习与答案

**练习 1**：roller 的打分函数是 `(traffic+1)*num_wave`，为什么不是单纯最小化 traffic？

**答案**：因为一个访存极少但波数极大的 tile（比如 block 极小、grid 极大）会在 SM 上拖很多波，末尾波的 SM 大量闲置，实际并不快。乘以 `num_wave` 把「并行度不足」的惩罚显式纳入打分，避免选出「访存最优但占不满 SM」的配置。

**练习 2**：`BestFit` 分配器在 `_compute_shared_memory_usage` 里为什么按拓扑序 `malloc` 然后 `free`，而不是简单地把所有 buffer 大小加起来？

**答案**：因为 shared memory 里「生命周期不重叠的 buffer 可以复用同一段」（比如 A_shared 算完释放后，C_shared 可以占它的位置）。简单求和会高估占用，导致本可放下的 tile 被误判为超 `smem_cap`。best-fit 按拓扑序分配/释放，得到的 `allocator.limit` 才是考虑复用后的真实峰值——和 tilelang 的 `merge_shared_memory_allocations` Pass（讲义 u2-l2）是同一思想。

**练习 3**：同一个 GEMM，`MatmulTemplate` 在 sm_90 上走 `TensorCorePolicy`、在 sm_75 上走 `TensorCorePolicy`、在一块「无张量核」的设备上走 `DefaultPolicy`，三者产出的 hint 字段有什么区别？

**答案**：前两者都带 `use_tc=True`、`warp=[...]`、`pipeline_stage`（sm_90 默认 2，含 `use_async_copy`）和 `intrin_info`；后者没有这些字段，改用 `thread=[...]` 描述线程切分。即使都走张量核，sm_90 的 hint 还会带 `pipeline_stage=2` 而 sm_75 可能是 1（`_legalize_info` 里只有 sm_80/90 才默认开 2 级流水线，[tensorcore.py:36-40](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/policy/tensorcore.py#L36-L40)）。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一个小任务：**对比「Carver 推荐」与「手工笛卡尔积」两组配置空间的规模与质量**。

任务背景：[examples/gemm/example_gemm_autotune.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm_autotune.py) 的 `get_configs(M, N, K, with_roller=...)` 提供了两种生成配置空间的方式——`with_roller=True` 调 Carver，`with_roller=False` 用固定笛卡尔积（[example_gemm_autotune.py:78-90](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm_autotune.py#L78-L90)，默认 288 个候选）。

操作步骤（**待本地验证**，需要 GPU）：

1. 对同一个 GEMM（如 `M=N=K=4096`），分别调用：
   ```python
   configs_roller = get_configs(4096, 4096, 4096, with_roller=True, topk=20)
   configs_grid   = get_configs(4096, 4096, 4096, with_roller=False)
   print(len(configs_roller), len(configs_grid))
   ```
2. 观察两组的规模差异（Carver 通常 ≤20 个，笛卡尔积 288 个）。
3. 用 u8-l1 的 Autotuner 分别对两组配置跑调优（`AutoTuner.from_kernel(...).set_compile_args(...).set_profile_args(...).run()`），记录两组的最优 latency。
4. 对比「Carver 这 20 个里是否包含/接近全局最优」。

需要观察的现象：Carver 的少量候选往往能逼近甚至超过 288 个笛卡尔积里搜到的最优——因为它用硬件模型预先剔除了大量「shared memory 放不下 / 占不满 SM / 不被张量核支持」的废配置。这就是 Carver 相对暴力搜索的核心价值：**用静态分析换搜索空间压缩**。

预期结果：理解 Carver 与 Autotuner 的分工——Carver 负责「缩小搜索空间」，Autotuner 负责「在空间内实测取优」。二者组合（Carver 出 hint → Autotuner 实测）是 tilelang 推荐的调优姿势。

## 6. 本讲小结

- **Carver 是「不跑 kernel 的 tile 推荐器」**：纯靠硬件模型 + 启发式静态分析，毫秒级产出排序后的 tile 候选清单，是 Autotuner 配置空间的高质量来源。
- **三层架构 `arch / template / roller` 正交解耦**：template 产 `PrimFunc`（算子长什么样），arch 提供数值约束（硬件长什么样），roller 在约束下推 tile（该怎么切）。换算子只改 template，换硬件只改 arch。
- **template 用 TVM `te.compute` 搭等价函数**：`MatmulTemplate` 搭标准 GEMM、`GeneralReductionTemplate` 用 `SSR` 结构串描述通用规约。`recommend_hints` 是固定模板方法，子类的 `get_hardware_aware_configs` 统一委托给 `get_roller_hints_from_func`。
- **roller = policy + bestfit + rasterization**：policy（`DefaultPolicy`/`TensorCorePolicy`）枚举 tile 并按 `(traffic+1)×num_wave` 排序；bestfit 是内存分配器，估 shared memory 峰值（考虑 buffer 复用）；rasterization 给 L2 友好的 blockIdx 遍历建议。
- **张量核分流由 `get_tensorized_func_and_tags` 决定**：它识别算子是否可规约成标准 matmul、dtype 是否被硬件张量核支持；通过则走 `TensorCorePolicy`（按 warp 切分、带 `pipeline_stage`/`intrin_info`），否则退回 `DefaultPolicy`（按 thread 切分）。
- **Carver 与 Autotuner 是上下游关系**：Carver 缩小搜索空间，Autotuner 在空间内实测取优；`examples/gemm/example_gemm_autotune.py` 的 `with_roller=True` 正是二者组合的标准用法。

## 7. 下一步学习建议

- **实践 Carver → Autotuner 全链路**：精读 [examples/gemm/example_gemm_autotune.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm_autotune.py)，动手跑一次 `with_roller=True` 的调优，对比下一讲 u8-l3 的 profiler 测量结果。
- **扩展 template**：阅读 [carver/template/flashattention.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/template/flashattention.py)、[gemv.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/template/gemv.py)、[conv.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/template/conv.py)，看更复杂的算子模板如何搭等价 `PrimFunc`，尝试为自己的算子写一个 template。
- **深入 roller 算法**：精读 [carver/roller/node.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/roller/node.py)（`PrimFuncNode`/`OutputNode` 的拓扑与 `propagate_inputs`）与 [carver/analysis.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/analysis.py)（`normalize_prim_func`/`BlockInfo`），理解 roller 如何把 `PrimFunc` 拆成 node 图并传播 tile 形状。
- **联系 u3-l4 的 swizzle**：对比本讲的 `rasterization`（blockIdx 遍历顺序）与讲义 u3-l4 的 `T.use_swizzle`，理解 L2 优化在「推荐阶段」和「手写阶段」的两种入口。
- **下一讲 u8-l3**：从「推荐」走向「实测」，学习 `tilelang.profiler` 的 `do_bench` 如何用 warmup/rep 精确测延迟，把 Carver 推荐的配置真正量化成性能数字。
