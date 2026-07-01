# RVV 后端总览与 Chisel 桥接

## 1. 本讲目标

本讲是**第 7 单元（RVV 向量/矩阵后端）的入口**。前面 6 个单元我们一直在讲 CoralNPU 的「标量核」——取指、译码、派发、ALU/MLU/DVU/LSU、TCM/Cache。从本讲开始，我们要跨过一道重要的边界，进入 CoralNPU 真正的「灵魂」：**向量（SIMD）与矩阵（MAC）后端**，即 RVV（RISC-V Vector）后端。

学完本讲，你应当能够：

1. 说清 **RVV 后端在 CoralNPU 中的角色**：它是被标量核「驱动」的协处理器，二者通过一个解耦的命令队列协作。
2. 画出一条向量指令从 **Chisel 标量核派发 → 跨语言边界 → SystemVerilog 后端执行** 的完整数据流，并标注每一段落在哪个文件、哪一行。
3. 分清 **Chisel 侧三件套**（`RvvCore` / `RvvInterface` / `RvvDecode`）与 **SystemVerilog 侧三件套**（`RvvCore.sv` / `RvvFrontEnd.sv` / `rvv_backend.sv`）各自的职责与握手接口。
4. 理解 RVV 后端的**资源模型**（向量寄存器、累加器），并能识别文档设计意图与当前 RTL 实现之间的差异。

> 本讲只做「总览 + 边界」。向量译码/派发细节（u7-l2）、VRF/ROB/Retire（u7-l3）、MAC 外积引擎（u7-l4）、向量算术单元（u7-l5）、Stripmining 编码（u7-l6）会在后续讲义逐层展开。

## 2. 前置知识

阅读本讲前，请确认你已理解以下概念（均在前面单元讲过）：

- **标量核的 inorder 派发 / 乱序退休**（u4-l4）：标量核每周期最多派发 4 条指令，其中一部分可以是向量指令。
- **记分板与握手**（u4-l4、u5-l1）：CoralNPU 各模块之间用 `Decoupled`（valid/ready/bits）握手，用记分板防数据冒险。
- **LSU 的 slot 机制**（u6-l1）：标量核只有一个 LSU，向量访存也复用它。
- **Chisel 与 SystemVerilog 双轨制**（u1-l2）：标量核/SoC/总线用 Chisel 写，向量与矩阵 MAC 后端用 SystemVerilog 写。

本讲新引入的术语：

| 术语 | 含义 |
|------|------|
| **RVV** | RISC-V Vector 扩展。CoralNPU 实现的是其一个**实用子集**（`Zve32x`），文档里「SIMD」与「vector」混用。 |
| **前端 / 后端（Frontend / Backend）** | 这里不是编译器概念。RVV「前端」指把标量核送来的指令装配成命令、维护向量配置状态（vsetvli）；「后端」指译码成微操作、派发、执行、退休的整条流水线。 |
| **uop（微操作）** | 一条向量指令在 SV 后端被拆成多个 uop（受 LMUL/stripmining 影响），每个 uop 处理向量寄存器的一段。 |
| **BlackBox（Chisel）** | Chisel 中把一段已存在的 Verilog 当作「黑盒」实例化的机制，是跨 Chisel→SystemVerilog 边界的标准手段。 |
| **RVVCmd** | SV 侧的「命令」结构 = 压缩指令 + 标量操作数 rs1 + 当前向量配置状态（arch_state）。 |

## 3. 本讲源码地图

本讲涉及的关键文件按「自上而下、先 Chisel 后 SV」排列：

| 文件 | 语言 | 作用 |
|------|------|------|
| [doc/overview.md](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/overview.md) | 文档 | 给出 RVV 后端的设计意图：64×256b 向量寄存器、acc\<8\>\<8\> 累加器、256 MACs/周期、stripmining。 |
| [hdl/chisel/src/coralnpu/scalar/SCore.scala](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/SCore.scala) | Chisel | 标量核顶层，**实例化并接线** RVV 后端（`io.rvvcore`）。 |
| [hdl/chisel/src/coralnpu/rvv/RvvCore.scala](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvCore.scala) | Chisel | RVV 后端的 Chisel 入口：`RvvCoreShim`（接口翻译 + CSR 影像）与 `RvvCoreWrapper`（BlackBox，声明 SV 资源）。 |
| [hdl/chisel/src/coralnpu/rvv/RvvInterface.scala](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvInterface.scala) | Chisel | 定义 Chisel↔SV 边界的 Bundle：`RvvCoreIO`、`Rvv2Lsu`/`Lsu2Rvv`、`RvvCsrIO`、`Rob2Rt`。 |
| [hdl/chisel/src/coralnpu/rvv/RvvDecode.scala](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvDecode.scala) | Chisel | 定义跨边界的**压缩指令格式** `RvvCompressedInstruction`（2b opcode + 25b bits）。 |
| [hdl/verilog/rvv/design/RvvCore.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/RvvCore.sv) | SystemVerilog | SV 侧顶层，参数化 `N`（指令通道数），总装 `RvvFrontEnd` + `rvv_backend`，并处理 LSU/写回的 tie-off。 |
| [hdl/verilog/rvv/design/RvvFrontEnd.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/RvvFrontEnd.sv) | SystemVerilog | SV 前端：把压缩指令 + 标量操作数 + 配置状态装配成 `RVVCmd`，处理 vsetvli/vl/lmul/sew/vill，送入命令队列。 |
| [hdl/verilog/rvv/design/rvv_backend.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend.sv) | SystemVerilog | SV 执行后端：命令队列 → 译码 → 派发 → 保留站 → 执行单元 → ROB → 退休 → VRF/XRF 写回。 |
| [hdl/verilog/rvv/inc/rvv_backend_define.svh](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend_define.svh) | SystemVerilog | 关键宏：`ISSUE_LANE`、`NUM_*`、`VLEN`、`ROB_DEPTH` 等，是后端的「参数真相源」。 |
| [hdl/verilog/rvv/inc/rvv_backend.svh](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend.svh) | SystemVerilog | 关键类型：`RVVInstruction`、`RVVCmd`、`RVVConfigState`、`UOP_QUEUE_t`、`ROB2RT_t`。 |

## 4. 核心概念与源码讲解

### 4.1 RVV 后端是什么：角色、数据流与资源模型

#### 4.1.1 概念说明

回顾 [overview.md](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/overview.md) 对整个 CoralNPU 的定位：这是一个「以矩阵能力为起点、再叠加向量与标量」的**融合设计（fused design）**。其中：

- **标量核**：一个精简的 `rv32im` 前端，跑 run-to-completion 模型，**唯一职责是驱动 ML+SIMD 后端的命令队列**。
- **向量核（vector/SIMD）**：被标量核前端用一个 **FIFO 结构解耦**，只有当向量寄存器堆里的依赖解除后，才把指令投递到对应命令队列。
- **矩阵 MAC 引擎**：以**量化外积（outer product）乘累加**为核心，是算力主力的所在。

换句话说，标量核是「指挥官」，RVV 后端是「干重活的兵团」。二者之间**不是函数调用关系，而是「生产者—消费者 + 解耦队列」关系**：标量核每周期最多把几条向量指令推进队列，之后就可以继续往下取指；RVV 后端按自己的节奏从队列取指令、译码、执行、写回。这种解耦正是 overview 强调的 *"decoupled from the backend by a FIFO structure"*。

#### 4.1.2 核心流程：一条向量指令的端到端旅程

以一条 `vadd.vv` 为例，它从生成到写回要跨越 **Chisel 标量核 → BlackBox 边界 → SV 前端 → SV 后端** 四大阶段：

```
[Chisel 标量核 SCore]                                    [SystemVerilog RVV 后端]
 Fetch ─▶ Decode ─▶ Dispatch                             ┌── RvvCore.sv (顶层, N=4)
   │            │                                  │     │      │
   │   把 32b RVV 指令压缩成                        │     │   ┌──┴──────────┐
   │   RvvCompressedInstruction(2b op+25b bits+pc) │     │   │RvvFrontEnd  │ 装配 RVVCmd
   │            │                                  │     │   │ .sv         │ + vsetvli 配置状态
   │            ▼ (Decoupled inst[] + rs[] + frs[]) │     │   └──┬──────────┘
   └──────► io.rvvcore (Flipped RvvCoreIO) ─────────┼─────┼──► cmd_valid/cmd_data
        经 RvvCoreShim → RvvCoreWrapper(BlackBox)   │     │      │
                                                   │     │   ┌──┴──────────┐
                                                   │     │   │ rvv_backend │ CQ→DE1→LCQ→DE2
                                                   │     │   │ .sv         │ →UQ→DP→RS→EXE
                                                   │     │   │             │ →ARB→ROB→Retire→VRF
   ◄───── rd_rob2rt_o (向量写回) / trap / csr ──────┼─────┼──┘
```

数据流的「关卡」依次是：

1. **派发（Chisel）**：标量核 `Dispatch` 识别出向量指令，把它的 32 位编码压成 `RvvCompressedInstruction`，连同标量操作数一起送上 `io.rvvcore.inst`。
2. **跨边界（BlackBox）**：`RvvCoreShim` 把 Chisel Bundle 翻译成扁平的 SV 信号，进入 SV 模块 `RvvCore`。
3. **前端装配（SV `RvvFrontEnd`）**：把压缩指令 + 标量 rs1 + 当前 `RVVConfigState`（sew/lmul/vl/…）打包成 `RVVCmd`，处理 `vsetvli`，对齐后送入命令队列。
4. **后端执行（SV `rvv_backend`）**：命令队列 → 两级译码 → uop 队列 → 派发到各保留站 → 执行单元（ALU/MUL/MAC/PMTRDT/DIV/LSU）→ 仲裁 → ROB → 退休 → 写回 VRF 或 XRF。
5. **结果回流**：向量结果经 `rd_rob2rt_o` 回到标量核的 ROB 写端口；标量结果（如 `vmv.x.s`、`vcpop`）经 `async_rd` 写回标量寄存器堆；异常经 `trap` 报给标量核的 FaultManager（`mcause=2`，非法指令）。

#### 4.1.3 源码精读：SCore 如何「接线」RVV 后端

RVV 后端在标量核 `SCore` 里是一个**可选模块**（由 `p.enableRvv` 开关控制）。它的 IO 声明见 [SCore.scala:47](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/SCore.scala#L47)，注意 `Flipped`——SCore 是「主机」，RVV 后端是「从机」：

```scala
val rvvcore = Option.when(p.enableRvv)(Flipped(new RvvCoreIO(p)))
```

最关键的几根连线（**这就是「指挥官 → 兵团」的全部通道**）：

- **指令下行**：派发器把压缩后的向量指令送给后端——[SCore.scala:434](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/SCore.scala#L434)
  ```scala
  dispatch.io.rvv.get <> io.rvvcore.get.inst
  ```
- **标量操作数**：把整数寄存器堆的读数据直接供给后端（向量-标量混合指令 OPIVX/OPFVF 需要）——[SCore.scala:440](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/SCore.scala#L440)
  ```scala
  io.rvvcore.get.rs := regfile.io.readData
  ```
- **浮点 rs1**：`OPFVF` 类指令的标量操作数来自浮点寄存器堆——[SCore.scala:452](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/SCore.scala#L452)。
- **向量写回**：后端的退休结果回到标量核 ROB 的向量写端口——[SCore.scala:80-84](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/SCore.scala#L80-L84)。
- **访存复用 LSU**：向量 load/store **不走独立通路，而是复用标量核唯一的 LSU**——[SCore.scala:242-243](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/SCore.scala#L242-L243)。
- **异常上报**：后端 trap 被包装成 `mcause=2`（非法指令），`mtval` 存原始指令编码——[SCore.scala:156-160](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/SCore.scala#L156-L160)。
- **CSR 同步**：`vstart`/`vxrm`/`vxsat`/`frm` 在 CSR 与后端间双向流动——[SCore.scala:460-468](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/SCore.scala#L460-L468)。
- **反压/空闲**：后端回报 `rvv_idle` 与 `queue_capacity`，派发器据此决定还能不能塞指令——[SCore.scala:436-437](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/SCore.scala#L436-L437)；`rvv_idle` 还参与 fetch fault 的精确判定（[SCore.scala:473-479](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/SCore.scala#L473-L479)）。

> **一句话总结**：标量核与 RVV 后端之间的全部交互，就是一个 `RvvCoreIO` Bundle——指令下行、操作数下行、结果/异常上行、CSR 同步、反压。把这个 Bundle 看懂，就掌握了「边界」。

#### 4.1.4 资源模型：向量寄存器与累加器（含文档/实现差异）

[overview.md](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/overview.md#L34-L72) 给出的设计意图资源模型是：

| 资源 | overview 设计意图 |
|------|------------------|
| 向量寄存器 | **64 个** `v0..v63`，每个 **256 位**（如 8×int32） |
| 累加器 | `acc<8><8>`：**8×8 个 32 位**槽位，供 MAC 外积引擎累加 |
| 算力 | MAC 引擎每周期 **256 MACs** |
| 编码 | 回收 C 扩展编码空间，提供 **6b 向量寄存器索引** + stripmining |

⚠️ **重要：文档与当前 RTL 实现存在版本差**（后续 u7-l3/u7-l4/u7-l6 会反复强调）。读源码时请以 RTL 为准：

- **VLEN（每寄存器位宽）**：[rvv_backend_define.svh:128-147](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend_define.svh#L128-L147) 用 `VLEN_128/256/512/1024` 宏四选一；**所有实际构建（FPGA/UVM/VCS/cocotb/Chisel）统一选 `-DVLEN_128`，即每寄存器 128 位**。可在 [hdl/chisel/src/coralnpu/BUILD:742](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/BUILD#L742) 与 [fpga/BUILD:75](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/BUILD#L75) 看到 `-DVLEN_128`。Chisel 侧 [Parameters.scala](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/Parameters.scala) 的 `rvvVlen = 128` 与之一致。
- **向量寄存器数量**：[rvv_backend_define.svh:58](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend_define.svh#L58) 的 `NUM_VRF 32` 与 [:162](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend_define.svh#L162) 的 `REGFILE_INDEX_WIDTH 5` 表明当前构建的是 **32 个、5b 索引**的向量寄存器。第 6 个索引位是「为 64 寄存器预留、尚未启用」。
- **算力**：MAC「256 MACs/周期」是 `VLEN=256` 时的数字；实际 `VLEN_128` 下为 128 MACs/周期（详见 u7-l4）。

> **学习策略**：先按 overview 的「64×256b + acc\<8\>\<8\> + 256 MACs」建立**设计直觉**（它解释了「为什么这么设计」），再在读 RTL 时随时换成「32×128b」的**实现现实**。本讲后续所有数据通路描述与该差异无关——无论 128 还是 256，跨边界与流水线结构都一样。

#### 4.1.5 代码实践：核对资源模型与构建配置

1. **实践目标**：亲手确认「文档说了什么」与「RTL 实际构建成什么」，建立读源码时切换两套数字的习惯。
2. **操作步骤**：
   - 阅读 [doc/overview.md:34-72](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/overview.md#L34-L72)，记录 Vector/MAC/Stripmining 三段给出的设计数字。
   - 用以下命令确认实际选用的 VLEN：
     ```bash
     git grep -n "VLEN_128" -- 'hdl/**/BUILD*' 'fpga/BUILD' 'tests/**/BUILD' 'tests/uvm/Makefile'
     ```
   - 打开 [rvv_backend_define.svh:57-85](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend_define.svh#L57-L85)，记录 `NUM_VRF`、`REGFILE_INDEX_WIDTH`、`ISSUE_LANE`、`NUM_LSU`/`NUM_ALU`/`NUM_MUL` 等执行单元数量。
3. **需要观察的现象**：所有构建目标都只出现 `VLEN_128`，不出现 `VLEN_256`；`REGFILE_INDEX_WIDTH=5`。
4. **预期结果**：得到一张「overview 设计值 ↔ RTL 构建值」对照表（64↔32、256↔128、6b↔5b）。
5. 待本地验证：上述 `git grep` 的具体匹配行数。

#### 4.1.6 小练习与答案

**练习 1**：为什么 CoralNPU 要用「FIFO 解耦」标量核与向量后端，而不是让标量核直接驱动执行单元？
> **参考答案**：向量指令延迟长、且常被 LMUL/stripmining 展开成多次执行，若标量核直接驱动会被频繁反压，浪费标量核的取指/派发带宽。用 FIFO 解耦后，标量核只需把指令「投递」进队列即可继续往下跑，后端按自己节奏消费，二者吞吐解耦。

**练习 2**：标量核检测到一个 RVV 后端上报的 `trap`，会把它当成什么异常？`mtval` 里放什么？
> **参考答案**：当成 `mcause=2`（非法指令）异常；`mtval` 存该向量指令的**原始 32 位编码**（由 `RvvCompressedInstruction.originalEncoding()` 还原，见 [SCore.scala:156-160](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/SCore.scala#L156-L160)）。

---

### 4.2 Chisel 前端桥接：RvvCore 三件套

#### 4.2.1 概念说明

RVV 后端的执行逻辑全在 SystemVerilog 里，但 CoralNPU 的标量核、SoC、寄存器堆都是 Chisel。要让两边对话，需要一个「翻译层」。Chisel 提供的标准做法是 **BlackBox**：把一段 Verilog 当作一个端口已知的黑盒实例化进 Chisel 图里。

CoralNPU 的这个翻译层由三个 Chisel 文件组成，可以记成「**一个入口 + 一份契约 + 一种压缩**」：

- **`RvvCore.scala`**（入口）：定义 `RvvCore`（工厂）→ `RvvCoreShim`（Module，做信号翻译 + CSR 影像）→ `RvvCoreWrapper`（BlackBox，声明要加载哪些 SV 文件）。
- **`RvvInterface.scala`**（契约）：定义边界两端的 Bundle 数据结构（`RvvCoreIO` 等）。
- **`RvvDecode.scala`**（压缩）：定义跨边界传递的「压缩指令」格式 `RvvCompressedInstruction`。

> 命名小坑：Chisel 里有个类叫 `RvvCore`，SystemVerilog 里也有个模块叫 `RvvCore`。为避免冲突，Chisel 侧真正实例化 SV 的类特意命名为 `RvvCoreShim`（见 [RvvCore.scala:615-618](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvCore.scala#L615-L618) 的注释）。

#### 4.2.2 核心流程：BlackBox 桥接的三层结构

```
SCore.io.rvvcore (RvvCoreIO, Flipped)
        │  Chisel Bundle（结构化、命名端口）
        ▼
  ┌──────────────────┐
  │  RvvCoreShim     │  ← Chisel Module
  │  - 把 Bundle 翻译成 Wrapper 的扁平 IO                  │  - 持有 vstart/vxrm/vxsat 影像寄存器
  │  - 在 CSR 写、后端 vcsr 写之间做 Mux 仲裁              │
  └────────┬─────────┘
           │  扁平信号（clk/rstn/inst_*/rs_*/rd_*/...）
           ▼
  ┌──────────────────┐
  │ RvvCoreWrapper   │  ← BlackBox + HasBlackBoxInline + HasBlackBoxResource
  │  - setInline("RvvCoreWrapper.sv", …)  生成把扁平信号                    │
  │    重新打包成 SV 数组/结构的胶水代码                                     │
  │  - addResource("…/rvv_backend.sv") 等  把整条 SV 后端源码                │
  │    作为资源塞进构建产物                                                  │
  └────────┬─────────┘
           │  RvvCoreWrapper.sv 内部实例化
           ▼
   SystemVerilog module RvvCore#(.N(4))  ← 真正的硬件
```

要点：

1. **`RvvCoreShim` 做两件事**：信号翻译 + CSR 影像。向量 CSR（`vstart`/`vxrm`/`vxsat`）既可能被标量核的 CSR 指令写，也可能被后端写回（如饱和运算置 `vxsat`），Shim 用 `MuxCase` 在多个写源之间仲裁，并持有一份影像寄存器供后端读取（[RvvCore.scala:669-690](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvCore.scala#L669-L690)）。它还在 CSR 指令修改 CSR 的那一拍「保守地」把 `configState.valid` 拉低，避免后端读到过渡态（[RvvCore.scala:650-656](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvCore.scala#L650-L656)）。
2. **`RvvCoreWrapper` 负责两份产物**：用 `GenerateCoreShimSource` 现场生成 `RvvCoreWrapper.sv`（把 SV `RvvCore#(.N)` 模块的结构化端口拆/装成扁平连线，[RvvCore.scala:27-391](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvCore.scala#L27-L391)）；用一长串 `addResource` 把整个 SV 后端（`RvvCore.sv`、`RvvFrontEnd.sv`、`rvv_backend.sv`、各执行单元、外部 cvfpu 等）作为资源登记进构建（[RvvCore.scala:559-612](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvCore.scala#L559-L612)）。
3. **`GenerateBackendConfig` 生成编译期开关**：例如 `DISPATCH3`（每周期派发 3 条 uop）与可选的 `ZVFBFWMA_ON`（BF16 扩展，受 `p.enableVectorBf16` 控制），见 [RvvCore.scala:394-423](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvCore.scala#L394-L423)。

#### 4.2.3 源码精读

**(a) 边界契约 `RvvCoreIO`** —— 这是「指挥官↔兵团」的全部接口，定义在 [RvvInterface.scala:61-92](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvInterface.scala#L61-L92)：

```scala
class RvvCoreIO(p: Parameters) extends Bundle {
    // 指令下行（译码周期）
    val inst = Vec(p.instructionLanes, Flipped(Decoupled(new RvvCompressedInstruction(p))))
    // 标量操作数（执行周期）：每条指令 2 个读口
    val rs  = Vec(p.instructionLanes * 2, Flipped(new RegfileReadDataIO(p)))
    val rd  = Vec(p.instructionLanes, Valid(new RegfileWriteDataIO(p)))  // 配置类回写
    val frs = Vec(p.instructionLanes, Input(UInt(32.W)))                 // OPFVF 的浮点 rs1
    // 向量访存复用 LSU
    val rvv2lsu = Vec(2, Decoupled(new Rvv2Lsu(p)))
    val lsu2rvv = Vec(2, Flipped(Decoupled(new Lsu2Rvv(p))))
    // 配置状态 / 异步回写 / 异常 / CSR / 反压
    val configState = Output(Valid(new RvvConfigState(p)))
    val async_rd    = Decoupled(new RegfileWriteDataIO(p))   // 标量回写（vmv.x.s 等）
    val async_frd   = Decoupled(new RegfileWriteDataIO(p))   // 浮点回写（vfmv.f.s）
    val trap        = Output(Valid(new RvvCompressedInstruction(p)))
    val csr         = new RvvCsrIO(p)
    val rvv_idle        = Output(Bool())
    val queue_capacity  = Output(UInt(4.W))
    val rd_rob2rt_o = Vec(4, new Rob2Rt(p))                   // 向量结果回标量 ROB
}
```

读这份契约可以看到几个设计决策：`inst` 是 `instructionLanes`（=4）路 `Decoupled`，意味着标量核每周期可并行投递最多 4 条向量指令；`rs` 是 `2×instructionLanes` 路，对应每条指令最多读 2 个标量寄存器；向量访存只给 2 路 `rvv2lsu/lsu2rvv`（`NUM_LSU=2`，见后）。

**(b) 压缩指令格式** —— 跨边界传递的不是 32 位原始编码，而是「**2 位 opcode + 25 位 bits + pc**」的压缩体，定义在 [RvvDecode.scala:36-48](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvDecode.scala#L36-L48)：

```scala
object RvvCompressedOpcode extends ChiselEnum {
  val RVVLOAD = Value(0.U)   // 原 opcode 0000111 (LOAD-FP)
  val RVVSTORE = Value(1.U)  // 原 opcode 0100111 (STORE-FP)
  val RVVALU  = Value(2.U)   // 原 opcode 1010111 (OP-V)
}
class RvvCompressedInstruction(p: Parameters) extends Bundle {
  val pc     = UInt(p.programCounterBits.W)
  val opcode = RvvCompressedOpcode()   // 2b：三大类
  val bits   = UInt(25.W)              // 原指令的 inst[31:7]
}
```

RVV 指令在 RV32 里只占 3 个主 opcode（load/store/OP-V），所以 7 位 opcode 可以无损压成 2 位；高 25 位（`funct6`/`vm`/`vs2`/`vs1`/`funct3`/`vd` 等）原样保留。压缩与还原分别由 `from_uncompressed`（[RvvDecode.scala:150-180](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvDecode.scala#L150-L180)）与 `originalEncoding`（[RvvDecode.scala:41-48](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvDecode.scala#L41-L48)）完成；load/store 还会用 `width` 字段做合法性过滤。

> 注意：`RvvDecode.scala` 里还有一个 `RvvS1DecodeInstruction`（[:423-456](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvDecode.scala#L423-L456)），是 Chisel 侧的「一级译码」辅助。但生产路径里，`RvvCoreShim` 把压缩指令**原样透传**给 SV（[RvvCore.scala:629](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvCore.scala#L629)），**真正的向量译码发生在 SV 侧**（u7-l2）。Chisel 侧只负责压缩 + 翻译。

**(c) Shim 的 CSR 影像仲裁** —— 以 `vxsat` 为例，它有三个写源：后端饱和运算写（`wr_vxsat`）、后端 vcsr 整体写（`vcsr_valid`）、标量核 CSR 指令写（`csr.vxsat_write`）。Shim 用 `MuxCase` 串起来，且后端写是「或」累加（粘性标志）——[RvvCore.scala:681-686](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvCore.scala#L681-L686)：

```scala
val vxsat_wdata = MuxCase(vxsat, Seq(
    rvvCoreWrapper.io.wr_vxsat_valid_o -> (vxsat | rvvCoreWrapper.io.wr_vxsat_o),  // 粘性或
    rvvCoreWrapper.io.vcsr_valid       -> rvvCoreWrapper.io.vcsr_vxsat,
    io.csr.vxsat_write.valid           -> io.csr.vxsat_write.bits,
))
```

#### 4.2.4 代码实践：跟踪 BlackBox 的资源登记

1. **实践目标**：理解「Chisel 一个 BlackBox 如何把整套 SV 后端拉进构建」。
2. **操作步骤**：
   - 打开 [RvvCore.scala:559-612](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvCore.scala#L559-L612)，把 `addResource(...)` 列出的 SV 文件分类：哪些是 `design/`（可综合 RTL）、哪些是 `inc/`（头文件/类型）、哪些是 `external/`（第三方 cvfpu 等）。
   - 在 [:484-487](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvCore.scala#L484-L487) 找到 `setInline`/`addResource` 的头文件加载顺序，解释为什么 `rvv_backend_config.svh` 要用 `setInline`（按 `Parameters` 动态生成）而 `rvv_backend_define.svh` 用 `addResource`（静态资源）。
3. **需要观察的现象**：`rvv_backend.sv`、`RvvCore.sv`、`RvvFrontEnd.sv` 都在 `design/` 列表里；`rvv_backend.svh`/`rvv_backend_define.svh`/`rvv_backend_opcode.svh` 在 `inc/` 列表里。
4. **预期结果**：你能画出「RvvCoreWrapper 这个 BlackBox 内部一共包含多少个 SV 文件」的清单。
5. 待本地验证：`addResource` 的总数（提示：含大量 external cvfpu 文件）。

#### 4.2.5 小练习与答案

**练习 1**：为什么把 7 位 opcode 压成 2 位？这样做有什么代价？
> **参考答案**：RVV 在 RV32 里只占 load-FP/store-FP/OP-V 三个主 opcode，7→2 位是无损压缩，能省下边界连线的位宽与 SV 端 `RVVInstruction` 结构的位宽。代价是失去了用 opcode 区分「非法指令」的能力——压缩时已用 `width` 字段做了一层过滤（[RvvDecode.scala:159-164](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvDecode.scala#L159-L164)），但更细的非法判定要等 SV 译码。

**练习 2**：`RvvCoreShim` 为什么要持有 `vstart/vxrm/vxsat` 的影像寄存器，而不是直接把 CSR 的值接给后端？
> **参考答案**：因为这三个 CSR 有**多个写源**（标量核 CSR 指令、后端 vcsr 整体写、后端 vxsat 粘性写）。Shim 用影像寄存器 + `MuxCase` 做仲裁，给后端一个稳定的「当前有效值」，并在标量核写 CSR 的过渡拍把 `configState.valid` 拉低，避免后端读到半新半旧的状态。

---

### 4.3 SystemVerilog 顶层与前端装配：RvvCore.sv / RvvFrontEnd.sv

#### 4.3.1 概念说明

跨过 BlackBox 边界，进入 SystemVerilog 世界。这里的第一站是 SV 顶层 `RvvCore.sv`，它是一个**参数化模块**（参数 `N` = 指令通道数，默认 4），做两件事：

1. **总装**：实例化 `RvvFrontEnd`（前端）和 `rvv_backend`（后端），把二者用一条命令队列连起来。
2. **tie-off**：把后端与外界的 LSU、标量/浮点写回、trap、vxsat 等接口做「转接」与「兜底」。

`RvvFrontEnd.sv` 的职责正如其文件头注释所说（[RvvFrontEnd.sv:15-23](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/RvvFrontEnd.sv#L15-L23)）：把 `RVVInstruction` **装配成 `RVVCmd`** 再存入命令队列，并维护架构配置状态（LMUL/SEW/vl）。它的输入可能未对齐（`[无效, 有效, 有效, 无效]`），输出保证对齐（`[有效, 有效, 无效, 无效]`）。因为标量寄存器堆的参数（vx/配置类指令用）比指令**晚一拍**到达，前端会引入**一拍延迟**再入队。

#### 4.3.2 核心流程：从压缩指令到 RVVCmd

```
inst_valid/inst_data (RVVInstruction[])        reg_read_data / freg_read_data
   │  可能未对齐                                      │  晚一拍到达
   ▼                                                  │
 RvvFrontEnd                                          │
   ├─ inst_q[N]            ← 锁存一拍指令 ────────────┘
   ├─ inst_config_state[N] ← 串行推算 vsetvli 后的配置
   │     （vsetvli/vsetivli/vsetvl 三种编码分支）
   │     → 算 vlmax、vl（饱和）、vill（合法性）、lmul（可按 vl 收缩）
   ├─ unaligned_cmd[N]     ← RVVCmd = {opcode, bits, rs1, arch_state}
   │     rs1 来源：OPFVF→freg，其它需要 rs1 的→reg，否则 0
   ├─ Aligner              ← 把未对齐有效项压实到低位
   └─ cmd_valid/cmd_data (RVVCmd[])  ──▶  rvv_backend 的命令队列
        reg_write_*       ──▶  配置类指令把新 vl 写回标量 rd
        trap_valid/trap_data ──▶ vill 的指令触发异常
```

关键数据结构（定义在 [rvv_backend.svh:92-110](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend.svh#L92-L110)）：

```systemverilog
typedef struct packed {
  logic [`PC_WIDTH-1:0] pc;
  RVVOpCode             opcode;   // LOAD / STORE / RVV
  logic [24:0]          bits;     // 原 inst[31:7]
} RVVInstruction;

typedef struct packed {
  RVVOpCode  opcode;
  logic [24:0] bits;
  logic [31:0] rs1;               // 已从标量寄存器堆读出
  RVVConfigState arch_state;      // 当前 sew/lmul/vl/vstart/…
} RVVCmd;
```

即 `RVVCmd = RVVInstruction + 标量操作数 + 配置状态快照`。后端执行时不再依赖标量核，**自带全部上下文**——这正是「解耦」的物质基础。

`RVVConfigState`（[rvv_backend.svh:67-82](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend.svh#L67-L82)）承载 RVV 向量配置：`vill`（非法）、`vl`（向量长度）、`vstart`、`ma`/`ta`（mask/tail agnostic）、`xrm`（定点舍入）、`xsat`（饱和）、`sew`（元素宽）、`lmul`/`lmul_orig`（寄存器分组）。

#### 4.3.3 源码精读

**(a) SV 顶层 `RvvCore` 的总装** —— [RvvCore.sv:107-131](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/RvvCore.sv#L107-L131) 实例化前端：

```systemverilog
RvvFrontEnd#(.N(N)) frontend(
    .inst_valid_i(inst_valid), .inst_data_i(inst_data), .inst_ready_o(inst_ready),
    .reg_read_data_i(reg_read_data), .freg_read_data_i(freg_read_data),
    .cmd_valid_o(frontend_cmd_valid), .cmd_data_o(frontend_cmd_data),  // ★ 喂给后端
    .queue_capacity_i(queue_capacity_internal),                        // ★ 后端反压
    ...);
```

后端实例化在 [RvvCore.sv:239-283](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/RvvCore.sv#L239-L283)，注意它的输入就是前端的输出 `frontend_cmd_valid`/`frontend_cmd_data`。二者之间的反压由 `queue_capacity` 传递：后端报「还能塞几个」，前端据此决定收几条（[RvvCore.sv:133-142](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/RvvCore.sv#L133-L142)）。

`rvv_idle` 的定义点很值得读——它要求「后端空 + 前端没有在途命令」同时成立（[RvvCore.sv:238](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/RvvCore.sv#L238)）：

```systemverilog
assign rvv_idle = rvv_backend_idle && (frontend_cmd_valid == 0);
```

**(b) `RvvFrontEnd` 的 vsetvli 处理** —— [RvvFrontEnd.sv:142-333](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/RvvFrontEnd.sv#L142-L333) 是前端最复杂的一段，它按 RVV 规范 6.2/6.3 节实现 `vsetvli/vsetivli/vsetvl` 三种配置指令：解析 `avl`（应用向量长度）、算 `vlmax`、把 `vl` 饱和到 `vlmax`、判定 `vill`（非法 sew/lmul 组合）。还有一个 `REDUCE_LMUL` 优化：根据实际 `vl` 把 `lmul` 收缩到「刚好够用」，省功耗与寄存器压力（[:254-330](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/RvvFrontEnd.sv#L254-L330)）。

**(c) `RVVCmd` 的组装与对齐** —— [RvvFrontEnd.sv:358-402](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/RvvFrontEnd.sv#L358-L402)，注意 `rs1` 的来源选择（`bits[7]` 即 funct3 高位决定是否用标量 rs1，`funct3==101` 即 OPFVF 用浮点 rs1）：

```systemverilog
unaligned_cmd_data[i].rs1 = inst_q[i].bits[7] ?
      ((inst_q[i].bits[7:5] == 3'b101) ? freg_read_data_i[i]   // OPFVF
                                       : reg_read_data_i[2*i]) // OPIVX/OPMVX/OPCFG
    : 0;
...
Aligner#(.T(RVVCmd), .N(N)) cmd_aligner(   // 把未对齐压实
    .valid_in(unaligned_cmd_valid), .data_in(unaligned_cmd_data),
    .valid_out(cmd_valid_o), .data_out(cmd_data_o));
```

**(d) tie-off 细节** —— `RvvCore.sv` 里大量 `always_comb` 块做接口转接。例如标量回写目前**只接受 slot 0**（[RvvCore.sv:179-194](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/RvvCore.sv#L179-L194)，带 `TODO(derekjchow)` 注释表明待扩展）；LSU 的请求/反馈结构 `UOP_RVV2LSU_t`/`UOP_LSU2RVV_t` 与扁平端口的互转在 [:148-177](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/RvvCore.sv#L148-L177)。

#### 4.3.4 代码实践：核对话点契约

1. **实践目标**：确认「前端装配的 `RVVCmd` 字段，恰好就是后端命令队列的消费单元」。
2. **操作步骤**：
   - 在 [rvv_backend.svh:99-110](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend.svh#L99-L110) 记下 `RVVCmd` 的四个字段（opcode/bits/rs1/arch_state）。
   - 在 [RvvFrontEnd.sv:374-377](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/RvvFrontEnd.sv#L374-L377) 找到前端对这四个字段的赋值，一一对应。
   - 在 [rvv_backend.sv:323-350](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend.sv#L323-L350) 找到命令队列 `u_command_queue` 的 `datain(insts_rvs2cq)`，确认它就是前端的 `cmd_data_o`。
3. **需要观察的现象**：字段名与位宽在「生产者（前端）— 通道（命令队列）— 消费者（后端译码）」三处完全一致。
4. **预期结果**：你能不查文档写出 `RVVCmd` 的字段表，并指出每个字段在前端哪一行被填入。
5. 待本地验证：无。

#### 4.3.5 小练习与答案

**练习 1**：`RvvFrontEnd` 为什么引入「一拍延迟」再入队？
> **参考答案**：因为 `vx`/配置类指令需要的标量操作数（rs1）从标量寄存器堆读出，比指令派发**晚一拍**到达（[RvvFrontEnd.sv:20-23](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/RvvFrontEnd.sv#L20-L23) 注释）。前端用 `inst_q` 锁存指令一拍，等操作数到齐后再组装成 `RVVCmd` 入队。

**练习 2**：`rvv_idle` 为什么不能只看 `rvv_backend_idle`？
> **参考答案**：`rvv_backend_idle` 只表示后端流水线空了（命令队列/ROB 等全空，[rvv_backend.sv:1162](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend.sv#L1162)），但前端可能还有已装配、尚未入队的在途命令（`frontend_cmd_valid != 0`）。必须二者都空，整个 RVV 后端才算真正空闲（[RvvCore.sv:238](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/RvvCore.sv#L238)）。

---

### 4.4 SystemVerilog 执行后端：rvv_backend.sv

#### 4.4.1 概念说明

`rvv_backend.sv` 是 RVV 后端的「执行引擎」，一条长达千行的模块，内部是一条完整的**乱序执行、按序退休**流水线。它从命令队列消费 `RVVCmd`，一路走到写回向量/标量寄存器堆。理解它的关键是抓住「**两级译码 + 多队列 + 多执行单元 + ROB**」的骨架。

#### 4.4.2 核心流程：rvv_backend 的流水线骨架

```
RVVCmd (来自前端)
   │
   ▼
[Command Queue] multi_fifo(RVVCmd)        ← rvv_backend.sv:323  反压前端
   │ pop
   ▼
[DE1] rvv_backend_decode                  ← :367  算 EEW/EMUL 等，产出 LCMD_t（合法命令）
   │
   ▼
[Legal Command Queue] multi_fifo(LCMD_t)  ← :376
   │
   ▼
[DE2] rvv_backend_decode_de2              ← :408  按 LMUL/stripmine 拆成多个 uop
   │
   ▼
[Uop Queue] multi_fifo(UOP_QUEUE_t)       ← :422
   │
   ▼
[Dispatch] rvv_backend_dispatch           ← :470  结构冒险检测、操作数准备、旁路
   │  ├─▶ ALU_RS  ─▶ rvv_backend_alu       (NUM_ALU=2)
   │  ├─▶ MUL_RS  ─▶ rvv_backend_mulmac    (NUM_MUL=2，含 MAC 外积引擎)
   │  ├─▶ DIV_RS  ─▶ rvv_backend_div       (NUM_DIV=1)
   │  ├─▶ PMTRDT_RS ─▶ rvv_backend_pmtrdt  (NUM_PMTRDT=1，置换/归约)
   │  ├─▶ LSU_RS  ─▶ (送到标量核 LSU)      (NUM_LSU=2)
   │  └─▶ DP2ROB（同时入 ROB 排队）
   ▼
[ARB] rvv_backend_arb                      ← :1041  各执行单元结果仲裁入 ROB
   │
   ▼
[ROB] rvv_backend_rob                      ← :1059  重排序、按序退休、trap flush
   │
   ▼
[Retire] rvv_backend_retire                ← :1089  写回 VRF / XRF / FRF / vxsat / vcsr
   │
   ▼
[VRF] rvv_backend_vrf                      ← :1139  向量寄存器堆（读口供 Dispatch/PMT，写口供 Retire）
```

整条流水线的「宽度」与「深度」参数都集中在 [rvv_backend_define.svh:4-55](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend_define.svh#L4-L55)。当前默认 `DISPATCH3`（每周期派发 3 条 uop）：

| 参数 | 值（DISPATCH3） | 含义 |
|------|-----------------|------|
| `ISSUE_LANE` | 4 | 标量核每周期最多送入的指令条数 |
| `NUM_DE_INST` | 2 | 每周期 DE 译码的指令数 |
| `NUM_DE_UOP` | 6 | 每周期 DE2 产出的 uop 数 |
| `NUM_DP_UOP` | 3 | 每周期 Dispatch 派发的 uop 数 |
| `NUM_LSU`/`NUM_ALU`/`NUM_MUL` | 2/2/2 | 各执行单元实例数 |
| `NUM_PMTRDT`/`NUM_DIV` | 1/1 | 置换归约/除法各 1 个 |
| `CQ_DEPTH`/`UQ_DEPTH`/`ROB_DEPTH` | 8/16/8 | 命令队列/uop 队列/ROB 深度 |

> **断言守护**：注意 [rvv_backend.sv:358-361](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend.sv#L358-L361) 等处的 `ASSERT_ON` 断言——它们强制要求 push/pop 必须是「低位连续有效」（如 `4'b1111/0111/0011/0001/0000`），这正是 `Aligner` + inorder 派发的硬件契约。

#### 4.4.3 源码精读

**(a) 命令队列与反压** —— [rvv_backend.sv:323-356](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend.sv#L323-L356)。命令队列用通用 FIFO `multi_fifo` 实现；`insts_ready` 由 `~cq_almost_full` 决定；trap 期间拒绝接收新指令（`is_trapping ? 'b0`）；并把「剩余容量」回报给前端（`remaining_count_cq2rvs`）。

**(b) 两级译码** —— DE1（[:367-373](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend.sv#L367-L373)）把 `RVVCmd` 翻成 `LCMD_t`（合法命令，带 EEW/EMUL/uop_index_max 等，类型见 [rvv_backend.svh:139-154](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend.svh#L139-L154)）；中间隔一个 Legal Command Queue；DE2（[:408-419](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend.sv#L408-L419)）再按 LMUL/stripmining 把一条合法命令**展开成多个 uop**（`UOP_QUEUE_t`，类型见 [rvv_backend.svh:246-285](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/inc/rvv_backend.svh#L246-L285)）。这正是 overview 所说「一次派发→四次串行 issue」的 stripmining 的硬件落点（u7-l6 详讲）。

**(c) Dispatch 与保留站** —— [rvv_backend.sv:469-521](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend.sv#L469-L521) 实例化派发器；[:523-747](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend.sv#L523-L747) 是各执行单元的保留站（每个都是一个 `multi_fifo`，带 `CHAOS_PUSH` 允许乱序入队）。Dispatch 同时把 uop 信息送进 ROB（`uop_valid_dp2rob`），这样结果出来后能按序提交。

**(d) 执行单元实例化** —— [:854-945](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend.sv#L854-L945)：ALU、PMTRDT、MULMAC（含 MAC 外积引擎）、DIV（及可选 FALU）。注意 `NUM_MUL=2` 的两个 mulmac 实例就是 MAC 外积引擎的「双轴」之一（u7-l4 详讲）。

**(e) ROB / Retire / VRF** —— ROB（[:1058-1086](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend.sv#L1058-L1086)）负责重排序与 trap flush；Retire（[:1088-1126](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend.sv#L1088-L1126)）写回 VRF/XRF/FRF 并更新 vxsat/vcsr；VRF（[:1138-1159](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend.sv#L1138-L1159)）是向量寄存器堆，给 Dispatch 与 PMT 提供读口、给 Retire 提供写口。三者的细节留待 u7-l3。

**(f) 后端 idle** —— [rvv_backend.sv:1162](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/rvv_backend.sv#L1162)：所有队列与 ROB 全空才算 idle。

#### 4.4.4 代码实践：把流水线骨架对应到行号

1. **实践目标**：能在 `rvv_backend.sv` 这千行代码里「秒定位」每个流水级的实例化点。
2. **操作步骤**：按下表在源码中填入行号（部分已给）：

   | 流水级 | 模块/实例 | 行号 |
   |--------|-----------|------|
   | 命令队列 | `u_command_queue` | 324 |
   | DE1 译码 | `u_decode_de1` | ？ |
   | 合法命令队列 | `u_legal_command_queue` | ？ |
   | DE2 译码 | `u_decode_de2` | ？ |
   | uop 队列 | `u_uop_queue` | ？ |
   | Dispatch | `u_dispatch` | ？ |
   | ALU | `u_alu` | ？ |
   | MULMAC | `u_mulmac` | ？ |
   | ROB | `u_rob` | ？ |
   | Retire | `u_retire` | ？ |
   | VRF | `u_vrf` | ？ |

3. **需要观察的现象**：每个 `multi_fifo` 都接了 `clear(trap_flush_rvv)`，说明 trap 会一键清空所有队列。
4. **预期结果**：你能看着自己填的表，复述一条 `vadd` 从命令队列到 VRF 写回经过的每一级。
5. 待本地验证：无（纯阅读）。

#### 4.4.5 小练习与答案

**练习 1**：为什么要有「两级译码 + 中间再插一个队列（LCQ）」，而不是一步译码到底？
> **参考答案**：DE1 做与 `arch_state` 无关的「静态」译码（算 EEW/EMUL），DE2 做依赖 `vl`/`lmul` 的「动态」展开（拆 uop）。中间的 Legal Command Queue 把两级解耦，让 DE1 不必等 DE2，提高吞吐；同时 trap 时可统一 `clear` 两个队列，简化精确异常处理。

**练习 2**：向量除法 `vdiv` 与向量加法 `vadd` 分别走哪个执行单元？数量各是多少？
> **参考答案**：`vadd` 走 ALU（`NUM_ALU=2`），`vdiv` 走 DIV（`NUM_DIV=1`）。除法单元更少是因为它面积大、延迟长、在 ML 工作负载里频次低（算力与面积的取舍，与标量核 DVU 全核唯一同理）。

---

## 5. 综合实践：跟踪一条 `vadd.vv` 的完整跨边界旅程

把本讲四个模块串起来，做一次「端到端追踪」。选一条最简单的向量指令 `vadd.vv v1, v2, v3`（两个向量寄存器相加），假设此前已用 `vsetvli` 设好 `SEW=32`、`LMUL=1`。

**任务**：在下表中，对每一个阶段，填出「所在文件:行号」「数据结构/信号名」「握手方向」。前两行已示范：

| # | 阶段 | 文件:行号 | 数据/信号 | 握手 |
|---|------|-----------|-----------|------|
| 1 | 标量核识别向量指令并压缩 | RvvDecode.scala:150 (`from_uncompressed`) | `RvvCompressedInstruction` | Dispatch → rvvcore |
| 2 | 跨 Chisel→SV 边界 | RvvCore.scala:629 (`io.inst <> …`) | `inst_valid/inst_data` | Shim↔Wrapper |
| 3 | Shim 翻译 + CSR 影像 | RvvCore.scala:? | ? | ? |
| 4 | 进入 SV 顶层 | RvvCore.sv:107 (`RvvFrontEnd` 实例化) | `inst_valid_i/inst_data_i` | ? |
| 5 | 前端装配 RVVCmd | RvvFrontEnd.sv:374 | `unaligned_cmd_data` | ? |
| 6 | Aligner 对齐 + 入命令队列 | RvvFrontEnd.sv:397 / rvv_backend.sv:324 | `cmd_data_o` / `insts_rvs2cq` | ? |
| 7 | DE1→LCQ→DE2 拆 uop | rvv_backend.sv:367/408 | `LCMD_t`/`UOP_QUEUE_t` | ? |
| 8 | Dispatch 派发到 ALU_RS | rvv_backend.sv:470/524 | `rs_dp2alu` | ? |
| 9 | ALU 执行 → ARB → ROB | rvv_backend.sv:857/1041/1059 | `PU2ROB_t` | ? |
| 10 | Retire 写回 VRF | rvv_backend.sv:1089/1138 | `wr_data_rt2vrf` | ? |
| 11 | （若 `vmv.x.s`）标量回写 | RvvCore.sv:179 / SCore.scala:282 | `async_rd` | 后端→标量核 |

**进阶思考**（不必写代码，口头回答）：

- 在第 6 步，如果命令队列快满了，反压信号怎么传回标量核？（提示：`queue_capacity` → Shim → `io.queue_capacity` → `dispatch.io.rvvQueueCapacity`）
- 在第 9 步，`vadd` 在 `LMUL=1` 下会展开成几个 uop？在 `LMUL=2` 下呢？（提示：与 `VLEN/SEW` 和 LMUL 有关，u7-l6 详讲）

**预期产出**：一张填满的追踪表 + 一张 Chisel 前端与 SV 后端的模块边界图（手画即可，标注 `RvvCoreIO` 这条「边界线」上跨过的所有信号组）。

## 6. 本讲小结

- **RVV 后端是「被驱动的协处理器」**：标量核每周期最多投递 `instructionLanes`(=4) 条向量指令，二者用解耦的命令队列协作，标量核不直接参与向量执行。
- **边界就是一个 Bundle**：`RvvCoreIO`（[RvvInterface.scala:61](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/rvv/RvvInterface.scala#L61)）——指令下行、操作数下行、结果/异常上行、CSR 同步、反压，全部经它流动。
- **Chisel 侧三件套**：`RvvCore`(Shim/Wrapper/BlackBox) 做翻译与资源登记、`RvvInterface` 定契约、`RvvDecode` 把 32b RVV 指令**压缩**成 2b opcode+25b bits 跨边界。
- **SV 侧两段式**：`RvvFrontEnd` 把压缩指令+操作数+配置状态装配成自洽的 `RVVCmd`（含 vsetvli 处理）；`rvv_backend` 是「命令队列→两级译码→uop 队列→Dispatch→保留站→执行单元→ARB→ROB→Retire→VRF」的乱序执行、按序退休流水线。
- **资源模型有文档/实现差**：overview 设计意图是 64×256b 向量寄存器 + acc\<8\>\<8\> + 256 MACs/周期；当前 RTL 全部按 `VLEN_128` 构建，实际是 32×128b、5b 索引、128 MACs/周期。读源码以 RTL 为准，设计数字用于建立直觉。
- **真正的向量译码在 SV**：Chisel 侧只做压缩与透传，`rvv_backend_decode`/`rvv_backend_decode_de2` 才是把一条向量指令拆成 uop 的地方（u7-l2 详讲）。

## 7. 下一步学习建议

本讲只搭了「骨架与边界」。后续讲义会逐层填肉：

- **u7-l2（RVV 译码与派发）**：精读 `rvv_backend_decode.sv` / `rvv_backend_dispatch.sv` 与 `rvv_backend_opcode.svh`，看一条向量指令如何被拆成 uop、结构冒险如何检测、操作数如何旁路。
- **u7-l3（VRF / ROB / Retire）**：精读 `rvv_backend_vrf.sv` / `rvv_backend_rob.sv` / `rvv_backend_retire.sv`，看 32×128b 向量寄存器堆的读写口与乱序执行的按序提交。
- **u7-l4（MAC 外积引擎）**：精读 `rvv_backend_mulmac.sv`，看 wide×narrow 外积广播如何产生每周期 128/256 MACs、acc\<8\>\<8\> 如何被更新。
- **u7-l5（向量 ALU/浮点/除法）** 与 **u7-l6（Stripmining 与编码）**：补齐算术单元与 stripmine 展开的细节。

建议在读 u7-l2 之前，先把本讲「综合实践」的追踪表亲手填一遍——它能把本讲建立的全景图固化为肌肉记忆。
