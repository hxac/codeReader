# TilingData 与 TilingKey：host/device 数据契约与模板注册

## 1. 本讲目标

上一讲（u4-l1）我们理解了 Tiling 的「切分算法」：核切分与 UB 切分怎么算。本讲把镜头转向 Tiling 的「两份交付物本身」：

1. **TilingData**：一个 Host 侧写、Device 侧读的 POD 结构体，是 host 与 device 之间唯一的运行时数据契约——理解它如何定义、如何写入、如何传递、如何被 kernel 读取。
2. **TilingKey**：一个 uint64 整数编码，Host 侧设置、运行时据此挑选预编译 kernel 二进制、kernel 侧据此做 `if constexpr` 模板分支——掌握「tiling key 值 ↔ kernel 模板分支」三处必须严格对齐的对应关系。
3. **tiling 模板注册机制**：`common/inc/op_host/tiling_templates_registry.h` 提供的 `TilingBaseClass` 执行框架与 `REGISTER_OPS_TILING_TEMPLATE` 注册宏，理解生产算子如何按优先级注册多个 tiling 实现类并自动回退选择。

学完本讲，你应该能独立为算子新增一种 tiling 场景（新 tiling key + kernel 分支），并看懂生产算子里成排的 `REGISTER_*_TILING_TEMPLATE` 调用。

## 2. 前置知识

在进入源码前，先用通俗语言铺垫四个概念。

### 2.1 Host 侧与 Device 侧

- **Host**：指 CPU 侧，即 aclnn 调用、tiling 计算发生的的地方。C++ 编译成普通 x86/ARM 代码。
- **Device**：指 NPU 的 AI Core，执行 Ascend C kernel。Kernel 由 CANN 编译器编译成 NPU 指令。

两侧是**两套编译器、两套内存空间**，不能直接传 C++ 对象（没有虚表、没有 STL），只能通过约定好的字节布局交换数据。TilingData 就是这个字节布局。

### 2.2 POD 结构体

POD（Plain Old Data）指没有构造函数、虚函数、继承的纯数据结构，例如只含 `int64_t` 成员的 struct。它的内存布局就是「成员按声明顺序紧排」，所以 Host 侧把 POD 按字节拷走，Device 侧再按同样类型解释，就能原样还原。**TilingData 必须是 POD**，这是契约成立的前提。

### 2.3 模板与 `if constexpr`

C++ 函数模板 `template <uint32_t schMode> void f()` 在编译期按模板参数生成多份独立代码。`if constexpr (cond)` 表示「条件不满足的分支在编译期直接丢弃，根本不生成代码」。Ascend C kernel 用这一机制：为每个 tiling key 场景生成一份专用二进制，运行时无需在 NPU 上做动态判断。

### 2.4 上一讲的承接

u4-l1 已建立的事实，本讲直接使用：Tiling 运行在 Host 侧，由框架经 `gert::TilingContext` 回调，产出三样东西——**TilingData**（本讲 4.1）、**TilingKey**（本讲 4.2）、**BlockDim**；`SetTilingKey` 是 kernel 模板分支的「选择器」（float→0、int32→1）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `examples/add_example/op_kernel/add_example_tiling_data.h` | TilingData 结构体定义（host/device 共用的「合同文本」） |
| `examples/add_example/op_kernel/add_example_tiling_key.h` | tiling key 模板参数声明（schMode 的合法取值表） |
| `examples/add_example/op_host/add_example_tiling.cpp` | Host 侧：写 TilingData、SetTilingKey |
| `examples/add_example/op_kernel/add_example.cpp` | Device 侧：kernel 入口读 tiling data、按 schMode 分发 |
| `examples/add_example/op_kernel/add_example.h` | kernel 类消费 tiling 字段 |
| `common/inc/op_host/tiling_base.h` | 模板化 tiling 的执行框架 TilingBaseClass |
| `common/inc/op_host/tiling_templates_registry.h` | tiling 类注册表与注册宏 |
| `rnn/gru/op_kernel/gru_tiling_key.h` | 生产算子：多取值 tiling key 的真实样例 |
| `pooling/max_pool_with_argmax_v3/op_host/arch35/*` | 生产算子：多 tiling 类按优先级注册的真实样例 |

注意一个目录细节：两个 tiling 相关头文件放在 `op_kernel/` 下而不是 `op_host/` 下，正是因为它们要**同时被 host 编译单元和 device 编译单元 include**——合同文本由双方共同持有。

## 4. 核心概念与源码讲解

### 4.1 TilingData：Host 写、Device 读的数据契约

#### 4.1.1 概念说明

Tiling 是 Host 侧算出来的，但切分参数（每核处理多少元素、每次搬多少）是 kernel 执行时要用的。两边怎么通信？答案朴素得惊人：

> Host 把一个 POD 结构体按字节写进框架提供的 tiling 缓冲区；运行时把这块缓冲区的 **GM 地址**作为 kernel 入口的最后一个参数 `tiling` 传下来；kernel 把这段字节原样拷进局部内存，再 cast 回同一个结构体类型。

整个过程没有任何序列化框架，靠的就是「两侧 include 同一个头文件」来保证字节布局一致。

#### 4.1.2 核心流程

```text
[Host] AddExampleTilingFunc
  ├─ tiling = context->GetTilingData<AddExampleTilingData>()   // 拿到框架缓冲区的指针
  ├─ memset_s(tiling, ...) 清零                                  // POD 无构造函数，必须手动清
  ├─ tiling->totalNum / blockFactor / ubFactor = ...             // 按字节写入三个 int64_t
  └─ （框架把缓冲区内容随任务下发）
        │
        ▼  按字节拷贝，类型不变
[Device] add_example<schMode>(x, y, z, workspace, tiling)       // tiling 是 GM 地址
  ├─ REGISTER_TILING_DEFAULT(AddExampleTilingData)              // 注册默认结构体类型
  ├─ GET_TILING_DATA_WITH_STRUCT(AddExampleTilingData, tilingData, tiling)
  │      // 从 GM 拷字节 → 还原为局部变量 tilingData
  └─ op.Init(x, y, z, &tilingData)                              // kernel 类按字段消费
```

#### 4.1.3 源码精读

先看合同文本——结构体只有三个 `int64_t` 字段：

[examples/add_example/op_kernel/add_example_tiling_data.h:L19-L24](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example_tiling_data.h#L19-L24)——定义 `AddExampleTilingData`：`totalNum`（总元素数）、`blockFactor`（每核元素数）、`ubFactor`（每次 UB 搬运元素数），纯 POD，无任何成员函数。

> **阅读彩蛋**：注意该文件第 16-17 行的头文件保护宏是 `_ROTARY_POSITION_EMBEDDING_GRAD_TILING_DATA_H_`——一个来自 rotary_position_embedding_grad 算子的名字。这暴露了 add_example 是从生产算子脚手架复制改造而来的教学样例（`scripts/opgen/template/add_example/` 下有同款模板）。保护宏不冲突就不影响编译，但读源码时这类「化石」能告诉你代码的血统。

再看 Host 侧怎么写：

[examples/add_example/op_host/add_example_tiling.cpp:L205-L215](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L205-L215)——`context->GetTilingData<AddExampleTilingData>()` 拿到框架 tiling 缓冲区中本算子可写区域的指针；因为 POD 不会自动初始化，先用 `memset_s` 清零，再依次填入 `totalNum`、`blockFactor`（`CeilDiv(totalIdx, coreNum)` 核切分结果）。

最后是 Device 侧怎么读：

[examples/add_example/op_kernel/add_example.cpp:L39-L42](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp#L39-L42)——`REGISTER_TILING_DEFAULT(AddExampleTilingData)` 声明本 kernel 使用的默认 tiling 结构体；`GET_TILING_DATA_WITH_STRUCT(AddExampleTilingData, tilingData, tiling)` 把入口参数 `tiling`（GM 地址）指向的字节拷贝并还原为局部结构体 `tilingData`。

[examples/add_example/op_kernel/add_example.h:L57-L65](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L57-L65)——kernel 类的 `Init` 逐字段消费契约：用 `tilingData->blockFactor` 乘核号算出本核负责的 GM 窗口偏移，用 `totalNum - blockFactor * (GetBlockIdx() - 1)` 算尾核的实际长度。Host 写下的每个字段，在这里都有对应的读者。

契约的纪律只有两条：**字段只能追加语义、不能改语义**（改了要同步两侧）；**Host 写过的所有分支路径字段都必须有值**（所以先 memset 清零兜底）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「两侧 include 同一头文件」是契约成立的唯一保证。

1. 打开 `examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp`（u4-l1 已跑过该 UT），找到断言 `tilingData` 字段的用例。
2. 在 `AddExampleTilingData` 中**追加**一个字段 `int64_t usedCoreNum = 0;`（放在结构体末尾），并在 `AddExampleTilingFunc` 中给它赋值 `usedCoreNum`。
3. 重新编译 UT（`bash build.sh -u --ops=add_example`，具体命令以 `docs/zh/install/compile.md` 为准）观察用例是否仍通过；再在 UT 中新增一行断言检查新字段。
4. 把同一个字段**挪到结构体开头**（放在 `totalNum` 之前），不重编 kernel 只重跑 UT，观察现象。

**需要观察的现象**：步骤 3 一切正常（追加字段对两侧透明）；步骤 4 中如果存在按旧布局读取的编译产物，字段解释会整体错位——这正是「改布局必须两侧同编译」的直观体现。

**预期结果**：追加字段安全；调整字段顺序后必须全量重编（`--pkg`）才能保证正确。若无法本地运行，标注：待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AddExampleTilingData` 里不能放一个 `std::vector<int64_t>`？

**答案**：`std::vector` 含指向堆内存的指针，Host 侧的堆地址在 Device 上无意义，且它不是 POD，字节布局不保证跨编译器一致。变长数据在真实算子中用「定长数组 + 个数字段」或框架提供的可变长 tiling 容器（如 `max_pool_with_argmax_v3` 中 `tilingData_.SaveToBuffer(...)` 的序列化方式，见 4.3.3）表达。

**练习 2**：Host 侧写 tiling 前为什么要 `memset_s` 清零？

**答案**：POD 结构体的内存在缓冲区中是复用的裸内存，不含上次执行的残留值就是未定义内容；kernel 分支可能只读部分字段，清零保证任何未显式赋值的字段都是确定值（0），避免 kernel 读到脏数据。

**练习 3**：tiling data 是通过 kernel 入口哪个参数传到 Device 的？

**答案**：入口函数的最后一个参数 `tiling`（`GM_ADDR tiling`），它是 tiling 缓冲区的 GM 地址；kernel 用 `GET_TILING_DATA_WITH_STRUCT` 从该地址按字节拷贝还原。

### 4.2 TilingKey 的编码：从 schMode 到 kernel 模板分支

#### 4.2.1 概念说明

TilingKey 回答的问题是：**这次调用该加载哪一份 kernel 二进制？**

一个算子源码里往往有多份实现：不同 dtype（float/int32）、不同切分策略（是否切 M 维）、不同格式。编译器把它们编译成多份二进制，运行时根据 Host 侧 `SetTilingKey` 的值挑一份下发。ops-nn 的做法是用一组宏把「模板参数的取值组合」编码成一个 uint64：

- `ASCENDC_TPL_ARGS_DECL(算子名, ASCENDC_TPL_UINT_DECL(参数名, 位宽, 取值列表, v0, v1, ...))`：声明本算子 kernel 模板有一个 uint 模板参数、它的名字与合法取值集合。
- `GET_TPL_TILING_KEY(取值)`：Host 侧把某个取值编码成 tiling key。
- `ASCENDC_TPL_SEL(...)`：配套的选择声明。

这些宏定义在 CANN 工具链头文件 `ascendc/host_api/tiling/template_argument.h` 中（随 CANN 包安装，不在本仓库内），其内部位排布属于工具链实现细节，**待确认**；但「声明取值集合 → 编码成 key → key 对应模板实例」这层用法契约，从仓库内几十处一致调用即可完全确定。

#### 4.2.2 核心流程

以 add_example 为例，只有一个模板参数 `schMode`、两个合法取值 0/1 时，key 与取值恰好相等（0→0、1→1）。多参数时按位拼 接（示意）：

\[ \text{tilingKey} = \sum_{i} v_i \cdot 2^{b_i} \]

其中 \( v_i \) 是第 i 个模板参数的取值，\( b_i \) 是工具链为它分配的比特偏移。

三处对齐关系（本讲最重要的图）：

```text
add_example_tiling_key.h          add_example.cpp                    add_example_tiling.cpp
─────────────────────────         ────────────────────────────       ──────────────────────────────
ELEMENTWISE_TPL_SCH_MODE_0 = 0 ←→ TILING_KEY_EXAMPLE_FLOAT  = 0 ←→ GET_TPL_TILING_KEY(...MODE_0)  dtype==DT_FLOAT
ELEMENTWISE_TPL_SCH_MODE_1 = 1 ←→ TILING_KEY_EXAMPLE_INT32  = 1 ←→ GET_TPL_TILING_KEY(...MODE_1)  dtype==DT_INT32
        （声明取值集合）              （kernel 模板分支枚举）              （Host 按场景选 key）
```

三处只要有一处值对不上，就会出现「Host 选了 A 实现、运行时下发了 B 二进制」的静默错误。

#### 4.2.3 源码精读

先看取值声明：

[examples/add_example/op_kernel/add_example_tiling_key.h:L21-L28](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example_tiling_key.h#L21-L28)——为算子 `AddExample` 声明名为 `schMode` 的 uint 模板参数，合法取值列表为 `ELEMENTWISE_TPL_SCH_MODE_0(0)` 与 `ELEMENTWISE_TPL_SCH_MODE_1(1)`；`ASCENDC_TPL_SEL` 行是配套的选择声明，两行的取值列表必须一致。

Host 侧按 dtype 编码：

[examples/add_example/op_host/add_example_tiling.cpp:L228-L240](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L228-L240)——`GET_TPL_TILING_KEY(ELEMENTWISE_TPL_SCH_MODE_0)` 编码出 float 场景的 key 并 `context->SetTilingKey(tilingKey)`；`DT_INT32` 走 MODE_1；两者都不是则报错返回。这是 u4-l1 说过的「三道类型闸门」中的 tiling 一道。

Device 侧按模板参数分支：

[examples/add_example/op_kernel/add_example.cpp:L24-L27](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp#L24-L27)——kernel 侧用枚举 `AddExampleTilingKey` 固定 0=float、1=int32，与 tiling key 头文件的宏值一一对应（注意两侧是**各自独立定义**的，靠数值相等对齐，编译器不会替你检查）。

[examples/add_example/op_kernel/add_example.cpp:L36-L56](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp#L36-L56)——入口函数本身是 `template <uint32_t schMode>` 的模板；编译系统按 tiling key 头文件声明的取值集合为每个值实例化一份二进制；每份实例里 `if constexpr (schMode == ...)` 只保留命中分支，分别构造 `AddExample<float>` 或 `AddExample<int32_t>`。

再看一个**多取值真实样例**，说明这不是教学样例的专利：

[rnn/gru/op_kernel/gru_tiling_key.h:L21-L28](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/rnn/gru/op_kernel/gru_tiling_key.h#L21-L28)——GRU 算子声明模板参数 `mmSplit`，取值 `GRU_TPL_MM_FP16_SPLIT(0)` / `GRU_TPL_MM_FP32_SPLIT(1)`，结构与 add_example 完全同构。

[rnn/gru/op_host/gru_tiling.cpp:L220-L222](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/rnn/gru/op_host/gru_tiling.cpp#L220-L222)——Host 侧按场景选择 `GET_TPL_TILING_KEY(GRU_TPL_MM_FP16_SPLIT)` 或 `GRU_TPL_MM_FP32_SPLIT`，套路一致。

顺带回收 u3-l1 的知识点：def 文件里每个 dtype 槽位对应 binary json 中一份预编译二进制——那正是「按 tiling key 实例化的模板二进制」在交付包里的物理形态。

#### 4.2.4 代码实践

**实践目标**：体感验证「key 错位 = 静默错误」。

1. 在 `add_example_tiling.cpp` 中把 float 分支临时改为 `tilingKey = GET_TPL_TILING_KEY(ELEMENTWISE_TPL_SCH_MODE_1);`（int32 的 key）。
2. 重新 `--pkg` 编译、安装 run 包，运行 `bash build.sh --run_example add_example eager cust --vendor_name=custom`。
3. 观察输出 z 的值。
4. **改回原样**并重新编译验证恢复。

**需要观察的现象**：float 输入被按 int32 语义解释（两段 float 位模式被拼成一个 int32 的位模式再参与运算），输出完全错误但**不报错**——这就是 tiling key 对齐失败的危险之处：没有异常，只有错数。

**预期结果**：输出值与正确加法结果不符；恢复后输出正确。若本地无配套环境，标注：待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 kernel 入口用 `if constexpr` 而不是普通 `if`？

**答案**：`if constexpr` 在编译期裁剪分支，每份二进制只含一个场景的代码，NPU 上零分支开销；普通 `if` 会把 float 和 int32 两套模板实例都编进同一份二进制并在运行时判断，既浪费指令空间又拖慢执行。

**练习 2**：如果我新增 `ELEMENTWISE_TPL_SCH_MODE_2 = 2` 但忘了在 kernel 的 `AddExampleTilingKey` 枚举中加对应项，会发生什么？

**答案**：Host 可以 set 出 key=2，但 kernel 入口的两个 `if constexpr` 都不命中，该份二进制什么都不做，输出保持内存原值（静默错误）。所以新增场景必须「key 头文件、Host 分支、kernel 枚举与分支」三处同步（见第 5 节综合实践）。

**练习 3**：tiling key 与 u3-l1 讲的 def 文件 DataType 槽位是什么关系？

**答案**：def 的 DataType/Format 列表是「编译期」闸门，决定为哪些类型组合生成二进制；tiling key 是「运行期」闸门，决定本次调用挑哪份已生成的二进制。二者描述的候选集合必须一致，否则出现「def 放行了但 tiling 报错」或「tiling 选了但二进制不存在」。

### 4.3 tiling_templates_registry：模板化 tiling 的注册与优先级选择

#### 4.3.1 概念说明

add_example 用 `IMPL_OP_OPTILING(AddExample).Tiling(AddExampleTilingFunc)` 注册了**一个** tiling 函数。但生产算子常常需要**多个 tiling 实现**：不同 shape 特征、不同硬件架构各有一套最优策略，希望按「谁适用谁上，都不适用再兜底」的方式选择。`common/inc/op_host/tiling_templates_registry.h` 就是这套机制：

- **TilingBaseClass**：模板化 tiling 的基类，把 tiling 流程固化为 8 步框架（`tiling_base.h`）。
- **注册表**：按「架构号（或 soc 版本）→ 算子名 → 优先级 → tiling 类工厂」三级 map 组织。
- **注册宏**：`REGISTER_TILING_TEMPLATE` / `REGISTER_OPS_TILING_TEMPLATE` / `REGISTER_TILING_TEMPLATE_WITH_SOCVERSION` / `REGISTER_TILING_TEMPLATE_WITH_ARCH`，用全局静态对象在 main 之前完成注册（与 u3-l1 的 `OP_ADD` 同一手法）。
- **三态返回码**：`GRAPH_SUCCESS`（成功，结束）、`GRAPH_FAILED`（硬失败，中止）、`GRAPH_PARAM_INVALID`（本类不支持，试下一个）——这是优先级回退的核心协议。

#### 4.3.2 核心流程

一个生产算子 tiling 被调用时：

```text
框架回调 → TilingRegistry::DoTilingImpl(context)
  ├─ op_type = context->GetNodeType()                  // 从上下文拿算子名
  ├─ cases = GetTilingTemplates(op_type)               // 查注册表：优先级 → tiling 类工厂
  └─ for (按 priority 从小到大):                        // 优先级数值越小越优先
        template = cases[priority](context)            // 工厂创建 tiling 类实例
        status = template->DoTiling()                  // 跑 8 步框架
        ├─ GRAPH_SUCCESS       → 返回成功
        ├─ GRAPH_FAILED        → 返回失败（中止）
        └─ GRAPH_PARAM_INVALID → 本类 IsCapable()==false，继续下一个优先级
```

其中 `TilingBaseClass::DoTiling()` 的 8 步固定流程：`GetShapeAttrsInfo → GetPlatformInfo → IsCapable → DoOpTiling → DoLibApiTiling → GetWorkspaceSize → PostTiling → SetTilingKey(GetTilingKey())`。对比 add_example 的手写函数：步骤完全同构，只是从「自由发挥」变成「填空题」——子类只实现各个虚函数，流程编排由基类负责，最后还统一帮你 `SetTilingKey`。

#### 4.3.3 源码精读

先看执行框架：

[common/inc/op_host/tiling_base.h:L66-L102](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_base.h#L66-L102)——`DoTiling()` 固化 8 步流程；注释明确了三态返回码语义：`GRAPH_PARAM_INVALID` 表示「本类不支持，需要继续往下执行其他 Tiling 类的实现」；末尾 `context_->SetTilingKey(GetTilingKey())` 把 key 计算也收进框架。

再看注册表与遍历：

[common/inc/op_host/tiling_templates_registry.h:L31-L58](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_templates_registry.h#L31-L58)——`TILING_CLASS<T>` 是工厂函数模板（用 `new T(context)` 包出 `unique_ptr<TilingBaseClass>`）；`TilingCases::AddTiling<T>` 把工厂按 priority 存入 `std::map<int32_t, TilingClassCase>`，并检查同优先级重复注册。

[common/inc/op_host/tiling_templates_registry.h:L423-L440](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_templates_registry.h#L423-L440)——`TilingRegistry::DoTilingImpl`：按 map 迭代（priority 升序）依次尝试每个 tiling 类，`GRAPH_PARAM_INVALID` 则跳过继续，其余状态立即返回；全部不适用则报「no valid template is found」。

[common/inc/op_host/tiling_templates_registry.h:L531-L535](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_templates_registry.h#L531-L535)——`REGISTER_OPS_TILING_TEMPLATE(op_type, class_name, priority)` 宏：定义一个全局静态 `Register` 对象，构造时把 `class_name` 注册进单例——纯静态注册，无需集中清单（该文件另有按 soc 版本、按架构号区分的 `REGISTER_TILING_TEMPLATE_WITH_SOCVERSION`/`WITH_ARCH` 变体，见 [L497-L526](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_host/tiling_templates_registry.h#L497-L526)，第 495-496 行注释明确「priority 越小优先级越高」）。

最后看一个真实算子的完整用法——`max_pool_with_argmax_v3` 为同一个算子注册了 **5 个** tiling 类：

| 注册位置 | tiling 类 | priority |
| --- | --- | --- |
| [max_pool_with_argmax_v3_gather_tiling.cpp:L315](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/pooling/max_pool_with_argmax_v3/op_host/arch35/max_pool_with_argmax_v3_gather_tiling.cpp#L315) | GatherTiling | 0（最优先） |
| [max_pool_with_argmax_v3_big_kernel_mul_core_tiling.cpp:L191](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/pooling/max_pool_with_argmax_v3/op_host/arch35/max_pool_with_argmax_v3_big_kernel_mul_core_tiling.cpp#L191) | BigKernelMulCoreTiling | 4 |
| [max_pool_with_argmax_v3_big_kernel_tiling.cpp:L131](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/pooling/max_pool_with_argmax_v3/op_host/arch35/max_pool_with_argmax_v3_big_kernel_tiling.cpp#L131) | BigKernelTiling | 6 |
| [max_pool_with_argmax_v3_nhwc_tiling.cpp:L411](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/pooling/max_pool_with_argmax_v3/op_host/arch35/max_pool_with_argmax_v3_nhwc_tiling.cpp#L411) | NhwcTiling | 20 |
| [max_pool_with_argmax_v3_simt_tiling.cpp:L173](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/pooling/max_pool_with_argmax_v3/op_host/arch35/max_pool_with_argmax_v3_simt_tiling.cpp#L173) | SIMT 兜底 | 100（最后） |

[max_pool_with_argmax_v3_gather_tiling.cpp:L293-L313](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/pooling/max_pool_with_argmax_v3/op_host/arch35/max_pool_with_argmax_v3_gather_tiling.cpp#L293-L313)——一个 TilingBaseClass 子类的填空样例：`DoOpTiling` 做 UB/核切分并逐字段 `set_xxx` 填 tiling data（对比 add_example 直接写 POD 字段，这里用生成的 setter）；`PostTiling` 里 `SetBlockDim`、检查 `GetDataSize() > GetCapacity()` 防溢出、`SaveToBuffer` 把 tiling data 序列化进框架缓冲区。

**边界说明**：add_example 本身用的是 `IMPL_OP_OPTILING` 直连函数的简单路径（[add_example_tiling.cpp:L261](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L261)），并未走注册表；模板注册机制主要服务于 arch35 等生产目录下的多策略算子。两条路径最终交付同样的三样东西（TilingData/TilingKey/BlockDim）。

#### 4.3.4 代码实践

**实践目标**：学会「读注册表」——拿到任何生产算子，快速列出它的 tiling 策略梯队。

1. 在仓库根目录执行（源码阅读型实践）：

   ```bash
   grep -rn "REGISTER_TILING_TEMPLATE\|REGISTER_OPS_TILING_TEMPLATE" pooling/max_pool_with_argmax_v3/op_host/
   ```

2. 把输出整理成上面那张「tiling 类 × priority」表。
3. 打开 priority 最小（0）的 `max_pool_with_argmax_v3_gather_tiling.cpp`，找到它的 `IsCapable()` 实现，读一下什么 shape 条件会返回 false。
4. 回答：当 gather 策略不适用时，框架下一步会尝试哪个类？

**需要观察的现象**：grep 能列出全部注册点；`IsCapable()` 返回 false 的路径与 `DoTiling` 框架中返回 `GRAPH_PARAM_INVALID` 的位置（tiling_base.h L80-L82）对应起来。

**预期结果**：priority=0 的 GatherTiling 不适用时，框架自动落到 priority=4 的 BigKernelMulCoreTiling，以此类推直到 priority=100 的 SIMT 兜底。此实践为纯源码阅读，不依赖运行环境。

#### 4.3.5 小练习与答案

**练习 1**：两个 tiling 类用同一个 priority 注册会发生什么？

**答案**：`TilingCases::AddTiling` 检查到 `cases_.find(priority) != cases_.end()` 时打错误日志并放弃注册（tiling_templates_registry.h L46-L47），先注册者生效——不会崩溃，但后注册的策略永远不执行，排查依赖日志。

**练习 2**：`GRAPH_PARAM_INVALID` 和 `GRAPH_FAILED` 的本质区别是什么？

**答案**：`GRAPH_PARAM_INVALID` 是「礼貌拒绝」——本 tiling 类判断当前 shape/平台不在自己能力范围内，框架继续尝试低优先级类；`GRAPH_FAILED` 是「硬错误」——框架立即中止整个 tiling，不再尝试任何类。子类在 `IsCapable()` 里拒绝时应走前者。

**练习 3**：`TilingBaseClass::DoTiling()` 与 add_example 的手写 `AddExampleTilingFunc` 相比，多帮你做了什么？

**答案**：固定了步骤顺序（8 步）、统一了 `IsCapable` 回退协议、并在末尾自动 `context_->SetTilingKey(GetTilingKey())`——即 4.2 节的 key 设置也被收编进框架，减少手写遗漏。

## 5. 综合实践

**任务：为 AddExample 新增 MODE_2 tiling 场景——totalNum 很小时单核一次搬运完成。**

背景：现有实现中无论规模大小都做「核切分 + UB 分块循环」。当 `totalNum` 小于一个 `ubFactor` 时，起多核、跑循环是浪费。我们新增第三种 tiling 场景：小规模输入直接 key 走 MODE_2，kernel 侧分支只起 1 个核、单次 CopyIn-Compute-CopyOut。

按 4.2 节的「三处对齐」逐处修改（以下为示例代码，非仓库原有内容）：

**第 1 处——声明取值**，编辑 `examples/add_example/op_kernel/add_example_tiling_key.h`，在两个宏中同步追加取值 2：

```cpp
#define ELEMENTWISE_TPL_SCH_MODE_0 0
#define ELEMENTWISE_TPL_SCH_MODE_1 1
#define ELEMENTWISE_TPL_SCH_MODE_2 2  // 新增：小规模单核单趟场景

ASCENDC_TPL_ARGS_DECL(AddExample, ASCENDC_TPL_UINT_DECL(schMode, 1, ASCENDC_TPL_UI_LIST,
    ELEMENTWISE_TPL_SCH_MODE_0, ELEMENTWISE_TPL_SCH_MODE_1, ELEMENTWISE_TPL_SCH_MODE_2));
// ASCENDC_TPL_SEL 行同步追加 MODE_2
```

**第 2 处——Host 按场景选 key**，编辑 `add_example_tiling.cpp` 的 tiling key 段（L228-L240 处），在 dtype 判断内再加一层规模判断：

```cpp
if (dataType == ge::DT_FLOAT) {
    if (totalIdx <= tiling->ubFactor) {  // 单核单趟即可装下
        tilingKey = GET_TPL_TILING_KEY(ELEMENTWISE_TPL_SCH_MODE_2);
    } else {
        tilingKey = GET_TPL_TILING_KEY(ELEMENTWISE_TPL_SCH_MODE_0);
    }
    context->SetTilingKey(tilingKey);
}
// int32 分支保持 MODE_1 不变，简化实践范围
```

注意 MODE_2 场景下应同时 `context->SetBlockDim(1)`（把现有 `SetBlockDim(usedCoreNum)` 改为按场景选择）。

**第 3 处——kernel 加分支**，编辑 `add_example.cpp`：

```cpp
enum class AddExampleTilingKey : uint32_t {
    TILING_KEY_EXAMPLE_FLOAT = 0,
    TILING_KEY_EXAMPLE_INT32 = 1,
    TILING_KEY_EXAMPLE_SMALL = 2,  // 新增，与 MODE_2 数值对齐
};
// 入口函数内追加：
if constexpr (schMode == static_cast<uint32_t>(AddExampleTilingKey::TILING_KEY_EXAMPLE_SMALL)) {
    NsAddExample::AddExample<float> op;   // MODE_2 只服务 float
    op.Init(x, y, z, &tilingData);
    op.Process();                          // 单核时 Process 的 loopCount 自然为 1，无需改类实现
}
```

**验证闭环**：

1. `bash build.sh --pkg --soc=${soc_version} --ops=add_example -j16` 重新编译并安装 run 包（改了算子源码必须重装，见 u1-l2/u1-l4 的经验）。
2. 修改样例 `examples/add_example/examples/test_aclnn_add_example.cpp` 的输入 shape 为很小的 `{1,1,1,8}`（同步保证 shape 乘积 = 输入输出 vector 长度，见 u1-l4 的越界教训）。
3. `bash build.sh --run_example add_example eager cust --vendor_name=custom`，核对输出仍等于 x+y。
4. 再用大 shape（如 `{8,8,8,8}`）跑一次，确认 MODE_0 路径未受影响。
5. （可选进阶）在 tiling UT `test_add_example_tiling.cpp` 中加一个用例：小 shape 断言 `tilingKey` 为 2 且 BlockDim 为 1。

**预期结果**：两种规模下样例输出均正确；若本地无配套 CANN 环境，标注：待本地验证。

## 6. 本讲小结

- **TilingData 是纯 POD 字节契约**：host 与 device 通过 include 同一个头文件（放 `op_kernel/` 下）保证布局一致；Host 经 `GetTilingData<T>()` 写入、Device 经 `GET_TILING_DATA_WITH_STRUCT` 从 GM 地址按字节还原；字段只能追加、不能改语义。
- **TilingKey 是运行期二进制选择器**：`ASCENDC_TPL_ARGS_DECL` 声明模板参数取值集合，`GET_TPL_TILING_KEY` 编码、`SetTilingKey` 下发，kernel 入口以 `template <uint32_t schMode>` + `if constexpr` 为每个取值生成一份专用二进制。
- **三处必须人工对齐**：tiling key 头文件的宏值、kernel 侧枚举值、Host 侧选 key 的分支——编译器不做交叉检查，错位 = 静默错数。
- **模板注册机制是生产算子的多策略底座**：`TilingBaseClass` 固化 8 步流程与三态返回码（SUCCESS/FAILED/PARAM_INVALID），`REGISTER_*_TILING_TEMPLATE` 静态注册，优先级从小到大依次尝试、`IsCapable` 拒绝即回退。
- **add_example 走简单路径，生产算子走注册表**：`IMPL_OP_OPTILING` 直连单函数 vs `REGISTER_OPS_TILING_TEMPLATE` 注册策略梯队（如 max_pool_with_argmax_v3 的 5 级优先级），两条路径交付同样的 TilingData/TilingKey/BlockDim。

## 7. 下一步学习建议

下一讲 **u4-l3 公共 Tiling 设施**将展开本讲已两次出现的 `common/inc/op_host`：`tiling_util.h` 的数学对齐工具（本讲用到的 `CeilDiv`/`FloorAlign` 的实现）、`tiling_cache.h` 的 tiling 缓存机制（相同 shape 重复调用如何免重算），以及生产算子对这套公共设施的复用方式。如果你急于往 kernel 深处走，也可先跳到 u5-l1（Ascend C 编程模型），再回头补 u4-l3——两条线在 u5-l2（数据搬运）处会合。
