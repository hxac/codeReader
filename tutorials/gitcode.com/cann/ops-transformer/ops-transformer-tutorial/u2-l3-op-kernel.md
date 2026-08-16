# op_kernel：第一个 AscendC 设备核函数

## 1. 本讲目标

上一讲（u2-l2）我们读完了 host 侧三件套，知道了 host 负责「定户口（def）、量尺寸（infershape）、排计划（tiling）」。本讲跨过 host 与 device 的边界，进入 `op_kernel` 目录，读完本讲你应该能够：

1. 读懂一个最小 Ascend C kernel 的入口签名与 `Init → Process` 执行骨架。
2. 说清 tiling data 和 tiling key 如何从 host 侧「写入」、在 device 侧「读出」，理解二者是 host/device 之间仅有的两份合同。
3. 掌握 GM（Global Memory）与 Local（UB）内存的基本使用模式：`SetGlobalBuffer`、`InitBuffer`、`DataCopy`、队列（TQue）驱动的 CopyIn/Compute/CopyOut 三段流水。

本讲仍以教学算子 add_example 为样本——它只有 4 个文件，却是所有本仓库数百个工业级算子共同遵循的写法缩影。

## 2. 前置知识

在读代码之前，先用通俗语言建立几个设备侧概念：

- **AI Core 与 AIV**：NPU 上真正做计算的单元叫 AI Core。本仓库的 Vector 类算子（逐元素加减乘等）跑在 AI Core 的 Vector 子单元（AIV）上。host 侧 tiling 里 `GetCoreNumAiv()` 拿到的就是 AIV 核数，`SetBlockDim(8)` 就是要启动 8 个核。
- **GM（Global Memory）**：设备上的大容量主存（类似 CPU 世界的内存），输入 x/y、输出 z 都放在这里。容量大但访问慢。
- **UB / Local Memory（Unified Buffer）**：每个 AI Core 内部的高速缓存（类似寄存器+L1 的角色），容量小（几十到几百 KB）但访问快。Vector 计算指令**只能**作用于 UB 上的数据。
- **因此一个 kernel 的基本模式**永远是：GM → UB（搬入）→ UB 上计算 → UB → GM（搬出）。这个「搬运-计算-搬运」循环就是本讲的主角。
- **TPipe 与 TQue（队列）**：Ascend C 提供的内存管理抽象。`TPipe` 负责在 UB 上划分缓冲区，`TQue` 是带生产者/消费者语义的队列（`AllocTensor`/`EnQue`/`DeQue`/`FreeTensor`），用于让「搬入」和「计算」两件事异步重叠（流水线化），隐藏搬运延迟。
- **`__global__ __aicore__`**：kernel 入口函数的修饰符，类比 CUDA 的 `__global__`。它不是被 C++ 代码直接 call 的，而是由算子执行框架按 blockDim 启动到每个核上。
- **`if constexpr`**：编译期 if。tiling key 的分支就是在编译期实例化出不同的 kernel 二进制变体，运行期按 key 选择，没有任何运行时开销。

前置讲义承接：u2-l2 讲过「tiling data 是 host 填 device 读的数据合同」「tiling key 按 dtype 路由 kernel 变体」，本讲将在 device 侧看到这两个概念的接收端。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/add_example/op_kernel/add_example.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.cpp) | kernel 入口：定义 tiling key 枚举，按 schMode 模板参数实例化并启动算子类 |
| [examples/add_example/op_kernel/add_example.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.h) | 算子类 `NsAddExample::AddExample<T>`：Init/CopyIn/Compute/CopyOut/Process 全部实现 |
| [examples/add_example/op_kernel/add_example_tiling_data.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example_tiling_data.h) | tiling data 结构体定义（host 与 device 共享的「数据合同」） |
| [examples/add_example/op_kernel/add_example_tiling_key.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example_tiling_key.h) | tiling key 的模板参数声明与选择宏（二进制变体的路由表） |
| [examples/add_example/op_host/add_example_tiling.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp) | host 侧 tiling（u2-l2 已精读，本讲只看它如何「填合同」） |
| [examples/add_example/tests/ut/op_kernel/test_add_example.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/test_add_example.cpp) | kernel 的 CPU 侧单测：构造 tiling、以 `ICPU_RUN_KF` 直接运行 kernel |
| [examples/add_example/tests/ut/op_kernel/add_example_data/gen_data.py](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/add_example_data/gen_data.py) / [compare_data.py](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/add_example_data/compare_data.py) | UT 的输入/期望值生成与结果比对脚本（本讲实践的验证工具） |

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：① kernel 入口与执行骨架；② tiling 机制（data + key 的 host/device 传递）；③ GM/Local 内存搬运与 Vector 计算。

### 4.1 kernel 入口与执行骨架

#### 4.1.1 概念说明

一个 Ascend C 算子的 device 侧代码分两层：

- **入口函数**（add_example.cpp 里的 `add_example<schMode>`）：与执行框架的对接点，负责取出 tiling data、按 tiling key 选择具体实现。
- **算子类**（add_example.h 里的 `AddExample<T>`）：真正的计算逻辑，用「Init 初始化 → Process 主循环」两段式组织，Process 内部再拆成 CopyIn/Compute/CopyOut 三步。

这种「入口薄、类厚」的分层让同一个算子类可以被不同 tiling key 的入口复用（这里 float 和 int32 两个分支用的就是同一个类模板的不同实例化）。

#### 4.1.2 核心流程

```text
框架启动 kernel（blockDim=8 个核，每个核执行一遍下面的流程）
  └─ add_example<schMode>(x, y, z, workspace, tiling)   # 5 个 GM 地址参数
       ├─ REGISTER_TILING_DEFAULT / GET_TILING_DATA_WITH_STRUCT  # 从 GM 的 tiling 指针解出结构体
       ├─ if constexpr (schMode == 0) → AddExample<float>  实例
       ├─ if constexpr (schMode == 1) → AddExample<int32_t> 实例
       └─ 对选中实例依次调用：
            Init(x, y, z, &tilingData)   # 算 shape 分块、绑定 GM 缓冲、划分 UB 队列
            Process()                    # 循环 tileNum*BUFFER_NUM 次：CopyIn→Compute→CopyOut
```

注意参数表 `(x, y, z, workspace, tiling)` 是**所有** Ascend C 算子入口的统一签名：前面是算子自己的输入输出 GM 地址，最后两个固定是 workspace 和 tiling 的 GM 地址——host 侧算好的 tiling data 就躺在 `tiling` 指向的 GM 内存里。

#### 4.1.3 源码精读

kernel 入口全文只有 20 来行。[add_example.cpp:L18-L21](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.cpp#L18-L21) 定义 tiling key 枚举：float 对应 0，int32 对应 1，与 host 侧 tiling 写入的 key 值一一对应（见 4.2.3）。

[add_example.cpp:L23-L27](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.cpp#L23-L27) 是入口本体：`__global__ __aicore__` 修饰、模板参数 `schMode` 即编译期烤进二进制的 tiling key；`GET_TILING_DATA_WITH_STRUCT` 宏把 GM 上的 tiling 字节流解包成 `AddExampleTilingData` 结构体。

[add_example.cpp:L28-L37](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.cpp#L28-L37) 两个 `if constexpr` 分支：schMode 为 0 时实例化 `AddExample<float>`，为 1 时实例化 `AddExample<int32_t>`，随后都是同样的「实例获取 → Init → Process」三行。因为 `if constexpr` 是编译期裁剪，每个二进制变体里只存在一个分支的代码。

Process 主循环在 [add_example.h:L104-L113](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.h#L104-L113)：循环 `tileNum_ * BUFFER_NUM` 次，每次 CopyIn → Compute → CopyOut。这是教学用的**同步**写法（一次只处理一块）；工业级算子会把循环展开成「先搬 N 块、再边算边搬」的双缓冲流水，这里 `BUFFER_NUM = 2` 的双份队列就是为流水化预留的伏笔（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：不修改任何代码，仅通过阅读验证你对入口签名的理解——确认「5 个 GM 参数」与 host/UT 两侧的对应关系。

**操作步骤**：

1. 打开 [test_add_example.cpp:L78-L81](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/test_add_example.cpp#L78-L81)，这是 CPU 侧单测对 kernel 的直接调用点。
2. 对照 `ICPU_RUN_KF(add_example<0>, numBlocks, x, y, z, workspace, tiling)` 与入口签名 `(x, y, z, workspace, tiling)`，逐个参数标注：哪个是输入、哪个是输出、哪个本算子没用上。
3. 注意 `add_example<0>` 的显式模板实参 `0`，对应 `TILING_KEY_EXAMPLE_FLOAT`；`numBlocks = 8` 对应 host 侧 `SetBlockDim(BLOCK_DIM)` 里的 `BLOCK_DIM = 8`（[add_example_tiling.cpp:L27](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L27)）。

**需要观察的现象**：UT 里把**同一个**输入文件同时读进 x 和 y（[test_add_example.cpp:L62-L66](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/test_add_example.cpp#L62-L66)），所以这个测试实际算的是 `x + x`。

**预期结果**：你能在纸上写出 5 个参数的语义表；workspace 被分配了 16MB 但 kernel 完全没碰它（教学算子不需要中间内存）。

**待本地验证**：如需实际运行见 4.4 的编译命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么入口函数是模板函数 `add_example<schMode>`，而不是运行期读取一个 dtype 变量来分支？

**答案**：因为不同 dtype 需要不同的 `AddExample<T>` 实例化（`LocalTensor<T>`、`DataCopy` 的元素宽度都依赖 T），编译期实例化保证每个二进制变体只含一份代码、计算指令宽度在编译期确定；运行期分支则要求一个二进制同时携带所有 dtype 的代码且无法做类型特化优化。tiling key 正是「运行期选择编译期变体」的路由机制。

**练习 2**：入口的 5 个 GM 参数中，本算子实际使用了哪几个？

**答案**：x、y、z、tiling 四个（tiling 经 `GET_TILING_DATA_WITH_STRUCT` 解包使用）；workspace 未使用——host 侧仍固定申请了 16MB（[add_example_tiling.cpp:L93-L99](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L93-L99)），这是给需要中间内存的算子预留的通用机制。

### 4.2 tiling 机制：data 与 key 的 host/device 传递

#### 4.2.1 概念说明

u2-l2 讲过 host 侧如何生产 tiling，本讲看 device 侧如何消费。host → device 只传递两样东西：

1. **tiling data**：一段序列化字节流（本算子就是 16 字节：两个 int64），由框架放到 GM 的 `tiling` 地址，kernel 用宏解包回结构体。它承载「动态」信息——随输入 shape 变化的分块参数。
2. **tiling key**：一个整数，框架用它从多个预编译的二进制变体中**选出**一个来启动。它承载「离散」信息——dtype、layout 等需要不同代码路径的维度。

一个类比：tiling data 像施工图上的尺寸标注（每次施工可能不同），tiling key 像图纸编号（决定今天用哪一套模板）。

#### 4.2.2 核心流程

```text
host（add_example_tiling.cpp）                     device（op_kernel）
─────────────────────────────                     ─────────────────────
AddExampleTilingFunc(context)
  tiling->totalLength = N*C*H*W   ──序列化──►     GET_TILING_DATA_WITH_STRUCT
  tiling->tileNum     = 8                           → tilingData.totalLength / tileNum
  SetBlockDim(8)                  ──启动配置──►    8 个核各跑一遍入口函数
  dtype==FLOAT → SetTilingKey(                      框架按 key 选出 add_example<0>
    GET_TPL_TILING_KEY(..._SCH_MODE_0))             的二进制变体去执行
```

值得强调的时序：tiling 在 **aclnn GetWorkspaceSize 阶段或图编译期**执行一次，而 kernel 可能被执行成千上万次——所以 tiling 里只放「一次就能算定」的计划，kernel 内不做任何平台信息查询。

#### 4.2.3 源码精读

**数据合同本体**：[add_example_tiling_data.h:L19-L22](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example_tiling_data.h#L19-L22) 定义 `AddExampleTilingData { int64_t totalLength; int64_t tileNum; }`——只有两个成员。这个结构体放在 op_kernel 目录、被 host 的 [add_example_tiling.cpp:L20](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L20) 和 device 共同 include，就是 u2-l2 说的「host 填 device 读的合同」：字段顺序、类型就是隐式协议，两侧没有任何运行时校验，改了一侧不改另一侧就会读到错位数据。（顺带一提：该文件第 16-17 行的 include guard 沿用了 `ROTARY_POSITION_EMBEDDING_GRAD` 前缀——从别的算子复制文件时留下的痕迹，也侧面印证了「复制改一改」是本仓库算子开发的常见起点。）

**host 填合同**：[add_example_tiling.cpp:L119-L127](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L119-L127) 里 `context->GetTilingData<AddExampleTilingData>()` 拿到框架准备好的落点，memset 清零后写入 `totalLength` 与 `tileNum`，并 `SetBlockDim(8)`。

**host 写 key**：[add_example_tiling.cpp:L128-L139](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L128-L139) 按 dtype 分支：`DT_FLOAT` 走 `GET_TPL_TILING_KEY(ELEMENTWISE_TPL_SCH_MODE_0)`（即 0），`DT_INT32` 走 `..._SCH_MODE_1`（即 1）。

**key 的声明侧**：[add_example_tiling_key.h:L21-L28](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example_tiling_key.h#L21-L28) 用 `ASCENDC_TPL_ARGS_DECL` / `ASCENDC_TPL_SEL` 两个宏声明「本算子有一个名为 schMode 的 uint 模板参数，合法取值 {0, 1}」。这是新的模板化 tiling key 写法：host 侧的 `GET_TPL_TILING_KEY` 和 binary 编译系统都从这份声明生成路由表，保证「host 写的 key」与「device 编出的变体」天然对齐——替代了旧式手写 `#define TILING_KEY_XXX` 再各自维护的易错方式。

**device 读合同**：回到 [add_example.cpp:L26-L27](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.cpp#L26-L27)，`REGISTER_TILING_DEFAULT` 注册默认解包方式，`GET_TILING_DATA_WITH_STRUCT(AddExampleTilingData, tilingData, tiling)` 把 GM 上 `tiling` 指针的字节流重解释为结构体——此后 `tilingData.totalLength` 就是最初 host 算出的元素总数。

#### 4.2.4 代码实践

**实践目标**：亲手体会「合同两侧」的耦合关系——只改一侧会发生什么。

**操作步骤**：

1. 在 `AddExampleTilingData` 中把 `tileNum` 改名为 `tileCount`（[add_example_tiling_data.h:L19-L22](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example_tiling_data.h#L19-L22)）。
2. 只同步修改 device 侧读它的 [add_example.h:L61](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.h#L61)，故意不改 host 侧的 [add_example_tiling.cpp:L125](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L125)。
3. 编译：`bash build.sh --ophost --ops=add_example` 会直接编译失败（host 侧还在写 `tiling->tileNum`）——这就是唯一的保护：结构体成员名是编译期契约。
4. 把 host 侧也改好后编译通过；再思考：如果把 `int64_t tileNum` 改成 `int32_t`，两侧都能编译通过吗？结果还对吗？

**需要观察的现象**：步骤 3 的编译报错信息；步骤 4 中类型改动（int64→int32）**不会**触发编译错误（两侧都用新类型），但结构体布局变成 8+4 字节，若两侧不同步更新就会出现静默的数值错位。

**预期结果**：成员改名是编译期可见的契约、类型与布局是编译期不可见的契约——这正是「tiling data 无运行时校验」的含义。

**待本地验证**：编译命令需在配好 CANN toolkit 的环境执行（承接 u1-l4）。

#### 4.2.5 小练习与答案

**练习 1**：tiling data 和 tiling key 都是 host 传给 device 的信息，为什么拆成两个机制而不是把 dtype 也塞进 tiling data？

**答案**：二者变更「粒度」不同。dtype 决定走哪份**代码**（模板实例化、指令宽度），必须在编译期确定，所以用 key 选变体；totalLength 等是同一份代码内的**数据**，运行期从 GM 读即可。若把 dtype 放进 tiling data，kernel 就得在一个二进制里携带所有 dtype 分支且无法做类型特化，体积和性能都受损。

**练习 2**：本算子 float 和 int32 两个变体的 tiling data 完全相同（都是 totalLength + tileNum）。如果某算子 float 路径需要额外的 scale 参数而 int32 不需要，该怎么办？

**答案**：把 scale 作为 `AddExampleTilingData` 的一个字段（float 分支由 host 填有效值、int32 分支填 0 或忽略），结构体是全变体共享的；或者更彻底地按 key 拆成两个不同的 tiling data 结构（本仓库工业级算子常用多个 tiling data 结构配多个 key 的做法）。

### 4.3 GM 与 Local 内存：搬运、队列与 Vector 计算

#### 4.3.1 概念说明

算子类 `AddExample<T>` 的全部工作可以概括为一张「内存地图」：

- **GM 侧**：三个 `GlobalTensor<T>` 分别绑定 x、y、z 的本核分段地址。
- **Local（UB）侧**：三条 `TQue` 队列（两条 VECIN 输入、一条 VECOUT 输出），每条队列有 `BUFFER_NUM=2` 份缓冲。
- **切分规则**：8 个核均分 totalLength（`blockLength_`），每核内再切成 `tileNum * BUFFER_NUM` 块（`tileLength_`），一块一块过队列。

队列的 Alloc/EnQue/DeQue/Free 四个动作构成生产者-消费者协议：CopyIn 是「UB 数据的生产者」，Compute 是「消费者」；双份缓冲让第 i 块计算时第 i+1 块可以同时搬入——这就是 `BUFFER_NUM = 2`（双缓冲）存在的意义。

#### 4.3.2 核心流程

Init 阶段的一次除法链决定了所有切分：

\[ \text{tileLength} = \frac{\text{totalLength} / \text{blockDim}}{\text{tileNum} \times \text{BUFFER\_NUM}} \]

以 UT 的 shape (32,4,4,4) 为例：totalLength = 2048，blockDim = 8，tileNum = 8，BUFFER_NUM = 2，则每核 256 元素、每块 16 元素，每核循环 16 次。

每轮循环：

```text
CopyIn(i):   AllocTensor ← x/y 两块 UB；DataCopy 从 GM 第 i 块搬入；EnQue
Compute(i):  DeQue 取 x/y；Alloc z；Add(z, x, y)；z EnQue；x/y FreeTensor
CopyOut(i):  DeQue 取 z；DataCopy 写回 GM 第 i 块；FreeTensor
```

#### 4.3.3 源码精读

**成员与队列声明**：[add_example.h:L42-L54](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.h#L42-L54)——`TPipe pipe` 管理 UB 空间；两条 `TQue<QuePosition::VECIN, 2>` 输入队列、一条 `TQue<QuePosition::VECOUT, 2>` 输出队列；三个 `GlobalTensor<T>` 是 GM 侧视图；`BUFFER_NUM` 常量定义在 [add_example.h:L27](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.h#L27)。

**Init：切分 + 绑 GM + 划 UB**：[add_example.h:L57-L71](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.h#L57-L71)。第 60-62 行做上面的除法链；第 64-66 行 `SetGlobalBuffer` 把每个 GlobalTensor 绑到「基址 + 本核偏移」——`blockLength_ * GetBlockIdx()` 就是核间均分的实现，`GetBlockIdx()` 是当前核编号；第 68-70 行 `pipe.InitBuffer` 给三条队列各划 `tileLength_ * sizeof(T) * BUFFER_NUM` 的 UB 空间。

**CopyIn：GM → UB**：[add_example.h:L73-L82](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.h#L73-L82)。`AllocTensor` 从队列领一块空 UB 缓冲，`DataCopy(xLocal, inputGMX[progress * tileLength_], tileLength_)` 从 GM 偏移处搬 `tileLength_` 个元素进 UB，`EnQue` 把填好的缓冲挂到队列上（同时触发同步语义：DeQue 侧会等数据真正到位）。

**Compute：UB 上的 Vector 计算**：[add_example.h:L92-L102](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.h#L92-L102)。核心只有一行 [add_example.h:L98](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.h#L98)：`AscendC::Add(zLocal, xLocal, yLocal, tileLength_)`——Vector 加法指令，一次处理整块。用完的输入缓冲 `FreeTensor` 归还队列供下轮 Alloc 复用。

**CopyOut：UB → GM**：[add_example.h:L84-L90](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.h#L84-L90)。`DeQue` 取出已算好的 z 块（保证写入完成），`DataCopy` 写回 GM 对应偏移，再 `FreeTensor`。

把这四段连起来读一遍，你会发现一个 Ascend C Vector 算子的「血液循环」：GM 是心脏外的血库，UB 是心脏，队列是瓣膜，DataCopy 是血流，Vector 指令是收缩。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：把 kernel 从 `z = x + y` 改造成 `z = (x + y) * 2`，并判断 host/tiling 侧是否需要连带修改——这是「改一个算子」的最小完整体验。

**操作步骤**：

1. 修改 Compute：在 [add_example.h:L98](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.h#L98) 的 `Add` 之后追加一行 Vector 标量乘（示例代码）：

   ```cpp
   AscendC::Add(zLocal, xLocal, yLocal, tileLength_);
   AscendC::Muls(zLocal, zLocal, static_cast<T>(2), tileLength_);  // 新增：整体乘 2
   ```

   `Muls` 是 Ascend C 的「tensor × 标量」指令，签名与 `Add` 同构。
2. **判断连带影响**，逐项检查：
   - tiling data：分块只依赖元素个数，`(x+y)*2` 不改变元素个数 → **无需修改**；
   - tiling key：dtype 没变、没有新增代码变体（Muls 对 float/int32 都存在于同一份模板）→ **无需修改**；
   - workspace：没有新增中间内存需求 → **无需修改**；
   - infershape/def：输出 shape 与 dtype 不变 → **无需修改**。
   结论：**只改 op_kernel/add_example.h 一个文件**。这正是分层范式的价值——计算逻辑变化被封闭在 device 侧。
3. 同步修改期望值：[gen_data.py:L34](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/add_example_data/gen_data.py#L34) 的 golden 计算改为 `tmp_golden = np.add(tmp_input, tmp_input) * 2`（不改这里，UT 必然 FAILED）。
4. 编译 kernel：`bash build.sh --opkernel --soc=ascend910b --ops=add_example`（`--opkernel` 必须搭配 `--soc`，承接 u1-l4）。
5. 运行 kernel UT：`bash build.sh --opkernel_test --soc=ascend910b --ops=add_example`。UT 内部会自动执行 `gen_data.py` 生成输入/golden、用 `ICPU_RUN_KF` 跑 kernel、落盘 output 再调 `compare_data.py` 比对（调用点在 [test_add_example.cpp:L61](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/test_add_example.cpp#L61) 与 [L93](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/test_add_example.cpp#L93)）。

**需要观察的现象**：UT 结束打印 `PASSED!` 与 `compare result: True`；若忘了步骤 3，会看到 `FAILED!` 并列出前 5 个不匹配下标——output 恰好是 golden 的 2 倍。

**预期结果**：编译产物出现在 build 树的 kernel 输出目录；UT 通过即证明改动正确。注意 gen_data.py 的输入集包含 `np.nan` 与 `np.inf`（[gen_data.py:L30-L32](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/add_example_data/gen_data.py#L30-L32)），乘 2 后 NaN/Inf 语义不变，`np.isclose` 仍按相等处理，不会引入误报。

**待本地验证**：以上命令需在装有配套 CANN toolkit 的环境（物理机/Docker/CANNLab，承接 u1-l3）执行；无 NPU 时 `--opkernel_test` 走 CPU 仿真执行路径，具体可用性待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果把 UT 的 shape 从 `(32,4,4,4)` 改成 `(8,4,4,4)`（totalLength=512），不改任何 kernel 代码，还能整除吗？哪里会出问题？

**答案**：每核 512/8 = 64 元素，再除 `tileNum(8) × BUFFER_NUM(2)` = 16 得 `tileLength_ = 4`，仍然整除，kernel 可正常运行。但若 totalLength 不能被 `blockDim × tileNum × BUFFER_NUM = 128` 整除（如 totalLength = 100），整除截断会导致尾部元素永远不被处理——工业级算子会在 tiling 里计算「尾块」并用 `tileLength` 与「最后一块实际长度」两个量处理残块，本教学算子为简化没有做。

**练习 2**：为什么输入队列有两条（X、Y 各一），而不是把 x、y 搬进同一块 UB 再计算？

**答案**：`AscendC::Add` 的三个操作数各自独立寻址，两条队列让两次 `DataCopy` 可以并行发起、各自双缓冲；若共用一块则要先搬 x 再搬 y，串行化搬运且需要手动管理偏移。队列模型把「缓冲分配 + 同步」交给框架，是 Ascend C 相对裸指针写法的核心抽象。

**练习 3**：`CopyOut` 里 `DeQue` 之后才 `DataCopy` 回 GM，如果省略 DeQue 直接用上一步 Alloc 的指针会怎样？

**答案**：`EnQue/DeQue` 对携带同步语义（保证前序 Vector 写入对后续搬运可见）。跳过 DeQue 拿指针属于未定义行为——搬运可能读到还没算完的数据。队列四步（Alloc→EnQue→DeQue→Free）的顺序是协议，不能精简。

## 5. 综合实践

把本讲三个模块串起来做一次「kernel 侦探」任务：

1. **读合同**（4.2）：从 [add_example_tiling_data.h:L19-L22](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example_tiling_data.h#L19-L22) 出发，画出 `totalLength=2048` 时从 host 填值到 device `Init` 消费的完整数据流图，标注每一步的数值变化（2048 → blockLength 256 → tileLength 16）。
2. **读路由**（4.1/4.2）：写出 dtype=float32 时 tiling key 从 host 写入到 `add_example<0>` 变体被选中的完整链路，指出链路上有几个「编译期」节点、几个「运行期」节点。
3. **改kernel**（4.3）：完成 4.3.4 的 `(x+y)*2` 改造并通过 UT。
4. **写结论**：用 200 字总结「修改一个算子的计算逻辑时，哪些文件必然要动、哪些大概率不用动、判断依据是什么」。

## 6. 本讲小结

- Ascend C 算子 device 侧 = **薄入口**（`__global__ __aicore__` 函数，统一 5 参数签名 x/y/z/workspace/tiling）+ **算子类**（Init 初始化、Process 内 CopyIn→Compute→CopyOut 循环）。
- host→device 只传两样东西：**tiling data**（GM 字节流，结构体是两侧共享的无校验合同）和 **tiling key**（整数，运行期路由到编译期实例化的二进制变体，新式 `ASCENDC_TPL_ARGS_DECL` 宏统一维护这张路由表）。
- **内存模型**：Vector 计算只能发生在 UB（Local）上，所以 kernel 永远是「GM→UB→计算→UB→GM」；`TPipe`/`TQue` 用 Alloc/EnQue/DeQue/Free 四步协议管理 UB 缓冲并支撑双缓冲流水。
- 核间并行靠 `SetBlockDim` + `GetBlockIdx()` 偏移实现均分；核内并行靠 tileNum 切块 + 队列流水。
- 改计算逻辑（如 `(x+y)*2`）通常**只需改 op_kernel**，tiling/key/def/infershape 因 shape、dtype、变体数均未变而无需连带修改——这是五层范式分层封闭性的直接体现。
- kernel UT 用 `ICPU_RUN_KF` 在 CPU 侧直接跑 kernel 入口，配合 gen_data.py/compare_data.py 构成最小验证闭环，`--opkernel_test` 是其运行入口。

## 7. 下一步学习建议

下一讲（u2-l4 运行算子示例）将把视角拉回调用方：用 `bash build.sh --run_example add_example` 分别以 eager（aclnn 两段式直调）和 graph（GE 图执行）方式真正把本讲的 kernel 在 NPU/simulator 上跑起来，你会看到 host 侧 tiling 与 device 侧 kernel 如何在一次真实调用中被串起。建议提前浏览 [examples/add_example/examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_aclnn_add_example.cpp) 中 GetWorkspaceSize/Run 的调用顺序。若想深入 Ascend C 指令与内存模型的官方规范，可结合 CANN 文档中心的 Ascend C 教程对照本讲的 `DataCopy`/`Add`/TQue 章节阅读。
