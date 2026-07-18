# 移位运算：两阶段移位与桶形移位器

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 PicoRV32 为什么不给移位指令配一个「又快又小」的完美实现，而是提供**三种可配置的移位策略**，并解释它们各自的面积/速度代价。
- 推导默认的 `TWO_STAGE_SHIFT`（两阶段移位）「先移 4 位、再移 1 位」的迭代过程，并能算出移位量 \(N\) 时所需的时钟周期数。
- 看懂 `BARREL_SHIFTER`（桶形移位器）如何把移位并入 ALU，让一条移位指令和一条普通 ALU 指令花同样多的周期。
- 跟踪 `cpu_state_shift` 状态如何循环迭代完成多位移位，并画出 `reg_sh` 在迭代中的变化轨迹。
- 理解 `ld_rs1` / `ld_rs2` 状态如何像「道岔」一样，按指令类型和配置把移位指令分流到 `cpu_state_shift` 或 `cpu_state_exec`。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，RISC-V 的移位指令有哪些。** RV32I 一共 6 条移位指令，分「立即数」和「寄存器」两组，各有左移、逻辑右移、算术右移三种：

| 指令 | 含义 | 移位量来源 |
| --- | --- | --- |
| `slli rd, rs1, shamt` | 左移立即数 | 指令字里的 5 位 `shamt` |
| `srli rd, rs1, shamt` | 逻辑右移立即数（高位补 0） | 5 位 `shamt` |
| `srai rd, rs1, shamt` | 算术右移立即数（高位补符号） | 5 位 `shamt` |
| `sll rd, rs1, rs2` | 左移寄存器 | `rs2` 的低 5 位 |
| `srl rd, rs1, rs2` | 逻辑右移寄存器 | `rs2` 的低 5 位 |
| `sra rd, rs1, rs2` | 算术右移寄存器 | `rs2` 的低 5 位 |

移位量范围是 0–31（RV32 下 5 位），这是本讲一切周期数推导的前提。

**第二，Verilog 的三种移位运算符。** 这直接影响你读源码：

- `<<` 逻辑左移，低位补 0。
- `>>` 逻辑右移，高位补 0。
- `>>>` **算术右移**：当左操作数是 `$signed` 时高位补符号位，否则同 `>>`。

PicoRV32 用 `$signed(...)` 包裹操作数来让 `>>>` 实现算术右移，这是 `sra`/`srai` 的关键。

**第三，多周期迭代 vs 单周期组合电路。** 移位器有两种极端实现：

- **桶形移位器（barrel shifter）**：纯组合电路，一拍算出任意 0–31 位移位结果。快，但 32 位宽的桶形移位器面积不小，而且会进入 ALU 的关键路径，可能拉低主频（fmax）。
- **逐位迭代移位**：每拍只移 1 位，循环 N 拍。几乎没有额外硬件（复用一个 1 位移位器），但移 31 位要 31 拍，很慢。

PicoRV32 的聪明之处在于：默认走一条**折中路线**——`TWO_STAGE_SHIFT`，每拍要么移 4 位、要么移 1 位，把「最坏 31 拍」压到「最坏 11 拍」，而硬件只比纯逐位多一个 4 位移位器。本讲就围绕这三种取舍展开。

## 3. 本讲源码地图

本讲全部源码集中在 `picorv32.v`，并对照 `README.md` 的参数与周期说明：

| 文件 | 本讲关注的内容 |
| --- | --- |
| [picorv32.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v) | 移位的全部实现：两个配置参数、ALU 桶形移位、`cpu_state_shift` 迭代状态机、`ld_rs1`/`ld_rs2` 的派发分支 |
| [README.md](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md) | `TWO_STAGE_SHIFT` / `BARREL_SHIFTER` 参数语义、移位指令周期数表 |

涉及的源码点（全部位于 `picorv32.v` 内）：

- 参数声明：`TWO_STAGE_SHIFT`、`BARREL_SHIFTER`
- 移位量寄存器：`reg [4:0] reg_sh`
- 状态编码：`localparam cpu_state_shift`
- ALU 中的桶形移位：`alu_shl` / `alu_shr` 及 `alu_out` 的选择分支
- 派发分支：`cpu_state_ld_rs1`、`cpu_state_ld_rs2` 中对移位指令的分流
- 迭代主体：`cpu_state_shift` 状态

## 4. 核心概念与源码讲解

### 4.1 桶形移位器与三种移位策略的取舍

#### 4.1.1 概念说明

PicoRV32 把「如何实现移位」做成了一个**综合时可配置**的开关，对应两个 `parameter`：

- `TWO_STAGE_SHIFT`（默认 1）：是否启用「先 4 位、后 1 位」的两阶段迭代。
- `BARREL_SHIFTER`（默认 0）：是否改用单周期桶形移位器。

这两个参数组合出三种实际策略：

| 策略 | 参数取值 | 实现 | 速度 | 面积 |
| --- | --- | --- | --- | --- |
| 两阶段迭代（默认） | `TWO_STAGE_SHIFT=1, BARREL_SHIFTER=0` | `cpu_state_shift` 里 4 位大步 + 1 位小步迭代 | 中（最坏约 11 拍） | 中（一个 4 位移位器 + 一个 1 位移位器） |
| 纯逐位迭代 | `TWO_STAGE_SHIFT=0, BARREL_SHIFTER=0` | 只用 1 位小步迭代 | 慢（最坏 31 拍） | 小（只剩 1 位移位器） |
| 桶形移位器 | `BARREL_SHIFTER=1` | 移位并入 ALU，纯组合 | 快（1 拍，等同 ALU 指令） | 大（32 位桶形移位器进入 ALU 关键路径） |

为什么不做「又快又小」？因为不存在：单周期完成任意位移位所需的组合逻辑（桶形移位器）天生就比一个 1 位/4 位移位器大，而且它会坐在 ALU 的组合通路上，抬高关键路径长度、压低 fmax。PicoRV32 是**尺寸优先**的核，所以默认选择「中等速度、较小面积」的两阶段迭代；只有当移位很频繁、且不在意主频损失时，才打开 `BARREL_SHIFTER`。

#### 4.1.2 核心流程

`BARREL_SHIFTER=1` 时，移位根本不进入 `cpu_state_shift`，而是在 ALU 的组合逻辑里一拍算完：

```text
取指 fetch ──► 读 rs1 ld_rs1 ──► 执行 exec（ALU 组合算出 alu_shl/alu_shr）──► 回写 fetch
                       │
                       └─ reg_op2 <= 移位量（decoded_rs2）
```

整个流程和一条 `addi` 完全一样，只不过 ALU 的输出选择器多选了「移位结果」这一路。

`BARREL_SHIFTER=0` 时，移位走专用的多周期 `cpu_state_shift`（见 4.2、4.3）。

#### 4.1.3 源码精读

先看两个参数本身，它们默认值正体现了「尺寸优先」的取向：

[picorv32.v:68-69](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L68-L69) 声明 `TWO_STAGE_SHIFT=1`、`BARREL_SHIFTER=0`。

README 对二者的说明很直白：

[README.md:190-201](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L190-L201) —— 两阶段移位「加速移位但增加硬件」；桶形移位器则「改用一个桶形移位器替代」。

桶形移位器的真身在 ALU 里。ALU 用一组组合 `always @*` 块并行算出所有可能的中间结果，其中就包括左移和右移：

[picorv32.v:1244-1245](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1244-L1245) 计算 `alu_shl`（左移）与 `alu_shr`（右移）。注意右移那行用 `$signed({符号位, reg_op1}) >>> reg_op2[4:0]`——当指令是 `sra`/`srai` 时把 `reg_op1[31]` 拼到最高位作符号扩展，从而让 `>>>` 变成算术右移；否则补 0，即逻辑右移。移位量只取 `reg_op2[4:0]`（低 5 位，0–31）。

```verilog
alu_shl = reg_op1 << reg_op2[4:0];
alu_shr = $signed({instr_sra || instr_srai ? reg_op1[31] : 1'b0, reg_op1}) >>> reg_op2[4:0];
```

然后 `alu_out` 的选择器在 `BARREL_SHIFTER` 打开时把移位结果接出去：

[picorv32.v:1280-1283](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1280-L1283) —— `BARREL_SHIFTER && (instr_sll || instr_slli)` 选 `alu_shl`；右移类指令选 `alu_shr`。

```verilog
BARREL_SHIFTER && (instr_sll || instr_slli):     alu_out = alu_shl;
BARREL_SHIFTER && (instr_srl || instr_srli || instr_sra || instr_srai): alu_out = alu_shr;
```

这两行 case 项以 `BARREL_SHIFTER &&` 开头——当 `BARREL_SHIFTER=0` 时，它们是常量假，综合时整条分支被裁掉，`alu_shl`/`alu_shr` 也变成无人使用的死逻辑而被消除。这就是「关掉一个 `parameter` 就消失一块电路」的典型用法（参见 u3-l1）。

README 还给出了桶形移位器的速度承诺：

[README.md:362-363](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L362-L363) —— 打开 `BARREL_SHIFTER` 后，移位指令耗时与任意 ALU 指令相同。

#### 4.1.4 代码实践

**目标**：直观感受 `BARREL_SHIFTER` 对 ALU 关键路径的影响。

**步骤**：

1. 阅读 [picorv32.v:1267-1284](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1267-L1284) 的 `alu_out` 选择 `case`，列出当 `BARREL_SHIFTER` 分别为 0 和 1 时，这个 `case` 里实际会出现的分支。
2. 思考：`alu_shr` 那行包含一个 33 位 `$signed` 拼接 + `>>>` 的 5 位移位，它和 `alu_add_sub`（一个 32 位加法器）相比，哪一个的组合延迟更大？把它并进 `alu_out` 后，会不会成为新的关键路径？

**需要观察的现象（待本地验证）**：如果你有 Vivado 或 yosys，分别用 `BARREL_SHIFTER=0` 与 `BARREL_SHIFTER=1` 综合核（参考 `scripts/vivado`），对比两次的 fmax 与 LUT 数。预期桶形版本 LUT 更高、fmax 更低，但移位指令不再占用 `cpu_state_shift` 的多个周期。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `alu_shr` 要写成 `$signed({..., reg_op1}) >>> ...` 而不是直接 `reg_op1 >>> ...`？

**答案**：因为 `>>>` 只有在左操作数是 `signed` 时才做算术右移（补符号），否则等同逻辑右移（补 0）。`reg_op1` 本身是无符号 `reg [31:0]`，直接 `>>>` 永远补 0，无法实现 `sra`/`srai`。用 `$signed` 包裹一个「按指令类型决定最高位」的 33 位数，就能让同一条表达式在算术/逻辑右移间切换。

**练习 2**：把 `BARREL_SHIFTER` 从 0 改成 1，`alu_shl`/`alu_shr` 的计算逻辑（[picorv32.v:1244-1245](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1244-L1245)）会变吗？

**答案**：不会。`alu_shl`/`alu_shr` 在 `TWO_CYCLE_ALU` 关闭时永远由组合 `always @*` 算出；变的是它们是否被 `alu_out` 选中、是否被综合保留。`BARREL_SHIFTER=0` 时它们无人引用，被综合器当作死逻辑消除；`=1` 时才真正进入电路。

---

### 4.2 两阶段移位算法（TWO_STAGE_SHIFT）

#### 4.2.1 概念说明

当 `BARREL_SHIFTER=0` 时，移位由专门的 `cpu_state_shift` 状态机迭代完成。最朴素的迭代是「每拍移 1 位」，但移 31 位要 31 拍，太慢。`TWO_STAGE_SHIFT=1`（默认）的优化思路是：**大步走为主，小步收尾**。

- 当剩余移位量 `reg_sh ≥ 4` 时，每拍移 **4 位**（一步顶四步）；
- 当 `reg_sh < 4` 时，退回每拍移 **1 位**（处理 0/1/2/3 的尾巴）。

这样硬件上只比「纯逐位」多一个 4 位移位器（`<<4`/`>>4`/`>>>4`），却把最坏情况的迭代次数从 31 降到 7（4 位步）+ 3（1 位步）= 10 次移位。这是一个典型的「用少量硬件换显著速度」的折中。

#### 4.2.2 核心流程

设移位量为 \(N\)（\(0 \le N \le 31\)），进入状态时 `reg_sh = N`。每拍的决策：

```text
若 reg_sh == 0      → 写回 reg_out，返回 fetch（收尾，1 拍）
否则若 TWO_STAGE_SHIFT=1 且 reg_sh ≥ 4
                    → 移 4 位，reg_sh ← reg_sh − 4
否则                → 移 1 位，reg_sh ← reg_sh − 1
```

据此可以精确推出**停留在 `cpu_state_shift` 的拍数**（含收尾那一拍）：

- 4 位步数 \(= \lfloor N/4 \rfloor\)
- 余数 \(r = N \bmod 4\)，对应 1 位步数 \(= r\)
- 收尾 1 拍

\[ T_{\text{shift}} = \left\lfloor \frac{N}{4} \right\rfloor + (N \bmod 4) + 1 \]

如果关掉两阶段（`TWO_STAGE_SHIFT=0`），4 位分支恒不命中，每拍都移 1 位：

\[ T_{\text{shift}} = N + 1 \]

下表给出几组对照（仅 `cpu_state_shift` 内的拍数）：

| 移位量 \(N\) | `TWO_STAGE_SHIFT=1` | `TWO_STAGE_SHIFT=0` |
| --- | --- | --- |
| 0 | 1 | 1 |
| 1 | 2 | 2 |
| 4 | 2 | 5 |
| 5 | 3 | 6 |
| 8 | 3 | 9 |
| 31 | 11 | 32 |

可见 \(N=31\) 时，两阶段把 32 拍压到 11 拍，加速近 3 倍。

#### 4.2.3 源码精读

移位量寄存器 `reg_sh` 是 5 位，正好覆盖 0–31：

[picorv32.v:177](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L177) 声明 `reg [4:0] reg_sh;`。

每个周期开始时 `reg_sh` 被默认置为 `x`（避免锁存），只有真正需要它的状态才会显式赋值：

[picorv32.v:1404](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1404) 在主 `always @(posedge clk)` 开头 `reg_sh <= 'bx;`，这是 PicoRV32 一贯的「默认 X + 显式覆盖」防锁存写法。

迭代主体在 `cpu_state_shift`：

[picorv32.v:1829-1852](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1829-L1852) 是整个两阶段移位的核心。逐段看：

```verilog
cpu_state_shift: begin
    latched_store <= 1;                 // 结果待回写
    if (reg_sh == 0) begin              // 收尾：写回，返回 fetch
        reg_out <= reg_op1;
        mem_do_rinst <= mem_do_prefetch;
        cpu_state <= cpu_state_fetch;
    end else if (TWO_STAGE_SHIFT && reg_sh >= 4) begin   // 大步：移 4 位
        case (1'b1)
            instr_slli || instr_sll: reg_op1 <= reg_op1 << 4;
            instr_srli || instr_srl: reg_op1 <= reg_op1 >> 4;
            instr_srai || instr_sra: reg_op1 <= $signed(reg_op1) >>> 4;
        endcase
        reg_sh <= reg_sh - 4;
    end else begin                                       // 小步：移 1 位
        case (1'b1)
            instr_slli || instr_sll: reg_op1 <= reg_op1 << 1;
            instr_srli || instr_srl: reg_op1 <= reg_op1 >> 1;
            instr_srai || instr_sra: reg_op1 <= $signed(reg_op1) >>> 1;
        endcase
        reg_sh <= reg_sh - 1;
    end
end
```

几个要点：

- `reg_op1` 既是源操作数，又被原地更新（每拍移一点），所以 `reg_op1` 在迭代过程中逐步逼近最终结果，收尾时直接 `reg_out <= reg_op1`。
- 移位方向由 `instr_*` 决定：左移 `<<`、逻辑右移 `>>`、算术右移 `$signed(...) >>>`。算术右移再次靠 `$signed` 注入符号位。
- 大步分支的守卫是常量 `TWO_STAGE_SHIFT`：当它为 0 时，综合器把 `else if (TWO_STAGE_SHIFT && ...)` 折叠成永假，整个 4 位 `case` 消失，只剩 1 位小步——电路更小，但更慢（参见 4.2.2 的公式）。
- `latched_store <= 1` 加上收尾的 `reg_out <= reg_op1`，使得回到 `fetch` 状态时结果被写回寄存器堆（`fetch` 是唯一的回写点，见 u4-l2）。

#### 4.2.4 代码实践

**目标**：手算一次完整的两阶段移位迭代，确认公式与源码一致。

**步骤**：

1. 取指令 `slli x2, x1, 13`（移位量 \(N=13\)），假设 `x1 = 0x00000001`。
2. 列表逐拍记录 `reg_sh` 与 `reg_op1` 的变化，按 [picorv32.v:1835-1850](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1835-L1850) 的规则推进。
3. 用公式验证总拍数。

**需要观察的现象（可手算）**：

| 拍 | 入口 `reg_sh` | 动作 | `reg_op1`（左移后） | 出口 `reg_sh` |
| --- | --- | --- | --- | --- |
| 1 | 13 | 13≥4，移 4 | `0x00000010` | 9 |
| 2 | 9 | 9≥4，移 4 | `0x00000100` | 5 |
| 3 | 5 | 5≥4，移 4 | `0x00001000` | 1 |
| 4 | 1 | 1<4，移 1 | `0x00002000` | 0 |
| 5 | 0 | 收尾 | `reg_out<=0x00002000` | — |

**预期结果**：`x2 = 0x00002000`（即 \(1 \ll 13\)），共停留 5 拍。对照公式 \(T_{\text{shift}} = \lfloor 13/4 \rfloor + (13 \bmod 4) + 1 = 3 + 1 + 1 = 5\)，吻合。

#### 4.2.5 小练习与答案

**练习 1**：`srai x2, x1, 31`（\(N=31\)）在默认配置下要几拍？若关掉 `TWO_STAGE_SHIFT` 呢？

**答案**：默认 \(T_{\text{shift}} = \lfloor 31/4 \rfloor + (31 \bmod 4) + 1 = 7 + 3 + 1 = 11\) 拍。关掉两阶段则 \(= 31 + 1 = 32\) 拍。

**练习 2**：为什么收尾条件是 `reg_sh == 0` 而不是「移位完成」标志位？

**答案**：因为迭代是「把 `reg_sh` 从 \(N\) 递减到 0」的过程，`reg_sh` 本身就是剩余移位量。当它归零，意味着恰好移完了 \(N\) 位，`reg_op1` 此时就是最终结果。用 `reg_sh==0` 作结束条件省去了一个额外标志位，状态更简单。

---

### 4.3 移位状态机：派发路径与三种配置对比

#### 4.3.1 概念说明

`cpu_state_shift` 自己只负责「迭代」。但一条移位指令在进入它之前，还要先经过 `ld_rs1`（必要时还有 `ld_rs2`）的**派发**：根据 (a) 是立即数移位还是寄存器移位、(b) 是单端口还是双端口寄存器堆、(c) 是否打开了 `BARREL_SHIFTER`，决定：

- 移位量装进 `reg_sh`（迭代路径）还是 `reg_op2`（桶形路径）；
- 下一状态去 `cpu_state_shift`（迭代）还是 `cpu_state_exec`（桶形，走 ALU）。

这一节把整条路径串起来，并用本讲的核心实践任务对比三种配置下 `slli` 的周期数与硬件。

#### 4.3.2 核心流程

以 `slli rd, rs1, shamt`（立即数左移）为例，三种配置的状态流转：

```text
【默认：TWO_STAGE_SHIFT=1, BARREL_SHIFTER=0】
fetch ──► ld_rs1（reg_sh<=shamt, reg_op1<=rs1）──► shift（迭代 T_shift 拍）──► fetch

【TWO_STAGE_SHIFT=0, BARREL_SHIFTER=0】
fetch ──► ld_rs1（同上）──► shift（迭代 N+1 拍）──► fetch
（区别仅在 shift 内部不出现 4 位大步）

【BARREL_SHIFTER=1】
fetch ──► ld_rs1（reg_op2<=shamt, reg_op1<=rs1）──► exec（ALU 一拍算 alu_shl）──► fetch
（完全不进入 cpu_state_shift）
```

注意一个细节：立即数移位的移位量 `shamt` 在指令字里占的就是 `rs2` 字段（`inst[24:20]`），所以译码器把它放进 `decoded_rs2`；派发时无论是装进 `reg_sh`（迭代）还是 `reg_op2`（桶形），读的都是 `decoded_rs2`。

#### 4.3.3 源码精读

`cpu_state_shift` 的 one-hot 编码：

[picorv32.v:1177](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1177) `localparam cpu_state_shift = 8'b00000100;`。

派发发生在 `ld_rs1`。**立即数移位（slli/srli/srai）且未开桶形**时，直接装 `reg_sh` 并跳 `shift`：

[picorv32.v:1704-1711](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1704-L1711) —— `reg_op1 <= cpuregs_rs1; reg_sh <= decoded_rs2; cpu_state <= cpu_state_shift;`。注意它读 `decoded_rs2`（即 `shamt`）而非 `decoded_imm`。

```verilog
is_slli_srli_srai && !BARREL_SHIFTER: begin
    reg_op1 <= cpuregs_rs1;
    reg_sh  <= decoded_rs2;          // shamt 装进移位量寄存器
    cpu_state <= cpu_state_shift;
end
```

**立即数移位且开了桶形**时，改走 ALU：

[picorv32.v:1712-1717](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1712-L1717) 把移位量装进 `reg_op2`（ALU 的第二操作数），`cpu_state <= cpu_state_exec`：

```verilog
is_jalr_addi_slti_sltiu_xori_ori_andi, is_slli_srli_srai && BARREL_SHIFTER: begin
    reg_op1 <= cpuregs_rs1;
    reg_op2 <= is_slli_srli_srai && BARREL_SHIFTER ? decoded_rs2 : decoded_imm;
    ...
    cpu_state <= cpu_state_exec;
end
```

**寄存器移位（sll/srl/sra）**的移位量来自 `rs2` 寄存器而非立即数，所以要等读出 `rs2` 之后才能派发。双端口寄存器堆在 `ld_rs1` 一拍里同时读出 `rs1`、`rs2`：

[picorv32.v:1731-1743](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1731-L1743) —— `reg_sh <= cpuregs_rs2; reg_op2 <= cpuregs_rs2;`，并在 `is_sll_srl_sra && !BARREL_SHIFTER` 时 `cpu_state <= cpu_state_shift`。

单端口寄存器堆（`ENABLE_REGS_DUALPORT=0`）一拍只能读一个寄存器，所以要先去 `ld_rs2` 再派发：

[picorv32.v:1759-1793](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1759-L1793) —— `cpu_state_ld_rs2` 里 `reg_sh <= cpuregs_rs2`，然后在 1791-1793 同样判断 `is_sll_srl_sra && !BARREL_SHIFTER` 跳 `shift`。这正是单端口下寄存器移位比立即数移位多 1 拍的根因（参见 u5-l1）。

最后，形式化验证里有一处明确把 `cpu_state_shift` 列为「合法停留状态」：

[picorv32.v:2131](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2131) `if (cpu_state == cpu_state_shift) ok = 1;`——表明移位迭代可以合法地占用任意多拍，不会被「卡死」检查误判。

README 给出的移位指令整体周期区间（含取指、读寄存器等开销）：

[README.md:354](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L354) —— 移位指令双端口 4–14 拍、单端口 4–15 拍。这里的下界对应移位量为 0/1，上界对应移位量为 31；单端口比双端口多出的 1 拍正是 `ld_rs2`（仅寄存器移位才需要）。

#### 4.3.4 代码实践（本讲核心任务）

**目标**：对比 `slli` 指令在三种配置下的执行周期数与所需硬件，并画出 `cpu_state_shift` 的迭代流程。

**步骤**：

1. **解析周期（可纯手算）**。对 `slli x2, x1, 31`（\(N=31\)），按本讲公式填写下表。设双端口寄存器堆、理想 1 拍取指：

   | 配置 | 进入的状态序列 | `cpu_state_shift` 内拍数 | 整条指令大约拍数 |
   | --- | --- | --- | --- |
   | `TWO_STAGE_SHIFT=1, BARREL_SHIFTER=0` | fetch→ld_rs1→shift→fetch | 11 | 约 13 |
   | `TWO_STAGE_SHIFT=0, BARREL_SHIFTER=0` | fetch→ld_rs1→shift→fetch | 32 | 约 34 |
   | `BARREL_SHIFTER=1` | fetch→ld_rs1→exec→fetch | 0（不进入） | 约 3（同 addi） |

   说明：`cpu_state_shift` 内的拍数由源码精确决定（见 4.2.2 公式）；整条指令还含 `fetch`、`ld_rs1` 各 1 拍，以及握手可能引入的等待拍。README 给出的整体区间为 4–14 拍（双端口），与本表的「约 13」基本吻合，细微差异来自访存握手——**待本地用仿真计时核实**。

2. **画 `cpu_state_shift` 的迭代流程**（以 `TWO_STAGE_SHIFT=1`、`slli x2,x1,13` 为例，标注 `reg_sh` 变化）：

   ```text
          入口: reg_op1 = x1, reg_sh = 13
                    │
                    ▼
        ┌───── reg_sh == 0 ? ─────┐
        │ 否                      │ 是
        ▼                         ▼
   reg_sh ≥ 4 ?              reg_out <= reg_op1
   ├─ 是: reg_op1 << 4       cpu_state <= fetch
   │       reg_sh -= 4           （结束）
   │       (13→9→5→1 共 3 次)
   └─ 否: reg_op1 << 1
           reg_sh -= 1
           (1→0 共 1 次)
        │
        └──► 回到顶部判断 reg_sh == 0
   ```

   对照 4.2.4 的拍级表，`reg_sh` 的轨迹为 `13 → 9 → 5 → 1 → 0`（收尾）。

3. **（可选，待本地验证）仿真测时**。在 `testbench_ez.v` 里，每次 `mem_instr` 命中（取指）时打印 `$time`；用一条 `slli x2,x1,31` 前后各放一条 `nop`（或可识别指令），读出两次取指的时间差即为该指令的周期数。分别用默认参数与 `BARREL_SHIFTER=1`（在测试台实例化时改参数）跑两次，对比测得的周期数是否与上表一致。

**需要观察的现象**：默认配置下 `slli ...31` 明显比 `addi` 慢很多（多花约 10 拍在 `shift`）；打开 `BARREL_SHIFTER` 后它变得和 `addi` 一样快，代价是 ALU 关键路径变长。

**预期结果**：周期数符合上表；迭代流程图中 `reg_sh` 严格单调递减，每次减 4 或减 1，归零时收尾。

#### 4.3.5 小练习与答案

**练习 1**：同样是 `sll x3, x1, x2`（寄存器移位），在双端口和单端口寄存器堆下，状态序列有何不同？

**答案**：双端口在 `ld_rs1` 一拍内同时读出 `rs1`、`rs2`（[picorv32.v:1731-1732](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1731-L1732)），随即进入 `cpu_state_shift`，序列为 `fetch→ld_rs1→shift→fetch`。单端口必须先读完 `rs1`，再花一拍去 `ld_rs2` 读 `rs2`（[picorv32.v:1759-1762](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1759-L1762)），序列为 `fetch→ld_rs1→ld_rs2→shift→fetch`，多 1 拍。

**练习 2**：为什么立即数移位（`slli` 等）在单端口下**不**比双端口慢，而寄存器移位（`sll` 等）会慢？

**答案**：立即数移位的移位量 `shamt` 来自指令字（`decoded_rs2`），不需要读 `rs2` 寄存器，所以单/双端口都在 `ld_rs1` 直接派发（[picorv32.v:1704-1711](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1704-L1711)），路径相同。寄存器移位需要真正读 `rs2`，单端口缺第二个读口，才多出 `ld_rs2` 这一拍。

**练习 3**：如果应用里几乎没有移位指令，三种配置里该选哪个？

**答案**：选 `TWO_STAGE_SHIFT=0`（甚至可以保持默认）。移位指令极少时，移位器的速度不影响整体性能，反而应追求最小面积——纯逐位移位的硬件最小。`BARREL_SHIFTER=1` 反而会无谓地增大 ALU 关键路径、拉低全核 fmax，得不偿失。

## 5. 综合实践

把本讲三块知识串起来：给 PicoRV32 写一份「移位策略选型备忘录」。

**任务背景**：假设你要把 PicoRV32 用在一个加密协处理器里，核心循环大量执行 `slli`/`srli`/`sra`（移位是加密算法的常见操作），目标是「单位时间内完成尽可能多的移位」，但 fmax 不能掉太多。

**要做的事**：

1. **分析**：在「大量移位」的负载下，默认两阶段迭代的平均移位周期（假设移位量均匀分布于 0–31）大约是多少？用 4.2.2 的公式算平均 \(E[T_{\text{shift}}]\)。提示：\(\lfloor N/4 \rfloor\) 与 \(N \bmod 4\) 在 0–31 上分别求平均。
2. **对比**：若改用 `BARREL_SHIFTER=1`，每条移位变成约 3 拍，但 fmax 假设下降 10%。粗略判断哪种配置在「移位吞吐量 = fmax / 平均移位拍数」上更优。
3. **读源码核对**：确认 `BARREL_SHIFTER=1` 时 `cpu_state_shift` 完全不会被进入（看 [picorv32.v:1712-1717](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1712-L1717) 与 [picorv32.v:1280-1283](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1280-L1283)），并指出此时 `reg_sh` 是否还会被综合保留。
4. **结论**：写出你推荐的配置与理由。

**预期产出**：一张含「平均移位拍数、相对 fmax、移位吞吐量」三列的对比表，以及一段不超过 5 行的选型建议。本题的部分数值依赖具体工艺下的 fmax 变化，结论中的吞吐量数字**待本地综合验证**，但推理过程应自洽。

## 6. 本讲小结

- PicoRV32 提供**三种可配置移位策略**：默认的 `TWO_STAGE_SHIFT` 两阶段迭代、关闭后的纯逐位迭代、以及 `BARREL_SHIFTER` 单周期桶形移位，三者是「速度 vs 面积 vs 主频」的取舍。
- 两阶段迭代的精髓是「**`reg_sh ≥ 4` 时移 4 位，否则移 1 位**」，把最坏 31 拍压到 11 拍；停留拍数 \(T_{\text{shift}} = \lfloor N/4 \rfloor + (N \bmod 4) + 1\)。
- `cpu_state_shift` 用 `reg_op1` 原地迭代、`reg_sh` 递减计数，`reg_sh == 0` 时收尾写回；算术右移靠 `$signed(...) >>>` 注入符号位。
- `BARREL_SHIFTER=1` 时移位并入 ALU（`alu_shl`/`alu_shr` → `alu_out`），完全不进入 `cpu_state_shift`，移位指令耗时与普通 ALU 指令相同。
- 派发路径由 `ld_rs1`（必要时加 `ld_rs2`）按「立即数/寄存器、单/双端口、是否桶形」四象限分流到 `shift` 或 `exec`；立即数移位用 `decoded_rs2` 作移位量。
- 选型经验：移位密集且在意吞吐选桶形；移位稀疏选逐位（最小面积）；默认两阶段是通用均衡点。

## 7. 下一步学习建议

- 本讲只讲了「移位」这一类需要多周期迭代的数据通路操作。下一类需要专用状态机的运算是**乘除法**——它们通过 `cpu_state_ld_rs2` 里的 PCPI 派发（[picorv32.v:1768-1785](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1768-L1785)）外包给协处理器，建议接着学 **u6-l1 PCPI 协处理器接口**。
- 若想继续深挖数据通路，可回到 **u5-l1** 复习 ALU 的 `alu_add_sub`/`alu_out_0` 如何与本讲的 `alu_shl`/`alu_shr` 共用同一个 `alu_out` 选择器。
- 若对「配置参数如何改变最终电路」感兴趣，可结合 **u3-l1** 的 `ENABLE_*`/`TWO_*` 参数体系和 README 的 small/regular/large 三档综合数据，理解本讲的 `BARREL_SHIFTER` 为何会显著抬高 large 配置的 LUT 数。
