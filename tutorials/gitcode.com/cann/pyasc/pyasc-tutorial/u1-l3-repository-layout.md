# 目录结构地图：前后端代码都在哪里

## 1. 本讲目标

前一讲（u1-l1）我们建立了 pyasc 的全局地图：Python 源码 → AST → ASC-IR → Ascend C 代码 → Kernel 二进制 → NPU 执行。本讲把这张地图"落到磁盘上"——搞清楚这条链路上的每一段代码到底放在仓库的哪个目录里。读完本讲，你应该能够：

1. 在仓库中**快速定位四类代码**：Python 前端接口（你在算子里调用的 `asc.add`）、ASC-IR 定义（描述中间表示的 `.td` 文件）、Pass 实现（优化和改写 IR 的 C++ 代码）、Ascend C 代码发射实现（把 IR 翻译成 C++ 代码的部分）。
2. 理解一条核心对应规律：**`python/asc/language` 下的 `basic/adv/core/fwk` 四个子目录，与 `include/ascir/Dialect/Asc/IR` 下的 `Basic/Adv/Core/Fwk` 四个目录一一对应**——前端 Python API 与后端 IR 定义是镜像关系。
3. 说清楚 `examples`、`python/test`、`test` 三套示例/测试目录各自的分工：示例教你写算子，Python 测试管前端行为，后端测试管 IR 与代码生成。

本讲依然是"地图课"：不深入任何模块的算法细节，但要让你以后翻开任何一个 pyasc 文件，都能立刻说出它属于哪一层、和哪些文件是"亲戚"。

## 2. 前置知识

本讲假设你已读过 u1-l1（五大核心模块）和 u1-l2（构建安装）。再补充几个本讲会用到的概念：

- **前端 / 后端**：本手册沿用官方架构文档的划分。**前端**指 Python 侧的三个模块（编译和运行模块、Python 前端模块、AST 转 ASC-IR 模块），代码在 `python/` 目录；**后端**指 C++ 侧的两个模块（ASC-IR 定义、Ascend C 代码生成），代码在 `include/` 和 `lib/` 目录。
- **`.td` 文件与 TableGen**：TableGen 是 LLVM 社区的"接口描述语言"，`.td` 文件用声明式语法描述 IR 的操作（Op）、类型（Type）、属性（Attribute）。构建时 TableGen 工具把 `.td` "膨胀"成大量 C++ 代码，避免手写重复样板。你可以把 `.td` 理解为"IR 定义的源码"，生成的 `.inc` 才是被编译的 C++。
- **Dialect（方言）**：MLIR 中一组相关 Op/Type 的集合。pyasc 的 ASC-IR 主要由 `Asc` 方言（描述 Ascend C API）和 `EmitAsc` 方言（贴近 C 语法的低层过渡）构成。
- **Pass**：对 IR 做一次遍历和改写的编译步骤，比如"把 Tensor 声明物化为真实内存分配"。多个 Pass 串起来就是 Pass 流水线。
- **发射（Emission）**：把 IR 操作翻译成 Ascend C 文本代码的过程，也叫 Target / Translation。
- **声明与实现分离**：C++ 项目的常见惯例——头文件（`include/`）放声明，源文件（`lib/`）放实现。pyasc 后端严格遵守这个惯例。

## 3. 本讲源码地图

本讲涉及的关键文件（全部是"路标"性质的文件，帮你建立目录直觉）：

| 文件/目录 | 作用 |
| --- | --- |
| [README.md](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/README.md) | 项目门面，其中"目录结构"一节是本讲的官方依据 |
| [python/asc/__init__.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/__init__.py) | `import asc` 的入口，揭示前端顶层导出结构 |
| [python/asc/language/__init__.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/__init__.py) | `asc.add`、`asc.TPipe` 等用户 API 的汇聚点，揭示 language 层四象限 |
| [python/asc/language/core/__init__.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/__init__.py) | core 象限的导出清单：类型、Tensor、枚举等编程基础设施 |
| [examples/README.md](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/README.md) | 8 个端到端算子示例的总览与推荐学习顺序 |
| [python/test/unit/language/basic/test_vector_binary.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/language/basic/test_vector_binary.py) | Python 单元测试样例，展示"用 pytest 驱动 asc.add 生成 IR" |

## 4. 核心概念与源码讲解

### 4.1 仓库顶层目录

#### 4.1.1 概念说明

pyasc 是一个"双语"仓库：Python 前端 + C++ 后端。第一次打开仓库时最容易迷路，因为它不像纯 Python 项目那样只有 `src/`，也不像纯 C++ 项目那样只有 `lib/`——它是两者的缝合体，而且缝合的接缝（pybind 绑定）本身也是一大块代码。

README 中给了一张目录树，本讲的所有结论都可以追溯到它：

> 关键目录如下（摘自 README 的目录结构一节）：`bin`（工具文件）、`docs`（说明文档）、`examples`（算子开发样例）、`include`（后端头文件和 td 文件）、`lib`（后端源文件）、`scripts`（相关脚本目录）、`python`（python 前端代码）、`test`（后端的测试用例集）。

#### 4.1.2 核心流程

把顶层目录按"数据流经过的顺序"排一遍，就得到一张检索地图：

```text
你写的算子代码 import asc
        │
        ▼
python/asc/            ← Python 前端：language(API)、codegen(AST→IR)、runtime(编译调度执行)
        │  调用 libpyasc（C++ 扩展，源码在 python/src，构建产物落在 python/asc/_C）
        ▼
include/ascir/ + lib/  ← C++ 后端：.td 定义 IR → Pass 改写 IR → Target 发射 Ascend C
        │
        ▼
bin/                   ← 后端开发者工具（ascir-opt / ascir-translate / ascir-lsp）
        
辅助目录：examples（示例）、python/test（前端测试）、test（后端测试）、docs（文档）、scripts（脚本）
```

记忆口诀：**前端看 `python/`，后端看 `include/` + `lib/`，工具看 `bin/`，学习看 `examples/`，验证看两个 `test/`**。

#### 4.1.3 源码精读

README 的目录结构一节给出了官方口径的目录树，见 [README.md:L12-L35](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/README.md#L12-L35)——这段是整个仓库的"行政区划图"，其中 `include` 被注释为"后端头文件和 td 文件"、`lib` 被注释为"后端源文件"（下分 Dialect/TableGen/Target）、`python` 被注释为"python 前端代码"。

特别值得注意的是 README 对 `python` 目录的说明，见 [README.md:L27-L31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/README.md#L27-L31)——这里明确了 `python/asc` 是"用户可见的 python 包，对外发布的 wheel 包中以此目录为主，其他代码则按需打包"。也就是说：**你 `pip install pyasc` 之后在磁盘上看到的包，基本就是 `python/asc` 这个目录的化身**；`python/src`（pybind 的 cpp 代码）会被编译成二进制后按需打包进 `python/asc/_C`。

把顶层目录整理成职责表：

| 顶层目录 | 语言 | 职责 | 对应模块 |
| --- | --- | --- | --- |
| `python/asc` | Python | 用户 API、AST 转 IR、JIT 编译运行调度 | 前端三模块 |
| `python/src` | C++ | pybind11 绑定，桥接 Python 与 MLIR C++ | 前后端接缝 |
| `include/ascir` | C++/td | Dialect 定义（Op/Type/Attr 的 `.td`）、Pass 声明、发射函数声明 | ASC-IR 定义模块 |
| `lib` | C++ | Dialect 实现、Pass 实现、Ascend C 发射实现、TableGen 生成器 | Ascend C 代码生成模块 |
| `bin` | C++ | ascir-opt / ascir-translate / ascir-lsp 三个开发者工具 | 后端工具 |
| `examples` | Python | 8 个端到端算子示例 | — |
| `python/test` | Python | 前端测试（unit/kernels/generalization） | — |
| `test` | mlir/lit | 后端测试（Dialect/Target/tools） | — |
| `docs` | Markdown | 架构、快速入门、API 文档、开发指南 | — |
| `scripts` | 脚本 | 辅助脚本 | — |

#### 4.1.4 代码实践

**实践目标**：不看本讲正文，独立复述顶层目录职责，并验证"前端在 python/、后端在 include/+lib/"的结论。

**操作步骤**：

1. 在仓库根目录执行 `ls`，对照 README 目录树逐一说出每个顶层目录的作用。
2. 执行 `ls python/asc include/ascir lib bin`，确认：`python/asc` 下全是 `.py`；`include/ascir` 下有 `Dialect/`、`Target/` 和大量 `.td`/`.h`；`lib` 下有 `Dialect/`、`TableGen/`、`Target/` 三个子目录；`bin` 下只有 3 个 `.cpp`。
3. 用一条命令验证语言分布（`find python -name "*.py" | wc -l` 与 `find lib include -name "*.cpp" | wc -l`），直观感受两侧的体量。

**需要观察的现象**：`include/ascir` 与 `lib` 的子目录名高度重合（都有 Dialect、Target），这是"声明与实现分离"的直接体现；而 `python/src` 虽是 C++，却放在 `python/` 下——因为它是前端的"桥"，不是后端的"芯"。

**预期结果**：能不看资料说出 10 个顶层目录中至少 8 个的职责。本实践为纯目录观察，无运行结果，**待本地验证**的项目仅第 3 步的具体文件计数。

#### 4.1.5 小练习与答案

**练习 1**：同事告诉你"IR 定义改了"，你应该去哪个目录找改动？

**答案**：优先看 `include/ascir/Dialect/` 下的 `.td` 文件（IR 的"源"），其次看 `lib/Dialect/` 下的 `.cpp`（手写的实现部分）。`.td` 是声明式定义，大多数 IR 改动从 `.td` 开始。

**练习 2**：`python/src` 和 `python/asc` 都是 Python 目录下的东西，为什么 `python/src` 是 C++？

**答案**：`python/src` 存放 pybind11 绑定代码（Module.cpp、OpBuilder.cpp 等），负责把 MLIR C++ 能力暴露给 Python；它编译后的产物 `libpyasc.so` 会被放进 `python/asc/_C`，成为 `python/asc` 包的一部分。所以它物理上在前端目录，功能上是前后端的桥接层。

**练习 3**：仓库里有两个测试目录 `python/test` 和 `test`，为什么不合在一起？

**答案**：两者测试对象和驱动方式完全不同。`python/test` 是 pytest 驱动的 Python 测试（测前端行为：codegen、language、runtime）；`test` 是 lit 驱动的后端测试（`.mlir` 文件 + 命令行工具，测 Dialect、Target 发射、tools）。分开存放与各自被调度的构建脚本对应（`test/build_llt.sh`）。

### 4.2 python/asc 子包：用户可见的 Python 前端

#### 4.2.1 概念说明

`python/asc` 是整个项目里**唯一对最终用户可见的包**。你写算子时 `import asc`，拿到的所有东西都从这里出发。它内部再分成几个职责清晰的子包：

| 子包 | 职责 | 一句话理解 |
| --- | --- | --- |
| `language` | 用户编程 API（`asc.add`、`asc.TPipe`、`asc.LocalTensor`…） | "写算子时敲的东西" |
| `codegen` | AST → ASC-IR 转换器（FunctionVisitor 等） | "把 Python 函数翻译成 IR 的翻译器" |
| `runtime` | JIT 装饰器、编译器驱动、Launcher、缓存 | "调度编译和执行的大管家" |
| `lib` | Host 侧/Device 侧 C++ 库的 Python 代理（tiling 等） | "借 C++ 能力的代理人" |
| `common` | 兼容性工具 | 杂项 |
| `_C` | 构建产物 `libpyasc` 的落点 | "编译出来的 .so 放这里" |

其中 `language` 又分成四个象限，这是本讲最重要的对应关系：

```text
python/asc/language/
├── basic/   ← 基础向量/搬运算子（add、data_copy、exp...），对应后端 IR 的 Basic/
├── adv/     ← 高阶 API（Matmul、激活、归一化...），对应后端 IR 的 Adv/
├── core/    ← 编程基础设施（Tensor、dtype、枚举、range...），对应后端 IR 的 Core/
└── fwk/     ← 框架类（TPipe/TQue/TBuf），对应后端 IR 的 Fwk/
```

#### 4.2.2 核心流程

用户执行 `import asc` 之后，名字解析的过程是：

```text
import asc
   │
   ├─ asc/__init__.py 导入:
   │    ├─ jit            （来自 runtime/jit.py）        → @asc.jit 装饰器
   │    ├─ CompileOptions （来自 runtime/compiler.py）   → 编译选项
   │    ├─ LaunchOptions  （来自 runtime/launcher.py）   → 启动选项
   │    ├─ CodegenOptions （来自 codegen/function_visitor.py）→ 代码生成选项
   │    └─ language 包的全部导出（* 导入）
   │
   └─ language/__init__.py 再把四个象限的导出汇聚:
        ├─ from .basic.data_copy import data_copy, ...   → asc.data_copy
        ├─ from .basic.vec_binary import add, ...        → asc.add
        ├─ from .core.tensor import LocalTensor, ...     → asc.LocalTensor
        └─ from .fwk.tpipe import TPipe, TQue, ...       → asc.TPipe
```

所以当你在算子里写 `asc.add(...)`，实际执行的函数体在 `python/asc/language/basic/vec_binary.py`；写 `asc.TPipe()`，类定义在 `python/asc/language/fwk/tpipe.py`。**记住"API 名 → `language/__init__.py` 的 import 行 → 源文件"这条三步检索链**，它适用于 language 层的所有 API。

#### 4.2.3 源码精读

先看前端总入口 [python/asc/__init__.py:L9-L14](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/__init__.py#L9-L14)——这几行 import 揭示了前端顶层只有四样东西：三个 Options 类加一个 `jit` 装饰器来自 runtime/codegen 子包，其余全部来自 `language` 包（`from .language import *`）。这就是"前端 = 编译运行框架 + 编程语言 API"两大块的最直接证据。

再看 language 层的汇聚点 [python/asc/language/__init__.py:L9-L12](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/__init__.py#L9-L12)——`from . import adv / basic / core / fwk` 四行连读，就是 language 层四象限的"户口本"。之后的三条 import 则演示了三个常用 API 的真实出处：

- [python/asc/language/__init__.py:L52](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/__init__.py#L52)——`asc.data_copy` 来自 `basic/data_copy.py`；
- [python/asc/language/__init__.py:L104](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/__init__.py#L104)——`asc.add` 来自 `basic/vec_binary.py`；
- [python/asc/language/__init__.py:L301](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/__init__.py#L301)——`asc.TPipe`、`asc.TQue` 来自 `fwk/tpipe.py`。

core 象限的导出清单见 [python/asc/language/core/__init__.py:L84](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/__init__.py#L84)——这一行从 `core/tensor.py` 导入 `GlobalTensor/LocalTensor` 等张量类；同文件 [python/asc/language/core/__init__.py:L125-L252](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/__init__.py#L125-L252) 的巨型 `__all__` 列表，等于 core 象限的"API 目录页"：dtype、enums、types（各种 Params 结构）、tensor、range、utils 应有尽有。

#### 4.2.4 代码实践

**实践目标**：体验"从 `asc.某API` 反查源码文件"的检索能力。

**操作步骤**：

1. 打开 [examples/01_add/add.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py)，圈出所有 `asc.` 开头的调用（如 `asc.LocalTensor`、`asc.data_copy`、`asc.add`、`asc.set_flag`）。
2. 对每个圈出的 API，在 `python/asc/language/__init__.py` 中搜索它的 import 行，确定来源子包。
3. 用编辑器跳转到对应文件（例如 `asc.get_block_idx` → `basic/sys_var.py`）。

**需要观察的现象**：所有 API 都能在 `language` 四象限中找到归属；没有一个 API 需要去 `runtime/` 或 `codegen/` 里找（那两个子包是框架，不是编程接口）。

**预期结果**：得到一张"API → 文件"清单，例如 `data_copy → language/basic/data_copy.py`、`TPipe → language/fwk/tpipe.py`、`float16 → language/core/dtype.py`。本实践为纯源码检索，**待本地验证**的只有你机器上的编辑器跳转是否顺畅。

#### 4.2.5 小练习与答案

**练习 1**：`asc.runtime` 和 `asc.language` 都是 `python/asc` 的子包，用户算子代码会直接调用 `runtime` 里的函数吗？

**答案**：通常只间接使用。用户直接调用的 API 几乎都在 `language`；`runtime` 提供 `@asc.jit`（装饰器算"贴"在函数上，不是在函数体内调用）和三个 Options 类（在 Host 侧启动时使用）。Kernel 函数体内写的都是 `language` 层的东西。

**练习 2**：想找 `asc.BinaryRepeatParams`（add 的重复参数结构）定义在哪，怎么找最快？

**答案**：看 `python/asc/language/core/__init__.py` 的 import——`BinaryRepeatParams` 来自 `.types`，即 `python/asc/language/core/types.py`。core 象限的 `__init__.py` 就是 core API 的总索引。

**练习 3**：`python/asc/lib` 和 `python/asc/language` 都叫"库"，区别是什么？

**答案**：`language` 是**你写进 Kernel 代码里的编程 API**；`lib` 是**对 C++ 实现的动态代理**（Host 侧 tiling 计算、Device 侧运行时支持的 Python 壳），一般不直接出现在 Kernel 函数体里，而是被 runtime 或高阶 API 内部使用。

### 4.3 include 与 lib 的对应关系：后端代码的组织规律

#### 4.3.1 概念说明

后端代码遵循 C++ 惯例：`include/ascir/` 放声明（`.h` 头文件 + `.td` TableGen 定义），`lib/` 放实现（`.cpp`）。两者内部再按**同一条纵向轴**组织：

| 纵向轴 | include 位置 | lib 位置 | 职责 |
| --- | --- | --- | --- |
| Dialect（IR 定义） | `include/ascir/Dialect/Asc/IR/**.td` | `lib/Dialect/Asc/IR/*.cpp` | 定义 ASC-IR 有哪些 Op/Type/Attr |
| Transforms（Pass） | `include/ascir/Dialect/Asc/Transforms/` | `lib/Dialect/Asc/Transforms/*.cpp` | 改写、优化、合法化 IR |
| Target（发射） | `include/ascir/Target/Asc/**.h` | `lib/Target/AscendC/**/*.cpp` | 把 IR 翻译成 Ascend C 代码 |
| TableGen（生成器） | `lib/TableGen/include/` | `lib/TableGen/*.cpp` | 从 `.td` 生成 C++ 代码的扩展 backend |

而**横向**上，前端的 language 四象限与后端 IR 目录严格镜像：

```text
python/asc/language/basic  ←→  include/ascir/Dialect/Asc/IR/Basic/   ←→  lib/Target/AscendC/Basic/
python/asc/language/adv    ←→  include/ascir/Dialect/Asc/IR/Adv/     ←→  lib/Target/AscendC/Adv/
python/asc/language/core   ←→  include/ascir/Dialect/Asc/IR/Core/    ←→  lib/Target/AscendC/Core/
python/asc/language/fwk    ←→  include/ascir/Dialect/Asc/IR/Fwk/     ←→  lib/Target/AscendC/Fwk/
```

（注意大小写差异：Python 侧目录小写，C++ 侧目录首字母大写。）

有了这张镜像表，检索路径变成机械动作：**看到 Python API 文件名，把首字母大写、加上 `Op` 前缀相关命名，去对应后端目录找 `.td`；再按同样名字去 `lib/Target/AscendC` 找发射实现。**

#### 4.3.2 核心流程

一个 Python API 从"被调用"到"变成 Ascend C 代码"，在后端经历的文件层次：

```text
python/asc/language/basic/vec_binary.py        （用户 API：asc.add）
        │  builder.create_asc_AddL0Op(...)      ← pybind 暴露的 C++ 函数
        ▼
include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td   （defm Add : BinaryTemplateL0123Op）
        │  TableGen 生成 Op 的 C++ 类（AscendC_AddL0Op 等）
        ▼
lib/Dialect/Asc/IR/Ops.cpp 等                    （方言实现：Op 注册、验证）
        │  Pass 流水线改写 IR（lib/Dialect/Asc/Transforms/*.cpp）
        ▼
lib/Target/AscendC/Basic/ 或 include/ascir/Target/Asc/Basic/   （发射：printOperation）
        │
        ▼
Ascend C 代码（ascendc.cpp）→ 毕昇编译器 → Kernel 二进制
```

**一个重要提醒（检索陷阱）**：发射实现**不一定**有与 td 同名的 `.cpp`。例如 `OpVecBinary.td` 的发射逻辑写在头文件模板 [include/ascir/Target/Asc/Basic/VecBinary.h](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Basic/VecBinary.h) 里（`lib/Target/AscendC/Basic/` 下并没有 `VecBinary.cpp`），因为二元算子的发射模式高度统一，用 C++ 模板按 Op 类型参数化更省代码。**找不到同名 cpp 时，去 `include/ascir/Target/Asc/` 下找同名 `.h`。**

#### 4.3.3 源码精读

以三个 API 为例，把镜像关系逐一落到真实文件。

**例一：`asc.add`（basic 象限）**

- 前端实现在 [python/asc/language/basic/vec_binary.py:L40-L43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L40-L43)——`add` 的函数体只有几行：拿到全局 builder，然后调用 `op_impl("add", ..., builder.create_asc_AddL0Op, builder.create_asc_AddL1Op, builder.create_asc_AddL2Op)`。同文件 L22-L38 是它的三个 `@overload` 声明（count 版、mask int 版、mask list 版），用于类型检查和文档。
- IR 定义在 [include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td:L23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td#L23)——`defm Add : BinaryTemplateL0123Op<"add", "Add", "operator+">;` 一行就定义了 Add 的 L0/L1/L2/L3 四级 API 变体，这是 TableGen 模板威力的缩影。
- 发射实现在 [include/ascir/Target/Asc/Basic/VecBinary.h:L19-L24](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Basic/VecBinary.h#L19-L24)——模板函数 `printBinaryL0Params` 按 `dst, src0, src1, mask, repeatTimes, repeatParams` 的固定顺序拼出 Ascend C 调用参数。

**例二：`asc.data_copy`（basic 象限）**

- 前端实现在 [python/asc/language/basic/data_copy.py:L144-L160](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/data_copy.py#L144-L160)——`data_copy` 用 `OverloadDispatcher` 按参数形状分发：传 `DataCopyParams` 走 `create_asc_DataCopyL0Op`，传 `count`（int）走 `create_asc_DataCopyL2Op`。同文件 L62-L145 是约 20 个 `@overload` 声明，覆盖 GM↔UB、UB↔UB、ND↔NZ 等所有搬运组合。
- IR 定义在 [include/ascir/Dialect/Asc/IR/Basic/OpDataCopy.td:L76-L88](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpDataCopy.td#L76-L88)——`AscendC_DataCopyL0Op`（L76）与 `AscendC_DataCopyL2Op`（L83）分别对应上面两个分发分支；它们的公共基类 `DataCopyOp` 定义在 [include/ascir/Dialect/Asc/IR/Base.td:L75-L76](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L75-L76)。
- 发射实现在 [lib/Target/AscendC/Basic/DataCopy.cpp:L39-L75](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Basic/DataCopy.cpp#L39-L75)——这里有一组 `printOperation` 重载，分别处理 `DataCopySliceOp`、`CopyL0Op`、`CopyL1Op` 等，每个函数把一个 IR Op 打印成一条 Ascend C 语句。

**例三：`asc.TPipe`（fwk 象限）**

- 前端类定义在 [python/asc/language/fwk/tpipe.py:L365-L391](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L365-L391)——`class TPipe(IRValue)` 的构造函数调用 `create_asc_PipeOp()` 生成 IR；其 `init_buffer` 方法（L456-L465）为 TQue/TBuf 分配内存。
- IR 定义在 [include/ascir/Dialect/Asc/IR/Fwk/TPipe.td:L25-L40](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TPipe.td#L25-L40)——`AscendC_PipeOp`（实例化 pipe）与 `AscendC_TPipeInitBufferOp`（init_buffer 的 IR 形态）；队列相关操作在同目录 `TQue.td`（如 L25 起的 `AscendC_TQueBindAllocTensorOp` 等）。
- 发射实现在 [lib/Target/AscendC/Fwk/TQue.cpp:L20-L31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Fwk/TQue.cpp#L20-L31)——TQueBind 家族的 `printOperation` 重载；`lib/Target/AscendC/Fwk/` 下还有 `TBuf.cpp`，共同覆盖 fwk 象限的发射。

最后看后端工具与生成器：[bin/](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin) 下只有三个 cpp——[bin/ascir-opt.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin/ascir-opt.cpp)（跑 Pass 的 IR 优化器）、[bin/ascir-translate.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin/ascir-translate.cpp)（IR 与 Ascend C 互转）、[bin/ascir-lsp.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin/ascir-lsp.cpp)（语言服务器）；[lib/TableGen/](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/TableGen) 下的 `GenPybindDefs.cpp`、`GenOpEmitDefs.cpp` 等文件是 pyasc 对 LLVM TableGen 的扩展 backend，负责从 `.td` 生成 pybind 绑定和发射函数（第 5 单元详解）。

#### 4.3.4 代码实践

**实践目标**：独立完成"一个 API、三个文件"的检索，验证镜像规律。

**操作步骤**：

1. 任选一个你没见过的 basic 算子，例如 `asc.mul`（在 [python/asc/language/basic/vec_binary.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py) 中搜索 `def mul`）。
2. 在同文件找到它调用的 `builder.create_asc_Mul...` 函数名。
3. 用该函数名去掉 `create_asc_` 前缀，去 [include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td) 搜索对应的 `defm Mul`。
4. 再去 [include/ascir/Target/Asc/Basic/VecBinary.h](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Basic/VecBinary.h) 确认发射模板是否复用。
5. 换一个 fwk 算子重复以上过程，例如 `TQue.enque` → `fwk/tpipe.py` → `Fwk/TQue.td` → `lib/Target/AscendC/Fwk/TQue.cpp`。

**需要观察的现象**：`mul` 与 `add` 共用同一套 `BinaryTemplateL0123Op` 模板和同一个发射模板函数——"新增一个二元算子"往往只是加一行 `defm`。这正是目录镜像 + TableGen 模板带来的可扩展性。

**预期结果**：三个文件全部命中，且 `defm Mul` 与 `defm Add` 相邻（同在 OpVecBinary.td L23/L34 附近）。检索过程不依赖运行环境，可直接在代码托管页面完成。

#### 4.3.5 小练习与答案

**练习 1**：`include/ascir/Dialect/Asc/IR/Core/` 下的 `Types.td` 定义了 Tensor 类型，它的实现（手写 C++ 部分）在哪个文件？

**答案**：`lib/Dialect/Asc/IR/Types.cpp`。对应规律：`include/ascir/Dialect/Asc/IR/Xxx.td` 的实现在 `lib/Dialect/Asc/IR/Xxx.cpp`（Types.td→Types.cpp、Ops.td→Ops.cpp、Attributes.td→Attributes.cpp）。

**练习 2**：为什么 `lib/Target/AscendC/Basic/` 下没有 `VecBinary.cpp`，data_copy 却有 `DataCopy.cpp`？

**答案**：二元向量算子的发射输出格式完全一致（只有 API 名和操作数不同），适合用 C++ 模板参数化，于是实现放进头文件 `VecBinary.h` 的模板函数；data_copy 家族变体多、参数拼接逻辑差异大，模板覆盖不了，就落成独立的 `.cpp` 重载集合。检索口诀：**同名 cpp 不存在时，找同名 .h**。

**练习 3**：`include/ascir/Dialect/EmitAsc` 是什么，为什么不与 language 象限对应？

**答案**：EmitAsc 是"贴近 C 语法"的低层过渡方言，用于统一表达常量、循环、函数调用等通用结构（来自 MLIR 标准方言的 arith/scf/func 等），它不对应任何用户 API，所以没有 Python 侧镜像。它的外部方言降级实现在 `lib/Target/AscendC/External/`（Arith.cpp、Scf.cpp、Func.cpp 等 6 个文件）。

### 4.4 examples 与 test：示例与三套测试的分工

#### 4.4.1 概念说明

学一门算子语言，最快的路径是"照着示例改"。pyasc 提供了三层互相补充的样例/测试：

| 目录 | 内容 | 定位 |
| --- | --- | --- |
| `examples/` | 8 个端到端算子（Add → Matmul → GELU/SwiGLU/RMSNorm） | **教程**：教用户怎么写算子，带 README |
| `python/test/` | pytest 测试：`unit/`（按 codegen/language/lib/runtime 分目录）、`kernels/`（端到端 kernel 用例）、`generalization/` | **前端回归**：保证前端行为不变 |
| `test/` | lit 测试：`Dialect/`、`Target/`（`.mlir` 输入 + 期望输出）、`tools/`，外加 `build_llt.sh` | **后端回归**：保证 IR 与 Ascend C 发射不变 |

三者的关键差异在**判定标准**：examples 只求跑通并验证数值正确；`python/test/unit` 多数只要求"能生成合法的 Ascend C / IR 即可"（配合 mock），跑得快；`test/` 的 lit 用例则逐字节比对生成的代码文本。第 7 单元会展开测试体系，本讲只需记住"去哪儿找参考代码"。

#### 4.4.2 核心流程

想找"某个 API 怎么用"时的高效检索路径：

```text
想知道 asc.XXX 怎么用
   │
   ├─ 1. 查 docs/python-api/          （API 文档，按语言组织）
   ├─ 2. 搜 examples/                  （端到端用法，带 launch 代码）
   ├─ 3. 搜 python/test/unit/language/ （最小用法，通常更精简、覆盖更多重载）
   └─ 4. 搜 test/Dialect|Target/       （IR 形态与 Ascend C 输出，用于理解后端行为）
```

#### 4.4.3 源码精读

示例总览见 [examples/README.md:L5-L15](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/README.md#L5-L15)——8 个示例的功能表格：01_add（手动同步流水）、02_add_framework（框架自动同步）……08_rmsnorm（高阶 API + 行归一化）；推荐学习顺序在 [examples/README.md:L22-L23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/README.md#L22-L23) 起的编号列表，明确指出 01 是入门首选、02 引入 TPipe/TQue/TBuf 机制。这也正好对应我们 u1-l4、u2-l6 两讲的内容安排。

前端测试的组织见 [python/test/unit/language/basic/](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/language/basic)——文件名与 language/basic 的 API 文件一一对应（`test_data_copy.py` 对 `data_copy.py`、`test_vector_binary.py` 对 `vec_binary.py`）。以 [python/test/unit/language/basic/test_vector_binary.py:L13-L30](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/language/basic/test_vector_binary.py#L13-L30) 为例：`setup_function` 里把平台设为 Model（仿真器），`test_add_kernel` 里直接创建三个 `LocalTensor` 然后连调三种重载的 `asc.add`——**这就是"最小可运行用法"的活样本**，比 examples 更短，且天然覆盖边界。

后端测试见 [test/](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test)——`Dialect/AscendC` 与 `Target/AscendC` 下是 `.mlir` 用例（如 `fwk.mlir`、`emitasc.mlir`），`tools/` 下是工具测试；总控脚本 [test/build_llt.sh](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh) 负责构建并运行这批 lit 测试。

#### 4.4.4 代码实践

**实践目标**：体验三套目录各自的"参考价值"。

**操作步骤**：

1. 打开 [examples/01_add/add.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py) 通读一遍（如果已读过 u1 相关内容，可跳到步骤 2）。
2. 打开 [python/test/unit/language/basic/test_vector_binary.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/language/basic/test_vector_binary.py)，对比它使用 `asc.add` 的方式与 examples 的差异（不需要启动参数、不需要 torch 张量）。
3. 打开 `test/Dialect/AscendC/fwk.mlir`（或任一 `.mlir` 用例），观察它记录的是 IR 文本而非 Python 代码。

**需要观察的现象**：同一个 `asc.add`，在 examples 里是"完整算子的一部分"，在 unit 测试里是"三行最小调用"，在 lit 用例里则根本不出现（lit 层面只有 IR 和 C 代码）——**越靠近后端，抽象层级越低**。

**预期结果**：能说出"写新算子抄 examples、查 API 细节抄 unit 测试、调后端问题看 lit 用例"的分工。本实践为纯阅读，无运行结果；若想实际跑 unit 测试（`python3 -m pytest python/test/unit/language/basic/test_vector_binary.py`），需要先完成 u1-l2 的环境搭建，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：你想给一个冷门算子（如 `asc.brcb`）写最小验证代码，去哪里抄模板最快？

**答案**：`python/test/unit/language/basic/` 下找 `test_vector_*.py` 风格的用例；brcb 对应 `vec_brcb.py` API，若 unit 下暂无用例，则退回 `examples/` 找相近算子改写。unit 测试是最小、最不依赖环境的模板。

**练习 2**：`python/test/unit` 与 `python/test/kernels` 都在 Python 测试目录下，区别是什么？

**答案**：`unit` 是单元级测试，多数只验证"前端能正确生成 IR / Ascend C"，用 mock launcher，不真正执行；`kernels` 是端到端用例（如 `test_vadd.py`、`test_matmul.py`），需要在 Model 或 NPU 上真正执行并校验数值。

**练习 3**：后端 lit 测试为什么用 `.mlir` 文件做输入，而不是 Python 算子代码？

**答案**：lit 测试的目标是被测对象是后端（Pass、发射），直接以 IR 为输入可以精确控制测试点、逐字节比对输出，避免把前端 bug 混进来；同时不依赖 Python 环境，可在 C++ 构建流水线中并行运行。

## 5. 综合实践

完成本讲的**目录职责对照表**（这也是本讲规格中指定的实践任务）：挑选 `asc.data_copy`、`asc.TPipe`、`asc.add` 三个 API，分别在 **Python 前端**、**include 的 td 定义**、**lib 的实现**三个维度找到对应文件，并记录路径与关键行号。

参考答案（均已在本讲源码精读中验证过）：

| API | Python 前端 | include 的 td 定义 | lib 侧实现 |
| --- | --- | --- | --- |
| `asc.add` | [vec_binary.py:L40-L43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L40-L43)（函数体调 `create_asc_AddL0/1/2Op`） | [Basic/OpVecBinary.td:L23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td#L23)（`defm Add : BinaryTemplateL0123Op`） | 发射在头文件 [Target/Asc/Basic/VecBinary.h:L19-L24](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Basic/VecBinary.h#L19-L24)（模板，无同名 cpp） |
| `asc.data_copy` | [data_copy.py:L144-L160](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/data_copy.py#L144-L160)（OverloadDispatcher 分发） | [Basic/OpDataCopy.td:L76-L88](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpDataCopy.td#L76-L88)（`AscendC_DataCopyL0Op/L2Op`） | [lib/Target/AscendC/Basic/DataCopy.cpp:L39-L75](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Basic/DataCopy.cpp#L39-L75)（printOperation 重载） |
| `asc.TPipe` | [fwk/tpipe.py:L365-L391](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L365-L391)（`class TPipe`，构造调 `create_asc_PipeOp`） | [Fwk/TPipe.td:L25-L40](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TPipe.td#L25-L40)（`AscendC_PipeOp`、`TPipeInitBufferOp`；队列类在 `TQue.td`） | [lib/Target/AscendC/Fwk/TQue.cpp:L20-L31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Fwk/TQue.cpp#L20-L31)（另有 `TBuf.cpp`） |

操作步骤：

1. 自己先不看答案，从 `python/asc/language/__init__.py` 的 import 行出发，独立找到三个前端文件。
2. 用 `create_asc_XXXOp` 中的名字去 `include/ascir/Dialect/Asc/IR/` 下 `grep -rn "def.*XXX" --include="*.td"` 找 td 定义。
3. 再到 `lib/Target/AscendC/` 对应象限目录找 `printOperation` 实现；找不到同名 cpp 时转去 `include/ascir/Target/Asc/` 找同名 `.h`。
4. 与上表核对；不一致时以你的 grep 结果为准（仓库演进可能导致行号漂移）。

预期结果：三条链全部命中，并额外发现"镜像命名 + 陷阱兜底"两条规律。

## 6. 本讲小结

- **顶层分区**：`python/` 是前端（language/codegen/runtime/lib 四子包 + `_C` 二进制落点），`include/ascir/` + `lib/` 是后端（声明/实现分离），`bin/` 是三个开发者工具，`examples` 与两套 `test` 分别承担教学与回归。
- **入口即地图**：`python/asc/__init__.py` 只导出 `jit` + 三个 Options + `language` 全部内容；`python/asc/language/__init__.py` 的四行 `from . import adv/basic/core/fwk` 就是前端 API 的四象限户口。
- **镜像规律**：`language/{basic,adv,core,fwk}` ↔ `include/ascir/Dialect/Asc/IR/{Basic,Adv,Core,Fwk}` ↔ `lib/Target/AscendC/{Basic,Adv,Core,Fwk}` 三层目录名一一对应，检索时按象限直达。
- **检索链条**：Python API 文件里的 `builder.create_asc_XxxOp` → 去对应象限的 `.td` 找 `def/defm Xxx` → 去 `lib/Target/AscendC` 同象限找 `printOperation`；**找不到同名 cpp 时去 `include/ascir/Target/Asc/` 找同名 `.h`**（模板化发射，VecBinary 是典型）。
- **三层参考代码**：写新算子抄 `examples/`（端到端），查 API 最小用法抄 `python/test/unit/`（pytest + mock），调后端问题看 `test/`（lit + `.mlir` 逐字节比对）。

## 7. 下一步学习建议

本讲之后，地图已经建立，下一讲（u1-l4「运行第一个算子」）将带你**在地图上跑起来**：端到端运行 `examples/01_add/add.py`，观察 Model/NPU 两种模式。建议提前浏览：

1. [examples/01_add/add.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py) 与 [examples/README.md](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/README.md)——用本讲的四象限知识标注示例中每个 `asc.` 调用的来源文件。
2. [python/asc/runtime/jit.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py)——只看 `@asc.jit` 装饰器和 `__getitem__`（中括号启动语法的实现位置），为 u1-l5 的主链路走读热身。
3. 带着一个问题进入下一讲：`kernel[core_num, stream](...)` 这种"方括号调用"是 Python 的什么特性？它最终落到 `python/asc/runtime/` 的哪个文件？
