# Kernel 入口与数据搬运：extern C 入口、DataCopy 与对齐

## 1. 本讲目标

上一讲（u5-l1）我们看清了矢量算子 Kernel 类的「CopyIn-Compute-CopyOut」三段式结构与 TQue 双缓冲流水。本讲往下钻一层，回答三个问题：

1. AI Core 上执行的程序从哪里开始？入口函数的参数是谁传进来的、按什么顺序排列？
2. Host 侧 tiling 计算好的 TilingData，Device 侧是怎么「取回来」的？
3. `DataCopyPad` 的参数表怎么填？为什么搬运要关心 32 字节对齐？尾块不对齐时会发生什么？

学完本讲，你应该能独立读懂任何一个 ops-nn 算子的 kernel 入口文件，并能在尾块长度不满足对齐要求时正确处理数据搬运。

## 2. 前置知识

- **GM 与 UB**：Global Memory（GM）是 Device 上的大容量 DDR 内存，Host 下发的输入输出 Tensor 都放在这里；Unified Buffer（UB）是 AI Core 内部的高速暂存区，矢量计算单元只能读写 UB。所以数据必须先从 GM 搬到 UB 才能计算（详见 u5-l1）。
- **Host 侧 / Device 侧**：tiling 运行在 Host 侧（CPU），kernel 运行在 Device 侧（AI Core）。两侧不共享 C++ 变量，只能通过「内存 + 约定的结构体布局」传数据，这就是 TilingData 契约（详见 u4-l2）。
- **32 字节对齐**：AI Core 的搬运硬件（DMA）一次搬一个 32 字节的「块」。`DataCopy` 要求搬运的字节长度是 32 的整数倍；长度不是 32 的倍数时，要用 `DataCopyPad`，由硬件自动补齐（pad）到块边界。
- **尾块（tail block）**：把大任务按 `ubFactor` 切成若干块后，最后一块的长度往往小于 `ubFactor`，也可能不满足 32 字节对齐，这块就叫尾块。
- **`__gm__` 指针与 `GM_ADDR`**：`GM_ADDR` 是一个无类型的 GM 地址（本质是 `__gm__ uint8_t*`），kernel 入口拿到的就是它，使用时需转型为 `__gm__ T*`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/add_example/op_kernel/add_example.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp) | kernel 入口函数：接收 GM 地址与 tiling 地址，按 tiling key 分发到模板 Kernel 类 |
| [examples/add_example/op_kernel/add_example.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h) | Kernel 类实现：Init 定位 GM 窗口，CopyIn/CopyOut 完成 GM↔UB 搬运 |
| [examples/add_example/op_kernel/add_example_tiling_data.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example_tiling_data.h) | TilingData 结构体定义（Host 写、Device 读的字节契约） |
| [examples/add_example/op_kernel/add_example_tiling_key.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example_tiling_key.h) | tiling key 模板参数声明（`ASCENDC_TPL_ARGS_DECL`） |
| [common/inc/op_kernel/kernel_utils.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_kernel/kernel_utils.h) | Device 侧公共工具：`Ceil`/`CeilAlign`/`Aligned` 等取整对齐函数 |

## 4. 核心概念与源码讲解

### 4.1 kernel 入口函数：AI Core 程序的起点

#### 4.1.1 概念说明

AI Core 上的 kernel 不是普通 C++ 函数——它由运行时（runtime）在算子下发时启动，入口参数也不是调用方压栈传进来的，而是按照 **CANN 约定的固定顺序**从一块参数内存中还原出来的。因此入口函数必须：

- 用 `__global__ __aicore__` 修饰，标记这是一个全局可见的 AI Core 入口；
- 保持 **C 链接约定**（不被 C++ 名称修饰污染），使运行时能按符号名找到它；
- 参数顺序固定：**输入 Tensor 的 GM 地址 → 输出 Tensor 的 GM 地址 → workspace 地址 → tiling data 地址**，与 def 文件中 `Input`/`Output` 的声明顺序一一对应。

> 术语解释：**名称修饰（name mangling）** 是 C++ 编译器把函数签名编码进符号名的机制。kernel 符号必须裸名可寻址，所以 Ascend C 工具链在底层保证入口按 C 约定导出；这也是文档里习惯称它为「extern C 入口」的原因。

#### 4.1.2 核心流程

```text
runtime 下发算子
   │
   ▼
按参数区还原入口实参：x, y, z, workspace, tiling（全是 GM_ADDR）
   │
   ▼
REGISTER_TILING_DEFAULT(AddExampleTilingData)   ← 声明本 kernel 使用的 tiling 结构
   │
   ▼
GET_TILING_DATA_WITH_STRUCT(...)                ← 从 tiling 的 GM 地址按字节还原结构体
   │
   ▼
if constexpr (schMode == ...)                   ← 编译期按 tiling key 选分支
   │
   ▼
Kernel 实例 Init(x, y, z, &tilingData) → Process()
```

#### 4.1.3 源码精读

入口函数定义在 [examples/add_example/op_kernel/add_example.cpp:36-57](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp#L36-L57)：这是一个模板函数，模板参数 `schMode` 就是 tiling key；五个形参依次是输入 x、输入 y、输出 z、workspace、tiling data 的 GM 地址，文件头注释（第 31-35 行）对每个参数做了逐一说明。

tiling key 枚举在同文件 [add_example.cpp:24-27](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp#L24-L27) 定义：`0` 对应 float 实现，`1` 对应 int32_t 实现。注意它必须与 Host 侧 tiling 里 `SetTilingKey` 写入的值、以及 [add_example_tiling_key.h:21-25](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example_tiling_key.h#L21-L25) 中 `ASCENDC_TPL_ARGS_DECL` 声明的取值集合（`ELEMENTWISE_TPL_SCH_MODE_0/1`）三处保持一致——这就是 u4-l2 讲过的「三道闸门对齐」，任何一处错位都会导致静默算错。

分发逻辑在 [add_example.cpp:45-56](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp#L45-L56)：`if constexpr` 是**编译期**分支——编译器会为每个 `schMode` 取值各生成一份专用二进制，运行时按 Host 侧选定的 tiling key 直接加载对应二进制，Device 上没有任何分支开销。每个分支内都是同样的三步：构造 Kernel 实例 → `Init` → `Process`。

#### 4.1.4 代码实践

1. **实践目标**：确认入口参数顺序与 def 文件声明顺序的对应关系。
2. **操作步骤**：
   - 打开 [examples/add_example/op_host/add_example_def.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp)，找到 `Input("x", ...)`、`Input("y", ...)`、`Output("z", ...)` 的声明顺序；
   - 对照入口函数形参 `(GM_ADDR x, GM_ADDR y, GM_ADDR z, GM_ADDR workspace, GM_ADDR tiling)`，写下两列对应表；
   - 再任选一个双输入生产算子（如 `activation/gelu` 的 kernel 入口），重复一次对照。
3. **需要观察的现象**：输入在前、输出在后、workspace 与 tiling 永远垫底；def 里 `OPTIONAL` 输入在缺席时入口仍保留形参位置。
4. **预期结果**：得出结论——「入口形参表 = def 的 Input/Output 顺序 + workspace + tiling」，这是读任何算子入口的通用口诀。
5. 本实践为纯源码阅读，无需硬件，可直接完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么入口分发用 `if constexpr` 而不是普通 `if`？

**答案**：`if constexpr` 在编译期求值，只为命中的分支生成代码。这样每个 tiling key 对应一份「为该数据类型专门实例化」的二进制（模板 `AddExample<float>` 与 `AddExample<int32_t>` 的 UB 字节宽度、指令选择都不同），运行时零分支开销；普通 `if` 则要求两个分支都能通过编译并在运行时判断，既浪费指令也常因类型差异无法统一编译。

**练习 2**：入口的 `workspace` 参数在本算子中用到了吗？什么时候会用到？

**答案**：add_example 没有用 workspace（它不需要中间临时内存）。当算子需要跨核交换数据、存放中间结果（如某些 norm、split 类算子）时，Host 侧 tiling 会计算 workspace 大小，aclnn 第一段申请内存，kernel 通过这个形参拿到其 GM 地址。

### 4.2 tiling data 的 Device 侧获取

#### 4.2.1 概念说明

u4-l2 讲过 TilingData 是「Host 写、Device 读」的 POD 字节契约。本模块看 Device 这一侧的读法。Host 侧在 tiling 函数里用 `GetTilingData<AddExampleTilingData>()` 拿到一块内存写入三个字段；框架随后把这块内容按字节拷贝到 GM 上的 tiling 区。Device 侧要做的，就是在入口处把它从 GM 还原成结构体。

#### 4.2.2 核心流程

```text
Host: tiling 函数填 totalNum / blockFactor / ubFactor
        │  框架按字节拷贝到 GM 的 tiling 区
        ▼
Device: REGISTER_TILING_DEFAULT(AddExampleTilingData)
        GET_TILING_DATA_WITH_STRUCT(AddExampleTilingData, tilingData, tiling)
        │  此后 tilingData 是栈上的结构体实例，字段可直接访问
        ▼
Init(x, y, z, &tilingData) —— 把 tiling 交给 Kernel 类
```

#### 4.2.3 源码精读

结构体定义在 [add_example_tiling_data.h:19-23](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example_tiling_data.h#L19-L23)：三个 `int64_t` 字段分别是总元素数、每核分到的元素数（核切分粒度）、每次 UB 循环处理的元素数（UB 切分粒度）。注意它是纯 POD（无虚函数、无 STL 成员），且头文件被 Host 与 Device 两侧的编译单元共同 include——布局一致是字节级拷贝成立的前提。

还原动作在 [add_example.cpp:40-42](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp#L40-L42)：`REGISTER_TILING_DEFAULT` 向 Ascend C 运行时登记本 kernel 关联的 tiling 结构类型；`GET_TILING_DATA_WITH_STRUCT(类型, 变量名, 入口的 tiling 形参)` 展开后会以入口第五个形参（GM 地址）为源，按结构体布局在本地生成实例 `tilingData`。之后 [add_example.cpp:48](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp#L48) 把 `&tilingData` 传给 `Init`，Kernel 类从此只跟这个结构体打交道，不再感知 GM 地址层面的 tiling。

一个值得注意的细节：这个仓库的示例 `Init` 直接解引用 `tilingData->totalNum` 等字段（见 [add_example.h:59-61](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L59-L61)），因为 `GET_TILING_DATA_WITH_STRUCT` 生成的是本地可解引用的实例。而旧式写法 `GET_TILING_DATA(tilingData, tiling)` 得到的是 `__gm__` 指针，字段访问语义不同——阅读老算子源码时要留意这两种形态。

#### 4.2.4 代码实践

1. **实践目标**：用「打断点式阅读」验证 tiling 字段从 Host 到 Device 的完整旅程。
2. **操作步骤**：
   - 打开 [examples/add_example/op_host/add_example_tiling.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp)，找到 `tilingData.totalNum`、`blockFactor`、`ubFactor` 的赋值处；
   - 再对照 Device 侧 `Init` 中三处读取（`add_example.h:59-61`）；
   - 在纸上画一条时间线：tiling 函数执行（Host，一次）→ kernel 入口执行（Device，每个核各一次）。
3. **需要观察的现象**：特别注意 `GET_TILING_DATA_WITH_STRUCT` 位于入口函数体内、`if constexpr` 之前——**每个 AI Core 核都会各自还原一份 tilingData**。
4. **预期结果**：理解「一次 tiling、多核共享」：tiling 只在 Host 算一次，所有核读同一份 GM 数据，再各自结合自己的 `GetBlockIdx()` 划定职责。
5. 本实践为纯源码阅读，无需硬件。

#### 4.2.5 小练习与答案

**练习 1**：为什么 TilingData 结构体「字段只能追加在尾部，不能修改已有字段的类型或顺序」？

**答案**：因为 Host 与 Device 是两个独立编译的单元，靠「相同头文件 + 字节级拷贝」保持一致。若改动已有字段顺序或类型，Host 写入的字节布局与 Device 读取的解释不一致（且预编译二进制还是按旧布局生成的），会静默读出垃圾值。追加在尾部则新旧布局前缀兼容，风险最小。

**练习 2**：`GET_TILING_DATA_WITH_STRUCT` 的第三个实参来自哪里？

**答案**：来自 kernel 入口函数的第五个形参 `GM_ADDR tiling`——即 runtime 按约定放在参数区最后一位的 tiling 数据 GM 地址。

### 4.3 DataCopyPad：参数表、GM 窗口与 32 字节对齐

#### 4.3.1 概念说明

搬运是矢量算子的生命线。Ascend C 提供两族搬运 API：

| API | 字节长度要求 | 典型用途 |
| --- | --- | --- |
| `DataCopy` | 搬运字节数必须是 32 的整数倍 | 主循环中规则的大块搬运 |
| `DataCopyPad` | 任意字节数，硬件按需补齐 | 尾块、shape 本身不对齐的场景 |

`DataCopyPad` 的搬运描述由 `DataCopyParams` 结构给出：

- `blockCount`：搬运的「段数」，每段之间由 stride 隔开；
- `blockLen`：**每段的字节数**（注意不是元素数！要乘 `sizeof(T)`）；
- `srcStride` / `dstStride`：两段之间的间隔字节数（单段搬运时填 0）。

GM→UB 方向还有一个 pad 配置参数 `{false, 0, 0, 0}`，四个分量依次是：是否由用户手动指定 pad、左侧补齐数、右侧补齐数、补齐值掩码；填 `false` 表示交给硬件自动处理——UB 里 pad 出来的字节内容不确定，因此**计算必须只作用于 `currentNum` 个真实元素**，输出侧也只搬 `currentNum` 个，pad 字节永远不落回 GM。

#### 4.3.2 核心流程

每个核的搬运范围由 Init 中的「GM 窗口」决定：

```text
Init:
  本核剩余量 remainder = totalNum - blockFactor × (核号相关偏移)
  blockLength_ = min(blockFactor, remainder)        ← 本核要处理的总元素数
  ubLength_   = ubFactor                            ← 每轮循环搬运的元素数
  inputGMX/Y、outputGMZ.SetGlobalBuffer(基址 + blockFactor×核偏移, blockLength_)
                                                  ↑ 把 GM 视图收缩到「本核负责的那一段」

Process 主循环（i = 0 .. loopCount-1）:
  currentNum = 末轮 ? blockLength_ - ubLength_×i : ubLength_   ← 尾块贯穿三阶段
  CopyIn:  DataCopyPad(xLocal ← inputGMX[i×ubLength_], blockLen = currentNum×sizeof(T))
  Compute: Add(zLocal, xLocal, yLocal, currentNum)             ← 只算真实元素
  CopyOut: DataCopyPad(outputGMZ[i×ubLength_] ← zLocal, blockLen = currentNum×sizeof(T))
```

对齐的数学关系：设元素宽度为 \( w = \text{sizeof}(T) \) 字节，尾块搬运量 \( b = \text{currentNum} \times w \)。`DataCopy` 要求 \( b \equiv 0 \pmod{32} \)；当 \( b \not\equiv 0 \pmod{32} \) 时必须用 `DataCopyPad`，实际搬运会被硬件补齐到 \( \lceil b/32 \rceil \times 32 \) 字节——这也是 tiling 阶段 `ubFactor` 要做 32 字节**向下**对齐的原因：保证除尾块外的所有整块都走对齐快路径（u4-l1）。

#### 4.3.3 源码精读

GM 窗口与队列缓冲初始化在 [add_example.h:57-70](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L57-L70)：`SetGlobalBuffer` 的第一个实参是 `(__gm__ T*)x + tilingData->blockFactor * AscendC::GetBlockIdx()`——把无类型 GM 地址转型后加上本核的元素偏移；第二个实参 `blockLength_` 限定本核可见的长度。此后 `inputGMX[progress * ubLength_]` 这类下标访问都发生在这个窗口内。`pipe.InitBuffer` 则按 `ubLength_ * sizeof(T)` 字节为每个队列分配 BUFFER_NUM 份 UB 空间。

CopyIn 在 [add_example.h:73-86](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L73-L86)：先 `AllocTensor` 从队列借两块 UB，然后填 `copyParams`——`blockCount = 1`（单段连续）、`blockLen = currentNum * sizeof(T)`（**字节**长度，含尾块的真实长度）、双 stride 为 0；第 82-83 行调用 GM→UB 方向的 `DataCopyPad`，第四个参数 `{false, 0, 0, 0}` 表示 pad 交给硬件自动处理；最后 `EnQue` 把 Tensor 推进队列供 Compute 消费。

CopyOut 在 [add_example.h:88-99](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L88-L99)：方向反过来（UB→GM），`DataCopyPad` 只需三个参数——UB→GM 方向不提供 pad 配置，硬件按 `blockLen` 精确写出真实字节，pad 部分天然不会污染 GM。

主循环与尾块计算在 [add_example.h:113-123](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L113-L123)：`loopCount = ⌈blockLength_ / ubLength_⌉`；`currentNum` 只在最后一轮取差值 `blockLength_ - ubLength_ * i`，其余轮等于 `ubLength_`。这个 `currentNum` 同时贯穿 CopyIn、Compute、CopyOut 三处——这是防越界、防漏数据的「尾块贯穿」写法（u5-l1 已建立整体图景，本讲关注它在搬运参数里的落点：`blockLen = currentNum * sizeof(T)`）。

> **阅读思考（待本地验证）**：`Init` 中剩余量计算用的是 `GetBlockIdx() - 1`（[add_example.h:59](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L59)），而三处 GM 偏移用的是 `GetBlockIdx()`（[add_example.h:63-65](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L63-L65)），两处下标基准相差 1。建议读者结合本机 `GetBlockIdx()` 的实际取值基准（0 起还是 1 起）跟踪一遍多核场景下各核的 `[偏移, 偏移+blockLength_)` 区间，验证首尾核是否恰好覆盖 `totalNum` 且不越界——这是一个非常好的源码阅读练习。

#### 4.3.4 代码实践

**实践目标**：构造非 32 字节对齐的尾块，验证 `DataCopyPad` 的正确性，并体会 `DataCopy` 在同一场景下的局限。

**操作步骤**（待本地验证，需配套 CANN 环境与 Atlas 环境）：

1. 打开 [examples/add_example/examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp)，把输入输出 shape 从默认值改为 `{1, 3, 5, 7}`（共 105 个 float，105×4 = 420 字节，不是 32 的倍数），同步把三份 std::vector 的长度改为 105（shape 乘积与 vector 长度必须一致，见 u1-l4 的教训）。
2. 重新编译并安装：
   ```bash
   bash build.sh --pkg --soc=${soc_version} --ops=add_example -j16
   # 安装 build_out 下生成的 run 包后
   bash build.sh --run_example add_example eager cust --vendor_name=custom
   ```
3. 确认输出正确：z[i] = x[i] + y[i] 对全部 105 个元素成立，末尾若干「不齐」元素没有丢、没有错位。
4. **对照实验**：把 `CopyIn`/`CopyOut` 中的 `DataCopyPad` 临时换成 `DataCopy`（去掉 pad 配置参数，其余实参不变），重复步骤 2。
5. 再换一组 shape（如 `{1, 1, 1, 1000}`，整块对齐）重复步骤 2-4，作为对照。

**需要观察的现象**：

- 步骤 3 中 105 元素场景输出应完全正确——`DataCopyPad` 的 `blockLen = currentNum * sizeof(T)` 接受任意字节长度；
- 步骤 4 中，尾块字节数非 32 倍数时可能出现执行报错或末尾数据异常（具体表现随芯片与驱动版本而定，**待本地验证**）；整块对齐的 1000 元素场景则两种 API 行为一致；
- 若把 `copyParams.blockLen` 误写成 `currentNum`（漏乘 `sizeof(T)`），搬运量会缩为 1/4，输出只剩前段正确——这是新手最常犯的错。

**预期结果**：得出结论——「整块走 `DataCopy` 快路径、尾块或天生不对齐的 shape 走 `DataCopyPad`」；生产算子常按 `if (对齐) DataCopy else DataCopyPad` 双路径编写以兼顾性能与正确性。完成后记得把对照实验的改动还原。

#### 4.3.5 小练习与答案

**练习 1**：`copyParams.blockLen` 的单位是什么？对 float 输入搬运 1000 个元素应填多少？

**答案**：单位是**字节**。1000 个 float 应填 `1000 * sizeof(float) = 4000` 字节。漏乘 `sizeof(T)` 是最典型的错误，表现为只搬了前 1/4 的数据。

**练习 2**：GM→UB 的 `DataCopyPad` 有第四个参数 `{false, 0, 0, 0}`，UB→GM 的却没有，为什么？

**答案**：pad 只发生在 UB 侧——硬件把不足 32 字节块的尾部在 UB 中补齐，方便按块搬运；GM 是用户可见的全局内存，必须精确写 `blockLen` 字节，多写的 pad 字节会污染输出 Tensor 之后（或同块内相邻）的数据，所以写出方向没有也不需要 pad 配置。

**练习 3**：为什么 `Compute` 里 `AscendC::Add` 的元素数用 `currentNum` 而不是 `ubLength_`？

**答案**：尾块时 UB 中只有前 `currentNum` 个元素是真实数据，其后是 `DataCopyPad` 补齐的脏字节。用 `ubLength_` 计算会把脏字节也加进结果；虽然 `CopyOut` 只写出 `currentNum` 个，看似无害，但一旦计算含前后依赖（如累加、归一化统计），脏字节就会污染结果。统一用 `currentNum` 是最安全的纪律。

### 4.4 公共搬运工具：kernel_utils.h 的取整与对齐函数

#### 4.4.1 概念说明

对齐计算不只发生在 Host 侧 tiling（那边的 `CeilDiv`/`FloorAlign` 来自 CANN 包与 matmul 包装层，见 u4-l3）。Device 侧 kernel 代码里也经常需要取整对齐——比如把尾块补齐到块边界、按 32 字节计算搬运段数。为此仓库提供了 [common/inc/op_kernel/kernel_utils.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_kernel/kernel_utils.h)，一套纯头文件、可在 `__aicore__` 函数里使用的轻量工具。

#### 4.4.2 核心流程

本模块是工具箱，没有流程，只有一个「选函数」的决策表：

| 需求 | 函数 | 语义 |
| --- | --- | --- |
| 向上取整（防漏元素） | `Ceil(a, b)` | \( \lceil a/b \rceil \) |
| 向上再对齐（UB/块边界） | `CeilAlign(a, b)` | \( \lceil a/b \rceil \times b \) |
| 安全除法向上取整 | `CeilDiv(a, b)` | 同 `Ceil`，但 `b==0` 时返回 `a` |
| 向下取整 | `FloorDiv(a, b)` | `a / b`，`b==0` 时返回 `a` |
| 值对齐到 alignment 的倍数 | `Aligned(v, align)` | 同 `CeilAlign` 的别名语义 |

#### 4.4.3 源码精读

五个函数定义在 [common/inc/op_kernel/kernel_utils.h:31-68](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_kernel/kernel_utils.h#L31-L68)：全部是 `template <typename T> __aicore__ inline` 的一行实现，例如 `Ceil` 就是 `(a + b - 1) / b`，`CeilAlign` 是 `(a + b - 1) / b * b`；`CeilDiv`/`FloorDiv`/`Aligned` 在此基础上加了除零保护（`b == 0` 时直接返回 `a`），这是 Device 侧代码「错误难调试、防御优先」的体现。文件前半部分（第 20-29 行）还提供了 `IsSame` 等编译期类型判断小工具，供模板 kernel 做类型分支。

取整方向的直觉（承接 u4-l1 的原则）：**向上防漏元素，向下防越界**。例如计算「需要多少个 32 字节块」要向上（`CeilDiv(bytes, 32)`，少了就丢数据）；计算「UB 里能放下多少个对齐块」要向下（多了就 UB 越界）。add_example 因为 tiling 已在 Host 侧做完对齐、尾块交给 `DataCopyPad`，所以 kernel 内没有直接调用这些函数；但在 shape 不受控的生产算子里，它们是 kernel 内局部对齐计算的常用件。

#### 4.4.4 代码实践

1. **实践目标**：掌握用 `ops::CeilDiv` 改写手写取整公式的方法。
2. **操作步骤**：
   - 阅读 [add_example.h:116](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L116) 的手写公式 `(blockLength_ + ubLength_ - 1) / ubLength_`；
   - 在文件头部 `#include "kernel_utils.h"`（需确认 CMake include 路径包含 `common/inc/op_kernel`，可参考 [examples/add_example/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/CMakeLists.txt) 的头文件搜索配置，**待确认**）；
   - 把该行替换为 `ops::CeilDiv(blockLength_, ubLength_)`，重新编译。
3. **需要观察的现象**：编译通过、运行结果与原先完全一致（loopCount 不变）。
4. **预期结果**：两种写法语义等价，工具函数可读性更好；若 include 路径未配置，保留手写公式亦可，不影响正确性。
5. 编译与运行依赖本地环境，**待本地验证**；纯阅读部分（公式对照）可直接完成。

#### 4.4.5 小练习与答案

**练习 1**：`ops::Ceil(33, 32)`、`ops::CeilAlign(33, 32)`、`ops::FloorDiv(33, 32)` 分别等于多少？

**答案**：`Ceil(33,32) = 2`（需要 2 个块）；`CeilAlign(33,32) = 64`（补齐到 64）；`FloorDiv(33,32) = 1`（最多完整放下 1 个块）。

**练习 2**：为什么这些函数都加了 `b == 0` 保护（`Ceil` 反而没加）？

**答案**：`CeilDiv`/`FloorDiv`/`Aligned` 是给运行期参数（shape、对齐值来自外部输入）使用的，除数可能为 0，Device 侧除零会导致核挂死且极难定位，所以宁可返回原值留待上层校验；`Ceil` 无保护属于历史实现，使用时应确保除数非 0——这也提醒我们读公共代码时要留意各函数防御等级的差异。

## 5. 综合实践

**任务：给 add_example 做一次「对齐压力测试」并撰写搬运分析笔记。**

1. 选取三组 shape：完全对齐（如 1024 元素）、尾块不对齐（如 105 元素）、极小输入（如 3 元素，总字节数 12，连一个完整 32 字节块都不满）。
2. 对每组 shape 运行 `--run_example`，记录：输出是否正确、末尾元素值、（可选）执行是否报错。
3. 对照 kernel 源码，为每组 shape 手工推演：`totalNum`、`blockFactor`、`ubFactor`、`loopCount`、每轮 `currentNum` 与 `blockLen` 字节数，以及尾块需要 pad 多少字节（\( \lceil b/32 \rceil \times 32 - b \)）。
4. 把推演值与实际运行对照，写一份不超过一页的《add_example 搬运路径分析》，重点回答：哪几轮搬运走对齐快路径、哪一轮依赖 `DataCopyPad`、如果把 `DataCopyPad` 全部换成 `DataCopy` 哪组 shape 会出问题。
5. 附加挑战：把三处 `GetBlockIdx()` 相关偏移画成多核分区图，验证第 4.3.3 节「阅读思考」中的下标基准问题。

本实践把入口参数、tiling 获取、GM 窗口、尾块贯穿与对齐计算串成一条完整链路，完成后你对「一个矢量算子在 AI Core 上如何搬数据」就有了可复用的分析模板。

## 6. 本讲小结

- kernel 入口是 `__global__ __aicore__` 模板函数，形参固定为「def 声明顺序的输入/输出 GM 地址 + workspace + tiling」，模板参数 `schMode` 即 tiling key。
- `GET_TILING_DATA_WITH_STRUCT` 在每个核的入口处从 GM 按字节还原 TilingData；结构体必须保持 POD 且字段只追加不改动，Host/Device 靠同一头文件维持布局契约。
- `DataCopyParams` 的 `blockLen` 单位是**字节**（元素数 × sizeof(T)）；`DataCopy` 要求 32 字节整倍，`DataCopyPad` 接受任意长度并只在 UB 侧自动补齐，GM 侧永远精确写出。
- 尾块长度 `currentNum` 必须贯穿 CopyIn/Compute/CopyOut 三阶段：搬运只搬真实字节、计算只算真实元素，pad 字节不落 GM、不参与计算。
- `Init` 用 `SetGlobalBuffer(基址 + blockFactor×核偏移, blockLength_)` 把 GM 视图收缩到本核窗口，主循环只在这个窗口内以 `ubLength_` 步进。
- Device 侧公共工具 `kernel_utils.h` 提供 `Ceil/CeilAlign/CeilDiv/FloorDiv/Aligned`，口诀仍是「向上防漏元素、向下防越界」。

## 7. 下一步学习建议

下一讲（u5-l3）将离开教学样例，精读生产算子 `activation/gelu` 的 kernel（`gelu_apt.cpp`），观察多架构目录（arch35）、性能写法与工程组织与 add_example 的差异——本讲建立的「入口→tiling→搬运」阅读框架将直接复用。此后 u8-l1 会回到 `add_example.h`，用 `AscendC::PRINTF` 与 `DumpTensor` 把搬运链路上的中间数据打出来，与本讲的纸面推演相互印证；建议顺带浏览 [docs/zh/debug/op_debug_prof.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/op_debug_prof.md) 预习调试开关的打开方式。
