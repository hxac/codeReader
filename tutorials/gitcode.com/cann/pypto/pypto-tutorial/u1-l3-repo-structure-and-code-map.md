# u1-l3 仓库目录结构与源码地图

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `docs/`、`examples/`、`models/`、`python/`、`framework/` 五大目录各自的职责，以及 `python/pypto` 与 `python/pypto_pro` 两个 Python 包的分工。
2. 拿到一个具体概念（例如「jit 装饰器」「PIL」「某个编译 Pass」「CodeGen」），能直接说出它对应的源码目录和文件，而不需要在仓库里盲目搜索。
3. 理解 `CMakeLists.txt` 与 `build_ci.py` 在构建体系中扮演的角色：前者定义「编译什么、安装什么」，后者是「CI 场景的总入口」。
4. 建立一张属于自己的「源码地图」，后续阅读任何讲义提到源码时，都能迅速定位。

本讲是纯「地图课」：不深究任何模块的实现细节，只解决「东西放在哪、谁负责什么」这个问题。有了地图，后面的源码精读才不会迷路。

## 2. 前置知识

阅读本讲前，你需要了解以下基础概念（不熟悉也没关系，下面用通俗语言解释）：

- **仓库（repository）**：项目的源码根目录，也就是你执行 `git clone` 得到的目录。本讲的「仓库根目录」指 `CMakeLists.txt` 所在的那一层。
- **Python 包（package）**：一个包含 `__init__.py` 的目录，`import pypto` 时 Python 会先执行 `pypto/__init__.py`。因此 `__init__.py` 是理解一个包「对外暴露什么」的最佳入口。
- **pybind11**：一个让 C++ 代码可以被 Python 调用的绑定库。PyPTO 的编译器主体是 C++ 写的，通过 pybind11 编译成 `.so` 共享库供 Python 侧加载。这解释了为什么仓库里 Python 代码和 C++ 代码各占一半。
- **CMake**：C/C++ 世界的构建工具，`CMakeLists.txt` 是它的配置文件，描述「编译哪些目标（target）、依赖什么、安装到哪里」。
- **共享库（shared library）**：Linux 下的 `.so` 文件，程序运行时动态加载。PyPTO 安装后，`pypto/lib/` 目录下会有一组 `libtile_fwk_*.so`。
- **编译链路回顾**（来自 u1-l1）：用户 Python 代码 → AST 解析为 PIL → 转换为 IR → C++ 多层图 Pass（Tensor Graph → Tile Graph → Block Graph）→ CodeGen 生成 PTO 虚拟指令 → 设备执行。本讲的目录结构，正是这条链路在文件系统上的投影。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件/目录 | 作用 |
| --- | --- |
| [README.md](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/README.md) | 项目门面：特性介绍、学习路径、目录结构官方说明 |
| `python/pypto/` | PyPTO 主 Python 包：Tensor API、前端解析器（frontend）、PIL、算子库（op） |
| `python/pypto_pro/` | pypto_pro 低层编程 Python 包：面向 Tile/Block 层次的语言 API、IR 构建器、运行时 |
| `python/src/` | pybind11 绑定源码，把 C++ 框架能力暴露给 Python |
| `python/tests/` | Python 测试用例（ut 单测 / st 系统测试） |
| `framework/include/` | C++ 对外头文件（core、ir、tile_fwk_bundle 等） |
| `framework/src/` | C++ 框架源码主体：passes（编译 Pass）、codegen（代码生成）、machine（运行时/仿真）、interface（接口层）、operator、platform、cost_model、adapter、utils |
| `examples/` | 三级示例体系：00_hello_world、01_beginner、02_intermediate、03_advanced |
| `models/` | 大模型算子实现样例（GLM、DeepSeek、Qwen3 等） |
| `docs/zh/` | 中文文档：api、tutorials、install、invocation、pypto_pro、contribute |
| `CMakeLists.txt` | 顶层 CMake 配置：定义公开编译开关、组织子目录、安装规则 |
| `build_ci.py` | CI 场景构建控制总入口：封装 cmake 命令、跑 UTest/STest |

## 4. 核心概念与源码讲解

### 4.1 仓库全景：README 给出的官方目录结构

#### 4.1.1 概念说明

README 是任何开源项目的第一手地图。PyPTO 的 README 在「目录结构」一节明确画出了关键目录树，并给每个目录标注了职责注释。这一节我们先读官方地图，再用实际 `ls` 验证它。

核心认知：PyPTO 仓库由五大功能区域构成——

1. **文档区 `docs/`**：教程、API 参考、安装与贡献指南，全部中文文档在 `docs/zh/` 下。
2. **示例区 `examples/` 与 `models/`**：`examples/` 是按难度分级的「教材」，`models/` 是真实大模型算子的「参考答案」。
3. **Python 区 `python/`**：两个用户可见的包（`pypto` 高层、`pypto_pro` 低层）加 pybind11 绑定源码与测试。
4. **C++ 区 `framework/`**：编译框架主体，多层图 Pass、CodeGen、运行时都在这里。
5. **构建区**：根目录的 `CMakeLists.txt`、`build_ci.py`、`setup.py`、`pyproject.toml`、`cmake/` 目录。

#### 4.1.2 核心流程

用户代码在目录间的流转关系可以概括为：

```text
examples/*.py / models/*.py        # 用户用 python/pypto 提供的 API 写算子
        │
        ▼
python/pypto (frontend/pil/op)     # Python 前端：解析、生成 PIL、转 IR
        │  (经 python/src 的 pybind11 绑定)
        ▼
framework/src (passes → codegen → machine)   # C++：多层图 Pass、代码生成、运行时
        │  (由根目录 CMakeLists.txt 编译为 libtile_fwk_*.so)
        ▼
设备执行 / SIM 仿真
```

记住这条投影关系，「哪个概念去哪个目录找」就有了系统性答案。

#### 4.1.3 源码精读

README 的目录结构一节（含目录树与逐行注释）在这里：

[README.md:L76-L115](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/README.md#L76-L115) —— 官方目录树：标注了 `docs/zh`（api、contribute、tutorials）、`examples`（三级示例）、`models`、`python`（pypto 包、pybind11 src、tests）、`framework`（include、src 下的 codegen 与 passes、tests）、`tools`、`cmake`，以及根目录的 `build_ci.py`、`CMakeLists.txt`、`pyproject.toml`、`setup.py`。

其中两行注释特别值得记住：

- `build_ci.py` 被描述为「CI 执行构建、执行UTest、执行STest辅助脚本」；
- `CMakeLists.txt` 被描述为「顶层CMakeLists.txt，定义所有对外公开编译开关」。

这两句就是本讲 4.4 节的提纲。

另外，[README.md:L43-L49](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/README.md#L43-L49) 给出了 examples 的三级学习路径（beginner/intermediate/advanced），与 u1-l2 跑通的 `examples/00_hello_world/` 相衔接。

#### 4.1.4 代码实践

1. **实践目标**：把 README 的目录树与磁盘上的真实目录一一对应，确认文档没有「画错地图」。
2. **操作步骤**：
   - 在仓库根目录执行 `ls`，对照 README 目录树逐项勾选；
   - 执行 `ls python framework examples models docs/zh`，观察第二层结构；
   - 执行 `ls framework/src`，确认 README 里提到的 `codegen/`、`passes/` 真实存在。
3. **需要观察的现象**：README 目录树中列出的每个目录都能在磁盘上找到；同时磁盘上会有 README 未展开的目录（如 `framework/src/machine/`，README 用 `...` 省略了）。
4. **预期结果**：得到一份「README 说法 ↔ 实际磁盘」对照清单，例如 `framework/src/codegen/` 存在、`framework/src/machine/` 是 README 未展开的隐藏项。
5. 本实践为目录浏览，不依赖 PyPTO 安装，任何环境均可执行；具体输出以你本机为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：README 说 `python/src/` 是「pybind11源码根目录」。请进入该目录，找到一个能证明它确实是 Python↔C++ 绑定层的文件。

**答案**：`python/src/pybind11.cpp` 以及 `python/src/bindings/` 目录（内含 `controller.cpp`、`function.cpp`、`operation.cpp` 等绑定实现文件）。`pybind11.cpp` 是绑定总入口，`bindings/` 下按模块拆分。

**练习 2**：`examples/` 和 `models/` 都是示例代码，它们的定位差别是什么？

**答案**：`examples/` 是按 beginner/intermediate/advanced 分级的教学样例，教你怎么用 API；`models/` 是真实大模型（GLM、DeepSeek、Qwen3 等）中算子的完整实现，是接近生产水准的参考实现，难度和规模都远大于 examples。

### 4.2 Python 包入口：从 `import pypto` 到 jit 装饰器

#### 4.2.1 概念说明

`python/pypto/__init__.py` 是整个 PyPTO 用户 API 的汇聚点：你在示例里用到的 `pypto.Tensor`、`pypto.add`、`pypto.jit` 全部从这里（直接或间接）导出。读包先读 `__init__.py`，是源码阅读的通用技巧——它就是这家店的「菜单」。

本模块要解决三个问题：

1. `import pypto` 时发生了什么（尤其是 C++ 共享库如何被加载）；
2. `pypto.jit` 这个符号的定义链条到底经过哪几个文件；
3. `python/pypto` 内部各子目录（frontend、pil、op 等）分别放什么。

`python/pypto` 下的关键子目录/文件一览（已实测存在）：

| 路径 | 职责 |
| --- | --- |
| `tensor.py` | Tensor 对象定义 |
| `frontend/parser/` | Python 函数捕获与 AST 解析：`entry.py`（入口）、`parser.py`（解析器）、`pil.py`/`pil_builder.py`（PIL 数据结构与构建）、`pil_io_text.py`/`pil_parser.py`（PIL 文本格式）、`context.py`、`diagnostics.py`、`evaluator.py`、`liveness.py` |
| `pil/` | PIL 到 IR 的转换：`pil2ir.py`、`pir.py`、`dispatcher.py`、`op_registry.py`、`ops.py`、`compile_pipeline.py` |
| `op/` | 算子库，按类别分文件：`math.py`、`reduction.py`、`matmul.py`、`quantization.py`、`distributed.py` 等 |
| `runtime.py`、`functions.py`、`converter.py`、`symbolic_scalar.py`、`cost_model.py`、`pass_config.py` | 运行模式、Function 复用、torch 转换、符号标量、代价模型、Pass 开关 |

#### 4.2.2 核心流程

`import pypto` 的加载顺序（由 `__init__.py` 的语句顺序决定）：

```text
1. 先 import torch           # 避免 cxxabi 冲突导致 torch 崩溃
2. from . import _loader     # 用 ctypes 预加载 libtile_fwk_*.so 共享库
3. 依次导入 config / op / operation / tensor / runtime / functions 等子模块
4. 最后 from . import frontend   # 放在最后是为了避免循环导入
5. jit = frontend.jit        # 暴露 pypto.jit
```

`pypto.jit` 的定义链条：

```text
pypto/__init__.py:48  jit = frontend.jit
        │
        ▼
frontend/__init__.py:69  from .parser import function, jit
        │
        ▼
frontend/parser/entry.py:1133  def jit(func=None, *, host_options=..., ...)
```

也就是说：**「jit 装饰器定义」的最终源头在 `python/pypto/frontend/parser/entry.py`**，前面两层只是转发。这条三跳链条是本讲综合实践要你亲手验证的目标之一。

#### 4.2.3 源码精读

[python/pypto/__init__.py:L14-L19](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/__init__.py#L14-L19) —— 注释解释了为什么必须先加载 torch：torch/torch_npu 可能使用 cxxabi=0 或 1，而 pypto 只支持 cxxabi=0，若 pypto 先加载可能导致 torch 崩溃，因此强制 torch 先行加载。

[python/pypto/__init__.py:L23-L46](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/__init__.py#L23-L46) —— `# shared lib should be loaded first`：先导入 `_loader` 加载 C++ 共享库，再依次导入 `op`、`tensor`、`runtime`、`symbolic_scalar`、`functions` 等子模块，最后一句注释点明 frontend 必须放在所有导入之后以避免循环导入。

[python/pypto/__init__.py:L48-L51](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/__init__.py#L48-L51) —— 对外别名定义：`jit = frontend.jit`、`tensor = Tensor`、`element = Element`、`symbolic_scalar = SymbolicScalar`。这就是示例代码里 `@pypto.jit` 能工作的直接原因。

[python/pypto/_loader.py:L61-L104](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/_loader.py#L61-L104) —— 共享库加载清单：`desc_lst` 列出 `libtile_fwk_utils.so`、`libtile_fwk_adapter.so`、`libtile_fwk_cann_host_runtime.so`、`libtile_fwk_platform.so`、`libtile_fwk_interface.so`、`libtile_fwk_codegen.so`、`libtile_fwk_compiler.so`、`libtile_fwk_runtime.so`、`libtile_fwk_simulation.so`、`libtile_fwk_simulation_pv.so`，逐一用 `ctypes.CDLL(..., RTLD_GLOBAL)` 加载。注意：这份清单与 4.4 节 CMakeLists 的安装目标几乎一一对应——构建系统装什么，运行时就加载什么。

[python/pypto/frontend/__init__.py:L67-L69](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/frontend/__init__.py#L67-L69) —— frontend 包入口：`from .parser import function, jit`，把解析器子模块中的两个装饰器导出为包级 API。

[python/pypto/frontend/parser/entry.py:L1133-L1144](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/frontend/parser/entry.py#L1133-L1144) —— `def jit(...)` 真正定义处：支持 `@jit` 与 `@jit()` 两种写法（`func` 可为 `None` 时返回装饰器），并接受 `host_options`、`codegen_options`、`pass_options`、`runtime_options`、`verify_options`、`debug_options` 六组关键字配置——这些正是 u1-l2 里 `runtime_options={"run_mode": ...}` 的落脚点。

顺带认识 PIL 构件文件（综合实践目标之一）：[python/pypto/frontend/parser/pil.py:L12-L33](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/frontend/parser/pil.py#L12-L33) —— 模块文档字符串说明 PIL（Python Intermediate Language）是「简化版 Python AST，只保留代码生成所需信息」，并列出 12 条化简规则（Lambda 转命名函数、推导式转生成器函数、AugAssign 转显式 load+BinOp+store 等）。同目录的 `pil_builder.py` 负责「搭」，`pil_io_text.py`/`pil_parser.py` 负责「读/写文本格式」。u3-l3 会专门精读它们，本讲只需记住位置。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证 `pypto.jit` 的三跳定义链，确认「菜单—转发—定义」三层关系。
2. **操作步骤**：
   - 打开三个文件，分别定位到上面给出的三处代码（`__init__.py:48`、`frontend/__init__.py:69`、`entry.py:1133`）；
   - （可选，需已安装 pypto）在 Python 里执行：
     ```python
     import pypto
     print(pypto.jit)                # 期望打印 frontend.parser.entry 中的函数对象
     print(pypto.jit.__module__)     # 期望显示定义它的模块路径
     print(pypto.jit.__wrapped__ if hasattr(pypto.jit, "__wrapped__") else "no wrapper")
     ```
3. **需要观察的现象**：`pypto.jit.__module__` 应指向 `pypto.frontend.parser.entry`（或其再包装所在模块），证明定义源头在 entry.py，而非 `__init__.py`。
4. **预期结果**：三处源码与运行时信息互相印证，你能在 30 秒内向别人讲清「pypto.jit 定义在哪」。
5. 源码定位部分无条件即可完成；运行验证部分需要安装好的 PyPTO 环境，具体打印内容待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`__init__.py` 为什么把 `from . import frontend` 放在所有导入的最后，还要加一行注释？

**答案**：frontend 子模块反过来引用了 `pypto` 包里的其他成员（如 `symbolic_scalar`），如果 frontend 先导入会造成循环导入。放在最后，保证它依赖的子模块都已就绪。这是大型 Python 包常见的「导入顺序即依赖顺序」手法。

**练习 2**：不看文档，只看 `python/pypto/op/` 目录下的文件名，说出你能找到哪几类算子。

**答案**：按文件名分类——数学（`math.py`）、归约（`reduction.py`）、矩阵乘（`matmul.py`）、卷积（`conv.py`）、比较（`comparison.py`）、创建（`creation.py`）、索引（`indexing.py`）、拼接（`joining.py`）、量化（`quantization.py`）、随机（`random.py`）、分布式通信（`distributed.py`）、校验（`verify.py`）等。文件名即 API 分类，这是 PyPTO 算子库的组织约定。

**练习 3**：`pypto.Tensor` 对应哪个源码文件？

**答案**：`python/pypto/tensor.py`。`__init__.py` 第 40 行 `from .tensor import Tensor`，第 49 行又给了别名 `tensor = Tensor`，所以 `pypto.Tensor` 与 `pypto.tensor` 是同一个类。

### 4.3 C++ 框架源码组织：以一个 Pass 和 CodeGen 入口为例

#### 4.3.1 概念说明

`framework/` 是 C++ 编译框架的大本营，分四块：

- `framework/include/`：对外头文件（`core`、`ir`、`pypto_pro`、`tile_fwk_bundle`、`tilefwk`）。
- `framework/src/`：源码主体，子目录职责如下（实测）：

| 子目录 | 职责 |
| --- | --- |
| `passes/` | 编译 Pass：`tensor_graph_pass/`、`tile_graph_pass/`、`block_graph_pass/` 三层图各一组，外加 `pass_mgr/`（Pass 管理器）、`pass_check/`、`pass_log/`、`pass_utils/`、`algorithms/`、`statistics/` 等基础设施 |
| `codegen/` | 代码生成：`codegen.cpp`、`codegen_cce.cpp`、`codegen_op.cpp`、`codegen_factory.h` 及 `npu/`、`stmt_mgr/`、`symbol_mgr/` 子模块 |
| `machine/` | 运行时与执行：`host/`、`device/`、`runtime/`（含 `bundle/`、`launcher/`、`runner/`、`distributed/`）、`simulation/`（SIM 仿真，含 `aicore_hardware.cpp`）、`compile/` |
| `interface/` | 对外接口层：`ir/`、`tensor/`、`operation/`、`interpreter/`（解释器）、`configs/`、`program/`、`function/` 等 |
| `operator/`、`platform/`、`cost_model/`、`adapter/`、`utils/`、`cann_host_runtime/` | 算子底层实现、平台信息、代价模型、适配层、公共工具、CANN 宿主运行时 |

- `framework/tests/`：C++ 测试。

面对这么大的目录，初学者最容易犯的错是「陷进某个 .cpp 的细节」。正确做法是先抓两类「样本」感受代码风格：一个编译 Pass（本讲选 `tensor_graph_pass/auto_cast.cpp`）和代码生成入口（`codegen.cpp`）。它们分别代表「图的变换」和「图的输出」两种典型代码形态，u4/u5 单元会系统展开。

#### 4.3.2 核心流程

C++ 侧对一个算子的处理路径（与 u1-l1 的编译链路对应）：

```text
Python 侧生成 IR
    ▼
passes/tensor_graph_pass/*   Tensor 图变换（tile shape 推导、类型提升 auto_cast、循环展开…）
    ▼
passes/tile_graph_pass/*     Tile 图优化、子图划分
    ▼
passes/block_graph_pass/*    Block 图调度、同步插入、内存复用
    ▼
codegen/*                    由工厂选择平台后端，生成 PTO 虚拟指令
    ▼
machine/runtime/*            加载编译产物并调度执行
```

C++ 代码有两个反复出现的模式，先记住名字：

- **Pass 模式**：每个 Pass 实现 `RunOnFunction(Function&)` 风格的入口，由 `pass_mgr` 统一调度，逐个作用于图。
- **工厂模式**：`CodeGenFactory::GetCodeGenCCE(ctx)` 根据平台架构（NPUArch）返回不同的 CodeGen 后端实例。

#### 4.3.3 源码精读

[framework/src/passes/tensor_graph_pass/auto_cast.cpp:L79-L100](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/framework/src/passes/tensor_graph_pass/auto_cast.cpp#L79-L100) —— `AutoCast::RunOnFunction`：tensor graph 阶段类型提升 Pass 的入口。它先打日志标记函数开始处理（`APASS_LOG_INFO_F`），根据 NPU 架构（如 `DAV_3510`）决定合法的类型转换对，再依次调用 `InsertBF16Cast`、`InsertFP16Cast`、`InsertInt32Fp16Cast` 在不支持某数据类型的算子前后插入 CAST 节点。这就是「Pass 作用于图上的一个函数、按硬件能力改写图」的典型样本。

[framework/src/passes/tensor_graph_pass/auto_cast.cpp:L30-L34](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/framework/src/passes/tensor_graph_pass/auto_cast.cpp#L30-L34) —— 辅助方法 `CreateFp32TensorLike`：为任意张量创建同形状的 FP32 副本，是插入 CAST 时构造中间张量的基础操作。顺带能看到 C++ 侧的张量类型叫 `LogicalTensorPtr`——Python 的 `Tensor` 到了 C++ 图里就是 LogicalTensor 节点。

`tensor_graph_pass/` 目录下与 auto_cast 并列的 Pass 文件（实测清单）：`derivation_tile_shape`、`set_heuristic_tile_shapes`、`cube_tile_setting`（tile 形状推导与设置）、`expand_function`（函数展开）、`infer_memory_conflict`、`infer_tensor_format`（内存冲突/格式推断）、`loop_unroll`（循环展开）、`remove_redundant_reshape`、`remove_undriven_view`（冗余节点消除）。每个文件都是「一个 Pass」的组织约定——想找某个优化，直接按文件名索引。

[framework/src/codegen/codegen.cpp:L21-L27](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/framework/src/codegen/codegen.cpp#L21-L27) —— CodeGen 入口 `CodeGen::GenCode(Function& topFunc)`：整个文件只有这一个函数，先断言 `rootFunc` 非空，然后通过 `CodeGenFactory::GetCodeGenCCE(ctx_)` 拿到具体后端并委托执行。「入口薄、逻辑在别处」——这是阅读大项目时识别「门面文件」的信号。

[framework/src/codegen/codegen_factory.h:L33-L46](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/framework/src/codegen/codegen_factory.h#L33-L46) —— 工厂实现：按 `Platform::Instance().GetSoc().GetNPUArch()` 判断，`DAV_2201`/`DAV_3510` 走 `CodeGenCloudNPU`，LiteNPU 走 `CodeGenLiteNPU`，其余架构直接断言报错。真实后端逻辑在同目录 `codegen_cce.cpp`、`codegen_op.cpp` 与 `npu/` 子目录中。

#### 4.3.4 代码实践

1. **实践目标**：用「文件名索引法」遍历三层图 Pass 目录，统计每层 Pass 的数量与命名规律，建立 Pass 全景索引。
2. **操作步骤**：
   - 在仓库根目录执行：
     ```bash
     ls framework/src/passes/tensor_graph_pass/
     ls framework/src/passes/tile_graph_pass/
     ls framework/src/passes/block_graph_pass/
     ```
   - 把每个 `.cpp/.h` 的主名抄成表格，按「做什么」分一列注明（如 `loop_unroll` → 循环展开）；
   - 打开 `auto_cast.cpp` 跳到 `RunOnFunction`（L79 起），只看函数骨架：日志 → 平台判断 → 三个 InsertXxxCast 调用，不看任何 InsertXxx 的实现体。
3. **需要观察的现象**：三层目录中 Pass 数量不同、命名都能「望文生义」；`auto_cast.cpp` 与 `auto_cast.h` 成对出现（实现与声明分离）。
4. **预期结果**：得到一张三层 Pass 索引表（约 20+ 个 Pass），并能说出 auto_cast 在 tensor graph 层的位置与职责。
5. 本实践为纯目录/源码阅读，无需构建环境；Pass 数量以当前 HEAD 实测为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：想在 framework 里找「SIM 仿真执行」的代码，应该去哪个目录？依据是什么？

**答案**：`framework/src/machine/simulation/`（内含 `aicore_hardware.cpp`、`host_core_context.cpp` 等）。依据是 4.3.1 的子目录职责表：machine 负责「运行时与执行」，其下 simulation 子目录对应 u1-l2 讲过的 RunMode.SIM 主机侧仿真。

**练习 2**：`codegen.cpp` 只有不到 30 行有效代码，为什么它值得作为「CodeGen 入口」精读？

**答案**：因为它是门面（facade）：`GenCode` 展示了 CodeGen 阶段的完整调用骨架（校验 → 取后端 → 委托），顺着它引用的 `codegen_factory.h`、`codegen_cce.cpp` 可以自然深入到真正的生成逻辑。读大项目要优先找这种「小而关键」的入口文件。

**练习 3**：`interface/interpreter/` 与 `machine/simulation/` 都和「不依赖真机的执行」有关，它们是什么关系？

**答案**：`machine/simulation/` 提供仿真的硬件侧模型（如 AI Core 仿真），`interface/interpreter/` 提供指令/算子的解释执行（逐条计算语义，如 `calc.cpp`、`calc_vector.cpp`）。二者配合构成 SIM 模式的执行路径；具体协作细节在 u5-l4 展开，本讲只需知道各自的目录归属。

### 4.4 构建体系：CMakeLists.txt 与 build_ci.py

#### 4.4.1 概念说明

u1-l2 已经带大家用 `build_ci.py` 编译安装过 PyPTO。本模块从源码角度回答两个问题：

1. **顶层 `CMakeLists.txt` 定义了什么？**——它定义所有「对外公开编译开关」（README 原话），组织 `framework/` 与 `python/` 两个子目录的编译，并规定编译产物如何安装进 whl 包。
2. **`build_ci.py` 是什么？**——它不是构建系统本身，而是 CI 场景的「总入口/驾驶舱」：把命令行参数翻译成 cmake `-D` 选项，拼装并执行 cmake 命令，还能顺带跑 UTest/STest、控制超时与清理。

理解二者关系的关键线索是：**CMake 安装的共享库清单 = `_loader.py` 运行时加载的清单**。构建脚本不直接出现在用户的算子代码里，但它决定了 `import pypto` 时磁盘上有什么。

#### 4.4.2 核心流程

从源码到可安装包的流水线：

```text
python build_ci.py [-f python3 -b npu -j 8 ...]      # 用户/CI 调用
        │  翻译为 cmake -DENABLE_UTEST=ON 等选项
        ▼
顶层 CMakeLists.txt
        ├─ add_subdirectory(framework)   → framework/src 各模块 → libtile_fwk_*.so
        ├─ add_subdirectory(python)      → pybind11 绑定 → pypto_impl 等
        └─ install 规则 → 把 .so/头文件/配置 打进 pypto whl 包的 lib/ 等目录
        ▼
pip install 产出的 whl → import pypto 时 _loader 按 lib/ 目录加载 libtile_fwk_*.so
```

#### 4.4.3 源码精读

[CMakeLists.txt:L54-L79](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/CMakeLists.txt#L54-L79) —— 「公开编译开关」全景：三组 `option()`——构建类（`BUILD_WITH_CANN`、`ENABLE_ASAN`/`UBSAN`/`GCOV`）、特性类（`ENABLE_FEATURE_PYTHON_FRONT_END`、`ENABLE_FEATURE_PYBIND11_IMPL_COMPILE_ONLINE`）、测试类（`ENABLE_UTEST`、`ENABLE_STEST`、`ENABLE_STEST_DISTRIBUTED` 等十余项）。u1-l2 里启用 SIM/UTest 的那些行为，最终都落到这些开关上。

[CMakeLists.txt:L105-L106](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/CMakeLists.txt#L105-L106) —— 编译流程的核心两行：`add_subdirectory(framework)` 与 `add_subdirectory(python)`，把 C++ 框架和 Python 绑定两条编译线挂到顶层。framework 内部再由 [framework/src/CMakeLists.txt:L15-L23](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/framework/src/CMakeLists.txt#L15-L23) 依次加入 `utils`、`adapter`、`interface`、`passes`、`codegen`、`machine`、`cost_model`、`cann_host_runtime`、`platform` 子目录——与 4.3 节的源码地图完全吻合。

[CMakeLists.txt:L113-L131](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/CMakeLists.txt#L113-L131) —— whl 安装目标清单：`InstallTargets` 列出 `tile_fwk_utils`、`tile_fwk_adapter`、`tile_fwk_cann_host_runtime`、`tile_fwk_platform`、`tile_fwk_interface`、`tile_fwk_codegen`、`tile_fwk_compiler`、`tile_fwk_runtime`、`tile_fwk_bundle`、`tile_fwk_simulation`、`tile_fwk_simulation_pv`。把这份清单与 4.2.3 节 `_loader.py` 的 `desc_lst` 并排对照，除了 `tile_fwk_bundle`（供脱离前端消费 `.pyptokb` 的调用方使用）外一一对应：**CMake 装什么，import 时就加载什么**。

[CMakeLists.txt:L138-L143](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/CMakeLists.txt#L138-L143) —— 二进制与 `pypto_impl` 目标的安装：通过 `PTO_Fwk_InstallBinaries` 把上述目标装入 whl 的 lib 目录，并单独安装 `pypto_impl` 目标——它对应 `_loader.py` 末尾 `ensure_pypto_impl()` 的在线编译/加载机制。

[build_ci.py:L11-L47](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/build_ci.py#L11-L47) —— 模块文档字符串自述职责：「PyPTO 项目 CI 场景构建控制总入口」，支持 whl 常规/可编辑编译、UTest/STest/Examples 执行、超时控制与子进程清理；常用参数 `-f/--frontend`（python3/cpp）、`-b/--backend`（npu/cost_model）、`-j/--job_num`、`--build_type`、`-u/--utest`、`-s/--stest`、`-c/--clean`，并给出四个使用示例。u1-l2 用到的构建命令参数全部来自这里。

[build_ci.py:L1230-L1243](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/build_ci.py#L1230-L1243) —— `main()` 入口：包裹 `_main()` 统一处理 `KeyboardInterrupt` 与 `subprocess.TimeoutExpired`，最后统计并打印总耗时。这个约 1200 行的脚本用「参数类 → cmake 命令片段」的方式组织（如 `CMakeParam` 抽象基类约定子类实现 `reg_args()`/`get_cfg_cmd()`），把 4.4.3 第一条里那些 `ENABLE_*` 开关暴露成命令行选项。

#### 4.4.4 代码实践

1. **实践目标**：验证「CMake 安装清单 = 运行时加载清单」这条贯穿本讲的线索。
2. **操作步骤**：
   - 并排打开 `CMakeLists.txt` L119-L131（InstallTargets）与 `python/pypto/_loader.py` L61-L104（desc_lst）；
   - 逐项比对，标出「两边都有」「只在 CMake 有」「只在 _loader 有」的目标；
   - （可选）在已安装环境中执行 `python -c "import pypto, pathlib; p=pathlib.Path(pypto.__file__).parent/'lib'; print(sorted(x.name for x in p.glob('*.so')))"` 查看实际落盘的 `.so`。
3. **需要观察的现象**：绝大多数 `libtile_fwk_*.so` 两边同时出现；`tile_fwk_bundle` 只在 CMake 安装清单里。
4. **预期结果**：得到一张三方对照表（CMake 目标 / _loader 清单 / 磁盘 .so），从而把「构建 → 安装 → import 加载」三段串成闭环。
5. 源码比对无条件限制；运行验证需已安装环境，磁盘清单待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`build_ci.py` 和 `CMakeLists.txt` 是什么关系？能不能只用其中一个？

**答案**：`CMakeLists.txt` 是真正的构建描述（cmake 的输入）；`build_ci.py` 是包装层，把易用的命令行参数翻译成 `cmake -D` 选项并负责执行、测试、超时清理。理论上可以绕过 `build_ci.py` 直接手敲 cmake 命令，但 `build_ci.py` 降低了出错成本并统一了 CI 行为；反过来只用 `build_ci.py` 而没有 CMakeLists 则无从构建。

**练习 2**：想在编译时开启 UTest，涉及哪两个文件？

**答案**：`CMakeLists.txt`（`ENABLE_UTEST` 等 option，L69-L79 区域）定义开关；`build_ci.py`（`-u/--utest` 参数，docstring L37-L43 有示例）负责把开关传给 cmake。

**练习 3**：为什么 `python/pypto/_loader.py` 要在导入 `op`、`tensor` 等子模块之前加载共享库？

**答案**：因为 Python 侧的很多类型/函数是对 C++ 对象的 pybind11 包装，底层 `.so` 必须先以 `RTLD_GLOBAL` 方式进入进程，后续符号解析才能成功。所以 `__init__.py` 用注释 `# shared lib should be loaded first` 强调了这个顺序。

## 5. 综合实践

**任务：完成「源码地图四点定位 + 模块归属表」**，把本讲所有知识串成一张可长期使用的地图。

具体步骤：

1. **定位四个关键点**（本讲实践任务的核心），逐一给出精确文件路径与行号：
   - **jit 装饰器定义**：从 `pypto.jit` 出发，经过 `python/pypto/__init__.py:48` → `python/pypto/frontend/__init__.py:69` → 最终定义于 `python/pypto/frontend/parser/entry.py:1133`；
   - **PIL 构件文件**：`python/pypto/frontend/parser/` 下的 `pil.py`（数据模型，文档字符串在 L12 起）、`pil_builder.py`（构建器）、`pil_io_text.py` 与 `pil_parser.py`（文本读写）；另注明「PIL→IR 转换」位于 `python/pypto/pil/`（`pil2ir.py` 等）；
   - **tensor graph 的一个 Pass**：`framework/src/passes/tensor_graph_pass/auto_cast.cpp`，入口 `RunOnFunction` 位于 L79；
   - **codegen 入口**：`framework/src/codegen/codegen.cpp` 的 `CodeGen::GenCode`，位于 L21-L27，其后端选择在 `codegen_factory.h` L35-L46。
2. **绘制模块归属表**：按下面格式整理（至少覆盖 12 行），列为你自己验证过的路径：
   | 概念 | 所属语言/目录 | 关键文件 | 一句话职责 |
   | --- | --- | --- | --- |
   | jit 装饰器 | Python / frontend | entry.py:1133 | 捕获 Python 函数并编译为 PTO 算子 |
   | PIL 数据模型 | Python / frontend/parser | pil.py | 简化版 Python AST |
   | …… | …… | …… | …… |
3. **串联构建线索**：在表末尾追加三行——顶层 CMake 子目录（L105-L106）、whl 安装目标（L119-L131）、运行时加载清单（`_loader.py` L61-L104），并注明它们的对应关系。
4. **验收标准**：遮住讲义，仅凭你的表格说出「SIM 仿真的源码在哪」「算子库 math 在哪」「想禁用某个编译 Pass 该去哪个 Python 文件找配置」中的任意两题，即算通过。

本综合实践全程只需要仓库本身（读文件 + `ls`），不需要安装与真机；表中所有路径都应来自你亲手验证，而不是照抄本讲。

## 6. 本讲小结

- 仓库五大区域各司其职：`docs/`（文档）、`examples/`+`models/`（示例与模型实现）、`python/`（Python 包与绑定）、`framework/`（C++ 编译框架）、根目录构建文件。
- `import pypto` 的加载顺序是「先 torch → 再 `_loader` 加载 `libtile_fwk_*.so` → 各子模块 → 最后 frontend」；`pypto.jit` 的定义源头在 `frontend/parser/entry.py:1133`。
- `framework/src` 的子目录与编译链路一一对应：`passes/`（三层图 Pass）、`codegen/`（代码生成）、`machine/`（运行时/仿真）、`interface/`（接口层）；Pass 采用「一个文件一个 Pass、`RunOnFunction` 入口」的组织约定，CodeGen 采用工厂模式按平台选择后端。
- 顶层 `CMakeLists.txt` 定义全部公开编译开关并挂接 `framework`/`python` 两条编译线；`build_ci.py` 是 CI 总入口，把命令行参数翻译成 cmake 选项。
- 一条贯穿全讲的线索：**CMake 的 InstallTargets ≈ `_loader.py` 的加载清单 ≈ 安装后 `pypto/lib/` 下的 `.so`**，构建体系与运行时由这份清单闭环。

## 7. 下一步学习建议

下一讲（u1-l4）将带你看懂 `examples/` 的三级示例体系与 `docs/zh/tutorials` 官方教程的组织方式，学会「用示例自学 API」——你在这讲建立的目录地图将直接派上用场。

进入第 2 单元前，建议先做两件热身：

1. 用本讲的地图，通读 `examples/00_hello_world/hello_world.py`（已部署环境的话再跑一遍），把其中出现的每个 `pypto.*` 符号在 `python/pypto/` 里找到出处。
2. 浏览 `framework/src/passes/` 三层图目录的文件名列表各一遍，只看名字猜测职责，为 u4 单元的 Pass 精读预置印象。

如果想提前深入某个方向：Python 前端看 `python/pypto/frontend/developer_doc.md`（前端开发者文档），多层图 Pass 从 `framework/src/passes/pass_mgr/` 入手，运行时从 `framework/src/machine/runtime/` 的 `launcher/`、`runner/` 入手。
