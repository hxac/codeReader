# 厂商仿真库与库声明

## 1. 本讲目标

上一讲（u2-l1）我们已经看清本库的核心设计模式——「同一 entity 配多套 architecture」，并知道 Xilinx 实现 `xilinx_behavioural_*` 内部会例化 `xpm` 原语、Intel 实现 `intel_behavioural_*` 内部会例化 `altera_mf` 原语。但这里藏着一个上一讲刻意绕开的问题：**你写下 `xpm_cdc_single` 只是一句例化，真正在仿真里跑起来的「行为」从哪里来？** 答案就是本讲的主角——厂商仿真库（vendor simulation library）。

学完本讲，你应当能够：

- 说出 `xpm`、`altera_mf`、`unisim` 分别属于哪家厂商、各自提供哪一类原语，并能据其选择对应的库。
- 解释 README 警告里 `Failed to find 'glbl' in hierarchical name 'glbl.GSR'` 报错的成因，以及 `use_xilinx_libs=True` 是如何消除它的。
- 读懂 `vhdl_ls.toml` 如何为编辑器声明这些库、CI 流水线又如何为 NVC 仿真器提供这些库的源码。
- 理解同一份「厂商库」要在编辑器、本地仿真器、CI 仿真器三处分别「被供给」的工程现实。

## 2. 前置知识

本讲假设你已经掌握以下概念（来自前置讲义）：

- **综合与仿真的区别**（u1-l2）：设计源码 `.vhd` 会被综合工具映射成真实电路，而测试台只在仿真里运行。厂商原语是这两条路径的交汇点——它既对应一块真实硬件，又需要一个仿真行为模型。
- **厂商原语与多架构模式**（u2-l1）：`xilinx_behavioural_*` 架构内部例化 Xilinx `xpm` 原语，`intel_behavioural_*` 架构内部例化 Intel `altera_mf` 原语；`own_behavioural_*` 不依赖任何厂商库。
- **`library` / `use` 子句**：VHDL 用 `library xpm; use xpm.vcomponents.all;` 这样的上下文子句，把一个外部库的名字引入当前作用域。这些库不会凭空存在，必须由工具链「编译并注册」后才能被引用。
- **混合语言仿真**：Xilinx / Intel 的厂商原语仿真模型大多是 **Verilog** 写的，而本项目代码是 VHDL-2008。能同时编译两种语言的仿真器（如 ModelSim/QuestaSim）称为「混合语言仿真器」；只能编译 VHDL 的仿真器（如 NVC）则需要 **纯 VHDL** 的行为模型替代——这点是后面 CI 章节的关键。

一句话点题：**厂商库就是「原语的行为模型仓库」。你的 RTL 只负责「点菜」（例化原语），厂商库负责「上菜」（提供仿真行为 + 综合映射）。本讲讲的就是这张「菜单」怎么登记、怎么上菜。**

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 角色 | 本讲用它讲什么 |
| --- | --- | --- |
| `vhdl_ls.toml` | 编辑器（VHDL-LS）的库声明 | 把 `unisim`/`xpm`/`altera_mf` 三个厂商库映射到磁盘源码文件，并标记 `is_third_party` |
| `ip/test_runner.py` | 本地仿真包装器 | `use_xilinx_libs=True` 这一关键开关，以及 docstring 里的 glbl 警告 |
| `ip/test_runner_ci_cd.py` | CI 专用仿真包装器 | 在 CI 里同时打开 Xilinx + Intel 库，并用 `excluded_list` 排除跑不了的 PLL |
| `.github/workflows/vunit.yml` | CI 流水线 | 如何 clone grlib/gplgpu 提供 VHDL 厂商库源码，再用 `nvc --install` 编译进缓存 |
| `README.md` | 项目说明 | 「Technology Support」一节的库路径约定，以及 glbl 报错的官方描述 |
| `ip/ff_synchroniser/ff_synchroniser.vhd` | 设计源码（参照） | 真实例化 `xpm_cdc_single` 的样子，作为本讲实践的对象 |

## 4. 核心概念与源码讲解

### 4.1 厂商仿真库：xpm / altera_mf / unisim 是什么

#### 4.1.1 概念说明

当你在 VHDL 里写下一句：

```vhdl
xpm_cdc_single_sync_inst: xpm_cdc_single
    generic map ( ... )
    port map ( ... );
```

你其实只是「点了一个名字」。`xpm_cdc_single` 并不是 VHDL 语言自带的东西，它是 **Xilinx 提供的一个参数化宏（XPM，Xilinx Parameterized Macros）**。这句话要能通过编译并仿真，必须有人把 `xpm_cdc_single` 的「实现」递给仿真器——这个「实现仓库」就是厂商仿真库。

本库会涉及三个厂商库，先建立一张「身份对照表」：

| 库名 | 厂商 | 全称 | 提供什么 | 典型原语举例 |
| --- | --- | --- | --- | --- |
| `unisim` | Xilinx | Unified Library（统一库） | FPGA 最底层硬件原语（LUT、FF、BUFG、BRAM、IOB…）的仿真模型 | `BUFGCE`、`BUFG`、`PLLE2_BASE` |
| `xpm` | Xilinx | Xilinx Parameterized Macros | 参数化的中高层宏（CDC、FIFO、内存），内部封装 unisim 原语并附带约束 | `xpm_cdc_single`、`xpm_cdc_array_single`、`xpm_fifo_sync` |
| `altera_mf` | Intel/Altera | Altera MegaFunction | Intel 侧的参数化宏（FIFO、PLL、内存、LPM） | `scfifo`、`dcfifo`、`altclklock` |

三者的关系可以这样理解：

- `unisim` 是「原子」（最底层的硬件砖块）。
- `xpm` 是「分子」（把原子拼好、加上时序约束的成品件），位置上更高一层，内部会用到 `unisim`。
- `altera_mf` 是 Intel 阵营对等的「分子库」，和 `xpm`/`unisim` 平行，不互通。

> 术语解释——**原语（primitive）**：厂商已经替你实现好的、对应一块确定硬件的底层模块。你只需例化它，综合工具就知道该映射成什么电路。

> 术语解释——**XPM**：Xilinx 官方推荐的「现代例化方式」，用以替代旧的手写 RAM/FIFO RTL。它的好处是厂商保证推断正确、自带约束，缺点是强绑定 Xilinx。这正是本库 `own_behavioural_*` 架构存在的价值——当你要脱离 Xilinx 时改用它。

#### 4.1.2 核心流程

理解厂商库，关键是区分**两条路径**对同一原语的不同需求：

```
        你写的 RTL（例化 xpm_cdc_single）
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
    仿真路径                   综合路径
        │                       │
  需要「行为模型」         需要「硬件映射规则」
  （厂商仿真库）          （综合工具内置知识）
        │                       │
  由 xpm.vcomponents.all     由 Vivado/Quartus
  指向的库提供行为           直接推断成电路
```

- **仿真路径**：仿真器不认识 `xpm_cdc_single`，它需要这个原语的「行为模型」（一段描述它在时间轴上如何动作的代码）。这段代码就放在厂商仿真库里，本讲的主角。
- **综合路径**：综合工具（Vivado / Quartus）自带「这个原语对应什么硬件」的知识，所以综合时根本不需要你提供仿真库——它直接把原语映射成真实电路。

所以一个常见的初学者困惑是：「我在 Vivado 里综合好好的，怎么一到 ModelSim 仿真就报错？」答案正是：综合工具自带原语知识，而仿真器需要你额外把**厂商仿真库**喂给它。`use_xilinx_libs=True` 就是干这件事的开关。

#### 4.1.3 源码精读

先看设计源码里「点菜」的真实样子。`ff_synchroniser.vhd` 在 `xilinx_behavioural_ff_synchroniser` 架构之前声明了 Xilinx 库：

[ip/ff_synchroniser/ff_synchroniser.vhd:35-36](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L35-L36) — 在该 architecture 之前声明 `library xpm; use xpm.vcomponents.all;`，这正是 u2-l1 讲过的「厂商库声明紧贴各自 architecture 之前、使依赖局部化」的体现。注意这两行只出现在 xilinx 架构前，Intel/自研架构前没有，这就是「依赖局部化」的源码证据。

[ip/ff_synchroniser/ff_synchroniser.vhd:45-57](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L45-L57) — 例化 `xpm_cdc_single`，这是 XPM 库里的「单比特跨时钟域同步宏」。这一句要能仿真，前提是仿真器已经拿到了 `xpm` 库（里面有 `xpm_cdc_single` 的行为模型）。

本库中其它真实的厂商库引用点（验证了三个库都在用）：

- [ip/ff_synchroniser/ff_synchroniser_vector.vhd:32](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser_vector.vhd#L32) — `use xpm.vcomponents.all;`，配套例化 `xpm_cdc_array_single`（多比特同步）。
- [ip/memories/fifo/fifo_sync.vhd:28](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L28) 与 [ip/memories/fifo/fifo_sync.vhd:104-105](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L104-L105) — 同一文件内既有 `xpm`（Xilinx FIFO）又有 `altera_mf`（Intel `scfifo`），是「同一 entity 多架构」的集中体现。
- [ip/clock_enable/clock_enable.vhd:23](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L23) — `use unisim.vcomponents.all;`，例化 `BUFGCE`（全局时钟门控原语，属于 unisim 而非 xpm）。
- [ip/pll/pll.vhd:28](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/pll.vhd#L28) 与 [ip/pll/pll.vhd:68-69](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/pll.vhd#L68-L69) — PLL 同时引用 `unisim`（`PLLE2_BASE`）与 `altera_mf`（`altclklock`）。

再看编辑器侧如何把这些「库名」落到「文件」。`vhdl_ls.toml` 是 VHDL-LS 语言服务器的配置，它用「库名 → 文件 glob」的方式登记每个库：

[vhdl_ls.toml:35-41](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/vhdl_ls.toml#L35-L41) — `UNISIM.files` 列出了 unisim 库对应的 VHDL 源文件路径，注意它同时给了 Windows（`C:/Xilinx/...`）和 Linux（`/opt/...`）两套候选，并带 `# NOTE: Set the correct path!` 提示要按本机改。

[vhdl_ls.toml:43-49](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/vhdl_ls.toml#L43-L49) — `xpm.files` 登记的是 Xilinx 的 **XPM VHDL 封装**（注意后缀是 `.vhd`，是 VHDL 源，不是 Verilog），路径在 `data/ip/xpm/`，与 unisim 的 `data/vhdl/src/` 不同。

[vhdl_ls.toml:51-57](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/vhdl_ls.toml#L51-L57) — `altera_mf.files` 指向 Intel 的 `altera_mf_components.vhd`，这是 Intel 的「组件声明包」（声明了所有 megafunction 的端口，供 VHDL 例化）。

[vhdl_ls.toml:16](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/vhdl_ls.toml#L16)、[vhdl_ls.toml:41](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/vhdl_ls.toml#L41)、[vhdl_ls.toml:49](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/vhdl_ls.toml#L49)、[vhdl_ls.toml:57](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/vhdl_ls.toml#L57) — 每个库都有 `is_third_party = true`。这个标记告诉 VHDL-LS：这些库不归本项目维护，做静态检查（lint）时只供「跳转/补全」，不要拿本项目的严格规则去挑它们的错。本项目的代码则归 `defaultlib`（见 [vhdl_ls.toml:31-33](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/vhdl_ls.toml#L31-L33)），接受完整检查。

#### 4.1.4 代码实践

**实践目标**：建立「原语 → 所属厂商库」的直觉，为后面排错打基础。

**操作步骤**：

1. 打开 README 的「Technology Support」一节，对照 [README.md:312-327](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L312-L327) 的支持表。
2. 用前面「4.1.3 源码精读」给出的真实例化点，自己填一张表（示例代码，请用真实文件核对）：

   | 原语 | 出现在哪个文件 | 属于哪个厂商库 | 厂商 |
   | --- | --- | --- | --- |
   | `xpm_cdc_single` | ff_synchroniser.vhd | xpm | Xilinx |
   | `xpm_cdc_array_single` | ff_synchroniser_vector.vhd | xpm | Xilinx |
   | `scfifo` | fifo_sync.vhd | altera_mf | Intel |
   | `BUFGCE` | clock_enable.vhd | unisim | Xilinx |
   | `PLLE2_BASE` | pll.vhd | unisim | Xilinx |
   | `altclklock` | pll.vhd | altera_mf | Intel |

3. 找一个 `own_behavioural_*` 架构（如 fifo_sync 的自研实现），确认它的 architecture 前面**没有**任何 `library xpm` / `library altera_mf` 声明。

**需要观察的现象**：`xpm` 和 `unisim` 都属于 Xilinx，但 `xpm` 出现在「分子层」（CDC/FIFO），`unisim` 出现在「原子层」（时钟缓冲、PLL）；`altera_mf` 则同时覆盖 Intel 的「分子」与部分「原子」。

**预期结果**：你能不看资料，凭原语名猜出它属于哪个厂商、哪个库；并能一眼看出某段 RTL 是否「厂商绑定」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `xpm_cdc_single`（CDC 同步器）能被本库的 `own_behavioural_ff_synchroniser`（如果存在自研版）替代，而 `PLLE2_BASE`（PLL）却没有自研版？

**参考答案**：CDC 同步器本质上就是一串触发器链，可以用纯 RTL 行为级描述，因此能写出自研版；而 PLL 是 FPGA 里专用的硬核模拟资源（锁相环），不可能用可综合的数字 RTL 复现，所以无法提供 `own_behaviourral_*` 实现。这正是 README 表格里 PLL 那一列 Own/Behavioral 是「No」的根本原因（见 [README.md:322](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L322)）。

**练习 2**：`vhdl_ls.toml` 里 `xpm.files` 指向的是 `.vhd` 文件，但 4.2 节会说 Xilinx 原语仿真模型是 Verilog 写的。这两者矛盾吗？

**参考答案**：不矛盾。`data/ip/xpm/` 下的 `.vhd` 是 Xilinx 提供的 **XPM VHDL 封装/声明**，作用是让 VHDL 代码能 `use xpm.vcomponents.all` 并例化这些宏；而封装内部引用的真实仿真行为模型（在 `unisims_ver` 等 Verilog 库里）才是 Verilog。编辑器（VHDL-LS）只需要 VHDL 封装就能做跳转和补全，不必看 Verilog 内部。

---

### 4.2 use_xilinx_libs 与 glbl 模块报错

#### 4.2.1 概念说明

这是本讲最实战的一节：解决一个几乎所有 Xilinx 仿真新手都会撞到的报错——

```
Failed to find 'glbl' in hierarchical name 'glbl.GSR'
Error loading design
```

要理解它，先认识 Xilinx 仿真模型里的一个特殊模块 **`glbl`（global，全局模块）**。

Xilinx 的原语仿真模型（用 Verilog 写的）在描述上电行为时，会去读一组「仿真全局信号」，它们被集中声明在一个名为 `glbl` 的模块里，最重要的两个是：

- **`GSR`（Global Set/Reset，全局置位/复位）**：模拟 FPGA 专用的全局复位网络。仿真时刻 0 时 `GSR` 会先有效一小段时间，把所有原语（触发器、BRAM……）驱动到它们的 `INIT` 初值，复现真实 FPGA 上电的过程。
- **`GTS`（Global Tri-State，全局三态）**：全局输出使能控制。

也就是说，Xilinx 原语在仿真里会写类似 `glbl.GSR` 这样的**跨模块层次引用**（hierarchical reference）去拿那个全局复位信号。这就是 `glbl` 模块存在的意义：它是 Xilinx 仿真模型的「上电复位总机」。

#### 4.2.2 核心流程

报错的成因与修复，可以画成一条因果链：

```
你在 tb 里例化了含 XPM/UNISIM 原语的 DUT
        │
        ▼
原语的 Verilog 仿真模型里写了 glbl.GSR
        │
        ▼
仿真器（ModelSim/QuestaSim）试图解析 glbl.GSR
        │
        ▼
  glbl 模块没被编译/加载？
        │
   ┌────┴────┐
   ▼         ▼
  是        否
   │         │
   ▼         ▼
 报错         正常仿真
 glbl.GSR
```

修复方法：把 `glbl` 模块（来自 Xilinx 的 `glbl.v`）连同必要的预编译库一起加载。本库把这个动作封装进了 `run_all_testbenches_lib` 的 `use_xilinx_libs` 开关。当它为 `True` 时，包装器会向仿真命令追加 Xilinx 库并加载 glbl。README 用一句话点明了它实际做的事：

> The flag automatically includes the Xilinx `glbl` module and required simulation libraries (`-L xpm -L unisims_ver -L secureip`).

这里三个 `-L` 选项的含义：

- `-L xpm`：把 XPM 预编译库加入搜索路径（提供 `xpm_cdc_single` 等的行为模型）。
- `-L unisims_ver`：注意 `_ver` 后缀，指 **Verilog 版**的 UNISIM 库（提供 `BUFGCE`、`PLLE2_BASE` 等的 Verilog 仿真模型，其中就引用了 `glbl.GSR`）。
- `-L secureip`：部分原语（如 PLL、加密 IP）的仿真模型以加密形式提供，放在 `secureip` 库里。

> 关键洞察：`use_xilinx_libs=True` 做的是**两件事**，不只是「加库」——既要加预编译库，又要编译加载 `glbl` 这个特殊模块。缺任何一件，Xilinx 原语仿真都会失败。

#### 4.2.3 源码精读

先看本地仿真包装器 `test_runner.py` 里的关键开关与警告：

[ip/test_runner.py:6-8](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py#L6-L8) — 文件顶部的 docstring 警告：若设计用到 Xilinx 原语（XPM/UNISIM），**必须**设 `use_xilinx_libs=True` 以避免 `glbl` 模块报错。这是开发者最显眼的提示位。

[ip/test_runner.py:28](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py#L28) — 真正的开关：`use_xilinx_libs=True, # Add Xilinx simulation libraries, note set it true to load glbl module`。行尾注释直接点明「设为 True 是为了加载 glbl 模块」。注意这是传给 `run_all_testbenches_lib`（来自 vhdl_utils 子模块）的参数，真正的「加库 + 加载 glbl」逻辑在那个子模块里（本仓库未检入其源码，标注为待确认）。

再看 README 对同一个问题的官方描述，措辞更完整：

[README.md:295-302](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L295-L302) — 给出了两类报错的原文（`Failed to find 'glbl' in hierarchical name 'glbl.GSR'` 与 `Error loading design`），并列出该开关自动附加的三个库（`-L xpm -L unisims_ver -L secureip`）。这是你遇到报错时最权威的排查依据。

> 小心一个「文档漂移」陷阱：README 的「Optional Customization」示例（[README.md:288](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L288)）里把 `use_xilinx_libs=False`，但磁盘上的真实 `test_runner.py` 默认是 `True`（见上面 [test_runner.py:28](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py#L28)）。这与 u1-l3 提到的「README 示例与真实文件不一致，以真实文件为准」一致：要复现/修复 glbl 报错，请改 `test_runner.py`，而不是照抄 README 的定制示例。

#### 4.2.4 代码实践

**实践目标**：亲手复现并修复 `glbl.GSR` 报错，建立肌肉记忆。

**操作步骤**：

1. **准备一个最小测试台**（示例代码，非项目原有文件）：

   ```vhdl
   -- 示例代码：tb_glbl_demo.vhd（演示用，需自行放进某 IP 的 tb/ 目录或单独管理）
   library ieee;
   use ieee.std_logic_1164.all;
   library vunit_lib;
   use vunit_lib.com_types_pkg.all;
   use vunit_lib.run_pkg.all;

   entity tb_glbl_demo is
     generic (runner_cfg : runner_cfg_t);
   end entity;

   architecture sim of tb_glbl_demo is
     signal src_clk, dst_clk, din, dout : std_ulogic := '0';
   begin
     -- 直接例化 Xilinx CDC 同步原语
     dut: entity work.ff_synchroniser(xilinx_behavioural_ff_synchroniser)
       generic map (SYNC_SHIFT_FF => 2)
       port map (source_clk => src_clk, destination_clk => dst_clk,
                 source_domain => din, destination_domain => dout);

     main : process is
     begin
       test_runner_setup(runner, runner_cfg);
       wait for 1 us;
       check_equal(dout, '0', "默认稳态");
       test_runner_cleanup(runner);
     end process;
   end architecture;
   ```

2. **第一次跑（复现报错）**：临时把 `ip/test_runner.py` 的 `use_xilinx_libs` 改为 `False`，运行 `./.venv/Scripts/python.exe ./ip/test_runner.py`（或 Linux 下对应命令）。
3. **第二次跑（修复）**：把它改回 `True`，再次运行。

**需要观察的现象**：

- 第一次：编译阶段可能通过，但**加载设计（elaboration/加载）**阶段报 `Failed to find 'glbl' in hierarchical name 'glbl.GSR'`，随后 `Error loading design`，测试台根本没跑起来。
- 第二次：库被预编译加载、`glbl` 模块被纳入仿真，测试台正常运行，`check_equal` 通过。

**预期结果**：记录两次的输出差异——重点不是「通过/失败」，而是「**失败发生在加载阶段而非测试阶段**」。这能帮你以后快速判断：凡是在设计加载期就报 `glbl` 的，一律是 `use_xilinx_libs` 没开或厂商库未编译。

> 待本地验证：本实践需要一个已编译好 Xilinx 预编译库的 ModelSim/QuestaSim 环境。若你只有开源 NVC，无法复现此报错（NVC 走的是纯 VHDL 行为模型路线，见 4.3）。在 EDA Playground 上用 RivieraPro 可复现。

#### 4.2.5 小练习与答案

**练习 1**：报错信息是 `glbl.GSR` 而不是某个具体的原语名（如 `xpm_cdc_single`），这说明什么？

**参考答案**：说明 `xpm_cdc_single` 本身已经被成功找到了（XPM 库已加载），失败发生在它**内部**的仿真模型去引用全局复位信号 `glbl.GSR` 时。也就是说「库加了一半」——原语模型进来了，但它们依赖的 `glbl` 模块没进来。理解这点能帮你定位：问题不在 `xpm` 库本身，而在 `glbl` 这个全局模块。

**练习 2**：为什么 `own_behaviourral_*` 架构从来不会触发 glbl 报错？

**参考答案**：因为自研行为级实现是纯 RTL，不例化任何 Xilinx 原语，自然不会产生对 `glbl.GSR` 的层次引用。这也呼应了 u2-l1 的结论——`own_behaviourral_*` 是「厂商无关、可开箱仿真」的，是你在没有厂商库环境时最省心的选择。

---

### 4.3 厂商库文件路径约定与多环境供给

#### 4.3.1 概念说明

同样的「厂商库」，要喂给三个不同的消费者，每个消费者要的「形态」还不一样：

| 消费者 | 用途 | 需要的形态 | 配置在哪 |
| --- | --- | --- | --- |
| 编辑器（VHDL-LS / TerosHDL） | 跳转、补全、查符号 | 原始 `.vhd` 源文件路径 | `vhdl_ls.toml` |
| 本地仿真器（ModelSim/QuestaSim） | 编译并仿真 | 预编译好的仿真库（`-L` 加入） | `test_runner.py` 的 `use_xilinx_libs` |
| CI 仿真器（NVC） | 编译并仿真 | 纯 VHDL 源文件（NVC 不吃 Verilog） | `vunit.yml` + `nvc --install` |

这就是为什么 README 的「Technology Support」会专门列出一节「库必须装在哪里」——它在给三个消费者指路。理解「同一份库、三种供给方式」是本节的精髓。

#### 4.3.2 核心流程

三个环境的供给链路并排对比：

```
[编辑器] vhdl_ls.toml 的 globs  ──► 直接读 Vivado/Quartus 安装目录的 .vhd 源
                                       （只是看，不编译）

[本地仿真] test_runner.py
   use_xilinx_libs=True ──► run_all_testbenches_lib 向 vsim 追加
                              -L xpm -L unisims_ver -L secureip + 加载 glbl
                              （依赖 ModelSim 预编译库已存在）

[CI仿真] vunit.yml
   ① clone grlib ──► 拷出 UNISIM 的 VHDL 行为模型（unisim_VPKG/VCOMP）
   ② clone gplgpu ──► 拷出 Intel altera_mf 源
   ③ nvc --install xpm_vhdl / quartus / vivado ──► 编译进 NVC 库缓存
   ④ test_runner_ci_cd.py 跑（排除 tb_pll.vhd / pll.vhd）
```

CI 之所以这么麻烦，根源是一个硬约束：**NVC 是纯 VHDL 仿真器，无法编译 Verilog**，而 Xilinx/Intel 原语的原生仿真模型大多是 Verilog。所以 CI 用了一个迂回——找 **开源的纯 VHDL 行为模型**（来自 GRLIB 项目）来顶替 Verilog 模型。这个迂回能覆盖大部分原语（如 `BUFGCE`、基本同步器），但**覆盖不了 `PLLE2_BASE`**（GRLIB 没有它的开源 VHDL 模型），所以 PLL 必须被排除。

#### 4.3.3 源码精读

先看 README 给出的「库该装在哪」的路径约定：

[README.md:328-338](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L328-L338) — 明确列出：
- Xilinx 库（XPM/UNISIM/UNIMACRO）：Linux CI 在 `/opt/xilinx/vivado/data/vhdl/src/`，Windows 在 `C:\Xilinx\Vivado\<version>\data\vhdl\src\`；XPM 的 VHDL 在 `.../data/ip/xpm/`。
- Intel 库（altera_mf/lpm）：Linux CI 在 `/opt/intelFPGA/<version>/quartus/eda/sim_lib/`，Windows 在 `C:/intelFPGA_pro/<version>/quartus/eda/sim_lib/`。
- 并强调自研行为级实现「永远可用、不需要厂商库」。

再看 CI 流水线如何把这些路径「造」出来。[.github/workflows/vunit.yml:34-79](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L34-L79) 是一个长步骤，分三段：

[.github/workflows/vunit.yml:36-50](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L36-L50) — clone `nselvara/gplgpu`，把 Intel `sim_lib` 拷到 `/opt/intelFPGA/20.1/quartus/eda/`；并用 `sudo touch` 造一批**空文件**（如 `cyclonev_atoms.vhd`）。注释写明是「avoid errors with NVC」——因为 NVC 的 quartus 安装脚本会按清单逐个编译这些器件库文件，缺一个就报错，所以用空文件占位。

[.github/workflows/vunit.yml:54-74](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L54-L74) — clone `nselvara/grlib`，从中拷出 UNISIM 的 VHDL 行为模型（`unisim_VPKG.vhd`、`unisim_VCOMP.vhd`）到 `/opt/xilinx/Vivado/2023.1/...`，再造一批空的 `vhdl_analyze_order`（NVC 的编译顺序清单，缺失会报错）。

[.github/workflows/vunit.yml:77-79](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L77-L79) — 三条 `nvc --install`：`xpm_vhdl`（XPM 的 VHDL 封装）、`quartus`（Intel 库）、`vivado`（UNISIM 行为模型）。`nvc --install <name>` 是 NVC 内置的「安装脚本」，它按厂商库的既定顺序把源文件编译进 NVC 的全局库缓存，之后 VHDL 里的 `library xpm;` 就能解析到。

接着看 CI 专用 runner 如何使用这一切：

[ip/test_runner_ci_cd.py:50-62](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner_ci_cd.py#L50-L62) — 与本地 `test_runner.py` 的关键差异：`use_xilinx_libs=True` **且** `use_intel_altera_libs=True`（CI 同时开两家厂商库，因为 NVC 依赖纯 VHDL 行为模型，不怕混语言），并传入 `excluded_list=["tb_pll.vhd", "pll.vhd"]`。

[ip/test_runner_ci_cd.py:45-48](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner_ci_cd.py#L45-L48) — 排除 PLL 的原因写得清楚：「missing VHDL binding for PLLE2_BASE」。结合前面可知：`PLLE2_BASE` 在 grlib 里没有开源 VHDL 行为模型，NVC 无法仿真，只能排除。

#### 4.3.4 代码实践

**实践目标**：让编辑器（VHDL-LS）在你的 Linux 机器上能正确跳转到厂商原语定义，亲手补全 `vhdl_ls.toml` 里带 `NOTE` 的占位路径。

**操作步骤**：

1. 打开 [vhdl_ls.toml:38-39](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/vhdl_ls.toml#L38-L39)，看到 UNISIM 的 Linux 路径写的是 `/opt/data/vhdl/src/unisims/*.vhd`，旁边注释 `# NOTE: Set the correct path!`。
2. 找到本机 Vivado 安装目录（例如 `ls /opt/Xilinx/Vivado/<版本>/data/vhdl/src/unisims/`），把这两行改成真实路径，如 `/opt/Xilinx/Vivado/2023.1/data/vhdl/src/unisims/*.vhd`。
3. 同样补全 [vhdl_ls.toml:46-47](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/vhdl_ls.toml#L46-L47)（xpm）与 [vhdl_ls.toml:54-55](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/vhdl_ls.toml#L54-L55)（altera_mf，若有 Quartus）。
4. 重启 VSCode / VHDL-LS，在 `ff_synchroniser.vhd` 里对 `xpm_cdc_single` 做「转到定义」。

**需要观察的现象**：补全前，跳转会失败或标红（VHDL-LS 找不到库里的符号）；补全后，应能跳进 XPM 库的 VHDL 封装文件里看到 `xpm_cdc_single` 的组件声明。

**预期结果**：编辑器对厂商原语不再报「unresolved」错误。注意这只解决**编辑器跳转**，不影响仿真——仿真库要靠 `use_xilinx_libs` 或 `nvc --install` 另外解决，印证了「三个消费者、三种供给」。

> 待本地验证：需要本机已安装 Vivado（或至少拷出其 VHDL 源库）。若无 Vivado，可只做「读 `vunit.yml` 中的 `/opt/xilinx/Vivado/2023.1/...` 路径」的源码阅读型实践，理解 CI 是怎么填这些坑的。

#### 4.3.5 小练习与答案

**练习 1**：CI 里为什么要 `sudo touch` 一批空的 `*_atoms.vhd` / `*_components.vhd` 文件？

**参考答案**：`nvc --install quartus` 脚本会按 Intel 器件库的标准清单去逐个编译这些文件，缺一个就报错中断。CI 实际用不到 CycloneV 等具体器件的行为模型（项目只用到 `altera_mf` 这一层 megafunction），但安装脚本仍会扫描它们，于是用空文件占位让脚本顺利跑完。这是一种「绕过工具链硬依赖」的常见 CI 技巧。

**练习 2**：如果有一天 GRLIB 项目新增了 `PLLE2_BASE` 的开源 VHDL 行为模型，CI 的 `excluded_list` 可以怎么改？

**参考答案**：可以把 `tb_pll.vhd` 和 `pll.vhd` 从 `excluded_list` 移除，让 PLL 也进 CI。但前提是 `nvc --install vivado` 能编译进这个新模型，且 `tb_pll` 的断言在 NVC 行为模型下仍然成立（行为模型与真实 PLL 的锁定时序可能有差异，需另行验证）。

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「从报错到闭环」的排查。

**任务**：假设你的同事报告「我在本地 ModelSim 跑 `tb_fifo_sync`，报了一堆 `glbl.GSR` 错误，但 CI 却是绿的」。请你定位并解决。

**建议步骤**：

1. **判断症状归属**：`glbl.GSR` 属于 4.2 讲的 Xilinx 原语加载问题，说明同事的 DUT 用到了 `xilinx_behaviourral_*` 架构（fifo_sync.vhd 里确实有，见 [fifo_sync.vhd:28](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L28) 的 `use xpm.vcomponents.all`）。
2. **查本地开关**：让他确认 `ip/test_runner.py` 的 `use_xilinx_libs` 是否为 `True`（参考 [test_runner.py:28](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py#L28)）。若他照抄了 README 的定制示例（`use_xilinx_libs=False`），这就是病根。
3. **查预编译库**：若开关已开仍报错，说明他的 ModelSim 没有预编译 Xilinx 库（`-L xpm -L unisims_ver -L secureip` 找不到实体）。让他用 Vivado 的 `compile_simlib` 生成这些预编译库。
4. **解释 CI 为何是绿的**：参考 [vunit.yml:54-79](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L54-L79)，CI 用 NVC + GRLIB 的纯 VHDL 行为模型绕开了 Verilog glbl 依赖，所以不踩这个坑——但这只对「GRLIB 有模型的原语」成立，PLL 仍被排除（[test_runner_ci_cd.py:45-48](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner_ci_cd.py#L45-L48)）。
5. **写一张处置清单**：用本讲的术语（库 / 原语 / glbl / 行为模型 / 预编译库）输出一份「问题 → 可能原因 → 验证方法」的三列表。

**预期产出**：一张能放进团队 Wiki 的「Xilinx 仿真库排查指南」，且你能用一句话向同事解释「为什么本地和 CI 对同一份代码的仿真方式根本不同」。

## 6. 本讲小结

- **厂商仿真库 = 原语的行为模型仓库**：`unisim`（Xilinx 原子原语）、`xpm`（Xilinx 参数化宏）、`altera_mf`（Intel megafunction）；你的 RTL 只「点菜」，仿真行为由这些库「上菜」，综合映射则由 Vivado/Quartus 内置知识完成。
- **库声明是局部的**：`library xpm; use xpm.vcomponents.all;` 紧贴在 `xilinx_behaviourral_*` 架构之前，`own_behaviourral_*` 架构前没有厂商库——这正是「厂商无关」的源码证据。
- **`glbl.GSR` 报错的本质**：Xilinx 原语的 Verilog 仿真模型用层次引用 `glbl.GSR` 来模拟上电全局复位，若 `glbl` 模块未被编译加载就报错；`use_xilinx_libs=True` 同时做「加预编译库（`-L xpm -L unisims_ver -L secureip`）」和「加载 glbl 模块」两件事来修复。
- **文档漂移要以源码为准**：README 的定制示例里 `use_xilinx_libs=False`，但磁盘上的 `test_runner.py` 默认 `True`；排查时改真实文件。
- **同一份库、三种供给方式**：编辑器吃 `.vhd` 源路径（`vhdl_ls.toml`）、本地 ModelSim 吃预编译库（`use_xilinx_libs`）、CI 的 NVC 吃纯 VHDL 行为模型（`grlib`/`gplgpu` + `nvc --install`）。
- **NVC 的硬约束决定 CI 策略**：纯 VHDL 仿真器不能编译 Verilog glbl，故 CI 改用开源 VHDL 行为模型，并因此排除没有开源模型的 `PLLE2_BASE`（PLL）。

## 7. 下一步学习建议

本讲解决了「厂商库是什么、怎么供给、怎么排错」。接下来两个方向：

- **继续本单元（u2）**：下一讲 **u2-l3 综合属性、防优化与时钟门控策略** 会深入 `preserve` / `dont_touch` / `altera_attribute` 等综合属性，并讲清 `BUFGCE`（本讲提到的 unisim 原语）在 Xilinx 与 Intel 下的门控取舍。它与本讲是「仿真」对「综合」的镜像——本讲管仿真库，下一讲管综合属性。
- **横向跳转**：如果你想立刻看厂商库在真实设计里如何被例化，可先读 **u8（时钟域跨域同步器）**，那里的 `ff_synchroniser` 正是本讲反复引用的 `xpm_cdc_single` 的载体；或读 **u5（时钟生成与门控）** 看 `BUFGCE` / `PLLE2_BASE` 的实际用法。但建议先完成 u2-l3，把「仿真 + 综合」两条厂商适配线凑齐，再进入具体 IP。
