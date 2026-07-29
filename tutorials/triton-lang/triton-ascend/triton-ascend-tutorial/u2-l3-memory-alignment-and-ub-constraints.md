# 内存对齐、UB 与 coreDim 约束

## 1. 本讲目标

本讲是「GPU → Ascend NPU 迁移」单元的第三步（前两步分别是 Python 侧迁移与 Grid 核心分配）。上一讲我们解决了「一个 kernel 要开多少个 program、绑到哪些物理核」的问题；这一讲我们要解决另一个同等重要的问题：**当数据真正在 NPU 片上流动时，有哪些硬性边界不能越过**。

学完本讲，你应当能够：

1. 说出**向量算子 32 字节对齐、Cube-Vector 融合算子 512 字节对齐**两条规则，理解它们从何而来。
2. 认识**Unified Buffer（UB）**这块片上缓存：它有多大、为什么会溢出、溢出时报什么错。
3. 掌握 **coreDim 不超过 65535（UINT16_MAX）** 这条硬上限，以及「调大 BLOCK_SIZE」与「开 `TRITON_ALL_BLOCKS_PARALLEL`」两种解法。
4. 最关键的是：理解这三者构成一个**相互制约的三角**——调大 `BLOCK_SIZE` 能压低 coreDim，却可能撑爆 UB，从而引出「分核分块（BLOCK_SIZE_SUB）」这种叠加 tiling 写法。

本讲刻意只讲「正确性与可编译性」的边界，不讲精细性能调优（那是 u9 自动调优单元的主题）。

## 2. 前置知识

在进入正文前，请确认你已经理解以下概念（来自 u1 与 u2 前两讲）：

- **kernel / block / grid / program**：一个 `@triton.jit` kernel 被 `grid` 切成若干 program，每个 program 处理一个数据块（block）。参见 u1-l4。
- **物理核心绑定模型**：与 GPU 的逻辑维度不同，昇腾 NPU 采用「强物理核心绑定」——grid 直接对应物理核占用。参见 u2-l2。
- **Cube Core / Vector Core**：AI 核内分矩阵单元（Cube，专做 `tl.dot`）与向量单元（Vector）。参见 u2-l2。
- **片上存储层级（直觉版）**：可以把 NPU 的单个核想象成一个「袖珍 CPU」——它从片外全局内存（HBM/DDR）把数据搬进**片上缓存（UB，Unified Buffer）**，在缓存里做计算，再写回全局内存。本讲的核心约束几乎全部围绕「搬多少、怎么搬、片上放不放得下」。

> 术语对照：UB（Unified Buffer，统一缓冲区）= 片上高速缓存；MTE2/MTE3 = 搬入/搬出访存指令；DOP（Degree of Parallelism）= 指令并行度。这些术语在本讲和后续性能讲义中会反复出现。

## 3. 本讲源码地图

本讲涉及的关键文件分为「文档」与「实现代码」两类：

| 文件 | 作用 |
|------|------|
| `docs/en/migration_guide/migrate_from_gpu.md` | 迁移总流程，明确列出对齐、UB、coreDim 三大约束与示例。 |
| `docs/en/migration_guide/performance_guidelines.md` | 高性能编程指南，讲多缓冲与 UB、tiling 的关系。 |
| `docs/en/migration_guide/architecture_difference.md` | 讲 Auto-Blockify 如何突破 65535 逻辑块上限。 |
| `docs/en/debug_guide/ub_overflow.md` | UB 溢出排查专题，列出常见成因与解法。 |
| `third_party/ascend/backend/utils.py` | 读取 `TRITON_ALL_BLOCKS_PARALLEL` 等环境变量、维护 AutoBlockify 黑名单。 |
| `third_party/ascend/backend/runtime/utils.py` | 探测核数、**UB 容量（192/256 KB）**、`byte_per_numel` 表。 |
| `third_party/ascend/backend/compiler.py` | 编译选项装配，含 UB 打印开关、`NPUOptions` 字段。 |
| `third_party/ascend/backend/driver.py` | 运行时核心数裁剪（`std::min` cap）、`NPU_DEVICE_LIMIT`。 |
| `third_party/ascend/backend/runtime/ubtuner.py` | `@ubtuner`：编译期自动规避 UB 溢出。 |

## 4. 核心概念与源码讲解

### 4.1 内存对齐：32 字节与 512 字节

#### 4.1.1 概念说明

「对齐（alignment）」是一个硬件层面的访存要求：当一段数据在内存中的起始地址是某个字节数（如 32）的整数倍时，硬件可以用一条最高效的访存指令一次搬完；否则硬件要么拆成多次访问、要么补齐（padding）。

昇腾 NPU 对 Triton kernel 的访存有两条明确规则：

- **纯向量（Vector）算子：要求 32 字节对齐。**
- **Cube-Vector 融合算子：要求 512 字节对齐。**

这两条规则来自迁移文档的「检查单 program 数据搬运」一节：

> Vector operators require 32-byte memory access alignment, and cube-vector fused operators require 512-byte alignment.
> —— [docs/en/migration_guide/migrate_from_gpu.md:L34-L38](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/migrate_from_gpu.md#L34-L38)

为什么向量场景是 32 字节？因为 NPU 硬件强制 UB 访存按 32 字节对齐。当一次 `tl.load` 的数据无法整除对齐宽度时，编译器会在**最内层维度**补一个大小为若干的轴，把「未对齐」的部分展开成对齐的小块再搬运。文档用一个 `(64, 32)` 的二维搬运例子说明了这一点：

> the hardware mandates 32-byte UB memory alignment in vector operator scenarios … In unaligned access scenarios, an additional axis of size 1 is added to the innermost dimension, yielding a shape of `(64, 32, 4)`.
> —— [docs/en/migration_guide/migrate_from_gpu.md:L363-L365](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/migrate_from_gpu.md#L363-L365)

直观地说：**不连续、不对齐的访存会让一条向量指令退化成很多条小指令**，既慢又费 UB。所以迁移时若能保证「最内层维度连续且对齐」，性能与可编译性都会好得多。

#### 4.1.2 核心流程

判定一个 kernel 是否「对齐友好」的思考流程：

```text
1. 找出每个 tl.load / tl.store 访问的指针表达式
2. 看最内层维度（连续方向）的元素数 × 元素字节数，是否 ≥ 32 字节且整除 32
   - 纯向量算子门槛：32 字节
   - 含 tl.dot 的 CV 融合算子门槛：512 字节
3. 若不满足：
   - 优先用 tl.make_block_ptr 把数据当 2D 矩阵、显式给出连续 stride=(行步长, 1)
   - 否则编译器会自动补轴，代价是性能下降 + UB 占用上升
```

元素字节数由运行时的 `byte_per_numel` 表给出，这份表正是为了让 autotune「感知对齐信息」而维护的：

```python
# wrapper npu 32 bytes align, get and pass unalign info to triton meta
# then autotune choose tiling param and send them to bishengIR
byte_per_numel = {
    torch.float32: 4,
    torch.float16: 2,
    torch.bfloat16: 2,
    torch.int8: 1,
    ...
}
```

> 见 [third_party/ascend/backend/runtime/utils.py:L72-L91](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/runtime/utils.py#L72-L91) —— 注释点明了「32 字节对齐」是这条链路的根本动机。

一个常被忽视的对齐副作用是**默认值补零（padding）**。当 `tl.load` 的 `mask` 只覆盖了张量的一部分、且未指定 `other` 值时，NPU 为了与 GPU 行为一致，会先用向量核把整块缓冲区置零，再用访存指令搬入数据——这会在「搬入」和「置零」之间引入依赖，降低并行度。文档给出的优化是：若未填充部分不影响后续计算，可加 `care_padding=False` 去掉补零。

> 见 [docs/en/migration_guide/performance_guidelines.md:L22-L53](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/performance_guidelines.md#L22-L53)。

#### 4.1.3 源码精读

对齐信息如何参与编译？关键在 `byte_per_numel` 这张表——它把每个张量的元素字节数（1/2/4/8）交给 autotune，autotune 据此挑选能让最内层维度凑齐 32 字节的 tiling 参数，再把结果传给底层 BiSheng 编译器。注意第 72 行那两行注释，它直接说明了这条机制的存在意义。

此外，`NPUOptions` 里有一个 `storage_align` 选项（默认 `None`），它对应底层编译器的存储对齐开关，在 UB 调优（4.2 节）时会作为「以对齐换 UB」的手段之一被打开：

> [third_party/ascend/backend/compiler.py:L1029-L1033](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1029-L1033) —— `multibuffer`、`storage_align` 等字段都在这里声明。

#### 4.1.4 代码实践

**实践目标**：观察「最内层维度是否凑齐 32 字节」对编译产出的影响。

**操作步骤**：

1. 准备一个简单的逐元素 kernel，用 `tl.make_block_ptr` 读取一段 fp32 数据。
2. 写两个版本：
   - 版本 A：`block_shape` 最内层 = 8 个 fp32（= 32 字节，恰好对齐）。
   - 版本 B：`block_shape` 最内层 = 5 个 fp32（= 20 字节，不对齐）。
3. 开启环境变量 `export TRITON_DEBUG=1`、`export MLIR_ENABLE_DUMP=1`，分别编译两个版本。
4. 在 dump 目录里找到 `*.ttadapter` 中间表示，对比两个版本的访存形状。

**需要观察的现象**：版本 B 的访存形状在最内层维度被「撑开」成对齐宽度（出现补轴或更细粒度的访问），版本 A 保持紧凑。

**预期结果**：版本 A 的 IR 更干净、访存指令更少。具体 IR 形态**待本地验证**（依赖真实硬件与 CANN 环境）。

#### 4.1.5 小练习与答案

**练习 1**：一段 bf16（每元素 2 字节）数据，要让纯向量算子满足 32 字节对齐，最内层维度至少需要多少个元素？

**答案**：\( 32 \div 2 = 16 \)，至少 16 个 bf16 元素。

**练习 2**：为什么 CV 融合算子要求更严格的 512 字节对齐，而不是 32 字节？

**答案**：CV 融合算子在 Cube（矩阵）单元上运行，矩阵单元一次处理的「瓦片（tile）」更大，硬件按 512 字节的粒度搬运与对齐数据；32 字节只是 Vector 单元的粒度。对齐粒度由硬件访存单元的天然处理宽度决定。

---

### 4.2 Unified Buffer（UB）：片上缓存与溢出

#### 4.2.1 概念说明

**UB（Unified Buffer，统一缓冲区）**是每个 NPU 核上的一块**片上高速缓存**。每个 program 在一个核上执行时，它 `tl.load` 进来的数据、计算中间结果、待 `tl.store` 写回的数据，都要先放在这块 UB 里。UB 极快但**容量很小**——典型值是 **192 KB**，最新的 910_95/950 是 **256 KB**。

当单个 program 要在 UB 里同时放下「数据 + 中间量 + 多缓冲副本」超过 UB 容量时，就会发生 **UB 溢出（UB overflow）**，典型报错形如：

> `ub overflow, requires xxxx bits while 1572864 bits available!`

注意这里的单位是 **比特（bits）**：\( 1\,572\,864 \text{ bits} = 192 \times 1024 \times 8 \text{ bits} = 192 \text{ KB} \)。这个数字正是 910B/93 的 UB 容量。

> 报错原文见 [docs/en/migration_guide/migrate_from_gpu.md:L143-L148](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/migrate_from_gpu.md#L143-L148)。

UB 容量在代码里是按硬件型号探测出来的：

```python
ub_size_in_kbytes = 192
ASCEND_VARIANTS = ["Ascend910B", "Ascend910_93", "Ascend910_95", "Ascend950"]
if any(variant in target.arch for variant in ASCEND_VARIANTS):
    num_vector_core = num_cube_core * 2
if target.arch.startswith("Ascend910_95") or target.arch.startswith("Ascend950"):
    ub_size_in_kbytes = 256
    rf_size_in_kbytes = 128
```

> 见 [third_party/ascend/backend/runtime/utils.py:L39-L61](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/runtime/utils.py#L39-L61) —— 注意默认值是 192 KB，910_95/950 提升到 256 KB。

#### 4.2.2 核心流程

什么操作最「吃 UB」？UB 溢出排查文档列了四大类常见成因：

1. **触发额外处理逻辑的接口参数**——例如 `tl.maximum/minimum/clamp` 设置 `propagate_nan=tl.PropagateNAN.NONE`，会自动注入 NaN 检测逻辑，显著增加 UB 占用。
2. **中间变量过多**——kernel 里定义了大量临时张量。
3. **大数据类型 / 大形状**——fp64、bf16、高维大张量。
4. **复杂控制流 / 多层嵌套循环**。

> 详见 [docs/en/debug_guide/ub_overflow.md:L7-L60](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/debug_guide/ub_overflow.md#L7-L60)。

还有一个常被忽视的 UB「吞金兽」：**多缓冲（multi-buffer）**。为了实现「搬入/计算/搬出」三段流水重叠，编译器会把 UB 里的同一份缓冲复制成多份（ping-pong）。这能提升并行度，但代价是成倍占用 UB。文档明确指出：

> The multi-buffer mechanism requires additional UB space. If the UB space is insufficient during computation, the multi-buffer mechanism cannot be enabled.
> —— [docs/en/migration_guide/performance_guidelines.md:L13-L17](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/performance_guidelines.md#L13-L17)

对应的解法也是一条线索贯穿全讲：**缩小每个 program 一次处理的数据量（tiling）**。文档原话：「与一次性处理整块数据相比，用 `for` 循环做 tiling 会**降低 UB 占用**」。

> 见 [docs/en/migration_guide/performance_guidelines.md:L55-L58](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/performance_guidelines.md#L55-L58)。

整个 UB 管控流程可以概括为：

```text
编译期：
  - 编译器按 kernel 语义估算每个 program 的 UB 占用
  - 若估算 > UB 容量 → 抛 "ub overflow, requires xxxx bits while yyyy bits available!"
排查期：
  - 开 ENABLE_PRINT_UB_BITS=1 → 编译时加 --enable-print-memory-allocated-size，打印真实 UB 位数
解法：
  - 减小 BLOCK_SIZE / 增加 tiling（手工）
  - 关闭/限制 multi-buffer（NPUOptions.multibuffer / limit-auto-multi-buffer-of-local-buffer）
  - 用 @ubtuner 自动尝试 bisheng 编译选项
```

#### 4.2.3 源码精读

**（1）UB 占用打印开关。** `ENABLE_PRINT_UB_BITS` 环境变量会把 `--enable-print-memory-allocated-size` 选项传给底层编译器：

```python
if _enable_print_ub_bits():
    _compile_option_list += ["--enable-print-memory-allocated-size"]
```

> 见 [third_party/ascend/backend/compiler.py:L588-L589](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L588-L589)。这是定位「到底哪个算子吃 UB」的关键手段。

**（2）multi-buffer 选项。** `NPUOptions.multibuffer` 默认为 `True`，并在编译选项里装配 `--limit-auto-multi-buffer-of-local-buffer` 等参数来约束多缓冲行为：

> 见 [third_party/ascend/backend/compiler.py:L626-L634](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L626-L634)。

**（3）`@ubtuner` 自动规避。** 当 kernel 编译报 `ub overflow` 时，`@ubtuner` 装饰器会捕获这个错误，然后按「代价模型」贪婪地尝试不同的 bisheng 编译选项（如 `storage_align`、`multibuffer`、`vf_fusion_mode`），只编译不运行，找到一个不溢出的配置：

```python
UB_OVERFLOW_ERROR = "ub overflow"
...
def run(self, *args, **kwargs):
    try:
        return self.fn.run(*args, **kwargs)
    except Exception as e:
        ...
        if _Config.UB_OVERFLOW_ERROR not in str(e).lower() or autotuned:
            raise e
        print(f"{e}, Ub overflow try ub tuner.")
```

> 见 [third_party/ascend/backend/runtime/ubtuner.py:L58-L60](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/runtime/ubtuner.py#L58-L60) 与 [third_party/ascend/backend/runtime/ubtuner.py:L478-L487](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/runtime/ubtuner.py#L478-L487)。它把 UB 键映射到底层 NPUOptions 键的表是 `UB_TO_NPU_OPTION_MAP`（[ubtuner.py:L84-L85](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/runtime/ubtuner.py#L84-L85)）。`@ubtuner` 的完整用法见 u9-l4。

#### 4.2.4 代码实践

**实践目标**：复现一次 UB 溢出，再用 tiling 把它「编译通过」，对比前后差异。

**操作步骤**（示例代码，非项目原有文件）：

```python
# 示例代码：一个吃 UB 的逐元素 GELU kernel
import torch, torch_npu, triton
import triton.language as tl

@triton.jit
def gelu_huge(in_ptr, out_ptr, n_elements,
              BLOCK_SIZE: tl.constexpr):  # BLOCK_SIZE 取很大
    pid = tl.program_id(0)
    offsets = pid * BLOCK_SIZE + tl.arange(0, BLOCK_SIZE)
    mask = offsets < n_elements
    x = tl.load(in_ptr + offsets, mask=mask)            # 1 份大缓冲
    mid = x * 0.5                                       # 中间量 1
    mid2 = 1.0 + tl.erf(x / tl.sqrt(2.0))               # 中间量 2
    ret = mid * mid2                                    # 中间量 3
    tl.store(out_ptr + offsets, ret, mask=mask)

# 用一个非常大的 BLOCK_SIZE（如 65536）启动，易触发 UB 溢出
```

1. 先用一个偏大的 `BLOCK_SIZE`（如 65536）启动，观察是否报 `ub overflow`。
2. 开 `export ENABLE_PRINT_UB_BITS=1`，记录报错中的 `requires xxxx bits` 与 `available` 位数。
3. **修复**：引入 `BLOCK_SIZE_SUB`，在核内用 `for` 循环分批处理（即 4.1.2 里提到的「核内 tiling」），把每次落进 UB 的数据量降到容量以内。参考写法见 [docs/en/migration_guide/architecture_difference.md:L109-L126](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/architecture_difference.md#L109-L126) 的 `XBLOCK`/`XBLOCK_SUB` 双层分块。
4. 对比修复前后：修复后能编译通过；`requires` UB 位数明显下降。

**需要观察的现象**：大 `BLOCK_SIZE` 时编译报 `ub overflow`；改为核内 tiling 后编译通过，且打印的 `requires` UB 位数缩小到 `available` 以内。

**预期结果**：核内 tiling 把单次 UB 占用从「整个 BLOCK」降到「一个子块」，从而消除溢出。具体位数**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：报错 `requires 2000000 bits while 1572864 bits available` 说明溢出了多少？这是哪一代硬件？

**答案**：\( 2\,000\,000 - 1\,572\,864 = 427\,136 \) bits ≈ 52 KB 溢出。`1572864 bits = 192 KB`，对应 910B/93。

**练习 2**：为什么打开 multi-buffer（`multibuffer=True`）反而可能让一个原本刚好够用的 kernel 溢出？

**答案**：multi-buffer 为了做「搬入/计算/搬出」流水重叠，会把 UB 里的同一缓冲复制成多份（如 ping-pong 两份），UB 占用近乎翻倍；原本贴近上限的 kernel 一开 multi-buffer 就可能超出容量，编译器只能放弃 multi-buffer，导致并行度下降或直接报溢出。

---

### 4.3 coreDim 上限：65535 与 Auto-Blockify

#### 4.3.1 概念说明

**coreDim** 是 NPU 启动 kernel 时「占用的物理核数」维度。与 GPU 不同，NPU 的 grid 直接绑定物理核——在没开 Auto-Blockify 时，**grid 里逻辑块的数量不能超过 65535（即 UINT16_MAX）**：

> `coreDim` cannot exceed `UINT16_MAX` (65535). For large shapes, control grid size through BLOCK_SIZE or tiling.
> —— [docs/en/migration_guide/migrate_from_gpu.md:L23-L25](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/migrate_from_gpu.md#L23-L25)

超限时报错形如 `coreDim=xxxx can't be greater than UINT16_MAX`。

> 见 [docs/en/migration_guide/migrate_from_gpu.md:L139-L143](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/migrate_from_gpu.md#L139-L143)。

注意一个关键事实：在当前 HEAD，`TRITON_ALL_BLOCKS_PARALLEL` 这个开关**默认是开启的**（`utils.py` 中默认读取值为 `"true"`），也就是说 Auto-Blockify 默认就生效，逻辑块上限被自动突破。但当 kernel 含有「顺序敏感」的算子（atomic、volatile、inline asm 等）时，Auto-Blockify 会被**黑名单禁用**，此时 65535 上限重新生效——这就是为什么你仍然需要理解这条约束。

#### 4.3.2 核心流程

coreDim 与 BLOCK_SIZE 的关系是一条简单公式：

\[
\text{coreDim} = \left\lceil \frac{N}{\text{BLOCK\_SIZE}} \right\rceil \le 65535
\]

反解出 `BLOCK_SIZE` 的下限：

\[
\text{BLOCK\_SIZE} \ge \left\lceil \frac{N}{65535} \right\rceil
\]

以文档案例 \( N = 1\,073\,741\,824 \) 为例：

\[
\left\lceil \frac{1\,073\,741\,824}{65\,535} \right\rceil = 16\,385,\quad
\text{next\_power\_of\_2}(16\,385) = 32\,768
\]

即若 `BLOCK_SIZE` 取 2 的幂，至少要 **32768** 才能让 coreDim 落在 65535 以内。

> 计算过程见 [docs/en/migration_guide/migrate_from_gpu.md:L166-L181](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/migrate_from_gpu.md#L166-L181)。

解 coreDim 超限有两条路：

```text
方案 1：增大 BLOCK_SIZE → coreDim 降下来
  风险：BLOCK_SIZE 越大，单 program 的 UB 占用越大 → 可能触发 UB 溢出！（见 4.2）

方案 2：开 TRITON_ALL_BLOCKS_PARALLEL（Auto-Blockify）
  原理：编译期把 kernel 体包进 scf.for，按 gpu.linear_block_id 迭代；
        运行期把 blockNum 裁剪到物理核数。
  前提：逻辑块之间无顺序依赖；含 atomic/volatile/inline-asm 等会被黑名单禁用。
```

**复合问题（coreDim + UB）**：这正是本讲最核心的洞察——方案 1 和 4.2 的 UB 约束相互冲突。文档用一个 `masked_fill` 的案例说明：把 `BLOCK_SIZE` 从 4096 调到 32768 解决了 coreDim，却引发了 UB 溢出；最终解法是引入 `BLOCK_SIZE_SUB`，**外层用大 `BLOCK_SIZE` 压 coreDim（分核），内层用小 `BLOCK_SIZE_SUB` 压 UB（核内 tiling）**。

> 完整案例见 [docs/en/migration_guide/migrate_from_gpu.md:L268-L361](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/migrate_from_gpu.md#L268-L361)。

这种「分核 + 核内分块」的双层结构，是处理超大数据规模的标准范式：

```python
@triton.jit
def kernel(..., BLOCK_SIZE: tl.constexpr, BLOCK_SIZE_SUB: tl.constexpr):
    pid = tl.program_id(0)
    base_offset = pid * BLOCK_SIZE                       # 分核：每核负责一大段
    num_sub = tl.cdiv(BLOCK_SIZE, BLOCK_SIZE_SUB)
    for i in range(num_sub):                             # 核内 tiling：每次只搬一小段进 UB
        offsets = base_offset + i * BLOCK_SIZE_SUB + tl.arange(0, BLOCK_SIZE_SUB)
        ...
```

#### 4.3.3 源码精读

**（1）Auto-Blockify 的开关读取。** `_is_auto_map_parallel_blocks_enabled()` 读取 `TRITON_ALL_BLOCKS_PARALLEL`，**默认为 `true`**：

```python
def _is_auto_map_parallel_blocks_enabled() -> bool:
    return os.getenv("TRITON_ALL_BLOCKS_PARALLEL", "true").lower() in ("true", "1")
```

> 见 [third_party/ascend/backend/utils.py:L349-L350](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py#L349-L350)。

**（2）黑名单保护。** 含顺序敏感算子时，Auto-Blockify 会被禁用，此时 65535 上限重新生效：

```python
AUTO_BLOCKIFY_BLACKLIST_RULES = (
    (re.compile(r"\btt\.atomic_(?:rmw|cas)\b"), "atomic operations"),
    (re.compile(r"\btt\.elementwise_inline_asm\b"), "inline elementwise assembly"),
    (re.compile(r"\btt\.load\b[^\n]*\bisVolatile\s*=\s*true\b"), "loads with volatile"),
    (re.compile(r"\btt\.(?:load|store)\b[^\n]*\bcacheModifier\s*="), "loads or stores with cache modifiers"),
)
```

> 见 [third_party/ascend/backend/utils.py:L53-L64](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py#L53-L64)。这是因为 Auto-Blockify 会改变逻辑块的访问顺序，对顺序敏感的操作不安全。

**（3）编译期 wrap。** 在 `ttir_to_linalg` 编译路径里，根据 `auto_blockify_size`（默认 1）决定是否把 kernel 体包进循环；当被黑名单禁用或开关关闭时，`auto_blockify_size` 被强制设回 1：

```python
auto_blockify_size = metadata["auto_blockify_size"]
...
if has_auto_blockify_blacklist_op or not auto_map_parallel_blocks_enabled:
    auto_blockify_size = 1
```

> 见 [third_party/ascend/backend/compiler.py:L184-L192](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L184-L192)。

**（4）运行期 cap。** 这是与编译期配套的另一半——生成出的 C++ launcher 里，`blockNum` 被 `std::min` 裁剪到物理核数，镜像了编译期的折叠：

```python
num_physical_blocks = npu_utils.get_aivector_core_num() if mix_mode == "aiv" else npu_utils.get_aicore_num()
...
{'blockNum = std::min(blockNum, (uint32_t)' + str(num_physical_blocks) + ');' if enable_auto_map_parallel_blocks else ''}
```

> 见 [third_party/ascend/backend/driver.py:L545-L547](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L545-L547) 与 [third_party/ascend/backend/driver.py:L915-L922](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L915-L922)。

架构文档把这个「编译期折叠 + 运行期裁剪」的双侧机制讲得很清楚：两侧共享同一份门控元数据（`enable_auto_blockify` 回退到 `TRITON_ALL_BLOCKS_PARALLEL`），所以「编译模式」和「启动模式」永远同步，不会出现「按一种模式编译、按另一种模式启动」的错配。

> 见 [docs/en/migration_guide/architecture_difference.md:L26-L41](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/architecture_difference.md#L26-L41)。

#### 4.3.4 代码实践

**实践目标**：亲手算出某个大数据规模下不触发 coreDim 上限的最小 `BLOCK_SIZE`。

**操作步骤**：

1. 取一个大数据规模，例如 \( N = 1\,073\,741\,824 \)（即 \( 2^{30} \)）。
2. 用本节公式手算：\( \lceil N / 65535 \rceil = 16385 \)，再取 `next_power_of_2` 得 32768。
3. 验证：用 `BLOCK_SIZE=32768` 时 coreDim \( = \lceil N / 32768 \rceil = 32768 \le 65535 \) ✓；用 `BLOCK_SIZE=16384` 时 coreDim \( = 65536 > 65535 \) ✗。
4. 进阶：把同一个 kernel 用 `BLOCK_SIZE=32768` 启动，再开 `export ENABLE_PRINT_UB_BITS=1` 看是否反过来触发 UB 溢出——若有，按 4.3.2 的「双层分块」加 `BLOCK_SIZE_SUB`。

**需要观察的现象**：`BLOCK_SIZE` 从刚好不足（16384，coreDim=65536 超限）切到 32768 后，coreDim 回到限内；若 32768 触发 UB 溢出，则需再叠加核内 tiling。

**预期结果**：coreDim 随 `BLOCK_SIZE` 增大而反比下降；UB 占用随 `BLOCK_SIZE` 增大而上升。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：\( N = 536\,870\,912 \)（\( 2^{29} \)，fp16），不依赖 Auto-Blockify 时，`BLOCK_SIZE`（2 的幂）至少要多大？

**答案**：\( \lceil 536\,870\,912 / 65\,535 \rceil = 8193 \)，`next_power_of_2(8193) = 16384`。至少 16384。

**练习 2**：为什么 atomic 操作会被列入 Auto-Blockify 黑名单？

**答案**：Auto-Blockify 会让一个物理核按 chunk 顺序迭代多个逻辑块，改变了逻辑块间的执行/访问顺序。atomic 操作（以及 volatile load、cache modifier）对访存顺序敏感，顺序改变可能导致竞态或正确性问题，因此必须禁用 Auto-Blockify，回到「逻辑块数 = 物理核数、≤ 65535」的原始约束。

---

## 5. 综合实践

把本讲三大约束串起来，设计一个**「约束三角」小任务**。

**任务**：实现一个 `masked_scale` 算子——对长度为 \( N \)（取一个很大的值，如 \( 2^{30} \)）的向量，按下标掩码 `mask` 把对应位置缩放 `value` 倍，写回输出。

**要求依次满足三条约束**：

1. **对齐**：用 fp32，让最内层连续访问凑齐 32 字节（即每块至少 8 个 fp32）。
2. **coreDim**：先用公式算出能让 coreDim ≤ 65535 的最小 `BLOCK_SIZE`（应得到 32768）。
3. **UB**：用 4.3.2 的双层分块写法，外层 `BLOCK_SIZE=32768` 控 coreDim，内层 `BLOCK_SIZE_SUB`（如 1024）控 UB，用 `for` 循环做核内 tiling。

**验收方法**：

- 用 `torch_npu` 生成输入，跑你写的 kernel，与 PyTorch 参考实现 `torch.where(mask_bool, x * value, x)` 比对，`torch.allclose` 应为 `True`。
- 开 `export TRITON_DEBUG=1`、`export ENABLE_PRINT_UB_BITS=1`，确认编译通过且 UB 占用在容量内。
- 若直接用大 `BLOCK_SIZE` 不加 `BLOCK_SIZE_SUB`，尝试复现 UB 溢出报错，再恢复双层分块。

**思考题**：如果这个 kernel 里用到了 `tl.atomic_add`，4.3 的 Auto-Blockify 还能帮你兜底 coreDim 吗？为什么？（答：不能，atomic 会进黑名单，65535 上限重新生效，必须靠调大 `BLOCK_SIZE`，而这又可能压向 UB 上限——这正是「约束三角」的张力所在。）

> 提示：双层分块的写法可直接参考 [docs/en/migration_guide/migrate_from_gpu.md:L321-L361](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/migrate_from_gpu.md#L321-L361) 中 `masked_fill_kernel` 的 `BLOCK_SIZE`/`BLOCK_SIZE_SUB` 实现。

## 6. 本讲小结

- **三条硬约束**：向量算子访存需 **32 字节对齐**、CV 融合算子需 **512 字节对齐**；UB 片上缓存很小（**192/256 KB**），超出即 `ub overflow`；coreDim 不超过 **65535（UINT16_MAX）**。
- **UB 的本质**：每个核的片上缓存，`tl.load` 的数据、中间量、多缓冲副本都要放进去；报错单位是比特，`1572864 bits = 192 KB`。
- **UB 四大成因**：触发额外逻辑的参数（如 `propagate_nan=NONE`）、中间变量过多、大数据类型/形状、复杂控制流；multi-buffer 会成倍吃 UB。
- **UB 排查与解法**：`ENABLE_PRINT_UB_BITS` 打印真实占用；缩小 `BLOCK_SIZE` / tiling；`@ubtuner` 自动试编译选项。
- **coreDim 解法**：调大 `BLOCK_SIZE`（风险：撑爆 UB）或开 `TRITON_ALL_BLOCKS_PARALLEL`（Auto-Blockify，默认开，但 atomic/volatile 等会被黑名单禁用）。
- **核心洞察——约束三角**：coreDim 与 UB 此消彼长，处理超大数据的标准范式是**「外层大 BLOCK 压 coreDim + 内层 BLOCK_SIZE_SUB 压 UB」的双层分块**。

## 7. 下一步学习建议

本讲聚焦「正确性与可编译性」的硬边界。接下来：

- **想理解「编译期如何估算并规避 UB」**：阅读 u9-l4（`@ubtuner`）与 u9-l1/u9-l2（autotune 如何在候选配置里挑选对齐/tiling）。
- **想理解「Auto-Blockify 的 C++ pass 实现」**：阅读 u8-l3（AutoBlockify 与并行块映射）与 u10-l5（扩展 C++ pass）。
- **想系统掌握调试手段**：u10-l1（IR dump、解释器模式）会讲如何用 `TRITON_DEBUG`/`MLIR_ENABLE_DUMP` 精确定位「到底是哪个算子、哪个阶段」吃掉了 UB 或撞上 coreDim。
- **下一单元（u3）**：进入 Triton 编译流水线总览，理解这些约束在 `make_ttir → ttir_to_linalg → npubin` 链路中分别由哪些 pass 处理。
