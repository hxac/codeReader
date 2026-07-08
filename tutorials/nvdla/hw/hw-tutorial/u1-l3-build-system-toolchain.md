# 构建系统与工具链：tmake 与 build.config

## 1. 本讲目标

本讲是入门层的第三篇。上一篇我们建立了「目录划分 ≈ 模块划分」的源码地图；本讲要回答下一个自然的问题：**这么多分散在 `vmod/`、`spec/`、`verif/` 下的子目录，到底是怎么被拧成一股绳、最终编译出可以仿真的 RTL 的？**

读完本讲，你应当能够：

- 说清敲下 `make` 之后顶层 `Makefile` 做的第一件事，以及 `tree.make` 是什么、从哪来。
- 读懂 `tools/bin/tmake` 这个 Perl 驱动脚本：它如何把 `tools/etc/build.config` 这棵 YAML 依赖树，按拓扑序逐个 sandbox 地调用 `make`。
- 读懂每个 sandbox 内部共享的 `tools/make/vmod_common.make` 与 `common.make`：它们如何用 `vcp`、`eperl`、`defgen` 三个生成器，把带宏与内嵌 Perl 的 Verilog 模板预处理成纯 Verilog。
- 解释为什么 `vmod_nvdla_top` 必须排在 `cdma/csc/cmac/cacc` 等子模块之后编译。

本讲只讲**构建机制**，不进入任何具体 RTL 模块的电路细节。下一篇（u1-l4）才会真正跑一次仿真。

## 2. 前置知识

### 2.1 什么是「构建（build）」

软件里「编译」是把 `.c` 变成 `.o` 再链接成可执行文件。硬件里「构建」要做的事更多：

1. **展开配置**：NVDLA 有一堆 `#define`（开关、位宽、bank 数），要先生成统一的宏头文件。
2. **预处理 Verilog**：很多 `.v` 文件里夹杂着 `#ifdef` 风格的条件编译，以及内嵌的 Perl 脚本（用来批量生成重复的寄存器/流水线代码），需要先被「编织」成纯 Verilog。
3. **按依赖顺序产出**：先把底层模块处理好，再处理例化它们的顶层。
4. **交给仿真器/综合器**：把处理好的文件列表交给 VCS、Verilator 或 Design Compiler。

NVDLA 把上面每一步都拆成了可复用的小工具，再用 `make` 与一个 Perl 编排器（`tmake`）串起来。

### 2.2 几个会反复出现的术语

| 术语 | 含义 |
|------|------|
| **TOT（Top Of Tree）** | 仓库根目录，NVDLA 通过查找 `LICENSE` 文件来定位它 |
| **sandbox（沙箱）** | 一个可以独立 `make` 的源码子目录，如 `vmod/nvdla/cdma`、`spec/defs` |
| **target（构建目标）** | `build.config` 里的一个节点，由若干 sandbox + 依赖组成，如 `vmod_nvdla_top` |
| **trace** | 仿真激励序列，下一篇详讲 |
| **eperl / vcp / defgen** | 三个核心生成器（内嵌 Perl、Verilog 预处理器、宏定义生成器） |

### 2.3 为什么不用一个巨大的 Makefile

NVDLA 有上千个 Verilog 文件、17 个功能子模块。如果全塞进一个 Makefile，既难维护也无法按需只编译某一层。所以它采用「**每个 sandbox 一个小 Makefile + 共享 include + 顶层编排器**」的分层结构——这正是本讲要拆解的内容。

## 3. 本讲源码地图

本讲涉及的关键文件按职责分组如下：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md) | 文档化最简构建命令 `bin/tmake` |
| [Makefile](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/Makefile) | 顶层入口，交互式生成 `tree.make` 环境配置 |
| [tools/bin/tmake](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/tmake) | Perl 编排器，按 `build.config` 拓扑序驱动各 sandbox |
| [tools/etc/build.config](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config) | YAML 写的整棵依赖树（sandbox 清单 + 依赖关系） |
| [tools/bin/defgen](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/defgen) | 把 `%define` 宏翻译成各后端（C / Verilog / Perl / Python）的 define |
| [tools/bin/vcp](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/vcp) | Verilog 预处理器（保护 `#` 注释后调用 C 预处理器） |
| [tools/bin/eperl](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/eperl) | 内嵌 Perl 执行器，运行 Verilog 注释里的 Perl 脚本 |
| [tools/make/common.make](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/common.make) | 所有 Makefile 的公共基础（包含 tree.make、定位 TOT） |
| [tools/make/vmod_common.make](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/vmod_common.make) | RTL sandbox 的公共规则（vcp → eperl 流水线） |
| [tools/make/tools.mk](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/tools.mk) | 定义 `VCP / EPERL / DEFGEN` 三个工具的路径 |
| [spec/defs/Makefile](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/Makefile) | 配置层 sandbox 的 Makefile，产出 `project.h/.vh` |
| [spec/defs/nv_full.spec](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/nv_full.spec) | nvdlav1 的固定特性开关集合 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **tree.make 环境配置** —— 敲 `make` 后发生的第一件事。
2. **build.config 依赖图** —— 整个构建的「总图纸」。
3. **tmake 脚本** —— 真正的编排大脑。
4. **eperl / defgen / vcp 生成器** —— sandbox 内部的代码加工流水线。

### 4.1 tree.make：一次性的环境配置文件

#### 4.1.1 概念说明

`tree.make` 是一个**用户可编辑的环境配置文件**，记录「用哪个项目、Perl/Java/SystemC 在哪」等信息。它不是源码自带的，而是由顶层 `Makefile` 在你第一次敲 `make` 时**交互式生成**的。生成一次之后，后续所有 sandbox 的 Makefile 都会通过 `include` 读它，从而知道编译环境。

把 `tree.make` 理解成「整棵构建树的环境变量总开关」即可。

#### 4.1.2 核心流程

```
用户在仓库根目录敲: make
        │
        ▼
顶层 Makefile 的 default 目标依赖 tree.make
        │
        ▼
交互式提问（一路回车即用默认值）
   - 项目名        (默认 nv_full)
   - cpp / g++ 路径
   - perl / java / systemc 路径
   - verilator / clang 路径（可选）
        │
        ▼
把回答写成 tree.make（一行一个 := 赋值）
        │
        ▼
后续每个 sandbox 的 Makefile 通过 include $(DEPTH)/tree.make 读取
```

#### 4.1.3 源码精读

顶层 `Makefile` 一开头就把目标名定死为 `tree.make`，并让 `default` 目标依赖它：

[Makefile:4](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/Makefile#L4) 把 `tree.make` 定义为一个变量；[Makefile:10](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/Makefile#L10) 是整个仓库的入口目标——敲 `make` 就是为了生成它。

[Makefile:12-19](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/Makefile#L12-L19) 列出了一组默认工具路径（`DEFAULT_CPP`、`DEFAULT_GCC`、`DEFAULT_PERL`、`DEFAULT_JAVA`、`DEFAULT_SYSTEMC`、`DEFAULT_VERILATOR`、`DEFAULT_CLANG`），以及默认项目 `DEFAULT_PROJ := nv_full`。注意这些 `/home/utils/...` 路径是 NVIDIA 内部机器的路径，**在你自己的机器上需要在交互提问时改成你本机的路径**（见下面的实践）。

真正生成 `tree.make` 的 recipe 在 [Makefile:21-64](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/Makefile#L21-L64)：它用一堆 `@echo ... >> $@` 往目标文件里写注释和赋值，其中每一项工具都用 `@read -p ...` 读你的输入，留空则采用上面的默认值。例如项目名这一行：

[Makefile:35](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/Makefile#L35) 提示「Enter project names (Press ENTER to use: nv_full)」，回车就会写入 `PROJECTS := nv_full`。

这个 `PROJECTS` 变量非常关键——它决定后面 `tmake` 要为哪个 project 构建。而 `tmake` 正是靠读 `tree.make` 里的 `PROJECTS` 行来获取项目名的（后面 4.3 节会看到）。

#### 4.1.4 代码实践

**实践目标**：亲手生成一次 `tree.make` 并看懂它的内容。

**操作步骤**：

1. 在仓库根目录执行 `make`。
2. 面对一连串提问，**全部直接回车**（先用默认值，能跑通再说）。
3. 用 `cat tree.make` 查看生成结果。

**需要观察的现象**：终端会逐行提问；`tree.make` 里会出现形如 `PROJECTS := nv_full`、`CPP := ...`、`PERL := ...` 的赋值。

**预期结果**：仓库根目录多出一个 `tree.make` 文件。如果你本机没有 `/home/utils/perl-5.8.8/bin/perl`，这一步先生成文件即可，稍后可手动改成本机真实路径。

> 若交互提问在你的环境里卡住（比如 `read` 不可用），也可以**手写**一个最小 `tree.make`，至少包含 `PROJECTS := nv_full` 和指向你本机工具的 `PERL :=` / `CPP :=` / `GCC :=` 三行——后续 sandbox 的 make 只关心这几个变量能否被 include 到。**待本地验证**：你本机的 Perl/g++ 具体路径。

#### 4.1.5 小练习与答案

**练习 1**：为什么 NVDLA 要把环境配置单独放进 `tree.make`，而不是直接写死在每个 sandbox 的 Makefile 里？

**参考答案**：因为环境（Perl/g++/Java 路径、项目名）因机器而异，且整棵树共享同一份；集中到一个文件里，改一处即可，避免几十个 Makefile 各自维护。

**练习 2**：`PROJECTS := nv_full` 这一行会被谁读取？

**参考答案**：会被 `tmake` 脚本的 `get_projects` 函数读取（用来知道要构建哪些 project），也会被 `common.make` 通过 `include tree.make` 读入，再由 `PROJECT ?= $(firstword $(PROJECTS))` 选出当前 project。

---

### 4.2 build.config：整棵构建依赖树

#### 4.2.1 概念说明

`tools/etc/build.config` 是一个 **YAML 文件**，它是整个构建系统的「总图纸」。它用一种非常简洁的格式同时表达了两件事：

- 每个**构建目标（target）**包含哪些 **sandbox**（要 `make` 哪些目录）；
- 每个 target **依赖**哪些其他 target。

可以说：**看懂了 `build.config`，就看懂了 NVDLA 整个仓库的构建骨架**。它和上一篇讲的「目录划分 ≈ 模块划分」是一一对应的——这里把每个模块目录登记成一个 sandbox，再用依赖关系把它们连成 DAG（有向无环图）。

#### 4.2.2 核心流程

`build.config` 里每个 target 的结构固定为：

```yaml
<target名>:
  sandbox:          # 可选：要 make 的目录列表
    - <目录1>
    - <目录2>
  dependencies:     # 可选：依赖的其他 target
    - <target_A>
    - <target_B>
```

由此构成的依赖 DAG 大致长这样（只画主干）：

```
                        defs ──┐
                        manual ┤
                               ▼
vmod_vlibs ─┐             vmod_nvdla_deps ◄── 汇聚点
vmod_rams ──┤                  │
vmod_include ┘                 ├─► vmod_nvdla_car ─┐
                               ├─► vmod_nvdla_cdma │
                               ├─► vmod_nvdla_csc  ├─► vmod_nvdla_glb ─┐
                               ├─► vmod_nvdla_cmac │                    │
                               ├─► ...（每个引擎） │                    ▼
                               └─► vmod_nvdla_cacc ┴──────► vmod_nvdla_top
                                                                 │
                                                  ┌──────────────┴──────────────┐
                                                  ▼                             ▼
                                            verif_sim                       verilator
```

`tmake` 的任务就是**反复摘取叶子节点（没有任何未构建依赖的节点）**，构建它，再从图里删掉，直到目标子树全部建完——这正是经典的拓扑排序。

#### 4.2.3 源码精读

`build.config` 开头是配置与寄存器规格两个最底层 target，它们没有任何依赖，是整棵树的根：

[tools/etc/build.config:1-8](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L1-L8) 定义 `defs`（对应 `spec/defs`）与 `manual`（对应 `spec/manual`）。`defs` 产出宏头文件，`manual` 产出寄存器模型——所有 RTL 都依赖它们。

中间有一个关键的**汇聚点** `vmod_nvdla_deps`：

[tools/etc/build.config:36-42](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L36-L42) 它**自己没有任何 sandbox**（不直接 make 任何目录），只把 `manual / vmod_vlibs / vmod_include / vmod_rams` 四个公共依赖打包成一个别名。这样下面所有 nvdla 引擎只需写 `dependencies: [vmod_nvdla_deps]` 一行，就能继承这四个依赖——这是 YAML 依赖图里非常常见的「公共依赖收口」技巧。

每个引擎 target 都长一个样，例如卷积取数引擎：

[tools/etc/build.config:73-78](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L73-L78) 定义 `vmod_nvdla_cdma`，sandbox 是 `vmod/nvdla/cdma`，依赖 `vmod_nvdla_deps`。卷积五件套（cdma/csc/cmac/cacc/cbuf）都是这个形状，彼此**平级**、互不依赖。

整棵树的终点是顶层 target：

[tools/etc/build.config:140-159](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L140-L159) `vmod_nvdla_top` 的 sandbox 只有 `vmod/nvdla/top`，却**依赖几乎所有引擎**：apb2csb、cdma、cbuf、csc、cmac、cacc、sdp、pdp、cdp、bdma、rubik、glb、csb_master、nocif、retiming、car。因为 `top` 里的 `NV_nvdla.v` 会例化所有这些子模块，是它们的集大成者。

再往下是两条仿真出口：

[tools/etc/build.config:161-172](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L161-L172) `verilator`（开源 Verilator 路径）与 `verif_sim`（VCS 路径，标记了 `optional: true`）都依赖 `vmod_nvdla_top`。注意 `verif_sim` 带 `optional: true`，表示即使本机没有 VCS 也不应让整棵构建失败。

#### 4.2.4 代码实践

**实践目标**：完成大纲指定的实践——定位 `vmod_nvdla_top` 的全部依赖，并解释为什么卷积子模块必须在 top 之前编译。

**操作步骤**：

1. 打开 [tools/etc/build.config:140-159](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L140-L159)，逐行抄下 `vmod_nvdla_top` 的 `dependencies`。
2. 在 16 个依赖里圈出「卷积核心四件套」：`vmod_nvdla_cdma`、`vmod_nvdla_csc`、`vmod_nvdla_cmac`、`vmod_nvdla_cacc`（外加 `vmod_nvdla_cbuf`）。
3. 回溯它们的依赖，确认它们都只依赖 `vmod_nvdla_deps`，彼此平级。

**需要观察的现象**：你会发现 cdma/csc/cmac/cacc 之间**没有**互相依赖，但都排在 top 之前。

**预期结果 / 解释**：

- **直接原因（机制层）**：`tmake` 按拓扑序构建——先建依赖、再建被依赖者。top 显式声明了这四个为依赖，所以必然先建。
- **根本原因（工程层）**：每个 sandbox 的 make 会把它目录下的 `.v` 用 `vcp + eperl` 预处理，输出到统一的 `outdir/<project>/...` 目录树。`top` 目录里的 `NV_nvdla.v` **例化**了 `NV_NVDLA_cdma/csc/cmac/cacc`，下游的 `verif_sim`/`verilator` 要把这些预处理后的文件一起喂给仿真器。如果 top 先建、卷积子模块还没产出文件，下游组装文件列表时就会缺件，仿真无法编译。所以依赖图实际上编码的是「**模块例化关系决定构建顺序**」。
- **顺带结论**：因为这四者平级（共享 `vmod_nvdla_deps`），它们之间顺序无关、理论上可以并行——`build.config` 只保证「都在 top 之前」，不规定四者内部先后。

#### 4.2.5 小练习与答案

**练习 1**：`vmod_nvdla_deps` 自己没有 `sandbox` 字段，那它存在的意义是什么？

**参考答案**：它是一个「纯依赖聚合」节点，把 `manual/vmod_vlibs/vmod_include/vmod_rams` 四个公共依赖收口成一个别名，让下游引擎只写一行依赖即可继承全部，减少重复。

**练习 2**：如果我在 `build.config` 里把 `vmod_nvdla_top` 的依赖里删掉 `vmod_nvdla_cdma`，会发生什么？

**参考答案**：拓扑序不再保证 cdma 在 top 之前构建。tmake 仍可能（因为别的路径）先建 cdma，也可能后建；一旦 cdma 的预处理文件在下游 `verif_sim` 组装文件列表时尚未产出，仿真编译就会因为缺少 `NV_NVDLA_cdma` 的文件而失败。这正是依赖图必须完整的原因。

**练习 3**：`verif_sim` 标了 `optional: true`，`verilator` 没标。这两条路径分别面向什么用户？

**参考答案**：`verif_sim` 用 Synopsys VCS（商业、需 license），标 optional 是让没有 VCS 的环境不至于整体失败；`verilator` 是开源仿真器，任何人都能用，是开源用户的主路径。

---

### 4.3 tmake：按拓扑序驱动一切的编排器

#### 4.3.1 概念说明

`tools/bin/tmake` 是一个 Perl 脚本，它是 README 里推荐的最简构建命令（[README.md:55-56](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L55-L56) 写的 `bin/tmake`）。它做的事很简单却很关键：

> 读 `build.config`，对每个想要的 target 子树，**反复找出当前没有任何未构建依赖的叶子节点，调用 `make -C <sandbox>` 构建它，再从图里删掉**，直到子树建完。

它本身**不编译任何代码**——编译由每个 sandbox 自己的 Makefile 完成；tmake 只负责「按对的顺序、对对的目录、依次喊一声 make」。

#### 4.3.2 核心流程

tmake 的主循环是一个**反复摘叶子的拓扑排序**，伪代码如下：

```
读 tree.make 得到 PROJECTS（要构建哪些 project）
加载 build.config 为 YAML 树 $tree
默认要构建的 target 前缀 @build = ["verif"]    # 见源码 line 39

对每个 project:
    while 树里还有节点:
        在树里找第一个「名字以 @build 任一前缀开头」的节点 key
            （默认即匹配 "verif_sim"）
        若找不到 → 跳出循环（这个子树建完了）
        leaf = find_any_leaf($tree, key)        # 从 key 一路向下递归到叶子
        for sandbox in leaf.sandbox:
            执行: make -C <sandbox> PROJECT=<project>
        标记 leaf 已构建，从树里删除 leaf
打印 BUILD PASS
```

其中 `find_any_leaf` 是一个递归 DFS：从给定节点出发，只要它还有「未构建」的依赖，就钻进那个依赖继续找，直到找到一个所有依赖都已建好的节点（叶子）返回。

#### 4.3.3 源码精读

脚本一上来声明默认行为：

[tools/bin/tmake:39](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/tmake#L39) `push @build, "verif" unless @build;`——如果你没传 `--build`，默认就构建名字以 `verif` 开头的 target。注意 `/^verif/` 能匹配 `verif_sim`，但**匹配不到 `verilator`**（它以 `veril` 开头）。所以**直接敲 `tmake` 走的是 VCS 路径**；想走 Verilator 路径要显式 `tmake --build verilator`。这是初学者常踩的坑。

[tools/bin/tmake:43](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/tmake#L43) 指明依赖树文件就是 `tools/etc/build.config`；[tools/bin/tmake:49](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/tmake#L49) 调用主函数 `build()`。

主循环在 [tools/bin/tmake:76-107](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/tmake#L76-L107)。其中：

- [tools/bin/tmake:83](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/tmake#L83) 用 `grep {$keys[$i] =~ /^$_/} @build` 找出第一个名字匹配构建前缀的 target。
- [tools/bin/tmake:95](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/tmake#L95) 调 `find_any_leaf` 钻到叶子。
- [tools/bin/tmake:99-102](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/tmake#L99-L102) 对叶子的每个 sandbox 执行 `make -C $sandbox PROJECT=$project ...`，输出 tee 到日志。注意 `==0 or die` ——任何一个 sandbox 的 make 失败，整个构建立即终止。
- [tools/bin/tmake:104-105](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/tmake#L104-L105) 把这个叶子标记为 `$done` 并从 `$tree` 里 `delete`，下一轮循环就不会再选它。

递归找叶子的 `find_any_leaf` 在 [tools/bin/tmake:119-136](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/tmake#L119-L136)：它对当前节点的每个依赖，只要还没 `$done`，就递归钻进去；所有依赖都已完成时，当前节点就是叶子，返回它。这就是拓扑排序的「摘叶子」核心。

最后，[tools/bin/tmake:138-153](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/tmake#L138-L153) 的 `get_projects` 用正则 `^\s*PROJECTS` 从 `tree.make` 抓出项目列表——这正是 4.1 节 `tree.make` 里那行 `PROJECTS := nv_full` 的消费者。

> 小贴士：tmake 还提供 `--only <target>`（只构建某 target，见 [tools/bin/tmake:68-75](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/tmake#L68-L75)）与 `--clean`（清理）等选项，调试单个模块时很有用。

#### 4.3.4 代码实践

**实践目标**：不改任何源码，仅用「源码阅读 + 干跑」推演出 tmake 默认构建的顺序。

**操作步骤**：

1. 读 [tools/bin/tmake:39](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/tmake#L39)，确认默认 `@build = ["verif"]`。
2. 在 `build.config` 里找出名字以 `verif` 开头的 target → `verif_sim`。
3. 手动模拟 `find_any_leaf(verif_sim)`：它依赖 `vmod_nvdla_top` → 顶层又依赖一串引擎 → 引擎依赖 `vmod_nvdla_deps` → 最终叶子是 `defs`/`manual`/`vmod_vlibs`/`vmod_include`/`vmod_rams`。
4. 写下你预测的前 5 个被构建的 target。

**需要观察的现象**：你应当发现第一个被 build 的总是 `defs`（或 `manual`/`vlibs` 之一，它们都是互不依赖的根），最后才是 `verif_sim`。

**预期结果**：一种合法的构建顺序是 `defs → manual → vmod_vlibs → vmod_include → vmod_rams → vmod_nvdla_car → ... → vmod_nvdla_top → verif_sim`。叶子之间的先后由 YAML 哈希迭代顺序决定，但「依赖必先于被依赖者」恒成立。若本地装了 Perl，可执行 `./tools/bin/tmake --debug --norun`（`--norun` 关闭实际执行，`--debug` 打印选中的 key/leaf）观察打印，与你的推演对比。**待本地验证**：`--norun` 在你本地 tmake 版本上是否生效（看 `--run!` 选项 [tools/bin/tmake:34](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/tmake#L34)）。

#### 4.3.5 小练习与答案

**练习 1**：为什么直接敲 `tmake` 不会构建 `verilator`？

**参考答案**：默认 `@build=["verif"]`，匹配规则是 `$key =~ /^verif/`；`verilator` 以 `veril` 开头不匹配，故默认不构建。要构建它需 `tmake --build verilator`。

**练习 2**：`find_any_leaf` 是深度优先还是广度优先？它保证「依赖先于被依赖者构建」吗？

**参考答案**：深度优先（递归钻进第一个未完成依赖）。保证——因为一个节点只有在所有依赖都进入 `$done` 后才会被当作叶子返回并构建，这等价于拓扑排序。

**练习 3**：如果中途某个 sandbox 的 `make` 返回非 0，tmake 会怎样？

**参考答案**：[tools/bin/tmake:100](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/tmake#L100) 的 `==0 or die` 会让脚本立即终止，不会继续构建后续 target。

---

### 4.4 eperl / defgen / vcp：sandbox 内部的代码加工流水线

#### 4.4.1 概念说明

tmake 只负责喊 `make -C <sandbox>`；真正「加工 Verilog」的是每个 sandbox 内部共享的一套 Makefile（`common.make` + `vmod_common.make`）和三个生成器：

| 工具 | 输入 | 输出 | 干什么 |
|------|------|------|--------|
| **defgen** | `.spec` 里的 `%define` 行 | 各后端的 define | 把一份宏定义翻译成 C(`#define`) / Verilog(`` `define ``) / Perl / Python 四种写法 |
| **vcp** | 带 `#ifdef` 的 `.v` | 展开条件编译后的 `.vcp` | 保护 Verilog 注释里的 `#`，再借用 C 预处理器 `cpp` 展开 `#ifdef/#define` |
| **eperl** | 注释里内嵌 Perl 的 `.vcp` | 纯 Verilog | 执行 Verilog 注释里 `//:` 后的 Perl 脚本，把生成结果插回文件 |

它们的依赖顺序是：**defgen（造宏头）→ vcp（展开 ifdef）→ eperl（跑内嵌 Perl）**。下面分别看。

#### 4.4.2 核心流程

整条 RTL 加工流水线（以一个 vmod sandbox 为例）：

```
spec/defs/nv_full.spec   ──┐
spec/defs/projects.spec ──┤  (CPP 预处理)
                           ▼
                    project.def  ──(defgen -b c / -b v)──► project.h  / project.vh
                                                                     │ (宏头)
                                                                     ▼
vmod/nvdla/xxx/*.v  ──(vcp -imacros project.h)──► *.vcp  (展开 #ifdef)
                                                     │
                                                     ▼
                       *.vcp ──(eperl，跑 //: 脚本)──► outdir/.../纯 Verilog
```

`project.h / project.vh` 是「**全树共享的宏头**」，所有 vmod sandbox 都依赖它（见 `vmod_common.make` 里的存在性检查）。

#### 4.4.3 源码精读

**先看 defgen**。它的核心是把 `.spec` 文件里的 `%define` 行翻译成不同后端：

[tools/bin/defgen:81](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/defgen#L81) 用正则 `/^\s*%define\s*(\w+)\s*(\d+)?/` 匹配形如 `%define NVDLA_MAC_ATOMIC_C_SIZE 64` 的行；[tools/bin/defgen:83-93](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/defgen#L83-L93) 根据后端 `-b` 选前缀：`c` → `#define`、`v` → `` `define ``、`pl` → `$`、`py` → 空。这就是「一份宏定义，多后端输出」。

`%define` 来自哪里？来自 `projects.spec`。例如：

[spec/defs/projects.spec:99-105](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/projects.spec#L99-L105) 把用户开关 `MAC_ATOMIC_C_SIZE_64` 翻译成带数值的内部宏 `%define NVDLA_MAC_ATOMIC_C_SIZE 64`（或 `8`）。而用户开关本身定义在 `nv_full.spec`：

[spec/defs/nv_full.spec:16](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/nv_full.spec#L16) `#define MAC_ATOMIC_C_SIZE_64`，并在 [spec/defs/nv_full.spec:36](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/nv_full.spec#L36) `#include "projects.spec"` 把映射逻辑引出来。所以链路是：`nv_full.spec`（用户开关）→ `projects.spec`（映射+校验，产生 `%define`）→ `defgen`（翻译成 `project.h/.vh`）。

这套转换由 `spec/defs/Makefile` 编排：

[spec/defs/Makefile:21-23](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/Makefile#L21-L23) 先用 `cpp` 把 `nv_full.spec`（含 `#include`）展平成 `project.def`；[spec/defs/Makefile:28-32](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/Makefile#L28-L32) 再用 `defgen` 分别以 `-b c` 和 `-b v` 产出 `project.h` 与 `project.vh`。

**再看 vcp**。Verilog 里 `#` 是合法字符（如 `#5` 延时、`#(parameter)`），直接交给 `cpp` 会被误判为预处理指令。vcp 的解法是先把非指令的 `#` 临时替换成占位串，跑完 `cpp` 再换回来：

[tools/bin/vcp:34-38](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/vcp#L34-L38) 只保留真正的 `#define/#ifdef/...` 行，其余行的 `#` 全部替换掉；[tools/bin/vcp:44-45](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/vcp#L44-L45) 再调用 `cpp -imacros $imacros` 展开宏（`$imacros` 就是上一步的 `project.h`）。这样 Verilog 就能安全地用 `#ifdef NVDLA_*` 做条件编译。

**再看 eperl**。它在 Verilog 注释里识别 `//:` 或 `#:` 开头的行，当作 Perl 脚本执行，输出夹在 `generated_beg/generated_end` 标记之间：

[tools/bin/eperl:179-194](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/eperl#L179-L194) 扫描每行，遇到 `//:` 就把脚本累积进 `$Script`，遇到非脚本行就 `EvalScript` 执行并把结果写进输出。可用的内建插件（`flop/pipe/retime/assert`）登记在 [vmod/plugins/eperl.pm:8-11](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/plugins/eperl.pm#L8-L11)，例如 `//: &eperl::flop(...)` 能自动生成一段带复位的寄存器always块。这部分会在 u6-l5（retime/eperl 插件）深入，本讲只要知道「Verilog 注释里能藏 Perl，由 eperl 执行」即可。

**最后看这三者如何被 Makefile 串起来**。所有 vmod sandbox 的 Makefile 都只有两行：

[vmod/nvdla/top/Makefile:1-2](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/Makefile#L1-L2) 设 `DEPTH` 后 include `vmod_common.make`。真正的规则在 [tools/make/vmod_common.make](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/vmod_common.make)：

- [tools/make/vmod_common.make:7-10](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/vmod_common.make#L7-L10) 用 `find` 收集当前 sandbox 下所有 `.vh/.h/.v/.vlib` 文件作为待处理对象。
- [tools/make/vmod_common.make:13-17](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/vmod_common.make#L13-L17) 检查 `project.h` 是否存在——不存在就报错并提示「请先 make hw/spec/defs」。这正是 `build.config` 里所有 vmod 依赖 `defs` 的原因在 Makefile 层面的体现。
- [tools/make/vmod_common.make:28-30](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/vmod_common.make#L28-L30) 是 vcp 规则：`%.vcp` 由原文件 + `project.h` + `VCP` 生成，调用 `$(VCP) -imacros $(PROJ_HEAD)`。
- [tools/make/vmod_common.make:32-38](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/vmod_common.make#L32-L38) 是最终规则：把 `.vcp` 经 eperl 加工成 `outdir` 下的纯 Verilog。

三个工具的路径在 [tools/make/tools.mk:5-12](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/tools.mk#L5-L12) 定义为 `VCP / EPERL / DEFGEN`，都指向 `tools/bin/` 下对应脚本。

而这一切的公共底座 `common.make` 做两件事：[tools/make/common.make:30](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/common.make#L30) `include tree.make`（读环境），[tools/make/common.make:45-46](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/common.make#L45-L46) 用 `depth` 脚本定位 TOT（靠向上找 `LICENSE` 文件，见 [tools/bin/depth:54-71](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/depth#L54-L71)）。

#### 4.4.4 代码实践

**实践目标**：手动走一遍「defgen 翻译宏」的最小复现，理解多后端输出。

**操作步骤**：

1. 准备一个临时输入文件 `/tmp/demo.spec`，内容为一行：`%define NVDLA_MAC_ATOMIC_C_SIZE 64`。
2. 运行（注意 `-b` 后端）：
   ```
   perl tools/bin/defgen -i /tmp/demo.spec -o /tmp/out.vh -b v
   perl tools/bin/defgen -i /tmp/demo.spec -o /tmp/out.h  -b c
   ```
3. 用 `cat` 查看两个输出文件。

**需要观察的现象**：同一个 `%define`，`-b v` 产出 `` `define NVDLA_MAC_ATOMIC_C_SIZE 64``，`-b c` 产出 `#define NVDLA_MAC_ATOMIC_C_SIZE 64`。

**预期结果**：你会亲眼看到 defgen 如何用一份输入、靠 `-b` 切换前缀，同时服务 Verilog 与 C（cmod）两个世界。这就是 `project.vh`（给 RTL）与 `project.h`（给 cmod）共享同一来源的原因。**待本地验证**：你本机是否有可用的 Perl；若没有，可只做源码阅读——对照 [tools/bin/defgen:83-93](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/defgen#L83-L93) 手工推演两种前缀即可。

#### 4.4.5 小练习与答案

**练习 1**：为什么需要 vcp，而不能直接用 `cpp` 处理 Verilog？

**参考答案**：Verilog 里 `#` 是合法语法（延时、参数列表），直接用 `cpp` 会被当成预处理指令而破坏代码。vcp 先把非指令的 `#` 替换成占位串，跑完 `cpp` 再换回，从而安全地借用 `cpp` 的 `#ifdef` 能力。

**练习 2**：`vmod_common.make` 里检查 `project.h` 是否存在（[L13-17](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/vmod_common.make#L13-L17)）。这和 `build.config` 里 vmod 依赖 `defs` 是什么关系？

**参考答案**：两者是「同一约束的两道防线」。`build.config` 的依赖保证 tmake **先**构建 `defs` 产出 `project.h`；`vmod_common.make` 的存在性检查则是「万一你单独进 sandbox 手敲 make」，它会直接报错提示先 build `spec/defs`，避免莫名其妙的编译失败。

**练习 3**：如果我想给 NVDLA 加一个新的可配置开关 `FOO_ENABLE`，从 spec 到 RTL 要改哪几处？

**参考答案**：(1) 在 `nv_full.spec` 加 `#define FOO_ENABLE`；(2) 在 `projects.spec` 加一段 `#if defined(FOO_ENABLE) %define NVDLA_FOO_ENABLE ... #else #error ...` 的映射校验；(3) 在 RTL 里用 `` `ifdef NVDLA_FOO_ENABLE `` 引用。defgen/`project.vh` 会自动把映射后的宏喂给 vcp 与 RTL。

---

## 5. 综合实践：把整条构建链串起来

设计一个贯穿本讲四个模块的小任务，验证你真的理解了从 `make` 到「纯 Verilog 产出」的完整链路。

**任务**：在本机跑通「配置层 → 卷积子模块 → 顶层」的最小构建切片，并用你掌握的源码知识解释每一步看到的产物。

**操作步骤**：

1. **生成环境配置**（对应 4.1）：在仓库根目录 `make`，按提示填入本机真实路径（或回车用默认），确认生成 `tree.make`，检查其中 `PROJECTS := nv_full`。
2. **只构建配置层**（对应 4.4 的 defgen）：执行
   ```
   perl tools/bin/tmake --only defs
   ```
   （`--only` 见 [tools/bin/tmake:68-75](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/tmake#L68-L75)）。构建完成后到 `outdir/nv_full/spec/defs/` 下确认生成了 `project.def`、`project.h`、`project.vh`。打开 `project.vh`，找到 `` `define NVDLA_MAC_ATOMIC_C_SIZE 64 ``，回溯它来自 [spec/defs/nv_full.spec:16](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/nv_full.spec#L16) 与 [spec/defs/projects.spec:99-105](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/projects.spec#L99-L105)。
3. **只构建一个卷积子模块**（对应 4.2 + 4.3 + 4.4 的 vcp/eperl）：
   ```
   perl tools/bin/tmake --only vmod_nvdla_cdma
   ```
   到 `outdir/nv_full/vmod/nvdla/cdma/` 下确认原始 `.v` 已被加工成纯 Verilog（ifdef 已展开、eperl 段已生成）。
4. **回归本讲的指定问题**（对应 4.2.4）：再次打开 [tools/etc/build.config:140-159](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L140-L159)，列出 `vmod_nvdla_top` 的全部依赖；在步骤 3 之后，尝试 `perl tools/bin/tmake --only vmod_nvdla_top`，对照依赖图解释「为何 cdma/csc/cmac/cacc 必须先于 top」。

**需要观察的现象**：

- 步骤 2：`outdir` 下出现 `project.h/.vh`，且其中的宏值与 `nv_full.spec` 的开关一一对应。
- 步骤 3：`cdma` 目录下的产出文件里，`` `ifdef NVDLA_* `` 已经按宏决定保留/删除，eperl 生成的代码段夹在 `//| eperl: generated_beg ... generated_end` 之间。
- 步骤 4：`--only vmod_nvdla_top` 只会构建 `top` 自己的 sandbox（因为 `--only` 不递归依赖，见 [tools/bin/tmake:68-75](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/tmake#L68-L75)）；若依赖的子模块尚未产出文件，top 仍能单独预处理自己的 `.v`，但下游仿真会缺件——这正是依赖图存在的意义。

**预期结果**：你应当能用自己的话讲清这条链路：
`make` 生成 `tree.make` → `tmake` 读 `build.config` 按拓扑序喊 make → `spec/defs` 先用 defgen 造 `project.h/.vh` → 每个 vmod sandbox 用 vcp（借 cpp 展开 ifdef）+ eperl（跑注释 Perl）把模板加工成纯 Verilog → 顶层 `vmod_nvdla_top` 在所有子模块之后汇聚。

> 若本机缺 Perl/cpp/VCS 等工具导致命令跑不通，可退化为**纯源码阅读型实践**：按上面 4 个步骤逐个打开对应源码与产物路径，口头讲一遍每一步「输入是什么、工具做了什么、输出在哪」，同样达成学习目标。所有命令的**实际可运行性待本地验证**。

## 6. 本讲小结

- NVDLA 用「**顶层 Makefile + tmake 编排器 + 每 sandbox 共享 Makefile + 三个生成器**」的分层结构管理上千个 Verilog 文件的构建。
- 第一次敲 `make` 只做一件事：交互式生成环境配置文件 `tree.make`（项目名 + 工具路径）。
- `tools/etc/build.config` 是 YAML 写的整棵依赖树，**看懂它就看懂了仓库构建骨架**；`vmod_nvdla_top` 依赖几乎所有引擎，是依赖图的汇聚点。
- `tmake` 用「反复摘叶子」的拓扑排序驱动各 sandbox 的 make；默认 `--build verif` 走 VCS 路径，**不会**自动构建 `verilator`。
- 三个生成器各司其职：**defgen** 把 `%define` 翻译成 C/Verilog/Perl/Python 多后端宏；**vcp** 借用 cpp 安全展开 Verilog 的 `#ifdef`；**eperl** 执行注释里内嵌的 Perl 脚本生成重复 RTL。
- 「**模块例化关系决定构建顺序**」——卷积子模块 cdma/csc/cmac/cacc 必须在 top 之前编译，因为 top 例化它们，下游仿真需要完整的预处理文件集。

## 7. 下一步学习建议

- **下一篇 u1-l4（运行第一次仿真）**：本讲只把 RTL 加工成纯 Verilog，还没真正跑仿真。下一篇将用 `verif/sim/Makefile` 与 `run_sanity` 跑通一个 sanity trace，看到 PASSED。你会发现 `verif_sim` 正是本讲依赖图的终点。
- **u8-l1（spec/defs 配置体系与 defgen）**：如果想更深入 `nv_full.spec` / `projects.spec` 的开关映射与校验机制，那是专门讲配置体系的一篇。
- **u6-l5（retime 与 eperl 插件）**：本讲只点到 eperl 能跑注释里的 Perl；那篇会详解 `flop/pipe/retime` 插件如何自动生成流水寄存器。
- **继续阅读建议**：动手把 `tools/bin/tmake` 通读一遍（只有约 160 行 Perl），它是理解整个构建系统投入产出比最高的一份源码；再对照 `build.config` 手动模拟一次拓扑排序，本讲的内容就真正「钉」在脑子里了。
