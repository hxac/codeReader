# 算子原型定义：OpDef 与输入输出规格

## 1. 本讲目标

学完本讲，你应该能够：

1. 看懂 `op_host/${op_name}_def.cpp` 算子信息库文件中 `Input`/`Output` 的 `ParamType`、`DataType`、`Format` 声明，理解它们如何决定「这个算子能吃什么样的张量」。
2. 理解 `OpAICoreConfig` 中 `DynamicShapeSupportFlag` 等编译开关的含义，以及 `AICore().AddConfig("ascend910b", ...)` 如何为不同 SOC 版本挂载不同的 AI Core 编译配置。
3. 理解 `OP_ADD` 宏如何把一个 `OpDef` 子类注册进算子库，成为算子在 CANN 框架中的「身份证」。
4. 完成一个综合实践：为 AddExample 增加 `DT_INT16` 数据类型支持，并同步打通 def / tiling / kernel 三处修改。

本讲承接 u1-l3 对算子工程目录的解剖：那一讲我们知道了 `op_host` 下有哪些交付件，本讲深入其中第一份——算子原型定义文件 `*_def.cpp`。

## 2. 前置知识

- **算子（Operator）**：神经网络中的最小计算单元，比如加法、激活函数、矩阵乘。在 CANN 生态里，一个算子要能被框架调用，必须先「登记造册」——声明自己叫什么、有几个输入输出、支持什么数据。
- **Host 与 Device**：Host 指 CPU 侧（控制面），Device 指 NPU 侧（计算面）。`*_def.cpp` 完全工作在 Host 侧，它描述的是算子的「规格」，而不是计算逻辑本身。
- **ge 命名空间**：`ge` 是 Graph Engine 的缩写，CANN 基础软件层提供的数据类型与图描述能力。`ge::DT_FLOAT`、`ge::FORMAT_ND` 这些枚举都来自这一层。
- **dtype 与 format**：dtype（data type）指元素的数据类型，如 float32、int32、bfloat16；format 指内存排布格式，如 ND（n-dimensional，任意维度连续排布）、NZ（Fragment 类排布）。本讲只涉及 ND，更多格式在 u3-l3 展开。
- **soc_version**：芯片型号短名，如 `ascend910b`（Atlas A2）、`ascend910_93`（A3）、`ascend950`。同一个算子可以只配置其中一种芯片，也可以多种都配。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/add_example/op_host/add_example_def.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp) | 教学样例 AddExample 的算子信息库，本讲的主读对象，注释最全 |
| [activation/gelu/op_host/gelu_def.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp) | 生产算子 Gelu 的算子信息库，用来对照真实写法 |
| [examples/add_example/op_host/add_example_tiling.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp) | AddExample 的 tiling 实现，实践任务要同步修改它的 dtype 校验与 tiling key 分支 |
| [examples/add_example/op_kernel/add_example.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp) | AddExample 的 kernel 入口，按 tiling key 分发到不同 dtype 的模板实例，实践任务要新增 int16 分支 |
| [examples/add_example/op_kernel/add_example_tiling_key.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example_tiling_key.h) | tiling key 的模板参数声明，实践任务要新增 MODE_2 |
| [examples/add_example/op_host/config/ascend910b/add_example_binary.json](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/config/ascend910b/add_example_binary.json) | 预编译 kernel 二进制的描述文件，能看到 def 声明如何「落到」二进制清单 |
| [docs/zh/develop/aicore_develop_guide.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md) | AI Core 算子开发官方指南，明确 def 是算子定义阶段的交付件 |

## 4. 核心概念与源码讲解

### 4.1 OpDef 类与 OP_ADD 注册机制

#### 4.1.1 概念说明

`*_def.cpp` 被官方文档称为「算子信息库」。它回答的问题是：**这个算子是谁、长什么样、在哪些芯片上可用**。

注意它**不包含**任何计算逻辑，也不直接包含 tiling/shape 推导逻辑。开发指南在讲算子迁移时明确说：老的 `${op_name}.cpp` 里的 `SetInferShape` 和 `SetTiling` 内容要剥离出去——shape 推导迁到 `*_infershape.cpp`，tiling 注册迁到 `*_tiling.cpp`，`*_def.cpp` 只留纯规格声明。也就是说，现代 ops-nn 工程里，def 文件是**单一职责的规格层**。

注册机制靠两个东西：

- 继承 `OpDef` 基类，在构造函数里用链式调用描述规格；
- 文件末尾的 `OP_ADD(类名)` 宏，把这个类的注册逻辑挂入算子库的静态初始化流程。

#### 4.1.2 核心流程

```text
编译期（cmake 把 *_def.cpp 编进宿主库）
  └─> OP_ADD(AddExample) 展开为带构造副作用的注册器
        └─> 进程加载宿主库时静态构造，把 "AddExample" 这个算子名
            连同 Input/Output/AICore 配置写入全局算子注册表
运行期（框架收到 aclnnAddExample / 图模式 AddExample 节点）
  └─> 按算子名查注册表，用 def 里声明的 dtype/format 规格做匹配校验
```

为什么要用「静态注册」而不是集中清单？因为 ops-nn 有上千个算子目录，集中清单会变成合并冲突重灾区；每个算子自带 def 文件、编译进各自库，`OP_ADD` 宏在库加载时自动登记，新增算子零改动框架代码。

#### 4.1.3 源码精读

AddExample 的 def 文件整体骨架：

[examples/add_example/op_host/add_example_def.cpp:18-20](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L18-L20) 引入注册头文件并进入 `ops` 命名空间——所有算子定义都必须放在 `namespace ops` 内，框架在固定命名空间下查找注册符号。

[examples/add_example/op_host/add_example_def.cpp:29-41](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L29-L41) 定义 `class AddExample : public OpDef`，构造函数接收算子名。类名就是注册时的算子类型名（`OP_ADD(AddExample)` 与之二对应）。

[examples/add_example/op_host/add_example_def.cpp:82-84](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L82-L84) 末尾一行 `OP_ADD(AddExample);` 完成注册。这是 def 文件的「落款」。

gelu 的写法一模一样，只是更精简（没有教学注释）：

[activation/gelu/op_host/gelu_def.cpp:19-21](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp#L19-L21) `class Gelu : public OpDef`，[activation/gelu/op_host/gelu_def.cpp:46](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp#L46) 以 `OP_ADD(Gelu);` 收尾。

开发指南对 def 交付件的定位：

[docs/zh/develop/aicore_develop_guide.md:91-105](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md#L91-L105) 说明算子定义需要 `README.md` 与 `${op_name}_def.cpp` 两个交付件，并把 add_example 的 def 文件作为参考实现直接链接出来——它就是官方钦定的教学范本。

[docs/zh/develop/aicore_develop_guide.md:875-905](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md#L875-L905) 迁移章节演示了老式 `${op_name}.cpp` 如何剥离 `SetInferShape` / `SetTiling`，只留 `Input`/`Output`/`AICore()` 规格迁入 def 文件。

#### 4.1.4 代码实践

**实践目标**：直观感受 `OP_ADD` 注册的是「类名 = 算子名」这层绑定关系。

**操作步骤**：

1. 打开 `examples/add_example/op_host/add_example_def.cpp`，把末尾 `OP_ADD(AddExample);` 临时改成 `OP_ADD(AddExample2);`（只改这一处）。
2. 按 u1-l2 的方式重新编译：`bash build.sh --pkg --soc=${soc_version} --ops=add_example -j16`，安装 run 包。
3. 运行 `bash build.sh --run_example add_example eager cust --vendor_name=custom` 观察报错。
4. 改回 `OP_ADD(AddExample);` 重新编译安装，恢复原状。

**需要观察的现象**：改名后注册表里只有 `AddExample2`，而样例和 proto 都按 `AddExample` 找算子，应当报「算子不存在/未注册」类错误。

**预期结果**：恢复原名后样例恢复正常输出。若你的环境无法完成编译，此实验为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`*_def.cpp` 里为什么看不到 `Add` 的计算代码？计算逻辑在哪里？

**答案**：def 文件是纯规格层（算子信息库），只声明输入输出规格、dtype/format 和编译配置。计算逻辑在 `op_kernel` 下的 Kernel 实现（如 `add_example.h` 的 `CopyIn/Compute/CopyOut`）中；shape 推导与 tiling 也分别在各自的 `*_infershape.cpp`、`*_tiling.cpp` 里注册。

**练习 2**：`OP_ADD` 宏展开后大概做什么事？为什么 ops-nn 上千个算子不需要一份集中注册清单？

**答案**：它展开出一个带静态构造副作用的注册器，在宿主库被加载时把算子类名与构造函数中收集的规格写入全局算子注册表。因为注册靠库加载时的静态初始化自动完成，每个算子目录自带 def 文件即可，无需集中清单，避免大规模仓库的合并冲突。

### 4.2 Input/Output 规格：ParamType、DataType、Format

#### 4.2.1 概念说明

`Input("x1")` / `Output("y")` 的链式调用声明了每个张量端口的四要素：

| 链式方法 | 含义 |
| --- | --- |
| `ParamType(REQUIRED)` | 参数必选。与之相对的是 `OPTIONAL`（可选输入）和 `DYNAMIC`（动态数量输入，如若干个 tensor 组成的列表） |
| `DataType({ge::DT_FLOAT, ge::DT_INT32})` | 该端口支持的 dtype 列表。**列表是有「槽位」语义的**：第 i 个 dtype 与第 i 个 Format 配对成一个候选组合 |
| `Format({ge::FORMAT_ND, ge::FORMAT_ND})` | 与 DataType 一一对应的格式列表，长度必须与 DataType 相同 |
| `UnknownShapeFormat(...)` | 图编译时 shape 未知（动态 shape）场景下使用的格式 |
| `AutoContiguous()` | 框架在调用算子前自动把非连续内存整理成连续（详细背景见 u3-l3） |

「槽位配对」是初学者最容易踩的坑：`DataType({DT_FLOAT, DT_INT32}) + Format({FORMAT_ND, FORMAT_ND})` 表示两个候选组合——`(float, ND)` 和 `(int32, ND)`。如果想表达 `float 用 ND、int32 用 NC`，就要写 `Format({FORMAT_ND, FORMAT_NC})`。gelu 的三个 dtype 配三个 ND 就是同样的展开方式。

#### 4.2.2 核心流程

```text
用户调用（如 aclnn 传来的 aclTensor 描述）
  └─> 框架取 def 中对应端口的 DataType/Format 候选列表
        └─> 输入张量的 (dtype, format) 是否命中某个槽位？
              是 → 通过校验，继续走 infershape/tiling/kernel
              否 → 直接报参数不支持错误，kernel 根本不会被调起
```

也就是说，def 的规格声明是算子的**第一道闸门**：你在 def 里没声明的类型，后面 tiling、kernel 写得再对也用不上；反过来，def 里声明了但 tiling/kernel 没处理的类型，会在运行期失败。def、tiling、kernel 三者对 dtype 的支持范围必须**一致**——这正是本讲综合实践要验证的。

#### 4.2.3 源码精读

AddExample 输入 x1 的完整声明：

[examples/add_example/op_host/add_example_def.cpp:44-49](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L44-L49) 声明输入 `x1`：必选参数；dtype 候选为 float32 与 int32；两个候选都是 ND 格式；未知 shape 时也用 ND；并开启内存自动连续化。x2 与 y 的声明完全相同（[L51-L63](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L51-L63)），因为 elementwise 加法要求输入输出同构。

gelu 的对应声明，展示了「一个输入、三个 dtype 槽位」的写法：

[activation/gelu/op_host/gelu_def.cpp:23-27](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp#L23-L27) 输入 `x` 支持 `{DT_BF16, DT_FLOAT16, DT_FLOAT}` 三个 dtype，配三个 `FORMAT_ND`——三个槽位。输出 `y`（[L28-L32](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp#L28-L32)）与输入对称。注意 gelu **没有**调用 `AutoContiguous()`，非连续张量的处理放在了 aclnn 适配层（u2-l1 讲过的 Contiguous→Gelu→ViewCopy 链条）——同一问题可以在不同层解决，这是真实工程里的取舍。

def 声明最终会落到二进制清单。看 `--pkg` 模式预编译产物的描述文件：

[examples/add_example/op_host/config/ascend910b/add_example_binary.json:4-28](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/config/ascend910b/add_example_binary.json#L4-L28) `op_list` 的第一项把输入 `x1`/`x2` 描述为 `dtype: float32, format: ND, paramType: required, shape: [-2]`（-2 表示动态 shape），对应一个具体的 `bin_filename` 编译产物；[L47-L71](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/config/ascend910b/add_example_binary.json#L47-L71) 第二项是 int32 版本。**一个 def 里的两个 dtype 槽位，展开成两份预编译 kernel 二进制**——这就是槽位语义在产物侧的体现。

#### 4.2.4 代码实践

**实践目标**：验证「def 是第一道闸门」——只改 def 不改 kernel，观察行为变化。

**操作步骤**：

1. 在 `add_example_def.cpp` 中，把 `x1` 的 `DataType({ge::DT_FLOAT, ge::DT_INT32})` 里删掉 `ge::DT_INT32`，只留 `ge::DT_FLOAT`（`Format` 列表也同步减为 1 个）。
2. 重新编译安装：`bash build.sh --pkg --soc=${soc_version} --ops=add_example -j16`。
3. 修改样例 `examples/add_example/examples/test_aclnn_add_example.cpp`，把输入 dtype 换成 `ACL_INT32`（数据 vector 改为 `int32_t`），运行 `bash build.sh --run_example add_example eager cust --vendor_name=custom`。
4. 恢复 def 原状，重新编译安装。

**需要观察的现象**：第 3 步应当在校验阶段就报「dtype 不支持」类错误，尽管 kernel 代码里 int32 分支原封未动。

**预期结果**：恢复 def 后 int32 输入重新可用。此实验需要完整编译环境，无法本地执行时标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`DataType({ge::DT_FLOAT, ge::DT_INT32})` 配 `Format({ge::FORMAT_ND, ge::FORMAT_ND})`，一共声明了几个候选组合？分别是什么？

**答案**：2 个：`(float32, ND)` 和 `(int32, ND)`。DataType 与 Format 按下标配对成槽位。

**练习 2**：如果 `DataType` 写了 3 个枚举、`Format` 只写了 2 个，会发生什么？

**答案**：两个列表长度不一致，槽位无法配对，属于非法声明，注册/编译阶段会出错。规则是 Format 列表长度必须与 DataType 列表一致（gelu 的 3 对 3、add_example 的 2 对 2 都遵守这一点）。

**练习 3**：AddExample 在 def 里开了 `AutoContiguous()`，gelu 没开。非连续输入问题 gelu 是在哪一层解决的？

**答案**：在 aclnn 适配层。u2-l1 分析过，`aclnnGelu` 的执行链是 Contiguous→Gelu→ViewCopy，先做连续化再进算子。同一问题既可以在 def 层用 `AutoContiguous()` 声明式解决，也可以在适配层手动编排，两种工程选择各有代价。

### 4.3 OpAICoreConfig 编译开关与多 SOC 配置

#### 4.3.1 概念说明

`OpAICoreConfig` 描述算子在 AI Core 上的**编译期行为开关**，再通过 `AICore().AddConfig("<soc>", config)` 挂到具体芯片上。add_example 用到的开关：

| 开关 | add_example 取值 | 含义 |
| --- | --- | --- |
| `DynamicCompileStaticFlag` | true | 支持「动态算子静态编译」优化：动态 shape 算子按静态方式预编译 |
| `DynamicFormatFlag` | false | 不支持运行时动态推导格式（format 需在调用前确定） |
| `DynamicRankSupportFlag` | true | 支持动态维数（rank 可变，如 3 维与 4 维输入都能跑） |
| `DynamicShapeSupportFlag` | true | 支持动态 shape（某一维长度未知，binary json 里的 `shape: [-2]` 即此含义） |
| `NeedCheckSupportFlag` | false | 调用前是否需要额外的「支持性检查」流程（旧式 aicpu/老版本兼容场景用） |
| `PrecisionReduceFlag` | true | 允许框架在精度允许时降精度（如 float 降 half）以换取性能 |
| `ExtendCfgInfo("opFile.value", "add_example")` | — | 扩展配置：指定 kernel 入口文件名（不带扩展名），编译系统据此定位 `add_example.cpp` |

这些开关只在**编译/图编译阶段**起作用，不影响 kernel 内部逻辑——它们决定框架允许以什么方式调用这个算子。例如 `DynamicShapeSupportFlag(false)` 的算子遇到 `shape: [-2]` 的输入会在编译期被拒绝。

#### 4.3.2 核心流程

```text
构造 OpAICoreConfig aicoreConfig（一份开关组合）
  └─> this->AICore().AddConfig("ascend910b", aicoreConfig)   # 同一份配置挂 910b
        .AddConfig("ascend910_93", aicoreConfig)             # 再挂 A3
        .AddConfig("ascend950", aicoreConfig)                 # 再挂 950
  └─> 编译时按 --soc=ascend910b 只编对应芯片的 kernel 二进制
  └─> config/${soc_version}/ 下生成/携带 binary json 与预编译产物
```

同一份 `aicoreConfig` 复用挂三个芯片，说明这份配置对三代芯片都成立；如果某代芯片需要不同开关或不同 kernel 文件，就构造第二份 `OpAICoreConfig` 单独 `AddConfig`。gelu 只挂了 `ascend950`，意味着仓库当前只为 950 编译交付 gelu。

#### 4.3.3 源码精读

[examples/add_example/op_host/add_example_def.cpp:66-74](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L66-L74) 构造 `OpAICoreConfig`：开启动态静态编译、动态 rank、动态 shape 与精度降低，关闭动态格式与支持性检查，并通过 `ExtendCfgInfo("opFile.value", "add_example")` 把配置与 kernel 入口文件 `add_example.cpp` 绑定。

[examples/add_example/op_host/add_example_def.cpp:76-78](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L76-L78) 把这份配置分别挂到 `ascend910b`、`ascend910_93`、`ascend950` 三代芯片——AddExample 是全芯片适配的教学样例。

[activation/gelu/op_host/gelu_def.cpp:34-42](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp#L34-L42) gelu 的开关组合与 add_example 相同，但 `opFile.value` 指向 `gelu_apt`（对应 u5-l3 将精读的 `gelu_apt.cpp`），且只 `AddConfig("ascend950")`——生产算子按交付计划只配目标芯片。

[examples/add_example/op_host/config/ascend910b/add_example_binary.json:1-3](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/config/ascend910b/add_example_binary.json#L1-L3) 二进制描述文件按 `op_type` 与芯片目录组织，是 `AddConfig("ascend910b", ...)` 在产物目录结构上的投影：换一块芯片，json 就放在 `config/ascend910_93/` 等对应目录下。

#### 4.3.4 代码实践

**实践目标**：验证 SOC 配置与编译目标的对应关系。

**操作步骤**：

1. 读 [examples/add_example/op_host/add_example_def.cpp:76-78](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L76-L78)，记下三个 soc 名。
2. 查看 `examples/add_example/op_host/config/` 下实际存在哪些芯片子目录（目前仓库里是 `ascend910b`）。
3. 对照 u1-l2：用 `--soc=ascend910b` 编译，再换 `--soc=ascend910`（一个 def 里不存在的芯片名）编译，观察第二次的报错。

**需要观察的现象**：`--soc` 传入 def 未 `AddConfig` 的芯片名时，cmake/编译阶段应报「算子不支持该 soc」类错误或直接无产物可编。

**预期结果**：`ascend910b` 正常出包；未配置的芯片名失败。芯片环境不具备时标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`DynamicShapeSupportFlag(true)` 与 binary json 里的 `"shape": [-2]` 是什么关系？

**答案**：前者是 def 层声明「本算子支持动态 shape」，后者是产物清单里对每个输入输出 shape 的具体描述，`-2` 即「该维长度未知/动态」。只有前者为 true，框架才会允许 `-2` 描述的张量匹配到这份二进制。

**练习 2**：`ExtendCfgInfo("opFile.value", "add_example")` 里的字符串指向什么？

**答案**：kernel 入口文件名（不含扩展名），即 `op_kernel/add_example.cpp`。编译系统靠它把 def 的编译配置与具体 kernel 源文件关联起来。gelu 对应的是 `gelu_apt`。

**练习 3**：为什么 gelu 只 `AddConfig("ascend950")` 而 add_example 挂了三块芯片？

**答案**：add_example 是官方教学样例，追求全芯片演示；gelu 是生产交付算子，仓库当前只为 ascend950 交付（其 kernel 还有 arch35 子场景目录做多架构细分，见 u5-l3）。AddConfig 的芯片集合就是该算子的交付范围。

### 4.4 def/tiling/kernel 三层联动：为 AddExample 增加 DT_INT16

#### 4.4.1 概念说明

前三个模块分别看了 def 的三个侧面。本模块把它们串起来回答一个工程问题：**「在 def 里声明一个新 dtype」到底意味着什么？**

答案是三层联动：

1. **def 层**：`DataType` 列表加 `ge::DT_INT16`，`Format`/`UnknownShapeFormat` 列表同步加一项，打开第一道闸门。
2. **tiling 层**：`add_example_tiling.cpp` 里有一个显式的 dtype 白名单 `supportedDtype`，以及按 dtype 分派 tiling key 的 if-else——都要加 int16 分支。
3. **kernel 层**：入口函数按 tiling key 用 `if constexpr` 分发到 `AddExample<float>` / `AddExample<int32_t>` 模板实例——要加 `AddExample<int16_t>` 分支，tiling key 头文件也要声明新的 MODE_2。

这三处正好对应 u1-l3 讲过的「目录是合同」：def 管准入，tiling 管切分与分派，kernel 管计算。少改任何一层，新 dtype 都会在某一层被卡住。

#### 4.4.2 核心流程

```text
用户以 DT_INT16 调用 AddExample
  ├─ def 层：DataType 列表含 DT_INT16？ —— 否 → 参数校验失败
  ├─ tiling 层：supportedDtype 含 DT_INT16？
  │     └─ 是 → 计算切分，SetTilingKey(MODE_2) 写入 tiling data
  └─ kernel 层：入口按 tiling key 实例化 AddExample<int16_t>
        └─ CopyIn/Compute/CopyOut 以 int16 元素宽度执行
```

还有一个隐蔽的第四处：tiling 中计算 UB 切分用了常量 `TYPE_SIZE = 4`（按 float/int32 的 4 字节估算单个元素大小）。int16 只有 2 字节，沿用 4 会导致 `ubFactor` 偏保守（UB 利用率减半），功能仍正确但性能打折；严谨做法是让 TYPE_SIZE 随 dtype 变化。

#### 4.4.3 源码精读

tiling 层的 dtype 白名单与分派：

[examples/add_example/op_host/add_example_tiling.cpp:139-147](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L139-L147) `GetShapeAttrsInfo` 用 `std::set<ge::DataType> supportedDtype = {ge::DT_FLOAT, ge::DT_INT32}` 做白名单校验——即使 def 放行了，这里没登记的 dtype 也会报 `invalid dtype`。这就是「def 与 tiling 支持范围必须一致」的代码证据。

[examples/add_example/op_host/add_example_tiling.cpp:227-240](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L227-L240) 主 tiling 函数末尾按 dtype 设置 tiling key：float → `MODE_0`，int32 → `MODE_1`，其它报错。加 int16 就要在此加 `MODE_2` 分支。

[examples/add_example/op_host/add_example_tiling.cpp:41-43](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L41-L43) `TYPE_SIZE = 4` 常量在 [L222](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L222) 参与 `ubFactor` 计算（`ubCanUse / TYPE_SIZE` 把字节数换算成元素数）——int16 场景应取 2。

kernel 层的分派：

[examples/add_example/op_kernel/add_example.cpp:24-27](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp#L24-L27) 定义 tiling key 枚举：0 对应 float、1 对应 int32。新增 int16 即加 `TILING_KEY_EXAMPLE_INT16 = 2`。

[examples/add_example/op_kernel/add_example.cpp:45-56](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp#L45-L56) 入口函数按模板参数 `schMode` 用 `if constexpr` 分发到 `AddExample<float>` / `AddExample<int32_t>`。照葫芦画瓢加一个 `schMode == 2` 分支实例化 `AddExample<int16_t>` 即可——模板机制保证新增分支对 Kernel 类零侵入。

tiling key 的模板声明：

[examples/add_example/op_kernel/add_example_tiling_key.h:21-28](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example_tiling_key.h#L21-L28) 用 `ASCENDC_TPL_ARGS_DECL` 声明模板参数 `schMode` 的合法取值列表（当前只有 MODE_0/MODE_1）。加 `ELEMENTWISE_TPL_SCH_MODE_2` 后要同步把新值追加进 `ASCENDC_TPL_ARGS_DECL` 与 `ASCENDC_TPL_SEL` 的取值列表，编译系统才能为新的 key 组合生成二进制（binary json 里也就多出一组 int16 条目）。

#### 4.4.4 代码实践

**实践目标**：完整走通「def → tiling → kernel」三层联动，为 AddExample 增加 `DT_INT16` 支持。

**操作步骤**：

1. **改 def**：在 `add_example_def.cpp` 的三处（x1、x2、y）把 `DataType({ge::DT_FLOAT, ge::DT_INT32})` 改为 `DataType({ge::DT_FLOAT, ge::DT_INT32, ge::DT_INT16})`，`Format` 与 `UnknownShapeFormat` 列表各补一个 `ge::FORMAT_ND`。
2. **改 tiling**：在 `add_example_tiling.cpp` 的 `supportedDtype` 集合中加入 `ge::DT_INT16`；在 tiling key 分派处加 `else if (dataType == ge::DT_INT16) { tilingKey = GET_TPL_TILING_KEY(ELEMENTWISE_TPL_SCH_MODE_2); context->SetTilingKey(tilingKey); }`；把 `TYPE_SIZE` 从常量改为按 dtype 取值（int16 取 2，其余取 4）。
3. **改 tiling key 声明**：在 `add_example_tiling_key.h` 中加 `#define ELEMENTWISE_TPL_SCH_MODE_2 2`，并把 `ASCENDC_TPL_ARGS_DECL` / `ASCENDC_TPL_SEL` 的取值列表各追加 `ELEMENTWISE_TPL_SCH_MODE_2`。
4. **改 kernel 入口**：在 `add_example.cpp` 的枚举加 `TILING_KEY_EXAMPLE_INT16 = 2`，并加一段 `if constexpr (schMode == ...INT16)` 分支，实例化 `NsAddExample::AddExample<int16_t>`。
5. **改样例**：复制 `test_aclnn_add_example.cpp` 的构造逻辑，把 aclTensor 的 dtype 换为 `ACL_INT16`，输入输出 vector 改为 `int16_t`（注意 shape 乘积与 vector 长度一致，参见 u1-l4 的教训）。
6. **编译验证**：`bash build.sh --pkg --soc=${soc_version} --ops=add_example -j16`，安装 run 包，运行 `bash build.sh --run_example add_example eager cust --vendor_name=custom`。

**需要观察的现象**：int16 输入的样例输出应为两组 int16 输入的逐元素之和；同时可打开 `config/${soc_version}/` 下重新生成的 binary json，确认多出一组 `dtype: int16` 的条目（是否自动再生成取决于构建流程，待本地验证）。

**预期结果**：样例打印的输出与 CPU 侧手算的 int16 加法结果一致。若只改 def 不改 tiling，运行会在 tiling 阶段报 `invalid dtype`；若 def/tiling 都改了但 kernel 没加分支，会在 kernel 分发阶段失败——可以故意分层验证，体会三层各自的卡点。本实践依赖完整编译与 Atlas 环境，无法在纯阅读环境完成，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果只在 def 里加了 `DT_INT16`，tiling/kernel 都没动，调用 int16 输入会在哪里失败、报什么？

**答案**：在 tiling 阶段失败。`GetShapeAttrsInfo` 的 `supportedDtype` 白名单不含 `DT_INT16`，走 `OP_LOGE(context, "invalid dtype")` 并返回 `ge::GRAPH_FAILED`，kernel 根本不会被调起。

**练习 2**：为什么加 int16 后建议把 `TYPE_SIZE` 从常量改成按 dtype 取值？不改会怎样？

**答案**：`TYPE_SIZE` 用于把 UB 字节数换算成元素数以计算 `ubFactor`。int16 元素只有 2 字节，按 4 计算会让每次搬运的元素数减半、搬运次数翻倍——功能正确但 UB 利用率下降、性能受损。改成按 dtype 取值（int16 取 2）才能吃满 UB。

**练习 3**：binary json 里两个 dtype 槽位对应两份 `bin_filename`。加入 int16 后产物侧应该发生什么？

**答案**：`op_list` 应多出第三项，输入输出 `dtype` 为 `int16`，绑定一份新的 `bin_filename` 预编译二进制——每个 (dtype, format) 槽位组合各自编译一份 kernel 产物。该 json 由构建过程生成/更新，具体再生成行为待本地验证。

## 5. 综合实践

**任务：给 AddExample 做「dtype 全链路体检」，再实施 DT_INT16 扩展。**

1. 先做体检（纯阅读，无需环境）：对照本讲 4.4 的三处代码点，填写下表——def 的 DataType 列表、tiling 的 `supportedDtype` 集合、kernel 的 tiling key 分支，确认三者对 float/int32 完全对齐。
2. 再做扩展（需编译环境）：按 4.4.4 的六个步骤完成 `DT_INT16` 支持，编译、安装、运行验证。
3. 最后做回归：确认 float 与 int32 两个原有 dtype 的样例仍然通过，理解「加槽位」理论上不影响旧槽位，但如果 `TYPE_SIZE` 改动影响了共享的 `ubFactor` 计算路径，需要用 float 样例回归确认无副作用（待本地验证）。

完成后你应当获得一个可复用的肌肉记忆：**改算子规格 = def 放行 + tiling 认路 + kernel 干活，三层缺一不可。**

## 6. 本讲小结

- `*_def.cpp` 是算子信息库（规格层）：只声明「是谁、输入输出长什么样、在哪些芯片上可用」，不含计算逻辑；`OP_ADD` 宏在库加载时把它静态注册进全局算子注册表。
- `Input`/`Output` 的 `ParamType`（REQUIRED/OPTIONAL/DYNAMIC）、`DataType` 与 `Format` 按下标配对成候选槽位，是算子参数校验的第一道闸门；`UnknownShapeFormat` 服务动态 shape 场景，`AutoContiguous` 声明式解决非连续内存（gelu 则选择在 aclnn 适配层解决）。
- `OpAICoreConfig` 的 `DynamicShapeSupportFlag` 等开关描述编译期行为（动态 shape/rank、精度降低等），`ExtendCfgInfo("opFile.value", ...)` 绑定 kernel 入口文件，`AICore().AddConfig("<soc>", ...)` 决定算子的芯片交付范围。
- def 里的每个 dtype 槽位最终落到 binary json 里的一份独立预编译二进制；`shape: [-2]` 对应动态 shape 支持。
- 新增 dtype 是三层联动：def 放行、tiling 白名单 + tiling key 分派、kernel 模板分支；此外还要检查 tiling 中元素宽度假设（TYPE_SIZE）这类隐式依赖。

## 7. 下一步学习建议

- 下一讲 **u3-l2（Shape 推导：Infershape 的实现与验证）** 将进入 op_host 的第二份交付件 `*_infershape.cpp`，看输出 shape 如何由输入 shape 推导出来，并学习用 infershape UT 验证推导逻辑。
- 之后 **u3-l3** 会系统补齐 dtype/format 的背景知识（ND/NZ/FRAC、非连续 Tensor），加深对本讲槽位语义的理解。
- 建议顺带精读 [docs/zh/develop/aicore_develop_guide.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md) 的「算子定义」「Tiling 实现」两节，把 def 与下一单元的 tiling 交付件清单串起来。
