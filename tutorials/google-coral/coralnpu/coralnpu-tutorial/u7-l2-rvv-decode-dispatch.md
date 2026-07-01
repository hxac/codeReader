# RVV 译码与派发

## 1. 本讲目标

在 u7-l1 里我们看到：RVV 后端是一个「被标量核驱动的协处理器」，标量核把每条向量指令打包成一个 `RVVCmd` 投进命令队列，然后就转头继续取指。那么，**拿到 `RVVCmd` 之后，RVV 后端自己是如何把它变成可在各执行单元上跑的操作、又如何在有数据依赖时安全地把它们发射出去的？** 这就是本讲要回答的两个问题——**译码（Decode）** 与 **派发（Dispatch）**。学完后你应当能够：

- 说清一条向量指令如何被 **stripmining（条带挖掘）** 展开成一串 **uop（微操作）**，以及 `first_uop_valid` / `last_uop_valid` 的作用；
- 在 `rvv_backend_decode_unit_ari_de2.sv` 里指出 `uop_index_base`、`uop_valid`、`uop_exe_unit`、`uop_class` 这些关键字段是如何算出来的；
- 复述派发单元同时检查的三类冒险——**RAW（读后写）**、**结构冒险（VRF 读端口不够）**、**保留站/ROB 满**——以及对应的硬件子模块；
- 解释 `UOP_CLASS`（如 `VVV` / `XVV`）的三字母编码如何决定一条 uop 要从向量寄存器堆（VRF）读几个端口，从而决定结构冒险；
- 描述「ROB 旁路」如何在数据还在 ROB 里没回写时就把它转发给后继 uop。

本讲承接 u7-l1（RVV 后端总览与 Chisel 桥接），是 u7-l3（VRF/ROB/退休）与 u7-l4（MAC 引擎）的直接前置。

## 2. 前置知识

进入本讲前，请确认你已理解下列概念（在 u7-l1 与 u4 系列讲义中讲过）：

- **RVV 后端的两段式结构**：Chisel 侧 `RvvCore`/`RvvFrontEnd` 负责装配 `RVVCmd`，SystemVerilog 侧 `rvv_backend` 是一条「命令队列 → 译码 → uop 队列 → 派发 → 保留站 → 执行单元 → ROB → 退休」的乱序执行、按序退休流水线。本讲只盯其中 **译码** 和 **派发** 两段。
- **uop（微操作）**：向量寄存器很宽，一条向量指令往往要拆成若干个「每次处理 VLEN 位」的小操作，每个小操作就是一个 uop。uop 是 RVV 后端内部调度的基本单位。
- **LMUL / SEW / EMUL**：`LMUL`（向量长度倍数）决定一条指令占几个向量寄存器组；`SEW`（元素位宽）决定每个元素几位；`EMUL`（effective MUL，有效倍数）是考虑 widening/narrowing 后实际占用的寄存器组数。一条指令要拆成多少个 uop，由 `EMUL` 决定。
- **VRF（向量寄存器堆）**：RVV 后端的寄存器堆，当前 RTL 按 `VLEN=128` 构建（见 [rvv_backend_define.svh:129-131](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend_define.svh#L129-L131)）。
- **ROB（重排序缓冲）**：保存所有已派发但尚未退休的 uop 的状态（包括它们即将写回的数据）。派发单元要拿 ROB 的内容来做 RAW 检测与旁路。
- **ready-valid 握手**：所有模块间数据传递都用 `valid`/`ready`，`fire = valid && ready`。

一个关键直觉：**派发不是「能取到 uop 就发」**。同一周期里取到的两个 uop 可能彼此有依赖、可能一起把 VRF 的读端口撑爆、也可能目标保留站已满。派发单元的工作，就是在这些约束下**按序地**决定「这一拍到底放行几个 uop、分别去哪个执行单元」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/verilog/rvv/inc/rvv_backend_opcode.svh](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend_opcode.svh) | 向量指令的 **funct3 / funct6 操作码**定义（如 `VADD`、`VMUL`、`VMACC`），是译码识别指令身份的「字典」。 |
| [hdl/verilog/rvv/inc/rvv_backend_define.svh](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend_define.svh) | 流水线宽度宏：每周期译码几条指令、展开几个 uop、派发几个 uop、VRF 几个读端口等（`NUM_DE_INST`/`NUM_DE_UOP`/`NUM_DP_UOP`/`NUM_DP_VRF`）。 |
| [hdl/verilog/rvv/inc/rvv_backend.svh](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend.svh) | 关键数据结构：`RVVCmd`、`LCMD_t`、`UOP_QUEUE_t`、`UOP_CLASS_e`、`EXE_UNIT_e` 等。 |
| [hdl/verilog/rvv/design/rvv_backend_decode_unit_ari_de2.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari_de2.sv) | **算术类指令的 uop 生成器**：stripmining 展开、`uop_index` 步进、`first/last_uop_valid`、`uop_exe_unit`/`uop_class` 赋值。 |
| [hdl/verilog/rvv/design/rvv_backend_decode_ctrl.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_ctrl.sv) | 译码控制器：把两条指令各自展开的 uop **拼接**进 uop 队列，并维护跨周期的 `uop_index_remain`。 |
| [hdl/verilog/rvv/design/rvv_backend_dispatch.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch.sv) | **派发单元顶层**：例化各冒险检测/旁路/控制子模块，并把准备好的 uop 推向保留站与 ROB。 |
| [hdl/verilog/rvv/design/rvv_backend_dispatch_structure_hazard.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_structure_hazard.sv) | **结构冒险**：按 `uop_class` 分配 VRF 读端口，当两个 uop 合计需要的读端口超过物理端口数时拉高 `vr_limit`。 |
| [hdl/verilog/rvv/design/rvv_backend_dispatch_raw_uop_rob.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_raw_uop_rob.sv) | **RAW 冒险（对 ROB）**：把当前 uop 的源寄存器与 ROB 中所有未完成 uop 的目的寄存器比较。 |
| [hdl/verilog/rvv/design/rvv_backend_dispatch_bypass.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_bypass.sv) | **ROB 旁路**：逐字节决定 uop 的操作数取自 VRF 还是直接转发自 ROB。 |
| [hdl/verilog/rvv/design/rvv_backend_dispatch_ctrl.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_ctrl.sv) | **派发握手控制**：综合各类冒险与下游 ready，按序决定每个 uop 能否本拍发射、发往哪个执行单元。 |

---

## 4. 核心概念与源码讲解

### 4.1 操作码字典与流水线宽度：两个头文件

#### 4.1.1 概念说明

在动手读译码器之前，先认识两份「字典 + 参数表」。RVV 后端是纯 SystemVerilog，所有指令身份、流水线宽度都用宏/参数集中定义在头文件里，而不是散落在各模块。这样改一处配置（比如「每周期派发 3 个 uop」）就能整体变形。

- **`rvv_backend_opcode.svh`** 是指令字典：把 RVV 规范里的 `funct3`（指令大类）和 `funct6`（具体操作）编码成命名常量。
- **`rvv_backend_define.svh`** 是宽度参数表：用 `DISPATCH2` / `DISPATCH3` 两套宏切换「每周期处理几个 uop」。

#### 4.1.2 核心流程

`funct3` 区分**操作数来源**（向量-向量 / 向量-标量 / 向量-立即数），`funct6` 区分**具体运算**。以最典型的 `vadd` 与 `vmul` 为例：

| 指令 | funct3 | funct6 参数 | 含义 |
| --- | --- | --- | --- |
| `vadd.vv` / `vadd.vx` / `vadd.vi` | `OPIVV`/`OPIVX`/`OPIVI` | `VADD = 6'b000_000` | 向量加，走 ALU |
| `vmul.vv` / `vmul.vx` | `OPMVV`/`OPMVX` | `VMUL = 6'b100_101` | 向量乘，走 MUL |
| `vmacc.vv` | `OPMVV` | `VMACC = 6'b101_101` | 乘累加，走 MAC |

`funct3` 的定义见 [rvv_backend_opcode.svh:4-12](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend_opcode.svh#L4-L12)：

```systemverilog
parameter  OPIVV=3'b000;   // vs2, vs1, vd.      向量-向量（整数）
parameter  OPMVV=3'b010;   // vs2, vs1, vd/rd.   向量-向量（多用途）
parameter  OPIVI=3'b011;   // vs2, imm[4:0], vd. 向量-立即数
parameter  OPIVX=3'b100;   // vs2, rs1, vd.      向量-标量
parameter  OPMVX=3'b110;   // vs2, rs1, vd/rd.   向量-标量（多用途）
```

`VADD`、`VMUL`、`VMACC` 等 funct6 定义见 [rvv_backend_opcode.svh:16-112](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend_opcode.svh#L16-L112)。

> **关于「vdot」的说明**：本讲任务里举的 `vdot` 例子，在 RVV 1.0 规范与本仓库里**并没有一个叫 `VDOT` 的 funct6 参数**。文档里说的「VDOT / 外积乘累加」在硬件上是由 **MAC 类指令**（如 `VMACC`、`VWMACCU` 等 OPM\* 指令，见 [rvv_backend_opcode.svh:94-112](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend_opcode.svh#L94-L112)）驱动 `MAC` 执行单元实现的，我们会在 u7-l4 专门讲。本讲用 `vadd`（ALU 类）和 `vmul`/`vmacc`（MUL/MAC 类）作为追踪样例。

流水线宽度则由 `rvv_backend_define.svh` 的两套分支控制。默认（不定义 `DISPATCH3` 宏）走 **DISPATCH2** 配置（[rvv_backend_define.svh:32-55](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend_define.svh#L32-L55)）：

| 宏 | DISPATCH2（默认） | 含义 |
| --- | --- | --- |
| `NUM_DE_INST` | 2 | 每周期从命令队列译码 **2 条**指令 |
| `NUM_DE_UOP` | 4 | 每周期最多向 uop 队列写 **4 个** uop |
| `NUM_DP_UOP` | 2 | 每周期最多 **派发 2 个** uop |
| `NUM_DP_VRF` | 4 | VRF 有 **4 个读端口**供派发使用 |
| `EMUL_MAX` | 8 | 单条指令最多展开 **8 个** uop |

记住「派发每拍 2 个 uop、VRF 4 个读端口」这两个数，第 4.3 节的结构冒险就围绕它们展开。

#### 4.1.3 源码精读

`UOP_CLASS_e` 是派发阶段最关键的分类编码（[rvv_backend.svh:221-230](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend.svh#L221-L230)）：

```systemverilog
typedef enum logic [2:0] {
  XXX=0, XXV=1, XVX=2, XVV=3, VXX=4, VXV=5, VVX=6, VVV=7
} UOP_CLASS_e;
```

这三个字母描述这条 uop **要从 VRF 读几个操作数**。结合派发阶段 `rvv_backend_dispatch_operand.sv` 的 DISPATCH2 分支（[rvv_backend_dispatch_operand.sv:284-307](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_operand.sv#L284-L307)）逐个核对，三字母按 **(vd, vs2, vs1)** 从高到低排列，`V` 表示「这个操作数是向量寄存器、本拍必须从 VRF 读出来」，`X` 表示「不是向量读」（标量 `rs1`、立即数、或不是源）：

| `uop_class` | 需要的 VRF 读数 | 典型指令 |
| --- | --- | --- |
| `VVV` | 3（vd+vs2+vs1） | `vadc.vvm`（带进位加，vd 也是源）、`vmadc` |
| `XVV` | 2（vs2+vs1） | `vadd.vv`（vd 只作目的） |
| `VVX` | 2（vd+vs2） | `vmadd.vv`（乘累加，vd 作累加源） |
| `XVX` | 1（vs2） | `vadd.vx`（标量加） |
| `VXX` | 1（vd） | `vmerge` 的某些形态 |
| `XXV` | 1（vs1） | `vmv` 等 |
| `XXX` | 0 | 无向量源（如 `vid.v`） |

> 这张表的「V 的个数 = VRF 读端口数」是后面结构冒险的钥匙：两个 uop 同周期派发，若它们各自的 V 数之和 > 4，就读不过来，必须停一个。

执行单元种类定义在 `EXE_UNIT_e`（[rvv_backend.svh:157-176](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend.svh#L157-L176)）：`ALU / MUL / MAC / PMT / RDT / CMP / DIV / LSU`（开 `ZVE32F_ON` 时还有 `FMA`/`FCVT` 等）。译码阶段会把每条 uop 归到其中一个。

#### 4.1.4 代码实践

**实践目标**：熟悉操作码字典与宽度宏，建立「指令 → funct3/funct6 → 执行单元」的直觉。

**操作步骤**：

1. 打开 [rvv_backend_opcode.svh](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend_opcode.svh)，找到 `VADD`（第 16 行）、`VMUL`（第 91 行）、`VMACC`（第 96 行）三条参数。
2. 在 [rvv_backend_define.svh:32-55](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend_define.svh#L32-L55) 确认 DISPATCH2 配置下 `NUM_DE_UOP=4`、`NUM_DP_UOP=2`、`NUM_DP_VRF=4`。
3. 在 [rvv_backend.svh:221-230](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend.svh#L221-L230) 数一下 `UOP_CLASS_e` 有几个枚举值，并自测：`VVV` 含几个 `V`？

**需要观察的现象 / 预期结果**：`vadd.vv` 走 `OPIVV`+`VADD`、`vmul.vv` 走 `OPMVV`+`VMUL`；DISPATCH2 下每拍最多派发 2 个 uop、VRF 有 4 个读端口；`VVV` 含 3 个 V。结论可在源码中直接读出，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：`vadd.vi`（向量加立即数）的 `funct3` 是哪个？它的 `uop_class` 大概率是几？

**参考答案**：`funct3 = OPIVI`（`3'b011`）。它的 vs2 是向量、vd 是目的、第三操作数是 5 位立即数（不读 VRF），所以 `uop_class = XVX`（只读 vs2 一个向量）。

**练习 2**：若两个 uop 同周期派发，一个是 `VVV`、一个是 `VVV`，会发生什么？

**参考答案**：共需 3+3=6 个 VRF 读端口 > 4，触发结构冒险 `arch_hazard.vr_limit=1`，第二个 uop 本拍不能发射（详见 4.3）。

---

### 4.2 译码：把一条向量指令展开成一串 uop（stripmining）

#### 4.2.1 概念说明

向量寄存器有 VLEN 位，但一条向量指令在 `LMUL=8` 时要处理 8 组向量寄存器、共 `8×VLEN` 位。后端不可能一拍做完，于是采用 **stripmining（条带挖掘）**：把一条指令**折叠**成若干个「每次处理 VLEN 位」的 uop，串行发射。文档原话（[overview.md:63-72](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/overview.md#L63-L72)）：

> ... a single frontend dispatch event to the command queue into four serialized issue events into the SIMD units. For instance a "vadd v0" in Dispatch will produce "vadd v0 : vadd v1 : vadd v2 : vadd v3" at Issue.

也就是说，前端派发一次，后端展开成多次串行发射。具体展开成几个 uop，由指令的有效倍数 `EMUL` 决定（`EMUL1`→1 个、`EMUL2`→2 个……最多 `EMUL8`→8 个，对应 `UOP_NUM_ALU=8`，见 [rvv_backend_define.svh:118](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend_define.svh#L118)）。

每个 uop 身上带两个关键标记：

- **`first_uop_valid`**：这是这条指令展开出的**第一个** uop。标量操作数（如 `vadd.vx` 的 `rs1`）只在第一个 uop 上携带。
- **`last_uop_valid`**：这是**最后一个** uop。很多指令只有最后一个 uop 才真正写回目的（如归约 `vredsum`），或才更新掩码/标量结果。

#### 4.2.2 核心流程

RVV 后端的译码分两级（在顶层 [rvv_backend.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend.sv) 里串起来）：

```
RVVCmd(来自命令队列 CQ)
   │  DE1 级：rvv_backend_decode / rvv_backend_decode_unit
   ▼        按 opcode 分流(LOAD/STORE → lsu 译码器；RVV → ari 译码器)
   │        算出 EMUL/EEW、合法性，产出 LCMD_t(合法命令)
   ▼  Legal Command Queue (LCQ)
   │  DE2 级：rvv_backend_decode_de2 / rvv_backend_decode_unit_*_de2
   ▼        stripmining：按 uop_index 逐个展开 NUM_DE_UOP 个 uop
   │        赋 uop_exe_unit / uop_class / first_uop_valid / last_uop_valid
   ▼  rvv_backend_decode_ctrl：把 2 条指令的 uop 拼成 4 路写入
   ▼  Uop Queue (UQ)
```

DE1 负责「这条指令合法吗、要拆成几段、各段宽度多少」；DE2 负责「这一拍具体展开哪几个 uop」。两级之间隔着 LCQ，是因为 DE1 的展开结果（`uop_index_max` 等）要锁存一拍供 DE2 使用。

DE2 的 stripmining 用一个**游标 `uop_index`** 跟踪进度。每拍从 `uop_index_base` 开始，连续生成最多 `NUM_DE_UOP`（=4）个 uop，下标依次为 `base, base+1, base+2, base+3`；当一拍装不下整条指令时，把「下一个要生成的下标」存进 `uop_index_remain`，下拍接着来，直到生成出 `last_uop_valid` 的那个 uop 为止。

#### 4.2.3 源码精读

**顶层分流**。`rvv_backend_decode_unit.sv` 按 `opcode` 把指令交给 ari 或 lsu 译码器（[rvv_backend_decode_unit.sv:43-80](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_unit.sv#L43-L80)）：

```systemverilog
assign valid_lsu = inst_valid & ((inst.opcode==LOAD) | (inst.opcode==STORE));
assign valid_ari = inst_valid & (inst.opcode==RVV);
// ...分别例化 u_lsu_decode / u_ari_decode，再用 case(1'b1) 选出 lcmd
```

而外层 `rvv_backend_decode.sv` 只是用 `generate` 循环对 `NUM_DE_INST`（=2）条指令各例化一个 `decode_unit`（[rvv_backend_decode.sv:31-41](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode.sv#L31-L41)）。

**stripmining 游标**。真正的 uop 生成在 DE2 的算术译码器 `rvv_backend_decode_unit_ari_de2.sv`。先确定本拍起始下标 `uop_index_base`（[rvv_backend_decode_unit_ari_de2.sv:140-172](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari_de2.sv#L140-L172)）：优先用上一拍留下的 `uop_index_remain`，否则用 `uop_vstart`（来自 `vstart` CSR，支持异常恢复后从中途续跑）：

```systemverilog
uop_index_base = (|uop_index_remain) ? uop_index_remain : uop_vstart;
```

随后用 `generate` 循环算出本拍 4 个候选 uop 的下标 `uop_index_current[j] = base + j`（[rvv_backend_decode_unit_ari_de2.sv:175-179](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari_de2.sv#L175-L179)），再决定每个是否有效（[rvv_backend_decode_unit_ari_de2.sv:182-186](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari_de2.sv#L182-L186)）：

```systemverilog
uop_valid[i] = lcmd_valid & ({1'b1,uop_index_base} <= ({1'b1,uop_index_max}-i));
```

`uop_index_max` 是这条指令要展开的总段数减一（DE1 算好锁进 LCMD）。当 `base + i` 超过 `uop_index_max`，第 `i` 个 uop 就无效——这就是「装不下就停」的判断。

**首/末标记**。`first_uop_valid` 在「下标等于 `uop_vstart`」时拉高（[rvv_backend_decode_unit_ari_de2.sv:189-223](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari_de2.sv#L189-L223)）；`last_uop_valid` 在「下标等于 `uop_index_max`」时拉高（[rvv_backend_decode_unit_ari_de2.sv:226-230](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari_de2.sv#L226-L230)）：

```systemverilog
first_uop_valid[i] = uop_index_current[i][...] == uop_vstart;
last_uop_valid[i]  = uop_index_current[i][...] == uop_index_max;
```

**分配执行单元**。`uop_exe_unit` 由 `funct6` 决定（[rvv_backend_decode_unit_ari_de2.sv:232-480](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari_de2.sv#L232-L480)），默认是 `ALU`（第 235 行），遇到 `VMUL` 类置 `MUL`、`VMACC` 类置 `MAC`、`VDIV` 类置 `DIV`、归约类置 `RDT`、置换类置 `PMT` 等。`uop_class` 则按指令形态逐 case 赋值，并常随 `first_uop_valid` 切换（例如 `VIOTA` 在首 uop 是 `XVX`、其余是 `XXX`，见 [rvv_backend_decode_unit_ari_de2.sv:675-676](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari_de2.sv#L675-L676)）。

**装进 UOP_QUEUE_t**。最后把所有字段装配成输出 `uop[j]`（[rvv_backend_decode_unit_ari_de2.sv:2176-2222](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari_de2.sv#L2176-L2222)），其中 `uop_exe_unit`/`uop_class`/`uop_index`/`first_uop_valid`/`last_uop_valid`/`pshrob_valid` 都在这里落地。`pshrob_valid` 表示「这个 uop 要不要占一个 ROB 表项」——比较/归约类只在 `last_uop_valid` 那个 uop 才入 ROB（[rvv_backend_decode_unit_ari_de2.sv:2161-2174](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari_de2.sv#L2161-L2174)）：

```systemverilog
CMP, RDT: pshrob_valid[i] = last_uop_valid[i];
default:  pshrob_valid[i] = 1'b1;
```

**控制器拼接**。`rvv_backend_decode_ctrl.sv` 把「指令 0 的若干 uop」和「指令 1 的若干 uop」首尾相接，拼成本拍要写入 uop 队列的 `NUM_DE_UOP` 路（DISPATCH2 下即 4 路）。第 0 路恒取指令 0 的第 0 个 uop（[rvv_backend_decode_ctrl.sv:66-67](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_ctrl.sv#L66-L67)）；后续各路用 `casez` 按两条指令各自还有几个有效 uop 来选填（[rvv_backend_decode_ctrl.sv:76-192](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_ctrl.sv#L76-L192)）。它还负责算 `uop_index_remain`：本拍推走的所有 uop 里，最后一个若不是 `last_uop_valid`，就把「它的下标+1」存起来当下拍起点（[rvv_backend_decode_ctrl.sv:671-694](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_ctrl.sv#L671-L694)）；并据此决定何时 `pop` 命令队列（一条指令的最后一个 uop 被推走，才释放它占的命令队列槽，[rvv_backend_decode_ctrl.sv:656-668](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_ctrl.sv#L656-L668)）。

#### 4.2.4 代码实践

**实践目标**：亲手追踪一条 `vadd.vv`（LMUL=8）从指令到 uop 的展开过程。

**操作步骤**：

1. 假设 `vadd.vv` 配置为 `SEW=8`、`LMUL=8`、`VLEN=128`。先在纸上算：它要处理 `8×128/8 = 128` 个元素，每次 uop 处理 `128/8 = 16` 个元素，故共需 `128/16 = 8` 个 uop（`uop_index_max = 7`）。
2. 打开 [rvv_backend_decode_unit_ari_de2.sv:182-186](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari_de2.sv#L182-L186)，代入 `uop_index_base=0`、`uop_index_max=7`，验证本拍 `uop_valid[0..3]` 全为 1（因为 0,1,2,3 都 ≤ 7）。
3. 再代入「第二拍」`uop_index_base=4`，验证 `uop_valid[0..3]` 仍全为 1，且 `uop_index_current[3]=7` 命中 `uop_index_max`，故 `last_uop_valid[3]=1`（[rvv_backend_decode_unit_ari_de2.sv:226-230](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari_de2.sv#L226-L230)）。
4. 打开 [rvv_backend_decode_ctrl.sv:671-678](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_ctrl.sv#L671-L678)，确认第一拍推走的是下标 3 的 uop（非 `last_uop_valid`），所以 `uop_index_remain` 被更新为 `3+1=4`，正好是第二拍的起点。

**需要观察的现象 / 预期结果**：一条 LMUL=8 的 `vadd.vv` 在 DISPATCH2 配置下，需要 **2 拍**才能展开完全部 8 个 uop（4+4），第二拍的最后一个 uop 带 `last_uop_valid=1`。`vadd.vv` 的 `uop_class` 应为 `XVV`（只读 vs2、vs1）。

**待本地验证**：上述展开数依赖 `EMUL`/`uop_index_max` 的实际计算（在 DE1 的 `rvv_backend_decode_unit_ari.sv` 里由 `LMUL`/`SEW` 推导）。若你能跑 VCS/Verilator 仿真，可在波形里数一条 `vadd.vv` 真实产生的 `push_de2uq` 脉冲数与 `last_uop_valid` 位置来核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `vadd.vx` 的标量 `rs1` 只放在第一个 uop 上，而不是每个 uop 都带？

**参考答案**：标量 `rs1` 对这条指令的所有 uop 都是同一个值，重复携带浪费带宽。译码器只在 `first_uop_valid` 的 uop 上把 `rs1` 经 `rs1_data`/`rs1_data_valid` 送出（见 [rvv_backend_decode_unit_ari_de2.sv:2213-2214](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari_de2.sv#L2213-L2214)），后续 uop 的 `uop_class` 也相应从首 uop 的 `XVX` 变成 `XXX`（如 `VIOTA` 的处理，[rvv_backend_decode_unit_ari_de2.sv:675-676](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari_de2.sv#L675-L676)），省掉无谓的标量传递。

**练习 2**：`uop_index_remain` 的初值来自哪里？为什么需要它？

**参考答案**：初值来自 `uop_vstart`（`vstart` CSR，[rvv_backend_decode_unit_ari_de2.sv:142](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari_de2.sv#L142)）。需要它是因为一条 LMUL>1 的指令一拍展开不完，必须记住「下次接着从第几个 uop 开始」，才能在多拍间连续推进 stripmining；同时也让发生异常（`vstart≠0`）后能从中断点恢复。

---

### 4.3 派发：冒险检测、操作数准备与发射控制

#### 4.3.1 概念说明

uop 进了 uop 队列（UQ）之后，由**派发单元（Dispatch）**每拍取最多 `NUM_DP_UOP`（=2）个，检查通过后推向保留站（RS）与 ROB。派发要同时处理三类约束：

1. **RAW（Read After Write，读后写）数据冒险**：当前 uop 要读的源向量寄存器，正好是某个尚未完成的老 uop 要写的目的——必须等那个老 uop 把数据写出来。要查两个地方：
   - 与 **ROB 里所有未完成 uop** 的目的比（`raw_uop_rob`）；
   - 与 **本拍同发的更早 uop** 的目的比（`raw_uop_uop`，因为同拍取出的 uop 可能有依赖）。
2. **结构冒险（structural hazard）**：本拍两个 uop 合计需要的 VRF 读端口是否超过 4 个。超过则只能发第一个。
3. **下游满**：目标保留站或 ROB 满（`*_ready` 为 0），不能发。

派发还有一项「增值服务」——**ROB 旁路（bypass）**：若 RAW 命中的那个老 uop 已经算完、数据在 ROB 里（`w_valid=1`）但还没回写 VRF，派发单元就**直接从 ROB 把数据转发**给当前 uop，而不必停下来等回写。这正是模块头注释里说的两种解法：「a. stall pipeline；b. forward data from ROB」（[rvv_backend_dispatch.sv:1-7](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch.sv#L1-L7)）。

#### 4.3.2 核心流程

派发单元 `rvv_backend_dispatch` 是个「组装车间」，内部例化 7 个子模块，数据流如下（对应 [rvv_backend_dispatch.sv:146-498](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch.sv#L146-L498)）：

```
uop_uop2dp[0..1] (来自 UQ)
   │
   ├─► suc_uop              抽出每个 uop 的源/目寄存器索引与 valid
   │
   ├─► raw_uop_rob          vs ROB：哪些源被未完成老 uop 占着？(hit/wait)
   ├─► raw_uop_uop          vs 本拍更早 uop：同上
   ├─► structure_hazard     按 uop_class 分配 VRF 读端口 rd_index[0..3]；
   │                        端口不够 → arch_hazard.vr_limit
   │
   ├─► VRF 读 (rd_index_dp2vrf → rd_data_vrf2dp)
   ├─► operand              把 4 个读端口的数据按 uop_class 还原成 vs1/vs2/vd/v0
   ├─► bypass               逐字节：VRF 数据 vs ROB 转发数据（按 raw_uop_rob.hit）
   │
   ├─► ctrl                 综合所有 wait / vr_limit / 下游 ready：
   │                        按序决定 uop_ready_dp2uop、rs_valid_dp2*、uop_valid_dp2rob
   ▼
   RS(ALU/MUL/MAC/DIV/PMTRDT/LSU) + ROB
```

注意三个要点：**派发是按序的**（uop1 能不能发，前提是 uop0 也发了）；**VRF 读是组合完成的**（`rd_data_vrf2dp` 当拍返回，[rvv_backend_dispatch.sv:113-114](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch.sv#L113-L114)）；**旁路是逐字节的**（因为一条向量写可能只写部分字节，要精细到字节级决定取 VRF 还是取 ROB）。

#### 4.3.3 源码精读

**（1）RAW 检测：对 ROB**。`rvv_backend_dispatch_raw_uop_rob.sv` 把当前 uop（`suc_uop`）的 4 个源——`vs1_index`、`vs2_index`、`vd_index`（vd 作源时，即 `vs3_valid`）、以及掩码 `v0`——分别与 ROB 全表（`ROB_DEPTH=8` 项）的 `w_index` 比较（[rvv_backend_dispatch_raw_uop_rob.sv:43-49](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_raw_uop_rob.sv#L43-L49)）。命中要同时满足三条件（[rvv_backend_dispatch_raw_uop_rob.sv:56-62](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_raw_uop_rob.sv#L56-L62)）：

```
hit = (src_index == pre.w_index)    // a. 索引相等
      & src_valid                   // b. 当前 uop 确实要读这个源
      & pre.valid                   // c. 老 uop 是有效的未完成项
      & (pre.w_type==VRF);          //   且写的是向量寄存器
```

命中后还要区分「数据出来了没」：`wait = hit & ~pre.w_valid`（[rvv_backend_dispatch_raw_uop_rob.sv:65-71](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_raw_uop_rob.sv#L65-L71)）。`w_valid=1` 表示 ROB 里已有数据→可旁路；`w_valid=0` 表示老 uop 还没算完→必须 `wait`（停拍）。任意一项 `wait` 命中，对应的 `vs1_wait/vs2_wait/vd_wait/v0_wait` 即拉高（[rvv_backend_dispatch_raw_uop_rob.sv:74-81](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_raw_uop_rob.sv#L74-L81)）。`raw_uop_uop`（[rvv_backend_dispatch_raw_uop_uop.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_raw_uop_uop.sv)）逻辑相同，只是比较对象换成「本拍下标更小的那些 uop」，且参数化 `PREUOP_NUM=i`（[rvv_backend_dispatch_raw_uop_uop.sv:197-205](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch.sv#L197-L205) 处的例化）。

**（2）结构冒险：VRF 读端口分配**。`rvv_backend_dispatch_structure_hazard.sv` 在 DISPATCH2 分支（[rvv_backend_dispatch_structure_hazard.sv:343-438](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_structure_hazard.sv#L343-L438)）做两件事：按 `uop_class` 给 4 个读端口各分配一个要读的寄存器号 `rd_index[0..3]`（uop0 的读占低端口、uop1 占剩余），再判断「两个 uop 合计读端口是否超过 4」。判断逻辑极简（[rvv_backend_dispatch_structure_hazard.sv:427-436](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_structure_hazard.sv#L427-L436)）：

```systemverilog
case({strct_uop[0].uop_class, strct_uop[1].uop_class})
  {VVV,VVV},{VVV,XVV},{VVV,VVX},{XVV,VVV},{VVX,VVV}: arch_hazard.vr_limit = 1'b1;
  default:    arch_hazard.vr_limit = 1'b0;
endcase
```

回忆 4.1.3 的「V 的个数 = 读端口数」：`VVV`=3、`XVV`/`VVX`=2。上面 5 种组合的端口和都 >4（3+3、3+2、2+3），故 `vr_limit=1`（第二个 uop 本拍停）；其余组合 ≤4，可同发。

**（3）操作数还原**。`rvv_backend_dispatch_operand.sv` 把 4 个读端口的回读数据 `rd_data_vrf2dp[0..3]` 按 `uop_class` 还原成每个 uop 的 `vs1/vs2/vd/v0`（DISPATCH2 分支 [rvv_backend_dispatch_operand.sv:273-333](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_operand.sv#L273-L333)）。例如 `VVV` 时 `vs2←rd[0], vs1←rd[1], vd←rd[3]`，与结构冒险模块对端口的分配严格对应——两个模块是**配对**的：一个决定「哪个端口读哪个寄存器」，另一个决定「读回来的数据怎么摆回 vs1/vs2/vd」。

**（4）ROB 旁路**。`rvv_backend_dispatch_bypass.sv` 逐字节决定操作数取自 VRF 还是 ROB。对每个 ROB 项 `i`、每个字节 `j`，先算「这个字节能否从该项转发」的选择信号（[rvv_backend_dispatch_bypass.sv:31-56](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_bypass.sv#L31-L56)）：

```systemverilog
vs1_sel[i][j] = (raw_uop_rob.vs1_hit[i]) & 
                (rob_byp[i].byte_type[j]==BODY_ACTIVE | ...);  // 该字节已就绪
```

再用一个逐字节的 `always` 块在 VRF 默认值之上覆盖：命中就取 ROB 的 `w_data`，命中但属于 tail/inactive-agnostic 字节则填 `0xFF`（[rvv_backend_dispatch_bypass.sv:58-77](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_bypass.sv#L58-L77)）。这里的 `byte_type`（`BODY_ACTIVE`/`TAIL`/`BODY_INACTIVE`，定义见 [rvv_backend.svh:288-293](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend.svh#L288-L293)）让旁路精确到「老 uop 只写了向量的前几个字节」这种情形。

**（5）发射控制**。`rvv_backend_dispatch_ctrl.sv` 把一切汇成最终握手。核心是一条**按序的 `uop_valid` 链**（[rvv_backend_dispatch_ctrl.sv:74-106](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_ctrl.sv#L74-L106)）：

```systemverilog
uop_valid[0] = uop_valid_uop2dp[0] & ~raw_uop_rob[0].vs1_wait & ~...vs2_wait & ~...vd_wait & ~...v0_wait;
uop_valid[i] = uop_valid[i-1] & uop_valid_uop2dp[i] & ~各类wait[i] ...;   // i>0 还要 AND 前一个
uop_valid[last] = ... & ~arch_hazard.vr_limit;                            // 末位额外看结构冒险
```

「`uop_valid[i]` 要 AND 上 `uop_valid[i-1]`」正体现了**按序派发**：uop0 停了，uop1 就算自己没冒险也不能越过它。结构冒险只在链的末位接上 `~vr_limit`——因为 `vr_limit` 表达的是「第二个 uop 读不下」，自然只卡 uop1。

接着 `rs_ready[i]` 按 `uop_exe_unit` 选出目标保留站的 ready 并与前一个 AND（[rvv_backend_dispatch_ctrl.sv:107-160](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_ctrl.sv#L107-L160)），最终（[rvv_backend_dispatch_ctrl.sv:161-200](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_ctrl.sv#L161-L200)）：

```systemverilog
uop_ready_dp2uop[i] = uop_valid[i] & uop_ready_rob2dp[i] & rs_ready[i];
rs_valid_dp2alu[i]  = uop_ready_dp2uop[i] & (exe_unit==ALU|CMP);
rs_valid_dp2mul[i]  = uop_ready_dp2uop[i] & (exe_unit==MUL|MAC);
uop_valid_dp2rob[i] = uop_ready_dp2uop[i] & pshrob_valid[i];
// ...依此扇出到各 RS 与 ROB
```

于是「能否发射」=「无 RAW 等待 ∧ 无结构冒险 ∧ 目标 RS 没满 ∧ ROB 没满」，且严格按 uop 顺序。派发顶层 `rvv_backend_dispatch.sv` 把这些子模块的输出与 uop 字段组装成各 RS 的结构体（`ALU_RS_t`/`MUL_RS_t`/…，[rvv_backend_dispatch.sv:329-498](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch.sv#L329-L498)），同时按 `uop_exe_unit` 给每个 uop 分配本拍唯一的 ROB 表项号 `rob_address[i]`（首项取 ROB 给的指针，后续项在前项基础上按 `pshrob_valid` 递增，[rvv_backend_dispatch.sv:331-337](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch.sv#L331-L337)）。

#### 4.3.4 代码实践

**实践目标**：复现「两个有依赖的 uop 如何被派发」，并定位结构冒险的判定。

**操作步骤**：

1. **RAW 场景**：设想 uop0 是 `vadd.vv vd=v1`，uop1 是 `vmul.vv` 且 `vs2=v1`。打开 [rvv_backend_dispatch_raw_uop_uop.sv:44-62](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_raw_uop_uop.sv#L44-L62)，确认 `vs2_cmp` 会命中（uop1 的 `vs2_index` 等于 uop0 的 `dst_index`），又因 uop0 本拍还没写出数据（`w_valid=0`），`vs2_wait=1`。
2. 在 [rvv_backend_dispatch_ctrl.sv:82-92](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_ctrl.sv#L82-L92) 确认 `uop_valid[1]` 因此为 0，本拍只发 uop0；等 uop0 算完进 ROB 且 `w_valid=1` 后，[rvv_backend_dispatch_bypass.sv:33-36](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_bypass.sv#L33-L36) 的 `vs2_sel` 命中，uop1 的 vs2 直接从 ROB 转发，无需读 VRF、无需再等。
3. **结构冒险场景**：设想 uop0、uop1 都是 `VVV`。在 [rvv_backend_dispatch_structure_hazard.sv:428-433](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_structure_hazard.sv#L428-L433) 确认 `{VVV,VVV}` 落入 `vr_limit=1` 分支，再到 [rvv_backend_dispatch_ctrl.sv:93-105](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_ctrl.sv#L93-L105) 看 `uop_valid[last]` 因 `~arch_hazard.vr_limit` 而为 0，本拍只发 uop0。

**需要观察的现象 / 预期结果**：RAW 依赖下，后继 uop 会停拍直到前驱进 ROB；一旦前驱数据就绪，后继靠旁路立即发射、不等 VRF 回写。两个 `VVV` 同拍时，第二个被结构冒险卡住、延后一拍。

**待本地验证**：以上为静态推断。若跑仿真，可在 `uop_ready_dp2uop` 与 `rs_valid_dp2*` 上观察「每拍实际放行几个 uop」来验证按序与冒险停拍行为。

#### 4.3.5 小练习与答案

**练习 1**：为什么 RAW 检测要分别做 `raw_uop_rob` 和 `raw_uop_uop` 两套，而不是只查 ROB？

**参考答案**：因为本拍从 uop 队列**同时**取出的两个 uop，彼此可能就有依赖（uop1 的源 = uop0 的目的）。而 uop0 本拍才刚派发，**还没进 ROB**（要等本拍结束、`uop_valid_dp2rob` 握手成功才入 ROB），所以仅查 ROB 查不到这条依赖，必须额外用 `raw_uop_uop` 在同拍 uop 之间查（[rvv_backend_dispatch.sv:189-206](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch.sv#L189-L206)）。

**练习 2**：派发是「乱序」还是「按序」的？依据是哪段代码？

**参考答案**：派发本身是**按序**的。依据是 [rvv_backend_dispatch_ctrl.sv:82-92](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_ctrl.sv#L82-L92) 中 `uop_valid[i] = uop_valid[i-1] & …`——后一个 uop 的有效性强依赖前一个。「乱序」发生在更后面的**执行**阶段（保留站之后），最终再由 ROB 按序退休，那是 u7-l3 的内容。

**练习 3**：`structure_hazard` 里 `rd_index[0..3]` 既用于 VRF 读、又用于判断 `vr_limit`。这两个职责会不会冲突？

**参考答案**：不冲突，而是协作。`rd_index` 的分配保证「读端口够时，把两个 uop 的源紧凑塞进 4 个端口」；`vr_limit` 则是分配失败（塞不下）时的停拍信号。两者基于同一张 `uop_class` 真值表，所以端口分配与停拍判定完全一致（[rvv_backend_dispatch_structure_hazard.sv:349-436](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_structure_hazard.sv#L349-L436)）。

---

## 5. 综合实践

把译码与派发串起来，完成一次「端到端追踪」。

**任务**：构造一条 `vadd.vv v4, v2, v1`（`LMUL=2`，即占 v4–v5 两组、读 v2–v3 与 v1–v2），紧接一条 `vmul.vx v6, v4, a0`（依赖前一条的 v4）。完成下表（在源码中找依据，标注文件与行号）：

| 追踪点 | 你的答案 | 依据 |
| --- | --- | --- |
| `vadd.vv` 展开成几个 uop？`uop_index_max`=? | | [rvv_backend_decode_unit_ari_de2.sv:182-186](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari_de2.sv#L182-L186) |
| 每个 uop 的 `uop_exe_unit`、`uop_class` | | [rvv_backend_decode_unit_ari_de2.sv:232-480](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari_de2.sv#L232-L480) |
| `vadd` 与 `vmul` 之间是否存在 RAW？命中的源是？ | | [rvv_backend_dispatch_raw_uop_rob.sv:43-62](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_raw_uop_rob.sv#L43-L62) |
| `vmul` 何时能发射？靠停拍还是旁路？ | | [rvv_backend_dispatch_ctrl.sv:74-106](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_ctrl.sv#L74-L106)、[rvv_backend_dispatch_bypass.sv:58-77](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend_dispatch_bypass.sv#L58-L77) |

**参考思路**：`vadd.vv`（LMUL2、SEW8、VLEN128）处理 32 个元素、每 uop 16 个→2 个 uop，`uop_index_max=1`，`uop_class=XVV`、`uop_exe_unit=ALU`。后一条 `vmul` 的 vs2=v4 正是 `vadd` 的目的→RAW 命中 `vs2`；若 `vadd` 已完成入 ROB（`w_valid=1`），`vmul` 经旁路取 v4、本拍即可发射，否则停拍等待。

---

## 6. 本讲小结

- **译码 = stripmining 展开**：一条向量指令按 `EMUL` 展开成至多 8 个 uop，每拍由 `rvv_backend_decode_unit_ari_de2.sv` 用游标 `uop_index` 连续生成 `NUM_DE_UOP`=4 个，跨拍靠 `uop_index_remain` 续接；`first_uop_valid`/`last_uop_valid` 标记首尾。
- **`uop_exe_unit` 由 funct6 决定**（`VADD`→ALU、`VMUL`→MUL、`VMACC`→MAC、归约→RDT、置换→PMT），`uop_class`（XXX…VVV）描述要读几个 VRF 端口。
- **派发三查**：RAW（对 ROB 与对本拍更早 uop 两套）、结构冒险（两个 uop 的 V 数和 >4 则 `vr_limit`）、下游满（RS/ROB ready）。
- **派发按序**：`uop_valid[i]` 依赖 `uop_valid[i-1]`，后继 uop 不可越过前驱；乱序只发生在执行阶段。
- **ROB 旁路逐字节**：RAW 命中且老 uop 数据就绪时，直接从 ROB 转发，避免停拍等 VRF 回写；tail/inactive-agnostic 字节填 `0xFF`。
- **结构冒险与操作数还原是配对模块**：`structure_hazard` 分配读端口并判停拍，`operand` 按同一张 `uop_class` 表把读回数据还原成 vs1/vs2/vd/v0。

## 7. 下一步学习建议

- **u7-l3（向量寄存器堆、ROB 与退休）**：本讲反复提到的 VRF（4 读端口）与 ROB（8 项、`w_valid`/`w_data`/`byte_type`）在那里有完整定义；派发写入的 RS 结构体如何被消费、ROB 如何按序退休，也都由该讲收口。
- **u7-l4（MAC 外积乘累加引擎）**：本讲把 `VMACC` 类 uop 派发到 MAC 保留站后，真正的 256-MAC 外积运算如何展开，是下一站的重点。
- **延伸阅读**：想看「派发宽度可配置」的全貌，可对比 [rvv_backend_define.svh:8-55](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend_define.svh#L8-L55) 的 `DISPATCH3` 分支（3 派发、6 读端口），体会结构冒险判定表随之放大的代价；还可读 [rvv_backend.sv:470-521](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend.sv#L470-L521) 看派发如何在顶层与各保留站、ROB、VRF 连线。
