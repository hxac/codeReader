# Dump 配置与调试数据生成

## 1. 本讲目标

本讲是「show_kernel_debug_data 解析工具」单元（u7）的第一讲，解决一个前置问题：**算子侧的调试数据从哪里来、怎么配置、怎么生成**。

读完本讲你应该能够：

- 用 `acl.json` 配置 Dump 功能，并知道 `dump_kernel_data` 各取值的含义；
- 说清 Dump 文件存储路径的三种来源及其优先级（`ASCEND_DUMP_PATH` > `ASCEND_WORK_PATH` > 配置文件 `dump_path`）；
- 在 Ascend C 算子源码里使用 `DumpTensor`、`printf`/`PRINTF`、`PrintTimeStamp`、`ascend_assert` 四类调试 API 主动产出调试数据；
- 解释生成的 `.bin` 文件名各字段（核类型/核号/index/loop）与代码的对应关系。

> 重要边界：本讲只讲「生成」与「配置」，不讲「解析」。`.bin` 文件如何被翻译回可读文本，留给 u7-l2（TLV 格式）和 u7-l3（printf/tensor/timestamp 解析实现）。理解这条边界，是理解整个 show_kernel_debug_data 工具的前提。

## 2. 前置知识

本讲默认你已经掌握 u1-l4（一键编译与运行第一个样例）的内容，知道：

- Ascend C 算子源码以 `.asc` 为扩展名，经 ASC 语言编译后既可在 CPU 域也可在 NPU 域运行；
- 一个算子由宿主侧（`main`/`kernel_add`）与核函数侧（`__global__` + Kernel 类）两部分组成；
- `<<<>>>` 是核函数启动语法。

本讲还会用到几个新术语：

- **Dump（转储）**：把算子运行过程中产生的调试信息（Tensor 片段、格式化日志、时间戳）落盘成二进制文件的过程。
- **kernel 侧**：运行在 NPU 核（AICore）上的代码，即 Kernel 类与核函数内部。相对的是 **host 侧**（运行在 CPU 上的 `main`/`kernel_add`）。
- **bin 文件**：Dump 落盘的二进制产物，扩展名 `.bin`，内部是 TLV（Type-Length-Value）结构，u7-l2 会详述。
- **AIV / AIC**：AI Vector（向量核）/ AI Cube（立方核），融合编译下一个算子可能同时占用两类核，文件名里会出现 `aiv` / `aic` 区分。

一个关键认知（承接 u6-l1、u6-l2）：**show_kernel_debug_data 工具本身不参与生成 bin 文件**。生成 bin 文件的是 CANN ACL 运行时（`aclInit` 读 `acl.json` 后接管 Dump）加上算子侧的调试内建函数（由 `kernel_operator.h` 提供）。asc-tools 提供的 show_kernel_debug_data 只做「离线解析」。所以本讲引用的源码以**样例工程**和**文档**为主——它们才是「生成端」的真实代表。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [docs/04_show_kernel_debug_data.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/04_show_kernel_debug_data.md) | 工具官方说明，定义了 `dump_kernel_data` 的取值、`dump_path` 的要求，以及环境变量优先级。是配置语义的权威来源。 |
| [examples/01_show_kernel_debug_data/acl.json](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/01_show_kernel_debug_data/acl.json) | 样例的 Dump 配置文件，是最小可用的配置范例。 |
| [examples/01_show_kernel_debug_data/add.asc](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/01_show_kernel_debug_data/add.asc) | 样例算子源码，在 Kernel 类的 `Compute` 里调用 `DumpTensor`/`printf`/`PrintTimeStamp`；在 `kernel_add` 里用 `aclInit("../acl.json")` 装载配置。 |
| [examples/01_show_kernel_debug_data/README.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/01_show_kernel_debug_data/README.md) | 样例说明，给出运行步骤、产物目录结构与文件名字段含义。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 Dump 配置文件**、**4.2 路径与环境变量优先级**、**4.3 kernel 侧调试 API**。三者关系是：配置文件（或环境变量）决定「要不要 Dump、Dump 什么、Dump 到哪」，kernel 侧 API 决定「Dump 的具体内容」，二者共同决定了最终落盘的 bin 文件。

### 4.1 Dump 配置文件

#### 4.1.1 概念说明

Dump 功能由 CANN ACL 运行时提供，通过 `aclInit` 接口在初始化时读取一个 JSON 配置文件（习惯上命名为 `acl.json`）来启用。这个文件和你在 u1-l3 见过的 CANN 环境变量是两套东西：环境变量管「CANN 装在哪」，`acl.json` 管「这次算子运行时开哪些运行时特性」。

对 show_kernel_debug_data 而言，我们只关心 `acl.json` 里的 `dump` 对象，它有两个必备字段：

- `dump_kernel_data`：导出哪些类型的调试数据；
- `dump_path`：调试数据落到哪个目录。

#### 4.1.2 核心流程

从「写配置」到「出 bin 文件」的链路如下：

1. 宿主侧代码调用 `aclInit("../acl.json")`，ACL 运行时读取并解析该 JSON；
2. 运行时发现 `dump` 对象存在且 `dump_kernel_data` 非空，于是开启 Dump；
3. 算子在 NPU 上运行，kernel 侧每调用一次 `DumpTensor`/`printf`/`PrintTimeStamp`，运行时就按 `dump_kernel_data` 的类型过滤，把符合条件的数据写入缓冲；
4. 算子结束、流同步（`aclrtSynchronizeStream`）后，运行时把缓冲里的数据按核号/类型落盘成 `.bin` 文件到 `dump_path`（或更高优先级的路径）；
5. 之后才轮到 asc-tools 的 show_kernel_debug_data 离线解析这些 bin。

> 注意第 1 步与第 5 步的分界：第 1～4 步属于 CANN 运行时（生成端），第 5 步属于 asc-tools（解析端）。本讲聚焦 1～4 步。

#### 4.1.3 源码精读

样例的配置文件 `examples/01_show_kernel_debug_data/acl.json` 只有 6 行，是 Dump 配置的最小范例：

[examples/01_show_kernel_debug_data/acl.json:1-6](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/01_show_kernel_debug_data/acl.json#L1-L6) —— 样例采用最省事的写法：`dump_kernel_data` 设为 `"all"`（导出全部类型），`dump_path` 设为 `"../output"`（相对路径，落在样例目录上一级的 `output` 下）。

各取值含义见官方文档（这是配置语义的权威定义）：

[docs/04_show_kernel_debug_data.md:74-79](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/04_show_kernel_debug_data.md#L74-L79) —— `dump_kernel_data` 支持的类型清单，可用英文逗号组合：

| 取值 | 导出内容 | 对应 kernel 侧 API |
|------|----------|--------------------|
| `all` | 以下全部类型 | —— |
| `printf` | 格式化日志 | `AscendC::printf` / `AscendC::PRINTF` |
| `tensor` | Tensor 片段 | `AscendC::DumpTensor` |
| `assert` | 断言失败信息 | `ascend_assert` |
| `timestamp` | 时间戳 | `AscendC::PrintTimeStamp` |

[docs/04_show_kernel_debug_data.md:80](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/04_show_kernel_debug_data.md#L80) —— 一条硬性约束：**开启 Dump 时 `dump_path` 必须配置**，支持绝对路径或相对路径。如果不写，运行时无法落盘。

而装载这个配置的入口在宿主侧 `kernel_add` 函数里：

[examples/01_show_kernel_debug_data/add.asc:148](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/01_show_kernel_debug_data/add.asc#L148) —— `aclInit("../acl.json")` 在分配显存、启动核函数之前执行，确保整条算子调用链都处于 Dump 已开启的运行时环境中。这也是为什么配置装载必须「尽可能靠前」。

#### 4.1.4 代码实践

**实践目标**：把样例从「导出全部」改为「只导出 tensor 和 printf」，观察产物差异。

**操作步骤**：

1. 打开 `examples/01_show_kernel_debug_data/acl.json`；
2. 把 `"dump_kernel_data":"all"` 改成 `"dump_kernel_data":"tensor,printf"`；
3. 清空旧的 `output` 目录后重新编译运行（步骤见 4.3.4）。

**需要观察的现象**：相比 `all` 模式，新产物里应当**不再出现** `time_stamp_core_*.csv`（因为 `timestamp` 未选），但仍能看到 `DumpTensor` 与 `printf` 对应的 bin。

**预期结果**：`output` 目录下存在 tensor/printf 的 bin，缺少时间戳 csv。**待本地验证**（本环境无真实 NPU，无法实跑）。

#### 4.1.5 小练习与答案

**练习 1**：如果 `acl.json` 里写了 `dump_kernel_data` 却忘了写 `dump_path`，会发生什么？

> **参考答案**：根据 [docs/04 第 80 行](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/04_show_kernel_debug_data.md#L80) 的约束，开启 Dump 时 `dump_path` 必须配置。缺失会导致运行时无法确定落盘位置，Dump 功能不生效或报错，最终 `output` 目录里不会生成 bin。

**练习 2**：`dump_kernel_data:"printf,tensor"` 和 `"tensor,printf"` 有区别吗？

> **参考答案**：没有。该字段是一个**类型集合**，类型之间用逗号分隔、顺序无关，运行时只判断某个类型是否在集合里，用来决定是否落盘对应数据。

### 4.2 Dump 路径与环境变量优先级

#### 4.2.1 概念说明

`dump_path` 只是配置 Dump 路径的**三种方式之一**。CANN 运行时还允许用两个环境变量来指定 Dump（以及更广义的工作区）路径：

- `ASCEND_DUMP_PATH`：专门为 Dump 数据指定的路径；
- `ASCEND_WORK_PATH`：算子运行时的通用工作路径，Dump 数据也会落到它的子目录里。

当三者同时存在时，必须有一套明确的优先级，否则不同来源的配置会互相冲突。

#### 4.2.2 核心流程

路径决策可以表达为一个简单的选择函数。设最终落盘路径为 \(P\)，三个候选来源为 \(P_{dump}\)（环境变量 `ASCEND_DUMP_PATH`）、\(P_{work}\)（环境变量 `ASCEND_WORK_PATH`）、\(P_{file}\)（配置文件 `dump_path`），则：

\[
P = \begin{cases}
P_{dump} & \text{若 } ASCEND\_DUMP\_PATH \text{ 已设置} \\
P_{work} & \text{否则若 } ASCEND\_WORK\_PATH \text{ 已设置} \\
P_{file} & \text{否则使用配置文件中的 } dump\_path
\end{cases}
\]

也就是 **`ASCEND_DUMP_PATH` > `ASCEND_WORK_PATH` > 配置文件 `dump_path`**。这条规则的工程意义是：环境变量可以**临时覆盖**配置文件，方便在不改 `acl.json`（甚至不重新编译）的情况下，把 Dump 数据重定向到一个新目录——例如把多次运行的结果分开存放。

#### 4.2.3 源码精读

优先级规则在官方文档里只有一句话，但它是本模块的核心：

[docs/04_show_kernel_debug_data.md:82](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/04_show_kernel_debug_data.md#L82) —— 明确给出三级优先级：`ASCEND_DUMP_PATH > ASCEND_WORK_PATH > 配置文件中的 dump_path`。

样例 `add.asc` 的 `aclInit("../acl.json")` 传递的是配置文件方式；该调用本身并不感知环境变量。环境变量的读取与三级裁决发生在 ACL 运行时内部（属于 CANN 闭源部分，不在 asc-tools 仓库里），所以我们只能以文档为权威依据。这也再次印证 4.1.2 的边界：**asc-tools 不实现生成端逻辑**。

> 一个易混点：`ASCEND_WORK_PATH` 是「工作区」而非「专门 Dump 区」，它还承载算子编译中间产物等；只有当 `ASCEND_DUMP_PATH` 没设时，Dump 数据才会退而求其次落进工作区。专门做 Dump 时建议直接用 `ASCEND_DUMP_PATH`，语义最清晰。

#### 4.2.4 代码实践

**实践目标**：不改 `acl.json`，仅用环境变量把 Dump 重定向到新目录。

**操作步骤**：

1. 保持样例 `acl.json` 中 `dump_path` 为 `"../output"` 不变；
2. 运行 demo 前 `export ASCEND_DUMP_PATH=/tmp/dump_override`；
3. 运行 demo。

**需要观察的现象**：bin 文件落在 `/tmp/dump_override` 下，而**不是** `../output`。

**预期结果**：因为 `ASCEND_DUMP_PATH` 优先级最高，配置文件的 `dump_path` 被覆盖。**待本地验证**（本环境无真实 NPU）。

#### 4.2.5 小练习与答案

**练习 1**：同时 `export ASCEND_WORK_PATH=/a` 和 `export ASCEND_DUMP_PATH=/b`，Dump 数据落到哪？

> **参考答案**：落到 `/b`。`ASCEND_DUMP_PATH` 优先级高于 `ASCEND_WORK_PATH`。

**练习 2**：为什么设计上要允许环境变量覆盖配置文件？

> **参考答案**：配置文件（`acl.json`）随源码一起编译、改动成本高且影响所有人；环境变量是**进程级临时设置**，能让开发者在不碰 `acl.json`、不重新编译的前提下，把每次运行的 Dump 结果导向不同目录，便于对比和归档。

### 4.3 kernel 侧调试 API

#### 4.3.1 概念说明

配置只决定「要不要、在哪」，真正产生调试内容的是算子源码里的四类调试内建函数。它们都声明在 `kernel_operator.h`（CANN 提供，算子通过 `#include "kernel_operator.h"` 引入，见样例第 22 行），属于 Ascend C 的调测原语：

- **`AscendC::DumpTensor`**：把一段 LocalTensor 的内容 dump 出来，相当于「在核内打印一段内存」；
- **`AscendC::printf` / `AscendC::PRINTF`**：类 C 的格式化打印，前者是函数、后者是宏，二者行为一致，只是写法不同；
- **`AscendC::PrintTimeStamp`**：打一个带 ID 的时间戳，用于测同一段代码的耗时；
- **`ascend_assert`**：断言，失败时把信息 dump 出来（对应 `dump_kernel_data` 的 `assert` 类型）。

#### 4.3.2 核心流程

在样例 `KernelAdd::Compute` 里，这几类 API 的调用顺序是：

1. `Add` 计算完成后，`zLocal` 已有结果；
2. 三次 `DumpTensor` 分别 dump 输入 x、输入 y、输出 z 的片段（用第二参数 `desc` 0/1/2 区分）；
3. 仅在第一轮循环（`progress == 0`）时，打一个时间戳、两条 int 格式日志、两条 float 格式日志（用 `progress==0` 避免每轮都刷屏）。

这些调用在 NPU 上执行时不会直接写文件，而是把「数据 + 元信息（是 tensor 还是 printf？desc 是几？第几轮？哪个核？）」交给运行时缓冲，最终由运行时统一落盘。落盘时，元信息会编码进 bin 文件名和文件内的 TLV 结构——这正是 u7-l2 要拆解的内容。

#### 4.3.3 源码精读

[examples/01_show_kernel_debug_data/add.asc:89-104](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/01_show_kernel_debug_data/add.asc#L89-L104) —— 这是本讲最关键的一段，集中了全部三类调试 API：

```cpp
// DumpTensor show input/output info
AscendC::DumpTensor(xLocal[64], 0, 16);   // desc=0 -> xLocal
AscendC::DumpTensor(yLocal[64], 1, 16);   // desc=1 -> yLocal
AscendC::DumpTensor(zLocal[64], 2, 16);   // desc=2 -> zLocal
if (progress == 0) {
    AscendC::PrintTimeStamp(65577);       // 时间戳，ID=65577
    AscendC::printf("fmt string int: %d\n", 0x123);
    AscendC::PRINTF("fmt string int: %d\n", 0x123);
    float a = 3.14;
    AscendC::printf("fmt string float: %f\n", a);
    AscendC::PRINTF("fmt string float: %f\n", a);
}
```

逐点说明：

- `DumpTensor(xLocal[64], 0, 16)`：从 `xLocal` 的第 64 号元素起，dump 16 个元素；第二参数 `0` 是 **desc（描述符）**，纯粹由开发者自定义，用来在解析时区分「这一段是谁的」。样例里 0/1/2 对应 x/y/z。这个 desc 会直接出现在解析后的文件名 `index_<desc>` 中。
- `PrintTimeStamp(65577)`：参数是用户自定义的时间戳 ID，用于在 csv 里识别这个打点。
- `printf` 与 `PRINTF`：同一个能力的两种写法，格式串与 C 语言 `printf` 一致（`%d`、`%f`）。
- `if (progress == 0)`：把昂贵的 printf 限制在首轮，是一个实用的「省刷屏」技巧。

落盘后的文件名结构，样例 README 给出了明确定义：

[examples/01_show_kernel_debug_data/README.md:131-140](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/01_show_kernel_debug_data/README.md#L131-L140) —— 解析后的 tensor 文件名为 `asc_kernel_data_aiv_0_index_0_loop_0.bin`，各字段含义：

| 字段 | 含义 | 在代码中的来源 |
|------|------|----------------|
| `aiv` | AI Vector 核（向量核）；融合产物可能为 `aic`（立方核） | 算子编译时核类型 |
| `0`（核号） | 第几个核 | `GetBlockIdx()`，样例共 8 核（0～7） |
| `index_0` | DumpTensor 的 desc 值 | `DumpTensor` 第二参数 0/1/2 |
| `loop_0` | 第几轮循环 | `Process()` 里 `i` 的取值 |

同一目录下还有：

- `asc_kernel_data_aiv_0_index_0_loop_0.txt`：同名 `.txt` 是 bin 解析后的可读文本；
- `time_stamp_core_0.csv`：该核（core 0）的全部时间戳，对应 `PrintTimeStamp`。

样例 README 还说明了顶层 `0`~`7` 子目录与核号的对应：

[examples/01_show_kernel_debug_data/README.md:140](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/01_show_kernel_debug_data/README.md#L140) —— `dump_data` 下的 `0`、`1`、…、`7` 分别对应 8 个核；`index0/1/2` 分别对应 `desc=0/1/2` 即 xLocal/yLocal/zLocal。

而未解析前的原始产物目录则扁平一些：

[examples/01_show_kernel_debug_data/README.md:84-93](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/01_show_kernel_debug_data/README.md#L84-L93) —— 运行后 `output` 目录下是一组带时间戳父目录的 `asc_kernel_data_xxx.bin`，尚未按核号分子目录（分子目录是 show_kernel_debug_data 解析时做的事）。

> 串起来理解：**代码里的 `desc` → 文件名里的 `index`，代码里的核号 `GetBlockIdx()` → 文件名里的核号与顶层子目录，代码里的循环变量 `i` → 文件名里的 `loop`**。掌握这个映射，就能从一堆 bin 文件反推它们分别来自源码的哪一次调用。

#### 4.3.4 代码实践

**实践目标**：编译运行样例，亲眼看到 Dump 产物，并把 bin 文件名对应回源码。

**操作步骤**（来自样例 README）：

1. 配好 CANN 环境变量：`source ${install_path}/cann/set_env.sh`；
2. 检查工具：`show_kernel_debug_data -h` 能正常显示帮助；
3. 在 `examples/01_show_kernel_debug_data` 目录下：
   ```bash
   mkdir -p build output && cd build
   cmake -DCMAKE_ASC_ARCHITECTURES=dav-2201 ..
   make -j
   ./demo
   ```
4. 运行成功标志：终端出现 `[Success] Case accuracy is verification passed.`，且 `../output` 下生成 bin 文件；
5. 解析（预览 u7-l2/u7-l3）：
   ```bash
   mkdir -p dump_info_output
   show_kernel_debug_data ../output dump_info_output
   ```

**需要观察的现象**：

- 终端打印 `fmt string int: 291`、`fmt string float: 3.140000` 等——注意 `0x123` 十进制正是 291，验证了 printf 格式串与参数被正确还原；
- `dump_info_output/.../dump_data/` 下出现 `0`~`7` 八个子目录；
- 每个子目录里有 `asc_kernel_data_aiv_<核号>_index_0/1/2_loop_*.bin` 与 `time_stamp_core_<核号>.csv`。

**预期结果**：bin 文件名的核号、index、loop 三个字段分别与 `GetBlockIdx()`、`DumpTensor` 的 desc、`Process()` 的循环变量一一对应。**待本地验证**（本环境无真实 NPU，无法实跑；可先做下面的源码阅读型实践）。

**源码阅读型实践（无需 NPU）**：在 `add.asc` 的 `Compute` 里把三次 `DumpTensor` 的 desc 改成 `10/11/12`，**不运行**，仅根据本讲的文件名映射规则，预测解析后会出现哪些文件名，然后对照 README 的产物结构自检。

#### 4.3.5 小练习与答案

**练习 1**：为什么样例把 `printf`/`PrintTimeStamp` 放在 `if (progress == 0)` 里，而 `DumpTensor` 不放？

> **参考答案**：`Process()` 共循环 `tileNum*BUFFER_NUM = 8*2 = 16` 次。printf 和时间戳的输出对每轮基本一样，全量打印会刷屏且冗余；而 `DumpTensor` dump 的是每轮的真实数据（不同轮数据所在 buffer 可能不同），有保留每轮的必要，所以放在循环里无条件执行。

**练习 2**：若把 `DumpTensor(xLocal[64], 0, 16)` 的第三参数 `16` 改成 `64`，文件名会怎么变？解析出的数据量会怎么变？

> **参考答案**：文件名不变（文件名只含核号/index/loop，与 dump 长度无关）；但每个 bin（及其 `.txt`）里还原出的元素数从 16 个变成 64 个，因为第三参数控制的是 dump 的元素个数。

**练习 3**：`DumpTensor` 的第二参数 desc 有什么用？为什么样例特意用 0/1/2？

> **参考答案**：desc 是开发者自定义的「标签」，唯一作用是在解析时区分不同的 dump 来源（文件名里的 `index_<desc>`）。样例用 0/1/2 分别标记 xLocal/yLocal/zLocal，这样解析后一眼就能把 `index_0/1/2` 对应到输入和输出，方便定位。

## 5. 综合实践

把本讲三个模块串起来，完成一次「配置 → 改造算子 → 预测产物」的小任务：

1. **配置层**：把 `acl.json` 的 `dump_kernel_data` 设为 `"tensor,timestamp"`（去掉 printf），保留 `dump_path`；
2. **API 层**：在 `add.asc` 的 `CopyIn` 末尾新增一行 `AscendC::DumpTensor(xLocal[0], 5, 8);`（dump 输入 x 的前 8 个元素，desc=5）；
3. **路径层**：不改 `acl.json`，改用 `export ASCEND_DUMP_PATH=./mydump` 重定向；
4. **预测**：在**不运行**的前提下，写出解析后会出现哪些文件名（提示：注意 desc=5、新增点位在 `CopyIn` 每轮都会执行、printf 被关闭所以没有 printf 产物、timestamp 仍开所以有 csv）。

完成后，若有真实 NPU 环境，按 4.3.4 的步骤实跑一遍对照你的预测。

> 这个任务同时覆盖了三个最小模块：配置文件取值（4.1）、环境变量覆盖（4.2）、kernel 侧 API 与文件名映射（4.3）。

## 6. 本讲小结

- Dump 配置写在 `acl.json` 的 `dump` 对象里，两个必备字段是 `dump_kernel_data`（导出类型）和 `dump_path`（落盘目录），后者在开启 Dump 时必须配置。
- `dump_kernel_data` 支持 `all/printf/tensor/assert/timestamp` 五种取值，可逗号组合，是一个无序集合。
- Dump 路径有三个来源，优先级为 **`ASCEND_DUMP_PATH` > `ASCEND_WORK_PATH` > 配置文件 `dump_path`**，环境变量可临时覆盖配置文件。
- kernel 侧四类调试 API（`DumpTensor`/`printf`/`PrintTimeStamp`/`ascend_assert`）才是调试内容的真正生产者，它们由 `kernel_operator.h` 提供，声明不在 asc-tools 仓库。
- bin 文件名三字段 `核号 / index / loop` 分别对应 `GetBlockIdx()`、`DumpTensor` 的 desc、`Process()` 的循环变量——这是连接「源码」与「产物」的钥匙。
- **关键边界**：生成 bin 文件的是 CANN ACL 运行时（`aclInit` 装载配置）+ 算子侧内建函数；asc-tools 的 show_kernel_debug_data 只做离线解析。本讲讲生成，u7-l2/u7-l3 讲解析。

## 7. 下一步学习建议

本讲只把 bin 文件「造」出来，还没解释它的内部结构。建议下一步学习：

- **u7-l2 dump bin 文件 TLV 格式解析**：拆开 `.bin` 的二进制布局（TLV、DumpMessageHeader、BlockInfo、magic 0xAE86/0x5AA5BCCD），理解 bin 文件是如何被「分流」到 FIFO 与 workspace 两类解析器的；
- **u7-l3 printf / tensor / timestamp 解析实现**：深入 `PrintStruct._read_arg`、`DumpTensor` 数据还原、时间戳解析，看懂格式串参数如何从二进制里读回。

读完这两讲，你就能把本讲生成的 bin 文件，从字节一直还原成终端里那行 `fmt string float: 3.140000`。
