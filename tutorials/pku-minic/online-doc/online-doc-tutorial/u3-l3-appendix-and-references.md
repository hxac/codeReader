# 附录与参考资料体系

## 1. 本讲目标

学完本讲，你应当能够：

- 说明 `misc-app-ref/`（杂项/附录/参考）这一章在整个文档站中的定位，并能把它和「手把手教的 Lab 章节」区分开。
- 看懂 `docs/misc-app-ref/README.md` 给出的附录清单，并能把 10 个附录条目归入「规范类 / 环境与评测类 / 外部参考类」三大类。
- 在需要时**快速定位**三大核心规范：SysY 语言规范、Koopa IR 规范、RISC-V 指令速查，并知道它们各自覆盖什么内容。
- 看懂正文各 Lab 是如何用 Docsify 路由链接（`/misc-app-ref/...`、`/misc-app-ref/...?id=锚点`）交叉引用这些附录的，并能用 `grep` 统计这种引用关系。

## 2. 前置知识

本讲承接两篇前置讲义：

- **u1-l3 仓库目录结构一览**：你已经知道 `docs/` 下有一个叫 `misc-app-ref/` 的目录，名字拆开是 **misc（杂项）/ app（附录 appendix）/ ref（参考 reference）**，是整站最后一个内容章节。本章只读这个目录，不再讲目录全貌。
- **u3-l1 实验分层与编译流水线映射**：你已经知道文档把「写一个编译器」拆成 SysY → Koopa IR → RISC-V 三段流水线，并切成 Lv0–Lv9（+Lv9+）十个增量阶段。本讲要讲的三大规范，正好分别对应这三段流水线。

此外需要复习两个来自 **u3-l2 Docsify 扩展 Markdown 写作规范** 的事实：

1. 本仓库的站内链接写的是 **Docsify 路由**而不是相对文件路径。形如 `/misc-app-ref/koopa` 指向 `docs/misc-app-ref/koopa.md`，形如 `/misc-app-ref/sysy-spec?id=文法定义` 则进一步跳到该页某个标题锚点。
2. 这些链接能在浏览器里跳转，靠的是 Docsify 的路由机制（u1-l4 讲过）。

> 一个直觉：如果各 Lab 章节是一本「教你造编译器」的**教程**，那么 `misc-app-ref/` 就是随书附赠的**手册（manual）**——你不会从头到尾读它，但造编译器的过程中遇到「SysY 里数组到底怎么定义？」「Koopa IR 的跳转指令长什么样？」「RISC-V 有哪些寄存器？」时，你会反复翻它。

## 3. 本讲源码地图

本讲涉及的文件全部位于 `docs/` 内，且都不含真正的程序代码——它们都是文档（Markdown）：

| 文件 | 作用 |
|------|------|
| `docs/misc-app-ref/README.md` | 这一章的**目录页**，用一份列表枚举了全部 10 个附录条目，是「分类总览」的源头。 |
| `docs/misc-app-ref/koopa.md` | **Koopa IR 规范**，本章最有分量的一份核心规范，按 IR 的语法要素分了 14 节。 |
| `docs/toc.md` | 整站侧边栏。它的最后一组（10 条）与 `README.md` 的清单一一对应，是把附录挂到导航树上的地方。 |

围绕这三个文件，本讲还会**引用**若干正文 Lab 文件（如 `docs/lv3-expr/unary-exprs.md`、`docs/lv0-env-config/riscv.md`），用来演示「正文如何交叉引用附录」。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先看附录**有哪些**（分类总览），再定位**三大核心规范**（语言/IR/指令），最后看正文 Lab 如何**交叉引用**它们。

### 4.1 附录分类总览

#### 4.1.1 概念说明

`misc-app-ref/` 的 `README.md` 第一段就说明了这一章的定位：

> 本章谈论了一些杂项内容, 同时包含文档的附录和参考文献.

换句话说，这一章是「装不进前面 Lv0–Lv9 主线教程、但读者又会反复需要」的内容的**集中收纳处**。README 紧接着给出一句**使用指南**，点明了它的工具书属性：

> 同学们在遇到关于语言/IR 定义的问题, 或者是 OJ 使用上的问题时, 可以直接查阅附录.

这一点是理解全章的钥匙：**主线 Lab 是「按顺序学」，附录是「按需查」**。两类内容写作风格也不同——Lab 是步骤式的「先做 A 再做 B」，附录是定义式的「X 的语法是 …，语义是 …」。

#### 4.1.2 核心流程

`README.md` 用一份普通的无序列表枚举了 10 个附录条目，每条都是一个 Docsify 路由链接。整章的「入口 → 清单 → 各页」结构可以画成：

```
toc.md（侧边栏，最后一组 10 条）
        │  与 README 清单一一对应
        ▼
misc-app-ref/README.md（章节目录页：10 条链接）
        │
        ├─ why.md            为什么学编译
        ├─ environment.md    实验环境使用说明
        ├─ sysy-spec.md      SysY 语言规范        ┐
        ├─ sysy-runtime.md   SysY 运行时库        │
        ├─ koopa.md          Koopa IR 规范        ├ 规范类（核心）
        ├─ libkoopa.md       Koopa IR C 接口      │
        ├─ riscv-insts.md    RISC-V 指令速查      ┘
        ├─ oj.md             在线评测使用说明
        ├─ references.md     参考文献
        └─ examples.md       示例编译器
```

这 10 条并不是随意排列的，可以按用途归为三大类：

| 分类 | 条目 | 作用 |
|------|------|------|
| **环境与评测类** | why、environment、oj | 课程背景、本地实验环境/调试方法、在线评测（OJ）怎么用 |
| **规范类（核心手册）** | sysy-spec、sysy-runtime、koopa、libkoopa、riscv-insts | 源语言、IR、目标指令、运行时库的**定义**——写编译器时反复查 |
| **外部参考类** | references、examples | 课后延伸阅读、可参考的示例编译器实现 |

#### 4.1.3 源码精读

附录清单定义在 `docs/misc-app-ref/README.md` 的第 7–16 行：

[docs/misc-app-ref/README.md:L7-L16](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/misc-app-ref/README.md#L7-L16) —— 这 10 行无序列表就是全章的「事实清单」，每条链接的路径（如 `/misc-app-ref/koopa`）即对应 `docs/misc-app-ref/` 下同名 `.md` 文件（`koopa.md`）。这正是 u1-l3 讲过的 Docsify 路由规则：`/path` → `path.md`。

同一份清单在侧边栏 `docs/toc.md` 中又出现了一次，第 57–67 行：

[docs/toc.md:L57-L67](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/toc.md#L57-L67) —— 这是「杂项/附录/参考」一级条目下的 10 个二级条目，与 README 清单**逐条对应**。这种「README 列一次、toc.md 再列一次」的重复是有意为之：README 服务于「翻到这一章后看目录」，toc.md 服务于「在侧边栏里随时跳转」。两份清单必须保持同步，否则会出现「侧边栏能看到、章节页却没列」（或反之）的不一致。

> 小提示：清单里有个 `why.md` 只有标题 `# 为什么学编译?` 和一句 `TODO: 待补充`——这是仓库里少数的占位页，正好说明附录是「随课程演进、持续补全」的，不是一次性写死的。

#### 4.1.4 代码实践

**实践目标**：亲手核对「README 清单 == toc.md 侧边栏条目 == 实际存在的 `.md` 文件」三者一致。

**操作步骤**：

1. 打开 `docs/misc-app-ref/README.md`，数一下无序列表共有多少条（应为 10 条）。
2. 打开 `docs/toc.md`，找到「杂项/附录/参考」一级条目，数其下缩进的二级条目数（也应为 10 条）。
3. 用 `ls` 列出 `docs/misc-app-ref/` 下的 `.md` 文件，确认每个链接都能落到一个真实文件。

**需要观察的现象**：三处数量一致、文件名一致（路由 `/misc-app-ref/koopa` ↔ 文件 `koopa.md`）。

**预期结果**：10 条清单、10 条侧边栏、对应 10 余个 `.md`（目录里还有 `judging-1.png` 等图片资源，不计入）。本讲写作时已核对一致。

#### 4.1.5 小练习与答案

**练习 1**：附录清单里，`/misc-app-ref/libkoopa` 对应磁盘上哪个文件？它属于上面三大类里的哪一类？

> **答案**：对应 `docs/misc-app-ref/libkoopa.md`，属「规范类（核心手册）」——它是 Koopa IR 的 C 语言接口（`libkoopa`）说明，和 `koopa.md` 配套。

**练习 2**：为什么 `README.md` 和 `toc.md` 要各列一份相同的清单？能不能只保留一份？

> **答案**：两者用途不同——`README.md` 是翻进本章后看的「章节目录」，`toc.md` 是全站侧边栏里随时可点的「导航树」。Docsify 的侧边栏由 `toc.md`（经 `loadSidebar`）单独驱动，不会自动从 `README.md` 生成，所以必须各写一份并保持同步。

### 4.2 语言/IR/指令规范定位

#### 4.2.1 概念说明

上一模块把附录分了类，其中**规范类**是写编译器时翻得最勤的部分。它内部又恰好和 u3-l1 讲的三段流水线一一对应：

| 流水线阶段 | 对应的规范附录 | 回答的问题 |
|------------|----------------|------------|
| 源语言（被编译的对象） | SysY 语言规范 `sysy-spec` | SysY 的文法和语义约束是什么？ |
| ↓ 前端/中端 | Koopa IR 规范 `koopa`（+ C 接口 `libkoopa`） | 中间表示长什么样？有哪些指令？ |
| ↓ 后端 | RISC-V 指令速查 `riscv-insts` | 要生成哪些目标汇编指令？寄存器有哪些？ |

外加一份 **SysY 运行时库 `sysy-runtime`**，定义 `getint`、`putint` 等 SysY 程序可调用的库函数——它属于「源语言一侧」的配套（因为这些函数的语义是 SysY 程序运行时依赖的）。

把规范和流水线对齐后，定位就变得很机械：**你在实现流水线的哪一段，就去查对应的那一份规范**。

#### 4.2.2 核心流程

三份核心规范的**内部组织方式**各不相同，反映了它们描述的对象不同：

- **SysY 语言规范**（`sysy-spec.md`）：先给「文法定义」（EBNF），再给「语义约束」（合法程序的额外限制）。也就是说它从「语法上合法」和「语义上合法」两个层次定义源语言。
- **Koopa IR 规范**（`koopa.md`）：按 IR 的**语法要素**分节——从最基本的「符号名称」「类型」「值」，逐步到「符号定义」「内存声明/访问」「指针运算」「二元运算」「分支和跳转」「函数调用」，再到进阶的「SSA 扩展」。每一节都是「语法（EBNF）+ 说明」两段式。
- **RISC-V 指令速查**（`riscv-insts.md`）：很紧凑，三节——「寄存器一览」「指令记法」「指令一览」，是一张可查的指令表。

注意一个贯穿全章的写作模式：**规范页几乎都用 EBNF 代码围栏给出语法**，例如 Koopa IR 的类型定义：

```ebnf
Type ::= "i32" | ArrayType | PointerType | FunType;
ArrayType ::= "[" Type "," INT "]";
PointerType ::= "*" Type;
FunType ::= "(" [Type {"," Type}] ")" [":" Type];
```

这种 EBNF 能被 Prism 着色（u2-l2 讲过仓库加载了 `ebnf` 高亮组件），是规范类页面的标志特征。

#### 4.2.3 源码精读

Koopa IR 规范是三份里最有分量的。它的章节切分（用二级标题 `##` 组织）如下，可以清楚看到「从原子到分子」的递进：

[docs/misc-app-ref/koopa.md:L3-L3](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/misc-app-ref/koopa.md#L3-L3) —— 第一节「符号名称」，定义 `@具名符号` 与 `%临时符号`，是后面一切定义的词法基础。

整份 `koopa.md` 的二级标题依次为（行号为当前 HEAD 实测）：

| 行号 | 章节 | 大致对应的能力 / Lab |
|------|------|----------------------|
| L3 | 符号名称 | 全局基础（Lv1 起一直用） |
| L16 | 类型 | i32 / 数组 / 指针 / 函数类型（Lv1、Lv9） |
| L40 | 值 | 整数、符号引用、初始化列表（Lv4、Lv9） |
| L58 | 符号定义 | `@x = ...` 给符号赋值（Lv1 IR 生成） |
| L77 | 内存声明 | `alloc` 等（Lv4 变量、Lv9 数组） |
| L113 | 内存访问 | `load` / `store`（Lv4、Lv9） |
| L136 | 指针运算 | `getelemptr`（Lv9 数组寻址） |
| L182 | 二元运算 | `add`/`sub`/`mul`/...（Lv3 表达式） |
| L212 | 分支和跳转 | `br`/`jump`（Lv6 if、Lv7 while） |
| L245 | 函数调用和返回 | `call`/`return`（Lv8 函数） |
| L274 | 函数和参数 | 函数定义形参（Lv8） |
| L318 | 函数声明 | 声明库函数（Lv8） |
| L347 | Koopa IR 的 SSA 扩展 | 进阶（Lv9+） |
| L396 | 注释和注解 | 语法注释 |

这张表把「Koopa IR 规范的章节」和「u3-l1 的 Lab 进阶路线」勾连了起来：**你每推进一个 Lab，就会用到规范里新的一两节**。这也是为什么 `koopa` 是全站被正文引用次数最多的附录（见下一模块）。

作为对照，RISC-V 指令速查则短得多、更像一张表：

[docs/misc-app-ref/riscv-insts.md:L5-L33](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/misc-app-ref/riscv-insts.md#L5-L33) —— 三节「寄存器一览 / 指令记法 / 指令一览」，覆盖编译实践所需的目标指令，是后端（Lv2 起）生成汇编时的查表依据。

#### 4.2.4 代码实践

**实践目标**：体验「按流水线阶段定位规范」的查法。

**操作步骤**：

1. 假设你正在做 **Lv3 表达式**，需要知道 Koopa IR 支持哪些二元运算。打开 `docs/misc-app-ref/koopa.md`，定位到「二元运算」一节（上表 L182），阅读其说明。
2. 假设你在做 **Lv2 目标代码生成**，需要知道某条 RISC-V 指令的含义。打开 `docs/misc-app-ref/riscv-insts.md` 的「指令一览」一节查表。
3. 假设你不确定某段 SysY 是否合法。打开 `docs/misc-app-ref/sysy-spec.md`，先查「文法定义」判断语法，再查「语义约束」判断语义。

**需要观察的现象**：三份规范各自的**组织方式不同**（Koopa 按语法要素分节、RISC-V 是指令表、SysY 是文法+语义两段），但都遵循「EBNF 给语法 + 文字给说明」的模式。

**预期结果**：你能不靠搜索、只靠目录跳转，在 30 秒内找到上述三个问题的答案页。

#### 4.2.5 小练习与答案

**练习 1**：你要实现「函数调用」，应该主要查哪一份规范的哪一节？

> **答案**：查 Koopa IR 规范 `koopa.md` 的「函数调用和返回」（L245）和「函数和参数」（L274）。若涉及调用约定/栈帧相关的目标指令，再辅以 RISC-V 指令速查。

**练习 2**：SysY 运行时库（`sysy-runtime`）为什么归在「规范类」而不是「外部参考类」？

> **答案**：因为它**定义**了 SysY 程序可调用的库函数（如 `getint`/`putint`）的行为，是「源语言一侧的契约」，写编译器时需要知道这些函数的签名与语义；而「外部参考类」（references、examples）是课后延伸阅读和示例代码，性质不同。

### 4.3 与正文的交叉引用

#### 4.3.1 概念说明

前两个模块讲的是「附录内部」的结构。本模块换个视角，看**正文各 Lab 是怎么引用附录的**——也就是「教程」与「手册」之间的连线。

这种连线在文档里体现为 Markdown 链接，且严格遵守 u3-l2 立下的写作规范：**站内链接写 Docsify 路由**。于是你会看到两种形态：

- **整页引用**：`/misc-app-ref/koopa` —— 「详细见 Koopa IR 规范」。
- **锚点引用**：`/misc-app-ref/koopa?id=符号名称` —— 「具体见 Koopa IR 规范的『符号名称』一节」，`?id=` 后面跟目标标题（可用中文，Docsify 会做锚点归一化）。

> 为什么要交叉引用？因为 Lab 的职责是「教你怎么实现」，规范的职责是「定义它是什么」。Lab 在讲到某个语法点时，与其把规范整段抄进来，不如一句话加一个链接，把读者「弹」到手册去核对权威定义。这是一种很常见的「教程 + 手册」分层写法。

#### 4.3.2 核心流程

我们可以用一条命令把全站所有指向附录的链接捞出来，看看「谁引用了谁」。思路是：在 `docs/` 下搜 `/misc-app-ref/`，再排除两类噪声——附录自身的自引用、以及 `toc.md` 侧边栏（它只是导航，不算「正文引用」）。

```
grep -rn "/misc-app-ref/" docs --include="*.md" \
  | grep -v "docs/misc-app-ref/" \
  | grep -v "docs/toc.md"
```

按本讲写作时的 HEAD 实测，结果是**正文 Lab 中共 13 处引用，分布在 11 个文件**。把它们按 Lab 整理如下：

| Lab | 引用所在文件（行） | 指向的附录 |
|-----|-------------------|-----------|
| Lv0 | `lv0-env-config/koopa.md`（L5） | koopa |
| Lv0 | `lv0-env-config/riscv.md`（L5） | riscv-insts |
| Lv1 | `lv1-main/structure.md`（L93） | sysy-spec（`?id=文法定义`） |
| Lv1 | `lv1-main/ir-gen.md`（L91） | koopa（`?id=符号名称`） |
| Lv1 | `lv1-main/testing.md`（L28, L44） | environment（`?id=调试你的编译器`）、oj |
| Lv2 | `lv2-code-gen/processing-ir.md`（L12） | koopa |
| Lv2 | `lv2-code-gen/code-gen.md`（L283） | riscv-insts |
| Lv2 | `lv2-code-gen/testing.md`（L24, L30） | environment、oj |
| Lv3 | `lv3-expr/unary-exprs.md`（L53） | koopa |
| Lv8 | `lv8-func-n-global/lib-funcs.md`（L21） | sysy-runtime |
| Lv9+ | `lv9p-reincarnation/ssa-form.md`（L139） | koopa |

从这张表能读出两条规律：

1. **被引用最多的是 Koopa IR 规范 `koopa`**（Lv0/Lv1/Lv2/Lv3/Lv9+ 都引），因为 Koopa IR 是贯穿前端→后端的中心产物，与 u3-l1「大多数 Lab 都在扩展前端/中端」的结论一致。
2. **引用位置常在「讲到某个语法点 / 工具用法」处**，例如 Lv3 讲一元运算时链接到 Koopa 规范、Lv8 讲库函数时链接到运行时库、Lv1/Lv2 的 testing 页统一链接到 environment 和 oj——印证了「教程讲到此处，把你弹去手册」的分层写法。

#### 4.3.3 源码精读

挑三个有代表性的引用点逐一看：

**① 整页引用——Lv3 讲一元运算时弹去 Koopa 规范**：

[docs/lv3-expr/unary-exprs.md:L53](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/lv3-expr/unary-exprs.md#L53) —— 正文说「查询 Koopa IR 规范，你会发现 Koopa IR 并不支持一元运算，而只支持如下的二元运算」，随后给链接 `/misc-app-ref/koopa`。这是典型的「教程负责解释为什么、手册负责给出权威清单」的分工。

**② 锚点引用——Lv1 讲符号命名时精确弹到某一节**：

[docs/lv1-main/ir-gen.md:L91](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/lv1-main/ir-gen.md#L91) —— 链接写成 `/misc-app-ref/koopa?id=符号名称`，用 `?id=` 直接定位到 Koopa 规范的「符号名称」一节，解释 `@main` 里 `@` 前缀的由来。这演示了 u3-l2 提到的「锚点可直用中文标题」。

**③ 配套引用——Lv8 讲库函数时弹去运行时库**：

[docs/lv8-func-n-global/lib-funcs.md:L21](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/lv8-func-n-global/lib-funcs.md#L21) —— 链接 `/misc-app-ref/sysy-runtime`，把读者导向 SysY 运行时库里 `getint` 等函数的完整定义。

这三处合起来，正好覆盖了交叉链接的三种典型用法：**整页跳转**、**锚点精确跳转**、**配套手册跳转**。

#### 4.3.4 代码实践

**实践目标**：用 `grep` 自己跑一遍上面的统计，验证「正文引用附录」的引用图，并回答「哪份附录被引用最多」。

**操作步骤**：

1. 在仓库根目录执行上一模块给出的命令，统计正文 Lab 指向 `/misc-app-ref/` 的链接总数与分布。
2. 再跑一条命令，单独数一下每份附录被引用的次数，找出「引用冠军」：

```
grep -rho "/misc-app-ref/[a-z-]*" docs --include="*.md" \
  | grep -v "^/misc-app-ref/$" \
  | sort | uniq -c | sort -rn
```

（这条命令把每条链接里的 `/misc-app-ref/xxx` 部分抽出来，按目标分组计数。）

3. 对照 4.3.2 的表，确认你跑出来的分布与表一致。

**需要观察的现象**：

- 步骤 1 的结果应是「正文 Lab 共 13 处、11 个文件」（不含 `toc.md` 和附录自引用）。
- 步骤 2 里，`/misc-app-ref/koopa` 应排在最前（被引最多）；`environment`、`oj`、`riscv-insts`、`sysy-runtime`、`sysy-spec` 等次之。

**预期结果**：与上表吻合。若你得到的数字略多/略少，通常是过滤条件差异（例如是否计入 `toc.md` 或附录自引用）造成的——**先想清楚「你想数的是哪一类引用」再决定过滤条件**，这本身就是练习的一部分。本讲数据基于当前 HEAD 实测，后续若文档增改，数字会变。

#### 4.3.5 小练习与答案

**练习 1**：在 4.3.2 的表里，`lv1-main/testing.md` 一处文件却占了两个引用（L28、L44），为什么？

> **答案**：因为 testing 页同时讲「本地测试/调试」和「在线评测」两件事，分别弹去 `environment`（实验环境说明，含调试）和 `oj`（在线评测说明）。一个页面按主题引用多份附录是正常现象。

**练习 2**：如果某天有人在正文里把链接写成了相对路径 `../misc-app-ref/koopa.md` 而不是 Docsify 路由 `/misc-app-ref/koopa`，会有什么后果？

> **答案**：在 Docsify 单页应用里，相对文件路径不能被路由正确解析，点击后会跳到错误位置或 404；而且仓库的链接检查器（`scripts/check_links.py`，第四单元会讲）是按 Docsify 路由风格校验本地链接的，这种写法很可能被判为坏链。所以站内链接必须遵守 u3-l2 的路由写法。

## 5. 综合实践

把三个模块串起来，完成一个小小的「附录导览图」任务：

1. **分类**：打开 `docs/misc-app-ref/README.md`，把 10 个条目按「环境与评测类 / 规范类 / 外部参考类」抄成一张表（参考 4.1.2，但自己判断 `libkoopa` 和 `sysy-runtime` 归哪类，并写一句理由）。
2. **定位**：为三段流水线（SysY / Koopa IR / RISC-V）各指定一份规范附录，并各写出「它定义了什么、它内部第几节讲的是函数调用/数组/分支跳转」。
3. **画引用图**：运行 4.3.4 的 `grep` 命令，把结果画成一张简单的「Lab → 附录」引用图（哪个 Lab 引用了哪些附录），并在图上标出「被引用最多的附录」。
4. **验证一致性**：对比 `docs/misc-app-ref/README.md` 与 `docs/toc.md` 的附录清单，确认二者逐条一致；若发现不一致，说明可能带来的后果。

完成后，你应当得到一张「附录有什么、各规范在哪、正文怎么用它们」的全景图——这正是后续第四单元「链接检查器」要自动校验的对象之一：检查器会顺着这些 `/misc-app-ref/...` 链接，确认它们指向的目标文件和锚点真实存在。

## 6. 本讲小结

- `misc-app-ref/`（杂项/附录/参考）是整站最后一个章节，定位是**按需查阅的手册**，与按顺序学习的 Lab 主线互补；`README.md` 用一份 10 条清单枚举了全部附录。
- 10 个附录可分为三类：**环境与评测类**（why/environment/oj）、**规范类**（sysy-spec/sysy-runtime/koopa/libkoopa/riscv-insts）、**外部参考类**（references/examples）。
- 三份**核心规范**与三段流水线一一对应：SysY 规范↔源语言、Koopa IR 规范↔中端 IR、RISC-V 指令速查↔后端目标代码；其中 `koopa.md` 按 IR 语法要素分 14 节，章节顺序大致对应 Lab 的能力进阶。
- 正文 Lab 通过 Docsify 路由链接**交叉引用**附录，有整页引用（`/misc-app-ref/koopa`）和锚点引用（`/misc-app-ref/koopa?id=符号名称`）两种形态；当前 HEAD 下正文共 13 处引用、11 个文件，`koopa` 被引最多。
- `README.md` 的清单与 `toc.md` 的侧边栏条目必须**手动保持同步**，否则会出现导航不一致。
- 这些 `/misc-app-ref/...` 链接的「目标文件/锚点是否真实存在」，正是第四单元链接检查器要自动校验的内容。

## 7. 下一步学习建议

- **横向巩固**：回到 u3-l1，把本讲的「Koopa IR 规范章节 ↔ Lab 能力」对照表和 u3-l1 的「Lab ↔ 前端/中端/后端」映射合并起来看，你会得到一张更完整的「Lab 进阶 ↔ IR 章节」对应图。
- **纵向深入**：进入第四单元。建议先读 **u4-l1 链接检查器总览、配置与入口**，了解 `scripts/check_links.py` 如何扫描 `docs/`；随后 **u4-l3 Docsify 路由解析与本地链接校验** 会精确讲解检查器如何把 `/misc-app-ref/koopa?id=符号名称` 这类路由解析成候选文件、再校验锚点是否存在——本讲提到的「附录交叉引用」正是它要保护的对象。
- **动手延伸**：挑一份你还没细读的规范（例如 `libkoopa.md` 的 C 接口，或 `sysy-spec.md` 的语义约束），通读一遍并记下你后续做 Lab 最可能反复查阅的小节，建立自己的「速查索引」。
