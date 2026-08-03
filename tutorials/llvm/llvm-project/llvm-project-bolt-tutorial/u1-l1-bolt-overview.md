# BOLT 是什么：后链接二进制优化器的定位与原理

## 1. 本讲目标

本讲是整个 BOLT 学习手册的第一篇，面向「完全没接触过 BOLT」的读者。读完本讲，你应该能够：

- 用一句话说清楚 BOLT 是什么、它工作在程序生命周期的哪个阶段。
- 区分「编译期优化（含编译期 PGO）」与「后链接（post-link）优化」，并理解二者为什么是互补的。
- 说出 BOLT 对输入二进制的要求（架构、符号表、重定位），并能解释每条要求背后的原因。
- 理解 BOLT 优化的核心收益来源（代码布局、热冷分离），以及收益为什么依赖 profile 质量。

本讲不要求你写过编译器，也不要求你懂 LLVM 内部。我们会从最直观的概念讲起，所有结论都来自仓库里的 `README.md` 与 `docs/GettingStarted.md`。

## 2. 前置知识

为了读懂本讲，你需要大致了解以下概念（不熟也没关系，我们会顺带解释）：

- **编译与链接**：源代码（`.c`/`.cpp`）经编译器变成目标文件（`.o`），再由链接器把多个 `.o` 合并成一个可执行文件（ELF 二进制）。BOLT 工作在这一步「之后」。
- **可执行文件 / ELF**：Linux 上常见的可执行格式。ELF 里既包含机器指令（代码段，如 `.text`），也包含符号表、重定位信息、调试信息等元数据。
- **指令缓存（Instruction Cache, I-Cache）**：CPU 内部用来缓存最近执行指令的小型高速存储。CPU 按缓存行（通常 64 字节）批量取指；如果热点代码排列得「紧凑且连续」，I-Cache 命中率就高，程序就快。
- **分支预测（Branch Prediction）与 fall-through**：CPU 遇到条件跳转时会猜测走哪条路；猜对了流水线不中断，猜错了要清空流水线付出代价。「不跳转、顺序执行下一条指令」称为 fall-through，是最省钱的路径。
- **采样剖析（Sampling Profiler）**：以一定频率记录程序「正在执行哪条指令 / 走了哪个分支」的工具，Linux 上最常用的是 `perf`。它不修改程序，只观察。
- **Profile**：程序运行行为的统计记录（例如「这条分支走了多少次」），用来指导优化。

## 3. 本讲源码地图

本讲主要阅读 BOLT 仓库的两份说明文档（它们也是整个手册后续讲义的入口）。本讲是概念入门，因此「源码」以文档为主；真正的 C++ 源码精读从第 2 单元开始。

| 文件 | 作用 |
|------|------|
| `README.md` | BOLT 项目的总说明：定位、输入要求、安装、四步使用流程、性能调优。本讲引用最多。 |
| `docs/GettingStarted.md` | 入门文档，与 README 内容高度重叠，但补充了 DWARF v5 现状等细节。 |

后续讲义（u1-l3）会深入 `docs/OptimizingClang.md` 的端到端实操，本讲先建立概念。

## 4. 核心概念与源码讲解

### 4.1 BOLT 的定位与历史

#### 4.1.1 概念说明

要理解 BOLT，先回答一个关键问题：**程序是在什么时候被优化的？**

传统优化的时间点是「编译期」：编译器（如 Clang、GCC）把每个源文件翻译成机器码时，会做大量优化（常量折叠、内联、循环展开……）。`-O3` 就是告诉编译器「请尽力优化」。

但编译期优化有一个先天限制：**编译器一次只看一个（或少数几个）编译单元，看不到整个程序最终被链接成什么样。** 尤其是当代码被链接成最终二进制后，各个函数在内存里的排列顺序、彼此之间的距离，编译器是不知道的——而这些恰恰深刻影响着指令缓存命中率与分支预测。

**BOLT（Binary Optimization and Layout Tool）** 选择了一个不同的时间点：**后链接（post-link）**。它直接接收已经链接好的完整二进制，把它反汇编、重建出控制流图（Control Flow Graph, CFG），然后基于真实运行采集的 profile，**重新排列代码布局**。

#### 4.1.2 核心流程

把 BOLT 放进程序生命周期来看：

```
源代码 ──编译──▶ .o 目标文件 ──链接──▶ 可执行二进制（ELF）
                                              │
                                              │  运行 + perf 采集
                                              ▼
                                         perf.data（profile）
                                              │
                                              ▼
                                          ┌────────┐
                                          │  BOLT  │  ◀── post-link 优化器
                                          └────────┘
                                              │
                                              ▼
                                       优化后的二进制
```

BOLT 在二进制层面做的核心事情（概念上）：

1. **反汇编**：把字节流还原成一条条机器指令。
2. **重建 CFG**：识别每个函数、切分基本块、连接控制流边。
3. **叠加 profile**：把 perf 采集到的执行频度赋给基本块和分支。
4. **重新布局**：根据 profile，把「热」的代码聚拢、把「冷」的代码分开，重排基本块和函数。
5. **重新生成二进制**：把优化后的布局写回一个新的 ELF。

> ⚠️ 这里只是建立直觉。第 3 单元（跟着 `RewriteInstance::run()` 走）会逐阶段精读真实的源码实现。

**BOLT 与编译期 PGO 的关系（互补而非替代）**

编译期 PGO（Profile-Guided Optimization）是编译器自己用 profile 来辅助优化（例如根据热点决定是否内联、设置分支预测位）。它的作用域主要在「单个函数内部」和「跨函数内联决策」。

BOLT 的作用域则在「**全程序布局**」：它能看到所有函数的最终排列，做函数级排序、热冷分裂、基本块重排——这些是编译器因为看不到最终链接结果而做不好的事。

因此二者可以叠加：**先用编译期 PGO（或普通 -O2/-O3）编译，再用 BOLT 做后链接布局优化**，往往能获得额外收益。

#### 4.1.3 源码精读

README 顶部给出了 BOLT 的精确定义和论文来源：

[README.md:L3-L8](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L3-L8) —— 这段说明 BOLT 是为加速大型应用而开发的 post-link 优化器，靠基于采样的 profile 优化代码布局；其设计思想与结果在 CGO'19 论文中有详细讨论。

关键短语拆解：
- **post-link optimizer**：链接之后的优化器——点明工作阶段。
- **optimizing application's code layout**：优化对象是「代码布局」，而非像编译器那样改写算法。
- **based on execution profile**：依据真实执行 profile，而非静态推断。
- **sampling profiler, such as Linux `perf`**：profile 来自采样剖析器。

> **关于历史**：BOLT 最初由 Meta（原 Facebook）开发，针对数据中心的大型服务做了大量验证，论文发表于 CGO'19（链接见 README），后来开源并进入 LLVM 体系，成为 LLVM 的一个子项目。这也是后续讲义里你会看到它和 LLVM、`perf`、DWARF 紧密耦合的原因。

> **关于「为什么后链接能看到编译器看不到的东西」**：链接器决定了每个函数最终的虚拟地址和相对位置；只有拿到链接产物，BOLT 才能基于真实距离计算跳转代价与缓存密度，从而做出全局最优的布局。

#### 4.1.4 代码实践

**实践目标**：建立对 BOLT 定位的一句话认知，并确认论文出处。

**操作步骤**：
1. 打开本仓库的 `README.md`，阅读第 1–8 行（即上面的永久链接所指向的范围）。
2. 在第 8 行找到 CGO'19 论文链接（可选：用浏览器打开浏览摘要）。

**需要观察的现象**：注意 README 用了三个关键词——`post-link`、`code layout`、`execution profile`。这三者合起来就是 BOLT 的全部定位。

**预期结果**：你能用自己的话写出类似下面的句子（这是本讲综合实践的一部分，先不急着写完整答案）：
> BOLT 与 `-O3` 的本质区别在于「作用阶段」和「优化对象」——`-O3` 在编译期改写单个函数的指令；BOLT 在链接后、基于真实运行 profile 重排整个二进制里函数/基本块的布局。

#### 4.1.5 小练习与答案

**练习 1**：如果只能保留 README 定义里的一个关键词来概括 BOLT，你会选哪个？为什么？
> **参考答案**：`post-link`（后链接）。它同时点明了 BOLT 的工作阶段（链接之后）和它与编译器优化的根本区别；其他两个词（code layout、execution profile）描述的是「做什么」和「凭什么做」，而 post-link 回答的是「在什么时候做」这个定位性问题。

**练习 2**：编译器已经做了 PGO，为什么还需要 BOLT？请用「编译器看不到什么」来回答。
> **参考答案**：编译器（即使是 PGO）在编译时看不到最终链接结果——它不知道各个函数最终会被排到什么虚拟地址、彼此距离多远。因此它无法做全局的函数排序和热冷分离来优化指令缓存。BOLT 拿到的是链接产物，能基于真实布局距离做全局优化，补上了编译器做不到的部分。

---

### 4.2 输入二进制要求与限制

#### 4.2.1 概念说明

BOLT 不是随便给一个二进制就能优化。它对输入有明确要求。理解这些要求的「为什么」，比记住它们更重要——因为后面学重定位、CFG 重建时，你会反复看到这些约束的影子。

README 用一段话概括了最低要求（与 `docs/GettingStarted.md` 表述一致）：

> BOLT operates on X86-64 and AArch64 ELF binaries. At the minimum, the binaries should have an unstripped symbol table, and, to get maximum performance gains, they should be linked with relocations (`--emit-relocs` or `-q` linker flag).

这里有四层信息，我们逐条拆解并说明原因。

#### 4.2.2 核心流程

下表把要求、强度、原因对照起来：

| 要求 | 强度 | 为什么必要 |
|------|------|-----------|
| ① 架构：X86-64 或 AArch64 | 必须 | BOLT 需要能反汇编、分析该架构的指令；目前后端只完整实现了这两种（RISCV 为部分支持，详见 u7-l1）。 |
| ② 格式：ELF | 必须 | BOLT 读写 ELF 的 section/symbol/relocation 结构；Mach-O 另有专门路径但能力受限。 |
| ③ 符号表未 strip | 最低要求 | BOLT 靠符号表定位「函数在哪里、有多大」；没有符号表就找不到函数边界，无法反汇编和重建 CFG。 |
| ④ 链接时加 `--emit-relocs`（即 `-q`） | 想要最大收益 | 让重定位信息留在二进制里（产生 `.rela.text`），BOLT 移动代码/数据后才能修正引用，安全地重排函数。 |

还要注意几条代码层面的限制（README 也明确写出）：

- **C/C++ 代码不能依赖代码布局属性**（如函数指针差值 `&funcB - &funcA`）。因为 BOLT 会移动代码，依赖固定布局的程序会被破坏。
- **与 `-freorder-blocks-and-partition` 不兼容**：GCC8 起默认开启该选项，用 GCC≥8 编译时必须显式加 `-fno-reorder-blocks-and-partition`。
- **DWARF v5 调试信息支持仍在进行中**（见 GettingStarted.md 的 NOTE2）：可以优化 v5 二进制，但若要用 `-update-debug-sections` 更新调试信息，目前建议用 `-gdwarf-4` 编译。

#### 4.2.3 源码精读

输入要求的权威表述在两份文档中一致：

[README.md:L10-L21](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L10-L21) —— 「Input Binary Requirements」整段：含架构、符号表、`emit-relocs`，以及「重建 CFG 依赖启发式（已在 Clang/GCC 产物上测试）」「C/C++ 不能依赖代码布局属性」的说明。

[docs/GettingStarted.md:L5-L20](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/docs/GettingStarted.md#L5-L20) —— 同样的要求，措辞稍细化。

关于 `-freorder-blocks-and-partition` 不兼容的 NOTE：

[README.md:L28-L31](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L28-L31) —— 说明 GCC8 默认开启该选项、必须显式关闭。

关于 `--emit-relocs` 在实际流程中的位置（Step 0）：

[README.md:L118-L124](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L118-L124) —— 解释 BOLT 要重排函数就需要链接器「留一手」：加 `--emit-relocs`，可通过检查 `.rela.text` section 验证；BOLT 处理时也会报告是否检测到重定位。

DWARF v5 现状的补充说明：

[docs/GettingStarted.md:L28-L35](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/docs/GettingStarted.md#L28-L35) —— 说明 v5 支持是 work in progress，目前可用 `-gdwarf-4` 临时绕过，以保证 `-update-debug-sections` 可用。

#### 4.2.4 代码实践

**实践目标**：验证一个二进制是否满足 BOLT 的输入要求。

**操作步骤**：
1. 找一个本机的 ELF 可执行文件（例如 `/bin/ls`，或你自己编译的小程序）。
2. 用 `readelf` 查看它是否有符号表和 `.rela.text`：
   ```bash
   readelf -S /bin/ls | grep -E "symtab|rela.text"
   ```
3. 用 `file` 查看架构：
   ```bash
   file /bin/ls
   ```

**需要观察的现象**：
- 是否存在 `.symtab`（完整符号表）？系统自带的 `/bin/ls` 通常被 strip 过，可能只有 `.dynsym`（动态符号表）而没有 `.symtab`——这正是「为什么不能直接拿发行版二进制喂给 BOLT」的原因。
- 是否存在 `.rela.text`？默认链接通常不会保留它，需要你用 `-Wl,-q`（即把 `--emit-relocs` 传给链接器）重新编译链接才会出现。

**预期结果**：你会直观体会到——默认编译出来的二进制往往**不满足** BOLT 的输入要求，必须按 README 的方式重新链接。这也是为什么 u1-l3 会专门讲「Step 0：emit-relocs 链接」。

> 如果手头没有可编译环境，这一步可标记为「待本地验证」，转而阅读 README Step 0 理解 `.rela.text` 的含义即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么「符号表未 strip」是最低要求，而 `--emit-relocs` 只是「想要最大收益」的要求？两者分别解决哪类问题？
> **参考答案**：符号表告诉 BOLT「函数的边界在哪」，没有它 BOLT 连函数都认不出来，所以是最低（必需）要求。`--emit-relocs` 提供的是「移动代码后修正引用」的能力——没有它，BOLT 仍能做函数**内部**的优化，但难以安全地**重排函数**（跨函数移动）。所以前者是「能不能跑」，后者是「能跑多好」。

**练习 2**：有人写了一段代码 `ptrdiff_t d = (char*)funcB - (char*)funcA;`，依赖两个函数的地址差。用 BOLT 优化它会有什么风险？
> **参考答案**：BOLT 会重排函数布局，`funcA` 和 `funcB` 的相对位置几乎一定会改变，于是 `d` 的值与原意不符，程序行为错误。这正是 README 强调「C/C++ 代码不能依赖代码布局属性（如函数指针差值）」的原因。

---

### 4.3 BOLT 优化能带来什么收益

#### 4.3.1 概念说明

理解了「BOLT 做布局优化」之后，下一个问题是：**重排代码布局，为什么能让程序变快？**

核心答案有两个词：**指令缓存局部性** 与 **分支预测**。

- **指令缓存局部性**：CPU 取指令是按缓存行批量载入的。如果一个函数里真正频繁执行的热路径代码被零散地铺开、中间夹杂大量冷代码，那么载入一个缓存行时，很多字节其实是「冷」的浪费。BOLT 把热基本块聚拢、把冷基本块剥离到独立的 `.text.cold` section，让热代码更紧凑，I-Cache 命中率上升。
- **fall-through 与分支预测**：CPU 执行到条件跳转时，如果「不跳转、顺序执行下一条」是最常见路径，流水线最顺畅。BOLT 在重排基本块时，会尽量把「最可能执行的后继」紧跟在当前块后面（fall-through），从而减少被预测错的跳转。

README 的 Step 3 给出了一条典型的「最大化收益」命令，里面的选项几乎全是布局相关：

```
$ llvm-bolt <executable> -o <executable>.bolt -data=perf.fdata \
  -reorder-blocks=ext-tsp -reorder-functions=cdsort \
  -split-functions -split-all-cold -split-eh -dyno-stats
```

这里每个选项对应一种收益手段：

| 选项 | 作用 | 收益来源 |
|------|------|---------|
| `-reorder-blocks=ext-tsp` | 用 Ext-TSP 算法重排基本块 | 缓存密度 + fall-through |
| `-reorder-functions=cdsort` | 重排函数顺序（按调用图聚簇） | 函数间缓存局部性 |
| `-split-functions` / `-split-all-cold` | 把冷基本块分裂到独立 cold section | 让热代码更紧凑 |
| `-split-eh` | 分裂异常处理冷代码 | 同上，针对异常路径 |
| `-dyno-stats` | 打印「动态指令统计」对比 | 用于**评估**收益，不是优化本身 |

> 注：`ext-tsp` 是「Extended Traveling Salesman Problem（扩展旅行商问题）」的缩写——把基本块排序建模成一个加权 TSP，目标是让整体「跳转 + 缓存」代价最小。具体算法在第 6 单元（u6-l1）精读，本讲只需知道它是 BOLT 的默认块排序算法。

除了布局，BOLT 还能做一些「调用图改造」类的优化（后续单元深入），例如：
- **ICP（Indirect Call Promotion，间接调用提升）**：把热点间接调用改写成「比较 + 直接调用」梯子，利于分支预测与内联。
- **Inliner**：二进制层面的内联。
- **SCTC（Simplify Conditional Tail Calls）**：简化条件尾调用跳转。

但本讲聚焦于最核心、最典型的收益来源：**代码布局 + 热冷分离**。

#### 4.3.2 核心流程

把收益的产生过程串起来：

```
热代码零散分布 ──▶ I-Cache 频繁 miss ──▶ 取指慢
        │
        ▼  （BOLT: reorder + split）
热代码聚拢、冷代码剥离
        │
        ▼
I-Cache 命中率↑、fall-through↑、分支预测错误↓
        │
        ▼
程序运行更快（对 I-Cache/分支敏感的大型程序尤其明显）
```

需要强调一个现实：**收益大小高度依赖 profile 质量**。README 在 Step 1 给出经验建议——profile 应覆盖约 10 亿（1B）条指令（由 `-dyno-stats` 报告）；当输入二进制与采集 profile 时的二进制不一致（stale）时，收益会下降（BOLT 会报告 stale 函数数量）。这部分细节在第 4 单元（Profile 子系统）展开。

#### 4.3.3 源码精读

典型优化命令与选项说明：

[README.md:L206-L213](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L206-L213) —— Step 3 的完整 `llvm-bolt` 命令，集中体现了布局类选项（reorder-blocks / reorder-functions / split-*）。

关于 profile 采样量与 `-dyno-stats` 的建议：

[README.md:L155-L160](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L155-L160) —— 建议 profile 覆盖约 1B 指令（由 `-dyno-stats` 报告），并说明如何通过加长采样时间或提高 `-F<N>` 频率来增加样本。

关于 stale profile 会降低收益的提醒：

[README.md:L220-L227](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L220-L227) —— 说明输入二进制不必与采集 profile 时的二进制 100% 一致，但差异越大、stale 函数越多，收益越低。

> 说明：本讲引用的是文档层面的命令与说明；这些选项在源码中的注册与算法实现会在第 5、6 单元精读（如 `lib/Passes/ReorderAlgorithm.cpp`）。现在你只需要建立「选项 → 收益手段」的对应关系。

#### 4.3.4 代码实践

**实践目标**：理解 `-dyno-stats` 如何用来「看」优化前后的差异。

**操作步骤**：
1. 阅读 README Step 3 的命令，注意 `-dyno-stats` 是用来输出统计的（而非参与优化）。
2. （可选，需本地环境）如果你能跑通一次 BOLT 优化，对比「不加 `-reorder-*`」与「加上 `-reorder-blocks=ext-tsp -reorder-functions=cdsort -split-functions`」两次输出的 `-dyno-stats` 表格。

**需要观察的现象**：`-dyno-stats` 会打印一张「动态指令统计」表（如各类指令执行次数、分支数等）。优化前后这些数字的相对变化，能直观反映布局优化带来的影响。

**预期结果**：你会看到，布局优化后「与缓存/分支相关的统计」趋向更优。如果暂时没有可运行环境，标记为「待本地验证」，并先理解：`-dyno-stats` 是 BOLT 内置的、不依赖外部工具的收益评估手段（其实现见 `lib/Core/DynoStats.cpp`，第 5 单元讲解）。

#### 4.3.5 小练习与答案

**练习 1**：`-reorder-blocks` 和 `-split-functions` 分别针对「布局」的哪个层面？为什么不重复？
> **参考答案**：`-reorder-blocks` 针对**函数内部**基本块的顺序（让热路径 fall-through、提升块级缓存密度）；`-split-functions` 针对**函数级别**的冷热分离（把冷块挪到 `.text.cold`）。前者管「块怎么排」，后者管「冷块挪出去」，互补不重复。

**练习 2**：如果采集到的 profile 采样很少（例如只跑了 1 秒），用 BOLT 优化效果会怎样？为什么？
> **参考答案**：效果会差，甚至可能变差。布局优化依赖 profile 区分「热/冷」；采样不足时，很多本该被判为热的块会被当成冷块，重排反而把真正热的代码拆散。README 也建议 profile 覆盖约 1B 指令，并提示 stale profile 会降低收益。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面的任务（这是本讲规格指定的核心实践）：

**任务**：
1. **用一句话写出 BOLT 与编译器 `-O3` 优化的本质区别。**
   - 提示：从「作用阶段」「优化对象」「依据」三个维度里挑最关键的来概括。
2. **列出 BOLT 对输入二进制必须满足的 3 个条件，并说明每个条件为什么必要。**
   - 提示：回顾 4.2 节的表格；区分「最低要求」与「最大收益要求」。

**参考答案（先自己写，再对照）**：

1. 本质区别一句话：
   > `-O3` 在**编译期**对单个函数的指令做改写（算法级优化）；BOLT 在**链接之后**、基于**真实运行 profile** 对整个二进制的**代码布局**（函数/基本块顺序、热冷分离）做全局重排。二者作用阶段与对象不同，因此可叠加。

2. 三个条件（任选三条并解释）：
   - **架构为 X86-64 或 AArch64 的 ELF**：BOLT 的反汇编与分析后端目前只完整支持这两种架构、ELF 格式，否则无法解码指令。
   - **符号表未 strip**：BOLT 靠符号表定位函数边界，没有它就找不到函数，无法反汇编和重建 CFG。
   - **链接时加 `--emit-relocs`（`-q`）**：在二进制里保留重定位信息（`.rela.text`），BOLT 移动代码/数据后才能修正引用，从而安全地重排函数。这是「想要最大收益」的关键条件。
   - （加分项）**C/C++ 代码不得依赖代码布局属性**（如函数指针差值）：因为 BOLT 会改变布局，依赖固定布局会破坏正确性。

**进阶（可选）**：用 `readelf -S <binary> | grep -E "symtab|rela.text"` 检查一个真实二进制，判断它能否直接被 BOLT 处理；若不能，说明缺哪一项、该如何重新编译链接（参考 README Step 0）。

## 6. 本讲小结

- BOLT 是一个 **post-link（后链接）profile-guided 二进制优化器**，工作在「链接之后」，优化对象是**代码布局**。
- 它与编译期 `-O3`/PGO **互补**：编译器管函数内/内联，BOLT 管全程序布局（编译器看不到最终链接结果）。
- 输入要求：**X86-64/AArch64 的 ELF**、**符号表未 strip**、（想要最大收益时）**`--emit-relocs` 链接**；且代码不能依赖布局属性。
- 核心收益来自**指令缓存局部性提升**与**分支预测改善**，通过基本块重排（ext-tsp）、函数重排、热冷分裂实现。
- profile 质量（采样量、是否 stale）直接决定收益大小；可用 `-dyno-stats` 评估。
- 这些要求与收益会在后续单元的源码里反复出现：重定位（u3-l4）、CFG 重建（u3-l3）、profile（单元 4）、布局算法（单元 6）。

## 7. 下一步学习建议

本讲建立了 BOLT 的概念地图。接下来建议：

- **u1-l2（构建与运行）**：动手把 BOLT 编译出来，认识 `bin/` 下的各个工具——这是后续所有实操的前提。
- **u1-l3（端到端流程）**：跟着 `docs/OptimizingClang.md` 走一遍 perf 采集 → perf2bolt → llvm-bolt 的完整四步流程，把本讲的命令真正跑起来。
- **u1-l4（目录与入口）**：进入源码，看 `main()` 如何分发到不同模式，为第 2 单元读核心数据结构做准备。

如果你想先建立「全局处理流程」的直觉，也可以直接跳读第 3 单元（u3-l1，`RewriteInstance::run()` 总览），但建议先完成单元 1 的环境搭建。
