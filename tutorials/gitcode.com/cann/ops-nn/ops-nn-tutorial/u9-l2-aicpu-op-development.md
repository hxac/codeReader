# u9-l2 AI CPU 算子开发：add_example_aicpu 对照实现

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 AI Core 算子与 AI CPU 算子的区别，以及「什么时候应该选择 AI CPU」。
2. 掌握 `op_kernel_aicpu` 目录的三件交付件：`*_aicpu.h`（算子类声明）、`*_aicpu.cpp`（Compute 实现与注册）、`*.json`（算子信息库）的组织方式。
3. 理解 AI CPU 工程为什么**没有 tiling、没有 `*_def.cpp`**，Host 侧只剩 infershape。
4. 能独立编译、安装并运行 `add_example_aicpu` 样例，并与 u1-l4 学过的 AI Core 版 `add_example` 做逐项对比。

本讲是 u9-l1（`--genop` 脚手架）的姊妹篇：`--genop` 生成 AI Core 工程，`--genop_aicpu` 生成 AI CPU 工程，两者共用同一套 build.sh / cmake 编译部署体系。

## 2. 前置知识

**AI CPU 是什么？** 一颗昇腾 NPU 芯片上不只有 AI Core（矩阵/矢量专用计算单元），还挂了若干通用 CPU 核，称为 AI CPU。AI Core 算子用 Ascend C 语言开发（我们在 u5 单元精读过的 CopyIn-Compute-CopyOut 流水线），而 AI CPU 算子就是**普通的 C++ 代码**，跑在这些通用核上。

**为什么需要 AI CPU 算子？** 两类典型场景：

- **逻辑复杂但计算量小的算子**：比如带大量分支、字符串处理、动态数据结构的算子（index、hash 类中部分实现），用 Ascend C 的矢量指令表达反而别扭，用 C++ 写一遍 for 循环最自然。
- **移植已有 CPU 实现**：把 TensorFlow 等框架已有的 CPU kernel 移植到昇腾，AI CPU 路径几乎可以原样复用代码。

代价是性能：AI CPU 核数少、无专用计算单元，吞吐远低于 AI Core，所以仓库里 AI CPU 算子只占少数，且同一个算子常常 AI Core / AI CPU 双实现并存（u6-l3 见过的 `gather_v2` 就是例子）。

**CpuKernel 基类与 Compute 函数**：AI CPU 算子框架（源自 `cpu_kernel.h` 头文件）约定每个算子实现为一个继承 `CpuKernel` 的类，重写 `Compute(CpuKernelContext &ctx)`。`ctx` 是上下文，通过 `ctx.Input(i)` / `ctx.Output(i)` 拿到输入输出 Tensor，通过 `ctx.Attr(...)` 拿属性——角色上等价于 AI Core 侧的 `gert::TilingContext` 和 kernel 入口的 GM 地址参数，只是形态从「指针 + tiling data」变成了「带方法的上下文对象」。

**Eigen**：C++ 原生不支持半精度浮点，AI CPU 算子可借助 Eigen 库表示（官方开发指南特别建议 3.3.9 版本），本仓库 UT 链接了 `Eigen3::Eigen`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/zh/develop/aicpu_develop_guide.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicpu_develop_guide.md) | AI CPU 算子开发官方指南：开发流程、目录约定、编译部署 |
| [examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu.h) | 算子类声明：`AddExampleCpuKernel` 继承 `CpuKernel` |
| [examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu.cpp) | Compute 实现、模板化 AddCompute、`REGISTER_CPU_KERNEL` 注册 |
| [examples/add_example_aicpu/op_kernel_aicpu/add_example.json](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/op_kernel_aicpu/add_example.json) | 算子信息库：engine、kernelSo、输入输出类型声明 |
| [examples/add_example_aicpu/op_host/add_example_infershape.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/op_host/add_example_infershape.cpp) | Host 侧唯一的推导交付件：输出 shape 复制输入 shape |
| [examples/add_example_aicpu/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/CMakeLists.txt) | 工程入口：子目录收集（注意剔除了 op_graph） |
| [examples/add_example_aicpu/op_kernel_aicpu/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/op_kernel_aicpu/CMakeLists.txt) | aicpu kernel 构建逻辑：交叉编译工具链 + `add_aicpu_cust_kernel_modules` |
| [examples/add_example_aicpu/tests/ut/op_kernel_aicpu/test_add_example.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/tests/ut/op_kernel_aicpu/test_add_example.cpp) | AI CPU kernel UT：NodeDefBuilder + RUN_KERNEL |
| [examples/add_example_aicpu/examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/examples/test_aclnn_add_example.cpp) | aclnn 调用样例：与 AI Core 版几乎逐字相同 |
| [examples/add_example_aicpu/README.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/README.md) | 算子说明：产品支持矩阵（注意 950 系不支持） |
| [build.sh](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh) | `--genop_aicpu`、`--opkernel_aicpu`、`--opkernel_aicpu_test`、`--noaicpu` 等参数 |

## 4. 核心概念与源码讲解

### 4.1 AI Core 还是 AI CPU：选型与工程差异总览

#### 4.1.1 概念说明

官方开发指南开篇一句话给出定义（[docs/zh/develop/aicpu_develop_guide.md:L3-L7](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicpu_develop_guide.md#L3-L7)）：

> 算子根据运行的硬件单元不同，可分为 AI Core 算子和 AI CPU 算子（少数）。前者使用 Ascend C 语言开发，运行在 AI Core 硬件单元；后者使用 C++语言开发，运行在 AI CPU 硬件单元。

选型判断可以归纳成一张表：

| 维度 | AI Core 算子 | AI CPU 算子 |
| --- | --- | --- |
| 语言 | Ascend C | C++ |
| 计算单元 | 矢量/矩阵专用单元，吞吐极高 | 通用 CPU 核，吞吐低但编程自由 |
| 适合 | 大规模规整并行计算（elementwise、matmul、norm…） | 逻辑分支多、计算量小、动态结构（部分 index/hash/control 类） |
| 性能 | 高 | 低，通常只作功能兜底或特殊场景 |
| 硬件支持 | 全系列 | 受限，如本样例仅 A2/A3 支持，950PR/DT 不支持 |

#### 4.1.2 核心流程

AI CPU 算子的标准开发流程（指南给出的七步，[docs/zh/develop/aicpu_develop_guide.md:L13-L27](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicpu_develop_guide.md#L13-L27)）：

```text
前提条件（环境准备）
→ 工程创建（build.sh --genop_aicpu=${op_class}/${op_name}）
→ 算子定义（README.md + ${op_name}.json）
→ Kernel 实现（*_aicpu.h + *_aicpu.cpp）
→ aclnn 适配（编译后自动生成，无需手写）
→ 编译部署（build.sh --pkg ...）
→ 算子验证（UT / aclnn 样例）
```

注意与 AI Core 流程（u9-l1）相比**少了 tiling 步骤**——这是两种算子最本质的工程差异之一。

#### 4.1.3 源码精读

脚手架生成的标准目录结构（[docs/zh/develop/aicpu_develop_guide.md:L48-L63](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicpu_develop_guide.md#L48-L63)）：

```bash
${op_name}
├── examples                            # 算子调用示例
│   └── test_aclnn_${op_name}.cpp
├── op_host                             # Host侧实现
│   └── ${op_name}_infershape.cpp       # 只做 shape 推导
├── op_kernel_aicpu                     # Device侧Kernel实现（注意目录名）
│   ├── ${op_name}_aicpu.cpp            # Kernel入口：Compute + 注册
│   ├── ${op_name}_aicpu.h              # 算子类声明
│   └── ${op_name}.json                 # 算子信息库
├── tests                               # UT实现
└── CMakeLists.txt
```

创建命令是 `bash build.sh --genop_aicpu=${op_class}/${op_name}`（[docs/zh/develop/aicpu_develop_guide.md:L33-L40](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicpu_develop_guide.md#L33-L40)），对应 build.sh 的参数表（[build.sh:L27-L29](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L27-L29)）中登记的 `genop_aicpu=` 选项。

`examples/add_example_aicpu` 就是这个模板的完整实例，实际文件树（多出 op_graph 图模式交付件）：

```text
add_example_aicpu/
├── CMakeLists.txt
├── README.md
├── examples/            test_aclnn_add_example.cpp、test_geir_add_example.cpp
├── op_host/             add_example_infershape.cpp（没有 _def.cpp、没有 _tiling.cpp！）
├── op_kernel_aicpu/     add_example_aicpu.h / .cpp / add_example.json
├── op_graph/            add_example_proto.h（REG_OP 注册，供 GE 图模式）
└── tests/ut/op_kernel_aicpu/  test_add_example.cpp
```

**关键对照**：AI Core 版 `add_example` 的 op_host 下有 `add_example_def.cpp`（OP_ADD 注册算子原型）和 `add_example_tiling.cpp`（两级切分）；AI CPU 版两者都没有——算子原型信息搬进了 `add_example.json`，而 C++ 标量循环根本不需要切分。

#### 4.1.4 代码实践

**实践目标**：建立两种工程结构的直观对照。

**操作步骤**：

1. 在仓库根目录执行 `diff -r examples/add_example examples/add_example_aicpu | head -60`（或并排打开两个目录）。
2. 逐一核对下表每一行：

| 对照项 | add_example（AI Core） | add_example_aicpu（AI CPU） |
| --- | --- | --- |
| Device 目录名 | `op_kernel` | `op_kernel_aicpu` |
| 注册算子原型 | `op_host/*_def.cpp` 的 `OP_ADD` | `op_kernel_aicpu/*.json` |
| tiling | `op_host/*_tiling.cpp` | 无 |
| kernel 语言 | Ascend C（TPipe/TQue/LocalTensor） | C++（裸指针 for 循环） |
| kernel 注册宏 | 编译期按 tiling key 实例化模板 | `REGISTER_CPU_KERNEL` |
| 算子名 | `AddExample` | `AddExampleAicpu`（必须不同名） |

**需要观察的现象**：AI CPU 版 op_host 只剩 infershape 一个 `.cpp`；两个工程的 `examples/` 下样例文件名完全相同。

**预期结果**：上表 7 行全部核对成立。特别注意算子名——两个工程功能相同但注册名必须区分（`AddExample` vs `AddExampleAicpu`），否则同一算子库内重名冲突。

### 4.2 op_kernel_aicpu 三件套：json 信息库与 CpuKernel 实现

#### 4.2.1 概念说明

AI CPU 算子的「身份证」不在 `*_def.cpp`，而在一个 JSON 文件里，称为**算子信息库**。它告诉运行时：这个算子跑在哪个引擎（engine）、编译进哪个动态库（kernelSo）、入口函数名是什么、支持哪些输入输出类型。而计算逻辑则是标准的 C++ 面向对象写法：一个继承 `CpuKernel` 的类 + 一个重写的 `Compute` 方法 + 一个注册宏。

#### 4.2.2 核心流程

Compute 函数的执行逻辑：

```text
框架调度 RunCpuKernel（json 中 functionName）
→ 按 json 中算子名找到 REGISTER_CPU_KERNEL 注册的类
→ 调用 Compute(ctx)
  ├─ ctx.Input(0) / ctx.Input(1) / ctx.Output(0) 取 Tensor
  ├─ 空指针与空数据校验
  ├─ 按 input0 的 DataType switch 分发到模板 AddCompute<float>/<int32_t>
  │    └─ 取裸指针，for 循环 y[i] = x0[i] + x1[i]
  └─ 返回 0（kSuccess）/ 1（kParamInvalid）
```

没有 tiling、没有 BlockDim、没有双缓冲——所有元素在一个函数调用里串行/由编译器向量化处理完。

#### 4.2.3 源码精读

先看算子信息库 [examples/add_example_aicpu/op_kernel_aicpu/add_example.json:L1-L27](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/op_kernel_aicpu/add_example.json#L1-L27)。这段 JSON 声明了算子 `AddExampleAicpu` 的全部元信息：`engine: DNN_VM_AICPU` 表示跑在 AI CPU 引擎上（这是与 AI Core 算子最根本的分流字段）；`kernelSo: libcv_aicpu_kernels.so` 与 `functionName: RunCpuKernel` 告诉运行时去哪个动态库找统一入口；`opKernelLib: CUSTAICPUKernel` 标记自定义 AI CPU kernel 库；`workspaceSize: 100` 预留 workspace 字节数（AI Core 版是由 tiling 函数算出来的，这里直接写死）。`input0/input1/output0` 三个字段用 `type: "DT_INT32,DT_FLOAT"` 声明支持的类型列表，角色上等价于 def 文件里 `Input(...).ParamType(REQUIRED).DataType(...)` 的链式声明。

再看算子类声明 [examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu.h:L27-L51](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu.h#L27-L51)。这段代码包含 `cpu_kernel.h` 基础库头文件后，在固定命名空间 `aicpu` 内声明 `AddExampleCpuKernel : public CpuKernel`，重写 `Compute(CpuKernelContext&)`，并声明模板成员 `AddCompute<T>` 供不同数据类型复用同一套循环。指南强调命名空间 `aicpu` 固定不允许修改（[docs/zh/develop/aicpu_develop_guide.md:L118-L132](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicpu_develop_guide.md#L118-L132)）。

Compute 主入口 [examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu.cpp:L47-L72](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu.cpp#L47-L72)。这段代码先从 `ctx` 取出两个输入和一个输出 Tensor，做空指针防御（失败记 `KERNEL_LOG_ERROR` 日志并返回 `kParamInvalid=1`）；随后做一个值得注意的短路：任一输入 `GetDataSize()==0` 时直接返回成功——空 tensor 无需计算。最后按 `input0->GetDataType()` switch，把 `DT_FLOAT`/`DT_INT32` 分发到对应模板实例，其余类型返回参数错误。**注意**：这里对输入数据类型做白名单校验的写法，与 u6-l1 讲过的 aclnn 适配层 dtype 白名单、u3-l1 讲过的 def 候选槽位是同一思想在三层的重复落点。

真正的计算体 [examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu.cpp:L83-L109](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu.cpp#L83-L109)。`AddCompute<T>` 模板函数把三个 Tensor 的 `GetData()` 裸指针 `reinterpret_cast<T*>` 后，一个 for 循环完成 \( y_i = x_{0,i} + x_{1,i} \)。对比 AI Core 版 add_example.h 里 TQue 双缓冲、CopyIn/Compute/CopyOut 三段流水、尾块 currentNum 处理那一整套机制，这里全部不存在——这就是「用编程自由换计算吞吐」的具体含义。循环次数来自 `input0->NumElements()`，等价于 AI Core 版 tiling 算出的 `totalNum`。

最后是注册 [examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu.cpp:L111-L113](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu.cpp#L111-L113)。`REGISTER_CPU_KERNEL(kAddExample, AddExampleCpuKernel)` 把字符串 `"AddExampleAicpu"`（匿名命名空间常量，[L24-L35](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu.cpp#L24-L35)）映射到算子类，供框架按 json 里的算子名查找。它与 AI Core 侧的 `OP_ADD`（u3-l1）、`IMPL_OP_INFERSHAPE`（u3-l2）、`IMPL_OP_OPTILING`（u4-l1）同属「静态注册免集中清单」一族宏。

#### 4.2.4 代码实践

**实践目标**：体会「改 AI CPU 算子语义」比 AI Core 版还简单。

**操作步骤**：

1. 把 [add_example_aicpu.cpp:L106](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu.cpp#L106) 的 `y[i] = x0[i] + x1[i];` 改成 `y[i] = x0[i] * x1[i];`。
2. 按 4.4 节的命令重新 `--pkg` 编译、安装 run 包、重跑样例。
3. 观察输出后改回（本实践只验证机制，不留改动）。

**需要观察的现象**：样例输出从「每项 2.0」（1+1）变为「每项 1.0」（1×1）。

**预期结果**：一行改动即生效，无需触碰任何 tiling、缓冲、对齐逻辑——对照 u1-l4 中 AI Core 版同样的一行改动（`AscendC::Add`→`AscendC::Mul`），两边改动量相当，但 AI CPU 版完全没有隐式约束（如矢量指令参数顺序、32 字节对齐）。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果把 json 中 `input0` 的 type 改成只留 `"DT_FLOAT"`，但 Compute 里的 switch 不动，会发生什么？

答案：json 是运行时匹配算子的第一道闸门，DT_INT32 输入将在算子选择阶段就被拒掉，根本走不到 Compute 的 switch；switch 里的 `DT_INT32` 分支变成死代码。反过来，只改 switch 不改 json 则会让非法类型漏进 Compute。两侧（外加 README 的参数表）应保持一致——这与 def 文件 DataType 列表和 aclnn 白名单必须对齐是同一条纪律。

**练习 2**：`Compute` 里为什么先做 `GetDataSize() == 0` 就直接返回成功，而不是跳过 switch？

答案：空数据 tensor（如 shape 含 0）元素数为 0，循环体一次都不执行，结果天然正确；提前返回避免了对空指针数据做无意义的类型分发和模板实例化，属于廉价而正确的短路。

**练习 3**：AI CPU 算子为什么不需要 tiling？

答案：tiling 解决的是「把大任务切给多个 AI Core、把数据块切进有限的片上 UB」的调度问题（u4-l1）。AI CPU kernel 是普通 C++ 函数，直接在 Device 内存地址上以裸指针遍历，没有 UB 容量约束，也不由 tiling 决定并行核数，因此两级切分、BlockDim、TilingKey、TilingData 契约整套机制都不存在。

### 4.3 Host 侧交付件：只剩 infershape（与被剔除的 op_graph）

#### 4.3.1 概念说明

框架在下发任何算子前仍必须知道输出 shape 和 dtype，所以 AI CPU 算子同样需要 infershape——写法与 AI Core 版（u3-l2）完全同构：`IMPL_OP_INFERSHAPE(算子名).InferShape(...)` 注册，函数内从 `gert::InferShapeContext` 取输入 shape、写输出 shape。aclnn 适配层则在编译后**自动生成**，无需像 AI Core 算子那样手写 `aclnn_*.cpp`（指南明确说明，[docs/zh/develop/aicpu_develop_guide.md:L194-L196](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicpu_develop_guide.md#L194-L196)）。

#### 4.3.2 核心流程

```text
aclnn 第一段 / GE 图编译
→ 按 IMPL_OP_INFERSHAPE 注册表回调 InferShapeAddExample
→ 取 xShape（输入0 的 shape）
→ yShape 逐维复制 xShape
→ 引擎按 json 的 engine=DNN_VM_AICPU 选择 AI CPU 执行路径
→ RunCpuKernel → REGISTER_CPU_KERNEL 查表 → Compute(ctx)
```

#### 4.3.3 源码精读

推导函数本体 [examples/add_example_aicpu/op_host/add_example_infershape.cpp:L33-L55](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/op_host/add_example_infershape.cpp#L33-L55)。这段代码先 `GetInputShape(0)`/`GetOutputShape(0)` 并用 `OP_CHECK_NULL_WITH_CONTEXT` 做空指针防御，然后 `SetDimNum(xShapeSize)` + 逐维 `SetDim(i, dim)` 把输出 shape 设成与输入完全一致——与 AI Core 版 add_example 的 infershape 逐字同构，印证了「infershape 属于框架侧公共契约，与 Device 侧用什么硬件实现无关」。

注册语句 [examples/add_example_aicpu/op_host/add_example_infershape.cpp:L57-L58](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/op_host/add_example_infershape.cpp#L57-L58)：`IMPL_OP_INFERSHAPE(AddExampleAicpu).InferShape(InferShapeAddExample)`，算子名字符串必须与 json 的 key、`REGISTER_CPU_KERNEL` 的第一个参数严格一致，三处对齐才能被框架串起来。

一个构建层细节：工程入口 [examples/add_example_aicpu/CMakeLists.txt:L11-L19](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/CMakeLists.txt#L11-L19) 用 `file(GLOB)` 收集子目录，但显式 `list(REMOVE_ITEM CURRENT_DIRS op_graph)` 把 op_graph 从常规构建中剔除（`tests` 仅在 `ENABLE_TEST` 时编入）。也就是说 op_graph 下的 `REG_OP(AddExampleAicpu)` proto 头文件只在特定构建路径下参与编译，图模式支持是可选交付件。

#### 4.3.4 代码实践

**实践目标**：验证 infershape 与 Device 实现确实解耦。

**操作步骤**：

1. 并排打开 `examples/add_example/op_host/add_example_infershape.cpp` 与 `examples/add_example_aicpu/op_host/add_example_infershape.cpp`。
2. 逐行 diff 两个文件（忽略版权头与注释）。

**需要观察的现象**：两者除算子名（`AddExample` vs `AddExampleAicpu`）和日志字符串外逻辑一致。

**预期结果**：确认「同一个 shape 推导可以服务任意硬件实现」；进而理解为什么 u3-l2 学过的 infershape UT 方法（`InfershapeContextPara` + `ExecuteTestCase`）对 AI CPU 算子同样适用。

#### 4.3.5 小练习与答案

**练习 1**：AI CPU 算子的「三处算子名对齐」指哪三处？

答案：① `op_kernel_aicpu/*.json` 的顶层 key（`"AddExampleAicpu"`）；② `REGISTER_CPU_KERNEL(kAddExample, ...)` 传入的字符串常量值；③ `IMPL_OP_INFERSHAPE(AddExampleAicpu)` 的注册名。名字对不上时框架查不到推导函数或 kernel 类，算子无法被调起。

**练习 2**：为什么这个工程没有 aclnn 适配目录，却仍能用 aclnn 调用？

答案：AI CPU 算子的 aclnn 接口由构建系统在编译完成后自动生成（指南「aclnn适配」一节），不像 AI Core 生产算子（如 gelu）需要手写 `op_api/aclnn_gelu.cpp` 分层封装。所以目录里没有 op_api，样例却能 include `aclnn_add_example.h` 调用两段式接口。

### 4.4 构建与验证：交叉编译、UT 与 aclnn 样例

#### 4.4.1 概念说明

AI CPU kernel 是要在设备上的 Linux 环境里跑的 `.so`，因此构建时使用**交叉编译工具链**（而非编译 AI Core kernel 用的 Ascend C 编译器）；UT 则因为本来就是 C++ 代码，可以直接编成 x86 主机程序跑（无需 tikicpulib 仿真，对照 u7-l2）。build.sh 为此提供了独立的参数族。

#### 4.4.2 核心流程

```text
bash build.sh --pkg --soc=... --ops=add_example_aicpu
→ cmake 进入 op_kernel_aicpu/CMakeLists.txt
→ （非 UT 构建）切换 aarch64 交叉编译器，收集 *.json 进全局属性
→ add_aicpu_cust_kernel_modules 编出 AI CPU kernel 目标
→ 打包为 run 包 → 安装到 opp/vendors/<vendor>_nn
→ bash build.sh --run_example add_example_aicpu eager cust   # 上板验证
→ 或 bash build.sh --opkernel_aicpu_test --ops=...           # 无卡 UT 验证
```

#### 4.4.3 源码精读

构建脚本 [examples/add_example_aicpu/op_kernel_aicpu/CMakeLists.txt:L11-L27](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/op_kernel_aicpu/CMakeLists.txt#L11-L27)。这段 CMake 在 `BUILD_WITH_INSTALLED_DEPENDENCIES_CANN_PKG` 前提下工作：正式构建（非 UT）时把 C++ 编译器切到 `${ASCEND_DIR}/toolkit/toolchain/hcc/bin/aarch64-target-linux-gnu-g++` 交叉工具链并设置 CXX11 ABI；随后 `file(GLOB_RECURSE)` 收集本目录所有 `*.json`（算子信息库）挂到全局属性 `AICPU_JSON_FILES`，`file(GLOB ... *_aicpu*.cpp)` 收集 kernel 源码，交给 `add_aicpu_cust_kernel_modules(${OBJ_NAME})` 这个公共函数编成自定义 AI CPU kernel 目标。注意源码收集规则是文件名含 `_aicpu`——这就是 `_aicpu.cpp` 命名约定的由来。

UT 侧 [examples/add_example_aicpu/tests/ut/op_kernel_aicpu/test_add_example.cpp:L29-L52](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/tests/ut/op_kernel_aicpu/test_add_example.cpp#L29-L52)。这段用例用宏 `CREATE_NODEDEF` 构造算子节点：`NodeDefBuilder` 以 `("AddExampleAicpu", "AddExampleAicpu")` 声明算子，链式挂上两个输入（shape `{2}` 与 `{1}`）和一个输出；然后 `RUN_KERNEL(node_def, HOST, KERNEL_STATUS_OK)` 直接在**主机上**执行 kernel（这是 AI CPU UT 与 AI Core kernel UT 的关键差异——后者要借 tikicpulib 仿真，前者本来就是 CPU 代码）；最后 `CompareResult` 把输出 `{5, 8}` 与期望对账（`{2,5}+{3}` 广播相加）。用例还示范了标量输入：input2 shape 为 `{1}`，Compute 的 for 循环按 `input0->NumElements()` 遍历而 input1 只有 1 个元素——本样例 kernel 并未做广播处理，此用例能通过依赖的是 `{2}+{1}` 恰好逐元素可加的语义约定。

⚠️ **一个值得注意的细节（待本地验证）**：该文件 [L27](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/tests/ut/op_kernel_aicpu/test_add_example.cpp#L27) 定义的 fixture 类名是 `TEST_AddExample_UT`，而 [L36](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/tests/ut/op_kernel_aicpu/test_add_example.cpp#L36) 的 `TEST_F(TEST_ADD_UT, ...)` 引用的却是 `TEST_ADD_UT`，且全仓库搜不到 `TEST_ADD_UT` 的定义。这处不一致是否会导致该 UT 编译失败，需在本地跑 `--opkernel_aicpu_test` 时确认——正好作为下面的实践观察点。

调用样例 [examples/add_example_aicpu/examples/test_aclnn_add_example.cpp:L119-L136](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/examples/test_aclnn_add_example.cpp#L119-L136)。这段就是 u2-l1 学过的两段式骨架：`aclnnAddExampleGetWorkspaceSize` 登记执行清单 + `aclnnAddExample` 异步下发 + `aclrtSynchronizeStream` 同步——与 AI Core 版样例几乎逐字相同。区别只在头文件 `aclnn_add_example.h` 背后自动生成的适配层会把任务路由到 AI CPU 引擎（依据 json 的 engine 字段），调用者完全无感。这也印证了 aclnn 是统一调用面、硬件实现可替换的设计。

硬件支持矩阵见 [examples/add_example_aicpu/README.md:L5-L12](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/README.md#L5-L12)：仅 Atlas A2/A3 系列支持，Ascend 950PR/950DT 等标注 ×——选型时必须先查这张表。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：完整走通 AI CPU 算子的「编译 → 安装 → 运行 → 对比」闭环。

**操作步骤**（在配套 CANN 环境中，`soc_version` 按实际芯片取 `ascend910b` 或 `ascend910_93`）：

1. 编译并安装：

   ```bash
   bash build.sh --pkg --soc=${soc_version} --ops=add_example_aicpu -j16
   ./build_out/cann-ops-nn-custom_linux-${arch}.run
   ```

2. 配置环境变量（vendor 默认 custom）：

   ```bash
   export LD_LIBRARY_PATH=${ASCEND_HOME_PATH}/opp/vendors/custom_nn/op_api/lib:${LD_LIBRARY_PATH}
   export ASCEND_CUSTOM_OPP_PATH=${ASCEND_HOME_PATH}/opp/vendors/custom_nn
   ```

3. 运行 aclnn 样例：

   ```bash
   bash build.sh --run_example add_example_aicpu eager cust
   ```

4. （无卡可选）跑 AI CPU kernel UT：

   ```bash
   bash build.sh --opkernel_aicpu_test --ops=add_example_aicpu
   ```

5. 对照运行 AI Core 版：重复步骤 3 但把算子名换成 `add_example`（须先按 u1-l2 编译安装 AI Core 版）。

**需要观察的现象**：

- 样例输出 2048 个 `result[i] is: 2.000000`（输入全 1，1+1=2）。
- 对比两版：AI Core 版与 AI CPU版**输出格式与数值完全一致**（同一份样例模板）；差异藏在安装目录里——可在 `${ASCEND_HOME_PATH}/opp/vendors/custom_nn/` 下查找 `libcv_aicpu_kernels.so`（json 中 kernelSo 指向的库）确认 AI CPU kernel 的落盘位置。
- UT 运行时留意 4.4.3 提到的 fixture 名不一致是否导致编译报错。

**预期结果**：两版样例打印完全相同的加法结果，证明 aclnn 调用面对硬件实现透明。运行输出待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`--opkernel_aicpu` 与 `--opkernel_aicpu_test` 有何区别？各自何时用？

答案：前者（[build.sh:L902-L904](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L902-L904)）只构建 AI CPU kernel 交付件（交叉编译出 `.so`），用于出包前单独验证可编；后者额外触发 UT 目标 `${PKG_NAME}_aicpu_op_kernel_ut` 的构建并在主机上执行（[tests/ut/op_kernel_aicpu/CMakeLists.txt:L13-L23](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/op_kernel_aicpu/CMakeLists.txt#L13-L23)），用于无卡快速验证计算逻辑。另外 build.sh 还约束 `--opkernel_aicpu` 不能与 `-u` 系测试命令、`--jit` 同用（[build.sh:L466-L467](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L466-L467)、[L545-L546](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L545-L546)）。

**练习 2**：为什么 AI CPU kernel UT 能「直接在主机跑」，而 AI Core kernel UT 需要仿真库？

答案：AI CPU kernel 本身就是 x86/aarch64 主机可执行的 C++ 代码（编译目标与运行目标同类），UT 链接 `libaicpu_context_host.a` 等宿主侧桩库后即可用 `RUN_KERNEL` 真执行；AI Core kernel 是 Ascend C 写的设备代码，必须经 tikicpulib 编译为主机仿真代码才能执行（u7-l2 的 `ICPU_RUN_KF`）。

**练习 3**：如果一次 `--pkg` 构建只想编 AI Core 部分、跳过所有 AI CPU 交付件，用什么参数？

答案：`--noaicpu`（登记于 [build.sh:L27](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L27)，置位 `NO_AICPU=TRUE`，见 [build.sh:L896](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L896)）。

## 5. 综合实践

**任务：给 add_example_aicpu 增加 DT_FLOAT16 支持并全链路验证。**

1. **算子定义**：修改 [add_example.json](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/op_kernel_aicpu/add_example.json) 的 `input0/input1/output0` type 列表，追加 `DT_FLOAT16`。
2. **Kernel 实现**：指南提示 C++ 原生不支持半精度，需借助 Eigen（UT 链接里已有 `Eigen3::Eigen`）。在 [add_example_aicpu.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/op_kernel_aicpu/add_example_aicpu.cpp) 的 switch 中新增 `case DT_FLOAT16:` 分支，用 `Eigen::half` 作为 `AddCompute<T>` 的模板实参（示例代码，需自行验证头文件包含与编译）。
3. **样例验证**：仿照 [test_aclnn_add_example.cpp:L96-L98](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example_aicpu/examples/test_aclnn_add_example.cpp#L96-L98) 把三处 `ACL_FLOAT` 换成 `ACL_FLOAT16`、host 数据类型适配后，重新编译安装并 `--run_example` 验证输出。
4. **对照思考**：同样的需求在 AI Core 版 add_example 上要动几处？（提示：回顾 u3-l1 综合实践——def 的 DataType 列表、tiling 的类型白名单与 TilingKey、kernel 的模板分支，至少三层。）把两侧改动点写成一张对照表。

这个任务串起了本讲全部模块：json 算子定义 ↔ Compute 类型分发 ↔ aclnn 自动适配 ↔ 编译部署验证，并以「改动面对比」收束到「何时选 AI CPU」的选型判断——逻辑改动越「C++ 原生」，AI CPU 路径越划算；越是大规模数据并行，越应该忍受 AI Core 的多层约束换取吞吐。

## 6. 本讲小结

- AI CPU 算子用 C++ 开发、跑在通用 CPU 核上，适合逻辑复杂、计算量小或需移植 CPU 实现的场景；吞吐远低于 AI Core，是少数派选择，且硬件支持受限（本样例仅 A2/A3）。
- `op_kernel_aicpu` 三件套：`*_aicpu.h` 声明继承 `CpuKernel` 的算子类、`*_aicpu.cpp` 实现 `Compute(CpuKernelContext&)` 并以 `REGISTER_CPU_KERNEL` 注册、`*.json` 算子信息库声明 engine/kernelSo/类型等元信息。
- AI CPU 工程**没有 tiling、没有 `*_def.cpp`、无需手写 aclnn 适配**：切分机制整体不存在，算子原型信息搬进 json，aclnn 接口编译后自动生成。
- 三处算子名必须严格对齐：json 的 key、`REGISTER_CPU_KERNEL` 的字符串、`IMPL_OP_INFERSHAPE` 的注册名。
- 构建上 AI CPU kernel 走 aarch64 交叉工具链（`add_aicpu_cust_kernel_modules`），源码按 `*_aicpu*.cpp` 文件名约定收集；UT 因本就是 C++ 代码可直接在主机执行（`RUN_KERNEL`），无需仿真库。
- 调用面对硬件透明：aclnn 两段式样例与 AI Core 版几乎逐字相同，引擎选择由 json 的 `engine: DNN_VM_AICPU` 在下层完成。

## 7. 下一步学习建议

- 下一讲 u9-l3 进入贡献流程与 experimental 目录：把你按第 5 节思路开发的算子按 `CONTRIBUTING.md` 清单准备贡献。
- 推荐阅读一个 AI Core / AI CPU 双实现并存的真实算子做横向对照：从 `index/` 或 `hash/` 大类里任选一个，比较两条实现路径在同一算子上的分工（可从 u6-l3 介绍的 `docs/zh/op_list.md` 查执行硬件单元）。
- 结合 u7-l2 思考测试策略差异：为你的 AI CPU 算子补一个 `tests/ut/op_kernel_aicpu/test_*.cpp` 用例（`NodeDefBuilder` + `RUN_KERNEL` + `CompareResult` 三步即可成案）。
- 若想深入 AI CPU 运行时机制，可阅读 CANN 包中 `cpu_kernel.h` 相关头文件与 `libaicpu_context_host.a` 暴露的宿主接口，理解 `CpuKernelContext` 如何与框架侧 Tensor 描述对接。
