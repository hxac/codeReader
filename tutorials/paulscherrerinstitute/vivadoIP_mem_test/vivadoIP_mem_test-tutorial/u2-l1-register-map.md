# 寄存器地图：mem_test_pkg 详解

## 1. 本讲目标

本讲带你读懂 IP 核的「软件可见面」——寄存器地图。寄存器地图是 CPU（通过 AXI-Lite）和 FPGA 内部测试逻辑之间的唯一约定：CPU 往哪些地址写什么值就能启动测试，从哪些地址读什么值就能拿到结果，全部由一个 VHDL package 文件定义。

学完本讲，你应当能够：

- 说出本 IP 全部已实现寄存器的**偏移地址、字段含义和读写方向**。
- 理解 `rd_t` / `wr_t` / `rdata_t` / `wdata_t` 这四个子类型描述的是一种「**按寄存器编号访问**」的接口模型，而不是常见的扁平地址/数据总线。
- 根据 `STATUS` 寄存器的数值，反推出核心状态机当前处于哪个阶段（空闲 / 写入中 / 读出中 / 出错）。
- 仅看 package，就能推测出「跑一次完整内存测试」最少要按什么顺序写哪几个寄存器。

本讲**只读一个源文件** `hdl/mem_test_pkg.vhd`，并在「源码精读」里少量引用 `hdl/mem_test.vhd` 与 `hdl/mem_test_wrapper.vhd` 来印证这些定义如何被使用。寄存器背后的状态机与 pattern 算法细节留给后续讲义（u3-l3、u3-l4）。

## 2. 前置知识

在进入源码前，先用三段大白话建立直觉。这些概念在前两讲（u1-l1、u1-l2）已经铺垫过，这里只做最小回顾。

**什么是寄存器（register）。** 在 FPGA 设计里，寄存器就是一段「软件可读写的 32 位存储单元」。CPU 把它当成普通内存地址来读写，但它的每一位都可能连到硬件内部某个控制信号或状态信号上。本 IP 的控制面 `S00_AXI` 就是一个 AXI-Lite 从机，CPU 通过它访问一组寄存器。

**什么是 package。** VHDL 的 `package` 类似 C 的头文件：它集中声明常量（`constant`）、子类型（`subtype`）、函数等，供多个设计单元共享。`mem_test_pkg.vhd` 就是本 IP 的「单一事实来源」——寄存器编号、字段编码全在这里定义，RTL 和 C 驱动都要对齐它。

**为什么用一个「按寄存器编号」的接口。** 普通 AXI 外设给软件的是一条「地址 + 数据」总线。而本 IP 在 wrapper 里用了一个现成的 AXI-Lite 从机 IP（`psi_common_axi_slave_ipif`，见 u4-l1），它已经帮你把地址译码好了，转而给核心逻辑一个更友好的接口：「第 N 号寄存器正在被写，值是 X」。所以 package 里的子类型是为这种接口量身定做的。这个机制本讲只点到为止，u4-l1 会展开。

> 名词速查：`S00_AXI`（控制面，AXI-Lite 从机）、`M00_AXI`（数据面，AXI4 主机，接被测存储器）、`generic`（VHDL 的可参数化常量）、`subtype`（在已有类型上加约束得到的新类型）。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用到什么 |
| --- | --- | --- |
| `hdl/mem_test_pkg.vhd` | **本讲主角**。整个寄存器地图的唯一来源。 | 全部内容：接口子类型、寄存器地址常量、字段枚举。 |
| `hdl/mem_test.vhd` | 核心测试逻辑。 | 仅引用其 entity 端口与 `p_comb` 中读写寄存器的代码，**印证** package 定义如何被消费；以及 `FsmToInt` 如何把状态机映射成 `STATUS` 数值。 |
| `hdl/mem_test_wrapper.vhd` | 顶层 wrapper。 | 仅引用其内部信号声明与实例端口映射，说明四种子类型在顶层如何连线。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，按「先接口、后地址、再编码」的顺序：

1. **寄存器接口子类型**：`rd_t` / `wr_t` / `rdata_t` / `wdata_t` 描述「怎么访问」。
2. **寄存器常量定义**：`REG_*` 常量描述「在哪个地址、字段怎么布局」。
3. **状态 / 模式 / Pattern 枚举**：`C_STATUS_*` / `C_MODE_*` / `C_PATTERN_SEL_*` 描述「字段取什么值代表什么含义」。

### 4.1 寄存器接口子类型：按寄存器编号访问的硬件模型

#### 4.1.1 概念说明

很多 AXI 外设教科书会给你这样一个模型：一条 `reg_addr` 地址总线 + 一条 `reg_wdata` / `reg_rdata` 数据总线，软件每次先给地址再给数据。本 IP **不是**这样。

本 IP 的 wrapper 内部接了一个 AXI-Lite 从机 IP，它已经完成了「地址 → 寄存器编号」的译码。于是核心逻辑拿到的是一个**已经按寄存器拆开**的接口：

- 「**哪些**寄存器正在被访问」用一根每寄存器一位的位向量表示；
- 「访问的**数据**是什么」用一个「按寄存器编号索引」的数组表示。

这样设计的好处是：核心代码不用自己写 `case addr when ...`，直接用寄存器常量当下标就能拿到对应数据，例如 `Reg_WData(REG_START)` 直接就是 START 寄存器的写入值。package 里的四种子类型就是为这种接口定义的。

#### 4.1.2 核心流程

一次「CPU 写寄存器 i 值为 V」在内部表现为两路信号同时有效：

```text
CPU 写 S00_AXI 地址 4*i，数据 V
        │  (AXI-Lite 从机 IP 完成 8 位地址译码)
        ▼
  wr_t(i)     = '1'        ── 第 i 号寄存器「正在被写」
  wdata_t(i)  = V          ── 第 i 号寄存器「写入的值」
        │
        ▼  核心逻辑在组合进程中消费：
     next_state / 内部变量  依据 wdata_t(i) 更新
```

读路径对称：CPU 读地址 `4*i` 时，`rd_t(i)` 拉高一个周期，核心逻辑必须在同一周期把回读值驱动到 `rdata_t(i)` 上。注意：读数据是**组合回送**的（在 `p_comb` 进程里赋值），而不是写进真正的触发器——因为读到的多半是实时状态（当前 FSM 状态、实时错误计数）。

#### 4.1.3 源码精读

接口子类型集中定义在 package 顶部的 `-- Register General` 段：

[hdl/mem_test_pkg.vhd:L22-L27](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L22-L27) — 定义寄存器数量与四种子类型。

逐行解读：

- `USER_SLV_NUM_REG : integer := 32`：寄存器总数固定为 32（注释强调「只允许 2 的幂」，这是 AXI-Lite 从机 IP 的约束）。这正是 u1-l1 提到的 8 位地址空间：8 位字节地址可寻址 \(2^8=256\) 字节，每寄存器 4 字节，故最多 \(2^8/4 = 2^6 = 64\) 个寄存器；本 IP 取其中 32 个。
- `rd_t` / `wr_t`：`std_logic_vector(31 downto 0)`，**每寄存器一位**的访问选通。第 `i` 位为 `'1'` 表示第 `i` 号寄存器本周期被读 / 被 写。
- `rdata_t` / `wdata_t`：`t_aslv32(0 to 31)`，即「32 个 32 位字」的数组。`t_aslv32` 来自外部依赖 `psi_common_array_pkg`（见 u1-l2），含义是「array of std_logic_vector(31 downto 0)」。第 `i` 个元素就是第 `i` 号寄存器的数据。

这四种类型随后被核心 entity 直接用作端口类型：

[hdl/mem_test.vhd:L35-L38](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L35-L38) — 核心逻辑 entity 的四路寄存器端口。

注意端口方向：`Reg_Rd` / `Reg_Wr` / `Reg_WData` 都是 `in`（由 AXI-Lite 从机送给核心），只有 `Reg_RData` 是 `out`（核心回送给从机，最终被 CPU 读到）。

在 wrapper 里，这四路信号就是顶层内部连线的类型：

[hdl/mem_test_wrapper.vhd:L125-L128](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L125-L128) — wrapper 声明的四条内部寄存器信号，类型即 package 子类型。

并以此把从机实例与核心实例对接：

[hdl/mem_test_wrapper.vhd:L316-L319](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L316-L319) — wrapper 把这四路信号连到核心逻辑实例的端口。

最后看一眼核心逻辑**怎么用**这套接口。下面这一行是「检测 START 寄存器被写 1」的典型写法：

[hdl/mem_test.vhd:L147](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L147) — `RegStart_v := Reg_WData(REG_START)(C_START_START) and Reg_Wr(REG_START);`

读法：`Reg_Wr(REG_START)` 判断「START 寄存器本周期是否被写」，`Reg_WData(REG_START)(C_START_START)` 取出写入值的第 `C_START_START`（=0）位，两者相与就得到「启动测试」的有效脉冲。这正是「按寄存器编号访问」的威力——没有地址比较，直接下标。

#### 4.1.4 代码实践

**实践目标**：通过「阅读型追踪」确认你对四种子类型的理解，不运行任何仿真。

**操作步骤**：

1. 打开 `hdl/mem_test.vhd`，找到 `p_comb` 进程（约 L127 起）。
2. 用编辑器搜索 `Reg_WData(`，列出所有出现的寄存器编号。它们就是**可写**寄存器集合。
3. 再搜索 `Reg_RData(`，列出所有出现的寄存器编号。它们就是**可读**寄存器集合。
4. 比较两个集合：只出现在 `Reg_WData` 的是「只写」，只出现在 `Reg_RData` 的是「只读」，两边都出现的是「读写」。

**需要观察的现象**：`START`、`STOP` 应当只出现在 `Reg_WData` 一侧（它们是触发型 strobe，没必要回读）；`STATUS`、`ERRORS`、`FIRSTERR`、`ITER` 应当只出现在 `Reg_RData` 一侧（它们是硬件状态，软件只读）；`MODE`、`SIZE`、`ADDR`、`PATTERN_SEL` 应当两边都出现（写进去之后能读回确认）。

**预期结果**：得到一张「读 / 写方向」分类，与第 4.2 节的地址表对齐。这正是 package 本身**没有显式声明**、却隐含在使用中的信息——一个很好的「读源码而非读文档」的练习。

> 说明：本实践为源码阅读型，无需运行；若你按上述步骤操作，结论应与 4.2 节表格的「读写」列一致。

#### 4.1.5 小练习与答案

**练习 1**：`rd_t` 和 `wr_t` 为什么是「每寄存器一位」，而不是一个数组？

**参考答案**：因为它们表达的是「第 i 号寄存器**本周期是否**被访问」这个布尔事实，一位足矣；而真正的数据走 `rdata_t` / `wdata_t` 数组。把「选通」和「数据」拆成两类信号，是 AXI-Lite 从机 IP 完成地址译码后给出的自然接口。

**练习 2**：`USER_SLV_NUM_REG` 为什么注释里写「only powers of 2 are allowed」？

**参考答案**：AXI-Lite 从机 IP（`psi_common_axi_slave_ipif`）要求寄存器数量是 2 的幂，以便用地址的低位做简洁译码。这也是地址宽度取 8 位、寄存器数取 32（而非任意值）的根本原因。

---

### 4.2 寄存器常量定义：地址地图与字段布局

#### 4.2.1 概念说明

package 的第二大块是 `-- Register Definition`，它给每个寄存器起一个符号名（`REG_START` 等）并赋一个**寄存器编号**（integer）。编号到字节地址的换算是固定的：

\[
\text{byte\_address} = 4 \times \text{index}
\]

因为每个寄存器是 32 位 = 4 字节，所以 `REG_START = 0` 对应 `0x00`，`REG_MODE = 3` 对应 \(4 \times 3 = 12 = \texttt{0x0C}\)，依此类推。注释里贴心地在每行末尾标了十六进制地址，你可以用上面这个公式自检。

字段（field）层面的常量则描述「寄存器内部某几位代表什么」。有两种风格：

- **单 bit 触发**：如 `C_START_START := 0`，表示 START 寄存器的第 0 位是启动位。
- **多 bit 枚举**：用一个 `subtype ... is natural range X downto Y` 声明字段所占的位段（如 `RNG_MODE` 是 `2 downto 0`，3 位），再用一组 `C_MODE_*` 常量给出枚举值。

#### 4.2.2 核心流程

一次完整内存测试，软件需要按下面顺序操作寄存器（配置类都是 R/W，先写后可读回确认）：

```text
1. 写 MODE        选择 Single/Continuous/WriteOnly/ReadOnly
2. 写 PATTERN_SEL 选择 Counter/Walk1/OwnAddr/Prbn
3. 写 ADDR_LO/HI  设置被测区域起始字节地址
4. 写 SIZE_LO/HI  设置被测区域字节数
5. 写 START=1     启动测试
   ── 轮询读 STATUS，直到回到 IDLE（或读到出错码）
6. 读 ERRORS      获取累计错误数
7. 读 FERR_ADDR_LO/HI  获取首个错误地址
8. 读 ITER        （Continuous 模式下）获取已完成迭代数
```

注意第 5 步：`START`、`STOP` 是 **strobe 型**寄存器，软件写 1 表示「执行一次动作」，并不保持状态；而 `MODE` / `PATTERN_SEL` / `SIZE` / `ADDR` 是**电平型配置**，写进去后一直有效，直到被改写。

#### 4.2.3 源码精读

整个地址地图集中定义在这段：

[hdl/mem_test_pkg.vhd:L29-L69](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L29-L69) — 全部寄存器编号与字段位段声明。

为方便查阅，把这段源码整理成下表（读写方向由 4.1.4 的追踪得出，**package 本身未显式声明方向**）：

| 地址 | 编号 | 寄存器常量 | 读写 | 字段 | 含义 |
| --- | --- | --- | --- | --- | --- |
| 0x00 | 0 | `REG_START` | W | bit `C_START_START`(=0) | 写 1 启动一次测试（strobe） |
| 0x04 | 1 | `REG_STOP` | W | bit `C_STOP_STOP`(=0) | 写 1 停止测试（strobe，主要用于 Continuous） |
| 0x08 | 2 | — | — | — | **保留**，未定义 |
| 0x0C | 3 | `REG_MODE` | R/W | `RNG_MODE` [2:0] | 测试模式（见 4.3） |
| 0x10 | 4 | `REG_SIZE_LO` | R/W | [31:0] | 测试字节数低 32 位 |
| 0x14 | 5 | `REG_SIZE_HI` | R/W | [31:0] | 测试字节数高 32 位（合计 64 位大小） |
| 0x18 | 6 | `REG_ADDR_LO` | R/W | [31:0] | 起始字节地址低 32 位 |
| 0x1C | 7 | `REG_ADDR_HI` | R/W | [31:0] | 起始字节地址高 32 位（合计 64 位地址） |
| 0x20 | 8 | `REG_PATTERN_SEL` | R/W | `RNG_PATTERN_SEL` [2:0] | 测试图形选择（见 4.3） |
| 0x24 | 9 | `REG_STATUS` | R | `RNG_STATUS` [2:0] | 当前状态机状态（见 4.3） |
| 0x28 | 10 | `REG_ERRORS` | R | [31:0] | 累计错误计数 |
| 0x2C | 11 | `REG_FERR_ADDR_LO` | R | [31:0] | 首个错误地址低 32 位 |
| 0x30 | 12 | `REG_FERR_ADDR_HI` | R | [31:0] | 首个错误地址高 32 位 |
| 0x34 | 13 | `REG_ITER` | R | [31:0] | Continuous 模式已完成迭代次数 |
| 0x38–0x7C | 14–31 | — | — | — | **保留**，IP 预留 32 寄存器空间但未实现 |

两个值得注意的点：

- **64 位地址与大小**：`ADDR`、`SIZE` 各占两个寄存器（低/高），拼成 64 位。核心代码用 `unsigned(Reg_WData(REG_SIZE_HI)) & unsigned(Reg_WData(REG_SIZE_LO))` 拼接，见 [hdl/mem_test.vhd:L153](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L153)。
- **大量保留寄存器**：编号 2、14–31 都没实现。IP 之所以声明 32 个寄存器空间，是因为 AXI-Lite 从机 IP 以 32 为单位配置（`USER_SLV_NUM_REG = 32`），但实际只用到 14 个。

#### 4.2.4 代码实践

**实践目标**：亲手把 package 的常量重算成一张「地址—寄存器名—读写—含义」对照表，并验证地址换算公式。

**操作步骤**：

1. 打开 `hdl/mem_test_pkg.vhd`，从 `REG_START`（L30）读到 `REG_ITER`（L69）。
2. 对每个 `REG_*` 常量，按 \(\text{byte\_address} = 4 \times \text{index}\) 手算十六进制地址，与行尾注释比对。
3. 给每个寄存器标注读写方向（参考 4.1.4 的追踪结果）。
4. 单独把 `STATUS` 一行展开，列出全部状态码数值（见下一节 4.3 的答案）。

**需要观察的现象**：你的手算地址应与源码注释**完全一致**；编号 2 这一行源码里没有任何 `REG_*` 定义，是「跳号」保留位。

**预期结果**：得到与 4.2.3 表格内容相同的对照表，外加一行 `STATUS` 状态码明细。这就是本讲 `practice_task` 要求的产物。

> 说明：纯阅读与手算，无需运行环境。

#### 4.2.5 小练习与答案

**练习 1**：`REG_STATUS = 9`，它的字节地址为什么是 0x24？

**参考答案**：\(4 \times 9 = 36\)，而 \(36 = 2 \times 16 + 4 = \texttt{0x24}\)。每个寄存器 4 字节，故地址 = 编号 × 4。

**练习 2**：编号 2（地址 0x08）为什么没有对应的 `REG_*` 常量？软件误写它会怎样？

**参考答案**：这是设计上保留的未用寄存器，故 package 不给它命名。写它不会触发任何核心逻辑（`p_comb` 里没有对编号 2 的引用），写入值被丢弃；读它通常返回 0。这是 IP 为未来扩展预留的空位。

---

### 4.3 状态 / 模式 / Pattern 枚举：字段取值的统一编码

#### 4.3.1 概念说明

第三大块是三组「枚举常量」。它们回答同一个问题：「某个字段写成几，代表什么含义？」

- `C_MODE_*`：`MODE` 寄存器（3 位）写 0/1/2/3 分别代表四种测试模式。
- `C_PATTERN_SEL_*`：`PATTERN_SEL` 寄存器（3 位）写 0/1/2/3 分别代表四种测试图形。
- `C_STATUS_*`：`STATUS` 寄存器（3 位）回读 0/1/2/3/6/7 分别代表六种状态。

注意 `STATUS` 是**只读**的——软件不写它，而是由核心状态机通过函数 `FsmToInt` 把当前状态翻译成一个整数写进 `STATUS` 的低 3 位。所以 `C_STATUS_*` 这组常量既被 RTL（生成状态码）用，也被软件驱动（解析状态码）用，是软硬两侧的共享词典。

#### 4.3.2 核心流程

`STATUS` 数值的产生链路：

```text
核心 FSM 当前状态 r.Fsm  (VHDL 枚举 Fsm_t)
        │
        │  调用函数 FsmToInt(r.Fsm)
        ▼
   一个 integer（C_STATUS_IDLE / WRITING / ...）
        │
        │  to_unsigned(..., 3) 放进 RNG_STATUS [2:0]
        ▼
   Reg_RData(REG_STATUS)  ── CPU 读 0x24 即可拿到
```

关键细节：**多个 FSM 状态会映射到同一个 `STATUS` 数值**。例如发写命令（`WrCmd_s`）和真正写数据（`Write_s`）对软件来说都是「正在写入」，都映射成 `C_STATUS_WRITING`。这是一种「对外简化状态」的常见做法。

#### 4.3.3 源码精读

**MODE 枚举**（`RNG_MODE` = [2:0]）：

[hdl/mem_test_pkg.vhd:L36-L41](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L36-L41) — 四种测试模式编码。

| 数值 | 常量 | 含义 |
| --- | --- | --- |
| 0 | `C_MODE_SINGLE` | 单次测试：写 pattern→读回比对→停止 |
| 1 | `C_MODE_CONTINUOUS` | 连续测试：循环迭代直到写 STOP |
| 2 | `C_MODE_WRITEONLY` | 只写：仅写入 pattern，不读回（用于排查写通路） |
| 3 | `C_MODE_READONLY` | 只读：仅读回比对（前提是存储器已有已知 pattern） |

**PATTERN_SEL 枚举**（`RNG_PATTERN_SEL` = [2:0]）：

[hdl/mem_test_pkg.vhd:L49-L54](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L49-L54) — 四种测试图形编码。

| 数值 | 常量 | 含义 |
| --- | --- | --- |
| 0 | `C_PATTERN_SEL_COUNT` | 递增计数 pattern |
| 1 | `C_PATTERN_SEL_WALK1` | 走 1（walking-1）pattern |
| 2 | `C_PATTERN_SEL_OWNADD` | 「自身地址」pattern（数据 = 所在地址） |
| 3 | `C_PATTERN_SEL_PRBN` | 伪随机 pattern（LFSR） |

> 这四种 pattern 的**生成算法**在 `mem_test.vhd` 的 `InitPattern` / `UpdatePattern` 中实现，本讲只认编码，算法细节见 u2-l2 与 u3-l4。

**STATUS 枚举**（`RNG_STATUS` = [2:0]）：

[hdl/mem_test_pkg.vhd:L56-L63](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L56-L63) — 六种状态编码。

| 数值 | 常量 | 含义 |
| --- | --- | --- |
| 0 | `C_STATUS_IDLE` | 空闲，可启动新测试 |
| 1 | `C_STATUS_WRITING` | 正在向存储器写 pattern |
| 2 | `C_STATUS_READING` | 正在从存储器读回并比对 |
| 3 | `C_STATUS_AXIERR` | AXI 总线返回错误（不可恢复） |
| 6 | `C_STATUS_INTERR` | 内部错误（不可恢复） |
| 7 | `C_STATUS_UNKNOWN` | 未知/不应出现的状态 |

**注意 4 和 5 这两个「空号」**：状态码从 3（AXIERR）直接跳到 6（INTERR），4、5 未使用。这不是笔误，而是有意留出的间隔——把「总线类错误」和「内部错误」在数值上拉开距离，方便软件用范围判断错误类别。

那么这些 `C_STATUS_*` 数值是谁写进 `STATUS` 寄存器的？是核心逻辑里的 `FsmToInt` 函数：

[hdl/mem_test.vhd:L77-L89](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L77-L89) — 把 FSM 枚举状态翻译成 `STATUS` 数值。

注意其中的「多对一」映射：`Write_s | WrCmd_s => return C_STATUS_WRITING;`、`Read_s | RdCmd_s => return C_STATUS_READING;`——两个内部细状态对外合并成一个粗状态。`when others => return C_STATUS_UNKNOWN;` 兜底所有未明确列出的情况。

最后，这个整数被放进 `STATUS` 寄存器的低 3 位：

[hdl/mem_test.vhd:L173-L174](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L173-L174) — 先清零整个 `STATUS`，再把 `FsmToInt` 结果放入 `RNG_STATUS` 位段。

这一步体现了「字段化写回」的典型套路：先把 32 位整体清零，再只填自己关心的那几位，避免未定义位把脏数据暴露给软件。

#### 4.3.4 代码实践

**实践目标**：把 `STATUS` 的状态码数值与 FSM 状态的对应关系彻底厘清，并解释「跳号」。

**操作步骤**：

1. 对照 [hdl/mem_test_pkg.vhd:L56-L63](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L56-L63) 抄出六个 `C_STATUS_*` 的数值。
2. 打开 [hdl/mem_test.vhd:L77-L89](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L77-L89) 的 `FsmToInt`，画一张「FSM 状态 → STATUS 数值」映射表。
3. 圈出 `C_STATUS_AXIERR`（3）与 `C_STATUS_INTERR`（6）之间的空号 4、5。

**需要观察的现象**：映射表里 `WrCmd_s` 与 `Write_s` 共享 `1`，`RdCmd_s` 与 `Read_s` 共享 `2`；空号 4、5 没有任何 `C_STATUS_*` 占用。

**预期结果**：得到一张完整的状态码表，并能用一句话解释「为什么 INTERR 是 6 而不是 4」——为了在数值上把总线错误（3）与内部错误（6）分组隔开。

> 说明：纯阅读型，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：CPU 读到 `STATUS = 0x00000001`，IP 当前在做什么？软件接下来该做什么？

**参考答案**：低 3 位 = 1 = `C_STATUS_WRITING`，说明 IP 正在向被测存储器写入 pattern（可能处于 `WrCmd_s` 或 `Write_s`）。软件应继续轮询 `STATUS`，直到它回到 `0`（IDLE）或变成 3/6（出错）。

**练习 2**：`FsmToInt` 里 `when others => return C_STATUS_UNKNOWN;` 的 `others` 会覆盖哪些状态？这个兜底有意义吗？

**参考答案**：`Fsm_t` 共 7 个状态（`Idle_s, WrCmd_s, Write_s, RdCmd_s, Read_s, AxiError_s, IntError_s`），前 5 个加两个错误态都已被显式 `when` 覆盖，理论上 `others` 永远命中不了。它是防御性写法：万一未来扩展 `Fsm_t` 增加了新状态却忘了更新 `FsmToInt`，软件读到的会是 `C_STATUS_UNKNOWN`(7) 而不是某个误导性的旧值，便于发现遗漏。

**练习 3**：为什么 `C_STATUS_*` 既在 VHDL package 里定义，又（你将在 u2-l3 看到）在 C 驱动头文件里重复定义一遍？

**参考答案**：因为 VHDL 和 C 是两个独立编译的世界，没有共享头文件机制。两侧必须**各自**用各自的语法声明同一套数值，并靠人工保持一致。这正是「寄存器地图是软硬契约」的体现——package 是契约的 VHDL 副本，C 头文件是契约的 C 副本。

## 5. 综合实践

把三个模块串起来，完成一次「纸面启动测试」。

假设你想用「自身地址 pattern」对一片 DDR 做**单次**测试，区域从 `0x0000_0001_0000_0000` 起、长度 `0x0000_0000_0001_0000`（64 KiB）。请仅依据本讲的 package，写出：

1. 需要按顺序写哪几个寄存器、各写什么值（用十六进制地址 + 十六进制数据列出）。
2. 启动后应当轮询哪个地址、期待读到什么值表示「完成」。
3. 完成后从哪两个地址拼出「首个错误地址」。

**参考作答**（请先自己写再对照）：

1. 顺序写入：
   - `0x0C`（MODE）← `0x0000_0000`（`C_MODE_SINGLE` = 0）
   - `0x20`（PATTERN_SEL）← `0x0000_0002`（`C_PATTERN_SEL_OWNADD` = 2）
   - `0x18`（ADDR_LO）← `0x0000_0000`，`0x1C`（ADDR_HI）← `0x0000_0001`（拼成 64 位起始地址）
   - `0x10`（SIZE_LO）← `0x0001_0000`，`0x14`（SIZE_HI）← `0x0000_0000`（拼成 64 KiB）
   - `0x00`（START）← `0x0000_0001`（写 1 启动）
2. 轮询 `0x24`（STATUS）。正常完成时低 3 位回到 `0`（`C_STATUS_IDLE`）；若读到 `3`（AXIERR）或 `6`（INTERR）表示出错。
3. 完成后读 `0x2C`（FERR_ADDR_LO）与 `0x30`（FERR_ADDR_HI），低/高拼成 64 位首个错误地址；再读 `0x28`（ERRORS）看总错误数。

这个练习覆盖了本讲全部三个模块：接口子类型（知道为何按编号访问）、地址地图（算出每个寄存器地址）、字段枚举（把模式与 pattern 翻译成数值）。

## 6. 本讲小结

- 本 IP 用一个 `package` 文件 `hdl/mem_test_pkg.vhd` 作为寄存器地图的**唯一事实来源**：地址、字段、编码全在这里。
- 四种子类型 `rd_t` / `wr_t` / `rdata_t` / `wdata_t` 描述的是「**按寄存器编号访问**」的接口模型——选通用位向量、数据用 32 元素数组，这是 AXI-Lite 从机 IP 完成地址译码后给出的友好接口。
- 地址换算公式固定为 \(\text{byte\_address} = 4 \times \text{index}\)；32 个寄存器空间里实际只实现了 14 个（编号 0–13），其余保留。
- 寄存器分三类：**触发型** strobe（START/STOP，只写）、**配置型**（MODE/SIZE/ADDR/PATTERN_SEL，读写）、**状态型**（STATUS/ERRORS/FERR_ADDR/ITER，只读）。
- 三组枚举 `C_MODE_*` / `C_PATTERN_SEL_*` / `C_STATUS_*` 是软硬两侧共享的「字段取值词典」；`STATUS` 由 `FsmToInt` 把内部 FSM 状态翻译而来，且多个细状态会合并成一个对外粗状态。
- `STATUS` 状态码存在「跳号」（3 之后直接到 6），用于把总线错误与内部错误在数值上分组。

## 7. 下一步学习建议

- 想理解四种 pattern 的**生成算法**（递增、走 1、自身地址、LFSR 伪随机）和四种模式的**触发/停止语义**，继续读 **u2-l2「测试模式与数据 pattern」**。
- 想看这些寄存器常量如何被 **C 裸机驱动**封装成 `MemTest_Start()` 等 API，读 **u2-l3「C 软件驱动：寄存器访问封装」**，并对照本讲的 `REG_*` / `C_*` 常量验证两侧是否一致。
- 想知道 `rd_t` / `wr_t` 这套「按寄存器编号」接口**到底是谁产生的**（即 AXI-Lite 从机 IP 如何译码），留到 **u4-l1「AXI-Lite 从机与寄存器译码」**。
- 想看核心逻辑 `p_comb` 如何**消费**这些寄存器（启动/停止脉冲、配置锁存、状态回填），进入第三单元 **u3-l2「核心实体接口与两进程设计」** 与 **u3-l3「主状态机」**。
