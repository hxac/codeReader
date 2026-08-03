# 通过 clang 与 opt 使用 Polly

## 1. 本讲目标

学完本讲，你应当能够：

1. 用 **一行 clang 命令** 让 Polly 真正参与编译优化，并知道为什么必须带 `-O1/-O2/-O3`。
2. 看懂 `-mllvm` 前缀的作用，区分「clang 转发选项」与「opt 直接选项」两种用法。
3. 开启 Polly 的两类高价值开关：并行（`-polly-parallel`）与向量化预处理（`-polly-vectorizer=stripmine`）。
4. 用 `-polly-dump-before-file` 抓出 Polly 实际「看到」的那份 LLVM-IR，并用 `opt` 单独跑 Polly 流水线、观察多面体模型与生成代码的差异。
5. 理解 `-polly-position` 的两个取值，以及一个容易踩的坑：**抓取输入 IR 时必须显式指定 `early` 位置**。

本讲只讲「怎么用」，不深入每条 pass 的内部实现——那是 U2 及以后各讲的任务。

## 2. 前置知识

在动手之前，请确认你已经具备以下认知（来自 u1-l1、u1-l2）：

- **Polly 吃的是 LLVM-IR，吐的也是 LLVM-IR**。它既不读源码，也不直接产机器码。因此「使用 Polly」的本质，是在 LLVM 的 IR 优化流水线里把它插进去。
- **Polly 是一个 Pass Plugin**。树内构建（`tools/polly`）后，`clang`/`opt`/`bugpoint` 自动带有 Polly，无需额外加载（详见 [docs/UsingPollyWithClang.rst:15-18](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/docs/UsingPollyWithClang.rst#L15-L18)）。
- **Polly 的入口是 New Pass Manager 的扩展点回调（EP callback）**，由 `registerPollyPasses` 注册；它最终把整条「检测→建模→调度→代码生成」流水线封装成一个 `PollyFunctionPass` 挂进流水线。这部分细节在 u1-l4 与 U2 详述，本讲你只需要知道「有这么个挂载点」。

如果你对下面两个名词还陌生，先建立直觉即可，不必深究：

| 名词 | 一句话解释 |
| --- | --- |
| **LLVM-IR** | 介于源码和机器码之间的中间表示，用文本看就像一门带 `%` 寄存器的汇编。Polly 的全部输入输出都是它。 |
| **pass / 流水线** | 一段对 IR 做特定改写的程序；多条 pass 串起来就是流水线。`-O3` 本质就是一组 pass 的预设序列。 |

## 3. 本讲源码地图

本讲主要阅读两份用户文档，并交叉验证它们背后真正的选项定义：

| 文件 | 作用 |
| --- | --- |
| [docs/UsingPollyWithClang.rst](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/docs/UsingPollyWithClang.rst) | 面向 clang 用户的「怎么用」手册：一键开启、并行、向量化、抓取 IR。 |
| [docs/HowToManuallyUseTheIndividualPiecesOfPolly.rst](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/docs/HowToManuallyUseTheIndividualPiecesOfPolly.rst) | 面向 opt 用户的「拆解流水线」教程：逐个 pass 手动执行，并展示矩阵乘从 IR 到可执行文件的全流程。 |
| [lib/Support/RegisterPasses.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp) | 上述命令行选项的**真正定义处**。文档可能滞后，但这里的代码永远是当前事实。 |
| [lib/Support/PollyPasses.def](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/PollyPasses.def) | 用宏登记 `-passes=` 能识别的 Polly 流水线名（`polly` / `polly-custom`）。 |

> 一个贯穿本讲的原则：**文档给直觉，源码定事实**。Polly 的命令行选项几乎全部集中在 `lib/Support/RegisterPasses.cpp`，遇到文档与实际行为不一致时，回到这里查 `cl::opt` 定义即可。

## 4. 核心概念与源码讲解

### 4.1 clang/opt 命令行：从一行命令让 Polly 跑起来

#### 4.1.1 概念说明

使用 Polly 有两条入口路径，对应两个最小模块中的「命令行」：

1. **clang 驱动（最常用）**：在普通 `clang` 编译命令上加 `-mllvm -polly`。`-mllvm` 的含义是「把下一个参数原样转发给 LLVM 的选项解析器」。也就是说，`clang -mllvm -polly` 等价于「在 clang 内部那条 opt 流水线上设置 `-polly` 开关」。Polly 的所有选项都是这么转发进去的：`-mllvm -polly-parallel`、`-mllvm -polly-vectorizer=stripmine` 等。
2. **opt 驱动（调试/学习用）**：直接对一份 `.ll` 文件跑 `opt`，此时**不需要** `-mllvm` 前缀，直接写 `-polly`、`-passes='polly'` 即可。`opt` 适合把 Polly 从完整 `-O3` 里「抠出来」单独观察。

> 为什么必须开 `-O1/-O2/-O3`？因为 Polly 默认通过「优化流水线扩展点」挂载，而 `-O0` 不构建该流水线；此外 `-Os`/`-Oz`（体积优化）不推荐（见 [docs/UsingPollyWithClang.rst:23-25](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/docs/UsingPollyWithClang.rst#L23-L25)）。源码侧，`-polly` 选项的描述也写明「with -O1, -O2 or -O3」。

#### 4.1.2 核心流程

从一行 `clang -O3 -mllvm -polly file.c` 到 Polly 真正生效，中间经历的链路（细节留给 u1-l4 / U2）：

```text
clang file.c -O3 -mllvm -polly
        │  ① 解析 C → LLVM-IR
        │  ② -O3 触发 New Pass Manager 构建流水线
        ▼
PassBuilder 的 EP 回调被触发（由 registerPollyPasses 注册）
        │  ③ 根据 -polly-position 选择 early / before-vectorizer
        ▼
buildEarlyPollyPipeline / buildLatePollyPipeline
        │  ④ 规范化（canonicalize）→ addPass(PollyFunctionPass)
        ▼
PollyFunctionPass → PhaseManager::run()   ← U2 的核心
        │  ⑤ 检测 → 建模 → 调度 → 代码生成
        ▼
优化后的 LLVM-IR → 继续后续 -O3 pass（含向量化等目标特化）
```

关键的认知是：**你加的所有 `-mllvm -polly-xxx` 开关，最终都是被上面这条链路里的某段代码读到的 `cl::opt`**。所以理解用法的最可靠办法，就是去 `RegisterPasses.cpp` 里看这些 `cl::opt` 的定义。

#### 4.1.3 源码精读

**① 总开关 `-polly`**：这是一个布尔 `cl::opt`，名为 `polly`，所以命令行写 `-polly`：

```cpp
static cl::opt<bool>
    PollyEnabled("polly",
                 cl::desc("Enable the polly optimizer (with -O1, -O2 or -O3)"),
                 cl::cat(PollyCategory));
```

见 [lib/Support/RegisterPasses.cpp:70-73](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L70-L73)（其上的 `PollyCategory` 是所有 Polly 选项归类的「Polly Options」分类，定义在同文件 [lib/Support/RegisterPasses.cpp:66-67](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L66-L67)）。

**② 挂载位置 `-polly-position`**：取值 `early`（一切之前）或 `before-vectorizer`（紧贴向量化器之前）：

```cpp
static cl::opt<PassPositionChoice> PassPosition(
    "polly-position", cl::desc("Where to run polly in the pass pipeline"),
    cl::values(clEnumValN(POSITION_EARLY, "early", "Before everything"),
               clEnumValN(POSITION_BEFORE_VECTORIZER, "before-vectorizer",
                          "Right before the vectorizer")),
    cl::Hidden, cl::init(POSITION_BEFORE_VECTORIZER), cl::cat(PollyCategory));
```

见 [lib/Support/RegisterPasses.cpp:84-89](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L84-L89)。

> ⚠️ **以代码为准**：`cl::init(POSITION_BEFORE_VECTORIZER)` 表明**当前实际默认值是 `before-vectorizer`**。注意同文件 [lib/Support/RegisterPasses.cpp:613-614](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L613-L614) 上方有一段历史注释仍写着「默认是 early」，那是过期文字——遇到冲突，永远以 `cl::init` 这一行为准。两种位置的取舍（编译时间、能否吃到内联红利）已在 u1-l1 讨论过，这里不重复。

**③ 向量化预处理 `-polly-vectorizer`**：

```cpp
static cl::opt<VectorizerChoice, true> Vectorizer(
    "polly-vectorizer", cl::desc("Select the vectorization strategy"),
    cl::values(
        clEnumValN(VECTORIZER_NONE, "none", "No Vectorization"),
        clEnumValN(VECTORIZER_STRIPMINE, "stripmine",
                   "Strip-mine outer loops for the loop-vectorizer to trigger")),
    cl::location(PollyVectorizerChoice), cl::init(VECTORIZER_NONE),
    cl::cat(PollyCategory));
```

见 [lib/Support/RegisterPasses.cpp:108-116](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L108-L116)。默认 `none`；设为 `stripmine` 时，Polly 会把外层循环做 strip-mine，留出一个规整的小内层，方便 LLVM 自带的 loop-vectorizer 接管。并行开关 `-polly-parallel` 的用法见 [docs/UsingPollyWithClang.rst:32-39](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/docs/UsingPollyWithClang.rst#L32-L39)（其代码生成后端在 U8 详述）。

**④ `-passes=` 能识别的名字**：在 New Pass Manager 下，Polly 还登记了两个可被 `-passes=` 解析的「流水线名」：

```cpp
MODULE_PASS("polly", createModuleToFunctionPassAdaptor(PollyFunctionPass(Opts)), parsePollyDefaultOptions)
MODULE_PASS("polly-custom", createModuleToFunctionPassAdaptor(PollyFunctionPass(Opts)), parsePollyCustomOptions)
```

见 [lib/Support/PollyPasses.def:4-5](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/PollyPasses.def#L4-L5)。也就是说，`-passes='polly'` 直接跑默认全流水线，`-passes='polly-custom<...>'` 跑可定制版本（定制参数语法在 u2-l2 详解）。两者最终都落到同一个 `PollyFunctionPass`，与上面 clang 路径里的 `buildCommonPollyPipeline` 殊途同归：

```cpp
PollyPassOptions &&Opts = Err(parsePollyOptions(StringRef(), /*IsCustom=*/false));
PM.addPass(PollyFunctionPass(Opts));
```

见 [lib/Support/RegisterPasses.cpp:471-473](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L471-L473)。

#### 4.1.4 代码实践

**实践目标**：亲手用 clang 把 Polly 跑起来，并对比开/关 Polly、开/关并行与向量化的差异。

**操作步骤**：

1. 准备一份 `matmul.c`（经典三重循环矩阵乘，`N` 取 1024 即可，初学不必 1536）。
2. 基线编译（不含 Polly）：

   ```console
   clang -O3 matmul.c -o matmul_base
   ```

3. 一键启用 Polly：

   ```console
   clang -O3 -mllvm -polly matmul.c -o matmul_polly
   ```

4. 再加并行（注意要链 GNU OpenMP 运行时 `-lgomp`）：

   ```console
   clang -O3 -mllvm -polly -mllvm -polly-parallel -lgomp matmul.c -o matmul_polly_omp
   ```

5. 再加向量化预处理：

   ```console
   clang -O3 -mllvm -polly -mllvm -polly-vectorizer=stripmine matmul.c -o matmul_polly_vec
   ```

**需要观察的现象**：

- 四个可执行文件都能正常运行且结果一致（先用小 `N` 自测正确性）。
- 用 `time ./matmul_xxx` 比较 wall-clock 时间。

**预期结果**：

- `matmul_polly` 通常明显快于 `matmul_base`——Polly 的 ISL 调度器（Pluto 风格）会做循环互换/分块以改善访存局部性（参见 [docs/HowToManuallyUseTheIndividualPiecesOfPolly.rst:445-470](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/docs/HowToManuallyUseTheIndividualPiecesOfPolly.rst#L445-L470) 的实测对比，其中「互换」一项把 11.3s 降到约 1s）。
- 并行/向量化的收益与机器、问题规模强相关，**该教程实测中盲目叠加向量化/OpenMP 反而变慢**（见同文档 [docs/HowToManuallyUseTheIndividualPiecesOfPolly.rst:448-452](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/docs/HowToManuallyUseTheIndividualPiecesOfPolly.rst#L448-L452)）。所以：**性能结论待本地验证**，不要照搬文档数字。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `clang -O0 -mllvm -polly file.c` 通常不会让 Polly 真正优化代码？

> **答案**：`-O0` 不构建优化流水线，Polly 通过流水线扩展点挂载，没有流水线就没有挂载点；同时 `-O0` 产出的 IR 未经规范化（如未做 mem2reg、循环未规整），即便强行跑也检测不到 SCoP。源码描述也写明需要 `-O1/-O2/-O3`。

**练习 2**：`clang -mllvm -polly-vectorizer=stripmine` 与直接 `opt -polly-vectorizer=stripmine` 有何区别？

> **答案**：`-mllvm` 是 clang 的「转发给 LLVM 选项解析器」前缀，即 clang 替你把 `-polly-vectorizer=stripmine` 传给内部的 opt；直接用 `opt` 时该前缀多余，写 `-polly-vectorizer=stripmine` 即可。两者设置的最终是同一个 `cl::opt`。

---

### 4.2 LLVM IR 基础：抓取并拆解 Polly 的输入与各 pass

#### 4.2.1 概念说明

Polly 跑在一大堆其它 pass 中间。其中一些**先于 Polly 运行的 pass 对它至关重要**——尤其是循环规范化、SSA 化（mem2reg）等。文档明确指出：正因如此，Polly 无法直接优化 clang `-O0` 的产物（见 [docs/UsingPollyWithClang.rst:84-90](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/docs/UsingPollyWithClang.rst#L84-L90)）。

要真正看懂 Polly「做了什么」，最有效的办法是把它从 `-O3` 大流水线里**隔离出来**：

1. 先抓出「Polly 看到的那份 IR」（已经过 SSA、循环规范化、内联等）。
2. 再用 `opt` 对这份 IR 单独跑 Polly 的 pass，逐段观察。

这就是最小模块「LLVM IR 基础」的核心：理解 Polly 的输入是规范化的文本 IR，并能用工具取出、喂回。

#### 4.2.2 核心流程

隔离与拆解的标准三步：

```text
① 抓取输入 IR
   clang ... -mllvm -polly-position=early \
                  -mllvm -polly-dump-before-file=before-polly.ll
        │  写出 before-polly.ll（Polly 实际看到的那份规范化 IR）
        ▼
② 观察 Polly 的分析结果（不改 IR）
   opt before-polly.ll -polly-print-scops -disable-output
        │  打印每个 SCoP 的 Domain / Schedule / Access Relation
        ▼
③ 跑完整流水线并对比产物
   opt before-polly.ll -passes='polly' -S -o after-polly.ll
        │  生成优化后的 IR；diff before vs after 看改了什么
```

第 ②③ 步也可以换成文档里的逐步法：`-polly-canonicalize` 规范化 → `-polly-print-scops` 看模型 → `-polly-print-deps` 看依赖 → 导出/改写 JScop → `-passes='polly'` 生成代码（见 [docs/HowToManuallyUseTheIndividualPiecesOfPolly.rst:16-37](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/docs/HowToManuallyUseTheIndividualPiecesOfPolly.rst#L16-L37)）。

#### 4.2.3 源码精读

**① `-polly-dump-before-file` 的定义**：它是一个字符串列表选项，把「Polly 处理前的 module」写到指定文件：

```cpp
static cl::list<std::string> DumpBeforeFile(
    "polly-dump-before-file",
    cl::desc("Dump module before Polly transformations to the given file"),
    cl::cat(PollyCategory));
```

见 [lib/Support/RegisterPasses.cpp:176-179](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L176-L179)（其旁还有 `-polly-dump-before`/`-polly-dump-after`/`-polly-dump-after-file`，见 [lib/Support/RegisterPasses.cpp:170-190](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L170-L190)）。

**② 关键陷阱：`-polly-dump-before-file` 只在 `early` 位置可用**。在 `early` 位置（`buildEarlyPollyPipeline`），dump 是 module 级的，能正常写文件：

```cpp
if (DumpBefore || !DumpBeforeFile.empty()) {
  MPM.addPass(createModuleToFunctionPassAdaptor(std::move(FPM)));
  if (DumpBefore)
    MPM.addPass(DumpModulePass("-before", true));
  for (auto &Filename : DumpBeforeFile)
    MPM.addPass(DumpModulePass(Filename, false));
  ...
}
```

见 [lib/Support/RegisterPasses.cpp:492-501](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L492-L501)。

但在**默认的 `before-vectorizer` 位置**（`buildLatePollyPipeline`，Polly 跑在 FunctionPass 层），用 `-polly-dump-before-file` 会**直接致命报错**：

```cpp
if (!DumpBeforeFile.empty())
  llvm::report_fatal_error(
      "Option -polly-dump-before-file at -polly-position=late "
      "not supported with NPM",
      false);
```

见 [lib/Support/RegisterPasses.cpp:522-526](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L522-L526)。

> 💡 **实践结论**：因为默认位置是 `before-vectorizer`，所以**抓取输入 IR 时务必显式加 `-mllvm -polly-position=early`**，否则会触发上面的 fatal error。文档 [docs/UsingPollyWithClang.rst:96-101](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/docs/UsingPollyWithClang.rst#L96-L101) 给的那条命令在现代构建里需要补这个位置参数（同文档 [docs/UsingPollyWithClang.rst:150-152](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/docs/UsingPollyWithClang.rst#L150-L152) 的「备选法」其实就带了 `early`，可作为正确范本）。

**③ 文档里的「逐步 pass 序列」**：`UsingPollyWithClang.rst` 给出标准流水线对应的手动 pass 序列：

```text
opt before-polly.ll -polly-simplify -polly-optree -polly-delicm \
  -polly-simplify -polly-prune-unprofitable -polly-opt-isl -polly-codegen
```

见 [docs/UsingPollyWithClang.rst:113-119](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/docs/UsingPollyWithClang.rst#L113-L119)。⚠️ 文档紧接着注明：**这用的是旧版（legacy）pass manager**。在当前 New Pass Manager 优先的代码库里，等价写法是 `-passes='polly'`（默认全流水线）或 `-passes='polly-custom<...>'`（按阶段定制，u2-l2 详解）。旧式 `-polly-opt-isl`、`-polly-codegen` 这些「裸 pass 名」在新构建里是否仍可被 `opt` 直接解析，**待本地验证**；新代码推荐用 `-passes=` 形式。

**④ 三个最稳的「观察」开关**：无论用哪种路径，下面三个由 `cl::opt` 控制的打印开关都是可靠的可视化手段（均在 `RegisterPasses.cpp` 中定义）：

- `-polly-print-detect`：打印检测到的 SCoP 区域（[lib/Support/RegisterPasses.cpp:207-210](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L207-L210)）。
- `-polly-print-scops`：打印多面体描述（Domain/Schedule/Accesses）（[lib/Support/RegisterPasses.cpp:212-215](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L212-L215)）。
- `-polly-print-deps`：打印数据依赖（[lib/Support/RegisterPasses.cpp:217-218](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L217-L218)）。

#### 4.2.4 代码实践

**实践目标**：抓出 Polly 的输入 IR，观察它识别出的多面体模型，并对比跑完流水线后的 IR 变化。

**操作步骤**：

1. 用 4.1.4 的 `matmul.c`，抓取输入 IR（**注意必须带 `early`**）：

   ```console
   clang matmul.c -c -O3 -mllvm -polly \
        -mllvm -polly-position=early \
        -mllvm -polly-dump-before-file=before-polly.ll
   ```

   成功后当前目录会生成 `before-polly.ll`。

2. 观察多面体模型（不改 IR，仅打印分析；`-disable-output` 丢弃改写后的 IR）：

   ```console
   opt -S before-polly.ll -passes='polly' \
       -polly-code-generation=none \
       -polly-print-scops -disable-output
   ```

   `-polly-code-generation=none` 见 [lib/Support/RegisterPasses.cpp:98-104](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L98-L104)，含义是「只做分析与调度，不回写 IR」，正好用于纯观察。

3. 跑完整流水线，生成优化后 IR 并对比：

   ```console
   opt -S before-polly.ll -passes='polly' -o after-polly.ll
   diff before-polly.ll after-polly.ll
   ```

**需要观察的现象**：

- 第 2 步会打印出类似下面的内容（摘自文档对 1536 维矩阵乘的输出）：

  ```text
  Function: main
      Statements {
          Stmt_for_body8
              Domain :=
                  { Stmt_for_body8[i0, i1, i2] : 0 <= i0 <= 1535 and ... };
              Schedule :=
                  { Stmt_for_body8[i0, i1, i2] -> [i0, i1, 1, i2] };
              ReadAccess :=  [Scalar: 0]
                  { Stmt_for_body8[i0, i1, i2] -> MemRef_C[i0, i1] };
              ...
      }
  ```

  见 [docs/HowToManuallyUseTheIndividualPiecesOfPolly.rst:144-189](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/docs/HowToManuallyUseTheIndividualPiecesOfPolly.rst#L144-L189)。这就是 u1-l1 所说的「迭代域 + 调度 + 访问关系」三件套在文本里的样子。
- 第 3 步 `diff` 会显示新生成的循环结构（很可能已被互换/分块），以及一个 `polly.split_new_and_old:` 之类的分支结构——那是「运行时检查通过走优化代码、否则走原始代码」的 region 替换（U8 详述）。

**预期结果**：

- 能稳定拿到 `before-polly.ll` 并看到 `Stmt_for_body8` 的多面体描述。
- `diff` 显示 IR 有实质性结构变化。

> 若第 1 步报 `Option -polly-dump-before-file at -polly-position=late not supported with NPM`，说明你漏了 `-polly-position=early`——这正是 4.2.3 强调的陷阱。

#### 4.2.5 小练习与答案

**练习 1**：如果不加 `-polly-position=early`，直接 `clang -O3 -mllvm -polly -mllvm -polly-dump-before-file=x.ll file.c` 会怎样？为什么？

> **答案**：会触发 `report_fatal_error` 终止编译。因为默认位置是 `before-vectorizer`（late），Polly 在该位置以 FunctionPass 运行，只能 dump 单个函数而非整个 module，所以源码在 `buildLatePollyPipeline` 里对 `-polly-dump-before-file` 直接报错（[lib/Support/RegisterPasses.cpp:522-526](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L522-L526)）。

**练习 2**：文档 [docs/UsingPollyWithClang.rst:170-172](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/docs/UsingPollyWithClang.rst#L170-L172) 提到「禁用 Polly 代码生成只看准备阶段的效果」用 `polly-no-codegen`。在当前代码里它的等价物是什么？

> **答案**：当前源码已无 `polly-no-codegen`（全仓搜索无此 `cl::opt`）。等价物是 `-polly-code-generation=none`（[lib/Support/RegisterPasses.cpp:98-104](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L98-L104)），取值 `full`/`ast`/`none`。这正是「文档滞后、以源码为准」的一个真实例子。

**练习 3**：`-passes='polly'` 与 `clang -O3 -mllvm -polly` 最终跑的 Polly 核心是同一个吗？

> **答案**：是。`-passes='polly'` 经 `PollyPasses.def` 落到 `PollyFunctionPass(Opts)`（[lib/Support/PollyPasses.def:4-5](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/PollyPasses.def#L4-L5)）；clang 路径里 `buildCommonPollyPipeline` 也 `addPass(PollyFunctionPass(Opts))`（[lib/Support/RegisterPasses.cpp:471-473](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L471-L473)）。二者用同一份默认 `Opts`，殊途同归。

## 5. 综合实践

把本讲两条路径串成一个端到端的小任务，对应规格里的实践要求：

**任务**：对一段嵌套循环矩阵乘，先用 clang 一键编译，再把 Polly 抠出来用 opt 单独跑调度与代码生成，亲眼看到循环结构被改写。

**步骤**：

1. 写 `matmul.c`（三重循环 `C[i][j] += A[i][k]*B[k][j]`）。
2. 一键编译并自测：

   ```console
   clang -O3 -mllvm -polly matmul.c -o matmul_polly
   clang -O3                matmul.c -o matmul_base
   ./matmul_polly && ./matmul_base     # 结果应一致
   ```

3. 抓取 Polly 输入 IR（记得 `early`）：

   ```console
   clang matmul.c -c -O3 -mllvm -polly \
        -mllvm -polly-position=early \
        -mllvm -polly-dump-before-file=before-polly.ll
   ```

4. 单独跑 Polly 的调度优化与代码生成，对照观察差异（新 PM 写法）：

   ```console
   # 仅看分析（多面体模型 + 依赖）
   opt -S before-polly.ll -passes='polly' -polly-code-generation=none \
       -polly-print-scops -polly-print-deps -disable-output

   # 跑完整代码生成并对比 IR
   opt -S before-polly.ll -passes='polly' -o after-polly.ll
   diff before-polly.ll after-polly.ll
   ```

5. （拓展）按文档 [docs/HowToManuallyUseTheIndividualPiecesOfPolly.rst:224-235](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/docs/HowToManuallyUseTheIndividualPiecesOfPolly.rst#L224-L235) 导出 JScop，手工把 schedule 改成「互换+分块」，再用 `-polly-import` 导回观察 AST/IR 变化（JScop 格式在 U9 详述）。

**验收标准**：

- 能解释 `before-polly.ll` 里 `Stmt_for_body8` 的 Domain 与 Schedule 各代表什么。
- 能指出 `after-polly.ll` 相对输入多了哪段「运行时检查 + 优化区域」的结构。
- 能说出为什么抓 IR 必须加 `-polly-position=early`。

## 6. 本讲小结

- 用 Polly 最简单的方式：`clang -O3 -mllvm -polly file.c`；并行加 `-mllvm -polly-parallel -lgomp`，向量化预处理加 `-mllvm -polly-vectorizer=stripmine`。
- `-mllvm` 只是 clang 向 LLVM 选项解析器的「转发前缀」；直接用 `opt` 时不需要它。
- 所有 Polly 命令行选项都是 `lib/Support/RegisterPasses.cpp` 里的 `cl::opt`——文档会滞后，**以代码为准**（实例：默认位置实为 `before-vectorizer`、`polly-no-codegen` 已被 `-polly-code-generation=none` 取代）。
- 抓取 Polly 输入 IR 用 `-polly-dump-before-file`，但**必须配 `-polly-position=early`**，否则在默认的 late 位置会致命报错。
- 隔离观察三件套：`-polly-print-detect`（区域）、`-polly-print-scops`（多面体模型）、`-polly-print-deps`（依赖）。
- 文档里的逐步 pass 序列（`-polly-opt-isl -polly-codegen` …）是 legacy pass manager 写法；当前推荐 `-passes='polly'`（默认全流水线）或 `-passes='polly-custom<...>'`（定制，u2-l2 详解）。

## 7. 下一步学习建议

你已经会「用」Polly 了，接下来应当理解它「为什么这样挂进 LLVM」与「内部怎么跑」：

- **u1-l4 插件入口与 LLVM Pass 注册**：从 `llvmGetPassPluginInfo` 一路追到 `registerPollyPasses`，看清本讲反复提到的 EP 回调（`registerPipelineStartEPCallback` / `registerVectorizerStartEPCallback`）到底是怎么把 `buildEarlyPollyPipeline`/`buildLatePollyPipeline` 拼进流水线的。
- **U2 执行主链路与阶段管理**：本讲里 `PollyFunctionPass` 内部到底跑了哪些阶段？答案在 `lib/Pass/PhaseManager.cpp` 的 `PhaseManager::run()`，它是后续所有讲义的「枢纽」。
- 想立刻看到更多端到端示例，可先翻 [docs/HowToManuallyUseTheIndividualPiecesOfPolly.rst](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/docs/HowToManuallyUseTheIndividualPiecesOfPolly.rst) 的第 5–9 步（多面体表示、依赖、JScop 导入导出、代码生成），建立对 U4/U5/U7/U8 的直观期待。
