# Autofuse 目录结构与六大模块总览

## 1. 本讲目标

上一讲（u3-l1）我们建立了 Autofuse 的「为什么」：相邻 Vector 算子之间反复搬运中间结果，导致 Memory Bound；自动融合把一串算子缝进一个 kernel，让中间结果只在片上缓冲（UB）流转。本讲解决「代码长什么样、按什么顺序跑」。

学完本讲，你应当能够：

1. 说出 `autofuse/` 下每个子目录的职责，并区分「六大主线模块」与「辅助目录」。
2. 用一条数据流把 Autofuse 从「输入子图」到「输出 kernel + tiling」串起来，并标注各模块的先后顺序。
3. 看懂 CMake 是如何把这些模块装配成「一个共享库 + 一个 Python 绑定」的，从而建立后续单元逐模块精读的地图。

本讲是**地图课**，只建立全局认知，不深入任何单个模块的算法细节——那些留给 u4～u9。

## 2. 前置知识

阅读本讲前，请确认你已理解上一讲引入的几个概念：

- **两级存储**：全局内存（HBM，大而慢）与片上统一缓冲（UB，小而快）。
- **Memory Bound**：elementwise 算子计算量小、搬运量大，瓶颈卡在内存带宽。
- **自动融合**：把相邻 Vector 算子合并成一个 kernel，消除中间结果的全局搬运。

本讲新增的几个工程概念，先用一句话解释：

- **模块（module）**：Autofuse 用「目录即模块」的方式组织代码，一个子目录通常对应编译流水线里的一个阶段或一种能力。
- **编译流水线（pipeline）**：Autofuse 本质是一个**编译器**——输入是框架送来的计算子图，输出是一段可在昇腾 AI Core 上运行的 C++ kernel 源码和它的 tiling 数据。
- **共享库（shared library）**：大部分 C++ 模块的源码并不各自编译成独立的 `.so`，而是被打包进同一个共享库里。
- **Python 绑定（binding）**：C++ 编译能力通过一个 Python 扩展模块暴露出去，让 PyTorch / TensorFlow 这类框架能调用它。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [autofuse/README.md](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md) | 给出目录结构注释、构建与上板指导、调测环境变量 |
| [autofuse/CMakeLists.txt](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/CMakeLists.txt) | 顶层装配：把各模块 `add_subdirectory` 进来，定义共享库 `aihac_codegen` 并链接、安装 |
| [autofuse/compiler/py_module/pyautofuse.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/py_module/pyautofuse.cpp) | Python 绑定入口，`Autofuser` 类编排「调度 + 代码生成」 |
| [autofuse/codegen/codegen_tiling.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/codegen_tiling.cpp) | codegen 内部触发 ATT 自动 tiling 的衔接点 |

各子模块自己的 `CMakeLists.txt` 也会在源码精读中引用。

## 4. 核心概念与源码讲解

本讲的三个最小模块是：**模块职责划分**、**端到端数据流**、**模块依赖关系**。

### 4.1 模块职责划分

#### 4.1.1 概念说明

Autofuse 的源码组织有一个很直观的规律：**目录即模块，模块即编译流水线的一个阶段**。README 在开头就给出了一张带注释的目录树，每个目录名旁边的一句话就是它的职责。

需要特别区分两类目录：

- **六大主线模块**：`graph_metadef`、`ascir`、`optimize`、`att`、`codegen`、`compiler`。它们沿着「图输入 → 注册 → 调度 → tiling → 代码生成 → 对外接口」这条主线排开，正是本讲标题里的「六大模块」。
- **辅助目录**：`ascendc`、`inc`、`common`、`v35`、`examples`、`cmake`、`scripts` 等。它们为六大模块提供算子能力头文件、GE 集成接口、通用工具、平台扩展或工程脚本，本身不在主线数据流上单独占一个阶段。

#### 4.1.2 核心流程

下表把 README 的目录注释整理成「目录 / 职责 / 所属类别」三列：

| 目录 | README 注释 | 类别 | 一句话补充 |
|------|-------------|------|-----------|
| `graph_metadef` | 基本图接口 | 主线 | 提供图 IR（ComputeGraph/Node/OpDesc 等），是承载子图的「容器」 |
| `ascir` | 算子注册 ascir | 主线 | 定义 ASCIR 类型并把算子登记进融合体系 |
| `optimize` | 调度切分 模块 | 主线 | 图改写 pass + 调度 + 任务切分 + buffer 分配 |
| `att` | 自动 tiling 生成 模块 | 主线 | 性能建模 + tiling 表达式 + 求解 + tiling 代码生成 |
| `codegen` | kernel 代码生成 模块 | 主线 | 生成 kernel 的 C++ 源码与 tiling 数据 |
| `compiler` | 对外API 接口 | 主线 | Python 绑定 `pyautofuse` + 编排脚本 |
| `ascendc` | ascendc api 定义 | 辅助 | 算子能力头文件，供 codegen 调用 |
| `inc` | 供 GE 调用接口 | 辅助 | 与图引擎（GE）集成的 C++ 接口 |
| `common` | 通用工具方法 | 辅助 | 跨模块复用的工具 |
| `v35` | 昇腾950 芯片相关优化 | 辅助 | 针对特定平台的专属扩展 |

#### 4.1.3 源码精读

**目录树直接来自 README**，六大主线模块的职责注释一目了然：

```text
autofuse/
├── ascendc                # ascendc api 定义
├── ascir                  # 算子注册 ascir
├── att                    # 自动 tiling 生成 模块
├── codegen                # kernel 代码生成 模块
├── compiler               # 对外API 接口
├── graph_metadef          # 基本图接口
├── optimize               # 调度切分 模块
...
```

参见 [autofuse/README.md:9-27](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L9-L27)（完整目录树）。

**CMake 的装配顺序**印证了主线模块的相对位置。顶层 `CMakeLists.txt` 用一连串 `add_subdirectory` 把子目录装进来，其中主线模块的装配顺序为：

```cmake
add_subdirectory(graph_metadef/graph)   # 图 IR
add_subdirectory(ascendc)               # 算子能力头
add_subdirectory(ascir)                 # 算子注册
add_subdirectory(optimize)              # 调度切分
add_subdirectory(att)                   # 自动 tiling
add_subdirectory(codegen)               # 代码生成
...
add_subdirectory(compiler)              # 对外接口（非测试模式）
```

参见 [autofuse/CMakeLists.txt:137-151](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/CMakeLists.txt#L137-L151)。注意 `compiler` 受 `RUN_TEST` 开关保护——测试模式下不装配它（[CMakeLists.txt:149-151](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/CMakeLists.txt#L149-L151)），`v35` 则用 `IS_DIRECTORY` 做存在性检查后才装配（[CMakeLists.txt:146-148](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/CMakeLists.txt#L146-L148)）。

#### 4.1.4 代码实践

1. **实践目标**：建立「目录注释 ↔ CMake 装配」的双向确认能力。
2. **操作步骤**：
   - 打开 [autofuse/README.md:9-27](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L9-L27)，数一下目录树里列出了多少个子目录。
   - 打开 [autofuse/CMakeLists.txt:137-151](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/CMakeLists.txt#L137-L151)，数一下有多少个 `add_subdirectory`。
3. **需要观察的现象**：两者数量并不完全相等——README 里的 `examples`、`cmake`、`scripts` 等辅助目录不出现在 `add_subdirectory` 列表中，因为它们不产生编译产物。
4. **预期结果**：能列出至少 3 个「README 里有、但 CMake 不装配」的目录，并解释原因（它们是文档/示例/脚本，不参与编译）。
5. 运行结果：**待本地验证**（本实践为源码阅读型，无需编译）。

#### 4.1.5 小练习与答案

**练习 1**：`v35` 目录在 README 注释里写的是什么？它属于主线还是辅助？

> **答案**：注释是「昇腾950 芯片相关优化」，属于辅助目录（平台扩展），不在六大主线模块之列。

**练习 2**：六大主线模块里，哪一个负责「把算子登记进融合体系」？

> **答案**：`ascir`（注释为「算子注册 ascir」）。

### 4.2 端到端数据流

#### 4.2.1 概念说明

把 Autofuse 当成一个**编译器**来看，它有明确的输入和输出：

- **输入**：框架（如 PyTorch）通过 `torch.compile(options={"npu_backend":"ascendc"})` 送来的一个待融合**计算子图**。
- **输出**：一段 C++ kernel 源码 + 一段 tiling 数据（运行时根据实际 shape 选 tiling 的「钥匙」）。

在子图到 kernel 之间，数据要依次流过六大模块。上一讲给出的骨架是：

> graph_metadef → ascir → optimize → att → codegen → compiler

本讲要把它落到真实代码上。这里有一个**重要且容易误解的细节**：在逻辑数据流上 att 排在 codegen 之前；但在代码实现里，是 **codegen 在生成 tiling 部分时反过来调用 att**。我们会在源码精读里指出这个衔接点。

#### 4.2.2 核心流程

用伪代码描述一次完整的 Autofuse 编译（以 Python 侧 `Autofuser.autofuse_backend` 一站式路径为蓝本）：

```text
输入: hint_graph（框架送来的计算子图）

# 1) graph_metadef + ascir: 把子图表示成 Autofuse 内部图
#    （graph_metadef 提供图容器，ascir 提供 ASCIR 类型与算子注册）

# 2) optimize: 调度切分
fused_schedule_result = optimizer.Optimize(hint_graph)
#   内部: 图改写 pass → AutoSchedule 轴分组/tiling case → 任务切分 → buffer 分配

# 3) att + codegen: 生成 tiling 与 kernel
codegen.GenerateForInductor(fused_schedule_result)
#   内部: codegen 生成 kernel 源码;
#         生成 tiling 时调用 att::GenTilingImplAutoFuseV3 得到 tiling 函数源码

# 4) compiler: 把源码交给下游（host/device 编译、打包）
输出: autofused_算子名 kernel + tiling 数据
```

把数据流画成一行：

```
子图 ──▶ [graph_metadef 装载] ──▶ [ascir 类型/注册] ──▶ [optimize 调度] 
      ──▶ [codegen 生成 kernel] ──▶(内部调用)──▶ [att 生成 tiling] 
      ──▶ [compiler 编排/输出] ──▶ autofused kernel
```

#### 4.2.3 源码精读

**入口类 `Autofuser`**：Python 绑定把编排能力封进 `Autofuser` 类，它内部同时持有一个调度器和一个代码生成器：

```cpp
class Autofuser {
 public:
  struct Object {
    PyObject_HEAD
        optimize::Optimizer *optimizer;
    codegen::Codegen *codegen;
  };
  ...
  static PyObject *AutofuseBackend(PyObject *self, ...);
  static PyObject *Schedule(PyObject *self, ...);
  static PyObject *Codegen(PyObject *self, ...);
};
```

参见 [autofuse/compiler/py_module/pyautofuse.cpp:123-142](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/py_module/pyautofuse.cpp#L123-L142)。三个对外方法对应三种用法：`autofuse_backend`（一站式）、`schedule`（只调度）、`codegen`（只生成），方法表见 [pyautofuse.cpp:144-149](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/py_module/pyautofuse.cpp#L144-L149)。

**调度阶段 = `optimize::Optimizer::Optimize`**：`schedule` 路径接收一个 hint 图，调用 `Optimize` 产出 `fused_schedule_result`：

```cpp
auto ret = self->optimizer->Optimize(hint_compute_graph->compute_graph,
                                     fused_schedule_result->fused_schedule_result);
```

参见 [autofuse/compiler/py_module/pyautofuse.cpp:392](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/py_module/pyautofuse.cpp#L392)。注意入参是 `compute_graph`（graph_metadef 的图），出参是 `fused_schedule_result`（ascir 的调度结果类型）——这一行就体现了 graph_metadef → ascir → optimize 的衔接。

**代码生成阶段 = `codegen::Codegen::Generate*`**：拿到调度结果后，codegen 生成 kernel 源码与 tiling 数据：

```cpp
std::string tiling_data = self->codegen->GenerateTilingData(fused_schedule_result);
af::Status ret = self->codegen->GenerateKernel(fused_schedule_result, kernel, false);
```

参见 [autofuse/compiler/py_module/pyautofuse.cpp:550-552](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/py_module/pyautofuse.cpp#L550-L552)。

**codegen 反向调用 att 的衔接点**：codegen 的 `TilingLib` 在没有外部自定义 tiling 库时，默认用 ATT 提供的入口生成 tiling 函数：

```cpp
GELOGI("TilingLib using default att api: GenTilingImplAutoFuseV3");
this->codegen_func_ = att::GenTilingImplAutoFuseV3;
```

参见 [autofuse/codegen/codegen_tiling.cpp:438-439](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/codegen_tiling.cpp#L438-L439)。这正是「逻辑上 att 在 codegen 之前、实现上 codegen 调用 att」的落点。

> **说明**：上面引用的 `att::GenTilingImplAutoFuseV3` 是示例性引用，展示调用衔接；ATT 内部的性能建模与求解细节会在第 7 单元（u7）展开。

#### 4.2.4 代码实践

1. **实践目标**：跟踪一条真实的「调度 → 代码生成」调用链，确认数据流顺序。
2. **操作步骤**：
   - 在 [pyautofuse.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/py_module/pyautofuse.cpp) 中找到 `Autofuser` 类，确认它持有 `optimizer` 和 `codegen` 两个成员。
   - 找到 `Schedule` 相关方法（如 `Schedule::ScheduleV2`），确认它先调用 `optimizer->Optimize(...)`。
   - 找到 `CodeGen` 相关方法，确认它随后调用 `codegen->GenerateKernel(...)` / `GenerateTilingData(...)`。
3. **需要观察的现象**：调度在前、代码生成在后；且调度产出 `fused_schedule_result` 正是代码生成的输入。
4. **预期结果**：能写出「`Optimize` 的输出类型 == `GenerateKernel` 的输入类型」这一事实，并指出该类型来自 `ascir` 命名空间。
5. 运行结果：**待本地验证**（本实践为源码阅读型，无需编译运行）。

#### 4.2.5 小练习与答案

**练习 1**：为什么说「att 在 codegen 之前」与「codegen 调用 att」并不矛盾？

> **答案**：前者指**逻辑数据流**——tiling 信息要在 kernel 最终定型前确定；后者指**代码实现**——codegen 在生成 tiling 部分时通过函数指针 `codegen_func_` 调用 `att::GenTilingImplAutoFuseV3`。两者是不同视角。

**练习 2**：`Autofuser` 暴露给 Python 的三个方法分别对应什么用法？

> **答案**：`autofuse_backend`（一站式调度+生成）、`schedule`（只做调度）、`codegen`（只做生成）。分开提供是为了让上层框架可以灵活组合或复用中间结果。

### 4.3 模块依赖关系

#### 4.3.1 概念说明

了解职责和数据流后，还要回答一个工程问题：**这些模块编译成了什么、谁依赖谁？** 这决定了你在改某个模块时要重新编译什么、会不会影响别人。

Autofuse 的依赖架构有一个关键特征：

- **大部分主线模块不单独成库**。`optimize`、`att`、`codegen` 的源码都通过 `target_sources(... PRIVATE ...)` **直接塞进同一个共享库 `aihac_codegen`**。换句话说，它们在编译产物层面是「一体的」。
- **少数模块是独立库，再被链接进来**。`graph_metadef` 编出 `graph_af` / `graph_base_af`，`ascir` 编出 `aihac_ir` / `aihac_ir_register`，它们作为 `aihac_codegen` 的链接依赖存在。
- **Python 绑定单独成库**。`compiler` 编出 `pyautofuse`，安装到 Python 的 `site-packages/autofuse` 下。

#### 4.3.2 核心流程

把依赖关系按「编译期装配 → 链接 → 安装」三步看：

```text
编译期:
  optimize/*.cpp  ┐
  att/*.cpp       ├──▶ target_sources(aihac_codegen)   ← 源码合流
  codegen/*.cpp   ┘

  graph_metadef   ──▶ graph_af, graph_base_af          ← 独立库
  ascir           ──▶ aihac_ir, aihac_ir_register      ← 独立库

链接期:
  aihac_codegen  ──links──▶ graph_af, graph_base_af,
                             aihac_ir, aihac_ir_register, ...

安装期:
  aihac_codegen, aihac_ir, graph_af ... ──▶ <arch>-linux/lib64/
  pyautofuse + *.py                      ──▶ python/site-packages/autofuse/
```

#### 4.3.3 源码精读

**主线模块源码合流进 `aihac_codegen`**：以 `optimize` 为例，它的 `CMakeLists.txt` 不 `add_library`，而是把所有 `.cpp` 加到既有的 `aihac_codegen` 目标上：

```cmake
file(GLOB_RECURSE SOURCES "${OPTIMIZE_DIR}/*.cpp")
target_sources(aihac_codegen PRIVATE ${SOURCES})
```

参见 [autofuse/optimize/CMakeLists.txt:2-6](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/CMakeLists.txt#L2-L6)。`att`（[att/CMakeLists.txt:26-29](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/CMakeLists.txt#L26-L29)）和 `codegen`（[codegen/CMakeLists.txt:1-6](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/CMakeLists.txt#L1-L6)）采用完全相同的写法。

**`aihac_codegen` 本身是顶层定义的共享库**：

```cmake
add_library(aihac_codegen SHARED)
```

参见 [autofuse/CMakeLists.txt:135](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/CMakeLists.txt#L135)。

**独立库被链接进来**：顶层把 `graph_af`、`graph_base_af`、`aihac_ir`、`aihac_ir_register` 等作为 `aihac_codegen` 的公开链接依赖：

```cmake
target_link_libraries(aihac_codegen PUBLIC
        ...
        graph_af
        graph_base_af
        aihac_ir
        aihac_ir_register
        ...)
```

参见 [autofuse/CMakeLists.txt:181-202](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/CMakeLists.txt#L181-L202)。而 `ascir` 自己确实是一个独立共享库（`add_library(ascir SHARED ...)`），见 [autofuse/ascir/meta/CMakeLists.txt:1-4](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/meta/CMakeLists.txt#L1-L4)。

**两种安装去向**：C++ 库装进架构相关目录，Python 绑定与脚本装进 `site-packages`：

```cmake
install(TARGETS pyautofuse
    LIBRARY DESTINATION ${AUTOFUSE_INSTALL_PYTHON_DIR}/autofuse ...)
install(FILES
        compiler/python/compile_adapter.py
        compiler/python/ascendc_compile.py
        ...
    DESTINATION ${AUTOFUSE_INSTALL_PYTHON_DIR}/autofuse ...)
```

参见 [autofuse/CMakeLists.txt:233-249](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/CMakeLists.txt#L233-L249)。这解释了上一讲（u1-l4）为什么安装 `.run` 包后，运行时才能加载到新的 Autofuse 能力。

> **说明**：`v35` 子目录的依赖关系较特殊——它内部又 `add_subdirectory` 了一份 ascir/codegen/optimize/att 的平台专属版本（见 [v35/CMakeLists.txt:1-7](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/v35/CMakeLists.txt#L1-L7)），细节留待第 11 单元（u11）。

#### 4.3.4 代码实践

1. **实践目标**：判断一段源码最终进入哪个编译产物。
2. **操作步骤**：
   - 任意挑一个 `autofuse/optimize/` 下的 `.cpp` 文件，沿它的目录回溯到 [optimize/CMakeLists.txt:2-6](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/CMakeLists.txt#L2-L6)，确认它被 `GLOB_RECURSE` 收进 `aihac_codegen`。
   - 再挑一个 `autofuse/ascir/` 下的源文件，回溯到 [ascir/meta/CMakeLists.txt:1-4](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/meta/CMakeLists.txt#L1-L4)，确认它进入的是独立库 `ascir`。
3. **需要观察的现象**：同样是「Autofuse 的源码」，optimize 的改动只影响 `aihac_codegen` 这一个 `.so`；而 ascir 的改动会先动 `aihac_ir` 等独立库，再通过链接关系影响 `aihac_codegen`。
4. **预期结果**：能说清「optimize 是源码合流、ascir 是独立库链接」这两种依赖模式的区别，并指出改哪类模块的「编译影响面」更小。
5. 运行结果：**待本地验证**（本实践为源码阅读型，无需编译）。

#### 4.3.5 小练习与答案

**练习 1**：如果你只改了 `optimize/` 里的一个文件，会不会影响 `graph_af` 这个库？

> **答案**：不会。`optimize` 的源码通过 `target_sources` 进入 `aihac_codegen`，与 `graph_af` 是不同的编译目标；改 optimize 只会触发 `aihac_codegen` 重新编译/链接。

**练习 2**：`pyautofuse` 安装到哪个目录？为什么它和 C++ 库不装在一起？

> **答案**：装到 `python/site-packages/autofuse`（见 [CMakeLists.txt:233-249](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/CMakeLists.txt#L233-L249)）。因为它是给 Python 框架 import 的扩展模块，必须落在 Python 的包搜索路径下；而 C++ 库装在架构相关的 `lib64` 下供运行时动态加载。

## 5. 综合实践

**任务**：画出 Autofuse 的端到端数据流图，作为你后续精读源码的「总索引」。

要求：

1. 左边是输入（框架送来的计算子图），右边是输出（`autofused_` kernel + tiling 数据）。
2. 中间按先后顺序标出六大主线模块：`graph_metadef` → `ascir` → `optimize` → `att` → `codegen` → `compiler`。
3. 在 `optimize` 与 `codegen` 之间标注它们交换的核心数据结构 `fused_schedule_result`（来自 ascir 命名空间）。
4. 在 `codegen` 与 `att` 之间画一条反向箭头，注明「codegen 通过 `att::GenTilingImplAutoFuseV3` 调用 att」。
5. 在图下方用一句话注明编译产物：`aihac_codegen`（共享库）与 `pyautofuse`（Python 绑定）。

**参考画法**（文字版流程图）：

```
计算子图
   │
   ▼
[graph_metadef] 图IR容器 ──▶ [ascir] ASCIR类型/算子注册
   │
   ▼  (compute_graph)
[optimize] 调度切分 ──产出──▶ fused_schedule_result
   │
   ▼
[codegen] kernel代码生成 ◀──调用── [att] 自动tiling
   │                  └─(att::GenTilingImplAutoFuseV3)─┘
   ▼
[compiler] pyautofuse编排
   │
   ▼
autofused kernel + tiling 数据

编译产物: aihac_codegen(.so) + pyautofuse(Python扩展)
```

完成后，请对照本讲 4.2.2 的伪代码自行核对模块顺序与衔接点是否一致。

## 6. 本讲小结

- Autofuse 用「目录即模块」组织代码，**六大主线模块** `graph_metadef / ascir / optimize / att / codegen / compiler` 沿数据流排开，另有 `ascendc / inc / common / v35` 等辅助目录。
- 端到端数据流为：**子图 → graph_metadef 装载 → ascir 类型/注册 → optimize 调度 → codegen 生成 kernel（内部调用 att 生成 tiling）→ compiler 编排输出**。
- `Autofuser` 是 Python 绑定的编排入口，内部持有 `optimize::Optimizer` 与 `codegen::Codegen`，提供 `autofuse_backend / schedule / codegen` 三种调用粒度。
- 在依赖关系上，`optimize / att / codegen` 的源码**合流进同一个共享库 `aihac_codegen`**；`graph_metadef`、`ascir` 是独立库再被链接；`pyautofuse` 单独装进 Python 的 `site-packages`。
- 一个关键细节：逻辑上 att 在 codegen 之前，实现上是 **codegen 通过 `att::GenTilingImplAutoFuseV3` 反向调用 att**。

## 7. 下一步学习建议

本讲建立的是「地图」，接下来就该逐个模块「下钻」。建议按数据流顺序学习：

1. **先进入图的底层表示**：第 4 单元（u4）讲 `graph_metadef` 的 ComputeGraph/Node/OpDesc 核心 IR，这是所有后续模块操作的「数据载体」。
2. **再看算子如何登记**：第 5 单元（u5）讲 `ascir` 的算子注册机制，理解一个算子从注册到被 codegen 可见要走哪些环节。
3. **随后沿主线推进**：第 6 单元（u6，optimize）、第 7 单元（u7，att）、第 8 单元（u8，codegen）、第 9 单元（u9，compiler）依次精读。
4. 如果你想先看到「效果」再回头读源码，可以先跳到第 3 单元第 3 讲（u3-l3）的框架使能与 DFX 调测，跑一个 `autofused_` 产物出来，再带着产物回到本讲的地图里定位每个文件的作用。
