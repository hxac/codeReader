# spec/defs 配置体系与 defgen

## 1. 本讲目标

本讲是专家层「配置、综合与端到端集成」单元的第一篇，回答一个贯穿全仓库的根本问题：**NVDLA 那一长串固定规格（2048 个 MAC、16 个 CBUF bank、512 位存储接口、是否支持 Winograd……）到底写在哪个文件里？又是怎样变成 RTL 能用的宏定义的？**

读完本讲，你应当能够：

- 读懂 `spec/defs/nv_full.spec`，知道它用一组 `#define ENABLE` 风格的用户开关描述了 nvdlav1 的固定特性集。
- 读懂 `spec/defs/projects.spec`，理解它如何把「用户开关」翻译成带数值的内部 `NVDLA_*` 宏，并用 `#error` 做完整性校验。
- 读懂 `spec/defs/Makefile` 与 `tools/bin/defgen`，掌握「CPP 预处理 → defgen 多后端展开」的两级流水线。
- 诚实区分两类宏的真实消费路径：**ENABLE 类特性宏**经 `#ifdef` 进 RTL，而**数值尺寸宏**主要喂给 C-model 配置头。

本讲承接 [u1-l3 构建系统与工具链](u1-l3-build-system-toolchain.md)，把其中点到为止的「defgen 把 `%define` 翻译成 `project.h/.vh`」一句展开成完整的源码级理解。

## 2. 前置知识

在进入源码前，先建立四个直觉。

**第一，nvdlav1 是「固定配置」。** 本仓库的 `nvdlav1` 分支不像可配置版 NVDLA 那样允许用户随意选规格，它的算力、缓冲、接口宽度都是锁死的（见 [u1-l1](u1-l1-project-overview.md)）。但「锁死」不等于「写死在每一行 RTL 里」——它把规格集中写在一个 spec 文件里，再由工具链展开成宏，喂给 RTL、C-model、综合。集中定义、多处消费，这是规格可维护的关键。

**第二，要区分三个层次的宏名。** 这是本讲最容易混淆的点，务必记住：

| 层次 | 例子 | 出现在 | 谁定义 |
|------|------|--------|--------|
| 用户开关（带后缀） | `MAC_ATOMIC_C_SIZE_64`、`BDMA_ENABLE` | `nv_full.spec` | 人手写 |
| 内部宏（带 `NVDLA_` 前缀，带值） | `NVDLA_MAC_ATOMIC_C_SIZE 64`、`NVDLA_BDMA_ENABLE` | `projects.spec` 的 `%define` | 翻译生成 |
| 最终头文件宏 | `#define NVDLA_MAC_ATOMIC_C_SIZE 64`、`` `define NVDLA_BDMA_ENABLE `` | `project.h` / `project.vh` | defgen 生成 |

用户开关是「选择题答案」（选 64 还是 8），内部宏是「标准化的结论」（值为 64），最终头文件宏是「按后端语法包装好的结论」（C 用 `#define`，Verilog 用 `` `define ``）。

**第三，spec 文件用的是 C 预处理器（CPP）语法。** `#define`、`#if defined(...)`、`#elif`、`#else`、`#error`、`#include` 全是标准 CPP 指令。这意味着 spec 的「翻译 + 校验」可以白嫖 GCC 的预处理器来完成，不用自己写解析器——这是设计上的取巧。

**第四，`%define` 是 defgen 自己的「中间语言」。** CPP 不认识 `%define`（它不是 C 指令），所以 CPP 会把它当成普通文本原样保留；而 defgen 只认 `%define` 开头的行。两者用不同的语法在同一个文件里分工：CPP 负责 `#if/#error` 选分支，defgen 负责把幸存下来的 `%define` 行改写成目标语言。

## 3. 本讲源码地图

本讲涉及的关键文件：

- [`spec/defs/nv_full.spec`](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/nv_full.spec) —— **用户开关层**。用约 32 个 `#define` 描述 nvdlav1 的固定特性，末尾 `#include "projects.spec"` 把翻译工作委托出去。
- [`spec/defs/projects.spec`](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/projects.spec) —— **翻译 + 校验层**。把每个用户开关映射成内部 `%define NVDLA_*` 宏，并用 `#error` 保证每个二选一开关都作出了选择。
- [`spec/defs/Makefile`](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/Makefile) —— **流水线编排**。先调 CPP 产出中间文件 `project.def`，再调 defgen 产出 `project.h`（C 后端）和 `project.vh`（Verilog 后端）。
- [`tools/bin/defgen`](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/defgen) —— **define 生成器**（Perl）。读 `project.def`，把每个 `%define KEY VAL` 改写成指定后端的语法。
- [`tools/make/vmod_common.make:13-30`](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/vmod_common.make#L13-L30) —— **消费侧**。证明生成的 `project.h` 被喂给 `vcp`，用来剥离 RTL 里的 `#ifdef NVDLA_*`。
- [`cmod/include/nvdla_config_large.h:28`](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/include/nvdla_config_large.h#L28) —— **C-model 消费侧**。数值尺寸宏在这里被直接定义，喂养黄金参考模型。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，恰好对应 spec/defs 流水线的三段：**用户开关层 → 翻译校验层 → 生成层**，最后用一节交代「生成的宏被谁消费」。

### 4.1 用户开关层：nv_full.spec

#### 4.1.1 概念说明

`nv_full.spec` 是规格的「单一事实来源（single source of truth）」。它回答一个问题：**这个版本的 NVDLA，开了哪些特性、各选了哪一档？** 它的全部内容就是一串 `#define`，每行一个开关，最后 `#include` 翻译表。

注意文件名 `nv_full` 暗示存在「多种配置档」的可能（如 small/large/full）。文件第 1 行留有一个被注释掉的 `//#define NV_LARGE 1`，说明历史上曾用 `NV_LARGE` 这类宏区分档位，当前 nvdlav1 走的是 full 档。

#### 4.1.2 核心流程

`nv_full.spec` 本身没有流程，它是一张「答卷」。它的工作流是：

1. 人按需求逐行写 `#define <开关>`，**只写选择题答案，不写数值**。
2. 每个开关都属于三类之一：
   - **特性开关（enable/disable 二选一）**：如 `BDMA_ENABLE`、`WINOGRAD_ENABLE`、`SDP_LUT_ENABLE`——决定某个引擎/特性是否存在。
   - **数值档位（多选一）**：如 `MAC_ATOMIC_C_SIZE_64`、`CBUF_BANK_NUMBER_16`——决定某项规格的取值档。
   - **接口参数**：如 `PRIMARY_MEMIF_WIDTH_512`、`PRIMARY_MEMIF_LATENCY_1200`——决定对外接口的宽度/延迟。
3. 文件末尾 `#include "projects.spec"`，把「答案如何计分」交给翻译表。

#### 4.1.3 源码精读

先看整张答卷的全貌：

[nv_full.spec:1-36](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/nv_full.spec#L1-L36) 就是全部内容。第 1 行是被注释掉的历史开关；第 2-15 行是一串特性 enable 开关；第 16-33 行是数值档位与接口参数；第 36 行 `#include "projects.spec"` 委托翻译。

挑几行最能说明问题的看。卷积算力的两个核心档位：

[nv_full.spec:16-18](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/nv_full.spec#L16-L18) 选中了 `MAC_ATOMIC_C_SIZE_64`（输入通道缩减维 = 64）、`MAC_ATOMIC_K_SIZE_32`（输出通道批量 = 32）、`MEMORY_ATOMIC_SIZE_32`。这正是 [u3-l5 CMAC](u3-l5-cmac-mac-array.md) 讲过的「2048 INT8 MAC = 64 × 32」的来源——答案写在 spec 里，不在 RTL 里。

再看 CBUF 缓冲规格：

[nv_full.spec:20-22](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/nv_full.spec#L20-L22) 选定 `CBUF_BANK_NUMBER_16`、`CBUF_BANK_WIDTH_64`、`CBUF_BANK_DEPTH_512`，对应 [u3-l3 CBUF](u3-l3-cbuf-convolution-buffer.md) 讲的「16 bank、每 bank 512 行」组织。

末尾的关键一行：

[nv_full.spec:36](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/nv_full.spec#L36) `#include "projects.spec"`。这一行是「答卷交卷」——把前面所有 `#define` 带进翻译表去计分。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：建立「把宏分类」的直觉，为后续追踪做准备。

**操作步骤**：

1. 打开 `spec/defs/nv_full.spec`。
2. 把全部 `#define` 分成三类：特性开关、数值档位、接口参数。
3. 特别标记 `MAC_ATOMIC_C_SIZE_64` 属于哪一类。

**需要观察的现象**：你会发现第 16 行 `MAC_ATOMIC_C_SIZE_64` 没有写数值 64，数值藏在名字后缀里；而第 2 行 `WEIGHT_COMPRESSION_ENABLE` 连后缀数值都没有，纯标志位。

**预期结果**：`MAC_ATOMIC_C_SIZE_64` 属于「数值档位」类（它的兄弟选项是 `MAC_ATOMIC_C_SIZE_8`，见 4.2.3）；`WEIGHT_COMPRESSION_ENABLE` 属于「特性开关」类（兄弟是 `WEIGHT_COMPRESSION_DISABLE`）。共约 32 个 `#define`。

#### 4.1.5 小练习与答案

**练习 1**：`nv_full.spec` 里为什么只写 `MAC_ATOMIC_C_SIZE_64` 而不直接写 `NVDLA_MAC_ATOMIC_C_SIZE 64`？

> **答**：因为 spec 文件要同时服务于「人读」和「工具校验」。用人读的开关名（选 64 还是 8）作答，再由 `projects.spec` 统一翻译成带 `NVDLA_` 前缀的标准内部宏。这样改名、加前缀只需改翻译表一处，答卷保持稳定。

**练习 2**：如果想让某个特性「关闭」，例如关掉 Winograd，应该改 `nv_full.spec` 的哪一行？

> **答**：把第 3 行 `#define WINOGRAD_ENABLE` 改成 `#define WINOGRAD_DISABLE`（兄弟开关，见 [projects.spec:8-13](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/projects.spec#L8-L13)）。注意必须二选一，否则翻译表会触发 `#error`。

---

### 4.2 翻译校验层：projects.spec

#### 4.2.1 概念说明

`projects.spec` 是「计分规则表」。它不回答选择题，它负责**把每个用户开关翻译成标准内部宏，并强制每个二选一开关都作出了选择**。它的每一块都是同一个模板：

```c
#if defined(<ENABLE 开关>)
    %define NVDLA_<标准宏> [数值]
#elif defined(<DISABLE 开关>)
#else
    #error "必须二选一"
#endif
```

这里同时用了两套语法：`#if/#elif/#else/#error` 是 CPP 指令（负责选分支与校验），`%define` 是 defgen 的中间语言（幸存下来等 defgen 改写）。

#### 4.2.2 核心流程

翻译 + 校验的流程是：

1. CPP 拿到 `nv_full.spec` 注入的所有 `#define`，逐块求值 `#if defined(...)`。
2. 对每块，**恰好一个分支为真**，该分支内的 `%define` 行幸存；其余分支被 CPP 丢弃。
3. 若 ENABLE 与 DISABLE 都没定义，CPP 命中 `#else #error`，**预处理报错退出**——这就是构建期完整性校验，比运行期失败早得多。
4. 全部幸存的 `%define` 行汇成一个中间文件 `project.def`。

校验的「数学」可以写成：对每个开关族 \(S=\{e, d\}\)（enable/disable），要求 \(\mathbf{1}_{e} + \mathbf{1}_{d} = 1\)；若为 0 则 `#error`。（多选一两边都 define 的情况，CPP 会因先命中 `#if` 分支而忽略 `#elif`，不报错——这是该机制的局限，校验只保证「至少选了一个」，不保证「只选了一个」。）

#### 4.2.3 源码精读

先看最典型的「enable/disable 二选一 + error」模板，以权重压缩为例：

[projects.spec:1-6](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/projects.spec#L1-L6) 把 `WEIGHT_COMPRESSION_ENABLE` 翻译成无值的标志宏 `%define NVDLA_WEIGHT_COMPRESSION_ENABLE`；若都没定义则 `#error`。BDMA 同理见 [projects.spec:57-62](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/projects.spec#L57-L62)。

再看本讲追踪主角——数值档位的翻译，它和特性开关的区别是 `%define` 后面**带数值**：

[projects.spec:99-105](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/projects.spec#L99-L105) 把用户开关 `MAC_ATOMIC_C_SIZE_64` 翻译成带值的 `%define NVDLA_MAC_ATOMIC_C_SIZE 64`（备选 `8`）。这就是 4.1 里那个没写数值的开关，在这里被补上了 `64`。配套的 K 维见 [projects.spec:107-113](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/projects.spec#L107-L113)。

还有一种「只有一个合法取值」的特殊块，用 `#error` 锁死：

[projects.spec:146-150](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/projects.spec#L146-L150) CBUF bank 深度只允许 `CBUF_BANK_DEPTH_512`，翻译成 `%define NVDLA_CBUF_BANK_DEPTH 512`，否则一律 `#error`。这说明 bank 深度是硬约束、不可调。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：亲手「脑补」一遍 CPP 求值，预测中间文件内容。

**操作步骤**：

1. 打开 `projects.spec`，定位 `MAC_ATOMIC_C_SIZE` 块（第 99-105 行）。
2. 回想 `nv_full.spec` 第 16 行定义了 `MAC_ATOMIC_C_SIZE_64`、**没有**定义 `MAC_ATOMIC_C_SIZE_8`。
3. 推断 CPP 求值后，哪一行 `%define` 会幸存进入 `project.def`。

**需要观察的现象**：`#if defined(MAC_ATOMIC_C_SIZE_64)` 为真，整块只留下 `%define NVDLA_MAC_ATOMIC_C_SIZE 64` 一行；`#elif`、`#else #error` 全被丢弃。

**预期结果**：`project.def` 中会出现一行 `%define NVDLA_MAC_ATOMIC_C_SIZE 64`（行尾可能带原文件的尾随空格，因为 [projects.spec:100](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/projects.spec#L100) 本身就有尾随空格）。把全部块都走一遍，`project.def` 应含约 32 行 `%define`。精确行数「待本地验证」（见 4.3 实践的实跑）。

#### 4.2.5 小练习与答案

**练习 1**：有人误把 `nv_full.spec` 里的 `BDMA_ENABLE` 删掉了，也没加 `BDMA_DISABLE`。构建会怎样？

> **答**：CPP 在 [projects.spec:57-62](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/projects.spec#L57-L62) 命中 `#else #error`，输出 `one of NVDLA_BDMA_{EN,DIS}ABLE must be set` 并以非零退出，`make` 在 `spec/defs` 这一步就失败，不会继续编 RTL。这正是 `#error` 校验的价值——把配置错误前移到构建最早期。

**练习 2**：为什么校验只查「至少选一个」，却允许「两个都选」？

> **答**：CPP 的 `#if/#elif` 是顺序短路：先命中 `#if defined(ENABLE)` 就进第一分支，即便同时也 define 了 DISABLE 也不会走到 `#elif`。所以「两个都选」会被静默当作「选了 ENABLE」。这是该模板的已知局限，靠人工不重复作答来规避。

---

### 4.3 生成层：Makefile 流水线与 defgen

#### 4.3.1 概念说明

前两节产出的是「CPP 中间语言」`%define`，但 RTL 用的是 Verilog `` `define ``、C-model 用的是 C `#define`。`%define` 是**与语言无关的中性表示**，需要 `defgen` 把它改写成各后端语法。`Makefile` 编排这条两级流水线：

```
nv_full.spec ──CPP──▶ project.def (%define) ──defgen -b c──▶ project.h  (#define)
                                       └──defgen -b v──▶ project.vh (`define)
```

#### 4.3.2 核心流程

`spec/defs/Makefile` 的构建分三步：

1. **CPP 预处理**：`cpp -undef -nostdinc -P -C nv_full.spec → project.def`。
   - `-undef`：不定义 GCC 内建宏（如 `__GNUC__`），保证 spec 命名空间干净。
   - `-nostdinc`：不搜系统头文件目录，只认 spec 自己的 `#include`。
   - `-P`：去掉行号标记（`# 1 "..."`），输出更干净。
   - `-C`：保留注释（本场景分支里无注释，影响不大）。
   - 结果 `project.def` 是纯文本，只剩幸存的 `%define` 行。
2. **defgen 展开（C 后端）**：`defgen -i project.def -o project.h -b c` → 每行变成 `#define KEY VAL`。
3. **defgen 展开（Verilog 后端）**：`defgen -i project.def -o project.vh -b v` → 每行变成 `` `define KEY VAL ``。
4. 流水线末尾 `Makefile` 删掉中间文件 `project.def`，只留 `project.h`/`project.vh` 两个产物。

`defgen` 支持四种后端（`c`/`v`/`pl`/`py`），靠加不同前缀实现：C 加 `#define `、Verilog 加 `` `define ``、Perl 加 `$`、Python 不加前缀。它用一条正则识别输入行：`^\s*%define\s*(\w+)\s*(\d+)?`——捕获宏名和（可选的）纯数字值。

#### 4.3.3 源码精读

先看 `Makefile` 如何声明「要生成哪些后端」：

[Makefile:7-10](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/Makefile#L7-L10) `BACKENDS = h vh`，再用 `$(BACKENDS:%=project.%)` 展开成目标 `project.h project.vh`。想加 Python 后端只需把 `py` 加进 `BACKENDS` 并补一条规则。

CPP 规则（流水线第一级）：

[Makefile:21-23](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/Makefile#L21-L23) 输入是 `$(PROJECT).spec`（`PROJECT` 取自 `tree.make`，本档即 `nv_full`），输出 `project.def`。`$(CPP)` 来自 [tools.mk](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/tools.mk)。注意 [Makefile:15-16](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/Makefile#L15-L16) 在 `default` 目标末尾 `@rm $(OUT_DIR)/project.def`，删掉中间产物。

defgen 两个后端规则（流水线第二级）：

[Makefile:28-32](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/Makefile#L28-L32) 同一份 `project.def`，`-b c` 出 `project.h`，`-b v` 出 `project.vh`。

再看 defgen 的核心逻辑。命令行解析与后端选项：

[defgen:48-63](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/defgen#L48-L63) 用 `Getopt::Long` 解析 `-i/-o/-b`，`-b` 即后端选择。

识别 `%define` 与后端分发：

[defgen:80-98](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/defgen#L80-L98) 是全部魔法所在。逐行读输入：
- 第 81 行正则 `^\s*%define\s*(\w+)\s*(\d+)?`：匹配 `%define` 开头，捕获宏名 `$1`、可选纯数字值 `$2`。
- 第 83-92 行按后端选前缀：`c`→`#define `、`v`→`` `define ``、`pl`→`$`、`py`→空串。
- 第 95 行 `$val = $2 ? " $2" : ""`：有值则前补空格，无值（特性标志宏）则空串。
- 第 97-98 行拼出 `前缀+宏名+值` 写入输出文件。

一个值得注意的细节——第 100 行有个裸 `print;`：

[defgen:100](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/defgen#L100) `print;`（等价 `print STDOUT $_;`）把**每一行输入原样回显到 stdout**，而改写后的内容只写入 `-o` 指定的文件。所以跑 defgen 时屏幕上会看到整份 `project.def` 的回显，文件里却只有翻译后的 `define` 行——这是一种「边跑边打日志」的副作用，初读容易困惑。

#### 4.3.4 关键澄清：两类宏的不同消费路径（本讲最重要的诚实交代）

读完生成层，必须回答一个容易被想当然的问题：**这些 `NVDLA_*` 宏，到底怎么「影响最终 RTL」？** 答案分两类，不能混为一谈。

**第一类：ENABLE 类特性宏 → 真的进 RTL。** vmod 的构建用 `vcp`（包了一层 CPP）配 `-imacros project.h`，把 RTL 里的 `#ifdef NVDLA_*` 分支就地剥离：

[vmod_common.make:13-30](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/vmod_common.make#L13-L30) 先断言 `project.h` 必须存在（第 15-17 行，不存在直接 `$(error)` 退出，要求先 `make hw/spec/defs`），再在第 29-30 行 `vcp ... -imacros $(PROJ_HEAD)` 把 `project.h` 的 `#define` 喂给每个 `.v` 文件。于是顶层里 `#ifdef NVDLA_BDMA_ENABLE`、`NVDLA_PDP_ENABLE`、`NVDLA_CDP_ENABLE`、`NVDLA_RUBIK_ENABLE` 等分支被真实裁剪——某个引擎关掉，对应 RTL 块就不进编译。这类宏是「真正影响最终 RTL」的。

**第二类：数值尺寸宏（MAC_ATOMIC_C_SIZE 等）→ 主要喂 C-model，不直接进 vmod RTL。** 一个反直觉但可验证的事实：在整个 `vmod/` 目录里 grep `MAC_ATOMIC`，**零命中**——RTL 里根本没有按 `NVDLA_MAC_ATOMIC_C_SIZE` 命名的 `#ifdef` 或参数。这些数值宏的真正消费点是 C-model 的配置头：

[cmod/include/nvdla_config_large.h:28](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/include/nvdla_config_large.h#L28) 直接 `#define NVDLA_MAC_ATOMIC_C_SIZE 64`（同文件还有 `NVDLA_BDMA_ENABLE` 等全套，第 21 行起）。也就是说，数值规格是黄金参考模型 C-model 的输入参数；而固定档 nvdlav1 的 RTL 把阵列规模在**作者时**就烤成了字面量（如 CMAC 的 64 个乘法器是写死的硬件结构，不再用宏参数化）。`project.vh`（Verilog 后端）在此更多是「留作工具消费/保持完整性」，RTL 的 `#ifdef` 实际走的是 `project.h`（C 后端）经 vcp。

> 一句话总结这个区分：**ENABLE 宏裁剪 RTL 结构，数值宏喂 C-model 并作为规格台账**。理解这点能避免「以为改 `MAC_ATOMIC_C_SIZE_64` 就能重配 RTL 阵列」的误解——在本仓库里那样做只会让 spec 与已烤死的 RTL/cmod 头对不上，并不能动态缩放硬件。

#### 4.3.5 代码实践（实跑 defgen，本讲主实践）

**实践目标**：亲手跑通「CPP → defgen」两级流水线，验证本讲主角 `MAC_ATOMIC_C_SIZE_64` 如何变成最终宏，并看清 C/Verilog 两后端的差异。

**操作步骤**：

1. 准备一个最小输入文件 `/tmp/demo.def`，内容两行（注意 `%define` 前缀）：
   ```
   %define NVDLA_MAC_ATOMIC_C_SIZE 64
   %define NVDLA_BDMA_ENABLE
   ```
   （一行带值、一行无值，正好覆盖 defgen 正则的两种情况。）
2. 跑 Verilog 后端：
   ```
   tools/bin/defgen -i /tmp/demo.def -o /tmp/demo.vh -b v
   cat /tmp/demo.vh
   ```
3. 跑 C 后端：
   ```
   tools/bin/defgen -i /tmp/demo.def -o /tmp/demo.h -b c
   cat /tmp/demo.h
   ```
4. 进阶（可选）：把真实 spec 跑一遍完整流水线，看真实产物：
   ```
   cpp -undef -nostdinc -P -C spec/defs/nv_full.spec > /tmp/real.def
   tools/bin/defgen -i /tmp/real.def -o /tmp/real.h -b c
   grep -nE 'MAC_ATOMIC|BDMA_ENABLE|CBUF_BANK_NUMBER' /tmp/real.h
   ```

**需要观察的现象**：

- 步骤 2、3 中，屏幕上会先回显 `/tmp/demo.def` 的全部内容（这是 defgen 第 100 行 `print;` 的副作用），然后 `cat` 出来的 `.vh`/`.h` 文件里只有翻译后的 define 行。
- 同一个 `%define`，两后端前缀不同。

**预期结果**（依据 [defgen:80-98](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/defgen#L80-L98) 的正则与后端分发逻辑推导）：

- `/tmp/demo.vh`：
  ```
  `define NVDLA_MAC_ATOMIC_C_SIZE 64
  `define NVDLA_BDMA_ENABLE
  ```
- `/tmp/demo.h`：
  ```
  #define NVDLA_MAC_ATOMIC_C_SIZE 64
  #define NVDLA_BDMA_ENABLE
  ```
  （无值的 `BDMA_ENABLE` 行不带尾随数值，对应 `$val` 为空串。）
- 步骤 4 的真实 `real.h` 里应能 grep 到 `#define NVDLA_MAC_ATOMIC_C_SIZE 64`、`#define NVDLA_BDMA_ENABLE`、`#define NVDLA_CBUF_BANK_NUMBER 16`，与 `projects.spec` 各 ENABLE 分支一一对应；总 define 行数约为 32（精确计数「待本地验证」）。

**诚实边界**：以上预期结果由源码逻辑确定性推出，但本讲义撰写环境未实跑（受沙箱限制），请你本地执行以确认；若发现 `real.h` 行数与预期不符，以本地实跑为准。

#### 4.3.6 小练习与答案

**练习 1**：若 `projects.spec` 里某块写成 `%define NVDLA_XY_SIZE 0x40`（十六进制），defgen 会怎样处理？

> **答**：defgen 的值捕获正则是 `(\d+)?`，只匹配**纯十进制数字**，`0x40` 不会被捕获为值。于是 `$val` 为空，输出会丢掉数值变成 `#define NVDLA_XY_SIZE`（仅留宏名）。所以 spec 里的数值必须用十进制，这也是为什么 [projects.spec](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/projects.spec) 全用 64/32/16 这样的十进制。

**练习 2**：为什么 `vmod_common.make` 用的是 `project.h`（C 后端）而不是 `project.vh`（Verilog 后端）来剥离 RTL 的 `#ifdef`？

> **答**：因为剥离 `#ifdef NVDLA_*` 用的是 C 预处理器（`vcp` 调 `cpp`），CPP 只认 `#define` 语法，所以喂 C 后端的 `project.h` 最直接。`project.vh` 的 `` `define `` 是给 Verilog 编译器/其他工具用的，CPP 不认反引号 define。这也解释了为什么 RTL 里是 `#ifdef`（CPP 语法）而非 `` `ifdef `` 来做配置裁剪——配置裁剪发生在 vcp（CPP）阶段，早于真正的 Verilog 编译。

---

## 5. 综合实践：追踪一个规格宏的完整生命

把三个模块串起来，做一次端到端追踪，目标宏是本讲主角 **`MAC_ATOMIC_C_SIZE_64`**。

**任务**：画出 `MAC_ATOMIC_C_SIZE_64` 从 spec 到消费的完整链路，并对比一个 ENABLE 类宏（`BDMA_ENABLE`）说明二者「影响 RTL 的方式不同」。

**操作步骤**：

1. **答卷层**：在 [nv_full.spec:16](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/nv_full.spec#L16) 找到 `#define MAC_ATOMIC_C_SIZE_64`（数值藏在后缀，无显式值）。
2. **翻译层**：进入 [projects.spec:99-105](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/projects.spec#L99-L105)，确认它被翻译成 `%define NVDLA_MAC_ATOMIC_C_SIZE 64`。
3. **生成层**：按 4.3.5 步骤 4 实跑，确认 `real.h` 中出现 `#define NVDLA_MAC_ATOMIC_C_SIZE 64`。
4. **消费层（关键）**：
   - 对比 `BDMA_ENABLE`：在 [projects.spec:57-62](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/projects.spec#L57-L62) 翻成 `%define NVDLA_BDMA_ENABLE`，进而在 vmod RTL 里能 grep 到 `#ifdef NVDLA_BDMA_ENABLE`（真实裁剪 RTL）。
   - 而 `NVDLA_MAC_ATOMIC_C_SIZE`：在 `vmod/` 全树 grep 不到，却在 [cmod/include/nvdla_config_large.h:28](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/include/nvdla_config_large.h#L28) 找到 `#define NVDLA_MAC_ATOMIC_C_SIZE 64`——它喂养 C-model，RTL 侧则把 64 烤成字面量。

**需要观察的现象**：你会清楚看到两条分叉——ENABLE 宏进 RTL 的 `#ifdef`，数值宏进 C-model 的配置头。

**预期结果（一张追踪表）**：

| 阶段 | MAC_ATOMIC_C_SIZE_64 | BDMA_ENABLE |
|------|----------------------|-------------|
| 答卷 nv_full.spec | L16 `#define MAC_ATOMIC_C_SIZE_64` | L10 `#define BDMA_ENABLE` |
| 翻译 projects.spec | L99-105 → `%define NVDLA_MAC_ATOMIC_C_SIZE 64` | L57-62 → `%define NVDLA_BDMA_ENABLE` |
| 生成 project.h | `#define NVDLA_MAC_ATOMIC_C_SIZE 64` | `#define NVDLA_BDMA_ENABLE` |
| 消费 vmod RTL | 不出现（阵列规模烤成字面量） | `#ifdef NVDLA_BDMA_ENABLE` 裁剪 RTL |
| 消费 cmod | `cmod/.../nvdla_config_large.h:28` | `cmod/.../nvdla_config_large.h:21` |

**交付物**：一张如上的追踪表 + 一句话结论：**「特性宏裁 RTL、数值宏喂 cmod，二者都源自同一份 spec、经同一条 CPP+defgen 流水线展开。」**

## 6. 本讲小结

- `nv_full.spec` 是规格的单一事实来源，用约 32 个 `#define` 用户开关描述 nvdlav1 的固定特性，末尾 `#include "projects.spec"` 委托翻译。
- `projects.spec` 是翻译 + 校验表，把每个用户开关翻成标准 `%define NVDLA_* [值]`，并用 `#else #error` 强制每个二选一开关都作出了选择（构建期早失败）。
- `Makefile` 编排两级流水线：CPP 把 spec 求值成中间 `project.def`（只剩 `%define`），defgen 再按 `-b c/v/pl/py` 后端改写成 `project.h`/`project.vh`，最后删掉中间文件。
- `defgen` 是一个不到 120 行的 Perl 脚本，核心是正则 `^\s*%define\s*(\w+)\s*(\d+)?` + 后端前缀分发；第 100 行 `print;` 会把输入逐行回显到 stdout（易混淆的副作用）。
- **关键区分**：ENABLE 类特性宏经 `vcp -imacros project.h` 真实裁剪 RTL 的 `#ifdef`；数值尺寸宏主要喂养 C-model 配置头 `cmod/include/nvdla_config_large.h`，vmod 侧阵列规模已烤成字面量。改 spec 数值并不能动态重配 RTL。
- 配置裁剪发生在 vcp（CPP）阶段，故 RTL 用 `#ifdef`（CPP 语法）而非 `` `ifdef ``；这也是 `project.h`（C 后端）比 `project.vh` 更早被消费的原因。

## 7. 下一步学习建议

- 本讲聚焦「特性宏 → RTL 裁剪」，下一篇 [u8-l2 寄存器规格与 RDL/Ordt 生成](u8-l2-rdl-ordt-reggen.md) 转向另一套「单一事实来源」——SystemRDL，看 `test.rdl` 如何经 Ordt 生成 [u2-l3](u2-l3-register-files-shadow-config.md) 里的 `_CSB_reg.v` 寄存器文件。二者思路同构（单一源 → 工具展开 → 多后端），可对比学习。
- 若想看 ENABLE 宏如何具体决定顶层例化，回到 [u1-l5 NV_nvdla.v 与分区结构](u1-l5-top-rtl-partitions.md) 与 [NV_NVDLA_partition_o.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v)，在其中搜索 `#ifdef NVDLA_BDMA_ENABLE` 等开关，观察引擎被条件例化的写法。
- 数值宏的 C-model 消费侧，可继续读 [u7-l3 C-model 参考模型](u7-l3-cmodel-reference.md)，理解 `nvdla_config_large.h` 如何驱动黄金参考模型的算力参数。
- 综合集成视角见 [u8-l4 端到端：编程一个网络层与集成指南](u8-l4-end-to-end-integration.md)，把配置、卷积、后处理、中断串成一次完整运行。
