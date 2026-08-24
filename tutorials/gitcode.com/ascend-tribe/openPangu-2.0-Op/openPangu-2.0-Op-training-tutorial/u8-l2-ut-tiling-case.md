# u8-l2 编写 Tiling 单元测试：TilingContextPara 用例法

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立读懂 `test_ai_infra_aggregate_hidden_tiling.cpp` 中的任意一个用例，说清「输入描述 → 期望结果 → 断言」三者的对应关系。
2. 使用 `gert::TilingContextPara` 描述一个算子的输入/输出张量（shape、dtype、format）、可选输入的「缺席」写法、属性列表与 CompileInfo。
3. 掌握 `ExecuteTestCase` 的断言顺序（返回值 → workspace → tilingKey → TilingData）与失败短路行为，能正确写出正向用例和异常用例的期望值。
4. 为一个新算子搭出 `tests/ut/op_host` 目录骨架，并理解 `test_*_tiling.cpp` 文件名硬约定、CMake 白名单过滤与构建期自动运行机制。

## 2. 前置知识

本讲建立在两讲之上，先快速回顾：

- **u8-l1（UT 框架总览）**：仓库的 op_host UT 不需要真实 NPU。框架用 **faker**（伪造被测代码的输入环境）拼出一个假的 `gert::TilingContext`，再从注册表里查出算子的 tiling 函数直接调用。入口 main（`test_op_host_main.cpp`）在 `SetUp` 里加载 `libophost_transformer_ut.so` 并注册；执行器核心是 `DO_TILING` 宏的八步流程；用例文件靠 `file(GLOB ... test_*_tiling.cpp)` 自动聚合，加算子零改动框架。
- **u2-l3（Tiling 入门）**：tiling 是 Kernel 启动前的 Host 侧「作战规划」，产出四项契约——`SetBlockDim`（核数）、`SetTilingKey`（跨侧分支信号）、序列化 TilingData、workspace 大小。本讲的断言对象正是这四项契约。

另外需要一点 GoogleTest 基础（本仓库 UT 的测试框架）：

- `TEST_F(FixtureName, case_name)`：把用例挂到某个 fixture 类下，fixture 的 `SetUpTestCase/TearDownTestCase` 在该组用例前后各执行一次。
- `ASSERT_EQ(a, b)`：相等断言，失败时**立即终止当前用例**（区别于 `EXPECT_EQ` 只记失败继续执行）。`ExecuteTestCase` 内部用的都是 `ASSERT_EQ`/`EXPECT_EQ`，所以我们在用例里只需一行调用。

还有一个本讲会反复出现的小工具：`Ops::Transformer::AnyValue`——框架公共目录里的「万能值」类型，配合 `AnyValue::CreateFrom<T>(v)` 可以把 `bool/int64_t/float/string/各类 vector` 统一塞进属性列表（见 4.2.3 的 lightning_indexer 实例）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp` | 本讲的标本用例文件：17 个 TEST_F，覆盖正向/异常两类写法 |
| `ascendc/src/tests/ut/framework_normal/common/tiling_context_faker.h` | `TilingContextPara` 参数包定义：TensorDescription、OpAttr、6 个构造函数重载与伪造平台默认值 |
| `ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.h` | 执行器接口：`ExecuteTestCase` 断言版与 `ExecuteTiling` 取值版、`TilingInfo` 结构 |
| `ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp` | 执行器实现：`DO_TILING` 宏、断言四连、TilingData 转串与 `*` 通配 |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.h` | 被测算子的 TilingData 定义（13 字段）、CompileInfo、tilingKey 常量（BF16=0 / FP16=1） |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp` | 被测逻辑：五步校验、CoreSplit 切分、workspace/tilingKey 写回——期望值的「标准答案」都从这里推导 |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/CMakeLists.txt` 与 `.../tests/ut/op_host/CMakeLists.txt` | 算子侧 UT 挂接点（本讲 4.4） |
| `ascendc/cmake/ut.cmake` | `add_modules_ut_sources`：按文件名 glob 收集用例 + 按目录名过滤算子白名单 |
| `ascendc/src/tests/ut/framework_normal/op_host/CMakeLists.txt` | 聚合出 `transformer_op_host_ut` 可执行文件并 POST_BUILD 自动运行 |
| `ascendc/src/ops-transformer/attention/lightning_indexer_enhance/tests/ut/op_host/test_lightning_indexer_enhance_tiling.cpp` | 进阶参照：带属性、常量张量、TensorV2、`ExecuteTiling` 的 richer 用例 |

## 4. 核心概念与源码讲解

### 4.1 用例五段式骨架：fixture 与 TilingContextPara

#### 4.1.1 概念说明

一个 tiling UT 用例本质上是回答一个问题：**「给定这样的输入描述和伪造平台，tiling 函数应该产出什么？」**

为此，框架提供了 `gert::TilingContextPara`（参数包）：你用声明式的数据结构描述「算子名、输入张量列表、输出张量列表、属性列表、CompileInfo、伪造平台参数」，执行器负责把这些描述拼装成真实的 `TilingContext` 再调用被测 tiling 函数（u8-l1 讲过的 `DO_TILING` 八步）。写用例的人**不需要接触任何 CANN builder 细节**，只填一张「参数表」。

#### 4.1.2 核心流程

一个用例的固定五段式：

```text
1. 定义局部变量        S/B/H/W 等 shape 参数（int64_t）
2. 构造 CompileInfo    optiling::XxxCompileInfo compileInfo = {};   // 通常零初始化即可
3. 构造 TilingContextPara：
      ("算子注册名",
       { 输入 TensorDescription 列表 },   // 顺序 = _def.cpp 的 Input 声明顺序
       { 输出 TensorDescription 列表 },
       { 属性 OpAttr 列表 },              // 无属性时传 {}
       &compileInfo)                     // 其余参数走默认值（伪造平台）
4. 声明期望值           expectTilingKey / expectTilingData / expectWorkspaces
5. 一行执行断言         ExecuteTestCase(tilingContextPara, 期望返回值, ...)
```

TensorDescription 的三要素与「缺席」语义：

- `{{{S,B,H},{S,B,H}}, ge::DT_BF16, ge::FORMAT_ND}`：第一对花括号是 `StorageShape{原始 shape, 存储 shape}`（一般填成一样），随后是 dtype 与 format。
- **shape 填 `{}`（零维）= 该输入「不存在」**：执行器会把它的 IR instance 数记为 0，tiling 侧 `GetInputShape/GetOptionalInputTensor` 拿到 `nullptr`——这是构造异常用例的手段（见 4.3）。
- **可选输入（如 mask）干脆不写进列表 = 缺席**：`ifMask` 走 0 分支。

#### 4.1.3 源码精读

先看用例文件的整体骨架——fixture 类几乎全是空壳，只打印两条日志：

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp:19-30](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp#L19-L30)

fixture 之上是头部 include：用相对路径 `"../../../op_host/ai_infra_aggregate_hidden_tiling.h"` 直接引到被测算子的 tiling 头（拿到 `CompileInfo` 等类型定义），再引框架两件套 `tiling_context_faker.h` / `tiling_case_executor.h`（[L11-L15](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp#L11-L15)）。

第一个正向用例（BF16、无 mask）是五段式的标准样本：

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp:33-58](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp#L33-L58)

这段代码做了什么：input 是 `[4096, 4, 768]` 的 BF16、weight 是 `[3, 768]` 的 BF16（两个必选输入），output 一个 `[4096, 4, 768]`；没有第三个输入描述，所以可选的 mask 缺席；属性列表为空 `{}`；`&compileInfo` 传入 CompileInfo 指针；期望 tiling 成功（`GRAPH_SUCCESS`）、tilingKey=0（BF16）、workspace 为 100MB。

带 mask 的正向用例只多一行第三输入描述，dtype 必须是 `ge::DT_BOOL`、shape 为 `[B, S]`：

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp:98-108](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp#L98-L108)

`TilingContextPara` 的主构造函数与伪造平台默认值定义在 faker 头里——这是理解「UT 里跑的是哪台假机器」的关键：

[ascendc/src/tests/ut/framework_normal/common/tiling_context_faker.h:49-68](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_context_faker.h#L49-L68)

这个构造函数的默认参数即「伪造平台」：`socVersion="Ascend910B"`、`coreNum=64`、`ubSize=262144`（256KB）、`tilingDataSize=4096`。aggregate_hidden 的用例全部吃默认值，所以它们都在一台「64 核、UB 256KB 的 Ascend910B 假机」上跑 tiling。aggregate_hidden 的 `GetNpuInfo` 恰好接受 `ASCEND910B` 与 `ASCEND910_93` 两款芯片（[ai_infra_aggregate_hidden_tiling.cpp:89-93](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L89-L93)），默认值天然合法。

`TensorDescription` 与 `OpAttr` 两个内嵌类的字段如下：

[ascendc/src/tests/ut/framework_normal/common/tiling_context_faker.h:23-47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_context_faker.h#L23-L47)

注意 `TensorDescription` 还有三个带默认值的尾巴：`isConst`（常量张量，可挂真实数据指针）、`constValue`、`isTensorV2`（PA 场景的新张量类型）——aggregate_hidden 用不到，lightning_indexer 用到了（见 4.2.3）。头文件里另有 5 个构造函数重载（[L70-L171](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_context_faker.h#L70-L171)），差别只是「要不要属性列表 / 要不要 instanceNum / 要不要确定性开关」，按需选用。

#### 4.1.4 代码实践

**实践目标**：不运行代码，手工推导用例 1 的完整切分结果，验证你真正读懂了 `TilingContextPara` → `CoreSplit` 的链路。

**操作步骤**：

1. 打开 [ai_infra_aggregate_hidden_tiling.cpp:291-353](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L291-L353) 的 `CoreSplit`。
2. 代入用例 1 的参数：S=4096、B=4、H=768，伪造平台 `coreNum=64`（`aivNum` 由 `GetCoreNumAiv()` 读取伪造平台信息得到，预期为 64，**待本地验证**）。
3. 按代码顺序手算：`baseHCnt = ceil(768/4096) = 1`；H≤4096 所以 `baseH=baseHTail=768`；`coreNumH = 64/1 = 64`；`baseB = ceil(4/64) = 1`；`baseB==1` 触发切 S：`baseSCnt = 64/4 = 16`，`baseS = ceil(4096/16) = 256`，`baseSCnt = 4096/256 = 16`，`baseSTail=256`；最后 \( blockDim = C_H \times C_B \times C_S = 1 \times 4 \times 16 = 64 \)。

**需要观察的现象**（若本地可跑）：UT 日志中该用例通过；把 `CoreSplit` 里任意一个 `OP_LOGD` 级别的 `DumpTilingInfo`（[L379-L396](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L379-L396)）临时改成 `OP_LOGI` 后重跑，应能看到 `baseS: 256`、`blockDim: 64` 与手算一致。

**预期结果**： blockDim=64、baseH=768、baseB=1、baseS=256。若实际 aivNum 不是 64（伪造平台对 AIV/AIC 的映射因芯片而异），S 维切分份数会不同——这正是要「待本地验证」的原因，但 H 不切（768<4096）与 B 切 4 份的结论不受影响。

#### 4.1.5 小练习与答案

**练习 1**：用例 1 与用例 3（有 mask）的期望 tilingKey 都是 0，为什么 mask 不影响 tilingKey？

**答案**：`GetTilingKey` 只看 input 的 dtype（[ai_infra_aggregate_hidden_tiling.cpp:282-289](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L282-L289)）：BF16 → `AGGREGATE_HIDDEN_BF16=0`，FP16 → `AGGREGATE_HIDDEN_HALF=1`（宏定义在 [ai_infra_aggregate_hidden_tiling.h:19-20](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.h#L19-L20)）。mask 是否存在只写进 TilingData 的 `ifMask` 字段，不进 tilingKey。

**练习 2**：若把用例 1 的 `TilingContextPara` 第五个参数后再传 `/*socVersion=*/"Ascend910_93"`（并保持 `coreNum=64`），用例还能过吗？

**答案**：能。`GetNpuInfo` 的白名单同时接受 `ASCEND910B` 与 `ASCEND910_93`。但要注意伪造字符串到枚举的映射由 CANN 平台解析完成，`"Ascend910_93"` 的确切写法**待本地验证**；若写错，会在白名单检查处返回 `GRAPH_FAILED`，用例 1 的期望值就需要整体改写。

**练习 3**：为什么用例里要传 `&compileInfo`，而这个 `AiInfraAggregateHiddenCompileInfo` 是个空结构体？

**答案**：CompileInfo 是 `TilingParse<CompileInfo>` 在真机编译期缓存的平台/算子级信息载体（注册见 [ai_infra_aggregate_hidden_tiling.cpp:478-480](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L478-L480)）。aggregate_hidden 的 `TilingPrepareFor...` 什么都不填（[L420-L423](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L420-L423)），tiling 主流程也不读它，所以空结构体零初始化即可；传指针只是为了满足 faker 的接口形状（它会把该指针塞进伪造 context 的 `GetPlatformInfo/CompileInfo` 通路）。

### 4.2 断言四连：ExecuteTestCase 的检查顺序与失败短路

#### 4.2.1 概念说明

`ExecuteTestCase` 是「带断言的执行器」：它先跑 `DO_TILING` 拿到真实结果，再按固定顺序断言四项——**返回值 → workspace → tilingKey → TilingData**。理解两件事最重要：

1. **失败短路**：期望 `GRAPH_FAILED` 的异常用例在断言完返回值后立即 `return`，后面三项期望值根本不会被检查——所以异常用例里的 `expectTilingKey=0`、`expectWorkspaces={0}` 只是占位符。
2. **期望值不是拍脑袋**：tilingKey、workspace 的「标准答案」全部来自被测 tiling 源码（`SetTilingKey` 写了什么、`workSpaces[0]` 赋了什么），写用例前先读源码。

另有姊妹接口 `ExecuteTiling`：不断言、只取值，把结果装进 `TilingInfo` 返回，适合想做「柔性断言」（如只断言 workspace 个数）的场景。

#### 4.2.2 核心流程

`ExecuteTestCase` 的断言流程：

```text
DO_TILING(para)                       # 伪造 context + 调用被测 tiling 函数，得到 tilingRet / tilingContext
ASSERT_EQ(tilingRet, expectResult)    # ① 返回值
if expectResult == GRAPH_FAILED:
    return                            #    失败短路：② ③ ④ 不再检查
if expectWorkspaces 非空 且 workspaceCount > 0:
    逐个 ASSERT_EQ(workspaceSizes[i], expectWorkspaces[i])   # ② workspace
ASSERT_EQ(GetTilingKey(), expectTilingKey)                   # ③ tilingKey
if expectTilingData != "":
    把 RawTilingData 按 tilingData2StrFunc 转成 "v0 v1 v2 ..." 字符串
    EXPECT_EQ(字符串, expectTilingData)                      # ④ TilingData（支持 * 通配）
```

#### 4.2.3 源码精读

执行器接口与 `TilingInfo` 的定义：

[ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.h:18-34](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.h#L18-L34)

这段代码做了什么：`TilingInfo` 是「取值版」结果包（tilingKey、workspace 列表、TilingData 字节流、blockNum）；`ExecuteTestCase` 的后两个参数（`tilingDataReservedLen` 跳过前 N 个 uint64 保留字段、`tilingData2StrFunc` 自定义转串函数）都有默认值，最简调用只需前五个参数——正是标本用例的写法。

断言主体的实现：

[ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp:255-294](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp#L255-L294)

逐段对照：L266 断言返回值；L267-L269 失败短路；L272-L280 workspace 断言——注意循环上界是 `workspaceCount`（实际个数）而非期望列表长度，**如果算子设置的 workspace 个数少于你给的期望个数，多出来的期望会被静默跳过**，写用例时要心里有数；L282-L284 tilingKey 断言；L287-L293 仅当期望串非空才转串比较。

期望值的「标准答案」从被测源码反推。workspace 的 100MB 来自常量与写回代码：

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp:430-435](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L430-L435)

`DEFAULT_WORKSPACE_SIZE = 100 * 1024 * 1024`（定义于 [L66](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L66)）被写进 `workSpaces[0]`，所以正向用例的期望是 `{100 * 1024 * 1024}`。tilingKey 的 0/1 对应 `GetTilingKey`（见 4.1.3 练习 1）。

TilingData 断言的转串与通配机制：

[ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp:162-189](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp#L162-L189)

`to_string<T>` 把 TilingData 字节流按类型 `T` 逐元素转成空格分隔的数字串；`GetMask` 从期望串里收集 `*` 出现的元素下标，比较时这些位置放行任意值——适合断言「切分结果中我关心的字段」而放过平台相关的字段。**诚实现状：本仓库现存所有 tiling 用例的 `expectTilingData` 均传 `""`（跳过 ④），通配能力由框架提供但尚未被任何用例使用**；若要启用，可参照 lightning_indexer 在用例文件里自定义的 `TilingData2Str<T>`（[test_lightning_indexer_enhance_tiling.cpp:33-43](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/tests/ut/op_host/test_lightning_indexer_enhance_tiling.cpp#L33-L43)）。

进阶参照——lightning_indexer 的一个用例同时展示了四个 aggregate_hidden 没用到的能力：

[ascendc/src/ops-transformer/attention/lightning_indexer_enhance/tests/ut/op_host/test_lightning_indexer_enhance_tiling.cpp:870-901](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/tests/ut/op_host/test_lightning_indexer_enhance_tiling.cpp#L870-L901)

这段代码做了什么：① 第四个输入 `isConst=true` 且挂了宿主机数组指针（`actual_seq_qlist`），模拟 varlen 场景的常量序列长度张量；② 第二个输入的 `TensorDescription` 尾参 `true` 打开 `isTensorV2`（PA 布局）；③ 属性列表用 `Ops::Transformer::AnyValue::CreateFrom<string/int64_t/bool>` 构造了 9 个属性，`TilingContextPara` 第四个参数位置传属性、编译期校验类型；④ 用 `ExecuteTiling` + `TilingInfo` 做柔性断言：只断言「成功、workspace 有 1 项、tilingKey 非零」。

`ExecuteTiling` 的取值实现（对照 `ExecuteTestCase` 的断言实现）：

[ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp:296-319](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp#L296-L319)

它把 tilingKey、`GetBlockDim()`（blockNum）、workspace 列表、RawTilingData 的字节拷贝全部装进 `TilingInfo`——也是手工验证 TilingData 内容时最方便的观测口。

#### 4.2.4 代码实践

**实践目标**：给 aggregate_hidden 补一条真正断言 TilingData 的用例（框架能力未被仓库用例覆盖，正好补上）。

**操作步骤**：

1. 在用例文件顶部（fixture 类之后）添加转串函数（示例代码，仿照 lightning_indexer 的 `TilingData2Str`）：

   ```cpp
   // 示例代码：按 int64_t 逐元素转串（ifMask 字段会被并入首个 int64 槽位，此处仅演示）
   template <typename T>
   static string TilingData2Str(void* buf, size_t size) {
       string result;
       const T* data = reinterpret_cast<const T*>(buf);
       size_t len = size / sizeof(T);
       for (size_t i = 0; i < len; i++) {
           result += std::to_string(data[i]);
           result += " ";
       }
       return result;
   }
   ```

2. 复制用例 1，先跑一次「打印版」：`ExecuteTiling(tilingContextPara, tilingInfo)` 后用 `cout` 打印 `tilingInfo.tilingData` 的前 13 个 int64（配合 `tilingInfo.tilingDataSize`），得到实际串。
3. 注意 TilingData 结构是 `uint8_t ifMask` 打头、后跟 12 个 `int64_t`（[ai_infra_aggregate_hidden_tiling.h:43-57](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.h#L43-L57)），按 `int64_t` 转串时首元素是 `ifMask` 与 `hSize` 的字节拼接值，直接断言会踩坑——建议对首元素用 `*` 通配。
4. 把打印出的串填进 `ExecuteTestCase(..., expectTilingDataStr, {}, 0, TilingData2Str<int64_t>)`。

**需要观察的现象**：期望串与实际串逐元素一致；把 `hSize` 从 768 改成 1536 后，串中对应位置的值应随之变化。

**预期结果**：用例通过；若不一致，`EXPECT_EQ` 会打印两串差异，据此定位是结构体对齐理解错误还是切分手算错误。完整数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：异常用例（如「input 为空」）里 `expectWorkspaces = {0}`，如果写成 `{100 * 1024 * 1024}` 会挂吗？

**答案**：不会。失败短路在先：期望 `GRAPH_FAILED` 时函数在断言返回值后立即 `return`（[tiling_case_executor.cpp:267-269](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp#L267-L269)），workspace 断言根本不执行。但为了可读性，异常用例的占位值建议保持仓库惯例 `{0}`。

**练习 2**：为什么正向用例必须给 `expectWorkspaces = {100 * 1024 * 1024}`，给空 `{}` 行不行？

**答案**：行，且效果相同——workspace 断言仅在期望列表非空时执行（[L272](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp#L272)）。lightning_indexer 的用例就传空列表只断言 tilingKey。写成具体值的意义是把 workspace 契约固定进测试，防止有人误改 `DEFAULT_WORKSPACE_SIZE` 而无人察觉。

**练习 3**：`ExecuteTestCase` 里 workspace 断言循环的上界是 `workspaceCount`，这个设计有什么坑？

**答案**：若被测算子只 `SetWorkspace` 了 1 项而期望列表写了 2 项，第二项期望永远不会被比较（静默通过）。反过来，若算子设了 2 项而期望只写 1 项，同样只查第一项。写用例时应保证期望列表长度与算子实际 workspace 个数一致。

### 4.3 异常用例设计：从校验代码反推用例矩阵

#### 4.3.1 概念说明

aggregate_hidden 的 17 个用例里有 12 个异常用例，它们不是随意编的，而是**从 tiling 源码的校验分支反推出来的**：`AHInfoParser` 的四步 Check（Input/Weight/Mask/Output）每一条 `OP_CHECK_IF` 都对应至少一个「构造违反该条件的输入」的用例。这种「源码分支 → 用例矩阵」的方法是给新算子补 UT 的最快路径。

#### 4.3.2 核心流程

反推流程：

```text
1. 通读 tiling.cpp 的 CheckInputValid / CheckWeightValid / CheckMaskValid / CheckOutputValid
2. 列出每条 OP_CHECK_IF 的触发条件（空指针 / 维度数 / 越界 / 不匹配 / dtype 非法）
3. 为每条条件设计「最小变异」输入：只改一个维度/一个 dtype/一项 shape
4. 期望值统一 GRAPH_FAILED（配合 4.2 的失败短路，其余占位）
5. 正向用例则覆盖合法空间的四角：BF16/FP16 × 有/无 mask × 特殊 H
```

#### 4.3.3 源码精读

标本文件的异常用例全景（分类注释即源码注释）：

| 分类 | 用例（TEST_F 名简称） | 构造手段 | 命中的被测校验 |
| --- | --- | --- | --- |
| 空张量 | emptyInput / emptyWeight | shape 填 `{}` | `GetInputShape==nullptr` / weight 同（[tiling.cpp:110-115](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L110-L115)、[L160-L165](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L160-L165)） |
| 维度数 | inputDimNot3 / weightDimNot2 / maskDimNot2 | 尾部补 1 维 | input 维度检查 [L122-L127](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L122-L127)、weight [L172-L177](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L172-L177)、mask [L209-L211](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L209-L211) |
| shape 不匹配 | sMismatch / bMismatch / hMismatch | mask 或 weight 换一个维 | mask S/B 一致性 [L214-L226](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L214-L226)、weight H 一致性 [L185-L190](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L185-L190) |
| 取值非法 | weightWNotSupported | W=3 改 4 | W 固定 3 [L180-L182](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L180-L182) |
| dtype 非法 | inputInvalidDtype / weightInputDtypeMismatch / maskNotBool | DT_FLOAT / 混用 / INT32 | input dtype [L118-L120](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L118-L120)、weight 一致性 [L168-L170](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L168-L170)、mask 必须 BOOL [L205-L207](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L205-L207) |

「空张量」用例的构造现场——shape 填 `{}` 即可让该输入从 IR 层面消失：

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp:181-190](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp#L181-L190)

执行器侧的对应实现：发现零维 shape 就把 instance 数记 0（输入在 [tiling_case_executor.cpp:32-34](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp#L32-L34)，输出在 [L66-L68](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp#L66-L68)），tiling 侧取 shape 时便得到 `nullptr`。

「dtype 非法」用例只改一个枚举值：

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp:439-448](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp#L439-L448)

input 用 `ge::DT_FLOAT`，直接命中「必须是 float16 或 bfloat16」的检查。

**一个必须澄清的准确性问题**：README/docs 说 H 必须是「192 的倍数」，但 tiling 源码对 H **只有范围检查、没有取模检查**：

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp:141-149](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L141-L149)

上界 `H_SIZE_UP_LIMIT = 24576`（=192×128）、下界 `H_SIZE_DOWN_LIMIT = 384`（=192×2）（常量见 [L57-L63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L57-L63)）——「192 对齐」只体现在两个边界值本身。因此综合实践里的 H=200 用例，实际命中的是**下界检查**（200 < 384），而非一条「不满足 192 对齐」的显式分支；界内但非 192 倍数的 H（如 500）目前会通过 tiling 校验进入切分。这是文档与实现的偏差，写用例时要如实标注。

#### 4.3.4 代码实践

**实践目标**：为「S 越界」和「B 越界」各写一条异常用例——这两条分支目前没有用例覆盖（存量用例只覆盖了 H 的范围）。

**操作步骤**：

1. 从 [tiling.cpp:128-140](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L128-L140) 确认触发条件：S > 32768 或 B > 8。
2. 复制用例 1，分别改成 `S = 32 * 1024 + 1` 与 `B = 9`（其余不动；weight 的 H 保持 768 与 input 一致）。
3. 期望值照抄异常用例惯例：`ExecuteTestCase(tilingContextPara, ge::GRAPH_FAILED, 0, "", {0});`。

**需要观察的现象**：两条用例均以 `GRAPH_FAILED` 通过；UT 运行日志中能看到对应的 `OP_LOGE` 文案（「the max of S size only support 32K...」/「the max of batch size only support 8...」）。

**预期结果**：全部通过。若 S 用例意外成功，说明对「`>` 与 `>=`」的边界理解有误（S=32768 恰好合法，32769 才非法）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：设计一条「H=500（界内、非 192 倍数）」的用例，期望应该写 `GRAPH_SUCCESS` 还是 `GRAPH_FAILED`？

**答案**：按当前源码应写 `GRAPH_SUCCESS`（workspace 100MB、tilingKey 按 dtype 定）——范围检查放行 500，「192 对齐」并无显式校验。这条用例实际上是在**固化文档与实现的偏差**；若未来补上对齐检查，它会第一时间变红，这正是回归用例的价值。

**练习 2**：用例 `weightWNotSupported` 把 W 从 3 改成 4，为什么不顺手把 weight 的 shape 写成 `{4, 768}` 之外更大的变异（如 `{8, 1024}`）？

**答案**：异常用例的设计原则是「最小变异、单一归因」——一次只违反一条约束，失败时才能确定命中的是哪条 `OP_CHECK_IF`。`{8, 1024}` 同时违反 W=3 与 H 一致性两条，即使失败也无法区分是哪条拦截，排查价值反而下降。

**练习 3**：为什么「空 input」用例连 output 也写成空 `{}`，而「input 维度不为 3」用例的 output 是正常的 `[S,B,H]`？

**答案**：空 input 用例模拟的是「上游根本没接这个输入」的极端场景，此时 InferShape 也推不出 output，一并置空更贴近真实失败路径（执行器对零维 output 同样记 instance 0）；维度用例则只想命中维度检查这一条分支，其余输入输出都保持合法，符合单一归因原则。两种写法最终都 `GRAPH_FAILED`，但日志里报错的检查点不同。

### 4.4 目录骨架与构建挂接：从 test 文件到 transformer_op_host_ut

#### 4.4.1 概念说明

写好用例只是第一步，还要让构建系统「发现」它。本仓库的挂接是**零注册**的：CMake 按目录约定自动递归、按文件名 glob 自动收编、按算子目录名自动对齐 `-n` 白名单。给新算子建 UT 骨架 = 建对目录 + 起对文件名 + 抄一份 13 行的 CMakeLists。

#### 4.4.2 核心流程

数据流：

```text
bash build.sh -u -n <算子目录名> --ophost -c <芯片>
  └─ ENABLE_TEST=TRUE, OP_HOST=TRUE → set_ut_mode → OP_HOST_UT=TRUE
      └─ cmake -DASCEND_OP_NAME=<算子目录名> -DOP_HOST_UT=TRUE -DENABLE_UT_EXEC=TRUE
          └─ 递归进入 <算子>/tests/CMakeLists.txt → tests/ut/CMakeLists.txt → tests/ut/op_host/CMakeLists.txt
              └─ add_modules_ut_sources：目录名对上白名单 → glob test_*_tiling.cpp 进 cases 静态库
                  └─ 聚合成 transformer_op_host_ut 可执行文件（whole-archive + gtest）
                      └─ POST_BUILD 自动执行 ./transformer_op_host_ut
```

#### 4.4.3 源码精读

算子侧三层 CMakeLists 都是「遍历子目录」的薄壳。最外层 `tests/CMakeLists.txt` 与 `tests/ut/CMakeLists.txt` 内容几乎相同（glob 子目录、存在 CMakeLists.txt 就 `add_subdirectory`）：

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/CMakeLists.txt:11-17](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/CMakeLists.txt#L11-L17)

真正干活的是 `tests/ut/op_host/CMakeLists.txt`——全仓库算子通用的 13 行模板：

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/CMakeLists.txt:10-18](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/CMakeLists.txt#L10-L18)

这段代码做了什么：在 UT 模式（`OP_HOST_UT` 或 `UT_TEST_ALL`）下，把本目录的源文件分别挂到 `optiling_cases` 与 `opinfershape_cases` 两个聚合目标上，然后继续向下遍历。新算子照抄这份文件即可。

「零注册」的两条硬约定在 `cmake/ut.cmake` 的 `add_modules_ut_sources` 里：

[ascendc/cmake/ut.cmake:196-218](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/ut.cmake#L196-L218)

- **文件名约定**：L216 `file(GLOB OPHOST_TILING_CASES_SRC ${MODULE_DIR}/test_*_tiling.cpp)`——tiling 用例必须叫 `test_..._tiling.cpp`；同理 infershape 用例是 `test_*_infershape.cpp`（[L234](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/ut.cmake#L234)）、op_api 用例是 `test_aclnn_*.cpp`（[L259](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/ut.cmake#L259)）。起错名字 = 文件被静默忽略。
- **目录名即算子名**：L203-L211 从用例目录向上回溯三级（`tests/ut/op_host` → `tests/ut` → `tests` → 算子目录）取出算子目录名，与 `-n` 传入的 `ASCEND_OP_NAME` 白名单比对，不在名单直接 `return()`。所以**算子目录名必须与 build.sh `-n` 的名字完全一致**。

聚合与运行在框架侧 `op_host/CMakeLists.txt`：用例静态库被 `--whole-archive` 全量链进可执行文件（保证 TEST_F 的注册代码不被裁剪），随后 POST_BUILD 自动执行：

[ascendc/src/tests/ut/framework_normal/op_host/CMakeLists.txt:44-73](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_host/CMakeLists.txt#L44-L73)

[ascendc/src/tests/ut/framework_normal/op_host/CMakeLists.txt:107-115](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_host/CMakeLists.txt#L107-L115)

`transformer_op_host_ut` 由 `test_op_host_main.cpp` 驱动；main 的 `SetUp` 从环境变量 `BUILD_PATH` 找到 `libophost_transformer_ut.so`（被测 tiling 函数的载体）并塞进注册表——这正是执行器里 `spaceRegistry->GetOpImpl(opName)->tiling` 能查到函数的原因（u8-l1 已讲，见 [test_op_host_main.cpp:29-43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_host/test_op_host_main.cpp#L29-L43)）。也因此，`TilingContextPara` 第一个参数 `"AiInfraAggregateHidden"` 必须与 `IMPL_OP_OPTILING(AiInfraAggregateHidden)` 的注册名逐字一致（[ai_infra_aggregate_hidden_tiling.cpp:478-480](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L478-L480)），写错会直接空指针崩溃。

build.sh 侧的开关链（本讲实践要用的命令就沿这条链生效）：

[ascendc/build.sh:291-294](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L291-L294) 定义 `-u|--test` → `ENABLE_TEST=TRUE`；[L337-L342](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L337-L342) 定义 `--ophost` → `OP_HOST=TRUE`；[L178-L192](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L178-L192) `set_ut_mode` 把二者折算成 `OP_HOST_UT=TRUE`；最终 [L459-L478](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L459-L478) 走 `cmake_config` + `build_ut`（目标是 `transformer_op_host_ut`，[L109-L118](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L109-L118)）。另外 [L396-L405](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L396-L405) 会导出 `BUILD_PATH` 并置 `ENABLE_UT_EXEC=TRUE`——这就是「编译完自动跑测试」的来源。还有一个容易踩的坑：`check_ophost_test_exists`（[L230-L251](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L230-L251)）规定 `-n` 指定的算子若没有 `tests/ut/op_host` 目录会**直接报错退出**——骨架必须先建好。

#### 4.4.4 代码实践

**实践目标**：不新增任何算子，验证你对挂接机制的理解——只改文件名，观察用例是否「消失」。

**操作步骤**：

1. 把 `test_ai_infra_aggregate_hidden_tiling.cpp` 临时改名为 `mytest_ai_infra_aggregate_hidden_tiling.cpp`（违反 `test_*_tiling.cpp` 约定）。
2. 执行 `bash build.sh -u -n ai_infra_aggregate_hidden -c ascend910_93 --ophost`。
3. 观察构建日志里的用例数量（编译命令不再包含该文件），运行结果里 `AiInfraAggregateHiddenTiling` 组的用例数为 0。
4. 改回原名，重新构建确认恢复。

**需要观察的现象**：改名后构建不报错（glob 落空是合法状态），但 17 个用例全部消失——「静默忽略」正是文件名约定的风险面。

**预期结果**：恢复后 `[ PASSED ]` 计数回到原值。**待本地验证**（需要 u1-l3 搭建的容器环境与 CANN 包）。

#### 4.4.5 小练习与答案

**练习 1**：新算子 `ai_infra_scale_mul` 要接 tiling UT，最少需要创建哪些文件/目录？

**答案**：三个：`tests/ut/CMakeLists.txt`（抄 aggregate_hidden 的 17 行遍历模板）、`tests/ut/op_host/CMakeLists.txt`（抄 13 行 `add_modules_ut_sources` 模板）、`tests/ut/op_host/test_ai_infra_scale_mul_tiling.cpp`（fixture + 用例）。外层 `tests/CMakeLists.txt` 若已存在则复用，不存在再补一份同样的遍历模板。无需改任何框架文件。

**练习 2**：构建命令写成 `bash build.sh -u -n AiInfraScaleMul --ophost -c ascend910_93`（用注册名而不是目录名）会发生什么？

**答案**：`check_ophost_test_exists` 里的 `find_op_dir` 按**目录名**在 `src/ops-transformer` 下查找（[build.sh:194-197](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L194-L197)），`AiInfraScaleMul` 找不到对应目录，直接 `ERROR: operator directory not found` 退出。`-n` 用目录名（小写下划线），`TilingContextPara` 第一个参数用注册名（大驼峰），两套名字不可混用。

**练习 3**：为什么用例静态库要用 `--whole-archive` 链接？

**答案**：TEST_F 宏生成的注册代码藏在 `.o` 的静态初始化段里，没有任何显式调用方。普通链接时链接器发现无引用就会整段丢弃，用例一个都跑不起来；`--whole-archive` 强制保留全部成员，gtest 才能在 `RUN_ALL_TESTS` 时枚举到它们。

## 5. 综合实践

按本讲规格完成一个完整的「读 → 写 → 跑」闭环：为 `test_ai_infra_aggregate_hidden_tiling.cpp` 新增两个用例并编译运行。

**任务 1：新增正向用例（B=8、S=32K、H=4096、BF16、带 mask）**

在文件末尾追加（示例代码，风格对齐存量用例）：

```cpp
// 用例18: BF16 上界规格 B=8 S=32K H=4096 带 Mask
TEST_F(AiInfraAggregateHiddenTiling, ai_infra_aggregate_hidden_bf16_32768_8_4096_hasMask_pangu_000001)
{
    int64_t S = 32 * 1024;  // S 维上界（S_SIZE_LIMIT，恰好合法）
    int64_t B = 8;          // B 维上界（B_SIZE_LIMIT，恰好合法）
    int64_t H = 4096;       // 等于 H_SIZE_FULL，UB 全载不切 H
    int64_t W = 3;

    optiling::AiInfraAggregateHiddenCompileInfo compileInfo = {};

    gert::TilingContextPara tilingContextPara(
        "AiInfraAggregateHidden",
        {
            {{{S, B, H}, {S, B, H}}, ge::DT_BF16, ge::FORMAT_ND},
            {{{W, H}, {W, H}}, ge::DT_BF16, ge::FORMAT_ND},
            {{{B, S}, {B, S}}, ge::DT_BOOL, ge::FORMAT_ND},
        },
        {
            {{{S, B, H}, {S, B, H}}, ge::DT_BF16, ge::FORMAT_ND},
        },
        {}, &compileInfo);

    int64_t expectTilingKey = 0;  // BF16
    std::string expectTilingData = "";
    std::vector<size_t> expectWorkspaces = {100 * 1024 * 1024};

    ExecuteTestCase(tilingContextPara, ge::GRAPH_SUCCESS, expectTilingKey, expectTilingData, expectWorkspaces);
}
```

写之前先自行验证期望值：S=32768 不大于 `S_SIZE_LIMIT`（[tiling.cpp:132-134](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L132-L134) 用的是 `>`），B=8 同理合法；H=4096 在界内。顺手手算切分（假设 aivNum=64，待本地验证）：`baseHCnt=1`、`baseH=4096`、`coreNumH=64`、`baseB=1`、`baseBCnt=8`、`baseSCnt=8`、`baseS=4096`、\( blockDim = 1 \times 8 \times 8 = 64 \)。

**任务 2：新增异常用例（H=200，期望 GRAPH_FAILED）**

```cpp
// 用例19: H=200 非法（低于下界 384；docs 的「192 对齐」在本算子 tiling 中体现为界值 384=192*2）
TEST_F(AiInfraAggregateHiddenTiling, ai_infra_aggregate_hidden_bf16_hSizeInvalid_200_000001)
{
    int64_t S = 4096;
    int64_t B = 4;
    int64_t H = 200;  // 非法：200 < H_SIZE_DOWN_LIMIT(384)，且不满足 192 对齐
    int64_t W = 3;

    optiling::AiInfraAggregateHiddenCompileInfo compileInfo = {};

    gert::TilingContextPara tilingContextPara(
        "AiInfraAggregateHidden",
        {
            {{{S, B, H}, {S, B, H}}, ge::DT_BF16, ge::FORMAT_ND},
            {{{W, H}, {W, H}}, ge::DT_BF16, ge::FORMAT_ND},
        },
        {
            {{{S, B, H}, {S, B, H}}, ge::DT_BF16, ge::FORMAT_ND},
        },
        {}, &compileInfo);

    int64_t expectTilingKey = 0;   // 失败短路，占位不检查
    std::string expectTilingData = "";
    std::vector<size_t> expectWorkspaces = {0};

    ExecuteTestCase(tilingContextPara, ge::GRAPH_FAILED, expectTilingKey, expectTilingData, expectWorkspaces);
}
```

注意两个细节：weight 的第二维必须与 input 的 H 一致（都写 200），否则会先命中「H 不一致」而非 H 范围检查；期望 `GRAPH_FAILED` 后其余占位值不会被断言（4.2 的失败短路）。命中分支是 [tiling.cpp:147-149](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L147-L149) 的下界检查——**不是**一条显式的 192 取模检查（见 4.3.3 的澄清）。

**任务 3：编译并运行**

```bash
cd ascendc
bash build.sh -u -n ai_infra_aggregate_hidden -c ascend910_93 --ophost
```

命令链路：`-u` 开 UT 模式、`-n` 限定算子白名单（目录名）、`-c` 指定芯片、`--ophost` 只跑 op_host UT（4.4.3 的开关链）。构建成功后 POST_BUILD 自动执行 `transformer_op_host_ut`，应在输出中看到 `AiInfraAggregateHiddenTiling` 组从 17 个用例变为 19 个、全部 `PASSED`。

**无 NPU 环境时的最低要求**：op_host UT 本就不需要 NPU（u8-l1 的核心结论），但仍需要 CANN 工具链与 bisheng 编译器（u1-l3/u1-l4 搭建的容器）。若容器也没有，请完成：① 两个用例代码写入文件；② 用 `g++ -fsyntax-only` 无法直接编译（缺 CANN 头文件），改为人工逐行核对括号/逗号与存量用例的形态一致性；③ 在文中记录「待本地验证」的三个观察点：用例计数 19、正向用例 PASSED、异常用例日志出现 `the min of H size must larger than 384` 文案。

## 6. 本讲小结

- 一个 tiling UT 用例 = 五段式：shape 变量 → `CompileInfo` → `TilingContextPara`（算子注册名 + 输入/输出 TensorDescription + 属性 + 伪造平台默认值）→ 期望三元组 → 一行 `ExecuteTestCase`。
- `ExecuteTestCase` 断言四连的顺序是「返回值 → workspace → tilingKey → TilingData」，期望 `GRAPH_FAILED` 时失败短路，后三项占位值不检查；期望值必须从 tiling 源码（`DEFAULT_WORKSPACE_SIZE`、`GetTilingKey`）反推，不能拍脑袋。
- TilingData 断言走「字节流 → 空格分隔数字串」比较并支持 `*` 通配，但本仓库现存用例全部传空串跳过该断言；`ExecuteTiling` + `TilingInfo` 是取值观测口，适合柔性断言。
- 异常用例的设计方法是「源码分支 → 用例矩阵」：每条 `OP_CHECK_IF` 配一个最小变异用例；shape 填 `{}` 可让输入从 IR 层缺席。注意文档说的「H 是 192 的倍数」在 tiling 源码中只体现为 `[384, 24576]` 的范围检查，H=200 命中的是下界分支。
- 挂接是零注册的：文件名必须匹配 `test_*_tiling.cpp` glob 约定，算子目录名必须与 `-n` 白名单一致，`tests/ut/op_host/CMakeLists.txt` 抄 13 行模板即可；`build.sh -u --ophost` 触发 `transformer_op_host_ut` 构建并 POST_BUILD 自动运行。
- `TilingContextPara` 第一个参数是注册名（`"AiInfraAggregateHidden"`，对应 `IMPL_OP_OPTILING`），`-n` 是目录名——两套名字、两个位置，都不能写错。

## 7. 下一步学习建议

- **u8-l3（ST 精度测试）**：UT 只验证 Host 侧逻辑与分支，数值精度要靠 ST——下一讲讲 `tests/st` 的 torch_npu 测试基类、CPU float64 golden 与 MARE/MERE/RMSE 判定，与本讲形成「流程正确性 ↔ 数值正确性」的互补。
- **u8-l4（构建与运行 UT/ST）**：本讲的 build.sh 链路会在那一讲完整跑通并排查常见环境问题，建议两讲实践连续做。
- **扩展阅读**：`test_lightning_indexer_enhance_tiling.cpp`（属性/常量张量/TensorV2/`ExecuteTiling` 的 fuller 用法）与 `ai_infra_sinkhorn_grad` 的 fixture 范式（`test_ai_infra_sinkhorn_grad_fixture.h` + `ErrorType` 错误注入枚举，用例一行一个场景）——后者是当用例参数组合爆炸时比本讲「平铺 TEST_F」更可维护的组织方式。
- 若你在做 u9-l4 的「新增算子」综合实战，本讲 4.4 的三文件骨架清单可直接落地，并把本讲的用例矩阵方法用于新算子的全部校验分支。
