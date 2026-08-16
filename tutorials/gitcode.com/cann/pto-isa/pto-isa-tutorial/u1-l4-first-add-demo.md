# 第一个算子：Add 示例逐行精读

## 1. 本讲目标

学完本讲，你应该能够：

- 读懂一个最小 PTO 算子的完整工程结构（kernel 侧 + host 侧 + 构建脚本）。
- 区分 `__gm__`、`AICORE`、`GlobalTensor`、`Tile`、`TASSIGN`、`TLOAD`/`TSTORE` 这几个关键概念，并说出它们各自处于"host 调用 → kernel 入口 → 片上计算 → 结果写回"链路的哪个环节。
- 理解 host 侧（PyTorch 算子注册）与 kernel 侧（PTO 指令序列）是如何配合工作的。
- 完成代码实践：仿照 Add 把算子改成 Sub（TSUB），并在 CPU 仿真下跑通验证。

本讲是承上启下的一讲：上一讲（u1-l3）你已经会用 `tests/run_cpu.py` 跑 CPU 仿真，本讲带你第一次"逐行读懂"一个真实算子的源码；下一单元（u2）将深入拆解本讲出现的 `GlobalTensor`、`Tile`、事件同步三大抽象。

## 2. 前置知识

本讲会用到的概念，先用一句话建立直觉，细节在后续单元展开：

| 概念 | 一句话解释 |
|------|-----------|
| `__gm__` | 编译器地址空间标注，表示指针指向 Global Memory（设备全局内存，即 NPU 上的 DDR），区别于片上缓存 |
| `AICORE` | 函数属性宏，标注该函数运行在 AI Core（昇腾计算核心）上，而非 CPU 上 |
| `AIV` | AI Vector Core，向量核心。一个 AI Core 可含多个向量核，本例中 20 个 AIV 并行处理不同数据块 |
| `GlobalTensor` | 描述"全局内存上一块数据"的视图，携带 shape（形状）和 stride（步长）元数据，本身不搬数据 |
| `Tile` | 片上（UB 缓冲）固定形状的二维数据块，是 PTO 指令的操作对象 |
| `UB` | Unified Buffer，向量核的片上统一缓冲，容量有限（本例 A2/A3 为 192KB），数据要先从 GM 搬进来才能算 |
| `TASSIGN` | 给 Tile 或 GlobalTensor 分配/绑定一个片上或全局地址 |
| `TLOAD` / `TSTORE` | 在 GM 与 Tile 之间搬入 / 搹出数据的指令 |
| `PIPE_MTE2` / `PIPE_V` / `PIPE_MTE3` | 三条硬件流水线：MTE2（搬入）、Vector（向量计算）、MTE3（搬出），它们并行执行，靠事件同步 |
| `set_flag` / `wait_flag` | 事件原语：前者"挂牌子"表示某流水线的数据已就绪，后者"等牌子"保证消费前数据已生产好 |

如果你对"流水线 + 事件同步"这段还觉得抽象，没有关系——本讲只需建立"数据要先 TLOAD 进 Tile、算完再 TSTORE 出去、中间靠事件保证顺序"的整体印象即可。

## 3. 本讲源码地图

本讲涉及的文件及其职责：

| 文件 | 职责 |
|------|------|
| [demos/baseline/add/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/README.md) | 示例总说明：目录结构、算子注册方法、构建运行步骤 |
| [demos/baseline/add/csrc/kernel/add_custom.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp) | kernel 侧：真正的 PTO 指令序列，运行在 AI Core 上 |
| [demos/baseline/add/csrc/host/my_add.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/host/my_add.cpp) | host 侧：把 kernel 包装成 PyTorch 算子 `torch.ops.npu.my_add` |
| [demos/baseline/add/CMakeLists.txt](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/CMakeLists.txt) | 构建脚本：把 kernel 源文件编成静态库并链接 PyTorch 扩展 |
| [demos/baseline/add/test/test.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/test/test.py) | 端到端验证脚本：调用自定义算子并与 CPU 结果比对 |
| [tests/cpu/st/testcase/tadd/tadd_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp) | tadd 的 CPU 仿真 ST 用例 kernel（本讲实践的参照模板） |
| [tests/cpu/st/testcase/tadd/main.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp) | tadd 的 gtest 主程序：造数、调用、比对 golden |
| [tests/cpu/st/testcase/tadd/gen_data.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/gen_data.py) | tadd 的 golden 数据生成脚本 |

注意一个重要事实：`demos/baseline/add` 是 **NPU 路径**的示例（需要真机 + CANN + torch_npu），而 CPU 仿真路径下与之等价的最小 Add 实现是 ST 用例 `tests/cpu/st/testcase/tadd`。本讲两个都读：前者教你完整工程结构，后者是你动手实践的载体。

## 4. 核心概念与源码讲解

### 4.1 kernel 侧代码：add_custom.cpp 逐行精读

#### 4.1.1 概念说明

kernel 侧是运行在 AI Core 上的代码，用 PTO 指令描述"数据如何搬运、如何计算、如何同步"。这是整个算子的灵魂：host 侧只是"发令员"，真正干活的指令序列全在这里。

一个 PTO kernel 的标准骨架是：

```text
① 定义常量（核数、UB 布局、tile 切分）
② 构造 GlobalTensor 视图（描述 GM 上的输入输出）
③ 构造 Tile 并用 TASSIGN 绑定 UB 地址
④ 循环 {
     TASSIGN 更新 GlobalTensor 地址（切到下一个数据块）
     TLOAD 搬入 → 事件同步 → 计算（TADD）→ 事件同步 → TSTORE 搬出
   }
⑤ 收尾同步
```

#### 4.1.2 核心流程

以本例（20 个向量核、ping-pong 双缓冲、每个核处理 4 个 tile 分片）为例：

```text
输入 x[20, 2048] (half) ──┐
输入 y[20, 2048] (half) ──┤
                          ▼
   每个向量核分到一行 2048 个元素（bTileRows=1, bTileCols=2048）
   再切成 tileNum(2) × BUFFER_NUM(2) = 4 段，每段 1×512
                          ▼
   主循环 4 次，每次：
     TLOAD x 段 → UB 的 X_PING/X_PONG
     TLOAD y 段 → UB 的 Y_PING/Y_PONG
     TADD  z = x + y   （在 Vector 流水线上）
     TSTORE z 段 → GM
   ping-pong 交替使用两套缓冲，让"搬入第 i+1 块"与"计算/搬出第 i 块"重叠
                          ▼
输出 z[20, 2048] (half)
```

为什么要有 ping-pong（乒乓）双缓冲？因为 MTE2（搬入）、Vector（计算）、MTE3（搬出）三条流水线是并行硬件。如果只有一套缓冲，计算第 i 块时无法同时搬入第 i+1 块（会覆盖还没算完的数据）。两套缓冲轮流用，搬入和计算就能重叠，这是 tile 级性能优化的最基本手法。

#### 4.1.3 源码精读

**编译守卫**。整个文件包在架构宏里，只有 A2/A3（C220，含向量扩展）才编译：

[add_custom.cpp:L11-L14](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L11-L14) —— 检查 `__CCE_AICORE__ == 220 && __DAV_C220_VEC__` 后引入 Ascend C 基础头 `kernel_operator.h` 和 PTO 统一入口 `pto/pto-inst.hpp`（还记得 u1-l2 讲的"唯一入口"吗？就是它）。

**UB 布局常量**。

[add_custom.cpp:L18-L30](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L18-L30) —— 这段手工规划了 192KB UB 的地址分配：输入 x 占 `0x0` 起、输入 y 占 `0x10000` 起、输出 z 占 `0x20000` 起，各自再分 PING/PONG 两个地址；`MAX_TILE_SIZE` 限制单个 tile 不超过可用段大小。注意这是 **Manual 模式**的典型写法——开发者自己管理片上内存；后续 u3、u9 会讲到 Auto 模式如何省掉这些样板。

**函数签名与地址空间**。

[add_custom.cpp:L32-L36](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L32-L36) —— `AICORE void runTAdd(__gm__ T* z, __gm__ T* x, __gm__ T* y, ...)`：`AICORE` 表示函数跑在 AI Core 上；三个 `__gm__ T*` 都是指向 Global Memory 的裸指针，`set_mask_norm()/set_vector_mask(-1, -1)` 是向量指令的掩码初始化（先照抄，u2-l2 详讲）。

**两级切分**。

[add_custom.cpp:L37-L45](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L37-L45) —— 第一级"核间切分"：20×2048 的总任务按 `BLOCK_ROWS=20` 行分给 20 个向量核，每核负责 `bTileRows × bTileCols = 1 × 2048`；第二级"核内切分"：每核把 2048 列再切成 `tileNum(2) × BUFFER_NUM(2)` 份，得到每片 `tileSRows × tileSCols = 1 × 512`。`static_assert` 在编译期就拦下"UB 装不下"的错误。

**GlobalTensor 视图**。

[add_custom.cpp:L47-L52](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L47-L52) —— 用 `Shape<1,1,1,tileSRows,tileSCols>`（5 维形状模板）+ `Stride<1,1,1,tileCols,1>`（步长）把裸指针包装成 `GlobalTensor`。注意 stride 用的是 `tileCols`（整行 2048）而不是分片宽 512——因为 GM 里数据是按完整行连续存放的，视图只是"每次看其中一段"。

**Tile 声明与 TASSIGN 绑定**。

[add_custom.cpp:L55-L71](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L55-L71) —— `TileData` 是静态形状（1×512）+ 动态掩码（构造参数 `vRows, vCols` 标记实际有效行列，防止尾块越界写）的片上缓冲类型；每个输入/输出各声明 `BUFFER_NUM=2` 个，再用 `TASSIGN` 依次绑到前面规划的 UB 地址上。

**主循环：搬运-计算-搬出 + 事件同步**。

[add_custom.cpp:L79-L109](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L79-L109) —— 每次迭代：

1. `TASSIGN(xGlobal, x + iterOffset)` 等三行：把 GlobalTensor 视图平移到本核本迭代的分段（L86-L88）。
2. `TLOAD(xTiles[...], xGlobal)` 两次：MTE2 流水线把 x、y 两段从 GM 搬进 UB（L90-L93）。
3. `set_flag`/`wait_flag` 一对：保证"搬入完成"之后 Vector 才开始算（L95-L96）。
4. `TADD(zTiles[...], xTiles[...], yTiles[...])`：向量核上逐元素相加（L98-L100）。
5. 再一对事件：保证"算完"之后 MTE3 才写回（L103-L104）。
6. `TSTORE(zGlobal, zTiles[...])`：结果搬回 GM（L105-L107）。
7. `pingpong_flag` 翻转，下轮换另一套缓冲（L108）。

事件方向（`PIPE_MTE2 → PIPE_V → PIPE_MTE3`）体现了生产者挂牌、消费者等牌的协议。事件 ID 只有 0/1 两个，正好配合 ping-pong 区分"这套缓冲"和"那套缓冲"。

**kernel 入口**。

[add_custom.cpp:L119-L126](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L119-L126) —— `extern "C" __global__ AICORE void add_custom(GM_ADDR x, GM_ADDR y, GM_ADDR z, uint32_t totalLength)` 是 host 侧启动的入口：参数就是 host 传进来的设备地址和动态长度；内部固定 tile 尺寸 20×2048（half），转发给模板函数 `runTAdd`。`__global__` 表示这是可从 host 启动的 kernel 函数。

#### 4.1.4 代码实践（阅读型）

1. **实践目标**：不看讲解，独立复述主循环一次迭代中 6 条 PTO/事件指令的顺序与作用。
2. **操作步骤**：打开 [add_custom.cpp:L83-L109](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L83-L109)，把每次 `set_flag`/`wait_flag` 的 `(源流水线, 目标流水线, 事件ID)` 三元组抄成一张表，共 6 对。
3. **需要观察的现象**：你会发现同一事件 ID 上 `set` 与 `wait` 的流水线方向是交替反转的（MTE2→V、V→MTE2、MTE3→V、V→MTE3……），每一对都对应一次"数据所有权交接"。
4. **预期结果**：能画出 MTE2 / Vector / MTE3 三条泳道的时间线，标出事件挂牌位置。画不出来的话，回到 4.1.2 的流程图再对照一遍。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Stride` 用 `tileCols` 而不是 `tileSCols`？

答案：GM 中数据按完整行（2048 个元素）连续存放，GlobalTensor 视图虽然每次只描述 1×512 的分片，但相邻两行起点之间的距离仍是 2048。如果 stride 写成 512，第二次循环就会读到错误的行。

**练习 2**：把 `BUFFER_NUM` 从 2 改成 1（假设 UB 地址也相应调整），程序还能得到正确结果吗？性能会怎么变？

答案：结果仍正确（事件同步保证顺序），但失去了乒乓重叠——搬入第 i+1 块必须等计算/搬出第 i 块完全结束，流水线出现"气泡"，带宽利用率下降。这也解释了为什么事件 ID 恰好需要 2 个。

**练习 3**：`vRows`、`vCols` 这两个动态掩码值在本例的调用条件下分别是多少？

答案：`vRows = tileRows / GetBlockNum() = 20 / 20 = 1`；`bLength = totalLength / 20`，`vCols = bLength / tileNum / BUFFER_NUM = (20×2048/20) / 2 / 2 = 512`。即 tile 的静态形状 1×512 恰好全部有效（见 [add_custom.cpp:L56-L63](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L56-L63)）。当 totalLength 不是整除关系时，掩码就会小于静态形状，这正是掩码存在的意义。

### 4.2 host 侧代码：my_add.cpp 与 PyTorch 算子注册

#### 4.2.1 概念说明

kernel 写好后，需要一个"从 host（CPU 侧）把它启动起来"的桥。本示例选择 PyTorch 生态：把 PTO kernel 注册成一个 `torch.ops.npu.my_add` 算子，上层 Python/PyTorch 代码就能像调用普通算子一样调用它。host 侧做三件事：

1. 声明算子 schema（签名）。
2. 实现算子函数：准备输出张量、算好长度、启动 kernel。
3. 把实现注册到 PyTorch 的 NPU 派发键上。

#### 4.2.2 核心流程

```text
Python: torch.ops.npu.my_add(x_npu, y_npu)
   │  （PrivateUse1 派发）
   ▼
C++: run_add_custom(x, y)
   │  z = empty_like(x)
   │  totalLength = ∏ x.sizes()
   ▼
EXEC_KERNEL_CMD(add_custom, blockDim=20, x, y, z, totalLength)
   │  （展开为 ACLRT_LAUNCH_KERNEL，启动 device 侧 kernel）
   ▼
AI Core: add_custom(GM_ADDR x, GM_ADDR y, GM_ADDR z, totalLength)
   │  runTAdd: TLOAD → TADD → TSTORE（见 4.1）
   ▼
返回 z 给 Python
```

#### 4.2.3 源码精读

**算子实现函数**。

[my_add.cpp:L16-L29](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/host/my_add.cpp#L16-L29) —— `run_add_custom` 用 `at::empty_like(x)` 分配输出；`blockDim = 20` 与 kernel 侧 `BLOCK_DIM = 20` 必须一致（host 决定启动多少个块，kernel 侧按同样数量切分数据）；`totalLength` 由各维尺寸连乘得到；最后 `EXEC_KERNEL_CMD(add_custom, blockDim, x, y, z, totalLength)` 一行完成启动，参数顺序与 kernel 入口签名一一对应。

**schema 声明**。

[my_add.cpp:L32-L38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/host/my_add.cpp#L32-L38) —— `TORCH_LIBRARY_FRAGMENT(npu, m)` 在 `npu` 命名空间声明 `my_add(Tensor x, Tensor y) -> Tensor`。声明之后 Python 侧就能看到 `torch.ops.npu.my_add` 这个名字。

**实现注册**。

[my_add.cpp:L40-L46](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/host/my_add.cpp#L40-L46) —— `TORCH_LIBRARY_IMPL(npu, PrivateUse1, m)` 把 `run_add_custom` 绑到该算子上。`PrivateUse1` 是 PyTorch 为第三方后端预留的派发键，torch_npu 用它接管 NPU 上的执行。

**端到端验证脚本**。

[test.py:L22-L37](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/test/test.py#L22-L37) —— 生成 `[20, 2048]` 的 float16 随机张量（尺寸恰好等于 kernel 固定的 tile 尺寸），搬上 NPU 调用自定义算子，再与 CPU 的 `torch.add` 结果做相对误差比对。

#### 4.2.4 代码实践（阅读型）

1. **实践目标**：弄清一个张量从 Python 到 AI Core 经过了几层。
2. **操作步骤**：沿 `test.py` 的 `torch.ops.npu.my_add(x_npu, y_npu)` → `my_add.cpp` 的 `run_add_custom` → `EXEC_KERNEL_CMD` → `add_custom.cpp` 的 `add_custom` 追一遍调用链，记录每一层各做了什么。
3. **需要观察的现象**：host 侧只出现 `blockDim` 和 `totalLength` 两个"形状类"参数，tile 切分细节（tileNum、BUFFER_NUM、UB 地址）完全被封装在 kernel 侧。
4. **预期结果**：写出 5 行的调用链表格。若需真机验证，可按 [README.md:L106-L131](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/README.md#L106-L131) 的步骤构建 wheel 并运行 test.py；无真机则此步骤为源码阅读练习。

#### 4.2.5 小练习与答案

**练习 1**：如果 host 侧 `blockDim` 写成 10，而 kernel 侧 `BLOCK_DIM` 仍是 20，会发生什么？

答案：kernel 侧 `AscendC::GetBlockNum()` 返回实际启动的块数。host 与 kernel 的核数不一致时，`vRows`/`vCols` 掩码与核间切分（`static_assert(BLOCK_ROWS * BLOCK_COLS == BLOCK_DIM)`）会失配，轻则数据覆盖错乱，重则编译期断言失败。两处必须保持一致。

**练习 2**：`EXEC_KERNEL_CMD(add_custom, ...)` 的四个张量/标量参数，与 kernel 入口 `add_custom(GM_ADDR x, GM_ADDR y, GM_ADDR z, uint32_t totalLength)` 是什么关系？

答案：一一对应的位置参数。host 传入的 `x, y, z` 张量在启动时被转换为设备内存地址（`GM_ADDR`），`totalLength` 原样传值；kernel 侧再把这些地址包装成 `__gm__ half*` 使用。

### 4.3 构建脚本与 CPU 仿真对照实现

#### 4.3.1 概念说明

构建脚本回答"这些源文件如何变成可运行的东西"。本讲看两套：

- **NPU 路径**：`demos/baseline/add/CMakeLists.txt` 把 kernel 编成 Ascend C 静态库，再与 host 侧 PyTorch 扩展链接成 wheel。
- **CPU 仿真路径**：`tests/cpu/st/testcase/tadd` 用一个 CMake 函数 `pto_cpu_sim_st` 把 kernel + gtest 主程序编成可执行文件，配合 `gen_data.py` 生成 golden 数据做比对——这是无硬件环境下验证 PTO kernel 的标准方式，也是本讲综合实践的载体。

#### 4.3.2 核心流程

NPU demo 的构建链（需要 CANN 环境）：

```text
add_custom.cpp ──ascendc_library──▶ no_workspace_kernel.a
my_add.cpp 等 host 源 ──▶ op_extension.so（链接 torch_npu / ascendcl）
两者 + setup.py ──▶ op_extension wheel ──pip install──▶ torch.ops.npu.my_add 可用
```

CPU 仿真 ST 用例的构建链（只需 GCC≥13 或 Clang≥15，见 u1-l3）：

```text
gen_data.py ──▶ input1.bin / input2.bin / golden.bin（numpy 计算 golden）
tadd_kernel.cpp + main.cpp ──pto_cpu_sim_st(tadd)──▶ tadd 可执行（gtest）
python3 tests/run_cpu.py --testcase tadd ──▶ 跑 gtest，比对 golden
```

#### 4.3.3 源码精读

**NPU demo 的 CMake 组织**。

[CMakeLists.txt:L70-L80](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/CMakeLists.txt#L70-L80) —— `ascendc_library(no_workspace_kernel STATIC csrc/kernel/add_custom.cpp)` 把 kernel 编成静态库，并通过 `ascendc_include_directories` 引入 `${PTO_LIB_PATH}/include`（PTO 头文件所在，需提前 `export PTO_LIB_PATH` 指向本仓库）；紧接着把 `csrc/host/*.cpp` 编成 `op_extension` 动态库并链接 `torch_npu`、`ascendcl` 等。SOC 版本在 [CMakeLists.txt:L30](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/CMakeLists.txt#L30) 处设置（默认 `ascend910b1`，即 A2/A3）。

**CPU 仿真的 tadd kernel**。

[tadd_kernel.cpp:L16-L43](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L16-L43) —— 与 NPU 版 `runTAdd` 结构完全相同但简化得多：单个 tile（无多核切分、无乒乓循环），`TASSIGN` 三个 tile 到 UB 偏移 0x0/0x4000/0x8000，`TLOAD` 两个输入 → 一对 `set_flag/wait_flag(PIPE_MTE2, PIPE_V)` → `TADD` → 一对 `set_flag/wait_flag(PIPE_V, PIPE_MTE3)` → `TSTORE`。这正是 4.1.2 骨架的"最小版"，读它比读 NPU 版更容易抓住主干。

**CPU 仿真的 gtest 主程序**。

[main.cpp:L60-L70](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp#L60-L70) —— 用 `ReadFile` 读取 `gen_data.py` 生成的 `input1.bin/input2.bin`，经 `LaunchTAdd` 跑 kernel（CPU 仿真下 `aclrtMalloc/aclrtMemcpy` 等调用都被仿真桩接管），把输出写成 `output.bin`。随后在 [main.cpp:L83-L90](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp#L83-L90) 与 `golden.bin` 逐元素比对（容差 0.001）。

**golden 生成脚本**。

[gen_data.py:L21-L38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/gen_data.py#L21-L38) —— numpy 生成两份 1~9 的随机整数输入，`golden = input1 + input2` 后连同输入一起写成二进制。golden 永远由"朴素的 numpy 参考实现"计算，与被测的 PTO 实现相互独立，这是 ST 测试的正确性锚点。

**用例注册机制**。

[tadd/CMakeLists.txt:L14](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/CMakeLists.txt#L14) —— 整个文件只有一行 `pto_cpu_sim_st(tadd)`。该函数定义在 [tests/cpu/st/testcase/CMakeLists.txt:L11-L35](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L11-L35)，自动把 `main.cpp` 和（若存在）`<名字>_kernel.cpp` 编成同名可执行文件并挂上 gtest；新用例还需把目录名加进同文件 L39 起的 `ALL_TESTCASES` 列表。

#### 4.3.4 代码实践（本讲综合实践见第 5 节）

1. **实践目标**：理解 `pto_cpu_sim_st` 的自动拼装规则。
2. **操作步骤**：对比 `pto_cpu_sim_st` 函数体与 tadd 目录的文件名（`tadd_kernel.cpp`、`main.cpp`），确认"命名约定驱动构建"的机制；再用 `python3 tests/run_cpu.py --testcase tadd --verbose` 跑一遍 tadd（该命令在 u1-l3 已介绍）。
3. **需要观察的现象**：输出中能看到 gen_data 生成数据、cmake 增量构建、gtest 通过的过程。
4. **预期结果**：tadd 全部 case 通过。若本机未配置 C++20 编译器，此步骤标记为「待本地验证」，改为纯阅读。

#### 4.3.5 小练习与答案

**练习 1**：CPU 仿真下 `aclrtMalloc`、`set_flag` 这些 NPU 运行时调用为什么能直接编译通过？

答案：CPU 仿真构建用 `__CPU_SIM` 宏路由到 `include/pto/cpu/` 与仿真桩（u1-l2、u1-l3 讲过的后端切换），这些运行时 API 被桩函数接管，模拟设备内存分配与流水线事件语义，内核代码无需改动。

**练习 2**：为什么 `gen_data.py` 里 golden 要乘掩码截断（`golden[:row_valid,:col_valid] = ...`）？

答案：tile 的静态形状可能大于有效数据（valid_row/valid_col 小于 tile 行列），越界部分不属于有效输出。golden 只在有效区域内填入参考结果，保证与"tile 只对有效区域负责"的语义一致。

**练习 3**：ST 用例中的 kernel（tadd_kernel.cpp）与 NPU demo 的 kernel（add_custom.cpp）是什么关系？

答案：同一指令序列的两个投影。ST 版是"最小骨架"（单 tile、单核、无循环），用于验证指令语义正确性；demo 版是"工程完整版"（多核切分、乒乓双缓冲、循环流水），用于真机性能。两者共享同一套 PTO 指令定义，只是编排规模不同。

## 5. 综合实践

**任务：把 Add 改成 Sub（TSUB），在 CPU 仿真下跑通并验证结果正确。**

思路：`demos/baseline/add` 是真机工程，改它无法在无硬件环境验证；CPU 仿真路径下等价的载体是 ST 用例。因此实践分两步：先读懂 demo 里要改哪一行，再在 ST 用例上完成可验证的改动。

**步骤（CPU 仿真路径，推荐）**：

1. **复制用例目录**：`cp -r tests/cpu/st/testcase/tadd tests/cpu/st/testcase/tsub`。
2. **重命名源文件**：`cd tests/cpu/st/testcase/tsub && mv tadd_kernel.cpp tsub_kernel.cpp`（文件名必须与 `pto_cpu_sim_st(tsub)` 的约定匹配）。
3. **修改 kernel**：把 [tadd_kernel.cpp:L38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L38) 中的 `TADD(dstTile, src0Tile, src1Tile);` 改为 `TSUB(dstTile, src0Tile, src1Tile);`（TSUB 与 TADD 同为二元向量指令，接口形式一致，可对照 [include/pto/npu/a2a3/TSub.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TSub.hpp) 确认）。函数名 `runTAdd`/`LaunchTAdd` 建议同步改名为 `runTSub`/`LaunchTSub`（纯重命名，不影响行为）。
4. **修改 CMakeLists.txt**：把目录内这一行改成 `pto_cpu_sim_st(tsub)`。
5. **注册用例**：在 [tests/cpu/st/testcase/CMakeLists.txt](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L39) 的 `ALL_TESTCASES` 列表里加一行 `tsub`。
6. **修改 main.cpp 与 gen_data.py**：把测试类名 `TADDTest` 改为 `TSUBTest`（`GetGoldenDir` 依赖套件名拼 golden 路径，必须一致）；gen_data.py 中 `golden = input1 + input2` 改为 `golden = input1 - input2`（int 输入可能相减为负，int16 用例需注意，可把输入改成 `np.random.randint(5, 10, ...)` 保证 input1 ≥ input2，或直接接受负数——int16/int32 本身可表示负数，float 用例无碍）。
7. **运行验证**：`python3 tests/run_cpu.py --testcase tsub --verbose`。

**需要观察的现象**：gen_data 重新生成 golden（此时是减法结果），cmake 增量编译出 `tsub` 可执行文件，gtest 输出全绿。若故意不改 gen_data.py 的 `+`，gtest 应当报不一致——这反向验证了 golden 比对机制真的在工作。

**预期结果**：`TSUBTest` 全部 case 通过。改动只涉及一行指令替换 + 一行 golden 运算符替换，其余（TASSIGN/TLOAD/事件/TSTORE）原样保留——这本身就说明 PTO 指令之间的高度正交性。本实践在无 CANN 环境的机器上即可完成；若编译环境未就绪，标记为「待本地验证」。

**加分项（有真机时）**：在 `demos/baseline/add` 里做同样的一行改动（[add_custom.cpp:L100](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L100) 的 `TADD` 改 `TSUB`），并把 [test.py:L34](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/test/test.py#L34) 的 `torch.add` 改 `torch.sub`，重新打 wheel 验证。

## 6. 本讲小结

- 一个 PTO 算子 = kernel 侧（指令序列，跑在 AI Core）+ host 侧（算子注册与启动，跑在 CPU）+ 构建脚本（把两者粘起来）；本例 host 侧只负责 `empty_like` + `EXEC_KERNEL_CMD`，所有 tile 编排都在 kernel 侧。
- kernel 的标准骨架是"构造 GlobalTensor 视图 → TASSIGN 绑定 Tile → 循环{更新视图地址 → TLOAD → 事件 → 计算 → 事件 → TSTORE}"，事件（set_flag/wait_flag）在 MTE2/Vector/MTE3 三条并行流水线之间做数据所有权交接。
- `__gm__` 标注全局内存地址空间，`AICORE` 标注设备端函数，`GlobalTensor` 是 GM 数据的 shape/stride 视图（不搬数据），`Tile` 是 UB 上的固定形状计算单元——四个概念分处不同层次，不要混。
- ping-pong 双缓冲（BUFFER_NUM=2 + 两个事件 ID）是让搬运与计算重叠的最基本手段；demo 版 kernel 展示了完整的 20 核切分 + 乒乓流水，ST 版 kernel 是同一骨架的最小单 tile 投影。
- CPU 仿真路径用"ST 用例 + numpy golden 比对"验证指令语义，无需任何硬件；`pto_cpu_sim_st(<name>)` 一行 CMake + `ALL_TESTCASES` 注册即可挂入新用例。
- 把 TADD 换成 TSUB 只需改动一行 kernel 指令和一行 golden 运算，说明 PTO 指令接口高度一致、可替换性好。

## 7. 下一步学习建议

本讲你已经第一次完整读穿一个算子，但 `GlobalTensor` 的 Shape/Stride 模板、`Tile` 的静态形状与动态掩码、事件同步的硬件原理都只是"用过"还没有"学透"。下一讲 **u2-l1（GlobalTensor：全局内存上的张量视图）** 将拆解 5 维 Shape 模板与 DYNAMIC 维度的设计；随后 **u2-l2（Tile 编程模型）** 和 **u2-l3（事件与同步）** 会把本讲三个"照抄即可"的部分逐一展开。建议在进入 u2 之前，重读一遍 [tadd_kernel.cpp:L17-L43](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L17-L43)，并列出你尚不能解释的每一行——那份清单就是你接下来三讲的学习地图。
