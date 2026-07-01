# RVV ALU、浮点与除法算术单元

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 RVV 后端里「算术类」处理单元（ALU / DIV / FALU / FDIV）各自的职责与数量配置。
- 在 `rvv_backend_alu_unit` 内部区分 **addsub / shift / mask / other** 四个一拍并行的子单元，并解释它们如何共用一条两拍流水。
- 解释整数向量除法为什么是**可变延迟**：恢复余数 + 前导零跳过的迭代式除法器，以及它如何用 `div_ready` 向派发端反压。
- 理解浮点向量算术（falu / fdiv）通过包装开源 `cvfpu`/`fpnew` 实现，并由 `ZVE32F_ON` 编译宏门控。
- 厘清 **Chisel 侧 `RvvAlu` 与 SV 侧 `rvv_backend_alu` 的边界**：前者只是枚举与译码中间表示，后者才是真正的执行电路。

## 2. 前置知识

本讲是第 7 单元（RVV 向量/矩阵后端）的一环，默认你已经读过：

- **u7-l1（RVV 后端总览）**：知道 RVV 后端是「Chisel 前端（RvvCore/译码/派发）+ SystemVerilog 后端（执行单元 + ROB + 退休）」的两段式结构。
- **u7-l2（RVV 译码与派发）**：知道一条向量指令被拆成 μop，经保留站（Reservation Station, RS）发射到各处理单元（Processing Unit, PU），结果再写回 ROB。
- **u7-l3（VRF/ROB/退休）**：知道结果经 `PU2ROB_t` 回灌 ROB、按序退休。

几个本讲反复出现的术语，先用一句话热身：

- **μop（micro-op）**：一条向量指令被拆成若干条更细粒度的操作（例如按 `uop_index` 分块），每条 μop 携带 `rob_entry`、操作码、源操作数等。
- **EEW（Effective Element Width）**：当前向量元素的位宽，取 `EEW8/EEW16/EEW32`（8/16/32 位）。同一份 128/256 位向量数据，按不同 EEW 切成不同数量的元素。
- **VLEN**：向量寄存器位宽。本仓库构建宏 `VLEN_128`（`VLEN=128`）；下面的乘法器/除法器阵列数量都随 VLEN 缩放。
- **vxrm / vxsat**：定点舍入模式与饱和标志（向量定点算术专属的「CSR」），舍入有 RNU/RNE/RDN/ROD 四种。
- **fpnew / cvfpu**：开源浮点 IP（PULP 平台 `fpnew`），CoralNPU 直接拿来当浮点执行电路。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [hdl/verilog/rvv/design/rvv_backend_alu.sv](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_alu.sv) | 向量 ALU 顶层：实例化 `NUM_ALU` 个单元，做双发射仲裁，cmp 指令只能进 0 号单元。 |
| [hdl/verilog/rvv/design/rvv_backend_alu_unit.sv](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_alu_unit.sv) | 单个 ALU 单元：并行挂 addsub/shift/mask/other 四个子单元，加一拍 P1 终结级。 |
| hdl/verilog/rvv/design/rvv_backend_alu_unit_addsub.sv | 加/减/饱和/带进位/min-max/宽化子单元。 |
| hdl/verilog/rvv/design/rvv_backend_alu_unit_shift.sv | 移位/舍入移位/窄化/裁剪（clip）子单元。 |
| hdl/verilog/rvv/design/rvv_backend_alu_unit_mask.sv | 逻辑与/或/异或、掩码归约、VID/VFIRST 等子单元。 |
| hdl/verilog/rvv/design/rvv_backend_alu_unit_other.sv | VMERGE/VMV、符号/零扩展等「其它」逻辑子单元。 |
| hdl/verilog/rvv/design/rvv_backend_alu_unit_execution_p1.sv | P1 终结级：比较结果合成、饱和、min/max 选择。 |
| [hdl/verilog/rvv/design/rvv_backend_div.sv](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_div.sv) | 向量除法顶层：按 `is_div` 把 μop 分给整数除法器或（可选）浮点除法器。 |
| hdl/verilog/rvv/design/rvv_backend_div_unit.sv | 整数向量除法：把 VLEN 切成 8/16/32 位通道，阵列化实例化 `intdivider`。 |
| hdl/verilog/rvv/design/rvv_backend_div_unit_divider.sv | 单 lane 的恢复余数迭代除法器（FSM + 前导零跳过）。 |
| [hdl/verilog/rvv/design/rvv_backend_falu.sv](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_falu.sv) | 浮点 ALU 顶层：把浮点 μop 按 addmul/cmp/cvt/tbl 分类，路由到 `NUM_FMA` 个 falu_unit。 |
| hdl/verilog/rvv/design/rvv_backend_fdiv_unit.sv | 浮点除法/开方单元，包装 `fpnew_divsqrt_th_64_multi`。 |
| [hdl/chisel/src/coralnpu/rvv/RvvAlu.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/rvv/RvvAlu.scala) | Chisel 侧：仅用 `ChiselEnum` 枚举 ALU 操作码 + 一个译码中间表示 Bundle，**不实现任何算术**。 |
| hdl/verilog/rvv/inc/rvv_backend_define.svh | 关键宏：`NUM_ALU=2`、`NUM_DIV=1`、`NUM_FMA=2`、`ZVE32F_ON` 门控等。 |

## 4. 核心概念与源码讲解

### 4.1 向量 ALU 的子单元划分：addsub / shift / mask / other

#### 4.1.1 概念说明

向量 ALU 承担 RVV 里所有「按元素、一拍可完成」的整数逻辑算术：加减、min/max、逻辑运算、移位、定点舍入移位、窄化与裁剪（narrowing/clip）、掩码归约、merge/move、比较等。指令种类极多，但本质上可以按**数据通路形状**归为四类：

| 子单元 | 代表指令 | 通路特征 |
| --- | --- | --- |
| **addsub** | `VADD/VSUB/VSADD/VSSUB/VADC/VMSEQ/VMIN/VMAX/VWADD…` | 逐字节加/减，含进位、饱和、宽化 |
| **shift** | `VSLL/VSRL/VSRA/VSSRL/VSSRA/VNSRL/VNCLIP/VNCLIPU` | 桶形移位 + vxrm 舍入 + 饱和 |
| **mask** | `VAND/VOR/VXOR/VMAND/VMOR/VMSBF/VID/VFIRST` | 逐位逻辑、掩码归约、元素编号 |
| **other** | `VMERGE/VMV/VZEXT/VSEXT/VSMUL` | 合并、扩展、标量搬运 |

四个子单元**同时**对同一份输入 μop 做译码与计算，每个子单元只在「这条指令属于我」时才把 `result_valid` 拉高——于是它们用 valid 信号互斥，天然不会撞车。这种「并行全算、valid 选通」的设计避免了在组合逻辑里串一张巨大的指令译码 Mux，时序更好。

#### 4.1.2 核心流程

一条 μop 进入 `rvv_backend_alu_unit` 后：

1. 拆出 `uop_funct6 / uop_funct3 / vs2_eew / vs1_data / vs2_data / rs1_data` 等字段，广播给四个子单元。
2. 每个子单元各自判断「我认不认这条指令」：认则 `result_valid_xxx_p0 = alu_uop_valid`，并产出 `result_xxx_p0`。
3. 顶层用 `case(1'b1)` 在四个 `result_valid_*_p0` 里**择一**，把胜出的结果送进 P1 寄存器（见 4.2）。

伪代码：

```text
if   addsub.recognizes(op): result_p0 = addsub(op, vs2, vs1); valid = ADDSUB
elif shift.recognizes(op):  result_p0 = shift(op, vs2, vs1, vxrm); valid = SHIFT
elif mask.recognizes(op):   result_p0 = mask(op, vs2, vs1, v0);  valid = MASK
elif other.recognizes(op):  result_p0 = other(op, vs2, vs1, v0); valid = OTHER
```

#### 4.1.3 源码精读

**四个子单元并行挂载**——注意它们共享同一组 `alu_uop_valid/alu_uop` 输入，各自只输出 `result_valid_*_p0`：[rvv_backend_alu_unit.sv:64-95](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_alu_unit.sv#L64-L95) 实例化 `u_alu_addsub / u_alu_shift / u_alu_mask / u_alu_other`。

**addsub 的逐字节加/减**——把整条向量拆成 `VLENB` 个字节通道，每通道一个 9 位加法（含进位 `cout8`），由 `opcode` 在 `ADDSUB_VADD` 与 `ADDSUB_VSUB` 间二选一：[rvv_backend_alu_unit_addsub.sv:1528-1535](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_alu_unit_addsub.sv#L1528-L1535)。比较、min/max、带进位加减也复用这同一组加减法，只是 `opcode` 取 `VSUB`。

**shift 的桶形移位 + vxrm 舍入**——每个通道调用一个 `barrel_shifter`，左/逻辑右/算术右由 `shift_mode` 控制：[rvv_backend_alu_unit_shift.sv:399-427](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_alu_unit_shift.sv#L399-L427) 定 `shift_mode`；[rvv_backend_alu_unit_shift.sv:433-466](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_alu_unit_shift.sv#L433-L466) 实例化 `barrel_shifter`。移出的低位被保留为 `round_bits`，再按 `vxrm ∈ {RNU,RNE,RDN,ROD}` 决定是否 `+1`：[rvv_backend_alu_unit_shift.sv:511-578](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_alu_unit_shift.sv#L511-L578)。

**mask 的逐位逻辑 + 掩码归约**——逻辑运算直接用 `& / | / ^`；`VMSBF/VMSOF/VMSIF`（找首个/末个置位）用 `src2 & ~(src2-1)` 这类位技巧实现：[rvv_backend_alu_unit_mask.sv:243-269](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_alu_unit_mask.sv#L243-L269) 算出所有候选，[rvv_backend_alu_unit_mask.sv:291-374](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_alu_unit_mask.sv#L291-L374) 按 `funct6` 选最终结果。

> 说明：以上四个子单元文件都很长（addsub 约 1580 行、shift 约 1010 行），绝大多数篇幅是「按 EEW8/16/32 × OPIVV/OPIVX/OPIVI 把标量 `rs1` 广播成向量」的源数据准备表。阅读时**先跳过这些 for 循环数据搬运**，只盯 `result_valid` 译码表与最末的 `result_data` 计算段即可。

#### 4.1.4 代码实践

**实践目标**：在不跑仿真的前提下，靠「valid 译码表」给一条指令归类到具体子单元。

**操作步骤**：

1. 打开 `rvv_backend_alu_unit_addsub.sv`，找到 `always_comb begin ... result_valid = 'b0;` 段（约 L76-L287），列出它认的 `funct6` 集合。
2. 同样在 `rvv_backend_alu_unit_shift.sv`（L99-L122）、`rvv_backend_alu_unit_mask.sv`（L106-L156）、`rvv_backend_alu_unit_other.sv`（L77 起）各列一份。
3. 任取三条指令 `vadd.vv`、`vssrl.vi`、`vmandn.mm`，分别查表判断它走哪个子单元。

**需要观察的现象**：四张表的 `funct6` 集合应当**两两不相交**——同一条指令不可能同时被两个子单元认领，否则顶层 `case(1'b1)` 会出多驱动冲突。

**预期结果**：`vadd` → addsub；`vssrl`（带舍入右移）→ shift；`vmandn`（掩码与非）→ mask。若你发现某指令同时出现在两张表里，说明你的理解有误，回到源码核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么四个子单元要「同时算、用 valid 选」，而不是先译码出「属于谁」再只让那一个算？
**答案**：把译码 Mux 拆进各子单元、用 `result_valid` 互斥选通，可以让每条子通路的关键路径更短、布局更规整；同时各子单元可独立综合与验证。代价是少量冗余翻转（没被选中的子单元也在算），但向量 ALU 一拍出结果，这点功耗可接受。

**练习 2**：`vnclipu`（无符号窄化裁剪）属于哪个子单元？它的饱和结果存在哪个字段？
**答案**：属于 shift 子单元（见 [rvv_backend_alu_unit_shift.sv:801-872](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_alu_unit_shift.sv#L801-L872)），饱和信息写进 `result.vsaturate`（`upoverflow`），由后续 VXSAT 累加。

---

### 4.2 ALU 顶层双发射、cmp 专用单元与两拍流水

#### 4.2.1 概念说明

`rvv_backend_alu` 是 ALU 的「车间主任」：它从 ALU 保留站一次最多收 2 条 μop（`NUM_ALU=2`），分派给两个 ALU 单元，再把两路结果交还 ROB。这里有两个关键约束：

1. **cmp 指令只能进 0 号单元**：只有 `u_alu_cmp_unit` 把参数 `CMP_SUPPORT` 设为 1（[rvv_backend_alu.sv:95-97](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_alu.sv#L95-L97)），其余单元 `CMP_SUPPORT=0`。所以比较类指令是结构冒险源——一个周期最多只发一条 cmp。
2. **ALU 是两拍流水**：P0 在子单元里组合算出原始和/差/移位值；P1 是寄存器级 `rvv_backend_alu_unit_execution_p1`，负责把原始结果「终结」成最终值（合成比较、min/max 选择、饱和判定）。

#### 4.2.2 核心流程

派发仲裁（简化）：

```text
读 result_ready[1:0]（ROB 是否能收两路结果）
  2'b11: uop0→unit0, uop1→unit1（uop1 不能是 cmp）
  2'b01: uop0→unit0, unit1 空闲
  2'b10: uop0→unit1，但仅当 uop0 不是 cmp
```

单条 μop 在 unit 内的流水：

```text
P0(组合): addsub/shift/mask/other 之一算出 result_*_p0
   ↓ 锁存一拍
P1(寄存器): execution_p1 做比较合成/饱和/min-max → result_p1
   ↓ result_valid 上报 ROB
```

#### 4.2.3 源码精读

**双发射仲裁**——用 `result_ready` 决定能否同时喂两条 μop，并对 cmp 做特殊放行：[rvv_backend_alu.sv:58-93](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_alu.sv#L58-L93)。注意 `2'b10` 分支里 `alu_valid[1] = uop_valid[0] & (!uop[0].is_cmp)`——cmp 不能借道 unit1。

**cmp 专用单元实例化**——0 号单元带 `CMP_SUPPORT(1'b1)`，其余由 `for (i=1; i<NUM_ALU; ...)` 生成：[rvv_backend_alu.sv:95-131](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_alu.sv#L95-L131)。

**P0→P1 的择一与锁存**——顶层用 `case({result_valid_p1, 任一p0_valid})` 管理流水占用，并在 `case(1'b1)` 里从四个 p0 结果中选一个打入 `alu_uop_p1`：[rvv_backend_alu_unit.sv:99-159](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_alu_unit.sv#L99-L159)。

**P1 终结级**——`execution_p1` 承接锁存后的 `PIPE_DATA_t`，做比较结果合成与饱和：[rvv_backend_alu_unit_execution_p1.sv:9-90](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_alu_unit_execution_p1.sv#L9-L90) 声明了 `is_cmp`、各 EEW 的 `product/minmax`、溢出标志等中间量。

#### 4.2.4 代码实践

**实践目标**：用一个具体场景验证「cmp 一周期一条」的结构冒险。

**操作步骤**：

1. 假设派发端同周期给出两条 μop：`uop[0] = vmseq`（比较，`is_cmp=1`）、`uop[1] = vadd`。
2. 假设此时 ROB 两路都 ready（`result_ready = 2'b11`）。
3. 套用 [rvv_backend_alu.sv:76-83](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_alu.sv#L76-L83) 的 `2'b11` 分支计算 `alu_valid[1]`。

**需要观察的现象**：`alu_valid[1] = uop_valid[1] & (!uop[1].is_cmp)`，由于 `uop[1]` 是 vadd（非 cmp），可进 unit1；`uop[0]` 是 vmseq 进 unit0。两条都能发。但如果把两条都换成 `vmseq`，`uop[1]` 会被 `!is_cmp` 屏蔽，只能发一条。

**预期结果**：连续两条 cmp 不能同周期双发——这正是派发端做结构冒险检测时要向 ROB 报告的约束。具体反压由派发器实现，本讲只确认 ALU 侧的硬约束来源。

#### 4.2.5 小练习与答案

**练习 1**：为什么 cmp 不像 addsub 那样在所有单元都实现？
**答案**：cmp 需要额外的「逐元素比较 → 压成一位/掩码」的终结逻辑（在 `execution_p1` 里），面积与连线代价高；而 ML 负载里比较指令占比低。只在一个单元配 `CMP_SUPPORT=1` 是面积与吞吐的折中，代价是 cmp 成为结构冒险点。

**练习 2**：ALU 的「两拍」分别由谁构成？为什么 addsub 自己是组合的却还要 P1？
**答案**：P0 是四个子单元的组合计算，P1 是 `execution_p1` 寄存器级（做比较合成/饱和/min-max）。即便 addsub 一拍能算出和/差，比较与饱和的终结逻辑路径较长，拆到 P1 是为了收敛时序。

---

### 4.3 整数向量除法：可变延迟的恢复余数除法器

#### 4.3.1 概念说明

整数除法是「慢且稀疏」的运算，所以 RVV 后端只配 **1 个**除法单元（`NUM_DIV=1`，见 [rvv_backend_define.svh:68](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L68)）。它要处理 `VDIV/VDIVU/VREM/VREMU` 四种（有/无符号 × 商/余数），EEW8/16/32 三种位宽。

除法没法像加法那样一拍算完，CoralNPU 用经典的**恢复余数（restoring）迭代除法**，并用**前导零跳过**让延迟随操作数大小变化——这就是「可变延迟」。计算期间除法器拉低 `div_uop_ready`，向派发端反压，阻止新 μop 进入。

#### 4.3.2 核心流程

`rvv_backend_div`（顶层）→ `rvv_backend_div_unit`（向量包装）→ `intdivider`（单 lane 迭代核）。

1. **顶层分流**：按 `uop.is_div` 把 μop 分给整数除法器；若开了 `ZVE32F_ON`，浮点除法 μop 走 `rvv_backend_fdiv_unit`，二者结果经轮转仲裁器汇成一个 `result` 交 ROB。
2. **向量包装**：按 `vs2_eew` 把 VLEN 切成若干 lane（EEW8 切最多、EEW32 切最少），每个 lane 实例化一个 `intdivider`；只有「所有 lane 都 `result_valid`」时整条 μop 才算完成（`result_all_valid`）。
3. **迭代核 FSM**：`DIV_IDLE → DIV_WORKING → DIV_PRINT`。IDLE 里做特殊情形（除零、有符号溢出）与前导零计数；WORKING 里每拍跑 3 步 `f_div_step`（恢复余数移位减）；PRINT 里等 ROB 收结果。

前导零跳过的核心：被除数前导零越多，需要迭代的位数越少，`count_shift = 位宽+1 - clzb` 越小，WORKING 停留拍数越少。

恢复余数单步（`f_div_step`）的数学含义：

\[
\text{remainder\_tmp} = \{\,\text{remainder\_in}[\text{W-2:0}],\ \text{quotient\_in}[\text{W-1}]\,\}
\]
\[
\text{diff} = \text{remainder\_tmp} - \text{divisor}
\]

若 `diff ≥ 0`（不借位）则本位商 1、余数更新为 diff；否则商 0、余数保持。这就是手工竖式除法的硬件翻版。

#### 4.3.3 源码精读

**顶层 is_div 分流与（可选）浮点仲裁**：[rvv_backend_div.sv:73-126](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_div.sv#L73-L126)。`assign x_uop_vld[i] = uop_valid[i] & uop[i].is_div;` 把整数除法挑出来；浮点分支由 `ZVE32F_ON` 门控。

**向量包装：按 EEW 切 lane**：`rvv_backend_div_unit` 在 EEW8 时同时实例化 8/16/32 位三组除法器（因为一条 EEW8 μop 的数据也要能落到更宽的通路上以支持混合位宽）：[rvv_backend_div_unit.sv:334-404](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_div_unit.sv#L334-L404)。

**「全部 lane 完成」才上报**：[rvv_backend_div_unit.sv:417-431](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_div_unit.sv#L417-L431) 按 `res_info_d1.vs2_eew` 把对应位宽组的 `result_valid*` 做与。

**迭代核 FSM 状态机**：[rvv_backend_div_unit_divider.sv:218-249](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_div_unit_divider.sv#L218-L249) 定义三态迁移。

**前导零跳过决定迭代拍数**：[rvv_backend_div_unit_divider.sv:252-265](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_div_unit_divider.sv#L252-L265)，`count_shift = 位宽+1 - clzb(被除数)`。

**恢复余数单步 `f_div_step`**：[rvv_backend_div_unit_divider.sv:545-568](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_div_unit_divider.sv#L545-L568)。

**特殊情形：除零与有符号溢出**：[rvv_backend_div_unit_divider.sv:305-352](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_div_unit_divider.sv#L305-L352)。除零时商全 1、余数 = 被除数（符合 RISC-V 语义）；`-2^(W-1) / -1` 溢出时商取 `-2^(W-1)`。

**反压**：`assign div_ready = state==DIV_IDLE;`——只要除法器没回到空闲就不收新 μop。

#### 4.3.4 代码实践

**实践目标**：手工推演一次 8 位无符号除法在迭代核里的状态流转。

**操作步骤**：

1. 取 `dividend = 0b00001011`（11）、`divisor = 0b00000011`（3），无符号（`opcode = DIV_ZERO`）。
2. 在 [rvv_backend_div_unit_divider.sv:261-264](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_div_unit_divider.sv#L261-L264) 算 `clzb8(0b00001011)`：最高位起 4 个前导零，故 `clzb=4`，`count_shift = 9 - 4 = 5`。
3. 初始化 `quotient = dividend << clzb = 0b10110000`，`remainder = 0`。
4. 每拍用 `f_div_step` 跑 3 步（WORKING 每拍 3 步），共约 `ceil(5/3)=2` 拍完成 WORKING，再进 PRINT。

**需要观察的现象**：WORKING 的拍数取决于 `count_shift`，而被除数越小（前导零越多）拍数越少——这就是「可变延迟」。

**预期结果**：最终 `quotient = 3`、`remainder = 2`（11 = 3×3 + 2）。若把被除数换成 `0b10000000`（前导零为 0），`count_shift=9`，需要更多拍——延迟变长。**待本地验证**：可在 testbench 里强制这两个操作数、用波形观察 `state` 在 DIV_WORKING 停留的拍数差异。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `rvv_backend_div_unit` 在 EEW8 时要同时实例化 8/16/32 三组 `intdivider`？
**答案**：因为一条向量 μop 的数据按 EEW 解释，但硬件通道要覆盖各种可能的位宽组合；同时实例化多组、按 `vs2_eew` 选择性 `div_valid`，让同一组硬件能服务 EEW8/16/32 三种指令，避免为每种 EEW 各做一套完整阵列。

**练习 2**：除法进行时，派发端怎么知道不能往除法器塞新 μop？
**答案**：迭代核 `div_ready = (state==DIV_IDLE)`，向量包装把它聚合成 `div_uop_ready`（所有相关 lane 都 ready），顶层再把 `div_uop_ready` 回给保留站；非 IDLE 期间 `div_uop_ready=0`，RS 自然不会派发新除法 μop。

---

### 4.4 浮点向量算术：falu 与 fdiv（包装 fpnew）

#### 4.4.1 概念说明

浮点向量算术**不是手写 RTL**，而是包装开源 PULP `fpnew`（底层 `cvfpu`，含 OpenC910/E906 的除开方核）实现。这一层受 `ZVE32F_ON` 编译宏门控——默认生成的 `rvv_backend_config.svh` 里该宏是注释掉的（见 [RvvCore.scala:404](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/rvv/RvvCore.scala#L404) `//\`define ZVE32F_ON`），生产 SoC 才按需打开。

两个浮点单元：

- **falu（`NUM_FMA=2`）**：浮点加减乘、FMA、比较、转换（cvt）、查表（tbl）。它把浮点 μop 按 `addmul/cmp/cvt/tbl` 四类分派给 `rvv_backend_falu_unit`（内部包装 fpnew）。
- **fdiv（`NUM_FDIV=1`）**：浮点除法 `VFDIV/VFRDIV` 与开方 `VFSQRT`，每个 32 位 lane 一个 `fpnew_divsqrt_th_64_multi`，可变延迟。

和整数除法一样，浮点除/开方是可变延迟；falu 里的加减乘则是流水化的固定延迟。

#### 4.4.2 核心流程

**falu 顶层分派**（双单元、支持跨单元借道）：

```text
为每条 μop 算 uop_type = {tbl, cvt, cmp, addmul}
unit0 能接 uop0? → uop0 进 unit0；再看 unit1 能否接 uop1
否则看 unit1 能否接 uop0（uop0 优先级高）；若能，再看 unit0 能否接 uop1
```

**fdiv 执行**：

```text
按 funct6 选 op_type: VFDIV/VFRDIV→DIV, VFUNARY1(fsqrt)→SQRT
每个 32 位 lane 喂一对操作数给 fpnew_divsqrt
所有 lane out_valid → result_valid（& 归约）
```

#### 4.4.3 源码精读

**falu 四类分派**：[rvv_backend_falu.sv:59-73](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_falu.sv#L59-L73) 由 `uop_exe_unit ∈ {FMA, FCMP/FNCMP, FCVT, FTBL}` 算出 `uop_type`。

**falu 双单元借道路由**：[rvv_backend_falu.sv:82-119](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_falu.sv#L82-L119)，`uop0` 优先填 unit0，填不下才借 unit1；注释明确「uop0 has higher pri」。

**fdiv 的 op_type 选择**：[rvv_backend_fdiv_unit.sv:53-78](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_fdiv_unit.sv#L53-L78)，`VFDIV/VFRDIV→DIV`、`VFUNARY1→SQRT`；`VFRDIV` 还把 `vs1`（标量除数）广播到每个 lane 作 `src2`。

**fpnew 除开方核逐 lane 实例化**：[rvv_backend_fdiv_unit.sv:105-152](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_fdiv_unit.sv#L105-L152)，舍入模式由 `fdiv_uop.frm` 传入，浮点异常标志 `status_o` 回写到 `result.fpexp`。

> 边界提示：falu/fdiv 的真正算术在 `fpnew_*` 里（外部 IP，见 [RvvCore.scala:539-557](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/rvv/RvvCore.scala#L539-L557) 的 `addResource` 清单）。CoralNPU 自己写的只是「把 RVV μop 翻译成 fpnew 操作码 + 数据打包/解包 + 握手」这层胶水。

#### 4.4.4 代码实践

**实践目标**：追踪一条 `vfdiv.vv`（向量浮点除）从 μop 到 fpnew 调用的字段映射。

**操作步骤**：

1. 在 [rvv_backend_fdiv_unit.sv:58-67](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_fdiv_unit.sv#L58-L67) 确认 `VFDIV` 分支：`op_type = fpnew_pkg::DIV`、`src2 = vs2_data`、`src1 = vs1_data`（OPFVV 时）。
2. 看 [rvv_backend_fdiv_unit.sv:115-118](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_fdiv_unit.sv#L115-L118) 这对操作数怎么拼成 fpnew 的 `operands_i`，`rnd_mode_i` 从哪来。
3. 对照 [rvv_backend_fdiv_unit.sv:154](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_fdiv_unit.sv#L154) `result_valid = &sub_result_vld` 确认「所有 lane 完成才上报」。

**需要观察的现象**：`vfdiv.vv` 与 `vfrdiv.vf`（标量除数在前）的 `src1/src2` 接法是**互换**的——前者 `vs2/vs1`，后者把标量广播到 `src2`、`vs2` 当 `src1`。这反映了「被除数 ÷ 除数」在不同指令里操作数位置不同。

**预期结果**：你能画出 `vfdiv.vv` 的字段映射表：`vs2_data→src2(被除数)`、`vs1_data→src1(除数)`、`frm→rnd_mode`、`fpnew result→sub_result→result.w_data`。**待本地验证**：浮点路径需要 `ZVE32F_ON` 打开，默认构建里 falu/fdiv 不参与综合。

#### 4.4.5 小练习与答案

**练习 1**：为什么浮点加减乘（falu）是固定延迟，而浮点除/开方（fdiv）是可变延迟？
**答案**：加减乘/FMA 用组合或流水化的乘法器与加法器，延迟固定；除/开方依赖迭代收敛（SRT 等算法），延迟随操作数指数/尾数变化，故 fpnew 的 divsqrt 核是可变延迟，需要 `busy/in_ready` 握手。

**练习 2**：`falu_uop_rdy` 为什么是个 4 位向量？
**答案**：因为它对应 addmul/cmp/cvt/tbl 四类子操作各自的就绪信号（见 [rvv_backend_falu.sv:140-143](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_falu.sv#L140-L143)）。某类子操作在 falu_unit 内部可能延迟不同，就绪位各异，顶层用 `uop_type & falu_uop_rdy` 判断「这个 μop 能不能被这个 unit 接」。

---

### 4.5 Chisel 侧的边界：RvvAlu 只枚举、不实现

#### 4.5.1 概念说明

初学者最容易踩的坑：看到 `hdl/chisel/src/coralnpu/rvv/RvvAlu.scala` 里有 `VADD/VSUB/VAND/VSLL…` 一长串操作码，就以为「Chisel 实现了向量 ALU」。**不是的**。这个文件只做两件事：

1. `object RvvAluOp extends ChiselEnum` —— 用 Scala 枚举列出一组 ALU 操作码名字（给 Chisel 侧 S1 译码阶段当类型用）。
2. `class RvvS1DecodedInstruction` —— 一个 Bundle，含 `op / is_float / is_widening` 三个字段，作为译码后、派发前的中间表示。

真正的逐元素加法、移位、比较电路全在 SystemVerilog 的 `rvv_backend_alu*` 里。Chisel 侧（`RvvCore`/`RvvCoreWrapper`）通过 `addResource` 把这些 `.sv` 文件作为黑盒挂进综合，自己只负责取指、译码、派发、与标量核/LSU 的接口。

#### 4.5.2 核心流程

边界划分：

```text
Chisel (RvvCore)                          SystemVerilog (rvv_backend)
  取指 → RVV 译码                            执行单元 (ALU/MULMAC/DIV/FALU/LSU)
  → RvvS1DecodedInstruction{op,...}  ──→   μop → RS → PU → ROB → 退休
  → 派发到 SV 后端                            RvvAluOp 枚举在这里不出现，
  (RvvAlu.scala 只定义类型)                   SV 用自己的 funct6/funct3 译码
```

关键证据：`RvvCore.scala` 的 `RvvCoreWrapper` 用一长串 `addResource("hdl/verilog/rvv/design/rvv_backend_alu*.sv")` 把 SV 执行单元挂进工程——[RvvCore.scala:561-611](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/rvv/RvvCore.scala#L561-L611)。

#### 4.5.3 源码精读

**RvvAluOp 枚举**：[RvvAlu.scala:19-83](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/rvv/RvvAlu.scala#L19-L83)，纯名字列表，`Value` 自动编号，没有任何运算实现。注释里 `// TODO(davidgao): values here can be tweaked.` 也暗示这只是占位/可调整的编码。

**译码中间表示**：[RvvAlu.scala:89-93](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/rvv/RvvAlu.scala#L89-L93) `RvvS1DecodedInstruction`，文件头注释明确「validity ... can only be fully checked when the current vector config is known」——即它只做与配置无关的初步检查。

**SV 后端作为黑盒挂载**：[RvvCore.scala:485-611](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/rvv/RvvCore.scala#L485-L611) 一连串 `addResource`，`rvv_backend_alu.sv`、`rvv_backend_div.sv`、`rvv_backend_falu.sv` 全在其中。

#### 4.5.4 代码实践

**实践目标**：对比 Chisel `RvvAlu` 与 SV `rvv_backend_alu`，判断「哪些算术由谁实现」。

**操作步骤**：

1. 在 [RvvAlu.scala:19-83](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/rvv/RvvAlu.scala#L19-L83) 里找 `VADD`——你会发现它只是枚举里的一个名字，文件里**没有任何 `+` 运算**。
2. 在 [rvv_backend_alu_unit_addsub.sv:1528-1535](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_alu_unit_addsub.sv#L1528-L1535) 里找真正的逐字节加法。
3. 在 [RvvCore.scala:561-568](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/rvv/RvvCore.scala#L561-L568) 确认这些 SV 文件是被 Chisel 当资源挂进来的。

**需要观察的现象**：Chisel 侧的 `VADD` 名字与 SV 侧 `VADD` 是两套独立编码——Chisel 枚举值不会直接成为 SV 的 `funct6`。SV 侧用 RISC-V 原生的 `funct6/funct3` 字段自己译码。

**预期结果**：结论是「**全部**向量算术（加减/移位/逻辑/比较/除法/浮点）都由 SystemVerilog 实现，Chisel 只做取指译码与接口胶水」。这样划分的原因：RVV 执行数据通路的位宽切片、ECC、流水非常细碎，用 SystemVerilog 直接写比用 Chisel 高层抽象更可控；而取指/派发/与标量核耦合的部分用 Chisel 更简洁。

#### 4.5.5 小练习与答案

**练习 1**：如果想在 Chisel 侧（`RvvAlu.scala`）新增一条向量指令 `vfoo`，够不够？
**答案**：不够。在 `RvvAluOp` 加一个枚举值只是让 Chisel 译码阶段「认识」这个名字；真正的执行电路还得在 SV 侧对应的子单元（addsub/shift/mask/other）里加 `funct6` 译码与数据通路，并在 opcode 头文件、派发 hazard 表、ROB 等处同步更新。

**练习 2**：为什么 CoralNPU 不把 ALU 也用 Chisel 写，而要切到 SystemVerilog？
**答案**：RVV ALU 的数据通路要精细处理 EEW8/16/32 切片、tail/mask、定点舍入与饱和、widen/narrow 等，用 SystemVerilog 显式写 for-loop 切片与时序更直观、更易与商业 EDA 工具/形式验证配合；而 Chisel 更适合写高层状态机与接口。这是「按数据通路复杂度选语言」的工程取舍，与本手册 u1-l2 讲的「Chisel 写核/SoC、SV 写 RVV 后端」的分工一致。

## 5. 综合实践

把本讲串起来的小任务：**给一条 RVV 指令做「全旅程」溯源**。

任选一条指令，例如 `vadd.vv`（向量加）或 `vdivu.vv`（无符号向量除），完成下表（全部基于本讲读过的源码）：

| 维度 | 你的回答 |
| --- | --- |
| Chisel 侧枚举名（`RvvAluOp`） | ? |
| 进入哪个 SV 执行单元（ALU / DIV / FALU） | ? |
| 在该单元里走哪个子单元 / 哪条通路 | ? |
| 延迟特征（一拍 / 两拍 / 可变） | ? |
| 结果如何上报 ROB（`result_valid` 何时拉高） | ? |
| 是否有结构冒险（如 cmp 只能进 unit0、除法单实例） | ? |

**参考作答（以 `vadd.vv` 为例）**：

1. Chisel 枚举：`RvvAluOp.VADD`（仅名字，无实现）。
2. SV 单元：ALU（`rvv_backend_alu`），可双发射，无 cmp 限制。
3. 子单元：addsub（`rvv_backend_alu_unit_addsub`），`opcode = ADDSUB_VADD`，逐字节 9 位加法。
4. 延迟：两拍（P0 组合加法 → P1 `execution_p1` 终结）。
5. 上报：P1 出 `result_valid_p1`，顶层在 ROB `result_ready` 时交还结果并 `pop_rs`。
6. 结构冒险：`vadd` 无特殊约束；若换成 `vmseq`（cmp）则受「每周期最多一条 cmp」限制。

完成后再换 `vdivu.vv` 自行作答（提示：走 DIV → `rvv_backend_div_unit` → `intdivider`，可变延迟，`div_ready` 反压）。

## 6. 本讲小结

- RVV 后端的算术类 PU 有明确分工：ALU（`NUM_ALU=2`）管加减/移位/逻辑/比较，DIV（`NUM_DIV=1`）管整数除法，FALU/FDIV（`NUM_FMA=2/NUM_FDIV=1`，受 `ZVE32F_ON` 门控）管浮点。
- ALU 单元内部由 **addsub / shift / mask / other** 四个一拍并行子单元 + 一拍 `execution_p1` 终结级构成；四子单元用 `result_valid` 互斥选通。
- cmp 指令只能进 0 号 ALU 单元（`CMP_SUPPORT=1`），是结构冒险点；ALU 顶层用 `result_ready` 驱动的 case 做双发射仲裁。
- 整数除法是**可变延迟**：恢复余数迭代 + 前导零跳过，通过 `div_ready=state==DIV_IDLE` 反压；除零、有符号溢出有专门处理。
- 浮点算术**包装开源 fpnew/cvfpu**，CoralNPU 只写「μop↔fpnew」胶水；浮点除/开方同样是可变延迟。
- **Chisel 侧 `RvvAlu` 只用枚举列出操作码、用 `RvvS1DecodedInstruction` 做译码中间表示，不实现任何算术**；全部执行电路在 SystemVerilog，经 `RvvCore` 的 `addResource` 黑盒挂载。

## 7. 下一步学习建议

- **u7-l4（MAC 外积乘累加引擎）**：本讲讲的是「标量化的向量算术」，而 ML 加速的灵魂是矩阵乘——下一站看 `rvv_backend_mulmac` 如何用外积广播实现每周期上百 MACs。
- **回到 u7-l2/u7-l3**：本讲多次提到「派发端结构冒险」「结果回灌 ROB」，若对 RS→PU→ROB 的握手还不熟，建议重读这两讲。
- **u9-l2（RVVI 指令追踪）**：想验证本讲对延迟/冒险的推断，可结合 RVVI 追踪接口在仿真里观察一条 `vdiv` 与一条 `vadd` 的完成顺序。
- **延伸阅读**：浮点路径可对照 u5-l4（标量核 FPU），看标量 `FloatCore` 与向量 `falu/fdiv` 同样都包装 cvfpu/fpnew 的异同。
