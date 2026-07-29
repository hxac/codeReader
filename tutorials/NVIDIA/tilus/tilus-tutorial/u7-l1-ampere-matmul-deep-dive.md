# Ampere matmul 进阶：分块、共享内存与 MMA

## 1. 本讲目标

本讲以 `examples/matmul/` 下的 `matmul_v2.py` → `matmul_v5.py` 为进阶路线，把 u1-l5 那个「能跑但很慢」的 naive matmul，一步步优化到接近 cuBLAS 的水平。

学完后你应该能够：

- 说清楚**共享内存分块（shared memory tiling）**为什么能提速，以及它在 K 维循环里的数据搬运形态。
- 理解 Tilus 里看似普通的 `self.dot(...)` 与 `self.load_shared(...)`，是如何被**自动映射**到 Ampere 硬件的 `mma.m16n8k16` 张量核指令与 `ldmatrix` 加载指令的——用户**不需要**手写这些 PTX。
- 掌握 Ampere 的 `cp.async` 异步拷贝（v3）如何绕过寄存器，以及它如何为**软件流水线（software pipeline / double buffering）**（v4）铺路。
- 理解 `@tilus.autotune`（v2 起）如何把分块/线程/流水线级数都纳入搜索空间，以及 Split-K + 信号量聚合（v5）如何吃掉「瘦长」形状的算力。
- 具备把每一步优化的收益（TFLOPS）拆解归因的能力。

## 2. 前置知识

本讲是「专家层」，建议先具备以下认知（均来自前置讲义）：

- **Tilus Script 骨架**（u1-l3、u1-l5）：`__init__` 设编译期超参，`__call__` 写算子逻辑，`global_view`/`load_global`/`store_global`/`dot`/`cast` 构成数据流。
- **指令分层**（u2-l2）：通用指令（`RootInstructionGroup`，含 `shared_tensor`/`store_shared`/`load_shared`/`sync`）做可移植逻辑，硬件指令组做显式性能。
- **自动调优**（u2-l4）：`@tilus.autotune` 把调优子空间累积到 `_autotune_space`，`span_space` 笛卡尔展开成一份份 schedule，首次调用时并行 benchmark 选优、落盘 dispatch 表。
- **布局系统**（u4 全单元）：四种张量对应四层内存；`RegisterLayout` 的 mode/spatial/local 决定元素如何分配给线程；**布局自动推理**（u4-l5）会为 `dot` 凭 `resolve_dot_config` 生成 MMA 原子布局、为访存生成相容布局。
- **后端发射器**（u6 全单元）：发射器（emitter）把单条 Tilus 指令翻译成 Hidet IR；通用发射器（u6-l4）中 `shared_ldst` 在布局能被 `ldmatrix` 原子布局整除时走 PTX 矩阵搬运快路径。

一句话回顾 **naive matmul（v0）的瓶颈**：v0 直接 `load_global` 把 A、B 的 tile 读进寄存器就做 `dot`，全程在 DRAM 与寄存器之间搬运，既不复用共享内存、也不用张量核流水线，因此 TFLOPS 远低于硬件峰值。本讲就是把这两块短板一块块补上。

> 关于硬件：本讲面向 **Ampere（sm_80）**。Ampere 的关键能力是：第三代 Tensor Core（`mma.m16n8k16` 等指令）、`cp.async` 异步全局→共享拷贝、`ldmatrix` 矩阵加载。Hopper/Blackwell 的 `wgmma`/`tma`/`tcgen05` 留到 u7-l2、u7-l3。

## 3. 本讲源码地图

本讲围绕四个真实示例文件展开，外加两个用于解释「自动生成 MMA/ldmatrix」的后端文件：

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| [examples/matmul/matmul_v2.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v2.py) | 共享内存分块 + `@autotune` | 引入分块、自动调优（4.1） |
| [examples/matmul/matmul_v3.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v3.py) | `cp.async` 异步拷贝 | 绕过寄存器搬运（4.3 上半） |
| [examples/matmul/matmul_v4.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v4.py) | 软件流水线（多级缓冲） | 计算与搬运 overlap（4.3 下半） |
| [examples/matmul/matmul_v5.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v5.py) | Split-K + 信号量聚合 | 吃瘦长形状算力（4.4） |
| python/tilus/backends/emitters/cuda/mma_dot.py | `DotInst` → Ampere MMA 发射器 | 解释 dot 如何变 mma（4.2） |
| python/tilus/lang/modules/cuda.py | `resolve_dot_config` / 原子 MMA 配置表 | 解释 dot 如何变 mma（4.2） |

> 说明：4.2 节会引用 `mma_dot.py`、`cuda.py`、`ir/layout/cuda/ldmatrix.py`、`lang/instructions/root.py` 等文件。这些是**支撑自动生成的底层**，本讲只精读其中「映射」相关的关键片段，深入实现请回到 u6 单元。

## 4. 核心概念与源码讲解

### 4.1 共享内存分块与自动调优（v2）

#### 4.1.1 概念说明

矩阵乘 \(C = A \times B\) 中，A 的一行要和 B 的所有列相乘，B 的一列要和 A 的所有行相乘。naive 做法里，每个元素从 DRAM 读多次。**共享内存分块（tiling）**的核心思想是：把 A、B 切成小块搬进片上共享内存（SRAM），让一个线程块在自己的 SRAM 里反复读这些块，从而把多次 DRAM 访问压缩成一次 DRAM + 多次 SRAM。

之所以在 **K 维**上做循环分块，是因为矩阵乘天然沿 K 维做规约：

\[
C_{i,j} = \sum_{k=0}^{K-1} A_{i,k} \cdot B_{k,j}
\]

把 K 维切成大小为 `block_k` 的小段后，每段只需把 `A[block_m, block_k]` 和 `B[block_k, block_n]` 读进共享内存一次，就能在 SRAM 里完成一次 `block_m×block_n×block_k` 的部分乘加，并累加进寄存器里的 `acc`。分块后每个元素从 DRAM 读取的次数从 \(O(K)\) 降到接近 1。

v2 在 v1（首次引入共享内存）的基础上做了一件关键的事：**自动调优**。分块尺寸 `block_m/block_n/block_k` 与 `num_warps` 没有唯一最优值——它们依赖具体形状、寄存器/共享内存占用与 SM 占用率。v2 用 `@tilus.autotune` 把这些参数声明成搜索空间，让 Tilus 自己 benchmark 选最快的。

#### 4.1.2 核心流程

v2 单个线程块的工作循环（每段 K）：

```
for offset_k in range(0, k_size, block_k):
    lda = load_global(ga, [offset_m, offset_k], [block_m, block_k])  # DRAM → 寄存器
    store_shared(sa, lda)                                             # 寄存器 → SRAM
    ldb = load_global(gb, [offset_k, offset_n], [block_k, block_n])
    store_shared(sb, ldb)
    sync()                                                            # 等所有线程写完 SRAM
    a  = load_shared(sa)                                              # SRAM → 寄存器（→ ldmatrix）
    b  = load_shared(sb)
    acc = dot(a, b, acc)                                              # 寄存器乘加（→ mma）
    sync()                                                            # 等读完后才能下一轮写
```

注意这里有**两次 `sync`**：第一次保证「写共享内存」全部完成后才能读；第二次保证「读共享内存」全部完成后，下一轮才允许覆盖写。同一块共享内存被读写交替复用，两次栅栏不可省略。

v2 的数据搬运路径是 **DRAM → 寄存器(lda) → SRAM(sa) → 寄存器(a)**——中间还借了一层寄存器。这层寄存器在 v3 会被 `cp.async` 干掉（见 4.3）。

自动调优流程（回顾 u2-l4）：`@autotune` 把每个参数的候选值累积到 `_autotune_space`，`span_space` 做笛卡尔积展开成多份 schedule，**首次调用** `matmul(...)` 时并行编译并 benchmark 每份，选最优落盘。

#### 4.1.3 源码精读

**搜索空间声明**——三个 `@tilus.autotune` 各定义一组候选，最终笛卡尔积共 \(2 \times 3 \times 2 = 12\) 份 schedule：

```python
# matmul_v2.py:55-57 —— 声明 num_warps / block_m,block_n / block_k 的候选
@tilus.autotune("num_warps", [4, 8])
@tilus.autotune("block_m, block_n", [(128, 128), (128, 64), (64, 128)])
@tilus.autotune("block_k", [16, 32])
class MatmulV2(tilus.Script):
```

[matmul_v2.py:L55-L57](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v2.py#L55-L57) 声明调优子空间；`"block_m, block_n"` 写在同一行表示二者**绑定**（只取给定的组合），而不是各自独立展开。

**`__init__` 接收被填好的超参**（由 `generate_schedules` 在实例化时绑定）：

```python
# matmul_v2.py:59-70
def __init__(self, num_warps, block_m, block_n, block_k):
    super().__init__()
    self.num_warps = num_warps
    self.block_m = block_m; self.block_n = block_n; self.block_k = block_k
```

**共享内存分块的核心循环**（含两次 `sync`）：

```python
# matmul_v2.py:98-116
for offset_k in range(0, k_size, self.block_k):
    lda = self.load_global(ga, offsets=[offset_m, offset_k], shape=[self.block_m, self.block_k])
    self.store_shared(sa, lda)
    ldb = self.load_global(gb, offsets=[offset_k, offset_n], shape=[self.block_k, self.block_n])
    self.store_shared(sb, ldb)
    self.sync()                                   # 写完才能读
    a = self.load_shared(sa)
    b = self.load_shared(sb)
    acc = self.dot(a, b, acc)                      # 等价于 out = a @ b + acc
    self.sync()                                   # 读完才能下一轮写
```

[matmul_v2.py:L98-L116](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v2.py#L98-L116) 是共享内存分块的标准模板。其中 `sa`/`sb` 由 `shared_tensor` 分配、`acc` 由 `register_tensor` 创建（见 [matmul_v2.py:L92-L96](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v2.py#L92-L96)），循环结束后 `free_shared` 释放共享内存（[matmul_v2.py:L118-L119](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v2.py#L118-L119)）。

> 小细节：v2 用 `acc = self.dot(a, b, acc)`（重新赋值），v3 起改成 `self.dot(a, b, acc, out=acc)`（原地累加）。两者语义相同（都是 `out = a@b + acc`），`out=` 写法更显式地表达「累加回原寄存器」。

#### 4.1.4 代码实践

**实践目标**：体会分块尺寸与 autotune 对性能的影响。

**操作步骤**：

1. 设一个临时缓存目录，便于观察生成的 schedule 与 `source.cu`：
   ```python
   import tilus
   tilus.option.cache_dir("/tmp/tilus-v2-cache")
   ```
2. 运行 `python examples/matmul/matmul_v2.py`，它会对 `[4096,4096,4096]` 自动调优并打印 TFLOPS。
3. 进入缓存目录，找到 `schedule.txt` / `dispatch_table.txt`，查看 Tilus 最终选中的 `block_m/block_n/block_k/num_warps`。
4. （可选）用 `debug_schedule=dict(block_m=128, block_n=128, block_k=32, num_warps=8)` 把整个搜索空间钉成单点，对比「手工猜的配置」与「autotune 选出的配置」的 TFLOPS 差距。

**需要观察的现象**：autotune 首次调用较慢（要编译并 benchmark 多份 schedule）；第二次调用直接命中 dispatch 缓存而飞快。不同 `block_k`（16 vs 32）下 TFLOPS 有明显差异。

**预期结果**：v2 的 TFLOPS 显著高于 v0/v1（因为 autotune 选了好分块），但仍明显低于 v3/v4（因为还没用上异步拷贝与流水线）。**具体数值待本地验证**（取决于 GPU 型号，需 sm_80+）。

#### 4.1.5 小练习与答案

**练习 1**：v2 的 K 循环里为什么需要两次 `self.sync()`，能不能只留一次？

**参考答案**：不能。第一次 `sync` 保证「写共享内存」对所有线程可见后才开始读（生产者→消费者定序）；第二次 `sync` 保证「读共享内存」全部完成后才允许下一轮覆盖写（否则快线程先写、慢线程还没读完，数据被冲掉）。两次栅栏分别守护写→读与读→写两个方向。

**练习 2**：`@autotune("block_m, block_n", [(128,128),(128,64),(64,128)])` 与把 `block_m`、`block_n` 拆成两个独立 `@autotune` 有什么区别？

**参考答案**：写在一行里，候选是**绑定的元组**，只产生 3 种 `(block_m,block_n)` 组合；拆开则各自独立笛卡尔展开，会产生 \(3 \times 3 = 9\) 种组合（包括 `(128,64)` 和 `(64,128)` 之外的如 `(64,64)`）。绑定写法用于表达「只有这几个经验上有效的组合」，能大幅缩小搜索空间、加快 autotune。

---

### 4.2 MMA 张量核与 ldmatrix 的自动生成

> 本节是理解 Tilus「为何好写又跑得快」的关键，也是本讲最容易误解的地方：**v2/v3 的 Python 源码里并没有出现 `mma` 或 `ldmatrix` 字样**——它们是 `dot` 与 `load_shared` 被自动映射出来的。

#### 4.2.1 概念说明

Ampere 的 `mma.m16n8k16` 是一条**张量核（Tensor Core）指令**：它一次性算出一个 \(16\times 8\) 的输出块，用 \(16\times 16\) 的 A 块和 \(16\times 8\) 的 B 块做乘加，即 \(C_{16\times 8} \mathrel{+}= A_{16\times 16} \cdot B_{16\times 8}\)。相比标量 FMA，张量核在一个时钟周期内完成原本需要上百条标量指令的乘加，是 matmul 性能的来源。

`ldmatrix` 则是为 MMA「喂料」的专用加载指令：它把共享内存里一段排布的数据，按 MMA 期望的寄存器布局，每个线程一次加载 16 字节，直接放进寄存器。它省去了「逐元素标量加载 + 重排」的开销。

关键认知：**用户在 Tilus 里写的 `self.dot(a, b, acc)` 与 `self.load_shared(sa)` 是块级语义指令；是否用 MMA / ldmatrix 由布局系统与发射器在编译期决定**：

- 布局自动推理（u4-l5）的 `MmaDotRule` 会凭 `resolve_dot_config` 给 `a/b/acc` 生成与 Ampere MMA 原子配置相容的 `RegisterLayout`。
- 后端发射器（u6）按指令类型派单：`DotInst` 在 sm_80 上派给 MMA 发射器，`load_shared` 在布局可整除时派给 ldmatrix 快路径（u6-l4）。

所以「`dot` + `load_shared`」这对组合，在 Ampere 上自然落地成 `mma + ldmatrix`，而用户代码保持与架构无关。

#### 4.2.2 核心流程

`dot` 到 MMA 的映射链：

```
用户 self.dot(a, b, acc, out=acc)
  → Transpiler 生成 DotInst（output=d, inputs=(a,b,c)）       # u3-l2
  → 布局推理 MmaDotRule 给 a/b/c/d 配 MMA 相容布局             # u4-l5
  → 后端按 target 派单：DotInst @ nvgpu_sm70 → DotInstEmitter  # 本节
  → resolve_mma_config 选出原子 MMA 配置（如 m16n8k16_f16_f32）
  → 用 mma_sync_v2 逐原子块发射 PTX mma.m16n8k16
```

一个 `block_m×block_n` 的 tile，每个 K 段需要的原子 MMA 条数：

\[
N_{\text{mma}} = \frac{\text{block\_m}}{16} \cdot \frac{\text{block\_n}}{8} \cdot \frac{\text{block\_k}}{16}
\]

例如 `block_m=128, block_n=128, block_k=32`：\(8 \times 16 \times 2 = 256\) 条 `mma.m16n8k16` 每个 K 段。这也是为什么分块尺寸通常取 16/8 的整数倍——否则布局无法被原子 MMA 整除，`resolve_mma_config` 会失败。

#### 4.2.3 源码精读

**`DotInst` 派单到 Ampere MMA 发射器**——注册目标是 `nvgpu_sm70`（覆盖 sm_70 及以上，含 Ampere sm_80）：

```python
# python/tilus/backends/emitters/cuda/mma_dot.py:27-28
@register_emitter(DotInst, target=nvgpu_sm70)
class DotInstEmitter(BaseInstEmitter):
```

[mma_dot.py:L27-L28](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cuda/mma_dot.py#L27-L28) 说明：同一条 `DotInst` 在不同 target 下会挂不同发射器（Hopper 挂 wgmma，Blackwell 挂 tcgen05），这里 sm_70+ 用经典 MMA。

**配置选择——靠布局整除性匹配**：`resolve_mma_config` 遍历所有原子 MMA 配置，用「张量布局 ÷ 原子布局」是否整除来判定可用性：

```python
# mma_dot.py:34-49（节选）
for config in AtomicMmaConfig.all_configs().values():
    if a.dtype != config.operand_type or c.dtype != config.acc_type:
        continue                                       # dtype 不匹配跳过
    try:
        outers = [p / q for p, q in zip(
            [a.layout, b.layout, c.layout, d.layout],
            [config.la, config.lb, config.lc, config.lc])]
    except LayoutOperationError:
        continue                                       # 布局不能整除跳过
    return config, tuple(outers)                       # 命中第一个可用配置
```

[mma_dot.py:L29-L49](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cuda/mma_dot.py#L29-L49) 是「布局决定能否用 MMA」的判定核心。`emit` 入口随后用 `mma_sync_v2` 逐原子块发射（[mma_dot.py:L60-L65](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cuda/mma_dot.py#L60-L65)，`from tilus.hidet.ir.primitives.cuda.mma import mma_sync_v2`）。

**fp16 输入 + fp32 累加的配置表**：

```python
# python/tilus/lang/modules/cuda.py:51-52, 60-65（节选）
m16n8k16_f16_f32: AtomicMmaConfig = AtomicMmaConfig.m16n8k16_f16_f32()
...
table = {
    (float16, float32): cuda.atomic_mma_configs.m16n8k16_f16_f32,   # v2~v5 走这一条
    (int8, int32):      cuda.atomic_mma_configs.m16n8k32_i8_i32,
}
```

[cuda.py:L51-L65](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/modules/cuda.py#L51-L65) 说明 v2~v5 里 `acc` 是 `float32`、`a/b` 是 `float16`，正好命中 `m16n8k16_f16_f32`。

**ldmatrix 的原子布局**（`load_shared` 的快路径依据，u6-l4）：

```python
# python/tilus/ir/layout/cuda/ldmatrix.py:24-38（节选）
@dataclass(frozen=True, eq=False)
class LoadMatrixConfig:
    nbytes: int; trans: bool; ldmatrix_layout: RegisterLayout
    @staticmethod
    @functools.cache
    def all() -> tuple[LoadMatrixConfig, ...]:
        return (
            LoadMatrixConfig(1, False, spatial(8, 4).local(1, 4)),
            LoadMatrixConfig(2, False, spatial(8, 4).local(1, 2)),
            LoadMatrixConfig(4, False, spatial(8, 4)),
            LoadMatrixConfig(2, True, column_spatial(4, 8).local(2, 1)),
        )
```

[ldmatrix.py:L24-L38](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/cuda/ldmatrix.py#L24-L38) 定义了几种 ldmatrix 的原子布局（按每元素字节数 1/2/4 与是否转置区分）。当共享内存里某张量的 `SharedLayout` 能被这些原子布局整除、且 16 字节对齐连续时，`shared_ldst` 发射器就发 `ldmatrix`；否则回退到向量化标量搬运（见 u6-l4）。

#### 4.2.4 代码实践

**实践目标**：亲眼确认 `dot`/`load_shared` 确实被编译成了 `mma`/`ldmatrix`。

**操作步骤**：

1. 删掉缓存目录（`rm -rf /tmp/tilus-v2-cache`）后运行 v2。
2. 在缓存里找到生成的 `source.cu`（路径形如 `programs/<hash>/.../source.cu`）。
3. 在 `source.cu` 中搜索 PTX 内联：`mma`、`ldmatrix`、`cp.async`（v3 才有）。

**需要观察的现象**：你会看到 `mma.sync.aligned.m16n8k16.row.col.f32.f16.f16.f32`（或其 `.asm` 包装）与 `ldmatrix.sync.aligned.m8n8.x4.shared.b16` 这类 PTX 指令，而 Python 源码里完全没有这些字样。

**预期结果**：确认「用户写块级 `dot`/`load_shared`，编译器自动生成 Ampere MMA/ldmatrix」。若某次你把 `block_k` 改成非 16 倍数，可能看到 ldmatrix 回退为普通加载甚至编译失败——这验证了「布局整除性」是快路径的前提。**具体 PTX 文本待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `acc` 的 dtype 从 `float32` 改成 `float16`，`resolve_dot_config` 会选哪条配置？精度会有什么变化？

**参考答案**：会从 `m16n8k16_f16_f32` 改选 `m16n8k16_f16_f16`（见 [cuda.py:L60-L65](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/modules/cuda.py#L60-L65) 的 `(float16,float16)` 分支）。累加在 fp16 下进行，K 较大时溢出与舍入误差会显著放大，数值精度下降，但寄存器占用减半、可能换来更高吞吐。

**练习 2**：为什么 `resolve_mma_config` 用「布局除法」而不是「形状匹配」来选配置？

**参考答案**：MMA 不仅要求张量形状是原子形状的整数倍，还要求**寄存器内的元素排布（RegisterLayout）**恰好是原子布局的外层复制（spatial/local 的拼接）。形状匹配只能保证数量对，布局除法（`p / q`，见 [mma_dot.py:L39-L47](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cuda/mma_dot.py#L39-L47)）才能保证「线程持有的每个元素正好落在 MMA 输入操作数的位置上」。

---

### 4.3 cp.async 异步拷贝（v3）与软件流水线（v4）

#### 4.3.1 概念说明

v2 的搬运路径 `DRAM → 寄存器(lda) → SRAM(sa)` 有两个浪费：① 数据经过寄存器「过一手」纯属中转，白白占用寄存器；② `load_global` + `store_shared` 是**同步**的，线程要等数据到齐。

Ampere 引入的 `cp.async` 硬件指令解决了这两点：它让**拷贝引擎**直接把数据从 DRAM 搬进 SRAM，**不经过寄存器**，而且**异步**——线程发完指令可以继续干别的活，之后再回来等。这就是 v3 的核心改动。`copy_async` 是 Tilus 对 `cp.async` 的块级封装（见 [root.py:L744-L798](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L744-L798)，文档明确写「Issues an `cp.async` transfer」「Requires compute capability 8.0+」）。

但仅有异步拷贝还不够。v3 的循环仍是「搬一段 → 等完 → 算一段 → 等完」的串行节奏，计算单元和访存单元无法同时工作。**软件流水线（software pipeline）**——v4 的核心——把搬运与计算**重叠**起来：在算第 \(i\) 段的同时，预先搬运第 \(i+1\) 段（甚至更后）的数据。其代价是需要多份共享内存缓冲（double buffering / 多级缓冲），用环形下标轮流复用。

v4 的文档注释把动机讲得很清楚（[matmul_v4.py:L9-L33](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v4.py#L9-L33)）：matmul 寄存器/共享内存占用大，单个 SM 上能并行的线程块少，靠「多块并行」掩盖延迟不够，必须靠软件流水线在**块内**重叠访存与计算。

#### 4.3.2 核心流程

**v3：单缓冲异步拷贝**（把 v2 的「寄存器中转」换成 cp.async）：

```
for offset_k in range(0, k_size, block_k):
    copy_async(ga → sa, [offset_m, offset_k])   # DRAM → SRAM，不经寄存器
    copy_async(gb → sb, [offset_k, offset_n])
    copy_async_wait_all()                        # 等拷贝完成
    sync()                                       # 等线程间共享内存可见
    a = load_shared(sa); b = load_shared(sb)
    dot(a, b, acc, out=acc)
    sync()
```

仍是「搬→等→算」串行，但省了寄存器中转、且为流水线铺好了异步原语。

**v4：多级软件流水线**（以 `num_stages=3` 为例，3 块共享内存缓冲环形复用）：

```
# 预热（prologue）：先把前 num_stages-1 段发出去，不等
for stage in [0 .. num_stages-2]:
    copy_async(ga → sa[stage], ...)
    copy_async(gb → sb[stage], ...)
    copy_async_commit_group()              # 把这批 cp.async 打成一个组

copy_async_wait_group(n=num_stages-2)      # 最多留 num_stages-1 组未完成
sync()

# 主循环：算当前段，同时预取后面第 num_stages-1 段
for offset_k in range(0, k_size, block_k, unroll=num_stages):
    a = load_shared(sa[current]); b = load_shared(sb[current])
    dot(a, b, acc, out=acc)                 # ← 计算当前段

    preload_offset_k = offset_k + (num_stages-1)*block_k
    copy_async(ga → sa[preload], [offset_m, preload_offset_k])  # ← 同时预取
    copy_async(gb → sb[preload], [preload_offset_k, offset_n])
    copy_async_commit_group()

    current  = (current + 1) % num_stages   # 环形前进
    preload  = (preload + 1) % num_stages
    copy_async_wait_group(n=num_stages-2)   # 保留流水深度
    sync()
```

`copy_async_commit_group` / `copy_async_wait_group` 是 cp.async 的**分组**机制（PTX `cp.async.commit_group` / `cp.async.wait_group`，见 [root.py:L817-L850](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L817-L850)）：`wait_group(n=num_stages-2)` 允许最多 `num_stages-1` 组在途未完成，从而维持 `num_stages-1` 级流水深度，把访存延迟藏进计算时间里。

为什么主循环用 `self.range(..., unroll=self.num_stages)`？因为环形缓冲的正确性依赖「`preload_offset_k` 不越界」这类与迭代号相关的条件，编译期完全展开 `num_stages` 次能让标量分析（u5-l3）算清边界、省掉运行期判断。

#### 4.3.3 源码精读

**v3：异步拷贝三件套**——`copy_async` + `copy_async_wait_all` + `sync`：

```python
# matmul_v3.py:84-98
for offset_k in range(0, k_size, block_k):
    self.copy_async(src=ga, dst=sa, offsets=[offset_m, offset_k])   # 不再有 load_global/store_shared
    self.copy_async(src=gb, dst=sb, offsets=[offset_k, offset_n])
    self.copy_async_wait_all()                                       # 等所有 cp.async 完成
    self.sync()                                                      # 仍需线程栅栏保证可见
    a = self.load_shared(sa); b = self.load_shared(sb)
    self.dot(a, b, acc, out=acc)
    self.sync()
```

[matmul_v3.py:L84-L98](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v3.py#L84-L98) 对比 v2：去掉了 `load_global`+`store_shared` 的寄存器中转，DRAM 直达 SRAM。注意 `copy_async_wait_all` 只保证「拷贝完成」，**不**同步线程，所以仍要 `sync()`。

> `copy_async` 的语义在 [root.py:L753-L788](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L753-L788) 有完整文档：越界访问默认补零（`check_bounds=True`），硬件要求 sm_80+。

**v4：多级缓冲的共享内存**——`sa`/`sb` 多了一个 `num_stages` 维：

```python
# matmul_v4.py:82-83
sa = self.shared_tensor(dtype=float16, shape=[self.num_stages, block_m, block_k])
sb = self.shared_tensor(dtype=float16, shape=[self.num_stages, block_k, block_n])
```

[matmul_v4.py:L82-L83](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v4.py#L82-L83) ——`num_stages` 既是流水线级数，也是缓冲份数；共享内存占用随 `num_stages` 线性增长，所以它也被纳入 autotune（[matmul_v4.py:L51-L54](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v4.py#L51-L54) 增加了 `num_stages [3,4,5]`）。

**v4：预热 + 环形主循环**：

```python
# matmul_v4.py:86-93（prologue）
for stage in range(self.num_stages - 1):
    offset_k = stage * self.block_k
    self.copy_async(src=ga, dst=sa[stage], offsets=[offset_m, offset_k])
    self.copy_async(src=gb, dst=sb[stage], offsets=[offset_k, offset_n])
    self.copy_async_commit_group()
self.copy_async_wait_group(n=self.num_stages - 2)
self.sync()
```

[matmul_v4.py:L86-L93](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v4.py#L86-L93) 预先发出 `num_stages-1` 个拷贝组，把流水线「灌满」。

```python
# matmul_v4.py:97-121（主循环）
current_stage = 0; preload_stage = self.num_stages - 1
for offset_k in self.range(0, k_size, block_k, unroll=self.num_stages):
    a = self.load_shared(sa[current_stage]); b = self.load_shared(sb[current_stage])
    self.dot(a, b, acc, out=acc)                                      # 算当前
    preload_offset_k = offset_k + (self.num_stages - 1) * block_k
    self.copy_async(src=ga, dst=sa[preload_stage], offsets=[offset_m, preload_offset_k])  # 取后续
    self.copy_async(src=gb, dst=sb[preload_stage], offsets=[preload_offset_k, offset_n])
    self.copy_async_commit_group()
    current_stage = (current_stage + 1) % self.num_stages
    preload_stage = (preload_stage + 1) % self.num_stages
    self.copy_async_wait_group(n=self.num_stages - 2)
    self.sync()
```

[matmul_v4.py:L97-L121](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v4.py#L97-L121) 是软件流水线的标准范式：`dot`（计算）与 `copy_async`（预取）背靠背，靠环形下标 `current_stage/preload_stage` 与 `wait_group(num_stages-2)` 维持固定流水深度。

> 注意 v4 的预取在循环内**无条件**发出，所以它要求 `k_size` 能被分块整除（否则末尾会越界预取）。v5 修正了这一点（见 4.4）。

#### 4.3.4 代码实践

**实践目标**：量化「异步拷贝」与「软件流水线」各自的收益。

**操作步骤**：

1. 依次运行 `matmul_v2.py`、`matmul_v3.py`、`matmul_v4.py`（都跑 `[4096,4096,4096]`）。
2. 记录三者的 tilus TFLOPS，计算相邻版本的加速比：
   - v2→v3 加速比 = TFLOPS(v3) / TFLOPS(v2)：体现「去掉寄存器中转 + 异步」的收益。
   - v3→v4 加速比 = TFLOPS(v4) / TFLOPS(v3)：体现「软件流水线掩盖延迟」的收益。
3. 用 `ncu`（Nsight Compute）或 Tilus 的 profiler（u8-l4）看 v3 与 v4 的「计算吞吐 / 访存吞吐」占比变化。

**需要观察的现象**：v3→v4 的加速通常**大于** v2→v3（访存延迟被重叠后，计算单元才真正喂饱张量核）；v4 的 `num_stages` autotune 选中的值往往不是最大（5），因为更大 `num_stages` 占用更多共享内存、降低 SM 占用率，存在权衡。

**预期结果**：定性排序 TFLOPS(v0) ≪ v1 ≪ v2 < v3 < v4，且 v4 已接近（但仍略低于）cuBLAS/torch.matmul。**具体数值与最优 num_stages 待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：v3 里 `copy_async_wait_all()` 之后为什么还需要 `self.sync()`？

**参考答案**：`copy_async_wait_all` 只让**当前线程**等到它发出的 cp.async 完成；它不等其他线程（文档明确「does not synchronize the threads in the block」，见 [matmul_v3.py:L24-L26](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v3.py#L24-L26)）。而 `sa`/`sb` 是全块共享的，必须用 `sync()` 让块内所有线程都到达、且各自的拷贝都对彼此可见后，才能安全地 `load_shared`。

**练习 2**：v4 主循环用 `wait_group(n=num_stages-2)` 而不是 `wait_group(n=0)`，目的是什么？

**参考答案**：`n=num_stages-2` 表示「最多允许 `num_stages-1` 个拷贝组同时在途未完成」（见 [root.py:L833-L836](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L833-L836)）。这正是流水线的深度——让后续段的拷贝与当前段的计算重叠。若用 `wait_group(0)`，等于每次都等全部拷贝完成，退化为串行，流水线失效。

**练习 3**：为什么把 `num_stages` 也放进 `@autotune`，而不是固定取最大值 5？

**参考答案**：`num_stages` 越大，`sa`/`sb` 占用的共享内存越多（线性增长），SM 上能并发的线程块（占用率）随之下降。更深流水（掩盖延迟）与更低占用率（掩盖延迟的另一手段）是一对矛盾，最优值随形状/架构变化，所以交给 autotune 搜索。

---

### 4.4 Split-K 与信号量聚合（v5）

#### 4.4.1 概念说明

到 v4 为止，一个输出 tile \(C_{\text{block\_m}\times\text{block\_n}}\) 由**一个线程块**算完。当矩阵「瘦长」（m、n 小，k 很大）时，输出 tile 数量少，网格里的线程块数少，**填不满 GPU 的 SM**，大量算力闲置。

**Split-K** 的思路：沿 K 维再切一刀，把一个 tile 的计算分给**多个线程块**并行做，每个块只算 K 的一段（部分和），最后把同一 tile 的所有部分和**聚合**成最终结果。这样网格 z 维变多、线程块变多，SM 被填满。代价是要解决「多个块往同一块 C tile 写」的竞争——v5 用**信号量（semaphore）**串行化聚合。

聚合有两种实现：① 用单独的 reduction kernel；② 在同一个 kernel 里用信号量做（v5 选这种）。每个 C tile 配一个全局信号量，同一 tile 的多个块按 `blockIdx.z` 排队：块 0 直接写入；块 \(i>0\) 等信号量到 \(i\)、读出已有结果、加上自己的部分和、写回、释放信号量为 \(i+1\)；最后一块释放为 0 以满足 `requires_clean`。

#### 4.4.2 核心流程

v5 的网格变成三维：`(cdiv(M,block_m), cdiv(N,block_n), split_k_factor)`，z 维是 Split-K 因子。每个块的 K 区间：

```
block_k_size = cdiv(cdiv(k_size, split_k_factor), block_k) * block_k   # 对齐到 block_k
start_offset_k = blockIdx.z * block_k_size
end_offset_k   = min(start_offset_k + block_k_size, k_size)
# 在 [start_offset_k, end_offset_k) 上跑 v4 的流水线循环（带越界保护）
```

聚合阶段（写回 C）：

```
把 acc cast 成 fp16，经共享内存中转（store_shared sc; sync; load_shared rc）改布局
gc = global_view(c_ptr, [m_size, n_size])
if blockIdx.z > 0:                          # 非首块：先取已有部分和
    lock_semaphore(sem, value=blockIdx.z)
    partial = load_global(gc, [offset_m, offset_n], [block_m, block_n])
    add(rc, partial, out=rc)
store_global(gc, rc, [offset_m, offset_n])  # 写回聚合结果
sync()                                      # 保证 store 完成
release_semaphore(sem, value=(blockIdx.z+1) % split_k_factor)  # 放行下一块
```

> 注意：v5 的 autotune 候选含 `split_k_factor ∈ {1,4,12,16}`（[matmul_v5.py:L57](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v5.py#L57)）。`split_k_factor=1` 时退化为「无切分」，块 0 直接写、释放为 `(0+1)%1=0`，信号量路径仍执行但无害。

#### 4.4.3 源码精读

**三维网格 + Split-K 区间**：

```python
# matmul_v5.py:85-97
self.attrs.blocks = [cdiv(m_size, self.block_m), cdiv(n_size, self.block_n), self.split_k_factor]
...
block_k_size = cdiv(cdiv(k_size, self.split_k_factor), self.block_k) * self.block_k
start_offset_k = self.blockIdx.z * block_k_size
end_offset_k = min(start_offset_k + block_k_size, k_size)
```

[matmul_v5.py:L85-L97](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v5.py#L85-L97) 把 z 维加进网格，并把每个块的 K 范围对齐到 `block_k`（外层 `cdiv` 保证覆盖，内层 `*block_k` 保证整除对齐）。

**带越界保护的预取**——修正了 v4 的无条件预取：

```python
# matmul_v5.py:120-147（主循环核心）
for offset_k in self.range(start_offset_k, end_offset_k, block_k, unroll=self.num_stages):
    a = self.load_shared(sa[current_stage]); b = self.load_shared(sb[current_stage])
    self.dot(a, b, acc, out=acc)
    preload_offset_k = offset_k + (self.num_stages - 1) * block_k
    if preload_offset_k < end_offset_k:                       # ← 越界保护
        self.copy_async(src=ga, dst=sa[preload_stage], offsets=[offset_m, preload_offset_k])
        self.copy_async(src=gb, dst=sb[preload_stage], offsets=[preload_offset_k, offset_n])
    self.copy_async_commit_group()
    current_stage = (current_stage + 1) % self.num_stages
    preload_stage = (preload_stage + 1) % self.num_stages
    self.copy_async_wait_group(n=self.num_stages - 2)
    self.sync()
```

[matmul_v5.py:L120-L147](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v5.py#L120-L147) 的 `if preload_offset_k < end_offset_k` 是相对 v4 的关键改进：因为 Split-K 后每个块的 K 区间不再是全局 K 的整数倍，末尾几轮不能盲目预取。

**epilogue：经共享内存改布局 + 信号量聚合**：

```python
# matmul_v5.py:154-186
sc = self.shared_tensor(dtype=float16, shape=[block_m, block_n])
casted_acc = self.cast(acc, dtype=float16)
self.store_shared(sc, casted_acc); self.sync()
rc = self.load_shared(sc); self.free_shared(sc)        # 借 SRAM 把布局改成适合 store_global

m_blocks, n_blocks = cdiv(m_size, block_m), cdiv(n_size, block_n)
gc = self.global_view(c_ptr, dtype=float16, shape=[m_size, n_size])
if self.split_k_factor == 0:                            # 当前搜索空间下不会命中（候选含 1 不含 0）
    self.store_global(gc, rc, offsets=[offset_m, offset_n])
else:
    semaphores = self.global_tensor(dtype=int32, shape=[m_blocks, n_blocks], requires_clean=True)
    semaphore = semaphores[self.blockIdx.x, self.blockIdx.y].item_ptr()
    if self.blockIdx.z > 0:                             # 非首块：取已有部分和再累加
        self.lock_semaphore(semaphore, value=self.blockIdx.z)
        partial_rc = self.load_global(gc, offsets=[offset_m, offset_n], shape=[block_m, block_n])
        self.add(rc, partial_rc, out=rc)
    self.store_global(gc, rc, offsets=[offset_m, offset_n])
    self.sync()                                         # 保证 store_global 落地
    self.release_semaphore(semaphore, value=(self.blockIdx.z + 1) % self.split_k_factor)
```

[matmul_v5.py:L154-L186](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v5.py#L154-L186) 是 Split-K 聚合的完整逻辑。几个要点：

- 借 `shared_tensor sc` 中转一次（`store_shared`→`sync`→`load_shared`），是为了把累加器的寄存器布局改成适合 `store_global` 的布局（布局变换的实用技巧，承接 u4-l4）。
- `global_tensor(..., requires_clean=True)`（[root.py:L346](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L346) 起）申请一块**运行时分配的全局 workspace** 存信号量，要求结束时归零。
- `lock_semaphore(sem, value=v)`（[root.py:L1843](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L1843)）自旋等到信号量等于 `v`；`release_semaphore(sem, value=v)`（[root.py:L1864](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L1864)）把它写成 `v` 放行下一块。
- 最后一块 `blockIdx.z = split_k_factor-1`，释放值 `(split_k_factor)%split_k_factor = 0`，正好满足 `requires_clean`。

#### 4.4.4 代码实践

**实践目标**：体会 Split-K 对「瘦长」形状的收益。

**操作步骤**：

1. 运行 `matmul_v5.py`，它自带两个形状：方形 `[4096,4096,4096]` 与瘦长 `[4096,4096,14336]`（[matmul_v5.py:L200-L203](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v5.py#L200-L203)）。
2. 查看 autotune 为这两个形状分别选了什么 `split_k_factor`（缓存里的 `dispatch_table.txt`）。
3. 对照 v4（只有方形 `[4096,4096,4096]` 和 `[1024,1024,14336]`），看瘦长形状上 v5 是否比「固定 split_k_factor=1」更快。

**需要观察的现象**：方形形状 autotune 往往选中 `split_k_factor=1`（不切分，因为已经填满 SM）；瘦长形状倾向选较大的 `split_k_factor`（4/12/16）以填满 SM。

**预期结果**：v5 在瘦长形状上明显优于「无 Split-K」版本；在方形形状上与 v4 相当（autotune 自动退化为不切分）。**具体选中值待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么最后一块要 `release_semaphore(value=0)` 而不是 `value=split_k_factor`？

**参考答案**：信号量按 `blockIdx.z` 的顺序轮转复用，下一个（未来的）「块 0」会 `lock_semaphore(value=0)`。若不归零，下次复用同一 C tile 时块 0 永远等不到 0而死锁。归零正是 `global_tensor(requires_clean=True)` 的契约要求。

**练习 2**：`if self.split_k_factor == 0` 这个分支在当前 autotune 空间里会命中吗？

**参考答案**：不会。候选是 `{1,4,12,16}`（[matmul_v5.py:L57](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v5.py#L57)），最小是 1。当 `split_k_factor=1` 时走 `else` 分支：`blockIdx.z` 恒为 0，跳过 `lock/load_global/add`，直接 `store_global` 再 `release(value=0)`，行为等价于「不切分」，但多走了一次信号量开销。所以这个 `== 0` 分支在此空间内是死代码（可能是为外部直接调用预留的接口）。

**练习 3**：Split-K 为什么通常配合 autotune 而不是固定一个大值？

**参考答案**：Split-K 用更多线程块换 SM 占满，但引入了信号量串行化与额外全局访存（每个非首块要多一次 `load_global` 读部分和）。形状「足够方」时 SM 已占满，切分反而因聚合开销变慢。是否切、切几份强依赖形状，故必须交给 autotune 按形状挑选。

---

## 5. 综合实践

把四个版本的性能拆解归因——这是本讲的总练习。

**任务**：在同一台 Ampere（sm_80+）机器上，依次运行 `matmul_v2.py`、`matmul_v3.py`、`matmul_v4.py`、`matmul_v5.py`，对形状 `[4096,4096,4096]` 收集 tilus 的 TFLOPS，填入下表并归因：

| 版本 | 关键改动 | TFLOPS | 相对 v2 加速比 | 主要收益来源 |
| --- | --- | --- | --- | --- |
| v2 | 共享内存分块 + autotune（基线） |  | 1.0× | 分块复用 + 自动选参 |
| v3 | + `cp.async` 异步拷贝 |  |  | 去寄存器中转、释放寄存器 |
| v4 | + 软件流水线（多级缓冲） |  |  | 访存/计算 overlap |
| v5 | + Split-K + 信号量 |  |  | （方形形状上通常≈v4） |

**步骤**：

1. 每个示例先 `rm -rf` 对应缓存目录再跑，确保走完整 autotune。
2. 记录 TFLOPS，并计算每步加速比。
3. 用 `ncu` 抓 v2 与 v4 的 `smsp__pipe_tensor_op_hmma_cycles_active`（张量核活跃）与访存吞吐，对比张量核利用率差异。
4. 给出结论：v2→v5 的总加速中，哪一项改动贡献最大？（预期：软件流水线 v3→v4 通常贡献最大，因为它真正喂饱了张量核。）

**进阶**：把 v5 的 `split_k_factor` 候选改成只含 `[1]`，在 `[4096,4096,14336]` 上跑，对比有/无 Split-K 的差距，亲手验证 Split-K 对瘦长形状的价值。

> 若当前环境无合适 GPU，可将 TFLOPS 列标注「待本地验证」，但**归因逻辑（哪个改动贡献什么）应能独立给出**——这正是本讲要训练的判断力。

## 6. 本讲小结

- **共享内存分块（v1/v2）** 把多次 DRAM 访问压缩成「一次 DRAM + 多次 SRAM」，K 维循环里靠两次 `sync` 守护读写交替；v2 用 `@autotune` 把分块/线程数纳入搜索空间，免去手调。
- **MMA / ldmatrix 是自动生成的**：用户写块级 `dot`/`load_shared`，布局推理（u4-l5）配出 MMA 相容布局，发射器（`DotInstEmitter` @ sm_70+、`shared_ldst`）按布局整除性发射 `mma.m16n8k16` 与 `ldmatrix`——这是 Tilus「好写又快」的根因。
- **cp.async（v3）** 用 `copy_async` 让拷贝引擎 DRAM→SRAM 直达、不经寄存器且异步，释放寄存器并为流水线铺路。
- **软件流水线（v4）** 用 `num_stages` 份共享内存环形缓冲 + `commit_group/wait_group` 分组，把访存延迟藏进计算时间里，通常贡献最大的加速。
- **Split-K（v5）** 沿 K 维再切、用信号量串行聚合，专治「瘦长」形状的 SM 不饱问题；autotune 会在方形形状上自动退化为不切分。
- 每一步优化都**保持用户代码与架构无关**，硬件细节（mma/ldmatrix/cp.async/semaphore）由布局系统与发射器在编译期落地。

## 7. 下一步学习建议

- **向 Hopper 进阶（u7-l2）**：把本讲的 `mma.m16n8k16` + `cp.async` 升级为 `wgmma`（warp-group MMA）+ `mbarrier`，体会「张量核本身异步」带来的更深流水线。可对照 `examples/hopper_matmul/`。
- **向 Blackwell 进阶（u7-l3）**：用 `tma`（张量内存加速器）替换 `cp.async` 的逐块搬运，用 `tcgen05` + TMEM 替换经典 MMA，理解「整 tile 异步搬运」的数据流。可对照 `examples/blackwell_matmul/`。
- **回看发射器实现（u6-l4）**：若想搞懂「布局如何被翻译成每线程的标量地址与 ldmatrix」，精读 `shared_ldst.py` 的快路径判定与回退逻辑。
- **动手扩展**：尝试在 v4 基础上把 K 循环的 `unroll` 或 `num_stages` 改成 autotune 之外的手工值，用 `dump_ir` 观察展开后的 IR 与生成的 PTX 差异，巩固 u5（变换）与 u6（代码生成）的认知。
