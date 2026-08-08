# cute_dsl_utils 与 compile_utils：dtype 映射、设备能力与编译期符号张量

## 1. 本讲目标

本讲聚焦两个被全项目复用的「公共地基」文件：`quack/cute_dsl_utils.py` 与 `quack/compile_utils.py`。它们解决的是 CuTe-DSL 内核在「主机侧（Python）」与「设备侧（CUDA）」边界上的三件通用小事，却几乎被每一个内核模块依赖。

学完后你应该能够：

- 说清 `torch2cute_dtype_map` 在做什么翻译、`get_device_capacity` 如何决定走哪条 SM 内核分支，以及 `QUACK_ARCH` 在无 GPU 编译中的作用。
- 理解 `make_fake_tensor` 如何用「符号维度 + 对齐提示」构造一个不需要真实数据的编译期张量，并能解释 `divisibility`、`leading_dim`、`assumed_align` 三个参数的含义。
- 区分 `ParamsBase`（dataclass 风格）与 `mlir_namedtuple`（NamedTuple 风格）两种 JIT 参数容器，理解「静态字段（编译期烘焙）」与「动态字段（运行期传递）」的分流。

## 2. 前置知识

本讲默认你已经读过：

- **u1-l2 安装、构建与运行测试**：知道 `pip install -e '.[dev]'` 与 pytest 切片运行方式。
- **u1-l4 CuTe-DSL 编程模型入门**：理解 `@cute.jit` / `@cute.kernel`、编译期常量（`const_expr`）与运行期值之分，并知道 `cute.compile()` 是触发编译的入口。
- **u2-l6 cute_op 自定义算子与编译缓存**：已经见过 `Softmax.compile` 用一个「batch 为符号维」的 fake 张量喂给 `cute.compile`，让产物对任意 batch 复用。本讲正是把那个 fake 张量「拆开盒子」讲清楚。

补充几个本讲会用到的通俗概念：

- **SM（流多处理器）与计算能力（capability）**：NVIDIA 用一个 `(major, minor)` 元组描述 GPU 架构代号。QuACK 关心的几代是 SM8x（Ampere/Ada，major=8）、SM90（Hopper，major=9）、SM100/SM110（Blackwell 数据中心，major=10/11）、SM120（Blackwell 消费级，major=12）。不同代的指令集（TMA、WGMMA、tcgen05 等）不同，所以同一算子要按 major 分发不同内核实现。
- **FFI（Foreign Function Interface）**：编译产物是一段机器码，PyTorch 调用它时需要跨越 Python↔C++ 边界传参，这条边界就叫 FFI。QuACK 用 TVM-FFI 作为这条边界的承载。
- **MLIR**：编译器的中间表示。DSL 内核在 `cute.compile` 时被翻译成一串 MLIR 值，最终再下沉到 PTX/机器码。「动态字段」会变成 MLIR 的块参数，「静态字段」则被烘焙进编译产物、不出现在运行期参数里。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [quack/cute_dsl_utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cute_dsl_utils.py) | dtype 映射、设备能力查询（含 `QUACK_ARCH` 覆盖）、`ParamsBase` 与 `mlir_namedtuple` 两种 JIT 参数容器、以及对 TVM-FFI 转换器的 Constexpr 补丁。 |
| [quack/compile_utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/compile_utils.py) | `make_fake_tensor` 符号张量构造、`div_for_dtype` 对齐推导、`fake_batched` 批次旋转封装、`make_fake_stream` 假流。 |
| [quack/softmax.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py) | `Softmax.compile` 是 `make_fake_tensor` 的最小真实用例。 |
| [quack/gemm.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py) | `_compile_gemm` 中的 `sm_to_cls` 字典是「设备能力驱动内核选类」的典型现场。 |
| [quack/gemm_base.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py) | `EpilogueParams = ParamsBase` 展示 ParamsBase 的实际用法。 |
| [quack/tile_scheduler.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py) | 多个 `@mlir_namedtuple` 装饰的参数容器。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：dtype 映射与设备能力查询（4.1）、`make_fake_tensor` 编译期符号张量（4.2）、`ParamsBase` 与 `mlir_namedtuple` 参数容器（4.3）。

### 4.1 dtype 映射与设备能力查询

#### 4.1.1 概念说明

内核从 PyTorch 接收的输入是 `torch.Tensor`，它的类型用 `torch.dtype`（如 `torch.float16`、`torch.bfloat16`）表示；但 CuTe-DSL 内核内部用的是 cutlass 的数值类型（如 `cutlass.Float16`、`cutlass.BFloat16`）。两者是不同体系，需要一个翻译表。这就是 `torch2cute_dtype_map` 的全部职责——一个普通的字典。

同时，主机侧在「决定实例化哪个内核类」之前，必须先知道当前 GPU 是哪一代。这件事由 `get_device_capacity()` 完成，它返回 `(major, minor)`。设备能力（capability）几乎是 QuACK 所有架构分支的总开关：

- 选哪个 GEMM 内核类（`GemmSm80` / `GemmSm90` / `GemmSm100` / `GemmSm120`）；
- 选哪套 autotune 配置空间；
- 是否支持 cluster、TMA、2D 稠密操作数等特性。

#### 4.1.2 核心流程

`get_device_capacity` 的决策链可以用伪代码概括：

```text
def get_device_capacity(device):
    if 环境变量 QUACK_ARCH 已设置:
        return _parse_arch_str(QUACK_ARCH)      # 无 GPU 也能交叉编译
    else:
        return torch.cuda.get_device_capability(device)   # 查真实硬件
```

`QUACK_ARCH` 是一个重要的「逃生口」：在没有 GPU 的机器上（例如 CI 的纯 CPU 节点），仍可以用 `QUACK_ARCH=90` 让编译链路相信「我们在为 Hopper 编译」，从而完成 trace 与编译产物生成。这与 u2-l6 介绍的「fake 张量让编译脱离真实数据」是同一哲学的另一半——脱离真实硬件。

注意 `get_device_capacity` 还有一个「兄弟」函数 `get_compile_target_capacity()`：前者回答「该 trace 哪个内核类 / 哪套配置」（受 `QUACK_ARCH` 影响），后者回答「trace 出来的代码可以发射哪些指令」（取自真实 ptxas 编译目标）。两者在 CI 代理机上会故意不一致。

#### 4.1.3 源码精读

dtype 映射字典——把 torch 的 dtype 一一翻译成 cutlass 的数值类型：

[quack/cute_dsl_utils.py:56-70](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cute_dsl_utils.py#L56-L70) 这段字典就是 `torch2cute_dtype_map`。值得注意两处特殊条目：`torch.float4_e2m1fn_x2`（打包 fp4，dlpack 会把逻辑 K 维展宽一倍呈现给 DSL）与 `torch.float8_e8m0fnu`（FP8 的纯指数缩放因子）。Softmax 的前向入口正是用这张表完成翻译的：

```python
dtype, out_dtype = [torch2cute_dtype_map[t.dtype] for t in [x, out]]
```

设备能力查询——支持 `QUACK_ARCH` 覆盖与缓存：

[quack/cute_dsl_utils.py:102-129](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cute_dsl_utils.py#L102-L129) 这里 `_get_device_capacity_cached` 带 `@lru_cache`（设备能力在一次进程里恒定，值得缓存），外层 `get_device_capacity` 还会把传入的 `torch.Tensor` 规范成 `torch.device`，避免把张量对象泄漏进 LRU 的键里。`QUACK_ARCH` 的解析由 `_parse_arch_str` 完成：

[quack/cute_dsl_utils.py:93-99](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cute_dsl_utils.py#L93-L99) 用正则兼容 `90`、`sm_90`、`sm_100a` 等多种写法。

`get_max_active_clusters` 体现了「设备能力驱动特性开关」——SM8x 根本不支持 cluster：

[quack/cute_dsl_utils.py:78-90](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cute_dsl_utils.py#L78-L90) 当 `device_capacity[0] < 9` 时，若 `cluster_size != 1` 直接抛错，因为 Ampere/Ada 没有 CTA cluster 硬件。

「设备能力驱动内核选类」的典型现场在 GEMM。`gemm()` 入口先断言架构受支持：

[quack/gemm.py:752-755](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L752-L755) 取出 `(major, minor)` 并断言 major ∈ {8,9,10,11,12}。随后在 `_compile_gemm` 里用一张字典完成「major → 内核类」的分发：

[quack/gemm.py:93-100](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L93-L100) `sm_to_cls` 把 major 映射到具体类，注意 SM110（B200）复用 `GemmDefaultSm100`。`GemmCls = sm_to_cls[device_capacity[0]]` 这一行就是架构分发的落点。

除了选类，设备能力还在更细的粒度上影响行为，例如 SM8x 没有 TMA、需要把 `add_to_output` 改写成「以 D 当 C」的特殊路径：

[quack/gemm.py:865-869](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L865-L869)。

#### 4.1.4 代码实践

**实践目标**：亲手验证 SM 编号如何驱动内核选择，并体验 `QUACK_ARCH` 覆盖。

**操作步骤**：

1. 在仓库根目录用环境变量覆盖架构，启动 Python：
   ```bash
   QUACK_ARCH=90 python -c "from quack.cute_dsl_utils import get_device_capacity; print(get_device_capacity())"
   ```
2. 改用 `QUACK_ARCH=120`、`QUACK_ARCH=100` 各跑一次，对比输出。
3. 在同一进程里连续调用两次 `get_device_capacity()`，用 `timeit` 粗测第二次是否明显更快（验证 `@lru_cache`）。
4. 阅读前文的 `sm_to_cls` 字典，回答：在一台 SM110 的 B200 上，`gemm()` 最终会实例化哪个类？

**需要观察的现象**：

- `QUACK_ARCH` 不同时，返回的 `(major, minor)` 随之改变，即使底层没有 GPU 也不报错。
- 第二次调用因缓存命中而显著更快。

**预期结果**：

- `QUACK_ARCH=90` → `(9, 0)`；`QUACK_ARCH=120` → `(12, 0)`。
- B200（major=11）走 `GemmDefaultSm100`。

> 若当前环境无 GPU，步骤 1–3 依赖 `QUACK_ARCH`；若 `import quack` 因缺 cutlass-dsl 失败，则该项为「待本地验证（需 dev 环境与 cutlass-dsl）」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_get_device_capacity_cached` 要用 `@lru_cache`，而外层 `get_device_capacity` 又要把 `torch.Tensor` 先转成 `torch.device` 再喂给缓存函数？

**参考答案**：设备能力在进程生命周期内不变，缓存可省去反复查询的开销；但 LRU 的键是参数本身，若把 `torch.Tensor` 直接作为键，每个不同的张量都会算作不同的缓存条目，缓存会膨胀且失效。先规范成 `torch.device`，让「同一设备上的所有张量」命中同一条目。

**练习 2**：`get_device_capacity` 与 `get_compile_target_capacity` 在 CI 代理机上为何会不一致？

**参考答案**：前者受 `QUACK_ARCH` 影响，回答「trace 哪个内核类 / 哪套配置」；后者回答「代码可以发射哪些指令」，取自真实 ptxas 编译目标（`CUTE_DSL_ARCH` 或物理 GPU）。CI 可能在 H100 上用 `QUACK_ARCH=120` trace 出 SM120 内核类，但仍为 `sm_90a` 编译目标发射指令，于是两者不同。

---

### 4.2 make_fake_tensor 与编译期符号张量

#### 4.2.1 概念说明

u2-l6 已经展示过：`Softmax.compile` 用一个 batch 为符号维的 fake 张量喂给 `cute.compile`，产物便能对任意 batch 复用。这一节我们打开这个「fake 张量」的盒子。

核心需求叫 **tensor-free compilation（无张量编译）**：编译内核时，我们不希望真的分配一大块 GPU 显存、填上随机数，再拿去编译——那既慢又没必要。我们只想告诉编译器「这里会有一个张量，它的形状大概是这样、对齐大概是多少」，编译器据此生成可对一族形状复用的机器码。

为此需要三个信息：

- **shape（形状）**：哪些维是符号维（运行期才知，用 `cute.sym_int()` / `cute.sym_int64()` 表示），哪些维是编译期常量。
- **leading_dim（主维）**：哪个维的 stride 静态为 1（即「连续维」）。连续维的存在让编译器能发射向量化加载。
- **divisibility（可整除性，单位是「元素」）**：告诉编译器「这一维的长度保证是 N 的倍数」，据此推导对齐，发射更宽的向量指令。

#### 4.2.2 核心流程

`make_fake_tensor` 的构造过程：

```text
def make_fake_tensor(dtype, shape, divisibility=1, leading_dim=-1):
    leading_dim = 规范成非负下标
    stride = 对每个维度 i:
        若 i == leading_dim: 1
        否则: cute.sym_int64(divisibility=divisibility)   # 符号步长
    assumed_align = max(divisibility * dtype.width // 8, 1)  # 字节，下取整到 ≥1
    return cute.runtime.make_fake_tensor(dtype, shape, stride, assumed_align)
```

关键点：

- 除 leading_dim 外，每个维的 stride 都是一个**符号值**，并附带 `divisibility` 提示——这正是让编译器敢于发射宽向量的依据。
- `assumed_align` 由 `divisibility`（元素数）与 `dtype.width`（位数）换算成字节，且对亚字节 dtype（如 fp4 宽 4 位）用 `max(..., 1)` **下取整**到至少 1 字节，避免「过度声明对齐」。

`divisibility` 与对齐的换算（以 16 字节 = 128 位对齐为目标）：

\[ \text{元素数} = \frac{128}{\text{dtype.width\_bits}}, \qquad \text{字节数} = \frac{\text{元素数} \times \text{dtype.width\_bits}}{8} \]

例如 `Float16`（width=16 位）：元素数 \(128/16=8\)，字节数 \(8 \times 16 / 8 = 16\) 字节，正好 128 位对齐。这正是 `div_for_dtype` 的来源。

#### 4.2.3 源码精读

`make_fake_tensor` 的完整实现：

[quack/compile_utils.py:8-33](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/compile_utils.py#L8-L33) 注意三件事：① `leading_dim=None` 表示「没有任何静态 stride-1 维」（完全动态布局）；② stride 用 `cute.sym_int64(divisibility=...)` 逐维构造；③ `assumed_align` 用 `max(divisibility * dtype.width // 8, 1)`，注释明确说明了为何对亚字节 dtype 要用 floor 而非 ceil——避免不可整除的亚字节情形过度声明对齐。

`div_for_dtype` 把上面的换算固化成一行：

[quack/compile_utils.py:36-38](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/compile_utils.py#L36-L38) `128 // dtype.width` 即「达到 16 字节对齐所需的元素数」。

GEMM 在构造 fake 张量时正是用它推每个操作数的 divisibility：

[quack/gemm_tvm_ffi_utils.py:590-593](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L590-L593) `div_a/div_b/div_d/div_c` 默认走 `div_for_dtype`；对于需要解包（unpack）的 dtype 则用更宽的 `256 // width`。

`fake_batched` 是 `make_fake_tensor` 的批次封装，注释揭示了一个关键约定——批次张量以调用者的自然顺序 `(l, x, y)` 跨过 FFI，内核在 trace 时把它们旋转成 `(x, y, l)`：

[quack/compile_utils.py:41-53](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/compile_utils.py#L41-L53) 注意 `leading_dim + 1` 那一行——因为 batch 维被前置了一格，所以 leading_dim 要跟着偏移。

最小真实用例：Softmax 的编译方法。它把 batch 设为符号维，N 与 dtype 为编译期常量：

[quack/softmax.py:178-190](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L178-L190)。其中 `div = math.gcd(128 // dtype.width, N)` 这一行很值得品味：divisibility 取「硬件想要的元素数」与「实际 N」的最大公约数，确保不会对一个 N 不是其倍数的值过度声明可整除性。最终缓存键里不含 batch（见 u2-l6），产物对任意 batch 复用。

#### 4.2.4 代码实践

**实践目标**：构造一个 `(sym, N)` 的 fake 张量，观察 `divisibility` 如何改变 stride 与对齐。

**操作步骤**：

1. 在 dev 环境里运行下面这段「示例代码」（标注为示例，非项目原有）：
   ```python
   # 示例代码
   import cutlass.cute as cute
   from cutlass import Float16, Float32
   from quack.compile_utils import make_fake_tensor, div_for_dtype

   batch = cute.sym_int()
   N = 4096
   for div in (1, div_for_dtype(Float16)):
       t = make_fake_tensor(Float16, (batch, N), divisibility=div)
       print("div =", div, "| stride =", t.stride(), "| 对齐元素 =", div_for_dtype(Float16))
   ```
2. 把 `Float16` 换成 `Float32`，重跑一次，对比 `div_for_dtype` 的返回值。
3. 阅读前文 `Softmax.compile`，解释为何它用 `math.gcd(128 // dtype.width, N)` 而非直接 `div_for_dtype`。

**需要观察的现象**：

- `div=1` 时 stride 是纯符号值，没有可整除提示；`div=div_for_dtype(Float16)=8` 时 stride 带 `divisibility=8` 的提示，编译器据此可发射更宽的向量加载。
- `Float16` 的 `div_for_dtype` 为 8，`Float32` 为 4——dtype 越宽，达到同样 16 字节对齐所需的元素数越少。

**预期结果**：

- `div=8, Float16` 时 assumed_align = 16 字节；`div=4, Float32` 时 assumed_align 同样 = 16 字节（都对应 128 位对齐）。

> 若 `import cutlass.cute` 因无 GPU/cutlass-dsl 失败，则输出为「待本地验证（需 dev 环境与 GPU 或纯 CPU cross-compile）」。

#### 4.2.5 小练习与答案

**练习 1**：对一个 4 位宽的 fp4 dtype（`width=4`），若 `divisibility=1`，`assumed_align` 等于多少？为何要 `max(..., 1)`？

**参考答案**：`1 * 4 // 8 = 0`，经 `max(0, 1) = 1` 字节。亚字节 dtype 在不可整除时算出的字节数会小于 1，若直接声明 0 字节对齐会误导编译器；floor 到 1 字节保证「至少不夸大对齐」，这是保守且安全的声明。

**练习 2**：`fake_batched` 里为何 `leading_dim` 要 `+ 1`？

**参考答案**：批次张量以 `(l, x, y)` 顺序跨 FFI（batch 维在最前），但内核在 trace 时把 batch 旋到最后变成 `(x, y, l)`。调用者传入的 `leading_dim` 是相对 `(x, y)` 的索引；构造 3D fake 张量时 batch 维占用了下标 0，所以原 leading_dim 要整体偏移 +1 才指向正确的连续维。

---

### 4.3 ParamsBase 与 mlir_namedtuple：JIT 参数容器

#### 4.3.1 概念说明

一个内核的主机侧调用往往要传一堆参数：几个张量、几个标量、几个编译期常量。QuACK 把它们捆进一个「参数容器」里统一传递。这个容器要解决的核心问题是：**哪些字段是静态的（编译期烘焙进 cubin），哪些是动态的（运行期才传）？**

- **静态字段**：编译期常量。包括 `cutlass.Constexpr[T]`、`NumericMeta`、`int`、`bool`、`str`、`float`、`None`。它们在编译时就已经确定，被烘焙进产物，运行期不占参数位。判定依据就是文件顶部的 `StaticTypes` 元组。

- **动态字段**：张量、运行期整型等。它们会变成 MLIR 块参数、跨 FFI 真实传递。

为此，参数容器必须实现一套「JitArgument 协议」：能把自己**摊平**成一串 MLIR 值（`__extract_mlir_values__`）、能从运行期值**重建**自己（`__new_from_mlir_values__`）、能声明类型与 C 指针（`__get_mlir_types__` / `__c_pointers__`）。QuACK 提供两种风格：

- **`ParamsBase`**：dataclass 风格，自动按字段类型分流静态/动态。`EpilogueParams = ParamsBase` 即用此风格。
- **`mlir_namedtuple`**：装饰器，给一个 `NamedTuple` 类挂上同一套协议方法。`TileSchedulerOptions` 等用此风格。

#### 4.3.2 核心流程

静态/动态分流的统一入口是 `_partition_fields`：

```text
def _partition_fields(obj):
    对 obj 的每个字段:
        若 isinstance(字段值, StaticTypes): 归入 constexpr 字典
        否则:                              归入 non_constexpr 字典
    return constexpr, non_constexpr
```

编译时，`__extract_mlir_values__` 只把 **non_constexpr** 字段摊平成 MLIR 值列表（静态字段被跳过）；运行期调用时，`__new_from_mlir_values__` 用收到的运行期值重建 non_constexpr 字段，并用模板里保存的静态值补回 constexpr 字段，重新拼出一个完整对象。

对 `Constexpr[T]` 注解的字段，QuACK 还打了一个 TVM-FFI 转换器补丁：让它在 FFI 规范里被发射成 `ConstNone`（即「这个参数位不存在」），调用时传 `None`，真实值已烘焙进内核。

#### 4.3.3 源码精读

`StaticTypes` 定义了「什么是静态字段」：

[quack/cute_dsl_utils.py:24](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cute_dsl_utils.py#L24) `StaticTypes = (cutlass.Constexpr, NumericMeta, int, bool, str, float, type(None))`。

分流函数：

[quack/cute_dsl_utils.py:160-165](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cute_dsl_utils.py#L160-L165) `_partition_fields` 用 `dataclasses.fields` 取全部字段，按 `isinstance(值, StaticTypes)` 一分为二。

TVM-FFI 转换器补丁——把 `Constexpr[T]` 注解的字段标记为「不存在」：

[quack/cute_dsl_utils.py:39-53](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cute_dsl_utils.py#L39-L53) `_patched_convert_single_arg` 检测到 `arg_type` 的 origin 是 `cutlass.Constexpr` 时返回 `spec.ConstNone`，使该字段在 FFI 规范里不占运行期参数位。

`ParamsBase`——dataclass 风格的参数基类：

[quack/cute_dsl_utils.py:270-282](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cute_dsl_utils.py#L270-L282) `__extract_mlir_values__` 只摊平 non_constexpr 字段，并用 `self._values_pos` 记录每个字段消耗了几个值（供重建时切分）；`__new_from_mlir_values__` 复用 `_new_from_mlir_values`：

[quack/cute_dsl_utils.py:168-173](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cute_dsl_utils.py#L168-L173) 用 `_values_pos` 把收到的值列表按字段切分重建 non_constexpr 字段，constexpr 字段直接从原对象取，最后 `self.__class__(**non_constexpr_fields, **constexpr_fields)` 拼回。

`mlir_namedtuple`——NamedTuple 风格的装饰器：

[quack/cute_dsl_utils.py:248-267](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cute_dsl_utils.py#L248-L267) 给类挂上四个协议方法。其重建逻辑见 `_namedtuple_new_from_mlir_values`：

[quack/cute_dsl_utils.py:176-199](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cute_dsl_utils.py#L176-L199) 遍历每个字段值，`None` 或静态值则原样保留（不消耗 MLIR 值），否则按其 MLIR 类型数量消耗对应个数的值并重建。

真实用例——两种风格各一：

[quack/gemm_base.py:229](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L229) `EpilogueParams = ParamsBase`，GEMM 的 epilogue 参数容器直接用 dataclass 基类。

[quack/tile_scheduler.py:225-232](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L225-L232) `TileSchedulerOptions` 用 `@mlir_namedtuple`，注意 `raster_order: cutlass.Constexpr[RasterOrderOption]` 是编译期常量字段（走 ConstNone 路径），而 `max_active_clusters: Int32` 是运行期字段。

#### 4.3.4 代码实践

**实践目标**：区分两种参数容器风格，并标注每个字段的静态/动态归属。

**操作步骤**：

1. 打开 [quack/tile_scheduler.py:225-232](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L225-L232)，对 `TileSchedulerOptions` 的每个字段，对照 `StaticTypes` 判断它是静态还是动态：
   - `max_active_clusters: Int32` —— ?
   - `raster_order: cutlass.Constexpr[RasterOrderOption]` —— ?
   - `tile_count_semaphore: Optional[cute.Pointer]` —— ?
   - `ag: Optional[AgSchedulerArguments]` —— ?
2. 打开 [quack/gemm_base.py:229](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L229)，确认 `EpilogueParams` 用的是 dataclass 风格而非 NamedTuple。
3. 回顾 `_patched_convert_single_arg`，解释 `Constexpr[T]` 字段为何在调用时要传 `None`。

**需要观察的现象**：

- 标了 `Constexpr[...]` 的字段是静态的（编译期烘焙），其余看实际值的类型是否落进 `StaticTypes`。
- 两种风格最终都实现了同一套「摊平 / 重建」协议，只是字段来源不同（dataclass 的 `fields()` vs NamedTuple 的位置迭代）。

**预期结果**：

- `raster_order` 是静态字段（Constexpr 注解）；`max_active_clusters`、`tile_count_semaphore`、`ag` 取决于其实际值是否为 `None` 或静态类型——非 None 时是动态字段。

> 这是源码阅读型实践，无需运行；结论可直接从代码与 `StaticTypes` 定义得出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_namedtuple_new_from_mlir_values` 在重建时要把 `None` 字段「原样保留」而不消耗 MLIR 值？

**参考答案**：`None` 表示「该字段不存在/未启用」（例如未传 colvec、未启用 AllGather）。这类字段在编译期模板里就是 `None`，运行期也没有对应的值要传递；若强行消耗 MLIR 值去重建它，会导致后续字段的值错位（off-by-N），在生成的 `llvm.call` 处触发操作数 arity 校验失败。

**练习 2**：`ParamsBase` 与 `mlir_namedtuple` 各自适合什么场景？

**参考答案**：`ParamsBase` 适合用 `@dataclass` 定义、字段可能带默认值且按名传递的容器（如各 SM 的 `EpilogueParams`，子类可加字段）；`mlir_namedtuple` 适合轻量、位置式、可作为值对象到处传递的参数包（如各种 `...Options` / `...Arguments`）。两者都实现同一套 JitArgument 协议，选型取决于是否需要继承与按名字访问。

---

## 5. 综合实践

把本讲三个模块串起来，完成规格中要求的核心任务：**用 `get_device_capacity` 解释 SM 编号如何驱动内核选择，并构造一个 `(sym, N)` 的 fake 张量说明 `divisibility` 的作用**。

**任务背景**：假设你要为新内核写一个 `compile` 静态方法（仿照 `Softmax.compile`），它需要①根据当前架构决定走哪条分支，②构造编译期符号张量。

**操作步骤**：

1. **架构驱动分支**。写一段「示例代码」，用 `QUACK_ARCH` 模拟不同 GPU，打印对应分支：
   ```python
   # 示例代码
   import os
   from quack.cute_dsl_utils import get_device_capacity

   def pick_branch():
       major = get_device_capacity()[0]
       if major < 9:
           return "SM8x: 无 cluster、无 TMA，走 cp.async 基础路径"
       elif major == 9:
           return "SM90: Hopper, TMA + WGMMA"
       elif major in (10, 11):
           return "SM100/110: Blackwell 数据中心, tcgen05 + TMEM"
       elif major == 12:
           return "SM120: Blackwell 消费级, warp MMA"
       return "unsupported"
   ```
   分别设 `QUACK_ARCH=8/90/100/120` 运行，对比输出，并对照 `gemm.py` 的 `sm_to_cls` 验证一致性。

2. **构造符号张量并解释 divisibility**。再写一段「示例代码」：
   ```python
   # 示例代码
   import cutlass.cute as cute
   from cutlass import BFloat16
   from quack.compile_utils import make_fake_tensor, div_for_dtype

   batch = cute.sym_int()
   N = 8192
   div = div_for_dtype(BFloat16)                       # = 128 // 16 = 8
   x_fake = make_fake_tensor(BFloat16, (batch, N), divisibility=div)
   print("stride =", x_fake.stride())
   print("div =", div, "意味着编译器确信每条加载可对齐到", div, "个元素 =", div * BFloat16.width // 8, "字节")
   ```
   运行后，用一句话写出：为什么把 batch 设成 `sym_int`、把 N 设成常量、再带上 `divisibility`，能让产物对任意 batch 复用、又能发射向量化加载。

3. **串联理解**。回顾 [quack/softmax.py:178-190](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L178-L190)，指出它同时用到了本讲的哪几个工具（`make_fake_tensor`、`div` 推导、`make_fake_stream`），并解释 `math.gcd(128 // dtype.width, N)` 相比直接 `div_for_dtype` 多了哪层保护。

**预期结果**：

- 步骤 1 的输出与 `sm_to_cls` 的键一一对应（8→SM80 路径，9→SM90，10/11→SM100，12→SM120）。
- 步骤 2 中 batch 为符号维、N 为常量、stride 带 divisibility=8 的提示，assumed_align = 16 字节；产物对任意 batch 复用，同时因 divisibility 提示可发射 128 位宽的向量加载。
- 步骤 3：`gcd` 防止当 N 不是 `128 // width` 的整数倍时「过度声明可整除性」。

> 步骤 1 在无 GPU 时可纯靠 `QUACK_ARCH` 完成（不触碰 CUDA）；步骤 2 依赖 cutlass-dsl 与（可能需要的）cross-compile 环境，若 import 失败则标注「待本地验证」。步骤 3 为纯源码阅读，无需运行。

## 6. 本讲小结

- `torch2cute_dtype_map` 是 PyTorch dtype ↔ cutlass 数值类型的翻译表，含 fp4/fp8 等特殊条目；`get_device_capacity()` 返回 `(major, minor)`，是 QuACK 所有架构分支（内核选类、配置空间、特性开关）的总开关，并用 `QUACK_ARCH` 支持无 GPU 交叉编译。
- `make_fake_tensor` 用「符号维 + leading_dim + divisibility」构造编译期张量，实现无张量编译；`assumed_align` 由 `divisibility * dtype.width // 8` 换算并对亚字节 dtype 下取整到 ≥1，`div_for_dtype` 固化了「128 位对齐所需的元素数」。
- `fake_batched` 是批次封装，揭示了「批次维前置跨 FFI、trace 时旋转到最后」的约定，因此 leading_dim 要 +1。
- `ParamsBase`（dataclass）与 `mlir_namedtuple`（NamedTuple）是两种 JIT 参数容器，都按 `StaticTypes` 把字段分流为静态（烘焙进 cubin）与动态（运行期传递），并实现「摊平 / 重建」的 JitArgument 协议；`Constexpr[T]` 字段经 TVM-FFI 补丁变成 `ConstNone`，调用时传 `None`。
- 这两个文件是全项目公共地基：归约家族与 GEMM 都依赖它们完成 dtype 翻译、架构分发与编译期符号张量构造。

## 7. 下一步学习建议

- 下一讲 **u3-l4 tile_scheduler 持久化内核调度** 会大量使用本讲的 `@mlir_namedtuple`（`TileSchedulerOptions`、`AgParams` 等），建议带着「静态/动态字段分流」的视角去读那些参数容器。
- 想看 `make_fake_tensor` 在更复杂场景的应用，可先读 **u4-l1 GEMM 编译与计划缓存** 中的 `_compile_gemm` 与 `make_fake_gemm_tensors`，观察多操作数、批次、varlen 下的符号张量构造。
- 想深入 `ParamsBase` 的实际用法，可在 **u5-l1 GemmBase 共享主循环** 与 **u6 可组合 Epilogue 系统** 中看到 `EpilogueParams` 如何承载 epilogue 参数。
- 若对编译产物如何被缓存复用感兴趣，回顾 **u2-l6** 并期待 **u8-l2 .o JIT 缓存与异步编译池**，那里会展开源码指纹与两级缓存的细节。
