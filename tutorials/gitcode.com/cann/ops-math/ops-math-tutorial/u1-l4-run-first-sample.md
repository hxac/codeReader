# 跑通第一个算子：AddExample 快速上手

## 1. 本讲目标

学完本讲，你应该能够：

1. 按照 `docs/QUICKSTART.md` 的流程，独立完成「编译 → 安装算子包 → 配置环境变量 → 运行样例」的端到端闭环。
2. 读懂一个 aclnn 层调用样例（`test_aclnn_add_example.cpp` / `test_aclnn_add.cpp`）的固定骨架：初始化 → 构造输入 → 两段式调用 → 拷回结果 → 释放资源。
3. 学会通过算子 README（以 `math/add/README.md` 和 `examples/add_example/README.md` 为例）快速确认一个算子支持什么、怎么调用。

本讲是单元一的收官：前三讲解决了「项目是什么、目录怎么组织、怎么编译」，本讲终于让算子真正在 NPU 上跑起来。

## 2. 前置知识

### 2.1 Host 侧与 Device 侧

- **Host**：指 CPU 侧。样例程序、CANN 的调度逻辑都运行在这里。
- **Device**：指 NPU 卡。算子的 kernel（真正的计算）运行在这里。
- 两侧内存不互通，需要用 `aclrtMemcpy` 显式搬运（类比 CPU 与 GPU 的关系）。

### 2.2 ACL 运行时的几个核心对象

运行一个算子样例前，先认识几个反复出现的「固定写法」：

| 对象/调用 | 作用 |
|---|---|
| `aclInit` / `aclFinalize` | ACL 运行时初始化与去初始化，进程生命周期内各一次 |
| `aclrtSetDevice` / `aclrtResetDevice` | 指定/释放使用的 NPU 设备（`deviceId`） |
| `aclrtStream` | 任务流，算子下发到 stream 上异步执行 |
| `aclTensor` | Device 侧张量的描述符（shape、dtype、地址等），由 `aclCreateTensor` 创建 |
| `aclrtSynchronizeStream` | 阻塞等待 stream 上任务全部完成 |

### 2.3 两段式 aclnn 接口

每个 aclnn 算子接口都拆成两段：

1. **`aclnnXxxGetWorkspaceSize`**：第一段，做约束检查、形状推导等准备工作，返回执行时所需的 workspace（临时工作内存）大小和一个 `aclOpExecutor` 执行器。
2. **`aclnnXxx`**：第二段，真正把算子任务下发到 stream 上执行。

这套设计的好处是：用户可以先问「需要多大 workspace」，自己决定内存分配策略，再发起执行。第 4.2 节会在源码中看到具体调用。

### 2.4 run 包与 vendors 目录

上一讲我们编译出了 `build_out/cann-ops-math-*linux*.run` 这样的自解压安装包。安装后，自定义算子会被放到：

```text
${ASCEND_HOME_PATH}/opp/vendors/
```

要让运行时找到新装的算子库，还需把它挂到 `LD_LIBRARY_PATH`。本讲第 4.1 节会给出完整命令。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [docs/QUICKSTART.md](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/QUICKSTART.md) | 官方快速入门文档，以 AddExample 为主线的五段式教程（编译运行 / 算子开发 / 算子调试 / 算子验证） |
| [examples/add_example/README.md](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/examples/add_example/README.md) | AddExample 教学算子的规格说明：产品支持、计算公式、参数、约束 |
| [examples/add_example/examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/examples/add_example/examples/test_aclnn_add_example.cpp) | AddExample 的 aclnn 调用样例（QUICKSTART 实际运行的程序） |
| [math/add/examples/test_aclnn_add.cpp](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/examples/test_aclnn_add.cpp) | 正式版 Add 算子的 aclnn 调用样例，骨架与上面一致，用于对照 |
| [math/add/README.md](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/README.md) | 正式版 Add 算子规格说明，支持类型比 AddExample 丰富得多 |

注意区分两个「加法算子」：

- **`math/add`**：商用正式算子，功能完整（支持十几种数据类型、广播等）。
- **`examples/add_example`**：为教学裁剪的简化版（仅 FLOAT/INT32、仅 4 维），是 QUICKSTART 的实践对象。对照阅读二者正是本讲的主线之一。

## 4. 核心概念与源码讲解

### 4.1 QUICKSTART 文档：五段式入门主线

#### 4.1.1 概念说明

`docs/QUICKSTART.md` 是官方为新手设计的「一条龙」教程，它把算子开发的完整闭环压缩成五个阶段：

1. **前提条件**：环境准备与源码下载（上一讲已完成）。
2. **编译运行**：编译算子包、安装、运行样例——本讲的重点。
3. **算子开发**：修改 kernel，把 Add 改成 Mul，体验开发闭环。
4. **算子调试**：kernel 内 printf 打印、msprof 性能采集。
5. **算子验证**：修改样例输入数据，验证算子功能正确性。

#### 4.1.2 核心流程

「编译运行」阶段的完整流程可以画成：

```text
source CANN 环境变量
        │
bash build.sh --pkg --soc=<soc_version> --ops=add_example -j16
        │  （单算子编译，产物为 run 包，位于 build_out/）
        ▼
./build_out/cann-ops-math-*linux*.run
        │  （安装：算子落到 ${ASCEND_HOME_PATH}/opp/vendors/）
        ▼
export LD_LIBRARY_PATH=${ASCEND_HOME_PATH}/opp/vendors/custom_math/op_api/lib:...
        │  （让运行时能找到自定义算子的动态库）
        ▼
bash build.sh --run_example add_example eager cust --vendor_name=custom
        │  （编译并运行样例可执行文件，验证算子功能）
        ▼
屏幕打印 AddExample 的加法结果
```

#### 4.1.3 源码精读

QUICKSTART 规定单算子编译的通用命令格式为 `bash build.sh --pkg --soc=<芯片版本> --ops=<算子名>`，并给出 AddExample 的具体命令：

- [docs/QUICKSTART.md:44-58](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/QUICKSTART.md#L44-L58)：说明单算子编译适合快速入门，`--soc` 按产品取值（Atlas A2 → `ascend910b`，Atlas A3 → `ascend910_93`，950 系列 → `ascend950`），编译成功标志是生成 `cann-ops-math-custom_linux-${arch}.run`。
- [docs/QUICKSTART.md:74-88](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/QUICKSTART.md#L74-L88)：安装 run 包后算子位于 `${ASCEND_HOME_PATH}/opp/vendors`，随后 `export LD_LIBRARY_PATH` 把 `custom_math/op_api/lib` 加入动态库搜索路径——这一步漏掉的话，样例运行时会报找不到符号。
- [docs/QUICKSTART.md:90-106](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/QUICKSTART.md#L90-L106)：运行命令 `bash build.sh --run_example add_example eager cust --vendor_name=custom`，预期打印前 10 组「输入 1 + 输入 2 = 结果」。文档特别说明：第一个输入是从 1.0 递增的序列，第二个输入是种子 42 生成的随机数，校验方法是「每组结果应等于两个输入之和」。

#### 4.1.4 代码实践

**实践目标**：走通「编译 → 安装 → 配置 → 运行」四步，看到 AddExample 的加法输出。

**操作步骤**：

1. 确认已 source CANN 环境（默认路径：`source /usr/local/Ascend/cann/set_env.sh`）。
2. 在仓库根目录执行：

   ```bash
   bash build.sh --pkg --soc=${soc_version} --ops=add_example -j16
   ```

   `${soc_version}` 按你的实际硬件填写。

3. 安装算子包：

   ```bash
   ./build_out/cann-ops-math-*linux*.run
   ```

4. 配置环境变量：

   ```bash
   export LD_LIBRARY_PATH=${ASCEND_HOME_PATH}/opp/vendors/custom_math/op_api/lib:${LD_LIBRARY_PATH}
   ```

5. 运行样例：

   ```bash
   bash build.sh --run_example add_example eager cust --vendor_name=custom
   ```

**需要观察的现象**：终端打印 `Print the first 10 groups of data:` 及若干行 `add_example first input[i] is: ..., second input[i] is: ..., result[i] is: ...`。

**预期结果**：每一行的 `result[i]` 恰好等于 `first input[i] + second input[i]`。任取一行手工验算即可确认。

**说明**：本讲编写环境无 NPU 硬件，以上命令的运行输出为「待本地验证」；预期输出以 QUICKSTART 文档描述为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么运行样例前必须 `export LD_LIBRARY_PATH=.../opp/vendors/custom_math/op_api/lib:...`？

**答案**：AddExample 是自定义算子，安装后其 aclnn 动态库不在系统默认搜索路径中。样例可执行文件链接了 `aclnnAddExample` 等符号，若不把该目录加入 `LD_LIBRARY_PATH`，动态链接器找不到库，程序启动即报错。

**练习 2**：`--run_example add_example eager cust --vendor_name=custom` 中 `cust` 和 `--vendor_name=custom` 分别表达什么？

**答案**：这是「包模式」与「厂商名」参数。`cust` 表示使用自定义算子包（区别于 CANN 商用包），`--vendor_name=custom` 指明样例编译链接时去 vendors 下哪个厂商目录找算子库，与安装路径 `opp/vendors/custom_math` 相对应。

### 4.2 aclnn 调用样例：一个固定的七步骨架

#### 4.2.1 概念说明

QUICKSTART 运行的样例程序是 `examples/add_example/examples/test_aclnn_add_example.cpp`。所有 aclnn 调用样例（包括正式算子的 `math/add/examples/test_aclnn_add.cpp`）都遵循同一个骨架，学会这一个就等于学会了读所有算子样例：

```text
① Init：aclInit → aclrtSetDevice → aclrtCreateStream
② 构造输入/输出：host 数据 → aclrtMalloc → aclrtMemcpy(H2D) → aclCreateTensor
③ 第一段接口：aclnnXxxGetWorkspaceSize(...) → 得到 workspaceSize + executor
④ 按 workspaceSize 申请 device 内存
⑤ 第二段接口：aclnnXxx(workspaceAddr, workspaceSize, executor, stream)
⑥ aclrtSynchronizeStream 等待完成，aclrtMemcpy(D2H) 拷回结果并打印
⑦ 释放：aclDestroyTensor / aclrtFree / aclrtDestroyStream / aclFinalize
```

#### 4.2.2 核心流程

以 AddExample 为例，计算任务的本质是：

\[ y = x_1 + x_2 \]

其中 \( x_1 \) 是 2048 个从 1.0 开始递增的 float（shape 为 `{32, 4, 4, 4}`），\( x_2 \) 是种子 42 生成的 `[-1024, 1024]` 均匀随机整数转成的 float。执行时序上：

```伪代码
host: 准备 x1、x2 数据，创建 aclTensor 描述符
host: GetWorkspaceSize(x1, x2, out) → (workspaceSize, executor)
host: malloc workspace; 下发 aclnnAddExample(workspace, executor, stream)
device: kernel 执行 out = x1 + x2（异步）
host: SynchronizeStream 阻塞等待 → D2H 拷回 out → 逐项校验
```

#### 4.2.3 源码精读

- [examples/add_example/examples/test_aclnn_add_example.cpp:60-70](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/examples/add_example/examples/test_aclnn_add_example.cpp#L60-L70)：`Init` 函数是「固定写法」——`aclInit`、`aclrtSetDevice`、`aclrtCreateStream` 三连，几乎每个样例都原样复制。
- [examples/add_example/examples/test_aclnn_add_example.cpp:110-126](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/examples/add_example/examples/test_aclnn_add_example.cpp#L110-L126)：构造第一个输入 `selfX`：shape 固定为 `{32, 4, 4, 4}`，用 `std::iota` 填充 1.0 起步的递增序列；第二个输入 `selfY` 用 `std::mt19937` 固定种子 42 生成随机数——固定种子的意义是结果可复现。代码注释明确提醒：**该样例算子未做 shape/dtype 全泛化，其他输入场景可能不支持**。
- [examples/add_example/examples/test_aclnn_add_example.cpp:143-155](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/examples/add_example/examples/test_aclnn_add_example.cpp#L143-L155)：两段式调用的现场——先 `aclnnAddExampleGetWorkspaceSize(selfX, selfY, out, &workspaceSize, &executor)`，再按需申请 workspace，最后 `aclnnAddExample(workspaceAddr, workspaceSize, executor, stream)` 下发执行。
- [examples/add_example/examples/test_aclnn_add_example.cpp:42-58](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/examples/add_example/examples/test_aclnn_add_example.cpp#L42-L58)：`PrintOutResult` 把 device 侧结果 `ACL_MEMCPY_DEVICE_TO_HOST` 拷回 host，并打印前 10 组「输入 1、输入 2、结果」三元组，方便逐行人工核对。
- 对照正式版 [math/add/examples/test_aclnn_add.cpp:115-128](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/examples/test_aclnn_add.cpp#L115-L128)：骨架完全相同，但调用的是 `aclnnAddGetWorkspaceSize(self, other, alpha, out, ...)` / `aclnnAdd(...)`——正式版 Add 多了一个 `alpha` 标量参数（对应 \( y = x_1 + \alpha \cdot x_2 \) 风格的接口形态），输入 shape 是自由的 `{4, 2}` 而非写死的 4 维。

#### 4.2.4 代码实践

**实践目标**：把端到端流程中的「验证」环节做扎实——修改样例输入数据，重新运行并核对结果。

**操作步骤**：

1. 打开 `examples/add_example/examples/test_aclnn_add_example.cpp`，找到 `main` 中 `selfXHostData` 的填充处（第 113 行附近）。
2. 把递增序列改为 0–9 循环值（保持 shape `{32, 4, 4, 4}` 与元素个数 2048 不变）：

   ```cpp
   for (int64_t i = 0; i < static_cast<int64_t>(selfXHostData.size()); ++i) {
       selfXHostData[i] = static_cast<float>(i % 10);
   }
   ```

   这正是 [docs/QUICKSTART.md:229-244](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/QUICKSTART.md#L229-L244) 中「算子验证」一节的官方做法。
3. 由于只改了样例代码、没改算子本身，**无需重新编译安装算子包**，直接重跑：

   ```bash
   bash build.sh --run_example add_example eager cust --vendor_name=custom
   ```

4. 逐行核对打印结果。
5. 进阶（可选）：若想体验修改 **shape**，请改用正式版样例 `math/add/examples/test_aclnn_add.cpp`——把 `{4, 2}` 改成如 `{8, 16}` 并同步调整 host 数据向量长度。正式版 Add 已全泛化，支持任意 shape；而 AddExample 官方明确警告勿改 shape（见 [docs/QUICKSTART.md:247](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/QUICKSTART.md#L247)）。

**需要观察的现象**：输出中 `first input[i]` 变为 0,1,2,...,9,0,1,... 的循环序列；`second input[i]` 因种子固定仍与修改前一致。

**预期结果**：每行 `result[i]` 仍严格等于 `first input[i] + second input[i]`。例如若第二输入某位置为 `-5.0`，第一输入该位置为 `3.0`，则结果应为 `-2.0`。

**说明**：无 NPU 环境下具体数值为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`CreateAclTensor` 中为什么要自己计算 `strides`？

**答案**：`aclCreateTensor` 的接口需要同时给出 shape 和 strides 才能完整描述一块内存的布局。样例构造的是连续（contiguous）张量，所以 strides 按 \( \text{strides}[i] = \text{shape}[i+1] \times \text{strides}[i+1] \) 从后向前累乘即可；这属于样例的通用工具代码，两个样例中逐字相同。

**练习 2**：第二段接口 `aclnnAddExample(...)` 调用返回成功后，结果就已经在 host 的 `resultData` 里了吗？

**答案**：没有。该调用只是把任务异步下发到 `stream`，立即返回。必须先 `aclrtSynchronizeStream(stream)` 等待 device 执行完毕，再 `aclrtMemcpy(ACL_MEMCPY_DEVICE_TO_HOST)` 把输出从 device 内存拷回 host，才能读到结果。

**练习 3**：AddExample 与正式 Add 的 aclnn 接口签名差在哪里？

**答案**：AddExample 是 `aclnnAddExample(selfX, selfY, out, ...)`，两个输入一个输出；正式 Add 是 `aclnnAdd(self, other, alpha, out, ...)`，多一个 `aclScalar* alpha` 标量参数。教学算子为降低复杂度砍掉了 alpha、广播、多数据类型等能力。

### 4.3 add 算子说明：如何用 README 判断「能不能用、怎么调」

#### 4.3.1 概念说明

每个算子目录下的 README 是它的「规格说明书」，是使用任何算子前的第一入口。两份 README 的对比正好展示了「教学算子」与「商用算子」的差距：

| 维度 | examples/add_example | math/add |
|---|---|---|
| 数据类型 | FLOAT、INT32 | BOOL/INT8/INT16/INT32/INT64/UINT8/FLOAT16/BFLOAT16/FLOAT/FLOAT64/COMPLEX 系列/STRING 等十余种 |
| shape 约束 | 仅支持 4 维 | 无固定维度限制（ND） |
| 调用方式 | aclnn、图模式 | aclnn、图模式 |
| 产品支持 | 950、A3、A2 | 950、A3、A2、Atlas 推理/训练系列等（200I/500 A2 不支持） |

#### 4.3.2 核心流程

读一份算子 README 的标准顺序：

```text
① 产品支持情况表 → 我的环境能不能用？
② 功能说明 + 计算公式 → 算子做什么？
③ 参数说明表 → 输入/输出/属性的类型与格式约束
④ 调用说明表 → 有哪些样例代码可以直接抄
```

#### 4.3.3 源码精读

- [math/add/README.md:3-12](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/README.md#L3-L12)：产品支持情况表。注意最后一行 `Atlas 200I/500 A2 推理产品` 标记为 ×——**用错硬件时算子根本无法运行**，这就是「使用前先查表」的原因。
- [math/add/README.md:14-22](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/README.md#L14-L22)：功能说明给出计算公式 \( y = x_1 + x_2 \)。
- [math/add/README.md:39-62](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/README.md#L39-L62)：参数说明表——x1/x2/y 均为 ND 格式，x2 与 y 的类型「同 x1」，即三个张量类型一致。
- [math/add/README.md:65-71](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/README.md#L65-L71)：调用说明表直接链接到 aclnn 样例与图模式样例，是上手代码的最短路径。
- [examples/add_example/README.md:62-64](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/examples/add_example/README.md#L62-L64)：AddExample 的约束说明一节明确写着「输入输出仅支持4维」——与 4.2 节样例代码注释中「勿改 shape」的警告互相印证。

#### 4.3.4 代码实践

**实践目标**：养成「先读 README 再写代码」的习惯，用规格表回答具体问题。

**操作步骤**：

1. 打开 `math/add/README.md`，只看参数说明表，回答：输入 x1 为 FLOAT16 时，输出 y 是什么类型？
2. 打开 `examples/add_example/README.md`，找出它的约束说明，解释为什么样例代码写死 `{32, 4, 4, 4}`。
3. 在两份 README 的「产品支持情况」表中，找一个两者支持情况不同的产品。

**需要观察的现象**：纯文档阅读，无需运行。

**预期结果**：

1. y 的类型「同 x1」，即 FLOAT16——Add 不做隐式类型提升。
2. 约束是「输入输出仅支持 4 维」，且样例未做 shape 泛化，所以固定 4 维 shape。
3. 例如 `Atlas 推理系列产品` / `Atlas 训练系列产品` 在 `math/add` 支持为 √，而 AddExample 的表中没有列出这两行（仅支持 950/A3/A2）——正式算子覆盖面更广。

#### 4.3.5 小练习与答案

**练习 1**：某同事想在 `Atlas 200I/500 A2` 上调用 Add 算子，README 哪一节直接告诉他不可行？

**答案**：「产品支持情况」表，该产品行标记为 ×。应改用其他支持的算子实现方案或更换硬件。

**练习 2**：Add 的参数表里 STRING、COMPLEX64 这类类型在 AddExample 中支持吗？

**答案**：不支持。AddExample 参数表只声明 FLOAT、INT32。教学算子为简化实现裁剪了类型支持，这也呼应了它 kernel 实现更简单的事实。

## 5. 综合实践

**任务：给 AddExample 做一次「数据级回归验证」。**

结合本讲三个模块，完成以下闭环：

1. **跑基线**：按 4.1.4 的四步命令跑通 AddExample，保存（截图或复制）前 10 组输出，此为基线数据。
2. **改数据**：按 4.2.4 的方法，把第一个输入改为 `i % 10` 循环序列，把第二个输入的随机数种子从 42 改为其他值（如 7），重跑样例。
3. **做校验**：写一小段独立的 C++ 或 Python 脚本，用同样的数据生成逻辑（`std::iota`/`std::mt19937` 种子 7）在 host 侧复现两个输入，逐项计算期望输出，与样例打印的 `result[i]` 比对，统计误差为 0 的比例。
4. **读规格**：对照 `math/add/README.md` 与 `examples/add_example/README.md`，写 3–5 行总结：如果把业务代码从 AddExample 迁移到正式 Add，接口层需要改哪几处（提示：头文件、函数名、alpha 参数、shape 自由度）。

验收标准：第 3 步误差比例为 100%，第 4 步总结能具体到函数名。无 NPU 环境时第 1–3 步标注「待本地验证」，第 4 步可独立完成。

## 6. 本讲小结

- QUICKSTART 的「编译运行」四步是所有算子共用的通用流程：`build.sh --pkg --soc=... --ops=...` 编译 → 安装 run 包 → `export LD_LIBRARY_PATH` → `build.sh --run_example` 运行验证。
- 所有 aclnn 样例共享一个七步固定骨架：初始化 → 构造输入 → GetWorkspaceSize → 申请 workspace → 第二段接口 → 同步并拷回 → 释放资源；读懂一个样例即可读懂全部。
- aclnn 接口是两段式设计：第一段做检查并给出 workspace 需求，第二段真正下发执行，调用后必须 `aclrtSynchronizeStream` 才能取结果。
- `examples/add_example` 是教学裁剪版（仅 FLOAT/INT32、仅 4 维），`math/add` 是全功能正式版（十余种类型、ND 格式）；对照二者能清晰看到「能跑的教学样例」与「商用规格」之间的距离。
- 算子 README 的产品支持表、参数表、调用样例链接是使用任何算子前的第一手权威资料，先查表再动手。

## 7. 下一步学习建议

至此单元一结束，你已经能编译并运行一个算子。单元二将以 `math/add` 为主线逐层解剖算子内部实现，建议按顺序：

1. **u2-l1（算子规格说明书怎么读）**：更系统地解读 README 与 aclnn 接口文档的对应关系。
2. **u2-l2（算子定义与注册）**：进入 `math/add/op_host/add_def.cpp`，看算子规格是如何用 OpDef DSL 注册进 CANN 的。
3. 提前浏览 `math/add/` 目录，对照 u1-l2 讲的目录模板，找出 `op_host`、`op_kernel`、`op_api` 中的入口文件，为单元二做铺垫。
