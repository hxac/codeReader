# 仓库结构与目录布局

## 1. 本讲目标

上一讲（u1-l1）我们已经建立了对 Open Logic 的全局认知：它是一个纯 VHDL-2008、厂商无关、可商用的 FPGA 组件库，并按 base / axi / intf / fix 四个区域组织。本讲把镜头拉近，带你**走一遍仓库的真实目录**，让你在阅读任何后续讲义之前，先知道「代码放在哪里、测试放在哪里、文档放在哪里、怎么把它们编译到一起」。

学完本讲后，你应当能够：

- 说出 `src`、`test`、`doc`、`sim`、`lint`、`tools` 各顶层目录的职责。
- 解释实体命名 `olo_<area>_<function>` 与目录路径的对应关系。
- 理解 `test` 目录如何「镜像」`src` 目录，以及 `test/tb` 里共享验证组件（VC）的作用。
- 说明 `compile_order.txt` 为什么**不是按区域分组**而是按依赖排序，以及它如何把所有源码串成一个 VHDL 库。
- 知道 `3rdParty/en_cl_fix` 子模块的来源、许可证，以及为什么必须用 `--recursive` 克隆。

## 2. 前置知识

本讲是纯「读目录」的导览，几乎不涉及电路细节，但会用到的术语先在这里统一：

- **实体（entity）**：VHDL 里一个可综合的设计模块。在 Open Logic 中，几乎每个实体对应 `src/<area>/vhdl/` 下的一个 `.vhd` 文件，文件名与实体名一致。
- **区域（area）**：仓库按功能划分的顶层分组，目前是 `base`、`axi`、`intf`、`fix` 四个。
- **testbench（测试台，简称 tb）**：只为仿真而存在、不可综合的测试模块，用来给被测实体施加激励并检查输出。
- **验证组件（VC，Verification Component）**：可以在多个 testbench 之间复用的标准化激励器/检查器（例如「AXI 主机 VC」「SPI 从机 VC」），相当于软件测试里的 mock / test double。
- **子模块（git submodule）**：把另一个独立的 git 仓库「嵌」进当前仓库的子目录。Open Logic 用它引入第三方定点库 `en_cl_fix`。
- **综合（synthesis）**：把 VHDL 代码翻译成 FPGA 硬件网表的过程；能综合意味着代码是真实可实现的，而不只是仿真模型。

如果这些概念还比较模糊也没关系，下面的源码讲解会结合真实文件再解释一遍。

## 3. 本讲源码地图

本讲涉及的关键文件与目录如下：

| 路径 | 作用 |
|------|------|
| `src/` | 全部可综合 RTL 源码，按区域拆分 |
| `test/` | 仿真测试台，目录结构镜像 `src/` |
| `doc/` | 每个实体的 Markdown 文档 + 原则文档 + 教程 |
| `sim/` | VUnit 仿真运行器、测试配置、覆盖率与徽章脚本 |
| `lint/` | VSG 代码风格检查脚本与配置 |
| `tools/` | 厂商导入脚本、FuseSoC、综合资源评估等工程化工具 |
| `compile_order.txt` | 全部源文件的**编译顺序清单**（依赖排序） |
| `.gitmodules` | 声明 `3rdParty/en_cl_fix` 子模块 |
| `Readme.md` | 项目说明，含区域划分与「单库编译」建议 |

## 4. 核心概念与源码讲解

本讲按四个最小模块拆分：先看源码区 `src`，再看测试区 `test`，然后看一圈工程化目录 `doc/sim/lint/tools`，最后把所有源码串起来的 `compile_order.txt` 与子模块。

### 4.1 src 区域划分

#### 4.1.1 概念说明

`src` 目录存放 Open Logic 的全部**可综合**源码。仓库把它按功能切成四个区域，每个区域就是一个子目录：

- **`base`**：基础逻辑，不依赖其他任何区域。流水线寄存器、RAM、FIFO、仲裁器、跨时钟域等都住在这里，是被其他区域反复复用的「地基」。
- **`axi`**：所有与 AXI4 / AXI4-Lite / AXI4-Stream 总线相关的组件，依赖 `base`。
- **`intf`**：与 FPGA **芯片外部**打交道的接口逻辑（UART、SPI、I2C、按键消抖等），依赖 `base`。
- **`fix`**：定点数学运算（加减乘、FIR、CORDIC 等），依赖 `base` **以及**第三方子模块 `en_cl_fix`。

区域之间存在明确的依赖方向（这一点在 README 的 Structure 章节写得很清楚），画成图就是：

```
            ┌────────────────────────┐
            │   3rdParty/en_cl_fix   │   (外部子模块，MIT)
            └───────────┬────────────┘
                        │ 仅 fix 依赖
            ┌───────────▼────────────┐
            │         base           │   ← 所有区域的地基，无外部依赖
            └─┬───────┬───────┬───────┘
              │       │       │
          ┌───▼──┐ ┌──▼───┐ ┌─▼──┐
          │ axi  │ │ intf │ │fix │
          └──────┘ └──────┘ └────┘
        (依赖 base)(依赖 base)(依赖 base + en_cl_fix)
```

这种分层是后续每一讲的基础：当你只用 `axi` 时，只需编译 `base` + `axi`；用到 `fix` 时才需要把 `en_cl_fix` 也拉进来。

#### 4.1.2 核心流程

一个实体在 `src` 中的典型布局是这样的：

```
src/
├── base/
│   ├── olo_base_dev.core      ← FuseSoC 的 core 描述文件（每区域一个）
│   ├── tcl/                    ← 该区域的综合约束（AMD/Gowin 等专用 .tcl）
│   │   ├── olo_base_cc_bits.tcl
│   │   └── olo_base_constraints_amd.tcl
│   └── vhdl/                   ← 真正的源码（每个实体一个 .vhd）
│       ├── olo_base_pkg_math.vhd
│       ├── olo_base_pl_stage.vhd
│       ├── olo_base_fifo_sync.vhd
│       └── ...
├── axi/   (同样 vhdl/ + .core)
├── intf/  (同样 vhdl/ + .core)
└── fix/   (同样 vhdl/ + .core)
```

注意三个细节：

1. **源码统一放在 `vhdl/` 子目录下**，区域目录本身只放 `.core`（FuseSoC 包描述）和 `tcl/`（约束）。
2. **命名严格遵循 `olo_<area>_<function>`**：看文件名就能反推出它的区域。例如 `olo_base_fifo_sync` 属于 base 区域、是同步 FIFO；`olo_axi_lite_slave` 属于 axi 区域、是 AXI4-Lite 从机；`olo_intf_uart` 属于 intf 区域、是 UART。这套命名是 Open Logic「易用」哲学的落地之一——名字本身就在自我说明。
3. **`.core` 文件与 `tcl/` 约束**是工程化集成用的：前者给 FuseSoC 用（见第 10 单元），后者给厂商综合工具用（例如 AMD 的 scoped 约束）。

#### 4.1.3 源码精读

README 的 Structure 章节用一句话概括了四个区域及其依赖，这是理解 `src` 划分的权威依据：

[Readme.md:68-78](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L68-L78) — 这里列出 base / axi / intf / fix 四个区域，并注明 axi 和 intf「requires: _base_」、fix「requires: _base_ and _en_cl_fix_」。

> 说明：这段就是上面依赖图的文字版本。`base` 没有任何 requires，是依赖链的根；`fix` 是唯一额外依赖第三方子模块的区域。

对照 `compile_order.txt` 的开头，可以看到 `base` 的公共包排在最前面——因为它们被几乎所有实体引用：

[compile_order.txt:1](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/compile_order.txt#L1) — 第一行是 `src/base/vhdl/olo_base_pkg_attribute.vhd`，它定义跨工具综合属性，是全库最底层的依赖之一，因此排在清单首位。

#### 4.1.4 代码实践

**实践目标**：用文件名规则快速判断实体所属区域，建立「名字 ↔ 目录」的直觉。

**操作步骤**：

1. 列出 `src/base/vhdl/`、`src/axi/vhdl/`、`src/intf/vhdl/`、`src/fix/vhdl/` 四个目录里的文件名（例如 `ls src/base/vhdl/`）。
2. 随机挑 5 个文件名，遮住路径，仅凭 `olo_<area>_<function>` 的第二段判断它属于哪个区域。
3. 对照真实路径核对你的判断。

**需要观察的现象**：文件名第二段（`base`/`axi`/`intf`/`fix`）与所在目录的 `<area>` 段应当完全一致。

**预期结果**：例如 `olo_intf_i2c_master.vhd` 第二段是 `intf`，所以它一定在 `src/intf/vhdl/` 下；`olo_axi_master_simple.vhd` 一定在 `src/axi/vhdl/` 下。这一规律在仓库中**无例外**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Open Logic 要把区域依赖设计成「`base` 是地基、其他区域单向依赖 `base`」的树形结构，而不是让区域之间互相依赖？

**参考答案**：树形依赖让「按需取用」成为可能——只用 `axi` 的工程只需编译 `base + axi`，不必拖入 `fix` 或 `intf`；同时避免了循环依赖，编译顺序（见 4.4）也容易确定。这与「易用、避免功能蔓延」的哲学一致。

**练习 2**：文件 `olo_fix_cordic_rot.vhd` 应当位于哪个目录？它可能依赖哪些区域？

**参考答案**：第二段是 `fix`，所以位于 `src/fix/vhdl/olo_fix_cordic_rot.vhd`；`fix` 依赖 `base` 和 `en_cl_fix`，因此它间接依赖这两者。

---

### 4.2 test 与 tb 验证组件

#### 4.2.1 概念说明

Open Logic 的「可信任代码（Trustable Code）」哲学要求**每个实体都配有 testbench**。这些测试台全部放在 `test/` 目录下。`test/` 的组织方式有一个关键特点：**它的结构镜像（mirror）`src/`**。

「镜像」的含义是：`src/` 有四个区域子目录，`test/` 也有完全对应的四个区域子目录；`src` 里每个实体在 `test` 里都有一个同名子目录，里面装着它的 testbench。这样你看到一个实体，就能立刻定位它的测试，反之亦然。

除此之外，`test/tb/` 是一个特殊目录，存放**跨实体共享的验证组件（VC）**。VC 不是某个实体的专属测试，而是可被任意 testbench 调用的标准化「演员」，例如一个会自动发起 AXI 读写事务的 AXI 主机 VC。

#### 4.2.2 核心流程

`test` 的典型布局：

```
test/
├── base/
│   ├── olo_base_fifo_sync/                 ← 与 src/base/vhdl/olo_base_fifo_sync.vhd 一一对应
│   │   └── olo_base_fifo_sync_tb.vhd       ← 该实体的 testbench
│   ├── olo_base_pl_stage/
│   │   └── olo_base_pl_stage_tb.vhd
│   └── ...（base 区每个实体一个目录）
├── axi/   (每个 axi 实体一个目录)
├── intf/  (每个 intf 实体一个目录)
├── fix/   (每个 fix 实体一个目录)
└── tb/                              ← 共享验证组件（VC），不属于任何单一实体
    ├── olo_test_axi_master_vc.vhd
    ├── olo_test_axi_slave_vc.vhd
    ├── olo_test_fix_checker_vc.vhd
    ├── olo_test_spi_master_vc.vhd
    ├── olo_test_i2c_vc.vhd
    └── olo_test_activity_pkg.vhd
```

要点：

1. **镜像对应**：`src/<area>/vhdl/<entity>.vhd` ↔ `test/<area>/<entity>/<entity>_tb.vhd`。testbench 文件名固定为 `<entity>_tb.vhd`。
2. **VC 命名约定**：共享组件统一加 `olo_test_` 前缀，并以 `_vc`（Verification Component）结尾，例如 `olo_test_axi_master_vc`。这样一眼就能把「可复用 VC」和「某个实体的专属 tb」区分开。
3. **VC 的价值**：假设你要测一个 AXI4-Lite 从机，不必自己手写 AXI 主机握手逻辑，直接实例化 `olo_test_axi_master_vc` 就能发读写命令；再配合 `olo_test_fix_checker_vc` 这类检查器自动比对结果。后续第 10 单元会专门讲 VC 的用法。

#### 4.2.3 源码精读

以同步 FIFO 为例，它的源码与测试台恰好成对出现，体现镜像关系：

源码：`src/base/vhdl/olo_base_fifo_sync.vhd`
测试台：[test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd) — 路径里连续出现两次 `olo_base_fifo_sync`（目录名 + 文件名），文件名加 `_tb` 后缀，这就是镜像约定的标准写法。

再看共享 VC 目录，里面的文件都遵循 `olo_test_*_vc` 命名：

[test/tb/olo_test_axi_master_vc.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_axi_master_vc.vhd) — 一个可被任意需要 AXI 主机的 testbench 复用的验证组件，前缀 `olo_test_`、后缀 `_vc` 标明它的身份。

> 说明：`test/tb/` 里的 VC 是「公共工具」，`test/<area>/<entity>/` 里的 `_tb.vhd` 是「专属测试」。两者通过命名约定清楚区分，混用时也不会混淆。

#### 4.2.4 代码实践

**实践目标**：验证 `src` 与 `test` 的镜像关系，并体会 VC 的复用价值。

**操作步骤**：

1. 在 `src/base/vhdl/` 中任选一个实体文件，例如 `olo_base_cam.vhd`。
2. 进入 `test/base/olo_base_cam/`，确认里面存在 `olo_base_cam_tb.vhd`。
3. 打开 `test/tb/` 目录，浏览其中 `olo_test_*_vc.vhd` 文件名，挑出一个 AXI 相关 VC，再打开某个 axi 区域实体的 testbench（如 `test/axi/...`），用文本搜索看它是否 `use` 或实例化了该 VC。

**需要观察的现象**：每个 `src` 实体在 `test` 下都有同名目录与 `_tb.vhd`；axi 类 testbench 倾向于复用 `test/tb` 里的 AXI VC 而非自己重写主机逻辑。

**预期结果**：镜像一一对应；VC 被 axi testbench 实际引用。若本地未配置仿真环境，可只做文件浏览，这部分结论仅靠 `ls` 与文本搜索即可得出（**源码阅读型实践，无需运行仿真**）。

#### 4.2.5 小练习与答案

**练习 1**：实体 `olo_intf_uart` 的 testbench 应当放在哪个路径、文件名叫什么？

**参考答案**：`test/intf/olo_intf_uart/olo_intf_uart_tb.vhd`（区域 intf → 同名目录 → `<entity>_tb.vhd`）。

**练习 2**：`olo_test_fix_checker_vc.vhd` 为什么放在 `test/tb/` 而不是某个具体实体的目录里？

**参考答案**：它是一个可复用的检查器 VC，会被多个 fix 区域的 testbench 共享调用，不属于任何单一实体，因此放在公共的 `test/tb/` 下。

---

### 4.3 doc / sim / lint / tools 工程化目录

#### 4.3.1 概念说明

除了「源码 + 测试」这两块核心，Open Logic 还有四个工程化目录，分别支撑**文档、仿真运行、代码风格检查、厂商集成与资源评估**。它们让你不仅能读懂代码，还能把整个项目跑起来、检查质量、集成进真实 FPGA 工程。

#### 4.3.2 核心流程

四个目录各司其职：

| 目录 | 职责 | 关键内容 |
|------|------|----------|
| `doc/` | 文档 | `EntityList.md`（全部实体清单）、每个实体的 `.md`、原则文档（如 `clock_crossing_principles.md`）、各厂商教程 |
| `sim/` | 仿真运行 | `run.py`（VUnit 运行器）、`test_configs/`（按区域组织的 generic 组合）、`codegen.py`（仿真前代码生成）、`AnalyzeCoverage.py` / `Badge.py`（覆盖率与徽章） |
| `lint/` | 代码风格 | `script/script.py`（运行 VSG 检查）、`config/vsg_config.yml`（规则配置） |
| `tools/` | 厂商集成与评估 | `vivado/`、`quartus/`、`libero/`、`gowin/`、`efinity/` 等的 `import_sources` 脚本、`fusesoc/`、`inference_test/`（综合资源评估） |

其中 `tools/` 内部按厂商再分子目录，每个厂商都有一个把 Open Logic 源码导入到该工具工程的脚本：

```
tools/
├── vivado/import_sources.tcl       ← AMD Vivado 导入（TCL）
├── quartus/import_sources.tcl      ← Intel Quartus 导入（TCL）
├── questa/vcom_sources.tcl         ← Questa 仿真编译（TCL）
├── libero/import_sources.tcl       ← Microchip Libero 导入
├── gowin/import_sources.tcl        ← 高云 Gowin 导入
├── efinity/import_sources.py       ← Efinity 导入（Python）
├── fusesoc/                        ← FuseSoC 包描述与维护脚本
└── inference_test/                 ← 基于 YAML 的多工具综合资源评估
    ├── InferenceTest.py
    └── yaml/{base,axi,intf,fix}.yml
```

#### 4.3.3 源码精读

`doc/EntityList.md` 是浏览全部实体的总入口，README 顶部就指向它：

[Readme.md:24](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L24) — 「Browse the [Entity List](./doc/EntityList.md) to see what is available.」说明 `doc/EntityList.md` 是「这个库到底提供了哪些实体」的权威清单。

仿真运行器位于 `sim/run.py`，它基于 VUnit 框架驱动整个仿真流程：

[sim/run.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py) — 仿真主入口，负责发现测试、选择仿真器（GHDL/NVC/Questa/Riviera）、收集覆盖率等。下一讲（u1-l4）会详细拆解它。

代码风格检查的运行脚本在 `lint/script/`：

[lint/script/script.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/lint/script/script.py) — 调用 VSG（VHDL Style Guide）对所有 `.vhd` 文件做风格检查，并区分生产代码与验证组件（VC）使用不同配置。

厂商导入脚本以 AMD Vivado 为例：

[tools/vivado/import_sources.tcl](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/vivado/import_sources.tcl) — 按 `compile_order.txt` 收集源文件并导入 Vivado 工程，统一编进名为 `olo` 的 VHDL 库。第 u1-l3 讲会带读者实际执行一次导入。

> 说明：这四个目录共同构成「工程化闭环」——`doc` 给人看、`sim` 跑验证、`lint` 守风格、`tools` 接厂商。后续进阶讲义（第 10 单元）会逐一深入。

#### 4.3.4 代码实践

**实践目标**：建立对四个工程化目录的感性认识，知道「要做某件事该去哪个目录找工具」。

**操作步骤**：

1. 打开 `doc/EntityList.md`，数一下它大致列出了多少个实体（不必精确）。
2. 查看 `sim/test_configs/` 下有哪些文件，确认它们按区域（`olo_base.py`、`olo_axi.py`、`olo_intf.py`、`olo_fix.py`）组织。
3. 查看 `lint/config/` 下的配置文件名，注意有一个 `vsg_config_overlay_vc.yml`——猜猜它为什么单独存在（提示：VC 与生产代码的规则不同）。
4. 列出 `tools/` 下的厂商子目录，统计支持几家厂商。

**需要观察的现象**：文档、仿真配置、lint 配置、厂商脚本都「按区域 / 按厂商」有规律地分文件存放。

**预期结果**：你会发现 `sim/test_configs/` 恰好有四个区域配置文件，与 `src` 的四个区域一一对应；`tools/` 支持 vivado、quartus、libero、gowin、efinity、questa、yosys、fusesoc、inference_test 等多家。**源码阅读型实践，无需运行命令**。

#### 4.3.5 小练习与答案

**练习 1**：你想知道 Open Logic 是否提供了「动态移位」实体，应该先看哪个文件？

**参考答案**：先看 `doc/EntityList.md`，它是实体总清单；找到后再点进对应的实体文档（如 `doc/base/...`）看详细说明。

**练习 2**：`lint/config/vsg_config_overlay_vc.yml` 这个「overlay」配置为什么会单独存在？

**参考答案**：验证组件（VC）是测试代码、不可综合，其风格要求与生产 RTL 不同（例如允许某些只用于仿真的写法），因此用一份 overlay 配置在基础 `vsg_config.yml` 之上为 VC 放宽或替换部分规则。

---

### 4.4 compile_order 与子模块

#### 4.4.1 概念说明

`compile_order.txt` 是把整个仓库的源码「串」起来的关键文件。它是一个**纯文本清单**，逐行列出**所有**需要编译的源文件路径，共 93 行（对应 93 个源文件）。

它有两个最重要的特性：

1. **不按区域分组，而是按依赖排序**。VHDL 要求「先编译被引用的包/实体，再编译引用者」，因此清单把最底层的公共包（如 `olo_base_pkg_attribute`、`olo_base_pkg_math`）排在最前，把依赖它们的实体排在后面。这意味着 base、axi、intf、fix 的文件在清单里是**交错**出现的，而不是四个连续的块。
2. **单库编译策略**。README 建议「把你需要的区域（及其依赖）的所有文件编译进**同一个** VHDL 库」，库名任选（Open Logic 自身的脚本与教程统一用 `olo`）。这样所有实体都在一个库里，互相 `use` 时无需跨库引用。

而 `3rdParty/en_cl_fix` 是一个 **git 子模块**：它是独立的 GitHub 仓库（MIT 许可证），通过 `.gitmodules` 声明后被嵌进 `3rdParty/en_cl_fix/` 目录，提供定点运算的基础设施。因为它是外部仓库，克隆 Open Logic 时必须加 `--recursive`，否则这个目录是空的，`fix` 区域将无法编译。

#### 4.4.2 核心流程

`compile_order.txt` 的内部顺序大致是：

```
1. base / intf 的底层包与实体（按依赖交错）    ← 第 1 ~ 50 行
2. axi 区域的全部实体                          ← 第 51 ~ 55 行
3. en_cl_fix 的包（外部子模块）+ fix 区域实体   ← 第 56 行起
   ...
4. en_cl_fix 的实现实体（放最后）              ← 第 91 ~ 93 行
```

关键观察：

- **公共包优先**：`olo_base_pkg_attribute` 在第 1 行，因为它定义综合属性、被全库引用。
- **区域交错**：第 2 行就出现了 `olo_intf_sync`（intf），夹在 base 文件之间——因为 `compile_order` 关心的是依赖顺序而非区域归属。
- **子模块的包先于 fix 实体**：`en_cl_fix_pkg.vhd` 在第 57 行，紧接其后第 58 行才是 `olo_fix_pkg.vhd`，因为 fix 区域的包要 `use` en_cl_fix 的类型。
- **实现实体放最后**：en_cl_fix 的 `saturate`/`round`/`resize` 实现实体排在第 91–93 行，放在引用它们的 fix 实体之后也无妨（VHDL 实体只需在「整体编译完成」后即可被引用）。

各区域的源文件数量（在当前 HEAD `ecca8af` 下，用 `grep -c "src/<area>/" compile_order.txt` 统计）：

| 区域 | 源文件数 |
|------|----------|
| `base` | 43 |
| `axi` | 5 |
| `intf` | 7 |
| `fix` | 33 |
| `3rdParty/en_cl_fix`（子模块） | 5 |
| **合计** | **93** |

子模块本身由 `.gitmodules` 描述：

```
[submodule "3rdParty/en_cl_fix"]
    path = 3rdParty/en_cl_fix
    url = https://github.com/open-logic/en_cl_fix.git
```

#### 4.4.3 源码精读

清单第 1 行是全库最底层依赖，第 2 行就出现 intf 文件，直观体现「按依赖排序而非按区域分组」：

[compile_order.txt:1-2](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/compile_order.txt#L1-L2) — 第 1 行 `olo_base_pkg_attribute.vhd`（base 公共包），第 2 行 `olo_intf_sync.vhd`（intf 实体）紧随其后，两行分属不同区域却相邻排列，因为 intf_sync 只依赖这些 base 包。

axi 区域作为连续的小块出现在中段：

[compile_order.txt:51-55](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/compile_order.txt#L51-L55) — 第 51–55 行是 axi 的 5 个实体（`olo_axi_pl_stage`、`olo_axi_pkg_protocol`、两个 master、一个 lite_slave），它们都依赖 base，所以排在 base 之后。

子模块的包先于 fix 区域的包被编译：

[compile_order.txt:56-58](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/compile_order.txt#L56-L58) — 第 56–57 行是 `3rdParty/en_cl_fix` 的两个包，第 58 行才是 `olo_fix_pkg.vhd`，说明 fix 的包依赖 en_cl_fix 的类型，必须先编译子模块的包。

README 的「Get It」章节解释了为什么必须递归克隆：

[Readme.md:39-54](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L39-L54) — 明确列出子模块 `en_cl_fix`（MIT 许可证），并强调克隆时要加 `--recurse-submodules`，否则子模块源码缺失。

README 同时给出「单库编译」建议：

[Readme.md:80-82](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/Readme.md#L80-L82) — 「It's suggested that you compile ALL files of the areas you need (plus their dependencies) into one VHDL library」，并说明库名可任选、可与用户代码共用同一个库。

子模块声明文件只有三行：

[.gitmodules:1-3](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.gitmodules#L1-L3) — 声明 `3rdParty/en_cl_fix` 指向 `https://github.com/open-logic/en_cl_fix.git`，这就是「外部依赖」在仓库里的唯一登记处。

> 说明：`compile_order.txt` + `.gitmodules` + 单库策略，三者合起来回答了一个核心问题——「拿到源码后，我该按什么顺序、把它们编译进哪个库、外部依赖从哪来」。这正是下一讲（u1-l3）动手集成的前提。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：亲手统计 `compile_order.txt` 中四个区域各自的源文件数量，并画出 `src` 与 `test` 的镜像对应关系示意图。

**操作步骤**：

1. 在仓库根目录运行下列命令，分别统计各区域在 `compile_order.txt` 中出现的次数：

   ```bash
   grep -c "src/base/" compile_order.txt
   grep -c "src/axi/"  compile_order.txt
   grep -c "src/intf/" compile_order.txt
   grep -c "src/fix/"  compile_order.txt
   grep -c "3rdParty/en_cl_fix" compile_order.txt
   ```

2. 再用 `wc -l < compile_order.txt` 得到总行数，验证五个数字之和等于总行数。
3. 在笔记里画一张 `src` ↔ `test` 的镜像示意图（可参考下面的模板）。

**需要观察的现象**：五个 grep 计数相加恰好等于总行数（93），说明清单覆盖了全部源码、没有遗漏也没有重复；区域分布是 base 最多、axi 最少。

**预期结果**（在当前 HEAD `ecca8af` 下实测）：

| 区域 | 计数 |
|------|------|
| `src/base/` | 43 |
| `src/axi/` | 5 |
| `src/intf/` | 7 |
| `src/fix/` | 33 |
| `3rdParty/en_cl_fix` | 5 |
| **合计** | **93** |

镜像示意图模板（供你填充实体的真实例子）：

```
   src/                                    test/
   ├── base/vhdl/                          ├── base/
   │   └── olo_base_fifo_sync.vhd    ────▶ │   └── olo_base_fifo_sync/
   │                                          │       └── olo_base_fifo_sync_tb.vhd
   ├── axi/vhdl/                           ├── axi/
   │   └── olo_axi_lite_slave.vhd    ────▶ │   └── olo_axi_lite_slave/
   │                                          │       └── olo_axi_lite_slave_tb.vhd
   ├── intf/vhdl/                          ├── intf/   ...同构...
   └── fix/vhdl/                           ├── fix/    ...同构...
                                            └── tb/                  ← 共享验证组件（VC）
                                                └── olo_test_*_vc.vhd   （src 中无对应，仅测试用）
```

如果你本地没有 shell 环境，也可以直接打开 `compile_order.txt` 人工计数，结论一致（**最低要求：阅读 `compile_order.txt` 并手数 base 行数，验证约为 43**）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `compile_order.txt` 不直接按 base、axi、intf、fix 四个连续块来排列，而要让区域交错？

**参考答案**：因为 VHDL 编译有依赖关系——引用者必须在被引用者之后编译。许多 intf / fix 实体只依赖少数几个 base 包，把它们紧跟在所依赖的包后面能让依赖关系一目了然；纯粹按区域分块反而会把「依赖」与「被依赖」拆到远处，看不出先后。区域归属已由路径里的 `<area>` 段体现，清单只需保证顺序正确。

**练习 2**：如果不小心用了普通 `git clone`（没加 `--recursive`），`3rdParty/en_cl_fix` 目录会是什么状态？会造成什么后果？

**参考答案**：该目录会是空的（只有一条 gitlink，没有实际文件）。后果是 `compile_order.txt` 第 56–57、91–93 行引用的 en_cl_fix 文件全部缺失，`fix` 区域无法编译。补救办法是执行 `git submodule update --init --recursive`。

**练习 3**：README 说「库名可任选」，那为什么厂商导入脚本和教程里几乎都用 `olo` 作为库名？

**参考答案**：这是一种**约定优于配置**的统一惯例。库名在技术上确实任意，但全项目统一用 `olo` 能让文档、脚本、教程、testbench 里的 `library olo;` / `use olo.xxx;` 引用保持一致，降低沟通成本。你自己当然可以叫它别的名字，但需相应替换所有引用。

---

## 5. 综合实践

把本讲的四个模块串起来，完成下面这个「仓库导览」小任务：

1. **取一个实体全程跟踪**：在 `doc/EntityList.md` 中任选一个实体（例如 `olo_base_fifo_sync`），依次定位它的：
   - 源码：`src/base/vhdl/olo_base_fifo_sync.vhd`
   - 测试台：`test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd`
   - 文档：`doc/base/` 下对应的 `.md`
   - 在 `compile_order.txt` 中的行号
2. **判断它的依赖**：根据所在区域（base）说明它依赖哪些区域（答案：base 不依赖其他区域）。
3. **画出你自己的「四区域依赖图 + src/test 镜像图」**，标注每个区域的源文件数（43 / 5 / 7 / 33）和子模块（5）。
4. **回答**：如果有一个工程只用 AXI 相关组件，它需要编译哪些区域？需要 `en_cl_fix` 吗？

参考答案：只用 AXI → 编译 `base + axi` 即可，**不需要** `en_cl_fix`（那是 `fix` 区域才依赖的）。这正是「按需取用」分层设计带来的好处。

> 说明：本实践为**源码阅读型实践**，全程只需 `ls`、文本编辑器和文件浏览，不需要安装任何 EDA 工具。完成后，你就建立了对整个仓库「地图」的清晰认知，后续每一讲都可以在这张地图上定位。

## 6. 本讲小结

- `src/` 按四个区域（`base`/`axi`/`intf`/`fix`）存放全部可综合源码，命名严格遵循 `olo_<area>_<function>`，区域间是 `base` 为地基的单向树形依赖。
- `test/` 的结构**镜像** `src/`：`test/<area>/<entity>/<entity>_tb.vhd`；`test/tb/` 则存放跨实体共享的验证组件（VC），统一以 `olo_test_*_vc` 命名。
- `doc/`、`sim/`、`lint/`、`tools/` 四个目录分别支撑文档浏览、仿真运行、风格检查、厂商集成与综合评估，构成工程化闭环。
- `compile_order.txt` 是按**依赖顺序**（而非区域）排列的 93 个源文件清单，配合「单库编译」策略把全库串成一个名为 `olo` 的 VHDL 库。
- `3rdParty/en_cl_fix` 是 MIT 许可证的 git 子模块，仅被 `fix` 区域依赖，必须用 `--recursive` 克隆，否则 `fix` 无法编译。
- 当前 HEAD 下各区域源文件数为 base 43、axi 5、intf 7、fix 33、en_cl_fix 5，合计 93。

## 7. 下一步学习建议

本讲让你看清了「代码放在哪」。接下来：

- **想立刻动手把源码导入厂商工具** → 进入 **u1-l3（获取、编译与集成到厂商工具）**，那里会带你实际执行 `tools/vivado/import_sources.tcl` 之类的导入脚本，并把源码编进 `olo` 库。
- **想先跑一个仿真看看效果** → 进入 **u1-l4（运行第一个仿真）**，学习用 `sim/run.py` 基于 VUnit + GHDL 跑一个 base 区域的测试。
- **想先学会「读」一个标准实体** → 进入 **u1-l5（编码规范与阅读一个实体）**，结合 `olo_base_pl_stage.vhd` 讲解命名后缀、握手与复位约定。

无论选哪条路，建议先回头确认你已经能凭文件名判断实体所属区域、能在 `test/` 下找到对应 testbench——这是后续所有讲义的基本功。
