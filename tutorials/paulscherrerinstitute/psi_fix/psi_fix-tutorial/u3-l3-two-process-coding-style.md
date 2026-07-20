# 两段式编码风格与命名约定

## 1. 本讲目标

本讲是「位真双模型与测试方法论」单元的收口讲义。前面两讲我们知道了**为什么**要写位真模型、**怎么**用协同仿真测试台去比对（u3-l1、u3-l2），本讲则落到 VHDL 代码本身：psi_fix 库里所有可综合组件都用同一种**编码风格**写出来，读懂它，你就能快速看懂库里任何一个 `.vhd`。

学完后你应该能够：

- 说出**两段式方法 (two-process method)** 的两个进程各自做什么，以及为什么 `v := r` 这一行是整个风格的灵魂。
- 理解为什么 psi_fix 用一个 **record** 把所有流水线寄存器（含 valid 信号）打包成一个对象。
- 解释 valid 信号如何像数据一样在流水线里逐级平移，保证「数据到哪一级，valid 跟到哪一级」。
- 掌握 psi_fix 的命名约定：`snake_case`、端口后缀 `_i / _o / _g`、以及 AXI-S 握手信号 `dat / vld / rdy` 的同义词表。

本讲通篇以 [hdl/psi_fix_mov_avg.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd)（滑动平均）为样板，并在需要时用 [hdl/psi_fix_resize_pipe.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_resize_pipe.vhd) 作为第二个范例互相印证。

## 2. 前置知识

在进入本讲前，你需要先具备以下认知（这些都在前置讲义中讲过）：

- **定点格式三元组 [s, i, f]** 与**位增长规则**（u1-l4）：加减法整数位 +1，两个有符号数相乘整数位相加后再 +1。mov_avg 里 `SumFmt_c` 比 `in_fmt_g` 多 `AdditionalBits_c` 个整数位，就是位增长规则的直接应用。
- **库级定点运算函数**（u2-l2）：`psi_fix_add / sub / mult / resize / shift_right` 这些函数「结果格式由调用者指定、函数不自动位增长」，默认 `trunc/wrap`。本讲会反复看到它们被嵌进组合进程里。
- **AXI-S 握手**（u1-l4）：`vld`（TVALID）与 `rdy`（TREADY）同拍为高才完成一次传递。mov_avg 只有 `vld` 没有 `rdy`，是一个无反压的简化模型。

如果对「为什么要在 VHDL 里手写流水线、而不是全靠综合工具重定时」还有疑问，可以先翻一眼 [doc/files/tips.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/tips.md) 的 *Heavy Pipelining* 一节——它解释了 Manual Splitting（手动把加法/舍入/饱和拆成三级流水）的动机，正是本讲两段式代码要落实的事情。

> 名词小贴士：本讲反复出现的「进程 (process)」是 VHDL 的并发语句单位；「组合进程」指不带时钟、描述纯组合逻辑的进程；「时序进程」指带时钟、只放寄存器的进程。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hdl/psi_fix_mov_avg.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd) | 本讲主样板。差分-累加-增益校正三级流水，完整展示两段式 + record + valid 链 + 命名约定。 |
| [hdl/psi_fix_resize_pipe.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_resize_pipe.vhd) | 第二范例。两级流水（先舍入、再饱和），结构更小，并带有 `rdy` 反压，可用来对照「带握手」的两段式写法。 |
| [doc/files/introduction.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/introduction.md) | 命名约定的权威出处：`snake_case`、`_i/_o/_g` 后缀、AXI-S 握手信号同义词表。 |
| [doc/files/tips.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/tips.md) | Manual Splitting 范式：把一个运算拆成多级流水。理解了它就理解了 mov_avg 为什么是「分级的」。 |

## 4. 核心概念与源码讲解

### 4.1 两段式方法 (two-process method)

#### 4.1.1 概念说明

写时序电路最朴素的方式，是在一个带时钟的进程里把「下一拍该是什么值」直接算出来再赋给寄存器，例如 `r <= PsiFixAdd(a, b, ...)`。这种写法在时钟频率不高时没问题，但它把**组合运算**（加法、舍入、饱和）和**寄存器**混在同一个进程里，带来两个麻烦：

1. **看不清寄存器边界**：哪些信号是真正的触发器、哪些只是中间变量，要逐行读才能判断；改起来容易漏复位。
2. **难以做 Manual Splitting**：当一段组合逻辑太深、时序不达标时，你想把它拆成两级流水，就得在同一个进程里反复插信号，代码越改越乱。

**两段式方法 (two-process method)** 把这件事彻底拆成两个职责单一的进程：

- **组合进程 `p_comb`**：只描述「给定当前状态 `r` 和输入，下一拍状态应该是什么」。它对时钟无感，敏感量是「当前寄存器状态 + 所有输入」。它把结果写进一个**影子信号** `r_next`。
- **时序进程 `p_seq`**：只描述「时钟上升沿把 `r_next` 打进 `r`」，外加复位。它**不做任何运算**，只是搬运。

这种「计算与寄存分离」的思路，让寄存器清单一目了然（全在 record 里），也让组合逻辑可以任意拆级而不用动时序进程。psi_fix 全库的可综合实体都遵循这一风格，连 record 类型名都统一叫 `two_process_r`，并在注释里写 `-- Two Process Method` 标注，便于检索。

两段式方法的「灵魂」是组合进程开头的 **`v := r;`**：先把局部变量 `v` 设成当前状态，于是**所有未被显式赋值的字段默认保持原值**——这正是一条寄存器该有的「保持」语义。之后只需描述「这一拍哪些字段要变化」，省去了写满 `else r <= r;` 的样板代码。

#### 4.1.2 核心流程

一个时钟周期里，两段式代码的数据流可以这样描述：

```
       ┌─────────────────────────────────────────────┐
输入 → │  p_comb（组合进程）                           │ → r_next
       │  1. v := r            （默认全部保持）         │
       │  2. 计算 v 中需要变化的字段（各级流水）         │
       │  3. r_next <= v       （交出下一拍状态）        │
       └─────────────────────────────────────────────┘
                          │
                          ▼  （r_next 是一根组合连线）
       ┌─────────────────────────────────────────────┐
       │  p_seq（时序进程）                            │ → r
       │  if rising_edge(clk_i) then                  │
       │      r <= r_next;       （打一拍）             │
       │      if rst_i='1' then  （选择性地复位部分字段）│
       │          r.<某些字段> <= 0;                   │
       │      end if;                                 │
       │  end if;                                     │
       └─────────────────────────────────────────────┘
                          │
                          ▼  （r 回流进 p_comb 的敏感量，形成闭环）
```

关键点：

- `r` 是「当前状态」，`r_next` 是「下一拍状态」。`p_comb` 读 `r` 写 `r_next`，`p_seq` 读 `r_next` 写 `r`。
- 组合进程的敏感量列表必须包含 `r` 和**所有**输入，否则仿真/综合行为不一致；psi_fix 的写法是把 `r` 整体列入，再逐个列输入信号。
- 复位是**同步复位**，且**只复位需要确定初值的字段**（如 valid、累加器），不复位那些「第一拍就会被新数据覆盖」的数据通路寄存器——这是 psi_fix 的一贯做法，目的是节省复位布线资源。

#### 4.1.3 源码精读

先看组合进程的骨架。[hdl/psi_fix_mov_avg.vhd:L79-L92](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L79-L92) 是注释横幅 + 进程头 + 变量声明 + `v := r`：

```vhdl
p_comb : process(r, vld_i, dat_i, DataDel)
  variable v         : two_process_r;
  variable CalcOut_v : std_logic_vector(...);
  variable CalcVld_v : std_logic;
begin
  CalcOut_v := (others => '0');
  CalcVld_v := '0';
  v := r;                 -- ← 灵魂：默认全部保持
```

注意三件事：(1) 敏感量是 `r`（整个 record）外加三个输入；(2) 局部变量 `v` 与 record 同类型；(3) `v := r` 之后，凡是没被赋值的字段都会在下一拍保持原值。

进程末尾 [hdl/psi_fix_mov_avg.vhd:L136-L139](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L136-L139) 把算好的 `v` 交给影子信号：

```vhdl
-- Apply to record
r_next <= v;
```

时序进程则极其简洁，[hdl/psi_fix_mov_avg.vhd:L144-L154](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L144-L154)：

```vhdl
p_seq : process(clk_i)
begin
  if rising_edge(clk_i) then
    r <= r_next;                       -- 只搬运，不算术
    if rst_i = '1' then
      r.Vld        <= (others => '0'); -- 选择性复位
      r.VldOutRegs <= (others => '0');
      r.Sum_1      <= (others => '0');
    end if;
  end if;
end process;
```

可以看到 `p_seq` 里**没有任何 `psi_fix_*` 运算**，只有一行 `r <= r_next` 和复位。复位也只点了三个字段：两套 valid（控制流必须确定）和累加器 `Sum_1`（反馈环路必须清零，否则上电残值会混进结果）；而 `Diff_0`、`RoughCorr_2`、`OutRegs` 这些「数据通路」寄存器不在复位列表里——它们在第一个有效数据到来时就会被覆盖。

> 对照参考：[hdl/psi_fix_resize_pipe.vhd:L93-L102](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_resize_pipe.vhd#L93-L102) 的 `p_seq` 结构完全一致，只是复位字段换成 `RndVld/SatVld`（同样是「只复位 valid」），证明这是库内通用模板。

#### 4.1.4 代码实践

**实践目标**：亲手在源码里把「组合」与「时序」两半圈出来，理解它们的分工。

**操作步骤**：

1. 打开 [hdl/psi_fix_mov_avg.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd)。
2. 用两支不同颜色的笔（或在编辑器里用两个折叠区）分别框选：
   - 组合进程：`p_comb`，约 L82–L139。
   - 时序进程：`p_seq`，约 L144–L154。
3. 在组合进程里数一数出现了几次 `psi_fix_` 开头的函数调用（`psi_fix_sub / add / resize / shift_right / mult`），再在时序进程里数一次。

**需要观察的现象**：

- 组合进程里密密麻麻全是定点运算；时序进程里一次运算都没有。
- 时序进程的核心只有 `r <= r_next` 一行，其余都是复位。

**预期结果**：

- 组合进程包含 5 处定点函数调用（sub、add、resize、shift_right×2、mult，具体取决于 `gain_corr_g` 分支）；时序进程包含 0 处定点函数调用。
- 由此可直观得出两段式的分工定理：**所有算术都在 `p_comb`，`p_seq` 只负责打拍和复位**。

> 本实践为「源码阅读型实践」，无需运行仿真；若你已按 u1-l3 搭好 PsiSim，也可以在仿真波形里观察 `r` 与 `r_next` 的关系来印证。

#### 4.1.5 小练习与答案

**练习 1**：如果把组合进程敏感量列表里的 `r` 删掉，只留 `(vld_i, dat_i, DataDel)`，仿真会出现什么问题？

**参考答案**：组合进程将不再对「寄存器当前状态」的变化敏感，于是当 `r` 改变时 `r_next` 不会重新求值，导致 `r_next` 只反映输入变化、丢失了反馈（如累加器 `Sum_1` 的自累加）。综合后由于是组合逻辑可能仍能工作，但**行为级仿真会出错**，这正是两段式要求把 `r` 整体列入敏感量的原因。

**练习 2**：为什么 `p_seq` 的复位里没有 `r.Diff_0`？

**参考答案**：`Diff_0` 是数据通路寄存器，其值在每个有效样本到来时由 `psi_fix_sub(dat_i, ..., DataDel, ...)` 重新计算覆盖；即便上电初值随机，只要对应的 `Vld(0)` 为 0，下游就不会消费它。复位它没有功能收益，反而浪费复位布线资源。psi_fix 只复位「控制流（valid）」和「反馈环路（累加器）」这类**初值会影响后续结果**的寄存器。

---

### 4.2 record 流水封装

#### 4.2.1 概念说明

一个流水线组件往往有十几甚至几十个寄存器：每一级的数据寄存器、每一级的 valid、若干配置寄存器。如果把这些寄存器散落成一堆独立的 `signal`，那么组合进程的敏感量列表会非常长，赋值时也要逐个 `r_next.X <= ...; r_next.Y <= ...;`，既啰嗦又易漏。

psi_fix 的做法是用一个 **record**（VHDL 的结构体）把**一个组件的所有寄存器打包成一个对象**，并约定：

- 类型名固定叫 `two_process_r`，配 `signal r, r_next : two_process_r;` 两个实例。
- record 的**每个字段就是一级流水寄存器**，字段名用后缀 `_0 / _1 / _2` 标明它属于第几级，例如 `Diff_0`（第 0 级差分）、`Sum_1`（第 1 级累加）、`RoughCorr_2`（第 2 级粗校正）。
- **valid 信号也住进 record**，通常是一个数组 `Vld : std_logic_vector(0 to N)`，下标对齐流水级。

这样做的直接好处是：组合进程敏感量只需写一个 `r`；变量 `v := r` 一行就把所有寄存器初始化为「保持」；要给某级打数据，就 `v.Sum_1 := ...`。寄存器清单一目了然，新增一级流水也只是往 record 里加一个字段。

#### 4.2.2 核心流程

record 字段与流水级的对应关系可以画成一张「级联表」：

| 流水级 | 数据寄存器 | valid 来源 | 该级做了什么 |
|--------|-----------|-----------|-------------|
| Stage 0 | `Diff_0` | `Vld(0) ← vld_i` | 当前样本减去延迟 `taps_g` 拍的旧样本（差分） |
| Stage 1 | `Sum_1` | `Vld(1) ← Vld(0)` | 把差分累加进运行和（累加器） |
| Stage 2 | `RoughCorr_2` | `Vld(2) ← Vld(1)` | 粗增益校正（右移，仅 EXACT 模式留存中间值） |
| 输出 | `OutRegs(0..n-1)` | `VldOutRegs(0..n-1)` | 可配数量的输出寄存器 |

注意 valid 是**和数据一起平移**的：每个时钟沿，`Vld` 向高位移一格。这保证了「数据走到第几级，对应位置的 valid 就在第几级为高」，下游只需看自己那一级的 valid 就知道当前数据是否有效。

平移在组合进程里是这样写的（伪代码）：

```
每一拍:
    v.Vld(1..N) := r.Vld(0..N-1)   # 整体右移一格
    对每一级 stage k:
        v.<第k级数据> := f(第k-1级数据, 输入)   # 计算新值
        v.Vld(0) := vld_i                         # 第0级 valid 直接接输入
```

#### 4.2.3 源码精读

mov_avg 的 record 定义见 [hdl/psi_fix_mov_avg.vhd:L61-L70](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L61-L70)：

```vhdl
-- Two Process Method
type two_process_r is record
  Vld         : std_logic_vector(0 to 2);                          -- 各级 valid
  Diff_0      : std_logic_vector(psi_fix_size(DiffFmt_c) - 1 downto 0);
  Sum_1       : std_logic_vector(psi_fix_size(SumFmt_c) - 1 downto 0);
  RoughCorr_2 : std_logic_vector(psi_fix_size(GcInFmt_c) - 1 downto 0);
  OutRegs     : OutReg_t(0 to out_regs_g - 1);                     -- 可配输出寄存器链
  VldOutRegs  : std_logic_vector(0 to out_regs_g - 1);
end record;
signal r, r_next : two_process_r;
```

字段命名里的 `_0/_1/_2` 后缀就是流水级编号；`Vld` 长度为 3，正好对应三级。`OutRegs` 和 `VldOutRegs` 是数组，长度由 generic `out_regs_g` 决定，体现「输出寄存器数量可配」。

valid 的平移在组合进程最前面完成，[hdl/psi_fix_mov_avg.vhd:L94-L97](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L94-L97)：

```vhdl
-- *** Pipe Handling ***
v.Vld(v.Vld'low + 1 to v.Vld'high)                      := r.Vld(r.Vld'low to r.Vld'high - 1);
v.VldOutRegs(v.VldOutRegs'low + 1 to v.VldOutRegs'high) := r.VldOutRegs(...'low to ...'high - 1);
v.OutRegs(v.OutRegs'low + 1 to v.OutRegs'high)          := r.OutRegs(...'low to ...'high - 1);
```

这三行用 VHDL 的切片赋值，把 `Vld`、`VldOutRegs`、`OutRegs` 三个数组各整体右移一格——这正是「valid 跟着数据走」的硬件实现。注意它用 `'low/'high` 而不是硬编码下标，所以数组长度可变（`out_regs_g` 改了也不用改这几行）。

随后各级数据计算，[hdl/psi_fix_mov_avg.vhd:L99-L123](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L99-L123)：

```vhdl
-- *** Stage 0 ***
v.Diff_0 := psi_fix_sub(dat_i, in_fmt_g, DataDel, in_fmt_g, DiffFmt_c, ...);
v.Vld(0) := vld_i;

-- *** Stage 1 ***
if r.Vld(0) = '1' then
  v.Sum_1 := psi_fix_add(r.Sum_1, SumFmt_c, r.Diff_0, DiffFmt_c, SumFmt_c, ...);
end if;
```

注意 Stage 1 的累加**只在 `r.Vld(0)='1'` 时进行**——这正是 valid 链的另一用途：**控制反馈环路的更新时机**，没有有效输入时累加器保持不变。`v.Vld(0) := vld_i` 则把输入 valid 注入链的起点。

输出寄存器段 [hdl/psi_fix_mov_avg.vhd:L125-L134](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L125-L134) 根据 `out_regs_g` 分两种情况：为 0 时直接输出组合结果；否则把结果灌进 `OutRegs(0)`，输出取 `OutRegs'high`（链尾），实现可配流水深度。

> 第二范例：[hdl/psi_fix_resize_pipe.vhd:L45-L52](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_resize_pipe.vhd#L45-L52) 的 record 只有四个字段（`RndReg/RndVld/SatReg/SatVld`），把「数据寄存器 + valid」成对摆放，是更紧凑的两级流水样板，可对照阅读。

#### 4.2.4 代码实践

**实践目标**：跟踪 valid 信号链 `v.Vld`，理解它如何随数据逐级传递。

**操作步骤**：

1. 在 [hdl/psi_fix_mov_avg.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd) 中搜索 `Vld`，把所有出现的位置标出来。
2. 按数据流方向串成一条链：`vld_i → Vld(0) → Vld(1) → Vld(2) → VldOutRegs → vld_o`。
3. 对照每一级的数据寄存器（`Diff_0 → Sum_1 → RoughCorr_2 → OutRegs`），确认「同一拍里，第 k 级数据对应的 valid 就在 `Vld(k)`」。

**需要观察的现象**：

- `Vld` 的平移发生在每级数据计算**之前**（L94–L97 在 L99 Stage 0 之前），所以「先移位、再注入新值」的顺序保证 `Vld(0)` 不会被上一拍的值污染。
- Stage 1 的累加被 `if r.Vld(0)='1'` 守卫，证明 valid 不仅驱动输出，还**参与控制反馈**。

**预期结果**：

- valid 链是一条与数据链等长、同步平移的「影子流水线」；数据延迟几拍，valid 就延迟几拍。
- 在 NONE/ROUGH 模式下输出 valid 取自 `r.Vld(1)`（两级），在 EXACT 模式下取自 `r.Vld(2)`（三级），见 [hdl/psi_fix_mov_avg.vhd:L108-L123](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L108-L123)；再加上 `out_regs_g` 级输出寄存器，就是最终 `vld_o` 的延迟。

> 待本地验证：若你在 Modelsim/GHDL 跑 mov_avg 测试台，可在波形里把 `r.Vld(0..2)` 与 `dat_i/vld_i` 对齐观察，确认 valid 脉冲逐级右移。

#### 4.2.5 小练习与答案

**练习 1**：record 字段名 `Diff_0 / Sum_1 / RoughCorr_2` 的数字后缀代表什么？如果要在 Stage 1 和 Stage 2 之间再插一级增益预算，应该怎么命名新字段？

**参考答案**：后缀是流水级编号（从 0 起）。新插入的一级应命名为 `Xxx_1`，并把原来的 `Sum_1`、`RoughCorr_2` 顺延——但更省事的做法是直接用新名字加合理的级号，只要组合进程里平移 `Vld` 的范围与数据级数一致即可。关键是**字段级号要与 valid 数组下标对齐**。

**练习 2**：为什么 valid 用一个 `std_logic_vector(0 to N)` 数组，而不是 N 个独立的 `signal`？

**参考答案**：(1) 可以用切片赋值 `v.Vld(1 to N) := r.Vld(0 to N-1)` 一行完成整体平移，独立信号则要写 N 行；(2) record 内统一管理，敏感量列表只需一个 `r`；(3) 数组长度可由计算得出，便于参数化。

---

### 4.3 命名约定

#### 4.3.1 概念说明

psi_fix 是个多人维护、跨多个 PSI 库（psi_common、psi_tb、en_cl_fix）协作的项目。为了让任何人「看到信号名就能猜出它的方向和用途」，库强制了一套命名约定，权威出处是 [doc/files/introduction.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/introduction.md) 的 *Contribute* 与 *Handshaking Signals/Naming* 两节。核心有三条：

1. **一律 `snake_case`，禁止 `camelCase`**：所有标识符用小写加下划线。这一点在 4.0.0 大版本里被强制推行（见 u1-l1 提到的 major 升级原因），所以你会在老代码注释里看到 `PsiFixAdd` 这种历史写法（如 tips.md 示例），但当前库内统一是 `psi_fix_add`。
2. **端口后缀表方向**：输入端口以 `_i` 结尾、输出端口以 `_o` 结尾、generic（类属参数）以 `_g` 结尾。一眼就能分清「这是进来的、出去的、还是编译期参数」。
3. **AXI-S 握手信号用同义词**：PSI 没有严格照搬 ARM 的 `TDATA/TVALID/TREADY` 命名，而是用一套更短的同义词（见下表）。

#### 4.3.2 核心流程

AXI-S 标准信号与 PSI 命名的对照表（据 introduction.md *Naming* 一节）：

| AXI-S 标准 | PSI 常用同义词 | 含义 |
|-----------|---------------|------|
| TDATA | `dat_i` / `dat_o`（或应用相关名） | 数据 |
| TVALID | `vld_i` / `vld_o` | 主设备声明数据有效 |
| TREADY | `rdy_i` / `rdy_o` | 从设备声明可以接收 |

注意两点：(1) PSI 允许「一组握手信号关联多个数据信号」（如复数组件的 `dat_iq_i` 共用一个 `vld_i`），而非 AXI-S 的单一 TDATA 大向量，这是为了可读性。(2) 并非所有组件都实现全部可选特性——mov_avg 就省略了 `rdy`（无反压），而 resize_pipe 同时有 `rdy_i/rdy_o`。

#### 4.3.3 源码精读

命名约定的「法律条文」在 [doc/files/introduction.md:L76-L101](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/introduction.md#L76-L101)，其中明确写：

> No camelCase **[此处原文意为「使用 snake_case 而非 camelCase」]** and ports must follow suffixes as such:
> - input: `_i`
> - output: `_o`
> - generic: `_g`

握手信号同义词表在 [doc/files/introduction.md:L124-L129](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/introduction.md#L124-L129)，列出了 `TDATA→dat_*`、`TVALID→vld_*`、`TREADY→rdy_*` 的映射。

mov_avg 的实体声明是这套约定的标准示范，[hdl/psi_fix_mov_avg.vhd:L22-L41](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L22-L41)：

```vhdl
entity psi_fix_mov_avg is
  generic(
    in_fmt_g    : psi_fix_fmt_t;          -- ← _g = generic
    out_fmt_g   : psi_fix_fmt_t;
    taps_g      : positive;
    gain_corr_g : string      := "ROUGH"; -- ← 字符串值用大写常量风格
    round_g     : psi_fix_rnd_t := psi_fix_round;
    sat_g       : psi_fix_sat_t := psi_fix_sat;
    out_regs_g  : natural     := 1
  );
  port(
    clk_i : in  std_logic;                -- ← _i = 输入
    rst_i : in  std_logic;
    dat_i : in  std_logic_vector(...);    -- ← AXI-S TDATA
    vld_i : in  std_logic;                -- ← AXI-S TVALID
    dat_o : out std_logic_vector(...);    -- ← _o = 输出
    vld_o : out std_logic                  -- ← 无 rdy，省略反压
  );
end entity;
```

逐条核对：

- 所有 generic 都以 `_g` 结尾：`in_fmt_g / out_fmt_g / taps_g / gain_corr_g / round_g / sat_g / out_regs_g`。
- 所有端口以 `_i` 或 `_o` 结尾，方向与后缀一致。
- 数据用 `dat_i/dat_o`、有效用 `vld_i/vld_o`，没有 `rdy`（无反压组件）。
- 全部 `snake_case`，没有任何大写驼峰。

内部信号同样守约，但内部信号没有方向后缀（它们既非端口也非 generic），而是用**含义后缀**：寄存器实例 `r / r_next`、延迟数据 `DataDel`、常数 `*_c`（如 `Gain_c / DiffFmt_c`，见 [hdl/psi_fix_mov_avg.vhd:L45-L56](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L45-L56)）。`_c` 后缀表示「编译期常量 (constant)」，是库内另一条隐含约定。

> 实战提醒：[hdl/psi_fix_cordic_vect.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd) 曾把一个 ready 输出误命名为 `rdy_i`（应为 `rdy_o`），在 7f7ec7d 提交中被修正（见 Changelog）。这正是后缀约定的重要性：方向标错会让使用者误判握手方向。

#### 4.3.4 代码实践

**实践目标**：用命名约定对一份陌生实体做「静态体检」，不看实现就能预测它的接口形状。

**操作步骤**：

1. 打开 [hdl/psi_fix_resize_pipe.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_resize_pipe.vhd) 的实体声明（约 L20–L38）。
2. 不看注释，仅凭信号名回答：
   - 哪些是 generic？哪些是输入？哪些是输出？
   - 哪些信号构成一组 AXI-S 握手？数据方向是输入还是输出？
   - `rdy_o` 是谁给谁的（主还是从）？
3. 把答案与 mov_avg 对比，列出 resize_pipe 比 mov_avg 多出的握手信号。

**需要观察的现象**：

- 仅凭 `_g/_i/_o` 后缀就能 100% 区分 generic 与端口方向。
- `rdy_o`（输出 ready）说明**这个组件作为从设备**，向下游主设备声明自己能否接收；`rdy_i`（输入 ready，默认 `'1'`）说明它也尊重下游从设备的反压。

**预期结果**：

- resize_pipe 的 generic：`in_fmt_g / out_fmt_g / round_g / sat_g / rst_pol_g`；输入：`clk_i / rst_i / vld_i / dat_i / rdy_i`；输出：`rdy_o / vld_o / dat_o`。
- 相比 mov_avg，resize_pipe 多了完整的双向 ready（`rdy_i/rdy_o`），是一个**支持反压**的 AXI-S 组件，而 mov_avg 是无反压的。

#### 4.3.5 小练习与答案

**练习 1**：看到一个名为 `coef_i` 的端口和一个名为 `ratio_g` 的对象，仅凭命名你能确定什么？

**参考答案**：`coef_i` 是**输入端口**（后缀 `_i`），名字暗示它是「系数」类数据；`ratio_g` 是 **generic**（后缀 `_g`），是编译期可配置的「比率」参数，不能在运行时改变。

**练习 2**：为什么 PSI 选择 `dat/vld/rdy` 而不是直接用 AXI-S 的 `TDATA/TVALID/TREADY`？

**参考答案**：(1) 更短、可读性更好；(2) PSI 允许一组握手信号关联多个数据信号（如复数 I/Q 共用一个 vld），用应用相关的名字比单一 TDATA 大向量更清晰；(3) PSI 只在「实现了的特性上」遵循 AXI-S 语义（部分组件省略反压），用自己的一套名字也便于表达「这是简化版 AXI-S」。

## 5. 综合实践

把本讲三件事（两段式、record 封装、命名约定）串起来，完成下面这个**源码阅读 + 接口设计**小任务。

**任务背景**：假设你要为 psi_fix 贡献一个最简单的组件——「定点增益」，即 `dat_o = G * dat_i`，其中 `G` 是一个 generic 给出的实数常数。它需要一级流水（输入寄存 + 乘法寄存）。

**请完成**：

1. **接口设计（练命名约定）**：用 psi_fix 命名约定写出 entity 的 generic 与 port 列表。要求：
   - 至少有 `in_fmt_g / out_fmt_g / gain_g`（gain 为 real）三个 generic；
   - 端口含 `clk_i / rst_i / dat_i / vld_i / dat_o / vld_o`，全部带正确后缀、全 `snake_case`。
2. **record 设计（练流水封装）**：定义一个 `two_process_r` record，包含两级流水所需的字段（输入寄存 + 乘法结果寄存）和对应的 valid 数组。给字段起带 `_0/_1` 级号的名字。
3. **进程骨架（练两段式）**：写出 `p_comb`（含 `v := r`、valid 平移、各级计算、`r_next <= v`）与 `p_seq`（`r <= r_next` + 选择性复位 valid）的骨架，不必算准确的定点格式，用注释占位即可。
4. **对照验证**：把你写的骨架与 [hdl/psi_fix_mov_avg.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd) 逐行比对，确认：组合进程里放算术、时序进程里只打拍、valid 随数据平移、所有命名守约。

**参考思路（骨架，非完整可综合代码）**：

```vhdl
-- 示例代码（仅示意，非项目原有代码）
entity psi_fix_gain is
  generic(
    in_fmt_g  : psi_fix_fmt_t;
    out_fmt_g : psi_fix_fmt_t;
    gain_g    : real                       -- ← _g 后缀
  );
  port(
    clk_i : in  std_logic;                 -- ← _i 后缀
    rst_i : in  std_logic;
    dat_i : in  std_logic_vector(psi_fix_size(in_fmt_g)-1 downto 0);
    vld_i : in  std_logic;
    dat_o : out std_logic_vector(psi_fix_size(out_fmt_g)-1 downto 0);
    vld_o : out std_logic                  -- ← _o 后缀
  );
end entity;

architecture rtl of psi_fix_gain is
  -- 增益系数定点化（编译期常量，_c 后缀）
  -- constant Coef_c : ... := psi_fix_from_real(gain_g, CoefFmt_c);
  type two_process_r is record
    Vld   : std_logic_vector(0 to 1);      -- 两级 valid
    Dat_0 : std_logic_vector(psi_fix_size(in_fmt_g)-1 downto 0);  -- 输入寄存
    Mul_1 : std_logic_vector(psi_fix_size(out_fmt_g)-1 downto 0); -- 乘法结果
  end record;
  signal r, r_next : two_process_r;
begin
  p_comb : process(r, vld_i, dat_i)
    variable v : two_process_r;
  begin
    v := r;                                -- 灵魂：默认保持
    -- valid 平移
    v.Vld(1) := r.Vld(0);
    -- Stage 0：寄存输入
    v.Dat_0  := dat_i;
    v.Vld(0) := vld_i;
    -- Stage 1：乘增益（定点格式略）
    -- v.Mul_1 := psi_fix_mult(r.Dat_0, in_fmt_g, Coef_c, CoefFmt_c, out_fmt_g, ...);
    r_next <= v;
    dat_o  <= r.Mul_1;
    vld_o  <= r.Vld(1);
  end process;

  p_seq : process(clk_i)
  begin
    if rising_edge(clk_i) then
      r <= r_next;
      if rst_i = '1' then
        r.Vld <= (others => '0');          -- 只复位 valid
      end if;
    end if;
  end process;
end architecture;
```

把这个骨架与 mov_avg 对照，你会发现结构是同构的——这就是两段式风格的复用价值：**学会一个，看懂全库**。

## 6. 本讲小结

- **两段式方法**把时序电路拆成两个职责单一的进程：组合进程 `p_comb` 描述「下一拍状态」并写出 `r_next`，时序进程 `p_seq` 只在时钟沿把 `r_next` 打进 `r` 并做选择性同步复位；所有算术都在 `p_comb`，`p_seq` 不做运算。
- 组合进程开头的 **`v := r`** 是整个风格的灵魂，它让「未被赋值的字段默认保持」，从而只需描述「这一拍变化的字段」。
- psi_fix 用一个固定名为 **`two_process_r`** 的 record 把组件全部寄存器（含 valid）打包成 `signal r, r_next`；字段名用 `_0/_1/_2` 标流水级，便于把 record 字段与流水级一一对应。
- **valid 是与数据等长、同步平移的影子流水线**：`v.Vld(1..N) := r.Vld(0..N-1)` 一行完成平移；它既驱动输出有效，也参与控制反馈环路（如累加器只在 `Vld(0)='1'` 时更新）。
- 复位是**选择性的**：只复位控制流（valid）和反馈环路（累加器），数据通路寄存器靠首个有效数据覆盖，以节省复位布线资源。
- **命名约定**三条：全 `snake_case`；端口 `_i/_o`、generic `_g` 表方向；AXI-S 握手用 `dat/vld/rdy` 同义词（未必全实现，mov_avg 无 `rdy`，resize_pipe 有双向 `rdy`）。内部常量另用 `_c` 后缀。

## 7. 下一步学习建议

本讲把「怎么读 psi_fix 的 VHDL」讲透了。接下来：

- **进入组件实战（单元 4）**：[u4-l1](u4-l1-resize-pipe-mov-avg.md) 会把 mov_avg 当作第一个端到端组件完整走通——讲它的差分-累加-增益校正（NONE/ROUGH/EXACT）三模式与 `out_regs_g` 配置，并对照 Python 位真模型验证中间格式推导。你本讲看到的 `DiffFmt_c/SumFmt_c/GcInFmt_c` 在那里会得到定点格式的完整解释。
- **若想再看一个更小的两段式样板**：直接精读 [hdl/psi_fix_resize_pipe.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_resize_pipe.vhd) 全文（仅 ~100 行），它带 `rdy` 反压，是理解「握手 + 两段式」如何叠加的好例子。
- **若关心 Manual Splitting 的动机**：重读 [doc/files/tips.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/tips.md) 的 *Heavy Pipelining* 一节，对照 mov_avg 的 Stage 2/Stage 3，体会「把加法、舍入、饱和拆成多级」在真实组件里是如何落地的。
