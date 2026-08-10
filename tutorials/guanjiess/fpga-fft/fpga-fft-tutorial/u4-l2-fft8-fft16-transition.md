# fft_8 与 fft_16：从寄存器延时过渡到 RAM 延时

## 1. 本讲目标

本讲是「逐级解析」单元的第二篇，承接 u4-l1（fft_2 / fft_4 的寄存器延时）和 u3（旋转因子 ROM、RAM 延时）两条线索，精读流水线中间过渡的两级：`fft_8`（8 点层）与 `fft_16`（16 点层）。

学完本讲，你应当能够：

- 看懂 `fft_8` 如何**不使用 RAM**，仅用三级寄存器 `B_real_1d / B_real_2d / C_real` 搭出 4 拍反馈延时，配合硬编码的 `RotatorMemory8` 完成一级运算。
- 看懂 `fft_16` 如何**首次改用 RAM 延时**（`delay #(.layer(4))`）和**首次改用 ROM 旋转因子**（`Rotator16`），成为流水线里第一个具备完整「蝶形 + RAM 延时 + ROM 旋转因子 + 复数乘法」四件套的层级。
- 从数量上解释**为什么从 16 点起必须用 RAM**：SDF 的反馈延时深度 \(=N/2=2^{\text{layer}-1}\) 随层级翻倍，寄存器数量随之爆炸，到 fft_16k 会需要 8191 个寄存器，只有 RAM（BRAM）扛得住。

> 本讲只聚焦「延时实现」与「旋转因子实现」在 fft_8 → fft_16 之间的过渡。时序对齐与跨级握手的细节（`rotator_valid`、`HALT_FOR_NEXT_LAYER`）已在 u3-l3 讲透，本讲只做承接性回顾。

## 2. 前置知识

阅读本讲前，请确认你已理解下面几个概念（来自前置讲义）：

- **SDF 单路延迟反馈**（u1-l4、u3-l2）：每一级蝶形的「下支」输出 B 先存进延时单元，攒满半周期后再当「上支」C 喂回蝶形，使相隔半周期的样本配对。延时深度恒为半个周期 \( \text{PERIOD}/2 \)。
- **蝶形 butterfly.v**（u2-l1）：D 是上支求和 \(A+C\)（直送下一级），B 是下支求差 \(C-A\)（需乘旋转因子后送下一级）；模块本身有 1 拍内置流水线延迟。
- **复数乘法 multiplier.v**（u2-l2）：\((a+jb)(c+jd)\) 拆成 4 个实数乘法，\(a/b\) 接蝶形 D 输出，\(c/d\) 接旋转因子；只用截断输出 `*_trunc`。
- **旋转因子两条实现路线**（u3-l1）：小点数用 `RotatorMemory8` 那样的 `case` 硬编码常量；大点数用 `Rotator16` 那样的 ROM IP（实部、虚部分存两块 ROM）。
- **RAM 延时 delay.v**（u3-l2）：双口 RAM「先写后读」，参数 `layer` 决定延时深度 `DELAY_TIME = 1<<(layer-1)`，内部五状态机 `IDLE→DELAY→OUT→TAIL→END`。

**两个关键数量关系**（本讲反复用到）：

- 周期 \(\text{PERIOD} = N = 2^{\text{layer}}\)。
- SDF 反馈延时深度 \(= \text{PERIOD}/2 = 2^{\text{layer}-1}\)。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [src/fft_8.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v) | 8 点层（layer=3） | 用**寄存器 latch** 实现 4 拍延时；硬编码 `RotatorMemory8` |
| [src/fft_16.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v) | 16 点层（layer=4） | 首次改用 **RAM delay** 与 **Rotator16 ROM**，第一个完整四件套层级 |
| [src/RotatorMemory8.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/RotatorMemory8.v) | 8 点旋转因子 | `case` 硬编码 4 个因子，fft_8 用 |
| [src/Rotator16.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v) | 16 点旋转因子 | ROM IP + 计数器高位做 select，fft_16 用 |
| [src/delay.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v) | RAM 延时单元 | fft_16 以 `layer=4` 例化它，`DELAY_TIME=8` |
| [src/fft_4.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_4.v) | 4 点层（layer=2） | 仅 1 个寄存器延时，作为对比基线 |

## 4. 核心概念与源码讲解

### 4.1 fft_8：用寄存器 latch 搭出 4 拍反馈延时

#### 4.1.1 概念说明

`fft_8` 是 8 点层，对应分治层级 layer=3，周期 \(\text{PERIOD}=8\)。它和 fft_2 / fft_4 一样，属于「简单层」——**延时只有 4 拍**，规模小到用几个触发器（寄存器 latch）就能装下，所以作者没有调用 RAM，而是直接写了几级 `always` 寄存器。这是 SDF 流水线里「能用寄存器就用寄存器」的最后一级。

它的旋转因子也只有 4 个（\(W_8^{0\sim3}\)），因此用 `case` 硬编码的 `RotatorMemory8` 即可，不需要 ROM IP。

#### 4.1.2 核心流程

`fft_8` 内部数据流（与 fft_4 几乎同构，只是延时更深、旋转因子更多）：

```text
        A_real/A_img (本级输入)
              │
              ▼
         butterfly ◄──────── C (反馈：延时后的 B)
              │ B(下支差)      │ D(上支和)
              │                ▼
              │         w_D_real/img ──► multiplier ──+──► out_real8/out_img8
              │                              ▲
              ▼                              │ c/d
   B_real_1d → B_real_2d → C_real      RotatorMemory8
        (3 级寄存器，构成 4 拍延时)
```

要点：

1. **状态机**：`IDLE → START → PROCESSING → END`，由 `start8` 启动（与 fft_2/fft_4 完全一致）。
2. **S 控制信号**：`S8` 在计数到 `PERIOD/2-1=3` 和 `PERIOD-1=7` 时翻转，形成占空比 50% 方波，驱动蝶形上下支切换。
3. **反馈延时**：蝶形 B 输出经过三级寄存器 `B_real_1d → B_real_2d → C_real` 回到蝶形 C 输入；加上蝶形自身的 1 拍，共 4 拍 \(= \text{PERIOD}/2\)。
4. **旋转因子**：`RotatorMemory8` 用 `case` 硬编码输出 4 个因子，`r_rotator_valid` 拉高后才输出真实因子，否则输出 \(W=1\)。
5. **下一级启动**：`start4_counter` 数到 `HALT_FOR_NEXT_LAYER-3` 时发 `start4` 单拍脉冲，启动 fft_4。

#### 4.1.3 源码精读

**(1) 周期与握手常量**

[src/fft_8.v:23-24](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L23-L24) 定义 `PERIOD=8`、`HALT_FOR_NEXT_LAYER = 6 + PERIOD/2 = 10`。其中 6 是固定流水开销，`PERIOD/2=4` 是延时建立期（u3-l3 已讲）。

**(2) S 控制信号翻转点**

[src/fft_8.v:97-111](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L97-L111) 中关键一行：

```verilog
end else if (S8_counter == PERIOD/2-1 | S8_counter == PERIOD-1)begin
    S8 <= ~S8;
```

即在计数 3 和 7 处翻转，生成每 8 拍一个周期的方波。这与 fft_4（在 1 和 3 翻转）是同一套写法。

**(3) 寄存器延时——本讲核心**

[src/fft_8.v:143-184](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L143-L184) 注释直接点明「**延时单元，简单，不用 ram**」。延时链由三级寄存器构成（实部为例，虚部完全对称）：

[src/fft_8.v:159-170](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L159-L170)

```verilog
// generate C, 4D latch of B.
always@(posedge clk) begin
    ...
    B_real_1d <= w_B_real;   // 第 1 拍
    B_real_2d <= B_real_1d;  // 第 2 拍
    C_real    <= B_real_2d;  // 第 3 拍
end
```

注释里的「4D」指**整条反馈环共 4 拍延时**：上面这三级寄存器贡献 3 拍，蝶形模块自身贡献 1 拍，合计 \(3+1=4=\text{PERIOD}/2\)，正好把相隔半周期的两个样本对齐。最终 `C_real` 经 [src/fft_8.v:183](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L183) 的 `assign w_C_real = C_real;` 喂回蝶形 C 输入 [src/fft_8.v:252](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L252)，形成 SDF 反馈闭环。

**(4) 硬编码旋转因子**

[src/fft_8.v:236-242](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L236-L242) 例化 `RotatorMemory8`。其内部用 `case` 把 4 个因子写死在 [src/RotatorMemory8.v:51-72](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/RotatorMemory8.v#L51-L72)：

```verilog
case (counter)
    3'b000: {rotator_real_tmp, rotator_img_tmp} <= {W0_real, W0_img}; // 1, 0
    3'b001: ... <= {W1_real, W1_img}; //  cos45, -cos45
    3'b010: ... <= {W2_real, W2_img}; //  0, -1
    3'b011: ... <= {W3_real, W3_img}; // -cos45, -cos45
endcase
```

其中 `cos45_18 = 46341`、`one = 1<<16`（Q1.16 定点，u2-l3 已讲）。因子规模小（仅 4 个）是它能用 `case` 硬编码的前提。

**(5) 下一级启动**

[src/fft_8.v:115-142](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L115-L142)：`start4_counter` 数到 `HALT_FOR_NEXT_LAYER-3 = 7` 时把 `r_start4` 拉一拍（vivado 版本，line 121）。另外 [src/fft_8.v:277](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L277) `assign end4 = 0;`——end 链在这里被钉死为 0，呼应 u1-l4 提到的「真正贯通的是 start 链，end 链基本未用」。

#### 4.1.4 代码实践

**实践目标**：亲手确认 fft_8 的反馈延时确实是 4 拍。

**操作步骤**：

1. 打开 [src/fft_8.v:154-170](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L154-L170)，数一数从蝶形输出 `w_B_real` 到反馈输入 `w_C_real` 之间串了几个触发器。
2. 打开 [src/butterfly.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly.v)，确认蝶形内部 B/D 输出是否经过寄存器（即是否自带 1 拍延迟）。
3. 把两个数加起来，与 `PERIOD/2 = 4` 比对。

**需要观察的现象**：fft_8 里实部（或虚部）延时链有 **3 个显式寄存器**，蝶形内部贡献 **1 拍**，合计 **4 拍**。

**预期结果**：\(3 + 1 = 4 = \text{PERIOD}/2\)，与 SDF 半周期配对要求一致。如果你数出的不是这个数，说明你把蝶形内置的那 1 拍漏掉了或重复算了。

> 本实践为「源码阅读型」，无需运行仿真；结论可直接从上述两文件读出。

#### 4.1.5 小练习与答案

**练习 1**：fft_8 里实部延时链用了 3 个寄存器（`B_real_1d / B_real_2d / C_real`），虚部延时链也用了 3 个（`B_img_1d / B_img_2d / C_img`）。每个寄存器 32 位，那么 fft_8 的延时部分一共占用多少个触发器（FF）？

**参考答案**：\(3 \times 32 \times 2 = 192\) 个 FF。

**练习 2**：为什么 fft_8 不需要像 fft_16 那样调用 RAM？`RotatorMemory8` 又为什么不需要 ROM IP？

**参考答案**：延时只有 4 拍，其中 3 拍用寄存器实现仅耗 192 个 FF，规模很小，直接写寄存器比例化 RAM 更直观；旋转因子只有 4 个，用 `case` 硬编码比生成 .coe 去初始化 ROM 更省事。两者都是「点数小，所以用最朴素写法」。

---

### 4.2 fft_16：改用 RAM delay 与 ROM 旋转因子

#### 4.2.1 概念说明

`fft_16` 是 16 点层，layer=4，\(\text{PERIOD}=16\)，反馈延时深度翻倍到 \(16/2=8\) 拍。如果继续沿用 fft_8 的写法，每个实/虚部通道要写 7 个寄存器（再加蝶形 1 拍凑 8）；作者在这里选择了**改用 RAM**，即例化 `delay #(.layer(4))`。

同时，旋转因子增加到 8 个（\(W_{16}^{0\sim7}\)），用 `case` 硬编码开始变得啰嗦，于是 fft_16 也**首次改用 ROM**——例化 `Rotator16`，把因子存进两块 ROM IP（实部一块、虚部一块）。

> 因此 fft_16 是整条流水线里**第一个结构完整**的层级：它同时具备「蝶形 + RAM 延时 + ROM 旋转因子 + 复数乘法」四件套。从 fft_32 往上的所有高层（fft_32 / fft_64 / … / fft_16k）都是把 fft_16 这套结构**参数化**后复用的（见 u4-l3 的 `butterfly_general`）。可以说 fft_16 是高层模块的「原型」。

#### 4.2.2 核心流程

`fft_16` 的数据流（与 fft_8 同构，但延时和旋转因子都换了实现）：

```text
        A_real/A_img (本级输入)
              │
              ▼
         butterfly ◄──────── C (反馈：RAM 延时后的 B)
              │ B(下支差)      │ D(上支和)
              │                ▼
              │         w_D_real/img ──► multiplier ──+──► out_real_16/out_img_16
              │                              ▲
              ▼                              │ c/d
         delay #(.layer(4))             Rotator16
         (RAM, 8 拍延时)              (ROM IP, 8 个因子)
```

要点：

1. **状态机**：仍是 `IDLE → START → PROCESSING → END`，由 `start16` 启动（与 fft_8 完全相同）。
2. **S 控制**：`S16` 在 `PERIOD/2-1=7` 和 `PERIOD-1=15` 处翻转。
3. **RAM 延时**：蝶形 B 输出送进 `delay #(.layer(4))`，`DELAY_TIME = 1<<(4-1) = 8 = PERIOD/2`；RAM 输出 `w_C_real/img` 喂回蝶形 C 输入。
4. **写使能 r_wea**：fft_16 多了一个生成 `r_wea` 的逻辑，控制 RAM 何时写入。
5. **ROM 旋转因子**：`Rotator16` 内部用计数器低位做 ROM 地址、最高位做 `select`，把一个 PERIOD 对半切——前半段读真实因子，后半段补 \(W=1\)（u3-l1 讲过的「计数器两用」）。

#### 4.2.3 源码精读

**(1) 周期常量**

[src/fft_16.v:22-23](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L22-L23)：`PERIOD=16`、`HALT_FOR_NEXT_LAYER = 6 + 16/2 = 14`。注意 HALT 随 PERIOD 线性增长，正是因为延时建立期变长了。

**(2) RAM 延时——本讲核心**

[src/fft_16.v:143-180](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L143-L180) 的注释已变成「**延时单元，用 ram**」，与 fft_8 的「不用 ram」形成直接对照。写使能 `r_wea` 在 [src/fft_16.v:156-166](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L156-L166) 生成：处理期间为 1，否则为 0。然后 [src/fft_16.v:169-180](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L169-L180) 例化延时：

```verilog
delay #(.layer(4))
delay8(
    .clk(clk), .rst(rst),
    .din_real(w_B_real), .din_img(w_B_img),   // 蝶形 B → RAM 输入
    .wea(r_wea),
    .dout_real(w_C_real), .dout_img(w_C_img),  // RAM 输出 → 蝶形 C
    .out_first(w_delay_out_first),
    .out_last(w_delay_out_last)
);
```

注意实例名叫 `delay8`（驱动的是下一级 fft_8），但 `layer=4` 对应的是**本级** 16 点层。延时深度由 `delay.v` 内部 [src/delay.v:17](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L17) 的 `DELAY_TIME = 1<<(layer-1) = 1<<3 = 8` 决定。RAM 输出 `w_C_real/img` 经 [src/fft_16.v:247-248](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L247-L248) 接到蝶形 C 输入，闭环成立。`out_first/out_last` 是延时单元给出的「首/末有效样本」边界脉冲（u3-l2 讲过）。

**(3) ROM 旋转因子**

[src/fft_16.v:230-236](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L230-L236) 例化 `Rotator16`。其内部「计数器两用」的精髓在 [src/Rotator16.v:16-40](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v#L16-L40)：

```verilog
if(rotator_valid) r_addra <= r_addra + 1;  // 4 位计数器：低 3 位当地址，最高位当 select
...
select_1d <= r_addra[3];                   // 最高位打一拍
select_2d <= select_1d;                    // 再打一拍，对齐 ROM 读延迟
...
assign rotator_real = select_2d ? 1<<16 : w_rotator_real_tmp;  // 后半段补 W=1
assign rotator_img  = select_2d ? 0      : w_rotator_img_tmp;
```

两块 ROM IP 在 [src/Rotator16.v:60-70](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v#L60-L70)：`rotator_16_real` / `rotator_16_img`，用 `r_addra` 同时寻址。这比 fft_8 的 `case` 硬编码更省逻辑、也更易扩展到 fft_32 / fft_1k（只要换更大的 ROM）。

**(4) 复数乘法收尾**

[src/fft_16.v:255-267](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L255-L267) 例化 `multiplier`，`.a/.b` 接蝶形 D 输出 `w_D_real_tmp/w_D_img_tmp`，`.c/.d` 接旋转因子 `w_rotator_real/w_rotator_img`，`.rstn(~rst)` 把高有效复位反相成低有效（u2-l2 讲过），只取 `data_real_trunc/data_img_trunc` 作为本级输出 `out_real_16/out_img_16`。

至此，fft_16 把「蝶形 → RAM 延时 → ROM 旋转因子 → 复数乘法」四件套第一次集齐，成为后续所有高层模块的原型。

#### 4.2.4 代码实践

**实践目标**：确认 fft_16 改用 RAM 后，延时深度与 fft_8 的寄存器延时在数量上对应同一个公式。

**操作步骤**：

1. 打开 [src/delay.v:17-18](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L17-L18)，读出 `DELAY_TIME` 的定义式。
2. 代入 fft_16 的 `layer=4`，算出 `DELAY_TIME`。
3. 与 fft_8 的反馈延时（4 拍）对比，确认两者都等于各自的 `PERIOD/2`。

**需要观察的现象**：`DELAY_TIME = 1<<(layer-1)`，layer=4 时等于 8。

**预期结果**：fft_16 的 RAM 延时 \(=8=\text{PERIOD}/2=16/2\)，与 fft_8 的 \(4=\text{PERIOD}/2=8/2\) 完全同构——只是延时单元的实现从「寄存器链」换成了「RAM」。另外注意 [src/delay.v:18](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L18) 的 `required_delay_in_state_machine = DELAY_TIME - 1 - 3 - 1`（即 −5），这是 RAM 内部状态机为补偿边沿检测/打拍/读出延迟而做的微调（u3-l2 详述），不影响「名义延时 = 半周期」这一结论。

> 本实践为「源码阅读型」，无需运行仿真。如要本地验证延时数值，可在仿真里给 `din_real` 注入一个标志样本，数它从 `din_real` 出现到 `dout_real` 出现的时钟数（应为 8）——「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：fft_16 的 RAM 延时实例名叫 `delay8`，参数却是 `.layer(4)`，看起来「名不副实」。请解释为什么。

**参考答案**：实例名 `delay8` 表达的是「这条延时的输出要去驱动下一级 fft_8」（命名跟随下游），而参数 `.layer(4)` 表达的是「本级是 16 点层、layer=4」。两者含义不同：一个指下游，一个指本级。这是该项目命名上的一个小坑，阅读时要以参数 `layer` 为准来判断延时深度。

**练习 2**：fft_16 的 `Rotator16` 用 `r_addra[3]`（计数器最高位）做 `select`，而 fft_8 的 `RotatorMemory8` 没有这种 select 机制。为什么 fft_16 需要、fft_8 不需要？

**参考答案**：fft_16 的旋转因子以 ROM 形式给出，ROM 里只存了 N/2=8 个「不同」的因子；而一个 PERIOD=16 里前半段需要真实因子、后半段需要 \(W=1\)（蝶形上支不乘因子直通），所以要用 select 在两半段之间切换。fft_8 的 `RotatorMemory8` 用 `case` 直接把「真实因子 / W=1」两种情形都写进了分支（包括 `default` 输出 `1<<16,0`），不需要额外的 select 信号。

---

### 4.3 为什么 16 点起必须用 RAM：延时深度与资源爆炸

#### 4.3.1 概念说明

「fft_8 用寄存器、fft_16 用 RAM」不是审美选择，而是被 SDF 的延时深度公式逼出来的工程取舍。回顾关键关系：SDF 反馈延时深度

\[
\text{延时深度} = \frac{N}{2} = 2^{\text{layer}-1}
\]

若用寄存器 latch 实现，每个实/虚部通道需要的显式寄存器数为 \(\text{延时深度}-1\)（因为蝶形自带的 1 拍可顶 1 个）。这个数**每升一级就翻一倍**。

#### 4.3.2 核心流程

把各层「若用寄存器」的成本摊开（每寄存器 32 位，实虚两路）：

| 层级 | layer | PERIOD | 延时深度 \(N/2\) | 显式寄存器数 \(N/2-1\) | FF 成本 \((N/2-1)\times32\times2\) |
|---|---|---|---|---|---|
| fft_4 | 2 | 4 | 2 | 1 | 64 |
| **fft_8** | 3 | 8 | 4 | **3** | **192** |
| **fft_16** | 4 | 16 | 8 | **7** | **448** |
| fft_32 | 5 | 32 | 16 | 15 | 960 |
| fft_1k | 10 | 1024 | 512 | 511 | 32 704 |
| fft_16k | 14 | 16384 | 8192 | 8191 | **524 224** |

结论很直观：

- fft_4 / fft_8：寄存器成本几十到两百，**用寄存器最省事**，作者也确实这么写了。
- fft_16：成本 448，已经到了「写 7 级寄存器链又啰嗦又不划算」的**临界点**；更重要的是再往上每级翻倍，fft_32 就要 15 级。
- fft_16k：若坚持用寄存器，单级就要 50 万 + FF，远超普通 FPGA 的触发器总量，**物理上不可行**。

而一块 FPGA BRAM（如 Xilinx 36Kb 块）能存 36 864 bit，远超 fft_16k 单级延时所需的 \(8192\times32\times2 \approx 524\text{K bit}\)……实际上需要多块 BRAM 拼接，但 BRAM 是**专用存储资源**，不占用触发器、不占用 DSP，且容量随延时深度线性增长而成本远低。因此从 fft_16 开始切换到 RAM，既解决了 fft_16 这一档的啰嗦问题，又为 fft_32 ~ fft_16k 这一路同构扩展铺好了**可复用的实现模板**（即 `delay` + `Rotator_address` + ROM + `butterfly_general`）。

一句话：**寄存器方案无法随层级翻倍地扩展，RAM 方案可以。fft_16 是这条可扩展路线的起点。**

#### 4.3.3 源码精读

佐证「fft_16 是分水岭」的两个注释对照：

- fft_8 注释：[src/fft_8.v:143](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L143)「延时单元，简单，不用 ram」。
- fft_16 注释：[src/fft_16.v:143](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L143)「延时单元，用 ram」。

两段注释紧挨着「延时」一节开头，是作者留给读者的明确信号：**正是在 fft_16 这一级，延时实现切换了**。再看 [src/delay.v:17](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L17) 的 `DELAY_TIME = 1<<(layer-1)`——延时深度由参数线性决定，正是这套模板能一路扩展到 layer=14 的关键。

#### 4.3.4 代码实践

**实践目标**：用量化数据说明「16 点起必须用 RAM」。

**操作步骤**：

1. 在上面的表格基础上，自行补算 fft_64（layer=6）和 fft_256（layer=8）两档的「显式寄存器数」与「FF 成本」。
2. 查一款你熟悉的 FPGA（如 Xilinx Artix-7 XC7A100T，约 126 K 逻辑单元 / FF 资源），评估 fft_16k 的 50 万 + FF 是否放得下。
3. 写一段 3~5 句话的结论：从哪一级开始用寄存器已经不现实？为什么 RAM（BRAM）能扛住？

**需要观察的现象**：每升一级，FF 成本翻倍；到 fft_16k 达到 50 万量级。

**预期结果**：fft_64 需 15×32×2...（请按公式自行填出）；fft_16k 的 50 万 FF 远超单颗中端 FPGA 的触发器总数，故必须用 BRAM。结论应是：**寄存器方案在 fft_16 之后线性扩展不可行，RAM 是唯一可行路线**。

> 本实践为「计算 + 推理型」，无需运行仿真。

#### 4.3.5 小练习与答案

**练习 1**：如果把 fft_16 也改回寄存器延时（即把 `delay #(.layer(4))` 换成 7 级寄存器链），理论上能工作吗？为什么作者没这么做？

**参考答案**：理论上能工作——延时深度 8 拍用 7 个寄存器 + 蝶形 1 拍即可凑出，功能等价。作者没这么做，是因为（a）7 级寄存器链写起来啰嗦、易错；（b）更重要的是它无法为 fft_32 及以上的同构复用铺路。改用 RAM 后，fft_32 / fft_1k / fft_16k 只需改 `layer` 参数即可，代码可扩展性远好于寄存器链。

**练习 2**：fft_16 的 RAM 延时用 `delay.v`，而 u3-l2 提到还有一个 `delay_1k_plus.v`。它们接口几乎一样，为什么会有两个版本？

**参考答案**：两者逻辑相同，差别在计数器/地址位宽：`delay.v` 的地址较宽（`r_addra [13:0]`），足以覆盖 fft_16 这类中等延时；`delay_1k_plus.v` 针对大点数（layer≥11）做了位宽适配。fft_16 用的是标准 `delay.v`，大点数层才需要 `delay_1k_plus`（u4-l4 详述）。

## 5. 综合实践

把本讲三个模块串起来，完成一份「fft_8 vs fft_16 延时实现对比」小报告。

**任务**：

1. 建一张对比表，至少包含以下维度：

   | 维度 | fft_8 | fft_16 |
   |---|---|---|
   | layer / PERIOD | 3 / 8 | 4 / 16 |
   | 反馈延时深度 | 4 | 8 |
   | 延时实现 | 3 级寄存器 + 蝶形 1 拍 | RAM `delay #(.layer(4))` |
   | 延时源码位置 | fft_8.v:159-170 | fft_16.v:169-180 |
   | 旋转因子实现 | RotatorMemory8（case 硬编码） | Rotator16（ROM IP） |
   | 是否完整四件套 | 是（但延时为寄存器、因子为硬编码） | 是（首个「RAM + ROM」原型） |

2. 用 200 字以内回答：**为什么 16 点开始就必须用 RAM 而不能用寄存器 latch？** 要求引用本讲的 FF 成本数据（fft_16 需 448 FF、fft_16k 需 50 万 + FF）和「延时深度随层级翻倍」这一规律。

3. 进阶（可选）：打开 [src/butterfly_general.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v)，找出它内部例化 `delay` 的那一行，确认它也是用 `layer` 参数控制延时——这正是 fft_16 这套「RAM 延时」模板被参数化、推广到所有高层模块的证据（为下一讲 u4-l3 做铺垫）。

**参考结论要点**：fft_8 与 fft_16 的状态机、S 控制、蝶形、乘法器几乎完全同构，**唯一结构性差异在延时与旋转因子的实现**：fft_8 是「寄存器 latch + case 硬编码因子」的朴素写法，fft_16 是「RAM + ROM」的可扩展写法。切换发生在 fft_16 而非更早，是因为 SFF 反馈延时深度 \(=2^{\text{layer}-1}\) 随层级翻倍，寄存器成本从 fft_16 的 448 FF 一路炸到 fft_16k 的 50 万 + FF，只有 BRAM 能线性、低成本地扩展。

## 6. 本讲小结

- `fft_8`（layer=3，PERIOD=8）延续 fft_2/fft_4 的朴素写法：反馈延时 4 拍由 **3 级寄存器** `B_real_1d / B_real_2d / C_real`（加蝶形自带 1 拍）实现，旋转因子用 `RotatorMemory8` 的 **`case` 硬编码**。
- `fft_16`（layer=4，PERIOD=16）是分水岭：反馈延时 8 拍改用 **RAM `delay #(.layer(4))`**，旋转因子改用 **ROM IP `Rotator16`**（计数器低位当地址、最高位当 select）。
- fft_16 是流水线里**第一个集齐「蝶形 + RAM 延时 + ROM 旋转因子 + 复数乘法」四件套**的层级，是 fft_32 及以上所有高层模块的「原型」。
- 切换的根本原因是 SDF 延时深度 \(=N/2=2^{\text{layer}-1}\) 随层级翻倍：寄存器方案在 fft_16 需 448 FF、到 fft_16k 需 50 万 + FF，物理不可行；RAM（BRAM）可随参数线性扩展。
- fft_8 与 fft_16 的状态机、S 控制信号、HALT 握手写法高度一致——这印证了「延时与旋转因子的实现」才是这两级之间真正的差异点。

## 7. 下一步学习建议

- **下一讲 u4-l3** 将精读 `butterfly_general.v`：看它如何用一个 `layer` 参数把 fft_16 这套「状态机 + S 控制 + RAM 延时 + 下一级启动 + rotator_valid」封装成通用模块，供 fft_32 及以上所有层复用。建议先记住 fft_16 的四件套结构，因为 `butterfly_general` 本质上就是把 fft_16 参数化。
- **u4-l4** 会对比 fft_32 / fft_1k / fft_16k，你会看到它们只是改 `layer` 参数和 ROM 实例名；同时会讲 `delay_1k_plus.v` 在大点数下对位宽的适配。
- 若想加深对 RAM 延时内部状态机的理解，可回头重读 u3-l2（`delay.v` 的 `IDLE→DELAY→OUT→TAIL→END` 五状态机与 `−5` 补偿）。
- 想验证本讲数量结论的读者，可在 MATLAB 里（参考 matlab 目录脚本）画出 \(2^{\text{layer}-1}\) 随 layer 的增长曲线，直观感受「指数爆炸」。
