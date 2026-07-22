# VRF 寄存器堆与 VRAT 别名表

## 1. 本讲目标

在 u2-l3 里我们已经看到：vRRM 用一个滑动自由列表给每条向量指令分配**物理寄存器号**，并把这些映射写进一张「别名表」。但那张表内部长什么样？分配出来的物理寄存器号最终落在哪一块真实存储上？这块存储又是怎么被读、被写的？本讲就往下看这一层：

- 看懂 **VRF（向量寄存器堆）** 的三维存储组织与多套读写端口。
- 理解为什么 VRF 同时提供「元素级」与「整寄存器级」两种访问粒度，以及写冲突时的优先级。
- 搞清 **mask（掩码）** 是怎么从某个寄存器元素的最低位（LSB）提取出来的。
- 看懂 **VRAT（寄存器别名表）** 如何把架构寄存器号翻译成物理寄存器号，以及它的复位与重配（reconfigure）语义。

学完后，你应该能把「vRRM 分配物理寄存器 → VRAT 记录映射 → VRF 存取数据」这条链路在脑子里完整跑一遍。

## 2. 前置知识

- **架构寄存器 vs 物理寄存器**：程序里写的是架构寄存器（如 `v1`、`v2`，共 32 个）；硬件内部实际存储用的是物理寄存器。两者之间靠一张映射表翻译。这正是 u2-l3 讲的「寄存器重映射」要解决的对象。
- **寄存器重映射（register remapping）**：本项目的重映射不是乱序处理器那种「每条指令都重命名」，而是在一次配置周期内，给每个**架构目的寄存器**分配一块不重叠的物理寄存器，从而让软件循环的不同迭代写不同的物理块——这就是硬件循环展开（HW unrolling）能并行的前提。详见 u2-l3。
- **三维 packed 数组**：SystemVerilog 里 `logic [A-1:0][B-1:0][C-1:0] mem;` 声明一个 `A×B×C` 的紧凑数组，`mem[i][j][k]` 取最左维第 `i`、中间维第 `j`、最右维第 `k`。
- **one-hot（独热）编码**：把一个二进制地址 `a` 展开成「只有第 `a` 位为 1」的位向量，常用 `1 << a`。好处是可以用按位与直接选通某一行存储。
- **复位（reset）vs 重配（reconfigure）**：复位是上电/全局复位；重配是软件发出的一条特殊向量指令，用来「开启新一轮重命名周期」。本讲会看到它们对 VRAT/VRF 的行为**并不相同**，这是一个容易踩坑的点。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [rtl/vector/vrf.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrf.sv) | 向量寄存器堆本体：三维存储 + 元素级/整寄存器级端口 + mask 提取 |
| [rtl/vector/vrat.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrat.sv) | 寄存器别名表：架构号→物理号的映射 + remapped 标志 + 复位/重配逻辑 |
| [rtl/vector/vis.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv) | **例化 VRF 的地方**（计分板 + VRF 都在 vis 里），驱动 VRF 的各端口 |
| [rtl/vector/vrrm.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv) | **例化 VRAT 的地方**，把自由列表分配结果写入 VRAT |
| [rtl/shared/params.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv) | 提供 `VECTOR_REGISTERS`、`VECTOR_LANES`、`DATA_WIDTH` 等规模参数 |

一个关键的空间关系：**VRAT 住在 vRRM（重命名级）里，VRF 住在 vIS（发射级）里**。它们之间隔了一级流水线。vRRM 算出物理号、写进 VRAT，并把物理号塞进指令随流水往下传；vIS 拿着这个物理号去访问 VRF。所以「映射表」和「真实存储」是物理分离的。

## 4. 核心概念与源码讲解

### 4.1 寄存器堆组织与端口（VRF）

#### 4.1.1 概念说明

VRF 是向量数据通路里**真正存放数据**的地方。每一条向量指令的源操作数从这里读，结果写回这里。它有两个看似复杂、实则自然的设计：

1. **三维存储**：一个向量寄存器不是「一串位」，而是被切成 `ELEMENTS` 个**元素**，每个元素 `DATA_WIDTH` 位。这样一条 ALU 通路（一个 lane）正好吃一个元素，`ELEMENTS` 个 lane 一拍并行处理一个寄存器。
2. **两套访问粒度**：
   - **元素级（element-level）**：ALU 每拍产出 `ELEMENTS` 个独立结果，要按 lane 各自的写使能写回——所以是「每元素一根写使能」。
   - **整寄存器级（whole-register-level）**：访存单元（vMU）做一次 load，一次性搬回一整个寄存器（所有 lane），需要一条宽总线、一个地址搞定——所以是「整寄存器宽读写」。

#### 4.1.2 核心流程

VRF 的存储与端口可以这样理解（以 vis 实际例化的规模为准：`VREGS=32`、`ELEMENTS=8`、`DATA_WIDTH=32`）：

```text
存储： memory[32 个寄存器][8 个元素/lane][32 位]
            ↑              ↑             ↑
         物理寄存器号      每 lane 一个    元素位宽

读：
  元素级  rd_addr_1/2  → data_out_1/2[8][32]   (给 ALU 的两个源)
  掩码    mask_src     → mask[8]               (每 lane 取 1 位, 见 4.2)
  整寄存器 v_rd_addr_0/1/2 → v_data_out_0/1/2[256]  (给 vMU 取数)

写：
  元素级  el_wr_en[8] + el_wr_addr + el_wr_data[8][32]   (来自执行回写)
  整寄存器 v_wr_en[8] + v_wr_addr + v_wr_data[256]      (来自 load 回写)
  冲突时：整寄存器写 v_wr 优先于 元素写 el_wr
```

写回时的优先级是本模块的一个重点：当 **同一拍、同一物理寄存器、同一 lane** 同时被 `v_wr`（load 回写）和 `el_wr`（执行回写）命中时，代码用 `if ... else if` 让 `v_wr` 先赢。直觉解释：load 搬回来的是「权威的新数据」，执行通路如果同拍也写同一个位置，应当让位给 load。

#### 4.1.3 源码精读

**模块参数与端口**：注意默认参数 `ELEMENTS=4`，但 vis 用 `VECTOR_LANES` 覆盖它（见下方例化）。

模块声明与三维存储声明：[rtl/vector/vrf.sv:7-11](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrf.sv#L7-L11) 定义了 `VREGS/ELEMENTS/DATA_WIDTH` 三个规模参数；[rtl/vector/vrf.sv:39-39](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrf.sv#L39-L39) 声明核心存储：

```systemverilog
logic [VREGS-1:0][ELEMENTS-1:0][DATA_WIDTH-1:0] memory;
```

按模块默认值是 \(\text{memory}[32][4][32]\)；当 vis 用 `VECTOR_LANES=8` 例化时实际是 \(\text{memory}[32][8][32]\)（32 个物理寄存器 × 8 lane × 32 位，即每个寄存器 256 位）。这也呼应了 u1-l3 提到的「改 lane 数会牵动 VRF 端口位宽」。

**写逻辑与优先级**：地址先转成 one-hot，再用双层 `for` 遍历「每个 lane k × 每个寄存器 i」，用 `if/else if` 实现「整寄存器写优先」。

[rtl/vector/vrf.sv:42-44](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrf.sv#L42-L44) 把写地址转 one-hot：

```systemverilog
assign v_wr_addr_oh = (1 << v_wr_addr);   // 整寄存器写地址
assign wr_addr_oh   = (1 << el_wr_addr);  // 元素写地址
```

[rtl/vector/vrf.sv:46-64](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrf.sv#L46-L64) 是核心写回（节选）：

```systemverilog
for (int k = 0; k < ELEMENTS; k++) begin
    for (int i = 0; i < VREGS; i++) begin
        if (v_wr_addr_oh[i] && v_wr_en[k])            // 整寄存器写：优先
            memory[i][k] <= v_wr_data[k*DATA_WIDTH +: DATA_WIDTH];
        else if (wr_addr_oh[i] && el_wr_en[k])        // 元素写：次之
            memory[i][k] <= el_wr_data[k];
    end
end
```

注意 `v_wr_en[k]` 和 `el_wr_en[k]` 都是「每 lane 一位」的写使能，所以即便整寄存器写也是按 lane 独立使能的（load 可能只带回部分 lane 的有效数据）。

**读端口**：[rtl/vector/vrf.sv:66-75](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrf.sv#L66-L75) 用组合逻辑直接索引输出——三个整寄存器读（`v_data_out_0/1/2`，宽 `ELEMENTS*DATA_WIDTH`）+ 两个元素读（`data_out_1/2`）+ 一个 mask 读（`mask`，见 4.2）：

```systemverilog
assign v_data_out_0 = memory[v_rd_addr_0];   // 整寄存器读：一拍给出 256 位
...
data_out_1[i] = memory[rd_addr_1][i];        // 元素读：按 lane 取
mask[i]       = memory[mask_src][i][0];      // 掩码读：取每元素 LSB（4.2 详述）
```

**VRF 的 `reset` 接的是「重配」而非全局复位**——这是例化时一个很容易看漏的细节。在 [rtl/vector/vis.sv:366-395](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L366-L395) 的例化里：

```systemverilog
vrf #(.VREGS(VECTOR_REGISTERS), .ELEMENTS(VECTOR_LANES), .DATA_WIDTH(DATA_WIDTH)) vrf (
    .reset   (do_reconfigure),      // ← 重配时清空 VRF，不是全局 rst_n
    .el_wr_en(wr_en_masked),        // 元素写 = 执行回写（经计分板 mask 过）
    .v_wr_en (mem_wr_en),           // 整寄存器写 = load 回写
    ...
);
```

也就是说，VRF 的存储在**重配时**被整体清零（对应 vrf.sv 里 `if(reset) memory[i][k] <= 'h0`），从而配合「新一轮重命名周期」从干净状态开始。这一点和 VRAT 的行为要对照着记（见 4.3）。

> 补充：`wr_en_masked` 是 vis 用计分板的 `locked` 位把「尚未被访存解锁」的写回屏蔽掉的结果（[rtl/vector/vis.sv:360-364](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L360-L364)），属于 u2-l5 计分板的范畴，本讲只需知道它是「执行回写的有效写使能」即可。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认 VRF「整寄存器写优先于元素写」在真实例化下对应哪两条数据通路。
2. **步骤**：
   - 打开 [rtl/vector/vis.sv:380-394](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L380-L394)，找到 `.el_wr_en(wr_en_masked)` 与 `.v_wr_en(mem_wr_en)`。
   - 在 vis.sv 里反向搜索 `mem_wr_en`、`mem_wr_addr`、`mem_wr_data` 的驱动来源，确认它们来自 vMU（访存）侧；再搜索 `wr_en`、`wr_data`，确认它们来自 vEX（执行）侧的回写接口。
   - 在 vis.sv 的计分板更新段（[rtl/vector/vis.sv:305-309](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L305-L309)）可以看到两类写都会清除 `pending` 位，印证它们是两路并行的写回。
3. **需要观察的现象**：两类写端口分别服务「执行」与「访存」两条数据通路，且共用同一个 VRF。
4. **预期结果**：你会得出结论——`el_wr_*` ⇄ vEX 回写（元素级），`v_wr_*` ⇄ vMU load 回写（整寄存器级，但按 lane 使能）。写冲突时 load 优先，因为 load 数据更「新」、更权威。
5. 运行结果：待本地验证（需要综合/仿真环境才能观察真实写冲突拍）。

#### 4.1.5 小练习与答案

- **练习 1**：如果把 `ELEMENTS` 从 8 改成 16（即 16 lane），VRF 的 `memory` 总位数变成多少？端口 `v_data_out_0` 的位宽变成多少？
  - **答案**：\(\text{memory}[32][16][32] = 16384\) 位；`v_data_out_0 = ELEMENTS*DATA_WIDTH = 16*32 = 512` 位。这也印证了 u1-l3 提到的「加 lane 会迅速膨胀端口位宽」。
- **练习 2**：写回优先级那段为什么用 `if … else if` 而不是两个独立 `if`？
  - **答案**：两个独立 `if` 在同拍同位置同时命中时会变成竞争（综合成不确定或多驱动）；`if/else if` 明确规定「整寄存器写命中时，元素写被屏蔽」，给出确定的优先级。

### 4.2 mask 提取（VRF 的掩码读口）

#### 4.2.1 概念说明

RISC-V 向量指令支持「掩码（masked）」操作：只对某些 lane 做运算、另一些 lane 保持不变。本项目用一个很紧凑的约定——**用架构寄存器 `v1` 当掩码寄存器**，并且只用每个元素的**最低位（LSB）**当该 lane 的掩码位。于是：

\[
\text{mask}[i] = \text{memory}[\text{mask\_src}][i][0],\quad i\in[0,\text{ELEMENTS})
\]

即「读出 `mask_src` 指向的物理寄存器，取第 \(i\) 个元素的第 0 位，组成一个 `ELEMENTS` 位宽的掩码向量」。`mask_src` 不是随便给的——它来自 VRAT：**v1 当前映射到的那个物理寄存器号**（见 4.3.3）。

#### 4.2.2 核心流程

掩码从「架构 v1」到「逐 lane 门控」要经过三个模块：

```text
架构 v1 ──VRAT──▶ v1 的物理寄存器号 (ratMem[1])
                       │ (随指令传到 vis, 再按展开轮次偏移)
                       ▼
                     mask_src ──VRF──▶ mask[8] = 每元素 LSB
                       │
                       ▼
            vis 按 use_mask 决定: 用 mask / 用 ~mask / 不掩码
                       │
                       ▼
            data_to_exec[k].mask → 门控每个 lane 的写回
```

`use_mask` 的取值（在 vis 里解码，详见 u2-l6）大致是：`2'b10` → 用 `mask[k]`；`2'b11` → 用 `~mask[k]`（取反掩码）；其它 → 不掩码（全 1）。归约指令有特例，此处先不展开。

#### 4.2.3 源码精读

**VRF 取 LSB**：[rtl/vector/vrf.sv:69-75](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrf.sv#L69-L75) 的组合读里，这一行就是掩码提取的全部：

```systemverilog
mask[i] = memory[mask_src][i][0];   // 取第 i 个元素的 bit0
```

`memory[mask_src][i]` 是一个 `DATA_WIDTH` 位的元素，`[0]` 取其最低位。8 个 lane 各取一位，拼成 `mask[ELEMENTS]`。

**mask_src 的来源 1（VRAT 侧）**：[rtl/vector/vrat.sv:72-72](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrat.sv#L72-L72) 永远输出 1 号表项（即架构 v1）的内容：

```systemverilog
assign mask_src = ratMem['d1];   // v1 当前映射到的物理寄存器号
```

**mask_src 的来源 2（随指令流动 + 展开偏移）**：vRRM 把上面的物理号塞进指令（[rtl/vector/vrrm.sv:172-172](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L172-L172) 的 `.mask_src(instr_out.mask_src)`），传到 vis 后再加一个展开轮次偏移：[rtl/vector/vis.sv:171-171](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L171-L171)

```systemverilog
assign mask_src = instr_in.mask_src + current_exp_loop;
```

`current_exp_loop` 是硬件循环展开的当前轮次（u2-l6 详述）。当一条指令的 VL 超过一个寄存器能装下的元素数、需要展开成多个 micro-op 时，每一轮读 v1 物理块里的「下一个」寄存器。

**mask 的消费（vis 侧）**：[rtl/vector/vis.sv:221-226](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L221-L226) 把 `mask[k]` 组合进每个 lane 的执行控制：

```systemverilog
assign data_to_exec[k].mask = ... 
    (instr_in.use_mask == 2'b10) ? mask[k]  : // 用 v1 元素 LSB 作掩码
    (instr_in.use_mask == 2'b11) ? ~mask[k] : // 用取反掩码
                                   1'b1;      // 不掩码
```

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：解释 mask 如何从寄存器元素的 LSB 提取，并追完整条「架构 v1 → 逐 lane mask」的链路。
2. **步骤**：
   - 在 vrat.sv 找到 `assign mask_src = ratMem['d1]`，确认它读的是**表项 1**（架构 v1）。
   - 在 vrrm.sv 例化处确认 `mask_src` 被接进 `instr_out.mask_src`，即随指令下传。
   - 在 vis.sv 找到 `assign mask_src = instr_in.mask_src + current_exp_loop`，理解「展开偏移」。
   - 在 vrf.sv 找到 `mask[i] = memory[mask_src][i][0]`，确认取的是元素 `[0]` 位。
   - 在 vis.sv 找到 `data_to_exec[k].mask`，确认 mask 最终门控写回。
3. **需要观察的现象**：掩码位「逐 lane 独立」，且来源是 v1 每个元素的同一比特位（bit0）。
4. **预期结果**：你能讲清楚——**架构寄存器 v1 的每个元素的最低位，分别成为 lane 0…LANES-1 的掩码开关**；lane i 的掩码位 = v1 第 i 个元素的 bit0。
5. 运行结果：待本地验证（可在仿真里给 v1 灌入已知值，观察被掩码指令的写回 lane）。

#### 4.2.5 小练习与答案

- **练习 1**：为什么掩码只取每个元素的 bit0，而不是用整个元素？
  - **答案**：一位就足以表达「该 lane 使能/不使能」；用整元素会浪费存储与布线。把掩码「挤进」v1 各元素的 LSB 是一种紧凑编码，让 v1 既能当普通数据寄存器、又能当掩码源。
- **练习 2**：如果一条被掩码的指令同时又是多寄存器展开（VL 很大），`mask_src` 为什么要加 `current_exp_loop`？
  - **答案**：展开后每一轮 micro-op 操作的是 v1 物理块里**不同**的物理寄存器（vRRM 给 v1 分配了一整块连续物理寄存器，见 u2-l3 的 `vreg_hop`），所以每轮要读「块内第 current_exp_loop 个」物理寄存器才能拿到对应那一段元素的掩码。

### 4.3 别名表与复位（VRAT）

#### 4.3.1 概念说明

VRAT（Vector Register Aliasing Table）是一张「架构号 → 物理号」的查找表：输入架构寄存器号，输出它当前映射到的物理寄存器号。它还维护一个 1 位的 `remapped` 标志，标记「这个架构寄存器在当前配置周期内是否已经被分配过物理寄存器」——这个标志直接决定 vRRM 这次要不要给目的寄存器分配新物理块（`do_remap = do_operation & ~rdst_remapped`）。

VRAT 有两种「回到起点」的行为，且**它们不一样**，这是本模块的重点：

- **复位（`~rst_n`，上电/全局复位）**：建立**恒等映射**——架构 \(i\) 映射到物理 \(i\)，并把所有 `remapped` 标志置 1。
- **重配（`reconfigure`，软件下发）**：把整张表**清零**，并把所有 `remapped` 标志清 0——从而强制下一个配置周期里每个架构寄存器都重新从自由列表分配。

> ⚠️ 注意：任务的实践描述里写的是「reconfigure 时复位为恒等映射」，但**真实代码并非如此**——恒等映射发生在**复位**时；**重配**是把表清零。下面 4.3.3 会逐行给出依据，请以源码为准。

#### 4.3.2 核心流程

VRAT 的存储与读写：

```text
ratMem[32][5]   : 每项存「架构号 i → 物理号」(5 位, 因为 32 个寄存器)
remapped[32]    : 每项 1 位, 标记该架构号是否已分配

写： write_en 时, ratMem[write_addr] <= write_data; remapped[write_addr] <= 1
复位(~rst_n): ratMem[i] <= i (恒等);  remapped <= 全 1
重配(reconfigure): ratMem <= 全 0;     remapped <= 全 0

读： 3 个读口, 各输出 read_data + remapped 标志
特殊：mask_src = ratMem[1] (恒定读 v1 的映射, 给 4.2 用)
```

#### 4.3.3 源码精读

**模块规模**：[rtl/vector/vrat.sv:7-10](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrat.sv#L7-L10) 默认 `TOTAL_ENTRIES=32`、`DATA_WIDTH=4`；在 vRRM 里用 `TOTAL_ENTRIES=VECTOR_REGISTERS=32`、`DATA_WIDTH=REGISTER_BITS=$clog2(32)=5` 例化（[rtl/vector/vrrm.sv:148-151](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L148-L151)）。

**存储声明**：[rtl/vector/vrat.sv:35-36](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrat.sv#L35-L36)

```systemverilog
logic [TOTAL_ENTRIES-1:0][DATA_WIDTH-1:0] ratMem;   // 映射表本体
logic [TOTAL_ENTRIES-1:0]               remapped;   // 每项「是否已分配」标志
```

**映射表写逻辑（含复位=恒等、重配=清零）**：[rtl/vector/vrat.sv:39-51](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrat.sv#L39-L51)

```systemverilog
if(~rst_n) begin
    for (int i = 0; i < TOTAL_ENTRIES; i++) ratMem[i] <= i;   // 复位：恒等映射 i→i
end else begin
    if (reconfigure) begin
        ratMem <= 'b0;                                        // 重配：整表清零（非恒等!）
    end else if(write_en) begin
        ratMem[write_addr] <= write_data;                     // 正常写入分配结果
    end
end
```

**remapped 标志写逻辑**：[rtl/vector/vrat.sv:53-63](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrat.sv#L53-L63)

```systemverilog
if(~rst_n)            remapped <= 'b1;                       // 复位：全部标记「已映射」
else if(reconfigure)  remapped <= 'b0;                       // 重配：全部清除
else if(write_en)     remapped[write_addr] <= 1'b1;           // 分配后置 1
```

把这两段对照看，就能画出 VRAT 的两个「回起点」分支：

```text
           ┌─ ~rst_n (上电复位) ──▶ ratMem[i]=i (恒等),  remapped=全1
VRAT 状态 ──┤
           └─ reconfigure (软件) ─▶ ratMem=全0 (清零),  remapped=全0
                       │
                       └─ 之后每条写目的寄存器的指令:
                          remapped[dst]==0 ⇒ do_remap=1 ⇒ 分配 next_free_vreg
                          并写 ratMem[dst]<=next_free_vreg, remapped[dst]<=1
```

**为什么复位用恒等、重配用清零？** 复位时还没有任何「自由列表分配」发生过，用恒等映射让程序在「第一个配置周期」直接用架构号当物理号（`remapped=全1` 使 `do_remap=0`，不再分配）。而重配的目的是**强制重新分配**以支持新一轮硬件循环展开，所以要把表和标志都清干净，让每个架构寄存器在下一轮重新走自由列表。

**读口与 mask_src**：[rtl/vector/vrat.sv:65-72](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrat.sv#L65-L72) 提供三个读口（vRRM 分别用来查目的、源 1、源 2），每个读口同时给出映射值和 `remapped` 标志；另有恒定读 v1 的 `mask_src`：

```systemverilog
assign read_data_1 = ratMem[read_addr_1];
assign remapped_1  = remapped[read_addr_1];
...
assign mask_src    = ratMem['d1];   // 恒定输出 v1 的物理号, 供 4.2 掩码链路使用
```

**VRAT 的驱动者（vRRM 侧）**：在 [rtl/vector/vrrm.sv:148-173](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L148-L173) 的例化里可以看到：

```systemverilog
vrat #(.TOTAL_ENTRIES(VECTOR_REGISTERS), .DATA_WIDTH(REGISTER_BITS)) vrat (
    .reconfigure(do_reconfigure),
    .write_addr (instr_in.dst),        // 按架构目的寄存器号写
    .write_data (next_free_vreg),      // 写入自由列表分配出的物理号
    .write_en   (do_remap),            // 仅当需要分配时才写
    .read_addr_1(instr_in.dst),   .read_data_1(rdst_destination), .remapped_1(rdst_remapped),
    .read_addr_2(instr_in.src1),  .read_data_2(remapped_src1),
    .read_addr_3(instr_in.src2),  .read_data_3(remapped_src2),
    .mask_src   (instr_out.mask_src)
);
```

`rdst_remapped` 又被 [rtl/vector/vrrm.sv:111-111](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L111-L111) 用来决定是否分配：`do_remap = do_operation & ~rdst_remapped`；而最终目的物理号在 [rtl/vector/vrrm.sv:82-84](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L82-L84) 三选一（已映射读旧值 / 新分配 / 透传架构号）：

```systemverilog
assign instr_out.dst = rdst_remapped ? rdst_destination :   // 已分配: 复用旧物理号
                       do_remap      ? next_free_vreg   :   // 本拍新分配
                                        instr_in.dst;       // 复位恒等期: 直接用架构号
```

这条三元选择正好对应 VRAT 三种状态（已映射 / 待分配 / 恒等），把 4.3 的表语义和 u2-l3 的自由列表分配衔接起来。

#### 4.3.4 代码实践（源码阅读 + 画图）

1. **目标**：画出 VRAT 在**复位**与**重配**两种情况下的行为差异（注意：重配不是恒等映射，而是清零）。
2. **步骤**：
   - 打开 [rtl/vector/vrat.sv:39-63](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrat.sv#L39-L63)，把 `mem` 与 `remap` 两个 `always_ff` 并排看。
   - 在纸上画两个状态框：
     - **复位 `~rst_n=0`**：`ratMem[i]=i`（恒等），`remapped=32'b1`。
     - **重配 `reconfigure=1`**：`ratMem=32'b0`（清零），`remapped=32'b0`。
   - 再画一条「重配之后」的转移：第一条写 `v3` 的指令到来 ⇒ `remapped[3]==0` ⇒ `do_remap=1` ⇒ `ratMem[3]<=next_free_vreg(=0)`，`remapped[3]<=1`；下一条写 `v3` 的指令 ⇒ `remapped[3]==1` ⇒ 复用 `ratMem[3]`。
3. **需要观察的现象**：复位与重配对 `ratMem`/`remapped` 的赋值方向相反（一个建恒等并标记全映射、一个清零并标记全未映射）。
4. **预期结果**：你能向别人讲清「为什么重配要清零而不是恢复恒等」——因为重配的意义是开启新一轮自由列表分配，必须让所有架构寄存器重新变成「未分配」；而恒等映射只用于上电后还没开始重命名的初始状态。
5. 运行结果：待本地验证（可在仿真里发一条 `reconfigure` 指令，观察 `ratMem`/`remapped` 与 VRF 是否同时被清）。

#### 4.3.5 小练习与答案

- **练习 1**：复位后第一条写 `v5` 的指令，`instr_out.dst` 最终取哪个值？为什么？
  - **答案**：取 `instr_in.dst`（即架构号 5 本身）。因为复位使 `remapped[5]=1` ⇒ `rdst_remapped=1` ⇒ 走第一分支 `rdst_destination=ratMem[5]=5`（恒等），等价于直接用架构号。此时不触发自由列表分配。
- **练习 2**：重配后，`ratMem` 被清成全 0，那读 `v2`（源寄存器）会不会读到错误的物理号 0？
  - **答案**：在「重配刚发生、`v2` 还没被重新写」的窗口里，`ratMem[2]==0` 确实会被源读取读到。实践中这要求软件在重配后、用某架构寄存器作源之前，先写它（或程序保证源寄存器已在新周期内被生产）。本讲只指出这一表语义；完整的源使用约束属于 vRRM/程序约定范畴。
- **练习 3**：`remapped` 标志为什么不和 `ratMem` 合并成「特殊值表示未映射」？
  - **答案**：分开存更清晰、综合更友好：`ratMem` 是纯数据表（可按 SRAM 实现），`remapped` 是独立的 1 位/项 标志阵列。合并会让「物理号 0」与「未映射」歧义（恰好重配就把表清成全 0），所以独立标志更稳妥。

## 5. 综合实践

把本讲三个模块串起来，做一次「带掩码的 vadd」端到端追踪（源码阅读型）：

1. **情景**：程序里有一条 `vadd v3, v4, v5`（目的 v3，源 v4/v5），`use_mask=2'b10`（用 v1 作掩码）。重配刚发生过一次。
2. **任务**：在源码里逐步标出——
   - vRRM 如何查 VRAT 得到 v4/v5 的物理号（[rtl/vector/vrrm.sv:163-170](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L163-L170)），如何因为 `remapped[3]==0` 而给 v3 分配新物理号 `next_free_vreg`（[rtl/vector/vrrm.sv:156-158](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L156-L158)）。
   - VRAT 如何同时把 v1 的物理号通过 `mask_src` 送出（[rtl/vector/vrat.sv:72-72](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrat.sv#L72-L72)）。
   - 指令到了 vis 后，VRF 如何用 `rd_addr_1/2` 读出 v4/v5 物理寄存器的元素给 ALU（[rtl/vector/vrf.sv:71-72](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrf.sv#L71-L72)），又如何用 `mask_src` 读 v1 物理寄存器并取 LSB 得到 `mask`（[rtl/vector/vrf.sv:73-73](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrf.sv#L73-L73)）。
   - ALU 算完后，结果如何经 `el_wr_*` 写回 v3 的物理寄存器（[rtl/vector/vis.sv:381-383](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L381-L383) → [rtl/vector/vrf.sv:58-60](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrf.sv#L58-L60)），而 `mask` 如何经 `data_to_exec[k].mask` 门控每个 lane 是否真的写回（[rtl/vector/vis.sv:224-224](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L224-L224)）。
3. **产出**：一张时序/数据流图，包含「vRRM→VRAT→（指令下传）→vis→VRF」四个节点，标出每段上流动的是架构号还是物理号，以及掩码从哪里分叉、在哪里合流。
4. **思考题**：如果在这条 vadd 执行的同一拍，vMU 的一次 load 也正好要写同一个物理寄存器的同一个 lane，谁赢？为什么？（答：load 赢，因为 `v_wr` 优先于 `el_wr`，见 4.1.3。）

## 6. 本讲小结

- VRF 是 `memory[VREGS][ELEMENTS][DATA_WIDTH]` 的三维 packed 数组；按声明默认是 \([32][4][32]\)，vis 用 `VECTOR_LANES=8` 例化后实际是 \([32][8][32]\)。
- VRF 同时提供**元素级**（`el_wr_*`、`rd_addr_*`，服务 ALU）和**整寄存器级**（`v_wr_*`、`v_rd_addr_*`，服务 vMU load）两套端口；写冲突时**整寄存器写优先**（`if/else if`）。
- VRF 的 `reset` 端口在 vis 里接的是 `do_reconfigure`——**重配时整块清零**，不是全局复位。
- 掩码来自**架构寄存器 v1**：VRAT 经 `mask_src=ratMem['d1]` 给出 v1 的物理号，VRF 用 `mask[i]=memory[mask_src][i][0]` 取每个元素的 LSB，形成逐 lane 掩码；vis 再按 `use_mask` 决定用/取反/不用。
- VRAT 是「架构号→物理号」表 + 每项 1 位 `remapped` 标志；`remapped` 决定 vRRM 是否分配新物理块（`do_remap=~rdst_remapped`）。
- **复位 = 恒等映射 + 标志全 1；重配 = 整表清零 + 标志全 0**——两者方向相反，这是本讲最容易记错的地方，请以源码为准。

## 7. 下一步学习建议

- **下一讲 u2-l5（vIS 计分板与冒险检测）**：VRF 住在 vis 里，而 vis 围绕 VRF 还有一张「逐元素 pending/locked」的计分板。本讲看到的 `wr_en_masked`、`pending`、`locked` 都属于计分板，下一讲会完整讲解它们如何控制 VRF 的写回与读转发。
- **u2-l6（硬件循环展开与掩码）**：本讲的 `current_exp_loop`、`use_mask`、归约掩码特例都会在那里展开。
- **回头巩固 u2-l3（vRRM）**：本讲的 VRAT 是 vRRM 例化的，建议把 vRRM 的自由列表（`next_free_vreg`、`vreg_hop`）与本讲的 VRAT 写口对照阅读，确认「分配→记录映射」的闭环。
- **延伸阅读**：若对「重命名 + 计分板」的通用原理感兴趣，可对比经典乱序处理器的 Register Alias Table（RAT）与 Reorder Buffer（ROB）模型；本项目用的是更轻量的「按配置周期重映射」方案，区别值得体会。
