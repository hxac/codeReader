# S-box 与逆 S-box 的 ROM 实现

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 AES 的 S-box 本质上是一张 **256 项的查找表（ROM）**，并理解为什么硬件里直接「预先算好、存起来、查表」而不是「现场计算」。
- 读懂 `aes_sbox.v` / `aes_inv_sbox.v` 中 `wire [7:0] sbox [0:255]` 数组 + 4 路并行 `assign` 的写法，明白它如何在一个周期内并行处理一个 32 位字（4 个字节）。
- 解释正向 S-box 与逆向 S-box 在数学上互为反函数，并能从源码常量里验证 `inv_sbox[sbox[x]] = x`。
- 回答一个贯穿全工程的设计问题：**为什么加密通路（encipher）用的是 `aes_core` 里共享的那一个 `sbox_inst`，而解密通路（decipher）却在自己模块内部私挂了一个 `inv_sbox_inst`？**

本讲是 [u2-l1](u2-l1-aes-core-control-fsm.md) 的下钻：上一讲我们把 `aes_core` 当成「调度中枢」，看到它例化了唯一的正向 S-box `sbox_inst`；这一讲我们就钻进这个 S-box 的内部，看清这张表长什么样、怎么查、为什么正反两张表要分开。

## 2. 前置知识

在开始前，先用通俗语言把几个概念讲清楚。

### 2.1 字节（byte）与十六进制

AES 的基本处理单位是**字节**，每字节 8 位。源码里你会大量看到 `8'h63` 这样的写法：

- `8'` 表示位宽是 8 位；
- `h` 表示十六进制（hex）；
- `63` 是数值。

所以 `8'h63` 就是十进制的 99，二进制 `0110_0011`。一张 S-box 就是从「一个输入字节」映射到「一个输出字节」的对照表，一共 \(2^8 = 256\) 项。

### 2.2 什么是 S-box（替换盒）

S-box 全称 **Substitution Box（替换盒）**。它的作用就是**查表替换**：给你一个输入字节 `x`，它查表吐出一个输出字节 `y = S(x)`。

在 AES 里，S-box 是算法**唯一的非线性来源**。如果没有 S-box（或者说如果 S-box 是线性的），整个 AES 就退化成一组线性运算，攻击者可以用线性代数轻易破解。所以 S-box 必须「足够混乱」——这也是它要用一张看似随机的 256 项常量表来表达的原因。

### 2.3 GF(2⁸)：字节上的「有限域」乘法

要理解 S-box 的值是怎么来的，需要一点点数学。AES 把每个字节看作系数在 {0,1} 里的**次数最高为 7 次的多项式**，例如字节 `8'h57`（二进制 `0101_0111`）代表多项式：

\[
x^6 + x^4 + x^2 + x + 1
\]

这些字节多项式在一个叫 **GF(2⁸)** 的「有限域（Galois Field）」里做加法和乘法：

- **加法**就是按位异或 `^`（因为没有进位）。
- **乘法**是多项式乘法，并对一个不可约多项式 \(x^8 + x^4 + x^3 + x + 1\)（写成十六进制是 `0x11b`）取模。

你不必现在就能手算 GF(2⁸) 乘法——本讲后面会告诉你，**硬件其实根本不在运行时算它**。理解这个概念只是为了看懂「S-box 的那张表是怎么被预先算出来的」。

> 关键直觉：AES 的正向 S-box 对每个输入字节 `x` 做了两步——(1) 在 GF(2⁸) 里求**乘法逆元**（`0` 的逆元规定为 `0`）；(2) 再做一次**仿射变换（affine）**。这两步合起来就是 `S(x)`。下面 4.3 节会给出公式。

### 2.4 本讲用到的 Verilog 语法速查

| 写法 | 含义 |
|---|---|
| `wire [7:0] sbox [0:255];` | 声明一个**数组**：256 个元素，每个元素是 8 位的 wire |
| `assign sbox[8'h00] = 8'h63;` | 给数组下标为 0 的元素常量赋值（持续驱动） |
| `assign y = sbox[x];` | 用变量 `x` 作下标**读数组**，等价于一个查表/多路选择 |
| `assign new_sboxw[31:24] = ...` | 对一个 32 位 wire 的高字节做持续驱动 |

记住一句话：**这里的「数组 + assign」是纯组合逻辑（没有时钟），综合后就是一堆常量查表电路**。虽然注释叫它「ROM」，但它读出是异步的、当拍就出结果。

## 3. 本讲源码地图

本讲只聚焦两个文件，并参照它们被使用的上下文：

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [rtl/aes_sbox.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_sbox.v) | **正向 S-box**：256 字节常量表，输入 32 位字、输出 32 位字 | 数组声明、4 路并行查表、表内容 |
| [rtl/aes_inv_sbox.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_inv_sbox.v) | **逆向 S-box**：256 字节常量表，正向表的反函数 | 与正向表对照、互逆验证 |
| [rtl/aes_core.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v) | 核心调度 | 唯一的 `sbox_inst` 例化、`sbox_mux` 分时、`dec_block` 不接 S-box |
| [rtl/aes_decipher_block.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v) | 解密数据通路 | 内部私有的 `inv_sbox_inst` 例化 |

> 提示：本讲只读 S-box 的「内部实现」和「谁在用它」，不展开 encipher/decipher 的轮状态机——那是 [u2-l5](u2-l5-encipher-round-fsm.md) / [u2-l7](u2-l7-decipher-round-fsm.md) 的事。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **S-box 数组与查表**（4.1）——一张 256 字节的常量表是怎么用 Verilog 写出来的。
2. **4 路并行 32 位处理**（4.2）——为什么模块端口是 32 位、内部却只有「一张」表。
3. **正向 / 逆向 S-box 的关系**（4.3）——两张表互为反函数，以及由此引出的「共享 vs 私挂」设计。

---

### 4.1 S-box 数组与查表（256 字节 ROM）

#### 4.1.1 概念说明

正向 S-box 要解决的问题非常直接：**给定一个输入字节 `x`（0~255），返回一个固定的输出字节 `S(x)`。**

软件里你会写一个 `uint8_t sbox[256] = {...}` 然后查 `sbox[x]`。硬件里的思路一模一样，只不过用 Verilog 的 `wire` 数组 + `assign` 来表达，并且因为这是密码学常量，**全部 256 个值都被预先算好、硬编码在源码里**。

为什么要预先算好而不在硬件里「现场算」？因为 S-box 的数学定义（求 GF(2⁸) 逆元 + 仿射变换，见 4.3）算单个字节就要不少逻辑，而 AES 每一轮、每个字节都要查一次。与其为每个字节搭一套「计算电路」，不如把全部 256 个结果烧成一张常量表，查表即可——**用「存储」换「计算」**，又快又省。这是 AES 硬件实现里最经典的一个取舍。

#### 4.1.2 核心流程

整个 `aes_sbox` 模块的工作流程可以概括成两步：

```text
输入 sboxw (32 位 = 4 字节)
        │
        ├── 取高字节 sboxw[31:24] ──► 查 sbox[ ] ──► new_sboxw[31:24]
        ├── 取次高字节 sboxw[23:16] ─► 查 sbox[ ] ──► new_sboxw[23:16]
        ├── 取次低字节 sboxw[15:08] ─► 查 sbox[ ] ──► new_sboxw[15:08]
        └── 取低字节 sboxw[07:00] ──► 查 sbox[ ] ──► new_sboxw[07:00]
        │
输出 new_sboxw (32 位 = 4 个被替换后的字节)
```

要点：

1. **没有时钟、没有复位**——这个模块是纯组合逻辑，输入一变，输出当拍就变。
2. **查表 = 用输入字节作下标读数组**：`sbox[输入字节]` 直接得到输出字节。
3. 4 个字节彼此独立，所以可以「4 路并行」（见 4.2）。

#### 4.1.3 源码精读

先看模块端口——非常简洁，一个 32 位输入 `sboxw`，一个 32 位输出 `new_sboxw`：

[aes_sbox.v:10-13](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_sbox.v#L10-L13) 定义了模块与端口：输入一个 32 位的 `sboxw`（待替换的字），输出一个 32 位的 `new_sboxw`（替换后的字）。

接着是数组声明——**这就是「ROM」本身**：

[aes_sbox.v:19](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_sbox.v#L19) 声明了 `wire [7:0] sbox [0:255];`——256 个元素、每元素 8 位的 wire 数组，这就是 256 字节的 S-box 表存储。

表的内容是一长串 `assign` 常量赋值，从头几项就能看清规律：

[aes_sbox.v:34-35](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_sbox.v#L34-L35) 给出表的头两项：`sbox[8'h00] = 8'h63;` 和 `sbox[8'h01] = 8'h7c;`——即输入字节 `0x00` 替换为 `0x63`、输入 `0x01` 替换为 `0x7c`。

完整的 256 项一直填到：

[aes_sbox.v:289](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_sbox.v#L289) 是最后一项 `sbox[8'hff] = 8'h16;`，整张表从第 34 行到第 289 行共 256 条 `assign`，恰好覆盖下标 `0x00`~`0xff`。

> 小知识：为什么是 `0x63`？因为输入 `0x00` 在 GF(2⁸) 里的逆元规定为 `0x00`，再做仿射变换恰好等于常数 `0x63`（见 4.3 的公式）。所以 `sbox[0x00] = 0x63` 不是随便写的，它是数学推出来的。

#### 4.1.4 代码实践

**实践目标**：用肉眼在源码里完成一次「查表」，确认 `sbox[0x00] = 0x63` 和 `sbox[0x01] = 0x7c`，建立对「这就是一张表」的直觉。

**操作步骤**：

1. 打开 [rtl/aes_sbox.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_sbox.v)。
2. 定位到第 34~35 行，读出 `sbox[8'h00]` 和 `sbox[8'h01]` 的值。
3. 再挑一个值，例如查找输入 `0x53` 的替换结果：在文件里搜索 `sbox[8'h53]`，应得到 `8'hed`（第 117 行 `assign sbox[8'h53] = 8'hed;`）。`0x53` 正是 NIST 标准 AES-128 测试向量里明文的第一字节，记住它，[u3-l2](u3-l2-verification-and-nist-vectors.md) 会用到。

**需要观察的现象**：

- 表里没有重复模式，看起来「很乱」——这正是非线性带来的安全性。
- 每一项都是「下标 → 常量」的一对一映射，没有任何运算。

**预期结果**：

| 输入下标 | 表中的值 |
|---|---|
| `0x00` | `0x63` |
| `0x01` | `0x7c` |
| `0x53` | `0xed` |
| `0xff` | `0x16` |

如果你愿意真正跑一次仿真（可选，**待本地验证**），可以用工程里现成的 `rtl/tb_aes_key_mem.v`：它在第 73 行例化了 `aes_sbox sbox(...)`，并在第 147/150 行用 `$display` 打印 `sbox.sboxw` 和 `sbox.new_sboxw`，你能直接在日志里看到「输入字 → 替换字」的实时对照。

#### 4.1.5 小练习与答案

**练习 1**：源码里把 S-box 写成 `wire` 数组 + `assign` 常量，而不是用 `reg` + `always` 在时钟沿更新。为什么这样做是合理的？

> **答案**：S-box 的内容是密码学**固定常量**，永远不变。用 `wire` + `assign` 表达「持续驱动的常量」，综合出的是纯组合查表电路（异步读、当拍出结果），既不需要时钟也不需要复位，访问延迟最低。如果用 `reg`+时钟，反而会平白多出一个时钟周期的延迟和一堆触发器。

**练习 2**：模块注释说它是「256 Byte ROM」。但严格来说，这段代码综合出来的是不是一块带时钟的 ROM 存储器？

> **答案**：不是。它没有时钟、没有读使能，是**异步（组合）查表逻辑**。综合工具通常把它推断成 LUT/多路选择树，或（在特定工艺下）映射成组合 ROM，但读出是组合的、当拍完成。「ROM」在这里是概念上的称呼。

---

### 4.2 4 路并行 32 位处理

#### 4.2.1 概念说明

注意一个表面矛盾：S-box 在数学上一次只替换**一个字节**，可 `aes_sbox` 模块的端口却是 **32 位**（4 字节）进出。为什么？

答案是：**模块内部把「一张表」复制成了 4 路并行的查表通道**，于是一个周期就能并行替换 4 个字节。模块头注释说得很直白——「contains four parallel S-boxes to handle a 32 bit word」（含 4 个并行 S-box 来处理一个 32 位字）。

要特别分清两个层次，别混淆：

- **模块内部**：4 路并行查表，一次处理 32 位（4 字节）。
- **模块外部（整个核）**：整个 `aes_core` 只例化了**一个** `aes_sbox` 实例（`sbox_inst`），并通过分时复用让它服务多个消费者（见 4.3 和 [u2-l1](u2-l1-aes-core-control-fsm.md)）。

也就是说：「4 路并行」指的是**单个模块内部**对 4 个字节的同时处理，不是说工程里有 4 个 S-box 模块。

#### 4.2.2 核心流程

把 32 位输入 `sboxw` 按字节切成 4 段，每段独立查同一张表，再拼回 32 位输出：

```text
sboxw = [ B3 | B2 | B1 | B0 ]      （每个 B 是 8 位）
              │     │     │     │
              ▼     ▼     ▼     ▼
           sbox[ ] sbox[ ] sbox[ ] sbox[ ]   ← 同一张 256 项表，4 个独立读口
              │     │     │     │
              ▼     ▼     ▼     ▼
new_sboxw = [ S(B3)| S(B2)| S(B1)| S(B0) ]
```

这 4 路是**完全并行、互不影响**的——这正是组合逻辑 `assign` 的天然优势：4 条独立赋值语句，综合后就是 4 套并行的查表电路，在同一拍同时算出 4 个字节。

#### 4.2.3 源码精读

4 路并行查表就写在 4 条 `assign` 里：

[aes_sbox.v:25-28](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_sbox.v#L25-L28) 是 4 路并行查表：把 `sboxw` 的 4 个字节分别作下标去查 `sbox[ ]`，结果分别驱动 `new_sboxw` 的 4 个字节。4 条语句互相独立，因此 4 个字节的替换在同一拍并行完成。

逆向 S-box 的写法**完全对称**，只是端口名和数组名换了：

[aes_inv_sbox.v:24-27](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_inv_sbox.v#L24-L27) 同样是 4 路并行查表，区别仅在数组叫 `inv_sbox`、端口叫 `sword` / `new_sword`。正反两张表的「外壳」结构一模一样，装的常量不同。

> 对照看：正向表 `aes_sbox` 的端口是 `sboxw` / `new_sboxw`；逆向表 `aes_inv_sbox` 的端口是 `sword` / `new_sword`（少一个 `b`）。这种细微的命名差异在阅读源码时要留意，否则容易看错信号。

#### 4.2.4 代码实践

**实践目标**：在脑子里跑一次「32 位字并行查表」，体会一个周期出 4 个字节。

**操作步骤**：

1. 假设输入 `sboxw = 32'h00010200_`（为方便，取 `0x00_01_53_00`，即高到低四字节为 `0x00, 0x01, 0x53, 0x00`）。
2. 对每个字节查 4.1 节得到的结果：`S(0x00)=0x63`、`S(0x01)=0x7c`、`S(0x53)=0xed`、`S(0x00)=0x63`。
3. 按 [aes_sbox.v:25-28](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_sbox.v#L25-L28) 的拼接关系，得到 `new_sboxw = 0x63_7c_ed_63`。

**需要观察的现象**：

- 4 个字节的查表是同时发生的，没有先后依赖。
- 同一个输入字节（如这里的两个 `0x00`）会得到相同的输出（两个 `0x63`）——这正是 ECB 模式「相同明文得相同密文」的微观根源（参见 [u1-l1](u1-l1-project-overview.md)）。

**预期结果**：`new_sboxw = 0x637ced63`。**待本地验证**：可在仿真里给 `sboxw` 喂这个值，看 `new_sboxw` 是否吻合。

#### 4.2.5 小练习与答案

**练习 1**：如果只把 `sboxw` 的高字节 `sboxw[31:24]` 改成新值，`new_sboxw` 的哪些位会变？

> **答案**：只有 `new_sboxw[31:24]` 会变，其余 3 个字节不受影响。因为 [aes_sbox.v:25-28](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_sbox.v#L25-L28) 的 4 条赋值各自独立，每个输出字节只依赖对应的输入字节。

**练习 2**：模块注释自称「four parallel S-boxes」。整个 AES 核里到底有几个 `aes_sbox` 模块实例？

> **答案**：只有 **1 个**，即 `aes_core` 里的 `sbox_inst`（[aes_core.v:138](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L138)）。「4 个并行 S-box」指的是这一个模块**内部**对 4 字节的并行处理通道，不是 4 个外部实例。两者不要混淆。

---

### 4.3 正向 / 逆向 S-box 的关系（含 GF(2⁸) 数学）

#### 4.3.1 概念说明

AES 解密（decipher）是加密（encipher）的逆过程，于是它需要一张**逆向 S-box** `S⁻¹`，满足：

\[
S^{-1}(S(x)) = x \quad \text{对任意字节 } x
\]

也就是说，正向表把 `0x00` 映射成 `0x63`，逆向表就必须把 `0x63` 映射回 `0x00`。源码里这两张表是**两张不同的常量表**：正向表 `sbox[0:255]` 装一组 256 个常量，逆向表 `inv_sbox[0:255]` 装另一组 256 个常量，二者互为反函数。

这就直接引出本讲的核心设计问题：

> 为什么加密通路用 `aes_core` 里**共享**的那一个 `sbox_inst`，而解密通路却在自己模块内部**私挂**了一个 `inv_sbox_inst`？

答案有两条，缺一不可：

1. **两张表是不同的常量集合**，物理上没法用同一块表既查正向又查逆向。所以正向、逆向天然要两份硬件。
2. **消费者数量不同，决定了是否值得共享**：
   - 正向 S-box 有 **2 个消费者**——密钥扩展 `key_mem` 和加密通路 `encipher`。但它俩**从不同时**工作（一个在 `init` 阶段、一个在 `next` 阶段），所以可以用一个 `sbox_mux` 分时复用同一份正向表硬件 → **共享 1 个 `sbox_inst`**。
   - 逆向 S-box 只有 **1 个消费者**——解密通路 `decipher`。密钥扩展无论加密还是解密都用**正向**表（密钥编排是同一套），加密也用正向表，没人需要逆向表。既然没有第二个消费者，就没有共享的对象 → **逆向表直接长在 `decipher` 肚子里**（`inv_sbox_inst`）。

这是一次典型的「**资源共享换面积/功耗**」取舍：贵的资源（正向 S-box）被两个不冲突的消费者共用一份；孤立的资源（逆向 S-box）则无需共享。这个主题在 [u3-l4](u3-l4-asic-design-tradeoffs.md) 会系统讨论。

#### 4.3.2 核心流程

**正向 S-box 的数学定义**（用来离线算出那张表，不是硬件运行时算）：

设输入字节 \(x\)，先求其在 GF(2⁸)（模 \(x^8+x^4+x^3+x+1\)）中的乘法逆元 \(b\)（约定 \(0^{-1}=0\)），再做仿射变换：

\[
\begin{bmatrix}
s_7 \\ s_6 \\ s_5 \\ s_4 \\ s_3 \\ s_2 \\ s_1 \\ s_0
\end{bmatrix}
=
\begin{bmatrix}
1&0&0&0&1&1&1&1 \\
1&1&0&0&0&1&1&1 \\
1&1&1&0&0&0&1&1 \\
1&1&1&1&0&0&0&1 \\
1&1&1&1&1&0&0&0 \\
0&1&1&1&1&1&0&0 \\
0&0&1&1&1&1&1&0 \\
0&0&0&1&1&1&1&1
\end{bmatrix}
\begin{bmatrix}
b_7 \\ b_6 \\ b_5 \\ b_4 \\ b_3 \\ b_2 \\ b_1 \\ b_0
\end{bmatrix}
\oplus
\begin{bmatrix}
0\\1\\1\\0\\0\\0\\1\\1
\end{bmatrix}
\]

其中常量列是 `0x63`（二进制 `0110_0011`，注意低位在前）。由此 \(S(0)=\text{Affine}(0)=0\text{x}63\)，这正好解释了 [aes_sbox.v:34](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_sbox.v#L34) 的 `sbox[8'h00] = 8'h63`。

**逆向 S-box** 就是把上面两步反过来：先做逆仿射，再求逆元。NIST 标准已经把全部 256 个结果算好，本工程直接抄进 [aes_inv_sbox.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_inv_sbox.v)。

**硬件里的「正→逆」往返**可以这么验证（纯查表，不涉及上面的运算）：

```text
        正向表                     逆向表
x ─────► S(x) ──────────────────► S⁻¹(S(x)) = x
   例: 0x00 ─► 0x63 ─(以0x63为下标查逆向表)─► 0x00
        0x01 ─► 0x7c ─(以0x7c为下标查逆向表)─► 0x01
```

#### 4.3.3 源码精读

**第一组证据：正向、逆向两张表互为反函数。**

[aes_sbox.v:34-35](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_sbox.v#L34-L35)：正向 `sbox[0x00]=0x63`、`sbox[0x01]=0x7c`。

那么逆向表里下标 `0x63`、`0x7c` 处必须是 `0x00`、`0x01`：

[aes_inv_sbox.v:132](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_inv_sbox.v#L132) `assign inv_sbox[8'h63] = 8'h00;`——把正向表输出的 `0x63` 还原回输入 `0x00`。

[aes_inv_sbox.v:157](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_inv_sbox.v#L157) `assign inv_sbox[8'h7c] = 8'h01;`——把 `0x7c` 还原回 `0x01`。

再看逆向表的第一项 `inv_sbox[0x00] = 0x52`：

[aes_inv_sbox.v:33](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_inv_sbox.v#L33) `assign inv_sbox[8'h00] = 8'h52;`。它对应的「正向输入」是 `0x52`：在正向表里查 `sbox[0x52]` 应为 `0x00`——确实，[aes_sbox.v:116](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_sbox.v#L116) 正是 `assign sbox[8'h52] = 8'h00;`。两张表严丝合缝。

**第二组证据：正向 S-box 被两个消费者共享，靠 `sbox_mux` 分时。**

在 `aes_core.v` 里，正向 S-box 只例化了一次：

[aes_core.v:138](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L138) `aes_sbox sbox_inst(.sboxw(muxed_sboxw), .new_sboxw(new_sboxw));`——整个核里唯一的正向 S-box 实例，输入是 `muxed_sboxw`（被选中的那个消费者的字），输出 `new_sboxw` 同时回送给两个消费者。

两个消费者把自己的「待查字」分别送到 `enc_sboxw`（加密通路）和 `keymem_sboxw`（密钥扩展）：

[aes_core.v:96-97](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L96-L97) 加密模块 `enc_block` 用端口 `.sboxw(enc_sboxw)` 把自己要查的字送出去，用 `.new_sboxw(new_sboxw)` 接回结果。

[aes_core.v:133-134](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L133-L134) 密钥扩展模块 `keymem` 同样用 `.sboxw(keymem_sboxw)` / `.new_sboxw(new_sboxw)` 接同一份 S-box。

谁来决定这一拍 S-box 归谁用？就是 `sbox_mux`：

[aes_core.v:184-194](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L184-L194) `sbox_mux`：当 `init_state`（处于密钥扩展阶段）为真时，把 `keymem_sboxw` 送给 S-box；否则把 `enc_sboxw`（加密通路）送给 S-box。因为密钥扩展和加密永远不会同时发生，所以一份正向 S-box 硬件就能被两者分时共用。（这个 `init_state` 来自 [u2-l1](u2-l1-aes-core-control-fsm.md) 讲过的 `aes_core_ctrl` 状态机。）

**第三组证据：解密通路根本不接共享 S-box，而是自带逆向 S-box。**

看 `dec_block` 的例化——它的端口列表里**完全没有** `.sboxw` / `.new_sboxw`：

[aes_core.v:105-118](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L105-L118) 解密模块 `dec_block` 的例化端口里只有 `clk/reset_n/next/keylen/round/round_key/block/new_block/ready`，**没有任何与 S-box 相关的连线**。这与加密模块 `enc_block`（带 `.sboxw`/`.new_sboxw`）形成鲜明对比——解密通路不参与共享正向 S-box。

那解密用什么查逆向表？答案在它自己肚子里：

[aes_decipher_block.v:205](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L205) `aes_inv_sbox inv_sbox_inst(.sword(tmp_sboxw), .new_sword(new_sboxw));`——解密模块**内部**例化了一个私有的逆向 S-box，输入是自己准备的 `tmp_sboxw`，输出 `new_sboxw` 仅供本模块使用。

至此，「为什么加密共享、解密私挂」在源码层面被三条独立证据完整证明：

| 通路 | 用的表 | 例化位置 | 是否共享 | 原因 |
|---|---|---|---|---|
| 加密 encipher | 正向 `aes_sbox` | `aes_core` 里的 `sbox_inst` | **共享**（与密钥扩展分时） | 正向表有 2 个消费者，且不同时工作 |
| 密钥扩展 key_mem | 正向 `aes_sbox` | 同上 `sbox_inst` | **共享** | 同上 |
| 解密 decipher | 逆向 `aes_inv_sbox` | `aes_decipher_block` 里的 `inv_sbox_inst` | **私挂** | 逆向表只有 1 个消费者，无处共享 |

#### 4.3.4 代码实践

**实践目标**：动手验证两张表互为反函数，并用自己的话解释「加密共享、解密私挂」。

**操作步骤**：

1. **互逆验证（源码阅读）**：
   - 在 [aes_sbox.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_sbox.v) 里任取一项，例如 `sbox[0x53] = 0xed`（第 117 行）。
   - 在 [aes_inv_sbox.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_inv_sbox.v) 里查找下标 `0xed`，确认 `inv_sbox[0xed] = 0x53`（第 270 行 `assign inv_sbox[8'hed] = 8'h53;`）。
   - 再自选 2~3 个字节重复上述「正向查 → 逆向反查」，确认 `inv_sbox[sbox[x]] = x` 恒成立。
2. **共享 vs 私挂验证（源码阅读）**：
   - 打开 [aes_core.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v)，确认全局只有第 138 行一个 `sbox_inst`，且 [第 184-194 行](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L184-L194) 的 `sbox_mux` 把它在 `keymem_sboxw` 与 `enc_sboxw` 之间分时。
   - 确认 [第 105-118 行](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L105-L118) 的 `dec_block` 端口里**没有** S-box 连线。
   - 打开 [aes_decipher_block.v:205](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L205)，确认解密自带 `inv_sbox_inst`。
3. **写一句话解释**：基于上面的观察，用自己的话回答「为什么 encipher 用共享 `sbox_inst`，而 decipher 用自己的 `inv_sbox_inst`」。

**需要观察的现象**：

- 对任意 `x`，`inv_sbox[sbox[x]]` 总能还原回 `x`——两张表确实是反函数。
- `aes_core` 全局只有一个正向 `sbox_inst`，被密钥扩展和加密分时共用；解密模块自带逆向 `inv_sbox_inst`，与共享 S-box 毫无连接。

**预期结果**：

- 至少 3 组互逆验证全部通过（如 `0x00↔0x63`、`0x01↔0x7c`、`0x53↔0xed`）。
- 解释应包含两个要点：① 正向/逆向是两张**不同**的常量表，物理上不能合并；② 正向表有 2 个不同时工作的消费者（值得共享），逆向表只有解密 1 个消费者（无处共享）。

> 说明：步骤 1、2 是纯源码阅读，可直接得出确定结论；若要用仿真打印对照（可选），可借助 [rtl/tb_aes_key_mem.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v) 中已例化的 `sbox`，其运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：已知 `sbox[0x35] = 0x96`（[aes_sbox.v:87](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_sbox.v#L87)）。不求仿真，直接说出 `inv_sbox[0x96]` 应该是多少，并说明依据。

> **答案**：`inv_sbox[0x96] = 0x35`。依据：正向与逆向 S-box 互为反函数，`S(0x35)=0x96` 蕴含 `S⁻¹(0x96)=0x35`。可在 [aes_inv_sbox.v:141](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_inv_sbox.v#L141) 验证（`assign inv_sbox[8'h96] = 8'h35;`）。

**练习 2**：假如要让解密通路也「共享」一个逆向 S-box（比如未来新增一个也需要逆向表的消费者），按照本工程的风格，你会怎么改 `aes_core`？

> **答案**：照搬正向 S-box 的做法——在 `aes_core` 里例化一个共享的 `aes_inv_sbox inv_sbox_inst(...)`，把 `dec_block` 改成像 `enc_block` 那样带 `.sword`/`.new_sword` 端口引出到 `aes_core`，再加一个 `inv_sbox_mux`（类似 `sbox_mux`）在新消费者与 `dec_block` 之间分时。前提是两个消费者**不同时**工作。目前没有第二个消费者，所以工程选择了「私挂」这个更简单的方案。

**练习 3**：密钥扩展在做**解密密钥**编排时，用的是正向 S-box 还是逆向 S-box？为什么？

> **答案**：用**正向** S-box。AES 的密钥扩展算法对加密和解密是**同一套**（都生成同一组轮密钥），其中的 SubWord 字节替换永远用正向表。所以 [aes_core.v:133-134](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L133-L134) 的 `keymem` 也接的是正向 `sbox_inst`，与加/解密方向无关。这也是逆向表唯独只有解密一个消费者的根本原因。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「S-box 侦察」小任务：

**任务**：假设你要给一个新同事画一张「AES 核里的 S-box 资源图」，请：

1. **列表**：在源码里数清楚，整个核一共有几个 S-box **模块实例**、分别是什么类型（正向/逆向）、分别长在哪个文件里、各被谁使用。
   - 参考答案骨架：正向 `sbox_inst`（`aes_core.v:138`，被 `key_mem` + `encipher` 经 `sbox_mux` 分时共享）；逆向 `inv_sbox_inst`（`aes_decipher_block.v:205`，仅被 `decipher` 私用）。
2. **画图**：画一张连接图，标出 `sbox_inst` 的输入 `muxed_sboxw` 来自 `sbox_mux` 的二选一（`keymem_sboxw` / `enc_sboxw`），输出 `new_sboxw` 同时连回 `keymem` 和 `enc_block`；再单独画出 `dec_block` 内部的 `inv_sbox_inst` 自闭环。
3. **验证**：从源码任选 5 个字节，列表证明 `inv_sbox[sbox[x]] = x`，作为「两张表互逆」的实证。
4. **反思**：用一句话回答——如果设计师「不在乎面积」，把 `encipher` 也改成自带一个正向 `sbox_inst`（不再与 `key_mem` 共享），整核的正向 S-box 数量会变成几个？这样做会牺牲什么、换来什么？
   - 参考方向：正向 S-box 实例从 1 个变成 2 个，面积增大，但省掉了 `sbox_mux` 的选择延迟（关键路径可能更短），且 `encipher` 与 `key_mem` 可潜在并行——这是「面积换速度/时序」的经典权衡，详见 [u3-l4](u3-l4-asic-design-tradeoffs.md)。

> 这个任务把「表的内容（4.1）」「并行结构（4.2）」「正反关系与共享私挂（4.3）」全部用到了。完成后，你就不仅看得懂 S-box 这两张表，还能讲清楚它们在整个核里的资源布局。

## 6. 本讲小结

- AES 的 S-box 本质是一张 **256 项的字节查找表**：源码用 `wire [7:0] sbox [0:255]` + 256 条 `assign` 常量来表达，是**纯组合、异步读**的查表电路（注释里的「ROM」是概念称呼）。
- 模块端口是 32 位，内部用 **4 条独立 `assign` 做 4 路并行查表**，于是一个周期就能替换完一个 32 位字（4 字节）；但这只是单模块内部的并行，整个核只有 1 个正向 S-box 实例。
- 正向表 `sbox` 与逆向表 `inv_sbox` 是**两张不同的常量表**，互为反函数：`inv_sbox[sbox[x]] = x`（如 `0x00↔0x63`、`0x53↔0xed`）。
- 正向 S-box 有 2 个消费者（`key_mem` + `encipher`）且不同时工作，故在 `aes_core` 里**共享**一个 `sbox_inst`，由 `sbox_mux` 按 `init_state` 分时。
- 解密模块 `dec_block` **不接**共享 S-box，而在自己内部私挂一个 `inv_sbox_inst`——因为逆向表只有解密这一个消费者，无处共享。
- 这套「正向共享、逆向私挂」是典型的**资源共享换面积/功耗**取舍，是理解后续 [u3-l4 ASIC 设计取舍](u3-l4-asic-design-tradeoffs.md) 的垫脚石。

## 7. 下一步学习建议

本讲解清楚了 S-box「这张表本身」。接下来建议：

1. **[u2-l3 密钥扩展与轮密钥存储](u2-l3-key-expansion-and-round-key-mem.md)**：去看正向 S-box 的第一个消费者 `key_mem` 是怎么用它做 SubWord 的，理解 `keymem_sboxw` / `new_sboxw` 在密钥编排里的角色。
2. **[u2-l4 加密数据通路四个变换函数](u2-l4-encipher-datapath-functions.md)**：去看正向 S-box 的第二个消费者 `encipher` 如何把 SubBytes 与 ShiftRows/MixColumns/AddRoundKey 串成一轮。
3. **[u2-l6 解密数据通路逆变换函数](u2-l6-decipher-datapath-inverse-functions.md)**：去看私挂的 `inv_sbox_inst` 在解密里如何参与 InvSubBytes。
4. 想深入「资源共享 vs 流水线化」的架构取舍，可跳读 [u3-l4 面向 ASIC 的设计取舍](u3-l4-asic-design-tradeoffs.md)，里面会系统讨论「逐字 SubBytes、单个 S-box」带来的面积/吞吐影响。
