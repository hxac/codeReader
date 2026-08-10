# 多比特同步器 ff_synchroniser_vector

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚「为什么一个多比特总线不能简单地逐比特各接一个单比特同步器」——即**数据撕裂（data tearing）**问题。
- 读懂 `ff_synchroniser_vector` 的实体端口、两套厂商架构，以及它如何把 `in_data_valid` 与 `in_data` 拼接成一整条总线统一过同步链。
- 解释 `xpm_cdc_array_single` 与 Intel 显式并行同步链各自的实现方式。
- 看懂 `fifo_async` 为什么把这个模块用在跨时钟域的**格雷码指针**上，并且把 `in_data_valid` 恒定接成 `'1'`。

本讲承接 [u8-l1 单比特同步器 ff_synchroniser](u8-l1-ff-synchroniser.md)：上一讲解决了「搬一个比特过时钟域」，本讲解决「搬一整条总线过时钟域」。

## 2. 前置知识

### 2.1 单比特同步器回顾（来自 u8-l1）

跨时钟域（CDC，Clock Domain Crossing）就是把一个信号从「源时钟域」搬到「目的时钟域」。当目的时钟采样时撞进源信号的翻转瞬间，触发器会进入**亚稳态（metastable）**——输出停在半电压值，经过不可预测的时间才塌缩成 0 或 1。解决办法是串一串触发器（同步链），让亚稳态在链中「消化」掉，链越长、平均故障间隔 **MTBF** 越高。

单比特同步器（`ff_synchroniser`）之所以简单，是因为 1 比特只有 0/1 两种取值：即便某一拍采到的是「上一拍的旧值」，它依然是一个**合法的**单比特值，最多就是晚一拍到达。

### 2.2 多比特的新麻烦：数据撕裂

把思路直接扩展到多比特总线会出问题。设想一条 3 位二进制计数器从 `011`（3）翻到 `100`（4）——**三个比特同时变化**。如果给每一比特各挂一条独立的单比特同步器，由于每条链的布线延迟、亚稳态塌缩时刻都不同，三个比特可能**不在同一拍**被目的域采到：

- 比特 2 已经被采到新值 `1`
- 比特 1、0 还停在旧值 `1`、`1`

于是目的域在中间那一拍看到了 `111`（7）——一个源端**从未产生过**的值。这种现象叫**数据撕裂（data tearing）**，又叫「总线位相干性丢失」。它不是亚稳态本身，而是「多个比特各自独立采样、步调不一致」造成的。

> 直觉记忆：单比特同步器解决的是「采得准不准」；多比特同步器还要解决「各比特采得齐不齐」。

本讲的 `ff_synchroniser_vector`，就是本库给出的「把整条总线当作一个整体来同步」的原语。它本身并不能凭空消除撕裂——它真正强大的用法，是和**格雷码**以及一个**valid 比特**配合，这正是异步 FIFO 的核心技巧。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ff_synchroniser_vector.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser_vector.vhd) | 多比特同步器本体。一个 entity 配两套 architecture：Xilinx 版封装 `xpm_cdc_array_single`，Intel 版用显式并行同步链。 |
| [fifo_async.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd) | 异步 FIFO。其 `own_behavioural_async_fifo` 架构里两次例化 `ff_synchroniser_vector`，把写/读指针的格雷码搬到对方时钟域。本讲把它当作「最大用户」来读。 |
| [tb_fifo_async.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd) | 异步 FIFO 测试台。`ff_synchroniser_vector` 没有独立测试台，靠它间接覆盖。 |

> 提醒（承接 u1-l2）：`ff_synchroniser_vector` 在仓库里**没有**独立的 `tb/tb_ff_synchroniser_vector.vhd`，它通过 `tb_fifo_async` 被间接验证。本讲的实践因此以「源码阅读 + 自建最小测试台」为主。

---

## 4. 核心概念与源码讲解

### 4.1 多比特跨域的「数据撕裂」问题

#### 4.1.1 概念说明

上一节已点出撕裂的成因。这里把它形式化：设源域总线在时刻 \(t\) 从值 \(A\) 翻到值 \(B\)，汉明距离 \(d(A,B) = k\)（即有 \(k\) 个比特同时变化）。若每个比特各走一条独立同步链，则目的域有可能在某个中间拍采到一个**混搭值** \(C\)，它既非 \(A\) 也非 \(B\)。当 \(k \geq 2\) 时撕裂才可能发生；\(k = 1\)（每次只翻一个比特）则**不可能撕裂**——最多采到旧值。

这给出两条根治撕裂的工程路线，本库两条都用：

1. **格雷码路线**：让源端每次只翻一个比特（\(k \equiv 1\)），撕裂天然不存在。
2. **valid 握手路线**：源端在数据稳定时拉高一个 `valid` 比特，把 `valid` 与数据**捆绑成一条总线**一起同步；目的域只在 `valid` 有效时才采信数据。

`ff_synchroniser_vector` 同时为这两条路线提供了底层支持：它能让一条总线「整条一起过同步链」，于是 `valid` 比特和数据会经历**完全相同**的流水线延迟，在目的域**同一拍**一起出现。

#### 4.1.2 核心流程

```
源域:  in_data (N 位) ──┐
                        ├── 拼接成 (N+1) 位 ── 同一条同步链 ──┐
       in_data_valid ───┘                                    │
                                                             ├── 目的域
                        ┌── out_data (N 位) ─────────────────┤
                        └── out_data_valid ──────────────────┘
        （二者因走过同一链，延迟完全对齐，同一拍同时生效）
```

关键点：因为整条 `(N+1)` 位向量在同步链里是**一起移位**的，`valid` 比特不会比数据早到或晚到。这就是「valid + 数据同链同步」之所以可靠的根源。

---

### 4.2 ff_synchroniser_vector 实体与 valid 比特拼接

#### 4.2.1 概念说明

`ff_synchroniser_vector` 的实体端口刻意设计成「非约束（unconstrained）」的数据端口——`in_data`/`out_data` 是不带范围的 `std_ulogic_vector`，真实位宽推迟到例化时由外部连线决定（与 u6-l1 单口 RAM 的非约束端口是同一种范式）。这样一份源码就能服务任意位宽的总线。

它在功能上比单比特同步器多了一个关键角色：`in_data_valid` / `out_data_valid`。这对 valid 比特不是为了「握手控制」同步器本身，而是为了让**调用方**知道「目的域此刻的数据是一个稳定快照」。

#### 4.2.2 源码精读

实体声明与 generic（[ff_synchroniser_vector.vhd:13-29](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser_vector.vhd#L13-L29)）：

```vhdl
entity ff_synchroniser_vector is
    generic (
        DEST_SYNC_FF   : positive range 2 to 10 := 4;   -- 同步链级数（2~10）
        SIM_INIT_SYNC_FF : boolean := false;            -- 仿真初值
        SIM_ASSERT_CHK : boolean := false;              -- 仿真断言信息
        SRC_INPUT_REG  : boolean := true                -- 是否在源域先寄存一刀
    );
    port (
        source_clk      : in  std_ulogic;
        destination_clk : in  std_ulogic;
        in_data         : in  std_ulogic_vector;        -- 非约束：位宽由外部决定
        in_data_valid   : in  std_ulogic;
        out_data        : out std_ulogic_vector;
        out_data_valid  : out std_ulogic
    );
end entity;
```

> 注意 generic 名是 `DEST_SYNC_FF`（沿用 Xilinx xpm 命名），而单比特版 `ff_synchroniser` 用的是 `SYNC_SHIFT_FF`（见 u8-l1）。两者范围与默认值一致（2~10，默认 4），但名字不同，例化时别搞混。

Xilinx 架构里的**拼接**是本模块的灵魂（[ff_synchroniser_vector.vhd:34-54](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser_vector.vhd#L34-L54)）：

```vhdl
architecture xilinx_behavioural_ff_synchroniser_vector of ff_synchroniser_vector is
    signal sync_chain_out: std_ulogic_vector(in_data'length downto 0); -- +1 for the valid bit
begin
    out_data_valid <= sync_chain_out(in_data'high + 1);   -- 最高位是 valid
    out_data       <= sync_chain_out(out_data'range);     -- 其余位是数据

    xpm_cdc_array_single_inst: xpm_cdc_array_single
        generic map (
            ...
            WIDTH => sync_chain_out'length                -- 总宽 = 数据位宽 + 1
        )
        port map (
            src_clk  => source_clk,
            dest_clk => destination_clk,
            src_in   => in_data_valid & in_data,          -- ★ valid 拼在最高位
            dest_out => sync_chain_out
        );
end architecture;
```

三处要点：

1. `sync_chain_out` 声明为 `in_data'length downto 0`，即**数据位宽 + 1**，多出的那一位专门留给 valid（注释 `-- +1 for the valid bit` 一语道破）。
2. `src_in => in_data_valid & in_data`：用 `&` 把 valid 比特拼到数据的最高位之上，组成一个 `(N+1)` 位向量**整条喂给** `xpm_cdc_array_single`。valid 与数据从此「同生共死」，走完全相同的同步链。
3. 输出端再把 `sync_chain_out` 拆开：最高位还原成 `out_data_valid`，其余位还原成 `out_data`。

这样设计的好处是：**valid 和数据的延迟严格一致**。当 `out_data_valid` 在目的域某一拍拉高时，`out_data` 必然是与之配对的那个稳定快照——绝不会出现「valid 已到、数据还差一拍」的错配。

---

### 4.3 Xilinx 实现：xpm_cdc_array_single

#### 4.3.1 概念说明

`xpm_cdc_array_single` 是 Xilinx `xpm` 库提供的「数组单次采样」CDC 宏（承接 u2-l2：`xpm` 是 Xilinx 参数化宏，封装 `unisim` 原语并自带约束）。它对总线的**每一位**各建一条同步链，但这些链共享同一对 `src_clk`/`dest_clk`、相同的级数 `DEST_SYNC_FF`，以及可选的源域寄存 `SRC_INPUT_REG`。

注意它并不能凭空保证「总线各位同时到达」——如果源总线有多比特同时翻转，`xpm_cdc_array_single` 依然可能给出撕裂值。它的可靠性来自于**调用方保证源端是格雷码（单比特变化）或配合 valid 握手**。这正是它在异步 FIFO 里只用来传**格雷码指针**的原因。

#### 4.3.2 核心流程

```
src_in[N:0] ──┬── bit[N]   ── (DEST_SYNC_FF 级 + 可选 SRC_INPUT_REG) ── dest_out[N]
              ├── bit[N-1] ── (DEST_SYNC_FF 级 + 可选 SRC_INPUT_REG) ── dest_out[N-1]
              └── ...                                                └── ...
        （各级共享 src_clk / dest_clk；各级数相同 ⇒ 延迟一致）
```

延迟（目的时钟周期数）约为 `DEST_SYNC_FF`，若 `SRC_INPUT_REG = true` 还多一级源域寄存。具体节拍数「待本地验证」。

#### 4.3.3 源码精读

generic 映射把布尔参数用 `boolean'pos(...)` 转成 xpm 要求的整数（[ff_synchroniser_vector.vhd:40-53](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser_vector.vhd#L40-L53)）。`WIDTH => sync_chain_out'length` 让 xpm 内部按 `N+1` 位建立同步链。端口的 `src_in`/`dest_out` 就是上一节看到的拼接向量。可见这套架构**几乎全是布线**——真正的同步逻辑全交给了厂商宏。

---

### 4.4 Intel 实现：显式并行同步链

#### 4.4.1 概念说明

承接 u8-l1 的模式：`ff_synchroniser`（单比特）只有 Xilinx + Intel 两套架构，没有 `own_behavioural_*`；多比特版 `ff_synchroniser_vector` 同样如此。Intel 架构不用任何厂商 CDC 宏，而是**手写**一条「源域寄存 + 目的域移位同步链」的 RTL。源码注释明确指出它可以脱离 Intel 工具、当作厂商无关代码使用（`altera_attribute` 在非 Intel 工具下会被忽略）。

这给了我们一个**可开箱仿真的实现**——本讲实践就基于它，因为 Xilinx 版需要 `xpm`/`unisim` 厂商库。

#### 4.4.2 核心流程

```
源域进程（source_clk）:
    source_data_registered  <= in_data;        -- 整条总线一起寄存
    source_valid_registered <= in_data_valid;  -- valid 一起寄存

目的域进程（destination_clk）:
    meta_stable_data_reg  <= source_data_registered;            -- 第 1 级（亚稳态）
    meta_stable_valid_reg <= source_valid_registered;
    sync_chain_data <= sync_chain_data(下移) & meta_stable_data_reg;  -- 继续移位 N-2 级
    sync_chain_valid <= sync_chain_valid(下移) & meta_stable_valid_reg;

输出:
    out_data       <= sync_chain_data(最高级);   -- 数据与 valid 走相同级数
    out_data_valid <= sync_chain_valid(最高级);
```

关键点：`sync_chain_data` 的每个元素都是**整条数据向量**（`in_data'subtype`），所以整条总线是**一整块一整块地移位**，valid 与数据永远同拍前进。

#### 4.4.3 源码精读

存储类型用 `in_data'subtype` 继承数据位宽，并用数组类型 `in_data_arr_t` 存放中间各级（[ff_synchroniser_vector.vhd:62-83](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser_vector.vhd#L62-L83)）：

```vhdl
signal meta_stable_data_reg: in_data'subtype;
signal meta_stable_valid_reg: std_ulogic;

type in_data_arr_t is array (DEST_SYNC_FF - 2 downto 0) of in_data'subtype; -- -2: 含 meta_stable_reg
signal sync_chain_data: in_data_arr_t;
signal sync_chain_valid: std_ulogic_vector(in_data_arr_t'range);
```

注意 `array (DEST_SYNC_FF - 2 downto 0)`：`meta_stable_data_reg` 占第 1 级，`sync_chain_data` 再补 `DEST_SYNC_FF - 2` 级，合计目的域内 `DEST_SYNC_FF - 1` 级，与单比特版的算法一致（见 u8-l1）。

防优化属性与 u8-l1/u2-l3 完全同构（[ff_synchroniser_vector.vhd:73-83](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser_vector.vhd#L73-L83)）：`altera_attribute` 给首级打 `SYNCHRONIZER_IDENTIFICATION "FORCED IF ASYNCHRONOUS"` 并嵌一条 SDC `set_false_path`，`preserve` 属性顶住整条链不被综合工具优化或重定时掉。

源域寄存进程**无条件**存在（[ff_synchroniser_vector.vhd:87-93](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser_vector.vhd#L87-L93)）——这跟 Xilinx 版用 `SRC_INPUT_REG` generic 控制不同：Intel 架构里源域寄存是强制的（与单比特版 u8-l1 的结论一致）。

目的域移位进程把整条数据向量与 valid 一起移位（[ff_synchroniser_vector.vhd:96-107](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser_vector.vhd#L96-L107)）：

```vhdl
sync_chain_in_dst_dom_proc: process (destination_clk)
begin
    if rising_edge(destination_clk) then
        meta_stable_data_reg  <= source_data_registered;
        meta_stable_valid_reg <= source_valid_registered;
        sync_chain_data <= sync_chain_data(sync_chain_data'high - 1 downto sync_chain_data'low) & meta_stable_data_reg;
        sync_chain_valid <= sync_chain_valid(sync_chain_valid'high - 1 downto sync_chain_valid'low) & meta_stable_valid_reg;
    end if;
end process;

out_data       <= sync_chain_data(sync_chain_data'high);
out_data_valid <= sync_chain_valid(sync_chain_valid'high);
```

`sync_chain_data` 是「向量的数组」，`&` 在这里拼接的是**整条数据向量**与数组元素，因此数据总线作为一个整体逐级平移；`out_data` 与 `out_data_valid` 取各自链的最高级，必然同拍生效。

#### 4.4.4 代码实践

> 本实践的依据是：`ff_synchroniser_vector` 无独立测试台，本库靠 `tb_fifo_async` 间接覆盖它。下面我们仿照 [tb_ff_synchroniser.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/tb/tb_ff_synchroniser.vhd) 的骨架，自建一个最小测试台，直接驱动这个模块。

**实践目标**：用一个稳定不变的 8 位向量驱动 `ff_synchroniser_vector`（Intel 架构，厂商无关、可开箱仿真），在目的域观察 `out_data` 与 `out_data_valid` 的延迟节拍，验证二者**同拍**出现。

**操作步骤**：

1. 新建 `ip/ff_synchroniser/tb/tb_ff_synchroniser_vector.vhd`（注意：仅为本地学习用，**不要**提交到仓库，本讲禁止修改源码/新增被 `test_runner` 扫描的真实测试台——此处是你在本地练手的临时文件）。
2. 仿照 `tb_ff_synchroniser.vhd` 的结构：声明 `runner_cfg`/`tb_path` generic、用 `generate_advanced_clock` 产生源时钟 100 MHz 与目的时钟 25 MHz（频率取自 `tb_ff_synchroniser.vhd` 的既有配置）、写 `main` 与 `checker` 两个进程。
3. 例化 DUT（用厂商无关的 Intel 架构）：

   ```vhdl
   -- 示例代码（非项目原有，仅用于本练习）
   constant DATA_WIDTH : positive := 8;
   constant DEST_SYNC_FF : positive := 4;
   signal in_data  : std_ulogic_vector(DATA_WIDTH-1 downto 0) := x"8A";
   signal in_data_valid : std_ulogic := '1';   -- 数据始终稳定 ⇒ valid 常高
   signal out_data : std_ulogic_vector(DATA_WIDTH-1 downto 0);
   signal out_data_valid : std_ulogic;

   DUT: entity work.ff_synchroniser_vector(intel_behavioural_ff_synchroniser_vector)
       generic map (DEST_SYNC_FF => DEST_SYNC_FF)
       port map (
           source_clk      => source_clk,
           destination_clk => destination_clk,
           in_data         => in_data,
           in_data_valid   => in_data_valid,
           out_data        => out_data,
           out_data_valid  => out_data_valid);
   ```

4. 在 `checker` 进程里：先把 `in_data` 设为 `x"8A"`、`in_data_valid => '1'`，等待 `DEST_SYNC_FF + 2` 个目的时钟周期，再用 `check_equal(out_data, x"8A")` 与 `check_equal(out_data_valid, '1')` 校验。

**需要观察的现象**：

- `out_data` 与 `out_data_valid` 应当在**同一个目的时钟上升沿**同时变为有效值（`x"8A"` / `'1'`），不会出现一个先到、一个后到。
- 从 `in_data` 稳定到 `out_data` 出现，约经过 `DEST_SYNC_FF` 个目的时钟节拍（外加源域寄存 1 拍）。

**预期结果**：两条 `check_equal` 均通过。精确的延迟节拍数「待本地验证」（取决于你选的 `DEST_SYNC_FF` 与时钟频率）。

**进阶观察**：把 `in_data_valid` 改成与 `in_data` 同步翻转（例如先 `'0'` 一段、再 `'1'` 并改 `in_data`），你会看到 `out_data_valid` 与 `out_data` 的新值**依然同拍**变化——这正是 valid 与数据同链同步的价值。

---

### 4.5 在 fifo_async 中同步格雷码指针

#### 4.5.1 概念说明

异步 FIFO（`fifo_async`）有两个自由奔跑的时钟：写时钟 `write_clk` 与读时钟 `read_clk`。写侧要判断「满了没」需要知道读指针，读侧要判断「空了没」需要知道写指针——而这两个指针分别在对方时钟域里更新。因此必须把**写指针搬到读域**、把**读指针搬到写域**，这正是 `ff_synchroniser_vector` 的用武之地。

那为什么搬的是**格雷码**而不是二进制指针？回到 4.1 的结论：撕裂只在 \(k \geq 2\)（多比特同翻）时发生。二进制指针加 1 时经常多比特同翻（如 `011→100`），跨域时会被撕裂成非法值。而格雷码保证**相邻两个值只有 1 个比特不同**（\(k \equiv 1\)），跨域时即便目的域采到的是「上一拍的旧值」，它依然是一个**合法的、曾经存在过的**指针，只是滞后了一拍——绝不会被撕裂成从未有过的组合。

换句话说：格雷码把「不可撕裂」这个性质焊死在了数据本身里，于是 `ff_synchroniser_vector` 即便逐比特各自同步也安全，连 valid 握手都可以省掉——所以 `fifo_async` 里 `in_data_valid` 直接恒接 `'1'`。

#### 4.5.2 核心流程：二进制 ↔ 格雷码

设 \(n\) 位二进制指针 \(b\)，其格雷码 \(g\) 定义为：

\[
g_i = b_i \oplus b_{i+1}\quad(\text{低位}),\qquad g_{\text{MSB}} = b_{\text{MSB}}
\]

等价地，\(g = b \oplus (b \gg 1)\)。反变换为：

\[
b_i = g_i \oplus b_{i+1}\quad(\text{由高位向低位递推}),\qquad b_{\text{MSB}} = g_{\text{MSB}}
\]

格雷码的核心性质：相邻整数对应的格雷码**汉明距离恒为 1**。所以指针 `+1` 时格雷码只翻转一个比特。

#### 4.5.3 源码精读

`fifo_async` 的 `own_behavioural_async_fifo` 架构里实现了这两个变换函数（[fifo_async.vhd:203-215](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L203-L215)）：

```vhdl
function binary_to_gray(binary: unsigned) return std_ulogic_vector is begin
    return std_ulogic_vector(binary xor ('0' & binary(binary'high downto 1)));
end function;

function gray_to_binary(gray: std_ulogic_vector) return unsigned is
    variable binary: unsigned(gray'range);
begin
    binary(gray'high) := gray(gray'high);
    for i in gray'high - 1 downto 0 loop
        binary(i) := binary(i + 1) xor gray(i);
    end loop;
    return binary;
end function;
```

`binary_to_gray` 正是 \(g = b \oplus (b \gg 1)\)（用 `'0' & binary(high downto 1)` 实现右移一位）；`gray_to_binary` 正是「最高位直通、其余位由高到低异或递推」。

随后两次例化 `ff_synchroniser_vector`，把写指针格雷码搬到读域、读指针格雷码搬到写域（[fifo_async.vhd:258-282](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L258-L282)）：

```vhdl
write_pointer_sync: entity work.ff_synchroniser_vector(xilinx_behavioural_ff_synchroniser_vector)
    generic map (DEST_SYNC_FF => CDC_SYNC_STAGES)
    port map (
        source_clk      => write_clk,
        destination_clk => read_clk,
        in_data         => write_pointer_gray,   -- 格雷码指针
        in_data_valid   => '1',                  -- ★ 恒有效：格雷码已免撕裂
        out_data        => write_pointer_gray_sync,
        out_data_valid  => open);                -- ★ 不关心 valid

read_pointer_sync: entity work.ff_synchroniser_vector(xilinx_behavioural_ff_synchroniser_vector)
    generic map (DEST_SYNC_FF => CDC_SYNC_STAGES)
    port map (
        source_clk      => read_clk,
        destination_clk => write_clk,
        in_data         => read_pointer_gray,
        in_data_valid   => '1',
        out_data        => read_pointer_gray_sync,
        out_data_valid  => open);
```

三个关键细节：

1. **`in_data => write_pointer_gray`**：传的是格雷码，不是二进制——这是免撕裂的根本。
2. **`in_data_valid => '1'`**：因为格雷码每拍都是合法指针，不需要 valid 来标识「数据可信」，故恒接 `'1'`。这是「valid 路线」与「格雷码路线」在此处的合流：选了格雷码，就不再需要 valid。
3. **`out_data_valid => open`**：既然 valid 恒为 `'1'`，输出的 `out_data_valid` 无意义，悬空。

> 提醒（承接 u2-l1）：这里两次例化都**硬编码**选了 `xilinx_behaviourral_ff_synchroniser_vector` 架构。这意味着号称「厂商无关」的 `own_behaviourral_async_fifo`，其叶子同步器其实被钉死在 Xilinx 实现上。移植到 Intel 时必须手动改这两处例化的架构名——这是本库「自研是分层的」这一特点的又一个证据。

搬过来的同步指针随后直接用于空满判定。空标志用「读格雷码指针 = 同步过来的写格雷码指针」判等（[fifo_async.vhd:284](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L284)）：

```vhdl
empty <= '1' when read_pointer_gray = write_pointer_gray_sync else '0';
```

注意这里**直接比较格雷码**而不先转回二进制——因为格雷码与二进制是一一映射，两个格雷码相等当且仅当对应的二进制相等，所以判等无需反变换，省掉一组逻辑。满标志的比较也类似地基于格雷码的高位特征，详见下一讲 u9-l4。

#### 4.5.4 小练习与答案

**练习 1**：假如把 `fifo_async` 里的 `in_data` 从 `write_pointer_gray` 改成 `write_pointer_binary`（二进制指针），会发生什么？为什么 `in_data_valid => '1'` 就不再安全？

> **参考答案**：二进制指针 `+1` 时会多比特同翻（如 `011→100` 三比特同翻），`ff_synchroniser_vector` 逐比特同步时各比特可能落到不同拍，目的域会看到一个**从未存在过的撕裂值**。由于 `in_data_valid` 恒为 `'1'`，目的域会把这个非法指针当成合法的来用，导致空满标志误判（可能漏判满导致溢写，或误判空导致重读）。格雷码之所以安全，正是因为它把「每次只翻一比特」写进了数据本身。

**练习 2**：`ff_synchroniser_vector` 的 Xilinx 架构里，`WIDTH` 被设成 `sync_chain_out'length` 而不是 `in_data'length`。为什么多出一位？这一位承载的是什么？

> **参考答案**：多出的一位承载 `in_data_valid`。源码用 `in_data_valid & in_data` 把 valid 拼到数据最高位之上组成 `(N+1)` 位向量，于是 `sync_chain_out` 的长度是 `in_data'length + 1`。这样 valid 与数据走**同一条**同步链，延迟严格一致，目的域里 `out_data_valid` 与 `out_data` 必然同拍出现。

**练习 3**：在 `fifo_async` 里，为什么判空可以直接 `read_pointer_gray = write_pointer_gray_sync`，而不需要先把格雷码转回二进制再比？

> **参考答案**：格雷码与二进制是一一映射，两个格雷码值相等当且仅当它们对应的二进制值相等。因此「格雷码相等」与「二进制相等」是等价条件，判等时无需反变换，可直接比格雷码，省掉一组异或链。

---

## 5. 综合实践

**任务**：把 4.4 的最小测试台扩展成一个「撕裂对照实验」，亲手看清「为何要格雷码」。

1. 例化**两个** `ff_synchroniser_vector`（Intel 架构），输入位宽都设为 4 位、`DEST_SYNC_FF => 2`、源时钟 100 MHz、目的时钟 25 MHz。
2. 给第一个同步器喂一个**二进制计数器**（每个源时钟 `+1`：`0000→0001→...→0111→1000→...`），`in_data_valid => '1'`。
3. 给第二个同步器喂同一个计数器的**格雷码**（用 4.5.3 的 `binary_to_gray` 转换后再喂），`in_data_valid => '1'`。
4. 在目的域用 `check`/波形观察两组 `out_data`。

**预期与观察**：

- 二进制那组：在多比特翻转的拍附近（尤其 `0111→1000`），目的域 `out_data` 会短暂出现源端从未产生过的中间值（撕裂）。
- 格雷码那组：目的域 `out_data` 永远是源端**曾经产生过**的某个值，至多滞后几拍，绝不出现非法中间值。

把这个对照写成一句话结论记录下来。精确波形「待本地验证」，但上述定性差异是格雷码的数学性质决定的，必然成立。

> 这个实验直接对应了本讲的两条主线：`ff_synchroniser_vector` 提供了「整条总线一起同步」的原语，而真正消除撕裂的是**格雷码**这个数据编码——二者结合，正是异步 FIFO 指针跨域的完整解法，下一讲 u9-l3 会把整条 FIFO 串起来讲。

## 6. 本讲小结

- 多比特总线跨时钟域会产生**数据撕裂**：多比特同翻时，逐比特独立同步会让目的域看到从未存在过的混搭值；根源是「各比特采样步调不一致」。
- `ff_synchroniser_vector` 把 `in_data_valid & in_data` 拼成 `(N+1)` 位向量**整条过同步链**，使 valid 与数据延迟严格一致、同拍到达——这是「valid + 数据同链同步」的硬件基础。
- 它有两套架构：Xilinx 版封装 `xpm_cdc_array_single`（需厂商库），Intel 版是手写的「源域寄存 + 目的域移位同步链」（厂商无关、可开箱仿真，本讲实践基于它）。
- `fifo_async` 用它把**写/读指针的格雷码**搬到对方时钟域；格雷码保证每次只翻一比特（\(k \equiv 1\)），撕裂天然不存在，所以 `in_data_valid` 恒接 `'1'`、`out_data_valid` 悬空。
- 判空可直接比格雷码（一一映射 ⇒ 格雷码相等当且仅当二进制相等），无需反变换。
- 注意移植陷阱：`own_behaviourral_async_fifo` 的叶子同步器被硬编码为 Xilinx 架构，移植到 Intel 须手动改这两处例化。

## 7. 下一步学习建议

- **u9-l3 异步 FIFO 与格雷码指针**：把本讲的同步器放回完整的 `own_behaviourral_async_fifo` 语境，看二进制↔格雷码指针、双时钟双口 RAM、满空标志如何拼成一座完整的异步 FIFO。
- **u9-l4 异步 FIFO 的满空标志与读指针重放**：本讲只讲了「判空比格雷码相等」，满标志的「MSB 不同 + 低位相同」判定与 `reset_read_pointer` 重放机制留在这篇细讲。
- **重读 u8-l1**：如果你对 Intel 架构里的 `preserve` / `altera_attribute` / 同步链级数还有疑问，回到单比特同步器那篇对照，两者结构完全同构、只是位宽从 1 扩到了 N。
