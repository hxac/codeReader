# 循环与控制流

## 1. 本讲目标

学完本讲，你应当能够：

- 区分 TileLang 中五种主要循环原语——`T.Parallel`、`T.Pipelined`、`T.serial`、`T.Unroll`、`T.Persistent`——的语义与适用场景。
- 掌握 `T.Pipelined` 的 `num_stages`（软件流水）用法，理解它如何用多缓冲（multi-buffer）重叠「访存」与「计算」。
- 学会用 `T.ceildiv` 计算 tile 数量，并在同一个 kernel 里组合多种循环结构。
- 了解条件分支、`while`、`break`/`continue` 等控制流的写法与边界保护机制。

本讲承接 [u2-l1（T.Kernel 启动配置）](u2-l1-kernel-launch.md)：你已经会用 `T.Kernel` 把一个 threadblock 的启动配置写出来，本讲要回答的是「在这个 threadblock 内部，循环该怎么写、不同循环语义差别在哪」。

## 2. 前置知识

- **GPU 执行模型回顾**：一个 kernel 启动若干个 threadblock（grid 维度），每个 threadblock 内有若干线程（`threads` 维度）。`T.Kernel` 决定的是「块怎么发」，循环原语决定的是「块内的迭代怎么组织、由谁执行」。
- **tile / fragment / shared 的关系**（见 [u2-l2](u2-l2-tile-alloc.md)）：一次计算往往要把 global 上的大矩阵切成小块（tile）搬进 shared memory，再在 fragment（寄存器）上用 `T.gemm` 做矩阵乘。循环最常见的用途就是「沿某个维度迭代若干个 tile」。
- **软件流水（software pipelining）的直觉**：GPU 计算很快、显存搬运相对慢。如果第 `k` 步必须等搬运完成才开始计算，访存延迟就被白白浪费。软件流水的思路是：用多份共享内存缓冲，让「搬运第 `k+1` 个 tile」与「计算第 `k` 个 tile」同时进行，从而把访存延迟藏起来。
- **TIR / ForFrame**：TileLang 的所有 `T.*` 循环原语在前端都返回一个 TVM 的 `frame.ForFrame`（循环帧），它描述了「这是一个什么样的 for 循环」。真正的 GPU 代码要到后续编译 pass 里才会生成，所以前端写的是「循环语义」，不是最终的指令。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tilelang/language/loop.py` | **循环原语的权威实现**。`Parallel`、`Pipelined`、`Persistent`、`serial`、`unroll` 及大写别名 `Serial`/`Unroll` 都定义在这里，并从这里被 `tilelang/language/__init__.py` 导出。 |
| `tilelang/language/parallel.py` | `Parallel` 的单文件副本（与 `loop.py` 中的实现等价），便于单独查阅。 |
| `tilelang/language/pipeline.py` | `Pipelined` 的单文件副本（与 `loop.py` 中的实现等价）。 |
| `tilelang/language/v2/builder.py` | v2 前端构造器。当循环带 `step` 时，由这里的 `SerialForWithStep` / `UnrollForWithStep` 与 `ctx_for` 把步长翻译成合法的 TIR 循环。 |
| `tilelang/language/tir/ir.py` | `T.ceildiv` 等数学算子的导出处。 |
| `docs/programming_guides/control_flow.md` | 官方控制流使用指南，覆盖条件、循环、`while`、`break`/`continue`。 |
| `examples/quickstart.py` | matmul+relu 示例，集中展示了 `T.ceildiv`、`T.Pipelined`、`T.Parallel` 三者的真实用法。 |
| `examples/gemm/example_gemm_persistent.py` | 持久化 kernel 示例，展示了 `T.Persistent` 的真实用法，以及 `get_profiler().do_bench()` 的性能测量写法。 |

> 说明：`tilelang/language/__init__.py` 实际只从 `loop.py` 导入这些原语（见 [tilelang/language/__init__.py:18-26](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/__init__.py#L18-L26)）。`parallel.py` 与 `pipeline.py` 是内容等价的单用途副本，阅读时任选其一即可，但解释源码时以 `loop.py` 为准。

## 4. 核心概念与源码讲解

### 4.1 循环原语全景与 T.ceildiv

#### 4.1.1 概念说明

在 TileLang 里，「循环」不是普通 Python 循环，而是一类**带语义标注的 for 帧**。你写 `for k in T.Pipelined(...)` 时，`T.Pipelined(...)` 先构造一个 `ForFrame` 对象，Python 的 `for ... in` 语法把它交给前端，前端据此生成一个 TIR 的 `For` 节点，并在节点上打上「这是一个会被软件流水化的循环」之类的标记。后续编译 pass 会读这些标记，决定怎么生成最终代码。

五种循环可以按「执行方式」分为三类：

| 原语 | 执行方式 | 典型用途 |
| --- | --- | --- |
| `T.serial` | 一个块内**顺序**迭代 | 通用计数循环、持久化 kernel 里迭代 wave |
| `T.Unroll` | 编译期**展开**循环体 | 小循环展开以减少分支、增加指令级并行 |
| `T.Parallel` | **元素级并行**，循环体由块内线程并行执行 | elementwise 计算（如 relu、逐元素加） |
| `T.Pipelined` | **软件流水**，多缓冲重叠访存与计算 | GEMM / Attention 的 K 维 tile 迭代 |
| `T.Persistent` | **持久化线程块**，少量块循环处理所有 tile | 减少块调度开销、需要块间协作的场景 |

#### 4.1.2 核心流程

所有循环原语都遵循同一个「区间三参数」约定：`start`、`stop`、`step`。当只给一个参数时，它被解释为 `stop`，`start` 自动取 0、`step` 自动取 1。这个归一化逻辑在 `T.Pipelined`、`T.serial`、`T.unroll` 里都一样：

```text
T.Pipelined(K)            ⟹  start=0, stop=K, step=1
T.Pipelined(0, K, 2)      ⟹  start=0, stop=K, step=2  （仅 serial/unroll 支持 step）
```

计算 tile 数量时几乎一定会用到 `T.ceildiv(a, b)`，它等价于「向上取整除法」：

\[ \text{ceildiv}(a, b) = \left\lceil \frac{a}{b} \right\rceil \]

例如 `T.ceildiv(K, block_K)` 给出「K 维需要切多少个 tile」。之所以必须**向上取整**，是因为最后一个 tile 很可能是不满的（残余 tile），只有向上取整才能保证覆盖到最后一行/列。

#### 4.1.3 源码精读

`T.ceildiv` 在 `tilelang/language/tir/ir.py` 中通过 `_op_wrapper` 包装上游 TVM 的 `tir.ceildiv` 导出，和其他数学算子（`ceil`、`floor`、`exp` …）列在一起：

[tilelang/language/tir/ir.py:195-195](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/tir/ir.py#L195-L195) — 这一行把 `ceildiv` 注册为 `T.ceildiv`，调用时返回一个 TIR 表达式（编译期符号表达式），因此它既能出现在 `T.Kernel(...)` 的 grid 参数里，也能出现在循环范围里。

真实用法见 quickstart，`T.ceildiv` 一处用于算 grid 维度、一处用于算 K 维循环次数：

[examples/quickstart.py:17-17](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L17-L17) — `T.ceildiv(N, block_N)` 计算「列方向需要多少个 tile」，作为 `T.Kernel` 的 grid_x；

[examples/quickstart.py:28-28](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L28-L28) — `T.ceildiv(K, block_K)` 计算「K 维需要迭代多少个 tile」，作为 `T.Pipelined` 的迭代次数。

#### 4.1.4 代码实践

1. **目标**：验证 `T.ceildiv` 的「向上取整」语义。
2. **步骤**：在 Python 里（非 kernel 内，仅做直觉建立）对比 `T.ceildiv` 与 Python 整除，例如打印 `K=1024, block_K=32` 与 `K=1025, block_K=32` 两种情况下的 `T.ceildiv(K, block_K)` 求值结果。
3. **现象**：在 kernel 外直接 `import tilelang.language as T; print(int(T.ceildiv(1025, 32)))` 应得到 33（而非 32）。
4. **预期结果**：`1024/32=32`，`1025/32` 向上取整为 `33`，证明它会覆盖最后一个不满的 tile。
5. 若上述表达式无法在裸 Python 求值（取决于 TVM 版本对常量的折叠行为），请「待本地验证」，改在 kernel 内用 `T.print` 输出该值观察。

#### 4.1.5 小练习与答案

- **练习 1**：为什么计算 tile 数量必须用「向上取整」而不是普通整除 `//`？
  - **答案**：普通整除会向下取整，导致最后一个不满的 tile 被丢弃，输出矩阵的尾部行/列不会被计算。向上取整保证覆盖全部数据，残余 tile 由后续的边界保护 pass（`LegalizeSafeMemoryAccess`）自动加上越界判断。
- **练习 2**：`T.ceildiv` 在前端被翻译成什么？
  - **答案**：它被包装成 TVM 的 `tir.ceildiv` 表达式，是一个编译期符号表达式，因此可以出现在 `T.Kernel` 的 grid 维度、循环范围等需要 TIR 表达式的地方。

---

### 4.2 T.Parallel：元素级并行与多维权

#### 4.2.1 概念说明

`T.Parallel(ext0, ext1, ...)` 用来构造**元素级并行循环**：循环体的每次迭代之间互不依赖，可以直接分配给 threadblock 内的线程并行执行。它最常见的用法是写 elementwise 计算，比如对 fragment 逐元素做 relu、把两个张量逐元素相加。

它和「顺序循环」的本质区别在于**并行语义**：在 `T.Parallel` 的循环体里，你写的每一对 `(i, j)` 索引会被不同线程同时执行，因此循环体内部不能依赖「上一次迭代的结果」。

#### 4.2.2 核心流程

- 调用 `T.Parallel(M, N)` 会构造一层（或多层）嵌套的并行 for 帧。
- 循环头 `for i, j in T.Parallel(M, N)` 一次性接收所有维度的索引：`i ∈ [0, M)`、`j ∈ [0, N)`。
- 这些循环在 IR 中被标记为 `ForKind::kParallel`（编译期可在 `src/transform/lower_tile_op.cc`、`src/transform/layout_inference.cc` 等处看到对该 kind 的检查）。
- 布局推理（LayoutInference）会为每个 `T.Parallel` 循环推导出 fragment 的线程分布布局，记录在 `parallel_loop_layout` 这个 annotation 上，供 lowering 阶段生成正确的逐线程读写代码。

可选参数 `coalesced_width` 用于提示「最内层循环做内存合并访存的宽度」，帮助生成对显存更友好的访问顺序。

#### 4.2.3 源码精读

`Parallel` 的定义很短，核心是构造一个 annotation 字典并调用 FFI：

[tilelang/language/loop.py:13-33](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/loop.py#L13-L33) — 接收可变参数 `*extents`（1~N 维）与可选的 `coalesced_width`；当指定了 `coalesced_width` 时，把它写进 `annotations["coalesced_width"]`，最终通过 `_ffi_api.Parallel(extents, annotations)` 交给 C++ 侧构造并行 for 帧。

[examples/quickstart.py:41-42](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L41-L42) — 真实用法：对累加器 `C_local`（fragment）做逐元素 relu。`for i, j in T.Parallel(block_M, block_N)` 让块内线程并行处理 `block_M × block_N` 个元素，每个元素取 `T.max(., 0)`。

注意循环体里 `C_local[i, j]` 是**对 fragment 的逐元素读写**，这与 `T.Parallel` 的元素级并行语义完美契合——每个线程负责自己那一片 fragment 元素，互不干扰。

#### 4.2.4 代码实践

1. **目标**：用 `T.Parallel` 写一个 elementwise kernel，体会「多维权 + 并行」。
2. **步骤**：参考 quickstart，写一个 kernel `for i, j in T.Parallel(M, N): C[i,j] = A[i,j] + B[i,j]`（先用 `T.alloc_shared` 搬入再用 `T.Parallel` 写出，或直接对参数张量操作）。
3. **现象**：编译运行后与 `A + B` 的 PyTorch 结果用 `torch.testing.assert_close` 比对。
4. **预期结果**：逐元素相加正确，输出与参考一致。
5. 若想观察「并行语义」的影响，可尝试在循环体里写一个依赖上一次迭代的赋值（如 `C[i,j] = C[i-1,j] + ...`），编译器会报并行冲突——这说明 `T.Parallel` 要求迭代相互独立。该负面实验「待本地验证」。

#### 4.2.5 小练习与答案

- **练习 1**：`for i, j in T.Parallel(M, N)` 和写两层 `for i in T.serial(M): for j in T.serial(N):` 有什么本质区别？
  - **答案**：`T.Parallel` 标记了元素级并行语义，块内线程会并行执行各 `(i,j)`；而 `T.serial` 是顺序迭代，没有并行保证，性能与含义都不同。elementwise 计算必须用 `T.Parallel` 才能拿到并行加速。
- **练习 2**：`T.Parallel` 的循环体里能调用 `T.gemm` 吗？
  - **答案**：不应该。`T.gemm` 本身已经是一个 tile 级矩阵乘原语，内部自带 warp 划分（见 [u2-l3](u2-l3-compute-primitives.md)）；`T.Parallel` 适合的是**逐元素**、迭代独立的计算，二者用途不同。

---

### 4.3 T.Pipelined 与 num_stages 软件流水

#### 4.3.1 概念说明

`T.Pipelined` 是 TileLang 中**最重要**的循环原语之一，它是 GEMM / Attention 等 tile 循环的骨架。它把一个普通 for 循环标记为「可以被软件流水化」：编译器在后续 pass 中（`PipelinePlanning` → `InjectSoftwarePipeline`）把它改写成多缓冲结构，使「从 global 搬运下一个 tile」和「在 fragment 上计算当前 tile」重叠起来。

关键参数是 `num_stages`：它表示生产者和消费者之间**最多用几份缓冲**。`num_stages=0` 表示不启用流水，退化为普通顺序循环；`num_stages=N`（N≥2）启用多级流水。直观地说，`num_stages` 越大，能藏住的访存延迟越多，但代价是占用的 shared memory / 寄存器也越多。

#### 4.3.2 核心流程

一次软件流水化的 tile 循环（以 GEMM 的 K 维为例）大致是：

```text
prologue（预热）：先搬运前 (num_stages-1) 个 tile 进 shared 缓冲
main loop：每次迭代
    ├─ 启动下一份缓冲的搬运（cp.async / TMA，异步）
    ├─ 在当前缓冲上做 T.gemm 计算
    └─ 等待搬运完成、交换缓冲角色
epilogue（收尾）：处理最后剩下的 tile
```

理想情况下，搬运延迟被计算时间覆盖，总耗时近似为 `max(访存时间, 计算时间) × 迭代数`，而不是二者之和。多缓冲数量的下界关系可粗略表达为：要让访存完全被计算覆盖，至少需要

\[ n_{\text{stages}} \geq \left\lceil \frac{t_{\text{mem}}}{t_{\text{compute}}} \right\rceil \]

其中 \(t_{\text{mem}}\) 是搬运一个 tile 的时间、\(t_{\text{compute}}\) 是计算一个 tile 的时间。实际取值还要受 shared memory 容量约束——`num_stages` 越大，多缓冲占用的 shared memory 越多，可能挤占 block 数（occupancy）。

`T.Pipelined` 还提供了几个**高级**参数（`order`、`stage`、`sync`、`group`）用于精细控制缓冲排序、每条语句归属哪个流水级、同步点与分组。绝大多数情况下你只需要 `T.Pipelined(extent, num_stages=N)`，高级参数留给专家调优。

#### 4.3.3 源码精读

`Pipelined` 的签名与归一化逻辑：

[tilelang/language/loop.py:58-95](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/loop.py#L58-L95) — 注意 docstring 里明确写道：「`num_stages` is the max number of buffer used between pipeline producers and consumers. if num_stages is 0, pipeline will not be enabled.」；当只传一个位置参数时（`stop is None`），把它当作 `stop`、`start` 置 0；`order/stage/sync/group` 缺省为空列表。最终调用 `_ffi_api.Pipelined(...)` 构造流水 for 帧。

最典型的真实用法在 quickstart 里——一个标准的 tile 化 GEMM 内层循环：

[examples/quickstart.py:28-38](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L28-L38) — 这五行就是软件流水的心脏：
1. `for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=3)` —— K 维迭代 3 级流水；
2. `T.copy(A[...], A_shared)` —— 搬运 A 的当前 tile（生产者，会被异步化）；
3. `T.copy(B[...], B_shared)` —— 搬运 B 的当前 tile；
4. `T.gemm(A_shared, B_shared, C_local)` —— 在 shared 上做 tile 矩阵乘并累加到 fragment（消费者）。

`num_stages=3` 让三层「搬运 A、搬运 B、gemm 计算」可以重叠执行。

> 关于「搬运如何变异步」：`T.copy` 在 GPU 上会被 lowering 成 `cp.async` 或 TMA 指令，相关的 pass（如 `inject_ptx_async_copy`、`inject_tma_barrier`）会在更后面的阶段处理，本讲只关注**前端语义**，深层机制见 [u4-l2（软件流水线与异步拷贝）](u4-l2-software-pipeline.md)。

#### 4.3.4 代码实践（本讲主实践）

1. **目标**：用 `T.Pipelined` 写一个 matmul kernel，对比 `num_stages=1` 与 `num_stages=3` 的延迟差异，直观感受软件流水的收益。
2. **操作步骤**：
   1. 复制 `examples/quickstart.py` 为 `matmul_stages.py`。
   2. 把 `matmul` 改成接收一个 `num_stages` 参数，并把 `T.Pipelined(..., num_stages=3)` 里的硬编码 3 换成该参数。
   3. 分别以 `num_stages=1` 和 `num_stages=3` 编译同一个 kernel，用 `get_profiler().do_bench()` 测延迟：
      ```python
      for ns in (1, 3):
          kernel = matmul(M, N, K, block_M, block_N, block_K, num_stages=ns)
          profiler = kernel.get_profiler(tensor_supply_type=tilelang.TensorSupplyType.Normal)
          latency = profiler.do_bench()
          print(f"num_stages={ns}: {latency} ms")
      ```
   4. 参考写法见 [examples/gemm/example_gemm_persistent.py:114-116](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/gemm/example_gemm_persistent.py#L114-L116)，那里用 `profiler.do_bench(warmup=500)` 测延迟并换算 TFlops。
3. **需要观察的现象**：`num_stages=3` 的延迟应明显低于 `num_stages=1`（通常快一截），因为多缓冲把搬运藏到了计算背后。
4. **预期结果**：两次输出都应通过 `assert_allclose` / `torch.testing.assert_close` 的正确性校验，且 `num_stages=3` 延迟更低。
5. **进一步思考**：把 `num_stages` 加到 4、5，观察延迟是否继续下降——很可能在某个值后回升，因为 shared memory 占用过大导致 occupancy 下降。该拐点「待本地验证」（依赖具体 GPU 与 block 尺寸）。

> ⚠️ 注意：本实践需要真实的 CUDA 环境（含 GPU 与编译好的 `libtilelang.so`）。若你的环境无 GPU，请把它当作「源码阅读型实践」：阅读 quickstart 与 `example_gemm_persistent.py`，标注出 `T.Pipelined` 在循环里的位置，并写出你预期的 `num_stages` 对延迟的影响曲线。

#### 4.3.5 小练习与答案

- **练习 1**：`num_stages=0` 和 `num_stages=1` 有什么区别？
  - **答案**：根据 docstring，`num_stages=0` 时流水**完全不启用**，`T.Pipelined` 退化为顺序循环；`num_stages=1` 表示只用一份缓冲、没有重叠，行为接近顺序执行但已进入流水框架。
- **练习 2**：为什么 `num_stages` 不是越大越好？
  - **答案**：每多一级流水就要多分配一份 shared memory 缓冲，shared memory 总量有限；占用过大会降低每个 SM 能同时驻留的 block 数（occupancy），反而拖慢性能。最优值是「藏住访存延迟」与「不挤爆显存」之间的折中。
- **练习 3**：`T.Pipelined` 的循环体里通常包含哪两类语句？
  - **答案**：**生产者**（`T.copy` 把 global tile 搬进 shared）和**消费者**（`T.gemm` 在 shared/fragment 上做计算）。流水的意义正是让这两类语句在时间上重叠。

---

### 4.4 serial / Unroll / Persistent 循环变体

#### 4.4.1 概念说明

除了 `T.Parallel` 和 `T.Pipelined`，还有三种循环变体：

- **`T.serial`**：最朴素的顺序 for 循环，迭代按顺序执行，没有并行/展开/流水的特殊语义。它是「兜底」的通用循环，常用于持久化 kernel 里按 wave 迭代、或写需要顺序依赖的逻辑。`T.Serial` 是它的大写别名，强调「这是 tile 级循环」。
- **`T.Unroll`**（`T.unroll` 的大写强调形式）：请求编译器在编译期**展开**循环体。适合 trip count 较小、想减少分支开销、增加指令级并行的循环。它额外提供 `explicit`（完全展开）与 `unroll_factor`（按因子部分展开）两个专家旋钮。
- **`T.Persistent`**：构造**持久化线程块**循环。普通 kernel 里「一个 threadblock 处理一个 tile」，块数 = tile 数；持久化 kernel 则启动**固定数量**（通常等于 SM 数）的块，让每个块在一个循环里**反复处理多个 tile**。它的好处是减少硬件块调度开销，并能支持需要块间协作/通信的模式。

#### 4.4.2 核心流程

**serial / unroll 的步长处理**：两者都支持 `(start, stop, step)` 三参数形式。当 `step` 为 `None` 或 `1` 时，直接复用 TVM 的 `tb_tir.serial` / `tb_tir.unroll`；当 `step ≠ 1` 时，改用 TileLang 自定义的 `SerialForWithStep` / `UnrollForWithStep`，它会把带步长的区间折算成一个步长为 1 的等价区间，trip count 用 `ceildiv` 计算：

```text
real_stop = ceildiv(stop - start, step)   # step > 0
循环变量 = start + i * step                # i ∈ [0, real_stop)
```

**unroll 的两种模式**：默认 `explicit=False`。若设 `explicit=True`，请求**完全展开**（编译期把循环体复制 trip count 份）；若指定 `unroll_factor=N`，请求**按因子 N 部分展开**（注意 `unroll_factor` 与 `explicit=True` 互斥，二者不能同时启用）。

**Persistent 的执行模型**：

```text
启动 sm_num 个 threadblock（块数固定 = SM 数）
每个 block 在 T.Persistent 循环里反复取出下一个 tile 坐标 (bx, by)
处理该 tile（搬入 → gemm → 写回）
直到所有 tile 被处理完
```

它需要你传入 `domain`（tile 空间，如 `[m_blocks, n_blocks]`）、`wave_size`（块数/SM 数）、`index`（块索引变量，来自 `T.Kernel` 的块绑定），可选 `group_size`（默认 8，影响 tile 到 block 的映射分组）。

#### 4.4.3 源码精读

`serial` 的实现，注意 step 的分支：

[tilelang/language/loop.py:98-132](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/loop.py#L98-L132) — 当 `step is None` 或 `step==1` 时走 TVM 原生 `tb_tir.serial`；否则把 `(start, stop)` 归一化后返回一个 `SerialForWithStep`，把步长问题推迟到前端构造器里解决。

`unroll` 的实现，注意 annotation 的拼装：

[tilelang/language/loop.py:135-197](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/loop.py#L135-L197) — 它把 `explicit` 写进 `annotations["pragma_unroll_explicit"]`；当指定 `unroll_factor` 时把它写进 `annotations["pragma_unroll_factor"]`，并保证二者不冲突。最终同样在 `step` 为单位 1 时走 `tb_tir.unroll`，否则走 `UnrollForWithStep`。

带步长循环的真正翻译发生在 v2 构造器里：

[tilelang/language/v2/builder.py:299-323](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/builder.py#L299-L323) — 对 `SerialForWithStep` / `UnrollForWithStep`，先用 `tir.ceildiv` 算出等价的 `real_stop`（处理正负步长），再构造对应的 `tir.serial` / `tir.unroll` 帧，循环变量映射为 `start + i * step`。

`Persistent` 的定义：

[tilelang/language/loop.py:36-55](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/loop.py#L36-L55) — 接收 `domain`（tile 空间列表）、`wave_size`、`index`、`group_size`（默认 8），调用 `_ffi_api.Persistent(...)` 构造持久化 for 帧。

`T.Persistent` 的真实用法：

[examples/gemm/example_gemm_persistent.py:84-89](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/gemm/example_gemm_persistent.py#L84-L89) — `for bx, by in T.Persistent([T.ceildiv(M, block_M), T.ceildiv(N, block_N)], sm_num, block_id)`：domain 是 `[m_blocks, n_blocks]` 两个维度，`wave_size=sm_num`（SM 数），`index=block_id`（来自 `T.Kernel(sm_num, ...) as (block_id)`）。循环头直接解出当前要处理的 tile 坐标 `(bx, by)`，循环体仍是熟悉的「搬入 → `T.Pipelined` gemm → 写回」。

对比同一文件里的**非持久化**版本，它用普通 `T.Kernel` + `T.serial` 手写持久化逻辑：

[examples/gemm/example_gemm_persistent.py:51-60](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/gemm/example_gemm_persistent.py#L51-L60) — `T.Kernel(sm_num, ...) as (block_id)` 启动固定块数，再用 `for w in T.serial(waves)` 手动按 wave 迭代、自己计算 `tile_id` 与 `(bx, by)`。这正是 `T.Persistent` 想替你自动化的样板代码。

#### 4.4.4 代码实践

1. **目标**：用 `T.unroll` 替换一个小循环，观察 unroll hint 的作用；并对比「手写持久化」与「`T.Persistent`」两种写法。
2. **步骤**：
   1. 在一个已有 kernel 里，把某个小计数循环（如 `for kk in range(block_K_inner)`）改成 `for kk in T.unroll(block_K_inner)`，重新编译。
   2. 阅读 `example_gemm_persistent.py`，对照 `matmul_persistent`（手写 `T.serial` 持久化，[第 51-70 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/gemm/example_gemm_persistent.py#L51-L70)）与 `main_persistent_primitive`（`T.Persistent`，[第 84 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/gemm/example_gemm_persistent.py#L84-L89)），体会后者省去了手算 `tile_id` 的样板。
3. **现象**：`do_bench` 下，两种持久化写法延迟应接近；`T.Persistent` 版本的源码更短。
4. **预期结果**：功能一致（都通过 `assert_allclose`），`T.Persistent` 是更高级的写法。
5. unroll 对性能的影响「待本地验证」，取决于循环体大小与寄存器压力。

#### 4.4.5 小练习与答案

- **练习 1**：什么时候该用 `T.serial`，什么时候该用 `T.Parallel`？
  - **答案**：当循环迭代**相互独立**且是逐元素操作时用 `T.Parallel`（拿到并行）；当迭代**有顺序依赖**（如持久化里 `tile_id` 的累积、状态机推进）时用 `T.serial`。
- **练习 2**：`T.unroll` 的 `explicit=True` 和 `unroll_factor=4` 能同时用吗？
  - **答案**：不能。源码中指定 `unroll_factor` 时会校验 `pragma_unroll_explicit` 必须为 `False`（见 [loop.py:188-192](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/loop.py#L188-L192)），二者代表两种不同的展开策略（完全展开 vs 按因子部分展开）。
- **练习 3**：为什么持久化 kernel 要启动「等于 SM 数」的块？
  - **答案**：让每个 SM 恰好驻留一个块，块在循环里反复取 tile，避免硬件频繁调度/销毁块的开销；同时固定块数便于块间协作（如分布式通信、swizzle 调度）。

---

### 4.5 控制流补充：条件、while、break/continue

#### 4.5.1 概念说明

除了循环原语，TileLang 还支持常规控制流：`if/elif/else`、三元表达式、`while`、以及 Python 的 `break`/`continue`。这些写法和 Python 几乎一致，但有一个重要前提：**条件应当是 TIR 表达式**（如 `i < N`），普通 Python 布尔值会被当作编译期常量折叠。

还有一个值得注意的安全网：`LegalizeSafeMemoryAccess` pass 会在可能越界的访存处**自动插入保护判断**，并在能证明安全时**自动移除**判断。这意味着很多边界处理你不必手写 `if`。

#### 4.5.2 核心流程

- **条件分支**：`if i < N:` 里的 `i < N` 是 TIR 表达式，生成条件跳转；多条件可用 `T.all_of(...)` / `T.any_of(...)` 表达「且/或」。
- **while**：当条件是 TIR 表达式时支持；TileLang 会检测常真的死循环并报错。
- **break/continue**：可在 `T.serial` / `T.unroll` / `T.Parallel` / `while` 内使用，语义与 Python 一致。

#### 4.5.3 源码精读

控制流的权威说明在官方指南：

[docs/programming_guides/control_flow.md:16-46](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/programming_guides/control_flow.md#L16-L46) — 说明 `if/elif/else` 与三元表达式均受支持，条件应为 TIR 表达式；并明确指出 `LegalizeSafeMemoryAccess` pass 会自动处理越界保护。

while 与 break/continue 的说明：

[docs/programming_guides/control_flow.md:104-122](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/programming_guides/control_flow.md#L104-L122) — `while` 在条件为 TIR 表达式时可用，常真条件会被检测报错；`break`/`continue` 可在多种循环内使用，编译器会忽略 `break`/`continue` 之后的死路径。

真实条件分支示例见持久化 kernel，它用 `if` 处理残余 tile 边界：

[examples/gemm/example_gemm_persistent.py:62-62](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/gemm/example_gemm_persistent.py#L62-L62) — `if bx * block_M < M and by * block_N < N:` 只在 tile 落在矩阵范围内时才计算，跳过越界的 tile（这是手写持久化时需要的边界判断；`T.Persistent` 版本则把这类处理交给底层）。

#### 4.5.4 代码实践

1. **目标**：体会 `LegalizeSafeMemoryAccess` 自动边界保护。
2. **步骤**：写一个 `T.Parallel(M, N)` 的 elementwise kernel，故意让 grid 略大于矩阵（如 `T.ceildiv` 已经向上取整导致最后一个 tile 不满），**不写** `if` 边界判断，直接 `C[i,j] = A[i,j] + B[i,j]`。
3. **现象**：编译运行不崩溃、结果正确——因为 pass 自动给越界访问加了保护。
4. **预期结果**：与写了 `if T.all_of(...)` 的版本功能一致。
5. 若想确认保护确实被插入，可查看生成的 CUDA 源码（`kernel.get_kernel_source()`）中的边界判断，该现象「待本地验证」。

#### 4.5.5 小练习与答案

- **练习 1**：`if` 条件用普通 Python 布尔 `True` 和用 TIR 表达式 `i < N` 有什么不同？
  - **答案**：Python 布尔被当作编译期常量，会在编译期折叠（条件恒真/恒假）；TIR 表达式 `i < N` 则保留为运行时条件，生成真正的条件跳转。
- **练习 2**：为什么很多边界 `if` 可以省略？
  - **答案**：`LegalizeSafeMemoryAccess` pass 会在可能越界的访存处自动插入保护、在能证明安全时自动移除，所以简单的边界处理交给编译器即可；只有需要自定义边界逻辑时才手写 `if`。

---

## 5. 综合实践

把本讲的三种核心循环串起来，完成一个**带边界处理的 tile 化 GEMM**：

1. 用 `T.ceildiv` 计算 grid 维度与 K 维 tile 数。
2. 用 `T.Pipelined(T.ceildiv(K, block_K), num_stages=N)` 做主计算循环，内含 `T.copy` ×2 + `T.gemm`。
3. 用 `T.Parallel` 在循环外对累加器做逐元素激活（如 relu）。
4. 用 `get_profiler().do_bench()` 测量 `num_stages` 取 1、2、3、4 时的延迟，画出「num_stages–延迟」曲线，找到你机器上的拐点。
5. （进阶）把同一个 kernel 改写成 `T.Persistent` 版本，对比与普通 `T.Kernel` 版本的延迟（参考 `example_gemm_persistent.py`）。

验收标准：所有 `num_stages` 取值都通过正确性校验；能用自己的话解释曲线为什么在某个 `num_stages` 后不再下降甚至回升（shared memory / occupancy 折中）。

## 6. 本讲小结

- TileLang 的循环是**带语义标注的 for 帧**：`T.Parallel`（元素级并行）、`T.Pipelined`（软件流水）、`T.serial`（顺序）、`T.Unroll`（展开）、`T.Persistent`（持久化块），它们最终都通过 `_ffi_api` 交给 C++ 构造 TIR `For` 节点。
- `T.ceildiv(a, b)` 是计算 tile 数量的标准工具，**向上取整**保证覆盖残余 tile；它产生的是编译期 TIR 表达式。
- `T.Pipelined` 的 `num_stages` 控制多缓冲数量：越大越能藏访存延迟，但 shared memory 占用也越大，存在 occupancy 拐点；`num_stages=0` 退化为不流水。
- `T.serial`/`T.unroll` 支持 `(start, stop, step)`，带步长时由 `SerialForWithStep`/`UnrollForWithStep` 用 `ceildiv` 折算成单位步长循环；`unroll` 有 `explicit`（完全展开）与 `unroll_factor`（部分展开）两种互斥模式。
- `T.Persistent` 让固定数量（通常 = SM 数）的块循环处理所有 tile，省去手算 `tile_id` 的样板，并支持块间协作。
- 控制流（`if`/`while`/`break`/`continue`）条件需为 TIR 表达式；`LegalizeSafeMemoryAccess` pass 会自动处理大部分越界保护。

## 7. 下一步学习建议

- **进入编译流水线**：本讲只讲了循环的**前端语义**。`T.Pipelined` 如何被 `PipelinePlanning` / `InjectSoftwarePipeline` 改写成多缓冲、`T.copy` 如何变成 `cp.async`/TMA，请学习 [u3（编译流水线与 IR）](u3-l1-compile-overview.md)，尤其是 [u3-l3（LowerAndLegalize）](u3-l3-lower-legalize.md) 与 [u3-l4（OptimizeForTarget）](u3-l4-optimize-target.md)。
- **深入软件流水**：想彻底搞懂 `num_stages` 背后的多缓冲与异步拷贝机制，请学习 [u4-l2（软件流水线与异步拷贝）](u4-l2-software-pipeline.md)。
- **持久化的分布式应用**：`T.Persistent` 在分布式/通信 overlap 场景里非常有用，学完 [u6（分布式编程）](u6-l1-distributed-overview.md) 后可以回头看持久化 kernel 如何与通信原语配合。
- **建议阅读源码**：重读 `tilelang/language/loop.py` 全文（不足 230 行），对照本讲把每种循环的签名与归一化逻辑在脑子里走一遍；再读 `examples/gemm/example_gemm_persistent.py`，对比同一算法的普通/手写持久化/`T.Persistent` 三种写法。
