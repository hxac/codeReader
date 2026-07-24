# CRC 包与并行实现（base/crc）

## 1. 本讲目标

本讲讲解 SURF 如何在 `base/crc/` 里集中实现并提供 CRC32 计算。读完本讲后，你应该能够：

1. 说清楚「CRC 是多项式除法」这一直觉，以及并行 CRC 为什么能把 32 拍串行运算压成 1 拍组合逻辑。
2. 看懂 [CrcPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/CrcPkg.vhd) 里 `crc32Parallel<N>Byte(crcCur, data)` 这一族纯函数的接口，并知道它们是怎么生成的。
3. 读懂 [Crc32Parallel.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32Parallel.vhd) 这一层「把包函数包成带复位、带输入寄存、带输出反转的时序模块」的标准写法，并指出它的串入/结果时序。
4. 理解为什么 SURF 把 CRC 集中放在一个 `CrcPkg` 里维护，而不是让每个协议核（以太网、PGP、packetizer）各自手写一遍 CRC。

本讲只覆盖 CRC32（标准多项式 0x04C11DB7），不展开其他多项式族。

## 2. 前置知识

### 2.1 CRC 是多项式除法

把一段报文看成系数在 GF(2)（即 {0,1}）上的多项式 \(M(x)\)：字节的每一位就是一个系数。再选一个「生成多项式」 \(G(x)\)，例如 CRC32 用的标准多项式：

\[
G(x) = x^{32} + x^{26} + x^{23} + x^{22} + x^{16} + x^{12} + x^{11} + x^{10} + x^{8} + x^{7} + x^{5} + x^{4} + x^{2} + x + 1
\]

写成 32 位常数就是 `0x04C11DB7`（最高位 \(x^{32}\) 是隐含的，不写进 32 位里）。CRC 的本质就是把「报文左移 32 位后」对 \(G(x)\) 求余：

\[
\text{CRC} = M(x)\cdot x^{32} \bmod G(x)
\]

由于 GF(2) 上「加法就是异或（XOR）」「减法也是异或」，整个除法过程不涉及进位，特别适合硬件用一组 XOR 门实现。校验时，把收到的「报文 + CRC」再除一次 \(G(x)\)，余数为 0 即认为没出错。

### 2.2 串行 LFSR 实现

最直观的硬件实现是「Galois 线性反馈移位寄存器（LFSR）」：用一个 32 位寄存器存当前余数，每来 1 个输入比特，就移位一次，并在多项式系数为 1 的抽头处做 XOR。这样 1 个比特 1 拍，算 N 字节需要 \(8N\) 拍。问题是：现代总线动辄 32/64/128 位，逐比特算太慢，时序也紧。

### 2.3 并行 CRC 的核心思想

既然每一拍的 LFSR 更新是「线性」的（输出每一位都是若干输入位和当前余数位的 XOR），那么把连续 \(k\) 拍的线性变换「叠加（展开）」一次，就能在 1 拍里同时吃进 \(k\) 个比特，直接得到下一拍的余数。这就是 `crc32Parallel<N>Byte` 这一族函数背后的数学——它们把 8/16/.../64 个比特（1~8 字节）的 LFSR 迭代手工展开成一张「每一位 = 哪些位做 XOR」的表。SURF 在包头注释里给出了生成这张表的参考工具（见 [CrcPkg.vhd:L11-L14](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/CrcPkg.vhd#L11-L14)）。

> 本讲需要的前置：u1-l4 的 `StdRtlPkg`（`sl`/`slv` 别名、`_G`/`_C`/`Type`/`_INIT_C` 命名、`TPD_G`/`RST_POLARITY_G`/`RST_ASYNC_G` 三个复位/时序泛型），以及 u1-l5 的双进程风格（`RegType`/`REG_INIT_C`/`r`/`rin`/`comb`/`seq`）。Crc32Parallel 正是用这套骨架把包函数包成时序模块的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [base/crc/rtl/CrcPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/CrcPkg.vhd) | CRC 包：声明并实现 `crcLfsrShift`、`crcByteLookup`，以及 `crc32Parallel1Byte`…`crc32Parallel8Byte` 八个并行纯函数。是全仓库 CRC 的「单一事实来源」。 |
| [base/crc/rtl/Crc32Parallel.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32Parallel.vhd) | 把 `CrcPkg` 的并行函数包成一个可综合的时序模块：输入字节转置、可选输入寄存、按字节宽度选函数、输出反转取反。多项式固定 0x04C11DB7。 |
| [base/crc/rtl/Crc32.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32.vhd) | 「通用多项式」版：结构与 Crc32Parallel 几乎一致，但逐字节调用 `crcByteLookup`，且多项式 `CRC_POLY_G` 可由泛型覆盖。用于需要非标准多项式的场合，速度比并行版慢。 |
| [base/crc/wrappers/Crc32PolyWrapper.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/wrappers/Crc32PolyWrapper.vhd) | 仅仿真用的薄封装：把一个整数泛型 `CRC_POLY_INT_G` 转成 `slv(31 downto 0)` 再传给 `Crc32`，绕过本地 GHDL 流不允许命令行直接覆盖向量泛型的限制。 |
| [ethernet/EthMacCore/rtl/EthCrc32Parallel.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthCrc32Parallel.vhd) | 复用范例：以太网 MAC 的 CRC 核直接调用同名 `crc32Parallel<N>Byte` 函数（经 `EthCrc32Pkg` 扩展到 16 字节），而不是重写 CRC。 |
| [tests/base/crc/crc_test_utils.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/crc/crc_test_utils.py) | Python 软件参考模型：逐字节复刻 RTL 的「反射 + 查表」CRC 递推，用于和硬件逐拍比对。 |

`base/crc/ruckus.tcl` 极短，只是 `loadSource -lib surf -dir "$::DIR_PATH/rtl"`（见 u1-l2 的 ruckus 清单），把整个 `rtl/` 目录登记进 `surf` 库，`wrappers/` 不进综合构建、仅供仿真。

---

## 4. 核心概念与源码讲解

### 4.1 CrcPkg 并行函数

#### 4.1.1 概念说明

`CrcPkg` 是一个**纯函数包**：里面没有任何信号、没有时序，只有函数声明与函数体。把 CRC 写成包函数有两个直接好处：

- **可被任意上下文调用**：测试台可以调，别的模块的 `comb` 进程里也可以调，甚至可以连续调多次做软件风格的逐字节计算（这正是 `Crc32.vhd` 的做法）。
- **集中维护**：多项式、生成方式、位序约定只有这一个地方说了算。`Crc32Parallel`（硬件并行核）和 `Crc32`（通用多项式核）共用同一套 `crcByteLookup` 与同一套并行函数，协议核再复用 `Crc32Parallel`，于是「CRC 怎么算」这件事在全仓库只有一版真相。

包里一共暴露三类函数（见 [CrcPkg.vhd:L31-L46](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/CrcPkg.vhd#L31-L46)）：

| 函数 | 入参 | 用途 |
| --- | --- | --- |
| `crcLfsrShift(lfsr, poly, input)` | 当前余数、多项式、1 个输入比特 | 单拍 Galois LFSR 移位；「nondirect」实现，需对报文补 0。 |
| `crcByteLookup(inByte, poly)` | 1 字节数据、多项式 | 表驱动法：一次性把 1 字节（8 拍）的 LFSR 效果算成一个 32 位查表值。 |
| `crc32Parallel1Byte…8Byte(crcCur, data)` | 当前 32 位余数、N 字节数据 | **本讲主角**：把 8/16/.../64 比特的 LFSR 迭代展开成一张 XOR 表，1 拍算完。多项式固定 0x04C11DB7。 |

#### 4.1.2 核心流程

先看最底层的两块积木，再理解并行函数是怎么从它们「长」出来的。

**(a) Galois LFSR 单拍移位**（[CrcPkg.vhd:L67-L105](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/CrcPkg.vhd#L67-L105)）：

伪代码（降序索引方向）：

```
function crcLfsrShift(lfsr, poly, input):
    for i in 0..31:
        if poly(i) == '1':           # 这是多项式的抽头
            retVar(i) = lfsr(31) xor (lfsr(i-1) if i>0 else input)
        else:
            retVar(i) = (lfsr(i-1) if i>0 else input)
    return retVar
```

关键点：最高位 `lfsr(left)`（降序时即 `lfsr(31)`）是「反馈源」——每拍它决定要不要把多项式 XOR 进来；新数据从最低位端移入。注释明确指出这是 nondirect 实现，要算标准 CRC 必须给报文「补零」（见 [CrcPkg.vhd:L60-L64](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/CrcPkg.vhd#L60-L64)）。函数同时对升序/降序两种位序做了分支处理，方便不同总线约定复用。

**(b) 字节查表**（[CrcPkg.vhd:L113-L146](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/CrcPkg.vhd#L113-L146)）：把 1 字节（8 拍）的 LFSR 效果折叠成一次「移 8 位 + 条件 XOR 多项式」的循环，等价于一张 256 项的查表。`Crc32.vhd` 就是逐字节调它来支持任意多项式（见 4.1.3）。

**(c) 并行函数**：`crc32Parallel<N>Byte` 是把 (a) 的 LFSR 连续迭代 \(8N\) 拍后，把「输出第 i 位 = 哪些输入位 XOR 哪些余数位」这张表**预先离线算好、直接写死成代码**。由于是纯线性（GF(2)）运算，展开后每一位都形如：

\[
\text{retVar}(i) = \bigoplus_{j \in S_i^{\text{data}}} \text{data}(j) \;\oplus\; \bigoplus_{k \in S_i^{\text{crc}}} \text{crcCur}(k)
\]

其中 \(S_i^{\text{data}}\)、\(S_i^{\text{crc}}\) 是离线算出的下标集合。

#### 4.1.3 源码精读

并行函数的声明——注意 8 个函数的签名完全同构，只是 `data` 位宽递增（[CrcPkg.vhd:L36-L44](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/CrcPkg.vhd#L36-L44)）：

```vhdl
--Specific CRC32 parallel implementations with the standard polynomial: 0x04C11DB7
function crc32Parallel1Byte (crcCur : slv(31 downto 0); data : slv(7 downto 0)) return slv;
function crc32Parallel2Byte (crcCur : slv(31 downto 0); data : slv(15 downto 0)) return slv;
...
function crc32Parallel8Byte (crcCur : slv(31 downto 0); data : slv(63 downto 0)) return slv;
```

函数体就是一张巨大的 XOR 表。以 1 字节版为例（[CrcPkg.vhd:L151-L187](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/CrcPkg.vhd#L151-L187)），只看头两行：

```vhdl
retVar(0) := data(0) xor data(6) xor crcCur(24) xor crcCur(30);
retVar(1) := data(0) xor data(1) xor data(6) xor data(7)
             xor crcCur(24) xor crcCur(25) xor crcCur(30) xor crcCur(31);
```

这段代码做了什么：给定当前 32 位余数 `crcCur` 和 1 字节新数据 `data`，直接算出下一拍的 32 位新余数。`retVar(0)` 这一比特的新值，等于 `data` 的第 0、6 位与 `crcCur` 的第 24、30 位相 XOR——这正是 8 拍 LFSR 迭代展开后，最低位的线性依赖关系。2~8 字节版的逻辑完全相同，只是 `data` 位宽更大、依赖的 `data(j)` 下标更多（例如 4 字节版见 [CrcPkg.vhd:L265-L301](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/CrcPkg.vhd#L265-L301)）。

作为对照，「通用多项式」的 `Crc32.vhd` 不用这张展开表，而是在 `comb` 里逐字节查表（[Crc32.vhd:L123-L131](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32.vhd#L123-L131)）：

```vhdl
for byte in BYTE_WIDTH_G-1 downto 0 loop
   if (byteWidth >= BYTE_WIDTH_G-byte-1) then
      byteXor := v.crc(31 downto 24) xor data((byte+1)*8-1 downto byte*8);
      v.crc   := (v.crc(23 downto 0) & x"00") xor crcByteLookup(byteXor, CRC_POLY_G);
   end if;
end loop;
```

这段代码做了什么：每次取当前余数最高字节与新数据字节 XOR，再查 `crcByteLookup` 得到 32 位修正值，把余数左移 1 字节后异或上去。由于多项式 `CRC_POLY_G` 是泛型（[Crc32.vhd:L42](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32.vhd#L42)），它不能预先展开，只能运行时逐字节算——这正是它与 `Crc32Parallel`（多项式写死、可展开、更快）的根本区别。

#### 4.1.4 代码实践

**实践目标**：用源码阅读理解「并行函数 = 串行 LFSR 的展开」。

**操作步骤**：

1. 打开 [CrcPkg.vhd:L67-L105](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/CrcPkg.vhd#L67-L105) 的 `crcLfsrShift`，确认它实现的是「最高位反馈、多项式抽头 XOR、新比特从低位移入」。
2. 在 [CrcPkg.vhd:L151-L161](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/CrcPkg.vhd#L151-L161) 中找到 `retVar(22) := data(0) xor crcCur(14) xor crcCur(24);`，注意它对 `data(0)` 的依赖——这是 1 字节（8 比特）数据中第 0 比特经过 8 拍 LFSR 后「漏」到第 22 位的体现。
3. 对比 [Crc32.vhd:L123-L131](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32.vhd#L123-L131) 的逐字节查表循环，体会「可展开（多项式写死）vs 不可展开（多项式泛型）」的取舍。

**需要观察的现象**：并行函数体里没有 `for` 循环、没有条件分支，只有一行行 `retVar(i) := ... xor ...`；而通用版 `Crc32` 用了 `for byte in ... loop` 的运行时循环。

**预期结果**：能用自己的话讲清——并行函数把循环展开成了组合逻辑，所以更快、面积更大，但多项式被固化了。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `crc32Parallel<N>Byte` 的多项式被写死成 `0x04C11DB7`，而 `Crc32.vhd` 却把多项式做成泛型 `CRC_POLY_G`？
**答案**：并行函数的本质是把「特定多项式下 N 拍 LFSR」的线性变换预先离线展开成 XOR 表；换一个多项式，整张表就作废，必须重新生成。所以它只能在多项式固定时使用。`Crc32.vhd` 用运行时逐字节查表 `crcByteLookup(byteXor, CRC_POLY_G)`，多项式是运行期参数，自然可以泛型化，代价是慢。

**练习 2**：`crc32Parallel1Byte` 的入参 `data` 是 `slv(7 downto 0)`，而 `crc32Parallel4Byte` 是 `slv(31 downto 0)`。如果调用方只给了 3 字节有效数据，能直接调 `crc32Parallel4Byte` 吗？
**答案**：不能直接调——必须按「有效字节数」选对应函数（3 字节就用 `crc32Parallel3Byte`），否则高位会引入错误的 0 或残留数据。`Crc32Parallel` 模块正是用 `crcDataWidth` 来在 `case` 里选对函数（见 4.2）。

---

### 4.2 Crc32Parallel 模块

#### 4.2.1 概念说明

`CrcPkg` 的函数是「无状态的纯运算」：给余数、给数据，返回新余数。但真实协议核需要的是一个**时序模块**：有时钟、有复位、能一拍一拍地把数据喂进去、维护一个持续运行的余数，最后输出「成品 CRC」。`Crc32Parallel` 就是这层外壳。它把：

- 输入字节按位**反射（转置）**；
- 可选地**寄存一拍输入**（`INPUT_REGISTER_G`）以改善时序；
- 根据「本拍有效几字节」在 8 个并行函数里**选一个**调用；
- 维护余数寄存器 `r.crc`，复位时装载 `CRC_INIT_G`；
- 把最终余数**按字节反射再取反**输出（等价于与 `0xFFFFFFFF` 异或）。

它沿用 u1-l5 的双进程骨架，只是 `comb` 里把核心运算委托给了包函数。

#### 4.2.2 核心流程

```
每拍 (crcClk 上升沿)：
  seq:  r <= rin              # 打寄存器（异步/同步复位见 RST_ASYNC_G）

comb (纯组合)：
  1. v := r                                    # 复制现态
  2. 把 crcIn 按字节做位反射写入 v.data         # 转置（见 4.2.3）
     └─ 按 crcDataWidth 决定哪些字节有效，无效字节填 0
  3. 选择本次运算用的输入：
     └─ INPUT_REGISTER_G=true  → 用上一拍的 r.data/r.valid/r.byteWidth
        INPUT_REGISTER_G=false → 用本拍的 v.data/v.valid/v.byteWidth
  4. 决定 prevCrc：
     └─ crcReset='1' → prevCrc = crcInit        # 重置到初值
        crcReset='0' → prevCrc = r.crc          # 接着上一拍算
  5. if (valid='1') 按 byteWidth case 调用
        crc32Parallel1Byte..8Byte(prevCrc, data) → v.crc
     else v.crc := prevCrc                      # 空拍保持
  6. rin <= v
  7. 输出：
     crcRem <= r.crc                            # 内部原始余数
     crcOut <= 反射+取反(r.crc)                  # 成品 CRC（与 0xFFFFFFFF 异或）
```

`crcDataWidth` 的编码是「字节数减 1」：`"000"`=1 字节、`"001"`=2 字节、…、`"111"`=8 字节（[Crc32Parallel.vhd:L54](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32Parallel.vhd#L54)）。

**时序要点（串入/结果）**：`crcOut`/`crcRem` 永远反映「到上一拍为止」累加好的余数（寄存输出）。当 `INPUT_REGISTER_G=true`（默认）时，输入数据先在 `r.data` 里落一拍，于是从 `crcIn` 呈现到 `crcRem` 更新之间多一个时钟周期的流水延迟；`INPUT_REGISTER_G=false` 则把这一拍省掉，时序更紧但延迟更小。

#### 4.2.3 源码精读

实体泛型与端口（[Crc32Parallel.vhd:L40-L58](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32Parallel.vhd#L40-L58)）：

```vhdl
generic (
   BYTE_WIDTH_G     : positive         := 4;        -- 最大字节宽度 1..8
   INPUT_REGISTER_G : boolean          := true;     -- 是否在输入端加一拍寄存
   CRC_INIT_G       : slv(31 downto 0) := x"FFFFFFFF");  -- 余数初值
port (
   crcOut       : out slv(31 downto 0);             -- 成品 CRC
   crcRem       : out slv(31 downto 0);             -- 内部余数（未做最后反射/取反）
   crcClk       : in  sl;
   crcDataValid : in  sl;                            -- 本拍是否有新数据
   crcDataWidth : in  slv(2 downto 0);               -- 有效字节数-1
   crcIn        : in  slv((BYTE_WIDTH_G*8-1) downto 0);
   crcInit      : in  slv(31 downto 0) := CRC_INIT_G;  -- 运行时可覆盖初值
   crcReset     : in  sl);                           -- 把余数强置为 crcInit
```

这段代码做了什么：定义了一个最大支持 `BYTE_WIDTH_G` 字节（1~8）宽度的并行 CRC 核。注意三个复位/时序泛型 `TPD_G`/`RST_POLARITY_G`/`RST_ASYNC_G` 沿用全仓库约定（见 u1-l4），`crcReset` 是「业务复位」（重新装载初值），与「上电复位」`crcPwrOnRst` 是两件事。

状态与初值（[Crc32Parallel.vhd:L62-L75](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32Parallel.vhd#L62-L75)）：

```vhdl
type RegType is record
   crc       : slv(31 downto 0);
   data      : slv((BYTE_WIDTH_G*8-1) downto 0);
   valid     : sl;
   reset     : sl;
   byteWidth : slv(2 downto 0);
end record RegType;
constant REG_INIT_C : RegType := (crc => CRC_INIT_G, data => (others=>'0'), ...);
```

这段代码做了什么：把「运行余数 + 缓存的输入 + 控制位」打包成一个 `RegType`，并用 `REG_INIT_C` 把余数上电初值设为 `CRC_INIT_G`。`r : RegType := REG_INIT_C` 与 `rin : RegType` 正是 u1-l5 的现态/次态双信号。

输入字节位反射（转置）（[Crc32Parallel.vhd:L100-L109](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32Parallel.vhd#L100-L109)）：

```vhdl
for byte in (BYTE_WIDTH_G-1) downto 0 loop
   if (crcDataWidth >= BYTE_WIDTH_G-byte-1) then
      for b in 0 to 7 loop
         v.data((byte+1)*8-1-b) := crcIn(byte*8+b);   -- 每字节内比特顺序反转
      end loop;
   else
      v.data((byte+1)*8-1 downto byte*8) := (others => '0');  -- 无效字节填 0
   end if;
end loop;
```

这段代码做了什么：标准 CRC32 要求「先按字节反射再送入除法器」。这里用双重循环把每个字节内的 8 个比特顺序翻转后放进 `v.data`，同时按 `crcDataWidth` 把不存在的低字节填 0，保证有效字节落在总线的最高位侧。

按宽度选函数（核心运算）（[Crc32Parallel.vhd:L135-L172](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32Parallel.vhd#L135-L172)）：

```vhdl
if (valid = '1') then
   case (byteWidth) is
      when "000" => v.crc := crc32Parallel1Byte(prevCrc, data(...));
      when "001" => if (BYTE_WIDTH_G >= 2) then
                       v.crc := crc32Parallel2Byte(prevCrc, data(...)); end if;
      ...
      when "111" => if (BYTE_WIDTH_G = 8) then
                       v.crc := crc32Parallel8Byte(prevCrc, data(...)); end if;
      when others => v.crc := (others => '0');
   end case;
else
   v.crc := prevCrc;        -- 空拍保持余数不变
end if;
```

这段代码做了什么：这是模块与包函数的唯一接缝——根据本拍有效字节数 `byteWidth` 调用 4.1 里对应的并行函数，把 `prevCrc` 与新数据折成新余数。`if (BYTE_WIDTH_G >= N)` 是编译期守卫：若实体没配那么宽，就把对应分支空着（综合时被优化掉）。`assert (BYTE_WIDTH_G > 0 and BYTE_WIDTH_G <= 8)`（[Crc32Parallel.vhd:L82](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32Parallel.vhd#L82)）在 elaboration 时把非法宽度挡掉。

输出反射+取反（[Crc32Parallel.vhd:L180-L186](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32Parallel.vhd#L180-L186)）：

```vhdl
for byte in 0 to 3 loop
   for b in 0 to 7 loop
      crcOut(byte*8+b) <= not(r.crc((byte+1)*8-1-b));
   end loop;
end loop;
```

这段代码做了什么：把内部余数 `r.crc` 再做一次「按字节反射」，并对每一位取反。注释指出这等价于与 `0xFFFFFFFF` 异或（[Crc32Parallel.vhd:L180-L181](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32Parallel.vhd#L180-L181)）。这一步 + 输入端反射，合起来正是标准以太网/CRC-32 的「反射输入、反射输出、最终取反」约定，使本模块的 `crcOut` 能与 IEEE 802.3 的 FCS 直接对齐。

时序进程 `seq`（[Crc32Parallel.vhd:L190-L201](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32Parallel.vhd#L190-L201)）就是 u1-l5 的标准骨架：`RST_ASYNC_G=true` 走异步复位分支、`=false` 走同步复位分支，上升沿 `r <= rin after TPD_G`，几乎不做别的逻辑判断。

#### 4.2.4 代码实践

> 本实践对应规格里「用 CrcPkg 的函数对一段固定字节流计算 CRC32，并与 Crc32Parallel 模块逐拍输出的结果做对照」。仓库已经提供了现成的 cocotb 回归测试来做这件事——`tests/base/crc/test_Crc32Parallel.py` 配 `crc_test_utils.py` 就是「软件算一遍 ↔ 硬件逐拍算一遍 ↔ 比对」的参考实现。

**实践目标**：运行回归测试，验证硬件 `crcRem` 逐拍输出与软件模型逐字节折算的结果完全一致；并读懂软件模型如何复刻 `CrcPkg` 的运算。

**操作步骤**：

1. 按 u1-l2 / u9-l1 的工具链，先生成 cocotb 源缓存（**待本地验证**：具体命令依赖本地 ruckus/GHDL 环境，形如 `make MODULES=$PWD import`）。
2. 运行本测试（**待本地验证**：路径与虚拟环境以本地为准）：

   ```bash
   ./.venv/bin/python -m pytest -q tests/base/crc/test_Crc32Parallel.py
   ```
3. 打开软件模型 [crc_test_utils.py:L33-L54](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/crc/crc_test_utils.py#L33-L54)，对照 4.2.3 的 RTL：
   - `reverse_bits` 做字节内比特反射 ↔ RTL 的输入转置循环；
   - `crc_byte_lookup` 复刻 `crcByteLookup` 的查表；
   - `crc_update` 把余数左移 1 字节、异或查表值，逐字节推进 ↔ `Crc32.vhd` 的循环（等价于并行函数的展开结果）。
4. 看 [crc_test_utils.py:L57-L65](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/crc/crc_test_utils.py#L57-L65) 的 `crc_out_from_remainder`：它做「按字节反射 + 取反」，正是 RTL 输出端 `crcOut` 的 Python 镜像。
5. 在 [test_Crc32Parallel.py:L41-L68](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/crc/test_Crc32Parallel.py#L41-L68) 的 `crc_sequence_test` 里看断言 `assert crc_rem == remainder` 与 `assert crc_out == crc_out_from_remainder(remainder)`——这就是「软件 fold」与「硬件逐拍输出」的逐拍对照。

**需要观察的现象**：每喂一组字节，硬件 `crcRem` 都应等于软件模型同步推进后的余数；不喂数据（`crcDataValid=0`）的空拍里，`crcRem` 保持不变。

**预期结果**：参数扫描（1/4/8 字节、寄存/不寄存输入、同步/异步复位，见 [test_Crc32Parallel.py:L119-L144](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/crc/test_Crc32Parallel.py#L119-L144)）全部通过。若本地没有工具链，可退化为「源码阅读型实践」：手动用 `crc_update` 在 Python 里算 `[0x12]`、`[0x34,0x56]` 的余数，再画出 RTL 里 `prevCrc`、`v.crc` 在对应拍的变化。

> 说明：`Crc32PolyWrapper.vhd`（[Crc32PolyWrapper.vhd:L24-L43](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/wrappers/Crc32PolyWrapper.vhd#L24-L43)）是这个测试套件里「通用多项式」核 `Crc32` 的仿真外壳：它把整数泛型 `CRC_POLY_INT_G` 用 `toSlv(...)` 转成 `slv(31 downto 0)`（[Crc32PolyWrapper.vhd:L47](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/wrappers/Crc32PolyWrapper.vhd#L47)）再传给 `Crc32`，仅因为本地 GHDL 流不允许命令行直接覆盖向量泛型（见其文件头注释 [Crc32PolyWrapper.vhd:L4-L8](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/wrappers/Crc32PolyWrapper.vhd#L4-L8)）。

#### 4.2.5 小练习与答案

**练习 1**：`crcRem` 和 `crcOut` 都基于同一个寄存器 `r.crc`，为什么数值不同？
**答案**：`crcRem <= r.crc` 是「原始内部余数」；`crcOut` 是把 `r.crc` 做了「按字节反射 + 逐位取反」后的「成品 CRC」。两者是同一余数的不同呈现，校验和用 `crcOut`，调试看内部状态用 `crcRem`。

**练习 2**：默认 `INPUT_REGISTER_G=true` 时，从 `crcIn` 上出现一拍有效数据，到 `crcRem` 反映出这一拍的影响，中间隔几拍？
**答案**：隔 1 拍。因为 `INPUT_REGISTER_G=true` 时 `comb` 用的是已经寄存过的 `r.data`/`r.valid`/`r.byteWidth`（[Crc32Parallel.vhd:L112-L116](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32Parallel.vhd#L112-L116)），数据要先落进 `r.data`，下一拍才参与 `v.crc` 运算，再下一拍出现在 `crcRem`。测试台 `apply_transaction` 里因此多调了一次 `cycle(1)`（[crc_test_utils.py:L156-L161](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/crc/crc_test_utils.py#L156-L161)）。

---

### 4.3 CRC 复用约定

#### 4.3.1 概念说明

CRC 是个「到处都要、写错就全链路出错」的基础设施。如果把 CRC 散落到每个协议核里各自实现，会出现三件麻烦：多项式/位序约定可能不一致、修改时要改很多地方、测试无法集中。SURF 的做法是把 CRC 沉到 `base/crc/` 这一层，并形成两条清晰的复用路径：

1. **实例化模块**：需要「一个持续运行的 CRC 寄存器」的核，直接例化 `Crc32Parallel`（多项式标准、1~8 字节）或 `Crc32`（多项式可配、较慢）。
2. **直接调包函数**：已经在 `comb` 里自己管寄存器、只需要「算一步」的核，可以直接调用 `crc32Parallel<N>Byte(prevCrc, data)`。

这两种方式背后是同一份 `CrcPkg`，保证了「CRC 怎么算」在全仓库只有一个真相。

#### 4.3.2 核心流程

```
CrcPkg (单一事实来源)
   ├── Crc32Parallel  ── 实例化 ──▶ 以太网 RX/TX、PGP4、packetizer2 ...
   ├── Crc32          ── 实例化 ──▶ 需要非标准多项式的核
   └── crc32ParallelNByte ──直接调用──▶ EthCrc32Parallel 等已在 comb 自管寄存器的核
```

需要特别指出：以太网 MAC 用的 [EthCrc32Parallel.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthCrc32Parallel.vhd) 并没有把 `base/crc` 的模块再包一层，而是**直接调用同名包函数**。它 `use surf.EthCrc32Pkg.all`，而 `EthCrc32Pkg` 沿用了完全相同的命名与签名 `crc32Parallel1Byte(crcCur, data)`（[EthCrc32Pkg.vhd:L27](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthCrc32Pkg.vhd#L27)），并把这套机制从 1~8 字节扩展到 1~16 字节以支持更宽的以太网总线（如 128 位 XGMII）。这正说明：**函数命名/签名约定本身就是一种跨包复用契约**。

#### 4.3.3 源码精读

以太网 CRC 核复用包函数的现场——在它自己的 `case` 里调用 `crc32Parallel4Byte`（[EthCrc32Parallel.vhd:L196](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthCrc32Parallel.vhd#L196)）：

```vhdl
when x"3" =>                -- 4 Byte (32-bits)
   if (BYTE_WIDTH_G >= 4) then
      ...
      else
         v.crc := crc32Parallel4Byte(prevCrc, r.data(BYTE_WIDTH_G*8-1 downto (BYTE_WIDTH_G-4)*8));
   end if;
```

这段代码做了什么：以太网 CRC 核的 `comb` 自己维护 `r.crc`/`r.data`/`r.valid`，遇到 4 字节有效数据时，直接调用 `crc32Parallel4Byte`——和 `base/crc/Crc32Parallel.vhd:L149` 里那一行是同一个函数。换言之，MAC 核复用了 `base/crc` 的「算法」，只是把「外壳」（支持 16 字节、可选 DSP 实现、不同的复位接入）按以太网需求重写了一遍。

`base/crc` 这一层的模块则被更上层的协议核直接例化，例如 `protocols/packetizer/rtl/AxiStreamPacketizer2.vhd`、`protocols/pgp/pgp4/core/rtl/Pgp4TxLiteProtocol.vhd`、`ethernet/EthMacCore/rtl/EthMacRxImportGmii.vhd` 等都实例化了 `Crc32Parallel`。它们都不自己写 CRC。

> 为什么「集中放包里」比「叶子模块各写」好？三点：(1) 多项式 `0x04C11DB7` 与反射/取反约定只有一处定义，不会出现「A 核和 B 核 CRC 对不上」；(2) 修改 CRC（例如换生成器、修 bug）只动 `CrcPkg` + `Crc32Parallel`，协议核自动跟随；(3) 测试集中——`tests/base/crc/` 一次性回归 `Crc32Parallel`、`Crc32`、`CRC32Rtl`，所有复用方都受益于这套回归。

#### 4.3.4 代码实践

**实践目标**：实地确认「协议核没有自己写 CRC，而是复用了 `base/crc`」。

**操作步骤**：

1. 在 `protocols/` 与 `ethernet/` 下搜索谁实例化了 `Crc32Parallel`：

   ```bash
   grep -rl "entity surf.Crc32Parallel" protocols/ ethernet/
   ```
2. 任选一个命中文件（例如 `ethernet/EthMacCore/rtl/EthCrc32Parallel.vhd` 或 packetizer 的某个核），打开它实例化 `Crc32Parallel` 的那一行，记录它把 `BYTE_WIDTH_G` 配成了多少、把哪条总线的 `crcIn`/`crcDataValid`/`crcDataWidth` 接了进去。
3. 对照 [Crc32Parallel.vhd:L40-L58](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32Parallel.vhd#L40-L58) 的端口表，确认该核没有重写任何 CRC 运算逻辑。

**需要观察的现象**：命中文件里只有「例化 + 端口映射 + 生成 `crcDataWidth`/`crcDataValid`」的胶水代码，看不到任何 `xor`、`crcByteLookup`、`crc32Parallel` 之类的 CRC 算式。

**预期结果**：能列出至少 2 个协议核实例化 `Crc32Parallel`，并说清各自的 `BYTE_WIDTH_G` 取值（**待本地验证**：以仓库当前 HEAD 的 grep 结果为准）。

#### 4.3.5 小练习与答案

**练习 1**：如果一个新协议需要 16 字节宽的 CRC32，应该直接改 `Crc32Parallel` 把 `BYTE_WIDTH_G` 上限改成 16 吗？
**答案**：不应该。`CrcPkg` 只生成到 `crc32Parallel8Byte`，且 `Crc32Parallel` 用 `assert` 把宽度限制在 1~8（[Crc32Parallel.vhd:L82](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32Parallel.vhd#L82)）。正确做法是仿照 `EthCrc32Parallel` + `EthCrc32Pkg`：沿用相同的函数命名/签名约定，把并行函数扩展到 9~16 字节，在自己的包/模块里维护，而不是动 `base/crc` 这层公共基石。

**练习 2**：`crcByteLookup` 和 `crc32Parallel<N>Byte` 都实现了 CRC，为什么 `CrcPkg` 要同时保留两者？
**答案**：它们服务于不同场景。`crcByteLookup` 是「运行时、逐字节、多项式可配」的积木，被 `Crc32.vhd` 用来支持任意多项式；`crc32Parallel<N>Byte` 是「编译期展开、多项式固定、1 拍完成」的快路径，被 `Crc32Parallel.vhd` 和以太网 CRC 核用来追求吞吐和时序。同一个包提供两种「速度 vs 灵活性」的取舍，复用方按需挑选。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个小任务：**给一条「变宽」的字节流手算 CRC32，并在源码里追踪它如何流经 `CrcPkg → Crc32Parallel → crcOut`。**

1. **准备一段固定报文**：比如 5 字节 `[0xDE, 0xAD, 0xBE, 0xEF, 0x00]`。
2. **软件折算**（参照 [crc_test_utils.py:L46-L54](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/crc/crc_test_utils.py#L46-L54)）：
   - 取初值 `0xFFFFFFFF`；
   - 对每个字节先 `reverse_bits` 反射，再 `crc_byte_lookup` 推进余数；
   - 最后用 `crc_out_from_remainder`（[crc_test_utils.py:L57-L65](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/crc/crc_test_utils.py#L57-L65)）得到 `crcOut`。记下这个值。
3. **决定硬件怎么吃**：因为这 5 字节超过 4 字节，需要把 `Crc32Parallel` 配成 `BYTE_WIDTH_G=8`，分两拍喂入——第 1 拍 `crcDataWidth="100"`（5 字节）放高 5 字节，或者拆成「4 字节 + 1 字节」两拍（`crcDataWidth="011"` 再 `"000"`）。在 [Crc32Parallel.vhd:L135-L172](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32Parallel.vhd#L135-L172) 里指出：第 1 拍会调用哪个 `crc32Parallel<N>Byte`，第 2 拍调用哪个，`prevCrc` 如何从第 1 拍的 `r.crc` 接力到第 2 拍。
4. **追踪输出**：最后一拍结束后，`crcRem` 等于第 2 步的「反射前余数」，`crcOut` 等于第 2 步的「成品 CRC」。在 [Crc32Parallel.vhd:L178-L186](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/crc/rtl/Crc32Parallel.vhd#L178-L186) 指出两者的差别就是「是否做了最后的反射+取反」。
5. **回归验证**（可选，**待本地验证**）：把这段报文写进一个临时 cocotb 用例，复用 `CrcStreamingTB.apply_transaction`，断言读回的 `crcOut` 与第 2 步手算值相等。

通过这个任务，你会同时用到：并行函数的接口（4.1）、模块的时序与转置/输出（4.2）、以及「协议核复用同一份算法」的约定（4.3）。

## 6. 本讲小结

- CRC 的数学本质是 GF(2) 上的多项式除法 \(M(x)\cdot x^{32} \bmod G(x)\)，标准 CRC32 多项式 `0x04C11DB7`。
- `CrcPkg` 把 CRC 沉成一个纯函数包：底层是 `crcLfsrShift`（单拍 Galois LFSR）与 `crcByteLookup`（字节查表），上层是 `crc32Parallel1Byte…8Byte` 八个把 LFSR 展开成 XOR 表的并行函数。
- 并行函数的本质是「把 N 拍线性变换离线展开成一张 `retVar(i) := ... xor ...` 表」，所以快、但多项式被固化；通用版 `Crc32.vhd` 用 `crcByteLookup` 逐字节算，多项式可配但慢。
- `Crc32Parallel` 是把包函数包成时序模块的外壳：双进程骨架（u1-l5）+ 输入字节反射 + `INPUT_REGISTER_G` 可选流水 + `case` 选函数 + 输出反射取反；`crcRem` 是原始余数、`crcOut` 是成品 CRC。
- 复用约定：协议核（以太网、PGP4、packetizer 等）要么实例化 `Crc32Parallel`，要么直接调同名包函数（如 `EthCrc32Parallel` 扩展到 16 字节），从不重写 CRC——`base/crc` 是全仓库 CRC 的单一事实来源。
- 仓库自带 cocotb 回归（`tests/base/crc/test_Crc32Parallel.py` + `crc_test_utils.py`）即「软件逐字节 fold ↔ 硬件逐拍输出」的对照实验，参数扫描覆盖 1/4/8 字节、寄存/不寄存、同步/异步复位。

## 7. 下一步学习建议

- **横向对比另一套 CRC 实现**：阅读 [ethernet/EthMacCore/rtl/EthCrc32Parallel.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthCrc32Parallel.vhd) 与 `EthCrc32Pkg.vhd`，看它如何把同一套函数签名约定扩展到 1~16 字节、并可选 `USE_DSP_G` 用 DSP48 做 XOR——这是「沿用复用契约再扩展」的范例。
- **进入数据成帧层**：本讲的 CRC 是 u5-l4（Packetizer/Batcher/线路码/ECC）和 u6-l1（以太网 MAC）的校验基础。建议接着读 `AxiStreamPacketizer2.vhd` 里实例化 `Crc32Parallel` 的片段，看 CRC 如何在成帧时挂到帧尾。
- **跑一遍回归**：按 u9-l1（cocotb 工具链）搭好环境后，运行 `tests/base/crc/` 全目录回归，对照三个测试（`test_Crc32Parallel`、`test_Crc32`、`test_CRC32Rtl`）理解「并行 / 通用 / 老版」三种实现的等价性。
- **深入生成原理**：若对「XOR 表怎么来的」感兴趣，可参照 `CrcPkg.vhd` 头部注释里的 outputlogic.com 文献与 SLAC 内部生成脚本，尝试用 Python 为一个小多项式（如 CRC-8）自行生成一张并行表，加深对 4.1 数学展开的理解。
