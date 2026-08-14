# u9-l1 pyautofuse 绑定与编译编排

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `pyautofuse.cpp` 用原生 CPython C API 暴露了哪三类核心对象（`Autofuser`、`Schedule`、`CodeGen`，外加配置用的 `AutofuserOptions`），以及每个对象的方法如何映射到 u6/u8 讲过的 `Optimizer` 与 `Codegen` C++ 类。
2. 掌握 `compile_adapter.py` 如何把一次编译拆成 host（tiling 函数）与 device（kernel）两个 stage，并理解 Inductor PGO 的旁路（sidecar）产物。
3. 理解本次新增的 `ascir_api.py` 全局图元数据管理模块：`GraphMetadata` 如何维护算子计数器与 data/output 索引，让 Python 侧能用函数式风格搭图。
4. 了解 `ascendc_compile.py` 在整个链路末端的衔接作用：调用昇腾毕昇编译器产出 `.so`，以及 CV tiling wrapper 共享缓存的落点。

本讲是「Compiler 对外接口」单元的第一讲，把 u3-l2 建立的 `compiler` 模块地图展开成可调用、可追踪的真实代码。

## 2. 前置知识

- **pybind / 原生 Python C 扩展**：让 C++ 代码可以被 `import` 的机制。本项目没有用第三方 pybind11，而是直接使用 CPython 的 `PyTypeObject`、`PyMethodDef` 等 C API 手写绑定——每个 Python 类对应一个 C++ 类，类里持有裸指针（如 `optimize::Optimizer *`），`tp_init` 里 new、`tp_dealloc` 里 delete。
- **host 代码与 device 代码**：昇腾算子工程里，「host 代码」指运行在 CPU 侧的 tiling 函数（运行期根据真实 shape 计算切分参数），「device 代码」指运行在 AI Core 上的 kernel 实现。两者编译选项、头文件、产物形态都不同，所以必须拆开编译。
- **CV 融合**：cube（矩阵乘）与 vector 算子融合在同一 kernel 的场景，u8-l2 已讲过其 dtype 感知机制；本讲只关注它在绑定层表现为「一个调度结果生成 ub / common 两套代码」。
- **Inductor PGO**：Profile-Guided Optimization，先用建模候选编译一版，上板实测后回写配置再编译最终版。本讲关注它在 `compile_adapter.py` 里如何多出 `PgoRunner` / `PgoDeviceSource` 两个源文件。
- **`SizeExpr` / `Axis`**：ASCIR 的符号尺寸表达式与轴对象（u4-l2），`ascir_api.py` 的函数签名里大量出现，用于支持动态 shape。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [autofuse/compiler/py_module/pyautofuse.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyautofuse.cpp) | 编译出 `pyautofuse` 扩展模块：暴露 `Autofuser` / `Schedule` / `CodeGen` / `AutofuserOptions` 四个类型 |
| [autofuse/compiler/py_module/pyascir.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyascir.cpp) | 暴露 `ascir` 子模块：`HintGraph`/`FusedGraph`/`SizeExpr`/`Axis` 与 `ascir.ops.*` 全量算子类型 |
| [autofuse/compiler/py_module/pyascir.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyascir.h) | 定义 `REGISTERED_OPS` 算子名宏清单，是 Python 侧可见算子的「总花名册」 |
| [autofuse/compiler/python/ascir_api.py](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascir_api.py) | 本次新增：函数式建图 API，内部用 `GraphMetadata` 管理算子计数与 IO 索引 |
| [autofuse/compiler/python/compile_adapter.py](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/compile_adapter.py) | 编译编排：host/device stage 拆分、源码切分落盘、调 `ascendc_compile` |
| [autofuse/compiler/python/ascendc_compile.py](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py) | 最终编译器驱动：调毕昇编译器编 host/device 目标文件并链接 `.so` |
| [autofuse/tests/st/python/test_python_ascir.py](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/st/python/test_python_ascir.py) | `ascir_api` 的端到端用例，是本讲实践的素材 |

## 4. 核心概念与源码讲解

### 4.1 pybind 绑定的三类对象

#### 4.1.1 概念说明

Autofuse 的全部 C++ 能力（优化、调度、代码生成）都要经过一个 Python 扩展模块才能被 torch_npu 的 AscendC 后端（u3-l3）调用。这个模块就是 `pyautofuse`。它对外暴露三类**职责不同**的对象：

| 类型 | 持有的 C++ 对象 | 职责 | 对应讲义 |
| --- | --- | --- | --- |
| `Autofuser`（配 `AutofuserOptions`） | `Optimizer` + `Codegen` | 一站式：一次调用完成调度 + 代码生成 | u6-l1、u8-l1 |
| `Schedule` | `Optimizer` | 只做调度，返回 `FusedScheduledResult` | u6-l1 |
| `CodeGen` | `Codegen` | 只做代码生成，且拆成 host/device/PGO 多个入口 | u8-l1、u8-l2 |

这个拆分印证了 u3-l2 的结论：「Autofuser 提供 autofuse_backend、schedule、codegen 三种调用粒度」。

#### 4.1.2 核心流程

以 `Autofuser.autofuse_backend(graph)` 为例：

```text
Python 调用 autofuse_backend(hint_graph 或 fused_graph)
  ├─ 判断入参类型：HintGraph → 先 AssignDefaultIoIndex 补 IO 编号
  │                 FusedGraph → 直接用
  ├─ optimizer->Optimize(graph, fused_schedule_result)   # u6 整条流水线
  └─ codegen->GenerateForInductor(fused_schedule_result, result)
       └─ 返回三元组 (tiling_data, tiling, kernel) 三个源码字符串
```

`Autofuser.schedule()` 与 `Autofuser.codegen()` 是同一流程的两半，中间产物 `FusedScheduledResult`（Python 对象）可以在两侧之间传递、甚至跨进程保存。

#### 4.1.3 源码精读

**类型定义与方法表**。`Autofuser` 的 C++ 对象同时持有 `Optimizer` 与 `Codegen`，方法表登记了三个 Python 方法：

- [autofuse/compiler/py_module/pyautofuse.cpp:123-149](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyautofuse.cpp#L123-L149)：`Autofuser` 类声明与方法表——`autofuse_backend` / `schedule` / `codegen` 三个 METH_VARARGS 方法，`Object` 结构体内存放 `optimizer` 与 `codegen` 两个裸指针。
- [autofuse/compiler/py_module/pyautofuse.cpp:174-186](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyautofuse.cpp#L174-L186)：`tp_init` 里 `PyArg_ParseTuple(args, "O!", &AutofuserOptions::type, ...)` 强制构造参数必须是 `AutofuserOptions`，然后用它 new 出两个 C++ 对象——这是「配置对象 → 工作对象」的组装点。
- [autofuse/compiler/py_module/pyautofuse.cpp:89-121](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyautofuse.cpp#L89-L121)：`AutofuserOptions::Init` 从关键字参数读取 `tiling_lib_path`、`tiling_lib_codegen_symbol`（给 CodegenOptions）与 `graph_type`（给 OptimizerOptions），说明 Python 侧可配置项就这三类。

**一次调用的完整链路**（`autofuse_backend`）：

- [autofuse/compiler/py_module/pyautofuse.cpp:253-295](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyautofuse.cpp#L253-L295)：按 `PyObject_IsInstance` 分派 `HintGraph` / `FusedGraph` 两条支路，分别调 `Optimizer::Optimize` 的两个重载（u6-l1 讲过的重载 A/B），随后 `GenerateForInductor` 生成代码，最后 `Py_BuildValue("sss", tiling_data, tiling, kernel)` 把三个源码字符串打包返回。
- [autofuse/compiler/py_module/pyautofuse.cpp:25-39](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyautofuse.cpp#L25-L39)：`AssignDefaultIoIndex` 遍历图中 `Data` / `Output` 节点按出现顺序补 `SetIndex`——请记住这个行为，4.3 节会看到 `ascir_api.py` 在 Python 侧做了**同一件事**，两边语义对齐。

**Schedule 与 CodeGen 两类对象**：

- [autofuse/compiler/py_module/pyautofuse.cpp:314-317](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyautofuse.cpp#L314-L317)：`Schedule` 暴露 `schedule`（吃 `HintGraph`，映射 `Optimize` 重载 B）与 `scheduleV2`（吃 `HintComputeGraph`，即 GE 序列化计算图，映射重载 A）——与 u6-l1 的结论「scheduleV2 映射重载 A、schedule 映射重载 B」一一对应。
- [autofuse/compiler/py_module/pyautofuse.cpp:443-452](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyautofuse.cpp#L443-L452)：`CodeGen` 暴露四个方法：`device_code_generator`（生成 device kernel 与 TilingData 结构）、`host_code_generator`（生成 host tiling 函数与 infer_shape）、`get_kernel_and_json_generator`（生成取 kernel 二进制的胶水代码）、`pgo_code_generator`（生成 PGO 采样代码）。相比「一个 Generate 走天下」的 `Autofuser`，这里按 host/device/PGO 拆得更细，正是 compile_adapter 三种编译入口的源头。

**CV 融合在绑定层的形态**：

- [autofuse/compiler/py_module/pyautofuse.cpp:661-714](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyautofuse.cpp#L661-L714)：`device_code_generator` 用 `IsCubeFusedScheduled` 判断是否 CV 融合：是则走 `HandleDeviceCodeGenForCVFusion`（把调度结果过滤成 ub / common 两份分别生成，装进 `tiling_dict["ub"|"common"]` 与 `kernel_dict["ub"|"common"]`），否则走 Non-CV 分支只填 `"default"` 键。返回值从三元组字符串升级为**两个嵌套字典**，这是下游 host/device 分开编译的基础。
- [autofuse/compiler/py_module/pyautofuse.cpp:716-762](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyautofuse.cpp#L716-L762)：`GenerateHostCodeResult` 同样按 CV/非 CV 分支调 `GenerateTiling`，产出 `{模板类型: {文件名: 内容}}` 双层字典，并额外生成 `infer_shape` 源码。

**模块初始化与导入路径**：

- [autofuse/compiler/py_module/pyautofuse.cpp:903-958](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyautofuse.cpp#L903-L958)：`PyInit_pyautofuse` 把四个类型挂到模块上，并做了两个关键注册：把内嵌的 `ascir` 模块同时登记为 `sys.modules["ascir"]`（让 `from ascir import Max` 直接可用）和 `sys.modules["autofuse.pyautofuse"]`。
- [autofuse/compiler/py_module/pyautofuse.cpp:961-969](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyautofuse.cpp#L961-L969)：`PyInit_autofuse_pyautofuse` 与 `PyInit_autofuse` 两个别名入口，兼容 `import autofuse.pyautofuse` 与 `autofuse.so` 软链两种安装形态。

**pyascir 侧的算子花名册**（本次增量更新点）：

- [autofuse/compiler/py_module/pyascir.cpp:1530-1534](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyascir.cpp#L1530-L1534)：`kOpsOperators` 用 `REGISTERED_OPS` 宏 + `OP(NAME)` 展开为每个算子创建 `OpsOperatorTypeObject`，再逐个挂到 `ascir.ops` 子模块。
- [autofuse/compiler/py_module/pyascir.h:154-183](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyascir.h#L154-L183)：`REGISTERED_OPS` 清单本次新增了 `I0` / `I0e` / `I1e`、`LogNdtr` / `NextAfter` / `PolyGamma`、`ChebyshevPolynomialT/U/V/W`、`HermitePolynomialH/He`——与 u11-l5 将讲的 v2 特殊函数算子注册链路是同一次改动的两端。
- [autofuse/compiler/py_module/pyascir.cpp:1466-1475](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyascir.cpp#L1466-L1475)：`ascir.utils` 子模块提供 `debug_str` / `dump` / `deserialize` / `duration_record` / `report_durations` / `set_platform` 六个工具函数——compile_adapter 的编译耗时打点正是消费其中的 `duration_record`。

#### 4.1.4 代码实践

**实践目标**：不写代码，靠「方法表对照」吃透三类对象的边界。

**操作步骤**：

1. 打开 [autofuse/compiler/py_module/pyautofuse.cpp:144-149](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyautofuse.cpp#L144-L149)、[314-317](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyautofuse.cpp#L314-L317)、[443-452](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyautofuse.cpp#L443-L452) 三处方法表。
2. 画一张三列对照表：Python 方法名 → 内部调用的 C++ 方法 → 产物形态（字符串三元组 / `FusedScheduledResult` 对象 / 字典）。
3. 回答：为什么 `Autofuser.codegen` 返回 `"sss"` 三元组，而 `CodeGen.device_code_generator` 返回 `"OO"` 两个字典？（提示：前者走 `GenerateForInductor` 的旧路径，后者为 CV 融合多模板与 host/device 拆分编译服务。）

**需要观察的现象**：三类对象没有一个是「全能」的——`Schedule` 根本碰不到 codegen 指针，`CodeGen` 也拿不到 optimizer，编译期类型系统天然防止越权调用。

**预期结果**：得到一张 9 个 Python 方法（3+2+4）的总表，且能说出每个方法对应 u6/u8 的哪条 C++ 链路。

#### 4.1.5 小练习与答案

**练习 1**：`AutofuserOptions` 支持哪些关键字参数？分别注入哪个 C++ Options？

答案：`tiling_lib_path` 与 `tiling_lib_codegen_symbol` 注入 `codegen::CodegenOptions`，`graph_type` 注入 `optimize::OptimizerOptions`（见 `AutofuserOptions::Init`，[pyautofuse.cpp:96-117](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyautofuse.cpp#L96-L117)）。

**练习 2**：`Schedule.schedule` 与 `Schedule.scheduleV2` 的入参类型有何不同？为什么需要两个版本？

答案：`schedule` 只接受 `HintGraph`（单张带调度语义的图，映射 `Optimize` 重载 B）；`scheduleV2` 接受 `HintComputeGraph`（含序列化 `AscGraph` 子图节点的 GE 计算图，映射重载 A，需要先反序列化/摊平）。GE 来源的图走 V2，Inductor 侧直接搭的 hint 图走 V1。

**练习 3**：`Autofuser::Codegen` 失败时除了返回 Python 异常还做了什么？

答案：用 `DumpGraphGuard` 在析构时调 `AscGraphDumperContext::DumpWatchedGraphs()` 把被监视的融合图落盘（u3-l3 讲过的 DumpGraph 机制），成功路径则 `DumpGraphGuard::ReInit()` 清空监视列表避免误 dump（[pyautofuse.cpp:230-251](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/py_module/pyautofuse.cpp#L230-L251)）。

### 4.2 host/device 编译编排（compile_adapter.py）

#### 4.2.1 概念说明

`CodeGen` 生成的是**源码字符串**，不是可加载的 `.so`。从源码到能被 torch_npu 加载的算子库，中间需要：落盘源文件 → 选择编译 stage → 调编译器 → 拷回产物。`compile_adapter.py` 就是这段胶水代码，它提供三个入口，对应三种使用方式：

| 入口 | stage | 编译内容 |
| --- | --- | --- |
| `jit_compile(tiling_def, host_tiling, op_kernel, argv)` | `all` | host + device 一次编完（GE 路径 / 单算子 UT 用） |
| `host_compile(tiling_def, host_tiling, argv)` | `host` | 只编 tiling 函数（Inductor 先编 host 探路） |
| `kernel_compile(tiling_def, kernel, argv)` | `device` | 只编 kernel（host 已定，补 device） |

host 与 device 拆开的价值：host tiling 函数只依赖 shape 推导逻辑、可用 CPU 编译器快速迭代；device kernel 必须用毕昇编译器面向特定 `soc_version` 编译，耗时长。分开后 Inductor 可以缓存住不变的一侧。

#### 4.2.2 核心流程

`compile_core` 是三个入口共用的骨架：

```text
compile_core(sources, argv, stage)
  ├─ prepare_compile_context：解析 argv；stage=host 时补 -D_GLIBCXX_USE_CXX11_ABI=1；
  │   无 output_path 且未开 debug → 用 TemporaryDirectory 自动清理
  ├─ execute_compile
  │   ├─ stage ∈ {all, host} → write_compile_host_sources
  │   │     ├─ 无切分标记 → 整体写 {graph}_tiling_func.cpp
  │   │     ├─ 有切分标记 → 按 SPLIT 标记拆成多个 .cpp + 原子头
  │   │     └─ 若含 PgoRunner/PgoDeviceSource → 额外产出 PGO 旁路文件
  │   ├─ stage ∈ {all, device} → 写 {graph}_op_kernel.cpp
  │   └─ ascendc_compile.main(args)   # 真正编译
  └─ finally：记录总耗时并 report_durations()
```

host 源码切分是本模块的精华：ATT 生成的 host 源码里内嵌 `// AUTOFUSE_SPLIT_FILE_BEGIN:<Key>` / `END` 标记对，adapter 把它们解析成「一个公共头 + 多个原子 .cpp」（state/log/pgo/base/solver/api/entry/tail 等，正是 u7-l3 讲过的 tiling 产物五头结构），从而支持**按内容缓存**——只有内容变化的翻译单元需要重编。

#### 4.2.3 源码精读

- [autofuse/compiler/python/compile_adapter.py:23-48](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/compile_adapter.py#L23-L48)：切分标记常量与 `SPLIT_HEADER_FILES` 映射——`TilingHead` → `autofuse_tiling_func_common.h`、`TilingStateHeader` → `..._state.h` 等，外加 CV 场景的 `ACubeKernelTilingWrapperHpp` → `cube_kernel_tiling_wrapper.h`（u8-l2 讲过的 wrapper 精简为纯接口的落点）。
- [autofuse/compiler/python/compile_adapter.py:181-227](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/compile_adapter.py#L181-L227)：`parse_split_host_sources` 状态机——逐行找 BEGIN/END 标记，禁止嵌套、禁止 begin/end 不匹配、禁止标记外有内容、禁止 key 重复；`validate_split_key` 还拒绝含 `/`、`..` 的 key，防止路径逃逸。这是「生成器输出必须可校验」的典型防御式解析。
- [autofuse/compiler/python/compile_adapter.py:394-411](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/compile_adapter.py#L394-L411)：`prepare_compile_context`——host stage 强制补 CXX11 ABI 宏（保证与 torch_npu 的 libstdc++ ABI 一致，避免 undefined symbol）；`auto_cleanup` 逻辑决定是否用临时目录（想保留中间产物调试时设 `--output_path` 或开 `AUTOFUSE_DFX_FLAGS` 的 `codegen_compile_debug`）。
- [autofuse/compiler/python/compile_adapter.py:414-439](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/compile_adapter.py#L414-L439)：`write_compile_host_sources`——PGO 旁路的门禁在此：只有 host stage 才允许出现 `PgoRunner`/`PgoDeviceSource`，否则抛 `CompileError`；MSPTI 不可用时打印跳过提示。
- [autofuse/compiler/python/compile_adapter.py:282-305](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/compile_adapter.py#L282-L305)：`write_inductor_pgo_sources`——把 `PgoRunner` 从常规 host 文件里摘出来单独返回，`PgoDeviceSource` 写成 `{graph}_pgo_device.cpp` 放 device 目录，三者（常规 host 文件、runner、device 源）一起返回给 ascendc_compile 组装。
- [autofuse/compiler/python/compile_adapter.py:448-469](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/compile_adapter.py#L448-L469)：`execute_compile`——按 stage 写源码后调 `ascendc_compile.main(args)`，每一步包在 `InductorCompileDuration` 里打点。
- [autofuse/compiler/python/compile_adapter.py:508-546](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/compile_adapter.py#L508-L546)：三个入口 `jit_compile` / `host_compile` / `kernel_compile`——只是用不同的 sources 字典与 stage 调 `compile_core`，`kernel_compile` 额外透传 `tiling_repr`（静态形状改写用的 tiling 表示）。
- [autofuse/compiler/python/compile_adapter.py:357-373](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/compile_adapter.py#L357-L373)：`InductorCompileDuration` 上下文管理器——进入记 `time.time_ns()`，退出调 `ascir.utils.duration_record`，把 `InductorCompile/{stage}/{step}/{graph}` 标签的耗时交给 C++ 侧汇总上报。

#### 4.2.4 代码实践

**实践目标**：追踪一次 `host_compile` 调用的完整数据流，并验证 PGO 旁路行为。

**操作步骤**（源码阅读型 + 可选运行）：

1. 阅读 ST 用例 [autofuse/tests/st/python/test_inductor_pgo_compile_flow.py:94-146](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/st/python/test_inductor_pgo_compile_flow.py#L94-L146)：观察它如何 monkeypatch `ascendc_compile.main` 为假函数、构造带 `PgoRunner`/`PgoDeviceSource` 标记的 host 源码、调 `compile_adapter.host_compile`，最后断言 generation bundle 的发布顺序。
2. 手工模拟：把用例中的 `host_impl_code` 换成只含 `TilingHead` 一个标记的极简源码，在本地 Python 里执行 `parse_split_host_sources(code)`（`AUTOFUSE_DFX_FLAGS` 无关，此函数纯字符串处理，不依赖 NPU 环境），确认返回 `({"TilingHead": ...}, [])` 且因缺 cpp 源而走到 `validate_split_sources` 的报错分支。
3. （可选，需环境）跑 UT：`sh build.sh -u -m autofuse_framework`（测试调度方式见 u12-l1）。

**需要观察的现象**：`parse_split_host_sources` 对「标记外有杂散内容」「begin/end 不配对」都抛带明确文案的 `CompileError`，而不是静默吞掉。

**预期结果**：能画出 `host_compile → compile_core → execute_compile → write_compile_host_sources → ascendc_compile.main` 的调用链，并说出 PGO 三个额外产物（runner、device 源、manifest）在链路上的产生位置。步骤 2 的具体报错文案为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 host stage 要强制加 `-D_GLIBCXX_USE_CXX11_ABI=1`？

答案：torch_npu / PyTorch 官方 wheel 按 CXX11 ABI=1 编译，host tiling 函数最终要跟它们在同一进程里动态链接；ABI 不一致会导致符号找不到（u1-l4 讲过的 undefined symbol 类故障在这里提前拦截）。

**练习 2**：`get_debug_flag()` 从哪里读配置？开了之后行为有何变化？

答案：从环境变量 `AUTOFUSE_DFX_FLAGS` 解析 `codegen_compile_debug=true`（[compile_adapter.py:335-341](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/compile_adapter.py#L335-L341)）；开启后 `auto_cleanup` 为假，中间源码目录不会被 TemporaryDirectory 清掉，便于排查。

**练习 3**：`jit_compile` 与 `host_compile + kernel_compile` 分两次调，产物有何区别？

答案：功能等价（stage `all` vs `host`+`device` 各编一次），区别在编译单元组织与缓存粒度——分开编允许 host 侧复用按内容缓存（PCH、切分后的原子 .cpp），且 PGO 旁路只在 host stage 生成。

### 4.3 ascir_api.py 图元数据管理

#### 4.3.1 概念说明

`ascir_api.py` 是本次增量**新增**的 Python 模块（约 2000+ 行）。它解决的问题：`pyascir` 暴露的 `ascir.ops.Add` 等类型是「裸类型」——直接用时，用户必须自己起唯一的算子名、自己维护 Data/Output 的 index、自己推导 size/strides。`ascir_api` 把这些杂务收拢为模块级函数：

```python
x1 = ascir_api.Data(graph, dtype=ascir.dtypes.float16)
y  = ascir_api.Add(graph, load0, load1, axis=[z0, z1])
ascir_api.Output(graph, y)
```

每个函数内部完成四件事：**生成唯一名 → 实例化算子 → 填调度属性与视图 → dtype 推导**。支撑这一切的是一个按图隔离的全局状态表。

#### 4.3.2 核心流程

```text
_graph_metadata : {graph.name: GraphMetadata}
GraphMetadata:
  op_counters    {"add": 2, "load": 1, ...}   # 每类算子出现了几次
  data_indices   已创建 Data 节点数            # 下一个 Data 的 ir_attr.index
  output_indices 已创建 Output 节点数          # 下一个 Output 的 ir_attr.index
  ops            按创建顺序保存的算子实例列表

ascir_api.Add(graph, x1, x2, axis=...):
  name = f"add_{op_counters['add']}"   # 取号并自增
  op = ascir.ops.Add(name); ops.append(op)
  op.attr.sched.axis = axis; op.x1/x2 = ...
  _infer_or_set_view(op.y, axis, size, stride)   # 三种视图策略
  op.infer_dtype()
  return op.y
```

视图推导规则（`_infer_or_set_view`）：`size` 与 `stride` 都不给 → 按 axis 连续内存推导；只给 `size` → strides 按 size 连乘反推；只给 `stride` 不给 `size` → 直接拒绝（无法唯一确定）。

注意它与 4.1 节 `AssignDefaultIoIndex` 的呼应：C++ 侧在 `Schedule`/`autofuse_backend` 入口按节点顺序**重新**编号 Data/Output，所以 Python 侧的 index 只是初始值，最终以 C++ 侧为准——两层各自维护、语义一致。

#### 4.3.3 源码精读

- [autofuse/compiler/python/ascir_api.py:17-34](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascir_api.py#L17-L34)：全局字典 `_graph_metadata` 与 `GraphMetadata` 类——`__slots__` 限定四个字段，`get_counter(op_type)` 实现「取号并自增」，是唯一命名机制的原子操作。
- [autofuse/compiler/python/ascir_api.py:37-41](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascir_api.py#L37-L41)：`_get_metadata` 以 `graph.name` 为 key 懒初始化——**图的唯一名就是状态边界**，同名图共享计数器。
- [autofuse/compiler/python/ascir_api.py:90-94](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascir_api.py#L90-L94)：`_generate_op_name` 产出 `data_0`、`load_1` 这类名字，保证同图内名字唯一（名字会进入 dump 的 pbtxt 与日志，是调试时对节点的第一抓手）。
- [autofuse/compiler/python/ascir_api.py:97-116](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascir_api.py#L97-L116)：`_common_in_1_out_1_normal_op` 模板函数——`getattr(ascir.ops, op_type)` 动态取类，避免为上百个一进一出算子各写一遍样板代码；同族还有 `_common_in_2_out_1`、`_common_in_3_out_1`、`_common_dynamic_in_1_out_1`（concat 类变长输入）。
- [autofuse/compiler/python/ascir_api.py:69-88](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascir_api.py#L69-L88)：`_infer_or_set_view` 的三分支视图策略与长度校验。
- [autofuse/compiler/python/ascir_api.py:188-203](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascir_api.py#L188-L203)：`Data`——`meta.data_indices` 先取值再自增并写入 `attr.ir_attr.index`，这就是「data 索引」的维护现场；`Output` 同理用 `output_indices`（[245-262](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascir_api.py#L245-L262)）。运行期框架按这两个 index 把 kernel 参数与图节点一一对应。
- [autofuse/compiler/python/ascir_api.py:1014-1037](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascir_api.py#L1014-L1037)：`_shifted_chebyshev_polynomial_op`——本次新增的 Chebyshev 家族共用模板，内部用字符串处理把驼峰类名转蛇形算子名（`ChebyshevPolynomialT` → `chebyshev_polynomial_t`），并额外写 `attr.ir_attr.n`（多项式阶数）。`HermitePolynomialH/He`、`PolyGamma`、`I0/I0e/I1e`、`LogNdtr`、`NextAfter` 等新算子包装（[967](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascir_api.py#L967)、[1200-1239](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascir_api.py#L1200-L1239)、[2238-2274](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascir_api.py#L2238-L2274)）与 pyascir.h 的 `REGISTERED_OPS` 新增项一一配对——**C++ 花名册加一行、Python 包装加一个函数，两处必须同步**。
- [autofuse/tests/st/python/test_python_ascir.py:1859-1876](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/st/python/test_python_ascir.py#L1859-L1876)：真实用例——`Data → Load → Add → Store ×2 → Output ×2` 的完整搭图序列，可作为 `ascir_api` 用法的权威样例。

#### 4.3.4 代码实践

**实践目标**：验证 `GraphMetadata` 的计数与索引维护逻辑。

**操作步骤**（纯 Python，无需 NPU 环境，但需要已安装 `.run` 包使 `autofuse` 可导入；无环境时改为纯阅读）：

1. 写如下最小脚本（**示例代码**，非仓库原有文件）：

   ```python
   from autofuse.pyautofuse import ascir
   from autofuse import ascir_api

   graph = ascir.HintGraph("my_graph")
   z0 = ascir.Axis("z0", 16)
   x1 = ascir_api.Data(graph, dtype=ascir.dtypes.float16)
   x2 = ascir_api.Data(graph, dtype=ascir.dtypes.float16)
   l1 = ascir_api.Load(graph, x1, offset=0, axis=[z0])
   l2 = ascir_api.Load(graph, x2, offset=0, axis=[z0])
   y = ascir_api.Add(graph, l1, l2, axis=[z0])
   ascir_api.Output(graph, y)
   print(ascir_api._graph_metadata["my_graph"].op_counters)
   print(ascir_api._graph_metadata["my_graph"].data_indices,
         ascir_api._graph_metadata["my_graph"].output_indices)
   ```

2. 预判输出后再运行，对照是否一致。
3. 再创建一个 `HintGraph("my_graph")`（同名），直接调 `ascir_api.Add(...)`，观察计数器是否从上次的值继续——体会「以图名为 key」的全局状态行为。

**需要观察的现象**：`op_counters` 应为 `{'data': 2, 'load': 2, 'add': 1, 'output': 1}`，`data_indices` 为 2、`output_indices` 为 1；同名新图复用旧计数器（这既是特性也是隐患：长生命周期进程里图名冲突会串号，所以调用方必须保证图名唯一）。

**预期结果**：输出与预判一致即通过；本机无环境时标注「待本地验证」，改做纯阅读：在 [test_python_ascir.py:1859-1876](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/st/python/test_python_ascir.py#L1859-L1876) 用例里人工数出 `op_counters` 应有的键值。

#### 4.3.5 小练习与答案

**练习 1**：如果两次用同一个 `graph.name` 建图，`_graph_metadata` 会发生什么？有什么风险？

答案：共享同一个 `GraphMetadata`，计数器与 IO 索引延续旧值，导致算子名与 index 错乱。风险在于长驻进程（如 torch.compile 守护进程）中图名冲突会静默串号，因此调用方需保证图名全局唯一，或在重建图前清理对应条目。

**练习 2**：为什么 `_infer_or_set_view` 允许「都不给」和「只给 size」，却拒绝「只给 stride」？

答案：都不给时可按 axis 推连续布局；只给 size 时 stride 有唯一连续解（`_derive_strides` 从最内维 stride=1 反向连乘）；但只给 stride 时 size 有多解（空洞布局下无法反推元素个数），所以必须显式给 size。

**练习 3**：`ascir_api.Data` 写入的 `ir_attr.index` 与 `pyautofuse.cpp` 的 `AssignDefaultIoIndex` 是什么关系？

答案：Python 侧在建图时按创建顺序先写一版 index；C++ 侧在 `schedule` / `autofuse_backend` 入口对 `Data`/`Output` 节点再按图中顺序重写一版。两层各自独立实现同一语义，保证无论图从 Python 直建还是反序列化而来，进入 Optimizer 前 index 都是规范化的。

### 4.4 ascendc_compile 衔接

#### 4.4.1 概念说明

`compile_adapter` 落盘源码后，剩下「调编译器、链接、拷产物」的脏活全在 `ascendc_compile.py`。它是 Autofuse 与 CANN 工具链的衔接点：

- 编译器用 CANN 自带的毕昇编译器（`$ASCEND_PATH/tools/bisheng_compiler/bin/bisheng`）。
- host 侧有一套并行批量编译 + PCH（预编译头）加速。
- CV 融合场景的 tiling wrapper 编成**按内容寻址的共享 `.so`**，多个图复用同一份 wrapper（u8-l2 讲过的「wrapper 复用编译」在此落地）。
- Inductor PGO 场景额外链接可执行的 runner 与 device 采样源，产出带 generation 号的 bundle。

#### 4.4.2 核心流程

`main(args)` 按 stage 三分支：

```text
stage == "host"   → host_compile_batch(PCH) → build_host_output      → 只出 host .so
stage == "device" → link_kernel_target(None, ...)                    → 只出 kernel .so
stage == "all"    → host_compile_batch + compile_host_objs
                  → link_kernel_target(host_obj_paths) → copy_so_to_output
```

CV wrapper 共享缓存的关键设计：以「源文件内容 sha256 + ASCEND_PATH + machine + soc_version + compile_options + stage」为 cache key 生成 `.so` 文件名；编译时先写临时文件再 `os.replace` 原子替换，跨进程用 `fcntl.flock` 文件锁互斥——内容不变则零重编。

#### 4.4.3 源码精读

- [autofuse/compiler/python/ascendc_compile.py:1287-1305](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L1287-L1305)：`main`——先 `os.chdir(args.temp_dir)` 把工作目录切到编译目录（源码里的相对 include 才能解析），按 stage 三分支，最后 `copy_so_to_output` 把 `.so` 拷回调用方目录。
- [autofuse/compiler/python/ascendc_compile.py:233-242](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L233-L242)：`get_shared_cv_wrapper_so_path`——把源文件字节、CANN 路径、平台、soc 版本、编译选项、stage 全部喂进 sha256，取前 16 位十六进制做 `.so` 名。任何一个输入变化都会得到新名字，天然免失效。
- [autofuse/compiler/python/ascendc_compile.py:275-289](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L275-L289)：`ensure_shared_cv_wrapper_so`——先查存在性（快路径），不存在则拿独占文件锁、**双检**后再编；`build_shared_cv_wrapper_so`（[257-272](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L257-L272)）先编到带 pid+时间戳的临时 `.so` 再 `os.replace`，保证并发下不会有人加载到半成品。
- [autofuse/compiler/python/ascendc_compile.py:299-313](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L299-L313)：`prepare_shared_cv_wrapper`——非 CV 编译直接原样返回；CV 编译把 host 文件清单按 `is_cv_wrapper_source` 拆成 wrapper 与常规两类，wrapper 只保留第一个去编共享 so，其余文件正常走批量编译。
- [autofuse/compiler/python/ascendc_compile.py:292-296](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L292-L296)：`append_shared_cv_wrapper_so`——链接 kernel 目标时把共享 wrapper `.so` 追加进链接对象列表，配合 soname/rpath 选项（[245-254](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L245-L254)）让最终 `.so` 在运行期能找到它。
- [autofuse/compiler/python/ascendc_compile.py:193-204](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L193-L204)：`link_shared`——所有链接统一走毕昇编译器驱动，`--shared -fPIC`，库搜索路径指向 `$ASCEND_PATH/lib64` 与平台目录。
- [autofuse/compiler/python/ascendc_compile.py:316-319](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L316-L319)：`link_pgo_executable`——PGO runner 链接成**可执行文件**而非 so，并带上 MSPTI 链接选项（采样依赖）；其配置探测逻辑在 compile_adapter 的 `get_inductor_pgo_mspti_config`（[590-596](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/compile_adapter.py#L590-L596)，从 CANN 安装目录的 `tools/mspti` 下找头文件与 `.so`）。

#### 4.4.4 代码实践

**实践目标**：理解「按内容寻址缓存」如何消灭重复编译。

**操作步骤**（源码阅读型）：

1. 读 [autofuse/compiler/python/ascendc_compile.py:214-242](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L214-L242)，列出 cache key 的全部六个输入。
2. 读 [autofuse/compiler/python/ascendc_compile.py:257-289](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L257-L289)，回答：两个进程同时发现 wrapper `.so` 不存在会怎样？
3. 对照 u8-l2 的「Python 侧按内容 cache key 编为共享 so（文件锁+原子替换）」结论，确认这正是该讲所述机制的实现现场。

**需要观察的现象**：锁内有一次「再检查」——第二个进程拿到锁时文件大概率已被第一个进程放好，直接返回，不重编。

**预期结果**：能写出六元 key 清单（源内容、ASCEND_PATH、machine、soc_version、compile_options、stage）并用一句话解释「为什么 key 里要包含 soc_version」：不同芯片的二进制不通用，混用会加载失败。

#### 4.4.5 小练习与答案

**练习 1**：`main` 为什么要 `os.chdir(args.temp_dir)`？

答案：生成的源码之间用相对路径互相 include（如 `#include "autofuse_tiling_func_common.h"`），且编译产物先落在 temp_dir；切目录让所有相对引用在统一基准下解析，结束后再切回原目录拷贝产物。

**练习 2**：PGO 的 runner 为什么链接成可执行文件而不是共享库？

答案：runner 的职责是「上板跑一遍候选 tiling 并把实测耗时写回」，它需要独立进程启动（配合 MSPTI 采样与 `LD_PRELOAD`），可执行文件形态才能被直接拉起；而常规 host/device 产物要被 torch_npu 进程 `dlopen`，必须是 `.so`。

**练习 3**：`is_cv_wrapper_source` 如何识别一个源文件是 wrapper？

答案：文件名等于 `CV_WRAPPER_SOURCE_NAME` 或以 `_tiling_func_{CV_WRAPPER_SPLIT_KEY}.cpp` 结尾（[ascendc_compile.py:207-211](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L207-L211)）——即 4.2 节切分标记 `ACubeKernelTilingWrapperHpp` 对应的翻译单元，两个模块靠这个命名约定衔接。

## 5. 综合实践

**任务：给「一次 Autofuse 编译」写一份全程追踪笔记。**

假设 torch_npu 的 AscendC 后端要对 `Add + Exp` 子图做一次 Inductor 路径编译，请按顺序回答并落到具体源码行：

1. **建图**：后端用 `ascir_api.Data / Load / Add / Exp / Store / Output` 搭 hint 图——写出每个函数内部三步（`_generate_op_name` 取号、`ops.append`、`infer_dtype`）各自维护了 `GraphMetadata` 的哪个字段（4.3 节）。
2. **调度与生成**：分别用 `Schedule.scheduleV2` + `CodeGen.host_code_generator` / `device_code_generator` 两段式调用，写出两次调用各自的入参类型与返回的容器结构（4.1 节的 `FusedScheduledResult` 与双层字典）。
3. **host 编译**：追踪 `host_compile` → `parse_split_host_sources` 把 host 源码拆成哪些文件（对照 `SPLIT_HEADER_FILES` 清单），并指出 PGO 场景额外多出的两个源文件（4.2 节）。
4. **device 编译与链接**：追踪 `kernel_compile` → `ascendc_compile.main(stage="device")` → `link_kernel_target`，说明 CV 场景下 wrapper `.so` 如何被识别、缓存并追加进链接（4.4 节）。
5. 最后一问串联全链：这份产物 `.so` 被谁加载、`ir_attr.index` 在运行期起什么作用？

**验收标准**：每个环节都能给出至少一个带行号的永久链接；第 5 问应能答出「`.so` 由 torch_npu 按 `autofused_` 产物加载（u3-l3），index 决定 kernel 参数与 Data/Output 节点的对应关系」。

## 6. 本讲小结

- `pyautofuse` 用原生 CPython C API 暴露三类对象：`Autofuser`（调度+生成一站式，返回源码三元组）、`Schedule`（`schedule`/`scheduleV2` 对应 `Optimize` 两个重载）、`CodeGen`（`device_code_generator`/`host_code_generator`/`get_kernel_and_json_generator`/`pgo_code_generator` 四入口，返回嵌套字典）。
- CV 融合在绑定层的表征是「一份调度结果过滤出 ub / common 两套代码」，字典 key 即模板类型；非 CV 场景统一走 `"default"`。
- `compile_adapter.py` 把编译拆成 host/device/all 三个 stage；host 源码按内嵌标记切成「公共头 + 原子 .cpp」以支持按内容缓存，PGO 场景额外产出 runner 与 device 采样源，且只在 host stage 允许。
- 新增的 `ascir_api.py` 用全局 `_graph_metadata`（按图名隔离）维护算子计数器与 data/output 索引，把「唯一命名、IO 编号、视图推导」从用户手中收进模板函数；C++ 侧 `AssignDefaultIoIndex` 在入口做同语义的规范化，两层互为兜底。
- `ascendc_compile.py` 是与 CANN 工具链的衔接点：毕昇编译器编译、PCH 加速、CV tiling wrapper 用「内容 sha256 六元 key + 文件锁 + 原子替换」编成跨图共享的 `.so`。
- 本次增量在绑定层的可见变化：`REGISTERED_OPS` 新增 I0/I0e/I1e、LogNdtr、NextAfter、PolyGamma、ChebyshevPolynomialT/U/V/W、HermitePolynomialH/He，`ascir_api.py` 与 `pyascir.cpp` 属性访问器同步扩容——Python 包装与 C++ 花名册必须成对维护。

## 7. 下一步学习建议

- 下一讲 u9-l2 讲 `inc/` 目录对 GE 的 C++ 接口（autofuse_attrs、fusion decider），那是「不走 Python、直接在 GE 进程内」触发 Autofuse 的另一条通路，可与本讲的 Python 通路对照。
- 想深挖 PGO 产物形态与 generation 发布顺序，直接读 [autofuse/tests/st/python/test_inductor_pgo_compile_flow.py](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/st/python/test_inductor_pgo_compile_flow.py) 的断言，并回看 u8-l2 的 Inductor PGO 多阶段候选稳定化。
- 想看 `ascir_api` 的完整算子覆盖面，浏览 [autofuse/tests/st/python/test_python_ascir.py](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/st/python/test_python_ascir.py) 中各算子的用例；特殊函数类算子的注册链路在 u11-l5 展开。
