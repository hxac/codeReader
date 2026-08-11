# SPI 模式与 spi_pkg 通用包

## 1. 本讲目标

SPI（Serial Peripheral Interface，串行外设接口）是嵌入式世界最常见的同步串行总线之一。它的时序由两个开关量决定：时钟极性 CPOL 与时钟相位 CPHA，二者组合出 4 种「SPI 模式」。本讲不是讲 SPI 控制器本身（那是 u10-l2 ~ u10-l4 的任务），而是聚焦整个 SPI 子系统的**公共大脑**——通用包 `spi_pkg`。

学完本讲，你应当能够：

1. 说出 `spi_pkg` 是什么、为什么它要做成 **VHDL-2008 generic package（带类属的包）**。
2. 会用 `package … is new work.spi_pkg generic map(…)` 的语法把一个泛化包「例化」成一份具体实现。
3. 看懂 CPOL/CPHA 四种 SPI 模式下，「主机在哪个时钟边沿改数据（TX）」「从机/主机在哪个边沿采样数据（RX）」的对应关系，并把它们和 `spi_pkg` 里的 `tx_active_edge` / `rx_active_edge` 函数逐一对照。
4. 看懂 `last_bit_index` / `update_bit_index` 这两个过程如何用同一份代码，仅靠一个布尔 generic 就同时支持「先发高位（MSB first）」和「先发低位（LSB first）」两种位序。
5. 具备动手对照 Wikipedia SPI 模式表、核验源码边沿定义的能力——并在过程中发现源码里一个只有半数模式被测试覆盖的细节。

## 2. 前置知识

在进入源码前，先用三段话补齐两块基础：SPI 的基本时序、VHDL 的 package 机制。

### 2.1 SPI 一句话与时序四要素

SPI 是「主—从」结构的同步串行总线：主机（controller/master）给出时钟 SCK 和片选 SS_N（低有效），在时钟驱动下，主机与从机通过 MOSI（主出从入）、MISO（主入从出）两条数据线**同时、全双工地**逐位移传数据。

决定时序形状的有四个开关量，本讲关心前两个：

| 名称 | 含义 | 取值 |
|------|------|------|
| **CPOL**（Clock Polarity，时钟极性） | 时钟**空闲时**停在哪个电平 | `0`＝空闲低；`1`＝空闲高 |
| **CPHA**（Clock Phase，时钟相位） | 数据在时钟的**哪个边沿**被采样 | `0`＝前导边沿采样；`1`＝后导边沿采样 |
| 位序 | 先发 MSB 还是 LSB | MSB first / LSB first |
| 数据宽度 | 每帧多少比特 | 常见 8 |

CPOL 与 CPHA 组合出 4 种「模式」（Mode 0~3），是本讲后半段的核心。

### 2.2 前导边沿 / 后导边沿 与 CPOL 的关系

SPI 协议里反复出现「前导边沿（leading edge）」「后导边沿（trailing edge）」。它们与 CPOL 直接挂钩：

- CPOL=0（空闲低）：时钟从低拉高，**前导边沿 = 上升沿**，后导边沿 = 下降沿。
- CPOL=1（空闲高）：时钟从高拉低，**前导边沿 = 下降沿**，后导边沿 = 上升沿。

再叠加 CPHA 的规则——**CPHA=0 在前导边沿采样、CPHA=1 在后导边沿采样**——就能推导出四种模式各自在「哪个物理边沿」采样。我们会在 4.2 节给出整张表。

> 一个检验你是否理解的小推理：CPOL=0 时前导是上升沿；CPOL=1 时前导是下降沿。所以「物理采样边沿」会随 CPOL 整体翻转。记住这一点，本讲最后的源码核验会用到。

### 2.3 VHDL 的 package 与「generic package」

package（包）是 VHDL 里组织「可复用常量、类型、函数、过程」的容器，相当于别的语言里的「模块/命名空间」。`package … end package` 声明对外可见的接口，`package body … end package body` 放实现细节。这个概念在 u3-l1 讲 `memories_pkg` 时已建立。

本讲引入的新东西是 **VHDL-2008 的 generic package（带类属的包）**：从 VHDL-2008 起，`package` 自己也可以带 `generic` 子句。这样一个包就能被「参数化」，不同的使用者可以传入不同的参数，得到「同一个包的不同实例」。这正是 `spi_pkg` 的形态——它需要 `DATA_WIDTH`（位宽）和 `MSB_FIRST_AND_NOT_LSB`（位序）两个参数，所以做成 generic package，让 8 位的 `spi_tx`、16 位的 `spi_tx`、先高位或先低位的不同配置，各自例化出自己那一份。

> 提示：如果你对普通 package 还不熟，建议先翻 u3-l1 的 `memories_pkg` 讲义；本讲会把两者做一个直接对照。

## 3. 本讲源码地图

本讲涉及的关键文件，全部在 `ip/communication/spi/` 下：

| 文件 | 角色 | 本讲用它做什么 |
|------|------|----------------|
| `spi_pkg.vhd` | **主角**：generic package | 精读它的全部函数/过程与四种模式边沿定义 |
| `spi_tx.vhd` | SPI 发送端（消费方） | 看它如何例化 `spi_pkg`、如何用 `tx_active_edge` 对齐输出 |
| `spi_rx.vhd` | SPI 接收端（消费方） | 看它如何用 `rx_active_edge` 决定采样边沿 |
| `tb/tb_spi_tx.vhd`、`tb/tb_spi_rx.vhd` | 测试台（也是消费方） | 看测试台如何用同一套边沿函数做节拍同步 |
| `ip/memories/memories_pkg.vhd` | 普通包（对照） | 与 generic package 形态做对比 |

注意一个组织约定（承接 u1-l2）：模块专用的包 `spi_pkg.vhd` 就近放在 `spi/` 目录根，与本模块的设计源码同处一层；而大类共享的 `memories_pkg.vhd` 则放在 `memories/` 顶层。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **spi_pkg 作为 generic package**——它是什么、怎么被例化。
2. **CPOL/CPHA 与四种 SPI 模式**——把协议知识落到一张表上。
3. **有效边沿函数（tx / rx / 片选）**——源码如何把模式表翻译成 `rising_edge` / `falling_edge`。
4. **位序过程 last_bit_index / update_bit_index**——一份代码通吃 MSB/LSB。

### 4.1 spi_pkg：一个 generic package

#### 4.1.1 概念说明

`spi_pkg` 是整个 SPI 子系统的「公共工具箱」。它把所有和「时钟边沿」「位序」有关的判断都收拢成一组函数与过程，让 `spi_tx`、`spi_rx`、`spi_interface` 以及它们的测试台都引用同一份真相，而不是各自手写 `rising_edge` / `falling_edge`。

它有两个关键特征：

- 它是一个 **generic package**：声明里带 `generic` 子句，需要在使用方例化时绑定 `DATA_WIDTH` 与 `MSB_FIRST_AND_NOT_LSB` 两个类属。
- 它内部用 `DATA_WIDTH` 派生出一个子类型 `data_range_t`，所有「位序」相关的过程都以它为参数类型，从而位宽自动跟随例化方。

为什么必须做成 generic package？因为位序过程 `update_bit_index` 需要「知道」当前位序是 MSB 还是 LSB、位宽是多少，才能决定计数方向和折回点。如果做成普通包，就只能写死，或者把这两个参数一路透传到每个函数签名里，非常啰嗦。generic package 让参数在「例化一次」时就绑死，之后整个包内的所有函数/过程都能直接用，调用点极其干净。

#### 4.1.2 核心流程

`spi_pkg` 被使用的流程是：

```text
spi_tx / spi_rx / 测试台
        │  在自己的声明区写：
        │    package spi_pkg_constrained is new work.spi_pkg
        │        generic map (DATA_WIDTH => …, MSB_FIRST_AND_NOT_LSB => …);
        │    use spi_pkg_constrained.all;
        ▼
得到一个「具体实例包」spi_pkg_constrained（含已绑定的 data_range_t、边沿函数、位序过程）
        │
        ▼
在进程里直接调用 tx_active_edge(...) / rx_active_edge(...) / update_bit_index(...)
```

注意：**每个使用者各自例化一份**。`spi_tx` 例化它的 `spi_pkg_constrained`，`spi_rx` 例化它自己的 `spi_pkg_constrained`，测试台又例化一份。它们都是 `work.spi_pkg` 的实例，名字相同也互不冲突，因为包实例的作用域局限在各自的 architecture/tb 内。

#### 4.1.3 源码精读

先看包声明——注意第 14~17 行的 `generic` 子句，这就是「generic package」的标志：

[spi_pkg.vhd:13-28](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd#L13-L28) —— `spi_pkg` 的接口：两个 generic（`DATA_WIDTH`、`MSB_FIRST_AND_NOT_LSB`）、由 `DATA_WIDTH` 派生的子类型 `data_range_t`，以及五个函数与两个过程的签名。

几个要点：

- 第 19 行 `subtype data_range_t is natural range 0 to DATA_WIDTH - 1;`：位索引的合法范围。`DATA_WIDTH=8` 时就是 `0 to 7`。位序过程都用它作参数类型。
- 第 21~24 行的四个函数都带 `signal clk_in: std_ulogic` 形参，并且函数返回 `boolean`。这是为了能在 `if tx_active_edge(...)` 或 `wait until rx_active_edge(...)` 里直接使用。
- 第 25~27 行是两个**过程**（procedure）`reset_bit_index` / `update_bit_index`，参数模式为 `inout`——因为它们要回写 `bit_index`。函数做不到回写参数，所以这里必须是过程。

再看消费方如何例化它。下面是 `spi_tx` 在 architecture 声明区里的例化：

[spi_tx.vhd:58-63](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd#L58-L63) —— `spi_tx` 把 entity 的两个 generic 透传给 `spi_pkg`，得到本架构专属的 `spi_pkg_constrained`，再 `use … .all` 把它整体可见。

`spi_rx` 的写法完全相同（[spi_rx.vhd:34-39](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L34-L39)），测试台 `tb_spi_tx`、`tb_spi_rx` 里也各有一份（[tb_spi_tx.vhd:56-61](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L56-L61)）。这是 generic package 的典型用法：**声明即绑定，绑定即可用**。

> 对照：u3-l1 的 `memories_pkg` 是**普通**包（没有 generic 子句），声明完直接 `use work.memories_pkg.all` 即可，不需要「例化」这一步。能否看出 [memories_pkg.vhd:13-15](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/memories_pkg.vhd#L13-L15) 与上面 `spi_pkg` 在「有没有 generic 子句」上的差别？这正是普通包与 generic package 的全部外形区别。

#### 4.1.4 代码实践

**目标**：亲手把一个泛化包例化成两个不同位宽的实例，体会「同一份 generic package、多份实例」。

**操作步骤**：

1. 打开 `spi_pkg.vhd`，确认它的 generic 是 `DATA_WIDTH` 与 `MSB_FIRST_AND_NOT_LSB`。
2. 在一个临时 `.vhd`（或脑内推演）里写两段例化：

```vhdl
-- 示例代码：演示同一 generic package 的两个不同实例
architecture demo of something is
    package spi_pkg_8bit_msb is new work.spi_pkg
        generic map (DATA_WIDTH => 8, MSB_FIRST_AND_NOT_LSB => true);
    use spi_pkg_8bit_msb.all;

    -- 同一个 work.spi_pkg，换组参数就是另一份实例
    package spi_pkg_16bit_lsb is new work.spi_pkg
        generic map (DATA_WIDTH => 16, MSB_FIRST_AND_NOT_LSB => false);
    use spi_pkg_16bit_lsb.all;
begin
    -- 注意：两份实例里的 data_range_t 一个是 0..7，一个是 0..15，
    -- update_bit_index 的计数方向也相反，但调用语法完全一样。
end architecture;
```

3. 编译（用本库的 `test_runner.py` 或你本地的 VHDL-2008 仿真器）。

**需要观察的现象**：两份 `package … is new` 各自独立，`data_range_t` 的范围随 `DATA_WIDTH` 变化；位序过程的计数方向随布尔值变化。

**预期结果**：编译通过、无「重复声明」冲突（因为两份实例名字不同、且作用域分离）。如果你把两份都命名为 `spi_pkg_constrained` 放在**同一个**声明区，才会报重复声明错误——这正好说明「包实例名」就是普通的名字，需要唯一。

**待本地验证**：若你的仿真器对 VHDL-2008 generic package 支持不完整（少数老版本 ModelSim），可能报错；本库 CI 用的 NVC 与本地 VUnit 流程都已支持。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `update_bit_index` 必须是过程（procedure）而不是函数（function）？

> **答案**：它要**修改并回写**传入的 `bit_index`（计数递增/递减、到末尾折回）。VHDL 的函数参数都是 `in`（只读、按值），无法回写；过程的参数可以是 `inout` / `out`，才能把新值送回调用方。所以凡是「要改调用方变量」的逻辑，都得用过程。

**练习 2**：`spi_tx`、`spi_rx`、`tb_spi_tx` 都各自例化了一份 `spi_pkg_constrained`，它们会互相干扰吗？

> **答案**：不会。每个包实例的作用域局限在各自所在的 architecture（或块）。它们的内部名字虽然相同，但分属不同作用域，编译器视作三个独立实例；运行时也各算各的 `bit_index`。

---

### 4.2 CPOL/CPHA 与四种 SPI 模式

#### 4.2.1 概念说明

CPOL 与 CPHA 各是一个二值开关，组合出 4 种「SPI 模式」。两台 SPI 设备要通信，**双方必须配置成同一种模式**，否则会在错误的边沿采样、读出乱码。理解这张模式表，是读懂 `spi_pkg` 里那一串 `case` 的前提。

#### 4.2.2 核心流程：从 CPOL/CPHA 推出物理采样边沿

我们用 2.2 节的两条规则把四种模式逐一展开：

- 规则一：CPOL=0 → 前导=上升沿、后导=下降沿；CPOL=1 → 前导=下降沿、后导=上升沿。
- 规则二：CPHA=0 → 在前导边沿采样；CPHA=1 → 在后导边沿采样。
- 主机的「移位/改数据（TX）」边沿与「采样（RX）」边沿必须落在**相反**的两个物理边沿上（差半个时钟），全双工才能正确工作。

由此得到标准 SPI 模式表（采样/移位边沿）：

| 模式 | CPOL | CPHA | 时钟空闲 | RX 采样边沿 | TX 移位边沿 |
|:---:|:---:|:---:|:---:|:---:|:---:|
| 0 | 0 | 0 | 低 | 上升沿 | 下降沿 |
| 1 | 0 | 1 | 低 | 下降沿 | 上升沿 |
| 2 | 1 | 0 | 高 | 下降沿 | 上升沿 |
| 3 | 1 | 1 | 高 | 上升沿 | 下降沿 |

推导关键：**CPOL 从 0 变 1，会把前导/后导的物理边沿整体翻转**，所以模式 2/3（CPOL=1）的采样边沿相对模式 0/1 也是「翻转」的。

> 一个自洽性判据：表中每一行，RX 采样边沿与 TX 移位边沿必然一升一降、互为相反——这是 SPI 全双工的硬要求。我们会在 4.3 节用它来核验源码。

#### 4.2.3 源码精读

`spi_pkg.vhd` 的 body 顶部就贴了参考来源：

[spi_pkg.vhd:30-31](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd#L30-L31) —— 注释指向 Wikipedia 的 SPI 模式编号表，提示下面的边沿函数是按该表实现的。

具体的边沿定义在 4.3 节逐函数精读。这里先建立「模式编号 ↔ `clk_polarity & clk_phase` 字符串」的对应，因为源码用了一个巧妙写法：把两个 `bit` 拼接成 2 位的 `bit_vector`，再用字符串字面量匹配：

| 模式 | `clk_polarity` | `clk_phase` | 拼接结果 `clk_polarity & clk_phase` |
|:---:|:---:|:---:|:---:|
| 0 | `'0'` | `'0'` | `"00"` |
| 1 | `'0'` | `'1'` | `"01"` |
| 2 | `'1'` | `'0'` | `"10"` |
| 3 | `'1'` | `'1'` | `"11"` |

所以源码里 `case clk_polarity & clk_phase is when "00" | "11" => …` 这样的写法，本质就是「按模式号分支」。`'0'/'1'` 是 `bit` 类型的字面量（不是 `std_logic`），`"00"` 是 `bit_vector` 的字符串字面量。

#### 4.2.4 代码实践

**目标**：用纸笔把 4.2.2 的两条规则推一遍，确认你真的能自己「算」出四种模式的采样边沿，而不是死记。

**操作步骤**：

1. 盖住上面的标准模式表。
2. 对模式 3（CPOL=1, CPHA=1）推理：CPOL=1 → 前导是哪个物理边沿？CPHA=1 → 在前导还是后导采样？所以采样边沿是上升还是下降？
3. 同理推模式 2。

**需要观察的现象**：你的推理结果是否与表中一致（模式 2 采样下降沿、模式 3 采样上升沿）。

**预期结果**：模式 2 → 下降沿；模式 3 → 上升沿。如果算反了，多半是把「CPOL=1 时前导是下降沿」记成了上升沿——回到 2.2 节的物理直觉：空闲为高，时钟第一个动作只能是从高往低掉，所以前导是下降沿。

**待本地验证**：无（纯协议推理）。

#### 4.2.5 小练习与答案

**练习 1**：某传感器手册要求「SCK 空闲高、数据在第二个时钟边沿被采样」，应配置成哪种模式？

> **答案**：空闲高 → CPOL=1；第二个边沿采样 → CPHA=1；即**模式 3**。其 RX 采样边沿为上升沿。

**练习 2**：为什么同一个 SPI 链路上的主、从两方必须用相同模式？

> **答案**：因为双方约定了「在哪类边沿改数据、在哪类边沿采样数据」。若一方在上升沿改、另一方却在下降沿采，就会采到数据翻转的过渡期或不正确的值。模式相同，才能保证一方「改」与另一方「采」对齐到同一个稳定的时钟区间。

---

### 4.3 有效边沿函数：tx / rx / 片选

#### 4.3.1 概念说明

`spi_pkg` 把 4.2 节的模式表翻译成了三个返回 `boolean` 的函数：

- `tx_active_edge(clk_in, clk_polarity, clk_phase)`：主机在哪个边沿**改输出数据**（移位）。
- `rx_active_edge(clk_in, clk_polarity, clk_phase)`：在哪个边沿**采样输入数据**。
- `active_edge_chip_select_n_assertion / _deassertion`：片选在哪个边沿**拉低（选中）/ 拉高（释放）**。

它们都接收 `signal clk_in`，返回 `boolean`，于是能直接写进 `if … then` 或 `wait until …`。函数内部用 `rising_edge(clk_in)` / `falling_edge(clk_in)` 做实际判断——这也是为什么参数必须声明为 `signal` 类别：边沿检测需要信号的跳变语义，普通 `constant`（默认）参数拿不到 `'event`/`rising_edge`。

#### 4.3.2 核心流程

以 `spi_rx` 为例（它最直接）：接收进程对 `spi_clk` 敏感，进程一进就调用 `rx_active_edge(spi_clk, CPOL, CPHA)`，只有当前是「采样边沿」时才执行采样逻辑。换句话说，**采样边沿由包函数说了算，进程本身不关心到底是上升还是下降**。切换 SPI 模式只要改 entity 的 `SPI_CLK_POLARITY` / `SPI_CLK_PHASE` generic，代码一个字都不用动——这就是把边沿判断收进包的好处。

#### 4.3.3 源码精读

**TX 移位边沿函数**：

[spi_pkg.vhd:51-60](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd#L51-L60) —— `tx_active_edge`：模式 0（"00"）与模式 3（"11"）都在**下降沿**移位；模式 1（"01"）在**上升沿**移位；模式 2（"10"）直接返回 `true`（恒真，表示「不经边沿寄存、组合直通」）。

注意 `when "00" | "11" =>` 这种「多个选择合并」的写法，以及模式 2 返回常量 `true` 的特殊处理——它对应 `spi_tx` 里模式 2 的组合直通分支。

**RX 采样边沿函数**：

[spi_pkg.vhd:62-75](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd#L62-L75) —— `rx_active_edge`：四种模式各返回一个边沿判断，并带一个 `when others => return false` 兜底。

**片选边沿函数**（按 CPOL 分两种）：

[spi_pkg.vhd:33-49](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd#L33-L49) —— 片选的「选中」与「释放」边沿只与 CPOL 有关：`assertion` 在 CPOL=0 时用下降沿、CPOL=1 时恒真（立即）；`deassertion` 在 CPOL=0 时用上升沿、CPOL=1 时用下降沿。

**消费方——`spi_rx` 怎么用**：

[spi_rx.vhd:41-58](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L41-L58) —— 进程 `receiver` 在 `rx_active_edge(spi_clk, SPI_CLK_POLARITY, SPI_CLK_PHASE)` 为真时才动作：把 `serial_data_in` 写进 `rx_data(bit_index)`，并在收到最后一位时拉高 `rx_data_valid`。整个进程对「到底是哪个物理边沿」毫无感知，全交给包函数。

**消费方——`spi_tx` 怎么用边沿做对齐**：

[spi_tx.vhd:110-157](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd#L110-L157) —— 这段 `case … generate` 用编译期常量 `SPI_CLK_POLARITY & SPI_CLK_PHASE` 为四种模式各生成一段「把内部数据对齐到正确边沿再输出」的逻辑。注释（[spi_tx.vhd:107-109](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd#L107-L109)）解释了为什么这里要手写 `if falling_edge`/`rising_edge` 而不直接调包里的函数：Xilinx 工具对「内容与 `rising_edge` 等价的泛化函数」识别不出边沿、会推断成锁存器，所以发送端的边沿对齐改用 `case generate` 手写展开；接收端（Intel 友好）则直接用包函数。

> 这是个值得记的工程细节：**边沿检测函数在「综合」与「仿真」里命运不同**。仿真器老老实实调函数；综合器却需要「看得见」这是边沿敏感，才会生成触发器。包函数 + `case generate` 两种写法并存，正是为兼顾两家工具。

**测试台怎么用**：

[tb_spi_tx.vhd:119-124](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L119-L124) —— `wait_tx_spi_clk_cycles` 过程用 `wait until tx_active_edge(…)` 来数「发送了几个 SPI 时钟」，把包函数当节拍同步器用。`tb_spi_rx` 里同样用 `rx_active_edge` 做节拍对齐（[tb_spi_rx.vhd:108-113](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_rx.vhd#L108-L113)）。

#### 4.3.4 核心流程：把源码与标准模式表对照（本讲重点核验）

把 4.2.2 的标准表与 `spi_pkg` 实际返回值并排放：

| 模式 (CPOL,CPHA) | 标准 RX 采样 | 代码 `rx_active_edge` | 标准 TX 移位 | 代码 `tx_active_edge` |
|:---:|:---:|:---:|:---:|:---:|
| 0 (0,0) | 上升 | **上升 ✓** | 下降 | **下降 ✓** |
| 1 (0,1) | 下降 | **下降 ✓** | 上升 | **上升 ✓** |
| 2 (1,0) | 下降 | 上升 | 上升 | `true`（组合直通） |
| 3 (1,1) | 上升 | 下降 | 下降 | **下降 ✓** |

读这张表的三个结论：

1. **TX 侧（模式 0/1/3）与标准一致**；模式 2 改用组合直通，是本库的一种实现取舍（见 `spi_tx.vhd` 的 `pass_through` 分支）。
2. **RX 侧在 CPOL=0（模式 0/1）与标准完全一致**。
3. **RX 侧在 CPOL=1（模式 2/3）与标准给出的采样边沿相反**——并且 `rx_active_edge` 的返回值实际上**只取决于 CPHA**（CPHA=0 返回上升沿、CPHA=1 返回下降沿），并未随 CPOL 翻转；而 4.2 节已说明 CPOL=1 时物理边沿会整体翻转，所以对 CPOL=1 应给出相反结果。

一条独立佐证（不依赖外部表）：用 4.2.2 的「TX 与 RX 必须落在相反物理边沿」判据检查——模式 3 的代码值是 TX 下降沿、RX 下降沿，落在**同一个**物理边沿上，这与 SPI 全双工「差半个时钟」的要求不符；模式 0/1 则都是一升一降、自洽。这说明 CPOL=1 那一侧的边沿定义确实值得复核。

更重要的是覆盖率：本库所有 SPI 测试台都只配置了 `SPI_CLK_POLARITY => '0'`、`SPI_CLK_PHASE => '0'`（模式 0），见 [tb_spi_tx.vhd:50-51](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L50-L51) 与 [tb_spi_rx.vhd:50-51](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_rx.vhd#L50-L51)。也就是说，**模式 1/2/3 从未在仿真中被实际验证过**。模式 1 之所以和标准一致是「逻辑碰巧对」，而模式 2/3 的 RX 差异只有在你亲手核验或补一组模式 2/3 的测试时才会暴露。

> 小结：源码注释自称「参见 Wikipedia 模式表」，而经推导 RX 的 CPOL=1 分支与该表不一致且未被测试覆盖。本讲不匆忙下「这是 bug」的结论——请你用下面的实践任务，对照 Wikipedia 原表自己下判断；若确认不一致，补一组模式 2/3 的测试台就是非常有价值的贡献。

#### 4.3.5 代码实践（本讲主实践任务）

**目标**：对照 Wikipedia 的 SPI 模式表，逐一核验 `tx_active_edge` / `rx_active_edge` 在四种 CPOL×CPHA 组合下的返回边沿是否正确；并为一种新模式补一行注释说明采样时机。

**操作步骤**：

1. 打开 Wikipedia「Serial Peripheral Interface → Mode numbers」一节，抄下四种模式的「采样边沿 / 移位边沿」表。
2. 打开 `spi_pkg.vhd` 的 `tx_active_edge`（51~60 行）与 `rx_active_edge`（62~75 行），按 4.3.4 的表格逐格比对。
3. 标记每一格：与 Wikipedia 一致打 ✓，不一致打 ✗。
4. 在 `rx_active_edge` 上方，为「模式 1（CPOL=0, CPHA=1）」补一行注释，写清它的采样时机，例如：

```vhdl
-- 模式 1：时钟空闲低，在后导边沿（下降沿）采样，在前导边沿（上升沿）移位
```

5. 进阶（可选）：在 `tb_spi_rx.vhd` 里把 `SPI_CLK_POLARITY` 改成 `'1'`、`SPI_CLK_PHASE` 仍 `'0'`（模式 2），跑一次仿真，观察 `rx_data` 是否还能正确接收——这会直接暴露 4.3.4 提到的 CPOL=1 问题。

**需要观察的现象**：模式 0/1 全部 ✓；模式 3 的 RX 出现 ✗（代码下降沿 vs 标准上升沿）；若做了进阶实验，模式 2 的接收会出错。

**预期结果**：得到一张填满 ✓/✗ 的对照表，并亲手确认「只有模式 0 被现有测试覆盖」这一事实。

**待本地验证**：Wikipedia 表的措辞偶尔有版本差异，请以你当时打开的页面为准；模式 2/3 的仿真结果强烈建议本地实跑确认。

#### 4.3.6 小练习与答案

**练习 1**：为什么 `rx_active_edge` 的参数要写成 `signal clk_in: std_ulogic`，而不是默认的常量参数？

> **答案**：因为函数体内要调用 `rising_edge(clk_in)` / `falling_edge(clk_in)`，这俩依赖信号的跳变事件。`signal` 类别让形参关联到实际信号、保留事件/属性信息；若用默认 constant 类别，参数是「传入瞬间的值快照」，`rising_edge` 永远拿不到跳变，逻辑失效。

**练习 2**：`tx_active_edge` 用 `case clk_polarity & clk_phase is` 来分支，这里 `clk_polarity` 与 `clk_phase` 是什么类型？`"00"` 又是什么？

> **答案**：在 `spi_pkg` 的使用方，`SPI_CLK_POLARITY` / `SPI_CLK_PHASE` 都是 `bit`（取值 `'0'`/`'1'`）。两个 `bit` 用 `&` 拼接得到长度为 2 的 `bit_vector`；`"00"` 是 `bit_vector` 的字符串字面量，所以能直接在 `case` 里匹配。

---

### 4.4 位序过程：last_bit_index / update_bit_index

#### 4.4.1 概念说明

SPI 一帧数据可以「先发最高位（MSB first）」也可以「先发最低位（LSB first）」。这意味着同一个 `bit_index` 计数器，在两种位序下计数方向相反、起止点也不同：

- MSB first：从最高下标（如 7）数到 0，每步 `−1`。
- LSB first：从 0 数到最高下标（如 7），每步 `+1`。

`spi_pkg` 没有为两种位序写两套代码，而是用 generic `MSB_FIRST_AND_NOT_LSB` 这一个布尔开关，配上一组过程，做到「一份代码、两种位序」。核心是三个东西：

- `data_range_t`：位索引范围 `0 to DATA_WIDTH-1`。
- `last_bit_index(bit_index)`：判断「是不是最后一位」（到末尾要折回）。
- `reset_bit_index` / `update_bit_index`：复位 / 推进计数器。

#### 4.4.2 核心流程

位计数器的运转规则（伪代码）：

```text
复位：reset_bit_index(i)
      └─ MSB_FIRST ? i := 上界(DATA_WIDTH-1)   // 从最高位起
                   : i := 下界(0)                // 从最低位起

每拍推进：update_bit_index(i)
      └─ 若 last_bit_index(i)：折回（重新 reset_bit_index）
         否则：MSB_FIRST ? i := i - 1
                        : i := i + 1

判断末位：last_bit_index(i)
      └─ MSB_FIRST ? (i == 下界 0)
                   : (i == 上界 DATA_WIDTH-1)
```

关键是「上界/下界」不是写死的常数，而是用 VHDL 的 `'subtype` 属性从 `bit_index` 自己声明的子类型范围里动态读出来。所以无论 `DATA_WIDTH` 是 8、16 还是 32，这套过程都自动适配。

#### 4.4.3 源码精读

**判断末位**：

[spi_pkg.vhd:77-83](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd#L77-L83) —— `last_bit_index`：MSB 先发时，末位是下界 `'subtype'low`（数到 0）；LSB 先发时，末位是上界 `'subtype'high`（数到 DATA_WIDTH-1）。

**复位**：

[spi_pkg.vhd:85-87](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd#L85-L87) —— `reset_bit_index`：用一行条件赋值把起点设好。注意这里把 `bit_index'subtype'high/low` 当「常量」用，实际是运行期对子类型边界的查询。

**推进（含折回）**：

[spi_pkg.vhd:89-96](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd#L89-L96) —— `update_bit_index`：先查 `last_bit_index`，到末位就折回（调 `reset_bit_index`）并 `return`；否则按位序 `−1` 或 `+1`。过程间相互复用（`update` 调 `last_bit_index` 与 `reset_bit_index`），逻辑集中、不重复。

**消费方——`spi_tx` 的串行化**：

[spi_tx.vhd:68-105](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd#L68-L105) —— `bit_index` 是进程变量（第 69 行声明为 `natural range 0 to tx_data'subtype'high`）。发送时：握手成功后 `reset_bit_index`，每拍输出 `tx_data_reg(bit_index)`，再用 `last_bit_index` 判断是否发完、用 `update_bit_index` 推进。注意第 95~99 行的顺序——先判末位（决定是否结束本次传输），否则才 `update_bit_index` 推进。

**消费方——`spi_rx` 的装配**：

[spi_rx.vhd:44-56](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L44-L56) —— 接收侧同样：每个采样边沿把 `serial_data_in` 写进 `rx_data(bit_index)`，到 `last_bit_index` 时拉高 `rx_data_valid`，随后 `update_bit_index`。复位或片选无效时 `reset_bit_index` 重新对齐。

> 一个 VHDL 小知识：`'subtype` 属性返回对象所声明的子类型。`spi_tx` 里 `bit_index` 声明为 `natural range 0 to tx_data'subtype'high`，所以 `bit_index'subtype'low = 0`、`bit_index'subtype'high = tx_data'subtype'high`（= DATA_WIDTH-1）。过程正是靠这两个属性值来决定「从哪开始、到哪折回」，于是位宽完全由例化方的 `DATA_WIDTH` 决定，过程体里**没有写死任何数字**。

#### 4.4.4 代码实践

**目标**：手动跟踪 MSB first 与 LSB first 两种位序下 `bit_index` 的逐拍取值，确认同一份过程能产出正确的两种序列。

**操作步骤**：

1. 假设 `DATA_WIDTH = 8`，所以 `data_range_t = 0 to 7`，`'subtype'low = 0`、`'subtype'high = 7`。
2. **MSB first 场景**（`MSB_FIRST_AND_NOT_LSB = true`），在纸上模拟：
   - `reset_bit_index` → `i := 'subtype'high = 7`
   - `last_bit_index(7)`？MSB 分支看 `i == low(0)` → 否
   - `update_bit_index(7)` → `i := 7 - 1 = 6`
   - 重复，直到 `i = 0`：`last_bit_index(0)` → 是（折回）
3. **LSB first 场景**（`MSB_FIRST_AND_NOT_LSB = false`），再模拟一遍：起点变 0、末位变 7、每步 `+1`。
4. 把两组序列写出来：MSB → `7,6,5,4,3,2,1,0`；LSB → `0,1,2,3,4,5,6,7`。

**需要观察的现象**：两组序列方向相反、起止互换，但调用的是**同一份** `reset_bit_index` / `update_bit_index` / `last_bit_index`，区别只在那个布尔 generic。

**预期结果**：MSB 序列 `7→0`，LSB 序列 `0→7`，正好对应「先发 tx_data(7)」与「先发 tx_data(0)」。

**待本地验证**：无（纯逻辑推演）。

#### 4.4.5 小练习与答案

**练习 1**：`update_bit_index` 里为什么先判断 `last_bit_index` 再决定要不要 `±1`？

> **答案**：因为「最后一位」不能再继续 `±1`，否则会越界（MSB 时 `0-1`、LSB 时 `7+1` 都超出 `data_range_t`）。所以到末位要先 `reset_bit_index` 折回并 `return`，跳过 `±1`。这是典型的「带折回的环形计数器」写法。

**练习 2**：如果把 `bit_index` 的子类型从 `natural range 0 to 7` 改成 `natural range 1 to 8`，`update_bit_index` 还能正常工作吗？

> **答案**：能。因为过程内部用的是 `'subtype'low` / `'subtype'high`（即 1 与 8），而不是写死的 0 与 7。起止点与计数方向会自动跟随实际声明的子类型边界。这正是用属性而非魔数的好处——前提是 `data_range_t` 与 `bit_index` 的范围保持一致（本库里二者一致）。

---

## 5. 综合实践

把本讲四块内容串起来，完成下面这个「帧级追踪」小任务：

**设定**：`DATA_WIDTH = 8`、`MSB_FIRST_AND_NOT_LSB = true`、要发送的数据 `tx_data = 0xA5`（二进制 `1010_0101`）。SPI 配置为**模式 0**（CPOL=0, CPHA=0）。

**要你产出三样东西**：

1. **位序列表**：列出本次传输 `bit_index` 的逐拍取值，以及对应的 `serial_data_out`（即 `tx_data(bit_index)`）。
   - 提示：MSB first，从 `i=7` 开始。`0xA5` 的 bit7..bit0 是 `1,0,1,0,0,1,0,1`。
2. **边沿标注**：在模式 0 下，画出 SCK 波形的草图，标注哪几个边沿是「TX 移位」（下降沿）、哪几个是「RX 采样」（上升沿），并用箭头说明主机「改数据」与从机「采样数据」相差半个时钟。
3. **源码核验**：用 4.3.4 的对照表，圈出 `rx_active_edge` 在模式 2/3 与标准不一致的那两格；并写一句话说明「为什么现有测试台发现不了这个问题」。

**参考答案要点**：

1. 位序列（MSB first，8 拍）：

   | 拍 | bit_index | tx_data(bit_index) | serial_data_out |
   |:--:|:--:|:--:|:--:|
   | 1 | 7 | bit7 | 1 |
   | 2 | 6 | bit6 | 0 |
   | 3 | 5 | bit5 | 1 |
   | 4 | 4 | bit4 | 0 |
   | 5 | 3 | bit3 | 0 |
   | 6 | 2 | bit2 | 1 |
   | 7 | 1 | bit1 | 0 |
   | 8 | 0 | bit0 | 1 |

   串行线上依次出现 `1,0,1,0,0,1,0,1`，即先看到最高位。

2. 模式 0：SCK 空闲低。每个时钟周期里，**下降沿**主机改 MOSI（TX 移位）、**上升沿**双方采样（RX 采样）；改与采差半个时钟，互不干扰。

3. `rx_active_edge` 模式 2（"10"）返回上升沿、模式 3（"11"）返回下降沿，与标准的「模式 2 下降、模式 3 上升」相反；现有 `tb_spi_tx` / `tb_spi_rx` 都写死 `SPI_CLK_POLARITY='0'`、`SPI_CLK_PHASE='0'`（模式 0），从未仿真过 CPOL=1，所以这个问题在 CI 里是潜伏的。

## 6. 本讲小结

- `spi_pkg` 是 SPI 子系统的公共大脑，做成 **VHDL-2008 generic package**，靠 `DATA_WIDTH` 与 `MSB_FIRST_AND_NOT_LSB` 两个类属被「例化」成各使用方专属的实例。
- 例化语法是 `package spi_pkg_constrained is new work.spi_pkg generic map(…); use spi_pkg_constrained.all;`——`spi_tx`、`spi_rx`、两个测试台各例化一份，互不干扰。
- CPOL 决定时钟空闲电平、CPHA 决定在前导还是后导边沿采样；二者组合出 4 种 SPI 模式，**CPOL=1 时物理采样边沿相对 CPOL=0 整体翻转**。
- `tx_active_edge` / `rx_active_edge` 把模式表翻译成 `rising_edge` / `falling_edge`；参数必须是 `signal` 类别才能做边沿检测；`spi_tx` 因 Xilinx 综合的边沿识别问题，额外用 `case generate` 手写了边沿对齐。
- `last_bit_index` / `update_bit_index` 用一个布尔 generic + `'subtype` 属性，让同一份过程同时支持 MSB/LSB 两种位序与任意位宽，过程体内没有写死数字。
- **核验发现**：`tx_active_edge`（模式 0/1/3）与 `rx_active_edge`（模式 0/1）和标准一致；但 `rx_active_edge` 的 CPOL=1 分支（模式 2/3）与标准相反，且**现有测试台只覆盖模式 0**，模式 2/3 从未被仿真——这是值得你补测试的地方。

## 7. 下一步学习建议

本讲只讲了「模式表与工具包」，还没有真正进入 SPI 控制器的时序逻辑。建议接着学：

1. **u10-l2 SPI 发送 spi_tx**：精读本讲反复提到的 `spi_tx.vhd`——看握手 `tx_data_valid`/`tx_data_ack`、`bit_index` 串行化进程、以及 `case generate` 如何为四种模式对齐串行数据与片选，还有它如何复用 u5-l1 的 `clock_enable` 来门控 SPI 时钟。
2. **u10-l3 SPI 接收 spi_rx**：精读 `spi_rx.vhd`——看 `rx_active_edge` 采样、片选/复位共同控制位计数复位、收满一字拉高 `rx_data_valid` 的完整流程。
3. **u10-l4 SPI 顶层接口**：进入 `spi_interface.vhd` 的多片选状态机，看它如何把 `spi_tx` / `spi_rx` / 异步 FIFO（u9-l3）组合成完整控制器。
4. 若你完成了 4.3.5 的进阶实验、确认了模式 2/3 的 RX 问题，不妨尝试给 `tb_spi_rx` 增加一组模式 2/3 用例——这是把本讲从「读懂」推向「能改、能贡献」的最佳练手。
