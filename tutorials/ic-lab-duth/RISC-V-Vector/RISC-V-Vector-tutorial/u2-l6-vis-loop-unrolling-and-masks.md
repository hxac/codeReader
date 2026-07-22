# vIS 硬件循环展开与掩码

## 1. 本讲目标

本讲聚焦 `vis.sv`（向量发射级）里的两件事：**一条向量指令如何被硬件展开成多个 micro-op**，以及**每个元素是否写回如何被掩码门控**。

学完后你应该能够：

- 说清 `current_exp_loop` 这个计数器如何把一条 VL 元素的指令切成一串 micro-op，并能算出 micro-op 的数量与每个 micro-op 的 `dst/src1/src2` 偏移。
- 解释 `max_expansion = maxvl >> $clog2(LANES)` 这条公式的由来，以及 `vl_reached` / `maxvl_reached` 两个终止条件的区别。
- 理解归约（reduction）指令为何要给目的指针做 `+1` 偏移、最后一个 micro-op 又为何指回基址寄存器。
- 看懂 `v1[mask_src]` 的逐元素 LSB 如何变成 `data_to_exec[k].mask`，进而门控执行级的逐元素写回。

本讲严格承接 u2-l5（计分板与冒险）、u2-l4（VRF/VRAT 与掩码链路）、u2-l3（vRRM 物理寄存器块分配）与 u1-l4（结构体定义），不再重复这些前置内容。

## 2. 前置知识

- **micro-op（微操作）**：执行级 vEX 每拍只能并行处理 `VECTOR_LANES` 个元素（默认 8）。当一条向量指令的元素数 VL 大于 lane 数时，它必须被拆成若干个「每拍处理一组 lane」的微操作。本讲里「展开」指的就是这个拆分过程，由硬件自动完成，不需要标量核发循环——这正是 u1-l3 里 `USE_HW_UNROLL` 开关的含义。
- **物理寄存器块**：u2-l3 讲过，vRRM 会为每个架构目的寄存器分配一个连续的物理寄存器块，块内步长 `vreg_hop = maxvl/LANES`。本讲里你会看到 vis 如何用 `current_exp_loop` 在这个块里逐组 stride。
- **计分板**：u2-l5 讲过 `pending`/`locked` 位矩阵决定一条指令何时能发射。本讲关心的是「一条指令被切成多个 micro-op 后，这些 micro-op 如何轮流通过同一套计分板」。
- **VRF 掩码读出**：u2-l4 讲过用架构寄存器 v1 当掩码源，VRF 用 `memory[mask_src][k][0]`（元素 k 的最低位）形成逐 lane 掩码 `mask[k]`。本讲会把这组 `mask[k]` 接到执行级的写回门控上。

## 3. 本讲源码地图

本讲几乎全部内容都在同一个文件里：

| 文件 | 作用 |
| --- | --- |
| [rtl/vector/vis.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv) | 向量发射级。本讲覆盖其中的展开计数器、终止判定、目的指针生成与掩码门控。 |

辅助理解（不展开讲，给出交叉引用）：

| 文件 | 与本讲的关系 |
| --- | --- |
| [rtl/vector/vex.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv) | 归约树住在这里，消费 vis 送来的 `head_uop/end_uop`。详见 u2-l8。 |
| [rtl/vector/vstructs.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv) | `to_vector_exec_info` 里的 `head_uop/end_uop/vl` 字段，是 vis↔vEX 的 micro-op 契约。 |
| [rtl/vector/vmacros.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv) | `RDC_ADD/RDC_AND/...` 归约操作码、`INT_FU=2'b10` 功能单元编码。 |

---

## 4. 核心概念与源码讲解

### 4.1 硬件循环展开：一条指令切成多个 micro-op

#### 4.1.1 概念说明

向量执行级 vEX 是一个「宽 ALU」：它有 `VECTOR_LANES` 条并行 lane，每拍每条 lane 处理一个元素。但一条向量指令要处理的元素数 VL 是任意的（由程序给定，上限为 `maxvl`）。当 \( \text{VL} > \text{LANES} \) 时，一条指令不可能一拍做完。

软件做法是让标量核发一个循环，每次处理一组 lane。RISC-V² 的做法是**硬件循环展开**：标量核只发一条向量指令，vis 自己用一个计数器 `current_exp_loop` 把它展开成一串 micro-op，**每个 micro-op 处理一组（LANES 个）元素**，每拍发一个（前提是冒险已解除）。这样标量核的指令条数与 VL 无关，控制开销被压到最低。

展开出来的 micro-op 数量是：

\[
\text{uop 数} = \left\lceil \frac{\text{VL}}{\text{LANES}} \right\rceil
\]

每个 micro-op 的 `dst/src1/src2` 物理寄存器号，等于「架构基址 + `current_exp_loop`」——也就是说每往后一个 micro-op，三类寄存器都整体向后移一个物理寄存器，正好踩在 vRRM 预先分配好的连续物理块上（u2-l3 的 `vreg_hop`）。

#### 4.1.2 核心流程

```text
指令到达, current_exp_loop = 0
   │
   ▼
┌─ 循环：每个 micro-op 一拍 ──────────────────────┐
│  1. 算 total_remaining = vl - exp_loop*LANES     │
│  2. 算 vl_therm（本拍哪些 lane 有效）            │
│  3. 查计分板 → can_issue（冒险解除?）            │
│  4. 算 expansion_finished（本拍是最后一个?）     │
│  5. 若可发射：送出 dst/src = 基址 + exp_loop     │
│     head_uop = (exp_loop==0)                     │
│     end_uop  = expansion_finished                │
│  6. exp_loop <= exp_loop + 1                     │
│  7. 若 expansion_finished：ready_o 拉高 → pop    │
│     → exp_loop 复位为 0，准备下一条指令          │
└──────────────────────────────────────────────────┘
```

两个终止条件需要区分清楚：

- **`vl_reached`**：已经覆盖了运行时实际的 VL 个元素。\((\text{exp\_loop}+1)\times\text{LANES} \ge \text{VL}\)。
- **`maxvl_reached`**：已经达到这条指令声明的最大长度 maxvl 对应的 micro-op 上限。\(\text{exp\_loop} = \text{max\_expansion}-1\)。

其中 `max_expansion` 就是本讲主题里那句 `maxvl >> $clog2(LANES)`：

\[
\text{max\_expansion} = \text{maxvl} \gg \lceil\log_2\text{LANES}\rceil = \frac{\text{maxvl}}{\text{LANES}}
\]

它的含义是「maxvl 个元素，每组 LANES 个，最多能切出多少组 micro-op」。两者只要有一个先到，本拍就是最后一个 micro-op（`expansion_finished = maxvl_reached | vl_reached`）。

#### 4.1.3 源码精读

`current_exp_loop` 是整个展开机制的「主指针」，在复位、重配或指令弹出时清零，每成功发射一个 micro-op 自增 1：

[rtl/vector/vis.sv:L138-L149](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L138-L149) —— 展开计数器 `current_exp_loop`：`do_reconfigure | pop` 时清零，`do_issue` 时 +1。

`max_expansion` 只在复位/重配时锁存一次（maxvl 在一条指令的生命期内不变）：

[rtl/vector/vis.sv:L151-L158](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L151-L158) —— 把 `instr_in.maxvl` 右移 `$clog2(LANES)` 位，得到「最多展开多少个 micro-op」。

终止判定与剩余元素：

[rtl/vector/vis.sv:L119-L122](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L119-L122) —— `total_remaining_elements`、`expansion_finished`、`maxvl_reached`、`vl_reached` 四个信号。注意 `vl_reached` 用 `(current_exp_loop+1) << $clog2(LANES)` 与 `vl` 比较，等价于 \((\text{exp\_loop}+1)\times\text{LANES}\)。

`vl_therm` 是「本拍有效 lane」的温度计掩码，用来处理尾部不满一组的情况：

[rtl/vector/vis.sv:L126](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L126) —— `vl_therm = ~('1 << total_remaining_elements)`。当剩余元素 ≥ LANES 时 `'1<<n` 把所有位都移出去、结果为 0、取反得全 1（所有 lane 有效）；剩余元素不足时，低 n 位为 1。

`do_issue` / `pop` / `ready_o` 把展开和握手绑在一起：

[rtl/vector/vis.sv:L128-L136](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L128-L136) —— 非存储指令的 `do_issue` 需要 `valid_in & output_ready & can_issue`；`ready_o` 在 `expansion_finished`（且冒险解除、下游就绪）时才拉高，使上游弹出本指令。

micro-op 的边界标记与 vl 传递，构成 vis↔vEX 的契约：

[rtl/vector/vis.sv:L161-L169](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L161-L169) —— `head_uop = start_new_instr`（第一个 micro-op）、`end_uop = expansion_finished`（最后一个 micro-op）；`vl` 在第一个 micro-op 传完整 `instr_in.vl`，之后的 micro-op 传 `total_remaining_elements`，供 vEX 做尾部处理。

普通指令（非归约）的目的/源指针最直观——就是「基址 + 展开下标」：

[rtl/vector/vis.sv:L199-L203](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L199-L203) —— `dst/src_1/src_2 = instr_in.dst/src1/src2 + current_exp_loop`，每往后一组就整体跨一个物理寄存器。

#### 4.1.4 代码实践

**实践目标**：亲手把「VL=20、VECTOR_LANES=8」的一条普通（非归约）向量指令展开，验证 micro-op 数量、每组的寄存器偏移和尾部 lane 掩码。

**操作步骤**：

1. 打开 [rtl/vector/vis.sv:L119-L126](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L119-L126)，确认 `total_remaining_elements = 20 - exp_loop*8` 与 `vl_reached = ((exp_loop+1)*8 >= 20)`。
2. 逐拍代入 `exp_loop = 0,1,2,...`，记录每拍的 `total_remaining_elements`、`vl_reached`、`expansion_finished`、`vl_therm`。
3. 用 [rtl/vector/vis.sv:L199-L203](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L199-L203) 的公式写出每组的 `dst/src1/src2` 偏移（设架构基址分别为 `D/S1/S2`）。

**需要观察的现象 / 预期结果**：

| micro-op | exp_loop | 剩余元素 | vl_reached? | expansion_finished? | 有效 lane（vl_therm） | dst / src1 / src2 偏移 |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | 0 | 20 | 否（8<20） | 否 | 8’b1111_1111（全 8 lane） | D+0 / S1+0 / S2+0 |
| 1 | 1 | 12 | 否（16<20） | 否 | 8’b1111_1111（全 8 lane） | D+1 / S1+1 / S2+1 |
| 2 | 2 | 4 | 是（24≥20） | **是（末组）** | 8’b0000_1111（仅 lane 0–3） | D+2 / S1+2 / S2+2 |

micro-op 总数 \( = \lceil 20/8 \rceil = 3 \)。第 0、1 组各处理 8 个元素，第 2 组只处理 lane 0–3 共 4 个元素，合计 8+8+4 = 20。第 2 组由于 `expansion_finished` 为真，`ready_o` 才可能拉高、把指令弹出。

> 待本地验证：上表是基于源码公式的推演。若在 QuestaSim 里跑，可在 `vis` 内部用波形观察 `current_exp_loop`、`expansion_finished`、`vl_therm` 三个信号，确认它们按上表节拍变化。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `VECTOR_LANES` 改成 16，同一条 VL=20 的指令会展开成几个 micro-op？末组处理几个 lane？

**参考答案**：\( \lceil 20/16 \rceil = 2 \) 个 micro-op。第 0 组处理 lane 0–15（16 个），第 1 组 `total_remaining = 20-16 = 4`，`vl_therm = ~('1<<4) = 16'h000F`，只处理 lane 0–3（4 个）。

**练习 2**：`max_expansion` 为什么用 `maxvl >> $clog2(LANES)` 而不是直接 `maxvl / LANES`？

**参考答案**：二者在 LANES 为 2 的幂时完全等价（本设计 LANES 恒为 2 的幂，如 8/16）。用右移是为了让综合工具直接推断成一组合逻辑移位，而不是除法器，面积/时序更友好。`$clog2(LANES)` 正是「把元素数折算成组数」所需的移位量。

---

### 4.2 归约指令的目标偏移特例

#### 4.2.1 概念说明

归约（reduction）指令把多个元素归约成一个标量，典型例子是点积、求和。它的特殊性在于：**最终结果只需要写到基址寄存器的 0 号元素**（一个标量），而不是像普通指令那样铺满一整组 lane。

但归约指令仍然要按 lane 分组展开成多个 micro-op。如果照搬 4.1 的「`dst = 基址 + exp_loop`」，那么最后一个 micro-op 的 `dst` 会指向「基址 + (组数−1)」——也就是最后分配的那个物理寄存器，结果就会写错地方。

解决办法是源码注释里那句「**把所有目的指针 +1，唯独最后一个 micro-op 指回基址**」。这样既让计分板「覆盖到归约触及的整段寄存器范围」（正确跟踪冒险），又让最终标量结果落在基址寄存器的 0 号元素。再配合 4.3 的掩码（只有末组的 0 号元素真正写回），归约的写回语义就完整了。

#### 4.2.2 核心流程

```text
归约指令到达
   │
   ▼
判定 instr_is_rdc = (fu==INT_FU) & (microop[6:5]==2'b10)
   │
   ▼  对每个 micro-op 选目的指针：
   ├─ 既是首组又是末组（VL<LANES，单 micro-op）: dst = 基址 + exp_loop
   ├─ 末组（多组展开的最后一拍）:              dst = 基址 + 0   ← 指回基址
   ├─ 首组（多组展开的第一拍）:                 dst = 基址 + exp_loop + 1
   └─ 中间组:                                  dst = 基址 + exp_loop + 1
```

举例：一个 3-micro-op 的归约（基址为 `D`）：

| micro-op | exp_loop | 角色 | dst 指向 |
| --- | --- | --- | --- |
| 0 | 0 | 首组 | D+1 |
| 1 | 1 | 中间组 | D+2 |
| 2 | 2 | **末组** | **D+0（基址）** |

注意中间组虽然指向 D+1、D+2，但 4.3 会看到它们的写回掩码被强制清零（不真正写 RF）；只有末组的 0 号元素写回 D[0]。真正跨组的累加发生在 vEX 的归约树里（用 `head_uop` 复位累加器、`end_uop` 输出最终值），详见 u2-l8。vis 的职责只是**把指针和掩码摆对**。

#### 4.2.3 源码精读

归约指令的判定：

[rtl/vector/vis.sv:L173](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L173) —— `instr_is_rdc = (instr_in.fu == 2'b10) & (instr_in.microop[6:5] == 7'b10)`。`fu==2'b10` 即 `INT_FU`（见 vmacros.sv），`microop[6:5]==2'b10` 用操作码最高两位区分归约类。

> 说明：当前公开的 `v_int_op_t` 枚举（vstructs.sv）里没有任何操作码的最高两位是 `10`（枚举最大到 `VSLTU=7'b0011110`，最高两位都是 `00`）。也就是说，归约类操作码占用的是 `microop[6:5]==2'b10` 这段保留编码空间（对应 vmacros.sv 的 `RDC_ADD/RDC_AND/...`）。本设计里归约是真实功能（vEX 里有完整的归约树，见 u2-l8），只是其操作码不在当前公开枚举中。**待确认**：归约操作码的确切 7 位编码需对照未公开的译码器/生成器。

目的指针的四分支（含详细注释）：

[rtl/vector/vis.sv:L175-L204](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L175-L204) —— 归约的目的指针 trick：除末组外一律 `+1`，末组指回基址。注释原话："we offset all the destination by +1, except the last uop which will point to the base register, covering that way the whole range, while storing the result in the correct location in the RF"。

`head_uop` / `end_uop` 正是给 vEX 归约树用的边界信号：

[rtl/vector/vis.sv:L166-L167](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L166-L167) —— `head_uop = start_new_instr`（首组）、`end_uop = expansion_finished`（末组）。vEX 据此复位/输出归约累加器（交叉引用 [rtl/vector/vex.sv:L162-L163](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L162-L163) 把它们打拍传递）。

#### 4.2.4 代码实践

**实践目标**：用源码的四分支公式，手算一个 3-micro-op 归约的指针，并解释为何中间组「指向 D+1/D+2 却不写回」。

**操作步骤**：

1. 假设一条归约指令，VL=20、LANES=8，基址 `dst/src1/src2 = D/S1/S2`，且它满足 `instr_is_rdc`。
2. 对 `exp_loop = 0,1,2`，分别落入 [rtl/vector/vis.sv:L181-L198](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L181-L198) 的哪个分支，写出 `dst/src_1/src_2`。
3. 再查 [rtl/vector/vis.sv:L222-L223](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L222-L223) 的掩码（见 4.3），确认哪些组真正写回。

**预期结果**：

| micro-op | exp_loop | 分支 | dst | 写回掩码 |
| --- | --- | --- | --- | --- |
| 0 | 0 | 首组（`start_new_instr`） | D+1 | 全 0（中间组不写回） |
| 1 | 1 | 中间组（else） | D+2 | 全 0 |
| 2 | 2 | 末组（`expansion_finished`） | D+0 | 仅 lane 0 = 1 |

结论：中间组虽然在计分板里把 D+1、D+2 标记为 pending（覆盖整段范围，防止后续指令提前读），但因为掩码为 0，**不会真正写 RF**；最终标量结果只由末组写到 D[0]。跨组的部分和由 vEX 归约树在内部累加完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么末组要指回基址 D，而不是也 +1 指到 D+3？

**参考答案**：因为归约的最终结果必须落在架构基址寄存器的 0 号元素。如果末组也 +1，结果会写到 D+3 的 0 号元素，与架构约定不符。中间组 +1 只是为了「占位」让计分板覆盖整段范围，真正承载结果的是末组的基址。

**练习 2**：`instr_is_rdc` 同时检查 `fu` 和 `microop` 两位，缺一不可。如果只看 `microop[6:5]==2'b10` 会有什么风险？

**参考答案**：操作码最高两位为 `10` 的编码空间理论上可能被其他功能单元（如 FP/FXP）的指令复用。加上 `fu==INT_FU` 才能确保只有「整数归约」走这条特例路径，避免把别的指令误判成归约、错误地改写目的指针和掩码。

---

### 4.3 掩码门控写回：v1[mask_src] 如何逐元素控制写回

#### 4.3.1 概念说明

向量指令常带「条件写回」：只有满足条件的元素才更新目的寄存器，其余元素保持原值（类似 ARM SVE 的谓词、RISC-V 向量的掩码）。RISC-V² 用架构寄存器 **v1** 当掩码源：v1 的每个元素的最低位（LSB）当作对应 lane 的「写/不写」条件。

这一节有两层掩码，不要混淆：

1. **`vl_therm`（尾部掩码）**：4.1 已讲，处理末组不满一组 lane 的情况，由 VL 决定，与程序语义无关。
2. **`data_to_exec[k].mask`（谓词掩码）**：本节重点，由程序的 `use_mask` 字段和 v1 的 LSB 决定，是真正的「条件写回」。它随数据一起送到 vEX，vEX 据此逐元素决定是否写 RF。

#### 4.3.2 核心流程

```text
本拍 micro-op
   │
   ├─ mask_src = instr_in.mask_src + current_exp_loop   ← 掩码寄存器也跟着展开下标走
   │
   ▼
VRF 读出 mask[k] = memory[mask_src][k][0]  ← 每元素 LSB（详见 u2-l4）
   │
   ▼
vis 计算每个 lane 的 data_to_exec[k].mask：
   ├─ 归约 & 末组            → (k==0)        只有 0 号元素写
   ├─ 归约 & 非末组          → 0             中间组全不写
   ├─ use_mask == 2'b10      → mask[k]       用 v1 的 LSB
   ├─ use_mask == 2'b11      → ~mask[k]      用 v1 LSB 取反
   └─ 其余                   → 1             不掩码（全写）
   │
   ▼
vEX 拿到 mask，逐元素门控写回 VRF
```

`mask_src = instr_in.mask_src + current_exp_loop` 是个关键细节：掩码寄存器号也随展开下标递增，这样每个 micro-op 都能读到「自己那组」对应的谓词位——掩码寄存器组与数据寄存器组同步 stride。

#### 4.3.3 源码精读

掩码源指针随展开下标递增：

[rtl/vector/vis.sv:L171](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L171) —— `mask_src = instr_in.mask_src + current_exp_loop`。`instr_in.mask_src` 由 VRAT 给出 v1 对应的物理寄存器号（u2-l4），这里再叠加展开偏移。

每个 lane 的掩码计算（这是谓词写回的核心）：

[rtl/vector/vis.sv:L222-L226](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L222-L226) —— `data_to_exec[k].mask` 的三元运算：归约末组只放行 `k==0`；归约中间组全 0；`use_mask==2'b10` 用 `mask[k]`，`2'b11` 用 `~mask[k]`；否则恒 1。这段嵌在 [rtl/vector/vis.sv:L206-L228](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L206-L228) 的 `generate` 循环里，和数据选择 `data1/data2` 一起组装成 `to_vector_exec`。

VRF 实例化时把 `mask_src`/`mask` 接上：

[rtl/vector/vis.sv:L378-L379](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L378-L379) —— vis 把 `mask_src` 喂给 VRF，VRF 回送每元素 LSB 形成的 `mask[]`（u2-l4 讲过的 `memory[mask_src][k][0]`）。

> ⚠️ **一处需要本地验证的细节**：`remapped_v_instr` 结构体里 `use_mask` 当前声明为**单 bit**（见 [rtl/vector/vstructs.sv:L54](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L54) `logic use_mask;`），而 vis 用它和 2 位字面量比较（`== 2'b10`、`== 2'b11`）。SystemVerilog 里单 bit 左侧会被零扩展成 2 位（`00` 或 `01`），永远不可能等于 `2'b10` 或 `2'b11`，于是 `use_mask` 那两个分支**在当前宽度下恒不命中**，非归约指令会一直走最后的 `1'b1`（不掩码）。代码注释与 u1-l4/u2-l4 的描述都表明设计意图是 2 位编码（10 用 / 11 取反 / 其余不掩码）。**这要么是一个待修的位宽不一致，要么 `use_mask` 会在译码器侧被扩展为 2 位**——请在本地用波形确认 `use_mask` 的实际位宽与取值，不要默认谓词掩码已生效。

#### 4.3.4 代码实践

**实践目标**：理解谓词掩码如何随 `use_mask` 取值变化，并定位上面提到的位宽疑点。

**操作步骤（源码阅读型）**：

1. 在 [rtl/vector/vstructs.sv:L54](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L54) 确认 `use_mask` 的声明宽度。
2. 在 [rtl/vector/vis.sv:L224-L225](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L224-L225) 确认比较字面量是 2 位。
3. 假设把 `use_mask` 改成 2 位（`logic [1:0] use_mask;`），重新推演三种取值下 lane k 的 `data_to_exec[k].mask`。

**预期结果（假设 `use_mask` 为 2 位、`mask[k]=1`）**：

| use_mask | 语义 | data_to_exec[k].mask |
| --- | --- | --- |
| 2'b00 或 2'b01 | 不掩码 | 1（写） |
| 2'b10 | 用 v1 LSB | mask[k] = 1（写） |
| 2'b11 | 用 v1 LSB 取反 | ~mask[k] = 0（不写） |

**需要观察的现象**：在当前（单 bit）宽度下，上表后两行实际不会发生，所有非归约元素都会被标记为「写」。若要验证谓词功能，需先把 `use_mask` 扩展为 2 位并在生成器侧给出对应编码——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `mask_src` 要加 `current_exp_loop`，而不能像 `dst` 那样在归约时再加 `+1`？

**参考答案**：掩码源始终是 v1（架构寄存器），它的物理块也由 vRRM 按 `vreg_hop` 分配。每个 micro-op 处理一组 lane，对应的谓词位也位于 v1 物理块里的「下一组」，所以 `mask_src` 必须随 `current_exp_loop` 同步 stride，才能读到正确的谓词位。归约的 `+1` 是目的指针的专用 trick，与掩码源无关。

**练习 2**：`vl_therm`（4.1）和 `data_to_exec[k].mask`（本节）都是「逐 lane 掩码」，它们的作用层次有何不同？

**参考答案**：`vl_therm` 在**发射级**决定哪些 lane 参与本 micro-op（尾部不满、归约范围控制），影响 `valid_output` 和计分板；`data_to_exec[k].mask` 是**程序语义级**的谓词，随数据送到 vEX，决定计算结果是否写回 RF。前者是「这组有没有这些元素」，后者是「这个元素要不要写」。

---

## 5. 综合实践

把展开、归约、掩码三件事串起来，做一次完整的「纸面跟踪」加「波形核验」。

**任务**：给定一条 VL=20、VECTOR_LANES=8 的指令，分两种情形完成下表，再到仿真里验证。

**情形 A —— 普通指令（如 vadd，`use_mask` 取不掩码）**：

| micro-op | exp_loop | dst 偏移 | 有效 lane | head_uop | end_uop |
| --- | --- | --- | --- | --- | --- |
| 0 | 0 | D+0 | 0–7 | 1 | 0 |
| 1 | 1 | D+1 | 0–7 | 0 | 0 |
| 2 | 2 | D+2 | 0–3 | 0 | 1 |

**情形 B —— 归约指令（`instr_is_rdc` 为真）**：

| micro-op | exp_loop | dst 偏移 | 写回 lane | head_uop | end_uop |
| --- | --- | --- | --- | --- | --- |
| 0 | 0 | D+1 | 无（掩码全 0） | 1 | 0 |
| 1 | 1 | D+2 | 无（掩码全 0） | 0 | 0 |
| 2 | 2 | D+0 | 仅 lane 0 | 0 | 1 |

**操作步骤**：

1. 按上表对照 [rtl/vector/vis.sv:L175-L204](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L175-L204)（指针）与 [rtl/vector/vis.sv:L222-L226](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L222-L226)（掩码），确认每一格。
2. 按 u1-l5 的流程跑一次仿真，在波形里盯住 `vis/current_exp_loop`、`vis/expansion_finished`、`vis/start_new_instr`、`info_to_exec.head_uop`、`info_to_exec.end_uop` 五个信号，确认它们按上表节拍跳变。
3. 把 `VECTOR_LANES`（[rtl/shared/params.sv:L87](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv#L87)）从 8 改成 16（**别忘了同步** [rtl/vector/vstructs.sv:L9](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L9) 的 `DUMMY_VECTOR_LANES`，否则结构体位宽会与实际 lane 数不符——这是 u1-l3 反复强调的隐藏耦合），重算情形 A 的 micro-op 数与末组 lane，再用波形核验。

**预期结果**：情形 A 在 LANES=8 下为 3 个 micro-op、末组 4 lane；改成 LANES=16 后为 2 个 micro-op、末组 4 lane。`head_uop` 只在 exp_loop=0 那拍为 1，`end_uop` 只在末组那拍为 1。> 待本地验证：归约路径因当前操作码枚举不含 `microop[6:5]==2'b10` 的编码，需自行构造一条满足 `instr_is_rdc` 的指令才能在波形里观察情形 B。

## 6. 本讲小结

- vis 用计数器 `current_exp_loop` 把一条 VL 元素的指令**硬件展开**成若干 micro-op，每拍发一组（LANES 个元素）；micro-op 数 \(=\lceil \text{VL}/\text{LANES}\rceil\)，上限为 `max_expansion = maxvl >> $clog2(LANES)`。
- 终止由 `vl_reached`（覆盖完 VL）和 `maxvl_reached`（达到 maxvl 上限）二选一触发，二者相或成 `expansion_finished`；尾部不满一组时用 `vl_therm` 屏蔽无效 lane。
- 每个 micro-op 的寄存器指针 = 架构基址 + `current_exp_loop`；普通指令三类寄存器同步 stride，正好踩在 vRRM 分配的连续物理块上。
- **归约特例**：目的指针除末组外一律 `+1`，末组指回基址，使计分板覆盖整段范围；真正写回只有末组的 0 号元素（掩码门控），跨组累加在 vEX 归约树完成。
- **谓词掩码**：`mask_src = instr_in.mask_src + current_exp_loop` 让 v1 的物理块与数据同步 stride，VRF 读出每元素 LSB，vis 按 `use_mask`（10 用 / 11 取反 / 其余不掩码）生成 `data_to_exec[k].mask` 送 vEX 门控写回。
- **一处待本地验证的坑**：`use_mask` 当前声明为单 bit，而代码按 2 位比较，谓词分支在当前宽度下可能不生效——改 lane 数或启用谓词前务必核验位宽。

## 7. 下一步学习建议

- **接着读 u2-l7（vEX 与 vex_pipe 执行流水）**：看 `head_uop/end_uop` 和 `data_to_exec[k].mask` 如何被执行级消费、`mask` 如何真正门控写回 VRF。
- **读 u2-l8（整数 ALU 与跨 lane 归约树）**：本讲里归约的「跨组累加」黑盒在那里打开——vex.sv 的 `rdc_data_ex*_i/o` 互连（lane N ← lane N+1/+2/+4/+8）就是归约树的实物。
- **回看 u2-l3（vRRM）**：对照 `vreg_hop = maxvl/LANES`，理解 vis 的「+exp_loop stride」为何能正确命中连续物理块。
- **想做实验**：按综合实践把 `VECTOR_LANES` 改成 16 跑仿真，观察 micro-op 数与 stall 计数的变化，为 u4-l7（性能调优）做铺垫。
