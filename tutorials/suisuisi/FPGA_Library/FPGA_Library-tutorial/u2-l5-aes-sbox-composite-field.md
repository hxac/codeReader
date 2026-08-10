# S-Box 的复合域 GF(2^4) 高效实现

## 1. 本讲目标

本讲是 AES 数据通路里最「数学」的一讲，专讲单字节 S-Box（模块 `bSbox`）的内部实现。学完后你应该能够：

- 说清楚「为什么不用查表（ROM）实现 S-Box，而要用复合域（composite field）运算」；
- 看懂本仓库 `gf_s_box/` 目录里那一堆 `gf_*` 模块如何把 GF(2⁸) 上的求逆，层层递归分解到 GF(2⁴)、再到 GF(2²)；
- 画出从 `bSbox` 到最底层 `gf_inv_2` 的完整调用层次树，并指出每个模块对应哪一种数学运算；
- 理解「平方、求逆、标量乘」在 GF(2²) 正规基下可以退化成简单连线这一关键恒等式；
- 学会批判地阅读这份草稿级 RTL，识别其中文件名/模块名不一致、testbench 重复例化等问题，并知道应以仿真和黄金模型为准。

本讲承接 [u2-l2](u2-l2-aes-subbytes-shiftrows.md)：那一讲告诉你 `bSbox` 是「单字节 S-Box、用 Canright 复合域算法、加解密复用」，但把盒子内部当黑盒；本讲就把这个黑盒彻底打开。

## 2. 前置知识

阅读本讲前，请确保你已经理解以下概念（前几讲已建立）：

- **AES 的字节代换**：SubBytes 对状态矩阵的每个字节做非线性替换 `S(x) = Affine(x⁻¹)`，其中 `x⁻¹` 是 GF(2⁸) 上的乘法逆元，`0` 的逆元约定为 `0`。安全性主要来自这一步的非线性。
- **GF(2⁸) 伽罗瓦域**：AES 用不可约多项式 \(m(x)=x^8+x^4+x^3+x+1\) 定义 GF(2⁸)，域元素的加法是按位异或，乘法是多项式乘法后对 \(m(x)\) 取模（详见 [u2-l3](u2-l3-aes-mixcolumns-gf.md) 里对 `POLYNOMIAL_IRR=0x1B` 与 xtime 的讲解）。
- **encrypt 复用信号**：本仓库的 `bSbox` 模块靠一个 `encrypt` 输入在「正向 S-Box」与「逆向 S-Box」之间切换，同一条数据通路既加密又解密（见 [u2-l2](u2-l2-aes-subbytes-shiftrows.md)）。
- **正规基（normal basis）**：表示有限域元素的一种基底选择。本讲会用到 GF(2²) 的正规基 \(\{\beta,\beta^2\}\)，它有一个神奇性质——**平方和求逆都退化成系数位置互换**。我们会在 4.4 节严格证明。

本讲全程用到的符号约定：

| 符号 | 含义 |
|------|------|
| GF(2ⁿ) | 含 \(2^n\) 个元素的伽罗瓦域 |
| GF((2⁴)²) | 把 GF(2⁸) 看作「GF(2⁴) 上的二次扩张」，元素是两个 GF(2⁴) 半字节 |
| GF(((2²)²)²) | 域塔：GF(2⁸) → GF((2⁴)²) → GF((2²)²) → GF(2²) |
| \(x^{-1}\) | \(x\) 的乘法逆元，满足 \(x \cdot x^{-1}=1\) |

## 3. 本讲源码地图

本讲涉及的源码全部位于 AES 工程的 `hdl/` 下：

| 文件 | 模块 | 作用 |
|------|------|------|
| `hdl/src/aes_s_box.v` | `bSbox` | 单字节 S-Box 顶层：基变换 + 求逆 + 仿射合并 + 加/解密选择 |
| `hdl/gf_s_box/gf_inv_8.v` | `gf_inv_8` | GF(2⁸) 求逆，递归分解到 GF(2⁴) |
| `hdl/gf_s_box/gf_inv_4.v` | `gf_inv_4` | GF(2⁴) 求逆，递归分解到 GF(2²) |
| `hdl/gf_s_box/gf_inv_2.v` | `gf_inv_2` | GF(2²) 求逆（=平方=位交换），递归的「触底」模块 |
| `hdl/gf_s_box/gf_mul_4.v` | `gf_mul_4` | GF(2⁴) 乘法器，由 GF(2²) 子乘法器构成 |
| `hdl/gf_s_box/gf_mul_2.v` | `gf_mul_2` | GF(2²) 乘法器（共享因子优化） |
| `hdl/gf_s_box/gf_mul_scl_2.v` | `gf_mul_scl_2` | GF(2²) 「乘并乘常数」子模块 |
| `hdl/gf_s_box/gf_scl_4.v` | `gf_sq_scl_4` | GF(2⁴) 平方+标量乘（注意：文件名与模块名不一致，且未在主路径上例化） |
| `hdl/utils/select_not_8.v` | `select_not_8` | 8 位二选一多路器，用于加/解密路径切换 |
| `hdl/tb/tb_s_box.v` | `tb_sbox` | S-Box 的仿真激励（存在重复例化名 bug） |

> 说明：这些 `gf_*` 文件由 `hdl/src/aes_include.v` 用 `` `include `` 串起来（见 [aes_include.v:23-34](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_include.v#L23-L34)），所以它们构成一个「编译单元」。

---

## 4. 核心概念与源码讲解

### 4.1 aes_s_box：S-Box 顶层与基变换思想

#### 4.1.1 概念说明

实现 AES 单字节 S-Box 有两条主流路线：

1. **查表法（LUT / ROM）**：把 256 个字节的 `S(x)` 预算好存进一块 256×8 的 ROM，输入 `x` 当地址读出。优点是快、简单；缺点是 **面积大**（一块完整的 Block RAM 或大量 LUT），而且查表对 **侧信道攻击（如 DPA 功耗分析）** 比较敏感——因为不同地址访问会泄漏不同的功耗特征。
2. **复合域法（composite field / Canright 算法）**：不存表，而是「算」出 \(x^{-1}\)。关键技巧是把 GF(2⁸) 重新看作一个 **域塔**：

\[
\mathrm{GF}(2^8) \;=\; \mathrm{GF}\big((2^4)^2\big) \;=\; \mathrm{GF}\big(((2^2)^2)^2\big)
\]

每一层「平方阶」的扩张，都把运算降维到子域。最终在最小的 GF(2²) 里，求逆退化成一根连线。这样整个 S-Box 只用几十个逻辑门（与/或/异或），**面积远小于查表**，而且因为运算是「算」出来的、没有大表，**对侧信道更友好**。这正是本仓库（以及多数面积受限的 AES 核）采用复合域的原因。

本仓库的复合域 S-Box 来自 **Canright (2005)** 的经典结构，模块名 `bSbox`，文件头注释也明确写了 "by Canright Algorithm"（见 [aes_s_box.v:11](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_s_box.v#L11)）。

Canright 的另一个关键技巧是：**把 AES 仿射变换的线性（矩阵）部分，合并进基变换矩阵里**。回忆 S-Box 定义 \(S(x)=\mathrm{Affine}(x^{-1})\)。Affine 由「一个线性矩阵乘」加「一个常数 0x63」组成。Canright 发现：从标准多项式基换到复合正规基的「同构映射」本身也是一个线性变换，于是可以和仿射的矩阵乘 **合并成前后两个 8×8 的位矩阵**，省掉一次独立的矩阵乘。常数 0x63 则被吸收进矩阵的某一行/加法项。

#### 4.1.2 核心流程

`bSbox` 的数据通路因此非常对称，分为 4 段：

```text
       输入字节 A (8 bit, 多项式基)
            │
   ┌────────┴────────┐
   │ 1. 输入侧基变换  │  把 A 从多项式基换到复合正规基，
   │   + 合并仿射矩阵  │  并同时算出「正向」向量 B 和「逆向」向量 Y
   └────────┬────────┘
            │  select_not_8(B, Y, encrypt) → Z   ← 加/解密在这里二选一
            ▼
   ┌────────┴────────┐
   │ 2. GF(2^8) 求逆  │  C = Z^(-1)   ← 核心非线性，由 gf_inv_8 完成
   └────────┬────────┘
            │
   ┌────────┴────────┐
   │ 3. 输出侧基变换  │  把 C 换回多项式基，合并逆向仿射矩阵，
   │   + 合并仿射矩阵  │  同时算出「正向」向量 D 和「逆向」向量 X
   └────────┬────────┘
            │  select_not_8(D, X, encrypt) → Q   ← 加/解密再次二选一
            ▼
       输出字节 Q (8 bit)
```

要点：

- 求逆本身 **不区分加/解密**——GF(2⁸) 里 \(x\) 的逆就是 \((x^{-1})^{-1}=x\)，正逆对称。
- 加/解密的差别全在 **前后两处基变换里合并的仿射矩阵**：`encrypt=1` 选正向仿射（输出 = S-Box），`encrypt=0` 选逆向仿射（输出 = 逆 S-Box）。
- 所以同一个 `bSbox` 既能加密又能解密，靠 `encrypt` 在前后各切一次多路器。

#### 4.1.3 源码精读

模块端口与中间线网声明（[aes_s_box.v:21-27](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_s_box.v#L21-L27)）：

```verilog
module bSbox ( A, encrypt, Q );
    input [7:0] A;
    input encrypt;            /* 1 for Sbox, 0 for inverse Sbox */
    output [7:0] Q;
    wire [7:0] B, C, D, X, Y, Z;
    wire R1..R9, T1..T10;     // 临时位
```

注释 `/* 1 for Sbox, 0 for inverse Sbox */` 直接印证了「`encrypt` 切换正/逆」的设计意图。

**第 1 段：输入侧基变换**，把 `A` 经一连串异或/同或（`~^` 是异或非）展开成两个 8 位向量 `B` 和 `Y`。这些赋值就是一个预计算好的 8×8 位矩阵（[aes_s_box.v:30-54](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_s_box.v#L30-L54)）。这里只摘两行示意：

```verilog
assign R1 = A[7] ^ A[5] ;          // 位级异或，构造矩阵的一行
...
assign B[7] = R7 ~^ R8 ;           // B = 正向(加密)基变换后的向量
...
assign Y[0] = A[1] ^ R5 ;          // Y = 逆向(解密)基变换后的向量
```

随后用 `select_not_8` 在 `B`、`Y` 之间二选一，得到送入求逆器的 `Z`（[aes_s_box.v:55-56](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_s_box.v#L55-L56)）：

```verilog
select_not_8 sel_in( B, Y, encrypt, Z );   // Z = encrypt ? ... : ...
gf_inv_8 inv( Z, C );                       // C = Z^(-1) in GF(2^8)
```

`select_not_8` 是用 8 个 `mux2_1` 做的逐位二选一（[select_not_8.v:21-34](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/select_not_8.v#L21-L34)）。

**第 2 段：输出侧基变换**，对 `C` 再做一次位矩阵展开，得到 `D` 和 `X`，并二选一输出 `Q`（[aes_s_box.v:58-84](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_s_box.v#L58-L84)）：

```verilog
assign T1 = C[7] ^ C[3] ;          // 输出侧矩阵的一行
...
assign D[7] = T4 ;                 // D = 正向(加密)输出向量
...
select_not_8 sel_out( D, X, encrypt, Q );   // 最终输出
```

> 注意：AES 仿射变换的常数 0x63 并没有以字面量出现——它被 Canright 预先吸收进了 `B/Y/D/X` 这些位矩阵里。这符合「线性部分合并」的预期，但具体每一位是否正确，**必须靠仿真对照 FIPS-197 标准表来验证**（见 4.1.4）。

#### 4.1.4 代码实践

**实践目标**：确认 `bSbox` 的四段结构，并发现 testbench 里的问题。

**操作步骤**：

1. 打开 `hdl/src/aes_s_box.v`，用三个书签分别圈出：输入侧基变换（约 30–55 行）、求逆调用（55–56 行）、输出侧基变换（58–84 行）。
2. 打开 `hdl/tb/tb_s_box.v`，阅读它的例化与激励（[tb_s_box.v:31-47](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/tb/tb_s_box.v#L31-L47)）：

```verilog
bSbox sbox_e(data_in, `ENCRIPT, data_out_e);
bSbox sbox_e(data_out_e, `DECRIPT, data_out_d);   // ← 注意例化名重复
```

3. 宏 `ENCRIPT=1'b1`、`DECRIPT=1'b0` 来自 [aes_types.v:21-22](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/utils/aes_types.v#L21-L22)。

**需要观察的现象**：

- 这个 testbench **声明了两个同名实例 `sbox_e`**，这在 Verilog 里属于「网/实例名重复声明」，多数仿真器会报错或告警；第二句本意应是 `sbox_d`。
- 这个 testbench **只施加激励、没有任何 `$display` 或自检断言**，所以即便能跑起来，也不会自动告诉你 S-Box 输出对不对。

**预期结果**：

- 若直接用 Icarus Verilog 编译 `tb_s_box.v`，多半会因为重复例化名而报错；把第二个 `sbox_e` 改成 `sbox_d` 后才能跑通。
- 修正后，你只能从波形里人工读 `data_out_e`，再对照 FIPS-197 表（`0x01 → 0x7c`，`0x10 → 0xca`，`0x02 → 0x77`）判断对错。

**待本地验证**：本仓库为草稿级 RTL，作者并未保证 `bSbox` 的位矩阵完全正确，请务必以仿真结果为准。

#### 4.1.5 小练习与答案

**练习 1**：如果用查表法实现 AES S-Box，需要多大的存储？复合域法为什么更省？

**参考答案**：查表需要存全部 256 个输入对应的输出，即 256×8 bit = 2048 bit（一块 Block RAM 或大量 LUT）。复合域法把求逆「算」出来，整条通路只用几十到一百多个与/或/异或门，不占专用存储，面积小一个数量级。

**练习 2**：为什么 `bSbox` 里要在前后 **两处** 都放 `select_not_8`，而求逆模块 `gf_inv_8` 本身却不接 `encrypt`？

**参考答案**：GF(2⁸) 求逆满足 \((x^{-1})^{-1}=x\)，正逆运算对称，所以求逆不需要区分方向。加/解密的差异来自 AES 仿射变换（及其常数），Canright 把正/逆仿射的线性部分分别合并进了输入侧和输出侧的基变换矩阵，因此需要在前后各做一次「正向矩阵 vs 逆向矩阵」的二选一，由 `encrypt` 控制。

---

### 4.2 gf_inv_8 / gf_inv_4 / gf_inv_2：求逆的递归分解

#### 4.2.1 概念说明

GF(2⁸) 上的直接求逆电路很复杂。复合域法的核心思想是 **降维**：把 GF(2⁸) 的一个元素拆成两个 GF(2⁴) 半字节，于是求逆归结为「子域 GF(2⁴) 上的求逆 + 子域乘法」；再把 GF(2⁴) 拆成两个 GF(2²) 位对，又归结为 GF(2²) 上的求逆 + 乘法；最终在 GF(2²) 触底，求逆变成一根连线。

具体地，设 GF(2⁸) 被表达为 GF(2⁴) 上的二次扩张，元素 \(A = a\cdot\delta + b\)，其中 \(a\) 是高半字节、\(b\) 是低半字节（\(\delta\) 是扩张用的根）。求逆的代数结果可以写成：

\[
A^{-1} \;=\; (d\cdot b)\,\delta \;+\; (d\cdot a), \qquad d \;=\; c^{-1}
\]

其中 \(c\) 是一个 **只依赖 \(a,b\) 的 GF(2⁴) 元素**（即「分母」），\(\delta\) 的约束已折进 \(c\) 的表达式里。于是算法只有三步：

1. 在 GF(2⁴) 里算出分母 \(c\)（纯组合逻辑，门级展开）；
2. 在 GF(2⁴) 里求 \(d = c^{-1}\)（递归调用 `gf_inv_4`）；
3. 在 GF(2⁴) 里做两次乘法 \(d\cdot b\)、\(d\cdot a\)（调用 `gf_mul_4`），分别作为结果的高低半字节。

`gf_inv_8` 实现的正是这个过程；`gf_inv_4` 用完全相同的结构再降一层到 GF(2²)；`gf_inv_2` 在 GF(2²) 触底。这就是「域塔」式递归。

#### 4.2.2 核心流程

**gf_inv_8 的流程**：

```text
A (8 bit) ──拆分──> a=A[7:4], b=A[3:0]   (各 4 bit, GF(2^4) 元素)
                       │
        计算 GF(2^4) 分母 c  (门级组合, 含 a,b 的平方与乘积)
                       │
                  gf_inv_4(c) → d          (在 GF(2^4) 里求 c 的逆)
                       │
            ┌──────────┴──────────┐
        gf_mul_4(d,b)→p      gf_mul_4(d,a)→q   (GF(2^4) 乘法)
            └──────────┬──────────┘
                  Q = {p, q}   (p 为高半字节, q 为低半字节)
```

**gf_inv_4 的流程**（结构同构，再降一层）：

```text
x (4 bit) ──拆分──> a=x[3:2], b=x[1:0]   (各 2 bit, GF(2^2) 元素)
                    │
         计算 GF(2^2) 分母 c
                    │
              gf_inv_2(c) → d              (在 GF(2^2) 里求 c 的逆)
                    │
          gf_mul_2(d,b)→p    gf_mul_2(d,a)→q
                    │
              y = {p, q}
```

**gf_inv_2 触底**：在 GF(2²) 正规基下，求逆 = 平方 = 系数互换，所以它只是一句赋值。

#### 4.2.3 源码精读

**gf_inv_8**（[gf_inv_8.v:22-61](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/gf_s_box/gf_inv_8.v#L22-L61)）。拆分与「共享因子」预计算：

```verilog
assign a = A[7:4];                 // 高半字节
assign b = A[3:0];                 // 低半字节
assign sa = a[3:2] ^ a[1:0];       // a 的位对异或, 预计算以省门
assign al = a[1] ^ a[0];
...
```

这些 `sa/sb/al/...` 是把 `a`、`b` 的若干位异或提前算好，**既用于算分母 `c`，又传给后面的 `gf_mul_4` 复用**，是 Canright 的面积优化手法。

分母 `c` 的门级展开（[gf_inv_8.v:45-49](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/gf_s_box/gf_inv_8.v#L45-L49)）：

```verilog
assign c = {
    (~(sa[0] | sb[0]) ^ (~(a[3] & b[3]))) ^ c1 ^ c3 ,
    ...
};
```

这一大段就是把上式 \(c(a,b)\) 的 4 个比特每个都用与/或/非/异或直接「拍」出来。它内部已经隐含了「对 \(a,b\) 平方」等运算（注意：作者在这里 **内联** 了平方，没有调用平方模块——见 4.4 节）。

递归与重组（[gf_inv_8.v:51-59](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/gf_s_box/gf_inv_8.v#L51-L59)）：

```verilog
gf_inv_4 dinv( c, d);                                       // d = c^(-1) in GF(2^4)
gf_mul_4 pmul(d, sd, dl, dh, dd, b, sb, bl, bh, bb, p);     // p = d*b
gf_mul_4 qmul(d, sd, dl, dh, dd, a, sa, al, ah, aa, q);     // q = d*a
assign Q = { p, q };                                        // p 高位, q 低位
```

> 注意端口顺序的「交叉」：高位结果 `p` 由 `d*b` 给出、低位结果 `q` 由 `d*a` 给出。这与 4.2.1 的公式 \(A^{-1}=(d\,b)\delta+(d\,a)\) 一致（\(\delta\) 项是高位）。`gf_mul_4` 的参数表很长，是因为它把上面预计算的共享因子逐个传进去复用。

**gf_inv_4**（[gf_inv_4.v:22-45](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/gf_s_box/gf_inv_4.v#L22-L45)）：结构与 `gf_inv_8` 同构，只是位宽减半、子模块换成 GF(2²) 版本：

```verilog
assign a = x[3:2];  assign b = x[1:0];
assign sa = a[1] ^ a[0];  assign sb = b[1] ^ b[0];
assign c = { ~(a[1]|b[1]) ^ (~(sa & sb)),  ~(sa|sb) ^ (~(a[0]&b[0])) };
gf_inv_2 d_inv(c, d);                       // 触底：GF(2^2) 求逆
gf_mul_2 pmul(d, sd, b, sb, p);             // p = d*b
gf_mul_2 qmult(d, sd, a, sa, q);            // q = d*a
assign y = {p, q};
```

> ⚠️ 文件头注释把 `gf_inv_4.v` 写成 "GF(2^2) inverter / inverse in gf(2^2)"（[gf_inv_4.v:5-11](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/gf_s_box/gf_inv_4.v#L5-L11)），这是 **复制粘贴造成的注释错误**：模块名 `gf_inv_4`、端口 4 位、实际是 GF(2⁴) 求逆器（其内部才用到 GF(2²)）。阅读时以代码为准。

**gf_inv_2**（[gf_inv_2.v:22-30](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/gf_s_box/gf_inv_2.v#L22-L30)）：整个递归的终点，只有一句赋值：

```verilog
assign y = {x[0], x[1]};   // 交换两位
```

为什么「交换两位」就是 GF(2²) 求逆？见 4.2.4 的实践与下面的证明。

#### 4.2.4 代码实践

**实践目标**：亲手验证「在 GF(2²) 正规基下，求逆 = 平方 = 交换两位」这一恒等式，从而理解为什么整个递归能在一句赋值上触底。

**操作步骤**（纯纸笔/心算，无需上板）：

1. 取 GF(2²) 正规基 \(\{\beta,\beta^2\}\)，约束 \(\beta^2+\beta+1=0\)，即 \(\beta^2=\beta+1\)。
2. 乘法群的阶为 3（非零元 \(\beta,\beta^2,\beta^3=1\))。
3. 验证求逆：\(\beta\cdot\beta^2=\beta^3=1\)，故 \(\beta^{-1}=\beta^2\)；同理 \((\beta^2)^{-1}=\beta\)。即 **求逆把系数互换**。
4. 验证平方：\((\beta^2)^2=\beta^4=\beta\)，故平方也把系数互换。
5. 把元素写作 \(x=x_1\beta+x_0\beta^2\)（位序按 `gf_inv_2` 的约定），则 \(x^{-1}=x_0\beta+x_1\beta^2\)，正好对应 `y={x[0],x[1]}`。

**预期结果**：

\[
x = (x_1,x_0) \;\Longrightarrow\; x^{-1} = x^2 = (x_0,x_1)
\]

这正是 `gf_inv_2` 里那一句位交换。**待本地验证**：可用一个 2 位 LFSR/穷举小 testbench 对 `gf_inv_2` 喂入全部 4 个输入（00/01/10/11），核对输出是否符合上表（注意 `00` 的逆按 AES 约定仍为 `00`）。

#### 4.2.5 小练习与答案

**练习 1**：在 `gf_inv_8` 中，最终 `Q = {p, q}`，其中 `p`、`q` 分别是怎么来的？为什么高位用 `d*b`、低位用 `d*a`？

**参考答案**：`d = c^{-1}` 是 GF(2⁴) 分母的逆；`p = d*b`、`q = d*a` 是 GF(2⁴) 乘法（各调一次 `gf_mul_4`）。因为 \(A^{-1}=(d\,b)\,\delta + (d\,a)\)，\(\delta\)（扩张根）对应高位，所以 `Q` 的高半字节放 `p=d*b`、低半字节放 `q=d*a`。

**练习 2**：`gf_inv_4` 与 `gf_inv_8` 的结构有何相似之处？这反映了什么设计思想？

**参考答案**：二者同构——都是「拆分高/低半部分 → 算子域分母 → 子域求逆 → 两次子域乘法 → 重组」。这反映了 **域塔/分治思想**：每一层都用相同的模板把「2n 阶域的求逆」化归为「n 阶域的求逆 + 乘法」，直到最小的 GF(2²) 触底。

---

### 4.3 gf_mul_4：复合域乘法器与共享因子优化

#### 4.3.1 概念说明

上面三个求逆模块都依赖一个子模块：**子域乘法器**。`gf_inv_8` 调 `gf_mul_4`（GF(2⁴) 乘），`gf_inv_4` 调 `gf_mul_2`（GF(2²) 乘）。乘法器本身也是按「域塔」递归构造的。

设 GF(2⁴) 被表达为 GF(2²) 的二次扩张，乘数 \(A=a_h\delta+a_l\)、\(B=b_h\delta+b_l\)（\(a_h,a_l,b_h,b_l\) 都是 GF(2²) 元素，\(\delta\) 为扩张根，满足一个含常数 \(N\) 的二次方程）。多项式乘开后对 \(\delta\) 的约束取模，整理得：

\[
A\cdot B \;=\; \big(a_h b_h \cdot N + (a_h b_l + a_l b_h)\big)\,\delta \;+\; \big(a_l b_l + (a_h b_l + a_l b_h)\big)
\]

即三部分：高位乘积 \(a_h b_h\) 再乘常数 \(N\)、低位乘积 \(a_l b_l\)、以及交叉和 \((a_h+a_l)(b_h+b_l) - a_hb_h - a_lb_l\)（用「和之积减积之和」省一次乘法）。`gf_mul_4` 正是用 3 个 GF(2²) 子乘法器实现这三部分；其中「交叉和乘上一个常数 \(N\)」由专门的 `gf_mul_scl_2`（乘并乘常数）完成。

「共享因子（shared factor）」优化：`a_h+a_l`、`b_h+b_l` 这类位异或会被多处用到，模块把它们提前算好，作为额外端口 `a/aa/b/bb/...` 传进来，避免在乘法器内部重复计算。

#### 4.3.2 核心流程

```text
gf_mul_4(A, B) → Q   (4 bit)
   │
   ├─ gf_mul_2  himul(A[3:2], B[3:2])            → ph   // 高位×高位
   ├─ gf_mul_2  lomul(A[1:0], B[1:0])            → pl   // 低位×低位
   ├─ gf_mul_scl_2 summul((a_h+a_l), (b_h+b_l))  → p    // 交叉和 × 常数 N
   │
   └─ Q = { ph ^ p ,  pl ^ p }     // 高位 = ph⊕p, 低位 = pl⊕p
```

`gf_mul_2` 是 GF(2²) 乘法器（同样带共享因子端口），`gf_mul_scl_2` 是 GF(2²)「乘且乘常数」版本——它把交叉项的结果顺带乘上扩张多项式里的常数 \(N\)，省掉一次单独的标量乘。

#### 4.3.3 源码精读

`gf_mul_4` 端口（[gf_mul_4.v:22-33](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/gf_s_box/gf_mul_4.v#L22-L33)）——注意它除了两个 4 位乘数 `A`、`B`，还接收一长串「预计算好的共享因子」：

```verilog
module gf_mul_4(A, a, Al, Ah, aa,  B, b, Bl, Bh, bb,  Q);
    input [3:0] A;   input [1:0] a;   input Al, Ah, aa;   // A 的共享因子
    input [3:0] B;   input [1:0] b;   input Bl, Bh, bb;   // B 的共享因子
    output [3:0] Q;
```

三个子乘法器与重组（[gf_mul_4.v:38-41](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/gf_s_box/gf_mul_4.v#L38-L41)）：

```verilog
gf_mul_2     himul (A[3:2], Ah, B[3:2], Bh, ph);   // a_h * b_h
gf_mul_2     lomul (A[1:0], Al, B[1:0], Bl, pl);   // a_l * b_l
gf_mul_scl_2 summul(a, aa, b, bb, p);              // (a_h+a_l)(b_h+b_l) * N
assign Q = { (ph^p), (pl^p) };                     // 高=ph⊕p, 低=pl⊕p
```

> ⚠️ `gf_mul_4.v` 的文件头注释自相矛盾：先写 "GF(2^2) multiplier" 又写 "GF(2^4) multiplier"（[gf_mul_4.v:6-12](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/gf_s_box/gf_mul_4.v#L6-L12)）。以代码为准：模块 4 位输入、调用 GF(2²) 子乘法器，是 **GF(2⁴) 乘法器**。

最底层的 `gf_mul_2`（[gf_mul_2.v:22-34](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/gf_s_box/gf_mul_2.v#L22-L34)）用「共享因子 + 减乘法」技巧，把 GF(2²) 乘法压缩到 3 个与门加几个异或：

```verilog
assign abcd = ~(ab & cd);                 // 共享
assign p = (~(x[1] & y[1])) ^ abcd;       // 高位
assign q = (~(x[0] & y[0])) ^ abcd;       // 低位
assign z = {p, q};
```

而 `gf_mul_scl_2`（[gf_mul_scl_2.v:22-34](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/gf_s_box/gf_mul_scl_2.v#L22-L34)）形式类似，但把「乘常数 \(N\)」直接硬编码进两条赋值的与门选择里，从而在算交叉项的同时完成标量乘。

#### 4.3.4 代码实践

**实践目标**：读 `gf_mul_4`，列出它例化的 3 个子模块各自对应的 GF(2²) 运算，并与 4.3.1 的公式逐项对应。

**操作步骤**：

1. 在 `gf_mul_4.v` 第 38–40 行标注：`himul → a_h·b_h`、`lomul → a_l·b_l`、`summul → (a_h+a_l)(b_h+b_l)·N`。
2. 对照公式的高位项 \(a_hb_h\,N + \text{交叉}\) 与低位项 \(a_lbl + \text{交叉}\)，确认 `Q={ph^p, pl^p}` 的两次异或分别实现了「+ 交叉项」。
3. 注意：`gf_mul_4` 不自己算 `a`、`aa`、`b`、`bb`，而是从外部接收——因为 `gf_inv_8` 已经为别的用途算过它们了，这里直接复用，省门。

**需要观察的现象**：高位 `ph^p` 里，`p` 同时贡献了「交叉项」和「\(a_hb_h\cdot N\) 中的常数 \(N\)」两重含义（因为 `summul` 已经把 \(N\) 乘进去了）；这正是 `gf_mul_scl_2` 存在的意义。

**预期结果**：你会得到一张「子模块 ↔ GF(2²) 子表达式」的对应表，这正是后面 4.4 与综合实践里画「递归分解树」的乘法器分支。

#### 4.3.5 小练习与答案

**练习 1**：`gf_mul_4` 为什么要额外接收 `a、aa、b、bb` 这些「难看的」端口，而不是只传 `A、B`？

**参考答案**：这些是乘数高低半部分位异或后的「共享因子」。`gf_inv_8` 在算分母 `c` 时已经用过它们，传进 `gf_mul_4` 复用可以避免在乘法器内部重复计算同一组异或，从而省门、降面积。这是 Canright 结构里常见的面积优化。

**练习 2**：`gf_mul_scl_2` 相比 `gf_mul_2` 多做了什么？

**参考答案**：它在 GF(2²) 乘法之外，**顺带把结果乘上一个固定常数 \(N\)**（GF((2²)²) 扩张多项式里的常数）。这样 `gf_mul_4` 在算「交叉项」时，一次 `gf_mul_scl_2` 调用就同时完成了「交叉乘」和「乘常数」，省掉了一次单独的标量乘法。

---

### 4.4 gf_scl_4：平方与标量乘（库内模块，但未在主路径）

#### 4.4.1 概念说明

求逆公式里需要「平方」运算（例如分母 \(c\) 含 \(a^2\)、\(b^2\)）。复合域里，平方有一个极好的性质：

\[
\text{在正规基下，平方 } \Leftrightarrow \text{「系数循环移位」}
\]

因为正规基形如 \(\{\alpha,\alpha^2,\alpha^4,\ldots\}\)，对元素平方等价于把这些基向量的指数都乘 2，于是系数整体「旋转」一位。对 GF(2²) 正规基 \(\{\beta,\beta^2\}\) 而言，旋转一位 = **交换两个系数** ——我们在 4.2.4 已经证明过：在 GF(2²) 正规基下，平方、求逆都退化成同一个「交换两位」操作。

本仓库的 `gf_s_box/` 目录里为此提供了「平方 + 标量乘」组合模块：

- `gf_sq_scl_2`：GF(2²) 的「\(N\cdot x^2\)」（平方再乘常数），在正规基下退化为两条简单赋值。
- `gf_sq_scl_4`（位于文件 `gf_scl_4.v`）：GF(2⁴) 的「\(v\cdot x^2\)」，把 GF(2⁴) 元素拆成两个 GF(2²) 半部分，分别平方（复用 `gf_inv_2`！）再各自乘上需要的常数。

**一个关键事实**：`gf_sq_scl_4`（即 `gf_scl_4` 文件里的模块）**并不在 `bSbox → gf_inv_8 → gf_inv_4` 的实际调用路径上**——`gf_inv_4` 和 `gf_inv_8` 在算分母 `c` 时，把平方运算 **直接用门级逻辑内联** 了，没有调用 `gf_sq_scl_4`。在仓库里全局搜索可知，`gf_sq_scl_4` 只被它自己的 testbench `tb_gf_scl_4.v` 例化。所以它是一个「库里备好、但当前 S-Box 主路径未启用」的模块。理解它仍有价值——它展示了「正规基下平方=位交换」这一思想如何向上一层复用。

#### 4.4.2 核心流程

`gf_sq_scl_4`（文件 `gf_scl_4.v`）的流程：

```text
x (4 bit, GF(2^4)) ──拆分──> a=x[3:2], b=x[1:0]   (各 2 bit, GF(2^2))
                                 │
            gf_inv_2(a^b) → ab2     // 复用「位交换」当平方器: (a+b)^2
            gf_inv_2(b)   → b2      // b^2
            gf_sq_scl_2(b2) → b2N2  // N * b^2  (再乘常数)
                                 │
                  y = { ab2, b2N2 }   (高位=(a+b)^2, 低位=N·b^2)
```

`gf_sq_scl_2`（[gf_sq_scl_2.v:22-34](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/gf_s_box/gf_sq_scl_2.v#L22-L34)）在正规基下也只用了两条赋值（`d1=x[0]^x[1]; d0=x[1]`），把「平方 + 乘常数」压缩到位重排。

#### 4.4.3 源码精读

`gf_sq_scl_4`（[gf_scl_4.v:22-39](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/gf_s_box/gf_scl_4.v#L22-L39)，注意 **文件名 `gf_scl_4.v` 与模块名 `gf_sq_scl_4` 不一致**）：

```verilog
module gf_sq_scl_4( x, y );
    input  [3:0] x;  output [3:0] y;
    wire [1:0] a, b, ab2, b2, b2N2;
    assign a = x[3:2];   assign b = x[1:0];

    gf_inv_2 absq(a ^ b, ab2);     // (a+b)^2 —— 复用 gf_inv_2 当平方器!
    gf_inv_2 bsq(b, b2);           // b^2
    gf_sq_scl_2 bmulN2(b2, b2N2);  // N * b^2

    assign y = {ab2, b2N2};
```

这里最值得玩味的是 **它用 `gf_inv_2` 来做平方**。这乍看像「写错了」（哪有用求逆器当平方器的？），但结合 4.2.4 的恒等式就完全合理：在 GF(2²) 正规基下，求逆与平方是 **同一个运算（交换两位）**，所以同一个模块既能当求逆器又能当平方器——作者借此复用，少写一个模块。这是一个非常漂亮、但也极易让初学者迷惑的设计。

`gf_scl_2`（[gf_scl_2.v:22-35](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/gf_s_box/gf_scl_2.v#L22-L35)）是 GF(2²) 的纯标量乘（\(N\cdot x\)），同样是两条赋值；它与 `gf_sq_scl_2` 的区别只是「不平方，只乘常数」。同样地，`gf_scl_2` 也只在自己的 testbench `tb_gf_scl_2.v` 里出现，主路径未用。

> 全局例化关系（可用 `grep` 复核）：`gf_mul_scl_2` 被 `gf_mul_4` 调用，**在** 主路径上；而 `gf_sq_scl_4`、`gf_sq_scl_2`、`gf_scl_2` **都只在自己的 testbench 里**，不在 `bSbox` 的实际数据通路里。

#### 4.4.4 代码实践

**实践目标**：用搜索验证「哪些 `gf_*` 模块真正在 S-Box 主路径上、哪些只活在 testbench 里」，并理解 `gf_sq_scl_4` 复用 `gf_inv_2` 当平方器的合理性。

**操作步骤**：

1. 在工程根目录执行（只读检索）：

```bash
grep -rn "gf_sq_scl_4\|gf_scl_4\|gf_sq_scl_2\|gf_scl_2" \
    HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl
```

2. 再检索主路径上的乘/求逆模块被谁调用：

```bash
grep -rn "gf_inv_8\|gf_inv_4\|gf_inv_2\|gf_mul_4\|gf_mul_2\|gf_mul_scl_2" \
    HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl
```

3. 对照 `gf_inv_4.v` 第 37–39 行、`gf_inv_8.v` 第 45–49 行：确认分母 `c` 是用门级 `assign` 直接算出来的（内联了平方），而 **没有** 调用 `gf_sq_scl_4`。

**需要观察的现象**：

- 第 1 条命令的命中几乎都落在 `hdl/tb/`（各模块自己的 testbench）和 `gf_scl_4.v`/`gf_sq_scl_2.v` 内部自调用，**没有** 出现在 `gf_inv_8.v`、`gf_inv_4.v`、`aes_s_box.v` 中。
- 第 2 条命令会显示完整的调用链：`aes_s_box → gf_inv_8 → {gf_inv_4, gf_mul_4×2}`，`gf_inv_4 → {gf_inv_2, gf_mul_2×2}`，`gf_mul_4 → {gf_mul_2×2, gf_mul_scl_2}`。

**预期结果**：得到一张「主路径 vs 仅 testbench」的模块分类表（这正是综合实践要画的分解树的一部分）。**待本地验证**：不同仿真器/版本下 `grep` 输出格式一致，但结论稳定。

#### 4.4.5 小练习与答案

**练习 1**：`gf_sq_scl_4`（`gf_scl_4.v`）里用 `gf_inv_2` 来做 `(a+b)^2` 和 `b^2`，这是 bug 吗？为什么？

**参考答案**：不是 bug，而是巧妙的复用。在 GF(2²) 正规基下，平方与求逆都退化为「交换两位」（即 `gf_inv_2` 里那句 `{x[0],x[1]}`），二者是同一个运算。作者直接拿求逆器当平方器用，省掉一个专用平方模块。初学者容易误判，需要结合正规基的数学性质来理解。

**练习 2**：`gf_sq_scl_4` 会在 S-Box 实际工作时被执行吗？

**参考答案**：不会。`gf_inv_8`/`gf_inv_4` 在计算分母 `c` 时，已把平方运算用门级 `assign` 内联了，并未调用 `gf_sq_scl_4`。在仓库内 `gf_sq_scl_4` 只被它自己的 testbench `tb_gf_scl_4.v` 例化。它是「库里备好、主路径未启用」的模块。

---

## 5. 综合实践

**任务**：画出 GF(2⁸) 求逆的 **完整递归分解树**，标注每个 `gf_*` 模块对应的数学运算，并区分「主路径」与「仅 testbench」的模块；最后用仿真验证整条 S-Box 链。

**步骤**：

1. **建树**：以 `bSbox`（[aes_s_box.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_s_box.v)）为根，按本讲各节列出的例化关系，画一棵树。预期主干如下（每条边标「数学运算」）：

```text
bSbox  (S-Box: 基变换+仿射+求逆)
 ├── select_not_8 sel_in   (加/解密二选一, 输入侧)
 ├── gf_inv_8              (GF(2^8) 求逆)
 │    ├── gf_inv_4         (GF(2^4) 求逆)
 │    │    ├── gf_inv_2    (GF(2^2) 求逆 = 交换两位, 触底)
 │    │    ├── gf_mul_2    (GF(2^2) 乘: d*b)
 │    │    └── gf_mul_2    (GF(2^2) 乘: d*a)
 │    ├── gf_mul_4         (GF(2^4) 乘: d*b)
 │    │    ├── gf_mul_2    (a_h*b_h)
 │    │    ├── gf_mul_2    (a_l*b_l)
 │    │    └── gf_mul_scl_2 ((a_h+a_l)(b_h+b_l)·N)
 │    └── gf_mul_4         (GF(2^4) 乘: d*a)  [内部同上]
 └── select_not_8 sel_out  (加/解密二选一, 输出侧)

—— 仅 testbench / 主路径未启用 ——
gf_sq_scl_4 (文件 gf_scl_4.v): GF(2^4) 平方+标量乘
gf_sq_scl_2: GF(2^2) N·x^2
gf_scl_2  : GF(2^2) N·x
```

2. **标注**：在每个节点旁用一句话写它对应的数学运算（参考各节 4.x.1 的公式）。

3. **仿真验证**：
   - 先修正 `tb_s_box.v` 的重复例化名（把第二个 `sbox_e` 改为 `sbox_d`），并补上 `$display` 与期望值比对（参考 FIPS-197：`0x53 → 0xed`、`0x01 → 0x7c`）。
   - 也可优先运行 `hdl/tb/tb_gf_inv_8.v`、`tb_gf_inv_4.v`、`tb_gf_inv_2.v`（[tb 目录](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/tb/tb_gf_inv_8.v)）逐层验证求逆模块，由底向上确认每一层正确，再验证顶层 `bSbox`。
   - 若结果与 FIPS-197 不符，说明某层位矩阵/分母 `c` 的实现有误——这是本仓库作为草稿级 RTL 的已知风险，请定位到具体层后对照 Canright 原文修正。

4. **反思**：对比「查表 S-Box」与「复合域 S-Box」在你目标 FPGA（如 Zynq-7020）上的资源占用（LUT/FF/可能没有 BRAM），体会复合域的面积优势。

**预期结果**：一张清晰的分解树 + 一份逐层仿真记录 + 对资源权衡的简短结论。**待本地验证**：具体时序与资源以你的 Vivado 综合报告为准。

## 6. 本讲小结

- S-Box 不一定要查表：用 **Canright 复合域算法** 把 GF(2⁸) 求逆「算」出来，可大幅省面积、降低侧信道泄漏。
- `bSbox` 的结构是「**输入侧基变换（合并仿射）→ GF(2⁸) 求逆 → 输出侧基变换（合并仿射）**」，靠 `encrypt` 在前后两处 `select_not_8` 切换正/逆 S-Box；求逆本身对加解密对称。
- 求逆走 **域塔**：`gf_inv_8`（GF(2⁸)）→ `gf_inv_4`（GF(2⁴)）→ `gf_inv_2`（GF(2²)），每层都是「拆高低半部分 → 算子域分母 → 子域求逆 → 两次子域乘法」。
- 乘法器同样递归：`gf_mul_4` 由两个 `gf_mul_2` 加一个 `gf_mul_scl_2`（乘并乘常数）构成，并大量复用预计算的「共享因子」省门。
- 最底层 `gf_inv_2` 只有一句 `assign y={x[0],x[1]}`——因为 **GF(2²) 正规基下，求逆=平方=交换两位**；`gf_sq_scl_4` 正是利用这一点把求逆器当平方器复用。
- 本仓库为草稿级 RTL：存在文件名与模块名不一致（`gf_scl_4.v`↔`gf_sq_scl_4`）、文件头注释错乱、`tb_s_box.v` 重复例化名、`gf_sq_scl_4`/`gf_scl_2` 仅在 testbench 中而未进主路径等问题，阅读与复用时 **必须以仿真和 FIPS-197 标准表为准**。

## 7. 下一步学习建议

- **横向验证**：去阅读 Unit 3 将讲到的 SystemVerilog 验证环境 `hdl/VE_sv/`（尤其黄金参考模型 `ve_AES_Core.sv`），用它来交叉验证本讲的 `bSbox` 输出是否与标准一致——这是判断草稿级 RTL 正确性的最可靠途径。
- **纵向深入**：若你想彻底搞懂每一位 `R1..R9`、`T1..T10` 的来历，建议阅读 Canright 的原文 *A Very Compact Rijndael S-Box*（以及它对 GF((2⁴)²) 扩张多项式常数的选择），再回头对照本仓库的位矩阵，体会「线性变换合并」的工程价值。
- **回到主线**：本讲完成了 AES 数据通路最难的「非线性字节替换」内部剖析。下一讲（[u3-l1](u3-l1-vivado-ip-structure.md) 起的 Unit 3）将跳出算法本身，转而看 **如何把这个 AES 核封装成 Vivado 自定义 AXI IP**，进入软硬件协同的话题。
