# 仓库结构与构建系统

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `pulp-platform/axi` 仓库里 `src/`、`include/`、`test/`、`doc/`、`scripts/` 各自承担什么职责，拿到任意一个文件能判断它属于哪一类。
- 看懂 `Bender.yml` 里 `sources` 段落中「Level 0–6」分层注释的含义，并理解它为什么决定编译顺序。
- 区分 `Bender.yml`、`axi.core`、`src_files.yml`、`ips_list.yml` 四个清单文件分别服务于哪套工具链、为什么要并存。
- 理解 `include/axi/` 下三类 `.svh` 宏文件是如何被「导出」给本库和下游使用的。

本讲只讲「仓库怎么组织、怎么编译」，**不**讲任何 AXI 协议细节，也不教你跑某一条具体仿真命令（那是后续讲义的内容）。

## 2. 前置知识

阅读本讲前，最好已经读过上一讲《项目定位与设计哲学》(u1-l1)，知道这是一套用 SystemVerilog 写的 AXI4/AXI4-Lite 片上互联 IP 库，核心哲学是「组合优于配置」——把职责单一的小模块背靠背拼起来。

本讲会用到几个软件工程里的常识概念，先用一句话解释：

- **包管理器（package manager）**：类似 npm/cargo，负责声明「我这个硬件库依赖哪些别的硬件库」，并按声明把它们拉下来、排好编译顺序。本库用的是 pulp-platform 自研的 **Bender**。
- **编译顺序 / 依赖层级**：SystemVerilog 里，如果一个文件 `B` `import` 了文件 `A` 里定义的 `package`，那么 `A` 必须先于 `B` 编译。这种「谁必须先编译」的关系构成一张有向图，本库用「Level 0–6」把它压平成一张线性清单。
- **清单文件（manifest）**：描述「这个包叫什么、作者是谁、依赖什么、包含哪些源文件」的元数据文件。本库同时维护了三份内容近似、格式不同的清单，只为兼容不同的 EDA 工具链。

如果你对 SystemVerilog 的 `package` / `import` / `\`include`（宏包含）完全不熟悉，只要记住一句话即可：**`package` 是「先编译、后被引用」的类型仓库**，本讲的核心就是围绕这句话展开的。

## 3. 本讲源码地图

本讲涉及的关键文件清单如下：

| 文件 / 目录 | 作用 |
| --- | --- |
| `Bender.yml` | **Bender** 包清单，本库的「正本」。声明包名、作者、依赖，并按 Level 0–6 列出所有源文件。 |
| `src_files.yml` | 简单文件列表格式（带 `vlog_opts`/`incdirs`），供不支持 Bender 的工具使用。 |
| `axi.core` | **FuseSoC**（CAPI2）格式的清单，供 FuseSoC/EDAlize 等 EDA 流程使用。 |
| `ips_list.yml` | 发布清单，固定三个外部依赖的 commit 版本。 |
| `Bender.lock` | Bender 锁文件，记录依赖被解析后的确切 revision。 |
| `Makefile` | 顶层 make 入口，封装编译 / 仿真 / 综合脚本。 |
| `src/` | 可综合 RTL 模块（64 个 `.sv`）。 |
| `include/axi/` | 三类 `.svh` 宏文件：`typedef.svh` / `assign.svh` / `port.svh`。 |
| `test/` | 测试台（26 个 `.sv`），命名约定 `tb_<dut>.sv`。 |
| `doc/` | 每个主要模块的 Markdown 文档。 |
| `scripts/` | 构建 / CI / 发布辅助脚本（shell + python）。 |

## 4. 核心概念与源码讲解

### 4.1 仓库目录组织：每个目录的职责

#### 4.1.1 概念说明

一个硬件 IP 库的仓库通常要同时满足三类人的需求：**集成者**（拿模块去拼芯片）、**验证者**（写 testbench 跑仿真）、**贡献者**（改源码提 PR）。本库用清晰的目录划分来服务这三类人：可综合 RTL、验证组件、测试台、文档、脚本各居其位，互不混淆。

#### 4.1.2 核心流程

你可以把仓库想象成一条流水线：

```text
src/ (可综合模块)  ──┐
include/ (宏)      ──┼──►  被 Bender.yml / axi.core 收录  ──►  编译/仿真/综合
test/ (测试台)     ──┘
doc/ (说明)        ──►  供人阅读，不参与编译
scripts/ (脚本)    ──►  驱动编译/仿真/综合/发布
```

关键分工：

- `src/` 只放**可综合**的 RTL（64 个 `.sv`）。这些是你真正会放进芯片的模块。
- `include/axi/` 放**宏文件**（`.svh`），它们不是模块，而是用 `\`define` 展开的代码模板，用来减少重复样板。注意：它们被「包含」进别的文件，而不是被「编译成独立单元」。
- `test/` 放测试台（26 个 `.sv`），命名一律 `tb_<被测模块>.sv`，例如 `tb_axi_xbar.sv` 对应 `src/axi_xbar.sv`。
- `doc/` 放每个模块的设计说明（如 `doc/axi_xbar.md`），是给人看的，构建系统不编译它。
- `scripts/` 放 shell 与 python 脚本，例如 `compile_vsim.sh`（编译）、`run_vsim.sh`（仿真）、`synth.sh`（综合）、`axi_intercon_gen.py`（代码生成器）。

#### 4.1.3 源码精读

仓库根目录的文件清单（节选）：

[Makefile:22-40](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile#L22-L40) —— `TBS` 变量列出了所有可仿真的 testbench 名（去掉 `tb_` 前缀和 `.sv` 后缀）。这正好印证了 `test/` 的命名约定：每个名字都对应一个 `test/tb_<名字>.sv` 文件。

[Bender.yml:1-26](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml#L1-L26) —— `package` 段声明了包名 `axi`、作者列表和三个外部依赖（`common_cells`、`common_verification`、`tech_cells_generic`）。这一段是包的「身份证」。

#### 4.1.4 代码实践

1. **实践目标**：用只读 git 命令验证目录职责的划分。
2. **操作步骤**：
   - 运行 `git ls-files src/ | head`，确认 `src/` 下全是 `.sv`。
   - 运行 `git ls-files include/`，确认 `include/axi/` 下全是 `.svh`。
   - 运行 `git ls-files test/ | head`，确认 `test/` 下以 `tb_` 开头为主。
3. **需要观察的现象**：三个目录的文件扩展名/前缀各成一类，互不交叉。
4. **预期结果**：`src/` 全 `.sv`、`include/axi/` 全 `.svh`、`test/` 以 `tb_*.sv` 为主（另有少量 `*_pkg.sv` 和 `axi_synth_bench.sv`）。
5. 若本地无 git 仓库，可用 `ls src/ include/axi/ test/` 替代。

#### 4.1.5 小练习与答案

**练习 1**：`doc/axi_xbar.md` 会被编译进仿真吗？
**答案**：不会。`doc/` 只出现在文档生成流程里，`Bender.yml` 的 `sources` 段不收录它，因此不参与 SystemVerilog 编译。

**练习 2**：`include/axi/typedef.svh` 是模块还是宏文件？如何判断？
**答案**：是宏文件。判断依据是扩展名 `.svh`（header），且它在 `axi.core` 里被标记为 `is_include_file : true`（见 4.3 节），说明它靠 `\`include` 被嵌入别的编译单元，而不是独立编译。

---

### 4.2 依赖层级（Level 0–6）：编译顺序的拓扑分层

#### 4.2.1 概念说明

这是本讲最核心的概念。SystemVerilog 有一个硬规则：**被引用的编译单元必须先编译**。例如 `axi_intf.sv` 里会写 `import axi_pkg::*;`，那么 `axi_pkg.sv` 必须先于 `axi_intf.sv` 编译，否则编译器找不到这个 package。

如果文件不多，人工排个序就行。但本库有 64 个 RTL 文件，互相依赖关系是一张复杂的有向图。本库的做法是：把这张图**分层**——没有任何本库内部依赖的文件放 Level 0；只依赖 Level 0 的放 Level 1；以此类推。这样只要「按 Level 从小到大、同 Level 内按字母序」编译，就一定不会出现「先引用后定义」的错误。

#### 4.2.2 核心流程

每个文件的层级可以这样定义（即拓扑分层 / Kahn 分层）：

\[
\text{level}(f) =
\begin{cases}
0, & f \text{ 不依赖本包内任何文件} \\
1 + \max\{\text{level}(g) : g \text{ 是 } f \text{ 在本包内的依赖}\}, & \text{否则}
\end{cases}
\]

直观含义：**一个文件的层级 = 它依赖链上最长那条路再加 1**。编译时按 level 升序处理即可。本库的层级跨度是 0 到 6，所以共 7 层。

层级与「模块复杂度」高度相关：底层基础类型在 Level 0，叶子功能模块在 Level 2–3，组合体（如 `axi_xbar`）在 Level 5，最顶层的交叉点 `axi_xp` 在 Level 6。这种排布也正好是一条由浅入深的阅读路线。

#### 4.2.3 源码精读

[Bender.yml:31-39](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml#L31-L39) —— `sources` 段开头的注释把分层规则讲得很清楚：Level 0 不依赖本包内任何文件，Level 1 只依赖 Level 0，依此类推；同一层内按字母序。紧接着 `# Level 0` 下面只有一个文件：

```yaml
# Level 0
- src/axi_pkg.sv
# Level 1
- src/axi_demux_id_counters.sv
- src/axi_intf.sv
```

[Bender.yml:98-101](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml#L98-L101) —— 顶层两个文件，各占一层：

```yaml
# Level 5
- src/axi_xbar.sv
# Level 6
- src/axi_xp.sv
```

`axi_xbar.sv`（Level 5）之所以比绝大多数模块都高，是因为它实例化了 Level 4 的 `axi_xbar_unmuxed` 和 Level 2 的 `axi_mux`；而 `axi_xbar_unmuxed` 又实例化了 Level 3 的 `axi_demux`、`axi_err_slv`。可以验证这条链：

- [src/axi_xbar.sv:97](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L97) —— 实例化 `axi_xbar_unmuxed`（Level 4）。
- [src/axi_xbar.sv:123](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L123) —— 实例化 `axi_mux`（Level 2）。
- [src/axi_xbar_unmuxed.sv:164](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L164) —— 实例化 `axi_demux`（Level 3）。
- [src/axi_xbar_unmuxed.sv:195](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L195) —— 实例化 `axi_err_slv`（Level 3）。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证「层级 = 最长依赖链 + 1」。
2. **操作步骤**：
   - 在 `Bender.yml` 里查出 `axi_mux`（Level 2）、`axi_demux`（Level 3）、`axi_xbar_unmuxed`（Level 4）、`axi_xbar`（Level 5）。
   - 用 `grep -n "import axi_pkg" src/axi_intf.sv` 观察每个模块都引用了 `axi_pkg`。
3. **需要观察的现象**：每往上一层，实例化的子模块层级就更高一级，形成严格的「父比子高」关系。
4. **预期结果**：`axi_xbar`(5) > `axi_xbar_unmuxed`(4) > `axi_demux`(3) > `axi_demux_id_counters`(1) > `axi_pkg`(0)，链上每一步层级都严格递减。
5. 待本地验证：你也可以用 `scripts/` 下的脚本实际编译一次，观察 Bender 输出的文件顺序是否真的按 Level 升序。

#### 4.2.5 小练习与答案

**练习 1**：为什么不能把 `axi_xbar.sv` 放到 Level 2？
**答案**：因为它实例化了 Level 4 的 `axi_xbar_unmuxed`。若放在 Level 2，编译到它时被引用的子模块还没编译，会报「module not found」。层级必须严格高于它所有依赖的最高层级。

**练习 2**：同一层内的文件为什么按字母序排列？
**答案**：因为同层文件之间没有互相依赖（否则其中一个就该升到更高层），顺序对正确性无影响；按字母序只是为了让 diff 干净、便于审阅和维护。

---

### 4.3 三套清单文件：Bender.yml / axi.core / src_files.yml / ips_list.yml

#### 4.3.1 概念说明

你会注意到仓库里**同时**存在 `Bender.yml`、`axi.core`、`src_files.yml`` 三个长得差不多的清单文件，内容都包含同一份「Level 0–6 源文件列表」。初学者常困惑：为什么不只留一个？

原因是**工具链兼容**。不同团队/公司用不同的 EDA 流程：

- **Bender**（pulp-platform 自研）读 `Bender.yml`；
- **FuseSoC / EDAlize**（开源 EDA 流程管理）读 `axi.core`（CAPI2 格式）；
- 一些内部脚本/老工具只认最朴素的 `src_files.yml` 列表。

为了让无论你用哪套工具都能直接编译本库，维护者把同一份层级结构用三种格式各写了一遍。`ips_list.yml` 则是另一种用途：它是**发布清单**，固定三个外部依赖的 commit，用于发布 IP 包。

#### 4.3.2 核心流程

四份文件的职责对照：

```text
Bender.yml    ──► Bender 的正本（包名/作者/依赖/源文件层级/targets）
axi.core      ──► FuseSoC CAPI2 格式（filesets/generators/targets）
src_files.yml ──► 朴素文件列表（带 vlog_opts/incdirs，分 axi 与 axi_sim 两组）
ips_list.yml  ──► 发布清单（钉死 common_cells/common_verification/tech_cells_generic 的 commit）
```

三份源文件清单的内容**近似但不完全相同**——它们各自维护，所以偶尔会有细微漂移（见练习）。一个值得注意的共同点：验证专用的几个文件（`axi_test.sv`、`axi_sim_mem.sv`、`axi_dumper.sv`、`axi_chan_compare.sv`）**不参与综合**，它们在三份清单里都被单独标注，例如 `src_files.yml` 用 `skip_synthesis` 标志把它们隔离开。

#### 4.3.3 源码精读

[Bender.yml:103-112](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml#L103-L112) —— Bender 的 `simulation` target 把四个验证专用文件加进来，它们只用于仿真，综合时不会被纳入：

```yaml
- target: simulation
  files:
    - src/axi_chan_compare.sv
    - src/axi_dumper.sv
    - src/axi_sim_mem.sv
    - src/axi_test.sv
```

[src_files.yml:79-87](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src_files.yml#L79-L87) —— 同样这四个文件被归入 `axi_sim` 组，并打上 `skip_synthesis`（综合时跳过）和 `only_local` 标志：

```yaml
axi_sim:
  files:
    - src/axi_chan_compare.sv
    - src/axi_dumper.sv
    - src/axi_sim_mem.sv
    - src/axi_test.sv
  flags:
    - skip_synthesis
    - only_local
```

[axi.core:1-6](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/axi.core#L1-L6) —— FuseSoC 格式以 `CAPI=2:` 起头，声明全名 `pulp-platform.org::axi:0.39.10`、许可证 `SHL-0.51`，并依赖 `common_cells`：

```yaml
CAPI=2:
name: pulp-platform.org::axi:0.39.10
license: SHL-0.51
```

[axi.core:120-124](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/axi.core#L120-L124) —— `axi.core` 还多了一个 Bender 没有的段落：`generators`，声明用 python 脚本 `scripts/axi_intercon_gen.py` 自动生成 xbar 的 wrapper：

```yaml
generators:
  axi_intercon_gen:
    interpreter: python3
    command: scripts/axi_intercon_gen.py
```

[ips_list.yml:1-11](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/ips_list.yml#L1-L11) —— 发布清单钉死三个依赖的 commit（如 `common_cells: v1.39.0`），保证发布出去的 IP 包能被精确复现。

#### 4.3.4 代码实践

1. **实践目标**：体会「三份清单同源不同格式」。
2. **操作步骤**：
   - 在 `Bender.yml`、`axi.core`、`src_files.yml` 里分别定位 Level 5 的 `axi_xbar.sv`，确认三处都在同一相对位置（Level 5）。
   - 对比三份清单对验证专用文件的处理方式（Bender 用 `simulation` target，`src_files.yml` 用 `skip_synthesis` 标志）。
3. **需要观察的现象**：同一份层级列表被三种语法各表达一次。
4. **预期结果**：三份清单的 Level 划分一致；验证文件的「不参与综合」属性在每份清单里都有对应表达。
5. 待本地验证：注意 `src/axi_demux_id_counters.sv` 在 `Bender.yml` 的 Level 1 中存在，但在 `axi.core` 和 `src_files.yml` 里没有单列——这是一个真实存在的维护漂移，提醒我们三份清单需手工保持同步。

#### 4.3.5 小练习与答案

**练习 1**：为什么验证专用文件（`axi_test.sv` 等）要和可综合 RTL 分开管理？
**答案**：因为它们包含 `class`、随机化、`scoreboard` 等不可综合结构，综合工具会报错。分开后，综合流程只拿 RTL 部分，仿真流程才把它们加进来。

**练习 2**：`Bender.lock` 和 `Bender.yml` 的依赖声明有什么区别？
**答案**：`Bender.yml` 写的是**版本范围**（如 `version: 1.39.0`），`Bender.lock` 记录的是解析后**确切的 git revision**（如 `common_cells: 9ca8a765...`）。前者声明意图，后者锁定结果以保证可复现。

---

### 4.4 include 宏文件的导出机制

#### 4.4.1 概念说明

`include/axi/` 下有三个 `.svh` 宏文件：`typedef.svh`（声明通道 / 请求 / 响应 struct）、`assign.svh`（在接口与 struct 之间互连）、`port.svh`（生成扁平 AXI 端口）。它们不是独立编译单元，而是被 `\`include` 嵌进别的文件后展开。

关键问题是：**下游项目**怎么拿到这些宏？答案是 Bender 的 `export_include_dirs` 机制——本库声明把 `include/` 目录「导出」，任何依赖 `axi` 的包都能直接 `\`include "axi/typedef.svh"`，路径由 Bender 自动接好。

#### 4.4.2 核心流程

```text
Bender.yml: export_include_dirs: [include]
        │
        ▼
本库内部  ──┐
下游包    ──┴──►  都可用  `include "axi/typedef.svh"  （Bender 自动加 -I include）
```

`src_files.yml` 里也能看到对应的包含路径声明：`incdirs` 指向 `include` 和 `../../common_cells/include`，告诉编译器去哪里找 `.svh`。

#### 4.4.3 源码精读

[Bender.yml:28-29](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml#L28-L29) —— 导出 include 目录，这是下游能用本库宏的关键：

```yaml
export_include_dirs:
  - include
```

[src_files.yml:5-7](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src_files.yml#L5-L7) —— 朴素清单里的包含路径声明：

```yaml
incdirs:
  - include
  - ../../common_cells/include
```

[axi.core:10-11](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/axi.core#L10-L11) —— FuseSoC 格式里把两个 `.svh` 标记为 `is_include_file : true`，并指定 `include_path : include`，语义与上面一致。

#### 4.4.4 代码实践

1. **实践目标**：理解宏文件如何被定位。
2. **操作步骤**：
   - 在 `src/` 下用 `grep -rn '`include "axi/typedef.svh"' src/ | head`，观察哪些模块包含了它。
   - 对照 `Bender.yml` 的 `export_include_dirs`，思考为什么这些 `\`include` 不需要写完整路径。
3. **需要观察的现象**：引用方只写 `axi/typedef.svh` 这个相对路径，不写绝对路径。
4. **预期结果**：路径前缀 `axi/` 正好对应 `include/axi/` 目录，由导出机制补齐。
5. 待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：如果删掉 `Bender.yml` 里的 `export_include_dirs`，下游包还能 `\`include "axi/typedef.svh"` 吗？
**答案**：默认不能。删掉后 Bender 不再把 `include/` 加入下游的搜索路径，下游需要自己手动指定 include 路径才能找到。

**练习 2**：为什么 `.svh` 在 `axi.core` 里要标 `is_include_file : true`？
**答案**：告诉 FuseSoC「这是被包含的文件，不是独立的编译单元」，从而在生成文件列表 / 综合时正确处理它的依赖与路径，避免把它当成顶层模块单独编译。

---

## 5. 综合实践

把本讲的层级与依赖知识串起来，完成下面这个任务（本讲的核心实践）：

> **任务**：用 `Bender.yml` 的层级说明 `axi_xbar`（Level 5）依赖哪些更低层级的文件，并解释为什么 `axi_pkg` 必须是 Level 0。

操作步骤：

1. 打开 [Bender.yml:31-101](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml#L31-L101)，按层级摘录 `axi_xbar` 的依赖链。
2. 用 `grep` 验证 `axi_xbar.sv` 实例化了哪些子模块（提示：见 4.2.3 节给出的行号 `src/axi_xbar.sv:97` 与 `:123`）。
3. 继续往下追：`axi_xbar_unmuxed.sv` 又实例化了谁（`src/axi_xbar_unmuxed.sv:164` 实例化 `axi_demux`，`:195` 实例化 `axi_err_slv`）。
4. 用 `grep -n "import axi_pkg" src/*.sv | head` 观察几乎所有模块都 `import axi_pkg`。

参考答案要点：

- **`axi_xbar`（Level 5）依赖的更低层级文件**：直接依赖 Level 4 的 `axi_xbar_unmuxed`、Level 2 的 `axi_mux`；间接（经 `axi_xbar_unmuxed`）依赖 Level 3 的 `axi_demux`、`axi_err_slv`；再往下 `axi_demux` 依赖 Level 1 的 `axi_demux_id_counters`；所有模块共同依赖 Level 0 的 `axi_pkg` 和 Level 1 的 `axi_intf`。所以 `axi_xbar` 横跨 Level 0–4 的全部基础层。
- **为什么 `axi_pkg` 必须是 Level 0**：`axi_pkg.sv` 是一个 SystemVerilog `package`，集中定义了全库共享的类型与常量（如 `burst_t`、`resp_t`、`atop_t` 以及 `BURST_*`、`RESP_*` 等）。本库里几乎所有模块都会 `import axi_pkg::*` 或引用 `axi_pkg::` 下的名字。`package` 必须先于任何引用它的文件编译；而 `axi_pkg` 自身不依赖本包内任何其它文件。两条性质合起来，决定了它只能、也必须位于 Level 0——它是整张依赖图的根。

完成本任务后，你应当能对仓库里任意一个 `.sv` 文件，说出它「大概在哪一层、依赖谁、被谁依赖」。

## 6. 本讲小结

- 仓库按职责分目录：`src/`（可综合 RTL）、`include/axi/`（`.svh` 宏）、`test/`（`tb_*.sv` 测试台）、`doc/`（文档）、`scripts/`（构建/CI 脚本）。
- **Level 0–6 是编译顺序的拓扑分层**：被引用者先编译；一个文件的层级 = 它在本包内最长依赖链 + 1，按层级升序编译就不会出错。
- `axi_pkg.sv` 是 Level 0，因为它是全库共享的 `package`，被几乎所有模块 `import`，且自身无内部依赖。
- 三套清单 `Bender.yml` / `axi.core` / `src_files.yml` 表达同一份层级列表，只是格式不同，服务于 Bender、FuseSoC、朴素脚本三类工具链；`ips_list.yml` 则是发布用的依赖 commit 钉版清单。
- 验证专用文件（`axi_test`/`axi_sim_mem`/`axi_dumper`/`axi_chan_compare`）不可综合，在三份清单里都被单独隔离（target / `skip_synthesis` 标志）。
- `include/` 通过 `export_include_dirs` 导出，让本库与下游都能用 `\`include "axi/typedef.svh"` 等宏文件。

## 7. 下一步学习建议

- 下一讲《AXI4 协议快速回顾》(u1-l3) 会用 `axi_pkg.sv` 里的 `BURST_*`、`RESP_*` 等 localparam 作为切入点回顾协议，正好承接本讲对「`axi_pkg` 是类型根基」的理解。
- 想看「怎么真正编译/仿真/综合」，继续读《如何编译、仿真与综合》(u1-l4)，它会拆解 `Makefile` 与 `scripts/` 下的脚本——本讲只介绍了这些脚本的存在，下一单元会带你跑通它们。
- 建议直接打开 [src/axi_pkg.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv) 浏览一遍，对照本讲的「Level 0 是类型根基」建立直观印象，为 u2 单元（基础设施：类型、接口与宏）打好基础。
