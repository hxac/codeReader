# 第三方验证库集成

## 1. 本讲目标

PoC 自带一套轻量仿真辅助包（`src/sim/`，见 u4-l1），但真实项目的验证需求远不止「生成时钟、跑几个断言」——你需要覆盖率驱动的随机激励、结构化的总线 BFM、持续集成式的自动回归。这些能力 PoC 不重复造轮子，而是把业界成熟的四个开源验证库以 **git submodule** 的形式嵌进 `lib/`，再用 pyIPCMI 的 `.files` 清单把它们接进编译流水线。

学完本讲，你应当能够：

1. 准确区分 **cocotb / OSVVM / UVVM / VUnit** 四种验证方法学的定位、所用语言与典型用法。
2. 说清楚它们为什么以 git submodule 集成、`lib/` 下目录如何与 `.gitmodules` 对应、克隆时为什么要 `--recursive`。
3. 读懂 `lib/*.files` 清单，理解它如何用 `path` / `include` / `if` / `library` / `vhdl` 语句、配合 `Tool` / `VHDLVersion` / `ToolChain` 等条件变量，把第三方库「编译前选文件」地接入 PoC 工程。
4. 分辨「验证方法学库」与「厂商原语仿真库」这两类 `.files` 的不同职责。

> 本讲是专家层（第 5 单元）第二篇。它承接 u1-l2（`lib/` 是 git submodule 目录）、u4-l1（PoC 自带 sim 包的写法）与 u5-l1（pyIPCMI 委托链与 `.files` 语义），把视角从「PoC 自己的 VHDL」扩展到「PoC 如何与外部验证生态对接」。

## 2. 前置知识

阅读本讲前，建议你已经具备以下概念（不熟悉的部分会随讲补充）：

- **IP 核 / 测试台 / DUT**：硬件设计中，被测模块叫 DUT（Design Under Test），驱动并检查它的代码叫测试台（testbench）。u1-l4 讲过 PoC 的 `_tb` 后缀约定。
- **BFM（Bus Functional Model，总线功能模型）**：把一段总线协议（如 UART、AXI-Lite）封装成「给地址给数据，自动按时序驱动管脚」的过程，让测试台不必手写每一拍的时序。
- **覆盖率驱动随机验证**：不写死激励，而是随机生成满足约束的输入，并用功能覆盖率统计「哪些场景被测过了」，直到覆盖率收敛。这是现代 ASIC/FPGA 验证的主流范式。
- **git submodule**：在一个 git 仓库里嵌入另一个 git 仓库，外层只记录子仓库的某个 commit，不复制其历史。u1-l2 / u5-l1 已指出 PoC 用它嵌入第三方库，必须 `git clone --recursive`。
- **pyIPCMI 与 `.files` 清单**：`.files` 不是 VHDL，而是 pyIPCMI 在**编译前**读取的工具中立清单，用 `vhdl` / `include` / `if` / `report` / `path` 语句描述「编译哪些文件、按什么顺序、在什么条件下」。u5-l1 讲过它的整体语义，本讲聚焦它在 `lib/` 下的具体用法。
- **条件变量**：pyIPCMI 把工具链差异抽象成一组正交变量——`ToolChain`（如 `Xilinx_Vivado`）、`Tool`（具体仿真器，如 `GHDL`、`Mentor_vSim`）、`VHDLVersion`（`1993` / `2002` / `2008`）、`DeviceVendor`、`BoardName`、`Environment`。`.files` 里的 `if` 就是对它们求值。

## 3. 本讲源码地图

本讲涉及的关键文件都在仓库顶层 `lib/` 与根目录：

| 文件 | 作用 |
| --- | --- |
| [lib/README.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md) | 列出 `lib/` 下所有第三方库的目录、版权、许可证与上游链接，并说明 submodule 初始化步骤。 |
| [.gitmodules](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.gitmodules) | 声明每个 submodule 的 `path` 与 `url`，是「子模块集成机制」的权威来源。 |
| [lib/OSVVM.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/OSVVM.files) | OSVVM 验证库的编译清单：优先用预编译库，否则逐个源文件自编译。 |
| [lib/UVVM.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/UVVM.files) | UVVM 验证库的编译清单：按子库（util / vvc_framework / 各 VIP）分别挂预编译库。 |
| [lib/Xilinx.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/Xilinx.files) | Xilinx 原语仿真库（unisim/unimacro/secureip/simprim）的挂接清单。 |
| [lib/Altera.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/Altera.files) | Altera 原语仿真库（lpm/sgate/altera/altera_mf/altera_lnsim）的挂接清单。 |

辅助阅读：[lib/Xilinx-Vivado.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/Xilinx-Vivado.files)（Vivado 自带 unisim 的简化挂接）、各核 `.files`（如 `src/misc/sync/sync_Bits.files`）展示 `include "lib/XXX.files"` 的调用点。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 验证方法学对比**、**4.2 git submodule 集成机制**、**4.3 `.files` 清单如何把第三方库接入编译**。

### 4.1 验证方法学对比：cocotb / OSVVM / UVVM / VUnit

#### 4.1.1 概念说明

PoC 在 `lib/` 下集成的这四个库，覆盖了当今 VHDL/Verilog 验证的四种主流路线。理解它们的关键是「**用什么语言写测试、解决什么问题**」：

- **cocotb**（Coroutine Cosimulation Testbench）：用 **Python** 写测试，通过 VPI/VHPI 与仿真器协同（cosimulation）。你不必学 VHDL 测试台语法，直接用 Python 的协程驱动激励、用 `assert` 检查结果。适合软件工程师、适合做大量参数化回归。
- **OSVVM**（Open Source VHDL Verification Methodology）：纯 **VHDL-2008** 包集合，提供「智能覆盖率」（coverage-driven randomization）、随机化、记分板（scoreboard）、告警日志（AlertLog）。它的卖点是「不换语言、不推翻现有测试台」地渐进增强验证能力。
- **UVVM**（Universal VHDL Verification Methodology）：纯 **VHDL**，主打 **VVC（VHDL Verification Component）框架**——为每种总线协议提供一个「验证组件」，测试台用 `uart_expect(UART_VVCT, my_data)` 这类高层命令驱动它，背后自动调用对应 BFM。适合搭建结构化、多接口并行的测试台。
- **VUnit**：一个 **Python 驱动的单元测试框架**（自动化跑测、结果汇总、持续集成），本身也带一批 VHDL 验证工具库（如 check、logger、run）。它「补充而非取代」传统测试方法，强调 test early and often。

一句话区分：cocotb 与 VUnit 是 **Python 在外层调度**（前者偏 cosimulation 驱动管脚，后者偏工程化自动回归）；OSVVM 与 UVVM 是 **纯 VHDL 在内层提供验证原语**（前者偏覆盖率/随机/记分板，后者偏结构化 VVC + BFM）。

#### 4.1.2 核心流程

挑选验证库的决策流程可以这样画：

```text
要在测试台里做什么？
│
├─ 想用 Python 写激励 / 复用 Python 生态 ──→ cocotb（cosimulation，逐拍驱动）
│
├─ 想要自动化跑全套测试、CI 汇总 ────────→ VUnit（Python 调度 + VHDL check 库）
│
├─ 想在 VHDL 里做覆盖率/随机/记分板 ─────→ OSVVM（纯 VHDL 包，渐进增强）
│
└─ 想要现成的总线 BFM、结构化多接口台 ──→ UVVM（VVC 框架 + 各协议 VIP）
```

这四者并非互斥——例如 VUnit 可以调度一个用 OSVVM 记分板的测试台，cocotb 也能和 UVVM 的 BFM 共存。PoC 的做法是把它们都挂上，按测试台需要 `include` 进来。

#### 4.1.3 源码精读

`lib/README.md` 为每个库固定给出五项信息：Folder、Copyright、License、一句定位、Website/Source。先看 cocotb 的条目：

[lib/README.md:24-36](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md#L24-L36) 定义 cocotb 的目录、版权与「用 Python 协程写 VHDL/Verilog 测试台」的定位。

OSVVM 的条目强调它「无需学新语言、无需推翻现有测试台」：

[lib/README.md:39-57](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md#L39-L57) 给出 OSVVM 的目录（`lib\osvvm\`）、Artistic License 2.0、上游 `JimLewis/OSVVM`。

UVVM 的条目着重解释 VVC 框架的用途，并举了 `uart_expect(...)` / `axilite_write(...)` 这类高层命令的例子：

[lib/README.md:60-86](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md#L60-L86) 说明 UVVM 由 Utility Library、VVC Framework 与各协议 VIP 组成，源在 `UVVM/UVVM_All`。

VUnit 的条目点明它是「补充而非取代」的自动化测试框架：

[lib/README.md:89-105](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md#L89-L105) 给出 VUnit 的 MPL-2.0 许可证与上游 `VUnit/vunit`。

把四者整理成对比表（信息均来自上文 README 行）：

| 库 | 语言 | 许可证 | 目录 | 核心定位 |
| --- | --- | --- | --- | --- |
| cocotb | Python（cosim） | Revised BSD | `lib/cocotb/` | 用 Python 协程写测试台 |
| OSVVM | VHDL-2008 | Artistic 2.0 | `lib/osvvm/` | 覆盖率/随机/记分板，渐进增强 |
| UVVM | VHDL | MIT | `lib/uvvm/` | VVC 框架 + 总线 BFM/VIP |
| VUnit | Python + VHDL | MPL-2.0 | `lib/vunit/` | 单元测试自动化、CI 回归 |

#### 4.1.4 代码实践

**实践目标**：用本节的对比表，亲手把 OSVVM 与 UVVM 的定位区分清楚，并为后续 4.2 的 `.gitmodules` 追踪做准备。

**操作步骤**：

1. 打开 [lib/README.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md)，分别定位 OSVVM（39–57 行）与 UVVM（60–86 行）两段。
2. 各摘出一句「它解决什么问题」的原话。
3. 用一句话回答：如果你的测试台需要一个 AXI-Lite 总线 BFM，并且想用 `axilite_write(AXILITE_VVCT, addr, data, msg)` 这种命令式驱动，你该选 OSVVM 还是 UVVM？为什么？

**需要观察的现象**：UVVM 段落里明确出现了 `VVC Framework`、`Verification IPs (VIP)`、`axilite_write(...)` 等关键词；OSVVM 段落里则出现 `Intelligent Coverage`、`coverage driven randomization`、`directed, algorithmic, file based, and constrained random`。

**预期结果**：需要总线 BFM 与结构化多接口台 → **UVVM**；需要覆盖率驱动随机 + 记分板 → **OSVVM**。两者语言都是纯 VHDL，但抽象层次与切入点不同。

#### 4.1.5 小练习与答案

**练习 1**：cocotb 和 VUnit 都用 Python，它们的分工有什么不同？
**答案**：cocotb 是 **cosimulation** 库——Python 协程经 VPI/VHPI 直接驱动仿真器的管脚时序，重在「写激励」；VUnit 是 **测试工程化框架**——用 Python 调度编译、批量跑测、汇总结果、对接 CI，重在「自动化回归」。前者偏「怎么激励 DUT」，后者偏「怎么把成百上千个测试组织起来跑」。

**练习 2**：为什么 PoC 把 OSVVM/UVVM 做成 `.files` 清单，而 cocotb/VUnit 在 `lib/` 下没有对应的 VHDL `.files`？
**答案**：OSVVM 与 UVVM 是**纯 VHDL 包**，必须被编译进某个 VHDL 库才能 `use`，所以需要 `.files` 描述源文件顺序。cocotb 与 VUnit 的核心是 **Python**，不作为 VHDL 编译单元存在（cocotb 在运行期经 cosimulation 接入，VUnit 在 Python 层调度），因此没有 VHDL `.files`——cocotb 反而以 `Cocotb_QuestaSim` 这个 `Tool` 取值出现在厂商原语 `.files` 里（见 4.3）。

---

### 4.2 git submodule 集成机制

#### 4.2.1 概念说明

四个验证库（外加 pyIPCMI 基础设施）都不是 PoC 自己的代码，PoC 只想「在某个确定版本上引用它们」。git submodule 正是为此而生：外层仓库（PoC）在每个子模块路径下嵌一个独立 git 仓库，外层**只保存子模块的一个 commit 哈希**，不复制其完整历史。这样 PoC 仓库本身保持轻量，又能锁定第三方库的确切版本。

这种集成带来三个工程后果（u1-l2、u5-l1 已点到，这里展开）：

1. **克隆必须 `--recursive`**：普通 `git clone` 不会自动拉子模块，`lib/cocotb/` 等目录会是空的（你可以在本仓库里验证：这些目录确实为空壳，需要单独 init）。
2. **更新要两步**：`git submodule init`（在 `.git/config` 注册）+ `git submodule update`（按记录的 commit 检出）。
3. **URL 是相对的**：`.gitmodules` 里写的是 `../OSVVM.git` 这种相对路径，会相对于 PoC 自己的 origin 来解析（见 4.2.3），这是初学者最容易困惑的一点。

#### 4.2.2 核心流程

第三方库从「声明」到「可用」的流程：

```text
.gitmodules 声明 path + url（相对 URL）
        │
        ▼
git clone --recursive   ← 一步到位（或手工 init + update）
        │
        ▼
lib/<name>/ 检出到记录的 commit
        │
        ▼
pyIPCMI 读取 lib/<name>.files（编译期）── 把源文件接进 VHDL 库
        │
        ▼
测试台 .files 里 include "lib/<name>.files"  → 真正用上
```

关键认知：**submodule 负责「把代码弄到本地」，`.files` 负责「把代码接进编译」**，两者缺一不可——只 clone 不 include，代码躺在 `lib/` 里不会被编译；只 include 不 clone，`.files` 里的源文件路径根本不存在。

#### 4.2.3 源码精读

[.gitmodules](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.gitmodules) 是子模块集成的唯一权威。看四个验证库的条目：

[.gitmodules:1-9](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.gitmodules#L1-L9) 声明 VUnit、OSVVM、cocotb 三个子模块——每个都是 `path = lib/<name>` + `url = ../<name>.git` 的两行结构。

UVVM 的条目：

[.gitmodules:13-15](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.gitmodules#L13-L15) 声明 `lib/uvvm`，`url = ../UVVM.git`。

> **相对 URL 怎么解析？** PoC 的 origin 是 `https://github.com/VLSI-EDA/PoC.git`，那么 `../OSVVM.git` 会相对解析为 `https://github.com/VLSI-EDA/OSVVM.git`，`../UVVM.git` 解析为 `https://github.com/VLSI-EDA/UVVM.git`。也就是说，**在本仓库的 `.gitmodules` 里，OSVVM 与 UVVM 实际指向的是 `VLSI-EDA` 组织下的同名镜像**（不是上游官方仓库）。而 [README.tpl:99-102](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.tpl#L99-L102) 与 [lib/README.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md) 记录的「Source」则是**上游官方源**：OSVVM 上游是 `JimLewis/OSVVM`，UVVM 上游是 `UVVM/UVVM_All`。两者不要混淆——`.gitmodules` 决定你 clone 到的代码来源，README 记录的是该项目的正统出处。这正是本讲代码实践要你追踪的点。

lib/README.md 还给出了手工初始化的脚本（PowerShell 版），展示 `init` + `update` + 把 `origin` 改名为 `github` 的惯例：

[lib/README.md:9-22](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md#L9-L22) 给出非 `--recursive` 克隆后的补救步骤：进 `lib/`、`git submodule init`、`git submodule update`，再把每个子模块的 `origin` 远端改名为 `github`。

#### 4.2.4 代码实践

**实践目标**：追踪 OSVVM 与 UVVM 在 `.gitmodules` 中各自指向哪个上游仓库，并区分「`.gitmodules` 实际指向」与「README 记录的官方上游」。

**操作步骤**：

1. 读 [.gitmodules:4-6](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.gitmodules#L4-L6)（OSVVM）与 [.gitmodules:13-15](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.gitmodules#L13-L15)（UVVM），记下两者的 `url`。
2. 结合 PoC origin 为 `VLSI-EDA/PoC`，把相对 URL 解析成绝对 URL。
3. 对照 [lib/README.md:54](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md#L54)（OSVVM 的 Source）与 [lib/README.md:83](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md#L83)（UVVM 的 Source），记录官方上游。
4. （可选，待本地验证）在本地执行 `git -C lib/osvvm remote -v` 与 `git -C lib/uvvm remote -v`，确认 `update` 后真正拉取的远端地址。

**需要观察的现象**：`.gitmodules` 中两者都是 `../<Name>.git` 形式；解析后落在 `VLSI-EDA` 组织下；而 README 的 Source 行指向不同的官方 owner。

**预期结果**：

| 子模块 | `.gitmodules` url（相对） | 解析后绝对 URL（相对 origin） | README 记录的官方上游 |
| --- | --- | --- | --- |
| `lib/osvvm` | `../OSVVM.git` | `https://github.com/VLSI-EDA/OSVVM.git` | `https://github.com/JimLewis/OSVVM` |
| `lib/uvvm` | `../UVVM.git` | `https://github.com/VLSI-EDA/UVVM.git` | `https://github.com/UVVM/UVVM_All` |

> 注意 UVVM 的镜像名是 `UVVM`，而官方上游仓库名是 `UVVM_All`（因为 UVVM 官方把 utility/vvc_framework/各 VIP 拆在多个仓库，`UVVM_All` 是汇总仓库）。这是最容易踩的细节。

#### 4.2.5 小练习与答案

**练习 1**：同事只用 `git clone`（没加 `--recursive`）拉了 PoC，发现 `lib/osvvm/` 是空的，命令行也跑不起来。他该怎么办？
**答案**：执行 `cd lib && git submodule init && git submodule update`（见 [lib/README.md:13-16](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md#L13-L16)）。`init` 把 `.gitmodules` 里的子模块注册进 `.git/config`，`update` 按外层记录的 commit 检出代码。下次直接 `git clone --recursive` 可省去这步。

**练习 2**：为什么 PoC 用相对 URL（`../OSVVM.git`）而不是写死绝对地址？
**答案**：相对 URL 让仓库在 HTTPS 与 SSH、以及在不同 fork/镜像间都能正确解析——只要子模块与外层仓库在同一个 GitHub 组织下同名存在即可。PoC 在 `VLSI-EDA` 组织下为每个第三方库维护了同名镜像，相对 URL 自动指向这些镜像，clone 时不必关心具体传输协议。

---

### 4.3 `.files` 清单如何把第三方库接入编译

#### 4.3.1 概念说明

submodule 只把代码搬到 `lib/<name>/`，但 pyIPCMI 还不知道「这些代码要编译成什么 VHDL 库、按什么顺序、在什么条件下」。回答这些问题正是 `lib/*.files` 的职责。它延续 u5-l1 讲过的 `.files` 语义（`vhdl` / `include` / `if` / `report` / `path` 五种语句），并在 `lib/` 下形成两类用途：

1. **验证方法学库**（OSVVM.files、UVVM.files）：把纯 VHDL 验证包编译成对应的 VHDL 库（`osvvm`、`uvvm_util`、`bitvis_vip_*` 等），供测试台 `use`。
2. **厂商原语仿真库**（Xilinx.files、Altera.files）：不是验证方法学，而是厂商底层原语（Xilinx 的 `unisim`/`unimacro`/`secureip`/`simprim`、Altera 的 `lpm`/`sgate`/`altera`/`altera_mf`/`altera_lnsim`）的仿真模型——当你实例化 `ODDR`、`altsyncram` 这类原语（见 u3-l2、u3-l3）时，仿真器需要这些库才能运行。

两类清单共享同一套设计哲学：**优先复用预编译库，找不到才退回源码自编译**。这是因为这些库体积大、编译慢，主流仿真器都提供预编译好的版本，pyIPCMI 只需把它「挂」到正确的库名上。

#### 4.3.2 核心流程

`lib/*.files` 的通用判定骨架（以 OSVVM/UVVM 为代表）：

```text
if (VHDLVersion 太低) then
    report "不支持"                      ← 版本门槛（OSVVM 要 2008，UVVM 要 ≥2002）
elseif (版本达标) then
    if (Tool = "GHDL") then
        用 ?{路径探测} 找预编译库 ── 找到 → library <name> <预编译路径>
                                     └─ 没找到 → report
    elseif (Tool = "Mentor_vSim") then
        同上，换 QuestaSim/ModelSim 预编译路径
    else
        vhdl <lib> "lib/<name>/<File>.vhd"   ← 兜底：逐个源文件自编译
    end if
end if
```

三个要点：

- **`path` 变量 + `?{...}` 探测**：`path PreCompiled = ${CONFIG.DirectoryNames:PrecompiledFiles}` 用 pyIPCMI 配置项定位预编译根目录；`?{路径}` 是「该路径是否存在」的布尔探测，存在才挂库。
- **`library <名> <路径>`**：把一个已预编译好的 VHDL 库登记到某路径（这一步不编译，只声明）。
- **`vhdl <库名> "文件"`**：真正编译某个源文件进指定库（兜底分支）。

而调用方（各核的 `.files`）则用 `include "lib/XXX.files"` 把这些清单串进来，并用 `DeviceVendor` 守卫决定是否引入厂商原语库（与 u3-l2 的双层选择一脉相承）。

#### 4.3.3 源码精读

先看 **OSVVM.files** 的版本门槛与兜底自编译清单：

[lib/OSVVM.files:10-11](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/OSVVM.files#L10-L11) 用 `if (VHDLVersion < 2008)` 设门槛——OSVVM 必须用 VHDL-2008，否则只 `report` 提示。

[lib/OSVVM.files:13-26](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/OSVVM.files#L13-L26) 展示 GHDL 与 Mentor_vSim 两个分支：先 `path` 拼出预编译目录，再用 `?{...}` 探测 `.cf` 文件是否存在，存在则 `library osvvm <路径>`，否则 `report`。

[lib/OSVVM.files:29-46](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/OSVVM.files#L29-L46) 是兜底分支：当不是 GHDL 也不是 vSim 时，逐个 `vhdl osvvm "lib/osvvm/<Pkg>.vhd"` 自编译 OSVVM 的全部包（`AlertLogPkg`、`RandomPkg`、`CoveragePkg`、`ScoreboardGenericPkg` 等），最后编译 `OsvvmContext.vhd`。

再看 **UVVM.files**，它的特点是「按子库分别挂」——UVVM 不是单一库，而是拆成 `uvvm_util`、`uvvm_vvc_framework` 与各协议 VIP（`bitvis_vip_axilite` 等）多个库：

[lib/UVVM.files:16-38](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/UVVM.files#L16-L38) 在 GHDL 分支里对每个子库分别用 `?{...}` 探测其 `.cf` 并 `library <名>` 挂接——`uvvm_util`、`uvvm_vvc_framework`、`bitvis_vip_axilite/axistream/i2c/sbi/uart`。

[lib/UVVM.files:52-55](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/UVVM.files#L52-L55) 的兜底分支几乎为空（注释着 `# TODO self-compile section?`），说明 UVVM 目前只支持预编译挂接，尚未提供源码自编译清单。

接着看 **厂商原语库**。`Xilinx.files` 按 `Tool` 分三大类：GHDL（还要按 VHDL 版本再分 v93/v08）、Mentor 系（含 cocotb 走 QuestaSim）、以及厂商自家工具链（预装无需挂接）：

[lib/Xilinx.files:9-48](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/Xilinx.files#L9-L48) 是 GHDL 分支：用 `VHDLVersion` 在 `v93` 与 `v08` 两套预编译 `.cf` 间选择，分别挂 `unisim`/`unimacro`/`secureip`/`simprim` 四个库。

[lib/Xilinx.files:49-65](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/Xilinx.files#L49-L65) 揭示一个重要细节：`Tool in ["Mentor_vSim", "Cocotb_QuestaSim"]` —— **cocotb 在这里以一个 `Tool` 取值出现**，它复用 Mentor vSim 的 Xilinx 预编译库；而 `ToolChain in ["Xilinx_ISE", "Xilinx_Vivado"]` 分支则是 `# implicitly referenced; nothing to reference`，因为厂商自家工具链自带这些原语库，无需显式声明。

`Altera.files` 结构与 Xilinx 对称，挂的是 `lpm`/`sgate`/`altera`/`altera_mf`/`altera_lnsim`：

[lib/Altera.files:9-56](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/Altera.files#L9-L56) 同样按 GHDL（v93/v08 二分）与 Mentor/cocotb 分支挂接 Altera 原语库，`Altera_Quartus` 工具链分支留空（自带）。

最后看**调用方**如何引入这些清单。以 `sync_Bits.files` 为例（u3-l2 已读过它的 generate 分发，这里看 `.files` 侧）：

[src/misc/sync/sync_Bits.files:12-23](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.files#L12-L23) 用 `if (DeviceVendor = "Altera")` 守卫 `include "lib/Altera.files"`，`elseif (DeviceVendor = "Xilinx")` 守卫 `include "lib/Xilinx.files"`——**编译期按厂商 include 哪个原语清单，与展开期 generate 选哪个子实体（`sync_Bits_Altera` / `sync_Bits_Xilinx`）由同一个 `DeviceVendor` 驱动**，这正是 u3-l2 强调的「双层选择必须由同一份 `MY_DEVICE` 驱动」。

验证方法学库的引入则发生在测试台 `.files`，例如排序网络测试台：

[tb/sort/sortnet/sortnet_BitonicSort_tb.files:8](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/sort/sortnet/sortnet_BitonicSort_tb.files#L8) 直接 `include "lib/OSVVM.files"`，把 OSVVM 验证库挂进这个测试台的编译单元，随后 `sortnet_BitonicSort_tb.vhdl` 就能 `use` OSVVM 的记分板与覆盖率包。

#### 4.3.4 代码实践

**实践目标**：亲手追踪一条「测试台 → 验证库 → 预编译库/源码」的接入链，理解 `.files` 如何在编译前决定文件集合。

**操作步骤**：

1. 打开 [tb/sort/sortnet/sortnet_BitonicSort_tb.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/sort/sortnet/sortnet_BitonicSort_tb.files)，确认它 `include "lib/OSVVM.files"`（第 8 行）。
2. 打开 [lib/OSVVM.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/OSVVM.files)，回答：若当前 `Tool = "GHDL"` 且预编译目录里**有** `osvvm-obj08.cf`，pyIPCMI 会编译 OSVVM 的源文件吗？若**没有**该 `.cf` 且 `Tool` 既不是 GHDL 也不是 vSim 呢？
3. 再看 [lib/UVVM.files:52-55](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/UVVM.files#L52-L55)，对比它和 OSVVM 的兜底分支有什么不同。
4. （可选，待本地验证）若本地装了 GHDL，把 `MY_DEVICE` 设成一个 Xilinx 型号、跑一次 `poc.sh` 的 test 流程，观察日志里是否出现 `No precompiled ... found` 的 report 或挂库记录。

**需要观察的现象**：`.files` 的 `if/elseif/else` 决定的是「编译前选哪些物理文件」，而非运行期行为；OSVVM 有完整的源码自编译兜底，UVVM 的兜底是 TODO。

**预期结果**：
- GHDL 且 `.cf` 存在 → 不编译源文件，直接 `library osvvm <预编译路径>`。
- GHDL 且 `.cf` 不存在 → 只 `report`，不挂库（此时若测试台 `use osvvm` 会失败）。
- 既非 GHDL 也非 vSim → 走 else 兜底，逐个 `vhdl osvvm "lib/osvvm/*.vhd"` 自编译。
- UVVM 的兜底分支是注释掉的 TODO，说明当前**必须**依赖预编译库，否则无法用。

#### 4.3.5 小练习与答案

**练习 1**：`lib/OSVVM.files` 里 `library osvvm <路径>` 与 `vhdl osvvm "..."` 两条语句有什么本质区别？
**答案**：`library <名> <路径>` 是**登记一个已存在的预编译库**到某路径，不触发编译（前提是 `.cf` 已在那）；`vhdl <库名> "<文件>"` 是**编译某个源文件进指定库**。前者快（复用劳动成果），后者慢但自洽（兜底）。OSVVM.files 的设计是「能复用就 `library`，不能才 `vhdl`」。

**练习 2**：cocotb 在 `.files` 体系里以什么形式出现？为什么它没有自己的 `lib/cocotb.files`？
**答案**：cocotb 以 `Tool = "Cocotb_QuestaSim"` 这个取值出现在 [lib/Xilinx.files:49](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/Xilinx.files#L49) 和 [lib/Altera.files:57](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/Altera.files#L57) 的 `Tool in [...]` 列表里——它复用 Mentor QuestaSim 的厂商预编译库。因为 cocotb 本体是 Python 库、不是 VHDL 编译单元，所以没有 `lib/cocotb.files`；它「接入编译」的方式是作为仿真器的一种 `Tool`，与 vSim 共享同一套原语库挂接。

**练习 3**：为什么 `sync_Bits.files` 在 `DeviceVendor = "Xilinx"` 分支里才 `include "lib/Xilinx.files"`，而不是无条件 include？
**答案**：因为 `sync_Bits_Xilinx.vhdl` 实例化了 Xilinx 原语（如 `FD`），只有目标厂商是 Xilinx 时才会被编译（见 [src/misc/sync/sync_Bits.files:15-17](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.files#L15-L17)），此时才需要挂 Xilinx 原语仿真库。无条件 include 会给 Altera 目标也挂上 unisim，既无必要又可能在缺少预编译库时报错。这与 u3-l2「编译期 `.files` 选择与展开期 generate 选择同源」的结论一致。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿性任务（即本讲指定的代码实践任务）：

**任务**：比较 OSVVM 与 UVVM 的定位，并说明在 `.gitmodules` 中它们各自指向哪个上游仓库；进一步给出「如果要让一个新测试台用上 OSVVM」需要打通的完整链路。

**建议步骤**：

1. **方法学定位**（用 4.1 的结论）：从 [lib/README.md:39-57](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md#L39-L57) 与 [lib/README.md:60-86](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md#L60-L86) 各摘一句定位，填进对比表。要点：OSVVM = 纯 VHDL 的覆盖率/随机/记分板，渐进增强；UVVM = 纯 VHDL 的 VVC 框架 + 总线 BFM/VIP，结构化多接口台。

2. **追踪上游仓库**（用 4.2 的方法）：
   - 读 [.gitmodules:4-6](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.gitmodules#L4-L6) 与 [.gitmodules:13-15](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.gitmodules#L13-L15)，写出相对 URL；
   - 结合 origin `VLSI-EDA/PoC`，解析出绝对 URL：OSVVM → `VLSI-EDA/OSVVM`，UVVM → `VLSI-EDA/UVVM`；
   - 对照 README 的官方上游：OSVVM → `JimLewis/OSVVM`，UVVM → `UVVM/UVVM_All`；
   - 指出差异：`.gitmodules` 指向同组织镜像，README 记录官方源；UVVM 镜像名是 `UVVM`，官方仓库是 `UVVM_All`。

3. **打通接入链**（用 4.3 的机制）：要让新测试台 `tb/foo/foo_tb.vhdl` 用上 OSVVM，需要：
   - 确保 `lib/osvvm/` 已检出（`git submodule update`，否则 [.gitmodules:4-6](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.gitmodules#L4-L6) 指向的代码不在本地）；
   - 在 `foo_tb.files` 里加一行 `include "lib/OSVVM.files"`（参照 [tb/sort/sortnet/sortnet_BitonicSort_tb.files:8](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/sort/sortnet/sortnet_BitonicSort_tb.files#L8)）；
   - 确认仿真环境：若 `Tool="GHDL"` 需预编译 `osvvm-obj08.cf`，否则 [lib/OSVVM.files:29-46](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/OSVVM.files#L29-L46) 的兜底分支会逐个源文件自编译；同时 `VHDLVersion` 必须 ≥ 2008（[lib/OSVVM.files:10-11](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/OSVVM.files#L10-L11)）；
   - 在 `foo_tb.vhdl` 里 `use osvvm.AlertLogPkg.all;` 等即可调用 OSVVM 的验证原语。

**交付物**：一张对比表（OSVVM vs UVVM 的定位 + 上游仓库映射）+ 一份「新测试台接入 OSVVM」的步骤清单。

> 待本地验证项：第 3 步里 GHDL 是否真挂上预编译库、兜底自编译是否成功，取决于本地是否预编译过 OSVVM，需在装好 pyIPCMI + GHDL 的环境里实跑一次确认。

## 6. 本讲小结

- PoC 在 `lib/` 下以 git submodule 集成了 **cocotb / OSVVM / UVVM / VUnit** 四个开源验证库：cocotb（Python cosim）、VUnit（Python 自动化回归）在外层调度，OSVVM（覆盖率/随机/记分板）、UVVM（VVC + 总线 BFM）在 VHDL 内层提供验证原语。
- submodule 集成的权威是 [.gitmodules](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.gitmodules)，其 `url` 用**相对路径**（`../OSVVM.git` 等），相对 PoC origin 解析为同组织镜像（`VLSI-EDA/OSVVM`、`VLSI-EDA/UVVM`），与 README 记录的官方上游（`JimLewis/OSVVM`、`UVVM/UVVM_All`）不是一回事；克隆须 `--recursive` 或手工 `init`+`update`。
- 只有纯 VHDL 的 OSVVM 与 UVVM 有 `lib/*.files` 清单（cocotb/VUnit 是 Python，无 VHDL 清单；cocotb 反以 `Cocotb_QuestaSim` 这个 `Tool` 取值出现在厂商原语清单里）。
- `lib/*.files` 通用骨架是「**版本门槛 → 按 `Tool` 选预编译库 → `?{...}` 探测 → 找到则 `library` 挂接，找不到退回 `vhdl` 自编译**」；OSVVM 有完整自编译兜底，UVVM 兜底仍是 TODO（必须依赖预编译）。
- 厂商原语清单（`Xilinx.files`/`Altera.files`）属另一类用途——挂接 `unisim`/`altera_mf` 等原语仿真模型；调用方核 `.files` 用 `DeviceVendor` 守卫 `include` 哪一份，与展开期 `generate` 选厂商子实体由同一份 `MY_DEVICE` 驱动（呼应 u3-l2 双层选择）。
- 关键认知：**submodule 负责「把代码弄到本地」，`.files` 负责「把代码接进编译」**，两者必须同时到位，测试台才能真正 `use` 到第三方验证库。

## 7. 下一步学习建议

- **u5-l3 cache 子系统**：转向 PoC 自家的复杂子系统，看大型设计如何在真实测试台中验证——届时可以回想本讲的 OSVVM/UVVM 如何为之提供记分板与 BFM。
- **u5-l1 pyIPCMI 基础设施**（若尚未深读）：本讲反复依赖 `.files` 语义与条件变量，想搞清 `?{...}`、`path`、`CONFIG.DirectoryNames:*` 这些到底由谁求值，就回到 pyIPCMI 的 Python 源码（需先 `git submodule update lib/pyIPCMI`）。
- **继续阅读**：[lib/OSVVM.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/OSVVM.files) 的兜底清单里每一个 `*.vhd`（如 `CoveragePkg`、`ScoreboardGenericPkg`），对照 OSVVM 官方文档理解其 API；再看 `tb/sort/sortnet/*_tb.vhdl` 里这些包被实际调用的样子。
- **动手扩展**：仿照 `sortnet_BitonicSort_tb.files`，为你自己的一个核写一份带 `include "lib/OSVVM.files"` 的测试台清单（文件可写在 `PoC-tutorial/` 之外的练习目录），体会「测试台 `.files` → 验证库 `.files` → 预编译库」三级 include 的拼装过程（待本地验证编译结果）。
