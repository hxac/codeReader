# 从教学样例到生产算子：gelu 内核源码阅读

## 1. 本讲目标

前面两讲（u5-l1、u5-l2）我们已经把 `examples/add_example` 这个教学样例的 Kernel 结构、三段式流水、入口函数与数据搬运彻底拆完了。教学样例是「为了看懂而写」的：所有逻辑都摊开在一个头文件里，没有一层封装。

本讲要完成一次视角升级：**读一个真实的生产算子 `activation/gelu`**。读完本讲，你应该能够：

1. 对比出教学样例与生产算子在工程结构上的系统性差异（多架构目录、构建开关、配套测试）。
2. 理解 `arch35` 这类多架构适配目录的组织方式，以及 `ascend950` 等新芯片上算子工程的特殊之处。
3. 看懂生产算子 kernel 中「模板参数 + 计算图（DAG）描述 + MicroAPI 内联」这套分层写法，理解为什么生产代码要这样组织。
4. 掌握一套可复用的「入口 → tiling → 搬运」阅读框架，能独立去读仓库里任何一个矢量算子。

## 2. 前置知识

本讲假设你已学完 u5-l1（Ascend C 编程模型：GM/UB 两级存储、TPipe/TQue、CopyIn-Compute-CopyOut 三段式）和 u5-l2（kernel 入口、`GET_TILING_DATA_WITH_STRUCT`、DataCopy 与 32 字节对齐）。在此基础上补充三个新概念：

- **DAG（有向无环图）描述计算**：生产算子常用「把计算步骤声明成一条链式类型别名」的方式描述数据流，比如「搬入 → 类型提升 → 计算 → 类型回转 → 搬出」。编译器/公共调度框架按这张图生成搬运与计算的编排。类型别名在编译期展开，运行期没有图的开销。
- **MicroAPI**：比 `AscendC::Add` 这类高层矢量接口更贴近硬件的一层内联 API，直接操作矢量寄存器（`RegTensor`）和掩码寄存器（`MaskReg`）。高层接口最终也会展开成这些调用；生产算子在热点路径上直接写 MicroAPI 以榨取性能。
- **AIV 与 AIC 分离**：在 ascend950 这类新架构上，矢量核心（AIV）与矩阵核心（AIC）的分工更明确。纯逐元素算子只跑在矢量核心上，kernel 里会用 `KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY)` 显式声明这一点（教学样例没有这句）。

另外回顾一个数学恒等式，本讲源码会用到：\( \tanh(z) = 2\sigma(2z) - 1 \)，其中 \( \sigma \) 是 sigmoid 函数。由它可推出 \( 0.5(1+\tanh(z)) = \sigma(2z) \)。这正是 gelu 源码里 tanh 写法与 sigmoid 写法互相转化的桥梁，第 4.4 节会用到。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `activation/gelu/README.md` | 算子功能、支持产品、参数约束（含计算公式） |
| `activation/gelu/CMakeLists.txt` | 声明支持的芯片类型、tiling 目录、aclnn 排除等构建开关 |
| `activation/gelu/op_kernel/gelu_apt.cpp` | kernel 入口函数（本讲精读主线） |
| `activation/gelu/op_kernel/arch35/gelu_dag.h` | 用 DAG 类型别名 + MicroAPI 描述 gelu 的真实计算 |
| `activation/gelu/op_kernel/arch35/gelu_struct.h` | 模板参数（tiling key 取值集合）声明 |
| `activation/gelu/op_host/arch35/gelu_tiling_arch35.h/.cpp` | Host 侧 tiling：校验 + 委托公共 `ElewiseBaseTiling` |
| `activation/gelu/op_host/gelu_def.cpp` | 算子原型定义（u3-l1 已讲，本讲只取其芯片配置部分对照） |
| `activation/gelu/op_host/config/ascend950/gelu_binary.json` | 预编译二进制清单：每个 dtype 一份 |
| `examples/add_example/op_kernel/add_example.h/.cpp` | 教学样例对照组（u5-l1/u5-l2 已精读） |
| `activation/gelu/tests/ut/op_kernel/test_gelu_apt.cpp` | kernel UT：直接 include 入口源文件做仿真执行 |

注意：`gelu_apt.cpp` 中 include 的 `atvoss/elewise/elewise_sch_16b.h`、`atvoss/util/dag.h` 等头文件**不在本仓库内**，它们来自配套 CANN 包的 `op_common/atvoss/` 目录（tiling 侧 include 路径 `op_common/atvoss/elewise/elewise_tiling.h` 可证）。这些是公共调度框架，本讲会从调用侧推断其行为，不深入其内部实现。

## 4. 核心概念与源码讲解

### 4.1 工程结构对比：从 add_example 骨架到 gelu 生产工程

#### 4.1.1 概念说明

u1-l3 曾给出算子工程的「标准目录合同」：op_host、op_kernel、op_api、op_graph、tests。教学样例 add_example 是这份合同的最小子集；生产算子 gelu 是超集。差异不在「多几个目录」，而在三个维度：

1. **精度策略**：gelu 支持 FP16/BF16/FP32 三种 dtype，且半精度在内部提升为 float 计算再回转——类型处理成为代码的一等公民。
2. **多架构**：kernel 与 tiling 都放在 `arch35/` 子目录下，目录名本身就是架构代号。
3. **构建开关**：CMakeLists 里出现了教学样例没有的变量，控制这套源码「在哪些芯片上、以什么身份」参与编译。

#### 4.1.2 核心流程

gelu 工程的目录全貌：

```text
activation/gelu/
├── CMakeLists.txt            # 芯片与 tiling 目录声明
├── README.md                 # 功能/参数/样例索引
├── docs/aclnnGelu.md         # 接口文档
├── examples/test_aclnn_gelu.cpp
├── framework/gelu_tf_plugin.cpp
├── op_api/                   # aclnn 四件套（gelu.cpp/.h + aclnn_gelu.cpp/.h，u2-l1 已讲）
├── op_graph/gelu_proto.h     # GE 图模式原型（u2-l2 已讲）
├── op_host/
│   ├── gelu_def.cpp          # 算子原型（u3-l1 已讲）
│   ├── gelu_infershape.cpp   # shape 推导（u3-l2 已讲）
│   ├── arch35/               # ★ 架构专属 tiling
│   └── config/ascend950/gelu_binary.json  # 预编译二进制清单
├── op_kernel/
│   ├── gelu_apt.cpp          # kernel 入口
│   └── arch35/               # ★ 架构专属 DAG 与模板参数声明
└── tests/                    # ut(op_host/op_kernel/op_api) + st + golden.py
```

与 add_example 对照：add_example 的 op_kernel 下是 `add_example.h/.cpp` 两个平铺文件加两个 tiling 头；gelu 则把 kernel 的核心内容下沉到 `arch35/` 子目录，入口文件只剩 30 行。add_example 没有 tests 之外的 golden 脚本与 st 目录，gelu 两者齐备。

#### 4.1.3 源码精读

先看构建声明——这是理解「这份源码为谁编译」的钥匙：

[activation/gelu/CMakeLists.txt:L12-L15](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/CMakeLists.txt#L12-L15)：声明 `SUPPORT_COMPUTE_UNIT "ascend950" "mc62"` 与 `SUPPORT_TILING_DIR "arch35" "arch35"`——本仓库中这份 gelu 源码**只参与 ascend950/mc62 两个芯片目标**的编译，且两个芯片都用 `arch35` 目录下的 tiling 文件。

[activation/gelu/CMakeLists.txt:L15](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/CMakeLists.txt#L15)：`ACLNNTYPE aclnn_exclude` 与 `DISABLE_IN_OPP TRUE`——排除 aclnn 库构建、不安装进 opp 内置目录。结合 README 声称的广泛产品支持（[activation/gelu/README.md:L5-L12](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/README.md#L5-L12)）可以推断：**老芯片上的 Gelu 由 CANN 商用包提供，本仓库这份开源实现服务于新架构 ascend950**；README 的支持矩阵描述的是「Gelu 这个算子」而非「这份源码」。这一点与 u1-l1 讲过的「版本配套为硬约束」一脉相承。

再看 README 中的计算公式（[activation/gelu/README.md:L18-L22](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/README.md#L18-L22)）：

\[ \text{out} = 0.5 \cdot x \cdot \left(1 + \tanh\left(\sqrt{2/\pi}\,\left(x + 0.044715\,x^{3}\right)\right)\right) \]

这是第 4.4 节源码精读的「需求基准」。

#### 4.1.4 代码实践

1. **实践目标**：建立两个工程的目录级对比表，确认「目录是合同」这条 u1-l3 结论在生产算子上依然成立。
2. **操作步骤**：
   - 对照上面的目录树，在本地执行 `ls -R examples/add_example activation/gelu | head -60`。
   - 打开 [docs/zh/install/dir_structure.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/dir_structure.md)，为 gelu 的每个子目录标注对应的交付物类型。
3. **需要观察的现象**：gelu 比 add_example 多出的目录（framework、docs、st、arch35）分别对应什么交付物；哪些目录在 add_example 中完全缺席。
4. **预期结果**：得到一张两列对照表；能回答「如果我拷贝 gelu 的 op_kernel 到自己的工程，还缺哪些文件才能编译」。

#### 4.1.5 小练习与答案

**练习 1**：`SUPPORT_TILING_DIR` 设置为 `arch35` 意味着编译时使用哪个目录下的 tiling 源文件？

**答案**：使用 `op_host/arch35/` 下的 `gelu_tiling_arch35.cpp/.h`，而不是 `op_host/` 顶层的 tiling 文件。这就是「按芯片选 tiling 目录」的机制——同一算子可以为不同架构准备不同 tiling 实现。

**练习 2**：README 声称支持 Atlas A2 等老产品，但 CMake 里 `SUPPORT_COMPUTE_UNIT` 只有 ascend950/mc62，矛盾吗？

**答案**：不矛盾。README 描述的是 Gelu 算子在华为产品线上的总体支持情况（老芯片由 CANN 商用包内的实现提供）；本仓库这份开源源码的编译目标只有新架构。构建声明决定「这份源码编给谁」，两者叙述对象不同。

### 4.2 arch35 多架构适配目录与预编译二进制

#### 4.2.1 概念说明

不同代际的 Ascend 芯片在核心数量、UB 大小、指令集上都有差异。ops-nn 的适配策略不是在一个文件里写满 `#ifdef`，而是**按架构代号分目录**：`arch35` 是一个架构代号（对应 ascend950 这一代的 AIV 矢量架构），目录内的代码只服务这一架构。若未来有另一代架构需要不同实现，就并列加一个新目录，CMake 的 `SUPPORT_TILING_DIR` 按芯片指向对应目录。

与之配套的是 u3-l1 讲过的 binary json：每个 dtype 槽位对应一份独立的预编译 kernel 二进制。gelu 有三个 dtype，因此 json 里登记了三份。

#### 4.2.2 核心流程

```text
CMake 选择芯片目标 (ascend950/mc62)
        │  按 SUPPORT_TILING_DIR 指到 arch35
        ▼
op_host/arch35/gelu_tiling_arch35.cpp   ← Host 侧 tiling（架构专属）
op_kernel/arch35/gelu_dag.h             ← Device 侧计算描述（架构专属）
op_kernel/arch35/gelu_struct.h          ← 模板参数声明（架构专属）
        │  编译
        ▼
每个 dtype 一份二进制，登记进 gelu_binary.json
（bfloat16 / float16 / float32 三份，shape 均为 [-2] 动态）
```

#### 4.2.3 源码精读

[activation/gelu/op_host/gelu_def.cpp:L42-L42](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp#L42)：`this->AICore().AddConfig("ascend950", aicoreConfig)` 与 CMake 的芯片声明互相印证——def 声明芯片交付范围（u3-l1），CMake 决定实际编译目标，两处需一致。同文件 [L41](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp#L41) 的 `ExtendCfgInfo("opFile.value", "gelu_apt")` 把 def 与 kernel 入口文件 `gelu_apt.cpp` 绑定（对照 add_example 的同名约定）。

[activation/gelu/op_host/config/ascend950/gelu_binary.json:L4-L32](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/config/ascend950/gelu_binary.json#L4-L32)：`op_list` 数组的第一个条目——`bin_filename` 为一份预编译二进制，输入输出均为 `bfloat16`、`ND`、`shape: [-2]`。整个 json 共三个条目，dtype 分别为 bfloat16、float16、float32，与 def 中 `DataType({ge::DT_BF16, ge::DT_FLOAT16, ge::DT_FLOAT})`（[gelu_def.cpp:L25](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp#L25)）逐一对应。`format_match_mode: "FormatAgnostic"` 表示格式无关匹配（ND 无轴语义，u3-l3）。

注意 add_example 也有 `config/ascend910b/add_example_binary.json`（u9-l4 会用到），机制相同——多架构不是 gelu 专属，而是仓库通用约定；gelu 的特殊之处在于**源码目录本身**也按架构分了层。

#### 4.2.4 代码实践

1. **实践目标**：验证「def 的 dtype 槽位 ↔ binary json 条目 ↔ 模板参数取值」三者一一对应。
2. **操作步骤**：
   - 读 [gelu_def.cpp:L23-L32](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp#L23-L32)，抄下 x 的 DataType 列表。
   - 读 gelu_binary.json，抄下每个条目的 dtype。
   - 读下一节的 `gelu_struct.h`，抄下 `dType` 的取值宏。
3. **需要观察的现象**：三份列表的长度与成员是否完全一致；顺序是否相同。
4. **预期结果**：三处均为 BF16/FP16/FP32 三个成员。这正是 u3-l1 讲过的「三道独立类型闸门」在 gelu 上的具体形态（def 槽位、tiling key 分支、模板实例），错位任何一处都会静默选错二进制。

#### 4.2.5 小练习与答案

**练习 1**：如果想给 gelu 增加第四个 dtype（如 INT8），按本节机制需要动哪几处？

**答案**：至少四处：def 的 `DataType` 列表加槽位；binary json 加一个条目（或改走 JIT 编译）；`gelu_struct.h` 的 `dType` 声明加取值；kernel 入口加对应 `if constexpr` 分支并在 DAG 层提供该类型的计算实现。这呼应 u3-l1 综合实践「三层缺一不可」。

**练习 2**：`shape: [-2]` 在 json 里是什么含义？

**答案**：`-2` 表示该二进制支持任意动态 shape（u3-l1/u1-l3 已引入该记法）。gelu 是逐元素算子，切分逻辑由 tiling 在运行期决定，因此一份二进制可服务所有 shape，无需按 shape 预编译多份。

### 4.3 kernel 入口 gelu_apt.cpp：两维模板参数与调度器委托

#### 4.3.1 概念说明

add_example 的入口（u5-l2）把全部实现细节写在同一个头文件里，入口函数自己 new 算子对象、自己驱动 Init/Process。生产算子把这两件事彻底分开：

- **入口函数只做三件事**：注册 tiling 结构、读 tiling data、按模板参数实例化公共调度器。
- **真正的「切分循环 + 搬运 + 计算编排」委托给公共调度框架**（CANN 包 `op_common/atvoss/` 下的 `ElementwiseSch16B`），算子作者只需提供计算本身的 DAG 描述（下一节）。

这样做的收益：几十个逐元素算子共享同一套经过调优的调度/搬运/双缓冲代码，算子侧只维护「计算是什么」这一最小差异点。

模板参数也从 add_example 的一维（`schMode` 一种）扩展为**两维**：`schMode`（调度模式）与 `dType`（数据类型）。

#### 4.3.2 核心流程

gelu 入口的执行流程：

```text
__global__ gelu(x, y, workspace, tiling)
  ├─ REGISTER_TILING_DEFAULT(EleBaseTilingData16B)   # 声明 tiling 结构
  ├─ GET_TILING_DATA_PTR_WITH_STRUCT(...)             # 从 GM 还原 tiling（指针风格）
  ├─ KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY)   # 声明为纯矢量核
  └─ 按 dType 三分支（if constexpr）：
       ElementwiseSch16B<schMode, GeluDAG<T>::OpDag> sch(tilingData);
       sch.Init(x, y);     # 调度器完成 GM 窗口设置、UB 队列初始化
       sch.Process();      # 调度器驱动 切分→CopyIn→Compute→CopyOut 全流程
```

与 add_example 的对应关系：

| 环节 | add_example | gelu |
| --- | --- | --- |
| tiling 结构 | `AddExampleTilingData`（自研 3 字段） | `EleBaseTilingData16B`（公共框架） |
| 读取方式 | `GET_TILING_DATA_WITH_STRUCT`（值语义） | `GET_TILING_DATA_PTR_WITH_STRUCT`（指针语义） |
| 主循环位置 | `AddExample<T>::Process()`（自写） | `ElementwiseSch16B::Process()`（框架提供） |
| 计算位置 | `Compute()` 中一句 `AscendC::Add` | DAG 描述（`GeluDAG`，见 4.4） |
| 任务类型声明 | 无 | `KERNEL_TYPE_AIV_ONLY` |
| 模板参数 | `schMode` 一维 | `schMode + dType` 两维 |

#### 4.3.3 源码精读

[activation/gelu/op_kernel/gelu_apt.cpp:L26-L31](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/gelu_apt.cpp#L26-L31)：入口模板签名 `template <uint64_t schMode, uint64_t dType>` 与开头三行——注册公共 tiling 结构 `EleBaseTilingData16B`、以指针方式取 tiling data、声明 AIV-only。对照 add_example 的 [examples/add_example/op_kernel/add_example.cpp:L40-L42](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp#L40-L42)（`REGISTER_TILING_DEFAULT(AddExampleTilingData)` + 值语义读取），机制同源、写法略异。

[activation/gelu/op_kernel/gelu_apt.cpp:L41-L45](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/gelu_apt.cpp#L41-L45)：FP32 分支的全部三行——构造 `ElementwiseSch16B<schMode, GeluOp::GeluDAG<float>::OpDag> sch(tilingData)`，然后 `sch.Init(x, y); sch.Process();`。add_example 入口里「实例化算子对象并驱动」的职责没有变，变的只是对象从「手写 Kernel 类」换成「公共调度器 + 算子专属 DAG」的组合。FP16/BF16 分支（[L33-L40](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/gelu_apt.cpp#L33-L40)）结构完全相同，仅模板实参不同。

两维模板参数的取值集合在 [activation/gelu/op_kernel/arch35/gelu_struct.h:L21-L29](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/arch35/gelu_struct.h#L21-L29)：`TPL_FP16/BF16/FP32` 三个宏定义 dtype 取值，`ASCENDC_TPL_ARGS_DECL` 声明 `schMode`（取值 0/1）与 `dType`（三选一）两个模板参数。这正是 u4-l2 讲过的「tiling key 二进制选择器」声明——gelu 的 key 空间是 `schMode × dType` 的二维组合，而 add_example 只有一维（[examples/add_example/op_kernel/add_example_tiling_key.h:L24-L25](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example_tiling_key.h#L24-L25)）。

`GET_TILING_DATA_PTR_WITH_STRUCT` 与 `WITH_STRUCT` 的区别：前者拿到的是指针（后续以 `tilingData->` 访问），后者拿到的是栈上副本。二者都是「从 GM 按字节还原 Host 写好的 POD」（u4-l2 的字节契约），只是访问风格不同。

#### 4.3.4 代码实践

1. **实践目标**：不看本讲正文，独立说出 gelu 入口每一行对应 add_example 的哪个环节。
2. **操作步骤**：
   - 并排打开 `gelu_apt.cpp` 与 `add_example.cpp`（47 行 vs 57 行，都很短）。
   - 逐行配对：tiling 注册↔tiling 注册、tiling 读取↔tiling 读取、分支分发↔分支分发、`sch.Init/Process`↔`op.Init/Process`。
   - 记录两边 `Init` 的实参个数差异（gelu 只传 x、y 两个 GM 地址，add_example 传 x、y、z 三个再加 tiling 指针——因为调度器从 DAG 得知输入输出布局，且 tiling 已在构造时传入）。
3. **需要观察的现象**：入口函数的「骨架相似度」极高；差异全部集中在「谁提供 Process 的实现」。
4. **预期结果**：形成 6 行左右的对照笔记。若想进一步验证调度器行为，可阅读 CANN 包内 `op_common/atvoss/elewise/elewise_sch_16b.h`（路径在配套环境中，本仓库不含，具体实现待本地确认）。

#### 4.3.5 小练习与答案

**练习 1**：`KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY)` 这行在 add_example 中没有，去掉它会怎样？

**答案**：它向运行时声明该 kernel 只使用矢量核心。对新架构上 AIV/AIC 分离调度的芯片，缺少声明可能导致按默认混合任务类型分配资源、影响调度效率（具体行为依赖 CANN 版本，待本地验证）。gelu 是纯逐元素算子，显式声明是生产代码的规范性写法。

**练习 2**：为什么 gelu 的 `sch.Init` 只传 `x, y` 两个地址，而 add_example 的 `Init` 要传四个参数？

**答案**：gelu 的输入输出个数、搬运编排等信息都编码在 `GeluDAG` 的类型描述里，调度器据此知道「一个输入一个输出」；tiling data 也已在构造 `sch` 时传入。add_example 没有元信息层，全部靠手工传参。这是「元信息驱动」与「手工接线」两种风格的典型差异。

### 4.4 gelu_dag.h：DAG 描述计算 + MicroAPI 内联

#### 4.4.1 概念说明

`gelu_dag.h` 是 gelu 真正的「计算本体」，分两层：

- **外层 DAG（类型别名链）**：用 `Bind<算子, 前驱>` 把搬运与计算串成编译期类型链，声明「数据经过哪些站」。
- **内层 `GeluCustom`（MicroAPI 循环）**：DAG 中的一个自定义站点，用寄存器级 MicroAPI 写出 gelu 公式本身。

精度设计也在这层体现：FP16/BF16 输入先 `Cast` 提升为 float 计算，算完再 `Cast` 回原类型（RINT 舍入模式），即「外部半精度、内部全精度」。

#### 4.4.2 核心流程

DAG 数据流（`GeluDAG<U>`，U 为用户 dtype，T 默认 float）：

```text
GM(x, U 类型)
  │ CopyIn<U>                    搬入 UB
  ▼
Cast<float, U, NONE>             半精度 → float（FP32 路径等于直通）
  ▼
GeluCustom<float>                ★ gelu 公式（MicroAPI，见下）
  ▼
Cast<U, float, RINT>             float → 原类型（四舍五入到偶）
  ▼
CopyOut<U>                       搬回 GM(y)
```

`GeluCustom` 内层循环（float 分支）每个向量寄存器宽度的分片执行：

```text
vregInput ← DataCopy(UB)
vregInputSqr  = vregInput × vregInput            # x²
vregInputCub  = vregInputSqr × vregInput         # x³
vregInputCub  = Axpy(vregInputCub, vregInput, 1/0.044715)   # x³ + x/0.044715
vregInputCub  = Muls(vregInputCub, -1.5957691×0.044715)     # 取负常数倍
vregInputCub  = Exp(vregInputCub)                # e^(...)
vregInputCub  = Adds(vregInputCub, 1.0)          # 1 + e^(...)
vregOutput    = Div(vregInput, vregInputCub)     # x / (1+e^(...))
DataCopy(UB, vregOutput)
```

数学上验证它就是 README 的 tanh 公式。记 \( z = \sqrt{2/\pi}\,(x + 0.044715x^{3}) \)，则 \( 2\sqrt{2/\pi} \approx 1.5957691 \)，由 \( 0.5(1+\tanh z) = \sigma(2z) \) 得：

\[ \text{out} = x \cdot \sigma\big(1.5957691\,(x + 0.044715\,x^{3})\big) = \frac{x}{1 + e^{-1.5957691\,(x + 0.044715\,x^{3})}} \]

源码中指数部分是 \(-1.595769 \times 0.044715 \times (x/0.044715 + x^{3})\)，展开括号后正是上式（用 `Axpy` 一次完成「乘系数再加 x」是为了少发一条矢量指令）。tanh 与 sigmoid 两种写法只是恒等变形，硬件实现选了只需 Exp/Div 的后者。

#### 4.4.3 源码精读

[activation/gelu/op_kernel/arch35/gelu_dag.h:L74-L86](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/arch35/gelu_dag.h#L74-L86)：DAG 定义。`OpCopyIn0` 起链，依次 `Bind` 上提升 Cast、`GeluCustom`、回转 Cast、`CopyOut`，与上面流程图逐行对应；`MemOptCfg<MemLevel::LEVEL_2>` 声明内存优化级别，最后 `DAGSch<Outputs, void, MemCfg>` 把整条链封成一个可被调度器（4.3 节的 `ElementwiseSch16B`）驱动的类型。这一段没有一个运行期语句——**计算结构完全编码在类型里**。

[activation/gelu/op_kernel/arch35/gelu_dag.h:L25-L26](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/arch35/gelu_dag.h#L25-L26)：两个浮点常数 `NEG_SQRT_EIGHT_OVER_PI = -1.595769121 * 0.044715` 与 `TANH_APPROX_FACTOR = 1 / 0.044715`——名字保留了 tanh 公式的历史，实际用法是 4.4.2 节推导的 sigmoid 形式；[L29-L30](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/arch35/gelu_dag.h#L29-L30) 的注释如实记录了两种实现的等价改写。

[activation/gelu/op_kernel/arch35/gelu_dag.h:L34-L39](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/arch35/gelu_dag.h#L34-L39)：`GeluCustom` 的签名与准备——构造函数收到 UB 上的 `LocalTensor` 目的/源与元素数，按 `VECTOR_REG_WIDTH / sizeof(T)` 算出一个向量寄存器能装多少元素（`vl`），`CeilDivision(count, vl)` 得到循环次数，再用 `GetPhyAddr()` 拿 UB 物理地址。对照 add_example 的 `Compute()`（[add_example.h:L102-L111](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L102-L111) 只有一句 `AscendC::Add`）：生产代码把手递给寄存器层，因为 gelu 是 7 条指令的复合公式，高层接口逐条调用的开销在热点上不可忽略。

[activation/gelu/op_kernel/arch35/gelu_dag.h:L49-L67](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/arch35/gelu_dag.h#L49-L67)：float 分支的主循环——`UpdateMask` 按剩余元素数更新掩码（尾块靠掩码屏蔽多余 lane，这是比 add_example「currentNum 贯穿三阶段」更底层的尾块处理），随后 `DataCopy` 从 UB 装寄存器、7 条算术指令、`DataCopy` 写回 UB。注意这里的 `DataCopy` 是 **UB↔寄存器** 的 MicroAPI 搬运，与 u5-l2 讲的 GM↔UB `DataCopyPad` 同名不同层。

另外注意 [L49](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/arch35/gelu_dag.h#L49) 的 `if constexpr (std::is_same_v<T, float>)`：`GeluCustom` 只实现了 float 特化，因为外层 DAG 已保证半精度先提升——组合起来才是完整实现，单独读任何一个文件都会觉得「缺一块」。

#### 4.4.4 代码实践

1. **实践目标**：验证 4.4.2 节的数学等价性——亲手把 tanh 公式改写成 sigmoid 形式。
2. **操作步骤**：
   - 取 \( c = 0.044715 \)、\( k = 2\sqrt{2/\pi} \approx 1.5957691 \)。
   - 在 Python（或计算器）中任取 x（如 -2.0, 0.3, 1.7），分别计算：
     - `0.5*x*(1+math.tanh(k/2*(x+c*x**3)))`（README 公式，注意 \( \sqrt{2/\pi} = k/2 \)）
     - `x/(1+math.exp(-k*(x+c*x**3)))`（源码形式）
   - 打印两者之差。
3. **需要观察的现象**：两者之差应在浮点舍入量级（约 1e-7 或更小）。
4. **预期结果**：确认源码实现的正确性不需要依赖 tanh 指令，只用 Exp/Div 即可——这就是该实现的性能动机。此实践纯 CPU 可完成，无需 NPU 环境。

#### 4.4.5 小练习与答案

**练习 1**：`Cast` 为什么入方向用 `CAST_MODE_NONE`、出方向用 `CAST_MODE_RINT`？

**答案**：入方向 float→半精度提升是精确扩宽（不需要舍入策略，MODE_NONE）；出方向 float→FP16/BF16 是收窄，必须定舍入规则，RINT 是「四舍五入到偶」，可控制精度损失并避免系统性偏差。

**练习 2**：add_example 用「currentNum 传递有效元素数」处理尾块，gelu 的 `GeluCustom` 用什么机制？

**答案**：`UpdateMask<T>(count)` 按当前分片实际元素数生成掩码寄存器，硬件只对掩码内的 lane 执行运算，越界 lane 被屏蔽。两者目标相同（尾块不越界、不污染数据），层次不同：一个控制指令长度，一个控制指令作用范围。

### 4.5 Host 侧 tiling：ElewiseBaseTiling 委托与两段注册

#### 4.5.1 概念说明

u4-l1 讲过 tiling 的产出三件套（TilingData、BlockDim、TilingKey）。gelu 的 Host 侧 tiling（`op_host/arch35/gelu_tiling_arch35.cpp`）把这些产出的**计算**也委托出去了：只保留「参数校验」自己写，切分算法交给公共的 `ElewiseBaseTiling`（同样来自 CANN 包 `op_common/atvoss/elewise/elewise_tiling.h`）。此外它展示了 add_example 没有的**两段注册**：`Tiling`（运行期切分）+ `TilingParse`（编译期把平台信息烧进 compile info）。

#### 4.5.2 核心流程

```text
Tiling4Gelu(tilingContext)                 # IMPL_OP_OPTILING 注册的入口
  └─ GeluTiling(baseOpTiling).RunTiling()
       ├─ CalcInputDtype / CalcOutputDtype / CheckShape   # 自写校验
       ├─ 按输出 dtype 选 dType（TPL_FP16/BF16/FP32）
       ├─ ElewiseBaseTiling.DoTiling<GeluDAG<T>::OpDag>(*tiling)
       │      # 公共框架按 DAG 的内存需求做核切分/UB 切分，
       │      # 写入 EleBaseTilingData16B（dim0/coreNum/ubFormer…）
       ├─ currentWorkspace[0] = 16M                       # 固定 workspace
       ├─ SetTilingKey(GET_TPL_TILING_KEY(1, dType))      # 二维 key 编码
       └─ SetBlockDim(elewiseBaseTiling.GetBlockDim())

TilingPrepareForGelu(parseContext)         # 编译期回调
  └─ 读平台 AIV 核数与 UB 大小 → 存入 GeluCompileInfo
```

#### 4.5.3 源码精读

[activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp:L85-L107](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp#L85-L107)：`RunTiling` 主体——三项校验后按输出 dtype 三分支，每支先定 `dType` 再调 `elewiseBaseTiling.DoTiling<GeluOp::GeluDAG<T>::OpDag>(*tiling)`。注意模板实参正是 4.4 节的 DAG 类型：**tiling 侧与 kernel 侧共用同一份 DAG 描述**，框架据此知道该算子需要多少 UB 缓冲、产生什么访存模式，切分参数才能算准。这是「DAG 作为 host/device 契约」的第二个用途（第一个是 4.3 节的调度器驱动）。

[activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp:L118-L126](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp#L118-L126)：交付三件套的落盘——固定 16MB workspace（[L22](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp#L22) 定义 `ASCEND_WORKSPACE = 16777216`），`GET_TPL_TILING_KEY(1, dType)` 把 schMode 固定为 1、按 dtype 编出二维 tiling key，`SetBlockDim` 用框架算出的核数。对照 u4-l1：add_example 手写 `blockFactor = ⌈totalIdx/coreNum⌉`，gelu 一行委托。

[activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp:L139-L151](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp#L139-L151)：`TilingParse` 回调把 `GetCoreNumAiv()` 与 UB 大小写进 `GeluCompileInfo`（结构见 [gelu_tiling_arch35.h:L19-L22](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35/gelu_tiling_arch35.h#L19-L22)），最后一行 `IMPL_OP_OPTILING(Gelu).Tiling(Tiling4Gelu).TilingParse<GeluCompileInfo>(TilingPrepareForGelu)` 完成两段注册。add_example 只有 `.Tiling(...)` 一段，每次 tiling 时现查平台信息；生产算子把平台信息在编译期固化，运行期少一次平台查询。注意这里取的是 **AIV 核数**（与 kernel 侧 `KERNEL_TYPE_AIV_ONLY` 呼应），而 add_example 取的是 AI Core 数。

佐证：kernel UT 中可见公共 tiling 结构的字段。 [activation/gelu/tests/ut/op_kernel/test_gelu_apt.cpp:L56-L58](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/ut/op_kernel/test_gelu_apt.cpp#L56-L58) 直接给 `EleBaseTilingData16B` 的 `dim0/coreNum/ubFormer` 赋值——这三个字段就是 `ElewiseBaseTiling` 产出的切分参数，语义与 add_example 手写的 `totalNum/coreNum 切分/ubFactor` 同族（`ubFormer` 即每轮处理的元素数，命名待确认其完整语义）。该测试还以 `#include "../../../op_kernel/gelu_apt.cpp"` 的方式把入口源文件直接编进测试（[L21](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/ut/op_kernel/test_gelu_apt.cpp#L21)），并显式实例化 `::gelu<0, TPL_FP32>`（[L59-L61](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/ut/op_kernel/test_gelu_apt.cpp#L59-L61)）、用 `ICPU_SET_TILING_KEY(1003)` 设置运行期 key——1003 应为 `GET_TPL_TILING_KEY(1, TPL_FP32)` 的编码值（推测为按声明顺序的组合编码，具体编码规则在 CANN 包 `template_argument.h` 中，待本地确认）。

#### 4.5.4 代码实践

1. **实践目标**：对比 add_example 与 gelu 的 tiling「自写比例」，体会生产算子的复用策略。
2. **操作步骤**：
   - 重读 [examples/add_example/op_host/add_example_tiling.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp)（u4-l1 已精读）：数一数手写了几步——取平台信息、算 blockFactor、算 ubFactor、写 tiling 字段、SetBlockDim、SetTilingKey。
   - 再读 `gelu_tiling_arch35.cpp` 的 `RunTiling`：勾出哪些步骤消失了、被哪一行替代。
3. **需要观察的现象**：gelu 保留的是「业务校验」（dtype 白名单、x/y 同 shape、输入输出同 dtype，见 [L31-L37](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp#L31-L37)、[L60-L65](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp#L60-L65)、[L75-L81](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp#L75-L81)），消失的是「切分数学」。
4. **预期结果**：得出结论——校验是算子个性（每个算子的合法输入不同），切分是逐元素算子共性（可框架化）。这也解释了 `EnsureNotScalar`（[L41-L47](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp#L41-L47)，即 u4-l3 讲过的标量归一化 `{1}`）为什么在这里以 static 内联函数出现：gelu 支持 0-8 维（README 参数表），标量输入必须先归一。

#### 4.5.5 小练习与答案

**练习 1**：`DoTiling` 的模板参数为什么必须传 `GeluDAG<T>::OpDag` 而不是像 add_example 那样只传数据类型？

**答案**：公共框架需要知道算子的完整内存行为（几个输入输出、各站点的 UB 需求）才能算切分；这些信息编码在 DAG 类型里。传 `GeluDAG<half>::OpDag` 与 `GeluDAG<float>::OpDag` 得到的切分结果可能不同（缓冲宽度不同），所以按 dtype 分支各传各的。

**练习 2**：`TilingParse` 段和 `Tiling` 段分别在什么时机执行、各产出什么？

**答案**：`TilingParse`（TilingPrepareForGelu）在编译期执行，从平台读 AIV 核数与 UB 大小，固化进 `GeluCompileInfo` 随二进制保存；`Tiling`（Tiling4Gelu）在每次算子调用时执行，做校验、委托切分、写 TilingData、编 tiling key、定 BlockDim。add_example 没有前者，每次 tiling 现查平台信息。

## 5. 综合实践

**任务：绘制 add_example 与 gelu 的端到端数据流对照图，并标注全部差异点。** 这是本讲规格中要求的代码实践任务，综合了 4.1–4.5 全部模块。

具体做法：

1. 画两条竖向泳道，左为 add_example、右为 gelu，都从「aclnn 调用进入」画到「kernel 写回 GM」。
2. 左泳道按 u5-l1 的五段标注：tiling（手写切分）→ 入口（一维模板分发）→ Init（SetGlobalBuffer + InitBuffer）→ Process 循环（CopyIn/Compute/CopyOut，currentNum 尾块）→ `AscendC::Add`。
3. 右泳道按本讲标注：tiling（校验 + `DoTiling<DAG>` 委托 + TilingParse 固化平台信息）→ 入口（二维模板分发 + AIV_ONLY 声明）→ `sch.Init/Process`（公共调度器）→ DAG 五站（CopyIn → Cast ↑ → GeluCustom → Cast ↓ RINT → CopyOut）→ MicroAPI 7 指令循环（UpdateMask 尾块）。
4. 在两条泳道之间画对应连线（如左「currentNum 尾块」↔ 右「UpdateMask 掩码」；左「ubFactor」↔ 右「ubFormer」；左「手写 blockFactor」↔ 右「GetBlockDim 托管」），并对每条连线标注「谁承担了这个职责」。
5. 最后在图下方回答一个问题：**如果仓库新增第十个逐元素算子（如 silu），按 gelu 模式作者需要写哪些文件、总共大约多少行？**（提示：入口三分支 + 一个 DAG 头 + 校验 tiling，参照 `gelu_dag.h` 把 `GeluCustom` 换成 silu 公式即可，量级在百行以内。）

验证方式：把对照图给一位没读过 gelu 源码的同事（或未来的自己），只看图能否复述 gelu 的执行链路；能，则图达标。图中每条结论都应能在本讲引用的源码行号处找到出处。

## 6. 本讲小结

- 生产算子 gelu 与教学样例的工程差异体现在三个维度：dtype 精度策略（半精度内部提升 float、RINT 回转）、多架构目录（kernel 与 tiling 双双下沉 `arch35/`）、构建开关（`SUPPORT_COMPUTE_UNIT`/`SUPPORT_TILING_DIR`/`DISABLE_IN_OPP`，本仓库这份源码只编给 ascend950/mc62）。
- 入口函数骨架与 add_example 高度同构（注册 tiling → 读 tiling → `if constexpr` 分发 → Init/Process），但主循环与搬运全部委托给 CANN 包公共调度器 `ElementwiseSch16B`，算子只提供计算描述。
- 计算本体用「DAG 类型链 + MicroAPI 寄存器循环」两层表达：DAG 声明 CopyIn→Cast↑→GeluCustom→Cast↓→CopyOut 的数据流，`GeluCustom` 用 7 条矢量指令实现 sigmoid 形式的 gelu（与 tanh 公式数学恒等）。
- DAG 是 host/device 双侧共用的契约：kernel 侧调度器按它驱动执行，tiling 侧 `DoTiling<DAG>` 按它算切分。
- tiling 采用「个性校验自写 + 共性切分委托」的分工，并用 `TilingParse` 在编译期把 AIV 核数与 UB 大小固化进 compile info——这是 add_example 没有的两段注册。
- 尾块处理从 add_example 的「currentNum 控制指令长度」升级为「UpdateMask 控制寄存器掩码」，目标相同、层次更低。

## 7. 下一步学习建议

本讲之后，你已经具备独立阅读仓库内任意矢量算子的能力。建议路线：

1. **横向练手**：用本讲的「入口 → tiling → DAG/Compute → 搬运」框架去读 `activation/` 下另一个算子（如 `silu` 或 `fast_gelu`，可用 `docs/zh/op_list.md` 查找），检验框架的通用性——若发现某算子没有走 atvoss 框架而是全手写，对比两者即是最好的复习。
2. **顺承大纲**：下一讲 u5-l4 将介绍 `examples/fast_kernel_launch_example` 与 Cube 类算子的 `ops-tensor` 分层，把视野从「单个矢量 kernel 的写法」扩展到「kernel 下发路径的端到端开销」。
3. **回补测试**：本讲只顺带看了 kernel UT 的一个用例，u7-l1/u7-l2 将系统讲解 UT/ST 体系；到时可回到 `activation/gelu/tests/` 对照学习它的 ut（op_host/op_kernel/op_api 三类）与 st（atk_aclnnGelu）全套组织。
