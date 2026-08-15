# 从教学样例到生产算子：gelu 内核源码阅读

## 1. 本讲目标

前面两讲（u5-l1、u5-l2）我们以 `examples/add_example` 为标本，掌握了矢量算子 Kernel 的「CopyIn-Compute-CopyOut」三段式结构、TQue 双缓冲与数据搬运细节。但 add_example 是**教学样例**：所有东西都手写，从 tiling 切分到队列管理一览无余，代价是代码重复度高、性能写法朴素。

本讲把镜头切到**真实生产算子** `activation/gelu`，学完后你应该能：

1. 说出 gelu 与 add_example 在工程结构上的本质差异：手写 Kernel 类 vs 描述计算 DAG + 复用公共调度框架。
2. 理解 `arch35` 这类多架构适配目录的组织方式，以及 CMake 中 `SUPPORT_TILING_DIR` 如何把「芯片代际」映射到「tiling 实现目录」。
3. 看懂生产算子 kernel 中「双模板参数（schMode + dType）+ `if constexpr` 分发」的分支组织，以及 Host 侧 `GET_TPL_TILING_KEY` 与 Device 侧模板实参的对应关系。
4. 初识 MicroAPI 寄存器级编程：生产算子如何用 `RegTensor` + `UpdateMask` 直接操纵矢量寄存器换取性能。

## 2. 前置知识

阅读本讲前，请先回忆（或复习）以下概念，它们都来自前几讲：

- **三段式流水**：CopyIn（GM→UB）→ Compute（UB 上计算）→ CopyOut（UB→GM），见 u5-l1。
- **TilingData 契约**：Host 写、Device 按字节读的 POD 结构体，两侧 include 同一个头文件，见 u4-l2。
- **TilingKey**：uint64 的运行期二进制选择器，kernel 入口用 `if constexpr` 按它为每个取值生成一份专用二进制，见 u4-l2。
- **`ASCENDC_TPL_ARGS_DECL`**：声明模板参数取值集合的宏，Host 用 `GET_TPL_TILING_KEY` 编码、Device 用模板实参解码，见 u4-l2。

本讲新增两个背景概念：

- **DAG（有向无环图）**：生产算子把「搬入 → 类型转换 → 计算 → 类型转换 → 搬出」这条数据加工链描述成一个类型层面的 DAG，调度框架按 DAG 自动生成搬运与计算的流水编排。算子作者只描述「做什么」，不必手写「怎么搬」。
- **MicroAPI**：比 `AscendC::Add` 这类高阶矢量接口更底层的编程层，直接操作 `RegTensor`（矢量寄存器的抽象）与 `MaskReg`（逐元素掩码），一次处理一个寄存器宽度的数据（`VECTOR_REG_WIDTH` 位）。生产算子用它榨取峰值性能。

还有一点必须先澄清：gelu 引用的调度框架头文件（如 `atvoss/elewise/elewise_sch_16b.h`、`atvoss/util/dag.h`）**不在本仓库内**，它们来自 CANN 包安装目录（编译时经 `op_common/atvoss/...` 路径找到）。本仓库提供的是「算子侧的描述与定制」，框架本身是 CANN 交付的公共设施——这正是生产算子与教学样例最大的分工差异。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [activation/gelu/op_kernel/gelu_apt.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/gelu_apt.cpp) | kernel 入口函数：读 tiling、按 dType 分发到调度器 `ElementwiseSch16B` |
| [activation/gelu/op_kernel/arch35/gelu_struct.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/arch35/gelu_struct.h) | 声明 gelu 的模板参数取值集合（schMode × dType） |
| [activation/gelu/op_kernel/arch35/gelu_dag.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/arch35/gelu_dag.h) | 用类型 DAG 描述 gelu 的计算链，含 MicroAPI 实现的 `GeluCustom` |
| [activation/gelu/op_host/arch35/gelu_tiling_arch35.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35/gelu_tiling_arch35.h) / [.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp) | Host 侧 tiling：校验 + 复用 `ElewiseBaseTiling` + 编码 tiling key |
| [activation/gelu/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/CMakeLists.txt) | 声明支持的芯片类型与 tiling 目录映射（arch35 机制的入口） |
| [activation/gelu/op_host/gelu_def.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp) | 算子原型定义（u3-l1 已讲，本讲只看它与 kernel 的衔接点） |
| [activation/gelu/README.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/README.md) | 算子功能、计算公式、产品支持矩阵 |
| [activation/gelu/tests/ut/op_kernel/test_gelu_apt.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/ut/op_kernel/test_gelu_apt.cpp) | kernel 仿真 UT，是本讲实践的运行载体 |

## 4. 核心概念与源码讲解

### 4.1 生产算子的工程结构：与 add_example 逐目录对比

#### 4.1.1 概念说明

u1-l3 已经给出目录「合同」：`op_host`（Host 交付件）、`op_kernel`（AI Core 交付件）、`op_api`（aclnn 适配）、`op_graph`（图模式）、`tests`、`examples`。本节不看「有哪些目录」，而看**同一类交付件内部，教学样例与生产算子的写法差在哪**。

gelu 的功能是高斯误差线性单元激活函数（见 [README.md:14-22](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/README.md#L14-L22)）：

$$
\text{out} = 0.5 \times x \times \left(1 + \tanh\left(\sqrt{2/\pi}\,(x + 0.044715\,x^3)\right)\right)
$$

这是一个标准的逐元素（elementwise）一元算子——和 add_example 同属矢量算子家族，因此对比才有意义：**同一个问题域，两种工程化程度悬殊的解法**。

#### 4.1.2 核心流程

先给结论表，后面各节逐项展开：

| 维度 | add_example（教学） | gelu（生产） |
| --- | --- | --- |
| kernel 主体 | 手写 `AddExample<T>` 类，自己管 TPipe/TQue | 描述 `GeluDAG`，复用 CANN 调度器 `ElementwiseSch16B` |
| 计算实现 | `AscendC::Add` 高阶接口 | MicroAPI 寄存器级（`RegTensor`/`MaskReg`） |
| TilingData | 自定义 `AddExampleTilingData` | 复用公共 `EleBaseTilingData16B` |
| Host tiling | 手写两级切分 | 校验 + 委托 `ElewiseBaseTiling` |
| 模板参数 | 1 个（T，对应 dtype） | 2 个（schMode + dType） |
| 架构适配 | 无（单套代码） | `arch35` 子目录按芯片代际隔离 |
| 精度策略 | 直接用 T 计算 | half/bf16 先 Cast 到 float 计算，再 Cast 回去（RINT 取整） |

#### 4.1.3 源码精读

先看 README 声明的支持范围——注意一个细节：[README.md:5-12](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/README.md#L5-L12) 列出从 Ascend 950 到 Atlas 训练系列的六代产品，但仓库内 [gelu_def.cpp:42](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp#L42) 只 `AddConfig("ascend950", ...)`。这不是矛盾：**老芯片上的 gelu 由 CANN 内置算子库提供，本仓库维护的是 ascend950（新一代架构）上的开源实现**。README 的支持矩阵描述的是「Gelu 这个算子」在全产品线的支持情况，而非「这份源码」的编译范围。

def 文件中与本讲相关的只有一行衔接点：

[activation/gelu/op_host/gelu_def.cpp:41](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp#L41)

```cpp
.ExtendCfgInfo("opFile.value", "gelu_apt");
```

它把算子绑定到名为 `gelu_apt` 的 kernel 入口文件（即 `op_kernel/gelu_apt.cpp`）——这就是 def 声明与 kernel 实现之间唯一的显式绳索（u3-l1 讲过 `opFile.value`）。

#### 4.1.4 代码实践

实践目标：建立「同一算子、两种工程」的结构直觉。

1. 并排打开 [examples/add_example/op_kernel/add_example.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h)（126 行）和 [activation/gelu/op_kernel/gelu_apt.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/gelu_apt.cpp)（47 行）。
2. 数一数：add_example.h 里有 `TPipe`、`TQue`、`AllocTensor/FreeTensor`、`InitBuffer` 吗？gelu_apt.cpp 里有吗？
3. 记录你的发现：gelu 的 kernel 入口文件里**一行队列管理代码都没有**——这些都被推给了调度框架。

预期结果：gelu 入口只有「读 tiling → 按类型构造调度器 → `Init` + `Process`」三步，全部流水细节封装在 `ElementwiseSch16B` 内部（CANN 包提供，本仓库不可见其实现）。

#### 4.1.5 小练习与答案

**练习 1**：README 支持矩阵列了六代产品，为什么仓库 def 里只配置了 ascend950？

**答案**：ops-nn 仓库对 gelu 只维护新一代架构（ascend950，对应 arch35）的开源实现；老产品上的 Gelu 由 CANN 内置算子库（安装包里的 opp 目录）提供。README 描述的是算子全线支持情况，def 的 `AddConfig` 描述的是本仓库源码的编译交付范围。

**练习 2**：不看 def 文件，你能从哪个文件名猜出 kernel 入口在 `gelu_apt.cpp` 吗？

**答案**：反过来推不行——`opFile.value` 是 def→kernel 的单向绑定，kernel 文件名本身不承载注册信息。这正是 u3-l1 强调的：def 是算子的「身份证」，`ExtendCfgInfo("opFile.value", "gelu_apt")` 是身份证上的住址。

### 4.2 多架构适配：arch35 目录与 TILING_DIR 机制

#### 4.2.1 概念说明

不同代际的 Ascend 芯片（910b、910_93、950 等）在核心数量、UB 大小、指令集上都有差异。当一套 Host 逻辑无法通吃所有代际时，就需要**按架构分目录存放不同实现**。gelu 采用的方式是：通用文件放在 `op_host/`、`op_kernel/` 根下，新一代架构专属的实现放进 `arch35/` 子目录（35 对应 ascend950 这一代的架构版本号）。

#### 4.2.2 核心流程

arch35 目录不是「约定俗成」，而是 CMake 显式声明的映射：

```
SUPPORT_COMPUTE_UNIT:  ascend950  mc62        ← 支持的芯片短名列表
SUPPORT_TILING_DIR:    arch35     arch35      ← 与芯片一一对应的 tiling 目录
```

构建系统按下标把 `ascend950 → arch35`、`mc62 → arch35` 配对，编译时取 `op_host/arch35/` 下的 tiling 文件参与构建。于是目录结构成为一张「芯片 → 实现」的路由表。

#### 4.2.3 源码精读

[activation/gelu/CMakeLists.txt:11-15](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/CMakeLists.txt#L11-L15)

```cmake
set(SUPPORT_COMPUTE_UNIT "ascend950" "mc62")
set(SUPPORT_TILING_DIR "arch35" "arch35")
add_modules_sources(HOSTNAME ${OPHOST_NAME} MODE PRIVATE DIR ${CMAKE_CURRENT_SOURCE_DIR} OPTYPE gelu
                    ACLNNTYPE aclnn_exclude COMPUTE_UNIT ${SUPPORT_COMPUTE_UNIT}
                    TILING_DIR ${SUPPORT_TILING_DIR} DISABLE_IN_OPP TRUE)
```

这段 CMake 做了四件事：声明芯片支持范围（ascend950 与 mc62）；声明每款芯片用哪个 tiling 目录（都是 arch35）；`ACLNNTYPE aclnn_exclude` 表示本算子不随仓库构建生成 aclnn 适配层（走 CANN 内置的 aclnnGelu，见 u2-l1 讲过的那份 `op_api/aclnn_gelu.cpp` 是文档样例配套）；`DISABLE_IN_OPP TRUE` 表示不编入 opp 内置包。

Host 侧的 arch35 痕迹在 tiling 文件里也能看到，注意它的 include 路径穿过了目录边界：

[activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp:16-17](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp#L16-L17)

```cpp
#include "../op_kernel/arch35/gelu_dag.h"
#include "../op_kernel/arch35/gelu_struct.h"
```

这是 u4-l2「TilingData 契约两侧 include 同一头文件」的再现：Host tiling 需要知道 DAG 的形状（用来给 `ElewiseBaseTiling` 推导切分），所以 Host 代码直接 include 了 kernel 目录下的 DAG 描述头文件。**arch35 下的 Host 与 Kernel 是一个整体交付单元**。

#### 4.2.4 代码实践

实践目标：确认 arch35 是构建路由而不是摆设。

1. 对比 [examples/add_example/op_host/config/ascend910b/](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp) 的目录组织（config 按芯片分目录放 binary json）与 gelu 的 arch35（按芯片分目录放 tiling 源码）。
2. 打开 [activation/gelu/op_host/config/ascend950/gelu_binary.json](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/config/ascend950/gelu_binary.json)，数一数 `op_list` 里有几份预编译二进制条目。

预期结果：binary json 里有 3 个条目（bfloat16/float16/float32 各一份，见 [gelu_binary.json#L4-L90](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/config/ascend950/gelu_binary.json#L4-L90)），印证 u3-l3 的结论「每个 dtype 槽位对应一份独立预编译二进制」；`shape: [-2]` 表示动态 shape。

#### 4.2.5 小练习与答案

**练习**：add_example 用 `config/<soc>/` 目录放 json，gelu 用 `arch35/` 目录放源码，两者都是「按芯片分目录」，本质区别是什么？

**答案**：`config/<soc>/` 存的是**编译产物清单**（每个 dtype 一份二进制的描述 json），源码仍是单套；`arch35/` 存的是**另一套源码实现**（tiling 逻辑、DAG 描述都可能是芯片专属的）。前者是交付描述的分芯片，后者是开发实现的分芯片——当架构差异大到「一份源码编多份」覆盖不了时，就必须用后者。

### 4.3 Kernel 入口与双模板参数：gelu_apt.cpp 精读

#### 4.3.1 概念说明

u5-l2 讲过 kernel 入口的固定形态：`__global__ __aicore__` 模板函数，形参为「输入/输出 GM 地址 + workspace + tiling」，模板参数即 tiling key 的取值维度。gelu 把模板参数从一个（dtype）扩展到两个：**schMode（调度模式）+ dType（数据类型）**，这是生产算子常见的分支组织方式——同一份计算逻辑，可能在不同调度策略（如是否带标量广播、是否 16B 对齐模式）下各有专用二进制。

#### 4.3.2 核心流程

```
Host 侧（gelu_tiling_arch35.cpp）            Device 侧（gelu_apt.cpp）
─────────────────────────────              ─────────────────────────
按输出 dtype 选 dType ∈ {1,2,3}     ←契约→   template <uint64_t schMode, uint64_t dType>
GET_TPL_TILING_KEY(1, dType)                 if constexpr (dType == TPL_FP16) ...
SetTilingKey(key)                            → 实例化 ElementwiseSch16B<schMode, GeluDAG<T>>
SetBlockDim(...)                             → sch.Init(x, y); sch.Process();
```

关键点：tiling key 的编码维度与 kernel 模板参数**一一对应**，框架按 key 值在预编译的多份二进制中选中那一份去执行。

#### 4.3.3 源码精读

先看 Device 侧的取值集合声明：

[activation/gelu/op_kernel/arch35/gelu_struct.h:21-29](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/arch35/gelu_struct.h#L21-L29)

```cpp
#define TPL_FP16 1
#define TPL_BF16 2
#define TPL_FP32 3

#define TPL_SCH_MODE_0 0
#define TPL_SCH_MODE_1 1

ASCENDC_TPL_ARGS_DECL(Gelu, ASCENDC_TPL_UINT_DECL(schMode, 1, ASCENDC_TPL_UI_LIST, TPL_SCH_MODE_0, TPL_SCH_MODE_1),
                      ASCENDC_TPL_DTYPE_DECL(dType, TPL_FP16, TPL_BF16, TPL_FP32));
```

这段声明了模板参数空间：schMode ∈ {0, 1}，dType ∈ {1, 2, 3}，理论组合 2 × 3 = 6 种。随后第 31-36 行的 `ASCENDC_TPL_SEL` 枚举出实际交付的选择组合（三组，每组内 schMode 仍是二选一）。这正是 u4-l2 所说「取值宏、kernel 枚举、Host 选 key 分支三处须人工对齐」的实物。

再看入口函数本体：

[activation/gelu/op_kernel/gelu_apt.cpp:26-46](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/gelu_apt.cpp#L26-L46)

```cpp
template <uint64_t schMode, uint64_t dType>
__global__ __aicore__ void gelu(GM_ADDR x, GM_ADDR y, GM_ADDR workspace, GM_ADDR tiling)
{
    REGISTER_TILING_DEFAULT(EleBaseTilingData16B);
    GET_TILING_DATA_PTR_WITH_STRUCT(EleBaseTilingData16B, tilingData, tiling);
    KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY);

    if constexpr (dType == TPL_FP16) {
        ElementwiseSch16B<schMode, GeluOp::GeluDAG<half>::OpDag> sch(tilingData);
        sch.Init(x, y);
        sch.Process();
    } else if constexpr (dType == TPL_BF16) {
        // bfloat16_t / float 分支同构，略
    }
    return;
}
```

与 add_example 对照，三个新面孔：

1. `GET_TILING_DATA_PTR_WITH_STRUCT`——u5-l2 见过的是 `GET_TILING_DATA_WITH_STRUCT`（还原为栈上值），这里是**指针版**：只拿到指向 tiling data 的指针，不复制整个结构体。tiling data 结构较大或使用频繁时省一次拷贝。
2. `KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY)`——向框架声明本 kernel 只用 AI Vector 核心，帮助运行时做核任务调度（对照：matmul 类算子会用 AIV+AIC 混合）。
3. `ElementwiseSch16B<schMode, OpDag>`——公共调度器。构造（传 tiling）→ `Init(x, y)`（绑定 GM 地址、初始化队列）→ `Process()`（驱动整条流水）。add_example 里手写的 `Init/Process/CopyIn/Compute/CopyOut` 全部被这两个调用取代。

最后看 Host 侧如何编码 key：

[activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp:99-107+122-125](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp#L99-L107)

```cpp
if (this->outputDtype == ge::DT_FLOAT16) {
    dType = TPL_FP16;
    baseTilingResult = elewiseBaseTiling.DoTiling<GeluOp::GeluDAG<half>::OpDag>(*tiling);
} // BF16/FP32 分支同理
...
const uint64_t tilingKey = GET_TPL_TILING_KEY(1, dType);
tilingContext->SetTilingKey(tilingKey);
tilingContext->SetBlockDim(elewiseBaseTiling.GetBlockDim());
```

注意 Host 的 `DoTiling<GeluDAG<T>::OpDag>` 也带着类型——**tiling 切分参数是按 DAG 的实际形状推导的**（需要几个 UB 缓冲、每步多宽，DAG 都「告诉」了 tiling 框架）。这把 u4-l1「手写两级切分」升级成了「框架按计算图自动切分」。

一个值得玩味的细节：UT 中 `ICPU_SET_TILING_KEY(1003)` 且调用 `::gelu<0, TPL_FP32>`（见 4.4.3），而 Host 侧固定 `GET_TPL_TILING_KEY(1, dType)`——从编码结果看 FP32 对应 1003（千位 1 对应第一个参数、个位 3 对应 dType），但 Device 模板实参的 schMode 却是 0。仿真 UT 对 schMode 的取值与 Host 编码路径是否严格一致，属于框架编码细节，**待本地验证**（可在真机打印 `tilingKey` 对照）。

#### 4.3.4 代码实践

实践目标：亲手跑一次 gelu 的 kernel 仿真 UT，验证双模板分发可用。

1. 在配套了 CANN 开发环境（含 ascend950 支持）的机器上，进入仓库根目录。
2. 执行 UT（命令形态见 u7-l1 详述，这里先给出最小形态，具体参数以 [docs/zh/install/compile.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/compile.md) 为准）：

   ```bash
   bash build.sh -u --ops=gelu --soc=ascend950
   ```

3. 观察输出中 `gelu_test.test_case_fp32_1` 是否 PASSED。

预期结果：UT 通过；若环境不支持 ascend950 编译则会报编译错误，此时退回「源码阅读型实践」（见综合实践）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`GET_TILING_DATA_PTR_WITH_STRUCT` 与 `GET_TILING_DATA_WITH_STRUCT` 的区别是什么？gelu 为什么选指针版？

**答案**：值版把 tiling data 整个拷贝到栈上，指针版只取地址。gelu 的 tiling data 随后要传给调度器 `ElementwiseSch16B` 长期使用，拷贝一份既浪费栈空间又可能引入两份数据不同步的风险，指针传递更合适。

**练习 2**：如果给 gelu 新增 DT_INT32 支持，模板参数空间会怎么变？要改哪几处？

**答案**：dType 增加 TPL_FP32 之外的一个取值（如 TPL_INT32 = 4），`ASCENDC_TPL_ARGS_DECL/SEL` 要扩充；kernel 入口要加一个 `if constexpr (dType == TPL_INT32)` 分支（且 DAG 里的 Cast 链要适配 int32）；Host tiling 要加对应的 dtype 判断与 `dType` 赋值；def 文件的 DataType 列表要放行 DT_INT32；binary json 会多一份条目。这与 u3-l1「三层闸门缺一不可」的结论一致。

### 4.4 计算内核的描述与实现：gelu_dag.h 与 MicroAPI

#### 4.4.1 概念说明

生产算子的核心思想是**把「计算逻辑」写成可被框架理解的声明（DAG），把「性能关键的内层」用 MicroAPI 手写**。gelu_dag.h 恰好一文件两貌：文件下半部分是类型层面的 DAG 声明（做什么、按什么顺序做），上半部分是 `GeluCustom` 的 MicroAPI 实现（怎么算得快）。

还有一层精度设计：half/bfloat16 的 GELU **不在原类型上直接计算**，而是先 Cast 到 float 算完再 Cast 回去——低精度类型的中间量（如 \(x^3\)）舍入误差大，用 float 做中间计算能显著保精度。README 公式与代码实现的对应关系在文件注释里写得很清楚（[gelu_dag.h:29-30](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/arch35/gelu_dag.h#L29-L30)）：tbe 实现是 \(x/(1+e^{-1.5957691(x + 0.044715x^3)})\)，当前实现整理为 \(x/(1+e^{-1.5957691 \times 0.044715\,(x/0.044715 + x^3)})\)，以便映射到 `Axpy`（乘加）指令。

#### 4.4.2 核心流程

gelu 的 DAG 是一条五节点链：

```
CopyIn<U>                     从 GM 搬入类型 U 的数据
   ↓
Cast<float, U>                U → float（half/bf16 提精度；U=float 时也走此节点，开销可忽略）
   ↓
GeluCustom<float>             MicroAPI 计算核心
   ↓
Cast<U, float, RINT>          float → U，RINT 表示四舍五入取整
   ↓
CopyOut<U>                    写回 GM
```

MicroAPI 内层循环按「一个寄存器宽度」为步长处理数据：`vl = VECTOR_REG_WIDTH / sizeof(T)` 是每步处理的元素数，`loopNum = ⌈count / vl⌉` 是循环次数，尾块靠 `UpdateMask(count)` 生成掩码遮住无效 lane——这与 add_example 用 `currentNum` 贯穿三段防越界是同一问题的两种解法（掩码 vs 显式长度）。

#### 4.4.3 源码精读

DAG 声明部分（类型即流程）：

[activation/gelu/op_kernel/arch35/gelu_dag.h:74-86](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/arch35/gelu_dag.h#L74-L86)

```cpp
template <typename U, typename T = float>
struct GeluDAG {
    using OpCopyIn0 = Bind<Vec::CopyIn<U>, Placeholder::In0<U>>;
    using OpCopyIn0Cast = Bind<Vec::Cast<T, U, CAST_MODE_NONE>, OpCopyIn0>;
    using OpLogResult = Bind<GeluDag1::GeluCustom<T>, OpCopyIn0Cast>;
    using OpResultCast = Bind<Vec::Cast<U, T, CAST_MODE_RINT>, OpLogResult>;
    using OpCopyOut = Bind<Vec::CopyOut<U>, Placeholder::Out0<U>, OpResultCast>;
    using Outputs = Elems<OpCopyOut>;
    using MemCfg = MemOptCfg<MemLevel::LEVEL_2>;
    using OpDag = DAGSch<Outputs, void, MemCfg>;
};
```

每个 `using` 声明一个节点，`Bind<算子, 上游节点>` 串成链。对照 add_example：`Vec::CopyIn` ≈ `CopyIn()` 方法，`Vec::CopyOut` ≈ `CopyOut()` 方法，而 `AscendC::Add(zLocal, xLocal, yLocal, currentNum)` ≈ `GeluCustom`。**三段式流水没有消失，只是从「手写方法调用」升格为「类型可组合的声明」**，框架据此自动生成队列、双缓冲和流水编排。

MicroAPI 计算核心：

[activation/gelu/op_kernel/arch35/gelu_dag.h:34-67](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/arch35/gelu_dag.h#L34-L67)（节选）

```cpp
__aicore__ inline GeluCustom(LocalTensor<T>& dst, LocalTensor<T>& src, uint32_t count)
{
    uint32_t vl = VECTOR_REG_WIDTH / dtypeSize;          // 每步处理的元素数
    uint16_t loopNum = CeilDivision(count, vl);
    MicroAPI::RegTensor<T, MicroAPI::RegTraitNumOne> vregInput;
    ...
    for (uint16_t loopIdx = 0; loopIdx < loopNum; loopIdx++) {
        mask = MicroAPI::UpdateMask<T, MicroAPI::RegTraitNumOne>(count);  // 尾块掩码
        MicroAPI::DataCopy(vregInput, (__ubuf__ T*)(srcAddr + loopIdx * vlSize));
        MicroAPI::Mul(vregInputSqr, vregInput, vregInput, mask);          // x²
        MicroAPI::Mul(vregInputCub, vregInputSqr, vregInput, mask);       // x³
        MicroAPI::Axpy(vregInputCub, vregInput, TANH_APPROX_FACTOR, mask);// x³ + x/0.044715
        MicroAPI::Muls(vregInputCub, vregInputCub, NEG_SQRT_EIGHT_OVER_PI, mask);
        MicroAPI::Exp(vregInputCub, vregInputCub, mask);                  // e^(...)
        MicroAPI::Adds(vregInputCub, vregInputCub, (float)1.0, mask);     // 1 + e^(...)
        MicroAPI::Div(vregOutput, vregInput, vregInputCub, mask);         // x / (...)
        MicroAPI::DataCopy((__ubuf__ T*)(dstAddr + loopIdx * vlSize), vregOutput, mask);
    }
}
```

读法要点：

- 数据从 UB（`__ubuf__` 指针）直接搬进矢量寄存器 `vregInput`，此后所有计算都在寄存器间进行，最后一步搬回 UB——比「高阶接口 + LocalTensor」少了一层抽象。
- `tanh` 被改写成 sigmoid 形式（\(\tanh(a) = 2\sigma(2a) - 1\) 的变形），使得整条链只需要 `Exp` 一条超越指令，规避了昂贵的 `Tanh`。
- `mask` 每轮用 `UpdateMask(count)` 重算，天然处理尾块——**不再需要 add_example 那样把 currentNum 从 CopyIn 一路传到 CopyOut**。
- 注意 `if constexpr (std::is_same_v<T, float>)` 包住了整个实现体：当前 MicroAPI 路径只服务 float；结合 DAG 里「先 Cast 到 float」的设计，half/bf16 最终都落到这份 float 实现。

UT 侧可以反向印证模板分发（[test_gelu_apt.cpp:56-64](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/ut/op_kernel/test_gelu_apt.cpp#L56-L64)）：手工填 `EleBaseTilingData16B`（`dim0=256, coreNum=1, ubFormer=1024`），设 `ICPU_SET_TILING_KEY(1003)`，然后**显式实例化** `::gelu<0, TPL_FP32>` 在 CPU 仿真器上跑——这三个 tiling 字段名（dim0/coreNum/ubFormer）也让我们窥见了公共 tiling 结构体的内容：总元素数、核数、UB 每轮处理量，与 add_example 手写的 totalNum/blockFactor/ubFactor 角色对应。

#### 4.4.4 代码实践

实践目标：把「DAG 链」和「三段式」两种写法在纸上一一对齐。

1. 画出 add_example 的数据流（五个框）：`GM x/y → UB xLocal/yLocal → Add → UB zLocal → GM z`。
2. 画出 gelu 的数据流（按 4.4.2 的五节点 DAG）：标注每步的 类型 与 所属节点。
3. 在两条图上用红笔标出差异点，至少应包括：①gelu 多了两个 Cast 节点；②add_example 的循环边界由 tiling 三字段手工控制，gelu 由调度器按 DAG 自动编排；③add_example 用 DataCopyPad + currentNum 处理尾块，gelu 用 UpdateMask 掩码；④add_example 计算在 LocalTensor 上，gelu 计算在寄存器上。
4. （可选，上机）把 `GeluCustom` 里的 `MicroAPI::Div` 换成 `Muls(vregOutput, vregInput, 1.0f, mask)`（即恒等乘），重跑 4.3.4 的 kernel UT。

预期结果：第 4 步的输出 bin 将不再是 GELU 的值（而是近似恒等/常数缩放），证明你确实改动了真正执行的计算核心；输出文件为 `gelu_data/output.bin`，可与 `tests/assets/golden.py` 的参考实现对比。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 half/bf16 要 Cast 到 float 再计算，而不是直接在原类型上算？

**答案**：GELU 中间量 \(x^3\) 与 \(\exp\) 的动态范围大，half（11 位有效精度）直接计算舍入误差会快速放大；float 中间精度能保证最终 round 回 half 时的精度。DAG 中的 `CAST_MODE_RINT`（四舍五入）进一步减少了回程转换的偏差。

**练习 2**：`Axpy(vregInputCub, vregInput, TANH_APPROX_FACTOR, mask)` 完成的数学操作是什么？为什么要用它替代直觉写法？

**答案**：`Axpy(y, x, a)` 即 \(y \leftarrow a \cdot x + y\)，这里完成 \(x^3 + x/0.044715\)（`TANH_APPROX_FACTOR = 1/0.044715`）。注释里说明了动机：把公式整理成 \(e^{-1.5957691 \times 0.044715 (x/0.044715 + x^3)}\) 的形状，正好能映射到「乘加」这条硬件指令，一条指令干两步乘加的活。

**练习 3**：UT 中 `tilingDatafromBin->ubFormer = 1024` 与 add_example 的哪个 tiling 字段角色相同？

**答案**：`ubFactor`——两者都表示「一轮循环放进 UB 的元素数」。名字不同（公共结构体 vs 自定义结构体），语义一致，这正是 u4-l2「TilingData 是数据契约、字段语义由两侧约定」的具体体现。

## 5. 综合实践

**任务：产出一份《add_example ↔ gelu 内核对照报告》**，这是本讲实践的正式交付物。

1. **结构对照**（30 分钟）：按第 3 节源码地图通读 gelu 的 5 个核心文件，对照下表逐格填写你自己的版本（不要照抄本讲结论，写你读到的证据行号）：

   | 对照项 | add_example | gelu | 证据（文件:行） |
   | --- | --- | --- | --- |
   | kernel 入口与模板参数 | | | |
   | tiling data 来源 | | | |
   | 搬入/搬出实现 | | | |
   | 计算实现层级 | | | |
   | 尾块处理策略 | | | |
   | 精度策略 | | | |
   | Host tiling 的切分职责 | | | |

2. **数据流图**（20 分钟）：完成 4.4.4 的两幅数据流对照图并标注差异点。
3. **验证一处怀疑**（30 分钟，可选上机）：本讲指出「UT 的 schMode 实参（0）与 Host 编码 `GET_TPL_TILING_KEY(1, dType)` 的第一参数（1）看似不一致」。请设计验证方法：例如在 `RunTiling` 里已有的 `OP_LOGD("[TilingData] : tilingKey=%lu")` 日志（[gelu_tiling_arch35.cpp:123](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp#L123)）基础上跑 arch35 tiling UT（[tests/ut/op_host/arch35/test_gelu_tiling.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/ut/op_host/arch35/test_gelu_tiling.cpp)），观察 FP16 输入下 tilingKey 的实际值，推断 key 各数位与模板参数的映射规则。若无环境，写下你的推断并标注「待本地验证」。

这份报告将是你日后阅读任何生产算子 kernel 的模板——先找入口、再找 DAG/调度器、最后钻进计算核心。

## 6. 本讲小结

- 生产算子与教学样例的差异不是「更多代码」，而是**分工重构**：kernel 入口只剩「读 tiling → 按类型构造调度器 → Init/Process」，队列管理与流水编排全部交给 CANN 包的 atvoss 调度框架（`ElementwiseSch16B`）。
- **arch35 目录是构建层声明的芯片路由**：CMake 的 `SUPPORT_COMPUTE_UNIT` 与 `SUPPORT_TILING_DIR` 按下标配对，把 ascend950/mc62 映射到 `op_host/arch35/` 下的 tiling 实现；config 目录分的是产物 json，arch 分的是源码实现。
- gelu 用**双模板参数**（schMode × dType）组织 kernel 分支：`ASCENDC_TPL_ARGS_DECL` 声明取值空间，Host 用 `GET_TPL_TILING_KEY` 编码，Device 用 `if constexpr` 生成专用二进制——是 u4-l2 单维 tiling key 的多维推广。
- 计算逻辑被描述成**类型 DAG**（CopyIn→Cast→GeluCustom→Cast→CopyOut），框架按 DAG 自动做 tiling（`DoTiling<GeluDAG<T>::OpDag>`）与流水编排；三段式没有消失，只是升格为声明。
- 性能写法下沉到 **MicroAPI 寄存器级**：`RegTensor` + `UpdateMask` 掩码处理尾块，公式整理以复用 `Axpy`/`Exp` 等廉价指令（tanh 改写为 sigmoid 形式）；精度写法上 half/bf16 一律 Cast 到 float 计算、RINT 取整写回。

## 7. 下一步学习建议

- 下一讲 **u5-l4（fast_kernel_launch 与 Cube 类算子）** 将把视角从单个 kernel 抬到「下发路径」层面，看调用侧如何榨性能，并初步接触 matmul 这类 Cube 算子与 ops-tensor 的分层结构。
- 若想再消化本讲内容，建议横向读一个同样走 atvoss 调度框架的 elementwise 算子（可在 `activation/` 目录下 grep `ElementwiseSch16B` 找同类），验证「DAG + 调度器」模式是否已能独立辨认。
- MicroAPI 与 VECTOR_REG_WIDTH 的完整定义在 CANN 包的头文件中（`atvoss/util/vec.h` 等，随 toolkit 安装），可在本机 `ASCEND_HOME_PATH` 下的 include 目录中找到并阅读。
- tiling 侧的深度内容（`ElewiseBaseTiling` 的切分算法）同样位于 CANN 包 `op_common/atvoss/elewise/elewise_tiling.h`，读完可回答「ubFormer=1024 是怎么算出来的」。
