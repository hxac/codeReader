# 项目定位：sv-elab 是什么

## 1. 本讲目标

学完本讲，你应该能够：

- 用一句话说清 sv-elab 在 SystemVerilog（以下简称 SV）综合工具链里扮演的角色；
- 区分 **slang** 与 **sv-elab** 各自的职责边界（谁来解析、谁来翻译）；
- 说出 sv-elab 的几个真实下游用户，以及它支持的 SV 子集定位；
- 独立从 README 找到「如何构建、如何加载、如何调用 `read_slang`」这三件最基本的事。

## 2. 前置知识

本讲是整套手册的第一篇，**不需要你写过综合器**。下面几个名词先有个印象即可，后续讲义会结合源码再深入：

- **SystemVerilog（IEEE 1800）**：硬件描述语言，既能写可综合的电路，也能写用于仿真的测试与断言。本仓库只关心其中「可综合」的那一部分。
- **综合（synthesis）**：把 HDL 代码翻译成实际逻辑门（或更底层网表）的过程。
- **网表（netlist）**：由「单元（cell）+ 连线（net）」构成的图，是综合的中间产物。
- **RTLIL**：Yosys 内部使用的中间表示，是一种「字级（word-level）」网表 IR——本讲会解释「字级」的含义。
- **AST（抽象语法树）**：源码经过词法、语法分析后得到的树形结构，是编译前端的经典产物。

如果你对 **Yosys** 完全陌生，只需先知道它是一个开源逻辑综合框架，可以加载「插件（plugin）」来扩展功能。这正是 sv-elab 进入 Yosys 的方式。

## 3. 本讲源码地图

本讲的目标是「认人」，所以只围绕一个文件展开：

- **README.md**：项目对外的「说明书」，集中说明了定位、用户、构建、使用方式、依赖与许可。本讲几乎所有结论都来自这里。

补充一点目录速览（来自仓库的文件清单，帮你建立空间感，本讲暂不深入）：

| 位置 | 作用 |
|---|---|
| `src/*.cc`、`src/*.h` | sv-elab 自身的全部 C++ 源码（约 25 个文件），后续讲义会逐个打开 |
| `third_party/slang/`、`third_party/fmt/` | 内嵌的 slang 与 {fmt} 子模块（见 4.2 节） |
| `tests/` | 等价性测试与真实 IP 集成测试（见第 8 单元） |
| `CMakeLists.txt`、`BUILD.bazel` | 两套构建系统：CMake 与 Bazel |

## 4. 核心概念与源码讲解

### 4.1 项目背景：sv-elab 是一个 SystemVerilog 精化器

#### 4.1.1 概念说明

sv-elab 的定位写在 README 标题里：它是一个 **SystemVerilog elaborator（SystemVerilog 精化器/例化展开器）**。核心工作是：

> 把 SystemVerilog 设计 **精化（elaborate）成字级（word-level）网表**。

两个关键词要拆开讲：

1. **精化（elaboration）**。硬件源码里常用参数化模块（`#(parameter WIDTH=8)`）、`generate` 循环、层次化例化（一个模块里例化另一个模块）来写设计。这些写法是「给人看」的抽象结构。「精化」就是把它们展开：解析参数、展开 generate、把模块层次实例化成具体的一堆线和单元，最终得到一个确定的电路结构。可以类比成 C++ 里「模板实例化 + 链接」的阶段。

2. **字级（word-level）网表**。「字级」是相对「位级（bit-level）/门级」而言的。一个 32 位加法器，在字级网表里就是「一个 32 位宽的加法单元 + 一根 32 位连线」；而到了门级，它会被拆成上千个与/或/非门和单根线。字级网表更接近设计者脑中的电路，也更适合做高层优化，正是 Yosys RTLIL 这种 IR 原生表达的层次。

一句话定位：**sv-elab 把 SV 源码「精化」成 Yosys 能直接消费的字级 RTLIL 网表。**

一个重要历史细节：项目原名 **yosys-slang**，后来改名 **sv-elab**（反映它不再只服务 Yosys）。但为了兼容，进入 Yosys 后调用的命令名仍然是历史名 `read_slang`——这点你后面会反复看到。

还要注意：sv-elab 支持的是 SV 的「**可综合子集**」，遵循 IEEE 1800-2017 或 1800-2023。是「非正式定义的子集」——并非所有合法 SV 都能综合（例如动态分配、部分高级断言等就不在范围内）。

#### 4.1.2 核心流程

从设计者视角，sv-elab 处在工具链的如下位置。**这是贯穿整套手册的核心心智模型，请记住：**

```
SystemVerilog 源码
      │
      ▼
┌──────────────────────────────────┐
│  slang：词法 / 语法 / 语义分析       │   ← 解析、建 AST
└──────────────────────────────────┘
      │  slang AST（抽象语法树）
      ▼
┌──────────────────────────────────┐
│  sv-elab：遍历 AST，精化电路         │   ← 本仓库做的工作
└──────────────────────────────────┘
      │  字级 RTLIL 网表
      ▼
┌──────────────────────────────────┐
│  Yosys / OpenROAD：优化、技术映射    │  ← 下游综合器
└──────────────────────────────────┘
      │
      ▼
   门级网表 / 布局布线
```

关键点：sv-elab **不负责**把电路映射到具体的门或 FPGA/LUT——那是 Yosys、OpenROAD 等下游综合器的事。sv-elab 只产出「字级网表」这一层，相当于工具链里的「前端（frontend）」。

#### 4.1.3 源码精读

README 第一段就把定位讲清了：

- [README.md:L1-L5](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L1-L5)：标题点明 sv-elab 是 SystemVerilog 精化器；说明它把 SV 设计精化成字级网表，可作为综合/验证工具的软件组件；并交代了改名历史（原 yosys-slang）。其中第 3 行 "elaborates SystemVerilog designs into word-level netlist form" 就是本节「精化成字级网表」的原文出处。

关于「可综合子集」的定位：

- [README.md:L5-L7](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L5-L7)：明确支持「非正式定义的可综合子集」，遵循 IEEE 1800-2017 / 1800-2023；并指向更详细的 Feature Support wiki。

#### 4.1.4 代码实践

这是本讲的核心实践，让你亲手从 README 提取信息。

1. **实践目标**：能用一句话讲清 sv-elab 是什么，并从 README 原文验证。
2. **操作步骤**：
   - 打开 README.md 第 1–7 行（就是上面引用的那段）。
   - 写下这三个填空：
     - sv-elab 把 ____ 精化成 ____。
     - 它以前的名字叫 ____。
     - 它支持 SV 的 ____ 子集（而不是全部）。
3. **需要观察的现象**：README 用很少几句话就把项目定位讲完了——这是开源项目「门面」的典型写法。
4. **预期结果**：填空答案依次是「SystemVerilog 设计」「字级网表」「yosys-slang」「可综合」。
5. 待本地验证：无需运行命令，纯阅读实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么 sv-elab 强调产出「字级」网表，而不是直接产出门级网表？

**参考答案**：字级网表保留设计者的高层结构意图（如完整的 32 位加法、多路选择器），便于下游综合器（Yosys/OpenROAD）做高层优化和技术映射；过早拆成门会丢失优化机会。sv-elab 的定位就是「前端」，负责到字级为止。

**练习 2**：项目改名前后，进入 Yosys 的命令名有变化吗？

**参考答案**：没有。项目从 yosys-slang 改名为 sv-elab，但命令名出于兼容性仍是历史名 `read_slang`（见 4.3 节引用的 README「Using the plugin」）。

---

### 4.2 slang 依赖：sv-elab 的「解析大脑」

#### 4.2.1 概念说明

sv-elab 自己**不写** SV 的词法分析器、语法分析器，也不做完整的类型检查。这些「前端编译」工作全部交给一个叫 **slang** 的开源库。README 一句话点明了这种依赖：

> sv-elab builds on top of the slang library to provide comprehensive SystemVerilog support.

slang（由 Michael Popoloski 开发）是一个高质量的 SystemVerilog 编译器前端，能完成：

- 词法分析（token 化）
- 语法分析（构建语法树）
- 语义分析（类型推导、连接检查、参数精化）

sv-elab 做的事则是：**拿到 slang 已经建好的、经过语义分析的设计表示（AST/符号表），遍历它，把电路「翻译」成 Yosys RTLIL。**

这就是 sv-elab 与 slang 的职责边界，也是整套手册最重要的分工认知：

| 关注点 | 谁负责 |
|---|---|
| SV 源码 → 词法/语法/语义 | **slang** |
| slang 设计表示 → RTLIL 字级网表 | **sv-elab（本仓库）** |
| RTLIL → 门级 / 布局布线 | Yosys / OpenROAD |

slang 不是外部命令行工具，而是作为**源码子模块（submodule）内嵌**在仓库里，和 sv-elab 一起编译。README 末尾的 License 部分指明了这一点（sv-elab 内嵌了 slang）。所以 clone 仓库时需要 `--recursive`，否则 slang 子模块是空的，无法构建。

#### 4.2.2 核心流程

slang 与 sv-elab 协同的精简流程：

```
SV 源码
  │
  ├─► slang：parseAllSources（读源码、预处理、解析）
  │       └─► createCompilation（语义分析、精化参数）
  │              └─► 产生 slang 的 Compilation / AST
  │
  ▼ （sv-elab 在这里接手）
sv-elab：遍历 Compilation 的顶层实例（topInstances）
       └─► 为每个模块/过程块生成 RTLIL 单元与连线
              └─► 写入 Yosys 的 design 里
```

注意：第 2 单元（u2）会带你真正打开 `src/slang_frontend.cc`，看 sv-elab 如何「调用 slang 的 driver、拿到 Compilation、再遍历 topInstances」。本讲你只需记住「**slang 产出设计表示，sv-elab 消费它**」这条主线。

另外，仓库除了内嵌 slang，还内嵌了 **{fmt}** 库（一个 C++ 格式化库），用于生成诊断信息和字符串。它同样是 git 子模块。

#### 4.2.3 源码精读

- [README.md:L5-L7](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L5-L7)：说明 sv-elab 建立在 slang 之上，由 slang 提供「全面的 SystemVerilog 支持」——这是 slang 依赖关系的原文出处。
- [README.md:L53-L59](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L53-L59)：构建说明里写 `git clone --recursive`，正是因为 slang（和 fmt）是子模块，必须递归拉取；随后用 cmake/make 一并编译。
- [README.md:L114-L118](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L114-L118)：License 部分明确 sv-elab 内嵌了 slang 与 {fmt}，分别在 `third_party/slang/` 与 `third_party/fmt/`。

#### 4.2.4 代码实践

1. **实践目标**：验证 slang 是「内嵌子模块」而非「外部命令」，并定位它在仓库里的位置。
2. **操作步骤**：
   - 查看仓库根目录，确认存在 `third_party/` 目录（见第 3 节目录速览）。
   - 阅读 README 第 114–118 行的 License 说明，找到 slang 与 {fmt} 的存放路径。
   - 若本地有 git 仓库，运行 `git submodule status` 查看子模块状态（待本地验证）。
3. **需要观察的现象**：slang 作为源码子模块存在，不是一个你单独安装的工具。
4. **预期结果**：README 指明 slang 位于 `third_party/slang/`，{fmt} 位于 `third_party/fmt/`。
5. **待本地验证**：`git submodule status` 的具体输出取决于本地是否执行过 `git submodule update --init`；若未递归 clone，该目录可能为空。

#### 4.2.5 小练习与答案

**练习 1**：如果只执行 `git clone`（不带 `--recursive`）就尝试 `cmake -B build . && make`，会发生什么？

**参考答案**：slang（和 fmt）子模块不会被拉取，`third_party/slang/` 为空，编译时因找不到 slang 的源码与头文件而失败。这正是 README 强调 `--recursive` 的原因。

**练习 2**：用一句话区分 slang 与 sv-elab 的职责。

**参考答案**：slang 负责 SystemVerilog 的解析与语义分析，产出经过精化的设计表示；sv-elab 负责遍历该表示，把它翻译成 Yosys 的字级 RTLIL 网表。

---

### 4.3 Yosys/OpenROAD 集成：sv-elab 如何被「装」进综合器

#### 4.3.1 概念说明

既然 sv-elab 产出 RTLIL 网表，那谁来加载它、谁来回调它？答案是它以**插件（plugin）**的形式集成进综合器。README 的 Users 一节列出了它的真实下游用户：

- **OpenROAD 的集成综合工具**：OpenROAD 是一个开源的 RTL-to-GDS 流程工具，其综合模块 `src/syn` 直接把 sv-elab 作为组件使用。
- **Yosys**：从 **v0.67** 起，sv-elab 被集成进 Yosys 内部；对更老的 Yosys，sv-elab 可作为插件加载，且在新版 Yosys 里插件会「**覆盖（override）**」内置版本。这种插件被预装在两个常见发行版里：
  - **OSS CAD Suite**（YosysHQ 出品）
  - **IIC-OSIC-TOOLS**（约翰内斯·开普勒大学出品）

「集成进 Yosys」与「作为插件」这两件事容易混淆，厘清如下：

- **集成（integrated）**：sv-elab 的代码已编译进 Yosys 主程序，开箱即用，无需额外加载。
- **插件（plugin）**：sv-elab 被编译成独立的 `slang.so` 文件，用 `yosys -m slang` 或运行时 `plugin -i slang` 加载。

无论哪种方式，最终暴露给用户的命令都叫 `read_slang`。

#### 4.3.2 核心流程

从「用户想综合一段 SV」到「sv-elab 被触发」，流程是：

```
用户启动 Yosys
  │
  ├─ 若是插件方式：yosys -m slang   或   运行时 plugin -i slang
  │      └─► 加载 build/slang.so，注册 read_slang 命令
  │
  ├─ 用户执行：read_slang <源文件> --top <顶层模块> [选项]
  │      └─► 触发 sv-elab：slang 解析 + sv-elab 翻译
  │             └─► 把 RTLIL 网表写入当前 Yosys design
  │
  ▼
用户继续用 Yosys 的其它命令（synth、opt、write_*…）
```

一个真实调用例子（来自 README）：

```
read_slang picorv32.v --top picorv32 -D DEBUG
```

这条命令会：用 sv-elab 读取 `picorv32.v`，指定顶层模块为 `picorv32`，并给 slang 传入宏定义 `DEBUG`。命令行选项沿用 slang 的标准选项集（README 提供了链接）。

#### 4.3.3 源码精读

- [README.md:L9-L19](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L9-L19)：Users 一节，列明 sv-elab 是 OpenROAD 综合工具与 Yosys（v0.67+）的组件，并给出 OSS CAD Suite、IIC-OSIC-TOOLS 两个预装插件的发行版。
- [README.md:L41-L62](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L41-L62)：Building 一节，说明插件构建方式仍保留；列出支持的 Yosys 旧版本（0.52–0.66 等可用插件）、最低编译器要求（GCC 11 / clang 17），以及 `cmake -B build .` 与 `make -C build` 的构建步骤，产物是 `build/slang.so`。
- [README.md:L64-L80](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L64-L80)：Using the plugin 一节，给出 `yosys -m slang`、`plugin -i slang` 两种加载方式，明确命令名为 `read_slang`，并给出 `read_slang picorv32.v --top picorv32 -D DEBUG` 样例。
- [README.md:L76-L77](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L76-L77)：提示用 `help read_slang` 查看完整命令选项，并指向 slang 的命令行参考文档。

#### 4.3.4 代码实践

1. **实践目标**：把「加载插件 → 调用 read_slang」这两步在 README 里定位清楚（暂不要求真的跑通）。
2. **操作步骤**：
   - 在 README 找到「Using the plugin」小节（第 64–80 行）。
   - 抄下两条加载命令和一个 `read_slang` 样例。
   - 想象你已装好 Yosys 和插件，在脑子里走一遍：先加载、再 `read_slang`、再跑一个 Yosys 的 `synth`。
3. **需要观察的现象**：sv-elab 对外只暴露一个命令 `read_slang`，其余都是标准 Yosys 流程——它「很轻」地嵌入 Yosys。
4. **预期结果**：加载用 `yosys -m slang` 或 `plugin -i slang`；读设计用 `read_slang picorv32.v --top picorv32`。
5. **待本地验证**：真正运行需先按 README 构建出 `build/slang.so` 并安装好 Yosys；本讲不要求执行，留到第 8 单元的构建系统讲义（u8-l3）与测试讲义（u8-l1）再实操。

#### 4.3.5 小练习与答案

**练习 1**：「sv-elab 集成进 Yosys v0.67+」和「sv-elab 作为插件」会冲突吗？

**参考答案**：不会冲突。README 明确说，即使在已集成 sv-elab 的新版 Yosys 里，插件构建方式仍保留，并且加载插件会「覆盖（override）」内置版本——这正好方便开发者用自编的 sv-elab 替换 Yosys 自带版本来测试。

**练习 2**：列出 sv-elab 的三个真实下游用户/发行渠道。

**参考答案**：(1) OpenROAD 的集成综合工具；(2) Yosys（v0.67 起内置）；(3) 预装插件的 OSS CAD Suite（或 IIC-OSIC-TOOLS）。任选三个即可。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「认人」小任务：

1. 打开 README，画出一条从「SV 源码」到「门级网表」的工具链箭头图，并在图上标出三段分别由谁负责（slang / sv-elab / 下游综合器）。
2. 在图旁用一句话写出 sv-elab 的定位。
3. 列出 sv-elab 的至少三个下游用户，并标注它们分别是「内置集成」还是「插件」方式。
4. 最后写一段不超过 50 字的总结：为什么 sv-elab 要依赖 slang，而不是自己写解析器？

**参考要点（自己对照）：**

- 工具链分段正确（slang 解析 → sv-elab 翻译成 RTLIL → Yosys/OpenROAD 优化映射）。
- 下游用户示例：OpenROAD（组件）、Yosys v0.67+（内置）、OSS CAD Suite / IIC-OSIC-TOOLS（插件发行）。
- 依赖 slang 的理由：SV 的解析与语义分析极其复杂，复用 slang 的高质量前端能让 sv-elab 专注于「AST → RTLIL 翻译」这一核心价值，避免重复造轮子。

这是一个**纯阅读型实践**，不需要运行任何命令；目标是让你在进入源码之前，先在大脑里建立正确的「项目地图」。

## 6. 本讲小结

- sv-elab（原 yosys-slang）是一个 SystemVerilog 精化器，把 SV 设计精化成**字级网表**，定位是综合工具链的**前端**。
- 它依赖 **slang** 完成 SV 的词法/语法/语义分析；slang 产出设计表示，sv-elab 遍历它翻译成 Yosys RTLIL。这是两者最关键的职责边界。
- slang 与 {fmt} 以**源码子模块**形式内嵌在 `third_party/`，因此 clone 时需要 `--recursive`。
- sv-elab 是 **OpenROAD 综合工具**与 **Yosys（v0.67+ 内置）**的组件，对老版本 Yosys 以 `slang.so` 插件形式提供，预装在 OSS CAD Suite、IIC-OSIC-TOOLS。
- 无论哪种集成方式，对用户暴露的命令都叫 **`read_slang`**。
- sv-elab 支持的是 SV 的「**可综合子集**」（IEEE 1800-2017/2023），并非全部 SV 特性。

## 7. 下一步学习建议

本讲只读了 README，还没有进入任何 C++ 源码。建议下一步：

- 看 **u2-l1《Yosys 前端注册与 read_slang 命令》**，打开 `src/slang_frontend.cc`，看 `SlangFrontend::execute()` 如何把本讲的「slang 解析 + sv-elab 翻译」两段真正串起来。
- 如果想先动手感受，可跳到 **u8-l1《测试体系》**看 `tests/unit/dff.ys`，那是用 `read_slang` 做等价性测试的最小真实例子。
- 课外阅读：README 给出的 [Feature Support wiki](https://github.com/povik/sv-elab/wiki/Feature-support) 与 [compat suite](https://github.com/povik/yosys-slang-compat-suite)，了解 sv-elab 实际能吃下哪些真实开源 IP（如 Ibex、OpenTitan、CV32E40P 等）。
