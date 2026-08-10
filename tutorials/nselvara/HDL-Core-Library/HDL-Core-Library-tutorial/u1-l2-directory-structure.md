# 仓库目录结构与 IP 组织约定

## 1. 本讲目标

学完本讲后，你应该能够：

- 在 `ip/` 目录树中**快速定位任意一个 IP 的设计源码与测试台**。
- 说出本库「**设计源码 + `tb/` 测试台文件夹 + `.do` 波形脚本**」这三件套的命名约定。
- 识别 `ip/memories/` 下 `fifo / ram / rom` 的三级分类，以及共享包的摆放位置。
- 读懂一个 `.do` 波形脚本如何把信号分组展示。

本讲不涉及任何 VHDL 语法细节，只解决一个问题：**拿到这个仓库后，怎么找到你想看的东西**。这是一切后续阅读的基础。

## 2. 前置知识

承接上一讲（[u1-l1 项目定位与 IP 核库概览](u1-l1-project-overview.md)），你已经知道：

- 这是一个 **VHDL-2008 可复用 IP 核库**，核心是「同一 entity + 多套厂商 architecture」。
- IP 核分四大类：**存储器、同步与时序、通信接口（SPI）、输入处理**。

本讲会用到两个最基础的概念：

- **设计源码（design file）**：描述硬件行为的 `.vhd` 文件，最终会被综合成 FPGA 电路，例如 `debouncer.vhd`。
- **测试台（testbench，简称 tb）**：只用于仿真、不会被综合成电路的 `.vhd` 文件，它给设计「喂」激励并检查输出，例如 `tb_debouncer.vhd`。在本库里测试台都用 VUnit 框架编写（下一单元会讲）。

> 术语：**仿真（simulation）**指用软件模拟电路运行；**综合（synthesis）**指把代码翻译成真实逻辑门。测试台只参与仿真，不参与综合。

## 3. 本讲源码地图

本讲主要阅读的真实源码与配置：

| 路径 | 作用 |
| --- | --- |
| `README.md` | 项目说明，含一张简化的「Library Structure」目录树 |
| `ip/` 整棵目录树 | 本讲的主角，所有 IP 核都在这里 |
| `ip/communication/spi/tb/tb_spi_tx.do` | 一个真实的 `.do` 波形脚本样例 |
| `.gitmodules` | 声明 `ip/vhdl_utils` 子模块来源 |

## 4. 核心概念与源码讲解

### 4.1 `ip/` 目录树总览

#### 4.1.1 概念说明

整个项目的所有 IP 核都集中在一个根目录 `ip/` 下。`ip/` 之外只有配置文件（`README.md`、`vhdl_ls.toml`、`.github/`、`LICENSE` 等）。

`ip/` 内部按**功能大类**分成若干一级子目录，每一个一级子目录就是一个 IP 或一组同类 IP。其中 `memories/` 因为内部模块很多，又向下分了 `fifo / ram / rom` 三级。

#### 4.1.2 核心流程：从功能大类到具体文件

下面是 `ip/` 的**真实**完整目录树（基于当前 HEAD 的 `git ls-files` 输出整理，每个叶子文件都真实存在）：

```text
ip/
├── clock_enable/
│   └── clock_enable.vhd                  ← 设计（无独立 tb，见 4.2.4）
├── communication/
│   └── spi/
│       ├── spi_interface.vhd             ← 设计：SPI 顶层
│       ├── spi_pkg.vhd                   ← 设计：SPI 通用包
│       ├── spi_rx.vhd                    ← 设计：SPI 接收
│       ├── spi_tx.vhd                    ← 设计：SPI 发送
│       └── tb/
│           ├── tb_spi_interface.do       ← 波形脚本
│           ├── tb_spi_interface.vhd      ← 测试台
│           ├── tb_spi_rx.do
│           ├── tb_spi_rx.vhd
│           ├── tb_spi_tx.do
│           └── tb_spi_tx.vhd
├── debouncer/
│   ├── debouncer.vhd                     ← 设计
│   └── tb/
│       ├── tb_debouncer.do               ← 波形脚本
│       └── tb_debouncer.vhd              ← 测试台
├── ff_synchroniser/
│   ├── ff_synchroniser.vhd               ← 设计：单比特同步器
│   ├── ff_synchroniser_vector.vhd        ← 设计：多比特同步器
│   └── tb/
│       ├── tb_ff_synchroniser.do
│       └── tb_ff_synchroniser.vhd
├── memories/
│   ├── memories_pkg.vhd                  ← 共享包（fifo/ram/rom 都用它）
│   ├── fifo/
│   │   ├── docs/
│   │   │   └── async_fifo.drawio.svg     ← 设计图（draw.io）
│   │   ├── fifo_async.vhd                ← 设计：异步 FIFO
│   │   ├── fifo_sync.vhd                 ← 设计：同步 FIFO
│   │   └── tb/
│   │       ├── tb_fifo_async.do
│   │       ├── tb_fifo_async.vhd
│   │       ├── tb_fifo_sync.do
│   │       └── tb_fifo_sync.vhd
│   ├── ram/
│   │   ├── dual_clock_dual_port_ram.vhd  ← 设计（无独立 tb）
│   │   ├── dual_port/
│   │   │   ├── dual_port_ram.vhd
│   │   │   └── tb/
│   │   │       ├── tb_dual_port_ram.do
│   │   │       └── tb_dual_port_ram.vhd
│   │   └── single_port/
│   │       ├── single_port_ram.vhd
│   │       └── tb/
│   │           ├── tb_single_port_ram.do
│   │           └── tb_single_port_ram.vhd
│   └── rom/
│       ├── rom.vhd
│       └── tb/
│           ├── tb_rom.do
│           └── tb_rom.vhd
├── pll/
│   ├── pll.vhd
│   └── tb/
│       ├── tb_pll.do
│       └── tb_pll.vhd
├── reset_on_startup/
│   ├── reset_on_startup.vhd
│   └── tb/
│       ├── tb_reset_on_startup.do
│       └── tb_reset_on_startup.vhd
├── requirements.txt                      ← Python 依赖（VUnit）
├── test_runner.py                        ← 本地仿真入口（VUnit 包装）
├── test_runner_ci_cd.py                  ← CI 专用仿真入口
└── vhdl_utils/                           ← git 子模块（utils_pkg / tb_utils）
```

`README.md` 也给了一张简化版的目录树，可以对照阅读：

- [README.md:53-69](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L53-L69)：README 的「Library Structure」章节，按功能大类列出 `ip/` 的一级子目录，并对每个目录写了一行中文/英文注释。

注意，README 这张树是**精简版**，和真实磁盘结构有几处差异，读源码时要认真实结构为准：

| README 树中写的 | 真实情况 | 说明 |
| --- | --- | --- |
| `utils/` | 实际是 `vhdl_utils/`（git 子模块） | README 简写；真实目录是子模块 |
| 未列出 `tb/` 子目录 | 每个 IP 都有 `tb/` 文件夹 | README 只画到一级，没展开测试台 |
| 未列出 `docs/` | `memories/fifo/docs/` 存在 | 里面是设计图 |
| 未列出 `memories_pkg.vhd` | 它在 `memories/` 顶层 | 存储类共享的包 |

#### 4.1.3 源码精读：`memories/` 的三级分类

`memories/` 是全库最复杂的一级目录，按存储原语类型再分三层，理解它就能抓住存储类 IP 的全貌：

- **`fifo/`** —— 先进先出缓冲。含 `fifo_sync`（同步 FIFO，单时钟域）和 `fifo_async`（异步 FIFO，跨时钟域）。
- **`ram/`** —— 随机读写存储。又细分为 `single_port/`（单口）、`dual_port/`（双口）两个子文件夹，外加一个直接放在 `ram/` 下的 `dual_clock_dual_port_ram.vhd`（双时钟双口，给异步 FIFO 当存储底座）。
- **`rom/`** —— 只读存储，支持从文件加载初值。

三类之上还有一个**共享包** `memories_pkg.vhd`，它定义了 `rom_t` 等公共类型，被 `fifo/ram/rom` 共同复用。把包放在 `memories/` 顶层（而不是塞进某一个子文件夹），是因为它属于整类共享，下一单元会专门讲它。

#### 4.1.4 代码实践：核对真实目录树

1. **实践目标**：验证你看到的树和磁盘一致，并发现 README 简化树的遗漏。
2. **操作步骤**：在仓库根目录运行只读命令，列出真实文件：

   ```bash
   git ls-files ip/
   ```

   或直接用文件管理器展开 `ip/`。
3. **需要观察的现象**：`git ls-files` 的输出与上面 4.1.2 的树逐行对应。
4. **预期结果**：你会看到 README 没画出来的 `tb/` 文件夹、`memories_pkg.vhd`、`fifo/docs/` 都真实存在；而 README 写的 `utils/` 实际不存在，对应的是 `vhdl_utils/` 子模块。
5. 结论：**读源码时认 `git ls-files` 的真实结构，不认 README 的简化树**。

#### 4.1.5 小练习与答案

**练习 1**：`dual_clock_dual_port_ram.vhd` 放在哪一级？为什么它没有和 `single_port/`、`dual_port/` 一样有自己的子文件夹？

> **答案**：它直接放在 `ip/memories/ram/` 下，与 `single_port/`、`dual_port/` 平级，**没有自己的 `tb/` 测试台**。因为它在工程里只作为异步 FIFO 的内部存储底座被复用，靠 FIFO 的测试台间接覆盖（详见 [u9 FIFO 设计](u9-l1-sync-fifo-behavioral.md)）。

**练习 2**：`spi_pkg.vhd` 放在 `communication/spi/` 而不是 `memories/`，这说明了什么命名直觉？

> **答案**：包文件**就近放在使用它的模块同级目录**。`memories_pkg.vhd` 服务于整个存储大类所以放在 `memories/` 顶层；`spi_pkg.vhd` 只服务于 SPI 模块，所以放在 `communication/spi/`。

### 4.2 每个 IP 的「设计源码 + `tb/`」命名约定

#### 4.2.1 概念说明

本库对每个 IP 的文件组织有**严格约定**，让你只要知道 IP 名，就能预测它的所有相关文件名。这个约定是：

> **一个 IP = 一个设计文件 + 一个 `tb/` 文件夹**。`tb/` 文件夹里放同名前缀的测试台与波形脚本。

#### 4.2.2 核心流程：命名规则与「三件套」

命名规则可以用三条公式般的规则概括（`<ip>` 代表 IP 名，例如 `debouncer`）：

| 角色 | 文件名 | 放在哪 | 是否综合 |
| --- | --- | --- | --- |
| 设计源码 | `<ip>.vhd` | IP 目录根（如 `debouncer/debouncer.vhd`） | ✅ 综合 |
| 测试台 | `tb_<ip>.vhd` | `tb/` 子文件夹（如 `debouncer/tb/tb_debouncer.vhd`） | ❌ 仅仿真 |
| 波形脚本 | `tb_<ip>.do` | `tb/` 子文件夹（如 `debouncer/tb/tb_debouncer.do`） | ❌ 仅仿真 |

即「**设计在根、测试进 `tb/`、都加 `tb_` 前缀**」。

这套命名还有一个隐藏好处：`test_runner.py` 用递归通配符自动发现测试台时，匹配的就是 `tb_*.vhd`。README 对此有明确说明：

- [README.md:268-273](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L268-L273)：脚本「在 `./ip/` 里查找测试台，递归匹配 `tb_*.vhd`（pattern `**`）」。所以只要你的测试台叫 `tb_xxx.vhd` 且放在 `ip/` 下，就会被自动跑起来。

#### 4.2.3 源码精读：以 `debouncer` 为例对照

`debouncer` 是最干净的单文件 IP，正好用来印证约定：

```text
ip/debouncer/
├── debouncer.vhd          ← 设计源码（<ip>.vhd）
└── tb/
    ├── tb_debouncer.do     ← 波形脚本（tb_<ip>.do）
    └── tb_debouncer.vhd    ← 测试台（tb_<ip>.vhd）
```

- [ip/debouncer/debouncer.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/debouncer.vhd)：设计源码，会被综合。
- [ip/debouncer/tb/tb_debouncer.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/tb/tb_debouncer.vhd)：测试台，只仿真。

**一个目录里有多个设计文件时**（如 `communication/spi/` 和 `ff_synchroniser/`），约定依然成立：每个设计文件 `<name>.vhd` 在 `tb/` 里都有对应的 `tb_<name>.vhd`。例如 `ff_synchroniser/` 同时有 `ff_synchroniser.vhd` + `ff_synchroniser_vector.vhd` 两个设计，但只有单比特版有测试台 `tb_ff_synchroniser.vhd`（多比特版靠异步 FIFO 的测试间接覆盖）。

#### 4.2.4 代码实践：预测并验证文件名

1. **实践目标**：内化命名约定，做到「只听 IP 名就能报出文件名」。
2. **操作步骤**：
   1. 取一个 IP，例如 `reset_on_startup`。
   2. 凭规则预测它的三个文件路径（设计、测试台、波形脚本）。
   3. 用 `git ls-files ip/reset_on_startup/` 验证。
3. **需要观察的现象**：你预测的三件套是否都真实存在。
4. **预期结果**：应看到 `reset_on_startup.vhd`、`tb/tb_reset_on_startup.vhd`、`tb/tb_reset_on_startup.do` 三者齐全。
5. **例外**：再对 `clock_enable` 做一次同样预测——你会发现它**只有 `clock_enable.vhd`，没有 `tb/` 文件夹**。这是因为 `clock_enable` 作为时钟门控原语，被 `spi_tx` 等模块在内部例化，靠使用方的测试台间接验证。**这是本库最重要的一个约定例外**，读源码时要注意。

#### 4.2.5 小练习与答案

**练习 1**：新建一个叫 `my_fifo` 的 IP，按本库约定，它的测试台和波形脚本分别该叫什么、放哪？

> **答案**：测试台 `ip/my_fifo/tb/tb_my_fifo.vhd`，波形脚本 `ip/my_fifo/tb/tb_my_fifo.do`，设计 `ip/my_fifo/my_fifo.vhd`。命名严格遵守「`tb_` 前缀 + 同名」，这样 `test_runner.py` 才能自动发现它。

**练习 2**：为什么要把测试台放进 `tb/` 子文件夹，而不是和设计文件平级？

> **答案**：物理隔离「会被综合的设计」与「只用于仿真的测试台」，便于一眼区分、便于工具筛选（`tb_*.vhd` 通配）、也避免把测试文件误加入综合文件列表。

### 4.3 `.do` 波形脚本

#### 4.3.1 概念说明

`.do` 文件是 **ModelSim / QuestaSim 仿真器的 Tcl 命令脚本**，作用是「告诉仿真器在波形窗口里显示哪些信号、怎么分组、怎么排版」。它本身和 VHDL 无关，纯属于仿真工具的操作脚本。

> 术语：**Tcl**（读作 tickle）是一种脚本语言，EDA 工具普遍用它做自动化；`add wave` 是 ModelSim 里「往波形窗口加一根信号线」的命令。

每个测试台 `tb_<ip>.vhd` 都配一个同名的 `tb_<ip>.do`，构成「跑测试 + 看波形」的一对。

#### 4.3.2 核心流程：`.do` 脚本的典型结构

一个 `.do` 脚本通常做三件事：

1. **加分隔线**（`add wave -divider <名字>`）：在波形窗口插入一条命名分组横线，把信号按逻辑分区。
2. **加信号**（`add wave -noupdate <信号路径>`）：把某根信号线加入窗口，可选 `-radix binary/unsigned` 指定显示进制。
3. **排版**（`configure wave ...`、`WaveRestoreZoom ...`）：设置列宽、网格、缩放区间等显示细节。

本库的 `.do` 脚本有一个固定分组习惯：用 divider 把信号分成 **「DuT（被测器件）— Interface（接口）— Internal（内部）— tb Internal（测试台内部）」** 几段，内部信号还用 `-group` 进一步嵌套。

#### 4.3.3 源码精读：`tb_spi_tx.do`

以 SPI 发送测试台的波形脚本为例，看真实的 divider 与 group 用法：

- [ip/communication/spi/tb/tb_spi_tx.do:3-4](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.do#L3-L4)：用两条 `add wave -divider` 开出 `DuT` 和 `Interface` 两个分区。

  ```tcl
  add wave -noupdate -divider DuT
  add wave -noupdate -divider Interface
  ```

- [ip/communication/spi/tb/tb_spi_tx.do:5-13](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.do#L5-L13)：在 `Interface` 区下挂测试台对外接口信号，并用 `-radix` 指定进制（如 `selected_chips` 用二进制、`tx_data` 用无符号十进制）。
- [ip/communication/spi/tb/tb_spi_tx.do:14-23](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.do#L14-L23)：再开一个 `Internal` divider，下面用 `-expand -group fsm` 把被测器件（`DUT`）内部的有限状态机信号（`state`、`bit_index`、`current_chip_index` 等）**嵌套成一组**，方便整体折叠/展开。

  ```tcl
  add wave -noupdate -divider Internal
  add wave -noupdate -expand -group fsm /tb_spi_tx/DUT/spi_fsm/state
  ```

注意信号路径的写法：`/tb_spi_tx/DUT/spi_fsm/state` 表示「测试台 `tb_spi_tx` 里例化的器件 `DUT`，其内部 `spi_fsm` 块的 `state` 信号」。这种「**越过分隔线一路点到内部信号**」的能力，正是调试时定位 bug 的关键。

#### 4.3.4 代码实践：读懂一个 `.do` 脚本的分区

1. **实践目标**：能看懂任意一个 `.do` 脚本把信号分成了哪几段、哪些内部信号被探查。
2. **操作步骤**：
   1. 打开 [ip/communication/spi/tb/tb_spi_tx.do](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.do)。
   2. 数出共有几条 `add wave -divider`，分别叫什么名字。
   3. 找到 `-group fsm`，列出它下面挂了哪些 `DUT/spi_fsm/...` 内部信号。
3. **需要观察的现象**：divider 数量与 group 嵌套层级。
4. **预期结果**：共 4 条 divider（`DuT`、`Interface`、`Internal`、`tb - Internal`）；`group fsm` 下挂了 `state`、`bit_index`、`current_chip_index`、`selected_chips_reg`、`tx_data_reg` 五个状态机内部寄存器。
5. 待本地验证：如果你本地装了 QuestaSim/ModelSim，可在 GUI 模式（`gui=True`）下加载该脚本，确认波形分组与预期一致。

#### 4.3.5 小练习与答案

**练习 1**：`add wave -noupdate -radix unsigned /tb_spi_tx/tx_data` 中的 `-radix unsigned` 是什么意思？

> **答案**：让这根多比特信号在波形窗口里**按无符号十进制显示**数值，而不是默认的二进制 `1010...`，方便人眼读出大小。

**练习 2**：为什么 `.do` 脚本要用 `-divider` 把信号分区，而不是一股脑全列出来？

> **答案**：测试台往往要观察十几甚至几十根信号，全平铺会淹没重点。按「接口/内部/测试台」分区、对内部寄存器再 `-group` 嵌套，能让你**按职责快速定位**想看的信号，调试时也能整体折叠某一段。

## 5. 综合实践

**任务**：不借助任何代码搜索工具，凭本讲讲的目录约定，**手工画出** `memories`、`communication/spi`、`ff_synchroniser` 三个分支的文件结构图，并给每个文件标注它属于「设计」还是「测试」。

**步骤**：

1. 拿一张纸，写下这三个分支的根目录：`ip/memories/`、`ip/communication/spi/`、`ip/ff_synchroniser/`。
2. 应用本讲的规则逐层展开：
   - 设计文件 `<name>.vhd` 标注「设计」。
   - `tb/tb_<name>.vhd` 标注「测试台」。
   - `tb/tb_<name>.do` 标注「波形脚本」。
   - 包文件（`*_pkg.vhd`）标注「设计（共享包）」。
   - 图片（`docs/*.svg`）标注「设计图」。
3. 画完后用 `git ls-files ip/memories ip/communication/spi ip/ff_synchroniser` 核对。

**预期结果**（参考答案，标注格式 `文件名 [设计/测试/包/图/脚本]`）：

```text
memories/
├── memories_pkg.vhd                       [设计·共享包]
├── fifo/
│   ├── fifo_async.vhd                     [设计]
│   ├── fifo_sync.vhd                      [设计]
│   ├── docs/async_fifo.drawio.svg         [设计图]
│   └── tb/{tb_fifo_async.vhd, .do,
│            tb_fifo_sync.vhd, .do}        [测试·台/脚本]
├── ram/
│   ├── dual_clock_dual_port_ram.vhd       [设计·无独立 tb]
│   ├── dual_port/{dual_port_ram.vhd,      [设计]
│                  tb/tb_dual_port_ram.vhd,.do}  [测试]
│   └── single_port/{single_port_ram.vhd,  [设计]
│                    tb/tb_single_port_ram.vhd,.do}  [测试]
└── rom/
    ├── rom.vhd                            [设计]
    └── tb/{tb_rom.vhd, tb_rom.do}         [测试]

communication/spi/
├── spi_interface.vhd                      [设计]
├── spi_pkg.vhd                            [设计·包]
├── spi_rx.vhd, spi_tx.vhd                 [设计]
└── tb/{tb_spi_interface, tb_spi_rx,       [测试]
        tb_spi_tx}.vhd 及同名 .do

ff_synchroniser/
├── ff_synchroniser.vhd                    [设计]
├── ff_synchroniser_vector.vhd             [设计·无独立 tb]
└── tb/{tb_ff_synchroniser.vhd, .do}       [测试]
```

**自检要点**：你能否解释为什么 `dual_clock_dual_port_ram.vhd`、`ff_synchroniser_vector.vhd`、`clock_enable.vhd` 都没有自己的 `tb/`？答案是它们都作为子模块被其它 IP 复用，靠使用方的测试台间接覆盖。能想清楚这一点，说明你已经真正掌握了本库的 IP 组织逻辑。

## 6. 本讲小结

- 所有 IP 核集中在 **`ip/`** 下，按功能大类分一级子目录；`memories/` 再分 **`fifo / ram / rom`** 三级。
- 每个 IP 遵循**「设计 `<ip>.vhd` + `tb/tb_<ip>.vhd` 测试台 + `tb/tb_<ip>.do` 波形脚本」**三件套约定，命名带 `tb_` 前缀、测试进 `tb/` 子文件夹。
- 这套命名让 `test_runner.py` 能用 `tb_*.vhd` 通配符**自动发现并运行**全部测试台。
- 包文件**就近放置**：`memories_pkg.vhd` 在 `memories/` 顶层（大类共享），`spi_pkg.vhd` 在 `spi/` 下（模块专用）。
- `.do` 是 ModelSim/QuestaSim 的 Tcl 波形脚本，用 `add wave -divider` 分区、`-group` 嵌套，把信号按「接口/内部/测试台」分层展示。
- **重要例外**：`clock_enable`、`dual_clock_dual_port_ram`、`ff_synchroniser_vector` 没有独立测试台，靠使用方间接覆盖；README 的目录树是简化版，读源码以 `git ls-files` 真实结构为准。

## 7. 下一步学习建议

- 本讲让你「会找文件」，下一讲 [u1-l3 开发环境搭建与本地仿真运行](u1-l3-environment-and-simulation.md) 教你「会跑测试」——结合 `test_runner.py` 把这些 `tb_*.vhd` 真正运行起来。
- 之后进入 [第 2 单元：核心设计模式](u2-l1-multi-architecture-pattern.md)，打开任意一个 `<ip>.vhd`，深入理解「同一 entity + 多厂商 architecture」的设计精髓。
- 建议顺手打开 [ip/memories/memories_pkg.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/memories_pkg.vhd) 通读一遍，它是后续存储类讲义反复引用的共享包。
```
