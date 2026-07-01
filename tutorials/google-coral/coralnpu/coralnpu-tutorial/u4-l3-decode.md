# 讲义 u4-l3：指令译码

## 1. 本讲目标

上一讲（u4-l2）我们看到了取指单元如何每周期把一束 32 位原始指令送到派发端。但这些 `0`/`1` 序列本身没有任何“含义”——硬件并不知道哪几位是操作码、哪个寄存器是源、哪个是目的。**译码（Decode）** 就是把“原始 32 位编码”翻译成“一束可被硬件直接使用的控制信号”的过程。

学完本讲，你应当能够：

1. 说清楚 CoralNPU 如何用 `BitPat` 从一条 32 位指令里识别出它是哪一条具体指令，并提取出 6 种格式的立即数。
2. 读懂 `DecodedInstruction` 这个数据结构：它用一束布尔位（one-hot 风格）+ 一组辅助方法描述一条指令的全部属性。
3. 解释译码结果如何驱动**派发**：派发器根据译码布尔位，用 `SafeMuxUpTo1H` 选出该指令要送给哪个执行单元（ALU/BRU/MLU/DVU/LSU/CSR…），以及产生什么样的操作命令。
4. 解释操作数的来源：寄存器读取 vs. 立即数注入（`rs1Set` / `rs2Set`）。
5. 亲手在源码里追踪一条 `addi` 和一条 `lw` 的完整译码路径。

本讲全部内容都围绕一个文件展开：[`hdl/chisel/src/coralnpu/scalar/Decode.scala`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala)。

## 2. 前置知识

在进入源码前，先用三段话把几个关键概念讲清楚。

### 2.1 RISC-V 指令的“字段化”布局

RISC-V 的基础指令全是定长 32 位，不同指令类型（R/I/S/B/U/J）只是把 32 位切成不同的字段。但有几个字段位置在几乎所有类型里是固定的，这是 RISC-V 设计上的巧思：

- **opcode（操作码）**：固定在最低 7 位 `inst[6:0]`。
- **rd（目的寄存器号）**：固定在 `inst[11:7]`，共 5 位（编码 32 个寄存器 `x0..x31`）。
- **funct3**：固定在 `inst[14:12]`，3 位，用来在同一个 opcode 下细分。
- **rs1（第一源寄存器号）**：固定在 `inst[19:15]`，5 位。
- **rs2（第二源寄存器号）**：固定在 `inst[24:20]`，5 位（仅 R/S/B 类型用到）。
- 高位字段（`inst[31:25]` 或 `inst[31:20]`）在不同类型里含义不同：可能是立即数、可能是 `funct7`。

> 译码的第一步，本质就是“按字段切片 + 模式匹配”。固定字段让 `rd/rs1/rs2` 的提取对所有指令通用，而 `opcode+funct3(+funct7)` 的组合则唯一确定指令身份。

### 2.2 Chisel 的 `BitPat` 与 `ChiselEnum`

- **`BitPat("b...")`** 是 Chisel 提供的“带通配位的二进制字面量”。字符串里 `0`/`1` 表示必须精确匹配的位，`?` 表示“任意值（don't care）”。于是 `op === BitPat("b????????????_?????_000_?????_0010011")` 就在读作：高 12 位任意、rs1 任意、funct3 必须是 `000`、rd 任意、opcode 必须是 `0010011`——这正是 `addi` 的特征。
- **`ChiselEnum`** 是 Chisel 的枚举类型，每个 `Value` 会自动分配一个整数编码。本讲会看到 `AluOp.ADD`、`LsuOp.LW` 等，它们就是用 `ChiselEnum` 定义的硬件枚举（见 [`Alu.scala` 的 `object AluOp`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Alu.scala#L30-L61)）。

### 2.3 `Valid` 接口与“至多一热”选择器

- **`Valid[T]`** 是一个带 `valid: Bool` 和 `bits: T` 的简单握手接口（没有 `ready`，单向有效）。本讲里 `io.alu(i)` 就是 `Valid(new AluCmd(p))`——派发器在某周期把 `valid` 拉高、`bits` 填上命令，ALU 下一周期就执行。
- **`SafeMuxUpTo1H`**（定义在 [`common/Library.scala:251`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/Library.scala#L251-L271)）是本讲反复出现的工具：它接收一串 `(条件, 命令)` 对和一个默认值，要求**至多一个条件为真**，然后输出那个被选中的命令。它比 `MuxCase` 生成的链式 mux 更快（生成 mux 树）。因为译码结果是 one-hot 的（同一条指令只会命中一个分支），用它正合适。

> 一句话总结前置知识：译码 = “按字段切 + `BitPat` 匹配 → 一束布尔位”；派发 = “用这束布尔位在 `SafeMuxUpTo1H` 里选出一条 `ChiselEnum` 命令送给对应执行单元”。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲如何使用 |
|------|------|--------------|
| `hdl/chisel/src/coralnpu/scalar/Decode.scala` | **本讲主角**。包含译码数据结构 `DecodedInstruction`、译码函数 `DecodeInstruction.apply`，以及把译码结果派发到各执行单元的 `DispatchV2` | 全部精读 |
| `hdl/chisel/src/coralnpu/scalar/Alu.scala` | ALU 执行单元，定义了 `AluOp` 枚举与 `ADD/LUI` 等运算语义 | 理解译码产出的 `AluOp` 如何被执行 |
| `hdl/chisel/src/common/Library.scala` | `SafeMuxUpTo1H` / `MakeValid` 工具 | 理解派发选择的实现机制 |
| `hdl/chisel/src/coralnpu/scalar/SCore.scala` | 标量核顶层，把 `dispatch.io.alu(i)` 连到 `alu(i).io.req` | 理解派发输出在核内的去向 |
| `doc/microarch/dispatch.md` | 派发规则文档（in-order、冒险、执行单元约束、控制流） | 理解 `canDispatch` 的设计动机 |

## 4. 核心概念与源码讲解

本讲把 `Decode.scala` 拆成 4 个最小模块：

- **4.1 译码的产出：`DecodedInstruction` 数据结构**
- **4.2 指令识别：`BitPat` 模式匹配与立即数生成**
- **4.3 派发选择：从布尔信号到执行单元命令**
- **4.4 操作数来源：寄存器读取与立即数注入**

### 4.1 译码的产出：`DecodedInstruction` 数据结构

#### 4.1.1 概念说明

译码器要把一条指令“翻译”成什么？CoralNPU 的答案是：一个名为 `DecodedInstruction` 的 **Bundle（硬件结构体）**，它几乎是“一条指令所有属性的平面化清单”。它的设计哲学是 **“one-hot 布尔位 + 辅助方法”**：

- 对每一条具体指令（`addi`、`lw`、`mul`、`beq`……）各设一个 `Bool()` 字段。一条合法指令译码后，**恰好有一个这样的布尔位为真**（其余为假）。
- 再附上几个“立即数”字段（不同指令类型共用）。
- 最后提供一组**辅助方法**（`isAlu()`、`isLsu()`、`readsRs1()`…），把零散的布尔位聚合成语义更高级的判断，供派发器和记分板复用。

> 为什么用 one-hot 布尔位而不是“一个枚举表示指令类型”？因为下游派发需要做大量组合判断（“这条指令是不是访存？”“它读不读 rs2？”），用独立的布尔位可以让这些判断都是简单的 `||`，综合成简单的与/或门，时序友好。代价是字段多，但每个判断都极其直观。

#### 4.1.2 核心流程

`DecodedInstruction` 本身只是“数据格式定义”，它不自己填值——填值由 4.2 的 `DecodeInstruction.apply` 完成。它的生命周期是：

```
32 位原始 inst
   │  DecodeInstruction.apply(...)   （4.2）
   ▼
DecodedInstruction（一束布尔位 + 立即数 + 辅助方法）
   │  DispatchV2 读取它               （4.3 / 4.4）
   ▼
送给 ALU/BRU/LSU/... 的命令 + 寄存器读/写地址
```

#### 4.1.3 源码精读

数据结构定义在 [`Decode.scala:29-214`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L29-L214)。它分四块：

1. **原始编码与立即数**（[:31-39](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L31-L39)）：`inst` 保留原始 32 位；`imm12/imm20/immjal/immbr/immcsr/immst` 是 6 种立即数格式，全部预扩展成 32 位，按需取用。

2. **指令布尔位**（[:42-125](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L42-L125)）：按扩展集分组——RV32I（`lui/addi/add/lw/...`）、RV32M（`mul/div/...`）、ZBB 位操作（`clz/min/rol/...`）、核控制（`ebreak/mret/wfi/...`）、栅障（`fencei/flushat/...`）。注意 [:127-129](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L127-L129) 的 `rvv`/`float` 是 **`Option`**：只有当参数里 `enableRvv`/`enableFloat` 打开时才存在，体现了“可配置核”的设计。

3. **辅助分类方法**（[:131-163](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L131-L163)）：把布尔位聚合成语义判断。例如：

```scala
def isAluImm(): Bool = { addi || slti || ... || srai || rori }
def isAlu():    Bool = { isAluImm() || isAluReg() || isAlu1Bit() || isAlu2Bit() }
def isLsu():    Bool = { isScalarLoad() || isScalarStore() || flushat || flushall ||
                         isFloatLoad() || isFloatStore() || (rvv... isLoadStore()) }
```

   [`isLsu()`（:153-160）](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L153-L160) 尤其值得注意：标量 load/store、栅障刷写、浮点 load/store、**以及 RVV 向量 load/store** 全都归入 LSU——这解释了为什么 LSU 是核里最复杂的单元（见下一单元 u6-l1）。

4. **派发约束方法**（[:167-213](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L167-L213)）：描述这条指令对派发器的特殊要求，例如 [`forceSlot0Only()`（:167-169）](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L167-L169) 表示“只能在第 0 槽发射、且同周期不许发射别的”，`isJump()`、`readsRs1()`、`rs1Set()` 等会在 4.3/4.4 详述。

#### 4.1.4 代码实践

**实践目标**：建立对 `DecodedInstruction` 字段规模与分组的直观感受。

**操作步骤**：

1. 打开 [`Decode.scala:29-214`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L29-L214)。
2. 数一数：布尔位字段一共多少个？按“RV32I / RV32M / ZBB / 核控制 / 栅障”分成几组、各组几个？
3. 找到 `isAlu()`、`isMul()`、`isDvu()`、`isCsr()` 四个方法，分别列出它们各自“吃掉”了哪些具体指令布尔位。

**需要观察的现象**：你会发现布尔位远不止 6 个（ALU/BRU/MLU/DVU/LSU/CSR）——“指令身份”的粒度比“执行单元”细得多；辅助方法就是把细粒度的指令身份**归约**成粗粒度的执行单元归属。

**预期结果**：能口述“`isAlu()` 把约 30 条 ALU 类指令归为一类，`isLsu()` 把标量/浮点/向量的访存指令都揽进来”。

### 4.2 指令识别：`BitPat` 模式匹配与立即数生成

#### 4.2.1 概念说明

`DecodedInstruction` 只定义了“长什么样”，真正给它填值的是 `DecodeInstruction.apply` 这个工厂函数（[`Decode.scala:821-983`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L821-L983)）。它做三件事：

1. **算立即数**：根据 RISC-V 的 6 种立即数格式，把散落的字段拼成 32 位符号扩展值。
2. **匹配指令身份**：对每条指令写一行 `d.xxx := op === BitPat(...)`。
3. **判非法**：若没有任何布尔位命中，则 `undef = true`（非法指令）。

#### 4.2.2 核心流程

立即数拼接是 RISC-V 初学者最容易绕晕的地方，因为同一个立即数在 32 位里的位置因类型而异。下表给出 CoralNPU 的 6 种立即数及其在源码里的拼接方式（见 [:828-834](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L828-L834)）：

| 立即数字段 | 用于 | 符号扩展？ | 拼接（源码行） |
|-----------|------|-----------|----------------|
| `imm12`（I 型） | `addi/lw/...` 的 12 位立即数 | 是，复制 `op(31)` 共 20 次 | [:829](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L829) `Cat(Fill(20,op(31)), op(31,20))` |
| `imm20`（U 型） | `lui/auipc` 的高 20 位 | 否，低 12 位补 0 | [:830](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L830) `Cat(op(31,12), 0.U(12.W))` |
| `immjal`（J 型） | `jal` 跳转偏移 | 是 | [:831](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L831) |
| `immbr`（B 型） | `beq/bne/...` 分支偏移 | 是 | [:832](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L832) |
| `immcsr`（CSR） | CSR 立即数型的 5 位 rs1 字段当立即数 | 否（5 位零扩展） | [:833](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L833) `op(19,15)` |
| `immst`（S 型） | `sw/sh/sb` 的 12 位立即数 | 是 | [:834](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L834) |

> 关键直觉：分支/跳转偏移总是 2 字节对齐，所以 `immjal`/`immbr` 末尾恒补一个 `0` 位（即偏移左移 1 位）。

I 型立即数的符号扩展数学上可写作：

\[
\text{imm12} = \text{sign\_extend}_{32}\big(\text{inst}[31:20]\big)
\]

即把最高位 `inst[31]` 复制 20 份拼到高位。

#### 4.2.3 源码精读

`BitPat` 匹配集中在 [:837-919](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L837-L919)。本讲要重点对照的两条指令：

```scala
// addi: I 型算术立即数 —— opcode=0010011, funct3=000
d.addi := op === BitPat("b????????????_?????_000_?????_0010011")   // :859

// lw: I 型加载 —— opcode=0000011, funct3=010
d.lw   := op === BitPat("b????????????_?????_010_?????_0000011")   // :852
```

读法：下划线只是方便阅读的分隔符，从右到左依次是 `opcode(7) | rd(5) | funct3(3) | rs1(5) | 高位(12)`。两者的区别仅在 `funct3`（`000` vs `010`）和 `opcode`（`0010011` vs `0000011`）。

注意几个**特殊处理**：

- **pipeline > 0 的槽位屏蔽**（[:929-953](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L929-L953)）：CSR、除法、核控制（`ebreak/mret/...`）、栅障、浮点这些“只能在第 0 槽发射”的指令，在非 0 槽里被显式置为 `false.B`。这样即便它们出现在高槽位也不会被误派发。这是用译码阶段就强制实现“slot-0 only”约束的巧思。

- **非法指令检测**（[:960-979](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L960-L979)：把所有布尔位 `Cat` 成一个大数，若全 0 则 `d.undef := true`：

```scala
val decoded = Cat(d.lui, d.auipc, d.jal, ... , d.float.map(_.valid).getOrElse(false.B))
d.undef := decoded === 0.U
```

  `undef` 会在派发器里触发 `undefFault`（非法指令异常），交给 [`FaultManager`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FaultManager.scala) 处理。

#### 4.2.4 代码实践

**实践目标**：亲手把一条 `addi` 的 32 位编码对到 `BitPat` 上，验证字段切片。

**操作步骤**：

1. 取 `addi x15, x14, -1`。其编码为：`imm[11:0]=111111111111` | `rs1=01110` | `funct3=000` | `rd=01111` | `opcode=0010011`，即 `0xFFF50713`。
2. 对照 [:859](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L859) 的 `BitPat`，逐段确认：高 12 位（立即数）是 `?` 任意 ✓；`rs1=01110` 对应 `?` ✓；`funct3=000` 精确匹配 ✓；`rd=01111` 对应 `?` ✓；`opcode=0010011` 精确匹配 ✓。故 `d.addi=true`。
3. 用 [:829](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L829) 计算 `imm12 = Cat(Fill(20,op(31)), op(31,20))`：`op(31)=1` → 高 20 位全 1，`op(31,20)=111111111111` → 结果 `0xFFFFFFFF`（即 -1）。

**需要观察的现象**：`addi` 的 12 位立即数 `-1` 经符号扩展变成 32 位的 `0xFFFFFFFF`，可直接参与后续加法。

**预期结果**：能独立把 `lw x15, 0(x14)`（编码 `0x0007A283`，请自行核对）对到 [:852](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L852) 的 `BitPat` 上，并得出 `imm12=0`。

#### 4.2.5 小练习与答案

**练习 1**：`srai`（算术右移立即数）的 `BitPat` 在 [:867](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L867)，它是 `b0100000_?????_?????_101_?????_0010011`。它和 `srli`（[:866](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L866)）只差哪一位？这一位对应 RISC-V 的什么字段？

**答案**：只差最高 7 位里的第 30 位（`funct7` 的 bit5）：`srli` 是 `0000000`、`srai` 是 `0100000`。这一位就是 RISC-V 里区分“逻辑右移 / 算术右移”的 `funct7[5]`（也叫 arithmetic 位）。

**练习 2**：为什么 `immcsr` 直接取 `op(19,15)` 而不需要符号扩展？

**答案**：CSR 的立即数型指令（`csrrwi/csrrsi/csrrci`）把 `rs1` 字段（5 位）当作一个 **5 位无符号** 立即数（取值 0–31），所以直接取 `op(19,15)` 即可，无需符号扩展。

### 4.3 派发选择：从布尔信号到执行单元命令

#### 4.3.1 概念说明

译码本身只回答“这是条什么指令”。但核里有多个执行单元（ALU/BRU/MLU/DVU/LSU/CSR/RVV/FPU），一条指令该送给谁、产生什么命令？这个“翻译”由派发器 `DispatchV2` 完成（[`Decode.scala:301-819`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L301-L819)）。

派发器对每个发射槽（lane，共 `instructionLanes` 个）做两件事：

1. **能不能派发**（`canDispatch`）：综合 in-order、记分板冒险、执行单元约束、控制流屏障、槽位约束等一大堆条件。
2. **派发给谁**：用 `SafeMuxUpTo1H` 把布尔位翻译成对应执行单元的 `ChiselEnum` 命令，并通过 `lastReady` 链条把 in-order 反压传到上游。

本模块聚焦第 2 步（“派发给谁”），第 1 步（“能不能”）的记分板细节留到下一讲 u4-l4，这里只点出关键。

#### 4.3.2 核心流程

`DispatchV2` 先对每个槽并行译码（[:303-306](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L303-L306)）：

```scala
val decodedInsts = (0 until p.instructionLanes).map(i =>
  DecodeInstruction(p, i, io.inst(i).bits.addr, io.inst(i).bits.inst, io.csrFrm.getOrElse(0.U)))
```

然后对每个槽，依次为 ALU/BRU/MLU/DVU/LSU/CSR 构造命令。以 ALU 为例（[:533-568](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L533-L568)），核心是：

```scala
val alu = SafeMuxUpTo1H(MakeValid(false.B, AluOp.ADD), Seq(
  (d.auipc || d.addi || d.add) -> MakeValid(true.B, AluOp.ADD),
  d.sub                        -> MakeValid(true.B, AluOp.SUB),
  d.slli || d.sll              -> MakeValid(true.B, AluOp.SLL),
  ...
), AluOp)
io.alu(i).valid   := tryDispatch && alu.valid
io.alu(i).bits.op := alu.bits
```

含义：在所有 `(布尔位 → AluOp)` 里至多选一个；若该指令不是 ALU 类，则 `alu.valid=false`（用默认值 `AluOp.ADD` 占位但不发射）。`tryDispatch` 才是真正决定“这一槽本周期是否发射”的总闸。

派发的总闸 `canDispatch`（[:497-520](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L497-L520)）是十几个条件的与，对应 [`dispatch.md`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/dispatch.md) 里的规则：

- `!jumped(i)` —— in-order：前面有跳转就不派发后续（控制流屏障）。
- `!readAfterWrite(i) && !writeAfterWrite(i)` —— 记分板：避免 RAW/WAW 冒险。
- `slot0Interlock(i)` —— `forceSlot0Only()` 的指令只走第 0 槽（[:398-404](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L398-L404)）。
- `lsuInterlock(i)` / `rvvInterlock(i)` —— LSU/RVV 队列容量约束。
- `(!d.isCsr() || io.retirement_buffer_empty)` —— CSR 必须等 ROB 清空才能执行。

> 一个微妙点：`tryDispatch = lastReady(i) && canDispatch(i)`（[:528](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L528)）。`lastReady` 是从槽 0 往后传递的链条：只有前一槽“真的发射出去了（`fire`）”或本就是槽 0，本槽才允许尝试派发——这就是 **in-order 派发**在电路上的实现。

派发出去的命令在核顶层 [`SCore.scala:167-171`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L167-L171) 被接到 ALU 的请求端口：

```scala
for (i <- 0 until p.instructionLanes) {
  alu(i).io.req := dispatch.io.alu(i)   // 译码产出的 AluCmd 直接喂给 ALU
  alu(i).io.rs1 := regfile.io.readData(2*i + 0)
  alu(i).io.rs2 := regfile.io.readData(2*i + 1)
}
```

#### 4.3.3 源码精读：追踪 `addi` 的派发

接 4.2 的 `addi x15, x14, -1`（`d.addi=true`）：

1. 在 ALU 的 `SafeMuxUpTo1H` 里命中 [:535](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L535)：`(d.auipc || d.addi || d.add) -> MakeValid(true.B, AluOp.ADD)` → `alu.valid=true`，`alu.bits=AluOp.ADD`。
2. [`io.alu(i).valid := tryDispatch && alu.valid`（:566）](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L566)：若本槽可派发，则把 `AluCmd{addr=15, op=ADD}` 送给 ALU。
3. [`io.alu(i).bits.addr := rdAddr(i)`（:567）](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L567)，其中 `rdAddr = inst(11,7)`（[:331](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L331)）= 15，即目的寄存器 `x15`。
4. 在 ALU 执行端（[`Alu.scala:119-130`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Alu.scala#L119-L130)）：`AluOp.ADD -> (rs1 + rs2)`，即 `x14 + rs2`。`rs2` 是什么？见 4.4——它正是立即数 `-1`。所以 `addi` 复用了 `add` 的 ADD 运算，区别只在第二个操作数的来源（寄存器 vs. 立即数）。

> 这就是 CoralNPU 译码/派发设计上最优雅的一点：**`add` 和 `addi` 在 ALU 里是同一条 `AluOp.ADD` 运算**，立即数版本的差异完全被“操作数注入”机制（4.4）吸收掉了，ALU 硬件无需为立即数单独做加法器。

#### 4.3.4 代码实践

**实践目标**：在源码里验证“`addi` 走 ALU 的 ADD，而非一条独立的立即数运算”。

**操作步骤**：

1. 打开 [`Decode.scala:533-568`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L533-L568)，确认 `d.addi` 与 `d.add`、`d.auipc` 共享同一个 `AluOp.ADD` 分支。
2. 打开 [`Alu.scala:119-130`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Alu.scala#L119-L130)，确认 `AluOp.ADD -> (rs1 + rs2)`。
3. 回答：`auipc`（PC + 上立即数）也命中这个 ADD 分支，那么它的 `rs1` 和 `rs2` 分别从哪来？（提示：见 4.4 的 `rs1Set`/`rs2Set`。）

**需要观察的现象**：三条语义不同的指令（`add`/`addi`/`auipc`）共用同一个 ALU 运算，差异全在操作数来源。

**预期结果**：能写出 `addi` 的派发结论——`{单元:ALU, op:ADD, rd:x15, rs1:x14(寄存器), rs2:-1(立即数)}`。

#### 4.3.5 小练习与答案

**练习 1**：`mul`（`d.mul=true`）会被派发给哪个执行单元？看 [:610-618](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L610-L618)，它和 ALU 派发在“命令格式”上有什么结构共性？

**答案**：派发给 MLU（乘法单元），产生 `MluOp.MUL`。结构共性是：都用 `SafeMuxUpTo1H(MakeValid(false.B, 默认枚举), Seq(布尔位 -> MakeValid(true.B, 枚举值)), 枚举对象)` 这个固定模板——译码布尔位直接当选择条件，命中即产出对应枚举命令。

**练习 2**：为什么 `canDispatch`（[:497-520](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L497-L520)）里要有 `(i.U < io.retirement_buffer_nSpace)` 这一项？

**答案**：这是 in-order 退休缓冲（ROB）的空间约束——每个发射槽都需要 ROB 里预留一个表项来记录它，若 ROB 剩余空间不够容纳到第 i 槽，则第 i 槽不能派发。这保证每条被派发的指令都有地方记录其退休状态。

### 4.4 操作数来源：寄存器读取与立即数注入

#### 4.4.1 概念说明

执行单元需要操作数（如 ALU 的 `rs1`、`rs2`）。操作数有两个来源：

1. **从寄存器堆读**：按 `rs1`/`rs2` 字段（`inst[19:15]`/`inst[24:20]`）去整数寄存器堆取值。
2. **由译码器“注入”立即数**：对立即数指令，把某个操作数直接设成译码算出的立即数，而不去读寄存器。

CoralNPU 用两个关键方法表达“注入”：

- [`rs1Set()`（:212）](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L212)：`auipc || isCsrImm()` —— 为真时，`rs1` 用立即数（PC 或 CSR 立即数）代替寄存器值。
- [`rs2Set()`（:213）](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L213)：`rs1Set() || isAluImm() || isAlu1Bit() || lui` —— 为真时，`rs2` 用立即数代替寄存器值。

这就是 4.3 提到的“操作数注入”机制。配合“ALU 永远算 `rs1 OP rs2`”，立即数指令就被统一成了“把立即数塞进 rs2 槽位”的普通二操作数运算。

#### 4.4.2 核心流程

寄存器读/写地址与立即数注入的接线在 [`Decode.scala:751-817`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L751-L817)，关键几行：

```scala
// rs1/rs2 寄存器读取（只在该指令真需要时才读）
io.rs1Read(i).valid := io.inst(i).fire && (d.readsRs1() || d.jalr)   // :754
io.rs2Read(i).valid := io.inst(i).fire && d.readsRs2()                // :756

// rs1/rs2 立即数注入
io.rs1Set(i).valid := io.inst(i).fire && d.rs1Set()                   // :760
io.rs1Set(i).value := Mux(d.isCsr(), d.immcsr, io.inst(i).bits.addr)  // :761  CSR立即数 / PC
io.rs2Set(i).valid := io.inst(i).fire && d.rs2Set()                   // :762
io.rs2Set(i).value := MuxCase(d.imm12, IndexedSeq((d.auipc||d.lui) -> d.imm20))  // :763
```

读法：

- `rs2Set.value` 默认是 `imm12`（I 型立即数），只有 `auipc`/`lui` 用 `imm20`（U 型）。所以 `addi` 的 `rs2` = `imm12` = -1，而 `lui` 的 `rs2` = `imm20`。
- `rs1Set.value` 对 CSR 立即数型取 `immcsr`，对 `auipc` 取 PC（`io.inst(i).bits.addr`）。

读端口的有效信号由 `readsRs1()`/`readsRs2()`（[:200-209](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L200-L209)）决定——它们是“这条指令语义上是否真正消费 rs1/rs2 寄存器值”的判断，把所有消费源的布尔位 `||` 起来。

> 一个反直觉但重要的点：`addi` 的 `readsRs2() == false`（它不读 rs2 寄存器），但它的 `rs2Set() == true`（rs2 是立即数）。也就是说“读寄存器 rs2”和“rs2 用立即数”是互斥的两条路径，由不同信号控制，最终都汇入 ALU 的 `rs2` 输入端。

#### 4.4.3 源码精读：追踪 `lw` 的操作数与派发

取 `lw x15, 0(x14)`（`d.lw=true`）：

1. **指令识别**：[:852](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L852) 命中，`imm12=0`（4.2 已算）。
2. **归类**：`isScalarLoad()=true`（[:144](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L144)），`isLsu()=true`（[:154](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L154)）。
3. **派发选择**：在 LSU 的 `SafeMuxUpTo1H` 里命中 [:637](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L637)：`d.lw -> MakeValid(true.B, LsuOp.LW)` → `LsuCmd{addr=15, op=LW, store=0}`。注意 [`io.lsu(i).bits.store := io.inst(i).bits.inst(5)`（:666）](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L666)：load 指令 opcode 的 bit5=0，故 `store=0`（是读不是写）。
4. **操作数（地址）来源**：LSU 的访存地址 = 基址 + 偏移。基址来自 `rs1=x14`（LSU 通过总线端口读寄存器，并用 `usesRs1Regd = d.isLsu()`（[:347](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L347)）在记分板里追踪 RAW 冒险）；偏移来自立即数 `imm12=0`，经 [`io.busRead(i).immed`（:815-817）](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L815-L817) 注入。所以 `lw` 的 `rs1Set/rs2Set` 都是 false（它不走 ALU 的注入路径，偏移由 LSU 自己处理）。
5. **目的寄存器**：`rd=x15` 接收加载结果。[:771-777](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L771-L777) 里 `rdMark_valid` 含 `io.lsu(i).fire && d.isScalarLoad()`，故 `x15` 被标记为待写。

> 小结 `lw` 的译码结论：`{单元:LSU, op:LW, rd:x15, rs1:x14(基址寄存器), 偏移立即数:0, store:false}`。与 `addi` 对比：`addi` 走 ALU 且用 `rs2Set` 注入立即数；`lw` 走 LSU 且偏移经 `busRead.immed` 注入——两者都体现了“译码器负责把立即数送到正确位置”这一统一思想。

#### 4.4.4 代码实践

**实践目标**：把 `addi` 与 `lw` 两条指令的译码结论整理成对照表，固化对“操作数来源”的理解。

**操作步骤**：

1. 准备两条指令：`addi x15, x14, -1` 与 `lw x15, 0(x14)`。
2. 在源码里逐项填表（每项给出 `Decode.scala` 的行号依据）：执行单元、`ChiselEnum` 操作、`rd`、`rs1` 来源、第二操作数来源、是否读 rs2 寄存器。
3. 重点对比：两者都“用到立即数”，但立即数分别经由 `rs2Set`（addi）和 `busRead.immed`（lw）两条不同路径注入。

**需要观察的现象**：`addi` 的 `readsRs2()=false` 但 `rs2Set()=true`；`lw` 的 `readsRs1()=false` 但 `usesRs1Regd=true`（基址走 LSU 总线端口）。

**预期结果**：得到如下结论表（供核对）：

| 维度 | `addi x15,x14,-1` | `lw x15,0(x14)` |
|------|-------------------|-----------------|
| 命中布尔位 | `d.addi`（:859） | `d.lw`（:852） |
| 执行单元 | ALU（:533-568） | LSU（:634-674） |
| 枚举命令 | `AluOp.ADD`（:535） | `LsuOp.LW`（:637） |
| `rd` | `x15`（`inst[11:7]`） | `x15`（`inst[11:7]`） |
| `rs1` 来源 | 寄存器 `x14`（`readsRs1`=true） | 寄存器 `x14`（基址，LSU 总线端口） |
| 第二操作数 | 立即数 `-1`，经 `rs2Set` 注入（:762-763） | 偏移立即数 `0`，经 `busRead.immed` 注入（:815-817） |
| 读 rs2 寄存器？ | 否（`readsRs2`=false） | 否 |

> 本表无法在编译期由工具自动生成，需读者手工核对源码；若某行你无法确定，请标注「待本地验证」并在仿真里用指令轨迹确认。

#### 4.4.5 小练习与答案

**练习 1**：`lui x5, 0x12345` 的 `rs2Set()` 是否为真？它的 `rs2` 立即数值是多少？结合 [:213](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L213) 与 [:763](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L763) 回答。

**答案**：为真（`lui` 在 `rs2Set()` 里）。`rs2` 立即数值 = `imm20 = 0x12345000`（高 20 位左移 12）。在 ALU 里 `lui` 命中 `AluOp.LUI`，而 [`Alu.scala:130`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Alu.scala#L130) 里 `AluOp.LUI -> rs2`，即结果直接取 `rs2`（立即数），写入 `x5`。

**练习 2**：为什么 `readsRs2()`（[:205-209](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L205-L209)）里包含 `isScalarStore()` 但**不**包含 `isScalarLoad()`？

**答案**：store 指令（`sw/sh/sb`）要把 `rs2` 寄存器里的值写进存储器，所以必须读 `rs2`；而 load 指令（`lw/...`）的数据流方向相反——从存储器读到 `rd`，不消费 `rs2`，故不读 `rs2`。

## 5. 综合实践

把本讲四个模块串起来，完成一次“从编码到派发”的完整追踪。

**任务**：任选一条本讲没详述的指令，完整复现 4.3/4.4 的追踪流程。建议从下面三条里挑一条（难度递增）：

1. `sw x5, 4(x6)` —— S 型 store，考察 `immst` 立即数与 LSU 的 `store=1`。
2. `srai x7, x8, 3` —— 立即数算术右移，考察 `funct7` 位与 `isAluImm()`。
3. `csrrw x0, mstatus, x5` —— CSR 指令，考察 `forceSlot0Only()`、`isCsr()` 与 [:678-699](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L678-L699) 的 CSR 派发。

**对每条指令，交付**：

1. 手工写出 32 位编码，并对到对应的 `BitPat` 行（给出行号）。
2. 列出 `DecodedInstruction` 里哪些布尔位为真、哪些立即数字段被使用及其值。
3. 指出它被派发给哪个执行单元、产生哪个 `ChiselEnum` 命令（给出 `SafeMuxUpTo1H` 行号）。
4. 说明 `rs1`/`rs2` 是来自寄存器读取还是立即数注入（引用 `readsRs1`/`readsRs2`/`rs1Set`/`rs2Set` 或 `busRead.immed`）。
5. （进阶）在 cocotb ISA 回归 [`tests/cocotb/coralnpu_isa`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/coralnpu_isa/)（目标 `core_mini_axi_coralnpu_isa_test`）里找到覆盖该指令的用例，运行并观察指令轨迹是否符合你的译码推断。若无法本地运行仿真，明确写「待本地验证」。

> 这个练习把“字段切片 → `BitPat` 识别 → 立即数生成 → 执行单元选择 → 操作数来源”整条链路走通，是理解任何处理器前端的标准方法。

## 6. 本讲小结

- **译码的本质**是把 32 位原始编码翻译成一束控制信号。CoralNPU 用 **one-hot 布尔位 + 立即数 + 辅助方法** 的 `DecodedInstruction` 承载结果，粒度细到每条具体指令。
- **指令识别**靠 `BitPat` 模式匹配，固定字段（`opcode/funct3/rd/rs1/rs2`）让匹配直观；6 种立即数在 `DecodeInstruction.apply` 里一次性预扩展成 32 位。
- **派发选择**用 `SafeMuxUpTo1H` 把布尔位翻译成各执行单元的 `ChiselEnum` 命令（`AluOp/LsuOp/MluOp/...`）；`add`/`addi`/`auipc` 共享 `AluOp.ADD`，差异全由操作数来源吸收。
- **操作数来源**由 `readsRs1/readsRs2`（读寄存器）与 `rs1Set/rs2Set`（注入立即数）两套信号控制；`addi` 经 `rs2Set` 注入立即数，`lw` 经 `busRead.immed` 注入偏移。
- **派发总闸** `canDispatch` 把 in-order、记分板冒险、槽位约束、ROB 空间等十几个条件与起来；`lastReady` 链实现 in-order 反压。
- **非法指令**通过“所有布尔位 `Cat` 后为 0 → `undef`”检测，触发 `undefFault`。

## 7. 下一步学习建议

本讲只解决了“这条指令送给谁、带什么操作数”，但刻意没深入两件事：

1. **“能不能派发”的冒险检测细节**——记分板如何避免 RAW/WAW、为何没有 WAR、ROB 如何乱序退休。这是下一讲 **u4-l4（派发规则、记分板与退休）** 的主题，重点读 [`doc/microarch/dispatch.md`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/dispatch.md) 与 [`RetirementBuffer.scala`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala)。
2. **各执行单元内部如何执行命令**——例如 ALU 如何算、LSU 如何访存。这分别进入第 5 单元（`Alu.scala`/`Bru.scala`/`Mlu.scala`/`Csr.scala`/...）和第 6 单元（`Lsu.scala`）。

建议在读 u4-l4 前，回到本讲的 4.3.3/4.4.3，确认你已经能独立追踪任意一条标量指令的“译码 → 派发”路径——这是后续所有执行单元讲义的共同入口。
