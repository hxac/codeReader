# TLOAD/TSTORE：数据进出 tile 的门户

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 TLOAD/TSTORE 的完整签名、参数含义与主要约束（tile 类型、布局匹配、dtype 一致、有效区）。
2. 讲清楚 GM（GlobalTensor）与 tile 存储（UB/L1）之间的数据通路：谁提供寻址元数据、谁真正搬数据。
3. 对照读同一对指令的两套实现——CPU 仿真的逐元素循环 与 NPU 的 burst 级搬运 intrinsic——理解「同一语义、两种落地」。
4. 独立完成一次「TLOAD 加载 [128,128] 矩阵 → 乘以 2 → TSTORE 写回」的搬运+计算+写回闭环。

## 2. 前置知识

本讲默认你已完成单元一、单元二，这里快速回顾三个关键概念：

- **GlobalTensor**：全局内存（GM）数据的「零拷贝视图」，只保存 `__gm__` 指针 + 5 维 shape/stride 元数据，本身不搬数据。真正读写 GM 的是本讲要讲的 TLOAD/TSTORE 等搬运指令（见 u2-l1）。
- **Tile**：片上固定容量的 2-D 缓冲，由 `TileType` 决定它落在哪级存储——`Vec` 在 UB（Unified Buffer，向量/搬运用片上缓冲）、`Mat` 在 L1、`Acc` 是 Cube 累加器。Tile 的容量形状（Rows×Cols）编译期静态，有效区（valid region）运行期可变，指令只在有效区内定义语义（见 u2-l2）。
- **事件同步**：TLOAD 走 MTE2（搬入）流水线，TSTORE 走 MTE3（搬出）流水线，与 Vector/Cube 流水线并行执行，跨流水线依赖要用 set/wait 事件表达（见 u2-l3）。TLOAD/TSTORE 的 C++ API 已经把「等待事件 + 执行 + 返回记录事件」打包好了。

一个直觉比喻：GlobalTensor 是「地图上的地址」，Tile 是「你手里的箱子」，TLOAD 是「把货物从仓库装进箱子」，TSTORE 是「把箱子里的货放回仓库」。装多少，由箱子上的有效区标签决定。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/isa/TLOAD.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TLOAD.md) | TLOAD 的 ISA 文档：语义、约束、Auto/Manual 示例 |
| [docs/isa/TSTORE.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TSTORE.md) | TSTORE 的 ISA 文档：语义、约束、原子写与量化变体 |
| [include/pto/common/pto_instr.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp) | 用户可见的指令 API 层：`TLOAD`/`TSTORE` 薄壳（TSYNC + 转发 IMPL） |
| [include/pto/cpu/TLoad.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TLoad.hpp) | TLOAD 的 CPU 仿真实现（逐元素循环拷贝） |
| [include/pto/cpu/TStore.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TStore.hpp) | TSTORE 的 CPU 仿真实现（逐元素写回 + 可选量化/原子加） |
| [include/pto/npu/a2a3/TLoad.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TLoad.hpp) | A2/A3 真机上的 TLOAD 实现（burst 级 GM→UB / GM→L1 intrinsic） |
| [include/pto/npu/a2a3/TStore.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TStore.hpp) | A2/A3 真机上的 TSTORE 实现（UB→GM / 累加器→GM） |
| [include/pto/common/arch/memory/tload_common.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch/memory/tload_common.hpp) | 跨架构共享的 TLOAD 布局分发骨架（ND/DN/NZ 路由） |
| [tests/cpu/st/testcase/tload/](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tload/tload_kernel.cpp) | TLOAD 的 CPU ST 用例（ND/DN/NZ、静态/动态、多种 PadValue） |

## 4. 核心概念与源码讲解

### 4.1 TLOAD 语义：把 GM 数据装进 tile

#### 4.1.1 概念说明

TLOAD 是 PTO 的「搬入门户」：把一个 GlobalTensor 视图描述的 GM 数据搬进一个 Tile。它的数学语义在 ISA 文档里写成（以 2-D 视角、带基址偏移）：

\[ \mathrm{dst}_{i,j} = \mathrm{src}_{r_0 + i,\; c_0 + j} \]

即 tile 的第 \((i,j)\) 个元素等于 GM 中对应位置的元素。注意三点：

1. **地址从哪来**：GlobalTensor 的 shape/stride 元数据决定 GM 侧怎么寻址；Tile 的布局（BLayout/SLayout）决定片上怎么摆放。TLOAD 负责两侧的对齐映射。
2. **搬多少**：由 tile 的**有效区**（`GetValidRow()`/`GetValidCol()`）决定，不是容量形状。这是尾块不越界的关键。
3. **搬完之后**：TLOAD 返回一个 `RecordEvent`，供后续 Vector/Cube 指令作为等待事件，表达「MTE2 搬完 → 计算才能开始」的依赖。

#### 4.1.2 核心流程

调用 `TLOAD(dst, src, events...)` 时：

```text
TSYNC(events...)          # 先折叠等待传入的前序事件（如上一轮 TSTORE 搬出完成）
TLOAD_IMPL(dst, src)      # 按后端路由到 cpu/ 或 npu/<arch>/ 的实现
return RecordEvent        # 返回本指令的记录事件（挂在 MTE2 流水线上）
```

实现内部（以普通 tile 为例，卷积 tile 走 CONVTILE 分支）：

```text
检查（CheckTileData）
  ├── 编译期 static_assert：dtype 宽度一致、布局在支持列表内
  └── 运行期 assert：GlobalTensor shape 与 tile 有效区的乘积关系匹配
填充 padding：先把整个 tile 缓冲填成 PadValue（Null/Zero→0，Min/Max→±inf 或极值）
逐元素搬运：for row in [0, validRow) for col in [0, validCol)
           dst.SetElement(row, col, src.data()[offset(row, col)])
```

其中 `offset(row, col)` 由 GlobalTensor 的 5 维 shape/stride 计算得出——这正是「视图提供寻址元数据」的落点。

#### 4.1.3 源码精读

**① 用户 API 层**——[include/pto/common/pto_instr.hpp:L217-L223](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L217-L223)：`TLOAD` 薄壳先 `TSYNC` 折叠等待事件，再经 `MAP_INSTR_IMPL` 拼接成 `TLOAD_IMPL` 转发给具体后端，最后返回空的 `RecordEvent`。这印证了 u2-l4 讲过的三段式骨架——指令层不含任何实现逻辑。

**② CPU 仿真的填充与搬运**——[include/pto/cpu/TLoad.hpp:L126-L152](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TLoad.hpp#L126-L152)：`TLOAD_TILE_IMPL` 先做 `CheckTileData`，再用 `std::fill` 把整个 tile 填成 `getPadValue<TileData>()`，然后双重循环内通过 `MapTileIndicesToGlobalOffset` 把 (row, col) 映射到 GM 偏移并 `SetElement` 写入 tile。CPU 仿真刻意选了最朴素的双重循环——正确性优先，不考虑性能。

**③ padding 值的语义**——[include/pto/cpu/TLoad.hpp:L20-L50](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TLoad.hpp#L20-L50)：`getPadValue` 按 tile 的 `PadVal` 模板参数返回填充值。浮点类型 `Min/Max` 映射到 \(\pm\infty\)（配合 Exp/Max 等 attention 场景），定点类型映射到数值极限，`Null/Zero` 一律填 0。这解释了 ST 用例里 `case_float_GT_128_127_..._PADMAX` 这类命名：列数 127 不满 128 时，尾部一列会被填成 `+inf`。

**④ 运行期形状检查**——[include/pto/cpu/TLoad.hpp:L52-L82](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TLoad.hpp#L52-L82)：ND/DN 布局下断言「前三维（batch 维）乘积 × DIM_3 == validRow 且 DIM_4 == validCol」（行主序），NZ 布局下断言分形块尺寸匹配。这些断言把 ISA 文档 Constraints 一节的要求变成可执行的检查。

**⑤ ISA 约束速查**——[docs/isa/TLOAD.md:L47-L77](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TLOAD.md#L47-L77)：A2A3 上目标 tile 只能是 `Vec` 或 `Mat`；`Vec` 只支持同构布局 ND→ND / DN→DN / NZ→NZ；`Mat` 额外支持 ND→NZ、DN→ZN（在 L1 里顺便做分形重排）；int64/uint64 只走 ND/DN；Vec 行数上限 4095、Mat 行数上限 16384。这些数字在 NPU 实现里都有对应的 `PTO_ASSERT`。

**⑥ 文档示例**——[docs/isa/TLOAD.md:L100-L119](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TLOAD.md#L100-L119)：Manual 示例给出最短可用序列——`GTensor gin(in); TileT t; TASSIGN(t, 0x1000); TLOAD(t, gin);`。TASSIGN 把 tile 绑到片上缓冲地址（下一讲 u3-l2 详讲），TLOAD 只管搬。

#### 4.1.4 代码实践

**实践目标**：跑通仓库自带的 TLOAD ST 用例，从测试断言反推指令行为。

1. 操作步骤：
   - 进入仓库根目录，执行 `python3 tests/run_cpu.py --help` 确认过滤参数写法（脚本用法以 `--help` 输出为准）。
   - 用过滤参数只运行 `tload` 用例（例如 `--filter tload` 或按脚本提示的等价写法，具体参数名以 `--help` 为准）。
   - 打开 [tests/cpu/st/testcase/tload/main.cpp:L136-L160](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tload/main.cpp#L136-L160)，对照 13 个 `TEST_F` 用例名读 [tload_kernel.cpp:L212-L314](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tload/tload_kernel.cpp#L212-L314) 里的 13 个 `launchTLOAD_*`。
2. 需要观察的现象：全部用例 PASS；`case 3/4/5`（列数 127 + PADMAX/PADMIN）这类用例能通过，说明 padding 逻辑生效。
3. 预期结果：`run_cpu.py` 先执行 `gen_data.py` 建目录，再编译并运行 gtest，输出 `[ PASSED ] 13 tests`（数量以当前仓库为准）。
4. 若本机未装 GCC≥13 或 CMake，先按 u1-l3 的环境步骤补齐；运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：一个 `Tile<Vec, float, 128, 128>` 的有效区是 (128, 127)，`PadVal = PadValue::Max`，TLOAD 后 tile 中第 127 列的值是什么？
**答案**：`+inf`。搬运前整个 tile 被 `getPadValue` 填充，`Max` 对浮点类型返回 `std::numeric_limits<float>::infinity()`，有效列只有 127 列，第 127 列（0 起下标第 127 列即第 128 列）保持填充值不被覆盖。

**练习 2**：为什么 TLOAD 的 C++ 接口要返回 `RecordEvent`？
**答案**：TLOAD 挂在 MTE2 流水线上，与 Vector/Cube 流水线并行执行；后续计算指令必须等数据搬完才能开始。返回的 `RecordEvent` 作为入参传给计算指令，后者内部的 `TSYNC` 会把它翻译成 wait_flag，形成显式的跨流水线依赖（u2-l3 的事件三元组机制）。

**练习 3**：CPU 仿真下不写任何事件同步，程序结果也正确，为什么？
**答案**：CPU 仿真把 set/wait 做成空桩、单线程按书写顺序执行指令（见 u2-l4 的 `cpu_stub.hpp`），天然串行所以结果正确；但这不代表真机也正确——真机上 MTE2/V/MTE3 并行，缺事件会读到未搬完的数据。CPU 仿真只验证功能逻辑。

### 4.2 TSTORE 语义：把 tile 写回 GM

#### 4.2.1 概念说明

TSTORE 是「搬出门户」：把 Tile 有效区内的数据写回一个 GlobalTensor 视图。数学语义为：

\[ \mathrm{dst}_{r_0 + i,\; c_0 + j} = \mathrm{src}_{i,j} \]

除了基本形式，TSTORE 还有三类工程变体（都在 ISA 文档的 intrinsic 声明里）：

- **原子写**：模板参数 `AtomicType::AtomicAdd`，多核写同一块 GM 时做原子累加（多核规约协议的基础）。
- **标量预量化**：`TSTORE(dst, src, preQuantScalar)`，写回时把累加器数据按 scale 量化成 int8/uint8 等。
- **向量预量化 / TSTORE_FP**：每列一个 scale（来自一个专门的 scale tile）。

注意参数顺序与 TLOAD 相反：TLOAD 是 `TLOAD(dst_tile, src_gm)`，TSTORE 是 `TSTORE(dst_gm, src_tile)`——方向始终是「第一个参数是目的地语义上的接收端」这一约定的差异容易写反，靠模板类型检查兜底。

#### 4.2.2 核心流程

```text
TSYNC(events...)                # 等待前序事件（通常是 Vector/Cube 计算完成）
按 TileType 分派：
  ├── Vec  → 逐 burst 把 UB 写回 GM（copy_ubuf_to_gm）
  ├── Mat → 同上（L1 → GM）
  └── Acc → 走 copy_matrix_cc_to_gm，可在写回途中做量化/ReLU（set_quant_pre）
有效区裁剪：只写 validRow × validCol，越界部分不落 GM
可选：AtomicAdd 时打开硬件原子加，写完关闭
```

一个容易忽略的细节：**Acc 累加器直接写 GM 的类型转换是受限的**。A2A3 上 `float` 累加器可以写 `float/half/bfloat16_t`，`int32_t` 累加器只能写 `int32_t`；带量化时才能落 `int8_t/uint8_t`。这些组合在实现里是 `static_assert`，写错编译期就报错。

#### 4.2.3 源码精读

**① CPU 仿真的核心循环**——[include/pto/cpu/TStore.hpp:L41-L79](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TStore.hpp#L41-L79)：`TStore` 双重循环内 `GetElement` 读 tile、`ConvertStoreValue` 做可选量化/ReLU 转换、`MapTileIndicesToGlobalOffset` 算 GM 偏移，最后按 `atomicType` 选择 `AddToElement`（原子加，host 上退化为读-加-写）或 `SetElement`。与 TLOAD 的 CPU 实现完全对称，只是方向反过来。

**② CPU 实现的分发与量化入口**——[include/pto/cpu/TStore.hpp:L118-L168](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TStore.hpp#L118-L168)：`TSTORE_IMPL` 先用 `static_assert` 限定 GM 布局（ND/DN/NZ/NC1HWC0/NDC1HWC0），卷积 tile 走 `TStoreConv`；`preQuantScalar` 重载把标量广播成逐列 `scalars` 向量再进核心循环。

**③ ISA 文档的 Acc 类型矩阵**——[docs/isa/TSTORE.md:L69-L79](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TSTORE.md#L69-L79)：Acc→GM 的 dtype 支持表（如 `float` 源 + 无量化 → `float/half/bfloat16_t`；带量化 → `int8_t/uint8_t`）。表里每一行都对应实现里的一条 `static_assert`。

**④ 约束速查**——[docs/isa/TSTORE.md:L57-L81](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TSTORE.md#L57-L81)：源 tile 只能是 `Vec/Mat/Acc`；Vec/Mat 要求源和目标 dtype 宽度一致、布局匹配（ND/DN/NZ）；静态形状约束 `1 <= Cols <= 4095`，ND 行数上限 8192、NZ 类行数上限 65535 且列数 16 对齐。

**⑤ 文档示例**——[docs/isa/TSTORE.md:L129-L148](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TSTORE.md#L129-L148)：Manual 示例演示了原子写模板参数写法 `TSTORE<TileT, GTensor, AtomicType::AtomicAdd>(gout, t);`。

#### 4.2.4 代码实践

**实践目标**：通过「把 Add 示例的 TADD 换成 TSUB 仍能跑通」这件事（u1-l4 已做），反过来确认 TSTORE 的写回路径对任意 Vec 计算都是通用的——本实践改为一个纯阅读任务。

1. 实践目标：弄清 TSTORE 三类变体各自适用的一行场景。
2. 操作步骤：
   - 在 [include/pto/common/pto_instr.hpp:L318-L399](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L318-L399) 中数一数 `TSTORE`/`TSTORE_FP` 共有多少个重载，逐个标注它多带了哪个模板参数（`STPhase`/`AtomicType`/`ReluPreMode`）。
   - 用 Grep 在 `kernels/manual/a2a3/gemm_performance` 中搜索 `AtomicAdd`，看真实算子是否用到原子写。
3. 需要观察的现象：API 层重载数量明显多于「基本写回」一种，说明搬运指令承担了写回 + 量化 + 原子三种职责。
4. 预期结果：能列出至少 3 个重载并说出差异；gemm 类算子通常不用 AtomicAdd，而多核累加/分布式优化器类算子会用到（具体搜索结果**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 TSTORE 的有效区裁剪很重要？举一个具体场景。
**答案**：多核按行切分时，尾块的行数往往不是 tile 行数的整数倍，例如总行 1000、每核 tile 128 行，尾块只有 104 行有效。若不做裁剪，尾核会把 tile 里 24 行陈旧数据写回 GM，覆盖别的核已写好的数据。TSTORE 只写 `validRow × validCol` 保证了正确性。

**练习 2**：`TSTORE(gm_int8, accTile_float, preQuantScalar)` 和 `TSTORE(gm_half, accTile_float)` 有什么本质区别？
**答案**：前者是带标量预量化的写回——累加器中的 float 先乘/缩放 `preQuantScalar` 再量化成 int8；后者是普通类型转换写回，只做 float→half 的精度截断。量化路径在实现里会先 `set_quant_pre` 设置硬件量化参数，并在 `CheckAcc2gm<..., true>` 中走另一组 dtype 断言。

### 4.3 CPU/NPU 双实现对比：同一语义的两种落地

#### 4.3.1 概念说明

PTO 的核心承诺是「一份 kernel 源码，CPU 仿真与真机都能编译」。TLOAD/TSTORE 是观察这一承诺如何兑现的最佳标本：**两条指令在两个后端的实现策略完全不同，但对外语义逐条对齐**。

| 维度 | CPU 仿真（include/pto/cpu） | NPU A2/A3（include/pto/npu/a2a3） |
| --- | --- | --- |
| 搬运方式 | 逐元素 for 循环 + `SetElement`/`GetElement` | burst 级 DMA intrinsic（`copy_gm_to_ubuf_*` 等） |
| 存储层级 | host 内存里模拟的平坦数组 | 真实 UB（`__ubuf__`）/L1（`__cbuf__`）/GM 地址空间 |
| padding | `std::fill` 手动填 PadValue | `pto_set_tload_pad_val` 交给硬件填充 |
| 布局转换 | `MapTileIndicesToGlobalOffset` 换算下标 | 专用路径（如 `TLoadGm2L1Nd2nz` 用 `TLoadNd2nzInstr` 让 MTE 硬件边搬边重排） |
| 同步 | 空桩（单线程按序） | 真实 flag/事件硬件语义 |
| 目标 | 功能正确、可调试 | 性能 |

#### 4.3.2 核心流程

NPU 侧 TLOAD 的分派链（以普通 tile 为例）：

```text
TLOAD_TILE_IMPL (a2a3/TLoad.hpp + common/arch/memory/tload_common.hpp)
  ├── CheckNormalTileData：dtype/TileType/布局 static_assert + 运行期形状断言
  ├── TileType::Vec → TLoadGm2ub   → 按 ND/DN/NZ 路由 → copy_gm_to_ubuf_*
  └── TileType::Mat → TLoadGm2L1   → 按 ND/DN/NZ 路由 → copy_gm_to_cbuf
                                    └── ND2NZ/DN2ZN 额外走分形搬运
```

DMA intrinsic 的参数（`nBurst`/`lenBurst`/`gmGap`/`ubGap`）是理解性能的关键：一次 burst 是一整段连续内存，`nBurst` 是 burst 个数、`lenBurst` 是每段长度、两个 gap 是相邻 burst 之间 GM 侧与片上侧的跳空（按 32 字节块计）。实现的工作就是把 shape/stride 折算成这四个数。

#### 4.3.3 源码精读

**① NPU 的 burst 搬运封装**——[include/pto/npu/a2a3/TLoad.hpp:L20-L44](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TLoad.hpp#L20-L44)：`TLoadInstrGm2ub` 按 dtype 宽度（1/2/4/8 字节）选择 `copy_gm_to_ubuf_align_b8/b16/b32`——对齐宽度必须等于数据宽度的最小对齐粒度；`TLoadInstrGm2L1` 则统一走 `pto_copy_gm_to_cbuf`。对照 CPU 版的逐元素循环，能直观感受到「仿真求对、真机求快」。

**② NPU 的 shape/stride → burst 参数折算**——[include/pto/npu/a2a3/TLoad.hpp:L100-L136](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TLoad.hpp#L100-L136)：`TLoadGm2L1Nd2nd` 中，`nBurst = gShape3`（行数）、`lenBurst = validCol × sizeof(DType) >> 5`（每行字节数折成 32B 块）、`gmGap = (gStride3 - gShape4) × sizeof >> 5`（GM 侧行间跳空）、`l1Gap = (TileData::Cols - validCol) × sizeof >> 5`（片上侧列 padding 跳空），随后三层循环按维展开调用 DMA。这就是「leading dimension 决定 gap」的代码体现——为什么 u2-l1 强调 ld 类步长必须留动态：它直接进 burst 参数。

**③ 跨架构共享的布局分发**——[include/pto/common/arch/memory/tload_common.hpp:L332-L354](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch/memory/tload_common.hpp#L332-L354)：`TLOAD_TILE_IMPL` 先做编译期检查（dtype 白名单、`Vec/Mat` 限制、b64 只许 ND/DN），再按 `TileData::Loc` 分派到 `TLoadGm2ub`（[L152-L173](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch/memory/tload_common.hpp#L152-L173)，Vec→UB）或 `TLoadGm2L1`（[L217-L250](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch/memory/tload_common.hpp#L217-L250)，Mat→L1），内部再按 tile 布局路由到 Nd2nd/Dn2dn/Nz2nz。这份骨架放在 common 层供多架构复用，而 a2a3 目录补充架构特有的路径（5HD、FRACTAL_Z 等卷积布局）。

**④ NPU 的 TSTORE 分派**——[include/pto/npu/a2a3/TStore.hpp:L239-L287](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TStore.hpp#L239-L287)：`TSTORE_IMPL` 同样三路分派——`Vec` 走 `TStore`（UB→GM burst，见 [L17-L29](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TStore.hpp#L17-L29) 的 `copy_ubuf_to_gm_align_*`）、`Acc` 走 `TStoreAcc`（累加器直写 GM，NZ→NZ 路径在 [L98-L159](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TStore.hpp#L98-L159) 中手工拼 `xmReg/xtReg` 控制字调用 `copy_matrix_cc_to_gm`，把量化模式、ReLU、通道拆分编码进寄存器位域）、`Mat` 走 `TStoreMat`；`AtomicAdd` 用 `SetAtomicAdd/SetAtomicNone` 包夹。对照 CPU 版 [include/pto/cpu/TStore.hpp:L41-L79](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TStore.hpp#L41-L79)，同一个 `atomicType` 模板参数在两端落地成完全不同的机制，但对用户是同一个写法。

**⑤ 双端入口对照**——[include/pto/cpu/TLoad.hpp:L185-L193](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TLoad.hpp#L185-L193) 与 [include/pto/npu/a2a3/TLoad.hpp:L321-L329](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TLoad.hpp#L321-L329)：两个后端的 `TLOAD_IMPL` 签名逐字相同，函数体一个是循环、一个是 intrinsic 分派。`pto_instr_impl.hpp` 按「架构 × 后端」互斥 include 其中之一（u2-l4），这就是「一份 kernel 源码双端编译」的全部秘密。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：完成「TLOAD 加载 [128,128] float 矩阵 → 每元素乘 2 → TSTORE 写回」闭环，并跑通验证。

1. 操作步骤：
   - 复制 `demos/baseline/add` 目录（或复制 `tests/cpu/st/testcase/tadd`）为新目录，例如 `tests/cpu/st/testcase/tmul2`。
   - 参考下面示例代码改写 kernel（以下为**示例代码**，以仓库中 tadd 用例为骨架）：

     ```cpp
     // 示例代码：加载 [128,128] → ×2 → 写回（ND 布局、Vec tile）
     using TileT = Tile<TileType::Vec, float, 128, 128>;
     using GShape = Shape<1, 1, 1, 128, 128>;
     using GStride = BaseShape2D<float, 128, 128, Layout::ND>;
     using GTensor = GlobalTensor<float, GShape, GStride, Layout::ND>;

     GTensor gin(in), gout(out);
     TileT t;
     TASSIGN(t, 0);
     TLOAD(t, gin);              // GM → tile（有效区 = 128×128）
     auto ev = TSYNC();          // 事件风格可省略，裸指令下依赖编译器/真机约束
     TMULS(t, t, 2.0f, ev);      // 逐元素乘标量，声明见 pto_instr.hpp 的 TMULS 重载
     TSTORE(gout, t);            // tile → GM
     ```

     - 同步写法请对照你所选模板用例的原始事件编排，真机上缺事件会出错，CPU 仿真则总是通过。
   - golden 侧把参考实现改成 `golden[i] = input[i] * 2.0f`。
   - 用 `python3 tests/run_cpu.py`（加用例过滤参数，见 4.1.4）运行新用例。
2. 需要观察的现象：输出与逐元素 ×2 的 golden 完全一致；故意把 golden 写成 ×3 时用例应当 FAIL，证明比对机制真的在工作。
3. 预期结果：新用例 PASS；两次 TLOAD/TSTORE 的语义与 [docs/isa/TLOAD.md:L14-L16](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TLOAD.md#L14-L16)、[docs/isa/TSTORE.md:L14-L16](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TSTORE.md#L14-L16) 的数学解释一一对应。
4. 本实践依赖本地 C++20 工具链与 `run_cpu.py` 脚本，运行结果**待本地验证**；无法运行时退化为阅读型实践：把上面的示例与 [tests/cpu/st/testcase/tload/tload_kernel.cpp:L139-L161](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tload/tload_kernel.cpp#L139-L161) 的 `runTLOADND` 逐行对照，确认 tile 类型/shape/stride 三件套的构造方式一致。

#### 4.3.5 小练习与答案

**练习 1**：为什么 CPU 仿真可以用 `MapTileIndicesToGlobalOffset` 的下标换算支持任意布局组合，而 NPU 要写 Nd2nd/Dn2dn/Nz2nz 等一堆专用路径？
**答案**：CPU 仿真逐元素搬运，每个元素独立寻址，通用下标公式天然支持所有布局；NPU 用 burst DMA，一次搬运一段连续内存，性能依赖把 shape/stride 折算成 burst 参数并尽量减少 burst 数，不同布局的连续性结构完全不同，必须各写专用折算路径。

**练习 2**：ND→NZ 的 TLOAD（Mat tile）为什么只有 `SFractalSize == 512` 且 GM 前三维为 1 时才支持？
**答案**：ND→NZ 是「边搬边分形重排」——把行主序数据重排成 16×C0 的 NZ 分形块摆进 L1。这条路径复用了 MTE 的 `TLoadNd2nzInstr` 硬件能力，而该硬件单元按 512B 分形粒度工作且一次只接受二维输入，所以实现用 `static_assert` 把不满足的组合一律拦在编译期（见 [tload_common.hpp:L253-L273](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch/memory/tload_common.hpp#L253-L273) 与 [docs/isa/TLOAD.md:L54-L59](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TLOAD.md#L54-L59)）。

**练习 3**：如果让你为 TLOAD 增加一个新布局组合的 CPU 仿真支持，最小改动是什么？NPU 侧呢？
**答案**：CPU 侧最小改动通常只是放宽 `CheckTileData` 里的 `static_assert` 白名单并在 `MapTileIndicesToGlobalOffset`（nz_utils.hpp）里补充新布局的下标映射；NPU 侧则要在 `tload_common.hpp` 的分派链上加一条新路径、写出对应的 burst 折算（或复用现有 intrinsic），并补齐编译期检查——工作量量级完全不同，这正是 u11-l1「新增指令/能力清单」要展开的话题。

## 5. 综合实践

把本讲三块内容串起来：**给 4.3.4 的闭环加上尾块场景**。

1. 把矩阵改成 [128, **120**]（列不满 128），tile 保持 `Tile<Vec, float, 128, 128>`，有效区设为 (128, 120)。
2. 分别设 `PadValue::Null` 与 `PadValue::Max` 跑两次，先只做 TLOAD + 把 tile 原样 TSTORE 到一个 [128,128] 的输出 GM。
3. 预测并验证：输出矩阵的前 120 列等于输入；后 8 列在 `Null` 下是 0、在 `Max` 下是 +inf。参考 [tests/cpu/st/testcase/tload/tload_kernel.cpp:L348-L394](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tload/tload_kernel.cpp#L348-L394) 中 golden 生成函数对 padding 列的处理方式写你的 golden。
4. 再思考一步：如果用 TSTORE 写回一个同样是 [128,120] 的输出 GM（而不是 [128,128]），padding 列还会落 GM 吗？用有效区裁剪的结论回答并验证。
   - 运行结果**待本地验证**；无法运行时，依据 4.1.3 ③ 与 4.2.3 ① 的源码给出推理结论即可（答案：不会，TSTORE 只写有效区）。

## 6. 本讲小结

- TLOAD/TSTORE 是 GM 与 tile 存储之间的唯一常规通道：GlobalTensor 出寻址元数据，tile 出落点与有效区，指令完成映射与搬运；搬运量由 tile 的 validRow/validCol 决定。
- 两指令 API 都是「TSYNC 等待 → IMPL 执行 → 返回 RecordEvent」的薄壳，分别挂在 MTE2/MTE3 流水线上，跨流水线依赖靠返回的事件表达。
- TLOAD 有 padding 语义（PadValue 决定无效区填充值），TSTORE 有变体语义（AtomicAdd 原子写、preQuantScalar/TSTORE_FP 写回时量化），Acc→GM 的 dtype 组合受编译期白名单约束。
- CPU 仿真实现是「逐元素循环 + 下标换算」，求正确、可调试；NPU 实现是「shape/stride 折算成 burst 参数 + DMA intrinsic」，求性能；两端 `*_IMPL` 签名逐字相同，由 `pto_instr_impl.hpp` 按「架构 × 后端」互斥选择。
- 布局匹配是硬约束：Vec 只支持同构 ND/DN/NZ，Mat 额外支持 ND→NZ/DN→ZN 的边搬边分形；约束以 static_assert/PTO_ASSERT 双保险落地，违反在编译期或首次运行即暴露。

## 7. 下一步学习建议

下一讲（u3-l2）讲 **TASSIGN 与缓冲管理**：本讲所有示例都出现了一句 `TASSIGN(t, 0)`，它在 Manual 模式下把 tile 显式绑定到片上缓冲地址，配套的 TAlloc/TFree 管理缓冲生命周期。建议提前浏览 [docs/isa/TASSIGN.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TASSIGN.md) 与 [docs/isa/TALLOC.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TALLOC.md)。若想加深本讲理解，可以顺带精读 [include/pto/common/arch/memory/tload_common.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch/memory/tload_common.hpp) 中 NZ 路径的 `CheckNzFormat`，体会 NZ 分形约束（末两维必须是 [16, 32/sizeof]）如何同时出现在文档、CPU 断言与 NPU static_assert 三处。
