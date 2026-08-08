# 目录结构与项目布局

## 1. 本讲目标

学完本讲，你应当能够：

- 在脑海中建立一张「`xls/` 顶层目录 → 编译流水线某一阶段」的心智地图。
- 区分 XLS 的六大功能层：**前端 → IR → 优化 → 调度 → 代码生成 → 执行与验证**。
- 当你想研究某个具体功能（例如「DSLX 是怎么被解析的」「流水线是怎么调度的」「Verilog 是怎么生成的」）时，能**立刻定位到正确的目录与代表性文件**，而不是在几千个源文件里漫无目的地翻找。

本讲是「认路」讲义：我们不深入任何算法细节，只帮你把仓库的物理布局和编译器的逻辑阶段对应起来。一旦路认熟了，后续每一篇讲义你都能迅速找到它讲的代码在哪儿。

## 2. 前置知识

阅读本讲前，请确保你已经具备以下认知（它们来自前置讲义）：

- **XLS 是什么**（来自 `u1-l1`）：XLS 是一套**高层综合（HLS）工具链**，把高层功能描述翻译成可综合的 Verilog/SystemVerilog。它有两条前端入口——主推的 **DSLX**（`xls/dslx`）与实验性的 **C++/xlscc**（`xls/contrib/xlscc`），二者都汇入唯一的「真相之锚」**XLS IR**，再走相同的「优化 → 流水线调度 → 代码生成」流程。
- **怎么构建**（来自 `u1-l2`）：XLS 用 **Bazel** 构建，命令形如 `bazel build -c opt //xls/dslx:interpreter_main`，产物落在 `./bazel-bin/` 下。理解「目标（target）」「规则（rule）」这两个 Bazel 概念即可。

此外你需要一个朴素的常识：一个大型 C++ 项目通常会把代码按「职责」拆进不同子目录，XLS 正是这样，而且拆分得**和编译流水线几乎一一对应**，这正是本讲要帮你建立的对应关系。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目的「Stack Diagram and Project Layout」小节，是本讲**最权威的目录说明来源**。 |
| `xls/BUILD` | `xls/` 顶层包的 Bazel 文件，定义了可见性分组（package group），透露出哪些是公开 API。 |
| 各目录下的代表性源文件 | 用作每条流水线阶段的「路标」，本讲会逐一指出。 |

> 小提示：本讲的「源码」主要是**目录与文件本身**，而不是某段复杂算法。请把重点放在「这个目录是干什么的」上。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 顶层目录职责表**：把 `xls/` 下每个子目录映射到一句话职责。
- **4.2 编译阶段分层**：把目录按编译流水线的先后顺序重新组织，让你看清数据「从哪里流到哪里」。

### 4.1 顶层目录职责表

#### 4.1.1 概念说明

XLS 仓库的根目录长这样（节选与本讲相关的部分）：

```
google-xls/
├── dependency_support/   # 外部依赖的 Bazel 配置
├── docs_src/             # 文档 Markdown 源（渲染成 docs 站点）
├── third_party/          # 第三方代码
├── dist/                 # 发布打包相关
└── xls/                  # ★ 项目主体，本讲的主角
```

和很多 Bazel 项目一样，真正的源码并不在仓库根目录，而是被收拢进一个与项目同名的子目录 **`xls/`**。README 里对这一点有明确说明：`xls` 是「Project-named subdirectory within the repository, in common Bazel-project style」——见 [README.md:229-230](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L229-L230)（这段文字说明 `xls/` 是 Bazel 风格的项目同名子目录）。

我们的任务，就是搞清楚 `xls/` 内部那三十多个子目录各自负责什么。

#### 4.1.2 核心流程

读懂目录布局的「流程」其实是一个**查表习惯**：

1. 先想清楚你要解决的问题属于编译流水线的哪一阶段。
2. 到本讲的「目录职责表」里查这个阶段对应哪个目录。
3. 进该目录，挑一个与目录名同名的核心头文件读起（例如 `ir/` 目录的核心是 `xls/ir/package.h`、`xls/ir/op.h`）。

这套习惯能在几千个源文件的仓库里为你节省大量翻找时间。

#### 4.1.3 源码精读

README 的 **「Stack Diagram and Project Layout」** 小节是官方对目录布局的权威说明，它配了一张栈图，并逐一描述了每个目录。整段从 [README.md:213-218](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L213-L218) 开始（标题与「为什么要有这张图」的引子）。

下面这张表把 README 里描述的目录与职责整理在一起（职责文字依据 README 原文），并补上每个目录的代表性文件：

| 目录 | 一句话职责（依据 README） | 代表性文件 |
| --- | --- | --- |
| `xls/dslx` | DSLX：模仿 Rust、不可变的**表达式式数据流 DSL**，带硬件友好特性（任意位宽、定长、可静态分析调用图）。见 [README.md:256-262](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L256-L262)。 | `xls/dslx/frontend/ast.h`、`xls/dslx/interpreter_main.cc` |
| `xls/contrib/xlscc` | 实验性 **C++ 语法前端**，把 C++ 翻译成 XLS IR，是 DSLX 之外的另一条入口。见 [README.md:243-247](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L243-L247)。 | `xls/contrib/xlscc/` 下的转译器源码 |
| `xls/ir` | **XLS IR** 定义、文本解析器/格式化器、以及抽象求值设施——整个编译器的枢纽。见 [README.md:277-279](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L277-L279)。 | `xls/ir/package.h`、`xls/ir/op.h` |
| `xls/passes` | 跑在 IR 上的**优化 Pass**，在调度/代码生成之前执行。见 [README.md:289-291](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L289-L291)。 | `xls/passes/arith_simplification_pass.h` |
| `xls/scheduling` | **调度算法**：决定每个操作在时钟设计中何时执行（例如落到哪一级流水线）。见 [README.md:292-294](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L292-L294)。 | `xls/scheduling/pipeline_schedule.h` |
| `xls/estimators/delay_model` | 刻画/描述/插值每个 IR 操作在目标工艺下的**延迟**，是调度的输入。见 [README.md:251-255](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L251-L255)。 | `xls/estimators/delay_model/delay_estimator.h` |
| `xls/codegen` | **Verilog AST（VAST）** 与各类**生成器**（Pipeline/Sequential 等），把 IR 翻译成 Verilog/SystemVerilog 与状态机。见 [README.md:234-238](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L234-L238)。 | `xls/codegen/pipeline_generator.h`、`xls/codegen/vast/vast.h` |
| `xls/interpreter` | XLS IR 的**解释器**，用于调试与探索。见 [README.md:274-276](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L274-L276)。 | `xls/interpreter/block_evaluator.h` |
| `xls/jit` | 基于 LLVM 的 **JIT**，让 DSLX 与 IR 以接近原生的速度运行。见 [README.md:280-281](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L280-L281)。 | `xls/jit/function_jit.h` |
| `xls/simulation` | 封装 Verilog 仿真器、生成测试台，验证 codegen 产物。见 [README.md:295-299](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L295-L299)。 | `xls/simulation/module_simulator.h` |
| `xls/solvers` | 把 IR 翻译成 **SMT 求解器**输入，支持形式化证明（如 IR 与网表的逻辑等价检查）。见 [README.md:300-304](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L300-L304)。 | `xls/solvers/z3_ir_translator.h` |
| `xls/netlist` | 解析/分析网表级描述（结构化 Verilog + 单元库），为 LEC 等下游提供基础。见 [README.md:285-288](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L285-L288)。 | `xls/netlist/cell_library.h` |
| `xls/fuzzer` | 全栈多进程 Fuzzer：在 DSL 层生成程序，并交叉比较四套执行引擎。见 [README.md:263-268](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L263-L268)。 | `xls/fuzzer/ast_generator.h` |
| `xls/tools` | 大量**命令行工具**，以解耦的方式调用 XLS 各库。见 [README.md:311-314](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L311-L314)。 | `xls/tools/opt_main.cc`、`xls/tools/codegen_main.cc` |
| `xls/build_rules` | 把「DSL→IR→codegen」封装成可复用 Bazel 宏的**构建规则**。见 [README.md:231-233](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L231-L233)。 | `xls/build_rules/xls_dslx_rules.bzl` |
| `xls/common` | 基础工具，在标准库之上叠加（多用 Abseil 版本）。见 [README.md:239-242](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L239-L242)。 | `xls/common/` 下的通用头文件 |
| `xls/data_structures` | 通用数据结构（BDD、并查集、最小割等）。见 [README.md:248-250](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L248-L250)。 | `xls/data_structures/` 下的 BDD 等 |
| `xls/examples` | 经过测试、可执行的示例计算。见 [README.md:269-271](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L269-L271)。 | `xls/examples/gcd.x` |
| `xls/modules` | 硬件积木式的 DSLX「库」（在标准库之外）。见 [README.md:282-284](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L282-L284)。 | `xls/modules/` 下的 DSLX 模块 |
| `xls/tests` | 跨顶层组件的集成测试。见 [README.md:308-310](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L308-L310)。 | `xls/tests/` 下的集成测试 |
| `xls/synthesis` | 封装后端综合流程（ASIC/FPGA 等）的接口。见 [README.md:305-307](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L305-L307)。 | `xls/synthesis/` 下的综合封装 |
| `xls/visualization` | 交互式可视化工具。见 [README.md:315-317](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L315-L317)。 | `xls/visualization/` 下的可视化工具 |

此外，仓库里还有一些 README 未单列、但在目录中存在的子目录，它们多属于**正在演进 / 较新或较内部**的能力，了解即可：

| 目录 | 大致用途（按命名与上下文推断） |
| --- | --- |
| `xls/codegen_v_1_5` | 新一代代码生成流水线（Block 转换 Pass 管线）的演进版本。 |
| `xls/flows` | 把工具链以库形式**串联编排**的高层流程封装（如 `ir_wrapper`）。 |
| `xls/dev_tools` | 开发辅助脚本（如生成 `compile_flags.txt` 的 `make-compilation-db.sh`）。 |
| `xls/noc`、`xls/fdo`、`xls/eco` | 片上网络（NoC）、反馈导向优化（FDO）、工程变更（ECO）等较专项的能力。 |
| `xls/public`、`xls/protected` | 公开 API 收口与受保护目标相关的组织。 |

> 这些「未列目录」的具体定位在快速迭代中可能变化，遇到时以目录内的 `BUILD` 与文件名作为主要线索即可（标注「待确认」也完全正常）。

关于「哪些是公开 API」，可以读 `xls/BUILD` 顶部的可见性分组。例如 [`xls/BUILD:24-29`](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/BUILD#L24-L29) 定义了名为 `xls_public` 的 package group，注释说明被列入其中的目标是「intended public API of XLS，相对稳定」——这告诉你哪些目录是面向使用者、哪些是内部实现。

#### 4.1.4 代码实践

这是一道**源码阅读型**寻路练习，不需要编译运行。

1. **实践目标**：训练「问题 → 目录」的快速定位能力。
2. **操作步骤**：
   - 打开 [README.md:213-317](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L213-L317) 的目录布局说明。
   - 针对下面三个需求，各回答「应该去哪个目录、打开哪个代表性文件」：
     - (a) 「我想看 DSLX 源码是怎么被解析成一棵抽象语法树（AST）的。」
     - (b) 「我想看 IR 的核心数据结构 `Package` 是怎么定义的。」
     - (c) 「我想看生成的 Verilog 是怎么以 AST 形式被构造出来的。」
3. **需要观察的现象**：你会发现自己不需要通读全部代码，只靠目录名 + README 描述就能定位。
4. **预期结果**（参考答案）：
   - (a) `xls/dslx/frontend/`（解析器与 AST 节点，例如 `xls/dslx/frontend/ast.h`）。
   - (b) `xls/ir/`（核心是 `xls/ir/package.h`，`Package` 类声明就在其中）。
   - (c) `xls/codegen/vast/`（Verilog AST，核心头是 `xls/codegen/vast/vast.h`）。
5. 如果你在本地仓库里**没找到**对应文件，请检查拼写与大小写，或标注「待本地确认」。

#### 4.1.5 小练习与答案

**练习 1**：`xls/jit` 和 `xls/interpreter` 都是「执行 XLS 设计」的手段，它们的主要区别是什么？

> **参考答案**：`xls/interpreter` 是 IR 解释器，逐节点解释执行，**便于调试**但慢；`xls/jit` 基于 LLVM 把 IR 即时编译成本机代码，**追求接近原生的吞吐**，适合需要跑大量输入的场景（如 Fuzzer）。README 对二者分别用了「useful for debugging and exploration」和「native-speed execution」来描述。

**练习 2**：`xls/data_structures` 里通常会放什么样的代码？为什么 `passes/` 可能会依赖它？

> **参考答案**：放通用、与 XLS 业务无关的数据结构，例如二叉决策图（BDD）、并查集（union find）、最小割（min cut）。`passes/` 里的优化 Pass 常用 BDD 来推断「某些位恒为 0/1」等位级信息，因此会复用这里的基础结构。

---

### 4.2 编译阶段分层

#### 4.2.1 概念说明

上一节是「按目录平铺」的职责表。这一节换个视角：**按编译流水线的先后顺序**重新组织这些目录。

XLS 的主干是一条单向流水线：

```
            前端                枢纽        优化          调度          代码生成           执行与验证
DSLX 源码 ────────► XLS IR ──────► 优化 IR ──────► 带调度 IR ──────► Verilog ──────► 仿真/形式化验证
(.x / C++)          (.ir)        (opt)         (schedule)        (.v)         (iverilog / Z3)
```

关键认知：**目录的物理划分 ≈ 流水线的逻辑阶段**。这意味着当你顺着一条 `.x` 文件往前走，你访问的目录顺序基本就是上面这条流水线。

#### 4.2.2 核心流程

下面是「一条 DSLX 程序流过工具链」的全过程，每一步都标出对应的目录与工具：

```
[1] xls/dslx         编写/解析 DSLX            工具: xls/dslx/interpreter_main
        │  ir_convert（DSLX→IR）   子目录: xls/dslx/ir_convert
        ▼
[2] xls/ir           得到 XLS IR（.ir）         工具: xls/dslx/ir_convert/ir_converter_main
        ▼
[3] xls/passes       在 IR 上跑优化 Pass        工具: xls/tools/opt_main
        ▼
[4] xls/scheduling   把操作分配到流水线级       依赖: xls/estimators/delay_model（延迟）
        ▼
[5] xls/codegen      转成 Block → 生成 Verilog  工具: xls/tools/codegen_main
        ▼
[6] 执行与验证
        xls/interpreter   IR 解释器             工具: xls/tools/eval_ir_main
        xls/jit           LLVM JIT              工具: eval_ir_main（可选引擎）
        xls/simulation    Verilog 仿真          工具: simulate_module_main
        xls/solvers       SMT 形式化验证         工具: xls/tools/lec_main
        xls/netlist       网表分析（LEC 输入）
```

注意几个**横向支撑**目录，它们不严格属于某一级，而是被多个阶段共用：

- `xls/common`、`xls/data_structures`：基础工具与数据结构，被各阶段复用。
- `xls/tools`：所有命令行入口的集中地（`opt_main`、`codegen_main`、`eval_ir_main`、`lec_main` 等都在这里）。
- `xls/build_rules`：把流水线封装成 Bazel 目标的宏。
- `xls/fuzzer`：跨整条流水线（DSL→四套引擎）做一致性交叉验证。

#### 4.2.3 源码精读

把流水线两端「钉」住的真实文件确认一下（每条都给出目录与可定位的代码锚点）：

- **前端入口**：DSLX 解释器的主函数在 [`xls/dslx/interpreter_main.cc:371`](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/interpreter_main.cc#L371)（`int main(int argc, char* argv[])`），它属于 `xls/dslx` 目录，即流水线的最前端。
- **IR 枢纽**：IR 顶层容器 `Package` 定义在 [`xls/ir/package.h:81`](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/package.h#L81)（`class Package {`）；所有 IR 运算符的枚举 `Op` 在 [`xls/ir/op.h:34`](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op.h#L34)（`enum class Op : int8_t {`）。这两者都在 `xls/ir`，是整条流水线的公共枢纽。
- **优化阶段**：优化 Pass 集中在 `xls/passes`，代表性的是 `xls/passes/arith_simplification_pass.h`（算术化简）。
- **调度阶段**：调度结果 `PipelineSchedule` 定义在 [`xls/scheduling/pipeline_schedule.h:51`](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/scheduling/pipeline_schedule.h#L51)（`class PipelineSchedule {`），属于 `xls/scheduling`。
- **代码生成**：生成器（如 `pipeline_generator.h`）与 Verilog AST（`xls/codegen/vast/vast.h`）都在 `xls/codegen`。
- **工具集中地**：`opt_main` 与 `codegen_main` 都在 `xls/tools/`，见 `xls/tools/opt_main.cc`、`xls/tools/codegen_main.cc`。

把这六个锚点和上一节的目录表对照，你就能确认：**目录顺序 = 流水线顺序**。

#### 4.2.4 代码实践

这道练习帮你把「工具 → 目录 → 阶段」三者对齐。

1. **实践目标**：亲手确认四个核心工具各属于哪个目录、对应流水线的哪一级。
2. **操作步骤**：
   - 在仓库中定位下面四个 `*_main.cc`，记录它所在的目录：
     - `interpreter_main.cc`
     - `ir_converter_main.cc`
     - `opt_main.cc`
     - `codegen_main.cc`
   - 对每个工具填一张三列表：**工具 → 所在目录 → 流水线阶段**。
3. **需要观察的现象**：你会发现这四个工具**分散在不同目录**，正好对应流水线的四个节点；`tools/` 则是「收口」目录，里面也有同名的命令行入口。
4. **预期结果**（参考）：

   | 工具 | 所在目录 | 流水线阶段 |
   | --- | --- | --- |
   | `interpreter_main.cc` | `xls/dslx/` | 前端（DSLX 解释执行/测试） |
   | `ir_converter_main.cc` | `xls/dslx/ir_convert/` | 前端 → IR（DSLX lowering 到 IR） |
   | `opt_main.cc` | `xls/tools/`（驱动 `xls/passes`） | IR 优化 |
   | `codegen_main.cc` | `xls/tools/`（驱动 `xls/codegen`） | 代码生成（IR → Verilog） |

5. **可选运行验证**：如果你已在 `u1-l2` 成功构建，可以执行 `./bazel-bin/xls/dslx/interpreter_main --version` 等四条命令确认产物存在；若尚未构建，标注「待本地验证」即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `opt_main` 和 `codegen_main` 都在 `xls/tools/`，而不在 `xls/passes/` 和 `xls/codegen/` 里？

> **参考答案**：`xls/tools/` 是命令行入口的集中收口目录，负责「拼装参数、调用底层库」。真正的优化逻辑在 `xls/passes/`，真正的生成逻辑在 `xls/codegen/`。把「薄薄的命令行外壳」和「厚厚的库实现」分开放，是 XLS 的一致风格——这样库既能被工具调用，也能被 `xls/flows`、`xls/fuzzer` 等其他代码以库形式直接复用。

**练习 2**：`xls/estimators/delay_model` 在流水线里位于哪一级？它服务于谁？

> **参考答案**：它服务于**调度阶段**（`xls/scheduling`）。调度器在把操作分配到流水线级时，必须知道每个操作的延迟，而延迟就由 `delay_model` 提供。所以它不是流水线的一个「数据流节点」，而是调度阶段的一项**输入数据/模型**。

**练习 3**：`xls/fuzzer` 横跨了流水线的几级？它存在的意义是什么？

> **参考答案**：它几乎横跨**全部**阶段——在 DSL 层生成程序（前端），转成 IR（枢纽），分别用 DSL 解释器、IR 解释器、IR JIT、生成 Verilog 的仿真器这四套引擎执行（执行/验证），并交叉比较结果。意义是：用四套独立实现互相印证，任何一套出现不一致都能被捕捉到，从而为整条工具链的正确性提供高强度的自动化保障。

## 5. 综合实践

把本讲两个模块串起来，完成下面这张**「五目录 → 五阶段 → 代表文件」**的对照表（这正是本讲规格里的实践任务）：

1. **实践目标**：独立产出一张能长期放在手边当「路标」的速查表。
2. **操作步骤**：
   - 针对 `dslx`、`ir`、`passes`、`scheduling`、`codegen` 五个目录，分别填写：
     - 对应流水线的哪个阶段；
     - 该目录里**一个代表性源文件**及其作用（用一句话说清楚）。
   - 选取代表性文件时，优先选「与目录名同名/语义最核心」的头文件（例如 `ir/` 选 `package.h` 或 `op.h`）。
3. **需要观察的现象**：填完后你会发现，五个目录正好覆盖了「前端 → IR → 优化 → 调度 → 代码生成」这条主干，中间没有断层。
4. **预期结果**（参考答案，可对照）：

   | 目录 | 流水线阶段 | 代表性源文件 | 作用 |
   | --- | --- | --- | --- |
   | `dslx` | 前端 | `xls/dslx/frontend/ast.h` | DSLX 的 AST 节点定义，是源码解析的产物 |
   | `ir` | IR 枢纽 | `xls/ir/package.h` | 定义 IR 顶层容器 `Package`，容纳所有函数与节点 |
   | `passes` | 优化 | `xls/passes/arith_simplification_pass.h` | 算术化简 Pass，是众多优化之一 |
   | `scheduling` | 调度 | `xls/scheduling/pipeline_schedule.h` | 表达「每个操作落在第几级流水线」的调度结果 |
   | `codegen` | 代码生成 | `xls/codegen/pipeline_generator.h` | 把带调度的 IR 生成为带流水线寄存器的 Verilog |

5. 把这张表存下来——后续每一篇讲义开头都会有「源码地图」，这张表能帮你迅速把新讲义定位到正确的目录。

## 6. 本讲小结

- XLS 的项目主体在仓库根的 **`xls/`** 子目录里（Bazel 项目同名子目录风格）。
- 目录的物理划分**几乎等于**编译流水线的逻辑阶段：`dslx`（前端）→ `ir`（枢纽）→ `passes`（优化）→ `scheduling`（调度）→ `codegen`（代码生成）→ `interpreter`/`jit`/`simulation`/`solvers`/`netlist`（执行与验证）。
- `xls/tools/` 是命令行入口的集中收口；`xls/build_rules/` 把流水线封装成 Bazel 目标；`xls/common`、`xls/data_structures` 是被各阶段共用的横向支撑。
- `xls/fuzzer` 横跨全链路，用四套执行引擎交叉验证正确性；`xls/estimators/delay_model` 为调度提供延迟输入。
- README 的 **「Stack Diagram and Project Layout」** 小节是目录职责的最权威说明，遇到不确定的目录先回去查它。

## 7. 下一步学习建议

认完路之后，建议**顺着流水线**往前走：

- 想先体会「整个工具链端到端跑一遍」的读者，接着读 **`u1-l4`（用 DSLX 写第一个硬件函数）** 和 **`u1-l5`（完整工具链走一遍）**，亲手把 `.x` 变成 `.v`。
- 想深入某一层的读者，可以按目录直接跳到第二/三单元：例如对前端感兴趣看 `u2-l2`（DSLX 解析与 AST），对 IR 感兴趣看 `u3-l1`（IR 总览）。
- 推荐继续阅读源码：[`README.md:213-317`](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L213-L317) 的栈图与目录说明，以及 `xls/BUILD` 的可见性分组，作为本讲的延伸材料。
