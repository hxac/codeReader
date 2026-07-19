# 并串/串并转换 par_ser / ser_par

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清「并行字 ↔ 串行比特流」之间相互转换的本质——一次移位、按比特收发。
- 读懂 `psi_common_par_ser`（并转串）与 `psi_common_ser_par`（串转并）这两个组件的端口、generic 与内部状态机。
- 掌握 `msb_g`（MSB/LSB 优先）与 `ratio_g`（速率比）这两个核心参数如何影响输出波形。
- 理解把两者首尾相连时为何要保证位序一致、如何用 `ld_o → ld_i` 对齐帧边界。
- 能用二进程 record 设计法（承接 u7-l1）的视角去阅读这两个文件的 `r / r_next`。

## 2. 前置知识

本讲假设你已经掌握以下内容（它们在前置讲义中已建立）：

- **AXI-S 握手与 vld 语义**（u1-l4）：`vld` 是「本拍数据有效」的单周期标志，psi_common 用 `vld_i/vld_o` 简写。
- **strobe / 选通与 ratio 计数**（u6-l1）：选通是单周期宽的「点名」脉冲，本质是分频计数器；计数比 `ratio = ⌈f_clk/f_strobe⌉`。本讲的 `par_ser` 正是用一个内部 ratio 计数器自生节拍，概念上和 `strobe_generator` 同源。
- **二进程 record 设计法**（u7-l1）：所有寄存器收进一个 record，组合进程 `proc_comb` 算次态 `r_next`、时序进程 `proc_seq` 仅打拍与复位。本讲两个组件都严格沿用这套写法。
- **math_pkg 的 log2ceil**（u2-l1）：用于由 `width_g` 推导计数器位宽。

补充两个本讲要用到的通俗概念：

- **并行（parallel）**：一个时钟周期里同时出现 `width_g` 个比特，写成 `std_logic_vector(width_g-1 downto 0)`。
- **串行（serial）**：一个时钟周期只传 1 个比特，靠时间顺序把一个字「铺」开。串行线少占管脚、便于跨片/跨接口，但要花更多周期。

## 3. 本讲源码地图

| 文件 | 作用 |
|:--|:--|
| [hdl/psi_common_par_ser.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_ser.vhd) | 并转串。把 `width_g` 位并行字按 `ratio_g` 节奏逐比特移出，带 `msb_g` 位序、`ld_o/frm_o` 帧标志与 `err_o` 过快保护。 |
| [hdl/psi_common_ser_par.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ser_par.vhd) | 串转并。逐比特收满 `width_g` 位（或被 `ld_i` 触发）后吐出一个并行字，事件驱动、无内部 ratio。 |
| [testbench/psi_common_par_ser_tb/psi_common_par_ser_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_par_ser_tb/psi_common_par_ser_tb.vhd) | `par_ser` 的自检 TB，**且把 `par_ser` 与 `ser_par` 首尾相连做往返自检**，是本讲时序对齐的最佳示例。 |
| [testbench/psi_common_ser_par_tb/psi_common_ser_par_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_ser_par_tb/psi_common_ser_par_tb.vhd) | `ser_par` 的独立 TB，文件头有一段 ASCII 时序图，清楚地展示了 `VLDi/LD_i/DATi → VLDo/DATo` 的对应关系。 |

`par_ser` 与 `ser_par` 是一对镜像组件，但**默认参数不同**（见 4.3），首尾相连时必须显式对齐。

---

## 4. 核心概念与源码讲解

### 4.1 par_ser：并行字 → 串行比特流

#### 4.1.1 概念说明

`par_ser` 解决的问题是：我手上有 `width_g` 位的并行数据，每来一个 `vld_i`（「装载」脉冲），就要把这个字拆成 `width_g` 个比特，一个一个地从 `dat_o`（单 bit）送出去。

它的核心是一根**移位寄存器**：每来一个内部节拍（`tick`），把寄存器里的最高位（或最低位）弹到输出，剩下的位补 0 继续移。和 u6-l1 的 `strobe_generator` 思想一致——只不过这里节拍不是给外部用的，而是**内部自生**、用来控制「每隔多少个时钟吐一个比特」。

它还提供四类附加输出：

- `vld_o`：每吐出一个有效比特拉高一拍（输出选通）。
- `ld_o`：一帧（一个字）的**第一个**比特处拉高，标记帧首。
- `frm_o`：一帧的**最后一个**比特处拉高，标记帧尾。
- `err_o`：新的 `vld_i` 在上一个字还没移完时就到达，拉高报错（防止覆盖）。

#### 4.1.2 核心流程

伪代码（`ratio_g > 1` 的情况，节拍由内部计数器生成）：

```
每个时钟上升沿：
  若 vld_i=1（新字到达）:
      把 dat_i 锁进移位寄存器 idat
      置 active=1，开始串行化
  若处于 active：
      内部 count 计数器自增
      当 count 到达 ratio_g-1：产生一个 tick，count 归零
      每个 tick：
          按方向移位 idat（MSB→左移补0 / LSB→右移补0）
          cnt 自增，vld_o:=1
          cnt=0        → ld_o:=1（帧首）
          cnt=width-1  → frm_o:=1（帧尾），active:=0（本字完成）
  若 vld_i=1 且 cnt≠0：
      err_o:=1（上一个还没移完）
```

`ratio_g` 决定「一个比特占多少个时钟周期」：`ratio_g=1` 时每个时钟吐一比特（全速）；`ratio_g=4` 时每 4 个时钟吐一比特（吞吐降为 1/4）。串行化一个完整字共需 `width_g × ratio_g` 个时钟周期。

> 注意：在 u6-l1 里 `ratio = ⌈f_clk/f_strobe⌉` 是「快/慢」之比；这里 `ratio_g` 的含义更直接——它就是**输出比特的周期长度（以时钟周期计）**，所以 `ratio_g` 越大输出越慢。

#### 4.1.3 源码精读

**entity 与 generic**（[hdl/psi_common_par_ser.vhd:L25-L39](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_ser.vhd#L25-L39)）：

```vhdl
generic(rst_pol_g : std_logic               := '1';
        msb_g     : boolean                 := true;   -- 默认 MSB 优先
        ratio_g   : natural range 1 to 4096 := 2;      -- 输出比特周期(时钟数)
        width_g   : natural                 := 16);
```

四个 generic：复位极性、位序、速率比、字宽。注意 `msb_g` 默认 `true`（MSB 优先），这与 `ser_par` 的默认值相反（见 4.3）。

**二进程 record**（[hdl/psi_common_par_ser.vhd:L43-L56](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_ser.vhd#L43-L56)）：所有寄存器（比特计数 `cnt`、移位寄存器 `dat/idat`、各标志、内部 ratio 计数器 `count` 与节拍 `tick`）都收进 `two_process_t`，承接 u7-l1 的写法。

**移位与位序选择**（`ratio_g > 1` 分支，[hdl/psi_common_par_ser.vhd:L134-L139](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_ser.vhd#L134-L139)）：

```vhdl
if msb_g then
  v.idat := r.idat(width_g - 2 downto 0) & '0';   -- 左移，补0，输出端取最高位
else
  v.idat := '0' & r.idat(width_g - 1 downto 1);   -- 右移，补0，输出端取最低位
end if;
```

`ratio_g = 1`（全速）分支里有一模一样的移位逻辑（[hdl/psi_common_par_ser.vhd:L170-L175](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_ser.vhd#L170-L175)）。

**输出位选择**（[hdl/psi_common_par_ser.vhd:L203](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_ser.vhd#L203)）：

```vhdl
dat_o <= r.dat(width_g - 1) when msb_g else r.dat(0);
```

MSB 优先时移位左移、取最高位；LSB 优先时移位右移、取最低位。这就是 `msb_g` 控制的全部内容。

**内部节拍生成**（[hdl/psi_common_par_ser.vhd:L68-L101](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_ser.vhd#L68-L101)）：当 `ratio_g > 1` 时，`count` 计数器自增，到达 `ratio_g-1` 归零并产生 `tick`；`tick` 就是「该吐下一个比特了」的内部选通。`ratio_g=1` 走另一条全速分支（[hdl/psi_common_par_ser.vhd:L157-L196](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_ser.vhd#L157-L196)），每个时钟都吐一比特、无需 `tick`。

**过快保护**（[hdl/psi_common_par_ser.vhd:L150-L155](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_ser.vhd#L150-L155)）：

```vhdl
if vld_i = '1' and r.cnt /= 0 then
  v.err := '1';        -- 上一个字还没移完(cnt没回到0)就来了新字
```

#### 4.1.4 代码实践

**实践目标**：把 16 位并行数据按 MSB 优先、`ratio_g=4` 串行化，预测并验证 `dat_o` 的比特序列与节拍。

**操作步骤**：

1. 阅读注册在 `sim/config.tcl` 中的既有运行组合（[sim/config.tcl:L215-L220](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L215-L220)）：

   ```
   "-glength_g=8  -gmsb_g=true  -gratio_g=4"
   "-glength_g=16 -gmsb_g=false -gratio_g=1"
   "-glength_g=32 -gmsb_g=true  -gratio_g=5"
   ```

   其中 `length_g` 是 TB 的 generic，在例化时映射到 DUT 的 `width_g`（[testbench/...par_ser_tb.vhd:L69-L73](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_par_ser_tb/psi_common_par_ser_tb.vhd#L69-L73)）。注意：**已经存在一条 `-glength_g=8 -gmsb_g=true -gratio_g=4`**，与本实践目标仅字宽不同，可先跑它建立直觉。

2. 用既有组合 `-glength_g=8 -gmsb_g=true -gratio_g=4` 运行回归（按 u1-l3 中的 Modelsim 流程 `compile_files -all` → `run_tb -all`）。

3. 在波形里观察 `dat_obs`（串行输出）与 `vld_obs`。激励在每个 `i` 处送入 `to_uslv(i, 8)`（[testbench/...par_ser_tb.vhd:L123-L126](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_par_ser_tb/psi_common_par_ser_tb.vhd#L123-L126)）。

**需要观察的现象**：

- 对输入字 `i=1`（即 `"00000001"`），MSB 优先应依次输出 `0,0,0,0,0,0,0,1`。
- `vld_obs` 每 4 个时钟脉冲出现一次（因为 `ratio_g=4`），共 8 个脉冲对应 8 个比特。
- `ld_obs` 在第一个比特处为高，`frm_obs` 在最后一个比特处为高。

**预期结果**（待本地验证）：把字宽改到 16 位（即加一条 `-glength_g=16 -gmsb_g=true -gratio_g=4` 的运行组合，或在交互仿真里用 `vsim -glength_g=16 -gmsb_g=true -gratio_g=4 work.psi_common_par_ser_tb` 覆盖默认值），则对输入 `"0000000000000010"`（=2）应输出 `15 个 0 后跟 1 个 1`，`vld_o` 间隔 4 拍、共 16 个脉冲。

#### 4.1.5 小练习与答案

**练习 1**：`par_ser` 设 `width_g=16, msb_g=true, ratio_g=4`，输入 `dat_i = x"8000"`（即 `1000...0`）。`dat_o` 的前两个有效比特分别是什么？整个字串行化需要多少个时钟周期？

**答案**：`x"8000"` = `1000_0000_0000_0000`。MSB 优先先送最高位，故第 1 个有效比特 = `1`，第 2 个有效比特 = `0`。整个字需 `width_g × ratio_g = 16 × 4 = 64` 个时钟周期。

**练习 2**：如果两个 `vld_i` 之间只隔了 5 个时钟（`ratio_g=4, width_g=8`），`err_o` 会怎样？

**答案**：会拉高报错。因为 8 个比特要 `8 × 4 = 32` 个时钟才能移完，5 个时钟远不够，第二个 `vld_i` 到达时 `cnt ≠ 0`，命中 [L150-L155](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_ser.vhd#L150-L155) 的过快保护条件。

---

### 4.2 ser_par：串行比特流 → 并行字

#### 4.2.1 概念说明

`ser_par` 是 `par_ser` 的反操作：从 `dat_i`（单 bit）逐比特收数据，每收满 `width_g` 位就拼成一个并行字从 `dat_o` 输出，并拉一拍 `vld_o`。

它和 `par_ser` 有两个关键不同：

1. **事件驱动，无内部 ratio**。它不自己产生节拍，而是**跟随 `vld_i`**：每个 `vld_i` 脉冲收一个比特。所以无论上游多快或多慢，它都按 `vld_i` 的节奏工作。
2. **靠 `ld_i` 显式对齐帧首**。`ld_i` 一来，就认为「一帧重新开始」，把当前累积值送出并归零计数器。

同样有 `err_o`：当 `ld_i` 到达但还没收满 `width_g-1` 位时报错。

#### 4.2.2 核心流程

```
每个时钟上升沿：
  把 dat_i/vld_i/ld_i 打一拍寄存(边沿对齐用)
  若 (上一拍 ld_i=1) 或 (cnt=width-1 且 上一拍 vld_i=1)：
      reg := dat            -- 把累积的移位结果送进输出寄存器
      vld_o := 1            -- 拼好了一个字
      cnt := 0              -- 重新开始
  若上一拍 vld_i=1：
      按方向把 i_dat 移进 dat（MSB→左移入低位 / LSB→右移入高位）
      cnt 自增
  若 ld_i 到达但 cnt≠width-1：
      err_o := 1            -- 帧被提前打断
```

注意输出取自独立的 `reg`（在拼字完成那一刻从移位寄存器 `dat` 拷贝过来），所以 `dat_o` 在两次 `vld_o` 之间是稳定保持的。

#### 4.2.3 源码精读

**entity 与 generic**（[hdl/psi_common_ser_par.vhd:L26-L38](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ser_par.vhd#L26-L38)）：

```vhdl
generic(rst_pol_g : std_logic := '1';
        width_g   : natural   := 16;
        msb_g     : boolean   := false);   -- 注意：默认 false，与 par_ser 相反
port(   ...
        dat_i     : in  std_logic;         -- 单比特串行输入
        ld_i      : in  std_logic;         -- 帧首/装载
        vld_i     : in  std_logic;         -- 每个有效比特的选通
        dat_o     : out std_logic_vector(width_g - 1 downto 0);
        err_o     : out std_logic;
        vld_o     : out std_logic);
```

**输入打一拍**（[hdl/psi_common_ser_par.vhd:L65-L68](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ser_par.vhd#L65-L68)）：`i_dat/i_vld/i_ld` 是输入的寄存版本，组合逻辑用的是 `r.i_*`（再延迟一拍），用来做可靠的边沿/采样对齐。

**拼字完成判定**（[hdl/psi_common_ser_par.vhd:L71-L80](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ser_par.vhd#L71-L80)）：

```vhdl
if r.i_ld='1' or (r.cnt = width_g - 1 and r.i_vld = '1') then
  v.cnt := (others => '0');
  v.reg := r.dat;      -- 移位结果送输出寄存器
  v.vld := '1';        -- 一个字拼好了
```

要么 `ld_i` 强制收尾，要么自然收满 `width_g` 位。

**移位与位序**（[hdl/psi_common_ser_par.vhd:L82-L89](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ser_par.vhd#L82-L89)）：

```vhdl
if msb_g then
  v.dat := r.dat(width_g - 2 downto 0) & r.i_dat;   -- 左移，新比特落 LSB
else
  v.dat := r.i_dat & r.dat(width_g - 1 downto 1);   -- 右移，新比特落 MSB
end if;
```

MSB 优先时左移、新比特落在低位（先到的比特最终被推到最高位）；LSB 优先时右移、新比特落在高位。这与 `par_ser` 的移位方向**配套**——首尾相连时两侧 `msb_g` 必须一致，才能完整还原原字（见 4.4）。

**TB 文件头的 ASCII 时序图**（[testbench/...ser_par_tb.vhd:L7-L19](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_ser_par_tb/psi_common_ser_par_tb.vhd#L7-L19)）极其直观，建议直接打开看：`VLDi` 周期性到来、`LD_i` 在帧首拉一拍、`DATi` 逐比特变化、收满后 `VLDo` 拉一拍并送出 `DATo` 并行字。

#### 4.2.4 代码实践

**实践目标**：单独理解 `ser_par` 的事件驱动特性，验证它「不靠自己产生节拍」。

**操作步骤**：

1. 打开 `ser_par_tb`，注意激励的两种 testcase：
   - TESTCASE 1「Max speed」（[L148-L154](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_ser_par_tb/psi_common_ser_par_tb.vhd#L148-L154)）：`vld_i` 每拍都高（全速）。
   - TESTCASE 2「vld lower speed」（[L158-L175](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_ser_par_tb/psi_common_ser_par_tb.vhd#L158-L175)）：用一个 `cnt_v=9` 的计数器（[L102-L115](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_ser_par_tb/psi_common_ser_par_tb.vhd#L102-L115)）每 10 拍才给一次 `vld_i`（低速）。
2. 运行 `ser_par_tb`（`config.tcl` 中已注册，[L222-L223](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L222-L223)）。

**需要观察的现象**：无论 `vld_i` 是每拍一次还是每 10 拍一次，`dat_o/vld_o` 的**拼接逻辑完全一致**——只是耗时不同。这印证了 `ser_par` 没有内部 ratio，完全跟随 `vld_i`。

**预期结果**（待本地验证）：两个 testcase 的最终 `assert unsigned(dat_obs) = ...` 都应通过（不打印 `###ERROR###`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ser_par` 没有 `ratio_g` 这个 generic？

**答案**：因为它是事件驱动、跟随 `vld_i` 收比特，不自己产生节拍。速率由上游决定，组件本身不关心「每几拍收一个比特」。

**练习 2**：`ser_par` 的 `dat_o` 在两次 `vld_o` 之间会变化吗？

**答案**：不会。`dat_o` 取自输出寄存器 `r.reg`（[L105](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ser_par.vhd#L105)），只在拼字完成那一刻从移位寄存器 `dat` 拷贝一次（[L73](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ser_par.vhd#L73)），两次 `vld_o` 之间稳定保持。

---

### 4.3 msb/lsb 与 ratio：两个核心参数

#### 4.3.1 概念说明

这两个组件的行为几乎完全由两个参数决定，理清它们就读懂了大半。

**`msb_g`（位序）**——决定先送/先收哪一个比特：

| `msb_g` | `par_ser` 行为 | `ser_par` 行为 |
|:--|:--|:--|
| `true`  | MSB 先送（左移，输出最高位） | 接收到的第一个比特最终落在 MSB |
| `false` | LSB 先送（右移，输出最低位） | 接收到的第一个比特最终落在 LSB |

源码注释（[hdl/psi_common_par_ser.vhd:L12-L13](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_ser.vhd#L12-L13)）的原话是：「Data bit 0 is sent last when `msb_g` set true; if false bit 0 sent first」——MSB 优先时 bit 0 最后送，LSB 优先时 bit 0 最先送。

> ⚠️ **默认值陷阱**：`par_ser` 的 `msb_g` 默认 `true`，而 `ser_par` 的 `msb_g` 默认 `false`（[hdl/psi_common_ser_par.vhd:L29](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ser_par.vhd#L29)）。首尾相连时**绝不能两边都吃默认值**，否则收到的字会是镜像/错位的。

**`ratio_g`（速率比，仅 `par_ser` 有）**——决定输出比特的周期：

- `ratio_g = 1`：全速，每个时钟吐一比特。
- `ratio_g = N`（N>1）：每 N 个时钟吐一比特，吞吐降为 1/N。
- 串行化一个 `width_g` 位的字共需 `width_g × ratio_g` 个时钟周期。
- 取值范围 `1..4096`（[hdl/psi_common_par_ser.vhd:L28](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_ser.vhd#L28)）。

#### 4.3.2 核心流程：`ratio_g` 的两条分支

`par_ser` 在组合进程里用 `if ratio_g > 1 then ... else ...` 分成两套实现（[L69 与 L158](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_ser.vhd#L157-L196)）：

- **全速分支（`ratio_g=1`）**：不需要 `tick` 节拍，每个时钟直接移位输出。
- **分频分支（`ratio_g>1`）**：用 `count` 计数器产生 `tick`，每个 `tick` 才移位一次。

这是典型的「编译期 `if` 分支」——综合后只会留下实际 `ratio_g` 选中的那一套逻辑，另一套不存在，不浪费资源。

#### 4.3.3 源码精读

`ratio_g` 的取值约束在 generic 声明里直接写死（[hdl/psi_common_par_ser.vhd:L28](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_ser.vhd#L28)）：

```vhdl
ratio_g   : natural range 1 to 4096 := 2;
```

`msb_g` 是 `boolean`，在移位与输出两处用 `if msb_g then` 选择（移位 [L135-L139](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_ser.vhd#L134-L139)、输出 [L203](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_ser.vhd#L203)）。因为 `msb_g` 是编译期常量，这两个 `if` 在综合后同样退化为单条赋值。

#### 4.3.4 代码实践

**实践目标**：通过修改 `msb_g` 与 `ratio_g`，直观对比输出波形差异。

**操作步骤**：用 `config.tcl` 已注册的三条组合分别跑一次（[L217-L219](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L217-L219)）：

1. `length=8, msb=true, ratio=4`：MSB 优先、慢节拍。
2. `length=16, msb=false, ratio=1`：LSB 优先、全速。
3. `length=32, msb=true, ratio=5`：MSB 优先、更慢节拍。

**需要观察的现象**：对比 `dat_obs` 波形——`msb=true` 的运行里，对同一个输入字，最先出现在 `dat_o` 上的是最高位；`msb=false` 的运行里是最先出现的是最低位。`ratio=1` 的运行里 `vld_obs` 每拍都高，`ratio=4/5` 的运行里 `vld_obs` 明显稀疏。

**预期结果**（待本地验证）：三条运行组合都应无 `###ERROR###` 完成；`vld_obs` 的脉冲间距与 `ratio_g` 一致。

#### 4.3.5 小练习与答案

**练习**：为什么 `ratio_g` 在 `par_ser` 里写成两条 `if ratio_g > 1` 分支，而不是统一用 `tick` 处理？

**答案**：全速（`ratio_g=1`）时每个时钟都要吐比特，用 `tick` 反而多此一举；单独写一条全速分支可以让综合后逻辑更精简。而 `ratio_g` 是编译期常量，那条未选中的分支会被综合器删除，不占资源。

---

### 4.4 时序对齐：把 par_ser 与 ser_par 首尾相连

#### 4.4.1 概念说明

实际工程里，最常见的用法是把 `par_ser` 和 `ser_par` 首尾连起来——比如把并行数据串行化后送过一条单 bit 线（或光纤、或 SPI、或 TDM 通道），对端再 `ser_par` 还原成并行字。这要求两件事：

1. **位序一致**：发送端 `msb_g` 与接收端 `msb_g` 必须相同，否则收到的字是错位的。
2. **帧首对齐**：接收端要知道「从哪个比特开始算新的一帧」。这正是 `par_ser.ld_o → ser_par.ld_i` 这根线的用途。

#### 4.4.2 核心流程

`par_ser` 的 `ld_o` 在每个字的第一个串行比特处拉高一拍；把它接到 `ser_par` 的 `ld_i`，接收端就拿到了帧首标志。其余三根线一一对应：

```
par_ser.dat_o  ──>  ser_par.dat_i     (串行比特)
par_ser.vld_o  ──>  ser_par.vld_i     (每比特选通)
par_ser.ld_o   ──>  ser_par.ld_i      (帧首)
                  ser_par.dat_o/vld_o  (还原的并行字 + 字选通)
```

`ratio_g` 只影响发送端的节拍；接收端事件驱动，自然跟随 `vld_i`，所以两侧 `ratio_g` 不需要「配对」——只要发送端按 `ratio_g` 节奏送出，接收端就能跟上。

#### 4.4.3 源码精读

`par_ser_tb` 正是这样一台「往返自检机」（[testbench/...par_ser_tb.vhd:L68-L96](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_par_ser_tb/psi_common_par_ser_tb.vhd#L68-L96)）：

- `inst_dut`（`par_ser`）的 `dat_o/vld_o/ld_o` 直接接到 `inst_dut2`（`ser_par`）的 `dat_i/vld_i/ld_i`。
- 两个 DUT 共享同一个 `msb_g` generic（[L70-L73](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_par_ser_tb/psi_common_par_ser_tb.vhd#L70-L73) 与 [L85-L88](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_par_ser_tb/psi_common_par_ser_tb.vhd#L85-L88)），保证位序一致。
- 检查进程（[L99-L110](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_par_ser_tb/psi_common_par_ser_tb.vhd#L99-L110)）：每收到一个 `vld_dut2_obs`，断言还原字 `dat_dut2_obs` 等于原始发送序号 `i-1`，不等就报 `###ERROR###`。

这就是「往返自检」：并行字 → 串行线 → 还原回同一个并行字。

#### 4.4.4 代码实践

**实践目标**：验证往返链路在位序匹配/不匹配时的行为差异。

**操作步骤**：

1. 运行既有组合 `-glength_g=8 -gmsb_g=true -gratio_g=4`（[L217](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L217)）。注意 TB 把 `msb_g` 同时传给两个 DUT，所以天然匹配。
2. **思考实验（不改动源码）**：若把 `ser_par` 那一侧的 `msb_g` 故意取反（即两侧不一致），往返后 `dat_dut2_obs` 会变成什么？

**需要观察的现象**：

- 位序匹配时：`dat_dut2_obs` 逐字等于输入 `0,1,2,...`，无 `###ERROR###`。
- 位序不匹配（思考实验）：`dat_dut2_obs` 会是输入字的**比特反转**（MSB↔LSB 镜像），`assert` 将失败并打印 `###ERROR###`。

**预期结果**（待本地验证）：第 1 步应全部通过；第 2 步可由读者在交互仿真里手工改例化处验证，预期断言失败。

#### 4.4.5 小练习与答案

**练习 1**：往返链路中，`par_ser` 的 `ratio_g=5`，`ser_par` 没有 `ratio_g`。这会出问题吗？

**答案**：不会。`ratio_g` 只决定发送端每 5 拍送一个比特；接收端 `ser_par` 事件驱动，每个到达的 `vld_i`（=`par_ser.vld_o`）收一个比特。只要 `vld_i` 的节奏被 `ser_par` 跟得上（它本来就跟随 `vld_i`），就能正确还原。

**练习 2**：如果把 `par_ser.ld_o` 到 `ser_par.ld_i` 的连线断开，会发生什么？

**答案**：`ser_par` 失去显式帧首标志，只能靠「收满 `width_g` 位」自然成帧。如果比特流严格连续且对齐，仍可能还原；但一旦丢一个比特或起点错位，后续所有字都会错位。`ld_i` 的作用就是提供一个确定的帧边界，避免这种累积错位。

---

## 5. 综合实践

**任务**：用 `par_ser` 把一个 16 位并行计数器（`0,1,2,3,...`）按 MSB 优先、`ratio_g=4` 串行化，再用 `ser_par` 还原，画出关键信号波形并解释每个时序点。

**步骤**：

1. **阅读激励**：`par_ser_tb` 的 `proc_stim`（[L113-L150](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_par_ser_tb/psi_common_par_ser_tb.vhd#L113-L150)）就是这样一个计数器激励：循环送 `to_uslv(i, length_g)`，每个字送出后等待整字串行化完成（`for j in 0 to length_g loop ... wait until vld_obs='1'`）。
2. **运行**：在 `config.tcl` 里新增一条运行组合 `-glength_g=16 -gmsb_g=true -gratio_g=4`（或交互仿真用 `vsim -glength_g=16 -gmsb_g=true -gratio_g=4`），跑完整回归。
3. **画波形**：以输入字 `i=1`（`"0000000000000001"`）为例，画出 `clk / vld_i / dat_i(并行) / dat_obs(串行) / vld_obs / ld_obs / frm_obs / dat_dut2_obs / vld_dut2_obs`，标注：
   - `ld_obs` 在第 1 个串行比特处为高；
   - `vld_obs` 每 4 拍一次、共 16 次；
   - `frm_obs` 在第 16 个串行比特处为高；
   - 收满 16 比特后 `vld_dut2_obs` 拉一拍，`dat_dut2_obs = "0000000000000001"`（与输入一致，证明往返正确）。
4. **解释**：用一句话说明为什么 `ratio_g=4` 时一个字要 `16×4=64` 拍才能送完，而 `ser_par` 端不需要知道这个 64。

**预期结果**（待本地验证）：回归无 `###ERROR###`；`dat_dut2_obs` 逐字等于输入计数器值。

---

## 6. 本讲小结

- `par_ser` 与 `ser_par` 是一对镜像式移位转换器：一个把并行字拆成串行比特流，一个把串行比特流拼回并行字。
- `par_ser` 用内部 ratio 计数器自生节拍（概念同 u6-l1 的 strobe），`ratio_g` 就是「一个比特占多少个时钟周期」；`ser_par` 事件驱动、跟随 `vld_i`，没有 `ratio_g`。
- `msb_g` 控制位序（MSB 先/LSB 先），在移位与输出两处用编译期 `if` 选择；**两个组件的 `msb_g` 默认值相反**，首尾相连必须显式对齐。
- 两个组件都严格沿用 u7-l1 的二进程 record 设计法（`r/r_next` + `proc_comb/proc_seq`）。
- `par_ser.ld_o → ser_par.ld_i` 提供帧首对齐，防止累积错位；`err_o` 在「上一字未完成就来新字」时报错。
- 既有 TB 已经把 `par_ser → ser_par` 接成往返自检链路，是验证位序与时序对齐的最佳参考。

## 7. 下一步学习建议

- 若你的应用是**多通道同速率时分复用**而非单字串行化，下一步阅读 u8-l2（`par_tdm/tdm_par`）与 u8-l3（cfg 变体与 `tdm_mux`），它们用「隐式通道循环」处理 N 路并行 ↔ TDM 串行的转换，是本讲思想的多路扩展。
- 若需要**跨同步整数比时钟域**同时换宽（而非同域串行化），阅读 u5-l3（`sync_cc_n2xn/xn2n`），对比它与本讲 `par_ser` 的区别——前者跨时钟域，后者不跨。
- 想深入理解「节拍自生」的更通用形式，回顾 u6-l1 的 `strobe_generator/strobe_divider`，本讲 `par_ser` 的 `count/tick` 就是它的内联简化版。
- 若要在 AXI 总线路径上插入时序/位宽处理，可继续阅读 u9（SPI/I2C/AXI 接口）单元，那里会用到本讲的串行化思想（如 SPI 主机本质也是并串/串并转换）。
