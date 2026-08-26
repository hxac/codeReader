# 仓库结构与三层架构

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 PyPTO 仓库「三层架构」的职责划分：C++ 核心层（`include/pypto` + `src`）、绑定层（`python/bindings`）、Python API 层（`python/pypto`）。
2. 在源码树中**快速定位**四类核心代码：IR 定义、编译 Pass、PTO 代码生成、运行时执行。
3. 知道 `docs/`、`examples/`、`tests/` 各自的作用与组织方式，会利用它们自助答疑。
4. 沿着一条真实的跨层调用链（`inline_functions`）把三层串起来，理解「改了 C++ 为什么要重新编译」这一在上一讲中只给出了结论的问题。

本讲是**纯阅读讲**：不需要写算子，只需要带着「找东西」的任务在仓库里漫游一遍。这张地图会在后续每一讲中反复使用。

## 2. 前置知识

### 2.1 什么是「分层架构」

把一个大系统拆成若干层，每层只跟相邻层对话。PyPTO 用三层来组织代码：

| 层 | 目录 | 语言 | 一句话职责 |
| --- | --- | --- | --- |
| ① C++ 核心层 | `include/pypto/`、`src/` | C++17 | 定义 IR、实现 Pass、生成 PTO 指令——编译器的「发动机」 |
| ② 绑定层 | `python/bindings/` | C++（nanobind） | 把 C++ 类和函数「翻译」成一个 Python 模块 `pypto_core` |
| ③ Python API 层 | `python/pypto/` | Python | DSL 语法、`@pl.jit`、Pass 管理器、运行时——用户直接接触的「方向盘」 |

**为什么要有绑定层？** C++ 编译出的 IR、Pass、Codegen 都是 C++ 对象，Python 无法直接使用。nanobind 是一个「胶水代码生成器」：你在 C++ 里写一行 `passes.def("inline_functions", &pass::InlineFunctions)`，构建后 Python 里就能 `passes.inline_functions()` 拿到一个 `Pass` 对象。

### 2.2 「头文件目录 + 实现目录」的 C++ 惯例

PyPTO 的 C++ 代码分两处放：

- `include/pypto/`：**公共头文件**（`.h`），声明「有什么」——类、函数签名、文档注释。
- `src/`：**实现**（`.cpp`），写「怎么做」。

两边的目录结构是**镜像**的：`include/pypto/ir/expr.h` 对应 `src/ir/expr.cpp`，`include/pypto/codegen/pto/` 对应 `src/codegen/pto/`。记住这个镜像规则，找到一个就能找到另一个。

### 2.3 上一讲的结论回顾

上一讲（u1-l2）我们说过：改 `python/pypto` 下的纯 Python 文件即时生效，改 C++ 部分（`src`、`include`、`python/bindings`）必须重新编译。本讲会把「为什么」讲透——因为绑定层把 C++ 编译成了一个二进制扩展模块，Python 层 import 的其实是这个产物。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| [README.md](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.md) | 项目定位、安装方式、文档站入口 |
| [AGENTS.md](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/AGENTS.md) | 仓库贡献约定，内含官方「Repository Map」 |
| [python/pypto/__init__.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/__init__.py) | Python 包入口：`import pypto` 时执行 |
| [docs/en/dev/index.md](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/index.md) | 开发者文档总目录：IR / Passes / Language / Codegen / Backend |
| [python/bindings/bindings.cpp](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/bindings.cpp) | nanobind 扩展模块 `pypto_core` 的定义入口 |
| [include/pypto/ir/expr.h](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/include/pypto/ir/expr.h) | IR 表达式节点定义（Var / Call / Submit / …） |
| [src/ir/transforms/inline_functions_pass.cpp](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/src/ir/transforms/inline_functions_pass.cpp) | 一个具体 Pass 的实现（本讲的跨层案例） |
| [src/codegen/pto/pto_codegen.cpp](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/src/codegen/pto/pto_codegen.cpp) | PTO 代码生成入口 |
| [python/pypto/runtime/runner.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/runtime/runner.py) | 运行时执行入口（`RunConfig` / `run()`） |

## 4. 核心概念与源码讲解

### 4.1 仓库顶层地图：一张表读懂根目录

#### 4.1.1 概念说明

面对一个新仓库，第一步不是读代码，而是**建立坐标系**。PyPTO 官方在 `AGENTS.md` 里维护了一份「Repository Map」，这正是本讲的出发点。顶层目录可以分为三类：

- **代码类**：三层架构的三块目录。
- **内容类**：文档、示例、测试。
- **构建类**：构建声明与第三方依赖（上一讲已讲，本讲只标注位置）。

#### 4.1.2 核心流程

拿到 PyPTO 仓库后的推荐浏览顺序：

```text
1. README.md           → 项目是什么、怎么装、怎么跑示例
2. AGENTS.md           → 仓库约定 + 官方目录地图
3. docs/en/dev/index.md → 开发者文档总目录（按子系统查文档的入口）
4. examples/           → 挑一个能跑的例子建立直觉
5. 三层代码目录         → 按本讲 4.2 ~ 4.5 逐层进入
```

#### 4.1.3 源码精读

官方仓库地图写在 AGENTS.md 的「Repository Map」一节，把每个顶层目录的职责讲得一清二楚：

[AGENTS.md:101-112](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/AGENTS.md#L101-L112) —— 逐条列出 `include/pypto/`、`src/`、`python/bindings/`、`python/pypto/`、`tests/ut|st|lint/`、`docs/en|zh/dev/`、`examples/` 各自放什么。这一节是官方版的三层架构说明，值得原文通读一遍。

结合实际目录内容，可以补充成下面这张完整地图（目录内容均已核对）：

| 顶层目录 | 类别 | 内容要点 |
| --- | --- | --- |
| `include/pypto/` | 代码① | 公共头文件，含 `core/`、`ir/`、`codegen/`、`backend/` 四个子目录 |
| `src/` | 代码① | C++ 实现，与 `include` 镜像：`src/ir/transforms/`（56 个 Pass 源文件）、`src/codegen/`（`pto/`、`orchestration/`、`distributed/`）、`src/backend/`（`910B/`、`950/`、`common/`） |
| `python/bindings/` | 代码② | nanobind 绑定：`bindings.cpp` 总入口 + `modules/` 下 11 个分模块 |
| `python/pypto/` | 代码③ | Python API：`language/`（DSL）、`jit/`、`ir/`、`runtime/`、`backend/`、`tools/`、`debug/`、`arith/`、`pypto_core/`（仅类型桩） |
| `docs/` | 内容 | `docs/en/`（英文，权威）+ `docs/zh/`（中文镜像，文件名保持英文），下分 `dev/`、`user/`、`api/`、`reference/` |
| `examples/` | 内容 | 按难度与主题分七个子目录：`beginner/`、`intermediate/`、`advanced/`、`models/`、`distributed/`、`runtime/`、`utils/` |
| `tests/` | 内容 | `ut/`（单元测试，11 个子目录）、`st/`（系统/硬件测试）、`lint/`（仓库专用 lint 脚本）、`docs/`（文档校验） |
| `3rdparty/` | 构建 | git 子模块：`msgpack-c`（序列化）、`libbacktrace`（错误栈回溯） |
| `cmake/`、`toolchain/`、`scripts/` | 构建 | CMake 辅助模块、工具链版本、文档站构建脚本 |
| `.claude/` | 构建 | 项目规则与 AI 工作流技能（本手册写作时也参考了它） |

其中「开发者文档总目录」是查文档的第一入口：

[docs/en/dev/index.md:9-18](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/index.md#L9-L18) —— 用一张表列出六个子章节：**IR**（节点层级、类型系统、算子、构建器）、**Passes**（Pass 框架与默认流水线里的每个 Pass）、**Language**（DSL 语法规范）、**Code Generation**（PTO-ISA 方言与编排 C++ 生成）、**Backend**（按架构的 `BackendHandler` 分发）、**Debug**（降级为 PyTorch 脚本做数值验证）。

[docs/en/dev/index.md:20-35](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/index.md#L20-L35) —— 顶层专题表：生态总览、编译剖析、错误处理（`CHECK` vs `INTERNAL_CHECK`）、日志、运行时 DFx、模拟器 trace 清洗等。**以后遇到任何编译器子系统的疑问，先从这张表跳转。**

两个值得现在就知道的组织约定：

- **Pass 文档编号 = 执行顺序**：`docs/en/dev/passes/` 下共 54 个文件，从 `00-pass_manager.md`、`01-inline_functions.md` 一路编号到 `47-materialize_valid_shape_symbols.md`，另有 `91-utility_passes.md`（工具类 Pass）、`99-verifier.md`（验证器基础设施）与 `index.md`。编号即 Pass 在默认流水线中的执行位置——这套编号与 `.claude/rules/pass-doc-ordering.md` 中的规则一致。
- **中英文档严格镜像**：`docs/zh/dev/passes/` 同样是 54 个文件，文件名保持英文，代码示例不翻译。英文版是权威（ground truth）。

#### 4.1.4 代码实践

**实践目标**：亲手核对上面那张顶层地图，而不是背下来。

**操作步骤**：

1. 在仓库根目录执行 `ls`，把输出与 4.1.3 的表格逐项对照。
2. 执行 `ls docs/en/dev/passes | wc -l` 和 `ls docs/zh/dev/passes | wc -l`，核对两处是否都是 54。
3. 执行 `ls examples`，确认七个示例子目录；再执行 `ls tests/ut`，数一数子目录个数。
4. 打开 [docs/en/dev/index.md](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/index.md)，把六行子章节表读一遍，并点进 `passes/index.md` 浏览 Pass 文档列表。

**需要观察的现象**：

- `docs/en` 与 `docs/zh` 的目录结构完全同名同数量；
- `examples/` 的子目录名（beginner/intermediate/…）直接反映了难度阶梯；
- `tests/ut/` 的子目录名（backend/codegen/core/cpp/debug/ir/jit/language/pass/runtime/tools）与代码模块基本一一对应。

**预期结果**：三步的输出都与 4.1.3 表格吻合（54 / 54 / 七个子目录 / 11 个 ut 子目录）。这些数字是本讲编写时实际核对的；若你手上的仓库版本更新过，个别数字可能变化，以你的输出为准。

#### 4.1.5 小练习与答案

**练习 1**：我想知道「某个 Pass 在默认流水线里第几个执行」，应该读哪个文件？为什么？

**答案**：读 `docs/en/dev/passes/` 下对应编号的文档文件名即可——例如 `07-outline_hierarchy_scopes.md` 说明该 Pass 是第 7 个执行。因为仓库约定 Pass 文档编号与 `python/pypto/ir/pass_manager.py` 中默认策略的执行顺序一致（`.claude/rules/pass-doc-ordering.md` 强制了这条规则）。更严谨的做法是再打开 `pass_manager.py` 交叉核对。

**练习 2**：中文文档和英文文档冲突时以谁为准？在哪里确认这条规则？

**答案**：以英文 `docs/en/` 为准。规则写在 `.claude/rules/documentation.md`（"English is the ground truth"），`docs/en/dev/index.md` 同样把中文列为镜像翻译。

**练习 3**：`tests/st/` 和 `tests/ut/` 的区别是什么？什么时候才会跑 `st`？

**答案**：`ut` 是单元测试，纯软件环境即可运行；`st` 是系统/硬件相关测试，需要真实加速器或特定环境（`AGENTS.md` 明确说仅在任务需要且硬件可用时运行）。日常开发只跑 `ut`。

---

### 4.2 C++ 核心层：`include/pypto` 与 `src`

#### 4.2.1 概念说明

这一层是编译器本体，回答四个问题的地方分别是：

| 问题 | 头文件位置 | 实现位置 |
| --- | --- | --- |
| IR 长什么样？ | `include/pypto/ir/` | `src/ir/` |
| IR 怎么被变换（Pass）？ | `include/pypto/ir/transforms/` | `src/ir/transforms/` |
| 指令代码怎么生成？ | `include/pypto/codegen/` | `src/codegen/` |
| 不同芯片怎么适配？ | `include/pypto/backend/` | `src/backend/` |

`include/pypto/core/` 则放与编译器无关的基础设施：`dtype.h`（数据类型枚举）、`error.h`（PyPTO 异常类型）、`logging.h`（日志）、`hash.h`、`common.h`。

#### 4.2.2 核心流程

C++ 层内部的数据流（也是后续 u4~u6 各讲的地图）：

```text
DSL 函数（Python）
   │  解析（Python 层完成）
   ▼
IR：Program → Function → Stmt / Expr / Type        ← include/pypto/ir/{program,function,stmt,expr,type}.h
   │
   ▼ 47 个 Pass 依次变换                            ← src/ir/transforms/*.cpp（56 个源文件）
   │    Tensor 级 → Tile 级 → 内存规划 → 收尾
   ▼
PTO 代码生成                                        ← src/codegen/pto/pto_codegen.cpp
   │    （host 编排部分走 src/codegen/orchestration/）
   ▼
后端按架构微调                                       ← src/backend/{910B,950,common}/
```

#### 4.2.3 源码精读

**（a）IR 表达式定义**。所有表达式节点的类都声明在 `include/pypto/ir/expr.h`，三个最重要的节点行号如下（用 `grep -n "^class " include/pypto/ir/expr.h` 可复现）：

- [include/pypto/ir/expr.h:213](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/include/pypto/ir/expr.h#L213) —— `class Var : public Expr`：变量绑定，IR 里最基础的具名值。
- [include/pypto/ir/expr.h:428](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/include/pypto/ir/expr.h#L428) —— `class Call : public Expr`：函数/算子调用，DSL 里每写一个 `tile.add(a, b)` 最终都会变成一个 `Call` 节点。
- [include/pypto/ir/expr.h:981](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/include/pypto/ir/expr.h#L981) —— `class Submit : public Expr`：任务发射（`pl.submit`），比 `Call` 多出依赖字段 `deps_` 和尾部 `TASK_ID` 返回。

同一个文件里还有 `MakeTuple`（1387 行）、`TupleGetItemExpr`（1421 行）等。**记住 `expr.h` 这个文件名——u4-l2 会整篇精读它。**

**（b）一个具体 Pass 的实现**。以第一个 Pass 为例：

- [src/ir/transforms/inline_functions_pass.cpp:718](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/src/ir/transforms/inline_functions_pass.cpp#L718) —— `Pass InlineFunctions()`：工厂函数，把 `FunctionType::Inline` 的函数体在所有调用点展开（splice），是默认流水线的第 1 个 Pass。它上方的注释块（682–717 行）完整描述了算法五步骤与边界情况——PyPTO 的 Pass 源码普遍带这种高质量注释，读 Pass 先读注释是高效习惯。

Pass 的声明统一收口在：

- [include/pypto/ir/transforms/passes.h:195](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/include/pypto/ir/transforms/passes.h#L195) —— `Pass InlineFunctions();` 的原型声明。所有 Pass 的工厂函数都在这个头文件里声明，新增 Pass 也必须在这里登记（u5-l8 实战会走完整流程）。

**（c）PTO 代码生成入口**：

- [src/codegen/pto/pto_codegen.cpp:668](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/src/codegen/pto/pto_codegen.cpp#L668) —— `std::string PTOCodegen::Generate(const ProgramPtr& program, ...)`：整个 PTO 代码生成的最外层入口，接收降级完成的 `Program`，返回 PTO 指令文本。同目录还有 `pto_control_flow_codegen.cpp`（控制流指令化）与 `pto_scalar_expr_codegen.cpp`（标量表达式指令化）。

**（d）后端目录**。`src/backend/` 下只有三个子目录：`910B/`、`950/`（两代架构各一份 `backend_<arch>.cpp`、`backend_<arch>_handler.cpp`、`backend_<arch>_ops.cpp`）和 `common/`（公共注册与配置）。这印证了 README 里「按架构分发」的设计——Pass 不写死 `if (arch == 910B)`，而是通过 `BackendHandler` 查询（u6-l3 详讲）。

#### 4.2.4 代码实践

**实践目标**：不看本讲答案，自己用 `grep` 定位 IR 表达式定义、任一个 Pass、PTO 代码生成入口。

**操作步骤**：

1. `grep -n "^class " include/pypto/ir/expr.h | head -20` —— 列出所有顶层表达式类及行号。
2. `ls src/ir/transforms/*.cpp | head` —— 浏览 Pass 文件命名规律（`<pass名>_pass.cpp`）。
3. `grep -n "class PTOCodegen" include/pypto/codegen/pto/*.h` —— 在头文件侧找到 codegen 类声明，再与 `src/codegen/pto/pto_codegen.cpp` 镜像对照。
4. `ls src/backend/910B src/backend/950` —— 观察两个架构目录的文件名对称性。

**需要观察的现象**：`expr.h` 里的类继承自 `Expr` 或 `IRNode`；`transforms` 目录的文件名都以 `_pass.cpp` 结尾（少数是共用工具文件，如 `init_memref.cpp`）；两个后端目录文件名完全对称。

**预期结果**：`grep` 输出的行号与本讲 4.2.3 给出的行号一致（213 / 428 / 981 / 718 / 195 / 668）。这些行号基于当前 HEAD `c7ba9fb0` 核对过；若代码更新，以你的 `grep` 结果为准。

#### 4.2.5 小练习与答案

**练习 1**：`include/pypto/ir/stmt.h` 大概放什么？怎么不用打开就猜到？

**答案**：IR 的语句节点（赋值、`ForStmt`、`IfStmt`、各类作用域语句）。依据有二：`expr.h` 放表达式，`stmt.h` 按命名惯例放语句；且 `docs/en/dev/index.md` 的 IR 子章节里明确有「Node hierarchy」文档。镜像实现就是 `src/ir/stmt.cpp`。

**练习 2**：`src/ir/verifier/` 下有 30 多个 `verify_*.cpp`，它们是 Pass 吗？

**答案**：不是流水线 Pass，而是**验证器**——在每个 Pass 之后检查 IR 是否满足不变量（如 `verify_inline_functions_eliminated.cpp` 检查第 1 个 Pass 之后确实不再有 Inline 函数）。它们通过 `PropertyVerifierRegistry` 注册，属于基础设施，文档在 `docs/en/dev/passes/99-verifier.md`（这也是它不占用 01–47 编号的原因）。

**练习 3**：为什么 Pass 声明要集中在 `include/pypto/ir/transforms/passes.h` 一个头文件里，而不是各自一个头文件？

**答案**：集中声明提供了一个稳定「注册表」入口——绑定层（`python/bindings/modules/passes.cpp`）与 Python Pass 管理器都从这里拿工厂函数；新增 Pass 时只要在声明、实现、绑定三处各登记一次即可（见 4.5 的跨层链路）。这是典型的「注册表模式」，代价是头文件变大，收益是任何一层都不需要散弹式修改。

---

### 4.3 绑定层：`python/bindings` 与 `pypto_core` 扩展模块

#### 4.3.1 概念说明

绑定层只有一个任务：**把 C++ 编译成一个名为 `pypto_core` 的 Python 扩展模块**。构建完成后，Python 里 `import pypto.pypto_core` 得到的不是 `.py` 文件，而是一个二进制扩展（`.so`）。

一个容易踩的认知坑：`python/pypto/pypto_core/` 目录下**没有任何 `.py` 文件**，只有 8 个 `.pyi` 类型桩（`__init__.pyi`、`ir.pyi`、`passes.pyi`、`codegen.pyi`、`backend.pyi`、`arith.pyi`、`logging.pyi`、`testing.pyi`）。桩文件只为 IDE 补全和类型检查服务，真正的实现在编译出来的二进制模块里。这就是「改 C++ 必须重编」的直接原因。

#### 4.3.2 核心流程

绑定层的组织是「一个总入口 + 11 个分模块」：

```text
python/bindings/bindings.cpp        ← NB_MODULE(pypto_core, m) 总入口
   ├── modules/error.cpp            → BindErrors(m)        异常类型
   ├── modules/core.cpp             → BindCore(m)          DataType 等基础类型
   ├── modules/testing.cpp          → BindTesting(m)       测试工具
   ├── modules/ir.cpp               → BindIR(m)            IR 节点（最大的一个）
   ├── modules/ir_builder.cpp       → BindIRBuilder(m)     Builder API
   ├── modules/passes.cpp           → BindPass(m)          Pass 与 PassContext
   ├── modules/logging.cpp          → BindLogging(m)       日志框架
   ├── modules/codegen.cpp          → BindCodegen(m)       代码生成
   ├── modules/backend.cpp          → BindBackend(m)       后端
   ├── modules/arith.cpp            → BindArith(m)         表达式化简工具
   └── modules/functor.cpp          → BindFunctor(m)       IRVisitor/IRMutator
```

`import pypto` 的时刻还会执行两道**导入期自检**，任何一道失败都会让 import 直接报错——这是上一讲「import 瞬间快速失败」结论的代码落点。

#### 4.3.3 源码精读

**（a）模块总入口**：

[python/bindings/bindings.cpp:29-78](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/bindings.cpp#L29-L78) —— `NB_MODULE(pypto_core, m)` 宏定义了扩展模块。函数体内按顺序调用 11 个 `Bind*` 函数把各子系统挂载进来，最后两行做导入期校验：

- [python/bindings/bindings.cpp:73](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/bindings.cpp#L73) —— `ValidateTileOps()`：校验所有 `tile.*` 算子都声明了内存规格，缺一个就在 import 时抛错（注释原文："fails at import time if any are missing"）。
- [python/bindings/bindings.cpp:77](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/bindings.cpp#L77) —— `ValidateArgEffects()`：校验每个原地算子都声明了它对所写槽位的效果，否则依赖边会丢失。

**（b）IR 绑定的挂载点**：

[python/bindings/modules/ir.cpp:288](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/modules/ir.cpp#L288) —— `void BindIR(nb::module_& m)`：IR 节点绑定的入口函数。该文件开头（89–101 行）还有一套基于字段描述符的反射绑定辅助 `BindField` / `BindFields`，把 C++ 类字段批量暴露成 Python 属性。

**（c）Pass 绑定（本讲跨层案例的桥）**：

[python/bindings/modules/passes.cpp:579](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/modules/passes.cpp#L579) —— `passes.def("inline_functions", &pass::InlineFunctions, ...)`：一行代码把 C++ 工厂函数 `pass::InlineFunctions` 以 Python 名字 `inline_functions` 暴露出去。这一行就是 4.5 节跨层链路的「桥墩」。

**（d）类型桩**：

[python/pypto/pypto_core/passes.pyi:685](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/pypto_core/passes.pyi#L685) —— `def inline_functions() -> Pass:`：与上面那行绑定一一对应的类型桩。`AGENTS.md:57-58` 的「Working Agreements」明确要求公共 API 变更必须同步 `include/pypto/`、`src/`、`python/bindings/`、`python/pypto/pypto_core/*.pyi` 四处——这就是「跨层同步」纪律。

#### 4.3.4 代码实践

**实践目标**：数清绑定层的结构，并确认 `pypto_core` 目录里只有类型桩。

**操作步骤**：

1. `ls python/bindings/modules/*.cpp | wc -l` —— 应得 11。
2. `ls python/pypto/pypto_core/` —— 应只看到 8 个 `.pyi`，没有 `.py`。
3. `grep -n "Bind.*(" python/bindings/bindings.cpp | head -15` —— 数一数总入口里挂载了几个子系统。
4. 用编辑器打开 [python/bindings/bindings.cpp:29-78](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/bindings.cpp#L29-L78)，对照每条 `Bind*` 调用读注释。

**需要观察的现象**：`NB_MODULE` 宏的参数 `pypto_core` 正是模块名；两行 `Validate*` 位于所有 `Bind*` 之后（先挂载完再校验）。

**预期结果**：11 个绑定源文件；`pypto_core` 目录无 `.py`；11 次 `Bind*` 调用 + 2 次校验。以上数字均已核对。

#### 4.3.5 小练习与答案

**练习 1**：我在 C++ 里给 `Function` 类加了一个新方法 `foo()`，改了哪几处才能在 Python 里用 `func.foo()`？

**答案**：四处——① `include/pypto/ir/function.h` 声明；② `src/ir/function.cpp` 实现；③ `python/bindings/modules/ir.cpp` 里加 `.def("foo", &Function::Foo, ...)`（注意转 snake_case）；④ `python/pypto/pypto_core/ir.pyi` 加类型桩。依据是 `.claude/rules/cross-layer-sync.md` 与 `AGENTS.md:57-58`。

**练习 2**：为什么 `ValidateTileOps()` 放在 import 时而不是等第一次编译时才检查？

**答案**：尽早失败（fail fast）。算子注册表的问题属于「环境装错了」，与任何具体程序无关；放到 import 期意味着装好环境的那一刻就能发现缺漏，而不是等到用户写完第一个算子、点运行才报一个看似与代码有关的错。这也解释了绑定层末尾那两条注释的措辞。

**练习 3**：`python/pypto/pypto_core/passes.pyi` 里为什么要有 `"inline_functions"` 这样的字符串条目（1017 行附近）？

**答案**：那是 `__all__` 导出清单，告诉类型检查器（pyright/mypy）哪些名字是模块的公开 API。桩文件不影响运行时行为，但决定了 IDE 补全与类型检查的完整性——漏写会导致「运行没问题但 IDE 不认识」的体验裂缝。

---

### 4.4 Python API 层：`python/pypto`

#### 4.4.1 概念说明

这一层是用户直接接触的面孔：`import pypto as pl` 拿到的就是它。它本身又由若干子包组成，每个子包对应一个使用场景：

| 子包 | 职责 | 你会在哪一讲深入 |
| --- | --- | --- |
| `language/` | DSL 本体：类型注解、算子包装、`pl.at` 作用域、解析器 | u2 全单元 |
| `jit/` | `@pl.jit` 装饰器、特化与缓存 | u2-l1 |
| `ir/` | `compile()`、`PassManager`、`python_print`、Builder 封装 | u3-l3、u3-l5 |
| `runtime/` | `RunConfig`、runner、设备执行、ELF 解析 | u3-l4 |
| `backend/` | `pto_backend.py`：调用 PTOAS 汇编产物 | u6-l3、u6-l6 |
| `tools/` | IR trace、内存地图等分析工具 | u7-l5 |
| `debug/` | 把 IR 降级成 PyTorch 脚本做数值验证 | （自查） |
| `arith/` | 表达式化简工具 | — |
| `pypto_core/` | 仅类型桩（见 4.3） | — |

#### 4.4.2 核心流程

`import pypto` 时发生的事（衔接 4.3 的导入期自检）：

```text
import pypto
  └─ 执行 python/pypto/__init__.py
       ├─ from . import compile_profiling, ir, language, runtime   ← 拉起四大子包
       │     ├─ language 又会拉起 parser / op / scope / typing …
       │     └─ ir 会拉起 builder / pass_manager / printer …
       └─ from .pypto_core import (DataType, passes, codegen, …)   ← 触发二进制扩展加载
             └─ NB_MODULE(pypto_core) 里跑完两道 Validate* 自检
```

也就是说：**一次 `import pypto` 就把三层全部点亮**——Python 层组织 API，绑定层加载二进制，C++ 层完成注册表自检。

#### 4.4.3 源码精读

**（a）包入口**：

[python/pypto/__init__.py:19-39](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/__init__.py#L19-L39) —— 包的全部「顶层资产」只有两块：一行 `from . import compile_profiling, ir, language, runtime` 拉起纯 Python 子包，一段 `from .pypto_core import (...)` 从二进制扩展再导出 `DataType`、`passes`、`codegen`、`testing`、日志函数与异常类型。**注意顶层没有 `jit`、`backend` 等子包——它们通过 `pypto.language` / `pypto.backend` 二级路径访问，这是刻意的命名空间收敛。**

[python/pypto/__init__.py:42-62](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/__init__.py#L42-L62) —— 一批 `DT_*` 常量（`DT_FP16`、`DT_FP32`…）只是给 `DataType` 枚举成员起的别名，用 `cast` 标注类型。这解释了为什么示例里既能写 `pl.FP32` 也能写 `pl.DT_FP32`。

**（b）DSL 入口**。`import pypto.language as pl` 的面貌：

[python/pypto/language/__init__.py:13-39](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L13-L39) —— 模块文档字符串自述职责："Type-safe DSL API for writing IR functions"，并给出三个典型用法（Tensor 级函数、Tile 级 block 函数、Scalar 函数）。**读任何 PyPTO 子模块，先读这段 docstring，它是官方给的「使用说明书」。**

[python/pypto/language/__init__.py:242-244](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L242-L244) —— 三行 import 把 DSL 的三件套挂到 `pl.` 命名空间上：

- `from .parser.decorator import InlineFunction, function, inline, program` —— `@pl.function` / `@pl.program` 装饰器；
- `from .parser.text_parser import loads, loads_program, parse, parse_program` —— 文本 IR 解析（不走 Python AST 的那条路）；
- `from .scope import ScopeMode, manual_scope, scope, spmd_submit, submit` —— 作用域与任务发射（u7-l1 主题）。

**（c）DSL 解析器与运行时入口**（本讲只需知道位置）：

- [python/pypto/language/parser/ast_parser.py:10](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/parser/ast_parser.py#L10) —— 模块 docstring 一句话说明职责："AST parsing for converting Python DSL to IR builder calls"。`parser/` 目录下还有 `type_resolver.py`、`span_tracker.py`、`diagnostics/` 等配套，u3-l1 整讲精读。
- [python/pypto/runtime/runner.py:155](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/runtime/runner.py#L155) —— `class RunConfig`：一次运行的全部可配置项（DFx 开关、ring 大小、golden 对照等）。
- [python/pypto/runtime/runner.py:628](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/runtime/runner.py#L628) —— `def run(...)`：把编译产物送上设备执行的入口函数。u3-l4 精读。

#### 4.4.4 代码实践

**实践目标**：从 `python/pypto/__init__.py` 出发，画出「`import pypto as pl` 之后 `pl.` 下面有什么」的清单。

**操作步骤**：

1. 读 [python/pypto/__init__.py:64-112](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/__init__.py#L64-L112) 的 `__all__`，数出模块级导出与 dtype 常量两大块。
2. `grep -n "^from .op import\|^from .parser import\|^from .scope import" python/pypto/language/__init__.py` —— 列出 `pl.` 命名空间挂载了哪些子模块。
3. （可选，需已构建）`python -c "import pypto as pl; print(pl.__version__); print(pl.DT_FP32)"` 验证导入路径。

**需要观察的现象**：`__all__` 里既有 `"ir"`、`"language"` 这样的子包名，也有 `"DT_FP16"` 这样的常量名；`language/__init__.py` 用 `from .op import tile_ops as tile` 的方式把算子按命名空间挂成 `pl.tile`、`pl.tensor`、`pl.system`、`pl.array`。

**预期结果**：第 1、2 步是纯文件阅读，结果确定（`__version__` 为 `"0.1.0"`，见 [python/pypto/__init__.py:114](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/__init__.py#L114)）。第 3 步依赖本地已编译好 `pypto_core` 扩展，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`pl.Tensor`、`pl.load`、`pl.submit` 分别定义在哪个文件？

**答案**：都在 `python/pypto/language/` 之下，但入口统一在 `language/__init__.py`：`Tensor` 来自 `pypto.ir` 的视图类型（40 行 `from pypto.ir import TensorView, TileView` 之后的一批导入）；`load` 来自 `.op.tile_ops`（120/155 行的 `from .op.tile_ops import (...)`）；`submit` 来自 `.scope`（244 行）。要找任何一个 `pl.xxx` 的定义，先在 `language/__init__.py` 里 grep 它来自哪个子模块，再顺着跳。

**练习 2**：为什么 `pypto.language` 要同时提供 `parse/loads`（文本解析）和 AST 解析两条路？

**答案**：文本解析服务于「IR 字符串 → IR 对象」的场景（比如把 `python_print` 的输出读回来做 round-trip 测试，或从 `.pto`/文本片段恢复 IR）；AST 解析服务于「用户写的 Python DSL → IR」。两条路在 `language/parser/` 下分别是 `text_parser.py` 与 `ast_parser.py`，u3-l1 与 u4-l7 会分别用到。

**练习 3**：`python/pypto/runtime/` 下有 20 来个文件，第一眼看哪个？

**答案**：`runner.py`。它是运行时的总入口（`RunConfig` 在 155 行、`run()` 在 628 行），其余文件（`device_runner.py`、`kernel_compiler.py`、`task_interface.py`、`elf_parser.py`…）都被它直接或间接调用。u3-l4 会按这条入口展开。

---

### 4.5 三层串联：沿 `inline_functions` 走一遍跨层调用链

#### 4.5.1 概念说明

前面四节分别看了每一层。现在把三层接起来。选 `inline_functions`（默认流水线的第 1 个 Pass）作案例，因为它足够小、又在五个地方各留了一个落点——**每一层一个，加上类型桩**。这条链路是「跨层同步」纪律的具象化：改任何一个 Pass 的名字或签名，五个文件都要动。

#### 4.5.2 核心流程

`passes.inline_functions` 从 C++ 到 Python 流水线的完整路径：

```text
① 声明   include/pypto/ir/transforms/passes.h:195      Pass InlineFunctions();
② 实现   src/ir/transforms/inline_functions_pass.cpp:718  Pass InlineFunctions() { ... }
③ 绑定   python/bindings/modules/passes.cpp:579         passes.def("inline_functions", &pass::InlineFunctions)
④ 类型桩  python/pypto/pypto_core/passes.pyi:685          def inline_functions() -> Pass:
⑤ 使用   python/pypto/ir/pass_manager.py:158            passes.inline_functions,
```

注意 ③ 处发生的**命名转换**：C++ 侧是 `InlineFunctions`（大驼峰），Python 侧暴露为 `inline_functions`（蛇形）——这是 `.claude/rules/cross-layer-sync.md` 强制的绑定命名约定。

#### 4.5.3 源码精读

- [include/pypto/ir/transforms/passes.h:195](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/include/pypto/ir/transforms/passes.h#L195) —— ① 声明。上方的 docstring（176–194 行）写明该 Pass 的语义保证："After this pass, no Function with func_type == Inline remains"。
- [src/ir/transforms/inline_functions_pass.cpp:718](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/src/ir/transforms/inline_functions_pass.cpp#L718) —— ② 实现：收集 Inline 函数 → 环检测 → 迭代 splice 到不动点 → 清除 Inline 函数。
- [python/bindings/modules/passes.cpp:579](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/modules/passes.cpp#L579) —— ③ 绑定：把 C++ 符号挂到 Python 名字上。
- [python/pypto/pypto_core/passes.pyi:685](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/pypto_core/passes.pyi#L685) —— ④ 类型桩：IDE 据此提供补全与签名。
- [python/pypto/ir/pass_manager.py:158](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/ir/pass_manager.py#L158) —— ⑤ 使用：默认策略 `tensor_prefix_passes` 元组的第一个元素，其上方注释（155–157 行）解释了它为什么必须最先跑。

这条链也回答了 2.3 节遗留的问题：**改 C++ 为什么要重编？** 因为 ⑤ 的 `passes.inline_functions` 在运行时指向 ③ 绑定进二进制 `.so` 的函数指针；`.so` 不重编，⑤ 拿到的永远是旧实现。而 `passes.pyi` 只是给 IDE 看的说明，改它不产生任何运行时效果——这也是初学者常见的「改了桩文件以为生效了」的坑。

#### 4.5.4 代码实践

**实践目标**：不借助本讲答案，用 `grep` 自己走通这条五点链路。

**操作步骤**：

1. `grep -rn "InlineFunctions" include/pypto/ir/transforms/passes.h src/ir/transforms/inline_functions_pass.cpp` —— 找 ①②。
2. `grep -n "inline_functions" python/bindings/modules/passes.cpp` —— 找 ③（顺带能看到 `IRProperty::InlineFunctionsEliminated` 的枚举绑定，即该 Pass 产出的 IR 属性）。
3. `grep -n "def inline_functions" python/pypto/pypto_core/passes.pyi` —— 找 ④。
4. `grep -n "passes.inline_functions" python/pypto/ir/pass_manager.py` —— 找 ⑤。
5. 把五处的文件路径、行号抄成一张五列表格，并在每格写一句「这层做了什么」。

**需要观察的现象**：五个文件横跨三个目录层；C++ 名 `InlineFunctions` 与 Python 名 `inline_functions` 大小写风格不同；`pass_manager.py` 中该 Pass 出现在流水线元组的第一个位置。

**预期结果**：行号与 4.5.3 一致（195 / 718 / 579 / 685 / 158，均基于 HEAD `c7ba9fb0` 核对）。这条链路是后面 u5-l8「动手写一个新 Pass」的预习——那时你要自己为新增的 Pass 把这五个落点全部补齐。

#### 4.5.5 小练习与答案

**练习 1**：如果把 ③ 处的绑定名字写成 `passes.def("inline_fn", ...)` 而不改其他层，会发生什么？

**答案**：⑤ 处 `passes.inline_functions` 在运行时抛 `AttributeError`（二进制模块里已经没有这个名字），同时 ④ 的桩文件仍在骗 IDE 说 `inline_functions` 存在——于是出现「IDE 有补全、运行就崩」的典型分层不同步症状。这正是跨层同步规则存在的理由。

**练习 2**：`IRProperty::InlineFunctionsEliminated`（见 ③ 同文件 87 行附近的绑定）是做什么的？

**答案**：这是一个 **IR 属性**（IRProperty）。Pass 用 `PassProperties` 声明自己「产出」了它，后续 Pass 可以声明「依赖」它，验证器（`src/ir/verifier/verify_inline_functions_eliminated.cpp`）据此在每个 Pass 之后检查不变量是否仍成立。相当于给流水线装了一张「契约清单」——u5-l1 详讲。

**练习 3**：为什么流水线在 Python 层（`pass_manager.py`）组织，而 Pass 全是 C++ 实现？

**答案**：策略与机制分离。机制（每个 Pass 怎么变换 IR）是热路径，用 C++ 保证性能；策略（跑哪些 Pass、按什么顺序、何时 dump）是实验性配置，用 Python 写便于快速调整、子类化和被用户脚本自定义。绑定层恰好让这种「C++ 机制 + Python 策略」的组合成本最低。

## 5. 综合实践

**任务：亲手制作一张「PyPTO 仓库地图」**，标注出以下五个关键代码位置所在的目录与文件，并为每处写一句职责说明：

1. IR 表达式定义
2. 一个具体 Pass 的实现
3. PTO 代码生成入口
4. Python DSL 解析器
5. 运行时 runner

**要求**：

- 每个条目给出「目录 → 文件 → 关键行号」，并附上基于当前 HEAD 的永久链接；
- 五个条目中至少标出各自属于三层架构的哪一层；
- 用 `grep`/`ls` 自行定位，不要直接抄本讲答案——做完再对照。

**参考答案**（自查用，均已在 HEAD `c7ba9fb0` 下核对）：

| # | 要找的东西 | 目录 | 文件:行 | 层 | 职责一句话 |
| --- | --- | --- | --- | --- | --- |
| 1 | IR 表达式定义 | `include/pypto/ir/` | [expr.h:213](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/include/pypto/ir/expr.h#L213)（`Var`；`Call` 在 428、`Submit` 在 981） | C++ 核心层 | 声明所有 IR 表达式节点类 |
| 2 | 具体 Pass 实现 | `src/ir/transforms/` | [inline_functions_pass.cpp:718](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/src/ir/transforms/inline_functions_pass.cpp#L718)（`Pass InlineFunctions()`） | C++ 核心层 | 流水线第 1 个 Pass：内联展开 Inline 函数 |
| 3 | PTO 代码生成入口 | `src/codegen/pto/` | [pto_codegen.cpp:668](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/src/codegen/pto/pto_codegen.cpp#L668)（`PTOCodegen::Generate`） | C++ 核心层 | 把降级后的 Program 生成 PTO 指令文本 |
| 4 | Python DSL 解析器 | `python/pypto/language/parser/` | [ast_parser.py:10](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/parser/ast_parser.py#L10)（模块 docstring 处） | Python API 层 | 把 Python AST 翻译成 IR Builder 调用 |
| 5 | 运行时 runner | `python/pypto/runtime/` | [runner.py:155](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/runtime/runner.py#L155)（`RunConfig`）、[runner.py:628](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/runtime/runner.py#L628)（`run()`） | Python API 层 | 配置并执行一次设备运行 |

**加分项**：在地图上再补第六个条目——绑定层把 ② 的 Pass 暴露给 Python 的那一行（[python/bindings/modules/passes.cpp:579](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/modules/passes.cpp#L579)），并用箭头把 ②→⑥→Python 流水线（[pass_manager.py:158](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/ir/pass_manager.py#L158)）连成一条线。这条线就是 4.5 节的跨层调用链。

## 6. 本讲小结

- PyPTO 仓库是**三层架构**：C++ 核心层（`include/pypto` + `src`，含 IR / Pass / codegen / backend 四类代码）、绑定层（`python/bindings`，产出二进制扩展 `pypto_core`）、Python API 层（`python/pypto`，DSL / jit / ir / runtime）。
- C++ 侧 `include` 与 `src` 目录**镜像**：`include/pypto/ir/expr.h` ↔ `src/ir/expr.cpp`；IR 表达式在 `expr.h`（`Var`:213 / `Call`:428 / `Submit`:981），Pass 实现在 `src/ir/transforms/`（56 个源文件），PTO 生成入口是 `PTOCodegen::Generate`（`pto_codegen.cpp:668`）。
- `python/pypto/pypto_core/` **只有 `.pyi` 类型桩、没有 `.py`**——真正的模块是编译出来的二进制；这就是「改 C++ 必须重编、改桩文件不产生运行时效果」的根源。
- 一次 `import pypto` 依次拉起 Python 子包 → 加载二进制扩展 → 跑 `ValidateTileOps()` / `ValidateArgEffects()` 两道导入期自检（`bindings.cpp:73/77`）。
- **跨层同步**纪律：一个 API 横跨声明（`passes.h`）、实现（`*_pass.cpp`）、绑定（`bindings/modules/*.cpp`）、类型桩（`*.pyi`）、使用（`pass_manager.py`）五个落点，改名/改签名要五处齐动。
- `docs/en/dev/index.md` 是开发者文档总入口；Pass 文档编号即执行顺序（`01`–`47`，另有 `91`/`99` 特殊编号）；`docs/zh` 与 `docs/en` 严格镜像，英文为权威。

## 7. 下一步学习建议

下一讲（u1-l4「Hello World 逐行精读」）将拿 `examples/beginner/01_hello_world.py` 逐行拆解 `@pl.jit`、`pl.at`、`pl.load/pl.store`。在进入下一讲之前，建议先做两件事巩固本讲：

1. **把地图用起来**：打开 `examples/beginner/01_hello_world.py`，对其中出现的每个 `pl.xxx`，用 4.4.3 介绍的方法（在 `language/__init__.py` 里 grep 它的来源）回溯到定义文件。
2. **预习性浏览**：翻一遍 [docs/en/dev/passes/00-pass_manager.md](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/passes/00-pass_manager.md)，对照本讲 4.2.2 的数据流示意图，看看能否把 47 个 Pass 的分组（前端优化 / 作用域外提 / Tile 降级 / 内存规划 / 收尾）对号入座——这会是 u3-l5 的主线。
