# 脉冲/斜坡生成与整形

## 1. 本讲目标

学完本讲后，读者应能：

- 说清「斜坡（ramp）」与「脉冲（pulse）」在数字电路里的区别，以及为什么需要专门的发生器与整形器。
- 读懂 `psi_common_ramp_gene` 的四态状态机，理解斜坡速度由「步进幅度 × 选通频率」共同决定。
- 理解 `psi_common_pulse_generator_ctrl_static` 如何用「选通发生器 + 斜坡发生器」组合出梯形脉冲，并把「步数」换算成「步进」。
- 掌握 `psi_common_pulse_shaper` 的脉宽拉伸与 hold-off（静默）机制，以及 `pulse_shaper_cfg` 把脉宽/静默时间搬到运行时端口的做法。
- 能够独立实例化上述组件并预测其输出波形。

## 2. 前置知识

本讲假设读者已经掌握：

- **AXI-S 握手与选通（strobe）**（u1-l4、u6-l1）：单周期宽的「点名」脉冲，用于驱动按拍动作的电路。本讲组件几乎都「选通驱动」——没有选通，累加器不动。
- **二进程 record 设计法**（u7-l1）：所有寄存器收进一个 record，`r` 表现态、`r_next`（或 `rin`）表次态，组合进程算次态、时序进程只打拍与复位。本讲四个组件全部沿用此法。
- **边沿检测**：把信号打一拍，用「当前为 1、上一拍为 0」识别上升沿——这是本讲所有「触发」逻辑的基础。
- **math_pkg 工具函数**（u2-l1）：`log2ceil` 推位宽、`to_uslv`/`from_uslv` 整数与向量互转、`choose` 在端口声明区充当三元运算符。

两个本讲常用术语：

- **步进（increment / step）**：每个选通周期内数值的增减量，决定斜坡的「陡峭程度」。
- **hold-off（静默时间）**：识别到一个脉冲后，强制忽略后续输入脉冲的最小时钟周期数，用于限制最高脉冲速率。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [hdl/psi_common_ramp_gene.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ramp_gene.vhd) | 斜坡发生器：按选通节拍把累加器向目标值步进，四态状态机管理上坡/平顶/下坡 |
| [hdl/psi_common_pulse_generator_ctrl_static.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_generator_ctrl_static.vhd) | 组合式梯形脉冲发生器：内部例化 strobe_generator + ramp_gene，用 generic 定义各相位步数 |
| [hdl/psi_common_pulse_shaper.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_shaper.vhd) | 脉冲整形器：把任意宽度的输入脉冲拉成固定宽度，并支持 hold-off 限速 |
| [hdl/psi_common_pulse_shaper_cfg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_shaper_cfg.vhd) | 运行时可配置版整形器：脉宽与 hold-off 由端口寄存器在运行时设定 |

四个组件的依赖关系：

```
strobe_generator ──┐
                   ├──> pulse_generator_ctrl_static   (上层包装)
ramp_gene ─────────┘
ramp_gene  <──────────────────────────────────┘

pulse_shaper ──> pulse_shaper_cfg (运行时配置变体)
```

`ramp_gene` 与 `pulse_shaper` 是两个独立的叶子组件；`pulse_generator_ctrl_static` 是 `ramp_gene`（外加 `strobe_generator`）的上层包装，`pulse_shaper_cfg` 是 `pulse_shaper` 的运行时配置变体。

## 4. 核心概念与源码讲解

### 4.1 ramp_gene：斜坡发生器

#### 4.1.1 概念说明

斜坡发生器解决的问题是：让一个数值从当前值「平滑地」（按固定步长）走到目标值，而不是瞬间跳变。这在 DAC 驱动、电机加减速、激光器泵浦等场景很常见——瞬变会引起过冲或毛刺，而按拍逐步逼近则受控。

`psi_common_ramp_gene` 的核心是一个**累加器 + 四态状态机**。关键设计是：累加器**只在选通 `vld_i` 有效的拍**才步进。因此斜坡的「速度」由两个独立量共同决定：

\[
v_{\text{ramp}} \;=\; \Delta \cdot f_{\text{str}}
\]

其中 \(\Delta\) 是 `ramp_inc_i`（每个选通的幅度步进），\(f_{\text{str}}\) 是 `vld_i` 的频率。这一点把它和 u6-l1 的选通发生器天然衔接：用一个固定频率的选通去驱动 `vld_i`，斜坡就匀速爬升。改步进或改选通频率都能调速。

#### 4.1.2 核心流程

状态机四态，作者用 Gray 编码（`"00 01 11 10"`）使每次跳转只翻一位：

```
         ramp_cmd            到达目标          ramp_cmd(新目标更低)
 zero ──────────────> up ──────────────> flat ──────────────> dw
  00                  01                  11                  10
  ^                                                                |
  |   init_cmd (任何状态)                  init_cmd                |
  └────────────────────────────────────────────────────────────────┘
                  flat 收到新 ramp_cmd：新目标高 -> up，低 -> dw
```

四态含义：

- **zero (00)**：复位态，累加器 = `init_val_g`，收到 `ramp_cmd_i` 走向 `up`。
- **up (01)**：每个 `vld_i` 把累加器加 `ramp_inc_i`；离目标一步之遥时钳到目标值并转 `flat`。
- **flat (11)**：保持当前值；收到新 `ramp_cmd_i` 时比较「新目标 vs 当前值」决定走 `up` 还是 `dw`。
- **dw (10)**：每个 `vld_i` 把累加器减 `ramp_inc_i`；到达目标转 `flat`。

`init_cmd_i` 在 `up`/`flat`/`dw` 任何状态都立即回到 `zero`。

#### 4.1.3 源码精读

实体定义与 generic 见 [psi_common_ramp_gene.vhd:L28-L43](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ramp_gene.vhd#L28-L43)：

```vhdl
generic(width_g    : natural   := 16;    -- 累加器位宽
        is_sign_g  : boolean   := false; -- 有/无符号
        rst_pol_g  : std_logic := '1';
        init_val_g : integer   := 50);   -- 上电初值
```

状态机类型用 `enum_encoding` 强制 Gray 编码，[psi_common_ramp_gene.vhd:L48-L50](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ramp_gene.vhd#L48-L50)：

```vhdl
type fsm_t is (zero, up, flat, dw);
attribute enum_encoding : string;
attribute enum_encoding of fsm_t : type is "00 01 11 10";
```

`up` 态的步进与「临近目标」判定是全组件最关键的几行，见 [psi_common_ramp_gene.vhd:L84-L91](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ramp_gene.vhd#L84-L91)：

```vhdl
if vld_i = '1' then
  v.pulse := r.pulse + resize(unsigned(ramp_inc_i), width_g + 1);
  if r.pulse >= resize(unsigned(tgt_lvl_i) - unsigned(ramp_inc_i), width_g + 1)
     or (unsigned(tgt_lvl_i) <= unsigned(ramp_inc_i)) then
    v.fsm_state := flat;
    v.pulse     := resize(unsigned(tgt_lvl_i), width_g + 1);
  end if;
end if;
```

两个判定条件的作用：

- `r.pulse >= tgt - inc`：当前值离目标已不足一步，再加就会**过冲**，于是直接钳到 `tgt_lvl_i` 并转 `flat`。
- `tgt <= inc`：退化情形（目标本身不大于一个步进），立即到达。

`flat` 态的方向抉择，[psi_common_ramp_gene.vhd:L112-L125](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ramp_gene.vhd#L112-L125)：新 `ramp_cmd` 来时，比较新目标与当前值——目标更高走 `up`，更低走 `dw`。这正是测试平台「周期性上下斜坡」的驱动方式：每个平顶改写一次 `tgt_lvl_i` 再发一次 `ramp_cmd` 即可。

注意累加器内部用 `width_g+1` 位（record 中 `pulse : unsigned(width_g downto 0)`，见 [psi_common_ramp_gene.vhd:L52-L58](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ramp_gene.vhd#L52-L58)），多出的一位留作加法/比较的余量，输出只取低 `width_g` 位，见 [psi_common_ramp_gene.vhd:L179-L187](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ramp_gene.vhd#L179-L187)。

选通的输出对齐：组合进程里 `v.str_s := vld_i;`，[psi_common_ramp_gene.vhd:L154](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ramp_gene.vhd#L154)，经寄存后 `vld_o` 与 `puls_o` 同拍出现——每个 `vld_o` 脉冲都对应一个 freshly 步进后的 `puls_o`，便于下游按拍消费。时序进程只做打拍与同步复位，[psi_common_ramp_gene.vhd:L160-L173](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ramp_gene.vhd#L160-L173)，是标准二进程 record 法。

#### 4.1.4 代码实践

**实践目标**：实例化 `ramp_gene`，生成 0 → 8103 → 2000 的周期斜坡，并说明步进参数含义。

**操作步骤**：

1. 新建一个顶层，用 `strobe_generator`（u6-l1）生成 2 MHz 选通喂给 `vld_i`：

```vhdl
-- 示例代码（非项目原有）：选通驱动斜坡
inst_str : entity work.psi_common_strobe_generator
  generic map(freq_clock_g => 100.0e6, freq_strobe_g => 2.0e6, rst_pol_g => '1')
  port map(clk_i => clk, rst_i => rst, sync_i => '0', vld_o => str);

inst_ramp : entity work.psi_common_ramp_gene
  generic map(width_g => 16, is_sign_g => false, rst_pol_g => '1', init_val_g => 0)
  port map(clk_i => clk, rst_i => rst, vld_i => str,
           tgt_lvl_i => tgt, ramp_inc_i => inc,
           ramp_cmd_i => cmd, init_cmd_i => '0',
           sts_o => sts, vld_o => ramp_str, puls_o => ramp_val);
```

2. 参考已有测试平台 [psi_common_ramp_gene_tb.vhd:L191-L201](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_ramp_gene_tb/psi_common_ramp_gene_tb.vhd#L191-L201)：它先令 `ramp_inc=100`、`tgt=8103` 走 `up`，再令 `ramp_inc=300`、`tgt=2000` 走 `dw`，循环 4 次。该 TB 已在 `sim/config.tcl` 注册（`create_tb_run "psi_common_ramp_gene_tb"`），可用 u1-l3 的回归流程直接运行。

**需要观察的现象**：

- `sts_o` 在 `01`(up) → `11`(flat) → `10`(dw) → `11`(flat) 之间循环。
- `puls_o` 呈阶梯形：每个 `vld_o` 脉冲加 100（上坡）或减 300（下坡）。
- 平顶期 `puls_o` 恒等于 `tgt_lvl_i`，不会过冲。

**预期结果**：上坡到 8103 约需 \(\lceil 8103/100 \rceil = 82\) 个选通；以 2 MHz 选通计，时长 \(\approx 82 / (2\,\text{MHz}) \approx 41\,\mu s\)。**步进参数含义**：`ramp_inc_i` 是「每个选通的幅度增量」，`vld_i` 频率是「每秒走多少步」，二者乘积即每秒的数值变化量。

**待本地验证**：上坡末尾是否精确落在 8103（由「钳位」逻辑保证，不会跳到 8200）；可在 TB 的自检断言（[L72](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_ramp_gene_tb/psi_common_ramp_gene_tb.vhd#L72)、[L75-L77](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_ramp_gene_tb/psi_common_ramp_gene_tb.vhd#L75-L77)）里确认。

#### 4.1.5 小练习与答案

**练习 1**：把 `ramp_inc_i` 从 100 改成 50，其余不变，上坡时间会如何变化？
**答案**：步进减半，所需选通数翻倍。此时 \(\lceil 8103/50 \rceil = 163\) 个选通 ≈ 81.5 μs（2 MHz 选通下），约为原来的两倍。

**练习 2**：为何累加器用 `width_g+1` 位，而输出只取 `width_g` 位？
**答案**：多出的一位为加法与「目标 − 步进」减法提供余量，避免中间比较时下溢/溢出；最终输出截到用户位宽。

---

### 4.2 pulse_generator_ctrl_static：组合式梯形脉冲发生器

#### 4.2.1 概念说明

`ramp_gene` 只管「从 A 走到 B」，要产生一个完整的**梯形脉冲**（0 → 满量程 → 保持 → 0 → 保持），需要外层逻辑协调上坡、平顶高、下坡、平顶低四个相位。`psi_common_pulse_generator_ctrl_static` 就是这层包装：它内部例化一个 `strobe_generator` 提供节拍，再例化一个 `ramp_gene` 执行实际的数值步进。

关键思路是把 `ramp_gene` 的步进逻辑「反过来用」：`ramp_gene` 是「给定步进、走多少步随之确定」；而这个组件是「**给定步数**、反推步进幅度」。

#### 4.2.2 核心流程

四个相位各自由一个步数 generic 描述（单位是「选通数」）：

| 相位 | generic | 含义 |
|---|---|---|
| 上坡 | `nb_step_up_g` | 从 0 爬到满量程所需的选通数 |
| 平顶高 | `nb_step_flh_g` | 保持在满量程的选通数 |
| 下坡 | `nb_step_dw_g` | 从满量程降回 0 的选通数 |
| 平顶低 | `nb_step_fll_g` | 保持在 0 的选通数 |

满量程 \(F = 2^{\text{width\_g}} - 1\)。反推出的步进幅度：

\[
\Delta_{\text{up}} = \text{round}\!\left(\frac{F}{\text{nb\_step\_up\_g}}\right), \qquad
\Delta_{\text{dw}} = \text{round}\!\left(\frac{F}{\text{nb\_step\_dw\_g}}\right)
\]

整个循环里 `busy_o` 持续为高，只有在「平顶低」结束、回到 0 后才拉低；此时若 `trig_i` 仍为高（电平），则自动启动下一轮——这就是文档所说「`trig_i` 保持高则连续发脉冲」。

#### 4.2.3 源码精读

步进幅度在综合期由常量算出，[pulse_generator_ctrl_static.vhd:L57-L61](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_generator_ctrl_static.vhd#L57-L61)：

```vhdl
constant tgt_lvl_c     : std_logic_vector(...) := to_uslv(2**width_g-1, width_g);
constant inc_step_up_c : integer := integer(round(real(from_uslv(tgt_lvl_c))/real(nb_step_up_g)));
constant inc_step_dw_c : integer := integer(round(real(from_uslv(tgt_lvl_c))/real(nb_step_dw_g)));
```

这里综合期用到了 `math_pkg` 的 `to_uslv`/`from_uslv`，以及 `array_pkg` 的 `t_ainteger`（`step_array_c`，[L57-L58](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_generator_ctrl_static.vhd#L57-L58)）配 `max_a` 求计数器上限——典型的「包协作」。

内部例化两个子组件，[pulse_generator_ctrl_static.vhd:L79-L107](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_generator_ctrl_static.vhd#L79-L107)：

```vhdl
inst_strobe : entity work.psi_common_strobe_generator
  generic map(freq_clock_g => clk_freq_g, freq_strobe_g => str_freq_g, ...)
  port map(..., sync_i => trig_i, vld_o => str_s);

inst_pulse : entity work.psi_common_ramp_gene
  generic map(width_g => width_g, is_sign_g => false, init_val_g => 0)
  port map(..., vld_i => str_s, tgt_lvl_i => r.lvl, ramp_inc_i => r.inc_val,
               ramp_cmd_i => r.ramp_cmd, init_cmd_i => r.init_cmd, ...);
```

`strobe_generator` 的 `sync_i` 接 `trig_i`——触发沿会把选通计数器相位对齐（u6-l1），使斜坡从干净的节拍开始。

外层 FSM 的核心是「平顶计数器」。平顶高/低的保持时长靠 `count` 递减，[pulse_generator_ctrl_static.vhd:L140-L163](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_generator_ctrl_static.vhd#L140-L163)：

```vhdl
elsif sts_s = "11" then      -- ramp_gene 处于 flat
  if str_s = '1' then
    if r.count /= 0 then
      v.count := r.count - 1;            -- 还在保持
    else
      if dat_s = to_uslv(0, width_g) then  -- 已回到 0（平顶低结束）
        v.busy := '0';
        if trig_i = '1' then              -- 电平触发：自动重启
          v.lvl     := to_uslv(2**width_g-1, width_g);
          v.inc_val := to_uslv(inc_step_up_c, width_g);
          v.ramp_cmd := '1';
        end if;
      else                                -- 还在满量程（平顶高结束）→下坡
        v.lvl     := (others => '0');
        v.inc_val := to_uslv(inc_step_dw_c, width_g);
        v.ramp_cmd := '1';
      end if;
    end if;
  end if;
```

`count` 的初值在每次「刚离开 flat 进入 up/dw」时设定，[pulse_generator_ctrl_static.vhd:L165-L170](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_generator_ctrl_static.vhd#L165-L170)：上坡结束（`sts="01"` 且上拍 `="11"`）装载 `nb_step_flh_g-1`，下坡结束（`sts="10"` 且上拍 `="11"`）装载 `nb_step_fll_g-1`。`stop_i` 则立即把 `ramp_gene` 拉回 `zero`，实现中途夭折，[pulse_generator_ctrl_static.vhd:L121-L124](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_generator_ctrl_static.vhd#L121-L124)。

#### 4.2.4 代码实践

**实践目标**：用本组件产生一个周期约 1 ms 的梯形脉冲，并预测各相位时长。

**操作步骤**：

1. 实例化时设 `clk_freq_g => 100.0e6`、`str_freq_g => 10.0e6`（选通周期 100 ns）、`width_g => 16`。
2. 取 `nb_step_up_g => 1000`、`nb_step_flh_g => 4000`、`nb_step_dw_g => 1000`、`nb_step_fll_g => 4000`。
3. 把 `trig_i` 拉高保持。

**需要观察的现象**：`dat_o` 从 0 升到 65535、保持、降回 0、再保持，循环；`busy_o` 在整周期内为高。

**预期结果**：

- 上坡步进 \(\Delta_{\text{up}} = \text{round}(65535/1000) = 66\)。
- 各相位时长（选通数 ×100 ns）：上坡 1000×100 ns = 100 μs，平顶高 400 μs，下坡 100 μs，平顶低 400 μs，总周期 ≈ 1 ms。
- 由于 \(\Delta_{\text{up}} \cdot 1000 = 66000 > 65535\)，实际 `ramp_gene` 会在临近满量程时钳位，不会过冲。

**待本地验证**：上坡末尾 `dat_o` 是否精确停在 65535（钳位逻辑生效）。

#### 4.2.5 小练习与答案

**练习 1**：为什么说本组件把 `ramp_gene`「反过来用」？
**答案**：`ramp_gene` 是给定步进 `ramp_inc_i`、步数由「目标/步进」之比决定；本组件给定步数 `nb_step_*_g`，再由「满量程/步数」反推步进 `inc_step_*_c`。

**练习 2**：若 `trig_i` 只给一个单周期脉冲而非保持，会发生什么？
**答案**：完成一个完整梯形周期后回到 0，`busy_o` 拉低，之后保持 idle，等待下一次 `trig_i` 上升沿。

---

### 4.3 pulse_shaper：固定脉宽整形器

#### 4.3.1 概念说明

很多场景下，输入脉冲的宽度不可控（例如来自按键、比较器或异源中断），但下游需要宽度精确、速率受限的脉冲。`psi_common_pulse_shaper` 干两件事：

1. **拉伸/截断**：在输入上升沿触发一个**固定宽度**（`duration_g` 拍）的输出脉冲，与输入实际多宽无关。
2. **hold-off 限速**：识别到一个脉冲后，在随后 `hold_off_g` 拍内忽略任何输入脉冲，从而限定最高脉冲速率。

它还有 `hold_in_g` 模式：输出「跟随并保持」输入脉冲——既保证最小宽度，又能在输入持续为高时继续延伸输出（输出 = 维持宽度 ÷ 与输入相与）。

#### 4.3.2 核心流程

逻辑每拍：

1. 记录 `PulseLast`（用于下拍上升沿检测）。
2. 维护两个倒计数器：`DurCnt`（脉宽剩余）和 `HoCnt`（hold-off 剩余）。
3. 检测到输入上升沿且 `HoCnt=0` 时，置输出为 1、装载 `DurCnt = duration_g-1`、`HoCnt = hold_off_g`。
4. `DurCnt` 数到 0 后输出归 0（除非 `hold_in_g` 且输入仍高）。

文档给出的典型波形：`duration_g=3`、`hold_off_g=4` 时，第一个脉冲被拉成 3 拍，第二个脉冲因落在 hold-off 内被忽略，第三个脉冲被截成 3 拍。

#### 4.3.3 源码精读

generic 与接口极简，[pulse_shaper.vhd:L20-L29](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_shaper.vhd#L20-L29)：

```vhdl
generic(duration_g : positive := 3;    -- 输出脉宽（时钟周期数）
        hold_in_g  : boolean  := false;
        hold_off_g : natural  := 0;    -- 两脉冲间最小时钟周期数
        rst_pol_g  : std_logic:= '1');
port(clk_i : in std_logic; rst_i : in std_logic;
     dat_i : in std_logic; dat_o : out std_logic);
```

核心逻辑在 `p_comb`，[pulse_shaper.vhd:L44-L73](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_shaper.vhd#L44-L73)。触发判定，[pulse_shaper.vhd:L64-L68](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_shaper.vhd#L64-L68)：

```vhdl
if (dat_i = '1') and (r.PulseLast = '0') and (r.HoCnt = 0) then
  v.dat_o  := '1';
  v.HoCnt  := hold_off_g;
  v.DurCnt := duration_g - 1;
end if;
```

脉宽倒计数与 `hold_in` 分支，[pulse_shaper.vhd:L52-L60](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_shaper.vhd#L52-L60)：

```vhdl
if r.DurCnt = 0 then
  if hold_in_g then
    v.dat_o := r.dat_o and dat_i;   -- 输出跟随并保持输入
  else
    v.dat_o := '0';                  -- 干净归零
  end if;
else
  v.DurCnt := r.DurCnt - 1;          -- 脉宽期内：输出保持高、计数递减
end if;
```

关键点：`DurCnt≠0` 时 `v.dat_o` **不被改写**，于是维持触发时置的 '1'——这正是「脉宽期内固定为高」的实现。`duration_g=3` 时输出高电平恰好 3 拍（触发拍 + DurCnt 从 2 递减到 0 的两拍）。输出直接取寄存器，[pulse_shaper.vhd:L76](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_shaper.vhd#L76)，仍是二进程 record 法。

#### 4.3.4 代码实践

**实践目标**：验证 `duration_g` 与 `hold_off_g` 的实际效果。

**操作步骤**：

1. 实例化 `duration_g => 3, hold_off_g => 4, hold_in_g => false`。
2. 在测试平台里制造三个输入上升沿：相隔 2 拍、再相隔 6 拍。
3. 参考已有 [psi_common_pulse_shaper_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pulse_shaper_tb/psi_common_pulse_shaper_tb.vhd) 的激励结构（该 TB 已在 `sim/config.tcl` 注册）。

**需要观察的现象**：

- 第一个上升沿后，`dat_o` 高电平精确持续 3 拍。
- 第二个上升沿（隔 2 拍，落在 `HoCnt` 倒数中）被忽略，`dat_o` 无变化。
- 第三个上升沿（隔 6 拍 > `hold_off_g`）再次产生 3 拍脉冲。

**预期结果**：每个被接受的脉冲宽度恒为 3 拍；速率被限制为「每 `hold_off_g+1` 拍最多一个」。

**待本地验证**：若把 `hold_in_g` 设为 `true` 并输入一个 10 拍宽的长脉冲，`dat_o` 是否在前 3 拍后仍跟随输入保持高（因为 `r.dat_o and dat_i`）。

#### 4.3.5 小练习与答案

**练习 1**：`duration_g=1` 时输出是什么形态？
**答案**：`DurCnt` 初值为 0，触发当拍输出 '1'，下一拍 `DurCnt=0` 分支将其清零——输出为单周期脉冲（等价于把输入上升沿整形成一拍标准脉冲）。

**练习 2**：`hold_in_g=true` 时为什么文档建议把 `hold_off_g` 设为 0？
**答案**：`hold_in` 模式下输出与输入「相与」联动，目的是整形+保持而非限速；继续加 hold-off 会丢弃本应透传的脉冲，故静默时间无意义。

---

### 4.4 pulse_shaper_cfg：运行时可配置整形器

#### 4.4.1 概念说明

`pulse_shaper` 的脉宽与 hold-off 是综合期 generic，改一次要重新综合。`psi_common_pulse_shaper_cfg` 把这两个量搬到**运行时端口** `width_i`（脉宽）和 `hold_i`（hold-off），由寄存器动态配置。代价是多了两个 generic 定义上限（`max_duration_g`、`max_hold_off_g`），端口位宽由 `log2ceil` 推导。

#### 4.4.2 核心流程

行为与 `pulse_shaper` 完全同构，差别仅在于：

- `DurCnt` 上限由 `max_duration_g` 决定，初值来自 `width_i`（运行时）。
- `HoCnt` 初值来自 `hold_i`（运行时）。
- `width_i=0` 是特例：强制输出为 0，相当于运行时「关闭」整形器。
- `hold_off_ena_g=false` 时，hold-off 功能整个被旁路，`hold_i` 端口塌缩成 1 位（用 `choose` 实现）。

#### 4.4.3 源码精读

端口位宽在声明区用 `choose` + `log2ceil` 条件推导，[pulse_shaper_cfg.vhd:L32-L33](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_shaper_cfg.vhd#L32-L33)：

```vhdl
width_i : in std_logic_vector(log2ceil(max_duration_g) - 1 downto 0);
hold_i  : in std_logic_vector(choose(hold_off_ena_g, log2ceil(max_hold_off_g), 1) - 1 downto 0);
```

这里 `choose`（u2-l1）是关键——VHDL 端口声明区不能写 `if`，但 `choose` 是函数，可在综合期按 `hold_off_ena_g` 二选一端口宽度。触发逻辑用 `from_uslv` 把运行时端口转成整数装载计数器，[pulse_shaper_cfg.vhd:L75-L79](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_shaper_cfg.vhd#L75-L79)：

```vhdl
if (dat_i = '1') and (r.PulseLast = '0') and (r.HoCnt = 0) then
  v.OutPulse := '1';
  v.HoCnt    := from_uslv(hold_i);
  v.DurCnt   := from_uslv(width_i) - 1;
end if;
```

`width_i=0` 关闭特例，[pulse_shaper_cfg.vhd:L71-L73](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_shaper_cfg.vhd#L71-L73)：

```vhdl
if unsigned(width_i) = 0 then
  v.DurCnt   := 0;
  v.OutPulse := '0';
```

其余倒计数与 `pulse_shaper` 完全一致，[pulse_shaper_cfg.vhd:L59-L70](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_shaper_cfg.vhd#L59-L70)。

#### 4.4.4 代码实践

**实践目标**：运行时切换脉宽，观察输出变化。

**操作步骤**：

1. 实例化 `max_duration_g => 128, max_hold_off_g => 256, hold_off_ena_g => true`。
2. 在测试平台前半段令 `width_i = to_uslv(10, 7)`、`hold_i = to_uslv(20, 8)`，发若干输入脉冲；后半段改为 `width_i = to_uslv(3, 7)`、`hold_i = to_uslv(0, 8)`。
3. 参考已有 [psi_common_pulse_shaper_cfg_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pulse_shaper_cfg_tb/psi_common_pulse_shaper_cfg_tb.vhd)（已在 `sim/config.tcl` 注册）。

**需要观察的现象**：同一硬件、同一输入，前半段输出脉宽 10 拍、两脉冲间隔 ≥20 拍；后半段脉宽 3 拍、无 hold-off 限制。

**预期结果**：脉宽与 hold-off 随寄存器写入即时改变（下一拍生效）。

**待本地验证**：把 `width_i` 写为 0，确认输出恒为 0（关闭功能）。

#### 4.4.5 小练习与答案

**练习 1**：为什么不直接用 `pulse_shaper` 而要这套 `_cfg`？
**答案**：当脉宽/hold-off 需要软件在线调节（如通过 AXI 寄存器）时，用 `_cfg` 可避免重新综合；代价是多一个 generic 上限与略多的逻辑。

**练习 2**：`hold_off_ena_g=false` 时 `hold_i` 端口为何还在？
**答案**：端口必须存在以保持实体接口稳定，但宽度被 `choose` 塌缩成 1 位、且 `HoCnt` 永远不装载非零值，实际不消耗 hold-off 逻辑。

---

## 5. 综合实践

**任务**：搭建一条「梯形脉冲产生 → 触发整形」的完整链路，把本讲四个组件的概念串起来。

要求：

1. 用 `pulse_generator_ctrl_static`（内部即 `strobe_generator` + `ramp_gene`）产生周期性梯形波：`str_freq_g => 1.0e6`、`nb_step_up_g => 500`、`nb_step_flh_g => 2000`、`nb_step_dw_g => 500`、`nb_step_fll_g => 2000`。
2. 用一个简单比较器（`dat_o > 阈值`）把梯形波的上坡/下坡段转成一列**宽度不规则**的电平脉冲。
3. 把这列脉冲送入 `pulse_shaper_cfg`，设 `width_i => to_uslv(5, ...)`、`hold_i => to_uslv(50, ...)`，得到宽度恒为 5 拍、且最小间隔 50 拍的标准触发脉冲。

**验证要点**：

- 解释第 1 步的梯形「步进」由 \(\text{round}(F/500)\) 决定（步数反推步进），而第 2 步比较器输出的脉冲宽度则取决于上坡期间越过阈值的持续拍数（不固定）。
- 说明第 3 步如何用 hold-off 把不固定速率规整成固定速率，并把不规则宽度规整成 5 拍。
- 画出三段波形（梯形、比较器输出、整形输出）的对应关系。

**待本地验证**：在 Modelsim/GHDL 中用 u11-l1 的自校验 TB 结构（`###ERROR###` 约定）断言「整形输出每个脉冲宽度恰好 5 拍、两脉冲间隔 ≥50 拍」。

## 6. 本讲小结

- `ramp_gene` 是一个**选通驱动**的斜坡累加器，四态 Gray 状态机（zero/up/flat/dw）管理上坡/平顶/下坡；斜坡速度 = 步进幅度 × 选通频率。
- `ramp_gene` 的步进带「钳位」：临近目标一步之遥时直接对齐目标值，避免过冲；`init_val_g` 提供上电初值。
- `pulse_generator_ctrl_static` = `strobe_generator` + `ramp_gene` 的组合包装，把「给定步数反推步进」的四相位梯形脉冲参数化；`trig_i` 沿触发、电平连续触发，`stop_i` 中途夭折。
- `pulse_shaper` 用上升沿检测 + 两个倒计数器（`DurCnt`/`HoCnt`）实现「固定脉宽 + hold-off 限速」，`hold_in_g` 提供跟随保持模式。
- `pulse_shaper_cfg` 把脉宽与 hold-off 搬到运行时端口，靠 `choose`+`log2ceil` 推端口位宽，`width_i=0` 作运行时关闭开关。
- 四组件均沿用二进程 record 设计法（u7-l1），并复用 `math_pkg`/`array_pkg` 的编译期函数。

## 7. 下一步学习建议

- 若关注**节拍产生**的更多细节（`sync_i` 相位对齐、`strobe_divider` 分频），回看 u6-l1。
- 若想把这些脉冲用于 **AXI/SPI/I2C** 等接口的触发或定时，继续阅读 u9 单元的接口讲义。
- 下一讲 u10-l3 将讲解看门狗、消抖器等更多杂项组件，与本讲的整形/限速思路一脉相承（`debouncer` 同样基于边沿检测与计数器）。
