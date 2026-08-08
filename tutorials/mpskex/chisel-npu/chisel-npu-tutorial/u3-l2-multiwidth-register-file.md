# 多宽度寄存器堆 MultiWidthRegisterBlock

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `MultiWidthRegisterBlock` 的物理存储为什么是 \(L \times K \times N/8\) 字节，以及 VX/VE/VR 为什么是「同一块存储的三种视图」而非三块独立存储。
- 手推「写 VR[i] 原子更新底层 4 行 VX」的字节级别名机制，并反推读 VE/VR 时的位拼接顺序。
- 说出 vx/ve/vr 各类寄存器的读写端口数量与各自用途，并用 `log2Ceil` 算出每类寄存器的地址位宽。
- 解释写优先级 `VR > VE > VX > ext` 是如何用 Chisel「最后连接胜出」（last-connect-wins）实现的，以及它和「last writer wins / 软件负责」的关系。
- 解释为什么 `ext_r_addr` 即使不使用也必须被驱动，否则 firtool 报 `uninitialized sink`。

## 2. 前置知识

本讲是上一讲 [u3-l1 微操作与控制 Bundle](u3-l1-microop-and-ctrl-bundles.md) 的承接，我们继续在「译码器 → 执行单元」之间打转，但这次的对象是**存储**而非**控制**。

在进入源码前，先回顾几个前置概念（它们在 [u1-l4 全局参数 N/L/K 与寄存器类概念](u1-l4-params-nlk-and-regclass.md) 已建立，这里只做最小复述）：

- **N(bits)**：基础通道位宽，默认 8。NPU 的 INT8 数据就是一个 8 位的 lane。
- **L**：VX 寄存器的数量，默认 32，且**必须被 4 整除**。
- **K**：每条寄存器里的 SIMD 通道数（lane 数），测试态默认 8。
- **VX / VE / VR**：三类「寄存器」。本讲会证明它们不是三块存储，而是同一块物理字节数组的三种**别名视角**（alias view）。

另外需要两个 Chisel 基础知识：

- **`Vec`**：Chisel 里可索引的、同类型元素的硬件数组。本讲里 `mem` 是一个 `Vec(L, Vec(K, UInt(N.W)))`，即「L 行 × K lane × N 位」的三维结构。
- **`log2Ceil(n)`**：返回至少能编码 `n` 个地址所需的位数，即 \(\lceil \log_2 n \rceil\)。例如 `log2Ceil(32)=5`、`log2Ceil(16)=4`、`log2Ceil(8)=3`。
- **最后连接胜出（last-connect-wins）**：Chisel 里对同一个硬件节点多次用 `:=` 赋值时，**后面那条连接覆盖前面那条**。本讲的写优先级正是靠这条语义实现的。

## 3. 本讲源码地图

| 文件 | 作用 |
|:---|:---|
| [src/main/scala/sram/multiWidthRegister.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala) | 本讲主角。`MultiWidthRegisterBlock` 的全部 RTL，含参数、IO Bundle、异步读、同步写与优先级。 |
| [src/main/scala/backend/SimpleBackend.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala) | `NCoreBackend` 实例化寄存器堆、分配端口、驱动 `ext_r_addr` 的真实现场。 |
| [src/test/scala/sram/MultiWidthRegisterSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/sram/MultiWidthRegisterSpec.scala) | 验证 VX/VE/VR 别名关系与 ext 读写端口测试，是本讲代码实践的模板。 |
| [docs/implementations/Registers.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/Registers.md) | 官方文档：字节布局图、端口表、后端端口分配表、ext 陷阱告警。 |

> 顺带一提：`src/main/scala/sram/register.scala` 里还有一个**旧的**单宽度 `RegisterBlock`（多 bank、单一宽度），它只被独立的 MMALU/SA 测试（如 `MMALUSpec`）使用，`NCoreBackend` 并不用它。本讲只讲新的多宽度版本。

---

## 4. 核心概念与源码讲解

### 4.1 MultiWidthRegisterBlock：物理存储与三类视图

#### 4.1.1 概念说明

NPU 同时要做三类宽度相差很大的运算：

- 矩阵乘（MMALU）累加出 **32 位** INT32 结果；
- 向量 ALU（VALU）做 **16 位** INT16 算术逻辑；
- 同一个 VALU 还要做 **8 位** INT8 逐通道运算。

如果给每种宽度各开一块寄存器堆，不仅要复制存储、还要在三者之间来回搬运数据（量化流水线里 MMALU 的 INT32 要立刻交给 VALU 做缩放）。`MultiWidthRegisterBlock` 的解法是：**只建一块「以字节为粒度」的物理存储，然后用三种不同的「镜头」去看它**——

- **VX 视角**：把每行看作 K 个 N 位 lane（INT8）。
- **VE 视角**：把相邻 2 行拼起来，看成 K 个 2N 位 lane（INT16）。
- **VR 视角**：把相邻 4 行拼起来，看成 K 个 4N 位 lane（INT32 / FP32）。

因为拼接是**以字节为单位**对齐的，所以三种视角天然共享同一批字节，不需要任何数据拷贝。这就是「多宽度别名（multi-width aliasing）」的核心思想。

#### 4.1.2 核心流程

物理存储规模为：

\[
\text{Physical bytes} = L \times K \times \frac{N}{8}
\]

测试默认 \(L=32, K=8, N=8\)，代入得 \(32 \times 8 \times 1 = 256\) 字节。三类视角各自「展开」后也都是 256 字节，正好印证它们共享同一块存储：

| 视角 | 寄存器数 | 每 lane 位宽 | 每寄存器位宽 | 总位数 |
|:---|:---:|:---:|:---:|:---:|
| VX | \(L\) | \(N\) | \(K \times N\) | \(L \cdot K \cdot N\) |
| VE | \(L/2\) | \(2N\) | \(K \times 2N\) | \((L/2) \cdot K \cdot 2N = LKN\) |
| VR | \(L/4\) | \(4N\) | \(K \times 4N\) | \((L/4) \cdot K \cdot 4N = LKN\) |

三者总位数都等于 \(LKN\)，所以它们必然落在同一块物理存储上。别名关系为：

\[
\text{VE}[i] = \text{VX}[2i] \;\|\; \text{VX}[2i+1]
\]

\[
\text{VR}[i] = \text{VX}[4i+0] \;\|\; \text{VX}[4i+1] \;\|\; \text{VX}[4i+2] \;\|\; \text{VX}[4i+3]
\]

由此立刻得出两条**后果**：

1. 写 **VR[i]** 会一次性改动底层 4 行 VX（也连带改动 VE[2i]、VE[2i+1]）——这就是「原子更新 4 行」。
2. 因为 VR 需要对齐到 4 行边界，**L 必须能被 4 整除**，否则 VR 视角会出现「半个寄存器」。这正是代码里 `require(L % 4 == 0)` 的根本原因。

#### 4.1.3 源码精读

类的参数与不变量约束在文件头部——`require` 在 elaborate 时强制 `L` 被 4 整除，并计算 VE/VR 的数量：

- [multiWidthRegister.scala:37-49](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L37-L49)：`class MultiWidthRegisterBlock` 的 9 个参数（L/K/N 加 6 个端口数），以及 `require(L % 4 == 0, ...)` 这条断言。
- [multiWidthRegister.scala:52-53](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L52-L53)：`VE_SIZE = L/2`、`VR_SIZE = L/4`，决定了 VE/VR 各自的寄存器数量。

物理存储本身只有一行——一个「L 行 × K lane × N 位」的寄存器数组，每一行就是一条 VX：

- [multiWidthRegister.scala:89-91](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L89-L91)：`val mem = RegInit(VecInit(Seq.fill(L)(VecInit(Seq.fill(K)(0.U(N.W))))))`。注意整块寄存器堆**只有这一个 `mem`**，VX/VE/VR 全部由它派生，没有任何第二块存储。

参数 `L/K/N` 的默认值与文档记号表完全一致，参见 [Registers.md 的 Notation 小节](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/Registers.md#L13)（文档里还给出上板态 K=64、总存储 2 KiB 的数值）。

#### 4.1.4 代码实践

**目标**：用纸笔验证「三种视角共享 256 字节」，并把别名下标公式套到一个具体例子上。

**步骤**：

1. 取测试默认参数 \(L=32, K=8, N=8\)，分别算出 VX/VE/VR 的寄存器数量与每寄存器位宽。
2. 验证三者总字节数都等于 256。
3. 回答：VR[3] 由哪 4 行 VX 组成？VE[5] 由哪 2 行 VX 组成？

**预期结果**：

- VX 32 条 × 64 位 = 2048 位 = 256 B；VE 16 条 × 128 位 = 256 B；VR 8 条 × 256 位 = 256 B。
- VR[3] = VX[12] ∥ VX[13] ∥ VX[14] ∥ VX[15]（因为 4×3=12）。
- VE[5] = VX[10] ∥ VX[11]（因为 2×5=10）。

> 这一步纯靠 4.1.2 的公式即可推导，无需运行任何命令；公式正确性由下一节的源码与 `MultiWidthRegisterSpec` 共同保证。

#### 4.1.5 小练习与答案

**练习 1**：如果某天把 `L` 改成 30（不被 4 整除），会在哪一步出错？

> **答案**：elaborate 阶段触发 [multiWidthRegister.scala:49](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L49) 的 `require`，报错 `MultiWidthRegisterBlock: L=30 must be divisible by 4`，根本到不了生成 Verilog。

**练习 2**：上板参数 \(L=32, K=64, N=8\) 时，VR 一条寄存器能装多少个 INT32？

> **答案**：VR 每 lane 是 4N=32 位（正好一个 INT32），一条 VR 有 K=64 个 lane，所以一条 VR 装 64 个 INT32，共 64×4=256 字节。

---

### 4.2 地址宽度计算与 IO 端口 Bundle

#### 4.2.1 概念说明

既然 VX/VE/VR 是同一块存储的三种视角，那么给它们编址时，地址位宽就该**按各自的数量来定**——VE 只有 L/2 条，所以 VE 地址比 VX 少 1 位；VR 只有 L/4 条，所以比 VX 少 2 位。这一节解决两件事：

1. **地址位宽怎么算**（用 `log2Ceil`）。
2. **整个模块对外的 IO Bundle 长什么样**：多少个读端口、多少个写端口，每个端口的位宽是多少。

#### 4.2.2 核心流程

三类寄存器的地址位宽为：

\[
\text{VX\_ADDR} = \lceil \log_2 L \rceil,\quad
\text{VE\_ADDR} = \lceil \log_2 (L/2) \rceil,\quad
\text{VR\_ADDR} = \lceil \log_2 (L/4) \rceil
\]

默认 \(L=32\) 时分别为 5、4、3 位。

端口布局（测试/后端默认）：

| 类别 | 读端口 | 写端口 | 每端口数据位宽 |
|:---|:---:|:---:|:---|
| VX | 4（`vx_rd`） | 2（`vx_wr`） | K 个 N 位 lane（INT8） |
| VE | 2（`ve_rd`） | 1（`ve_wr`） | K 个 2N 位 lane（INT16） |
| VR | 2（`vr_rd`） | 2（`vr_wr`） | K 个 4N 位 lane（INT32/FP32） |
| ext | 1 | 1 | K 个 N 位 lane（VX 宽度，测试/外部用） |

所有**读都是异步（组合）读**，所有**写都是同步（时钟沿）写**。VR 的两个写端口各司其职：端口 0 给 VALU 的宽结果，端口 1 专门接 MMALU 的累加器直写（无截断）。

#### 4.2.3 源码精读

地址位宽常量直接用 `log2Ceil` 算出，IO Bundle 的字段宽度再引用这些常量：

- [multiWidthRegister.scala:55-57](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L55-L57)：`val VX_ADDR = log2Ceil(L)` 等，把地址位宽集中成具名常量，避免到处写魔数。

IO Bundle 用 `Vec(端口数, ...)` 把「一组同类端口」打包，每个端口的 lane 又是一层 `Vec(K, UInt(宽度.W))`：

- [multiWidthRegister.scala:59-87](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L59-L87)：整个 `io = IO(new Bundle {...})`。注意三类寄存器的数据宽度分别是 `N.W`、`(2*N).W`、`(4*N).W`，正好对应「N 位 / 2N 位 / 4N 位」三种 lane。
- [multiWidthRegister.scala:81-86](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L81-L86)：`ext` 端口。它**不是** `Vec`，而是单个地址/数据/使能，宽度与 VX 一致（K 个 N 位 lane），文档里标注为「test-harness use」。

端口数默认值在类参数里写死，但 `NCoreBackend` 实例化时显式覆盖：

- [SimpleBackend.scala:117-118](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L117-L118)：`Module(new MultiWidthRegisterBlock(L, K, N, vx_rd = 4, vx_wr = 2, ve_rd = 2, ve_wr = 1, vr_rd = 2, vr_wr = 2))`——后端用的就是上面表格里那组端口数。

#### 4.2.4 代码实践

**目标**：核对地址位宽与端口数据位宽，避免改参数时算错。

**步骤**：

1. 打开 [multiWidthRegister.scala:59-87](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L59-L87)。
2. 假设把 `N` 从 8 改成 16（其余不变），手算 `vx_r_data`、`ve_r_data`、`vr_r_data` 各端口单条 lane 的位宽。
3. 回答：此时一条 VR 的总位宽是多少？`VR_ADDR` 会变吗？

**预期结果**：

- `vx_r_data` 单 lane = N = 16 位；`ve_r_data` 单 lane = 2N = 32 位；`vr_r_data` 单 lane = 4N = 64 位。
- 一条 VR = K × 4N = 8 × 64 = 512 位。
- `VR_ADDR = log2Ceil(L/4)` 只依赖 `L`，与 `N` 无关，仍是 3 位。

> 这一步是纯静态推导，结论可直接对照源码常量核对；不必实际 elaborate。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `vr_w_data` 是 `Vec(vr_wr, Vec(K, UInt((4*N).W)))` 而不是 `Vec(vr_wr, UInt((K*4*N).W))`？

> **答案**：因为寄存器堆是**按 lane 组织**的：每个 lane 独立是一个 4N 位的值（对应一个 INT32/FP32），K 个 lane 并行。用嵌套 `Vec(K, ...)` 保留了 lane 边界，写拆分时才能按 lane 把 4N 位切成 4 个 N 位字节（见 4.3）。压成一根 `K*4*N` 位的线会丢失 lane 结构。

**练习 2**：VR 有 2 个写端口，端口 1 留给 MMALU 直写。如果想让两个 VALU 同时写两条 VR，会够用吗？

> **答案**：不够。VR 写端口只有 2 个，端口 1 已被 MMALU 占用（见 [SimpleBackend.scala:177](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L177)），留给 VALU 的只有端口 0。所以同一拍最多只能有一条 VALU 宽结果写回 VR——这是端口数带来的结构限制。

---

### 4.3 别名机制：读路径的拼接与写路径的拆分

#### 4.3.1 概念说明

4.1 讲了「三种视角看同一块存储」，但还没说**RTL 怎么用一根 4N 位的 VR 线去读写底层 N 位的字节**。这一节是本讲最核心的机制：读 VE/VR 时把多行字节**拼（Cat）**成宽 lane，写 VE/VR 时把宽 lane **切（bits 切片）**成多行字节。

关键技巧是**用地址低位补零来对齐到行边界**：VE 地址后面拼 1 个 0 就是「乘 2」（指向偶数行），VR 地址后面拼 2 个 0 就是「乘 4」（指向 4 的倍数行）。

#### 4.3.2 核心流程

**读路径（异步组合读）**：

- VX 读：直接 `mem(addr)(lane)`，一进一出。
- VE 读：`baseRow = ve_addr ## 0`（即 `ve_addr × 2`），然后 `Cat(mem[baseRow+1], mem[baseRow])`——高位是下一行，低位是当前行。
- VR 读：`baseRow = vr_addr ## 00`（即 `vr_addr × 4`），然后 `Cat(mem[baseRow+3], mem[baseRow+2], mem[baseRow+1], mem[baseRow])`。

**写路径（同步写，先汇总到中间 wire 再统一落盘）**：

- VX 写：写 1 行。
- VE 写：把每个 2N 位 lane 切成低 N 位（写到 `baseRow`）和高 N 位（写到 `baseRow+1`），共写 2 行。
- VR 写：把每个 4N 位 lane 切成 4 段 N 位，分别写到 `baseRow+0..3` 共 4 行——这就是「写 VR[i] 原子更新 4 行 VX」的硬件实现。

位段对应关系（以 VR 写为例，`sub` 从 0 到 3）：

\[
\text{row} = \text{baseRow} + \text{sub},\qquad
\text{byte} = \text{lane}\,[\,N(\text{sub}+1)-1 \;:\; N\cdot\text{sub}\,]
\]

即 `sub=0` 取最低字节写到 `baseRow+0`，`sub=3` 取最高字节写到 `baseRow+3`，与读路径的 `Cat(b3,b2,b1,b0)` 完全一致——读写位序自洽，别名才正确。

#### 4.3.3 源码精读

**读 VE**：地址低位补 1 个零实现「×2」，再用 `Cat(hi, lo)` 把两行拼起来：

- [multiWidthRegister.scala:100-109](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L100-L109)：`val baseRow = io.ve_r_addr(p) ## 0.U(1.W)`（`##` 是位拼接，效果等同乘 2），`Cat(hi, lo)` 中 `hi=mem(baseRow+1)`、`lo=mem(baseRow)`。

**读 VR**：同理低位补 2 个零「×4」，拼 4 行：

- [multiWidthRegister.scala:111-121](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L111-L121)：`val baseRow = io.vr_r_addr(p) ## 0.U(2.W)`，`Cat(b3, b2, b1, b0)`。

**写 VE**：用 bits 切片把 2N 位切成两段 N 位，分写到相邻两行：

- [multiWidthRegister.scala:156-169](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L156-L169)：`wr_data(r0)(lane) := io.ve_w_data(p)(lane)(N-1, 0)`（低 N 位），`wr_data(r1)(lane) := io.ve_w_data(p)(lane)(2*N-1, N)`（高 N 位）。

**写 VR**：循环 `sub` 0..3，切 4 段写 4 行：

- [multiWidthRegister.scala:171-183](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L171-L183)：`val base = io.vr_w_addr(p) ## 0.U(2.W)`，`wr_data(row)(lane) := io.vr_w_data(p)(lane)(N*(sub+1)-1, N*sub)`。这段就是「写 VR[i] 原子更新 4 行 VX」的硬件真相——并没有什么「宽寄存器」，只是一拍内同时点亮 4 行的写使能、各塞一个字节。

测试侧 `MultiWidthRegisterSpec` 的「write VR and read back via VX」用例正面验证了这条别名链：

- [MultiWidthRegisterSpec.scala:92-133](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/sram/MultiWidthRegisterSpec.scala#L92-L133)：写 VR[1]，再分别读回 VR 与底层 VX[4..7]，断言 `(data32(i) >> (N*sub)) & 0xFF` 等于 `vx_r_data`——这正是 4.3.2 公式的 Scala 镜像。

#### 4.3.4 代码实践

**目标**：仿照 `MultiWidthRegisterSpec`，写一个新场景：**写 VR[0]，再读 VE[0] 与 VX[0..3]，验证别名**。这是本讲指定的实践任务，也是最能串起「VR→VE→VX」三重视角的练习。

**操作步骤**（以下为示例代码，请加入 `MultiWidthRegisterSpec.scala` 或新建一个 spec）：

```scala
// 示例代码：写 VR[0]，经 VE[0] 与 VX[0..3] 读回，验证别名
"MultiWidthRegisterBlock" should "write VR[0] and read back via VE[0] and VX[0..3]" in {
  simulate(new MultiWidthRegisterBlock(L, K, N)) { dut =>
    // 1) 先把所有写使能拉低、所有读地址置 0（含 ext_r_addr，否则 elaborate 失败）
    dut.io.vx_w_en(0).poke(false.B); dut.io.vx_w_en(1).poke(false.B)
    dut.io.ve_w_en(0).poke(false.B)
    dut.io.vr_w_en(0).poke(false.B);  dut.io.vr_w_en(1).poke(false.B)
    dut.io.ext_w_en.poke(false.B)
    for (p <- 0 until 4) dut.io.vx_r_addr(p).poke(0.U)
    for (p <- 0 until 2) dut.io.ve_r_addr(p).poke(0.U)
    for (p <- 0 until 2) dut.io.vr_r_addr(p).poke(0.U)
    dut.io.ext_r_addr.poke(0.U)

    // 2) 构造 K 个 4N 位(=32 位)随机值，写进 VR[0]
    val data32 = Array.fill(K)(rand.nextInt(Int.MaxValue) & 0xFFFFFFFFL).map(_.toInt)
    dut.io.vr_w_en(0).poke(true.B)
    dut.io.vr_w_addr(0).poke(0.U)
    data32.zipWithIndex.foreach { case (v, i) => dut.io.vr_w_data(0)(i).poke(v.U) }
    dut.clock.step()
    dut.io.vr_w_en(0).poke(false.B)

    // 3) 经 VE[0] 读回：VE[0] = VX[0] ∥ VX[1] = VR[0] 的低 2N(=16) 位
    dut.io.ve_r_addr(0).poke(0.U)
    for (i <- 0 until K) {
      val veExpected = data32(i) & 0xFFFF          // VR[0] 的低 16 位
      dut.io.ve_r_data(0)(i).expect(veExpected.U, s"VE[0] alias lane $i")
    }

    // 4) 经 VX[0..3] 读回：4 行各取 VR[0] 的一个字节
    for (sub <- 0 until 4) {
      dut.io.vx_r_addr(0).poke(sub.U)
      for (i <- 0 until K) {
        val byteVal = (data32(i) >> (N * sub)) & 0xFF
        dut.io.vx_r_data(0)(i).expect(byteVal.U, s"VX[$sub] alias lane $i sub=$sub")
      }
    }
  }
}
```

**需要观察的现象 / 预期结果**：

- VE[0] 每个 lane 等于 `data32(i) & 0xFFFF`（VR[0] 的低 16 位），因为 VE[0] = VX[0]∥VX[1]，而 VX[0]、VX[1] 正好是 VR[0] 的最低两个字节。
- VX[0..3] 每个 lane 分别等于 `data32(i)` 的第 0、1、2、3 字节，与 4.3.2 的位段公式一致。
- 若哪一档断言失败，多半是把 `Cat` 的高低位顺序或 `sub` 切片弄反了——回到 [multiWidthRegister.scala:111-121](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L111-L121) 与 [multiWidthRegister.scala:171-183](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L171-L183) 对照读写位序。

**运行方式**（用项目自带脚本，容器内跑）：

```bash
./tool/test-specific-spec.sh sram.mwreg.MultiWidthRegisterSpec
```

> 待本地验证：本例的期望值由别名公式严格推出，但具体打印/波形需在你的环境里实际跑一次确认。

#### 4.3.5 小练习与答案

**练习 1**：读 VE 时为什么是 `Cat(hi, lo)`（`hi` 是 `baseRow+1`），而不能反过来？

> **答案**：因为写 VE 时高 N 位写到了 `baseRow+1`、低 N 位写到了 `baseRow`（见 [multiWidthRegister.scala:164-167](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L164-L167)）。读时必须用同样的高低顺序拼回去，否则一个 16 位数的高低位会被对调，值就错了。读写位序必须互为逆运算。

**练习 2**：`baseRow = io.ve_r_addr(p) ## 0.U(1.W)` 这种「拼零」写法，相比 `io.ve_r_addr(p) * 2.U` 有什么好处？

> **答案**：`##` 是纯位拼接，不引入乘法器/加法器，综合后就是「把地址线低位补一根 0」，零面积零延迟；而 `* 2.U` 虽然综合工具多半也能优化成左移，但语义上多走一遍算术通路。地址对齐场景下用拼接是更精确、更省的写法。

---

### 4.4 写优先级、冲突规则与 ext 端口陷阱

#### 4.4.1 概念说明

寄存器堆有「4 类写源（ext / VX / VE / VR）」共 6 个写端口，它们可能在一拍内指向**同一行**（比如 VR 写 VX[0..3] 的同时，VX 写端口也想写 VX[0]）。如果任由两路同时驱动同一个 `mem(row)`，Chisel 会报「多驱动」错误。本节讲清三件事：

1. **写优先级 `VR > VE > VX > ext`** 如何用「最后连接胜出」一次性解决多驱动。
2. **冲突规则**：硬件优先级是确定性的，但「软件负责」是什么意思。
3. **ext 端口陷阱**：为什么 `ext_r_addr` 不用也得驱动。

#### 4.4.2 核心流程

**整体写策略**：先把每行的写使能 `wr_en(row)` 和写数据 `wr_data(row)` 声明成 wire 并默认全关，然后按优先级**从低到高**依次用 `when` 覆盖：

```
默认：所有行 wr_en=false
when(ext_w_en):  ext 写 1 行        ← 最低优先级，先写
for vx 写端口:   VX 写 1 行
for ve 写端口:   VE 写 2 行
for vr 写端口:   VR 写 4 行          ← 最高优先级，最后写，覆盖前面
时钟沿：when(wr_en(row)) mem(row) := wr_data(row)
```

由于 Chisel「同节点后连接覆盖前连接」，后执行的 `when` 块会覆盖同一行的前一次赋值，于是天然得到「VR 胜过 VE 胜过 VX 胜过 ext」的逐行优先级，且**每行最终只有一个驱动源**，杜绝多驱动。

**冲突规则（last writer wins）**：当多路同拍写重叠行时，只有最高优先级那路在**被争抢的行**上生效，低优先级那路对这些行的写静默丢失。源码注释和文档都强调这是「per row / software responsibility」——即硬件按优先级**确定性地**裁决，但软件要明白：**别指望同拍对重叠行的两路写都保留**，软件应避免在依赖两路都生效的场景下发出冲突写。

#### 4.4.3 源码精读

**写 wire 默认全关 + 四级覆盖**：

- [multiWidthRegister.scala:133-140](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L133-L140)：声明 `wr_data`/`wr_en` 并把所有行初始化为 `false.B` / `0.U`——这一步保证「没人写时行保持不变」。
- [multiWidthRegister.scala:142-183](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L142-L183)：四级 `when` 块按 ext → VX → VE → VR 的顺序依次覆盖，正是注释 [multiWidthRegister.scala:128-131](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L128-L131) 所说「VR > VE > VX > ext，避免推断出多驱动寄存器」。

**统一时钟沿落盘**：

- [multiWidthRegister.scala:185-190](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L185-L190)：`when (wr_en(row)) { mem(row) := wr_data(row) }`。注意这是**逐行**的，每行独立看自己的 `wr_en`——这正是「per row」优先级的落点。

**ext 端口陷阱**：

- [multiWidthRegister.scala:123-126](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L123-L126)：异步读 `io.ext_r_data(lane) := mem(io.ext_r_addr)(lane)`。这里用 `io.ext_r_addr` 去索引 `mem`，**若该输入未被驱动，索引值未定义**。
- 后端因此**无条件**驱动它：[SimpleBackend.scala:136-137](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L136-L137)：`rf.io.ext_r_addr := io.ext_rd_addr`，注释明说「separate from vx_r_addr」。
- 测试侧每个用例开头也都 `dut.io.ext_r_addr.poke(0.U)`（见 [MultiWidthRegisterSpec.scala:30](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/sram/MultiWidthRegisterSpec.scala#L30)），正是同一个原因。
- 官方告警见 [Registers.md 的 ext_r_addr must be driven 警告框](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/Registers.md#L137)：不驱动会让 firtool 报 `uninitialized sink`，建议默认给 `0.U`。

#### 4.4.4 代码实践

**目标**：亲手复现「ext_r_addr 不驱动 → elaborate 失败」这个陷阱，并理解其根因。这是本讲第二个指定实践点。

**操作步骤**：

1. 找到测试里所有 `dut.io.ext_r_addr.poke(0.U)`（如 [MultiWidthRegisterSpec.scala:30](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/sram/MultiWidthRegisterSpec.scala#L30)）。
2. 在一个用例里**临时注释掉** `dut.io.ext_r_addr.poke(0.U)` 这一行（只改测试，不动 RTL）。
3. 重新跑：
   ```bash
   ./tool/test-specific-spec.sh sram.mwreg.MultiWidthRegisterSpec
   ```

**需要观察的现象 / 预期结果**：

- elaborate 阶段即报错，典型信息含 `uninitialized` / `not fully initialized` / `sink` 字样（firtool 版本不同措辞略异），测试根本进不了 `step()`。
- 根因：[multiWidthRegister.scala:125](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L125) 用未驱动的 `io.ext_r_addr` 去索引 `mem`，索引未定义，firtool 拒绝生成。
- 恢复那行 `poke(0.U)`，测试应重新通过。

> 待本地验证：错误信息的确切措辞需在你的 firtool 版本下实际触发一次确认；但「不驱动即 elaborate 失败」这一行为由源码索引语义保证。

> 注意：本实践只注释**测试**里的一行 poke，不要去改 `multiWidthRegister.scala` 或 `SimpleBackend.scala`（后端恒驱动它，注释测试才是最小复现路径）。

#### 4.4.5 小练习与答案

**练习 1**：写优先级用的是「最后连接胜出」，那如果把四个 `when` 块的顺序改成 VR → VE → VX → ext（最高优先级在前），会发生什么？

> **答案**：优先级会反过来——ext 变成最高优先级、VR 变成最低。因为后执行的连接覆盖先执行的，谁排在**最后**谁胜出。源码故意把 ext 放最前、VR 放最后，才得到 `VR > VE > VX > ext`。顺序就是优先级。

**练习 2**：后端把 VR 写端口 1 专门留给 MMALU 直写 INT32（[SimpleBackend.scala:176-182](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L176-L182)）。若同一拍 VALU 也想经 VR 写端口 0 写回同一 VR 地址，按优先级谁赢？

> **答案**：两者都是 VR 写端口（端口 0 和端口 1），优先级相同——它们各自点亮不同的 `vr_w_addr`。若两端口地址不同则各行其是、互不干扰；若两端口**同地址**，则 [multiWidthRegister.scala:172-183](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L172-L183) 的循环里后迭代到的端口（p=1）覆盖先到的（p=0），即 MMALU 端口 1 胜出。这正是「软件应避免同拍重叠写」的实际场景。

---

## 5. 综合实践

把本讲三块知识（存储与视图、地址与端口、别名与优先级）串成一个端到端的小任务：

**任务**：用 `MultiWidthRegisterSpec` 的现成套路，设计一个能同时验证「别名 + 写优先级」的最小用例。

1. **写满底层**：用 VX 写端口把 VX[0]、VX[1]、VX[2]、VX[3] 各写一个已知字节模式（比如 0x11、0x22、0x33、0x44，每 lane 相同）。
2. **验证别名读**：读 VR[0]，期望每 lane 为 `Cat(0x44,0x33,0x22,0x11) = 0x44332211`；读 VE[0]，期望每 lane 为 `Cat(0x22,0x11)=0x2211`。
3. **触发优先级冲突**：在同一拍里，既用 VR 写端口 0 把 VR[0] 写成 `0xFFFFFFFF`（覆盖 4 行），又用 VX 写端口把 VX[0] 写成 `0x00`。一拍后读 VX[0]，期望仍为 `0xFF`（VR 优先级高于 VX，VX[0] 的写在争抢中输给 VR 写）。
4. **回答**：如果把第 3 步的 VR 写改成 ext 写，VX[0] 最后会是什么值？为什么？

**预期结论**：

- 第 2 步印证 4.3 的读写位序自洽。
- 第 3 步印证 4.4 的 `VR > VX` 优先级。
- 第 4 步：ext 优先级最低，VX 胜出，VX[0] = `0x00`。

> 这个综合用例需要你把 4.3.4 的写 VR 模板与 4.4.4 的多端口同拍写法组合起来；具体数值 `0x44332211` 等可由别名公式直接推出，但冲突拍的波形建议本地跑一次确认（待本地验证）。

## 6. 本讲小结

- `MultiWidthRegisterBlock` 只有**一块** `mem`（\(L \times K \times N/8\) 字节），VX/VE/VR 是它的三种字节级别名视角，并非三块存储。
- 三类寄存器数量为 \(L\)、\(L/2\)、\(L/4\)，地址位宽由 `log2Ceil` 算出（默认 5/4/3 位）；`L` 必须被 4 整除，由 `require` 在 elaborate 时强制。
- 读 VE/VR 靠「地址低位补零对齐 + `Cat` 拼多行」；写 VE/VR 靠「bits 切片 + 同点亮多行」——读写位序互为逆运算，别名才正确。
- 写优先级 `VR > VE > VX > ext` 用 Chisel「最后连接胜出」实现：`when` 块从低到高排列，逐行裁决，杜绝多驱动；冲突按优先级确定性解决，软件需避免同拍依赖重叠写。
- VR 写端口 1 专门直写 MMALU 的 INT32 累加器（无截断），是量化流水线 MMALU→VALU 数据复用的关键。
- `ext_r_addr` 即使不用也必须驱动（默认 `0.U`），否则异步读用它索引 `mem` 会触发 firtool `uninitialized sink`；后端与所有测试都恒驱动它。

## 7. 下一步学习建议

本讲收口了「控制与存储基础」单元（u3）。寄存器堆是 NPU 后端的数据中枢，下一步自然会问：**谁在读写这些端口？** 建议进入：

- [u4 矩阵乘法引擎 MMALU](u4-l1-processing-element.md)：重点看 MMALU 如何把累加结果经 VR 写端口 1 直写回本讲的寄存器堆（即 [SimpleBackend.scala:176-182](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L176-L182) 的上游）。
- [u5 向量 ALU VALU](u5-l1-valu-datapath.md)：看 VALU 如何按 `regCls` 选择写回 VX/VE/VR 哪个端口。
- [u6 神经核心集成 NCoreBackend](u6-l1-ncore-backend-wiring.md)：把本讲的端口分配表与 u4/u5 的执行单元整体连成一张图。

如果想再深挖存储本身，可继续阅读 [docs/implementations/Registers.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/Registers.md) 的「Backend Port Assignment」一节，对照 [SimpleBackend.scala:120-153](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L120-L153) 逐行核对每个读写端口到底连到了谁。
