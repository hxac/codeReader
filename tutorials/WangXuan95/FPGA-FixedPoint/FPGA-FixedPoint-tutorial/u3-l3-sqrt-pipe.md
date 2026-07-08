# fxp_sqrt_pipe：开方算法的流水线化

## 1. 本讲目标

本讲是专家层「把单周期运算改造成流水线」系列的第三篇。在 [u2-l4](u2-l4-sqrt.md) 里我们吃透了单周期开方 `fxp_sqrt` 的逐位试探算法，在 [u3-l1](u3-l1-mul-pipe.md) 里我们用 `fxp_mul_pipe` 建立了全库 `_pipe` 模块的统一范式。本讲把这两条线汇合：**把逐位开方的 `for` 循环展开成一条多级流水线 `fxp_sqrt_pipe`**。

学完后你应该能够：

- 说清为什么单周期 `fxp_sqrt` 不适合直接综合（关键路径过长），以及流水线如何把它逐段切开。
- 掌握「标量循环变量 → 级间寄存器数组」这一把组合 `for` 循环变成流水线的核心手法。
- 看懂 `jj = WRI-1-ii` 这个把「算法位序」映射到「流水线级序」的小技巧。
- 解释末级为什么是「符号恢复 + `fxp_zoom` 收敛 + 输出寄存」三件事的组合，并自行推导总级数 \(\lceil \text{WII}/2\rceil+\text{WIF}+2\)。
- 能搭建一个延迟对齐的验证环境，证明 `fxp_sqrt_pipe` 与单周期 `fxp_sqrt` 在功能上完全等价。

## 2. 前置知识

本讲默认你已经读过 [u2-l4](u2-l4-sqrt.md) 和 [u3-l1](u3-l1-mul-pipe.md)。这里把要用到的几个要点快速复习一下。

**逐位开方（digit-recurrence square root）。** 求一个定点数 \(x\) 的平方根，等价于从高到低逐位决定根 \(r\) 的每一位。维护两个量：部分根 `resu` 和它的平方累计 `resu2`（不变量为 `resu2 == resu²/2^WIF`）。每试探根的第 \(i\) 位时，利用平方展开的增量

\[
(r+2^i)^2 - r^2 = 2^{i+1}\cdot r + 2^{2i}
\]

算出候选 `resu2tmp`。若 `resu2tmp <= x` 且 \(x\neq 0\)，则该位置 1 并采纳候选；否则该位清 0、`resu2` 不变（恢复）。循环从 \(i=\text{WRI}-1\) 跑到 \(i=-\text{WIF}\)，恰好覆盖根的全部位。符号单独处理：先对输入取绝对值开方，再按符号补回，负数定义为 \(-\sqrt{|x|}\)。

**关键路径与流水线。** 一段组合逻辑里「最长的一条从输入到输出的串联链」叫关键路径，它决定芯片能跑到的最高时钟频率 \(f_{\max}\)。流水线的做法是在长路径中间插入若干级寄存器，把一条长链切成几段短链，从而提升 \(f_{\max}\)；代价是输出要等固定若干拍（延迟 latency）才出来，但吞吐量仍是「每拍一个」，称为无气泡。

**全库 `_pipe` 模块的统一接口约定**（来自 [u3-l1](u3-l1-mul-pipe.md)）：

1. 比单周期版多出 `rstn`（异步复位，低有效）与 `clk` 两个端口。
2. `out`/`overflow` 由 `wire` 改成 `reg`。
3. 用 `initial` 给输出赋初值，避免仿真一开始出现 `x`。
4. 时序块统一写成 `always @(posedge clk or negedge rstn)`，并用非阻塞赋值 `<=`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [RTL/fixedpoint.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v) | 全部可综合模块所在的单一文件。本讲关注 `fxp_sqrt`（单周期，作为黄金参考）与 `fxp_sqrt_pipe`（流水线，本讲主角），并复用公共原语 `fxp_zoom`。 |
| [SIM/tb_fxp_sqrt.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt.v) | 同时例化 `fxp_sqrt` 与 `fxp_sqrt_pipe` 的测试平台，是本讲综合实践的改造起点。 |

涉及的三个最小模块：

- **`fxp_sqrt`**：单周期开方，作为功能黄金参考（[u2-l4](u2-l4-sqrt.md) 已详述）。
- **`fxp_sqrt_pipe`**：本讲主角，把上面的循环展开成 \(\lceil \text{WII}/2\rceil+\text{WIF}+2\) 级流水线。
- **`fxp_zoom`**：位宽变换原语（[u1-l3](u1-l3-fxp-zoom.md) 已详述），在末级被例化一次，做舍入与溢出饱和。

---

## 4. 核心概念与源码讲解

### 4.1 回顾单周期 fxp_sqrt 与流水线化的动机

#### 4.1.1 概念说明

`fxp_sqrt` 是纯组合逻辑：所有位的试探在一次 `always @ (*)` 求值里完成。文件头部的注释直接给出了它的工程定位——关键路径太长，不建议直接综合：

> `not recommended due to the long critical path`

这条长路径就是 `for` 循环里每一位试探串联起来的链。具体地说，每一位试探包含一次「移位 + 两次加法 + 一次比较」，而第 \(i\) 位的试探又依赖第 \(i+1\) 位试探产生的 `resu` 与 `resu2`，于是全部 \(\text{WRI}+\text{WIF}\) 位首尾相连，构成一条深度正比于 \(\text{WRI}+\text{WIF}\) 的组合链。位宽越宽，这条链越长，\(f_{\max}\) 越低。

流水线的解法很自然：**既然每一位试探只依赖上一位的结果，那就让每一位试探独占一级寄存器**，相邻级之间用时钟沿隔开。原本「一个周期内串完所有位」变成「每拍只算一位，数据像流水一样逐级下传」。这样关键路径就缩短到「单级试探」的深度，\(f_{\max}\) 大幅提升；代价是结果要等 \(\text{WRI}+\text{WIF}\) 级（再加首尾两级）才出来。

#### 4.1.2 核心流程

单周期 `fxp_sqrt` 的数据通路可以画成：

```
in ──► 取绝对值 inu、记 sign
        │
        ▼
   ┌──────────── 逐位试探 for (ii=WRI-1 .. -WIF) ────────────┐
   │  读 resu, resu2 ──► 算 resu2tmp ──► 比较 inu ──► 更新位 │  ← 一条长组合链
   │                  （每一位都依赖上一位的 resu/resu2）     │
   └─────────────────────────────────────────────────────────┘
        │  得到完整 resu
        ▼
   符号恢复 resushort ──► fxp_zoom ──► out, overflow
```

流水线化就是要把中间那个大框「竖着」切成 \(\text{WRI}+\text{WIF}\）级，每级寄存一拍。

#### 4.1.3 源码精读

先看单周期的参数与位宽推导（[RTL/fixedpoint.v:L702-L707](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L702-L707)）：

```verilog
localparam WTI = (WII%2==1) ? WII+1 : WII;   // 把整数位宽凑成偶数
localparam WRI = WTI/2;                        // 平方根的整数位宽 = 一半
```

平方根的位数约为原数的一半，所以先把 WII 凑偶成 WTI，再取 `WRI=WTI/2`。

再看那条「长链」本身——逐位试探的 `for` 循环（[RTL/fixedpoint.v:L716-L732](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L716-L732)）：

```verilog
always @ (*) begin
    sign = in[WII+WIF-1];
    inu = 0;
    inu[WII+WIF-1:0] = sign ? (~in)+ONEI : in;
    {resu2,resu} = 0;
    for(ii=WRI-1; ii>=-WIF; ii=ii-1) begin
        resu2tmp = resu2;
        if(ii>=0) resu2tmp = resu2tmp + (resu<<( 1+ii));
        else      resu2tmp = resu2tmp + (resu>>(-1-ii));
        if(2*ii+WIF>=0) resu2tmp = resu2tmp + ( ONET << (2*ii+WIF) );
        if(resu2tmp<=inu && inu!=0) begin
            resu[ii+WIF] = 1'b1;
            resu2 = resu2tmp;
        end
    end
    resushort = sign ? (~resu[WRI+WIF:0])+ONER : resu[WRI+WIF:0];
end
```

注意三个要点，它们将在 4.3 节逐字复用到流水线版本：

1. 循环变量 `ii` 从 `WRI-1` 递减到 `-WIF`，迭代次数 = \((\text{WRI}-1)-(-\text{WIF})+1=\text{WRI}+\text{WIF}\)。
2. `resu`、`resu2` 是**标量**，跨迭代复用、逐位累积——这正是关键路径的来源。
3. 「成功才更新」：只有 `resu2tmp<=inu && inu!=0` 时才置位并更新 `resu2`，否则两位量都不动。

#### 4.1.4 代码实践（源码阅读型）

**目标**：亲手数清楚那条关键路径有多长，建立「为什么必须流水线」的直觉。

**步骤**：

1. 打开 [RTL/fixedpoint.v:L721-L730](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L721-L730)。
2. 用测试平台 [SIM/tb_fxp_sqrt.v:L16-L19](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt.v#L16-L19) 的参数（WII=10, WIF=10）算出 WTI、WRI。
3. 计算循环迭代次数 = WRI+WIF。

**需要观察的现象 / 预期结果**：WII=10 → WTI=10 → WRI=5 → 迭代次数 5+10=**15**。也就是说单周期版本的关键路径里串了 15 级「移位+加法+比较」，这就是注释里 "long critical path" 的具体含义，也是流水线版本要切开的对象。

#### 4.1.5 小练习与答案

**练习 1**：为什么开方结果的整数位宽是输入的一半，而不是相同？

<details><summary>参考答案</summary>
幅值上 \(\sqrt{x}\) 的位数约为 \(x\) 位数的一半：一个 \(n\) 位无符号数最大约 \(2^n\)，其平方根约 \(2^{n/2}\)，需要 \(n/2\) 位。所以先把 WII 凑成偶数 WTI，再取 WRI=WTI/2。</details>

**练习 2**：单周期版本的关键路径深度大约正比于哪个量？

<details><summary>参考答案</summary>
正比于循环迭代数 \(\text{WRI}+\text{WIF}\)，因为每一位试探都依赖上一位的 `resu`/`resu2`，全部首尾相连。位宽越宽，链越长，\(f_{\max}\) 越低。</details>

---

### 4.2 级间寄存器数组：把标量循环变成级联流水线

#### 4.2.1 概念说明

这是本讲最核心的一步。在单周期的 `always @ (*)` 里，`sign`、`inu`、`resu2`、`resu` 是**标量**：第 \(i\) 次迭代写进去，第 \(i-1\) 次迭代立刻读到——因为它们都在同一次组合求值里。这种「同一个变量跨迭代传递」在组合逻辑里没问题，但**到了时序逻辑里就会撞车**：流水线上同时有多个数据在飞，第 \(k\) 个数据和第 \(k+1\) 个数据不能用同一个寄存器，否则后者会把前者冲掉。

解决办法是**把每个标量变成一个数组**，数组下标就是流水线的级号。这样第 0 级、第 1 级、…… 各自拥有自己的 `resu[0]`、`resu[1]`、…… 互不干扰，数据像排队一样逐级下传。

于是原来的标量循环

```
resu  ← 跨迭代累积的标量
resu2 ← 跨迭代累积的标量
```

变成了数组之间的「打拍传递」

```
resu [jj+1] <= resu [jj]     // 第 jj 级的 resu 传给第 jj+1 级
resu2[jj+1] <= resu2[jj]
```

每一位试探对应一级，每来一个时钟沿，所有级同时前进一步——这正是流水线的「无气泡」特性。

#### 4.2.2 核心流程

变换前后的对照：

| 维度 | 单周期 `fxp_sqrt` | 流水线 `fxp_sqrt_pipe` |
|------|-------------------|------------------------|
| `sign` / `inu` / `resu` / `resu2` | 标量 `reg` | 数组 `reg [0..WRI+WIF]`，下标 = 级号 |
| 循环变量 `ii` | 算法位序（高位→低位） | 仍为位序，但每一位映射到一级 |
| 级号映射 | 无 | `jj = WRI-1-ii`（见 4.2.3） |
| 赋值方式 | 阻塞 `=`，同次求值内串行 | 非阻塞 `<=`，所有级同拍并行前移 |
| 一位试探 | 一次循环迭代 | 一级寄存器 |

数据流变成：

```
in ──► [stage 0: 取绝对值、记 sign、resu2=resu=0]
        │  时钟沿
        ▼
      [stage 1: 试探最高位 resu[WRI-1]]
        │  时钟沿
        ▼
      [stage 2: 试探次高位 resu[WRI-2]]
        │   ……
        ▼
      [stage WRI+WIF: 试探最低位 resu[-WIF]]  → 得到完整 resu
```

#### 4.2.3 源码精读

**数组声明**（[RTL/fixedpoint.v:L785-L788](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L785-L788)）——把四个标量换成四个数组，下标范围 `[WRI+WIF:0]`（共 WRI+WIF+1 个元素，对应级 0 到级 WRI+WIF）：

```verilog
reg               sign [WRI+WIF :0];
reg [WTI+WIF-1:0] inu  [WRI+WIF :0];
reg [WTI+WIF-1:0] resu2 [WRI+WIF :0];
reg [WTI+WIF-1:0] resu  [WRI+WIF :0];
```

**初始化**（[RTL/fixedpoint.v:L793-L798](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L793-L798)）：用 `initial` 把所有级清零，避免仿真出现 `x`，符合 [u3-l1](u3-l1-mul-pipe.md) 的接口约定。

**首级 stage 0**（[RTL/fixedpoint.v:L809-L813](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L809-L813)）——把外部输入注入流水线第 0 级，做的事与单周期 `always` 开头完全一致：记符号、取绝对值、把累积量清零：

```verilog
sign[0] <= in[WII+WIF-1];
inu[0] <= 0;
inu[0][WII+WIF-1:0] <= in[WII+WIF-1] ? (~in)+ONEI : in;
resu2[0] <= 0;
resu[0] <= 0;
```

> 注意 `inu[0]` 先整体置 0 再只写低 `WII+WIF` 位：当 WII 为奇数时 WTI=WII+1，多出的最高位保持 0，相当于对正数做符号扩展，这与单周期里 `inu=0; inu[...]=...` 的写法等价。

**级序映射 `jj = WRI-1-ii`**（[RTL/fixedpoint.v:L814-L815](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L814-L815)）：这是把「算法位序」翻译成「流水线级序」的关键一行。`ii` 从最高位 `WRI-1` 开始递减，而流水线级号 `jj` 从 0 开始递增，二者方向相反，所以用 `jj=WRI-1-ii` 翻转：试探根的最高位（ii=WRI-1）发生在最早的第 0 级（jj=0），最低位（ii=-WIF）发生在最晚的第 WRI+WIF-1 级。这样写的好处是循环结构可以照搬 [u2-l4](u2-l4-sqrt.md) 已经讲清的 `for(ii=WRI-1; ii>=-WIF; ii=ii-1)`，读者无需重新理解位序。

**默认打拍传递**（[RTL/fixedpoint.v:L816-L819](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L816-L819)）——每个时钟沿，符号、输入、部分根、平方累计都无条件向下一级传一拍：

```verilog
sign[jj+1] <= sign[jj];
inu [jj+1] <= inu [jj];
resu[jj+1] <= resu[jj];
resu2[jj+1]<= resu2[jj];
```

这四行就是「流水线主流」，让数据逐级下传；下面 4.3 节的「试探」逻辑只是在这股主流之上做条件覆盖。

> **为什么非阻塞 `<=` 让所有级「同时前移」而不乱？** 非阻塞赋值是「先算右值、最后统一更新」。所以在同一个时钟沿，第 jj 级读到的 `resu[jj]` 是**上一拍**锁存的旧值，而不是本次同一沿上更高级刚算出的新值。于是每个级都独立地「读自己入口寄存器的旧值、写自己出口寄存器」，N 个级恰好构成 N 级移位寄存器，数据整队前进——这正是流水线的语义。

#### 4.2.4 代码实践（参数推导型）

**目标**：给定奇数 WII，验证你能在脑子里跑通位宽与数组规模的推导。

**步骤**：

1. 假设 `WII=11, WIF=8`。
2. 算 WTI、WRI。
3. 写出四个数组的元素个数（下标范围）。

**预期结果**：WII=11 为奇数 → WTI=12 → WRI=6。数组下标 `[WRI+WIF:0] = [14:0]`，即每个数组 **15 个元素**，对应级 0 到级 14。流水线总级数将是 WRI+WIF+2 = 6+8+2 = **16**（级数推导见 4.4）。

#### 4.2.5 小练习与答案

**练习 1**：为什么不能像单周期那样，在流水线里也用标量 `resu` 跨级传递？

<details><summary>参考答案</summary>
流水线上同时有多个数据在飞。若用标量，第 \(k\) 个数据算出的 `resu` 会被第 \(k+1\) 个数据覆盖，导致信息丢失。改成数组 `resu[jj]` 后，每一级有独立的寄存器，多个数据各占一级、互不干扰。</details>

**练习 2**：`jj = WRI-1-ii` 这个映射，把根的最高位安排在第几级？

<details><summary>参考答案</summary>
最高位 ii=WRI-1 → jj=WRI-1-(WRI-1)=0，即最早的第 0 级（与 stage 0 同一时钟沿写入 stage 1 时生效）。最低位 ii=-WIF → jj=WRI+WIF-1，即最晚的一级。算法从高位到低位，流水线从早级到晚级，方向一致。</details>

---

### 4.3 逐位试探级：与单周期一致的算术内核

#### 4.3.1 概念说明

把循环展开成级联之后，**每一级的算术内核与单周期 for 循环体逐行相同**——这是本讲一个非常重要的结论。换句话说，流水线化只改变了「数据怎么存、怎么传」（标量→数组、阻塞→非阻塞），完全没有改变「每一位怎么算」。这就保证了流水线版本与单周期版本在数学上等价，可以用后者做前者的黄金参考。

每一级做两件事：

1. **算候选** `resu2tmp`：在「上一级的 `resu2[jj]`」基础上，按当前位 \(i\) 叠加增量 \(2^{i+1}\cdot \text{resu} + 2^{2i}\)（整数位用左移、小数位用右移），得到「如果这一位置 1，平方累计会是多少」。
2. **试探**：若 `resu2tmp <= inu[jj]` 且 `inu[jj]!=0`，则这一位置 1 并采纳候选；否则保持 0、`resu2` 不变（恢复）。

#### 4.3.2 核心流程

单级（处理第 \(i\) 位，级号 jj=WRI-1-ii）的伪代码：

```
读入口（上一拍锁存的旧值）：resu2[jj], resu[jj], inu[jj]

# 1. 算候选
resu2tmp = resu2[jj]
若 i>=0 : resu2tmp += resu[jj] << (1+i)      # 对应 2*resu*2^i
否则     : resu2tmp += resu[jj] >> (-1-i)
若 2i+WIF>=0 : resu2tmp += ONET << (2i+WIF)   # 对应 2^(2i)

# 2. 默认把上一级的 resu/resu2 原样下传（恢复分支）
resu [jj+1] <= resu [jj]
resu2[jj+1] <= resu2[jj]

# 3. 若试探成功，覆盖：置位 + 采纳候选
若 resu2tmp <= inu[jj] 且 inu[jj] != 0 :
    resu [jj+1][i+WIF] <= 1
    resu2[jj+1]        <= resu2tmp
```

这里有一个 Verilog 时序语义的小技巧：第 2 步先无条件传递，第 3 步在条件成立时用**非阻塞赋值**覆盖 `resu[jj+1]` 和 `resu2[jj+1]`。同一时钟沿里，对同一变量的多条非阻塞赋值按代码顺序「后写覆盖前写」，所以条件覆盖能正确生效——这等价于单周期里「`if` 成立才执行 `resu[..]=1; resu2=resu2tmp`，否则什么都不做」。

#### 4.3.3 源码精读

循环体（[RTL/fixedpoint.v:L820-L830](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L820-L830)），把它和单周期 [RTL/fixedpoint.v:L722-L729](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L722-L729) 并排看，几乎逐字相同，只是标量 `[ ]` 变成了 `[jj]`/`[jj+1]`、阻塞 `=` 变成了非阻塞 `<=`：

```verilog
resu2tmp = resu2[jj];
if(ii>=0)
    resu2tmp = resu2tmp + (resu[jj]<<( 1+ii));
else
    resu2tmp = resu2tmp + (resu[jj]>>(-1-ii));
if(2*ii+WIF>=0)
    resu2tmp = resu2tmp + ( ONET << (2*ii+WIF) );
if(resu2tmp<=inu[jj] && inu[jj]!=0) begin
    resu[jj+1][ii+WIF] <= 1'b1;     // 置位
    resu2[jj+1] <= resu2tmp;         // 采纳候选
end
```

几个要点：

- `resu2tmp` 是一个**普通的组合临时变量**（[RTL/fixedpoint.v:L791](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L791)），用阻塞 `=` 在每级开头重算，只在当前级内消费，不跨级——所以它不属于流水线寄存器，只是「本级的中间结果」。
- 三条加法分别对应平方增量 \((r+2^i)^2-r^2=2^{i+1}r+2^{2i}\) 的两项：`resu<<(1+ii)` 是 \(2^{i+1}r\)，`ONET<<(2*ii+WIF)` 是定点标度下的 \(2^{2i}\)（[u2-l4](u2-l4-sqrt.md) 已推导过这里的 WIF 标度）。
- 条件 `inu[jj]!=0` 保留单周期的特判：输入为 0 时根应为 0，不进入任何置位。

把这一级在脑里「复制 WRI+WIF 份」摞起来，就是一条完整的逐位开方流水线——最后一级 `resu[WRI+WIF]` 里就是完整的平方根。

#### 4.3.4 代码实践（源码阅读型）

**目标**：确认流水线某一级与单周期某次迭代「算的是同一个位」。

**步骤**：

1. 取 `ii = WRI-2`（次高位）。按 `jj=WRI-1-ii` 算出它落在第几级。
2. 在 [RTL/fixedpoint.v:L828](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L828) 找到这一级写的位 `resu[jj+1][ii+WIF]`，确认它写的是根的哪一位。
3. 对照单周期 [RTL/fixedpoint.v:L727](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L727) 的 `resu[ii+WIF]`，确认位地址一致。

**预期结果**：`ii=WRI-2 → jj=1`，即第 1 级（写到 `resu[2]`）。该级写位 `resu[2][ii+WIF] = resu[2][(WRI-2)+WIF]`，与单周期里处理 ii=WRI-2 时写的 `resu[(WRI-2)+WIF]` 是同一个位地址——只是流水线版本里这个位被锁进 `resu[2]` 这一级的寄存器，而单周期版本里它直接累积在标量 `resu` 上。

#### 4.3.5 小练习与答案

**练习 1**：流水线版本里，为什么把「默认传递 `resu2[jj+1]<=resu2[jj]`」放在前面、把「条件覆盖」放在后面，顺序不能反过来？

<details><summary>参考答案</summary>
非阻塞赋值对同一变量按代码顺序「后写覆盖前写」。先无条件传递、再条件覆盖，才能在试探成功时让覆盖生效、失败时保留传递值（即「恢复」语义）。若反过来，无条件传递会盖掉刚刚的条件置位，导致位永远写不进去。</details>

**练习 2**：`resu2tmp` 是流水线寄存器吗？为什么它可以是用 `=` 赋值的标量？

<details><summary>参考答案</summary>
不是。它是每一级内部、在同一个时钟沿里即时算出、即时消费的组合临时量，不跨级保存。因为它在每级开头都用阻塞 `=` 重新赋值，且只在当前级的覆盖语句里使用，下一级会用自己的 jj 重新算，所以不会在级间串扰，无需做成数组。</details>

---

### 4.4 末级收敛、输出寄存与级数推导

#### 4.4.1 概念说明

走完 \(\text{WRI}+\text{WIF}\) 级试探后，最后一级寄存器 `resu[WRI+WIF]` 里装着完整的无符号平方根，`sign[WRI+WIF]` 里装着对应的符号。但它们还不是最终输出，还差三件事：

1. **符号恢复**：开方是在绝对值上做的，现在要按符号补回二进制补码——负数定义为 \(-\sqrt{|x|}\)。
2. **位宽收敛**：根的格式是 `(WRI+1, WIF)`（多 1 位整数留给符号），而用户要的输出格式是 `(WOI, WOF)`，需要截断/扩展并做舍入与溢出饱和——这件事直接复用 `fxp_zoom`。
3. **输出寄存**：`fxp_zoom` 是组合逻辑，它的输出再打一拍寄存器，构成流水线的最后一级，也让 `out`/`overflow` 与时钟严格对齐。

这三步恰好对应单周期 `fxp_sqrt` 末尾的 `resushort` 与 `res_zoom`，只是末尾多了「再寄存一拍」。

#### 4.4.2 核心流程

末级数据通路：

```
resu[WRI+WIF] ──┐
sign[WRI+WIF] ──┤
                ├─► resushort = sign ? (~resu)+ONER : resu   （组合，符号恢复）
                │
                ▼
        fxp_zoom (WRI+1, WIF) → (WOI, WOF)                   （组合，收敛+舍入+饱和）
                │  outl, overflowl
                ▼
        [输出寄存器：再打一拍]  ──► out, overflow
```

**级数推导**。把全程的寄存器数清楚：

- 首级 stage 0：**1** 个寄存器（锁存输入与初始累积态）。
- 逐位试探：循环迭代 \(\text{WRI}+\text{WIF}\) 次，每次写 `[jj+1]`，即 **\(\text{WRI}+\text{WIF}\)** 个寄存器（级 1 到级 WRI+WIF）。
- 末级输出：**1** 个寄存器（锁存 `fxp_zoom` 结果）。

总级数（= 延迟 latency）

\[
\text{latency} = 1 + (\text{WRI}+\text{WIF}) + 1 = \text{WRI}+\text{WIF}+2
\]

又 \(\text{WRI}=\text{WTI}/2=\lceil \text{WII}/2\rceil\)（WII 为奇数时 WTI=WII+1，否则 WTI=WII），所以

\[
\boxed{\text{latency} = \lceil \text{WII}/2\rceil + \text{WIF} + 2}
\]

这与文件头注释 `pipeline stage = [WII/2]+WIF+2, [] means upper int`（[RTL/fixedpoint.v:L759](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L759)）完全一致。

#### 4.4.3 源码精读

**符号恢复 + fxp_zoom**（[RTL/fixedpoint.v:L835-L849](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L835-L849)）。`resushort` 是组合 `wire`，把最后一级的根按符号取补；`fxp_zoom` 把 `(WRI+1, WIF)` 收敛到 `(WOI, WOF)`：

```verilog
wire [WRI+WIF  :0] resushort = sign[WRI+WIF] ? (~resu[WRI+WIF][WRI+WIF:0])+ONER
                                               : resu[WRI+WIF][WRI+WIF:0];
wire [WOI+WOF-1:0] outl;
wire               overflowl;

fxp_zoom # (
    .WII      ( WRI+1          ),
    .WIF      ( WIF            ),
    .WOI      ( WOI            ),
    .WOF      ( WOF            ),
    .ROUND    ( ROUND          )
) res_zoom (
    .in       ( resushort      ),
    .out      ( outl           ),
    .overflow ( overflowl      )
);
```

对照单周期 [RTL/fixedpoint.v:L731-L744](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L731-L744)，二者除了「标量 resu/sign」换成「resu[WRI+WIF]/sign[WRI+WIF]」之外，包括 `fxp_zoom` 的参数 `(WRI+1, WIF) → (WOI, WOF)` 与 `.ROUND(ROUND)` 完全一致。这就是 4.3 节「算术内核不变」结论的末段体现——舍入与溢出饱和也由同一个 `fxp_zoom` 统一兜底（详见 [u1-l3](u1-l3-fxp-zoom.md)）。

**输出寄存**（[RTL/fixedpoint.v:L851-L855](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L851-L855)）——最后一级，把 `fxp_zoom` 的组合结果锁一拍：

```verilog
always @ (posedge clk or negedge rstn)
    if(~rstn)
        {overflow,out} <= 0;
    else
        {overflow,out} <= {overflowl,outl};
```

这正对应 [u3-l1](u3-l1-mul-pipe.md) 里确立的「组合结果寄存一拍」收尾范式（`fxp_mul_pipe` 末段也是这样）。

#### 4.4.4 代码实践（预测型）

**目标**：用测试平台现有的参数，亲手算出延迟，并预测输出节拍。

**步骤**：

1. 读 [SIM/tb_fxp_sqrt.v:L16-L19](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt.v#L16-L19) 的参数 WII=10, WIF=10。
2. 算 latency = \(\lceil 10/2\rceil+10+2\)。
3. 激励在 [SIM/tb_fxp_sqrt.v:L81](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt.v#L81) 给出第一个有效输入 `ival='hf0d77`。预测这个输入对应的 `oval2`（流水线输出）会在第几个 cycle 打印出来。

**预期结果**：latency = 5+10+2 = **17**。`oval1`（单周期）与 `ival` 同拍出现；`oval2`（流水线）要晚 17 拍。也就是说 `ival='hf0d77` 的开方结果，会在 `oval2` 列里滞后 17 行才打印——这正是「无气泡但有固定延迟」的直观体现。**待本地验证**：运行 `tb_fxp_sqrt_run_iverilog.bat` 后核对这 17 拍的滞后。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `fxp_zoom` 的输入整数位宽是 `WRI+1` 而不是 `WRI`？

<details><summary>参考答案</summary>
`resu` 是无符号的根幅值，格式 `(WRI, WIF)`。符号恢复后 `resushort` 可能是负数，需要一位符号位，所以传给 `fxp_zoom` 的整数位宽是 `WRI+1`，让 `fxp_zoom` 把它当作有符号数处理并做符号扩展/饱和。这与单周期版本 [RTL/fixedpoint.v:L735](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L735) 完全一致。</details>

**练习 2**：把 `fxp_sqrt_pipe` 与 [u3-l1](u3-l1-mul-pipe.md) 的 `fxp_mul_pipe` 对比，为什么前者级数远多于后者？

<details><summary>参考答案</summary>
`fxp_mul_pipe` 的组合核心是「一个乘法器 + 一个 fxp_zoom」两大块，自然切分只需 2 级；而 `fxp_sqrt_pipe` 的组合核心是「逐位试探的 for 循环」，每一位试探都要独占一级才能切断依赖链，所以级数 = 迭代数 WRI+WIF（再加首尾两级）。这正是「块切分」与「循环展开」两种流水线手法的差别。</details>

---

## 5. 综合实践：搭建延迟对齐的自校验验证环境

**任务**：在 [SIM/tb_fxp_sqrt.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt.v) 已有的「同屏打印 oval1/oval2」结构上，改造成**逐拍自动比对**的自校验 testbench，证明 `fxp_sqrt_pipe` 与单周期 `fxp_sqrt` 功能等价、且延迟正好是 \(\lceil \text{WII}/2\rceil+\text{WIF}+2\) 拍。

**思路**：

- `fxp_sqrt` 是组合逻辑，`oval1` 与 `ival` **同拍**有效，可直接当作黄金参考。
- `fxp_sqrt_pipe` 的 `oval2` 要晚 `LATENCY = WRI+WIF+2` 拍。所以把 `oval1`（连同当时的 `overflow1`）送进一个深度为 `LATENCY` 的移位寄存器队列，对齐到 `oval2` 的节拍，再逐拍比较。
- 用 `pass`/`fail` 两个计数器统计，仿真结束时 `$display` 汇总，要求 `fail==0`。

**可参考的最小骨架**（示例代码，需你补全并与现有 tb 的端口/参数衔接）：

```verilog
// LATENCY 由参数算出：WRI+WIF+2
localparam WTI = (WII%2==1) ? WII+1 : WII;
localparam WRI = WTI/2;
localparam LATENCY = WRI + WIF + 2;

// 延迟队列：把单周期参考 oval1/overflow1 延迟 LATENCY 拍
reg [WOI+WOF-1:0] ref_q  [0:LATENCY-1];
reg               ref_ov [0:LATENCY-1];
integer k;

always @(posedge clk or negedge rstn)
    if(~rstn) begin
        for(k=0;k<LATENCY;k=k+1) begin ref_q[k]<=0; ref_ov[k]<=0; end
    end else begin
        ref_q[0]  <= (rstn ? oval1 : 0);   // 与 oval1 同拍采，作黄金参考
        ref_ov[0] <= (rstn ? overflow1 : 1'b0);
        for(k=1;k<LATENCY;k=k+1) begin
            ref_q[k]  <= ref_q[k-1];
            ref_ov[k] <= ref_ov[k-1];
        end
    end

// 比对：队列最末端 = 延迟 LATENCY 拍的参考，与 oval2 同节拍
integer pass=0, fail=0;
always @(posedge clk)
    if(rstn) begin
        if(ref_q[LATENCY-1] !== oval2 || ref_ov[LATENCY-1] !== overflow2) begin
            fail <= fail + 1;
            $display("MISMATCH ref=%h ov=%b   pipe=%h ov=%b",
                     ref_q[LATENCY-1], ref_ov[LATENCY-1], oval2, overflow2);
        end else
            pass <= pass + 1;
    end

initial begin
    // ...原有激励...
    $display("PASS=%0d FAIL=%0d", pass, fail);
    $finish;
end
```

**操作步骤**：

1. 复制 `SIM/tb_fxp_sqrt.v` 为 `SIM/tb_fxp_sqrt_check.v`（只读 `RTL/fixedpoint.v`，不要改源码）。
2. 在新 tb 里加入上面的延迟队列与比对逻辑，保留原有的 `fxp_sqrt` + `fxp_sqrt_pipe` 例化与激励。
3. 新建对应的 `_run_iverilog.bat`，命令照搬 [SIM/tb_fxp_sqrt_run_iverilog.bat](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt_run_iverilog.bat)：`iverilog -g2001 -o sim.out tb_fxp_sqrt_check.v ../RTL/fixedpoint.v` 再 `vvp -n sim.out`。
4. 运行，观察终端的 `MISMATCH` 行与最后的 `PASS=.. FAIL=..`。

**需要观察的现象 / 预期结果**：

- 复位释放、且第一批输入填满流水线（约 LATENCY 拍）之后，不应再出现 `MISMATCH`。
- 汇总行应显示 `FAIL=0`（`PASS` 数 ≈ 有效激励拍数 − LATENCY）。
- 若出现大量 `MISMATCH`，最常见原因是 `LATENCY` 算错或延迟队列对齐差一拍——回看 4.4 的级数推导再核对。

> 说明：本实践为「待本地验证」——上述骨架需要你按本机 iverilog 环境补全并运行确认。能拿到 `FAIL=0` 即证明 `fxp_sqrt_pipe` 与 `fxp_sqrt` 功能等价、延迟为 \(\lceil \text{WII}/2\rceil+\text{WIF}+2\) 拍。

## 6. 本讲小结

- `fxp_sqrt_pipe` 的存在动机是：单周期 `fxp_sqrt` 的逐位试探串联成 \(\text{WRI}+\text{WIF}\) 级长组合链，关键路径过深、\(f_{\max}\) 受限。
- 核心改造手法是「标量循环变量 → 级间寄存器数组」：把 `sign/inu/resu/resu2` 从标量变成下标为级号的数组，配 `jj=WRI-1-ii` 把算法位序映射成流水线级序。
- 非阻塞赋值 `<=` 让所有级在同一个时钟沿「读旧值、写新值」，自然形成一条逐级前移的流水线，无气泡。
- 每一级的算术内核与单周期 `for` 循环体逐行相同（算 `resu2tmp`、比较 `inu`、条件置位），只是数据存取方式不同——这是「流水线版与单周期版数学等价」的保证。
- 末级 = 符号恢复（`resushort`）+ `fxp_zoom` 收敛到 `(WOI,WOF)`（舍入+溢出饱和）+ 再寄存一拍输出。
- 总级数 = \(\lceil \text{WII}/2\rceil+\text{WIF}+2\)，与文件注释一致；这是延迟对齐验证的依据。

## 7. 下一步学习建议

- 继续往浮点互转推进：[u3-l4](u3-l4-float-convert.md) 讲单周期 `fxp2float`/`float2fxp`，[u3-l5](u3-l5-float-convert-pipe.md) 把同样的「循环展开 + 级间数组」手法用到浮点转换的流水线版本——届时你会再次看到 `exp/inu/sign` 等数组与本讲 `resu/resu2/sign` 数组的同构性。
- 想巩固流水线验证方法学，可读 [u3-l6](u3-l6-simulation-testbench.md) 的自校验 testbench 总结，把本讲综合实践的 pass/fail 套路系统化。
- 建议回到源码横向对比三条「循环展开型」流水线：`fxp_div_pipe`（[RTL/fixedpoint.v:L513](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L513)）、`fxp_sqrt_pipe`（[RTL/fixedpoint.v:L762](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L762)）、`fxp2float_pipe`（[RTL/fixedpoint.v:L939](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L939)），体会它们共用同一套「数组 + 非阻塞逐级前移」范式的美感。
